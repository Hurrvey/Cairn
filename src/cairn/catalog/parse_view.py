"""Owner-authorized immutable parse inspection, located from index-specific runs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

from sqlalchemy import select

from cairn.authz.model import Principal
from cairn.catalog.models import Document, DocumentIngestion, KnowledgeBase
from cairn.catalog.storage import object_binding
from cairn.core.db import session_scope
from cairn.core.document_ir import DocumentIR
from cairn.core.document_preview import PreviewSettings
from cairn.core.errors import NotFound
from cairn.core.native_process import (
    ProcessOutputTooLarge,
    ProcessSupervisionUnavailable,
    ProcessTimedOut,
    run_bounded,
)
from cairn.core.parse_artifact import ParseArtifactIdentity, verified_parse_payload
from cairn.objectstore.registry import ObjectBindingRef, get_object_registry


class ParseVersionStale(NotFound):
    code = "PARSE_VERSION_STALE"
    title = "This parse version is no longer available."


class ParseUnavailable(NotFound):
    code = "PARSE_UNAVAILABLE"
    title = "No structured parse is available for this document and index."


class ParseLegacyUnavailable(ParseUnavailable):
    code = "PARSE_LEGACY_UNAVAILABLE"
    title = (
        "This legacy parse has no structured IR; its Markdown and indexed chunks remain readable."
    )


async def locate_parse(
    actor: Principal,
    document_id: UUID,
    *,
    view: Literal["active", "candidate"],
    parse_version: str | None = None,
) -> ParseLocator:
    async with session_scope() as session:
        document = await session.get(Document, document_id)
        if (
            document is None
            or document.deleted_at is not None
            or document.workspace_id != actor.workspace_id
            or not actor.can("kb:read", document.kb_id)
        ):
            raise NotFound("Document not found.")
        kb = await session.get(KnowledgeBase, document.kb_id)
        if kb is None or kb.status in ("deleting", "archived"):
            raise NotFound("Document not found.")
        index = kb.active_index_version if view == "active" else kb.building_index_version
        run = (
            await session.scalar(
                select(DocumentIngestion).where(
                    DocumentIngestion.document_id == document_id,
                    DocumentIngestion.revision == document.revision,
                    DocumentIngestion.index_version == index,
                )
            )
            if index is not None
            else None
        )
        if parse_version is not None and (run is None or run.parse_version_id != parse_version):
            raise ParseVersionStale()
        if run is not None and run.parsed_object_key is not None and run.parse_version_id is None:
            raise ParseLegacyUnavailable()
        if run is None or run.parsed_object_key is None or run.parse_version_id is None:
            raise ParseUnavailable()
        # ACTIVE only resolves a completed immutable run; candidate requires a committed parse.
        if view == "active" and run.state != "indexed":
            raise ParseUnavailable()
        from cairn.catalog.models import StorageBinding

        binding = await session.get(StorageBinding, kb.object_binding_id)
        if binding is None:
            raise ParseUnavailable()
        binding_ref = await object_binding(session, binding)
        key = run.parsed_object_key
        identity = ParseArtifactIdentity(
            document.workspace_id,
            document.kb_id,
            document.id,
            document.revision,
            document.content_hash,
            run.index_version,
            run.parse_version_id,
        )
        return ParseLocator(key, identity, binding_ref, document.object_key, document.page_count)


@dataclass(frozen=True, slots=True)
class ParseLocator:
    key: str
    identity: ParseArtifactIdentity
    binding: ObjectBindingRef
    source_key: str | None
    pages: int | None


async def read_parse(
    actor: Principal,
    document_id: UUID,
    *,
    view: Literal["active", "candidate"],
    parse_version: str | None = None,
) -> DocumentIR:
    locator = await locate_parse(actor, document_id, view=view, parse_version=parse_version)
    store = await get_object_registry().for_binding(locator.binding)
    try:
        raw = await store.get_bytes(locator.key, max_bytes=256 * 1024**2)
        payload = verified_parse_payload(raw, locator.identity)
        return DocumentIR.model_validate(payload["ir"])
    except (ValueError, TypeError, KeyError) as exc:
        raise ParseUnavailable("The saved parse evidence could not be validated.") from exc


_preview_slots: asyncio.Semaphore | None = None


async def preview_page(
    actor: Principal,
    document_id: UUID,
    *,
    view: Literal["active", "candidate"],
    parse_version: str,
    page: int,
) -> bytes:
    settings = PreviewSettings()
    deadline = asyncio.get_running_loop().time() + settings.preview_timeout_s
    global _preview_slots
    if _preview_slots is None:
        _preview_slots = asyncio.Semaphore(settings.preview_max_concurrency)
    try:
        async with asyncio.timeout_at(deadline):
            locator = await locate_parse(actor, document_id, view=view, parse_version=parse_version)
            async with _preview_slots:
                if (
                    locator.pages is None
                    or locator.pages > settings.preview_max_pages
                    or not 1 <= page <= locator.pages
                ):
                    raise ParseUnavailable("Page preview exceeds the configured page budget.")
                if not locator.source_key:
                    raise ParseUnavailable()
                store = await get_object_registry().for_binding(locator.binding)
                source = await store.get_bytes(locator.source_key, max_bytes=50 * 1024**2)
                actual = await asyncio.to_thread(lambda: hashlib.sha256(source).hexdigest())
                if actual != locator.identity.source_sha256:
                    raise ParseVersionStale()
                config = {
                    "page": page,
                    "max_pages": settings.preview_max_pages,
                    "pixels": settings.preview_max_page_pixels,
                    "bytes": min(
                        settings.preview_max_image_bytes,
                        settings.preview_response_max_bytes,
                        settings.preview_max_document_bytes // locator.pages,
                    ),
                }
                code, output, _stderr = await run_bounded(
                    (
                        sys.executable,
                        str(Path(__file__).with_name("preview_worker.py")),
                        json.dumps(config),
                    ),
                    source,
                    timeout_seconds=max(0.001, deadline - asyncio.get_running_loop().time()),
                    max_output_bytes=config["bytes"],
                    max_memory_bytes=1024**3,
                )
                if code:
                    raise ParseUnavailable(
                        "Page preview could not be rendered within its resource budget."
                    )
                return output
    except (TimeoutError, ProcessTimedOut, ProcessOutputTooLarge) as exc:
        raise ParseUnavailable("Page preview exceeded its total request resource budget.") from exc
    except (OSError, ProcessSupervisionUnavailable) as exc:
        raise ParseUnavailable("Page preview process supervision is unavailable.") from exc


async def read_asset(
    actor: Principal,
    document_id: UUID,
    *,
    view: Literal["active", "candidate"],
    parse_version: str,
    asset_id: str,
) -> tuple[bytes, str]:
    ir = await read_parse(actor, document_id, view=view, parse_version=parse_version)
    asset = next((item for item in ir.assets if item.asset_id == asset_id), None)
    if asset is None:
        raise ParseVersionStale()
    async with session_scope() as session:
        kb = await session.get(KnowledgeBase, ir.kb_id)
        from cairn.catalog.models import StorageBinding

        binding = await session.get(StorageBinding, kb.object_binding_id) if kb else None
        if binding is None:
            raise ParseUnavailable()
        ref = await object_binding(session, binding)
    store = await get_object_registry().for_binding(ref)
    key = (
        f"{ir.workspace_id}/{ir.kb_id}/assets/{ir.document_id}/{ir.parse_version}/{asset.asset_id}"
    )
    data = await store.get_bytes(key, max_bytes=min(asset.size_bytes, 8 * 1024**2))
    if len(data) != asset.size_bytes or hashlib.sha256(data).hexdigest() != asset.sha256:
        raise ParseUnavailable("The extracted image failed integrity validation.")
    return data, asset.mime_type
