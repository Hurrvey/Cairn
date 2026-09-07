"""Object key layout.

Callers own their keys, but the layout is centralised here because
``delete_prefix`` correctness depends on it: purging a knowledge base
(``FR-C-08`` step b) must remove everything it owns and nothing it does not.
"""

from __future__ import annotations

from uuid import UUID

__all__ = ["ObjectKeys"]


class ObjectKeys:
    """Every key Cairn writes. Prefix-purgeable by workspace and by KB."""

    @staticmethod
    def workspace_prefix(workspace_id: UUID) -> str:
        return f"{workspace_id}/"

    @staticmethod
    def kb_prefix(workspace_id: UUID, kb_id: UUID) -> str:
        return f"{workspace_id}/{kb_id}/"

    @staticmethod
    def original(workspace_id: UUID, kb_id: UUID, content_hash: str) -> str:
        # Content-addressed, which makes re-uploading identical bytes a no-op
        # write to the same key rather than a duplicate object.
        return f"{workspace_id}/{kb_id}/originals/{content_hash}"

    @staticmethod
    def parsed_markdown(workspace_id: UUID, kb_id: UUID, doc_id: UUID, revision: int) -> str:
        return f"{workspace_id}/{kb_id}/parsed/{doc_id}/{revision}/content.md"

    @staticmethod
    def parsed_layout(workspace_id: UUID, kb_id: UUID, doc_id: UUID, revision: int) -> str:
        return f"{workspace_id}/{kb_id}/parsed/{doc_id}/{revision}/layout.json"

    @staticmethod
    def asset(workspace_id: UUID, kb_id: UUID, doc_id: UUID, index: int, ext: str) -> str:
        return f"{workspace_id}/{kb_id}/assets/{doc_id}/{index}.{ext}"

    @staticmethod
    def icon(workspace_id: UUID, kb_id: UUID, name: str) -> str:
        return f"{workspace_id}/{kb_id}/icon/{name}"

    @staticmethod
    def export(workspace_id: UUID, export_id: str) -> str:
        return f"{workspace_id}/exports/{export_id}.jsonl"
