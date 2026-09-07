"""Outbound HTTP with SSRF protection.

Every outbound request in Cairn goes through here. Crawl seeds, webhook targets,
and pipeline HTTP node URLs are attacker-controlled *by definition*: a seed of
``http://169.254.169.254/latest/meta-data/iam/security-credentials/`` reads the
deployment's cloud credentials.

**Pre-flight IP checks alone are not sufficient.** DNS rebinding defeats them:
the attacker's resolver returns a public address for the check, then a private
one microseconds later for the actual connection. The defence is to resolve
once, validate what we resolved, and then *connect to that exact address* —
never to the hostname again.

Design (NFR-SEC-09):

1. Resolution and validation happen inside the transport, so they run for
   **every hop** — httpx invokes the transport once per redirect.
2. The connection target is rewritten to the validated IP, with ``Host`` and
   TLS SNI preserved so virtual hosting and certificate validation still work.
3. The original URL is restored before returning, so httpx resolves relative
   ``Location`` headers against the real hostname rather than the pinned IP.
4. Response size and total time are capped.
5. Only ``http`` and ``https`` are permitted.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Final

import httpx

from cairn.core.config import HttpSettings, get_settings
from cairn.core.errors import BlockedAddress, UpstreamUnavailable, ValidationFailed
from cairn.core.logging import get_logger

__all__ = ["BLOCKED_NETWORKS", "SafeTransport", "is_blocked_address", "safe_client"]

log = get_logger(__name__)

#: Address ranges no user-supplied URL may reach. 169.254.0.0/16 is the one that
#: matters most in practice: it is the cloud instance-metadata endpoint on AWS,
#: GCP, Azure, and most others.
BLOCKED_NETWORKS: Final[tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]] = (
    ipaddress.ip_network("0.0.0.0/8"),  # "this network"
    ipaddress.ip_network("10.0.0.0/8"),  # RFC 1918
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT
    ipaddress.ip_network("127.0.0.0/8"),  # loopback
    ipaddress.ip_network("169.254.0.0/16"),  # link-local + cloud metadata
    ipaddress.ip_network("172.16.0.0/12"),  # RFC 1918
    ipaddress.ip_network("192.0.0.0/24"),  # IETF protocol assignments
    ipaddress.ip_network("192.168.0.0/16"),  # RFC 1918
    ipaddress.ip_network("198.18.0.0/15"),  # benchmarking
    ipaddress.ip_network("224.0.0.0/4"),  # multicast
    ipaddress.ip_network("240.0.0.0/4"),  # reserved
    ipaddress.ip_network("::1/128"),  # loopback
    ipaddress.ip_network("fc00::/7"),  # unique local
    ipaddress.ip_network("fe80::/10"),  # link-local
    ipaddress.ip_network("ff00::/8"),  # multicast
)

_ALLOWED_SCHEMES: Final = frozenset({"http", "https"})


def is_blocked_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if this resolved address must not be connected to."""
    # IPv4-mapped IPv6 (::ffff:127.0.0.1) would otherwise slip past the v4 rules.
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address in network for network in BLOCKED_NETWORKS)


async def _resolve(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve a hostname ourselves, so we control what we validate and connect to."""
    # A literal address needs no resolution — and must still be validated.
    try:
        return [ipaddress.ip_address(host.strip("[]"))]
    except ValueError:
        pass

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UpstreamUnavailable(f"Could not resolve host: {host}") from exc

    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        sockaddr = info[4]
        try:
            addresses.append(ipaddress.ip_address(sockaddr[0]))
        except ValueError:  # pragma: no cover — getaddrinfo always yields valid addresses
            continue
    if not addresses:
        raise UpstreamUnavailable(f"Could not resolve host: {host}")
    return addresses


class SafeTransport(httpx.AsyncHTTPTransport):
    """Validates and pins every connection, including each redirect hop."""

    def __init__(self, *, allow_private: bool = False, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]  # reason: httpx kwargs are untyped
        self._allow_private = allow_private

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url

        if url.scheme not in _ALLOWED_SCHEMES:
            raise ValidationFailed(f"Only http and https URLs are permitted; got {url.scheme!r}.")

        host = url.host
        port = url.port or (443 if url.scheme == "https" else 80)
        addresses = await _resolve(host, port)

        if not self._allow_private:
            for address in addresses:
                if is_blocked_address(address):
                    log.warning(
                        "http.blocked_address",
                        host=host,
                        resolved=str(address),
                        scheme=url.scheme,
                    )
                    raise BlockedAddress(
                        f"The address for {host} resolves to a network that is not "
                        f"permitted for outbound requests."
                    )

        pinned = addresses[0]

        # Preserve the Host header and TLS SNI: without these, virtual hosts break
        # and certificate verification fails against the IP.
        request.headers["Host"] = url.netloc.decode("ascii")
        request.extensions = {**request.extensions, "sni_hostname": host}

        original_url = request.url
        request.url = url.copy_with(host=str(pinned))
        try:
            return await super().handle_async_request(request)
        finally:
            # httpx joins a relative Location against `response.request.url`.
            # Restoring means redirects resolve against the real hostname rather
            # than the pinned IP.
            request.url = original_url


@asynccontextmanager
async def safe_client(
    *,
    settings: HttpSettings | None = None,
    headers: dict[str, str] | None = None,
    timeout: float | None = None,  # noqa: ASYNC109 — forwarded to httpx, not a wait_for
) -> AsyncIterator[httpx.AsyncClient]:
    """An httpx client that cannot be pointed at internal infrastructure.

    Usage::

        async with safe_client() as client:
            response = await client.get(user_supplied_url)
    """
    cfg = settings or get_settings().http
    transport = SafeTransport(allow_private=cfg.allow_private_addresses, retries=0)
    merged = {"User-Agent": cfg.user_agent, **(headers or {})}

    async with httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(timeout or cfg.timeout_s, connect=cfg.connect_timeout_s),
        follow_redirects=True,
        max_redirects=cfg.max_redirects,
        headers=merged,
    ) as client:
        yield client


async def read_capped(response: httpx.Response, max_bytes: int) -> bytes:
    """Read a streamed response, aborting once the cap is exceeded.

    Prevents a hostile or misconfigured endpoint from exhausting worker memory
    by advertising a small body and then streaming gigabytes.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            await response.aclose()
            raise ValidationFailed(
                f"Response exceeded the maximum permitted size of {max_bytes} bytes."
            )
        chunks.append(chunk)
    return b"".join(chunks)


def internal_client(base_url: str, *, timeout: float = 10.0) -> httpx.AsyncClient:
    """A plain client for *known internal services* only (embed server, sandbox).

    Never pass a user-supplied URL to this. If the URL came from a request body,
    a database row, or a crawl frontier, use :func:`safe_client`.
    """
    return httpx.AsyncClient(base_url=base_url, timeout=timeout)


def validate_addresses(
    addresses: Sequence[ipaddress.IPv4Address | ipaddress.IPv6Address],
) -> None:
    """Standalone validation helper, for callers doing their own resolution."""
    for address in addresses:
        if is_blocked_address(address):
            raise BlockedAddress(f"Address {address} is not permitted.")
