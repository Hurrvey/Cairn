from __future__ import annotations

import socket

import pytest
from botocore.exceptions import ClientError

from cairn.core.errors import ValidationFailed
from cairn.objectstore.errors import ObjectNotFound, ObjectStoreTransient, ObjectStoreUnavailable
from cairn.objectstore.s3 import S3Config, translate_error
from cairn.objectstore.s3_transport import guarded_socket


@pytest.mark.parametrize(
    "code,status,expected",
    [
        ("NoSuchKey", 404, ObjectNotFound),
        ("NoSuchBucket", 404, ObjectStoreUnavailable),
        ("AccessDenied", 403, ObjectStoreUnavailable),
        ("SlowDown", 503, ObjectStoreTransient),
    ],
)
def test_s3_errors_are_classified_without_leaking_provider_text(code, status, expected):
    error = ClientError(
        {
            "Error": {"Code": code, "Message": "private credential"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "GetObject",
    )
    translated = translate_error(error)
    assert isinstance(translated, expected)
    assert "private credential" not in str(translated)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://user:pass@example.com",
        "ftp://example.com",
        "https://example.com/path",
        "https://example.com?x=1",
    ],
)
def test_s3_rejects_unsafe_endpoint_forms(endpoint):
    with pytest.raises(ValueError):
        S3Config(bucket="testing", endpoint=endpoint)


def test_socket_guard_checks_literal_ips_too():
    with pytest.raises(ValidationFailed):
        guarded_socket(False)((socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80)))


def test_socket_guard_rejects_unspecified_ipv6():
    with pytest.raises(ValidationFailed):
        guarded_socket(False)((socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::", 80, 0, 0)))


async def test_head_does_not_hide_a_missing_bucket():
    from cairn.objectstore.s3 import S3ObjectStore

    class Client:
        async def head_object(self, **kwargs):
            raise ClientError(
                {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                "HeadObject",
            )

        async def head_bucket(self, **kwargs):
            raise ClientError(
                {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                "HeadBucket",
            )

    with pytest.raises(ObjectStoreUnavailable):
        await S3ObjectStore(Client(), S3Config(bucket="missing")).head("key")


async def test_multipart_upload_aborts_on_source_failure():
    from cairn.objectstore.s3 import S3ObjectStore

    class Client:
        aborted = False

        async def create_multipart_upload(self, **kwargs):
            return {"UploadId": "upload"}

        async def upload_part(self, **kwargs):
            return {"ETag": "part"}

        async def abort_multipart_upload(self, **kwargs):
            self.aborted = True

    client = Client()
    store = S3ObjectStore(client, S3Config(bucket="testing"))

    async def stream():
        yield b"x" * (16 * 1024 * 1024 + 1)
        raise RuntimeError("source disconnected")

    with pytest.raises(RuntimeError):
        await store.put("document", stream())
    assert client.aborted


async def test_registry_replaces_clients_when_credentials_rotate(monkeypatch):
    from uuid import uuid4

    from cairn.objectstore.registry import ObjectBindingRef, ObjectStoreRegistry

    class Store:
        closed = False

        async def health(self):
            return True

        async def close(self):
            self.closed = True

    built = []

    async def open_store(config, credentials, **kwargs):
        store = Store()
        built.append(store)
        return store

    monkeypatch.setattr("cairn.objectstore.s3.S3ObjectStore.open", open_store)
    registry = ObjectStoreRegistry()
    id_ = uuid4()
    one = ObjectBindingRef(
        id_, "s3", {"bucket": "testing"}, {"access_key_id": "a", "secret_access_key": "one"}
    )
    two = ObjectBindingRef(
        id_, "s3", {"bucket": "testing"}, {"access_key_id": "a", "secret_access_key": "two"}
    )
    first = await registry.for_binding(one)
    assert await registry.for_binding(one) is first
    assert await registry.for_binding(two) is not first
    await registry.close()
    assert all(store.closed for store in built)


async def test_retirement_closes_after_active_stream_and_rejects_new_reads():
    from cairn.objectstore.managed import ManagedObjectStore

    class Store:
        closes = 0

        async def get(self, key):
            yield b"first"
            yield b"last"

        async def close(self):
            self.closes += 1

    store = Store()
    managed = ManagedObjectStore(store)
    unused = managed.get("unused")
    stream = managed.get("key")
    assert await anext(stream) == b"first"
    await managed.close()
    assert store.closes == 0
    with pytest.raises(ObjectStoreTransient):
        await anext(unused)
    assert await anext(stream) == b"last"
    await stream.aclose()
    assert store.closes == 1
    await managed.close()
    assert store.closes == 1


async def test_cancelled_initial_probe_closes_unpublished_client(monkeypatch):
    import asyncio
    from uuid import uuid4

    from cairn.objectstore.registry import ObjectBindingRef, ObjectStoreRegistry

    entered = asyncio.Event()

    class Store:
        closed = False

        async def health(self):
            entered.set()
            await asyncio.Event().wait()

        async def close(self):
            self.closed = True

    store = Store()

    async def open_store(*args, **kwargs):
        return store

    monkeypatch.setattr("cairn.objectstore.s3.S3ObjectStore.open", open_store)
    registry = ObjectStoreRegistry()
    task = asyncio.create_task(
        registry.for_binding(ObjectBindingRef(uuid4(), "s3", {"bucket": "testing"}))
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.closed
