"""Catalog — the system of record for what knowledge exists.

Knowledge bases, documents, chunks, index versions, storage bindings.

Three things here are load-bearing and easy to get subtly wrong:

* The embedding model is **immutable once indexed** (ADR-0006), enforced at three
  layers because the failure mode is silent rather than loud.
* Index versions are **blue/green** (ADR-0007), so retuning chunking is a
  background operation rather than an outage — which is what makes anyone
  actually do it.
* The data plane sees only :class:`KnowledgeBaseRuntime` (ADR-0002), never an ORM
  object. Adding a field to that DTO is an interface change.
"""

from cairn.catalog.config import ChunkConfig, RetrievalConfig
from cairn.catalog.dto import (
    ChunkSpec,
    ChunkView,
    CreateKbSpec,
    DocumentRegistration,
    DocumentView,
    KnowledgeBaseRuntime,
    KnowledgeBaseView,
    ReindexSpec,
    UpdateKbSpec,
    UploadSpec,
)
from cairn.catalog.service import CatalogService, get_catalog_service

__all__ = [
    "CatalogService",
    "ChunkConfig",
    "ChunkSpec",
    "ChunkView",
    "CreateKbSpec",
    "DocumentRegistration",
    "DocumentView",
    "KnowledgeBaseRuntime",
    "KnowledgeBaseView",
    "ReindexSpec",
    "RetrievalConfig",
    "UpdateKbSpec",
    "UploadSpec",
    "get_catalog_service",
]
