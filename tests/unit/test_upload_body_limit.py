from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI, File, Request, UploadFile

from apps.api import middleware as middleware_module
from apps.api.middleware import ErrorHandlingMiddleware, UploadBodyLimitMiddleware
from cairn.catalog.errors import UnsupportedFileType
from cairn.catalog.router import _new_spool, _validate_upload_signature


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(UploadBodyLimitMiddleware, max_body_bytes=8)
    app.add_middleware(ErrorHandlingMiddleware)

    @app.post("/v1/knowledge-bases/{kb_id}/documents/upload")
    async def upload(kb_id: str, request: Request) -> dict[str, int]:
        return {"size": len(await request.body())}

    return app


async def test_upload_body_limit_rejects_oversized_content_length_before_read() -> None:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/knowledge-bases/kb_test/documents/upload",
            headers={"Content-Length": "9"},
            content=b"",
        )

    assert response.status_code == 413
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "PAYLOAD_TOO_LARGE"


async def test_upload_body_limit_counts_chunks_without_content_length() -> None:
    async def chunks():  # type: ignore[no-untyped-def]
        yield b"12345"
        yield b"6789"

    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/knowledge-bases/kb_test/documents/upload",
            headers={"Transfer-Encoding": "chunked"},
            content=chunks(),
        )

    assert response.status_code == 413
    assert response.json()["code"] == "PAYLOAD_TOO_LARGE"


async def test_upload_body_limit_does_not_apply_to_other_routes() -> None:
    app = _app()

    @app.post("/other")
    async def other(request: Request) -> dict[str, int]:
        return {"size": len(await request.body())}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/other", content=b"123456789")

    assert response.status_code == 200
    assert response.json() == {"size": 9}


async def test_upload_body_limit_interrupts_real_multipart_parser_and_closes_spool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spools = []
    real_spool = middleware_module.tempfile.SpooledTemporaryFile

    def tracked_spool(*args, **kwargs):  # type: ignore[no-untyped-def]
        spool = real_spool(*args, **kwargs)
        spools.append(spool)
        return spool

    monkeypatch.setattr(middleware_module.tempfile, "SpooledTemporaryFile", tracked_spool)
    app = FastAPI()
    app.add_middleware(UploadBodyLimitMiddleware, max_body_bytes=256)
    app.add_middleware(ErrorHandlingMiddleware)

    @app.post("/v1/knowledge-bases/{kb_id}/documents/upload")
    async def actual_upload(kb_id: str, file: UploadFile = File()) -> dict[str, str]:
        return {"filename": file.filename or ""}

    boundary = "cairn-boundary"
    body = (
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="large.md"\r\n'
            "Content-Type: text/markdown\r\n\r\n"
        ).encode()
        + b"x" * 512
        + f"\r\n--{boundary}--\r\n".encode()
    )

    async def chunks():  # type: ignore[no-untyped-def]
        for offset in range(0, len(body), 64):
            yield body[offset : offset + 64]

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/knowledge-bases/kb_test/documents/upload",
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Transfer-Encoding": "chunked",
            },
            content=chunks(),
        )

    assert response.status_code == 413
    assert response.json()["code"] == "PAYLOAD_TOO_LARGE"
    assert spools and all(spool.closed for spool in spools)


async def test_upload_body_limit_replays_allowed_chunked_multipart_to_fastapi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spools = []
    real_spool = middleware_module.tempfile.SpooledTemporaryFile

    def tracked_spool(*args, **kwargs):  # type: ignore[no-untyped-def]
        spool = real_spool(*args, **kwargs)
        spools.append(spool)
        return spool

    monkeypatch.setattr(middleware_module.tempfile, "SpooledTemporaryFile", tracked_spool)
    app = FastAPI()
    app.add_middleware(UploadBodyLimitMiddleware, max_body_bytes=1024)

    @app.post("/v1/knowledge-bases/{kb_id}/documents/upload")
    async def actual_upload(kb_id: str, file: UploadFile = File()) -> dict[str, str]:
        return {"filename": file.filename or "", "content": (await file.read()).decode()}

    boundary = "cairn-allowed"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="small.md"\r\n'
        "Content-Type: text/markdown\r\n\r\n"
        "allowed body"
        f"\r\n--{boundary}--\r\n"
    ).encode()

    async def chunks():  # type: ignore[no-untyped-def]
        for offset in range(0, len(body), 17):
            yield body[offset : offset + 17]

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/knowledge-bases/kb_test/documents/upload",
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Transfer-Encoding": "chunked",
            },
            content=chunks(),
        )

    assert response.status_code == 200
    assert response.json() == {"filename": "small.md", "content": "allowed body"}
    assert spools and all(spool.closed for spool in spools)


async def test_upload_signature_rejects_disguised_pdf_and_invalid_office() -> None:
    pdf = _new_spool()
    pdf.write(b"%PDF-1.7 disguised")
    pdf.seek(0)
    with pytest.raises(UnsupportedFileType, match="PDF"):
        await _validate_upload_signature(pdf, "disguised.txt")
    pdf.close()

    office = _new_spool()
    office.write(b"not a zip")
    office.seek(0)
    with pytest.raises(UnsupportedFileType, match="Office"):
        await _validate_upload_signature(office, "broken.docx")
    office.close()
