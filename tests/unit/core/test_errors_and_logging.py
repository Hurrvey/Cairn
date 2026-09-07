"""Unit tests for errors and log redaction — TC-M00-03, TC-M00-04."""

from __future__ import annotations

import json

import pytest

from cairn.core.errors import (
    CairnError,
    FieldError,
    NotFound,
    RateLimitExceeded,
    ValidationFailed,
)
from cairn.core.logging import REDACTED, redaction_processor


def test_problem_document__matches_rfc9457() -> None:
    """TC-M00-03: the wire format is a contract, including the stable `code`."""
    error = NotFound("No such knowledge base.")
    problem = error.to_problem(instance="/v1/knowledge-bases/kb_x", request_id="req_abc")

    assert problem == {
        "type": "https://docs.cairn.io/errors/resource-not-found",
        "title": "Resource not found",
        "status": 404,
        "code": "RESOURCE_NOT_FOUND",
        "detail": "No such knowledge base.",
        "instance": "/v1/knowledge-bases/kb_x",
        "request_id": "req_abc",
    }
    json.dumps(problem)  # must be serialisable as-is


def test_problem_document__includes_field_errors_when_present() -> None:
    error = ValidationFailed(
        "The request contains invalid parameters.",
        errors=[FieldError("top_k", "OUT_OF_RANGE", "must be between 1 and 100", 500)],
    )
    problem = error.to_problem(instance="/v1/retrieval/query", request_id="req_abc")
    assert problem["errors"] == [
        {
            "field": "top_k",
            "code": "OUT_OF_RANGE",
            "detail": "must be between 1 and 100",
            "value": 500,
        }
    ]


def test_problem_document__omits_errors_when_empty() -> None:
    assert "errors" not in NotFound().to_problem(instance="/x", request_id="r")


def test_rate_limit__carries_retry_after() -> None:
    error = RateLimitExceeded("Slow down.", retry_after=23)
    assert error.retry_after == 23
    assert error.http_status == 429


@pytest.mark.parametrize(
    ("exc", "code", "status"),
    [
        (ValidationFailed(), "VALIDATION_FAILED", 400),
        (NotFound(), "RESOURCE_NOT_FOUND", 404),
    ],
)
def test_codes_and_statuses_are_stable(exc: CairnError, code: str, status: int) -> None:
    """These values are the published contract; changing one is a breaking change."""
    assert exc.code == code
    assert exc.http_status == status


# --- redaction ---------------------------------------------------------------


def test_redaction__replaces_top_level_sensitive_keys() -> None:
    """TC-M00-04."""
    event = {"event": "auth.login", "username": "admin", "password": "hunter2"}
    result = redaction_processor(None, "info", event)  # type: ignore[arg-type]
    assert result["password"] == REDACTED
    assert result["username"] == "admin"


def test_redaction__reaches_nested_structures() -> None:
    event = {
        "event": "provider.configured",
        "config": {"base_url": "https://api.example.com", "api_key": "sk-live-123"},
        "items": [{"token": "abc"}, {"name": "ok"}],
    }
    result = redaction_processor(None, "info", event)  # type: ignore[arg-type]
    assert result["config"]["api_key"] == REDACTED
    assert result["config"]["base_url"] == "https://api.example.com"
    assert result["items"][0]["token"] == REDACTED
    assert result["items"][1]["name"] == "ok"


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "Password",
        "password_hash",
        "api_key",
        "X-Api-Key",
        "authorization",
        "session_token",
        "master_key",
        "provider_credential",
        "private_key",
    ],
)
def test_redaction__catches_every_marker_variant(key: str) -> None:
    result = redaction_processor(None, "info", {key: "sensitive"})  # type: ignore[arg-type]
    assert result[key] == REDACTED
