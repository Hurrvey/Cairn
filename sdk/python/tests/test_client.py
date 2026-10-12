import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from cairn_client import (
    AsyncCairnClient,
    CairnClient,
    CairnError,
    RetrievalRequest,
    UpdateKbRequest,
)

RESPONSE = {
    "request_id": "req_1",
    "results": [],
    "usage": {},
    "degraded": [{"stage": "rerank", "reason": "load_shed", "detail": "Skipped"}],
    "partial_failures": [],
    "truncated_to_token_budget": False,
    "explain": None,
}

DOCUMENT = {
    "id": "doc_1",
    "kb_id": "kb_1",
    "title": "Guide",
    "source_type": "upload",
    "source_ref": None,
    "mime_type": "text/markdown",
    "size_bytes": 10,
    "content_hash": "abc",
    "state": "indexed",
    "stage_detail": None,
    "error_code": None,
    "error_detail": None,
    "progress_pct": 100,
    "revision": 1,
    "page_count": 1,
    "chunk_count": 1,
    "created_at": "2026-09-30T00:00:00Z",
    "indexed_at": "2026-09-30T00:00:01Z",
    "metadata": {},
}

CHUNK = {
    "id": "chk_1",
    "document_id": "doc_1",
    "index_version": 1,
    "parent_id": None,
    "ordinal": 0,
    "content": "hello",
    "token_count": 1,
    "metadata": {},
    "is_edited": True,
    "knowledge_base_id": "kb_1",
}
EDITED_CHUNK = {key: value for key, value in CHUNK.items() if key != "knowledge_base_id"}


def test_auth_typed_response_and_redacted_repr():
    def handler(request):
        assert request.headers["authorization"] == "Bearer private-key"
        assert request.url.path == "/v1/retrieval/query"
        return httpx.Response(200, json=RESPONSE)

    transport = httpx.Client(transport=httpx.MockTransport(handler))
    with CairnClient("https://example.test", "private-key", http_client=transport) as client:
        result = client.retrieve(
            RetrievalRequest(targets=[{"knowledge_base_id": "kb_t"}], query="test")
        )
        assert result.degraded[0].reason == "load_shed"
        assert "private-key" not in repr(client)
    assert not transport.is_closed
    transport.close()


def test_error_redaction_and_retry_after_without_mutation_retry():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            429,
            headers={"Retry-After": "9", "X-Request-Id": "req_1"},
            json={"code": "RATE_LIMIT_EXCEEDED", "detail": "private-key"},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = CairnClient("https://example.test", "private-key", http_client=http, max_retries=3)
        with pytest.raises(CairnError) as error:
            client.request("POST", "/v1/knowledge-bases", body={})
        assert error.value.retry_after == 9 and error.value.request_id == "req_1"
        assert "private-key" not in str(error.value) and len(calls) == 1


