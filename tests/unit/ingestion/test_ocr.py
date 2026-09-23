from __future__ import annotations

import sys
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from cairn.ingestion.base import ParseContext
from cairn.ingestion.ocr import (
    ImageOcrParser,
    OcrEngineUnavailable,
    OcrInputTooLarge,
    OcrLimits,
    OcrTimedOut,
    TesseractOcrEngine,
)


def _png(*, width: int = 80, height: int = 40) -> bytes:
    image = Image.new("RGB", (width, height), "white")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _fake_tesseract(tmp_path: Path, body: str) -> tuple[str, ...]:
    script = tmp_path / "fake_tesseract.py"
    script.write_text(body, encoding="utf-8")
    return (sys.executable, str(script))


async def test_tesseract_tsv_becomes_page_and_bbox_provenance(tmp_path: Path) -> None:
    """FR-F-03/07: image OCR preserves word geometry and source provenance."""
    command = _fake_tesseract(
        tmp_path,
        """import sys
sys.stdin.buffer.read()
print('level\\tpage_num\\tblock_num\\tpar_num\\tline_num\\tword_num\\tleft\\ttop\\twidth\\theight\\tconf\\ttext')
print('5\\t1\\t1\\t1\\t1\\t1\\t4\\t5\\t20\\t10\\t96.5\\tHello')
print('5\\t1\\t1\\t1\\t1\\t2\\t28\\t5\\t30\\t10\\t91.0\\tOCR')
""",
    )
    engine = TesseractOcrEngine(command=command, languages=("eng",), max_concurrency=1)

    parsed = await ImageOcrParser(engine).parse(
        _png(), ParseContext(source_url="https://example.test/scan.png", language="en")
    )

    assert parsed.markdown == "Hello OCR"
    assert parsed.pages == 1
    assert parsed.blocks[0].page == 1
    assert parsed.blocks[0].bbox == (4.0, 5.0, 58.0, 15.0)
    assert parsed.blocks[0].source_url == "https://example.test/scan.png"
    assert parsed.metadata["ocr_engine"] == "tesseract"
    assert parsed.metadata["coordinate_space"] == "image_pixels_top_left"


async def test_image_parser_rejects_oversized_input_before_ocr(tmp_path: Path) -> None:
    """FR-F-03: image OCR enforces byte and decoded-pixel limits."""
    command = _fake_tesseract(tmp_path, "raise AssertionError('must not run')")
    parser = ImageOcrParser(
        TesseractOcrEngine(command=command),
        limits=OcrLimits(max_input_bytes=20, max_pixels=100, max_dimension=100),
    )

    with pytest.raises(OcrInputTooLarge) as bytes_error:
        await parser.parse(_png(), ParseContext())
    assert bytes_error.value.code == "OCR_INPUT_TOO_LARGE"

    parser = ImageOcrParser(
        TesseractOcrEngine(command=command),
        limits=OcrLimits(max_input_bytes=10_000, max_pixels=100, max_dimension=100),
    )
    with pytest.raises(OcrInputTooLarge):
        await parser.parse(_png(width=20, height=20), ParseContext())


async def test_missing_tesseract_has_explicit_error() -> None:
    """FR-F-03: an absent OCR runtime never degrades to empty output."""
    engine = TesseractOcrEngine(command=("definitely-not-a-real-tesseract-binary",))
    with pytest.raises(OcrEngineUnavailable) as caught:
        await engine.recognize(_png(), language="en")
    assert caught.value.code == "OCR_ENGINE_UNAVAILABLE"
    assert caught.value.retryable is False


async def test_tesseract_timeout_is_bounded_and_explicit(tmp_path: Path) -> None:
    """FR-F-03: OCR subprocesses have a hard execution timeout."""
    command = _fake_tesseract(tmp_path, "import time; time.sleep(5)")
    engine = TesseractOcrEngine(command=command, timeout_seconds=0.05)
    with pytest.raises(OcrTimedOut) as caught:
        await engine.recognize(_png())
    assert caught.value.code == "OCR_TIMED_OUT"


