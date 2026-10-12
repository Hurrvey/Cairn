"""S3-compatible object storage with bounded multipart writes and reads."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from aiobotocore.config import AioConfig
from aiobotocore.session import get_session
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from cairn.core.errors import ValidationFailed
from cairn.objectstore.base import ObjectInfo, ObjectPage, Uploadable
from cairn.objectstore.errors import (
    ObjectNotFound,
    ObjectStoreTransient,
    ObjectStoreUnavailable,
    ObjectTooLarge,
)
from cairn.objectstore.s3_transport import session_class

PART_SIZE = 16 * 1024 * 1024


class S3Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    bucket: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
    endpoint: str | None = None
    region: str = Field(default="us-east-1", min_length=1, max_length=64)
    path_style: bool = True
    prefix: str = Field(default="", max_length=512)
    allow_private: bool = False

    @model_validator(mode="after")
    def validate_location(self) -> S3Config:
        if self.endpoint:
            url = urlsplit(self.endpoint)
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username
                or url.password
                or url.path not in {"", "/"}
                or url.query
                or url.fragment
            ):
                raise ValueError(
                    "S3 endpoint must be an HTTP(S) origin without credentials or a path."
                )
            _ = url.port
            if not self.path_style:
                raise ValueError("Custom S3 endpoints require path-style addressing.")
        if self.prefix.startswith("/") or "\\" in self.prefix or ".." in self.prefix.split("/"):
            raise ValueError("S3 key prefix must be a relative path without traversal.")
        return self


def translate_error(exc: Exception) -> ObjectStoreUnavailable | ObjectNotFound:
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        if code in {"NoSuchKey", "NotFound", "404"}:
            return ObjectNotFound("Object not found.")
        if code in {"SlowDown", "Throttling", "RequestTimeout"} or status == 429 or status >= 500:
            return ObjectStoreTransient("The S3 service is temporarily unavailable.")
        return ObjectStoreUnavailable("The S3 service rejected the operation; check the binding.")
    return ObjectStoreTransient("The S3 service could not be reached.")


def validate_config(value: object) -> S3Config:
    try:
        return S3Config.model_validate(value)
    except ValidationError as exc:
        raise ValidationFailed(
            "Invalid S3 configuration: check bucket, endpoint and supported fields."
        ) from exc


@asynccontextmanager
async def _errors() -> AsyncIterator[None]:
    try:
        yield
    except (ClientError, BotoCoreError, OSError, TimeoutError) as exc:
        raise translate_error(exc) from exc


class S3ObjectStore:
    def __init__(self, client: Any, config: S3Config, *, context: Any = None) -> None:
        # SDK's generated S3 methods have no static interface.
        self._client = client
        self._config = config
        self._context = context
        self._prefix = config.prefix.rstrip("/") + "/" if config.prefix else ""

    @classmethod
    async def open(
        cls, config: S3Config, credentials: Mapping[str, str], *, timeout_s: float = 30
    ) -> S3ObjectStore:
        if config.endpoint and not (
            credentials.get("access_key_id") and credentials.get("secret_access_key")
        ):
            raise ValidationFailed("Custom S3 endpoints require explicit credentials.")
        kwargs = {
            "aws_access_key_id": credentials.get("access_key_id"),
            "aws_secret_access_key": credentials.get("secret_access_key"),
        }
        if credentials.get("session_token"):
            kwargs["aws_session_token"] = credentials["session_token"]
        context = get_session().create_client(
            "s3",
            endpoint_url=config.endpoint,
            region_name=config.region,
            **kwargs,
            config=AioConfig(
                connect_timeout=5,
                read_timeout=timeout_s,
                proxies={},
                retries={"max_attempts": 2, "mode": "standard"},
                signature_version="s3v4",
                s3={"addressing_style": "path" if config.path_style else "virtual"},
                http_session_cls=session_class(config.endpoint, config.allow_private),
            ),
        )
        async with _errors():
            client = await context.__aenter__()
        return cls(client, config, context=context)

    def __repr__(self) -> str:
        return f"S3ObjectStore(bucket={self._config.bucket!r})"

    def _key(self, key: str, *, empty: bool = False) -> str:
        if (not key and not empty) or key.startswith("/") or "\\" in key or ".." in key.split("/"):
            raise ValidationFailed("Object key must be a relative path without traversal.")
        return self._prefix + key

    async def put(
        self,
        key: str,
        data: Uploadable,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectInfo:
        args: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": self._key(key),
            "ContentType": content_type or "application/octet-stream",
        }
        if metadata:
            args["Metadata"] = {
                "cairn-json": base64.b64encode(
                    json.dumps(dict(metadata), ensure_ascii=False).encode()
                ).decode("ascii")
            }
        size, etag = 0, None
        async with _errors():
            if isinstance(data, bytes):
                result = await self._client.put_object(**args, Body=data)
                size, etag = len(data), result.get("ETag")
            else:
                buffer = bytearray()
                upload_id: str | None = None
                parts: list[dict[str, Any]] = []
                try:
                    async for chunk in data:
                        view = memoryview(chunk)
                        while view:
                            taken = min(PART_SIZE - len(buffer), len(view))
                            buffer.extend(view[:taken])
                            view = view[taken:]
                            size += taken
                            if len(buffer) == PART_SIZE:
                                if len(parts) >= 10_000:
                                    raise ObjectTooLarge("Object exceeds the multipart part limit.")
                                if upload_id is None:
                                    upload_id = (
                                        await self._client.create_multipart_upload(**args)
                                    )["UploadId"]
                                part = await self._client.upload_part(
                                    Bucket=args["Bucket"],
                                    Key=args["Key"],
                                    UploadId=upload_id,
                                    PartNumber=len(parts) + 1,
                                    Body=bytes(buffer),
                                )
                                parts.append({"PartNumber": len(parts) + 1, "ETag": part["ETag"]})
                                buffer.clear()
                    if upload_id is None:
                        result = await self._client.put_object(**args, Body=bytes(buffer))
                    else:
                        if buffer:
                            if len(parts) >= 10_000:
                                raise ObjectTooLarge("Object exceeds the multipart part limit.")
                            part = await self._client.upload_part(
                                Bucket=args["Bucket"],
                                Key=args["Key"],
                                UploadId=upload_id,
                                PartNumber=len(parts) + 1,
                                Body=bytes(buffer),
                            )
                            parts.append({"PartNumber": len(parts) + 1, "ETag": part["ETag"]})
                        result = await self._client.complete_multipart_upload(
                            Bucket=args["Bucket"],
                            Key=args["Key"],
                            UploadId=upload_id,
                            MultipartUpload={"Parts": parts},
                        )
                    etag = result.get("ETag")
                except BaseException:
                    if upload_id is not None:
                        with suppress(Exception):
                            async with asyncio.timeout(5):
                                await self._client.abort_multipart_upload(
                                    Bucket=args["Bucket"], Key=args["Key"], UploadId=upload_id
                                )
                    raise
        return ObjectInfo(
            key=key, size=size, etag=etag, content_type=content_type, metadata=dict(metadata or {})
        )

    async def get(self, key: str) -> AsyncGenerator[bytes]:
        async with _errors():
            response = await self._client.get_object(Bucket=self._config.bucket, Key=self._key(key))
            async with response["Body"] as body:
                while chunk := await body.read(64 * 1024):
                    yield chunk

    async def get_bytes(self, key: str, *, max_bytes: int) -> bytes:
        info = await self.head(key)
        if info is None:
            raise ObjectNotFound("Object not found.")
        if info.size > max_bytes:
            raise ObjectTooLarge("Object exceeds the permitted size.")
        data = bytearray()
        stream = self.get(key)
        try:
            async for chunk in stream:
                if len(data) + len(chunk) > max_bytes:
                    raise ObjectTooLarge("Object exceeds the permitted size.")
                data.extend(chunk)
        finally:
            await stream.aclose()
        return bytes(data)

    async def head(self, key: str) -> ObjectInfo | None:
        try:
            async with _errors():
                value = await self._client.head_object(
                    Bucket=self._config.bucket, Key=self._key(key)
                )
        except ObjectNotFound:
            # HeadObject has no error body: 404 may mean the bucket is missing.
            try:
                await self._client.head_bucket(Bucket=self._config.bucket)
            except Exception as exc:
                raise ObjectStoreUnavailable("The S3 bucket is unavailable.") from exc
            return None
        metadata = value.get("Metadata", {})
        if "cairn-json" in metadata:
            try:
                metadata = json.loads(base64.b64decode(metadata["cairn-json"]))
            except (ValueError, TypeError):
                metadata = {}
        if not isinstance(metadata, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in metadata.items()
        ):
            metadata = {}
        return ObjectInfo(
            key=key,
            size=value["ContentLength"],
            etag=value.get("ETag"),
            content_type=value.get("ContentType"),
            last_modified=value.get("LastModified"),
            metadata=metadata,
        )

    async def delete(self, key: str) -> bool:
        if await self.head(key) is None:
            return False
        async with _errors():
            await self._client.delete_object(Bucket=self._config.bucket, Key=self._key(key))
        return True

    async def list(
        self, prefix: str, *, limit: int = 1000, cursor: str | None = None
    ) -> ObjectPage:
        if not 1 <= limit <= 1000:
            raise ValidationFailed("List limit must be between 1 and 1000.")
        args: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Prefix": self._key(prefix, empty=True),
            "MaxKeys": limit,
        }
        if cursor:
            args["ContinuationToken"] = cursor
        async with _errors():
            result = await self._client.list_objects_v2(**args)
        return ObjectPage(
            items=tuple(
                ObjectInfo(
                    key=row["Key"][len(self._prefix) :],
                    size=row["Size"],
                    etag=row.get("ETag"),
                    last_modified=row.get("LastModified"),
                )
                for row in result.get("Contents", [])
            ),
            next_cursor=result.get("NextContinuationToken"),
        )

    async def delete_prefix(self, prefix: str) -> int:
        deleted = 0
        while True:
            page = await self.list(prefix)
            if not page.items:
                return deleted
            async with _errors():
                result = await self._client.delete_objects(
                    Bucket=self._config.bucket,
                    Delete={
                        "Objects": [{"Key": self._key(item.key)} for item in page.items],
                        "Quiet": False,
                    },
                )
            if result.get("Errors"):
                raise ObjectStoreUnavailable(
                    "The S3 service could not delete every requested object."
                )
            deleted += len(page.items)

    async def presigned_get(self, key: str, ttl: timedelta) -> str:
        seconds = int(ttl.total_seconds())
        if not 1 <= seconds <= 604800:
            raise ValidationFailed("S3 signed URLs require a lifetime between 1 second and 7 days.")
        async with _errors():
            result: str = await self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._config.bucket, "Key": self._key(key)},
                ExpiresIn=seconds,
            )
        return result

    async def health(self) -> bool:
        try:
            await self._client.head_bucket(Bucket=self._config.bucket)
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._context is not None:
            await self._context.__aexit__(None, None, None)
            self._context = None