def test_error_redacts_server_echoes_from_all_public_fields_and_repr():
    def handler(request):
        return httpx.Response(
            400,
            headers={"X-Request-Id": "private-key"},
            json={"code": "private-key"},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = CairnClient("https://example.test", "private-key", http_client=http)
        with pytest.raises(CairnError) as caught:
            client.request("GET", "/v1/meta")
    assert "private-key" not in str(caught.value)
    assert "private-key" not in repr(caught.value)
    assert caught.value.code is None
    assert caught.value.request_id is None


def test_typed_payload_preserves_explicit_null_and_omits_unset_defaults():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        if request.url.path.endswith("/retrieval/query"):
            return httpx.Response(200, json=RESPONSE)
        return httpx.Response(503, json={"code": "UPSTREAM_UNAVAILABLE"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = CairnClient("https://example.test", "key", http_client=http)
        client.retrieve(RetrievalRequest(targets=[{"knowledge_base_id": "kb_1"}], query="q"))
        with pytest.raises(CairnError):
            client.update_knowledge_base("kb_1", UpdateKbRequest(description=None))

    assert bodies[0] == {"targets": [{"knowledge_base_id": "kb_1"}], "query": "q"}
    assert bodies[1] == {"description": None}


def test_low_level_cannot_enable_mutation_retries():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"code": "UPSTREAM_UNAVAILABLE"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = CairnClient("https://example.test", "key", http_client=http, max_retries=3)
        with pytest.raises(CairnError):
            client.request("DELETE", "/v1/knowledge-bases/kb_1", retryable=True)
    assert calls == 1


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf")])
def test_rejects_non_finite_timeout(timeout):
    with pytest.raises(ValueError):
        CairnClient("https://example.test", "key", timeout=timeout)


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, float("nan"), float("inf")])
def test_rejects_invalid_response_limits(limit):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as http:
        client = CairnClient("https://example.test", "key", http_client=http)
        with pytest.raises(ValueError):
            client.request("GET", "/v1/meta", response_limit=limit)


@pytest.mark.parametrize("retry_after", ["nan", "inf", "-inf"])
def test_non_finite_retry_after_is_ignored_without_crashing(retry_after):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, headers={"Retry-After": retry_after})
        return httpx.Response(200, json={"items": [], "has_more": False})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = CairnClient("https://example.test", "key", http_client=http, max_retries=1)
        assert client.list_knowledge_bases().items == []
    assert calls == 2


def test_redirects_never_forward_credentials_to_another_origin():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://foreign.test/"})

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as http:
        client = CairnClient("https://example.test", "private-key", http_client=http)
        with pytest.raises(CairnError):
            client.list_knowledge_bases()
        assert len(calls) == 1


@pytest.mark.parametrize(
    "base_url",
    [
        "ftp://example.test",
        "https://user:pass@example.test",
        "https://example.test?key=secret",
        "https://example.test#fragment",
    ],
)
def test_rejects_unsafe_base_urls(base_url):
    with pytest.raises(ValueError):
        CairnClient(base_url, "key")


@pytest.mark.parametrize(
    "path",
    ["https://foreign.test/path", "//foreign.test/path", "/path?api_key=secret", "/path#x"],
)
def test_low_level_request_rejects_paths_that_can_bypass_origin(path):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as http:
        client = CairnClient("https://example.test", "key", http_client=http)
        with pytest.raises(ValueError):
            client.request("GET", path)


def test_rejects_encoded_dot_segments_and_empty_api_keys():
    with pytest.raises(ValueError):
        CairnClient("https://example.test", "")
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as http:
        client = CairnClient("https://example.test", "key", http_client=http)
        with pytest.raises(ValueError):
            client.request("GET", "/v1/%2e%2e/foreign")
        with pytest.raises(ValueError):
            client.request("GET", "/v1/%2E")
        with pytest.raises(ValueError):
            client.request("GET", "/v1/")


def test_low_level_rejects_header_injection():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as http:
        client = CairnClient("https://example.test", "key", http_client=http)
        with pytest.raises(ValueError):
            client.request("GET", "/v1/meta", headers={"X-Request-Id": "ok\r\nX-Evil: 1"})


