from __future__ import annotations

import json
import math
from uuid import UUID

import pytest
from pydantic import ValidationError

from cairn.core.config import Settings
from cairn.retrieval.dto import RetrievalRequest

TARGET = {"knowledge_base_id": "kb_00000000000000000000000000"}


@pytest.mark.parametrize("weight", [True, math.nan, math.inf, -0.1, 10.1])
def test_target_weight_rejects_bool_nonfinite_and_out_of_range(weight: object) -> None:
    with pytest.raises(ValidationError):
        RetrievalRequest(targets=[{**TARGET, "weight": weight}], query="saturn")


@pytest.mark.parametrize("vector", [[0.0, math.nan], [0.0, math.inf], [0.0] * 65_537])
def test_query_vector_is_finite_and_request_bounded(vector: list[float]) -> None:
    with pytest.raises(ValidationError):
        RetrievalRequest(targets=[TARGET], query_vector=vector)


def test_request_requires_text_or_vector_and_fulltext_requires_text() -> None:
    with pytest.raises(ValidationError):
        RetrievalRequest(targets=[TARGET])
    with pytest.raises(ValidationError):
        RetrievalRequest(targets=[TARGET], query_vector=[1.0, 0.0], search_mode="fulltext")
    with pytest.raises(ValidationError):
        RetrievalRequest(targets=[TARGET], query_vector=[1.0, 0.0], search_mode="hybrid")


def test_candidate_pool_cannot_be_smaller_than_top_k() -> None:
    with pytest.raises(ValidationError):
        RetrievalRequest(targets=[TARGET], query="saturn", top_k=10, candidate_k=5)


def test_settings_parse_public_retrieval_runtime_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    provider_id = "20000000-0000-0000-0000-000000000002"
    monkeypatch.setenv("CAIRN_DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")
    monkeypatch.setenv("CAIRN_REDIS_URL", "redis://localhost/0")
    monkeypatch.setenv("CAIRN_MASTER_KEY", "test-key")
    monkeypatch.setenv(
        "CAIRN_RETRIEVAL__EMBEDDING_ENDPOINTS",
        json.dumps(
            {
                provider_id: {
                    "dialect": "tei",
                    "base_url": "http://127.0.0.1:38742",
                    "allow_private": True,
                    "namespace": "retrieval-20260915",
                    "max_batch_size": 16,
                }
            }
        ),
    )
    monkeypatch.setenv(
        "CAIRN_RETRIEVAL__TOKENIZER_FILES",
        json.dumps({"live-minilm": "D:/models/tokenizer.json"}),
    )

    settings = Settings()

    endpoint = settings.retrieval.embedding_endpoints[UUID(provider_id)]
    assert endpoint.dialect == "tei"
    assert endpoint.max_batch_size == 16
    assert settings.retrieval.tokenizer_files["live-minilm"] == "D:/models/tokenizer.json"
