# Qdrant Vector Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace pgvector with Qdrant as the only vector backend, with built-in BM25 sparse vectors so full-text and hybrid retrieval keep working.

**Architecture:** A new `QdrantVectorStore` implements the existing `VectorStore` protocol with one collection per namespace (`dense` named vector + `sparse` named sparse vector with the IDF modifier). A deterministic BM25 encoder in `cairn.embedding.sparse` produces document sparse vectors at index time and query sparse vectors at retrieval time; `VectorQuery.text` is removed. pgvector code, extension and Compose image are removed.

**Tech Stack:** Python 3.12+, `qdrant-client` 1.19 (async REST), Qdrant server v1.19.1, `jieba` 0.42.1, pytest + testcontainers, Docker Compose, Vue 3 web client.

**Spec:** `docs/superpowers/specs/2026-09-23-qdrant-vector-backend-design.md`

## Global Constraints

- Qdrant server image: `qdrant/qdrant:v1.19.1@sha256:12364fe851b9f17356fc88189fc06d1b521262e04659ec7345975b00c9246a10`.
- Python dependencies: `qdrant-client>=1.19.1`, `jieba>=0.42.1`; dev: `testcontainers[postgres,redis]` unchanged (Qdrant test container uses the generic `DockerContainer`).
- PostgreSQL image: `postgres:16` everywhere (Compose, CI, testcontainers).
- Namespace collection name: `Namespace.key()` = `cairn_vec_{kb_hex}_v{version}` (unchanged).
- Named vectors: dense `"dense"`, sparse `"sparse"`; hidden payload field `"_paths"`.
- BM25: `k1=1.2`, `b=0.75`, `avgdl=256`; index = first 4 bytes of `blake2b(token)` big-endian; `ENCODER_ID = "bm25-jieba-v1"`.
- Settings env: `CAIRN_QDRANT__URL` (default `http://localhost:6333`), `CAIRN_QDRANT__API_KEY`, `CAIRN_QDRANT__TIMEOUT_S` (default 10).
- Vector storage bindings accept only `driver="qdrant"` with empty config.
- Scores are always "higher is better"; Euclid distances are negated.
- Ordered filter comparisons with non-numeric operands raise `UnsupportedFilter`.
- Run commands from the repo root with `uv run`. Qdrant for local tests: `docker run -d --name cairn-qdrant-test -p 6333:6333 qdrant/qdrant:v1.19.1` and `export CAIRN_QDRANT__URL=http://localhost:6333`.

---

### Task 1: BM25 sparse encoder

**Files:**
- Modify: `pyproject.toml` (dependencies, mypy override), `uv.lock`
- Create: `src/cairn/embedding/sparse.py`
- Test: `tests/unit/embedding/test_sparse.py`

**Interfaces:**
- Produces: `cairn.embedding.sparse.ENCODER_ID: str`, `tokenize(text: str) -> list[str]`, `encode_document(text: str) -> SparseVector | None`, `encode_query(text: str) -> SparseVector | None`, `warm_up() -> None`.

- [ ] **Step 1: Add dependencies**

Run: `uv add "qdrant-client>=1.19.1" "jieba>=0.42.1"`
Then in `pyproject.toml` `[[tool.mypy.overrides]]` add `"jieba.*"` to `module`.

- [ ] **Step 2: Write the failing tests** — `tests/unit/embedding/test_sparse.py`

```python
from __future__ import annotations

from cairn.embedding.sparse import ENCODER_ID, encode_document, encode_query, tokenize


def weights(text: str) -> dict[int, float]:
    vector = encode_document(text)
    assert vector is not None
    return dict(zip(vector.indices, vector.values, strict=True))


def test_codes_are_indexed_whole_and_by_parts() -> None:
    tokens = tokenize("The XR-2200 controller needs firmware v4.1.")
    assert {"xr-2200", "xr", "2200", "v4.1", "v4", "1", "controller"} <= set(tokens)


def test_chinese_is_segmented_into_words_and_subwords() -> None:
    assert {"知识", "知识库", "检索"} <= set(tokenize("知识库检索"))


def test_mixed_script_and_width_are_normalised() -> None:
    assert tokenize("ＡＢＣ１２３") == ["abc123"]
    assert {"café", "déjà-vu", "déjà", "vu"} <= set(tokenize("Café déjà-vu"))
    assert {"api", "密钥"} <= set(tokenize("API密钥"))


def test_text_without_tokens_encodes_to_none() -> None:
    assert encode_document("，。!? ") is None
    assert encode_query("   ") is None


def test_encoding_is_deterministic_and_sorted() -> None:
    first = encode_document("Saturn has rings. 土星有光环。")
    assert first == encode_document("Saturn has rings. 土星有光环。")
    assert first is not None
    assert list(first.indices) == sorted(set(first.indices))
    assert all(0 <= index < 2**32 for index in first.indices)


def test_query_weights_are_one_per_unique_token() -> None:
    query = encode_query("rings rings saturn")
    assert query is not None
    assert len(query.indices) == 2
    assert set(query.values) == {1.0}


def test_term_frequency_saturates() -> None:
    doc = weights("alpha alpha alpha alpha beta")
    alpha = next(iter(weights("alpha")))
    beta = next(iter(weights("beta")))
    assert doc[beta] < doc[alpha] < 4 * doc[beta]


def test_query_tokens_hit_document_indices() -> None:
    query = encode_query("XR-2200")
    document = encode_document("The XR-2200 controller requires firmware 4.1.")
    assert query is not None and document is not None
    assert set(query.indices) <= set(document.indices)


def test_encoder_id_is_versioned() -> None:
    assert ENCODER_ID == "bm25-jieba-v1"
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/unit/embedding/test_sparse.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'cairn.embedding.sparse'`

- [ ] **Step 4: Implement** — `src/cairn/embedding/sparse.py`

