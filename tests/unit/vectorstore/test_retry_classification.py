"""Which vector-store failures ingestion retries (M06 §4.4).

Unreachable, overloaded (5xx) and rate-limited (429) stores are transient; a
rejected request or an unknown driver fails the same way every time.
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from qdrant_client.http.exceptions import UnexpectedResponse

from cairn.vectorstore.errors import VectorStoreTransient, VectorStoreUnavailable
from cairn.vectorstore.qdrant import _backend
from cairn.vectorstore.registry import VectorBindingRef, VectorStoreRegistry


def _rejected(status: int) -> UnexpectedResponse:
    return UnexpectedResponse(status, "reason", b'{"status": "error"}', httpx.Headers())


@pytest.mark.parametrize("status", [429, 500, 502, 503])
async def test_overload_and_rate_limits_are_transient(status: int) -> None:
    with pytest.raises(VectorStoreTransient) as caught:
        async with _backend("ns"):
            raise _rejected(status)
    assert caught.value.retryable is True


@pytest.mark.parametrize("status", [400, 401, 403, 413, 422])
async def test_rejected_requests_are_terminal(status: int) -> None:
    with pytest.raises(VectorStoreUnavailable) as caught:
        async with _backend("ns"):
            raise _rejected(status)
    assert not isinstance(caught.value, VectorStoreTransient)
    assert caught.value.retryable is False


async def test_an_unreachable_store_is_transient() -> None:
    with pytest.raises(VectorStoreTransient):
        async with _backend("ns"):
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")


async def test_an_unknown_driver_is_terminal() -> None:
    with pytest.raises(VectorStoreUnavailable) as caught:
        await VectorStoreRegistry().for_binding(VectorBindingRef(uuid4(), "pgvector"))
    assert caught.value.retryable is False
