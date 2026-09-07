"""Catalog errors.

The messages here are read by a knowledge engineer mid-task, so each one says
what to do next rather than only what went wrong.
"""

from __future__ import annotations

from cairn.core.errors import Conflict, ValidationFailed

__all__ = [
    "BindingInUse",
    "ChunkNotEditable",
    "EmbeddingModelImmutable",
    "KbIndexInProgress",
    "KbNotReady",
    "UnsupportedFileType",
]


class EmbeddingModelImmutable(Conflict):
    """ADR-0006. Changing the embedding space of an indexed KB silently ruins
    retrieval, so it is refused — and the message points at the reindex path
    rather than leaving the user stuck."""

    code = "EMBEDDING_MODEL_IMMUTABLE"
    title = "Embedding configuration is immutable for an indexed knowledge base"


class KbIndexInProgress(Conflict):
    code = "KB_INDEX_IN_PROGRESS"
    title = "An index build is already running for this knowledge base"


class KbNotReady(Conflict):
    code = "KB_NOT_READY"
    title = "Knowledge base is not in a usable state"


class ChunkNotEditable(Conflict):
    code = "CHUNK_NOT_EDITABLE"
    title = "Chunk belongs to an index version that is not active"


class UnsupportedFileType(ValidationFailed):
    code = "UNSUPPORTED_FILE_TYPE"
    title = "Unsupported file type"


class BindingInUse(Conflict):
    code = "STORAGE_BINDING_IN_USE"
    title = "Storage binding is in use by a knowledge base"
