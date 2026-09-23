"""Authorization-first orchestration for the bounded retrieval endpoint."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from cairn.authz.model import Principal
from cairn.core.config import get_settings
from cairn.core.errors import InvalidId, PermissionDenied, UpstreamUnavailable, ValidationFailed
from cairn.core.ids import InvalidIdError, decode_id, encode_id
from cairn.core.modelref import ModelRef
from cairn.core.retrieval_runtime import BindingRefModel, KnowledgeBaseRuntime
from cairn.retrieval.dto import (
    DegradationNotice,
    RetrievalHit,
    RetrievalRequest,
    RetrievalResponse,
    SourceInfo,
    TargetFailure,
    UsageInfo,
)
from cairn.retrieval.errors import (
    KbRuntimeCorrupt,
    RetrievalReadModelInvalid,
    RetrievalUnsupported,
)
from cairn.retrieval.fusion import FusedHit, rrf_fuse, weighted_fuse
from cairn.retrieval.rerank import RerankUnavailable, RuntimeReranker
from cairn.retrieval.runtime import KnowledgeBaseRuntimeLoader
from cairn.vectorstore.base import Hit, Namespace, VectorQuery
from cairn.vectorstore.registry import VectorBindingRef, VectorStoreRegistry, get_vector_registry

_MAX_PARENT_SNAPSHOT_CHARACTERS = 1_000_000
_MAX_PARENT_SNAPSHOT_TOKENS = 200_000
_MAX_PARENT_CITATIONS = 1000


class QueryEmbeddings(Protocol):
    async def embed_query(
        self, model: ModelRef, query: str
    ) -> tuple[Sequence[float], int, bool | None]: ...


class Reranker(Protocol):
    def supports(self, model_id: str | None) -> bool: ...

    async def score(
        self,
        workspace_id: UUID,
        model_id: str,
        query: str,
        documents: Sequence[str],
        *,
        timeout_s: float,
    ) -> list[float]: ...


@dataclass(frozen=True, slots=True)
class _RerankPlan:
    model_id: str
    timeout_s: float
    top_n: int
    candidate_limit: int


class UnconfiguredQueryEmbeddings:
    async def embed_query(
        self, model: ModelRef, query: str
    ) -> tuple[Sequence[float], int, bool | None]:
        raise RetrievalUnsupported(
            "Query embedding is not configured for this ACTIVE model provider."
        )


class RetrievalService:
    def __init__(
        self,
        *,
        runtime_loader: KnowledgeBaseRuntimeLoader | object | None = None,
        vectors: VectorStoreRegistry | object | None = None,
        embeddings: QueryEmbeddings | None = None,
        reranker: Reranker | None = None,
        search_timeout_s: float = 2.0,
        request_timeout_s: float = 5.0,
    ) -> None:
        self._runtime_loader = runtime_loader or KnowledgeBaseRuntimeLoader()
        self._vectors = vectors or get_vector_registry()
        self._embeddings = embeddings or UnconfiguredQueryEmbeddings()
        self._reranker = reranker
        self._search_timeout_s = search_timeout_s
        self._request_timeout_s = request_timeout_s

    async def query(
        self, principal: Principal, request: RetrievalRequest, *, request_id: str
    ) -> RetrievalResponse:
        try:
            async with asyncio.timeout(self._request_timeout_s):
                return await self._query(principal, request, request_id=request_id)
        except TimeoutError as exc:
            raise UpstreamUnavailable("The retrieval request exceeded its time limit.") from exc

    async def _query(
        self, principal: Principal, request: RetrievalRequest, *, request_id: str
    ) -> RetrievalResponse:
        started = time.perf_counter()
        targets = self._authorize_targets(principal, request)
        runtime_outcomes = await asyncio.gather(
            *(
                self._runtime_loader.load_one(kb_id, principal.workspace_id)  # type: ignore[attr-defined]
                for kb_id, _weight in targets
            ),
            return_exceptions=True,
        )
        active: list[tuple[KnowledgeBaseRuntime, float]] = []
        failures: list[TargetFailure] = []
        for (kb_id, weight), outcome in zip(targets, runtime_outcomes, strict=True):
            if isinstance(outcome, (PermissionDenied, KbRuntimeCorrupt)):
                raise outcome
            if isinstance(outcome, BaseException):
                failures.append(self._target_failure(kb_id, "KB_RUNTIME_UNAVAILABLE"))
            else:
                active.append((outcome, weight))
        if failures and request.strict:
            raise UpstreamUnavailable("Retrieval failed for one or more knowledge bases.")
        if not active:
            raise UpstreamUnavailable("Retrieval failed for every knowledge base.")

        runtimes = [runtime for runtime, _weight in active]
        degraded = self._validate_supported(request, runtimes)
        rerank_plan = self._resolve_rerank(request, runtimes, degraded)
        (
            query_vectors,
            embedding_tokens,
            cached_embedding,
            embedding_failures,
        ) = await self._query_vectors(request, runtimes)
        if embedding_failures:
            failed_ids = set(embedding_failures)
            failures.extend(
                self._target_failure(kb_id, "EMBEDDING_PROVIDER_UNAVAILABLE")
                for kb_id in embedding_failures
            )
            active = [
                (runtime, weight) for runtime, weight in active if runtime.id not in failed_ids
            ]
            if request.strict:
                raise UpstreamUnavailable("Retrieval failed for one or more knowledge bases.")
            if not active:
                raise UpstreamUnavailable("Retrieval failed for every knowledge base.")

        tasks = [
            self._search_target(runtime, weight, request, query_vectors.get(runtime.id))
            for runtime, weight in active
        ]
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        fused: list[FusedHit] = []
        for (runtime, _weight), outcome in zip(active, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                failures.append(self._target_failure(runtime.id, "STORAGE_BINDING_UNAVAILABLE"))
            else:
                fused.extend(outcome)

        if failures and request.strict:
            raise UpstreamUnavailable("Retrieval failed for one or more knowledge bases.")
        if not fused and len(failures) == len(targets):
            raise UpstreamUnavailable("Retrieval failed for every knowledge base.")

        fused.sort(key=lambda item: (-item.score, item.kb_id.int, item.hit.id.int))
        threshold = request.score_threshold
        if threshold is not None:
            fused = [item for item in fused if item.score >= threshold]
        rerank_units = 0
        rerank_top_n: int | None = None
        if rerank_plan is not None:
            fused, rerank_units, rerank_top_n = await self._rerank_fused(
                fused,
                request,
                runtimes[0].workspace_id,
                rerank_plan,
                degraded,
            )
        top_k = request.top_k or min(runtime.retrieval_config.top_k for runtime in runtimes)
        if rerank_top_n is not None:
            top_k = min(top_k, rerank_top_n)
        hits, expansion_notices = self._assemble_results(fused, request, runtimes)
        degraded.extend(expansion_notices)
        hits = hits[:top_k]
        hits, truncated = self._apply_token_budget(hits, request.options.max_context_tokens)
        total_ms = round((time.perf_counter() - started) * 1000)
        return RetrievalResponse(
            request_id=request_id,
            results=hits,
            usage=UsageInfo(
                embedding_tokens=embedding_tokens,
                rerank_units=rerank_units,
                cached_embedding=cached_embedding,
                index_versions={
                    encode_id("kb", runtime.id): runtime.index_version for runtime in runtimes
                },
                latency_ms={"total": total_ms},
            ),
            degraded=degraded,
            partial_failures=failures,
            truncated_to_token_budget=truncated,
        )

    def _target_failure(self, kb_id: UUID, code: str) -> TargetFailure:
        details = {
            "KB_RUNTIME_UNAVAILABLE": "The knowledge base runtime is unavailable.",
            "EMBEDDING_PROVIDER_UNAVAILABLE": "The query embedding provider is unavailable.",
            "STORAGE_BINDING_UNAVAILABLE": "The vector store is unavailable.",
        }
        return TargetFailure(
            knowledge_base_id=encode_id("kb", kb_id),
            code=code,
            detail=details[code],
        )

    def _authorize_targets(
        self, principal: Principal, request: RetrievalRequest
    ) -> list[tuple[UUID, float]]:
        targets: list[tuple[UUID, float]] = []
        seen: set[UUID] = set()
        for target in request.targets:
            try:
                kb_id = decode_id("kb", target.knowledge_base_id)
            except InvalidIdError as exc:
                raise InvalidId("A retrieval target has an invalid knowledge base ID.") from exc
            if kb_id in seen:
                raise ValidationFailed("Retrieval targets must be unique.")
            seen.add(kb_id)
            targets.append((kb_id, target.weight))
        for kb_id, _weight in targets:
            if not principal.can("kb:query", kb_id):
                raise PermissionDenied("This API key requires kb:query on every target.")
        if not any(weight > 0 for _kb_id, weight in targets):
            raise ValidationFailed("At least one retrieval target weight must be non-zero.")
        return targets

    def _validate_supported(
        self, request: RetrievalRequest, runtimes: Sequence[KnowledgeBaseRuntime]
    ) -> list[DegradationNotice]:
        if request.filters is not None:
            raise RetrievalUnsupported("Filters are not supported by the first retrieval endpoint.")
        for runtime in runtimes:
            fusion = request.fusion or runtime.retrieval_config.fusion
            weights = request.weights or runtime.retrieval_config.weights
            if fusion.method == "rrf" and request.weights is not None:
                raise ValidationFailed("Search weights are incompatible with RRF fusion.")
            if fusion.method == "weighted" and weights.dense == 0 and weights.sparse == 0:
                raise ValidationFailed("Weighted fusion requires at least one non-zero weight.")
        options = request.options
        if options.include_highlights:
            raise RetrievalUnsupported("Highlights are not supported by this endpoint.")
        if options.dedupe not in {None, "none", "by_chunk"}:
            raise RetrievalUnsupported("Document deduplication is not supported by this endpoint.")
        if options.mmr is not None and options.mmr.enabled:
            raise RetrievalUnsupported("MMR is not supported by this endpoint.")
        if options.explain:
            raise RetrievalUnsupported("Explain mode is not supported by this endpoint.")

        notices: dict[str, DegradationNotice] = {}
        for runtime in runtimes:
            config = runtime.retrieval_config
            if (
                request.search_mode is None
                and request.query_vector is not None
                and config.search_mode == "hybrid"
            ):
                notices["sparse"] = DegradationNotice(
                    stage="sparse",
                    reason="query_text_missing",
                    detail=(
                        "The hybrid default was reduced to vector search because no query text "
                        "was supplied."
                    ),
                )
            if options.mmr is None and config.mmr.enabled:
                notices["mmr"] = DegradationNotice(
                    stage="mmr",
                    reason="unsupported",
                    detail="Configured MMR is unavailable; fused ranking is returned.",
                )
            if options.dedupe is None and config.dedupe not in {"none", "by_chunk"}:
                notices["dedupe"] = DegradationNotice(
                    stage="dedupe",
                    reason="unsupported",
                    detail="Configured document deduplication is unavailable; chunks are returned.",
                )
        return list(notices.values())

    def _runtime_reranker(self) -> Reranker:
        if self._reranker is None:
            self._reranker = RuntimeReranker(get_settings().retrieval.rerank_endpoints)
        return self._reranker

    def _resolve_rerank(
        self,
        request: RetrievalRequest,
        runtimes: Sequence[KnowledgeBaseRuntime],
        degraded: list[DegradationNotice],
    ) -> _RerankPlan | None:
        explicit = request.rerank
        if explicit is not None and not explicit.enabled:
            return None
        configured = [runtime.retrieval_config.rerank for runtime in runtimes]
        if explicit is None and not any(spec.enabled for spec in configured):
            return None
        enabled = [spec for spec in configured if spec.enabled]
        override_model = (explicit.model_id or explicit.model) if explicit is not None else None
        configured_models = {spec.model_id for spec in enabled}
        if override_model is None and len(configured_models) > 1:
            raise ValidationFailed(
                "Configured rerank models differ across targets; request one model explicitly."
            )
        model_id = override_model or (next(iter(configured_models)) if configured_models else None)
        if request.query is None:
            if explicit is not None:
                raise ValidationFailed("Reranking requires query text.")
            degraded.append(
                DegradationNotice(
                    stage="rerank",
                    reason="query_missing",
                    detail="Configured reranking was skipped because no query text was supplied.",
                )
            )
            return None
        reranker = self._runtime_reranker()
        if model_id is None or not reranker.supports(model_id):
            if explicit is not None:
                raise RetrievalUnsupported("The requested rerank model is not configured.")
            degraded.append(
                DegradationNotice(
                    stage="rerank",
                    reason="unavailable",
                    detail="Configured reranking is unavailable; fused ranking is returned.",
                )
            )
            return None
        candidate_limit = min(
            1000,
            *(request.candidate_k or runtime.retrieval_config.candidate_k for runtime in runtimes),
        )
        if explicit is not None:
            timeout_s = explicit.timeout_s
            top_n = explicit.top_n
        else:
            timeout_s = min(spec.timeout_s for spec in enabled)
            top_n = min(spec.top_n for spec in enabled)
        return _RerankPlan(model_id, timeout_s, top_n, candidate_limit)

    async def _query_vectors(
        self, request: RetrievalRequest, runtimes: Sequence[KnowledgeBaseRuntime]
    ) -> tuple[dict[UUID, Sequence[float]], int, bool | None, list[UUID]]:
        modes = {runtime.id: self._mode(request, runtime) for runtime in runtimes}
        dense_runtimes = [
            runtime for runtime in runtimes if modes[runtime.id] in {"vector", "hybrid"}
        ]
        if not dense_runtimes:
            return {}, 0, None, []
        if request.query_vector is not None:
            identities = {runtime.embedding_model for runtime in dense_runtimes}
            if len(identities) != 1:
                raise ValidationFailed(
                    "A supplied query vector requires one embedding identity across all targets."
                )
            expected = dense_runtimes[0].embedding_model.dimension
            if expected is None or len(request.query_vector) != expected:
                raise ValidationFailed(
                    "The query vector dimension does not match the ACTIVE model dimension "
                    f"{expected}."
                )
            return {runtime.id: request.query_vector for runtime in dense_runtimes}, 0, None, []
        if request.query is None:
            raise ValidationFailed("Dense retrieval requires query text or a query vector.")

        vectors: dict[UUID, Sequence[float]] = {}
        tokens = 0
        cache_states: list[bool | None] = []
        by_model: dict[ModelRef, list[KnowledgeBaseRuntime]] = {}
        for runtime in dense_runtimes:
            by_model.setdefault(runtime.embedding_model, []).append(runtime)
        groups = list(by_model.items())
        outcomes = await asyncio.gather(
            *(
                self._embed_query(model_runtimes[0].workspace_id, model, request.query)
                for model, model_runtimes in groups
            ),
            return_exceptions=True,
        )
        failed: list[UUID] = []
        for (model, model_runtimes), outcome in zip(groups, outcomes, strict=True):
            if isinstance(outcome, RetrievalUnsupported):
                raise outcome
            if isinstance(outcome, BaseException):
                failed.extend(runtime.id for runtime in model_runtimes)
                continue
            vector, used_tokens, cached = outcome
            if len(vector) != model.dimension:
                failed.extend(runtime.id for runtime in model_runtimes)
                continue
            tokens += used_tokens
            cache_states.append(cached)
            for runtime in model_runtimes:
                vectors[runtime.id] = vector
        cached_embedding = (
            True
            if cache_states and all(state is True for state in cache_states)
            else False
            if any(state is False for state in cache_states)
            else None
        )
        return vectors, tokens, cached_embedding, failed

    async def _embed_query(
        self, workspace_id: UUID, model: ModelRef, query: str
    ) -> tuple[Sequence[float], int, bool | None]:
        workspace_method = getattr(self._embeddings, "embed_query_for_workspace", None)
        if workspace_method is not None:
            result: tuple[Sequence[float], int, bool | None] = await workspace_method(
                workspace_id, model, query
            )
            return result
        return await self._embeddings.embed_query(model, query)

    async def _search_target(
        self,
        runtime: KnowledgeBaseRuntime,
        target_weight: float,
        request: RetrievalRequest,
        dense: Sequence[float] | None,
    ) -> list[FusedHit]:
        mode = self._mode(request, runtime)
        candidate_k = request.candidate_k or runtime.retrieval_config.candidate_k
        top_k = request.top_k or runtime.retrieval_config.top_k
        candidate_k = max(candidate_k, top_k)
        binding = BindingRefModel.model_validate(runtime.vector_binding)
        store = await self._vectors.for_binding(  # type: ignore[attr-defined]
            VectorBindingRef(binding.id, binding.driver, binding.config)
        )
        namespace = Namespace(runtime.id, runtime.index_version)
        searches: list[tuple[str, asyncio.Task[list[Hit]], float]] = []
        if mode in {"vector", "hybrid"}:
            if dense is None:
                raise ValidationFailed("Dense retrieval has no query vector.")
            searches.append(
                (
                    "dense",
                    asyncio.create_task(
                        self._bounded_search(
                            store,
                            namespace,
                            VectorQuery(top_k=candidate_k, dense=dense, with_payload=True),
                        )
                    ),
                    target_weight,
                )
            )
        if mode in {"fulltext", "hybrid"}:
            if request.query is None:
                raise ValidationFailed("Full-text retrieval requires query text.")
            searches.append(
                (
                    "sparse",
                    asyncio.create_task(
                        self._bounded_search(
                            store,
                            namespace,
                            VectorQuery(top_k=candidate_k, text=request.query, with_payload=True),
                        )
                    ),
                    target_weight,
                )
            )
        try:
            results = await asyncio.gather(*(task for _stage, task, _weight in searches))
        except BaseException:
            for _stage, task, _weight in searches:
                task.cancel()
            await asyncio.gather(
                *(task for _stage, task, _weight in searches), return_exceptions=True
            )
            raise
        ranked = [
            (stage, self._validate_driver_hits(runtime, hits), weight)
            for (stage, _task, weight), hits in zip(searches, results, strict=True)
        ]
        fusion = request.fusion or runtime.retrieval_config.fusion
        if fusion.method == "weighted":
            weights = request.weights or runtime.retrieval_config.weights
            fused = weighted_fuse(
                runtime.id,
                ranked,
                weights={"dense": weights.dense, "sparse": weights.sparse},
            )
        elif len(ranked) == 1:
            stage, hits, weight = ranked[0]
            fused = [
                FusedHit(
                    kb_id=runtime.id,
                    hit=hit,
                    score=hit.score * weight,
                    scores={stage: hit.score},
                )
                for hit in hits
            ]
        else:
            fused = rrf_fuse(runtime.id, ranked, k=fusion.k)
        threshold = (
            request.score_threshold
            if request.score_threshold is not None
            else runtime.retrieval_config.score_threshold
        )
        return [item for item in fused if item.score >= threshold]

    async def _rerank_fused(
        self,
        fused: list[FusedHit],
        request: RetrievalRequest,
        workspace_id: UUID,
        plan: _RerankPlan,
        degraded: list[DegradationNotice],
    ) -> tuple[list[FusedHit], int, int | None]:
        candidates = fused[: plan.candidate_limit]
        if not candidates:
            return fused, 0, plan.top_n
        for item in candidates:
            item.scores.setdefault("fused", item.score)
        try:
            scores = await self._runtime_reranker().score(
                workspace_id,
                plan.model_id,
                request.query or "",
                [item.hit.content for item in candidates],
                timeout_s=plan.timeout_s,
            )
            if len(scores) != len(candidates) or not all(math.isfinite(score) for score in scores):
                raise RerankUnavailable("Rerank provider returned invalid scores.")
        except RerankUnavailable:
            degraded.append(
                DegradationNotice(
                    stage="rerank",
                    reason="unavailable",
                    detail="Reranking failed; fused ranking is returned.",
                )
            )
            return fused, 0, None
        rescored: list[tuple[float, FusedHit]] = []
        for item, score in zip(candidates, scores, strict=True):
            item.scores["rerank"] = score
            rescored.append((score, item))
        rescored.sort(
            key=lambda pair: (
                -pair[0],
                -pair[1].score,
                pair[1].kb_id.int,
                pair[1].hit.id.int,
            )
        )
        return (
            [item for _score, item in rescored] + fused[len(candidates) :],
            len(candidates),
            plan.top_n,
        )

    def _mode(self, request: RetrievalRequest, runtime: KnowledgeBaseRuntime) -> str:
        configured = request.search_mode or runtime.retrieval_config.search_mode
        if (
            request.search_mode is None
            and request.query_vector is not None
            and request.query is None
            and configured == "hybrid"
        ):
            return "vector"
        return configured

    def _validate_driver_hits(self, runtime: KnowledgeBaseRuntime, hits: list[Hit]) -> list[Hit]:
        for hit in hits:
            payload = hit.payload
            try:
                payload_chunk_id = UUID(str(payload["chunk_id"]))
                UUID(str(payload["document_id"]))
                payload_kb_id = UUID(str(payload["kb_id"]))
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                raise RetrievalReadModelInvalid(
                    "A retrieval result has invalid namespace identity."
                ) from exc
            content = payload.get("content")
            token_count = payload.get("token_count")
            if (
                not math.isfinite(hit.score)
                or payload_chunk_id != hit.id
                or payload_kb_id != runtime.id
                or payload.get("index_version") != runtime.index_version
                or not isinstance(content, str)
                or not content
                or (token_count is not None and (type(token_count) is not int or token_count < 0))
            ):
                raise RetrievalReadModelInvalid(
                    "A retrieval result does not match its ACTIVE namespace."
                )
        return hits

    async def _bounded_search(
        self, store: object, namespace: Namespace, query: VectorQuery
    ) -> list[Hit]:
        async with asyncio.timeout(self._search_timeout_s):
            return await store.search(namespace, query)  # type: ignore[attr-defined,no-any-return]

    def _assemble_results(
        self,
        fused: list[FusedHit],
        request: RetrievalRequest,
        runtimes: Sequence[KnowledgeBaseRuntime],
    ) -> tuple[list[RetrievalHit], list[DegradationNotice]]:
        runtime_by_id = {runtime.id: runtime for runtime in runtimes}
        results: list[RetrievalHit] = []
        expanded: dict[tuple[UUID, UUID], RetrievalHit] = {}
        notices: dict[str, DegradationNotice] = {}
        for item in fused:
            child = self._assemble(item, request)
            runtime = runtime_by_id[item.kb_id]
            expand = (
                request.options.expand_parent
                if request.options.expand_parent is not None
                else runtime.retrieval_config.expand_parent
            )
            if not expand:
                results.append(child)
                continue
            raw_parent_id = item.hit.payload.get("parent_id")
            if raw_parent_id is None:
                results.append(child)
                continue
            try:
                parent_id = UUID(str(raw_parent_id))
            except (TypeError, ValueError, AttributeError) as exc:
                raise RetrievalReadModelInvalid(
                    "A retrieval result has an invalid parent identity."
                ) from exc
            if not runtime.parent_snapshots_safe:
                notices["parent_snapshots_unsafe"] = DegradationNotice(
                    stage="expand_parent",
                    reason="parent_snapshots_unsafe",
                    detail=(
                        "Parent expansion was disabled because this active index has manual edits "
                        "or predates snapshot safety metadata."
                    ),
                )
                results.append(child)
                continue
            edit_generation = item.hit.payload.get("manual_edit_generation")
            preserved_edit = item.hit.payload.get("_manual_edit_preserved")
            if (
                (edit_generation is not None and type(edit_generation) is not int)
                or (type(edit_generation) is int and edit_generation < 0)
                or (preserved_edit is not None and type(preserved_edit) is not bool)
            ):
                raise RetrievalReadModelInvalid(
                    "A retrieval result has invalid manual-edit provenance."
                )
            if (type(edit_generation) is int and edit_generation > 0) or preserved_edit is True:
                notices["manual_child_edit"] = DegradationNotice(
                    stage="expand_parent",
                    reason="manual_child_edit",
                    detail=(
                        "A manually edited child was retained because its parent snapshot may "
                        "contain obsolete child text."
                    ),
                )
                results.append(child)
                continue
            snapshot = item.hit.payload.get("_parent_snapshot")
            if snapshot is None:
                notices["parent_snapshot_missing"] = DegradationNotice(
                    stage="expand_parent",
                    reason="parent_snapshot_missing",
                    detail=(
                        "A legacy child has no immutable parent snapshot; the child was retained."
                    ),
                )
                results.append(child)
                continue
            if not isinstance(snapshot, dict) or "metadata" not in snapshot:
                notices["parent_snapshot_metadata_missing"] = DegradationNotice(
                    stage="expand_parent",
                    reason="parent_snapshot_metadata_missing",
                    detail=(
                        "A parent snapshot has no parent citation metadata; the child was retained."
                    ),
                )
                results.append(child)
                continue
            parent = self._parent_result(item, child, parent_id, snapshot)
            key = (item.kb_id, parent_id)
            previous = expanded.get(key)
            if previous is None:
                expanded[key] = parent
                results.append(parent)
                continue
            if (
                previous.document_id != parent.document_id
                or previous.content != parent.content
                or previous.token_count != parent.token_count
            ):
                raise RetrievalReadModelInvalid(
                    "Sibling chunks contain inconsistent parent snapshots."
                )
            previous.matched_child_ids.extend(parent.matched_child_ids)
        return results, list(notices.values())

    def _parent_result(
        self,
        item: FusedHit,
        child: RetrievalHit,
        parent_id: UUID,
        snapshot: object,
    ) -> RetrievalHit:
        if not isinstance(snapshot, dict):
            raise RetrievalReadModelInvalid("A parent snapshot is malformed.")
        payload = item.hit.payload
        try:
            snapshot_id = UUID(str(snapshot["id"]))
            snapshot_document_id = UUID(str(snapshot["document_id"]))
            snapshot_kb_id = UUID(str(snapshot["kb_id"]))
            payload_document_id = UUID(str(payload["document_id"]))
            revision = payload["revision"]
            content = snapshot["content"]
            token_count = snapshot["token_count"]
            raw_metadata = snapshot["metadata"]
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise RetrievalReadModelInvalid("A parent snapshot is malformed.") from exc
        if (
            snapshot_id != parent_id
            or snapshot_id == item.hit.id
            or snapshot_document_id != payload_document_id
            or snapshot_kb_id != item.kb_id
            or snapshot.get("index_version") != payload.get("index_version")
            or type(revision) is not int
            or revision < 1
            or snapshot.get("revision") != revision
            or not isinstance(content, str)
            or not content
            or len(content) > _MAX_PARENT_SNAPSHOT_CHARACTERS
            or type(token_count) is not int
            or not 0 <= token_count <= _MAX_PARENT_SNAPSHOT_TOKENS
        ):
            raise RetrievalReadModelInvalid(
                "A parent snapshot does not match its ACTIVE child scope."
            )
        metadata = self._parent_metadata(raw_metadata)
        source_url = metadata.get("source_url")
        return RetrievalHit(
            chunk_id=encode_id("chk", parent_id),
            document_id=child.document_id,
            knowledge_base_id=child.knowledge_base_id,
            content=content,
            token_count=token_count,
            score=child.score,
            scores=dict(child.scores),
            matched_child_ids=[child.chunk_id],
            metadata=metadata,
            source=SourceInfo(url=source_url) if isinstance(source_url, str) else None,
        )

    @staticmethod
    def _parent_metadata(raw: object) -> dict[str, object]:
        if not isinstance(raw, dict):
            raise RetrievalReadModelInvalid("Parent citation metadata is malformed.")
        metadata: dict[str, object] = {}
        language = raw.get("language")
        if language is not None:
            if not isinstance(language, str) or len(language) > 64:
                raise RetrievalReadModelInvalid("Parent citation metadata is malformed.")
            metadata["language"] = language
        page = raw.get("page")
        if page is not None:
            if type(page) is not int or page < 1:
                raise RetrievalReadModelInvalid("Parent citation metadata is malformed.")
            metadata["page"] = page
        source_url = raw.get("source_url")
        if source_url is not None:
            if not isinstance(source_url, str) or len(source_url) > 8192:
                raise RetrievalReadModelInvalid("Parent citation metadata is malformed.")
            metadata["source_url"] = source_url
        heading_path = raw.get("heading_path")
        if heading_path is not None:
            if (
                not isinstance(heading_path, list)
                or len(heading_path) > 64
                or any(not isinstance(part, str) or len(part) > 1024 for part in heading_path)
            ):
                raise RetrievalReadModelInvalid("Parent citation metadata is malformed.")
            metadata["heading_path"] = list(heading_path)
        citations = raw.get("citations")
        if citations is not None:
            if (
                not isinstance(citations, list)
                or len(citations) > _MAX_PARENT_CITATIONS
                or any(not isinstance(citation, dict) for citation in citations)
            ):
                raise RetrievalReadModelInvalid("Parent citation metadata is malformed.")
            metadata["citations"] = [dict(citation) for citation in citations]
        return metadata

    def _assemble(self, item: FusedHit, request: RetrievalRequest) -> RetrievalHit:
        payload = item.hit.payload
        try:
            chunk_id = UUID(str(payload["chunk_id"]))
            document_id = UUID(str(payload["document_id"]))
            payload_kb_id = UUID(str(payload["kb_id"]))
            index_version = payload["index_version"]
            content = payload["content"]
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise RetrievalReadModelInvalid(
                "A retrieval result has invalid identity fields."
            ) from exc
        if (
            chunk_id != item.hit.id
            or payload_kb_id != item.kb_id
            or type(index_version) is not int
            or not isinstance(content, str)
            or not content
        ):
            raise RetrievalReadModelInvalid(
                "A retrieval result does not match its ACTIVE namespace."
            )
        token_count = payload.get("token_count")
        if token_count is not None and (type(token_count) is not int or token_count < 0):
            raise RetrievalReadModelInvalid("A retrieval result has an invalid token count.")
        internal = {
            "chunk_id",
            "document_id",
            "kb_id",
            "index_version",
            "content",
            "content_hash",
            "token_count",
            "parent_id",
            "revision",
            "_parent_snapshot",
            "_manual_edit_preserved",
            "manual_edit_generation",
        }
        metadata = (
            {key: value for key, value in payload.items() if key not in internal}
            if request.options.include_metadata
            else {}
        )
        source_url = payload.get("source_url")
        source = SourceInfo(url=source_url) if isinstance(source_url, str) else None
        return RetrievalHit(
            chunk_id=encode_id("chk", chunk_id),
            document_id=encode_id("doc", document_id),
            knowledge_base_id=encode_id("kb", item.kb_id),
            content=content,
            token_count=token_count,
            score=item.score,
            scores=item.scores,
            metadata=metadata,
            source=source,
        )

    def _apply_token_budget(
        self, hits: list[RetrievalHit], budget: int | None
    ) -> tuple[list[RetrievalHit], bool]:
        if budget is None:
            return hits, False
        if any(hit.token_count is None for hit in hits):
            raise RetrievalReadModelInvalid(
                "The token budget cannot be enforced because a legacy result has no token count."
            )
        used = 0
        kept: list[RetrievalHit] = []
        for hit in hits:
            assert hit.token_count is not None
            if used + hit.token_count > budget:
                return kept, True
            kept.append(hit)
            used += hit.token_count
        return kept, False