```python
"""BM25 sparse vectors for lexical retrieval on Qdrant.

Qdrant keeps these in each namespace's ``sparse`` slot with the ``idf``
modifier, so the index supplies inverse document frequency at query time and
this module only produces term-frequency weights. Everything here is
deterministic — the same text always yields the same vector — which is what
lets an index written by one worker be queried by another process.

Chinese has no word boundaries, so CJK runs go through jieba's search mode,
which emits words *and* their sub-words (``知识库`` -> ``知识``, ``知识库``).
Other letters and digits are split on everything else, and code-like tokens
such as ``XR-2200`` are indexed whole and by their parts: an exact product code
is precisely the query that embeddings are weakest at.

``ENCODER_ID`` names this contract. Anything that changes which indices or
weights a text produces is a new ID, and an index built under the old one must
be rebuilt.
"""

from __future__ import annotations

import logging
import re
import threading
import unicodedata
from collections import Counter
from hashlib import blake2b

from cairn.vectorstore.base import SparseVector

__all__ = ["ENCODER_ID", "encode_document", "encode_query", "tokenize", "warm_up"]

ENCODER_ID = "bm25-jieba-v1"

_K1 = 1.2
_B = 0.75
#: Fixed rather than measured: a corpus-wide average would make a chunk's
#: vector depend on every other chunk, and so change under a rebuild.
_AVGDL = 256.0

_CJK = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_LETTER = rf"(?:(?![{_CJK}])[^\W_])"
_RUN = re.compile(rf"([{_CJK}]+)|({_LETTER}+(?:[-_./]{_LETTER}+)*)")
_PART = re.compile(rf"{_LETTER}+")

_lock = threading.Lock()
_ready = False


def warm_up() -> None:
    """Load jieba's dictionary (~1 s) now rather than inside the first request."""
    global _ready
    if _ready:
        return
    with _lock:
        if not _ready:
            import jieba

            jieba.setLogLevel(logging.WARNING)
            jieba.initialize()
            _ready = True


def _segment(run: str) -> list[str]:
    warm_up()
    import jieba

    return [token for token in jieba.cut_for_search(run) if token.strip()]


def tokenize(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).lower()
    tokens: list[str] = []
    for match in _RUN.finditer(normalized):
        cjk, word = match.groups()
        if cjk:
            tokens.extend(_segment(cjk))
            continue
        tokens.append(word)
        parts = _PART.findall(word)
        if len(parts) > 1:
            tokens.extend(parts)
    return tokens


def _index(token: str) -> int:
    return int.from_bytes(blake2b(token.encode(), digest_size=4).digest(), "big")


def _vector(weights: dict[int, float]) -> SparseVector | None:
    if not weights:
        return None
    ordered = sorted(weights.items())
    return SparseVector(
        indices=tuple(index for index, _ in ordered),
        values=tuple(value for _, value in ordered),
    )


def encode_document(text: str) -> SparseVector | None:
    tokens = tokenize(text)
    if not tokens:
        return None
    norm = _K1 * (1 - _B + _B * len(tokens) / _AVGDL)
    weights: dict[int, float] = {}
    for token, tf in Counter(tokens).items():
        index = _index(token)
        weights[index] = weights.get(index, 0.0) + tf * (_K1 + 1) / (tf + norm)
    return _vector(weights)


def encode_query(text: str) -> SparseVector | None:
    return _vector({_index(token): 1.0 for token in set(tokenize(text))})
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/unit/embedding/test_sparse.py -q`
Expected: 9 passed

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/cairn/embedding/sparse.py tests/unit/embedding/test_sparse.py
git commit -m "feat(embedding): add deterministic BM25 sparse encoder"
```

---

### Task 2: Qdrant driver and conformance

**Files:**
- Modify: `src/cairn/core/config.py` (QdrantSettings), `src/cairn/vectorstore/errors.py` (UnsupportedFilter)
- Create: `src/cairn/vectorstore/qdrant.py`
- Modify: `tests/contract/vectorstore/test_conformance.py`
- Test: `tests/unit/vectorstore/__init__.py`, `tests/unit/vectorstore/test_qdrant_translate.py`

**Interfaces:**
- Consumes: `encode_document`, `encode_query` (Task 1) in tests.
- Produces: `QdrantSettings(url: str, api_key: SecretStr | None, timeout_s: int)` at `Settings.qdrant`; `QdrantVectorStore(client: AsyncQdrantClient)`; `translate(node: FilterNode) -> models.Filter`; `QDRANT_CAPABILITIES`; `UnsupportedFilter(UnsupportedCapability)`.

- [ ] **Step 1: Settings and error**

`src/cairn/core/config.py`, after `ObjectStoreSettings`:

```python
class QdrantSettings(BaseModel):
    """The deployment's Qdrant. Vector bindings carry no connection details."""

    url: str = "http://localhost:6333"
    api_key: SecretStr | None = None
    timeout_s: int = Field(default=10, ge=1, le=120)
```

and in `Settings`: `qdrant: QdrantSettings = Field(default_factory=QdrantSettings)`; add `"QdrantSettings"` is not exported (module `__all__` lists only `Role`, `Settings`, `get_settings`; keep it).

`src/cairn/vectorstore/errors.py`: add to `__all__` and define

```python
class UnsupportedFilter(UnsupportedCapability):
    """A filter this driver cannot evaluate with the normative semantics.

    Refused rather than approximated: an approximation would return a result set
    that differs from every other driver's, silently.
    """

    code = "VECTOR_FILTER_UNSUPPORTED"
    title = "Filter not supported by this vector store"
```

- [ ] **Step 2: Failing translation unit tests** — `tests/unit/vectorstore/test_qdrant_translate.py` (plus empty `__init__.py`)

```python
from __future__ import annotations

import pytest
from qdrant_client import models

from cairn.vectorstore.errors import UnsupportedFilter
from cairn.vectorstore.filters import parse_filter
from cairn.vectorstore.qdrant import PATHS, translate


def test_numbers_compare_by_range_so_int_matches_float() -> None:
    compiled = translate(parse_filter({"meta.version": 3}))  # type: ignore[arg-type]
    condition = compiled.must[0]  # type: ignore[index]
    assert condition.range == models.Range(gte=3, lte=3)


def test_negative_operators_use_must_not() -> None:
    compiled = translate(parse_filter({"meta.lang": {"$ne": "en"}}))  # type: ignore[arg-type]
    assert compiled.must_not and not compiled.must


def test_exists_reads_the_hidden_paths_field() -> None:
    compiled = translate(parse_filter({"meta.tags": {"$exists": True}}))  # type: ignore[arg-type]
    condition = compiled.must[0]  # type: ignore[index]
    assert condition.key == PATHS
    assert condition.match == models.MatchValue(value="meta.tags")


@pytest.mark.parametrize(
    "spec",
    [
        {"meta.lang": {"$gt": "a"}},
        {"meta.flag": {"$lt": True}},
        {"meta.obj": {"$eq": {"nested": 1}}},
        {"meta.tags": {"$in": [["a"]]}},
    ],
)
def test_unrepresentable_filters_are_refused(spec: dict[str, object]) -> None:
    with pytest.raises(UnsupportedFilter):
        translate(parse_filter(spec))  # type: ignore[arg-type]