def test_invalid_ocr_limits_are_rejected() -> None:
    """FR-F-03: invalid OCR resource limits fail during construction."""
    with pytest.raises(ValueError, match="positive"):
        OcrLimits(max_pixels=0)
    with pytest.raises(ValueError, match="command"):
        TesseractOcrEngine(command=())


async def test_ocr_rejects_language_injection_before_process_launch() -> None:
    from cairn.ingestion.ocr import OcrLanguageUnsupported

    engine = TesseractOcrEngine(command=("must-not-run",))
    with pytest.raises(OcrLanguageUnsupported):
        await engine.recognize(_png(), language="eng --tessdata-dir=/tmp")


@pytest.mark.parametrize("payload", [b"not an image", b"\x89PNG\r\n\x1a\n"])
async def test_ocr_rejects_invalid_image_header(payload: bytes) -> None:
    from cairn.ingestion.errors import ParseCorruptFile

    with pytest.raises(ParseCorruptFile):
        await TesseractOcrEngine(command=("must-not-run",)).recognize(payload)


async def test_ocr_does_not_silently_discard_multipage_tiff() -> None:
    output = BytesIO()
    first = Image.new("RGB", (40, 40), "white")
    second = Image.new("RGB", (40, 40), "black")
    first.save(output, format="TIFF", save_all=True, append_images=[second])
    with pytest.raises(OcrInputTooLarge):
        await TesseractOcrEngine(command=("must-not-run",)).recognize(output.getvalue())


async def test_ocr_rejects_malformed_tsv(tmp_path: Path) -> None:
    from cairn.ingestion.ocr import OcrFailed

    command = _fake_tesseract(tmp_path, "import sys; sys.stdin.buffer.read(); print('bad output')")
    with pytest.raises(OcrFailed):
        await TesseractOcrEngine(command=command).recognize(_png())


async def test_ocr_bounds_subprocess_output(tmp_path: Path) -> None:
    command = _fake_tesseract(
        tmp_path,
        "import sys; sys.stdin.buffer.read(); "
        "print('tesseract fake' if '--version' in sys.argv else 'x'*100000)",
    )
    with pytest.raises(OcrInputTooLarge):
        await TesseractOcrEngine(command=command, limits=OcrLimits(max_output_bytes=100)).recognize(
            _png()
        )


async def test_missing_requested_language_is_not_silently_ignored(tmp_path: Path) -> None:
    command = _fake_tesseract(
        tmp_path,
        "import sys\n"
        "sys.stdin.buffer.read()\n"
        "print('Failed loading language chi_sim', file=sys.stderr)\n"
        "print('level\\tpage_num\\tblock_num\\tpar_num\\tline_num\\tword_num\\tleft\\ttop\\twidth\\theight\\tconf\\ttext')\n"
        "print('5\\t1\\t1\\t1\\t1\\t1\\t4\\t5\\t20\\t10\\t96.5\\tHello')\n",
    )
    with pytest.raises(OcrEngineUnavailable):
        await TesseractOcrEngine(command=command).recognize(_png(), language="zh")


def test_tiff_header_check_does_not_enumerate_all_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    from PIL.TiffImagePlugin import TiffImageFile

    from cairn.ingestion.ocr import image_dimensions

    original = TiffImageFile.n_frames.fget

    def enumerate_frames(image: TiffImageFile) -> int:
        if image._n_frames is None:
            raise AssertionError("Enumerating an untrusted frame chain is unbounded")
        assert original is not None
        return original(image)

    output = BytesIO()
    Image.new("RGB", (40, 40), "white").save(output, format="TIFF")
    monkeypatch.setattr(TiffImageFile, "n_frames", property(enumerate_frames))
    assert image_dimensions(output.getvalue(), OcrLimits()) == (40, 40)
    multiple = BytesIO()
    first = Image.new("RGB", (40, 40), "white")
    first.save(multiple, format="TIFF", save_all=True, append_images=[first.copy()])
    with pytest.raises(OcrInputTooLarge):
        image_dimensions(multiple.getvalue(), OcrLimits())
