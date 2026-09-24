"""Resumable parse, chunk, embed, and index task handlers."""

from __future__ import annotations

import asyncio
import json
import unicodedata
from base64 import urlsafe_b64encode
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import cast
from uuid import UUID, uuid4

from cairn.catalog.dto import BindingRef, ChunkReembedTarget, ChunkSpec
from cairn.catalog.ingestion import (
    CatalogIngestionFacade,
    IngestionOwnershipError,
    IngestionRun,
    source_key_in_scope,
)
from cairn.core.errors import CairnError
from cairn.core.logging import get_logger
from cairn.core.modelref import ModelRef
from cairn.embedding.service import EmbeddingService
from cairn.embedding.sparse import encode_document
from cairn.embedding.tokenizers import Tokenizer
from cairn.ingestion.artifacts import (
    ChunkManifest,
    EmbeddingManifest,
    ParseManifest,
    decode_chunk_manifest,
    decode_embedding_manifest,
    decode_parse_manifest,
    encode_chunk_manifest,
    encode_embedding_manifest,
    encode_parse_manifest,
)
from cairn.ingestion.base import ParseContext, ParsedDocument
from cairn.ingestion.chunkers import DocumentChunker
from cairn.ingestion.custom import CustomChunkExecutor
from cairn.ingestion.registry import ParserRegistry
from cairn.ingestion.semantic import EmbeddingServiceSemanticEmbedding
from cairn.objectstore.base import ObjectStore
from cairn.objectstore.errors import ObjectNotFound, ObjectTooLarge
from cairn.tasks.dto import TaskContext, TaskResult
from cairn.tasks.service import TaskLeaseLostError
from cairn.tasks.worker import TaskWorker
from cairn.vectorstore.base import Hit, Metric, Namespace, NamespaceSpec, Point, VectorStore
from cairn.vectorstore.filters import Compare

log = get_logger(__name__)

_MAX_PARENT_SNAPSHOT_CHARACTERS = 1_000_000
_MAX_PARENT_SNAPSHOT_TOKENS = 200_000
_MAX_PARENT_SNAPSHOT_METADATA_BYTES = 512 * 1024


class EmbeddingInputTooLarge(CairnError):
    code = "EMBED_INPUT_TOO_LARGE"
    http_status = 422
    title = "A chunk exceeds the embedding model's input limit."
    retryable = False


class SourceIntegrityError(CairnError):
    code = "SOURCE_INTEGRITY_MISMATCH"
    http_status = 409
    title = "The stored source does not match the registered document."
    retryable = False


class PipelineArtifactError(CairnError):
    code = "INGESTION_ARTIFACT_INVALID"
    http_status = 500
    title = "A saved ingestion stage could not be verified."
    retryable = False


@dataclass(frozen=True, slots=True)
class PreparedEmbedding:
    model: ModelRef
    tokenizer: Tokenizer
    service: EmbeddingService
    binding_fingerprint: str


ObjectStoreResolver = Callable[[BindingRef], Awaitable[ObjectStore]]
VectorStoreResolver = Callable[[BindingRef], Awaitable[VectorStore]]
EmbeddingResolver = Callable[[ModelRef], Awaitable[PreparedEmbedding]]
CustomExecutorResolver = Callable[[IngestionRun], Awaitable[CustomChunkExecutor]]


@dataclass(frozen=True, slots=True)
class PipelineResources:
    parsers: ParserRegistry
    object_store_for: ObjectStoreResolver
    vector_store_for: VectorStoreResolver
    embedding_for: EmbeddingResolver
    max_source_bytes: int = 50 * 1024 * 1024
    max_artifact_bytes: int = 256 * 1024 * 1024
    custom_executor_for: CustomExecutorResolver | None = None


