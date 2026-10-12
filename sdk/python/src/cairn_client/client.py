from __future__ import annotations

import asyncio
import email.utils
import json
import math
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import quote, unquote, urlsplit

import httpx
from pydantic import BaseModel

from .models import (
    BatchRequest,
    BatchResponse,
    ChunkResponse,
    CitedChunkResponse,
    CreateKbRequest,
    CursorPageDocumentResponse,
    CursorPageKnowledgeBaseResponse,
    DocumentRegistrationResponse,
    DocumentResponse,
    EditChunkRequest,
    KnowledgeBaseResponse,
    RetrievalRequest,
    RetrievalResponse,
    UpdateKbRequest,
)

_ModelT = TypeVar("_ModelT", bound=BaseModel)
_JSON_LIMIT = 8 * 1024 * 1024
_CONTENT_LIMIT = 20 * 1024 * 1024
_UPLOAD_LIMIT = 50 * 1024 * 1024
_ERROR_LIMIT = 64 * 1024
_RETRYABLE_STATUS = frozenset({429, 502, 503, 504})
_READ_METHODS = frozenset({"GET", "HEAD"})
_RETRYABLE_PATHS = frozenset({"/v1/retrieval/query", "/v1/retrieval/batch"})


@dataclass(frozen=True)
class DocumentContent:
    text: str
    request_id: str | None
    revision: int | None


