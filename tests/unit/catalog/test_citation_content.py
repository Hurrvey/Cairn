"""Document content decoding stays in step with the ingestion artifact format."""

from __future__ import annotations

from uuid import uuid4

import pytest

from cairn.catalog.service import parsed_markdown
from cairn.core.errors import UpstreamUnavailable
from cairn.ingestion.artifacts import ParseManifest, encode_parse_manifest
from cairn.ingestion.base import ParsedDocument


def test_catalog_reads_the_markdown_ingestion_wrote() -> None:
    manifest = ParseManifest(
        document_id=uuid4(),
        revision=2,
        index_version=1,
        source_content_hash="a" * 64,
        parsed=ParsedDocument(
            markdown="# 标题\n\nSaturn has rings.", blocks=[], pages=1, language="en"
        ),
    )

    assert parsed_markdown(encode_parse_manifest(manifest)) == "# 标题\n\nSaturn has rings."


def test_a_tampered_artifact_is_unreadable_not_trusted() -> None:
    raw = encode_parse_manifest(
        ParseManifest(
            document_id=uuid4(),
            revision=1,
            index_version=1,
            source_content_hash="b" * 64,
            parsed=ParsedDocument(markdown="x", blocks=[], pages=1, language="en"),
        )
    ).replace(b'"checksum":"', b'"checksum":"0')

    with pytest.raises(UpstreamUnavailable):
        parsed_markdown(raw)
