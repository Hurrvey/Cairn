"""Resumable parse, chunk, embed, and index task handlers."""

from __future__ import annotations

import asyncio
import unicodedata
from base64 import urlsafe_b64encode
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import cast
from uuid import UUID, uuid4

from cairn.catalog.dto import BindingRef, ChunkSpec
from cairn.catalog.ingestion import CatalogIngestionFacade, IngestionRun, source_key_in_scope
from cairn.core.errors import CairnError
from cairn.core.logging import get_logger
from cairn.core.modelref import ModelRef
from cairn.embedding.service import EmbeddingService
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
from cairn.ingestion.base import ParseContext
from cairn.ingestion.chunkers import DocumentChunker
from cairn.ingestion.custom import CustomChunkExecutor
from cairn.ingestion.registry import ParserRegistry
from cairn.ingestion.semantic import EmbeddingServiceSemanticEmbedding
from cairn.objectstore.base import ObjectStore
from cairn.tasks.dto import TaskContext, TaskResult
from cairn.tasks.service import TaskLeaseLostError
from cairn.tasks.worker import TaskWorker
from cairn.vectorstore.base import Metric, Namespace, NamespaceSpec, Point, VectorStore
from cairn.vectorstore.filters import Compare

log = get_logger(__name__)


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
EmbeddingResolver = Callable[[UUID], Awaitable[PreparedEmbedding]]
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
            store = await self._resources.object_store_for(run.object_binding)
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
            prepared = await self._resources.embedding_for(run.embedding_model_id)
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
            ).chunk(parsed.parsed, run.chunk_config)
            await context.heartbeat()
            edits = await self._catalog.preserved_edits(context)
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
            prepared = await self._resources.embedding_for(run.embedding_model_id)
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
            prepared = await self._resources.embedding_for(run.embedding_model_id)
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

    def _verify_source(self, run: IngestionRun, source: bytes) -> None:
        if (
            not source_key_in_scope(run.object_key, workspace_id=run.workspace_id, kb_id=run.kb_id)
            or sha256(source).hexdigest() != run.source_content_hash
        ):
            raise SourceIntegrityError()

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
                "document_id": str(run.document_id),
                "index_version": run.index_version,
                "kb_id": str(run.kb_id),
                "parent_id": str(chunk.parent_id) if chunk.parent_id is not None else None,
                "revision": run.revision,
            }
        )
        points.append(Point(id=chunk.id, dense=values, payload=payload))
    return points


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
