"""Catalog runtime repair and destructive maintenance handlers."""

from __future__ import annotations

import asyncio
from base64 import urlsafe_b64encode
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from functools import partial
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cairn.catalog.ingestion import source_key_in_scope
from cairn.catalog.repository import CatalogRepository
from cairn.catalog.service import CatalogService
from cairn.core.config import get_settings
from cairn.core.db import get_engine, session_scope, transaction
from cairn.core.logging import get_logger
from cairn.core.time import utcnow
from cairn.modelgw.catalog import ModelCatalog, get_model_catalog
from cairn.objectstore.base import ObjectStore
from cairn.objectstore.keys import ObjectKeys
from cairn.objectstore.registry import ObjectBindingRef, get_object_registry
from cairn.tasks.dto import TaskContext, TaskResult
from cairn.tasks.service import TaskLeaseLostError, get_task_service
from cairn.tasks.worker import TaskWorker
from cairn.vectorstore.base import Namespace, VectorStore
from cairn.vectorstore.filters import Compare
from cairn.vectorstore.registry import VectorBindingRef, get_vector_registry

__all__ = [
    "RuntimeRefreshScheduler",
    "build_runtime_refresh_scheduler",
    "register_catalog_maintenance_handlers",
]

_EXTERNAL_TIMEOUT_S = 30.0
_RUNTIME_REFRESH_LOCK_ID = 0x434149524E525446
log = get_logger(__name__)

LeaderCheck = Callable[[], Awaitable[bool]]


class RuntimeRefreshScheduler:
    def __init__(
        self,
        *,
        refresh: Callable[[int], Awaitable[None]],
        interval_s: float,
        batch_size: int,
        leader_lock: Callable[[], object],
    ) -> None:
        self._refresh = refresh
        self._interval_s = interval_s
        self._batch_size = batch_size
        self._leader_lock = leader_lock

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                lock = self._leader_lock()
                async with lock as leadership:  # type: ignore[attr-defined]
                    if not leadership:
                        await _wait_or_stop(stop, self._interval_s)
                        continue
                    while not stop.is_set():
                        if callable(leadership) and not await leadership():
                            break
                        try:
                            await self._refresh(self._batch_size)
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            log.warning("catalog.runtime_refresh_failed", error=type(exc).__name__)
                        await _wait_or_stop(stop, self._interval_s)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("catalog.runtime_refresh_leader_failed", error=type(exc).__name__)
                await _wait_or_stop(stop, self._interval_s)


async def _wait_or_stop(stop: asyncio.Event, delay: float) -> None:
    with suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=delay)