```

Run: `uv run pytest tests/unit/vectorstore -q` → FAIL (`No module named 'cairn.vectorstore.qdrant'`).

- [ ] **Step 3: Implement the driver** — `src/cairn/vectorstore/qdrant.py`

```python
"""Qdrant driver.

**One collection per namespace**, named ``cairn_vec_{kb_hex}_v{version}``.

Qdrant's usual multi-tenancy pattern — a shared collection partitioned by a
tenant payload index — is wrong here: the ``sparse`` slot uses the ``idf``
modifier, and Qdrant computes IDF over the *whole collection*, so a shared
collection would rank one knowledge base's terms by every other knowledge
base's vocabulary. A collection per namespace keeps term statistics per index
build and makes retiring a version ``delete_collection``, the O(1) drop that
blue/green reindex (ADR-0007) relies on. The cost is a ceiling of a few hundred
live namespaces per node (ADR-0009).

Filters follow ``vectorstore.filters`` exactly, with one recorded divergence:
an ordered comparison against an *array* of numbers matches when any element
does (the reference never matches arrays there). Qdrant cannot tell an absent
field from ``[]``, so every point carries a hidden ``_paths`` keyword array of
the payload paths it has; ``$exists`` matches against it, and it is stripped
from every payload this driver returns.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import httpx
from qdrant_client import AsyncQdrantClient, models
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

from cairn.core.logging import get_logger
from cairn.vectorstore.base import (
    Capabilities,
    HealthStatus,
    Hit,
    Metric,
    Namespace,
    NamespaceSpec,
    Point,
    UpsertResult,
    VectorQuery,
    validate_dimensions,
)
from cairn.vectorstore.errors import (
    DimensionMismatch,
    NamespaceNotFound,
    UnsupportedCapability,
    UnsupportedFilter,
    VectorStoreUnavailable,
)
from cairn.vectorstore.filters import OPERATORS, And, Compare, Exists, FilterNode, Not, Or

__all__ = ["DENSE", "PATHS", "QDRANT_CAPABILITIES", "SPARSE", "QdrantVectorStore", "translate"]

log = get_logger(__name__)

DENSE = "dense"
SPARSE = "sparse"
PATHS = "_paths"
_BATCH = 256

_DISTANCE: dict[str, models.Distance] = {
    "cosine": models.Distance.COSINE,
    "dot": models.Distance.DOT,
    "l2": models.Distance.EUCLID,
}
_METRIC: dict[models.Distance, Metric] = {
    models.Distance.COSINE: "cosine",
    models.Distance.DOT: "dot",
    models.Distance.EUCLID: "l2",
}

QDRANT_CAPABILITIES = Capabilities(
    driver="qdrant",
    sparse_vectors=True,
    quantization=frozenset({"none"}),
    payload_partitioning=False,
    filter_operators=OPERATORS,
    max_top_k=1000,
    fast_namespace_drop=True,
)


@asynccontextmanager
async def _backend(namespace: str) -> AsyncIterator[None]:
    """Map client failures onto the vector store's error vocabulary."""
    try:
        yield
    except UnexpectedResponse as exc:
        if exc.status_code == 404:
            raise NamespaceNotFound(f"Vector namespace {namespace} does not exist.") from exc
        raise VectorStoreUnavailable(
            f"Qdrant rejected the request (HTTP {exc.status_code})."
        ) from exc
    except (ResponseHandlingException, httpx.TransportError, OSError) as exc:
        raise VectorStoreUnavailable("Qdrant is not reachable.") from exc


class QdrantVectorStore:
    def __init__(self, client: AsyncQdrantClient) -> None:
        self._client = client
        self._specs: dict[str, NamespaceSpec] = {}

    def capabilities(self) -> Capabilities:
        return QDRANT_CAPABILITIES

    # --- namespace lifecycle ------------------------------------------------

    async def ensure_namespace(self, ns: Namespace, spec: NamespaceSpec) -> None:
        name = ns.key()
        options = dict(spec.driver_options)
        async with _backend(name):
            if not await self._client.collection_exists(name):
                try:
                    await self._client.create_collection(
                        name,
                        vectors_config={
                            DENSE: models.VectorParams(
                                size=spec.dim,
                                distance=_DISTANCE[spec.metric],
                                hnsw_config=models.HnswConfigDiff(
                                    m=int(options.get("hnsw_m", 16)),
                                    ef_construct=int(options.get("hnsw_ef_construction", 128)),
                                ),
                            )
                        },
                        sparse_vectors_config=(
                            {SPARSE: models.SparseVectorParams(modifier=models.Modifier.IDF)}
                            if spec.sparse
                            else None
                        ),
                        on_disk_payload=True,
                    )
                except UnexpectedResponse:
                    # Two index workers ensuring the same namespace race here;
                    # losing the race is success, anything else is not.
                    if not await self._client.collection_exists(name):
                        raise
                for field in ("document_id", PATHS):
                    await self._client.create_payload_index(
                        name, field, field_schema=models.PayloadSchemaType.KEYWORD, wait=True
                    )
        self._specs[name] = spec
        log.info(
            "vectorstore.namespace_ready",
            driver="qdrant",
            namespace=name,
            dim=spec.dim,
            metric=spec.metric,
        )

    async def namespace_exists(self, ns: Namespace) -> bool:
        async with _backend(ns.key()):
            return bool(await self._client.collection_exists(ns.key()))

    async def list_namespaces(self, kb_id: UUID) -> tuple[Namespace, ...]:
        prefix = f"cairn_vec_{kb_id.hex}_v"
        async with _backend(prefix):
            response = await self._client.get_collections()
        versions = sorted(
            int(suffix)
            for collection in response.collections
            if collection.name.startswith(prefix)
            and (suffix := collection.name.removeprefix(prefix)).isdigit()
            and int(suffix) > 0
        )
        return tuple(Namespace(kb_id, version) for version in versions)

    async def drop_namespace(self, ns: Namespace) -> None:
        name = ns.key()
        async with _backend(name):
            if await self._client.collection_exists(name):
                await self._client.delete_collection(name)
        self._specs.pop(name, None)
        log.info("vectorstore.namespace_dropped", driver="qdrant", namespace=name)

    async def _spec_for(self, ns: Namespace) -> NamespaceSpec:
        name = ns.key()
        cached = self._specs.get(name)
        if cached is not None:
            return cached
        async with _backend(name):
            info = await self._client.get_collection(name)
        vectors = info.config.params.vectors
        dense = vectors.get(DENSE) if isinstance(vectors, Mapping) else None
        if dense is None:
            raise NamespaceNotFound(f"Vector namespace {name} has no dense vector.")
        spec = NamespaceSpec(
            dim=dense.size,
            metric=_METRIC[dense.distance],
            sparse=bool(info.config.params.sparse_vectors),
        )
        self._specs[name] = spec
        return spec

    # --- writes -------------------------------------------------------------

    async def upsert(self, ns: Namespace, points: Sequence[Point]) -> UpsertResult:
        name = ns.key()
        if not points:
            return UpsertResult(written=0, namespace=name)
        spec = await self._spec_for(ns)
        validate_dimensions(points, spec.dim, name)
        structs = [_point_struct(point, spec, name) for point in points]
        async with _backend(name):
            for offset in range(0, len(structs), _BATCH):
                await self._client.upsert(name, points=structs[offset : offset + _BATCH], wait=True)
        return UpsertResult(written=len(structs), namespace=name)

    async def delete(
        self,
        ns: Namespace,
        *,
        ids: Sequence[UUID] | None = None,
        filter: FilterNode | None = None,
    ) -> int:
        if ids is None and filter is None:
            raise ValueError("delete requires either ids or a filter")
        if ids is not None and not ids:
            return 0
        conditions: list[Any] = []
        if ids is not None:
            conditions.append(models.HasIdCondition(has_id=[str(point_id) for point_id in ids]))
        if filter is not None:
            conditions.append(translate(filter))
        selector = models.Filter(must=conditions)
        name = ns.key()
        async with _backend(name):
            matched = (
                await self._client.count(name, count_filter=selector, exact=True)
            ).count
            if matched:
                await self._client.delete(
                    name, points_selector=models.FilterSelector(filter=selector), wait=True
                )
        return int(matched)

    # --- reads --------------------------------------------------------------

    async def search(self, ns: Namespace, query: VectorQuery) -> list[Hit]:
        if (query.dense is None) == (query.sparse is None):
            raise ValueError("search takes exactly one of a dense or a sparse query vector")
        name = ns.key()
        spec = await self._spec_for(ns)
        vector: Any
        if query.sparse is not None:
            if not spec.sparse:
                raise UnsupportedCapability(f"Vector namespace {name} has no sparse vectors.")
            if not query.sparse.indices:
                return []
            vector = models.SparseVector(
                indices=list(query.sparse.indices), values=list(query.sparse.values)
            )
            using = SPARSE
        else:
            assert query.dense is not None
            vector = [float(value) for value in query.dense]
            if len(vector) != spec.dim:
                raise DimensionMismatch(
                    f"Expected a {spec.dim}-dimensional query vector, received {len(vector)}.",
                    namespace=name,
                )
            using = DENSE
        async with _backend(name):
            response = await self._client.query_points(
                name,
                query=vector,
                using=using,
                query_filter=translate(query.filter) if query.filter is not None else None,
                search_params=(
                    models.SearchParams(hnsw_ef=query.ef_search)
                    if query.ef_search is not None
                    else None
                ),
                limit=min(query.top_k, QDRANT_CAPABILITIES.max_top_k),
                with_payload=query.with_payload,
            )
        # Normalise every metric to "higher is better" so callers and fusion
        # never need to know which backend produced a score.
        sign = -1.0 if using == DENSE and spec.metric == "l2" else 1.0
        hits = [
            Hit(
                id=UUID(str(scored.id)),
                score=sign * float(scored.score),
                payload=_public(scored.payload) if query.with_payload else {},
            )
            for scored in response.points
        ]
        if query.score_threshold is not None:
            hits = [hit for hit in hits if hit.score >= query.score_threshold]
        return hits

    async def fetch(self, ns: Namespace, ids: Sequence[UUID]) -> list[Hit]:
        if not ids:
            return []
        name = ns.key()
        async with _backend(name):
            records = await self._client.retrieve(
                name, ids=[str(point_id) for point_id in ids], with_payload=True
            )
        return [
            Hit(id=UUID(str(record.id)), score=0.0, payload=_public(record.payload))
            for record in records
        ]

    async def count(self, ns: Namespace, filter: FilterNode | None = None) -> int:
        name = ns.key()
        async with _backend(name):
            result = await self._client.count(
                name,
                count_filter=translate(filter) if filter is not None else None,
                exact=True,
            )
        return int(result.count)

    async def health(self) -> HealthStatus:
        started = time.perf_counter()
        try:
            await self._client.get_collections()
        except Exception as exc:
            return HealthStatus(healthy=False, detail=type(exc).__name__)
        return HealthStatus(healthy=True, latency_ms=(time.perf_counter() - started) * 1000)

    async def close(self) -> None:
        await self._client.close()


