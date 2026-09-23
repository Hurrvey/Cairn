"""Verify the browser-created disposable KB through the real ingress and workers."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx


def checked(response: httpx.Response) -> Any:
    if response.is_error:
        raise AssertionError(
            f"{response.request.method} {response.request.url.path}: {response.text}"
        )
    return response.json() if response.content else None


def await_value(read: Any, predicate: Any, label: str, timeout: int = 120) -> Any:
    deadline = time.monotonic() + timeout
    value: Any = None
    while time.monotonic() < deadline:
        value = read()
        if predicate(value):
            print(f"PASS {label}", flush=True)
            return value
        time.sleep(2)
    raise AssertionError(f"Timed out: {label}; last state: {value}")


def run(base_url: str, output: Path, phase: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30) as client:
        login = checked(
            client.post(
                "/v1/auth/login",
                json={
                    "username": "admin",
                    "password": os.environ["CAIRN_SMOKE_PASSWORD"],
                },
            )
        )
        assert login["status"] == "ok", login["status"]
        client.headers["X-CSRF-Token"] = client.cookies["cairn_csrf"]
        bases = checked(client.get("/v1/knowledge-bases"))["items"]
        matches = [item for item in bases if item["name"] == "Product acceptance"]
        assert len(matches) == 1, (
            "Run the browser setup smoke once against a disposable deployment."
        )
        kb = matches[0]
        path = f"/v1/knowledge-bases/{kb['id']}"

        def docs() -> list[dict[str, Any]]:
            return checked(client.get(f"{path}/documents"))["items"]

        query = {
            "targets": [{"knowledge_base_id": kb["id"]}],
            "query": "Saturn rings",
            "search_mode": "hybrid",
            "rerank": {"enabled": False},
            "options": {"expand_parent": False},
        }
        if phase == "prepare":
            original = next(item for item in docs() if item["title"] == "astronomy.md")
            expected = b"# Astronomy\n\nSaturn has prominent rings of ice and rock."
            download = client.get(f"{path}/documents/{original['id']}/download")
            assert download.status_code == 200 and download.content == expected
            duplicate = checked(
                client.post(
                    f"{path}/documents/upload",
                    files={
                        "file": ("astronomy.md", expected, "text/markdown"),
                    },
                )
            )
            assert duplicate["status"] == "skipped"
            assert duplicate["document"]["id"] == original["id"]
            registered = checked(
                client.post(
                    f"{path}/documents/upload",
                    files={
                        "file": (
                            "ocean.txt",
                            b"The Pacific Ocean is the largest ocean on Earth.",
                            "text/plain",
                        ),
                    },
                )
            )
            extra_id = registered["document"]["id"]
            await_value(
                docs,
                lambda items: any(
                    item["id"] == extra_id and item["state"] == "indexed" for item in items
                ),
                "incremental upload indexed",
            )
            estimate = checked(client.post(f"{path}/reindex", json={"confirm": False}))
            assert estimate["confirmation_required"] and estimate["chunks"] > 0
            version = checked(client.get(path))["active_index_version"]
            checked(client.post(f"{path}/reindex", json={"confirm": True}))
            rebuilt = await_value(
                lambda: checked(client.get(path)),
                lambda value: (
                    value["active_index_version"] > version
                    and value["building_index_version"] is None
                ),
                "versioned rebuild",
            )
            key = checked(
                client.post(
                    "/v1/api-keys",
                    json={
                        "name": "Disposable product acceptance",
                        "scopes": ["kb:query"],
                        "knowledge_base_ids": [kb["id"]],
                    },
                )
            )
            with httpx.Client(
                base_url=base_url, headers={"Authorization": f"Bearer {key['key']}"}
            ) as machine:
                response = checked(machine.post("/v1/retrieval/query", json=query))
                assert any("Saturn" in item["content"] for item in response["results"])
                assert machine.get(f"{path}/documents/{original['id']}/download").status_code == 403
            checked(client.delete(f"/v1/api-keys/{key['api_key']['id']}"))
            checked(client.delete(f"{path}/documents/{extra_id}"))
            await_value(
                docs,
                lambda items: all(item["id"] != extra_id for item in items),
                "deleted document hidden",
            )
            assert client.get(f"{path}/documents/{extra_id}/download").status_code in {403, 404}
            result = checked(client.post("/v1/retrieval/query", json=query))
            assert any("Saturn" in item["content"] for item in result["results"])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(
                    {
                        "kb_id": kb["id"],
                        "document_id": original["id"],
                        "active_index_version": rebuilt["active_index_version"],
                    },
                    indent=2,
                )
            )
            print("PASS download, deduplication, bearer data plane, scoped denial and deletion")
        else:
            before = json.loads(output.read_text())
            assert kb["id"] == before["kb_id"]
            assert kb["active_index_version"] == before["active_index_version"]
            await_value(
                lambda: client.post("/v1/retrieval/query", json=query),
                lambda response: (
                    response.status_code == 200
                    and any("Saturn" in item["content"] for item in response.json()["results"])
                ),
                "persistent index and automatic runtime repair",
            )
            source = client.get(f"{path}/documents/{before['document_id']}/download")
            assert source.status_code == 200 and b"Saturn" in source.content
            print("PASS persistent document source")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:38880")
    parser.add_argument("--output", type=Path, default=Path("data/product-lifecycle.json"))
    parser.add_argument("--phase", choices=["prepare", "verify"], default="prepare")
    arguments = parser.parse_args()
    run(arguments.base_url, arguments.output, arguments.phase)
