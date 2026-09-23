"""Bounded durable enrollment for knowledge-base rebuilds."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cairn.catalog.models import DocumentIngestion, KbIndexVersion, KnowledgeBase
from cairn.catalog.repository import CatalogRepository
from cairn.core.db import transaction
from cairn.core.time import utcnow
from cairn.tasks.dto import TaskContext, TaskResult, TaskSpec
from cairn.tasks.service import TaskLeaseLostError, TaskService, get_task_service
from cairn.tasks.worker import TaskWorker

__all__ = ["ReindexFanoutService", "activate_build_if_ready", "register_reindex_handlers"]

_INDEX_RETENTION = timedelta(hours=24)


async def activate_build_if_ready(
    session: AsyncSession,
    *,
    kb: KnowledgeBase,
    version: KbIndexVersion,
    repository: CatalogRepository,
    tasks: TaskService,
) -> bool:
    if kb.building_index_version != version.version or version.state != "building":
        return False
    if version.enrollment_state != "complete":
        return False
    if (
        kb.active_index_version is not None
        and await repository.live_document_count(session, kb.id) == 0
    ):
        await fail_owned_build(
            session,
            kb=kb,
            version=version,
            repository=repository,
            tasks=tasks,
            error="Reindex abandoned because the knowledge base has no live documents.",
        )
        return False
    if (
        await repository.missing_current_ingestions(session, kb.id, version.version) != 0
        or await repository.deleted_target_point_count(session, kb.id, version.version) != 0
    ):
        return False

    total = await repository.ingestion_point_total(
        session, kb.id, version.version, indexed_only=False
    )
    previous = kb.active_index_version
    kb.active_index_version = version.version
    kb.building_index_version = None
    kb.status = "active"
    kb.config_version += 1
    kb.chunk_count = total
    kb.last_indexed_at = utcnow()
    await repository.set_index_state(session, kb.id, version.version, "active")
    if previous is not None and previous != version.version:
        retire_after = utcnow() + _INDEX_RETENTION
        await repository.set_index_state(
            session, kb.id, previous, "retired", retire_after=retire_after
        )
        await tasks.enqueue(
            session,
            TaskSpec(
                queue="maintain",
                kind="index.drop",
                workspace_id=kb.workspace_id,
                kb_id=kb.id,
                payload={"index_version": previous},
                priority=10,
                run_after=retire_after,
                dedupe_key=f"index.drop:{kb.id}:{previous}",
            ),
        )
    return True


async def fail_owned_build(
    session: AsyncSession,
    *,
    kb: KnowledgeBase,
    version: KbIndexVersion,
    repository: CatalogRepository,
    tasks: TaskService,
    error: str,
) -> None:
    if kb.building_index_version != version.version or version.state != "building":
        return
    kb.building_index_version = None
    kb.status = "active" if kb.active_index_version is not None else "error"
    await repository.set_index_state(session, kb.id, version.version, "failed", error=error[:2000])
    await tasks.enqueue(
        session,
        TaskSpec(
            queue="maintain",
            kind="index.drop",
            workspace_id=kb.workspace_id,
            kb_id=kb.id,
            payload={"index_version": version.version},
            priority=10,
            dedupe_key=f"index.drop:{kb.id}:{version.version}",
        ),
    )


class ReindexFanoutService:
    def __init__(
        self,
        *,
        batch_size: int = 100,
        repository: CatalogRepository | None = None,
        tasks: TaskService | None = None,
        publish_runtime: Callable[[UUID], Awaitable[None]] | None = None,
    ) -> None:
        if not 1 <= batch_size <= 1000:
            raise ValueError("batch_size must be between 1 and 1000")
        self.batch_size = batch_size
        self._repository = repository or CatalogRepository()
        self._tasks = tasks or get_task_service()
        self._publish_runtime = publish_runtime

    async def handle(self, context: TaskContext) -> TaskResult:
        identity = _fanout_identity(context)
        if identity is None or context.kb_id is None:
            return TaskResult.skipped("invalid reindex identity")
        index_version, expected_generation = identity
        if await self._is_active_publication_retry(
            context, index_version=index_version, expected_generation=expected_generation
        ):
            await self._publish(context.kb_id)
            return TaskResult.success("republished active reindex target")
        enrolled = 0
        successor = False
        activated = False
        async with transaction() as session:
            await self._tasks.lock_owned_task(session, context)
            observed = await self._repository.get_index_version(
                session, context.kb_id, index_version
            )
            if observed is None or observed.state != "building":
                return TaskResult.skipped("reindex version is no longer building")
            observed_state = observed.enrollment_state
            observed_cursor = observed.enrollment_cursor
            observed_generation = observed.enrollment_generation
            if observed_generation != expected_generation:
                return TaskResult.skipped("stale enrollment generation")

            if observed_state == "scanning":
                selected_ids = await self._repository.fanout_document_ids(
                    session,
                    context.kb_id,
                    after=observed_cursor,
                    limit=self.batch_size,
                )
            elif observed_state == "reconciling":
                selected_ids = await self._repository.missing_enrollment_document_ids(
                    session, context.kb_id, index_version, limit=self.batch_size
                )
            else:
                return TaskResult.skipped("enrollment is already complete")

            documents = await self._repository.lock_documents(session, selected_ids)
            kb = await self._repository.get_kb(session, context.kb_id, for_update=True)
            if (
                kb is None
                or kb.workspace_id != context.workspace_id
                or kb.status in {"deleting", "archived"}
                or kb.building_index_version != index_version
            ):
                return TaskResult.skipped("reindex no longer owns the building version")
            version = await self._repository.get_index_version(
                session, context.kb_id, index_version, for_update=True
            )
            if (
                version is None
                or version.state != "building"
                or version.enrollment_generation != expected_generation
                or version.enrollment_state != observed_state
            ):
                return TaskResult.skipped("reindex enrollment state changed")

            for document in documents:
                if (
                    document.kb_id != kb.id
                    or document.deleted_at is not None
                    or document.state == "deleting"
                ):
                    continue
                inserted = await self._repository.enroll_document_ingestion(
                    session,
                    DocumentIngestion(
                        document_id=document.id,
                        revision=document.revision,
                        kb_id=kb.id,
                        index_version=index_version,
                        source_content_hash=document.content_hash,
                        prior_parsed_object_key=await self._repository.reusable_parsed_object_key(
                            session,
                            document.id,
                            document.revision,
                            document.content_hash,
                            exclude_index_version=index_version,
                        ),
                    ),
                )
                if not inserted:
                    continue
                enrolled += 1
                await self._tasks.enqueue(
                    session,
                    TaskSpec(
                        queue="parse",
                        kind="document.parse",
                        workspace_id=document.workspace_id,
                        kb_id=kb.id,
                        document_id=document.id,
                        correlation_id=context.correlation_id,
                        payload={
                            "revision": document.revision,
                            "index_version": index_version,
                        },
                        dedupe_key=(
                            f"document.parse:{document.id}:{document.revision}:{index_version}"
                        ),
                    ),
                )

            version.enrollment_generation += 1
            if version.enrollment_state == "scanning" and selected_ids:
                version.enrollment_cursor = max(selected_ids)
                successor = True
            elif version.enrollment_state == "scanning":
                version.enrollment_state = "reconciling"
                successor = True
            elif selected_ids:
                successor = True
            else:
                version.enrollment_state = "complete"
                activated = await activate_build_if_ready(
                    session,
                    kb=kb,
                    version=version,
                    repository=self._repository,
                    tasks=self._tasks,
                )

            if successor:
                await self._tasks.enqueue(
                    session,
                    TaskSpec(
                        queue="maintain",
                        kind="kb.reindex_fanout",
                        workspace_id=kb.workspace_id,
                        kb_id=kb.id,
                        correlation_id=context.correlation_id,
                        payload={
                            "index_version": index_version,
                            "enrollment_generation": version.enrollment_generation,
                        },
                        priority=50,
                        dedupe_key=(
                            f"kb.reindex:{kb.id}:{index_version}:{version.enrollment_generation}"
                        ),
                    ),
                )
            if not await _still_owned(session, context):
                raise TaskLeaseLostError(context.task_id)

        if enrolled:
            await self._tasks.notify("parse")
        if successor:
            await self._tasks.notify("maintain")
        if activated:
            await self._publish(context.kb_id)
        return TaskResult.success(
            f"enrolled={enrolled} successor={int(successor)} activated={int(activated)}"
        )

    async def _is_active_publication_retry(
        self,
        context: TaskContext,
        *,
        index_version: int,
        expected_generation: int,
    ) -> bool:
        assert context.kb_id is not None
        async with transaction() as session:
            await self._tasks.lock_owned_task(session, context)
            version = await self._repository.get_index_version(
                session, context.kb_id, index_version
            )
            if (
                version is None
                or version.state != "active"
                or version.enrollment_state != "complete"
                or version.enrollment_generation != expected_generation + 1
            ):
                return False
            kb = await self._repository.get_kb(session, context.kb_id)
            if (
                kb is None
                or kb.workspace_id != context.workspace_id
                or kb.active_index_version != index_version
            ):
                return False
            if not await _still_owned(session, context):
                raise TaskLeaseLostError(context.task_id)
        return True

    async def _publish(self, kb_id: UUID) -> None:
        if self._publish_runtime is not None:
            await self._publish_runtime(kb_id)
            return
        from cairn.catalog.service import get_catalog_service

        await get_catalog_service().publish_runtime(kb_id)


def register_reindex_handlers(
    worker: TaskWorker, service: ReindexFanoutService | None = None
) -> None:
    worker.register("kb.reindex_fanout", (service or ReindexFanoutService()).handle)


def _fanout_identity(context: TaskContext) -> tuple[int, int] | None:
    index_version = context.payload.get("index_version")
    generation = context.payload.get("enrollment_generation", 0)
    if (
        type(index_version) is not int
        or type(generation) is not int
        or index_version < 1
        or generation < 0
    ):
        return None
    return index_version, generation


async def _still_owned(session: AsyncSession, context: TaskContext) -> bool:
    return bool(
        await session.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM task WHERE id=:task_id AND state='running' "
                "AND worker_id=:worker_id AND attempt=:attempt "
                "AND lease_until > clock_timestamp())"
            ),
            {
                "task_id": context.task_id,
                "worker_id": context.worker_id,
                "attempt": context.attempt,
            },
        )
    )