# --- points ------------------------------------------------------------------


def _paths(payload: Mapping[str, Any], prefix: str = "") -> Iterator[str]:
    for key, value in payload.items():
        path = f"{prefix}{key}"
        yield path
        if isinstance(value, Mapping):
            yield from _paths(value, f"{path}.")


def _point_struct(point: Point, spec: NamespaceSpec, namespace: str) -> models.PointStruct:
    # Round-trip through JSON so UUIDs and datetimes are stored as strings,
    # exactly as the read model has always stored them.
    payload: dict[str, Any] = json.loads(json.dumps(dict(point.payload), default=str))
    payload[PATHS] = sorted(set(_paths(payload)))
    vector: dict[str, Any] = {DENSE: [float(value) for value in point.dense]}
    if point.sparse is not None and point.sparse.indices:
        if not spec.sparse:
            raise UnsupportedCapability(f"Vector namespace {namespace} has no sparse vectors.")
        vector[SPARSE] = models.SparseVector(
            indices=list(point.sparse.indices), values=list(point.sparse.values)
        )
    return models.PointStruct(id=str(point.id), vector=vector, payload=payload)


def _public(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    return {key: value for key, value in (payload or {}).items() if key != PATHS}


# --- filter translation ------------------------------------------------------

#: Matches nothing: the negation of the empty filter, which matches everything.
_NOTHING = models.Filter(must_not=[models.Filter()])
_BOUND = {"$gt": "gt", "$gte": "gte", "$lt": "lt", "$lte": "lte"}


def translate(node: FilterNode) -> models.Filter:
    """Compile a filter AST to a Qdrant filter with the normative semantics.

    Every divergence from ``vectorstore.filters.matches`` is a correctness bug,
    which is why the conformance suite runs the same corpus through both.
    """
    if isinstance(node, And):
        return models.Filter(must=[translate(clause) for clause in node.clauses])
    if isinstance(node, Or):
        if not node.clauses:
            return _NOTHING
        return models.Filter(should=[translate(clause) for clause in node.clauses])
    if isinstance(node, Not):
        return models.Filter(must_not=[translate(node.clause)])
    if isinstance(node, Exists):
        present = _match(PATHS, node.field)
        return models.Filter(must=[present]) if node.present else models.Filter(must_not=[present])
    return _compare(node)


def _compare(node: Compare) -> models.Filter:
    if node.op == "$eq":
        return _eq(node.field, node.value)
    if node.op == "$ne":
        # must_not admits documents without the field: absent is "not equal".
        return models.Filter(must_not=[_eq(node.field, node.value)])
    if node.op in ("$in", "$nin"):
        any_of = (
            models.Filter(should=[_eq(node.field, value) for value in node.value])
            if node.value
            else _NOTHING
        )
        return any_of if node.op == "$in" else models.Filter(must_not=[any_of])
    value = node.value
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise UnsupportedFilter(
            f"Ordered comparison on {node.field!r} needs a number; Qdrant cannot "
            "order strings, booleans or dates."
        )
    return models.Filter(
        must=[
            models.FieldCondition(
                key=node.field, range=models.Range(**{_BOUND[node.op]: value})
            )
        ]
    )


def _eq(field: str, value: Any) -> models.Filter:
    if value is None:
        return models.Filter(
            must=[models.IsNullCondition(is_null=models.PayloadField(key=field))]
        )
    if isinstance(value, bool | str):
        return models.Filter(must=[_match(field, value)])
    if isinstance(value, int | float):
        # A range rather than a match: `3` must equal a stored `3.0`, as it
        # does in the reference implementation.
        return models.Filter(
            must=[models.FieldCondition(key=field, range=models.Range(gte=value, lte=value))]
        )
    raise UnsupportedFilter(f"Equality on {field!r} needs a string, number, boolean or null.")


def _match(field: str, value: bool | str) -> models.FieldCondition:
    return models.FieldCondition(key=field, match=models.MatchValue(value=value))
```

Run: `uv run pytest tests/unit/vectorstore -q` → 7 passed.

- [ ] **Step 4: Conformance — add the Qdrant driver and new cases**

In `tests/contract/vectorstore/test_conformance.py`:

1. Add the driver and register it:

```python
async def _qdrant() -> AsyncIterator[object]:
    url = os.environ.get("CAIRN_QDRANT__URL")
    if url is None and os.environ.get("CAIRN_TEST_USE_EXTERNAL_SERVICES") != "1":
        pytest.skip("qdrant conformance needs CAIRN_QDRANT__URL")

    from qdrant_client import AsyncQdrantClient

    from cairn.vectorstore.qdrant import QdrantVectorStore

    client = AsyncQdrantClient(
        url=url or "http://localhost:6333",
        api_key=os.environ.get("CAIRN_QDRANT__API_KEY"),
        timeout=10,
    )
    store = QdrantVectorStore(client)
    status = await store.health()
    if not status.healthy:
        await store.close()
        pytest.skip(f"qdrant unavailable: {status.detail}")
    yield store
    await store.close()


DRIVERS = {"fake": _fake, "pgvector": _pgvector, "qdrant": _qdrant}
```

2. In `test_upsert_then_fetch_round_trips_the_payload` add `assert "_paths" not in hits[0].payload`.

3. Replace `test_lexical_search_finds_what_dense_search_misses` with:

```python
async def test_sparse_search_finds_what_dense_search_misses(store, namespace) -> None:  # type: ignore[no-untyped-def]
    """TC-M05-09 — the reason hybrid exists.

    An exact product code or acronym is precisely where embeddings are weak.
    """
    if not store.capabilities().sparse_vectors:
        pytest.skip("driver has no sparse vectors")
    code = "The XR-2200 controller requires firmware 4.1."
    general = "General information about hardware maintenance."
    await store.upsert(
        namespace,
        [
            replace(point({"content": code}, dense=vec(0.0, 1.0)), sparse=encode_document(code)),
            replace(
                point({"content": general}, dense=vec(1.0, 0.0)), sparse=encode_document(general)
            ),
        ],
    )
    hits = await store.search(namespace, VectorQuery(sparse=encode_query("XR-2200"), top_k=5))
    assert [hit.payload["content"] for hit in hits] == [code]
```

with `from cairn.embedding.sparse import encode_document, encode_query` at the top.

4. Add edge-case filters and L2 direction:

```python
EDGE_CORPUS = [
    {"content": "null-tags", "meta": {"tags": None}},
    {"content": "empty-tags", "meta": {"tags": []}},
    {"content": "float-version", "meta": {"version": 3.0}},
    {"content": "absent"},
]


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ({"meta.tags": {"$exists": True}}, {"null-tags", "empty-tags"}),
        ({"meta.tags": {"$exists": False}}, {"float-version", "absent"}),
        ({"meta.version": 3}, {"float-version"}),
        ({"meta.tags": None}, {"null-tags"}),
        ({"meta.version": {"$in": [3, "three"]}}, {"float-version"}),
        ({"meta.version": {"$nin": []}}, {"null-tags", "empty-tags", "float-version", "absent"}),
        ({"$or": []}, set()),
    ],
)
async def test_edge_filters_agree_across_drivers(  # type: ignore[no-untyped-def]
    store, namespace, spec: dict[str, object], expected: set[str]
) -> None:
    await store.upsert(namespace, [point(dict(row)) for row in EDGE_CORPUS])
    hits = await store.search(
        namespace, VectorQuery(dense=vec(1.0), top_k=50, filter=parse_filter(spec))
    )
    assert {hit.payload["content"] for hit in hits} == expected


