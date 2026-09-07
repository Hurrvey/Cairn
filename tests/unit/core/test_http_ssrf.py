"""SSRF protection — TC-M00-05..08.

These are the tests that matter most in this module. An attacker who can supply
a URL (crawl seed, webhook target, pipeline HTTP node) will point it at cloud
instance metadata; the failure is total credential compromise, and it is easy to
regress because the happy path looks identical either way.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterator

import httpx
import pytest

import cairn.core.http as core_http
from cairn.core.config import HttpSettings
from cairn.core.errors import BlockedAddress, UpstreamUnavailable, ValidationFailed
from cairn.core.http import is_blocked_address, read_capped, safe_client


class FakeNetwork:
    """A resolver and origin server under the test's control."""

    def __init__(self) -> None:
        self.dns: dict[str, list[str]] = {}
        self.routes: dict[str, httpx.Response] = {}
        self.connected_ips: list[str] = []
        self.host_headers: list[str] = []
        self.resolve_calls: list[str] = []

    def resolves(self, host: str, *addresses: str) -> None:
        self.dns[host] = list(addresses)

    def serves(self, host: str, path: str, response: httpx.Response) -> None:
        self.routes[f"{host}{path}"] = response


@pytest.fixture
def network(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeNetwork]:
    net = FakeNetwork()

    async def fake_resolve(
        host: str, port: int
    ) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        net.resolve_calls.append(host)
        try:
            return [ipaddress.ip_address(host.strip("[]"))]
        except ValueError:
            pass
        if host not in net.dns:
            raise UpstreamUnavailable(f"Could not resolve host: {host}")
        return [ipaddress.ip_address(a) for a in net.dns[host]]

    async def fake_send(self: object, request: httpx.Request) -> httpx.Response:
        # By this point SafeTransport has already validated and pinned, so the
        # URL host is an IP and the Host header carries the real name.
        net.connected_ips.append(request.url.host)
        host_header = request.headers["Host"]
        net.host_headers.append(host_header)
        key = f"{host_header.split(':')[0]}{request.url.path}"
        response = net.routes.get(key)
        if response is None:
            return httpx.Response(404, request=request)
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            content=response.content,
            request=request,
        )

    monkeypatch.setattr(core_http, "_resolve", fake_resolve)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake_send)
    yield net


SETTINGS = HttpSettings(max_redirects=5)


# --- address classification --------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "169.254.169.254",  # AWS/GCP/Azure instance metadata — the crown jewel
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "0.0.0.0",  # noqa: S104 — an address under test, not a bind target
        "100.64.0.1",
        "::1",
        "fc00::1",
        "fe80::1",
        "::ffff:127.0.0.1",  # IPv4-mapped IPv6 — a classic bypass
    ],
)
def test_is_blocked_address__rejects_internal_ranges(address: str) -> None:
    assert is_blocked_address(ipaddress.ip_address(address)) is True


@pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700::1"])
def test_is_blocked_address__permits_public(address: str) -> None:
    assert is_blocked_address(ipaddress.ip_address(address)) is False


# --- TC-M00-05: direct metadata access ---------------------------------------


async def test_metadata_endpoint_is_blocked(network: FakeNetwork) -> None:
    """TC-M00-05: the single most valuable SSRF target must be unreachable."""
    async with safe_client(settings=SETTINGS) as client:
        with pytest.raises(BlockedAddress):
            await client.get("http://169.254.169.254/latest/meta-data/iam/security-credentials/")
    assert network.connected_ips == []


async def test_hostname_resolving_to_private_is_blocked(network: FakeNetwork) -> None:
    """A public-looking name that resolves inward is the common real-world shape."""
    network.resolves("internal.attacker.example", "10.0.0.5")
    async with safe_client(settings=SETTINGS) as client:
        with pytest.raises(BlockedAddress):
            await client.get("http://internal.attacker.example/")
    assert network.connected_ips == []


async def test_any_blocked_address_in_the_record_set_rejects(network: FakeNetwork) -> None:
    """Rejecting only if *all* records are private would leave a trivial bypass."""
    network.resolves("mixed.example", "93.184.216.34", "127.0.0.1")
    async with safe_client(settings=SETTINGS) as client:
        with pytest.raises(BlockedAddress):
            await client.get("http://mixed.example/")


# --- TC-M00-06: redirect hops ------------------------------------------------