def test_transport_failure_does_not_retain_secret_as_exception_cause():
    def handler(request):
        raise httpx.ConnectError("failed", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = CairnClient("https://example.test", "private-key", http_client=http)
        with pytest.raises(CairnError) as caught:
            client.request("GET", "/v1/meta")
    assert caught.value.__cause__ is None
    assert "private-key" not in repr(caught.value)


@pytest.mark.parametrize("header", ["Authorization", "authorization", "Host", "HOST"])
def test_low_level_request_cannot_override_security_headers(header):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as http:
        client = CairnClient("https://example.test", "key", http_client=http)
        with pytest.raises(ValueError):
            client.request("GET", "/v1/meta", headers={header: "hostile"})


def test_crud_paths_encode_ids_and_mutations_are_never_retried():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.raw_path, request.content))
        return httpx.Response(503, json={"code": "UPSTREAM_UNAVAILABLE"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = CairnClient("https://example.test", "key", http_client=http, max_retries=4)
        with pytest.raises(CairnError):
            client.update_knowledge_base("kb/../other", {"name": "new"})
        with pytest.raises(CairnError):
            client.retry_document("kb/../other", "doc?secret=1")
        with pytest.raises(CairnError):
            client.delete_document("kb/../other", "doc#fragment")
        with pytest.raises(CairnError):
            client.edit_chunk("kb/../other", "chunk/slash", {"content": "edited"})

    assert [entry[0] for entry in seen] == ["PATCH", "POST", "DELETE", "PATCH"]
    assert b"kb%2F..%2Fother" in seen[0][1]
    assert b"doc%3Fsecret%3D1" in seen[1][1]
    assert b"doc%23fragment" in seen[2][1]
    assert b"chunk%2Fslash" in seen[3][1]


def test_upload_uses_exact_multipart_fields_without_retry():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "POST"
        assert request.url.raw_path == b"/v1/knowledge-bases/kb%2F1/documents/upload"
        assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
        assert b'name="file"; filename="guide.md"' in request.content
        assert b'name="metadata"' in request.content
        assert json.dumps({"lang": "en"}).encode() in request.content
        return httpx.Response(
            202,
            json={"filename": "guide.md", "status": "accepted", "document": DOCUMENT},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = CairnClient("https://example.test", "key", http_client=http, max_retries=3)
        result = client.upload_document(
            "kb/1", b"# Guide", filename="guide.md", metadata={"lang": "en"}
        )
    assert result.document.id == "doc_1"
    assert len(calls) == 1


def test_streamed_upload_error_is_bounded_closed_and_redacted():
    class ErrorStream(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            yield b'{"code":"UPLOAD_FAILED","request_id":"private-key"}'

        def close(self):
            self.closed = True

    stream = ErrorStream()
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                400,
                stream=stream,
                headers={
                    "Content-Type": "application/problem+json",
                    "X-Request-Id": "private-key",
                },
            )
        )
    ) as http:
        client = CairnClient("https://example.test", "private-key", http_client=http)
        with pytest.raises(CairnError) as caught:
            client.upload_document("kb_1", b"text", filename="doc.txt")
    assert caught.value.code == "UPLOAD_FAILED"
    assert caught.value.request_id is None
    assert "private-key" not in repr(caught.value)
    assert stream.closed


def test_upload_redirect_request_id_is_redacted_and_response_closed():
    class RedirectStream(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            yield b""

        def close(self):
            self.closed = True

    stream = RedirectStream()
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                302,
                stream=stream,
                headers={"Location": "https://foreign.test", "X-Request-Id": "private-key"},
            )
        )
    ) as http:
        client = CairnClient("https://example.test", "private-key", http_client=http)
        with pytest.raises(CairnError) as caught:
            client.upload_document("kb_1", b"text", filename="doc.txt")
    assert caught.value.request_id is None
    assert stream.closed


def test_typed_citations_and_bounded_markdown():
    def handler(request):
        if request.url.path.startswith("/v1/chunks/"):
            return httpx.Response(200, json=CHUNK)
        return httpx.Response(
            200,
            content=b"# Guide",
            headers={
                "Content-Type": "text/markdown",
                "X-Request-Id": "req_content",
                "X-Document-Revision": "1",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = CairnClient("https://example.test", "key", http_client=http)
        assert client.get_chunk("chunk/one").knowledge_base_id == "kb_1"
        assert client.document_content("doc/one") == "# Guide"
        details = client.document_content_details("doc/one")
        assert (details.text, details.request_id, details.revision) == ("# Guide", "req_content", 1)


def test_list_methods_omit_absent_cursor_and_chunk_filters():
    urls = []

    def handler(request):
        urls.append(str(request.url))
        if request.url.path.endswith("/chunks"):
            return httpx.Response(200, json=[EDITED_CHUNK])
        if request.url.path.endswith("/documents"):
            return httpx.Response(200, json={"items": [], "has_more": False})
        return httpx.Response(200, json={"items": [], "has_more": False})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = CairnClient("https://example.test", "key", http_client=http)
        client.list_knowledge_bases()
        client.list_documents("kb_1")
        chunks = client.list_chunks("kb_1", "doc_1")

    assert urls == [
        "https://example.test/v1/knowledge-bases?limit=50",
        "https://example.test/v1/knowledge-bases/kb_1/documents?limit=50",
        "https://example.test/v1/knowledge-bases/kb_1/documents/doc_1/chunks?limit=50",
    ]
    assert chunks[0].id == "chk_1"


def test_edit_chunk_decodes_actual_chunk_response_without_kb_id():
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=EDITED_CHUNK))
    ) as http:
        client = CairnClient("https://example.test", "key", http_client=http)
        assert client.edit_chunk("kb_1", "chk_1", {"content": "edited"}).id == "chk_1"


