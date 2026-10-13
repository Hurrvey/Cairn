"""Authorization-first orchestration for the bounded retrieval endpoint."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from cairn.authz.model import Principal
from cairn.core.config import get_settings
from cairn.core.document_metadata import READ_MODEL_SCHEMA
from cairn.core.errors import (
    CairnError,
    InvalidId,
    PermissionDenied,
    UpstreamUnavailable,
    ValidationFailed,
)
from cairn.core.ids import InvalidIdError, decode_id, encode_id
from cairn.core.logging import get_logger
from cairn.core.modelref import ModelRef
from cairn.core.retrieval_runtime import (
    BindingRefModel,
    FusionSpec,
    KnowledgeBaseRuntime,
    MmrSpec,
)
from cairn.core.telemetry import retrieval_load_shed_total, retrieval_stage_seconds
from cairn.embedding.sparse import encode_query, tokenize
from cairn.retrieval.diversify import mmr_order, mmr_pool_size
from cairn.retrieval.dto import (
    BatchRequest,
    BatchResponse,
    BatchUsage,
    DegradationNotice,
    ExplainOutput,
    RetrievalHit,
    RetrievalRequest,
    RetrievalResponse,
    SourceInfo,
    TargetFailure,
    UsageInfo,
)
from cairn.retrieval.errors import (
    FilterRequiresRebuild,
    KbRuntimeCorrupt,
    KbRuntimeNotPublished,
    RetrievalReadModelInvalid,
    RetrievalUnsupported,
)
from cairn.retrieval.explain import RANK_CHANGES as RANK_CHANGE_ENTRIES
from cairn.retrieval.explain import ExplainRecorder
from cairn.retrieval.fusion import FusedHit, rrf_fuse, weighted_fuse
from cairn.retrieval.highlight import highlight_spans, query_terms
from cairn.retrieval.load import LoadMonitor
from cairn.retrieval.rerank import RerankUnavailable, RuntimeReranker
from cairn.retrieval.runtime import KnowledgeBaseRuntimeLoader
from cairn.vectorstore.base import Hit, Namespace, SparseVector, VectorQuery
from cairn.vectorstore.errors import DimensionMismatch, NamespaceNotFound
from cairn.vectorstore.filters import (
    And,
    Compare,
    FilterNode,
    check_filter_bounds,
    check_portable,
    parse_filter,
    with_prefix,
)
from cairn.vectorstore.registry import VectorBindingRef, VectorStoreRegistry, get_vector_registry

#: Encodings shared by the queries of one batch: (workspace, model, text, dense,
#: sparse) -> the running encode.
EncodeMemo = dict[tuple[object, ...], "asyncio.Future[Any]"]

log = get_logger(__name__)

MAX_FILTER_DOCUMENTS = 1000
#: Candidate pool per search while shedding load (M09 §4.10).
_SHED_CANDIDATE_K = 50
_BATCH_CONCURRENCY = 4
#: Point payload fields that describe the document (read model v2).
_DOCUMENT_KEYS = ("meta", "created_at", "doc_title", "doc_type", "doc_updated_at")


@dataclass(slots=True)
class _Encodings:
    """Query encodings per target: dense vectors, sparse vectors and their failures."""

    dense: dict[UUID, Sequence[float]]
    #: Present for every target whose sparse stage runs; ``None`` means the
    #: query has no indexable terms, which matches nothing lexically.
    sparse: dict[UUID, SparseVector | None]
    tokens: int
    cached: bool | None
    failed: list[UUID]
    notices: list[DegradationNotice]


@dataclass(slots=True)
class PreparedQuery:
    """A request whose targets are authorized, loaded and fully planned."""

    request: RetrievalRequest
    target_count: int
    active: list[tuple[KnowledgeBaseRuntime, float]]
    failures: list[TargetFailure]
    degraded: list[DegradationNotice]
    filter: FilterNode | None
    dedupe: str
    mmr: MmrSpec | None
    rerank: _RerankPlan | None
    top_k: int
    started: float = field(default_factory=time.perf_counter)
    #: Upper bound on candidates per search while shedding load.
    candidate_cap: int | None = None


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
        slo_p95_ms: float | None = None,
        load_monitor: LoadMonitor | None = None,
    ) -> None:
        self._runtime_loader = runtime_loader or KnowledgeBaseRuntimeLoader()
        self._vectors = vectors or get_vector_registry()
        self._embeddings = embeddings or UnconfiguredQueryEmbeddings()
        self._reranker = reranker
        self._search_timeout_s = search_timeout_s
        self._request_timeout_s = request_timeout_s
        #: ``None`` disables load shedding (M09 §4.10).
        self._slo_p95_ms = slo_p95_ms
        self.load = load_monitor or LoadMonitor()

    async def query(
        self, principal: Principal, request: RetrievalRequest, *, request_id: str
    ) -> RetrievalResponse:
        try:
            async with asyncio.timeout(self._request_timeout_s):
                prepared = await self.prepare(principal, request)
                return await self.execute(prepared, request_id=request_id)
        except TimeoutError as exc:
            # A timeout is the strongest overload signal there is.
            self.load.record(self._request_timeout_s * 1000)
            raise UpstreamUnavailable("The retrieval request exceeded its time limit.") from exc

    async def query_external(
        self,
        principal: Principal,
        request: RetrievalRequest,
        *,
        request_id: str,
    ) -> RetrievalResponse:
        """Dify's single-target scores, using the same authorized ACTIVE snapshot."""
        try:
            async with asyncio.timeout(self._request_timeout_s):
                prepared = await self.prepare(principal, request, preserve_missing=True)
                return await self.execute(prepared, request_id=request_id, external_scores=True)
        except TimeoutError as exc:
            raise UpstreamUnavailable("The retrieval request exceeded its time limit.") from exc

    async def prepare(
        self,
        principal: Principal,
        request: RetrievalRequest,
        *,
        preserve_missing: bool = False,
    ) -> PreparedQuery:
        """Authorize, load ACTIVE runtimes and resolve every option.

        Everything that makes the request itself invalid (400/403) is raised
        here, before any encoding or search, which is what lets a batch reject a
        malformed query without having run the others.
        """
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
            if preserve_missing and isinstance(outcome, KbRuntimeNotPublished):
                raise outcome
            if isinstance(outcome, (PermissionDenied, KbRuntimeCorrupt)):
                raise outcome
            if isinstance(outcome, BaseException):
                failures.append(
                    self._target_failure(
                        kb_id, getattr(outcome, "code", None) or "KB_RUNTIME_UNAVAILABLE"
                    )
                )
            else:
                active.append((outcome, weight))
        if failures and request.strict:
            raise UpstreamUnavailable("Retrieval failed for one or more knowledge bases.")
        if not active:
            raise UpstreamUnavailable(
                "Retrieval failed for every knowledge base: "
                + "; ".join(f"{item.knowledge_base_id}={item.code}" for item in failures)
            )

        runtimes = [runtime for runtime, _weight in active]
        degraded = self._validate_supported(request, runtimes)
        search_filter = self._build_filter(request, runtimes)
        dedupe, mmr = self._post_stages(request, runtimes)
        rerank_plan = self._resolve_rerank(request, runtimes, degraded)
        self._check_query_vector(request, runtimes)
        if request.query is None and any(
            self._mode(request, runtime) == "fulltext" for runtime in runtimes
        ):
            raise ValidationFailed("Full-text retrieval requires query text.")
        rerank_plan, candidate_cap = self._shed_load(rerank_plan, degraded)
        top_k = request.top_k or min(runtime.retrieval_config.top_k for runtime in runtimes)
        return PreparedQuery(
            request=request,
            target_count=len(targets),
            active=active,
            failures=failures,
            degraded=degraded,
            filter=search_filter,
            dedupe=dedupe,
            mmr=mmr,
            rerank=rerank_plan,
            top_k=top_k,
            started=started,
            candidate_cap=candidate_cap,
        )

    def _shed_load(
        self, rerank: _RerankPlan | None, degraded: list[DegradationNotice]
    ) -> tuple[_RerankPlan | None, int | None]:
        """Trade quality for latency when this process is falling behind (M09 §4.10)."""
        p95 = self.load.p95()
        if self._slo_p95_ms is None or p95 is None:
            return rerank, None
        cap: int | None = None
        shed_rerank = False
        if p95 > self._slo_p95_ms * 0.8 and rerank is not None:
            shed_rerank = True
            rerank = None
            degraded.append(
                DegradationNotice(
                    stage="rerank",
                    reason="load_shed",
                    detail="Reranking was skipped because the service is under load; "
                    "fused ranking is returned.",
                )
            )
        if p95 > self._slo_p95_ms * 1.5:
            cap = _SHED_CANDIDATE_K
            degraded.append(
                DegradationNotice(
                    stage="search",
                    reason="load_shed",
                    detail=f"The candidate pool was limited to {_SHED_CANDIDATE_K} per search "
                    "because the service is under load.",
                )
            )
        if shed_rerank or cap is not None:
            retrieval_load_shed_total.labels(
                level="candidates" if cap is not None else "rerank"
            ).inc()
        return rerank, cap

    async def execute(
        self,
        prepared: PreparedQuery,
        *,
        request_id: str,
        encode_memo: EncodeMemo | None = None,
        external_scores: bool = False,
    ) -> RetrievalResponse:
        request = prepared.request
        active = list(prepared.active)
        failures = list(prepared.failures)
        degraded = list(prepared.degraded)
        runtimes = [runtime for runtime, _weight in active]
        explain = ExplainRecorder(request.options.explain)
        latency: dict[str, int] = {}
        explain.plan = self._explain_plan(prepared)

        stage_started = time.perf_counter()
        encodings = await self._query_encodings(request, runtimes, encode_memo)
        latency["embed"] = _ms(stage_started)
        explain.stage(
            "embed",
            latency["embed"],
            cache_hit=encodings.cached,
            tokens=encodings.tokens,
            dims=sorted({runtime.embedding_model.dimension or 0 for runtime in runtimes}),
        )
        degraded.extend(encodings.notices)
        if encodings.failed:
            failed_ids = set(encodings.failed)
            failures.extend(
                self._target_failure(kb_id, "EMBEDDING_PROVIDER_UNAVAILABLE")
                for kb_id in encodings.failed
            )
            active = [
                (runtime, weight) for runtime, weight in active if runtime.id not in failed_ids
            ]
            if request.strict:
                raise UpstreamUnavailable("Retrieval failed for one or more knowledge bases.")
            if not active:
                raise UpstreamUnavailable("Retrieval failed for every knowledge base.")

        stage_started = time.perf_counter()
        explain_terms = set(tokenize(request.query)) if request.query and explain.enabled else None
        tasks = [
            self._search_target(
                runtime,
                weight,
                request,
                encodings.dense.get(runtime.id),
                encodings.sparse.get(runtime.id),
                sparse_stage=runtime.id in encodings.sparse,
                search_filter=prepared.filter,
                candidate_cap=prepared.candidate_cap,
                explain=explain,
                # Learned sparse weights have no readable terms to report.
                explain_terms=explain_terms if runtime.sparse.model is None else None,
            )
            for runtime, weight in active
        ]
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        fused: list[FusedHit] = []
        for (runtime, _weight), outcome in zip(active, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                # Never the query text: only which target failed and how.
                log.warning(
                    "retrieval.target_search_failed",
                    kb_id=str(runtime.id),
                    error=type(outcome).__name__,
                )
                failures.append(self._target_failure(runtime.id, "STORAGE_BINDING_UNAVAILABLE"))
            else:
                fused.extend(outcome)
        latency["search"] = _ms(stage_started)

        if failures and request.strict:
            raise UpstreamUnavailable("Retrieval failed for one or more knowledge bases.")
        if not fused and len(failures) == prepared.target_count:
            raise UpstreamUnavailable("Retrieval failed for every knowledge base.")

        fused.sort(key=lambda item: (-item.score, item.kb_id.int, item.hit.id.int))
        stage_started = time.perf_counter()
        threshold = request.score_threshold
        if threshold is not None:
            before = len(fused)
            fused = [item for item in fused if item.score >= threshold]
            explain.stage("filter", _ms(stage_started), removed_by_threshold=before - len(fused))
        rerank_units = 0
        top_k = prepared.top_k
        reranked = False
        if prepared.rerank is not None:
            stage_started = time.perf_counter()
            before_ids = [encode_id("chk", item.hit.id) for item in fused]
            fused, rerank_units, rerank_top_n = await self._rerank_fused(
                fused,
                request,
                runtimes[0].workspace_id,
                prepared.rerank,
                degraded,
            )
            latency["rerank"] = _ms(stage_started)
            if rerank_top_n is not None:
                top_k = min(top_k, rerank_top_n)
                reranked = True
            explain.stage(
                "rerank",
                latency["rerank"],
                model=prepared.rerank.model_id,
                scored=rerank_units,
                applied=reranked,
                rank_changes=(
                    explain.rank_changes(
                        before_ids,
                        [encode_id("chk", item.hit.id) for item in fused],
                        score={
                            encode_id("chk", item.hit.id): item.score
                            for item in fused[:rerank_units]
                        },
                    )
                    if explain.enabled and reranked
                    else []
                ),
            )

        stage_started = time.perf_counter()
        assembly_started = stage_started
        children = len(fused)
        hits, expansion_notices = self._assemble_results(fused, request, runtimes)
        degraded.extend(expansion_notices)
        explain.stage(
            "expand",
            _ms(stage_started),
            children_collapsed=children - len(hits),
            parents_returned=sum(1 for hit in hits if hit.matched_child_ids),
        )
        if prepared.dedupe != "none":
            stage_started = time.perf_counter()
            before = len(hits)
            hits = _dedupe(hits, prepared.dedupe)
            explain.stage(
                "dedupe", _ms(stage_started), mode=prepared.dedupe, removed=before - len(hits)
            )
        if prepared.mmr is not None:
            stage_started = time.perf_counter()
            # After a rerank, only reranked candidates compete: mixing in the
            # fused-score tail would quietly undo the rerank.
            pool = [hit for hit in hits if not reranked or "rerank" in hit.scores]
            pool = pool[: mmr_pool_size(top_k)]
            before_ids = [hit.chunk_id for hit in pool]
            hits = await asyncio.to_thread(_diversify, pool, prepared.mmr.lambda_, top_k)
            explain.stage(
                "mmr",
                _ms(stage_started),
                **{"lambda": prepared.mmr.lambda_},
                pool=len(pool),
                rank_changes=(
                    explain.rank_changes(before_ids, [hit.chunk_id for hit in hits])
                    if explain.enabled
                    else []
                ),
            )
        hits = hits[:top_k]
        stage_started = time.perf_counter()
        hits, truncated = self._apply_token_budget(hits, request.options.max_context_tokens)
        explain.stage(
            "budget",
            _ms(stage_started),
            budget=request.options.max_context_tokens,
            truncated=truncated,
            total_tokens=sum(hit.token_count or 0 for hit in hits),
        )
        if request.options.include_highlights and request.query:
            terms = query_terms(request.query)
            for hit in hits:
                hit.highlights = highlight_spans(terms, hit.content)
        if external_scores and hits:
            if len(runtimes) != 1:
                raise ValidationFailed("External retrieval requires exactly one knowledge base.")
            try:
                await self._external_scores(hits, prepared, runtimes[0], encodings)
            except (DimensionMismatch, NamespaceNotFound) as exc:
                raise UpstreamUnavailable(
                    "The index changed during external relevance scoring."
                ) from exc
        latency.update(explain.latency)
        latency["assemble"] = _ms(assembly_started)
        latency["total"] = _ms(prepared.started)
        self.load.record(latency["total"])
        for stage, value in latency.items():
            retrieval_stage_seconds.labels(stage=stage).observe(value / 1000)
        output = explain.output()
        return RetrievalResponse(
            request_id=request_id,
            results=hits,
            usage=UsageInfo(
                embedding_tokens=encodings.tokens,
                rerank_units=rerank_units,
                cached_embedding=encodings.cached,
                index_versions={
                    encode_id("kb", runtime.id): runtime.index_version
                    for runtime, _weight in prepared.active
                },
                latency_ms=latency,
            ),
            degraded=degraded,
            partial_failures=failures,
            truncated_to_token_budget=truncated,
            explain=ExplainOutput.model_validate(output) if output is not None else None,
        )

    async def _external_scores(
        self,
        hits: list[RetrievalHit],
        prepared: PreparedQuery,
        runtime: KnowledgeBaseRuntime,
        encodings: _Encodings,
    ) -> None:
        missing = [hit for hit in hits if "rerank" not in hit.scores and "dense" not in hit.scores]
        dense_needed = any("rerank" not in hit.scores for hit in hits)
        if dense_needed and runtime.metric != "cosine" and not runtime.embedding_model.normalize:
            raise ValidationFailed(
                "Dify scoring needs cosine distance or normalized embeddings; rebuild this index."
            )
        fetched: dict[str, float] = {}
        if missing:
            ids = sorted(
                {
                    decode_id("chk", id_)
                    for hit in missing
                    for id_ in (hit.matched_child_ids or [hit.chunk_id])
                },
                key=lambda id_: id_.int,
            )
            if len(ids) > 1000:
                raise ValidationFailed("Too many child passages for external scoring.")
            dense = encodings.dense.get(runtime.id)
            if dense is None:
                assert prepared.request.query is not None
                dense, tokens, cached = await self._embed_query(
                    runtime.workspace_id,
                    runtime.embedding_model,
                    prepared.request.query,
                )
                encodings.tokens += tokens
                encodings.cached = cached
            binding = runtime.vector_binding
            store = await self._vectors.for_binding(  # type: ignore[attr-defined]
                VectorBindingRef(binding.id, binding.driver, binding.config)
            )
            scored = await self._bounded_search(
                store,
                Namespace(runtime.id, runtime.index_version),
                VectorQuery(dense=dense, ids=ids, top_k=len(ids), with_payload=False),
            )
            fetched = {encode_id("chk", hit.id): hit.score for hit in scored}
        for hit in hits:
            if "rerank" in hit.scores:
                relevance = hit.scores["rerank"]
            else:
                score = hit.scores.get("dense")
                if score is None:
                    child_scores = [
                        fetched[id_]
                        for id_ in (hit.matched_child_ids or [hit.chunk_id])
                        if id_ in fetched
                    ]
                    if not child_scores:
                        raise UpstreamUnavailable(
                            "An indexed passage changed during external scoring."
                        )
                    score = max(child_scores)
                relevance = 1 - score * score / 2 if runtime.metric == "l2" else score
            if not math.isfinite(relevance):
                raise RetrievalReadModelInvalid("External scoring returned a non-finite value.")
            hit.scores["relevance"] = max(0.0, min(1.0, relevance))

    async def batch(
        self, principal: Principal, batch: BatchRequest, *, request_id: str
    ) -> BatchResponse:
        """Up to 20 queries; one malformed query rejects the batch, a failing one does not.

        Every query is planned first, so authorization and validation errors
        surface before any search runs. Queries then execute at most
        ``_BATCH_CONCURRENCY`` at a time and share identical encodings. A query
        that fails at run time is returned with no results and one partial
        failure per target.
        """
        started = time.perf_counter()
        deadline = self._request_timeout_s * 2
        memo: EncodeMemo = {}
        try:
            async with asyncio.timeout(deadline):
                plans: list[PreparedQuery | CairnError] = []
                for query in batch.queries:
                    try:
                        plans.append(await self.prepare(principal, query))
                    except UpstreamUnavailable as exc:
                        plans.append(exc)
                gate = asyncio.Semaphore(_BATCH_CONCURRENCY)

                async def run(index: int, plan: PreparedQuery | CairnError) -> RetrievalResponse:
                    query = batch.queries[index]
                    entry_id = f"{request_id}.{index}"
                    if isinstance(plan, CairnError):
                        return _failed_response(entry_id, query, plan.code, plan.detail)
                    async with gate:
                        try:
                            async with asyncio.timeout(self._request_timeout_s):
                                return await self.execute(
                                    plan, request_id=entry_id, encode_memo=memo
                                )
                        except UpstreamUnavailable as exc:
                            return _failed_response(entry_id, query, exc.code, exc.detail)
                        except TimeoutError:
                            return _failed_response(
                                entry_id,
                                query,
                                UpstreamUnavailable.code,
                                "The retrieval query exceeded its time limit.",
                            )

                responses = await asyncio.gather(
                    *(run(index, plan) for index, plan in enumerate(plans))
                )
        except TimeoutError as exc:
            raise UpstreamUnavailable("The retrieval batch exceeded its time limit.") from exc
        finally:
            for task in memo.values():
                if not task.done():
                    task.cancel()
            await asyncio.gather(*memo.values(), return_exceptions=True)
        return BatchResponse(
            request_id=request_id,
            responses=list(responses),
            usage=BatchUsage(
                embedding_tokens=sum(response.usage.embedding_tokens for response in responses),
                latency_ms={"total": _ms(started)},
            ),
        )

    def _explain_plan(self, prepared: PreparedQuery) -> dict[str, object]:
        request = prepared.request
        if not request.options.explain:
            return {}
        return {
            "top_k": prepared.top_k,
            "score_threshold": request.score_threshold,
            "rerank_model": prepared.rerank.model_id if prepared.rerank else None,
            "dedupe": prepared.dedupe,
            "mmr": {"lambda": prepared.mmr.lambda_} if prepared.mmr else None,
            "expand_parent": request.options.expand_parent,
            "filter": (
                request.filters.model_dump(mode="json", exclude_none=True)
                if request.filters
                else None
            ),
            "targets": [
                {
                    "kb": encode_id("kb", runtime.id),
                    "weight": weight,
                    "index_version": runtime.index_version,
                    "mode": self._mode(request, runtime),
                    "fusion": _fusion_label(request.fusion or runtime.retrieval_config.fusion),
                    "candidate_k": request.candidate_k or runtime.retrieval_config.candidate_k,
                    "sparse": runtime.sparse.kind,
                }
                for runtime, weight in prepared.active
            ],
        }

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
        for runtime in runtimes:
            fusion = request.fusion or runtime.retrieval_config.fusion
            weights = request.weights or runtime.retrieval_config.weights
            if fusion.method == "rrf" and request.weights is not None:
                raise ValidationFailed("Search weights are incompatible with RRF fusion.")
            if fusion.method == "weighted" and weights.dense == 0 and weights.sparse == 0:
                raise ValidationFailed("Weighted fusion requires at least one non-zero weight.")

        notices: dict[str, DegradationNotice] = {}
        for runtime in runtimes:
            if (
                request.search_mode is None
                and request.query_vector is not None
                and runtime.retrieval_config.search_mode == "hybrid"
            ):
                notices["sparse"] = DegradationNotice(
                    stage="sparse",
                    reason="query_text_missing",
                    detail=(
                        "The hybrid default was reduced to vector search because no query text "
                        "was supplied."
                    ),
                )
        return list(notices.values())

    def _build_filter(
        self, request: RetrievalRequest, runtimes: Sequence[KnowledgeBaseRuntime]
    ) -> FilterNode | None:
        """Compile ``filters`` into one condition pushed down to every search.

        ``metadata`` addresses the document's metadata (``meta.*`` in the read
        model); chunk fields such as ``page`` are not filterable. Metadata and
        date conditions need an ACTIVE version built with the v2 payload layout;
        an older one is refused by name rather than silently matching nothing.
        """
        spec = request.filters
        if spec is None:
            return None
        clauses: list[FilterNode] = []
        needs_documents = False
        if spec.metadata:
            check_filter_bounds(spec.metadata)
            parsed = parse_filter(spec.metadata)
            if parsed is not None:
                scoped = with_prefix(parsed, "meta")
                check_portable(scoped)
                clauses.append(scoped)
                needs_documents = True
        for bound, op in ((spec.created_after, "$gte"), (spec.created_before, "$lt")):
            if bound is None:
                continue
            if bound.tzinfo is None:
                raise ValidationFailed(
                    "Date filters need a timezone, for example 2026-01-01T00:00Z."
                )
            clauses.append(Compare("created_at", op, int(bound.timestamp())))  # type: ignore[arg-type]
            needs_documents = True
        if spec.document_ids is not None:
            if len(spec.document_ids) > MAX_FILTER_DOCUMENTS:
                raise ValidationFailed(
                    f"A filter may name at most {MAX_FILTER_DOCUMENTS} documents."
                )
            try:
                ids = sorted({str(decode_id("doc", value)) for value in spec.document_ids})
            except InvalidIdError as exc:
                raise InvalidId("A document filter has an invalid document ID.") from exc
            clauses.append(Compare("document_id", "$in", ids))
        if needs_documents:
            stale = [
                encode_id("kb", runtime.id)
                for runtime in runtimes
                if runtime.payload_schema < READ_MODEL_SCHEMA
            ]
            if stale:
                raise FilterRequiresRebuild(
                    "Metadata and date filters need a rebuilt index for: "
                    + ", ".join(stale)
                    + ". Rebuild these knowledge bases to enable them."
                )
        if not clauses:
            return None
        return clauses[0] if len(clauses) == 1 else And(tuple(clauses))

    def _post_stages(
        self, request: RetrievalRequest, runtimes: Sequence[KnowledgeBaseRuntime]
    ) -> tuple[str, MmrSpec | None]:
        """Resolve dedupe and MMR: the request's choice, else one shared KB default."""
        options = request.options
        if options.dedupe is not None:
            dedupe = options.dedupe
        else:
            configured = {runtime.retrieval_config.dedupe for runtime in runtimes}
            if len(configured) > 1:
                raise ValidationFailed(
                    "Configured deduplication differs across targets; set options.dedupe."
                )
            dedupe = configured.pop()
        if options.mmr is not None:
            mmr = options.mmr
        else:
            configured_mmr = {
                (runtime.retrieval_config.mmr.enabled, runtime.retrieval_config.mmr.lambda_)
                for runtime in runtimes
            }
            if len(configured_mmr) > 1:
                raise ValidationFailed("Configured MMR differs across targets; set options.mmr.")
            enabled, lambda_ = configured_mmr.pop()
            mmr = MmrSpec(enabled=enabled, lambda_=lambda_)
        return dedupe, mmr if mmr.enabled else None

    def _check_query_vector(
        self, request: RetrievalRequest, runtimes: Sequence[KnowledgeBaseRuntime]
    ) -> None:
        supplied = request.query_vector
        dense_runtimes = [
            runtime for runtime in runtimes if self._mode(request, runtime) in {"vector", "hybrid"}
        ]
        if supplied is None or not dense_runtimes:
            return
        if len({runtime.embedding_model for runtime in dense_runtimes}) != 1:
            raise ValidationFailed(
                "A supplied query vector requires one embedding identity across all targets."
            )
        expected = dense_runtimes[0].embedding_model.dimension
        if expected is None or len(supplied) != expected:
            raise ValidationFailed(
                f"The query vector dimension does not match the ACTIVE model dimension {expected}."
            )

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

    async def _query_encodings(
        self,
        request: RetrievalRequest,
        runtimes: Sequence[KnowledgeBaseRuntime],
        memo: EncodeMemo | None = None,
    ) -> _Encodings:
        """Encode the query once per model across all targets.

        A model that supplies both a target's dense vector and another's (or the
        same one's) sparse vector is called once for both. BM25 is computed
        locally, once. A failed sparse encoding degrades ``hybrid`` to vector
        search with a notice; for ``fulltext`` the target has nothing to search
        with, so it fails.
        """
        modes = {runtime.id: self._mode(request, runtime) for runtime in runtimes}
        dense_runtimes = [
            runtime for runtime in runtimes if modes[runtime.id] in {"vector", "hybrid"}
        ]
        sparse_runtimes = [
            runtime for runtime in runtimes if modes[runtime.id] in {"fulltext", "hybrid"}
        ]
        encodings = _Encodings(dense={}, sparse={}, tokens=0, cached=None, failed=[], notices=[])

        supplied = request.query_vector
        if dense_runtimes and supplied is not None:
            identities = {runtime.embedding_model for runtime in dense_runtimes}
            if len(identities) != 1:
                raise ValidationFailed(
                    "A supplied query vector requires one embedding identity across all targets."
                )
            expected = dense_runtimes[0].embedding_model.dimension
            if expected is None or len(supplied) != expected:
                raise ValidationFailed(
                    "The query vector dimension does not match the ACTIVE model dimension "
                    f"{expected}."
                )
            encodings.dense.update({runtime.id: supplied for runtime in dense_runtimes})
        elif dense_runtimes and request.query is None:
            raise ValidationFailed("Dense retrieval requires query text or a query vector.")
        if sparse_runtimes and request.query is None:
            raise ValidationFailed("Full-text retrieval requires query text.")

        # model -> (targets needing its dense vector, targets needing its sparse vector)
        plan: dict[ModelRef, tuple[list[KnowledgeBaseRuntime], list[KnowledgeBaseRuntime]]] = {}
        if supplied is None:
            for runtime in dense_runtimes:
                plan.setdefault(runtime.embedding_model, ([], []))[0].append(runtime)
        bm25_runtimes: list[KnowledgeBaseRuntime] = []
        for runtime in sparse_runtimes:
            model = runtime.sparse.model
            if model is None:
                bm25_runtimes.append(runtime)
            else:
                plan.setdefault(model, ([], []))[1].append(runtime)
        if bm25_runtimes:
            assert request.query is not None
            terms = encode_query(request.query)
            encodings.sparse.update({runtime.id: terms for runtime in bm25_runtimes})
        if not plan:
            return encodings

        assert request.query is not None
        query = request.query
        groups = list(plan.items())
        outcomes = await asyncio.gather(
            *(
                self._encode_shared(
                    memo,
                    (dense_targets or sparse_targets)[0].workspace_id,
                    model,
                    query,
                    dense=bool(dense_targets),
                    sparse=bool(sparse_targets),
                )
                for model, (dense_targets, sparse_targets) in groups
            ),
            return_exceptions=True,
        )
        cache_states: list[bool | None] = []
        failed: set[UUID] = set()
        sparse_unavailable: set[UUID] = set()
        for (model, (dense_targets, sparse_targets)), outcome in zip(groups, outcomes, strict=True):
            if isinstance(outcome, RetrievalUnsupported):
                raise outcome
            if isinstance(outcome, BaseException):
                failed.update(runtime.id for runtime in dense_targets)
                sparse_unavailable.update(runtime.id for runtime in sparse_targets)
                continue
            dense_vector, sparse_vector, used_tokens, cached = outcome
            if dense_targets and (dense_vector is None or len(dense_vector) != model.dimension):
                failed.update(runtime.id for runtime in dense_targets)
            elif dense_targets:
                assert dense_vector is not None
                encodings.dense.update({runtime.id: dense_vector for runtime in dense_targets})
            encodings.sparse.update({runtime.id: sparse_vector for runtime in sparse_targets})
            encodings.tokens += used_tokens
            cache_states.append(cached)

        for runtime in sparse_runtimes:
            if runtime.id not in sparse_unavailable:
                continue
            if modes[runtime.id] == "fulltext":
                failed.add(runtime.id)
            elif not any(
                notice.stage == "sparse" and notice.reason == "unavailable"
                for notice in encodings.notices
            ):
                encodings.notices.append(
                    DegradationNotice(
                        stage="sparse",
                        reason="unavailable",
                        detail=(
                            "The sparse query encoder is unavailable; vector search results "
                            "are returned."
                        ),
                    )
                )
        encodings.failed = [runtime.id for runtime in runtimes if runtime.id in failed]
        encodings.cached = (
            True
            if cache_states and all(state is True for state in cache_states)
            else False
            if any(state is False for state in cache_states)
            else None
        )
        return encodings

    async def _encode_shared(
        self,
        memo: EncodeMemo | None,
        workspace_id: UUID,
        model: ModelRef,
        query: str,
        *,
        dense: bool,
        sparse: bool,
    ) -> tuple[Sequence[float] | None, SparseVector | None, int, bool | None]:
        """Encode once per distinct (model, text, outputs) across a batch.

        Later users of a shared encoding report no tokens and a cache hit, so a
        batch's usage counts each provider call once.
        """
        if memo is None:
            return await self._encode_with(workspace_id, model, query, dense=dense, sparse=sparse)
        key = (workspace_id, model, query, dense, sparse)
        task = memo.get(key)
        if task is None:
            memo[key] = asyncio.ensure_future(
                self._encode_with(workspace_id, model, query, dense=dense, sparse=sparse)
            )
            first: tuple[
                Sequence[float] | None, SparseVector | None, int, bool | None
            ] = await asyncio.shield(memo[key])
            return first
        dense_vector, sparse_vector, _tokens, _cached = await asyncio.shield(task)
        return dense_vector, sparse_vector, 0, True

    async def _encode_with(
        self,
        workspace_id: UUID,
        model: ModelRef,
        query: str,
        *,
        dense: bool,
        sparse: bool,
    ) -> tuple[Sequence[float] | None, SparseVector | None, int, bool | None]:
        if not sparse:
            vector, tokens, cached = await self._embed_query(workspace_id, model, query)
            return vector, None, tokens, cached
        encode = getattr(self._embeddings, "encode_query_for_workspace", None)
        if encode is None:
            raise RetrievalUnsupported("This deployment cannot encode sparse queries.")
        encoding = await encode(workspace_id, model, query, dense=dense, sparse=True)
        return encoding.dense, encoding.sparse, encoding.tokens, encoding.cached

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
        sparse: SparseVector | None = None,
        *,
        sparse_stage: bool = False,
        search_filter: FilterNode | None = None,
        candidate_cap: int | None = None,
        explain: ExplainRecorder | None = None,
        explain_terms: set[str] | None = None,
    ) -> list[FusedHit]:
        explain = explain or ExplainRecorder(False)
        mode = self._mode(request, runtime)
        candidate_k = request.candidate_k or runtime.retrieval_config.candidate_k
        top_k = request.top_k or runtime.retrieval_config.top_k
        candidate_k = max(candidate_k, top_k)
        if candidate_cap is not None:
            candidate_k = max(min(candidate_k, candidate_cap), top_k)
        binding = BindingRefModel.model_validate(runtime.vector_binding)
        store = await self._vectors.for_binding(  # type: ignore[attr-defined]
            VectorBindingRef(binding.id, binding.driver, binding.config)
        )
        namespace = Namespace(runtime.id, runtime.index_version)
        searches: list[tuple[str, asyncio.Task[tuple[list[Hit], float]], float]] = []
        if mode in {"vector", "hybrid"}:
            if dense is None:
                raise ValidationFailed("Dense retrieval has no query vector.")
            searches.append(
                (
                    "dense",
                    asyncio.create_task(
                        self._timed_search(
                            store,
                            namespace,
                            VectorQuery(
                                top_k=candidate_k,
                                dense=dense,
                                filter=search_filter,
                                with_payload=True,
                            ),
                        )
                    ),
                    target_weight,
                )
            )
        # A query with no indexable terms ("?!") has nothing to match lexically;
        # it contributes no hits rather than an error.
        if mode in {"fulltext", "hybrid"} and sparse_stage and sparse is not None:
            searches.append(
                (
                    "sparse",
                    asyncio.create_task(
                        self._timed_search(
                            store,
                            namespace,
                            VectorQuery(
                                top_k=candidate_k,
                                sparse=sparse,
                                filter=search_filter,
                                with_payload=True,
                            ),
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
            for (stage, _task, weight), (hits, _elapsed) in zip(searches, results, strict=True)
        ]
        kb = encode_id("kb", runtime.id)
        for (stage, hits, _weight), (_hits, elapsed) in zip(ranked, results, strict=True):
            explain.stage(
                stage,
                elapsed,
                knowledge_base_id=kb,
                returned=len(hits),
                top=explain.top_hits(hits, terms=explain_terms if stage == "sparse" else None),
            )
        fusion_started = time.perf_counter()
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
        changes: list[dict[str, object]] = []
        if explain.enabled and len(ranked) > 1:
            stage_ranks = {
                stage: {encode_id("chk", hit.id): rank for rank, hit in enumerate(hits, 1)}
                for stage, hits, _weight in ranked
            }
            for rank, item in enumerate(fused[:RANK_CHANGE_ENTRIES], start=1):
                chunk_id = encode_id("chk", item.hit.id)
                changes.append(
                    {
                        "chunk_id": chunk_id,
                        "dense_rank": stage_ranks.get("dense", {}).get(chunk_id),
                        "sparse_rank": stage_ranks.get("sparse", {}).get(chunk_id),
                        "fused_rank": rank,
                    }
                )
        explain.stage(
            "fuse",
            _ms(fusion_started),
            applied=len(ranked) > 1,
            knowledge_base_id=kb,
            method=_fusion_label(fusion),
            unique=len(fused),
            rank_changes=changes,
        )
        filter_started = time.perf_counter()
        threshold = (
            request.score_threshold
            if request.score_threshold is not None
            else runtime.retrieval_config.score_threshold
        )
        kept = [item for item in fused if item.score >= threshold]
        explain.stage(
            "filter",
            _ms(filter_started),
            knowledge_base_id=kb,
            removed_by_threshold=len(fused) - len(kept),
        )
        return kept

    async def _timed_search(
        self, store: object, namespace: Namespace, query: VectorQuery
    ) -> tuple[list[Hit], float]:
        started = time.perf_counter()
        hits = await self._bounded_search(store, namespace, query)
        return hits, (time.perf_counter() - started) * 1000

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
        # The reranker's judgement is the final score (API contract); the fused
        # score stays available as scores["fused"].
        for score, item in rescored:
            item.score = score
        rescored.sort(
            key=lambda pair: (
                -pair[0],
                -pair[1].scores["fused"],
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
            parent = self._parent_result(
                item,
                child,
                parent_id,
                snapshot,
                include_metadata=request.options.include_metadata,
            )
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
        *,
        include_metadata: bool = True,
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
        if include_metadata:
            # Document metadata is the same for every chunk of a document.
            metadata = {**_document_metadata(payload), **metadata}
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
            source=_source(payload, url=source_url if isinstance(source_url, str) else None),
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
            *_DOCUMENT_KEYS,
        }
        metadata = (
            {
                **_document_metadata(payload),
                **{key: value for key, value in payload.items() if key not in internal},
            }
            if request.options.include_metadata
            else {}
        )
        source = _source(payload)
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


def _ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def _fusion_label(fusion: FusionSpec) -> str:
    return f"rrf(k={fusion.k})" if fusion.method == "rrf" else "weighted"


def _dedupe(hits: list[RetrievalHit], mode: str) -> list[RetrievalHit]:
    """Keep the first hit per chunk (``by_chunk``) or per document, in ranking order."""
    seen: set[str] = set()
    kept: list[RetrievalHit] = []
    for hit in hits:
        key = hit.document_id if mode == "by_document" else hit.chunk_id
        if key in seen:
            continue
        seen.add(key)
        kept.append(hit)
    return kept


def _diversify(pool: list[RetrievalHit], lambda_: float, top_k: int) -> list[RetrievalHit]:
    order = mmr_order(
        [hit.score for hit in pool], [hit.content for hit in pool], lambda_=lambda_, k=top_k
    )
    selected: list[RetrievalHit] = []
    for index, objective in order:
        hit = pool[index]
        hit.scores["mmr"] = objective
        selected.append(hit)
    return selected


def _document_metadata(payload: Any) -> dict[str, Any]:
    meta = payload.get("meta")
    return dict(meta) if isinstance(meta, dict) else {}


def _source(payload: Any, *, url: str | None = None) -> SourceInfo | None:
    source_url = url if url is not None else payload.get("source_url")
    title = payload.get("doc_title")
    kind = payload.get("doc_type")
    revised = payload.get("doc_updated_at")
    updated_at = (
        datetime.fromtimestamp(revised, UTC)
        if type(revised) is int and 0 <= revised < 32_503_680_000
        else None
    )
    source = SourceInfo(
        title=title if isinstance(title, str) else None,
        url=source_url if isinstance(source_url, str) else None,
        type=kind if isinstance(kind, str) else None,
        updated_at=updated_at,
    )
    return source if any(source.model_dump().values()) else None


def _failed_response(
    request_id: str, query: RetrievalRequest, code: str, detail: str
) -> RetrievalResponse:
    return RetrievalResponse(
        request_id=request_id,
        results=[],
        usage=UsageInfo(),
        partial_failures=[
            TargetFailure(knowledge_base_id=target.knowledge_base_id, code=code, detail=detail)
            for target in query.targets
        ],
    )
