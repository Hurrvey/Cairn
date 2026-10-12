from __future__ import annotations

import httpx
import pytest

from apps.bge_m3.server import SidecarSettings, create_app


class FakeEncoder:
    device = "cpu"

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], bool, bool]] = []

    def encode(
        self, texts: list[str], *, dense: bool, sparse: bool
    ) -> tuple[list[list[float]] | None, list[dict[int, float]] | None]:
        self.calls.append((texts, dense, sparse))
        return (
            [[float(len(text)), 0.5] for text in texts] if dense else None,
            [{9: 0.25, 3: 0.75, 4: 0.0} for _ in texts] if sparse else None,
        )


def client(settings: SidecarSettings | None = None, encoder: FakeEncoder | None = None):  # type: ignore[no-untyped-def]
    app = create_app(settings or SidecarSettings(), encoder=encoder or FakeEncoder())
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sidecar")


@pytest.fixture
async def running():  # type: ignore[no-untyped-def]
    encoder = FakeEncoder()
    app = create_app(SidecarSettings(max_inputs=2, max_chars=10), encoder=encoder)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://sidecar"
        ) as http,
    ):
        yield http, encoder


async def test_encode_returns_dense_and_sorted_positive_terms(running) -> None:  # type: ignore[no-untyped-def]
    http, encoder = running
    response = await http.post("/v1/encode", json={"inputs": ["abc", "de"]})
    assert response.status_code == 200
    body = response.json()
    assert body["dense"] == [[3.0, 0.5], [2.0, 0.5]]
    assert body["sparse"][0] == [{"index": 3, "value": 0.75}, {"index": 9, "value": 0.25}]
    assert encoder.calls == [(["abc", "de"], True, True)]

    only_sparse = await http.post("/v1/encode", json={"inputs": ["x"], "dense": False})
    assert only_sparse.json()["dense"] is None
    assert (await http.get("/health")).json() == {
        "status": "ok",
        "model": "BAAI/bge-m3",
        "device": "cpu",
    }


@pytest.mark.parametrize(
    ("payload", "status"),
    [
        ({"inputs": ["a", "b", "c"]}, 413),
        ({"inputs": ["x" * 11]}, 413),
        ({"inputs": ["  "]}, 422),
        ({"inputs": []}, 422),
        ({"inputs": ["x"], "dense": False, "sparse": False}, 422),
    ],
)
async def test_requests_are_bounded(running, payload: dict[str, object], status: int) -> None:  # type: ignore[no-untyped-def]
    http, encoder = running
    assert (await http.post("/v1/encode", json=payload)).status_code == status
    assert encoder.calls == []


async def test_tei_compatible_endpoints(running) -> None:  # type: ignore[no-untyped-def]
    http, _encoder = running
    assert (await http.post("/embed", json={"inputs": "abc"})).json() == [[3.0, 0.5]]
    sparse = (await http.post("/embed_sparse", json={"inputs": ["ab"]})).json()
    assert sparse == [[{"index": 3, "value": 0.75}, {"index": 9, "value": 0.25}]]


async def test_api_key_is_required_when_configured() -> None:
    app = create_app(SidecarSettings(api_key="sidecar-key"), encoder=FakeEncoder())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://s") as http,
    ):
        assert (await http.post("/v1/encode", json={"inputs": ["x"]})).status_code == 401
        wrong = {"Authorization": "Bearer nope"}
        assert (
            await http.post("/v1/encode", json={"inputs": ["x"]}, headers=wrong)
        ).status_code == 401
        right = {"Authorization": "Bearer sidecar-key"}
        assert (
            await http.post("/v1/encode", json={"inputs": ["x"]}, headers=right)
        ).status_code == 200
        # Health stays open for container orchestration.
        assert (await http.get("/health")).status_code == 200


async def test_health_reports_loading_until_the_model_is_ready() -> None:
    app = create_app(SidecarSettings(), encoder=FakeEncoder())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://sidecar"
    ) as http:
        assert (await http.get("/health")).status_code == 503
        assert (await http.post("/v1/encode", json={"inputs": ["x"]})).status_code == 503