class CairnError(Exception):
    """A bounded, credential-safe Cairn client failure."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        request_id: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.request_id = request_id
        self.retry_after = retry_after

    def __repr__(self) -> str:
        return (
            f"CairnError(status_code={self.status_code!r}, code={self.code!r}, "
            f"request_id={self.request_id!r}, retry_after={self.retry_after!r})"
        )


def _base_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("base_url must be an http(s) URL without credentials, query or fragment")
    return value.rstrip("/")


def _path(value: str) -> str:
    parsed = urlsplit(value)
    decoded_segments = [unquote(segment) for segment in parsed.path.split("/")[1:]]
    if (
        not value.startswith("/")
        or value.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or "\\" in value
        or any(segment in {"", ".", ".."} for segment in decoded_segments)
    ):
        raise ValueError(
            "request path must be an absolute-path reference without query or fragment"
        )
    return value


def _segment(value: str) -> str:
    return quote(value, safe="")


def _upload_bytes(path: Path) -> bytes:
    with path.open("rb") as source:
        content = source.read(_UPLOAD_LIMIT + 1)
    if len(content) > _UPLOAD_LIMIT:
        raise CairnError("Document upload exceeds the upload size limit.")
    return content


def _safe_request_id(response: httpx.Response, api_key: str) -> str | None:
    request_id = response.headers.get("X-Request-Id")
    if request_id is None or len(request_id) > 256 or api_key in request_id:
        return None
    return request_id


def _json_body(value: BaseModel | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_unset=True)
    return dict(value)


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
        return max(0.0, parsed) if math.isfinite(parsed) else None
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _error(response: httpx.Response, api_key: str) -> CairnError:
    request_id = _safe_request_id(response, api_key)
    code: str | None = None
    message = f"Cairn request failed with HTTP {response.status_code}."
    content_type = response.headers.get("content-type", "").lower()
    if "json" in content_type and len(response.content) <= _ERROR_LIMIT:
        try:
            payload = response.json()
        except (ValueError, UnicodeError):
            payload = None
        if isinstance(payload, dict):
            candidate = payload.get("code")
            if isinstance(candidate, str) and len(candidate) <= 128 and api_key not in candidate:
                code = candidate
            candidate_id = payload.get("request_id")
            if (
                request_id is None
                and isinstance(candidate_id, str)
                and len(candidate_id) <= 256
                and api_key not in candidate_id
            ):
                request_id = candidate_id
    return CairnError(
        message.replace(api_key, "***"),
        status_code=response.status_code,
        code=code,
        request_id=request_id,
        retry_after=_retry_after(response.headers.get("Retry-After")),
    )


def _decode_json(response: httpx.Response, model: type[_ModelT]) -> _ModelT:
    if len(response.content) > _JSON_LIMIT:
        raise CairnError("Cairn response exceeded the JSON size limit.")
    try:
        result = model.model_validate_json(response.content)
    except (ValueError, UnicodeError):
        failure = CairnError("Cairn returned an invalid response.")
    else:
        return result
    raise failure


class CairnClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        http_client: httpx.Client | None = None,
        timeout: float = 30.0,
        max_retries: int = 0,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("api_key must not be empty")
        if not math.isfinite(timeout) or timeout <= 0 or max_retries < 0:
            raise ValueError("timeout must be positive and max_retries must be non-negative")
        self._base_url = _base_url(base_url)
        self._api_key = api_key
        self._timeout = timeout
        self._max_retries = max_retries
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(follow_redirects=False, trust_env=False)

    def __repr__(self) -> str:
        return f"CairnClient(base_url={self._base_url!r})"

    def __enter__(self) -> CairnClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        retryable: bool | None = None,
        response_limit: int = _JSON_LIMIT,
    ) -> httpx.Response:
        if (
            not isinstance(response_limit, int)
            or isinstance(response_limit, bool)
            or response_limit <= 0
        ):
            raise ValueError("response_limit must be a positive integer")
        method = method.upper()
        safe_path = _path(path)
        supplied_headers = {key.lower(): value for key, value in (headers or {}).items()}
        if "authorization" in supplied_headers or "host" in supplied_headers:
            raise ValueError("Authorization and Host headers are managed by CairnClient")
        if any(
            "\r" in key or "\n" in key or "\r" in value or "\n" in value
            for key, value in (headers or {}).items()
        ):
            raise ValueError("header names and values must not contain line breaks")
        request_headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            **dict(headers or {}),
        }
        declared_retryable = method in _READ_METHODS or (
            method == "POST" and safe_path in _RETRYABLE_PATHS
        )
        can_retry = declared_retryable and retryable is not False
        attempts = self._max_retries + 1 if can_retry else 1
        deadline = time.monotonic() + self._timeout
        for attempt in range(attempts):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CairnError("Cairn request exceeded its total deadline.")
            request = self._http.build_request(
                method,
                self._base_url + safe_path,
                json=dict(body) if body is not None else None,
                params=params,
                headers=request_headers,
                timeout=remaining,
            )
            response: httpx.Response | None = None
            transport_failure: CairnError | None = None
            try:
                response = self._http.send(request, stream=True, follow_redirects=False)
                chunks = bytearray()
                for chunk in response.iter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > response_limit:
                        raise CairnError("Cairn response exceeded the size limit.")
                    if time.monotonic() >= deadline:
                        raise CairnError("Cairn request exceeded its total deadline.")
                response._content = bytes(chunks)
            except httpx.TimeoutException:
                transport_failure = CairnError("Cairn request timed out.")
            except httpx.HTTPError:
                transport_failure = CairnError("Cairn transport failed.")
            finally:
                if response is not None:
                    response.close()
            if transport_failure is not None:
                if attempt + 1 >= attempts:
                    raise transport_failure
                continue
            if 300 <= response.status_code < 400:
                raise CairnError(
                    "Cairn redirects are disabled.",
                    status_code=response.status_code,
                    request_id=_safe_request_id(response, self._api_key),
                )
            if response.is_success:
                return response
            failure = _error(response, self._api_key)
            if attempt + 1 >= attempts or response.status_code not in _RETRYABLE_STATUS:
                raise failure
            delay = failure.retry_after or 0.0
            if delay >= deadline - time.monotonic():
                raise failure
            if delay:
                time.sleep(delay)
        raise AssertionError("unreachable")

    def retrieve(self, request: RetrievalRequest | Mapping[str, Any]) -> RetrievalResponse:
        response = self.request(
            "POST", "/v1/retrieval/query", body=_json_body(request), retryable=True
        )
        return _decode_json(response, RetrievalResponse)

    def batch(self, request: BatchRequest | Mapping[str, Any]) -> BatchResponse:
        response = self.request(
            "POST", "/v1/retrieval/batch", body=_json_body(request), retryable=True
        )
        return _decode_json(response, BatchResponse)

    def list_knowledge_bases(
        self, *, limit: int = 50, cursor: str | None = None
    ) -> CursorPageKnowledgeBaseResponse:
        response = self.request(
            "GET",
            "/v1/knowledge-bases",
            params={
                key: value
                for key, value in {"limit": limit, "cursor": cursor}.items()
                if value is not None
            },
        )
        return _decode_json(response, CursorPageKnowledgeBaseResponse)

    def iter_knowledge_bases(self, *, page_size: int = 50) -> Iterator[KnowledgeBaseResponse]:
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            page = self.list_knowledge_bases(limit=page_size, cursor=cursor)
            yield from page.items
            if not page.has_more or page.next_cursor is None:
                return
            if page.next_cursor in seen:
                raise CairnError("Cairn returned a repeated pagination cursor.")
            seen.add(page.next_cursor)
            cursor = page.next_cursor

    def get_knowledge_base(self, knowledge_base_id: str) -> KnowledgeBaseResponse:
        response = self.request("GET", f"/v1/knowledge-bases/{_segment(knowledge_base_id)}")
        return _decode_json(response, KnowledgeBaseResponse)

    def create_knowledge_base(
        self, request: CreateKbRequest | Mapping[str, Any]
    ) -> KnowledgeBaseResponse:
        response = self.request("POST", "/v1/knowledge-bases", body=_json_body(request))
        return _decode_json(response, KnowledgeBaseResponse)

    def update_knowledge_base(
        self, knowledge_base_id: str, request: UpdateKbRequest | Mapping[str, Any]
    ) -> KnowledgeBaseResponse:
        response = self.request(
            "PATCH",
            f"/v1/knowledge-bases/{_segment(knowledge_base_id)}",
            body=_json_body(request),
        )
        return _decode_json(response, KnowledgeBaseResponse)

    def delete_knowledge_base(self, knowledge_base_id: str) -> dict[str, Any]:
        response = self.request("DELETE", f"/v1/knowledge-bases/{_segment(knowledge_base_id)}")
        return response.json() if response.content else {}

    def list_documents(
        self, knowledge_base_id: str, *, limit: int = 50, cursor: str | None = None
    ) -> CursorPageDocumentResponse:
        response = self.request(
            "GET",
            f"/v1/knowledge-bases/{_segment(knowledge_base_id)}/documents",
            params={
                key: value
                for key, value in {"limit": limit, "cursor": cursor}.items()
                if value is not None
            },
        )
        return _decode_json(response, CursorPageDocumentResponse)

    def get_document(self, knowledge_base_id: str, document_id: str) -> DocumentResponse:
        response = self.request(
            "GET",
            f"/v1/knowledge-bases/{_segment(knowledge_base_id)}/documents/{_segment(document_id)}",
        )
        return _decode_json(response, DocumentResponse)

    def upload_document(
        self,
        knowledge_base_id: str,
        file: str | Path | bytes,
        *,
        filename: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> DocumentRegistrationResponse:
        if isinstance(file, bytes):
            content = file
            upload_name = filename or "document"
        else:
            path = Path(file)
            content = _upload_bytes(path)
            upload_name = filename or path.name
        response = self._multipart_request(
            f"/v1/knowledge-bases/{_segment(knowledge_base_id)}/documents/upload",
            content,
            upload_name,
            metadata,
        )
        return _decode_json(response, DocumentRegistrationResponse)

    def _multipart_request(
        self,
        path: str,
        content: bytes,
        filename: str,
        metadata: Mapping[str, Any] | None,
    ) -> httpx.Response:
        if len(content) > _UPLOAD_LIMIT:
            raise CairnError("Document upload exceeds the upload size limit.")
        request = self._http.build_request(
            "POST",
            self._base_url + _path(path),
            headers={"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"},
            files={"file": (filename, content)},
            data={"metadata": json.dumps(dict(metadata))} if metadata is not None else None,
            timeout=self._timeout,
        )
        deadline = time.monotonic() + self._timeout
        transport_failure: CairnError | None = None
        response: httpx.Response | None = None
        try:
            response = self._http.send(request, stream=True, follow_redirects=False)
            chunks = bytearray()
            for chunk in response.iter_bytes():
                chunks.extend(chunk)
                if len(chunks) > _JSON_LIMIT:
                    raise CairnError("Cairn response exceeded the JSON size limit.")
                if time.monotonic() >= deadline:
                    raise CairnError("Cairn request exceeded its total deadline.")
            response._content = bytes(chunks)
        except httpx.TimeoutException:
            transport_failure = CairnError("Cairn request timed out.")
        except httpx.HTTPError:
            transport_failure = CairnError("Cairn transport failed.")
        finally:
            if response is not None:
                response.close()
        if transport_failure is not None:
            raise transport_failure
        assert response is not None
        if 300 <= response.status_code < 400:
            raise CairnError(
                "Cairn redirects are disabled.",
                status_code=response.status_code,
                request_id=_safe_request_id(response, self._api_key),
            )
        if not response.is_success:
            raise _error(response, self._api_key)
        return response

    def retry_document(self, knowledge_base_id: str, document_id: str) -> dict[str, Any]:
        response = self.request(
            "POST",
            f"/v1/knowledge-bases/{_segment(knowledge_base_id)}/documents/"
            f"{_segment(document_id)}/retry",
        )
        return response.json() if response.content else {}

    def delete_document(self, knowledge_base_id: str, document_id: str) -> dict[str, Any]:
        response = self.request(
            "DELETE",
            f"/v1/knowledge-bases/{_segment(knowledge_base_id)}/documents/{_segment(document_id)}",
        )
        return response.json() if response.content else {}

    def edit_chunk(
        self,
        knowledge_base_id: str,
        chunk_id: str,
        request: EditChunkRequest | Mapping[str, Any],
    ) -> ChunkResponse:
        response = self.request(
            "PATCH",
            f"/v1/knowledge-bases/{_segment(knowledge_base_id)}/chunks/{_segment(chunk_id)}",
            body=_json_body(request),
        )
        return _decode_json(response, ChunkResponse)

    def get_chunk(self, chunk_id: str) -> CitedChunkResponse:
        return _decode_json(
            self.request("GET", f"/v1/chunks/{_segment(chunk_id)}"), CitedChunkResponse
        )

    def list_chunks(
        self,
        knowledge_base_id: str,
        document_id: str,
        *,
        index_version: int | None = None,
        limit: int = 50,
        after_ordinal: int | None = None,
    ) -> list[ChunkResponse]:
        response = self.request(
            "GET",
            f"/v1/knowledge-bases/{_segment(knowledge_base_id)}/documents/"
            f"{_segment(document_id)}/chunks",
            params={
                key: value
                for key, value in {
                    "index_version": index_version,
                    "limit": limit,
                    "after_ordinal": after_ordinal,
                }.items()
                if value is not None
            },
        )
        if len(response.content) > _JSON_LIMIT:
            raise CairnError("Cairn response exceeded the JSON size limit.")
        try:
            return [ChunkResponse.model_validate(item) for item in response.json()]
        except (TypeError, ValueError, UnicodeError):
            raise CairnError("Cairn returned an invalid response.") from None

    def document_content(self, document_id: str) -> str:
        return self.document_content_details(document_id).text

    def document_content_details(self, document_id: str) -> DocumentContent:
        response = self.request(
            "GET",
            f"/v1/documents/{_segment(document_id)}/content",
            headers={"Accept": "text/markdown, text/plain"},
            response_limit=_CONTENT_LIMIT,
        )
        try:
            text = response.content.decode("utf-8")
        except UnicodeError:
            raise CairnError("Cairn returned invalid UTF-8 document content.") from None
        raw_revision = response.headers.get("X-Document-Revision")
        try:
            revision = int(raw_revision) if raw_revision is not None else None
        except ValueError:
            revision = None
        return DocumentContent(text, response.headers.get("X-Request-Id"), revision)


class AsyncCairnClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
        max_retries: int = 0,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("api_key must not be empty")
        if not math.isfinite(timeout) or timeout <= 0 or max_retries < 0:
            raise ValueError("timeout must be positive and max_retries must be non-negative")
        self._base_url = _base_url(base_url)
        self._api_key = api_key
        self._timeout = timeout
        self._max_retries = max_retries
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(follow_redirects=False, trust_env=False)

    def __repr__(self) -> str:
        return f"AsyncCairnClient(base_url={self._base_url!r})"

    async def __aenter__(self) -> AsyncCairnClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        retryable: bool | None = None,
        response_limit: int = _JSON_LIMIT,
    ) -> httpx.Response:
        if (
            not isinstance(response_limit, int)
            or isinstance(response_limit, bool)
            or response_limit <= 0
        ):
            raise ValueError("response_limit must be a positive integer")
        method = method.upper()
        safe_path = _path(path)
        supplied_headers = {key.lower(): value for key, value in (headers or {}).items()}
        if "authorization" in supplied_headers or "host" in supplied_headers:
            raise ValueError("Authorization and Host headers are managed by AsyncCairnClient")
        if any(
            "\r" in key or "\n" in key or "\r" in value or "\n" in value
            for key, value in (headers or {}).items()
        ):
            raise ValueError("header names and values must not contain line breaks")
        declared_retryable = method in _READ_METHODS or (
            method == "POST" and safe_path in _RETRYABLE_PATHS
        )
        can_retry = declared_retryable and retryable is not False
        attempts = self._max_retries + 1 if can_retry else 1
        deadline = asyncio.get_running_loop().time() + self._timeout
        for attempt in range(attempts):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise CairnError("Cairn request exceeded its total deadline.")
            request = self._http.build_request(
                method,
                self._base_url + safe_path,
                json=dict(body) if body is not None else None,
                params=params,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Accept": "application/json",
                    **dict(headers or {}),
                },
                timeout=remaining,
            )
            response: httpx.Response | None = None
            transport_failure: CairnError | None = None
            try:
                async with asyncio.timeout(remaining):
                    response = await self._http.send(request, stream=True, follow_redirects=False)
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > response_limit:
                            raise CairnError("Cairn response exceeded the size limit.")
                    response._content = bytes(chunks)
            except (TimeoutError, httpx.TimeoutException):
                transport_failure = CairnError("Cairn request timed out.")
            except httpx.HTTPError:
                transport_failure = CairnError("Cairn transport failed.")
            finally:
                if response is not None:
                    await response.aclose()
            if transport_failure is not None:
                if attempt + 1 >= attempts:
                    raise transport_failure
                continue
            if 300 <= response.status_code < 400:
                raise CairnError(
                    "Cairn redirects are disabled.",
                    status_code=response.status_code,
                    request_id=_safe_request_id(response, self._api_key),
                )
            if response.is_success:
                return response
            failure = _error(response, self._api_key)
            if attempt + 1 >= attempts or response.status_code not in _RETRYABLE_STATUS:
                raise failure
            delay = failure.retry_after or 0.0
            if delay >= deadline - asyncio.get_running_loop().time():
                raise failure
            if delay:
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def retrieve(self, request: RetrievalRequest | Mapping[str, Any]) -> RetrievalResponse:
        response = await self.request(
            "POST", "/v1/retrieval/query", body=_json_body(request), retryable=True
        )
        return _decode_json(response, RetrievalResponse)

    async def batch(self, request: BatchRequest | Mapping[str, Any]) -> BatchResponse:
        response = await self.request(
            "POST", "/v1/retrieval/batch", body=_json_body(request), retryable=True
        )
        return _decode_json(response, BatchResponse)

    async def list_knowledge_bases(
        self, *, limit: int = 50, cursor: str | None = None
    ) -> CursorPageKnowledgeBaseResponse:
        response = await self.request(
            "GET",
            "/v1/knowledge-bases",
            params={
                key: value
                for key, value in {"limit": limit, "cursor": cursor}.items()
                if value is not None
            },
        )
        return _decode_json(response, CursorPageKnowledgeBaseResponse)

    async def iter_knowledge_bases(
        self, *, page_size: int = 50
    ) -> AsyncIterator[KnowledgeBaseResponse]:
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            page = await self.list_knowledge_bases(limit=page_size, cursor=cursor)
            for item in page.items:
                yield item
            if not page.has_more or page.next_cursor is None:
                return
            if page.next_cursor in seen:
                raise CairnError("Cairn returned a repeated pagination cursor.")
            seen.add(page.next_cursor)
            cursor = page.next_cursor

    async def get_knowledge_base(self, knowledge_base_id: str) -> KnowledgeBaseResponse:
        response = await self.request("GET", f"/v1/knowledge-bases/{_segment(knowledge_base_id)}")
        return _decode_json(response, KnowledgeBaseResponse)

    async def create_knowledge_base(
        self, request: CreateKbRequest | Mapping[str, Any]
    ) -> KnowledgeBaseResponse:
        response = await self.request("POST", "/v1/knowledge-bases", body=_json_body(request))
        return _decode_json(response, KnowledgeBaseResponse)

    async def update_knowledge_base(
        self, knowledge_base_id: str, request: UpdateKbRequest | Mapping[str, Any]
    ) -> KnowledgeBaseResponse:
        response = await self.request(
            "PATCH",
            f"/v1/knowledge-bases/{_segment(knowledge_base_id)}",
            body=_json_body(request),
        )
        return _decode_json(response, KnowledgeBaseResponse)

    async def delete_knowledge_base(self, knowledge_base_id: str) -> dict[str, Any]:
        response = await self.request(
            "DELETE", f"/v1/knowledge-bases/{_segment(knowledge_base_id)}"
        )
        return response.json() if response.content else {}

    async def list_documents(
        self, knowledge_base_id: str, *, limit: int = 50, cursor: str | None = None
    ) -> CursorPageDocumentResponse:
        response = await self.request(
            "GET",
            f"/v1/knowledge-bases/{_segment(knowledge_base_id)}/documents",
            params={
                key: value
                for key, value in {"limit": limit, "cursor": cursor}.items()
                if value is not None
            },
        )
        return _decode_json(response, CursorPageDocumentResponse)

    async def get_document(self, knowledge_base_id: str, document_id: str) -> DocumentResponse:
        response = await self.request(
            "GET",
            f"/v1/knowledge-bases/{_segment(knowledge_base_id)}/documents/{_segment(document_id)}",
        )
        return _decode_json(response, DocumentResponse)

    async def upload_document(
        self,
        knowledge_base_id: str,
        file: str | Path | bytes,
        *,
        filename: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> DocumentRegistrationResponse:
        if isinstance(file, bytes):
            content = file
            upload_name = filename or "document"
        else:
            path = Path(file)
            content = await asyncio.to_thread(_upload_bytes, path)
            upload_name = filename or path.name
        if len(content) > _UPLOAD_LIMIT:
            raise CairnError("Document upload exceeds the upload size limit.")
        request = self._http.build_request(
            "POST",
            self._base_url
            + _path(f"/v1/knowledge-bases/{_segment(knowledge_base_id)}/documents/upload"),
            headers={"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"},
            files={"file": (upload_name, content)},
            data={"metadata": json.dumps(dict(metadata))} if metadata is not None else None,
            timeout=self._timeout,
        )
        transport_failure: CairnError | None = None
        response: httpx.Response | None = None
        try:
            async with asyncio.timeout(self._timeout):
                response = await self._http.send(request, stream=True, follow_redirects=False)
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > _JSON_LIMIT:
                        raise CairnError("Cairn response exceeded the JSON size limit.")
                response._content = bytes(chunks)
        except (TimeoutError, httpx.TimeoutException):
            transport_failure = CairnError("Cairn request timed out.")
        except httpx.HTTPError:
            transport_failure = CairnError("Cairn transport failed.")
        finally:
            if response is not None:
                await response.aclose()
        if transport_failure is not None:
            raise transport_failure
        assert response is not None
        if 300 <= response.status_code < 400:
            raise CairnError(
                "Cairn redirects are disabled.",
                status_code=response.status_code,
                request_id=_safe_request_id(response, self._api_key),
            )
        if not response.is_success:
            raise _error(response, self._api_key)
        return _decode_json(response, DocumentRegistrationResponse)

    async def retry_document(self, knowledge_base_id: str, document_id: str) -> dict[str, Any]:
        response = await self.request(
            "POST",
            f"/v1/knowledge-bases/{_segment(knowledge_base_id)}/documents/"
            f"{_segment(document_id)}/retry",
        )
        return response.json() if response.content else {}

    async def delete_document(self, knowledge_base_id: str, document_id: str) -> dict[str, Any]:
        response = await self.request(
            "DELETE",
            f"/v1/knowledge-bases/{_segment(knowledge_base_id)}/documents/{_segment(document_id)}",
        )
        return response.json() if response.content else {}

    async def list_chunks(
        self,
        knowledge_base_id: str,
        document_id: str,
        *,
        index_version: int | None = None,
        limit: int = 50,
        after_ordinal: int | None = None,
    ) -> list[ChunkResponse]:
        response = await self.request(
            "GET",
            f"/v1/knowledge-bases/{_segment(knowledge_base_id)}/documents/"
            f"{_segment(document_id)}/chunks",
            params={
                key: value
                for key, value in {
                    "index_version": index_version,
                    "limit": limit,
                    "after_ordinal": after_ordinal,
                }.items()
                if value is not None
            },
        )
        if len(response.content) > _JSON_LIMIT:
            raise CairnError("Cairn response exceeded the JSON size limit.")
        try:
            return [ChunkResponse.model_validate(item) for item in response.json()]
        except (TypeError, ValueError, UnicodeError):
            raise CairnError("Cairn returned an invalid response.") from None

    async def get_chunk(self, chunk_id: str) -> CitedChunkResponse:
        return _decode_json(
            await self.request("GET", f"/v1/chunks/{_segment(chunk_id)}"), CitedChunkResponse
        )

    async def edit_chunk(
        self,
        knowledge_base_id: str,
        chunk_id: str,
        request: EditChunkRequest | Mapping[str, Any],
    ) -> ChunkResponse:
        response = await self.request(
            "PATCH",
            f"/v1/knowledge-bases/{_segment(knowledge_base_id)}/chunks/{_segment(chunk_id)}",
            body=_json_body(request),
        )
        return _decode_json(response, ChunkResponse)

    async def document_content(self, document_id: str) -> str:
        return (await self.document_content_details(document_id)).text

    async def document_content_details(self, document_id: str) -> DocumentContent:
        response = await self.request(
            "GET",
            f"/v1/documents/{_segment(document_id)}/content",
            headers={"Accept": "text/markdown, text/plain"},
            response_limit=_CONTENT_LIMIT,
        )
        try:
            text = response.content.decode("utf-8")
        except UnicodeError:
            raise CairnError("Cairn returned invalid UTF-8 document content.") from None
        raw_revision = response.headers.get("X-Document-Revision")
        try:
            revision = int(raw_revision) if raw_revision is not None else None
        except ValueError:
            revision = None
        return DocumentContent(text, response.headers.get("X-Request-Id"), revision)