class RuntimeRefresher:
    def __init__(
        self,
        *,
        repository: CatalogRepository | None = None,
        catalog: CatalogService | None = None,
        models: ModelCatalog | None = None,
    ) -> None:
        self._repository = repository or CatalogRepository()
        self._catalog = catalog or CatalogService(repository=self._repository)
        self._models = models or get_model_catalog()

    async def refresh(self, batch_size: int) -> None:
        provider_after: UUID | None = None
        while True:
            provider_ids = await self._models.runtime_provider_ids(
                after=provider_after, limit=batch_size
            )
            for provider_id in provider_ids:
                try:
                    await self._models.publish_provider_runtime(provider_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning(
                        "catalog.runtime_refresh_provider_failed",
                        provider_id=str(provider_id),
                        error=type(exc).__name__,
                    )
            if len(provider_ids) < batch_size:
                break
            provider_after = provider_ids[-1]
            await asyncio.sleep(0)
        after: UUID | None = None
        while True:
            async with session_scope() as session:
                kb_ids = await self._repository.runtime_refresh_ids(
                    session, after=after, limit=batch_size
                )
            for kb_id in kb_ids:
                try:
                    await self._catalog.publish_runtime(kb_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning(
                        "catalog.runtime_refresh_kb_failed",
                        kb_id=str(kb_id),
                        error=type(exc).__name__,
                    )
            if len(kb_ids) < batch_size:
                return
            after = kb_ids[-1]
            await asyncio.sleep(0)


@asynccontextmanager
async def _postgres_refresh_leader() -> AsyncIterator[LeaderCheck | None]:
    connection = await get_engine().connect()
    acquired = False
    try:
        acquired = bool(
            await connection.scalar(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": _RUNTIME_REFRESH_LOCK_ID},
            )
        )
        await connection.commit()
        if not acquired:
            yield None
            return

        async def still_leader() -> bool:
            alive = bool(await connection.scalar(text("SELECT true")))
            await connection.commit()
            return alive

        yield still_leader
    finally:
        if acquired:
            with suppress(Exception):
                await connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": _RUNTIME_REFRESH_LOCK_ID},
                )
                await connection.commit()
        await connection.close()


def build_runtime_refresh_scheduler() -> RuntimeRefreshScheduler:
    settings = get_settings().tasks
    refresher = RuntimeRefresher()
    return RuntimeRefreshScheduler(
        refresh=refresher.refresh,
        interval_s=settings.runtime_refresh_interval_s,
        batch_size=settings.runtime_refresh_batch_size,
        leader_lock=_postgres_refresh_leader,
    )


async def _lease_budget(
    session: AsyncSession, context: TaskContext, timeout_s: float = _EXTERNAL_TIMEOUT_S
) -> float:
    remaining = await session.scalar(
        text(
            "SELECT extract(epoch FROM lease_until - clock_timestamp()) FROM task "
            "WHERE id=:task_id AND state='running' AND worker_id=:worker_id "
            "AND attempt=:attempt AND lease_until > clock_timestamp()"
        ),
        {
            "task_id": context.task_id,
            "worker_id": context.worker_id,
            "attempt": context.attempt,
        },
    )
    if remaining is None or float(remaining) <= 0.1:
        raise TaskLeaseLostError(context.task_id)
    return min(timeout_s, float(remaining) - 0.05)


async def _external[T](
    session: AsyncSession,
    context: TaskContext,
    operation: Callable[[], Awaitable[T]],
) -> T:
    async with asyncio.timeout(await _lease_budget(session, context)):
        return await operation()


def _document_id(context: TaskContext) -> UUID | None:
    raw = context.payload.get("document_id")
    try:
        parsed = UUID(raw) if isinstance(raw, str) else None
    except ValueError:
        return None
    if parsed is None or parsed != context.document_id:
        return None
    return parsed


async def _delete_prefix(
    session: AsyncSession,
    context: TaskContext,
    store: ObjectStore,
    prefix: str,
) -> None:
    await _external(session, context, lambda: store.delete_prefix(prefix))
    page = await _external(session, context, lambda: store.list(prefix, limit=1))
    if page.items:
        raise RuntimeError(f"objects remain under cleanup prefix {prefix!r}")


async def _delete_key(
    session: AsyncSession,
    context: TaskContext,
    store: ObjectStore,
    key: str,
) -> None:
    await _external(session, context, lambda: store.delete(key))
    if await _external(session, context, lambda: store.head(key)) is not None:
        raise RuntimeError(f"object remains after cleanup at {key!r}")


async def handle_document_purge(context: TaskContext) -> TaskResult:
    document_id = _document_id(context)
    if document_id is None or context.kb_id is None:
        return TaskResult.skipped("invalid document purge identity")
    await context.heartbeat()
    repository = CatalogRepository()
    tasks = get_task_service()
    async with asyncio.timeout(_EXTERNAL_TIMEOUT_S), transaction() as session:
        document = await repository.get_document(session, document_id, for_update=True)
        if (
            document is None
            or document.deleted_at is None
            or document.state != "deleting"
            or document.workspace_id != context.workspace_id
            or document.kb_id != context.kb_id
        ):
            return TaskResult.skipped("document is no longer purgeable")
        revision = document.revision
        runs = await repository.lock_document_ingestions(session, document_id)
        kb = await repository.get_kb_for_maintenance(session, context.kb_id, for_update=True)
        if kb is None or kb.workspace_id != context.workspace_id or document.revision != revision:
            return TaskResult.skipped("document purge ownership changed")
        await repository.lock_index_versions(session, kb.id)
        if not await tasks.drain_ready_for_document(
            session, document_id=document_id, exclude_task_id=context.task_id
        ):
            return TaskResult.failure("CLEANUP_BUSY", "document work is still running")
        vector_binding = await repository.get_binding(session, kb.vector_binding_id)
        object_binding = await repository.get_binding(session, kb.object_binding_id)
        if vector_binding is None or object_binding is None:
            raise RuntimeError("document cleanup binding is unavailable")
        vector_store: VectorStore = await get_vector_registry().for_binding(
            VectorBindingRef(
                vector_binding.id, vector_binding.driver, dict(vector_binding.config or {})
            )
        )
        object_store: ObjectStore = await get_object_registry().for_binding(
            ObjectBindingRef(
                object_binding.id, object_binding.driver, dict(object_binding.config or {})
            )
        )
        document_filter = Compare("document_id", "$eq", str(document_id))
        namespaces = await _external(session, context, partial(vector_store.list_namespaces, kb.id))
        for namespace in namespaces:
            if not await _external(
                session, context, partial(vector_store.namespace_exists, namespace)
            ):
                continue
            await _external(
                session,
                context,
                partial(vector_store.delete, namespace, filter=document_filter),
            )
            remaining = await _external(
                session,
                context,
                partial(vector_store.count, namespace, document_filter),
            )
            if remaining:
                raise RuntimeError("document vectors remain after cleanup")

        kb_prefix = ObjectKeys.kb_prefix(document.workspace_id, document.kb_id)
        compact_id = urlsafe_b64encode(document.id.bytes).rstrip(b"=").decode("ascii")
        prefixes = {
            f"{kb_prefix}parsed/{document.id}/",
            f"{kb_prefix}assets/{document.id}/",
            f"{kb_prefix}i/{compact_id}/",
        }
        for prefix in sorted(prefixes):
            await _delete_prefix(session, context, object_store, prefix)

        source_candidates: dict[str, str | None] = {}
        if document.object_key is not None:
            source_candidates[document.object_key] = document.content_hash
        for run in runs:
            source_candidates[
                ObjectKeys.original(document.workspace_id, document.kb_id, run.source_content_hash)
            ] = run.source_content_hash
        for key, content_hash in sorted(source_candidates.items()):
            if not source_key_in_scope(
                key, workspace_id=document.workspace_id, kb_id=document.kb_id
            ):
                raise RuntimeError("document cleanup source key is outside KB scope")
            if not await repository.source_is_referenced_by_live_document(
                session,
                kb_id=document.kb_id,
                document_id=document.id,
                object_key=key,
                content_hash=content_hash,
            ):
                await _delete_key(session, context, object_store, key)

        explicit_artifacts = {
            key
            for key in (
                document.parsed_object_key,
                *(run.parsed_object_key for run in runs),
                *(run.chunks_object_key for run in runs),
                *(run.embeddings_object_key for run in runs),
            )
            if key is not None
        }
        for key in sorted(explicit_artifacts):
            if not source_key_in_scope(
                key, workspace_id=document.workspace_id, kb_id=document.kb_id
            ):
                raise RuntimeError("document cleanup artifact key is outside KB scope")
            await _delete_key(session, context, object_store, key)

        await _lease_budget(session, context)
        if document.revision != revision or document.deleted_at is None:
            return TaskResult.skipped("document purge identity changed")
        await repository.finalize_document_purge(session, document=document, kb=kb)
    return TaskResult.success("document purged")


async def handle_kb_purge(context: TaskContext) -> TaskResult:
    if context.kb_id is None or context.payload.get("kb_id") != str(context.kb_id):
        return TaskResult.skipped("invalid knowledge-base purge identity")
    await context.heartbeat()
    repository = CatalogRepository()
    tasks = get_task_service()
    async with asyncio.timeout(_EXTERNAL_TIMEOUT_S), transaction() as session:
        kb = await repository.get_kb_for_maintenance(session, context.kb_id, for_update=True)
        if kb is None:
            return TaskResult.skipped("knowledge base is already purged")
        if (
            kb.workspace_id != context.workspace_id
            or kb.status != "deleting"
            or kb.deleted_at is None
        ):
            return TaskResult.skipped("knowledge base is not purgeable")
        await repository.lock_index_versions(session, kb.id)
        if not await tasks.drain_ready_for_kb(
            session, kb_id=kb.id, exclude_task_id=context.task_id
        ):
            return TaskResult.failure("CLEANUP_BUSY", "knowledge-base work is still running")
        vector_binding = await repository.get_binding(session, kb.vector_binding_id)
        object_binding = await repository.get_binding(session, kb.object_binding_id)
        if vector_binding is None or object_binding is None:
            raise RuntimeError("knowledge-base cleanup binding is unavailable")
        vector_store = await get_vector_registry().for_binding(
            VectorBindingRef(
                vector_binding.id, vector_binding.driver, dict(vector_binding.config or {})
            )
        )
        object_store = await get_object_registry().for_binding(
            ObjectBindingRef(
                object_binding.id, object_binding.driver, dict(object_binding.config or {})
            )
        )
        namespaces = await _external(session, context, partial(vector_store.list_namespaces, kb.id))
        for namespace in namespaces:
            if await _external(session, context, partial(vector_store.namespace_exists, namespace)):
                await _external(session, context, partial(vector_store.drop_namespace, namespace))
            if await _external(session, context, partial(vector_store.namespace_exists, namespace)):
                raise RuntimeError("knowledge-base namespace remains after cleanup")
        await _delete_prefix(
            session,
            context,
            object_store,
            ObjectKeys.kb_prefix(kb.workspace_id, kb.id),
        )
        await _lease_budget(session, context)
        await CatalogService(repository=repository).invalidate_runtime(kb.id)
        from cairn.authz.service import AuthzService

        await AuthzService().purge_knowledge_base_access(kb.workspace_id, kb.id)
        await repository.finalize_kb_purge(session, kb)
    return TaskResult.success("knowledge base purged")


async def handle_index_drop(context: TaskContext) -> TaskResult:
    raw_version = context.payload.get("index_version")
    if context.kb_id is None or type(raw_version) is not int or raw_version < 1:
        return TaskResult.skipped("invalid index-drop identity")
    await context.heartbeat()
    repository = CatalogRepository()
    async with asyncio.timeout(_EXTERNAL_TIMEOUT_S), transaction() as session:
        kb = await repository.get_kb_for_maintenance(session, context.kb_id, for_update=True)
        if kb is None:
            return TaskResult.skipped("knowledge base is already purged")
        version = await repository.get_index_version(session, kb.id, raw_version, for_update=True)
        if version is None:
            return TaskResult.skipped("index version is already purged")
        if (
            kb.workspace_id != context.workspace_id
            or version.workspace_id != context.workspace_id
            or raw_version in {kb.active_index_version, kb.building_index_version}
        ):
            return TaskResult.skipped("index version still owns a live pointer")
        if version.state == "retired":
            if version.retire_after is None or version.retire_after > utcnow():
                return TaskResult.failure(
                    "INDEX_RETENTION_ACTIVE", "retired index retention has not elapsed"
                )
        elif version.state != "failed":
            return TaskResult.skipped("index version is not droppable")
        binding = await repository.get_binding(session, kb.vector_binding_id)
        if (
            binding is None
            or binding.workspace_id != context.workspace_id
            or binding.kind != "vector"
        ):
            raise RuntimeError("index-drop vector binding is unavailable")
        vector_store = await get_vector_registry().for_binding(
            VectorBindingRef(binding.id, binding.driver, dict(binding.config or {}))
        )
        namespace = Namespace(kb.id, raw_version)
        if await _external(session, context, lambda: vector_store.namespace_exists(namespace)):
            await _external(session, context, lambda: vector_store.drop_namespace(namespace))
        if await _external(session, context, lambda: vector_store.namespace_exists(namespace)):
            raise RuntimeError("index namespace remains after drop")
        await _lease_budget(session, context)
        if raw_version in {kb.active_index_version, kb.building_index_version}:
            return TaskResult.skipped("index pointer changed before finalization")
        await repository.finalize_index_drop(session, kb.id, raw_version)
    return TaskResult.success("index dropped")


def register_catalog_maintenance_handlers(worker: TaskWorker) -> None:
    worker.register("document.purge", handle_document_purge)
    worker.register("kb.purge", handle_kb_purge)
    worker.register("index.drop", handle_index_drop)
