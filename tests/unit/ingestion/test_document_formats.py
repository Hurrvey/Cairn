from __future__ import annotations

import pytest

from cairn.catalog.errors import UnsupportedFileType
from cairn.catalog.router import _new_spool, _validate_upload_signature, _validated_upload_type
from cairn.ingestion.registry import get_parser_registry


@pytest.mark.parametrize(
    "filename,mime",
    [
        ("report.pdf", "application/pdf"),
        ("scan.png", "image/png"),
        ("scan.jpg", "image/jpeg"),
        ("scan.tiff", "image/tiff"),
    ],
)
def test_pdf_and_image_formats_are_available(filename: str, mime: str) -> None:
    assert _validated_upload_type(filename, mime) == (filename, mime)
    assert mime in get_parser_registry().supported_mimes


@pytest.mark.parametrize(
    "filename,magic",
    [
        ("report.pdf", b"%PDF-1.7"),
        ("scan.png", b"\x89PNG\r\n\x1a\n"),
        ("scan.jpg", b"\xff\xd8\xff\xe0"),
        ("scan.tiff", b"II*\x00"),
    ],
)
async def test_binary_upload_signature_matches_extension(filename: str, magic: bytes) -> None:
    with _new_spool() as spool:
        spool.write(magic + b"data")
        spool.seek(0)
        await _validate_upload_signature(spool, filename)
        assert spool.tell() == 0
    with _new_spool() as spool:
        spool.write(b"mismatched content")
        spool.seek(0)
        with pytest.raises(UnsupportedFileType):
            await _validate_upload_signature(spool, filename)


async def test_image_disguised_as_text_is_rejected() -> None:
    with _new_spool() as spool:
        spool.write(b"\x89PNG\r\n\x1a\n")
        spool.seek(0)
        with pytest.raises(UnsupportedFileType):
            await _validate_upload_signature(spool, "report.txt")
