"""Connection-time S3 address checks, including redirects and literal IPs."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from aiobotocore.httpsession import AIOHTTPSession
from aiohttp import TCPConnector
from aiohttp.abc import AbstractResolver, ResolveResult

from cairn.core.errors import ValidationFailed
from cairn.core.http import is_blocked_address


def guarded_socket(allow_private: bool) -> Callable[[Any], socket.socket]:
    def create(info: Any) -> socket.socket:
        family, type_, proto, _name, address = info
        if not allow_private and is_blocked_address(ipaddress.ip_address(address[0])):
            raise ValidationFailed("The S3 endpoint resolves to a restricted network.")
        return socket.socket(family=family, type=type_, proto=proto)

    return create


class GuardedResolver(AbstractResolver):
    def __init__(self, allow_private: bool) -> None:
        self._allow_private = allow_private

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET
    ) -> list[ResolveResult]:
        records = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=family, type=socket.SOCK_STREAM
        )
        result = []
        for af, _type, proto, _name, address in records:
            ip = str(address[0])
            if not self._allow_private and is_blocked_address(ipaddress.ip_address(ip)):
                raise ValidationFailed("The S3 endpoint resolves to a restricted network.")
            result.append(
                ResolveResult(
                    hostname=host,
                    host=ip,
                    port=port,
                    family=af,
                    proto=proto,
                    flags=socket.AI_NUMERICHOST,
                )
            )
        return result

    async def close(self) -> None:
        return None


def session_class(endpoint: str | None, allow_private: bool) -> type[AIOHTTPSession]:
    allowed = urlsplit(endpoint) if endpoint else None

    class Connector(TCPConnector):
        async def connect(self, req: Any, traces: Any, timeout: Any) -> Any:  # noqa: ASYNC109
            url = req.url
            if allowed is not None and (
                url.scheme != allowed.scheme
                or url.host != allowed.hostname
                or url.port != (allowed.port or (443 if allowed.scheme == "https" else 80))
            ):
                raise ValidationFailed("S3 redirected outside its configured endpoint.")
            return await super().connect(req, traces, timeout)

    class Session(AIOHTTPSession):  # type: ignore[misc]  # SDK transport is untyped
        async def _create_connector(self, proxy_url: Any) -> TCPConnector:
            if proxy_url:
                raise ValidationFailed("S3 proxy routing is disabled.")
            ssl_context, _proxy_ssl = await asyncio.to_thread(self._build_ssl_contexts, None)
            return Connector(
                limit=self._max_pool_connections,
                ssl=ssl_context,
                resolver=GuardedResolver(allow_private),
                socket_factory=guarded_socket(allow_private),
                use_dns_cache=False,
            )

    return Session