async def test_l2_scores_are_higher_for_nearer_points(store) -> None:  # type: ignore[no-untyped-def]
    ns = Namespace(kb_id=uuid4(), index_version=1)
    await store.ensure_namespace(ns, NamespaceSpec(dim=DIM, metric="l2"))
    try:
        await store.upsert(
            ns,
            [
                point({"content": "near"}, dense=vec(1.0, 0.0)),
                point({"content": "far"}, dense=vec(5.0, 0.0)),
            ],
        )
        hits = await store.search(ns, VectorQuery(dense=vec(1.0, 0.0), top_k=2))
        assert [hit.payload["content"] for hit in hits] == ["near", "far"]
        assert hits[0].score > hits[1].score
        assert hits[0].score <= 0
    finally:
        await store.drop_namespace(ns)
```

Note: `{"$or": []}` parses to `Or(())`; `point()` adds `document_id`/`content`, and `EDGE_CORPUS` rows already carry `content`.

- [ ] **Step 5: Run conformance against a live Qdrant**

```bash
docker run -d --name cairn-qdrant-test -p 6333:6333 qdrant/qdrant:v1.19.1
CAIRN_QDRANT__URL=http://localhost:6333 uv run pytest tests/contract/vectorstore tests/unit/vectorstore -q
```
Expected: all fake and qdrant cases pass; pgvector cases skip (no database configured).

- [ ] **Step 6: Commit**

```bash
git add src/cairn/core/config.py src/cairn/vectorstore tests/contract tests/unit/vectorstore
git commit -m "feat(vectorstore): add Qdrant driver with exact filter semantics"
```

---

### Task 3: Cut over from pgvector to Qdrant

**Files:**
- Delete: `src/cairn/vectorstore/pgvector.py`
- Modify: `src/cairn/vectorstore/base.py` (remove `VectorQuery.text`), `src/cairn/vectorstore/registry.py`, `tests/fakes/vectorstore.py`, `tests/contract/vectorstore/test_conformance.py` (remove pgvector driver and the sparse skip)
- Modify: `src/cairn/catalog/router.py:741-750`, `src/cairn/retrieval/service.py:465-480`, `src/cairn/retrieval/router.py:34`, `src/cairn/ingestion/pipeline.py:565-592,695-705`, `apps/api/main.py` lifespan, worker startup for the index queue
- Create: `migrations/versions/0013_qdrant.py`; Modify: `migrations/versions/0004_pgvector.py`
- Modify: `pyproject.toml` (drop the pgvector S608 exemption)
- Modify tests: `tests/unit/retrieval/test_service.py`, `tests/unit/retrieval/test_runtime_contract.py`, `tests/unit/ingestion/test_chunk_reembed.py`, `tests/unit/ingestion/test_parse_reuse.py`, `tests/integration/test_catalog.py` (driver strings `"pgvector"` → `"qdrant"`)

**Interfaces:**
- Consumes: `encode_document`, `encode_query`, `warm_up` (Task 1); `QdrantVectorStore`, `Settings.qdrant` (Task 2).
- Produces: `VectorQuery(top_k, dense, sparse, filter, with_payload, ef_search, score_threshold)` — no `text`. Registry resolves `driver == "qdrant"` only.

- [ ] **Step 1: Failing retrieval tests** (`tests/unit/retrieval/test_service.py`)

Change line 379 to `assert store.calls[0][1].sparse is None`, and add:

```python
@pytest.mark.anyio
async def test_fulltext_searches_with_a_bm25_query_vector() -> None:
    store = Store()
    service = RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: runtime()}),
        vectors=Registry(store),
        embeddings=Embeddings(),
    )

    await service.query(principal(ALLOWED_KB_ID), request("fulltext"), request_id="req_test")

    query = store.calls[0][1]
    assert query.dense is None
    assert query.sparse == encode_query("Saturn")


