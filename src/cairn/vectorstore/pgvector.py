"""pgvector driver.

**One table per namespace**, named ``cairn_vec_{kb_hex}_v{version}``.

The alternative — one shared table filtered by ``(kb_id, index_version)`` —
looks tidier and makes blue/green reindex expensive: dropping a retired version
becomes a bulk DELETE of every row plus the vacuum that follows, on the largest
table in the system, every time anyone changes a chunk size. With a table per
namespace it is ``DROP TABLE``: instant, no bloat, no vacuum.

PostgreSQL is comfortable with thousands of tables (they are catalog rows),
which is precisely the thing Qdrant is *not* comfortable with — hence the two
drivers make opposite layout choices for the same logical model.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from cairn.core.logging import get_logger
from cairn.vectorstore.base import (
    Capabilities,
    HealthStatus,
    Hit,
    Namespace,
    NamespaceSpec,
    Point,
    UpsertResult,
    VectorQuery,
    validate_dimensions,
)
from cairn.vectorstore.errors import NamespaceNotFound, VectorStoreUnavailable
from cairn.vectorstore.filters import (
    OPERATORS,
    And,
    Compare,
    Exists,
    FilterNode,
    Not,
    Or,
)

__all__ = ["PGVECTOR_CAPABILITIES", "PgVectorStore"]

log = get_logger(__name__)

#: Columns promoted out of the JSONB payload because they are filtered on every
#: query and deserve a real index.
_COLUMN_FIELDS = {"document_id": "document_id", "created_at": "created_at"}

_OPS_CLASS = {
    "cosine": "vector_cosine_ops",
    "dot": "vector_ip_ops",
    "l2": "vector_l2_ops",
}

_DISTANCE_OP = {"cosine": "<=>", "dot": "<#>", "l2": "<->"}

PGVECTOR_CAPABILITIES = Capabilities(
    driver="pgvector",
    # No native sparse vector type. Lexical search is PostgreSQL full-text,
    # driven by VectorQuery.text — which covers the same need at this scale but
    # is not the same thing, so the capability reports False.
    sparse_vectors=False,
    quantization=frozenset({"none"}),
    payload_partitioning=False,
    filter_operators=OPERATORS,
    max_top_k=1000,
    fast_namespace_drop=True,
)


class PgVectorStore:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        default_text_search_config: str = "simple",
    ) -> None:
        self._engine = engine
        # 'simple' does no stemming but works without extensions and handles the
        # exact-term matching sparse search mostly exists for. Deployments with
        # English or Chinese corpora should override per namespace: 'english',
        # or 'zhparser'/'jiebacfg' once the extension is installed (FR-F-04).
        self._text_config = default_text_search_config
        self._specs: dict[str, NamespaceSpec] = {}

    def capabilities(self) -> Capabilities:
        return PGVECTOR_CAPABILITIES

    # --- namespace lifecycle ------------------------------------------------

    async def ensure_namespace(self, ns: Namespace, spec: NamespaceSpec) -> None:
        table = ns.key()
        ops = _OPS_CLASS[spec.metric]
        options = dict(spec.driver_options)
        m = int(options.get("hnsw_m", 16))
        ef_construction = int(options.get("hnsw_ef_construction", 128))
        text_config = str(options.get("text_search_config", self._text_config))

        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        chunk_id     UUID PRIMARY KEY,
                        document_id  UUID NOT NULL,
                        embedding    vector({spec.dim}) NOT NULL,
                        content      TEXT NOT NULL DEFAULT '',
                        tsv          tsvector,
                        payload      JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                        created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
            )
            await conn.execute(
                text(f"CREATE INDEX IF NOT EXISTS {table}_doc ON {table} (document_id)")
            )
            await conn.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS {table}_payload "
                    f"ON {table} USING gin (payload jsonb_path_ops)"
                )
            )
            if spec.sparse:
                await conn.execute(
                    text(f"CREATE INDEX IF NOT EXISTS {table}_tsv ON {table} USING gin (tsv)")
                )
            # The HNSW index is created here rather than after bulk load, which
            # is slower to build. Building it afterwards is the right move for a
            # large reindex; that optimisation belongs in the reindex path
            # (T-M03-11), not in the generic ensure.
            await conn.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS {table}_hnsw ON {table} "
                    f"USING hnsw (embedding {ops}) "
                    f"WITH (m = {m}, ef_construction = {ef_construction})"
                )
            )

        self._specs[table] = spec
        log.info(
            "vectorstore.namespace_ready",
            driver="pgvector",
            namespace=table,
            dim=spec.dim,
            metric=spec.metric,
            text_config=text_config,
        )

    async def namespace_exists(self, ns: Namespace) -> bool:
        async with self._engine.connect() as conn:
            found = await conn.scalar(
                text("SELECT to_regclass(:name) IS NOT NULL"), {"name": f"public.{ns.key()}"}
            )
        return bool(found)

    async def list_namespaces(self, kb_id: UUID) -> tuple[Namespace, ...]:
        prefix = f"cairn_vec_{kb_id.hex}_v"
        async with self._engine.connect() as conn:
            names = list(
                (
                    await conn.scalars(
                        text(
                            "SELECT c.relname FROM pg_class c "
                            "JOIN pg_namespace n ON n.oid=c.relnamespace "
                            "WHERE n.nspname=current_schema() AND c.relkind='r' "
                            "AND c.relname LIKE :pattern ORDER BY c.relname"
                        ),
                        {"pattern": f"{prefix}%"},
                    )
                ).all()
            )
        versions: list[int] = []
        for name in names:
            suffix = str(name).removeprefix(prefix)
            if suffix.isdigit() and int(suffix) > 0:
                versions.append(int(suffix))
        return tuple(Namespace(kb_id, version) for version in sorted(versions))

    async def drop_namespace(self, ns: Namespace) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS {ns.key()}"))
        self._specs.pop(ns.key(), None)
        log.info("vectorstore.namespace_dropped", driver="pgvector", namespace=ns.key())

    async def _spec_for(self, ns: Namespace) -> NamespaceSpec:
        table = ns.key()
        cached = self._specs.get(table)
        if cached is not None:
            return cached
        async with self._engine.connect() as conn:
            dim = await conn.scalar(
                text(
                    """
                    SELECT a.atttypmod
                      FROM pg_attribute a
                      JOIN pg_class c ON c.oid = a.attrelid
                     WHERE c.relname = :table AND a.attname = 'embedding'
                    """
                ),
                {"table": table},
            )
        if dim is None:
            raise NamespaceNotFound(f"Vector namespace {table} does not exist.")
        spec = NamespaceSpec(dim=int(dim))
        self._specs[table] = spec
        return spec

    # --- writes -------------------------------------------------------------

    async def upsert(self, ns: Namespace, points: Sequence[Point]) -> UpsertResult:
        if not points:
            return UpsertResult(written=0, namespace=ns.key())

        spec = await self._spec_for(ns)
        validate_dimensions(points, spec.dim, ns.key())

        table = ns.key()
        text_config = str(spec.driver_options.get("text_search_config", self._text_config))

        rows = []
        for point in points:
            payload = dict(point.payload)
            rows.append(
                {
                    "chunk_id": point.id,
                    "document_id": payload.get("document_id"),
                    "embedding": _vector_literal(point.dense),
                    "content": payload.get("content", ""),
                    "payload": json.dumps(payload, default=str),
                }
            )

        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    f"""
                    INSERT INTO {table}
                        (chunk_id, document_id, embedding, content, tsv, payload)
                    VALUES
                        (:chunk_id, CAST(:document_id AS uuid), CAST(:embedding AS vector),
                         :content, to_tsvector('{text_config}', :content),
                         CAST(:payload AS jsonb))
                    ON CONFLICT (chunk_id) DO UPDATE SET
                        document_id = EXCLUDED.document_id,
                        embedding   = EXCLUDED.embedding,
                        content     = EXCLUDED.content,
                        tsv         = EXCLUDED.tsv,
                        payload     = EXCLUDED.payload
                    """
                ),
                rows,
            )
        return UpsertResult(written=len(rows), namespace=table)

    async def delete(
        self,
        ns: Namespace,
        *,
        ids: Sequence[UUID] | None = None,
        filter: FilterNode | None = None,
    ) -> int:
        if ids is None and filter is None:
            raise ValueError("delete requires either ids or a filter")

        table = ns.key()
        clauses: list[str] = []
        params: dict[str, Any] = {}

        if ids is not None:
            if not ids:
                return 0
            clauses.append("chunk_id = ANY(:ids)")
            params["ids"] = list(ids)
        if filter is not None:
            where, filter_params = translate(filter)
            clauses.append(where)
            params.update(filter_params)

        async with self._engine.begin() as conn:
            result = await conn.execute(
                text(f"DELETE FROM {table} WHERE {' AND '.join(clauses)}"), params
            )
        return int(getattr(result, "rowcount", 0) or 0)

    # --- reads --------------------------------------------------------------

    async def search(self, ns: Namespace, query: VectorQuery) -> list[Hit]:
        if query.dense is None and not query.text:
            raise ValueError(
                "pgvector search requires a dense vector or query text; it has no "
                "native sparse vector type (see Capabilities.sparse_vectors)"
            )

        spec = await self._spec_for(ns)
        table = ns.key()
        params: dict[str, Any] = {"limit": min(query.top_k, PGVECTOR_CAPABILITIES.max_top_k)}

        where = "TRUE"
        if query.filter is not None:
            where, filter_params = translate(query.filter)
            params.update(filter_params)

        if query.dense is not None:
            operator = _DISTANCE_OP[spec.metric]
            # Normalise every metric to "higher is better" so callers and the
            # fusion stage never have to know which backend produced a score.
            score = {
                "cosine": f"1 - (embedding {operator} CAST(:q AS vector))",
                "dot": f"-(embedding {operator} CAST(:q AS vector))",
                "l2": f"-(embedding {operator} CAST(:q AS vector))",
            }[spec.metric]
            params["q"] = _vector_literal(query.dense)
            order = f"embedding {operator} CAST(:q AS vector) ASC"
        else:
            text_config = str(spec.driver_options.get("text_search_config", self._text_config))
            params["tsq"] = query.text or ""
            score = f"ts_rank_cd(tsv, plainto_tsquery('{text_config}', :tsq))"
            where = f"({where}) AND tsv @@ plainto_tsquery('{text_config}', :tsq)"
            order = f"{score} DESC"

        sql = (
            f"SELECT chunk_id, payload, {score} AS score FROM {table} "
            f"WHERE {where} ORDER BY {order} LIMIT :limit"
        )

        async with self._engine.connect() as conn:
            if query.ef_search is not None:
                await conn.execute(text(f"SET LOCAL hnsw.ef_search = {int(query.ef_search)}"))
            result = await conn.execute(text(sql), params)
            rows = result.mappings().all()

        hits = [
            Hit(
                id=row["chunk_id"],
                score=float(row["score"]),
                payload=row["payload"] if query.with_payload else {},
            )
            for row in rows
        ]
        if query.score_threshold is not None:
            hits = [hit for hit in hits if hit.score >= query.score_threshold]
        return hits

    async def fetch(self, ns: Namespace, ids: Sequence[UUID]) -> list[Hit]:
        if not ids:
            return []
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text(f"SELECT chunk_id, payload FROM {ns.key()} WHERE chunk_id = ANY(:ids)"),
                {"ids": list(ids)},
            )
            rows = result.mappings().all()
        return [Hit(id=row["chunk_id"], score=0.0, payload=row["payload"]) for row in rows]

    async def count(self, ns: Namespace, filter: FilterNode | None = None) -> int:
        where, params = ("TRUE", {}) if filter is None else translate(filter)
        async with self._engine.connect() as conn:
            total = await conn.scalar(
                text(f"SELECT count(*) FROM {ns.key()} WHERE {where}"), params
            )
        return int(total or 0)

    async def health(self) -> HealthStatus:
        started = time.perf_counter()
        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
                installed = await conn.scalar(
                    text("SELECT count(*) FROM pg_extension WHERE extname = 'vector'")
                )
            if not installed:
                return HealthStatus(healthy=False, detail="the pgvector extension is not installed")
            return HealthStatus(healthy=True, latency_ms=(time.perf_counter() - started) * 1000)
        except Exception as exc:
            return HealthStatus(healthy=False, detail=type(exc).__name__)

    async def close(self) -> None:
        # The engine is owned by the registry, which shares it with the rest of
        # the application; disposing it here would close connections out from
        # under unrelated callers.
        return None


# --- filter translation ------------------------------------------------------


def translate(node: FilterNode, prefix: str = "f") -> tuple[str, dict[str, Any]]:
    """Compile a filter AST to SQL matching the normative semantics.

    Every divergence from ``vectorstore.filters.matches`` is a correctness bug,
    which is why the conformance suite runs the same corpus through both.
    """
    params: dict[str, Any] = {}
    counter = _Counter()
    sql = _translate(node, params, counter, prefix)
    return sql, params


class _Counter:
    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value = 0

    def next(self) -> int:
        self.value += 1
        return self.value


def _path_expr(field: str) -> tuple[str, bool]:
    """Return the SQL expression for a field, and whether it is a real column."""
    column = _COLUMN_FIELDS.get(field)
    if column is not None:
        return column, True
    segments = field.split(".")
    literal = "{" + ",".join(segments) + "}"
    return f"payload #> '{literal}'", False


def _translate(node: FilterNode, params: dict[str, Any], counter: _Counter, prefix: str) -> str:
    if isinstance(node, And):
        if not node.clauses:
            return "TRUE"
        return (
            "(" + " AND ".join(_translate(c, params, counter, prefix) for c in node.clauses) + ")"
        )
    if isinstance(node, Or):
        if not node.clauses:
            return "FALSE"
        return "(" + " OR ".join(_translate(c, params, counter, prefix) for c in node.clauses) + ")"
    if isinstance(node, Not):
        return f"(NOT ({_translate(node.clause, params, counter, prefix)}))"

    expr, is_column = _path_expr(node.field)

    if isinstance(node, Exists):
        # `#>` yields SQL NULL for an absent path and 'null'::jsonb for a stored
        # null, so IS NOT NULL is exactly the absent/null distinction we want.
        return f"({expr} IS NOT NULL)" if node.present else f"({expr} IS NULL)"

    name = f"{prefix}{counter.next()}"

    if is_column:
        return _translate_column(node, expr, name, params)
    return _translate_jsonb(node, expr, name, params)


def _translate_column(node: Compare, expr: str, name: str, params: dict[str, Any]) -> str:
    value = node.value
    if node.op == "$eq":
        params[name] = value
        return f"({expr} = :{name})"
    if node.op == "$ne":
        params[name] = value
        return f"({expr} IS NULL OR {expr} <> :{name})"
    if node.op in ("$in", "$nin"):
        params[name] = list(value)
        inner = f"{expr} = ANY(:{name})"
        return f"({inner})" if node.op == "$in" else f"({expr} IS NULL OR NOT ({inner}))"
    params[name] = value
    return f"({expr} {_SQL_OP[node.op]} :{name})"


_SQL_OP = {"$gt": ">", "$gte": ">=", "$lt": "<", "$lte": "<="}
_JSON_TYPE = {int: "number", float: "number", str: "string", bool: "boolean"}


def _translate_jsonb(node: Compare, expr: str, name: str, params: dict[str, Any]) -> str:
    value = node.value

    if node.op in ("$eq", "$ne"):
        params[name] = json.dumps(value, default=str)
        # `@>` gives containment for arrays and equality for scalars in one
        # operator — exactly the array semantics declared in filters.py.
        contains = f"{expr} @> CAST(:{name} AS jsonb)"
        return f"({contains})" if node.op == "$eq" else f"({expr} IS NULL OR NOT ({contains}))"

    if node.op in ("$in", "$nin"):
        parts = []
        for index, item in enumerate(value):
            key = f"{name}_{index}"
            params[key] = json.dumps(item, default=str)
            parts.append(f"{expr} @> CAST(:{key} AS jsonb)")
        if not parts:
            return "FALSE" if node.op == "$in" else "TRUE"
        any_of = "(" + " OR ".join(parts) + ")"
        return any_of if node.op == "$in" else f"({expr} IS NULL OR NOT {any_of})"

    # Ordered comparison. The jsonb_typeof guard is what keeps a badly-typed
    # document from erroring the whole query — semantics say "no match".
    json_type = _JSON_TYPE.get(type(value), "string")
    params[name] = value
    if json_type == "number":
        return (
            f"(jsonb_typeof({expr}) = 'number' "
            f"AND ({expr} #>> '{{}}')::numeric {_SQL_OP[node.op]} :{name})"
        )
    if isinstance(value, datetime):
        params[name] = value.isoformat()
    return f"(jsonb_typeof({expr}) = 'string' AND ({expr} #>> '{{}}') {_SQL_OP[node.op]} :{name})"


# --- helpers -----------------------------------------------------------------


def _vector_literal(values: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


async def ensure_extension(engine: AsyncEngine) -> None:
    """Install pgvector. Idempotent; requires superuser on first run."""
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    except Exception as exc:
        raise VectorStoreUnavailable(
            "The pgvector extension could not be installed. Install it manually "
            "with `CREATE EXTENSION vector;` as a superuser."
        ) from exc
