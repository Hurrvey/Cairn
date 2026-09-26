"""bge-m3 embedding sidecar: dense vectors and lexical (sparse) weights in one pass.

    uvicorn --factory bge_m3.server:create_app --host 0.0.0.0 --port 8000

Runs FlagEmbedding's ``BGEM3FlagModel`` against weights mounted read-only at
``BGE_M3_MODEL_PATH``. Cairn's ``bge_m3`` provider family calls ``/v1/encode``;
``/embed`` and ``/embed_sparse`` mirror TEI so other tools can use it too.

One inference runs at a time: the model batches internally, and parallel
forward passes on one CPU or GPU only thrash each other's memory. Requests are
bounded in count and size so a single caller cannot hold the model for long.

Configuration (environment):

* ``BGE_M3_MODEL_PATH`` — weights directory (``/model``)
* ``BGE_M3_DEVICE`` — ``cpu``, ``cuda`` or ``auto`` (default)
* ``BGE_M3_FP16`` — ``auto`` (on for CUDA), ``true`` or ``false``
* ``BGE_M3_MAX_LENGTH`` — tokens per input, at most 8192 (default 8192)
* ``BGE_M3_BATCH_SIZE`` — forward-pass batch size (default 16)
* ``BGE_M3_MAX_INPUTS`` / ``BGE_M3_MAX_CHARS`` — request bounds (64 / 64000)
* ``BGE_M3_API_KEY`` — when set, requests need ``Authorization: Bearer <key>``
"""

from __future__ import annotations

import asyncio
import hmac
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from fastapi import Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

__all__ = ["Encoder", "SidecarSettings", "create_app"]

MODEL_NAME = "BAAI/bge-m3"


class Encoder(Protocol):
    """What the server needs from a model; tests supply a fake."""

    device: str

    def encode(
        self, texts: list[str], *, dense: bool, sparse: bool
    ) -> tuple[list[list[float]] | None, list[dict[int, float]] | None]: ...


@dataclass(frozen=True, slots=True)
class SidecarSettings:
    model_path: str = "/model"
    device: str = "auto"
    fp16: str = "auto"
    max_length: int = 8192
    batch_size: int = 16
    max_inputs: int = 64
    max_chars: int = 64_000
    api_key: str | None = None

    @classmethod
    def from_env(cls) -> SidecarSettings:
        return cls(
            model_path=os.environ.get("BGE_M3_MODEL_PATH", "/model"),
            device=os.environ.get("BGE_M3_DEVICE", "auto"),
            fp16=os.environ.get("BGE_M3_FP16", "auto"),
            max_length=min(int(os.environ.get("BGE_M3_MAX_LENGTH", "8192")), 8192),
            batch_size=int(os.environ.get("BGE_M3_BATCH_SIZE", "16")),
            max_inputs=int(os.environ.get("BGE_M3_MAX_INPUTS", "64")),
            max_chars=int(os.environ.get("BGE_M3_MAX_CHARS", "64000")),
            api_key=os.environ.get("BGE_M3_API_KEY") or None,
        )


class FlagEncoder:
    """``BGEM3FlagModel`` behind the :class:`Encoder` protocol. Imports torch lazily."""

    def __init__(self, settings: SidecarSettings) -> None:
        import torch
        from FlagEmbedding import BGEM3FlagModel

        device = settings.device
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        fp16 = device.startswith("cuda") if settings.fp16 == "auto" else settings.fp16 == "true"
        self.device = device
        self._settings = settings
        self._model = BGEM3FlagModel(
            settings.model_path, normalize_embeddings=True, use_fp16=fp16, devices=[device]
        )

    def encode(
        self, texts: list[str], *, dense: bool, sparse: bool
    ) -> tuple[list[list[float]] | None, list[dict[int, float]] | None]:
        output: dict[str, Any] = self._model.encode(
            texts,
            batch_size=self._settings.batch_size,
            max_length=self._settings.max_length,
            return_dense=dense,
            return_sparse=sparse,
            return_colbert_vecs=False,
        )
        dense_rows = None
        if dense:
            vectors = output["dense_vecs"]
            dense_rows = [
                [float(value) for value in row] for row in vectors.reshape(len(texts), -1)
            ]
        sparse_rows = None
        if sparse:
            weights = output["lexical_weights"]
            if isinstance(weights, dict):  # one input may come back unwrapped
                weights = [weights]
            sparse_rows = [
                {int(token): float(weight) for token, weight in row.items()} for row in weights
            ]
        return dense_rows, sparse_rows