class IngestionPipeline:
    def __init__(self, *, catalog: CatalogIngestionFacade, resources: PipelineResources) -> None:
        self._catalog = catalog
        self._resources = resources

    async def handle_parse(self, context: TaskContext) -> TaskResult:
        try:
            run = await self._catalog.load_run(context)
            if run is None or run.state != "registered":
                return TaskResult.skipped("stale or already parsed")
            if not source_key_in_scope(
                run.object_key, workspace_id=run.workspace_id, kb_id=run.kb_id
            ):
                raise SourceIntegrityError()
            await self._prepared_embedding(run)
            store = await self._resources.object_store_for(run.object_binding)
            parsed = await self._reuse_parsed(run, store)
            if parsed is None:
                source = await store.get_bytes(
                    run.object_key, max_bytes=self._resources.max_source_bytes
                )
                self._verify_source(run, source)
                parsed = await self._resources.parsers.parse(
                    source,
                    mime=run.mime_type,
                    ctx=ParseContext(source_url=run.source_url),
                )
            await context.heartbeat()
            manifest = ParseManifest(
                document_id=run.document_id,
                revision=run.revision,
                index_version=run.index_version,
                source_content_hash=run.source_content_hash,
                parsed=parsed,
            )
            key = _stage_artifact_key(run, context, "parsed")
            await store.put(key, encode_parse_manifest(manifest), content_type="application/json")
            advanced = await self._catalog.commit_parsed(
                context, parsed_object_key=key, page_count=parsed.pages
            )
            return (
                TaskResult.success() if advanced else TaskResult.skipped("stage already committed")
            )
        except TaskLeaseLostError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return await self._failed(context, "parse", exc)

    async def handle_chunk(self, context: TaskContext) -> TaskResult:
        try:
            run = await self._catalog.load_run(context)
            if run is None or run.state != "parsed" or run.parsed_object_key is None:
                return TaskResult.skipped("stale or already chunked")
            store = await self._resources.object_store_for(run.object_binding)
            parsed = decode_parse_manifest(
                await store.get_bytes(
                    run.parsed_object_key, max_bytes=self._resources.max_artifact_bytes
                )
            )
            _verify_manifest_identity(run, parsed)
            prepared = await self._prepared_embedding(run)
            semantic = (
                EmbeddingServiceSemanticEmbedding(
                    prepared.service, prepared.model, prepared.binding_fingerprint
                )
                if run.chunk_config.strategy == "semantic"
                else None
            )
            custom = (
                await self._resources.custom_executor_for(run)
                if run.chunk_config.strategy == "custom"
                and self._resources.custom_executor_for is not None
                else None
            )
            chunks = await DocumentChunker(
                document_id=run.document_id,
                tokenizer=prepared.tokenizer,
                index_version=run.index_version,
                semantic=semantic,
                custom=custom,
                max_input_tokens=prepared.model.max_input_tokens,
            ).chunk(parsed.parsed, run.chunk_config)
            await context.heartbeat()
            edits = await self._catalog.preserved_edits(context)
            edited_ordinals = set(edits)
            chunks = [
                replace(
                    chunk,
                    content=edits[chunk.ordinal][0],
                    content_hash=edits[chunk.ordinal][1],
                    token_count=prepared.tokenizer.count(edits[chunk.ordinal][0]),
                )
                if chunk.ordinal in edits
                else chunk
                for chunk in chunks
            ]
            chunks = _attach_parent_snapshots(run, chunks, edited_ordinals=edited_ordinals)
            existing = set(await self._catalog.existing_point_ids(context))
            expected = {
                chunk.id for chunk in chunks if chunk.metadata.get("embed", True) is not False
            }
            stale = tuple(sorted(existing - expected, key=str))
            manifest = ChunkManifest(
                document_id=run.document_id,
                revision=run.revision,
                index_version=run.index_version,
                source_content_hash=run.source_content_hash,
                chunks=tuple(chunks),
                stale_point_ids=stale,
            )
            key = _stage_artifact_key(run, context, "chunks")
            await store.put(key, encode_chunk_manifest(manifest), content_type="application/json")
            advanced = await self._catalog.commit_chunked(
                context,
                chunks_object_key=key,
                chunks=chunks,
                stale_point_ids=stale,
            )
            return (
                TaskResult.success() if advanced else TaskResult.skipped("stage already committed")
            )
        except TaskLeaseLostError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return await self._failed(context, "chunk", exc)

    async def handle_embed(self, context: TaskContext) -> TaskResult:
        try:
            run = await self._catalog.load_run(context)
            if run is None or run.state != "chunked" or run.chunks_object_key is None:
                return TaskResult.skipped("stale or already embedded")
            store = await self._resources.object_store_for(run.object_binding)
            chunks = decode_chunk_manifest(
                await store.get_bytes(
                    run.chunks_object_key, max_bytes=self._resources.max_artifact_bytes
                )
            )
            _verify_manifest_identity(run, chunks)
            prepared = await self._prepared_embedding(run)
            previous = await self._previous_embeddings(run, store)
            manifest = await create_embedding_manifest(
                document_id=run.document_id,
                revision=run.revision,
                index_version=run.index_version,
                source_content_hash=run.source_content_hash,
                chunks=chunks.chunks,
                model=prepared.model,
                binding_fingerprint=prepared.binding_fingerprint,
                service=prepared.service,
                previous=previous,
            )
            await context.heartbeat()
            key = _stage_artifact_key(run, context, "embeddings")
            await store.put(
                key, encode_embedding_manifest(manifest), content_type="application/json"
            )
            advanced = await self._catalog.commit_embedded(context, embeddings_object_key=key)
            return (
                TaskResult.success() if advanced else TaskResult.skipped("stage already committed")
            )
        except TaskLeaseLostError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return await self._failed(context, "embed", exc)

    async def handle_chunk_reembed(self, context: TaskContext) -> TaskResult:
        try:
            target = await self._catalog.load_chunk_reembed(context)
            if target is None:
                return TaskResult.skipped("stale manual chunk edit")
            prepared = await self._resources.embedding_for(target.embedding_model)
            if prepared.model != target.embedding_model:
                raise PipelineArtifactError(
                    "The registered embedding model no longer matches the index version snapshot."
                )
            max_tokens = prepared.model.max_input_tokens
            if max_tokens is None:
                raise PipelineArtifactError("The embedding model has no input token limit.")
            embedding_token_count, response_token_count = _manual_reembed_token_counts(
                prepared.tokenizer, target.content
            )
            if embedding_token_count > max_tokens:
                raise EmbeddingInputTooLarge()
            vector_store = await self._resources.vector_store_for(target.vector_binding)
            namespace = Namespace(target.kb_id, target.index_version)
            expected_payload = _chunk_reembed_payload(
                target, actual_token_count=response_token_count
            )
            existing = await vector_store.fetch(namespace, [target.chunk_id])
            vector: Sequence[float] | None = None
            if not _chunk_reembed_hit_matches(existing, target, expected_payload):
                embedded = await prepared.service.embed_documents(
                    prepared.model, [target.content], batch_size=1
                )
                vector = embedded[0].values
            async with self._catalog.chunk_reembed_mutation(context, target) as mutation:
                if vector is not None:
                    point = _chunk_reembed_point(
                        target, vector, actual_token_count=response_token_count
                    )
                    await mutation.apply(lambda: vector_store.upsert(namespace, [point]))
                fetched = await mutation.apply(
                    lambda: vector_store.fetch(namespace, [target.chunk_id])
                )
                if not _chunk_reembed_hit_matches(fetched, target, expected_payload):
                    raise PipelineArtifactError(
                        "The vector store returned a stale manual chunk payload."
                    )
                await mutation.commit(embedding_token_count)
            return TaskResult.success()
        except IngestionOwnershipError:
            return TaskResult.skipped("stale manual chunk edit")
        except TaskLeaseLostError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return _manual_reembed_failure(exc)

    async def handle_index(self, context: TaskContext) -> TaskResult:
        try:
            run = await self._catalog.load_run(context)
            if run is None:
                return TaskResult.skipped("stale or already indexed")
            if run.state == "indexed":
                if run.active_index_version == run.index_version:
                    await self._catalog.publish_runtime(run.kb_id)
                    await self._catalog.confirm_published(context)
                return TaskResult.skipped("stage already committed")
            if (
                run.state != "embedded"
                or run.chunks_object_key is None
                or run.embeddings_object_key is None
            ):
                return TaskResult.skipped("stale or already indexed")
            object_store = await self._resources.object_store_for(run.object_binding)
            vector_store = await self._resources.vector_store_for(run.vector_binding)
            chunks = decode_chunk_manifest(
                await object_store.get_bytes(
                    run.chunks_object_key, max_bytes=self._resources.max_artifact_bytes
                )
            )
            embeddings = decode_embedding_manifest(
                await object_store.get_bytes(
                    run.embeddings_object_key, max_bytes=self._resources.max_artifact_bytes
                )
            )
            _verify_manifest_identity(run, chunks)
            _verify_manifest_identity(run, embeddings)
            prepared = await self._prepared_embedding(run)
            if (
                embeddings.binding_fingerprint != prepared.binding_fingerprint
                or embeddings.dimension != prepared.model.dimension
                or embeddings.normalized != prepared.model.normalize
            ):
                raise PipelineArtifactError("The embedding binding changed before indexing.")
            points = _points(run, chunks, embeddings)
            namespace = Namespace(run.kb_id, run.index_version)
            async with self._catalog.index_mutation(context) as mutation:
                await mutation.apply(
                    lambda: vector_store.ensure_namespace(
                        namespace,
                        NamespaceSpec(dim=run.embedding_dim, metric=cast("Metric", run.metric)),
                    )
                )
                await mutation.apply(lambda: vector_store.upsert(namespace, points))
                await _verify_expected(vector_store, namespace, points)
                if chunks.stale_point_ids:
                    await mutation.apply(
                        lambda: vector_store.delete(namespace, ids=chunks.stale_point_ids)
                    )
                    if await vector_store.fetch(namespace, chunks.stale_point_ids):
                        raise PipelineArtifactError("Stale vector points remain after deletion.")
                count = await vector_store.count(
                    namespace, Compare("document_id", "$eq", str(run.document_id))
                )
                if count != len(points):
                    raise PipelineArtifactError("The vector index contains a partial document.")
                committed = await mutation.commit(len(points))
            if committed.activated:
                await self._catalog.publish_runtime(run.kb_id)
                await self._catalog.confirm_published(context)
            return (
                TaskResult.success()
                if committed.advanced
                else TaskResult.skipped("stage already committed")
            )
        except TaskLeaseLostError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return await self._failed(context, "index", exc)

    async def handle_reindex_delete(self, context: TaskContext) -> TaskResult:
        try:
            if await self._catalog.is_deleted_activation_publication_retry(context):
                assert context.kb_id is not None
                await self._catalog.publish_runtime(context.kb_id)
                return TaskResult.success("republished active reindex target")
            async with self._catalog.deleted_index_mutation(context) as mutation:
                target = mutation.target
                vector_store = await self._resources.vector_store_for(target.vector_binding)
                namespace = Namespace(target.kb_id, target.index_version)
                await mutation.apply(
                    lambda: vector_store.ensure_namespace(
                        namespace,
                        NamespaceSpec(
                            dim=target.embedding_dim,
                            metric=cast("Metric", target.metric),
                        ),
                    )
                )
                document_filter = Compare("document_id", "$eq", str(target.document_id))
                await mutation.apply(lambda: vector_store.delete(namespace, filter=document_filter))
                remaining = await mutation.apply(
                    lambda: vector_store.count(namespace, document_filter)
                )
                if remaining:
                    raise PipelineArtifactError(
                        "Deleted document points remain in the building index."
                    )
                committed = await mutation.commit()
            if committed.activated:
                await self._catalog.publish_runtime(target.kb_id)
            return TaskResult.success()
        except IngestionOwnershipError:
            return TaskResult.skipped("stale deleted-document cleanup")
        except TaskLeaseLostError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return await self._failed(context, "index", exc)

    def _verify_source(self, run: IngestionRun, source: bytes) -> None:
        if (
            not source_key_in_scope(run.object_key, workspace_id=run.workspace_id, kb_id=run.kb_id)
            or sha256(source).hexdigest() != run.source_content_hash
        ):
            raise SourceIntegrityError()

    async def _prepared_embedding(self, run: IngestionRun) -> PreparedEmbedding:
        prepared = await self._resources.embedding_for(run.embedding_model)
        if prepared.model != run.embedding_model:
            raise PipelineArtifactError(
                "The registered embedding model no longer matches the index version snapshot."
            )
        return prepared

    async def _reuse_parsed(self, run: IngestionRun, store: ObjectStore) -> ParsedDocument | None:
        key = run.prior_parsed_object_key
        if key is None or not source_key_in_scope(
            key, workspace_id=run.workspace_id, kb_id=run.kb_id
        ):
            return None
        try:
            manifest = decode_parse_manifest(
                await store.get_bytes(key, max_bytes=self._resources.max_artifact_bytes)
            )
        except (OSError, ValueError, ObjectNotFound, ObjectTooLarge):
            return None
        if (
            manifest.document_id != run.document_id
            or manifest.revision != run.revision
            or manifest.source_content_hash != run.source_content_hash
        ):
            return None
        return manifest.parsed

    async def _previous_embeddings(
        self, run: IngestionRun, store: ObjectStore
    ) -> EmbeddingManifest | None:
        if run.prior_embeddings_object_key is None:
            return None
        try:
            previous = decode_embedding_manifest(
                await store.get_bytes(
                    run.prior_embeddings_object_key,
                    max_bytes=self._resources.max_artifact_bytes,
                )
            )
        except (ValueError, OSError):
            return None
        return previous if previous.document_id == run.document_id else None

    async def _failed(self, context: TaskContext, stage: str, exc: Exception) -> TaskResult:
        if isinstance(exc, CairnError):
            code = exc.code
            detail = exc.title
            retryable = bool(getattr(exc, "retryable", False))
        elif isinstance(exc, ValueError):
            code = PipelineArtifactError.code
            detail = PipelineArtifactError.title
            retryable = False
        else:
            code = "INGESTION_STAGE_ERROR"
            detail = "The ingestion stage could not be completed."
            retryable = True
        try:
            await self._catalog.record_failure(
                context,
                stage=stage,
                error_code=code,
                detail=detail,
                terminal=not retryable or context.is_final_attempt,
            )
        except TaskLeaseLostError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as record_exc:
            log.warning(
                "ingestion.failure_record_failed",
                stage=stage,
                error=type(record_exc).__name__,
            )
        return TaskResult.failure(code, detail, retryable=retryable)


