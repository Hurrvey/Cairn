"""Learned sparse retrieval on a live Compose stack with the bge-m3 sidecar.

    CAIRN_ACCEPTANCE_PASSWORD=... uv run python scripts/sparse_acceptance.py

Drives the public API the way an administrator and an agent would:

1. registers the bge-m3 sidecar as a provider and the model WITHOUT a
   dimension (detected from the live service), then tests it (dense + sparse);
2. creates two knowledge bases with the automatic keyword-search choice —
   one embedding with bge-m3 itself (dense and sparse in one pass), one
   embedding with the existing MiniLM model and taking its keyword weights from
   bge-m3 (a separate sparse model);
3. uploads the same mixed Chinese/English documents to both and waits for
   indexing through the real workers;
4. queries both through a bearer API key: a Chinese keyword, a product code,
   and a Chinese hybrid question.

Run against a disposable stack only: it creates providers, models, knowledge
bases and keys.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import httpx

DOCUMENTS = {
    "saturn.md": (
        "# 土星\n\n土星环主要由冰和岩石组成, 从地球上用小型望远镜即可看到。\n\n"
        "## Probe\n\nThe XR-2200 probe mapped Saturn's rings in 2031.\n"
    ),
    "mars.md": "# Mars\n\nMars is called the red planet because of iron oxide on its surface.\n",
    "manual.md": "# Hardware\n\nGeneral maintenance notes for lab equipment and cabling.\n",
}
QUERIES = [
    ("fulltext", "土星环", "土星环"),
    ("fulltext", "XR-2200", "XR-2200"),
    ("hybrid", "哪颗行星有光环?", "土星环"),
    ("vector", "Which planet is red?", "red planet"),
]


def checked(response: httpx.Response) -> dict:  # type: ignore[type-arg]
    if response.status_code >= 400:
        request = response.request
        raise SystemExit(
            f"{request.method} {request.url.path} -> {response.status_code}: {response.text[:400]}"
        )
    return response.json() if response.content else {}


def step(label: str) -> None:
    print(f"PASS {label}", flush=True)


def wait_indexed(api: httpx.Client, kb_id: str, count: int, timeout_s: float = 600) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        page = checked(api.get(f"/v1/knowledge-bases/{kb_id}/documents?limit=50"))
        states = [item["state"] for item in page["items"]]
        if "failed" in states:
            raise SystemExit(f"a document failed in {kb_id}: {page['items']}")
        if len(states) == count and all(state == "indexed" for state in states):
            return
        time.sleep(2)
    raise SystemExit(f"documents in {kb_id} were not indexed within {timeout_s:.0f}s")


def run(base: str, password: str, sidecar_url: str) -> None:
    with httpx.Client(base_url=base, timeout=120) as api:
        login = checked(
            api.post("/v1/auth/login", json={"username": "admin", "password": password})
        )
        if login.get("status") == "password_change_required":
            raise SystemExit(
                "complete the initial password change first (run the browser acceptance)"
            )
        api.headers["X-CSRF-Token"] = api.cookies["cairn_csrf"]

        provider = checked(
            api.post(
                "/v1/model-providers",
                json={
                    "name": "bge-m3 sidecar",
                    "family": "bge_m3",
                    "base_url": sidecar_url,
                    "config": {
                        "binding_revision": "bge-m3-5617a9f",
                        "allow_private": True,
                        "max_batch_size": 16,
                    },
                },
            )
        )
        model = checked(
            api.post(
                "/v1/models",
                json={
                    "provider_id": provider["id"],
                    "model_key": "BAAI/bge-m3",
                    "display_name": "bge-m3",
                    "max_input_tokens": 8192,
                    "optimal_batch_size": 16,
                    "tokenizer_id": "bge-m3",
                    "sparse": True,
                },
            )
        )
        assert model["dimension"] == 1024 and model["sparse"], model
        step("bge-m3 registered; dimension 1024 detected from the live sidecar")
        tested = checked(api.post(f"/v1/models/{model['id']}/test"))
        assert (
            tested["healthy"] and tested["dimensions"] == 1024 and (tested["sparse_terms"] or 0) > 0
        ), tested
        step(f"model test healthy with {tested['sparse_terms']} keyword terms")

        bindings = checked(api.get("/v1/storage-bindings"))
        objects = next(item for item in bindings if item["kind"] == "object")
        vectors = next(item for item in bindings if item["kind"] == "vector")
        minilm = next(item for item in checked(api.get("/v1/models")) if item["id"] != model["id"])

        created = {}
        for label, embedding_id in (
            ("bge-m3 dense+sparse", model["id"]),
            ("MiniLM + bge-m3 keywords", minilm["id"]),
        ):
            kb = checked(
                api.post(
                    "/v1/knowledge-bases",
                    json={
                        "name": f"Sparse acceptance · {label}",
                        "embedding_model_id": embedding_id,
                        "object_binding_id": objects["id"],
                        "vector_binding_id": vectors["id"],
                        "retrieval_config": {
                            "search_mode": "hybrid",
                            "rerank": {"enabled": False},
                            "expand_parent": False,
                        },
                    },
                )
            )
            assert kb["sparse_kind"] == "model" and kb["sparse_model_id"] == model["id"], kb
            created[label] = kb
        step("automatic keyword search picked bge-m3 for both knowledge bases")

        for kb in created.values():
            for name, text in DOCUMENTS.items():
                checked(
                    api.post(
                        f"/v1/knowledge-bases/{kb['id']}/documents/upload",
                        files={"file": (name, text.encode(), "text/markdown")},
                    )
                )
        for kb in created.values():
            wait_indexed(api, kb["id"], len(DOCUMENTS))
        step("six documents indexed through the real workers")

        key = checked(
            api.post(
                "/v1/api-keys",
                json={
                    "name": "sparse-acceptance",
                    "scopes": ["kb:query"],
                    "knowledge_base_ids": [kb["id"] for kb in created.values()],
                },
            )
        )["key"]
        agent = {"Authorization": f"Bearer {key}"}
        for label, kb in created.items():
            for mode, text, expected in QUERIES:
                response = checked(
                    api.post(
                        "/v1/retrieval/query",
                        headers=agent,
                        json={
                            "targets": [{"knowledge_base_id": kb["id"]}],
                            "query": text,
                            "search_mode": mode,
                            "top_k": 3,
                        },
                    )
                )
                hits = response["results"]
                assert hits and expected in hits[0]["content"], (label, mode, text, response)
                assert not response.get("degraded"), (label, mode, response["degraded"])
            step(
                f"{label}: Chinese keyword, product code, hybrid and vector queries rank correctly"
            )
    print("ALL SPARSE CHECKS PASSED", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--sidecar-url", default="http://bge-m3:8000")
    arguments = parser.parse_args()
    secret = os.environ.get("CAIRN_ACCEPTANCE_PASSWORD")
    if not secret:
        sys.exit("set CAIRN_ACCEPTANCE_PASSWORD to the admin password")
    run(arguments.base_url, secret, arguments.sidecar_url)
