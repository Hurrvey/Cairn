"""Document metadata that travels into the vector read model.

A document's metadata is copied into every one of its points (ADR-0005) so that
retrieval can filter on it without PostgreSQL. That makes its size a per-chunk
cost, so it is bounded where it enters the system and checked again where it is
written, for documents registered before the bound existed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from cairn.core.errors import ValidationFailed

__all__ = [
    "MAX_METADATA_BYTES",
    "MAX_METADATA_DEPTH",
    "MAX_METADATA_KEYS",
    "READ_MODEL_SCHEMA",
    "DocumentFacts",
    "DocumentMetadataTooLarge",
    "check_document_metadata",
    "document_type",
]

#: Version of the point payload layout. ``2`` adds ``meta``, ``created_at`` and
#: ``doc_*``; index versions captured before it record ``1`` and cannot answer
#: metadata or date filters until rebuilt.
READ_MODEL_SCHEMA = 2

MAX_METADATA_BYTES = 8 * 1024
MAX_METADATA_DEPTH = 4
MAX_METADATA_KEYS = 64


class DocumentMetadataTooLarge(ValidationFailed):
    code = "DOCUMENT_METADATA_TOO_LARGE"
    title = "Document metadata is too large."


@dataclass(frozen=True, slots=True)
class DocumentFacts:
    """What every point of one document revision carries about its document."""

    metadata: Mapping[str, Any]
    title: str | None
    type: str | None
    created_at: datetime
    revised_at: datetime

    def payload(self) -> dict[str, Any]:
        """Point payload fields; applied last so chunk metadata cannot override them."""
        return {
            "meta": check_document_metadata(self.metadata),
            "created_at": int(self.created_at.timestamp()),
            "doc_title": self.title,
            "doc_type": self.type,
            "doc_updated_at": int(self.revised_at.timestamp()),
        }


def document_type(filename: str | None, mime_type: str | None) -> str | None:
    """A short source type for citations: the file extension, else the MIME subtype."""
    if filename:
        suffix = PurePosixPath(filename.replace("\\", "/")).suffix.lower().lstrip(".")
        if suffix and len(suffix) <= 16 and suffix.isalnum():
            return suffix
    if mime_type and "/" in mime_type:
        subtype = mime_type.split("/", 1)[1].split(";", 1)[0].strip().lower()
        return subtype[:32] or None
    return None


def check_document_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe copy of ``metadata`` or raise if it exceeds the bounds."""
    if not isinstance(metadata, Mapping):
        raise DocumentMetadataTooLarge("Document metadata must be a JSON object.")
    _check_shape(metadata, depth=1)
    try:
        encoded = json.dumps(metadata, ensure_ascii=False, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise DocumentMetadataTooLarge("Document metadata must be plain JSON.") from exc
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise DocumentMetadataTooLarge(
            f"Document metadata exceeds {MAX_METADATA_BYTES} bytes of JSON; it is copied into "
            "every chunk of the document."
        )
    copied: dict[str, Any] = json.loads(encoded)
    return copied


def _check_shape(value: object, *, depth: int) -> None:
    pending = [(value, depth)]
    while pending:
        current, level = pending.pop()
        if isinstance(current, (Mapping, list)) and level > MAX_METADATA_DEPTH:
            raise DocumentMetadataTooLarge(
                f"Document metadata may nest at most {MAX_METADATA_DEPTH} containers deep."
            )
        if isinstance(current, Mapping):
            if len(current) > MAX_METADATA_KEYS:
                raise DocumentMetadataTooLarge(
                    f"A document metadata object may have at most {MAX_METADATA_KEYS} keys."
                )
            for key, item in current.items():
                if not isinstance(key, str) or not key or any(c in key for c in '.[]"'):
                    raise DocumentMetadataTooLarge(
                        'Document metadata keys must be non-empty and must not contain . [ ] or ".'
                    )
                pending.append((item, level + 1))
        elif isinstance(current, list):
            pending.extend((item, level + 1) for item in current)
        elif current is not None and not isinstance(current, (str, int, float, bool)):
            raise DocumentMetadataTooLarge("Document metadata must be plain JSON.")