@pytest.mark.anyio
async def test_fulltext_query_without_terms_searches_nothing() -> None:
    store = Store()
    service = RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: runtime()}),
        vectors=Registry(store),
        embeddings=Embeddings(),
    )

    response = await service.query(
        principal(ALLOWED_KB_ID), request("fulltext", query="?!"), request_id="req_test"
    )

    assert store.calls == []
    assert response.results == []
```

with `from cairn.embedding.sparse import encode_query`. Run: `uv run pytest tests/unit/retrieval/test_service.py -q` → FAIL (`VectorQuery` has `text`, not a sparse query).

- [ ] **Step 2: Contract change**

`src/cairn/vectorstore/base.py` — replace the `VectorQuery` docstring and fields:

```python
@dataclass(frozen=True, slots=True)
class VectorQuery:
    """One search against one namespace: exactly one of ``dense`` or ``sparse``.

    Lexical search is a sparse query (``cairn.embedding.sparse``); the driver
    never sees query text.
    """

    top_k: int = 10
    dense: Sequence[float] | None = None
    sparse: SparseVector | None = None
    filter: FilterNode | None = None
    with_payload: bool = True
    #: Accuracy/latency knob. Higher searches more of the graph.
    ef_search: int | None = None
    score_threshold: float | None = None
```

`tests/fakes/vectorstore.py` — in `search`, replace the scoring branch with:

```python
        if (query.dense is None) == (query.sparse is None):
            raise ValueError("search takes exactly one of a dense or a sparse query vector")
        scored: list[Hit] = []
        for point in candidates:
            if query.dense is not None:
                score = _score(query.dense, point.dense, spec.metric)
            else:
                score = _sparse_dot(query, point)
                if score <= 0:
                    continue
```

and delete `_lexical`. In the conformance file delete `_pgvector`, set `DRIVERS = {"fake": _fake, "qdrant": _qdrant}` and remove the `sparse_vectors` skip in TC-M05-09.

- [ ] **Step 3: Registry** — `src/cairn/vectorstore/registry.py`

Module docstring: `"""Vector driver resolution. Every binding resolves to the deployment's Qdrant (ADR-0009)."""`. Replace `_build`:

```python
    async def _build(self, binding: VectorBindingRef) -> VectorStore:
        if binding.driver != "qdrant":
            raise VectorStoreUnavailable(f"Unknown vector store driver: {binding.driver!r}")
        from qdrant_client import AsyncQdrantClient

        from cairn.core.config import get_settings
        from cairn.vectorstore.qdrant import QdrantVectorStore

        settings = get_settings().qdrant
        with warnings.catch_warnings():
            # The client warns when an API key travels over plain HTTP. Inside
            # the Compose network that is the configured, intended transport.
            warnings.simplefilter("ignore", UserWarning)
            client = AsyncQdrantClient(
                url=settings.url,
                api_key=settings.api_key.get_secret_value() if settings.api_key else None,
                timeout=settings.timeout_s,
            )
        return QdrantVectorStore(client)
```

In `for_binding`, close a store that fails its health check before raising:

```python
        store = await self._build(binding)
        status = await store.health()
        if not status.healthy:
            await store.close()
            raise VectorStoreUnavailable(
                f"The {binding.driver} vector store is not reachable: {status.detail}"
            )
```

Add `import warnings`. Delete `src/cairn/vectorstore/pgvector.py` and the S608 block for it in `pyproject.toml`.

- [ ] **Step 4: Binding validation** — `src/cairn/catalog/router.py`

```python
def _validate_binding_config(body: CreateBindingRequest) -> None:
    expected_driver = "local" if body.kind == "object" else "qdrant"
    if body.driver != expected_driver:
        raise ValidationFailed(
            f"The first-product {body.kind} binding driver must be {expected_driver!r}."
        )
    if body.kind == "vector":
        if body.config:
            raise ValidationFailed(
                "Qdrant connection details come from the deployment configuration."
            )
        return
```

- [ ] **Step 5: Ingestion writes sparse vectors** — `src/cairn/ingestion/pipeline.py`

Import `from cairn.embedding.sparse import encode_document`. In `_points`:

```python
        points.append(
            Point(
                id=chunk.id,
                dense=values,
                payload=payload,
                sparse=encode_document(chunk.content),
            )
        )
```

In `_chunk_reembed_point`:

```python
    return Point(
        id=target.chunk_id,
        dense=values,
        payload=_chunk_reembed_payload(target, actual_token_count=actual_token_count),
        sparse=encode_document(target.content),
    )
```

- [ ] **Step 6: Retrieval queries sparse vectors** — `src/cairn/retrieval/service.py`

Import `from cairn.embedding.sparse import encode_query`. Replace the full-text branch in `_search_target`:

```python
        if mode in {"fulltext", "hybrid"}:
            if request.query is None:
                raise ValidationFailed("Full-text retrieval requires query text.")
            sparse = encode_query(request.query)
            # A query with no indexable terms ("?!") has nothing to match
            # lexically; it contributes no hits rather than an error.
            if sparse is not None:
                searches.append(
                    (
                        "sparse",
                        asyncio.create_task(
                            self._bounded_search(
                                store,
                                namespace,
                                VectorQuery(top_k=candidate_k, sparse=sparse, with_payload=True),
                            )
                        ),
                        target_weight,
                    )
                )
```

`src/cairn/retrieval/router.py:34`: `"Requires kb:query on every target. Supports bounded full-text (BM25), vector, "`.

- [ ] **Step 7: Warm up the tokenizer outside the request path**

`apps/api/main.py` lifespan, before `yield` (inside the `try`):

```python
        if settings.serves_data_plane:
            # jieba loads its dictionary on first use (~1 s); pay that at start-up,
            # not inside the first full-text query's timeout.
            from cairn.embedding.sparse import warm_up

            await asyncio.to_thread(warm_up)
