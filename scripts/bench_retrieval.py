"""Retrieval latency at scale against a live Compose stack (NFR-P-01, NFR-P-03).

    CAIRN_ACCEPTANCE_PASSWORD=... uv run python scripts/bench_retrieval.py --points 1000000

What it does, on a DISPOSABLE stack only (it creates a knowledge base, an API key
and a million points):

1. creates a knowledge base on the existing MiniLM model and Qdrant binding
   (hybrid, RRF, no rerank), uploads one real document and waits until the
   index version is ACTIVE, so the runtime is published exactly as in
   production;
2. loads synthetic chunks straight into that version's Qdrant collection from
   inside the ``api-data`` container (the collection is not published to the
   host): random unit dense vectors of the model's width, real BM25 sparse
   vectors of generated text, and read-model v2 payloads — the ingestion
   pipeline would need hours of CPU embedding for the same volume and is not
   what is being measured;
3. waits for Qdrant to finish indexing, then runs hybrid queries through the
   public ``/v1/retrieval/query`` with a ``kb:query`` key at several
   concurrency levels and reports server-side (``usage.latency_ms.total``) and
   client-side p50/p95/p99, plus any ``degraded`` notices (load shedding).

The objective (``docs/03-api/02-data-plane-api.md`` §7): p95 < 150 ms hybrid,
no rerank, top_k ≤ 10, ≤ 1M vectors, measured server-side.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

LOADER = r"""
import asyncio, json, os, random, sys, time, uuid
from qdrant_client import AsyncQdrantClient, models
from cairn.core.config import get_settings
from cairn.embedding.sparse import encode_document

args = json.loads(sys.argv[1])
random.seed(args["seed"])
WORDS = args["words"]

def text(n):
    return " ".join(random.choice(WORDS) for _ in range(n))

def unit(dim):
    values = [random.gauss(0.0, 1.0) for _ in range(dim)]
    norm = sum(v * v for v in values) ** 0.5
    return [v / norm for v in values]

async def main():
    settings = get_settings().qdrant
    client = AsyncQdrantClient(url=settings.url, api_key=settings.api_key.get_secret_value()
                               if settings.api_key else None, timeout=120)
    name, dim, total = args["collection"], args["dim"], args["points"]
    started, sent, batch = time.monotonic(), 0, 500
    now = int(time.time())
    while sent < total:
        points = []
        for _ in range(min(batch, total - sent)):
            chunk_id = str(uuid.uuid4())
            content = text(80)
            sparse = encode_document(content)
            payload = {
                "chunk_id": chunk_id, "document_id": str(uuid.uuid4()),
                "kb_id": args["kb_uuid"], "index_version": args["version"], "revision": 1,
                "content": content, "content_hash": chunk_id.replace("-", "") * 2,
                "token_count": 110, "parent_id": None, "ordinal": 0,
                "meta": {"lang": "en", "tier": random.randint(1, 5)},
                "created_at": now - random.randint(0, 86400 * 365),
                "doc_title": "synthetic.md", "doc_type": "md", "doc_updated_at": now,
                "_paths": ["chunk_id", "content", "created_at", "doc_title", "doc_type",
                           "doc_updated_at", "document_id", "index_version", "kb_id", "meta",
                           "meta.lang", "meta.tier", "ordinal", "parent_id", "revision",
                           "token_count", "content_hash"],
            }
            vector = {"dense": unit(dim)}
            if sparse is not None:
                vector["sparse"] = models.SparseVector(indices=list(sparse.indices),
                                                       values=list(sparse.values))
            points.append(models.PointStruct(id=chunk_id, vector=vector, payload=payload))
        await client.upsert(name, points=points, wait=False)
        sent += len(points)
        if sent % 50000 == 0 or sent == total:
            print(json.dumps({"loaded": sent, "seconds": round(time.monotonic() - started)}),
                  flush=True)
    while True:
        info = await client.get_collection(name)
        if info.status == models.CollectionStatus.GREEN:
            break
        await asyncio.sleep(5)
    print(json.dumps({"indexed": True, "points": info.points_count,
                      "seconds": round(time.monotonic() - started)}), flush=True)
    await client.close()

