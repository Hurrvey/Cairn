"""Disposable real-Cairn acceptance for the adjacent Agent MCP integration."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import httpx

AGENT = Path("D:/code/ai-smart-screen-digital-human")
BASE = "http://127.0.0.1:38883"
OUTPUT = Path("data/acceptance-agent-20260918")


def checked(response: httpx.Response):
    response.raise_for_status()
    return response.json() if response.content else None


def run() -> None:
    marker = "CEDAR-472-" + uuid4().hex[:8]
    question = "What is the test store code?"
    observed: list[bool] = []
    control_key = uuid4().hex
    action_callback = None

    class ModelHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            if self.path in {"/stop", "/start"}:
                if (
                    self.headers.get("Authorization") != "Bearer " + control_key
                    or action_callback is None
                ):
                    self.send_error(403)
                    return
                action_callback(self.path[1:])
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            found = any(
                marker in message.get("content", "")
                for message in payload["messages"]
                if message["role"] == "system"
            )
            observed.append(found)
            if not found:
                self.send_error(400, "retrieved marker missing")
                return
            result = json.dumps(
                {"choices": [{"delta": {"content": f"The test store code is {marker}."}}]}
            )
            body = f"data: {result}\n\ndata: [DONE]\n\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    model = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    thread = threading.Thread(target=model.serve_forever, daemon=True)
    thread.start()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with ExitStack() as cleanup, httpx.Client(base_url=BASE, timeout=30) as api:
        cleanup.callback(thread.join, timeout=5)
        cleanup.callback(model.server_close)
        cleanup.callback(model.shutdown)
        password = os.environ.get("CAIRN_SMOKE_PASSWORD", "Agent-Acceptance-Initial-9")
        login = checked(
            api.post("/v1/auth/login", json={"username": "admin", "password": password})
        )
        if login["status"] == "password_change_required":
            checked(
                api.post(
                    "/v1/auth/complete-initial-setup",
                    headers={"Authorization": "Bearer " + login["change_token"]},
                    json={
                        "current_password": password,
                        "new_password": "Agent-Acceptance-Verified-9",
                        "confirm_password": "Agent-Acceptance-Verified-9",
                    },
                )
            )
        api.headers["X-CSRF-Token"] = api.cookies["cairn_csrf"]
        providers = checked(api.get("/v1/model-providers"))
        provider = (
            providers[0]
            if providers
            else checked(
                api.post(
                    "/v1/model-providers",
                    json={
                        "name": "Agent test TEI",
                        "family": "tei",
                        "base_url": "http://embedding:80",
                        "config": {
                            "binding_revision": "minilm-1110a243",
                            "allow_private": True,
                            "max_batch_size": 16,
                        },
                    },
                )
            )
        )
        models = checked(api.get("/v1/models"))
        embedding = (
            models[0]
            if models
            else checked(
                api.post(
                    "/v1/models",
                    json={
                        "provider_id": provider["id"],
                        "model_key": "sentence-transformers/all-MiniLM-L6-v2",
                        "display_name": "MiniLM",
                        "dimension": 384,
                        "max_input_tokens": 256,
                        "tokenizer_id": "minilm",
                    },
                )
            )
        )
        bindings = checked(api.get("/v1/storage-bindings"))
        objects = next((item for item in bindings if item["kind"] == "object"), None)
        vectors = next((item for item in bindings if item["kind"] == "vector"), None)
        if objects is None:
            objects = checked(
                api.post(
                    "/v1/storage-bindings",
                    json={
                        "name": "Agent objects",
                        "kind": "object",
                        "driver": "local",
                        "config": {},
                    },
                )
            )
        if vectors is None:
            vectors = checked(
                api.post(
                    "/v1/storage-bindings",
                    json={
                        "name": "Agent vectors",
                        "kind": "vector",
                        "driver": "pgvector",
                        "config": {},
                    },
                )
            )
        kb = checked(
            api.post(
                "/v1/knowledge-bases",
                json={
                    "name": "Agent " + marker,
                    "embedding_model_id": embedding["id"],
                    "object_binding_id": objects["id"],
                    "vector_binding_id": vectors["id"],
                    "retrieval_config": {"rerank": {"enabled": False}, "expand_parent": False},
                },
            )
        )
        doc = checked(
            api.post(
                f"/v1/knowledge-bases/{kb['id']}/documents/upload",
                files={
                    "file": (
                        "test-store.md",
                        f"# Test store\n\nThe test store code is {marker}. "
                        "This code is valid only for the isolated integration test.",
                        "text/markdown",
                    ),
                },
            )
        )["document"]
        for _attempt in range(90):
            doc = checked(api.get(f"/v1/knowledge-bases/{kb['id']}/documents/{doc['id']}"))
            if doc["state"] == "indexed":
                break
            if doc["state"] == "failed":
                raise RuntimeError(doc["error_code"])
            time.sleep(2)
        assert doc["state"] == "indexed"
        key = checked(
            api.post(
                "/v1/api-keys",
                json={
                    "name": "Temporary Agent acceptance",
                    "scopes": ["kb:query"],
                    "knowledge_base_ids": [kb["id"]],
                },
            )
        )
        environment = {
            **os.environ,
            "RAG_BACKEND": "mcp",
            "MCP_URL": BASE + "/mcp",
            "MCP_API_KEY": key["key"],
            "MCP_KB_ID": kb["id"],
            "MCP_TIMEOUT": "10s",
            "RAG_TOP_K": "5",
            "LLM_BASE_URL": f"http://127.0.0.1:{model.server_port}/v1",
            "LLM_API_KEY": "test-only",
            "LLM_MODEL": "acceptance",
            "MCP_AGENT_ACCEPTANCE": "1",
            "MCP_TEST_MARKER": marker,
        }
        environment["MCP_TEST_CONTROL_URL"] = f"http://127.0.0.1:{model.server_port}"
        environment["MCP_TEST_CONTROL_KEY"] = control_key

        def execute(name: str, command: list[str], success: bool = True):
            result = subprocess.run(
                command,
                cwd=AGENT,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
            output = (result.stdout + result.stderr).replace(key["key"], "<redacted>")
            output = output.replace(control_key, "<redacted>")
            (OUTPUT / f"{name}.log").write_text(output, encoding="utf-8")
            if (result.returncode == 0) != success:
                raise AssertionError(f"{name} failed expectation; see private local report")
            print(f"PASS {name}", flush=True)
            return result

        def action(name: str) -> None:
            status = checked(api.get("/v1/mcp-service"))
            checked(
                api.post(
                    "/v1/mcp-service/actions",
                    json={"expected_generation": status["generation"], "action": name},
                )
            )
            for _attempt in range(30):
                status = checked(api.get("/v1/mcp-service"))
                if status["generation"] == status["observed_generation"]:
                    return
                time.sleep(0.5)
            raise TimeoutError("MCP action did not complete")

        try:
            action_callback = action
            execute("mcp-cli", ["go", "run", "./test/mcp-smoke", "-query", question])
            chat = execute(
                "agent-cli",
                ["go", "run", "./test/agent-chat", "-rag", "-once", question, "-pretty"],
            )
            assert marker in chat.stdout and observed == [True]
            execute(
                "websocket",
                [
                    "go",
                    "test",
                    "./internal/session",
                    "-run",
                    "^TestMCPWebSocketAcceptance$",
                    "-count=1",
                    "-v",
                ],
            )
            action("stop")
            before = len(observed)
            execute(
                "mcp-stopped",
                ["go", "run", "./test/agent-chat", "-rag", "-once", question],
                success=False,
            )
            assert len(observed) == before
            action("start")
            execute("mcp-recovered", ["go", "run", "./test/mcp-smoke", "-query", question])
            execute(
                "same-client-restart",
                [
                    "go",
                    "test",
                    "./test/mcp-smoke",
                    "-run",
                    "^TestLiveManagedMCPReusesClientAfterRestart$",
                    "-count=1",
                    "-v",
                ],
            )
            (OUTPUT / "summary.json").write_text(
                json.dumps(
                    {
                        "knowledge_base_id": kb["id"],
                        "document_id": doc["id"],
                        "marker": marker,
                        "real_cairn": True,
                        "real_embedding": True,
                        "deterministic_chat_model": True,
                    },
                    indent=2,
                )
            )
        finally:
            try:
                status = checked(api.get("/v1/mcp-service"))
                if status["desired_state"] != "running":
                    action("start")
            finally:
                checked(api.delete(f"/v1/api-keys/{key['api_key']['id']}"))
                print("Temporary query key revoked; no live credential saved", flush=True)


if __name__ == "__main__":
    run()
