from uuid import uuid4

import pytest

from cairn.core.parse_artifact import decode_payload, encode_payload
from cairn.ingestion.artifacts import ParseManifest, decode_parse_manifest, encode_parse_manifest
from cairn.ingestion.base import Block, ParsedDocument
from cairn.ingestion.ir import bind_document, project_document


def test_v2_has_one_authoritative_projection_and_rejects_wrong_owner():
    ir = bind_document(
        ParsedDocument("hello", [Block("paragraph", "hello", 0)]),
        workspace_id=uuid4(),
        kb_id=uuid4(),
        document_id=uuid4(),
        revision=1,
        source_sha256="a" * 64,
        parser_fingerprint="b" * 64,
        context_fingerprint="c" * 64,
    )
    manifest = ParseManifest(
        ir.document_id,
        1,
        2,
        ir.source_sha256,
        project_document(ir),
        ir.parser_fingerprint,
        ir.context_fingerprint,
    )
    raw = encode_parse_manifest(manifest)
    assert decode_parse_manifest(raw).parsed.ir == ir
    payload = decode_payload(raw)
    payload["parsed"] = {"markdown": "different redundant projection"}
    assert decode_parse_manifest(encode_payload(payload, version=2)).parsed.markdown == "hello"
    payload["document_id"] = str(uuid4())
    with pytest.raises(ValueError):
        decode_parse_manifest(encode_payload(payload, version=2))