async def test_redirect_to_private_address_is_blocked(network: FakeNetwork) -> None:
    """TC-M00-06: validation must run on every hop, not only the first.

    A pre-flight check on the submitted URL alone is defeated by an origin that
    simply answers 302 to http://127.0.0.1/.
    """
    network.resolves("public.example", "93.184.216.34")
    network.resolves("localhost", "127.0.0.1")
    network.serves(
        "public.example",
        "/redirect",
        httpx.Response(302, headers={"Location": "http://localhost/admin"}),
    )

    async with safe_client(settings=SETTINGS) as client:
        with pytest.raises(BlockedAddress):
            await client.get("http://public.example/redirect")

    # The first hop connected; the second was refused before any connection.
    assert network.connected_ips == ["93.184.216.34"]


async def test_relative_redirect_resolves_against_the_real_hostname(
    network: FakeNetwork,
) -> None:
    """Pinning rewrites the URL to an IP; if we did not restore it, a relative
    Location would resolve against the IP and lose the Host entirely."""
    network.resolves("public.example", "93.184.216.34")
    network.serves("public.example", "/start", httpx.Response(302, headers={"Location": "/next"}))
    network.serves("public.example", "/next", httpx.Response(200, content=b"arrived"))

    async with safe_client(settings=SETTINGS) as client:
        response = await client.get("http://public.example/start")

    assert response.status_code == 200
    assert response.content == b"arrived"
    assert network.host_headers == ["public.example", "public.example"]


# --- TC-M00-07: DNS rebinding ------------------------------------------------


async def test_dns_rebinding_is_defeated_by_pinning(
    network: FakeNetwork, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-M00-07: resolve once, validate, then connect to *that address*.

    The attack: the attacker's resolver returns a public address for the check
    and a private one microseconds later for the connection. A pre-flight check
    followed by a hostname connect loses this race every time.

    The defence is structural, not a race we try to win — there is no second
    lookup to poison.
    """
    lookups = {"count": 0}

    async def rebinding_resolve(
        host: str, port: int
    ) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        lookups["count"] += 1
        # First lookup: public. Every later lookup: loopback.
        address = "93.184.216.34" if lookups["count"] == 1 else "127.0.0.1"
        return [ipaddress.ip_address(address)]

    monkeypatch.setattr(core_http, "_resolve", rebinding_resolve)
    network.serves("rebind.example", "/", httpx.Response(200, content=b"ok"))

    async with safe_client(settings=SETTINGS) as client:
        response = await client.get("http://rebind.example/")

    assert response.status_code == 200
    # Exactly one resolution, and the connection went to the address we validated.
    assert lookups["count"] == 1
    assert network.connected_ips == ["93.184.216.34"]


async def test_sni_and_host_survive_pinning(network: FakeNetwork) -> None:
    """Pinning must not break virtual hosting or certificate verification."""
    network.resolves("api.example.com", "93.184.216.34")
    network.serves("api.example.com", "/v1/ping", httpx.Response(200, content=b"pong"))

    async with safe_client(settings=SETTINGS) as client:
        response = await client.get("http://api.example.com/v1/ping")

    assert response.content == b"pong"
    assert network.connected_ips == ["93.184.216.34"]
    assert network.host_headers == ["api.example.com"]


# --- scheme and size ---------------------------------------------------------


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://evil/", "ftp://host/x"])
async def test_non_http_schemes_are_rejected(network: FakeNetwork, url: str) -> None:
    async with safe_client(settings=SETTINGS) as client:
        with pytest.raises((ValidationFailed, httpx.UnsupportedProtocol)):
            await client.get(url)


async def test_read_capped__aborts_an_oversized_body() -> None:
    """TC-M00-08: a hostile origin must not be able to exhaust worker memory."""
    payload = b"x" * 5000
    response = httpx.Response(200, content=payload)
    with pytest.raises(ValidationFailed, match="maximum permitted size"):
        await read_capped(response, max_bytes=1024)


async def test_read_capped__returns_a_body_within_the_cap() -> None:
    response = httpx.Response(200, content=b"small")
    assert await read_capped(response, max_bytes=1024) == b"small"


async def test_allow_private_addresses__is_honoured_for_local_development(
    network: FakeNetwork,
) -> None:
    """The escape hatch exists so a developer can crawl localhost; it must never
    be the default, and the setting name should make that obvious in review."""
    network.resolves("localhost", "127.0.0.1")
    network.serves("localhost", "/", httpx.Response(200, content=b"dev"))

    permissive = HttpSettings(allow_private_addresses=True)
    async with safe_client(settings=permissive) as client:
        response = await client.get("http://localhost/")
    assert response.content == b"dev"