asyncio.run(main())
"""

VOCABULARY = [
    "saturn",
    "rings",
    "ice",
    "rock",
    "probe",
    "orbit",
    "planet",
    "moon",
    "signing",
    "key",
    "rotate",
    "quarterly",
    "firmware",
    "controller",
    "maintenance",
    "cable",
    "network",
    "latency",
    "index",
    "vector",
    "sparse",
    "hybrid",
    "retrieval",
    "document",
    "policy",
    "security",
    "access",
    "grant",
    "audit",
    "backup",
    "restore",
    "cluster",
    "replica",
    "storage",
    "bucket",
    "upload",
    "parser",
    "chunk",
    "embedding",
    "model",
    "token",
    "budget",
    "rerank",
    "fusion",
    "query",
    "filter",
    "metadata",
    "tenant",
    "workspace",
    "deploy",
    "release",
    "incident",
    "runbook",
    "escalation",
    "on-call",
    "alert",
    "metric",
    "dashboard",
    "trace",
    "log",
    "error",
    "timeout",
    "retry",
    "queue",
    "worker",
    "lease",
]


def checked(response: httpx.Response) -> dict:  # type: ignore[type-arg]
    if response.status_code >= 400:
        raise SystemExit(
            f"{response.request.method} {response.request.url.path} -> "
            f"{response.status_code}: {response.text[:400]}"
        )
    return response.json() if response.content else {}


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(fraction * len(ordered)) - 1))]


def setup(api: httpx.Client, run_id: str) -> tuple[dict, str]:  # type: ignore[type-arg]
    bindings = checked(api.get("/v1/storage-bindings"))
    objects = next(item for item in bindings if item["kind"] == "object")
    vectors = next(item for item in bindings if item["kind"] == "vector")
    model = next(
        item for item in checked(api.get("/v1/models")) if item["provider_family"] == "tei"
    )
    kb = checked(
        api.post(
            "/v1/knowledge-bases",
            json={
                "name": f"Latency benchmark {run_id}",
                "embedding_model_id": model["id"],
                "object_binding_id": objects["id"],
                "vector_binding_id": vectors["id"],
                "sparse": {"kind": "bm25"},
                "retrieval_config": {
                    "search_mode": "hybrid",
                    "rerank": {"enabled": False},
                    "expand_parent": False,
                    "top_k": 10,
                    "candidate_k": 100,
                },
            },
        )
    )
    checked(
        api.post(
            f"/v1/knowledge-bases/{kb['id']}/documents/upload",
            files={
                "file": ("seed.md", b"# Seed\n\nSaturn has rings of ice and rock.", "text/markdown")
            },
        )
    )
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        current = checked(api.get(f"/v1/knowledge-bases/{kb['id']}"))
        if current["active_index_version"]:
            break
        time.sleep(2)
    else:
        raise SystemExit("the seed document was not indexed within 10 minutes")
    key = checked(
        api.post(
            "/v1/api-keys",
            json={
                "name": f"benchmark-{run_id}",
                "scopes": ["kb:query"],
                "knowledge_base_ids": [kb["id"]],
                "rate_limit_rpm": 100_000,
            },
        )
    )["key"]
    return current, key


def run_queries(base: str, key: str, kb_id: str, concurrency: int, total: int, seed: int) -> dict:  # type: ignore[type-arg]
    import random

    rng = random.Random(seed)  # noqa: S311 — reproducible synthetic queries, not secrets
    queries = [" ".join(rng.sample(VOCABULARY, 3)) for _ in range(total)]
    server: list[float] = []
    client: list[float] = []
    degraded: dict[str, int] = {}
    failures = 0
    lock = threading.Lock()
    local = threading.local()

    def one(text: str) -> None:
        nonlocal failures
        http = getattr(local, "http", None)
        if http is None:
            http = local.http = httpx.Client(
                base_url=base, timeout=30, headers={"Authorization": f"Bearer {key}"}
            )
        started = time.perf_counter()
        response = http.post(
            "/v1/retrieval/query",
            json={
                "targets": [{"knowledge_base_id": kb_id}],
                "query": text,
                "search_mode": "hybrid",
                "top_k": 10,
                "rerank": {"enabled": False},
            },
        )
        elapsed = (time.perf_counter() - started) * 1000
        with lock:
            if response.status_code != 200:
                failures += 1
                return
            body = response.json()
            server.append(body["usage"]["latency_ms"]["total"])
            client.append(elapsed)
            for notice in body["degraded"]:
                label = f"{notice['stage']}:{notice['reason']}"
                degraded[label] = degraded.get(label, 0) + 1

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(one, queries))
    wall = time.perf_counter() - started
    report = {
        "concurrency": concurrency,
        "queries": total,
        "failures": failures,
        "rps": round(len(server) / wall, 1),
        "degraded": degraded,
    }
    for label, values in (("server_ms", server), ("client_ms", client)):
        if values:
            report[label] = {
                "p50": round(statistics.median(values), 1),
                "p95": round(percentile(values, 0.95), 1),
                "p99": round(percentile(values, 0.99), 1),
            }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--points", type=int, default=1_000_000)
    parser.add_argument("--queries", type=int, default=2000)
    parser.add_argument("--concurrency", default="1,8,32")
    parser.add_argument("--compose-dir", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--report", default="data/bench-retrieval.json")
    parser.add_argument("--kb", help="reuse a knowledge base loaded by an earlier run")
    parser.add_argument("--key", help="API key for --kb")
    args = parser.parse_args()
    password = os.environ.get("CAIRN_ACCEPTANCE_PASSWORD")
    if not password:
        raise SystemExit("set CAIRN_ACCEPTANCE_PASSWORD to the admin password")

    run_id = uuid.uuid4().hex[:6]
    with httpx.Client(base_url=args.base_url, timeout=120) as api:
        checked(api.post("/v1/auth/login", json={"username": "admin", "password": password}))
        api.headers["X-CSRF-Token"] = api.cookies["cairn_csrf"]
        if args.kb and args.key:
            kb, key = checked(api.get(f"/v1/knowledge-bases/{args.kb}")), args.key
        else:
            kb, key = setup(api, run_id)
            collection = f"cairn_vec_{kb_uuid_hex(kb)}_v{kb['active_index_version']}"
            payload = json.dumps(
                {
                    "collection": collection,
                    "points": args.points,
                    "dim": kb["embedding_dim"],
                    "kb_uuid": str(uuid.UUID(hex=kb_uuid_hex(kb))),
                    "version": kb["active_index_version"],
                    "seed": 7,
                    "words": VOCABULARY,
                }
            )
            print(f"loading {args.points} points into {collection} …", flush=True)
            subprocess.run(  # noqa: S603 — fixed argv; the payload is our own JSON
                ["docker", "compose", "exec", "-T", "api-data", "python", "-", payload],  # noqa: S607
                input=LOADER.encode(),
                cwd=args.compose_dir,
                check=True,
            )
        print(json.dumps({"kb": kb["id"], "key_prefix": key[:12]}), flush=True)

    results = []
    for level in [int(value) for value in args.concurrency.split(",")]:
        run_queries(args.base_url, key, kb["id"], level, min(100, args.queries), seed=1)  # warm
        report = run_queries(args.base_url, key, kb["id"], level, args.queries, seed=level)
        print(json.dumps(report), flush=True)
        results.append(report)
    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"kb": kb["id"], "points": args.points, "results": results}, indent=2),
        encoding="utf-8",
    )
    print(f"report written to {out}")
    if any(item.get("server_ms", {}).get("p95", 1e9) >= 150 for item in results[:1]):
        sys.exit(2)


def kb_uuid_hex(kb: dict) -> str:  # type: ignore[type-arg]
    """The UUID behind a public ``kb_…`` ID, as the collection name spells it."""
    from cairn.core.ids import decode_id

    return decode_id("kb", kb["id"]).hex


if __name__ == "__main__":
    main()
