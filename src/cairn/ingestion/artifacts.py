"""Checksummed, revision-scoped artifacts for resumable ingestion stages."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from typing import Any
from uuid import UUID

from cairn.catalog.dto import ChunkSpec
from cairn.ingestion.base import Asset, Block, ParsedDocument
from cairn.vectorstore.base import SparseVector


@dataclass(frozen=True, slots=True)
class ParseManifest:
    document_id: UUID
    revision: int
    index_version: int
    source_content_hash: str
    parsed: ParsedDocument


@dataclass(frozen=True, slots=True)
class EmbeddingManifest:
    document_id: UUID
    revision: int
    index_version: int
    source_content_hash: str
    binding_fingerprint: str
    dimension: int
    normalized: bool
    vectors: dict[str, tuple[float, ...]]
    #: Identity of the sparse source these vectors came from; reuse never crosses it.
    sparse_encoder: str = ""
    #: Per content hash; ``None`` records a chunk with no indexable terms.
    sparse: dict[str, SparseVector | None] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ChunkManifest:
    document_id: UUID
    revision: int
    index_version: int
    source_content_hash: str
    chunks: tuple[ChunkSpec, ...]
    stale_point_ids: tuple[UUID, ...] = ()


def _encode(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    envelope = {
        "checksum": sha256(body).hexdigest(),
        "payload": base64.b64encode(body).decode("ascii"),
        "version": 1,
    }
    return json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode()


def _decode(raw: bytes) -> dict[str, Any]:
    try:
        envelope = json.loads(raw)
        if envelope.get("version") != 1:
            raise ValueError("unsupported artifact version")
        body = base64.b64decode(envelope["payload"], validate=True)
        if sha256(body).hexdigest() != envelope["checksum"]:
            raise ValueError("artifact checksum mismatch")
        payload = json.loads(body)
    except (KeyError, TypeError, json.JSONDecodeError, binascii.Error) as exc:
        raise ValueError("invalid ingestion artifact") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid ingestion artifact payload")
    return payload


def encode_parse_manifest(manifest: ParseManifest) -> bytes:
    parsed = manifest.parsed
    return _encode(
        {
            "document_id": str(manifest.document_id),
            "index_version": manifest.index_version,
            "parsed": {
                "assets": [
                    {
                        "data": base64.b64encode(asset.data).decode("ascii"),
                        "mime_type": asset.mime_type,
                        "name": asset.name,
                        "page": asset.page,
                    }
                    for asset in parsed.assets
                ],
                "blocks": [asdict(block) for block in parsed.blocks],
                "language": parsed.language,
                "markdown": parsed.markdown,
                "metadata": parsed.metadata,
                "pages": parsed.pages,
            },
            "revision": manifest.revision,
            "source_content_hash": manifest.source_content_hash,
            "type": "parse",
        }
    )


def decode_parse_manifest(raw: bytes) -> ParseManifest:
    payload = _decode(raw)
    if payload.get("type") != "parse":
        raise ValueError("artifact is not a parse manifest")
    parsed = payload["parsed"]
    return ParseManifest(
        document_id=UUID(payload["document_id"]),
        revision=int(payload["revision"]),
        index_version=int(payload["index_version"]),
        source_content_hash=str(payload["source_content_hash"]),
        parsed=ParsedDocument(
            markdown=str(parsed["markdown"]),
            blocks=[
                Block(
                    kind=block["kind"],
                    text=block["text"],
                    ordinal=int(block["ordinal"]),
                    heading_path=tuple(block.get("heading_path") or ()),
                    heading_level=block.get("heading_level"),
                    page=block.get("page"),
                    bbox=tuple(block["bbox"]) if block.get("bbox") is not None else None,
                    source_url=block.get("source_url"),
                )
                for block in parsed["blocks"]
            ],
            pages=int(parsed["pages"]),
            language=str(parsed["language"]),
            metadata=dict(parsed.get("metadata") or {}),
            assets=[
                Asset(
                    name=asset["name"],
                    mime_type=asset["mime_type"],
                    data=base64.b64decode(asset["data"], validate=True),
                    page=asset.get("page"),
                )
                for asset in parsed.get("assets", [])
            ],
        ),
    )


def encode_chunk_manifest(manifest: ChunkManifest) -> bytes:
    return _encode(
        {
            "chunks": [
                {
                    "content": chunk.content,
                    "content_hash": chunk.content_hash,
                    "document_id": str(chunk.document_id),
                    "id": str(chunk.id),
                    "metadata": chunk.metadata,
                    "ordinal": chunk.ordinal,
                    "parent_id": str(chunk.parent_id) if chunk.parent_id is not None else None,
                    "token_count": chunk.token_count,
                }
                for chunk in manifest.chunks
            ],
            "document_id": str(manifest.document_id),
            "index_version": manifest.index_version,
            "revision": manifest.revision,
            "source_content_hash": manifest.source_content_hash,
            "stale_point_ids": [str(point_id) for point_id in manifest.stale_point_ids],
            "type": "chunks",
        }
    )


def decode_chunk_manifest(raw: bytes) -> ChunkManifest:
    payload = _decode(raw)
    if payload.get("type") != "chunks":
        raise ValueError("artifact is not a chunk manifest")
    return ChunkManifest(
        document_id=UUID(payload["document_id"]),
        revision=int(payload["revision"]),
        index_version=int(payload["index_version"]),
        source_content_hash=str(payload["source_content_hash"]),
        chunks=tuple(
            ChunkSpec(
                id=UUID(chunk["id"]),
                document_id=UUID(chunk["document_id"]),
                ordinal=int(chunk["ordinal"]),
                content=str(chunk["content"]),
                content_hash=str(chunk["content_hash"]),
                token_count=int(chunk["token_count"]),
                parent_id=UUID(chunk["parent_id"]) if chunk.get("parent_id") else None,
                metadata=dict(chunk.get("metadata") or {}),
            )
            for chunk in payload["chunks"]
        ),
        stale_point_ids=tuple(UUID(point_id) for point_id in payload["stale_point_ids"]),
    )


def encode_embedding_manifest(manifest: EmbeddingManifest) -> bytes:
    return _encode(
        {
            "binding_fingerprint": manifest.binding_fingerprint,
            "dimension": manifest.dimension,
            "document_id": str(manifest.document_id),
            "index_version": manifest.index_version,
            "normalized": manifest.normalized,
            "revision": manifest.revision,
            "source_content_hash": manifest.source_content_hash,
            "type": "embedding",
            "vectors": {key: list(values) for key, values in manifest.vectors.items()},
            "sparse_encoder": manifest.sparse_encoder,
            "sparse": {
                key: None
                if vector is None
                else {"indices": list(vector.indices), "values": list(vector.values)}
                for key, vector in manifest.sparse.items()
            },
        }
    )


def decode_embedding_manifest(raw: bytes) -> EmbeddingManifest:
    payload = _decode(raw)
    if payload.get("type") != "embedding":
        raise ValueError("artifact is not an embedding manifest")
    return EmbeddingManifest(
        document_id=UUID(payload["document_id"]),
        revision=int(payload["revision"]),
        index_version=int(payload["index_version"]),
        source_content_hash=str(payload["source_content_hash"]),
        binding_fingerprint=str(payload["binding_fingerprint"]),
        dimension=int(payload["dimension"]),
        normalized=bool(payload["normalized"]),
        vectors={
            str(key): tuple(float(value) for value in values)
            for key, values in payload["vectors"].items()
        },
        sparse_encoder=str(payload.get("sparse_encoder", "")),
        sparse={
            str(key): None
            if entry is None
            else SparseVector(
                indices=tuple(int(index) for index in entry["indices"]),
                values=tuple(float(value) for value in entry["values"]),
            )
            for key, entry in (payload.get("sparse") or {}).items()
        },
    )