def register_pipeline_handlers(worker: TaskWorker, pipeline: IngestionPipeline) -> None:
    handlers = {
        "parse": ("document.parse", pipeline.handle_parse),
        "chunk": ("document.chunk", pipeline.handle_chunk),
        "embed": ("document.embed", pipeline.handle_embed),
        "index": ("document.index", pipeline.handle_index),
    }
    registration = handlers.get(worker.queue)
    if registration is not None:
        worker.register(*registration)
    if worker.queue == "embed":
        worker.register("chunk.reembed", pipeline.handle_chunk_reembed)
    if worker.queue == "index":
        worker.register("document.reindex_delete", pipeline.handle_reindex_delete)


def _stage_artifact_key(run: IngestionRun, context: TaskContext, stage: str) -> str:
    nonce = urlsafe_b64encode(uuid4().bytes).rstrip(b"=").decode("ascii")
    attempt_key = f"{context.task_id:x}-{context.attempt:x}-{nonce}"
    return f"{run.artifact_prefix}/{attempt_key}/{stage}.json"


def _verify_manifest_identity(
    run: IngestionRun, manifest: ParseManifest | ChunkManifest | EmbeddingManifest
) -> None:
    if (
        manifest.document_id != run.document_id
        or manifest.revision != run.revision
        or manifest.index_version != run.index_version
        or manifest.source_content_hash != run.source_content_hash
    ):
        raise PipelineArtifactError("The artifact belongs to another document revision.")