def test_sync_total_deadline_closes_a_slow_response_stream():
    class SlowStream(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            time.sleep(0.02)
            yield b"{}"

        def close(self):
            self.closed = True

    stream = SlowStream()
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    ) as http:
        client = CairnClient("https://example.test", "key", http_client=http, timeout=0.001)
        with pytest.raises(CairnError, match="total deadline"):
            client.request("GET", "/v1/meta")
    assert stream.closed


def test_sync_real_http_slow_drip_may_overrun_once_but_never_retries_after_deadline():
    calls = 0

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal calls
            calls += 1
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            time.sleep(0.04)
            self.wfile.write(b"{")
            self.wfile.flush()
            time.sleep(0.08)
            try:
                self.wfile.write(b"}")
            except OSError:
                pass

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        client = CairnClient(
            f"http://127.0.0.1:{server.server_port}", "key", timeout=0.05, max_retries=2
        )
        with pytest.raises(CairnError, match=r"deadline|timed out"):
            client.request("GET", "/v1/meta")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
    elapsed = time.monotonic() - started
    assert elapsed >= 0.08
    assert calls == 1


@pytest.mark.asyncio
async def test_async_query_uses_native_async_transport():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=RESPONSE))
    ) as http:
        async with AsyncCairnClient("https://example.test", "secret", http_client=http) as client:
            result = await client.retrieve(
                {"targets": [{"knowledge_base_id": "kb_t"}], "query": "test"}
            )
            assert result.request_id == "req_1"
        assert not http.is_closed


@pytest.mark.asyncio
async def test_async_client_exposes_typed_crud_and_document_methods():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=EDITED_CHUNK))
    ) as http:
        client = AsyncCairnClient("https://example.test", "secret", http_client=http)
        result = await client.edit_chunk("kb_1", "chk_1", {"content": "edited"})
        assert result.id == "chk_1"


@pytest.mark.asyncio
async def test_async_upload_is_multipart_and_does_not_retry():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "POST"
        assert request.url.path == "/v1/knowledge-bases/kb_1/documents/upload"
        assert b'name="file"; filename="guide.md"' in request.content
        return httpx.Response(
            202,
            json={"filename": "guide.md", "status": "accepted", "document": DOCUMENT},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = AsyncCairnClient("https://example.test", "secret", http_client=http, max_retries=3)
        result = await client.upload_document(
            "kb_1", b"# Guide", filename="guide.md", metadata={"lang": "en"}
        )
        assert result.document.id == "doc_1"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_async_streamed_upload_error_is_closed_and_respects_total_deadline():
    class SlowStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            await asyncio.sleep(0.05)
            yield b'{"code":"LATE"}'

        async def aclose(self):
            self.closed = True

    stream = SlowStream()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(400, stream=stream))
    ) as http:
        client = AsyncCairnClient("https://example.test", "secret", http_client=http, timeout=0.001)
        with pytest.raises(CairnError, match="timed out"):
            await client.upload_document("kb_1", b"text", filename="doc.txt")
    assert stream.closed