class EncodeRequest(BaseModel):
    inputs: list[str] = Field(min_length=1)
    dense: bool = True
    sparse: bool = True


class TeiRequest(BaseModel):
    inputs: str | list[str]
    normalize: bool = True
    truncate: bool = False

    @field_validator("inputs")
    @classmethod
    def _as_list(cls, value: str | list[str]) -> list[str]:
        return [value] if isinstance(value, str) else value


def _terms(row: dict[int, float]) -> list[dict[str, float | int]]:
    return [
        {"index": index, "value": weight} for index, weight in sorted(row.items()) if weight > 0
    ]


def create_app(
    settings: SidecarSettings | None = None, *, encoder: Encoder | None = None
) -> FastAPI:
    config = settings or SidecarSettings.from_env()
    state: dict[str, Encoder] = {}
    lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Loading takes tens of seconds on CPU; do it off the event loop so
        # /health answers "loading" meanwhile instead of timing out.
        state["encoder"] = (
            encoder if encoder is not None else await asyncio.to_thread(FlagEncoder, config)
        )
        yield

    app = FastAPI(title="bge-m3 sidecar", version="1.0.0", lifespan=lifespan, docs_url=None)

    def authorize(request: Request) -> None:
        if config.api_key is None:
            return
        header = request.headers.get("Authorization", "")
        expected = f"Bearer {config.api_key}"
        if not hmac.compare_digest(header.encode(), expected.encode()):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing API key")

    def bounded(texts: list[str]) -> list[str]:
        if not 0 < len(texts) <= config.max_inputs:
            raise HTTPException(
                413,
                f"send between 1 and {config.max_inputs} inputs",
            )
        if any(not text.strip() for text in texts):
            raise HTTPException(422, "inputs must not be empty")
        if any(len(text) > config.max_chars for text in texts):
            raise HTTPException(
                413,
                f"each input is limited to {config.max_chars} characters",
            )
        return texts

    async def run(
        texts: list[str], *, dense: bool, sparse: bool
    ) -> tuple[list[list[float]] | None, list[dict[int, float]] | None]:
        model = state.get("encoder")
        if model is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "model is loading")
        async with lock:
            return await asyncio.to_thread(model.encode, texts, dense=dense, sparse=sparse)

    @app.get("/health")
    async def health() -> dict[str, object]:
        model = state.get("encoder")
        if model is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "model is loading")
        return {"status": "ok", "model": MODEL_NAME, "device": model.device}

    @app.post("/v1/encode", dependencies=[Depends(authorize)])
    async def encode(body: EncodeRequest) -> dict[str, object]:
        if not body.dense and not body.sparse:
            raise HTTPException(422, "request dense or sparse")
        dense, sparse = await run(bounded(body.inputs), dense=body.dense, sparse=body.sparse)
        return {
            "model": MODEL_NAME,
            "dense": dense,
            "sparse": [_terms(row) for row in sparse] if sparse is not None else None,
        }

    @app.post("/embed", dependencies=[Depends(authorize)])
    async def embed(body: TeiRequest) -> list[list[float]]:
        dense, _sparse = await run(bounded(list(body.inputs)), dense=True, sparse=False)
        assert dense is not None
        return dense

    @app.post("/embed_sparse", dependencies=[Depends(authorize)])
    async def embed_sparse(body: TeiRequest) -> list[list[dict[str, float | int]]]:
        _dense, sparse = await run(bounded(list(body.inputs)), dense=False, sparse=True)
        assert sparse is not None
        return [_terms(row) for row in sparse]

    return app