def _points(
    run: IngestionRun,
    chunks: ChunkManifest,
    embeddings: EmbeddingManifest,
) -> list[Point]:
    points: list[Point] = []
    for chunk in chunks.chunks:
        if chunk.metadata.get("embed", True) is False:
            continue
        values = embeddings.vectors.get(chunk.content_hash)
        if values is None:
            raise PipelineArtifactError("An expected chunk embedding is missing.")
        payload = dict(chunk.metadata)
        payload.update(
            {
                "chunk_id": str(chunk.id),
                "content": chunk.content,
                "content_hash": chunk.content_hash,
                "token_count": chunk.token_count,
                "document_id": str(run.document_id),
                "index_version": run.index_version,
                "kb_id": str(run.kb_id),
                "parent_id": str(chunk.parent_id) if chunk.parent_id is not None else None,
                "revision": run.revision,
            }
        )
        points.append(
            Point(
                id=chunk.id,
                dense=values,
                payload=payload,
                sparse=encode_document(chunk.content),
            )
        )
    return points


def _attach_parent_snapshots(
    run: IngestionRun,
    chunks: Sequence[ChunkSpec],
    *,
    edited_ordinals: set[int],
) -> list[ChunkSpec]:
    """Copy immutable parent context into indexed children before persistence."""
    by_id = {chunk.id: chunk for chunk in chunks}
    enriched: list[ChunkSpec] = []
    for chunk in chunks:
        if chunk.document_id != run.document_id:
            raise PipelineArtifactError("A chunk belongs to another document.")
        metadata = dict(chunk.metadata)
        if chunk.ordinal in edited_ordinals:
            metadata["_manual_edit_preserved"] = True
        if chunk.parent_id is not None:
            parent = by_id.get(chunk.parent_id)
            if (
                parent is None
                or parent.document_id != run.document_id
                or parent.parent_id is not None
                or parent.metadata.get("embed", True) is not False
                or not parent.content
                or len(parent.content) > _MAX_PARENT_SNAPSHOT_CHARACTERS
                or not 0 <= parent.token_count <= _MAX_PARENT_SNAPSHOT_TOKENS
            ):
                raise PipelineArtifactError("A child chunk has an invalid parent snapshot.")
            metadata["_parent_snapshot"] = {
                "id": str(parent.id),
                "content": parent.content,
                "token_count": parent.token_count,
                "document_id": str(run.document_id),
                "kb_id": str(run.kb_id),
                "index_version": run.index_version,
                "revision": run.revision,
                "metadata": _parent_snapshot_metadata(parent.metadata),
            }
        enriched.append(replace(chunk, metadata=metadata))
    return enriched