```

Add `import asyncio`. Find the index/embed worker start in `apps/worker/main.py` and make the same `await asyncio.to_thread(warm_up)` call before the worker loop starts when `settings.tasks.queue in {"index", "embed"}`.

- [ ] **Step 8: Migrations**

`migrations/versions/0004_pgvector.py` — `upgrade()` becomes a no-op with the docstring note: "Superseded by Qdrant (ADR-0009, 0013_qdrant). Kept as a no-op so existing databases keep a linear history and a plain PostgreSQL image can run the chain."

Create `migrations/versions/0013_qdrant.py`:

```python
"""vectorstore: move vectors to Qdrant

Revision ID: 0013_qdrant
Revises: 0012_mcp_management
Create Date: 2026-09-24

Vectors now live in Qdrant (ADR-0009). This drops any per-namespace pgvector
tables left behind, drops the extension, and points existing vector bindings at
the Qdrant driver. Nothing is copied: indexes are rebuildable from chunks, and
no deployment carries vector data that must survive the switch.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013_qdrant"
down_revision: str | None = "0012_mcp_management"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        r"""
        DO $$
        DECLARE r record;
        BEGIN
          FOR r IN
            SELECT tablename FROM pg_tables
             WHERE schemaname = current_schema() AND tablename LIKE 'cairn\_vec\_%'
          LOOP
            EXECUTE format('DROP TABLE IF EXISTS %I', r.tablename);
          END LOOP;
        END $$;
        """
    )
    op.execute("DROP EXTENSION IF EXISTS vector")
    op.execute(
        "UPDATE storage_binding SET driver = 'qdrant', config = '{}'::jsonb "
        "WHERE kind = 'vector' AND driver = 'pgvector'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE storage_binding SET driver = 'pgvector' "
        "WHERE kind = 'vector' AND driver = 'qdrant'"
    )
```

- [ ] **Step 9: Replace driver strings in tests**

In `tests/unit/retrieval/test_service.py`, `tests/unit/retrieval/test_runtime_contract.py` (both the JSON literal and `BindingRefModel`), `tests/unit/ingestion/test_chunk_reembed.py`, `tests/unit/ingestion/test_parse_reuse.py`, `tests/integration/test_catalog.py` (lines 122 and 428), `tests/integration/test_ingestion_pipeline.py:168`, `tests/integration/test_live_model_pipeline.py:101`: `"pgvector"` → `"qdrant"`.

- [ ] **Step 10: Run unit and contract suites + static checks**

```bash
CAIRN_QDRANT__URL=http://localhost:6333 uv run pytest tests/unit tests/contract -q
uv run ruff check src apps tests migrations && uv run ruff format --check src apps tests
uv run mypy && uv run lint-imports --config .importlinter
```
Expected: all pass. Fix any test that compared `Point`s built without `sparse` by adding `sparse=encode_document(<content>)` to the expected value.

- [ ] **Step 11: Commit**

```bash
git add -A src apps migrations tests pyproject.toml
git commit -m "feat: make Qdrant the only vector backend"
```

---

### Task 4: Integration test infrastructure on Qdrant

**Files:**
- Modify: `tests/conftest.py`, `tests/integration/conftest.py`, `tests/integration/test_ingestion_pipeline.py`, `tests/integration/test_live_model_pipeline.py`, any other integration test found by `grep -rn "pgvector\|PgVectorStore\|get_engine())" tests/integration`

**Interfaces:**
- Produces: session fixture `qdrant_url() -> str`; integration env sets `CAIRN_QDRANT__URL`; helper `tests/integration/conftest.py::qdrant_store() -> QdrantVectorStore` factory.

- [ ] **Step 1: Root fixtures** — `tests/conftest.py`

Change `PostgresContainer("pgvector/pgvector:pg16", ...)` to `PostgresContainer("postgres:16", driver="asyncpg")` and add:

```python
QDRANT_IMAGE = "qdrant/qdrant:v1.19.1"


@pytest.fixture(scope="session")
def qdrant_url() -> Iterator[str]:
    """A live Qdrant REST endpoint."""
    if _use_external():
        yield os.environ.get("CAIRN_QDRANT__URL", "http://localhost:6333")
        return

    try:
        from testcontainers.core.container import DockerContainer
    except ImportError:  # pragma: no cover
        pytest.skip("testcontainers is not installed and no external services configured")

    import httpx

    with DockerContainer(QDRANT_IMAGE).with_exposed_ports(6333) as container:
        url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(6333)}"
        deadline = time.monotonic() + 60
        while True:
            try:
                if httpx.get(f"{url}/readyz", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError("Qdrant test container did not become ready")
            time.sleep(0.5)
        yield url
```

Add `import time`.

- [ ] **Step 2: Integration environment** — `tests/integration/conftest.py`

`_environment(postgres_url, redis_url, qdrant_url)` sets `os.environ["CAIRN_QDRANT__URL"] = qdrant_url`. In `_dispose` also run:

```python
    from cairn.vectorstore.registry import get_vector_registry

    await get_vector_registry().close()
```

Add a factory used by tests that previously built `PgVectorStore(get_engine())`:

```python
def qdrant_store():  # type: ignore[no-untyped-def]  # reason: returns QdrantVectorStore
    from qdrant_client import AsyncQdrantClient

    from cairn.vectorstore.qdrant import QdrantVectorStore

    return QdrantVectorStore(AsyncQdrantClient(url=os.environ["CAIRN_QDRANT__URL"], timeout=10))
```

- [ ] **Step 3: Port tests off PgVectorStore**

`tests/integration/test_ingestion_pipeline.py`: replace the import with `from cairn.vectorstore.qdrant import QdrantVectorStore`, the type hint in `_FailAfterVectorUpsert.__init__` with `QdrantVectorStore`, and `PgVectorStore(get_engine())` (line 415) with `qdrant_store()` imported from `tests.integration.conftest`.
`tests/integration/test_live_model_pipeline.py`: import `QdrantVectorStore`; lines 155/165 patch `QdrantVectorStore.upsert`; line 176 uses `qdrant_store()`. Any `VectorQuery(text=...)` there becomes `VectorQuery(sparse=encode_query(...))`.
Remove now-unused `get_engine` imports.

- [ ] **Step 4: Run the full backend suite against real services**

```bash
uv run pytest tests -q
```
(testcontainers starts postgres:16, redis and Qdrant.) Expected: 0 failures, 0 errors. Then with real TEI as in the verification stack memory (live-model tests enabled) — same command with the TEI env vars used on 2026-09-22.

- [ ] **Step 5: Commit**

```bash
git add tests
git commit -m "test: run integration suite on PostgreSQL 16 and Qdrant"
```

---

### Task 5: Deployment, CI and scripts

**Files:**
- Modify: `docker-compose.yml`, `scripts/init_env.py`, `.env.example`, `.github/workflows/ci.yml`, `scripts/agent_mcp_acceptance.py:156`

- [ ] **Step 1: Compose**

`postgres.image: postgres:16`. Add service:

```yaml
  qdrant:
    image: qdrant/qdrant:v1.19.1@sha256:12364fe851b9f17356fc88189fc06d1b521262e04659ec7345975b00c9246a10
    restart: unless-stopped
    environment:
      QDRANT__SERVICE__API_KEY: ${QDRANT_API_KEY:?set QDRANT_API_KEY in .env}
      QDRANT__TELEMETRY_DISABLED: "true"
    volumes: ["qdrantdata:/qdrant/storage"]
    healthcheck:
      # The image ships bash but neither curl nor wget.
      test: ["CMD", "bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/6333 && printf 'GET /readyz HTTP/1.0\\r\\n\\r\\n' >&3 && grep -q ' 200 ' <&3"]
      interval: 5s
      timeout: 3s
      retries: 20
```

In `x-app.environment` add `CAIRN_QDRANT__URL: http://qdrant:6333` and `CAIRN_QDRANT__API_KEY: ${QDRANT_API_KEY:?set QDRANT_API_KEY in .env}`; in `x-app.depends_on` add `qdrant: {condition: service_healthy}`; in `migrate.depends_on` add the same; add `qdrantdata:` under `volumes`.

- [ ] **Step 2: Environment generation**

`scripts/init_env.py` values: `"QDRANT_API_KEY": secrets.token_urlsafe(32),`. `.env.example`: add a `# --- qdrant ---` block with `QDRANT_API_KEY=change-me` and commented `# CAIRN_QDRANT__URL=http://qdrant:6333`, `# CAIRN_QDRANT__TIMEOUT_S=10`; retitle `# --- postgres container ---` unchanged.

- [ ] **Step 3: CI**

`integration.services.postgres.image: postgres:16`; add

```yaml
      qdrant:
        image: qdrant/qdrant:v1.19.1
        ports: ["6333:6333"]
```

top-level `env`: `CAIRN_QDRANT__URL: "http://localhost:6333"`; comment on the pytest step: "This also exercises the real Qdrant contracts."

- [ ] **Step 4: Scripts**

`scripts/agent_mcp_acceptance.py:156`: `"driver": "qdrant"`. `grep -rn "pgvector" scripts deploy` must return nothing.

- [ ] **Step 5: Validate and commit**

```bash
docker compose config -q
git add docker-compose.yml scripts .env.example .github/workflows/ci.yml
git commit -m "deploy: run Qdrant in Compose and CI, plain PostgreSQL 16"
```

---

### Task 6: Web client

**Files:**
- Modify: `apps/web/src/features/storage/StorageView.vue`, `apps/web/src/locales/zh-CN.json`, `apps/web/src/locales/en-US.json`, `apps/web/openapi.json`, `apps/web/src/api/schema.d.ts`, affected specs under `apps/web/tests/`

- [ ] **Step 1:** In `StorageView.vue` change the header comment to "Local objects and Qdrant are the drivers this build ships", `driver: form.kind === "object" ? "local" : "qdrant"`, and rename key usages `storage.pgvectorHint` → `storage.qdrantHint`.
- [ ] **Step 2:** Locales: replace `pgvectorHint` with `"qdrantHint": "Vectors and full-text terms in the platform's Qdrant."` (en-US) and `"qdrantHint": "向量与全文词项存放在平台的 Qdrant 中。"` (zh-CN).
- [ ] **Step 3:** Regenerate the API spec and types with the project script (`npm run api:check` shows the command it diffs against; run its generate counterpart, e.g. `npm run api:generate`) so the retrieval description change lands in `openapi.json` and `schema.d.ts`.
- [ ] **Step 4:** Update any web test asserting `"pgvector"`; run

```bash
cd apps/web && npm run api:check && npx vue-tsc --noEmit && npx vitest run && node scripts/check-i18n.mjs && npx vite build
```
Expected: all pass.
- [ ] **Step 5:** Commit: `git commit -am "feat(web): offer the Qdrant vector driver"`

---

### Task 7: Documentation

**Files:**
- Create: `docs/01-architecture/05-adr/ADR-0009-qdrant-only-vector-backend.md`
- Modify: `docs/01-architecture/05-adr/ADR-0004-vector-store-abstraction.md` (status line: "Amended by ADR-0009"), `docs/01-architecture/02-technology-stack.md` (§2 rows: vector → Qdrant only; CJK full-text → jieba BM25 sparse; §1 testing row), `docs/02-modules/M05-vectorstore.md` (phase line, §6 layout note, §7 pgvector section replaced by a pointer to ADR-0009, §8 reflects per-namespace collections + BM25), `README.md`, `docs/06-ops/01-deployment.md`, `docs/06-ops/04-local-product-quickstart.md`, `docs/06-ops/07-pdf-and-retrieval.md`, `docs/04-plan/15-execution-ledger.md`, `docs/README.md`

- [ ] **Step 1:** ADR-0009 with Context (product decision to run one vector backend; no vector data to migrate), Decision (Qdrant only; collection per namespace; BM25 sparse with IDF modifier; `_paths` for `$exists`; string ordering refused), Consequences (+ native sparse, one lexical implementation for CJK; − Qdrant is a required service, per-namespace collection ceiling, learned sparse and hosted vendors are sub-projects 2 and 3), Alternatives rejected (keep pgvector default; shared collection — IDF pollution; Qdrant full-text payload index — no ranking).
- [ ] **Step 2:** Update the listed docs; `grep -rn "pgvector" docs README.md` should only return historical records (execution ledger, earlier plan docs, ADR-0004 context) and ADR-0009.
- [ ] **Step 3:** Ledger entry for 2026-09-24 with the verification results from Task 8.
- [ ] **Step 4:** Commit: `git commit -am "docs: record Qdrant as the only vector backend"`

---

### Task 8: End-to-end verification

- [ ] **Step 1:** Full static + test gate: ruff, ruff format, mypy, import-linter, `scripts/check_route_authz.py`, `uv run pytest tests -q` (record pass count and coverage), web gate from Task 6.
- [ ] **Step 2:** `docker compose down -v` for the Cairn stack (no data kept), regenerate `.env` if it lacks `QDRANT_API_KEY` (append a generated key), `docker compose --profile local-model up -d --build`; wait for all services healthy; `curl -fsS http://127.0.0.1:8080/readyz`.
- [ ] **Step 3:** Run `scripts/browser_ui_acceptance.py` against the stack (storage step creates the Qdrant binding); expect 24/24 and zero console errors. Also a direct API check: upload a document containing `XR-2200` and Chinese text, then `fulltext`, `vector` and `hybrid` queries each return it.
- [ ] **Step 4:** Record results in the ledger (Task 7 Step 3) and commit.

---

### Task 9: Company fork sync

- [ ] **Step 1:** Port the same changes to `D:/code/data-platform` with the existing sync approach (`data/fork_sync.py` pattern), preserving 知服 branding and namespaces; scan for leaked `Cairn` strings.
- [ ] **Step 2:** In the fork: static checks, `docker compose down -v`, add `QDRANT_API_KEY` to its `.env`, rebuild and start; `/readyz` healthy; a fulltext query returns results.
