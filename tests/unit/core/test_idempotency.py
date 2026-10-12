"""Request replay isolation and error recovery (FR-I-10)."""

import pytest

from cairn.core.cache import InMemoryCache
from cairn.core.errors import IdempotencyKeyReused, RequestInProgress
from cairn.core.idempotency import idempotent


def request(cache: InMemoryCache, **changes):  # type: ignore[no-untyped-def]
    args = {
        "key": "upload-1",
        "method": "POST",
        "path": "/uploads",
        "principal_id": "u1",
        "body": b"data",
    }
    args.update(changes)
    return idempotent(cache, **args)


async def test_completed_request_replays_the_exact_response() -> None:
    cache = InMemoryCache()
    response = {"status": 201, "body": {"id": "doc_1"}, "headers": {"Location": "/doc_1"}}
    async with request(cache) as guard:
        assert not guard.replayed
        await guard.store(response)
    async with request(cache) as guard:
        assert guard.replayed
        assert guard.stored_response == response


@pytest.mark.parametrize("changes", [{"body": b"other"}, {"method": "PUT"}, {"path": "/other"}])
async def test_key_cannot_be_reused_for_a_different_request(changes: dict[str, object]) -> None:
    cache = InMemoryCache()
    async with request(cache) as guard:
        await guard.store({"status": 201})
    with pytest.raises(IdempotencyKeyReused):
        async with request(cache, **changes):
            pytest.fail("different request was accepted")


async def test_inflight_request_cannot_execute_twice() -> None:
    cache = InMemoryCache()
    async with request(cache):
        with pytest.raises(RequestInProgress):
            async with request(cache):
                pytest.fail("duplicate request was accepted")


async def test_failed_request_can_be_retried() -> None:
    cache = InMemoryCache()
    with pytest.raises(RuntimeError):
        async with request(cache):
            raise RuntimeError("write failed")
    async with request(cache) as guard:
        assert not guard.replayed
        await guard.store({"status": 201})


async def test_same_key_is_isolated_between_principals() -> None:
    cache = InMemoryCache()
    async with request(cache) as guard:
        await guard.store({"private": "u1 response"})
    async with request(cache, principal_id="u2") as guard:
        assert not guard.replayed
        assert guard.stored_response is None


async def test_no_key_does_not_create_replay_state() -> None:
    cache = InMemoryCache()
    for _ in range(2):
        async with request(cache, key=None) as guard:
            assert guard.key is None
            assert not guard.replayed
            await guard.store({"status": 201})
