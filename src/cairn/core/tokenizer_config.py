"""Configured immutable tokenizer asset identities shared by process roles."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping

from cairn.core.config import RetrievalSettings

__all__ = ["configured_tokenizer_ids"]


def configured_tokenizer_ids(retrieval: RetrievalSettings) -> tuple[str, ...]:
    retrieval_ids = set(retrieval.tokenizer_files)
    ingestion_ids: set[str] = set()
    raw = os.environ.get("CAIRN_INGESTION__TOKENIZERS_JSON", "{}")
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        parsed = {}
    if isinstance(parsed, Mapping):
        ingestion_ids = {
            name
            for name, binding in parsed.items()
            if isinstance(name, str) and name and isinstance(binding, str) and binding
        }
    if retrieval_ids and ingestion_ids:
        return tuple(sorted(retrieval_ids & ingestion_ids))
    return tuple(sorted(retrieval_ids | ingestion_ids))