def _parent_snapshot_metadata(metadata: Mapping[str, object]) -> dict[str, object]:
    selected = {
        key: deepcopy(metadata[key])
        for key in ("language", "heading_path", "page", "source_url", "citations")
        if key in metadata
    }
    try:
        encoded = json.dumps(
            selected,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError, RecursionError) as exc:
        raise PipelineArtifactError("Parent citation metadata is invalid.") from exc
    if len(encoded) > _MAX_PARENT_SNAPSHOT_METADATA_BYTES:
        raise PipelineArtifactError("Parent citation metadata exceeds its safety bound.")
    return selected


async def _verify_expected(
    store: VectorStore, namespace: Namespace, points: Sequence[Point]
) -> None:
    expected = {point.id: point for point in points}
    fetched = await store.fetch(namespace, list(expected))
    if {hit.id for hit in fetched} != set(expected):
        raise PipelineArtifactError("The vector store did not persist every expected point.")
    for hit in fetched:
        if hit.payload.get("content_hash") != expected[hit.id].payload.get("content_hash"):
            raise PipelineArtifactError("The vector store returned stale point payloads.")


def _chunk_reembed_payload(
    target: ChunkReembedTarget, *, actual_token_count: int | None = None
) -> dict[str, object]:
    payload: dict[str, object] = deepcopy(target.metadata)
    payload.update(
        {
            "chunk_id": str(target.chunk_id),
            "content": target.content,
            "content_hash": target.content_hash,
            "document_id": str(target.document_id),
            "index_version": target.index_version,
            "kb_id": str(target.kb_id),
            "parent_id": str(target.parent_id) if target.parent_id is not None else None,
            "revision": target.revision,
            "manual_edit_generation": target.edit_generation,
        }
    )
    if actual_token_count is not None:
        payload["token_count"] = actual_token_count
    return payload


