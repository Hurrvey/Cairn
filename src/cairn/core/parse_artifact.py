"""Shared checksummed artifact codec; no owner or parser dependencies."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any
from uuid import UUID

from cairn.core.document_ir import DocumentIR


@dataclass(frozen=True, slots=True)
class ParseArtifactIdentity:
    workspace_id: UUID
    kb_id: UUID
    document_id: UUID
    revision: int
    source_sha256: str
    index_version: int
    parse_version: str | None


def verified_parse_payload(raw: bytes, identity: ParseArtifactIdentity) -> dict[str, Any]:
    payload = decode_payload(raw)
    if (
        payload.get("type"),
        payload.get("document_id"),
        payload.get("revision"),
        payload.get("source_content_hash"),
        payload.get("index_version"),
    ) != (
        "parse",
        str(identity.document_id),
        identity.revision,
        identity.source_sha256,
        identity.index_version,
    ):
        raise ValueError("parse envelope does not match SQL locator")
    if payload.get("ir") is not None:
        ir = DocumentIR.model_validate(payload["ir"])
        if (
            ir.workspace_id,
            ir.kb_id,
            ir.document_id,
            ir.revision,
            ir.source_sha256,
            ir.parse_version,
        ) != (
            identity.workspace_id,
            identity.kb_id,
            identity.document_id,
            identity.revision,
            identity.source_sha256,
            identity.parse_version,
        ):
            raise ValueError("IR does not match SQL locator")
        if ir.parser_fingerprint != payload.get(
            "parser_fingerprint"
        ) or ir.context_fingerprint != payload.get("context_fingerprint"):
            raise ValueError("producer identity mismatch")
    elif identity.parse_version is not None:
        raise ValueError("legacy artifact cannot resolve a new parse version")
    return payload


def encode_payload(payload: dict[str, Any], *, version: int = 1) -> bytes:
    body = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False
    ).encode()
    return json.dumps(
        {
            "checksum": sha256(body).hexdigest(),
            "payload": base64.b64encode(body).decode("ascii"),
            "version": version,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def decode_payload(raw: bytes) -> dict[str, Any]:
    try:
        envelope = json.loads(raw)
        if envelope.get("version") not in (1, 2):
            raise ValueError("unsupported artifact version")
        body = base64.b64decode(envelope["payload"], validate=True)
        if sha256(body).hexdigest() != envelope["checksum"]:
            raise ValueError("artifact checksum mismatch")
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("invalid artifact payload")
        return payload
    except (KeyError, TypeError, AttributeError, json.JSONDecodeError, binascii.Error) as exc:
        raise ValueError("invalid ingestion artifact") from exc