def _manual_reembed_token_counts(tokenizer: Tokenizer, content: str) -> tuple[int, int]:
    normalized = " ".join(unicodedata.normalize("NFKC", content).split())
    return tokenizer.count(normalized), tokenizer.count(content)


def _chunk_reembed_point(
    target: ChunkReembedTarget,
    values: Sequence[float],
    *,
    actual_token_count: int | None = None,
) -> Point:
    return Point(
        id=target.chunk_id,
        dense=values,
        payload=_chunk_reembed_payload(target, actual_token_count=actual_token_count),
        sparse=encode_document(target.content),
    )


def _chunk_reembed_hit_matches(
    hits: Sequence[Hit],
    target: ChunkReembedTarget,
    expected_payload: Mapping[str, object],
) -> bool:
    return bool(
        len(hits) == 1
        and hits[0].id == target.chunk_id
        and dict(hits[0].payload) == dict(expected_payload)
    )


def _manual_reembed_failure(exc: Exception) -> TaskResult:
    if isinstance(exc, CairnError):
        return TaskResult.failure(
            exc.code,
            exc.title,
            retryable=bool(getattr(exc, "retryable", False)),
        )
    if isinstance(exc, ValueError):
        return TaskResult.failure(
            PipelineArtifactError.code,
            PipelineArtifactError.title,
            retryable=False,
        )
    return TaskResult.failure(
        "INGESTION_STAGE_ERROR",
        "The ingestion stage could not be completed.",
        retryable=True,
    )


async def create_embedding_manifest(
    *,
    document_id: UUID,
    revision: int,
    index_version: int,
    source_content_hash: str,
    chunks: Sequence[ChunkSpec],
    model: ModelRef,
    binding_fingerprint: str,
    service: EmbeddingService,
    previous: EmbeddingManifest | None = None,
) -> EmbeddingManifest:
    reusable: dict[str, tuple[float, ...]] = {}
    if (
        previous is not None
        and previous.binding_fingerprint == binding_fingerprint
        and previous.dimension == model.dimension
        and previous.normalized == model.normalize
    ):
        reusable.update(previous.vectors)

    searchable = [chunk for chunk in chunks if chunk.metadata.get("embed", True) is not False]
    max_tokens = model.max_input_tokens
    if max_tokens is None:
        raise ValueError("embedding model has no input token limit")
    for chunk in searchable:
        normalized = " ".join(unicodedata.normalize("NFKC", chunk.content).split())
        if chunk.token_count > max_tokens or service.count_tokens(model, normalized) > max_tokens:
            raise EmbeddingInputTooLarge()

    missing: dict[str, str] = {}
    for chunk in searchable:
        if chunk.content_hash not in reusable:
            missing.setdefault(chunk.content_hash, chunk.content)
    if missing:
        hashes = list(missing)
        vectors = await service.embed_documents(model, [missing[key] for key in hashes])
        for content_hash, vector in zip(hashes, vectors, strict=True):
            reusable[content_hash] = vector.values

    return EmbeddingManifest(
        document_id=document_id,
        revision=revision,
        index_version=index_version,
        source_content_hash=source_content_hash,
        binding_fingerprint=binding_fingerprint,
        dimension=model.dimension or 0,
        normalized=model.normalize,
        vectors={chunk.content_hash: reusable[chunk.content_hash] for chunk in searchable},
    )
