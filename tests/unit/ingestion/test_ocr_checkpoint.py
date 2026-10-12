import io

import pytest
from tests.unit.ingestion.test_pdf import _image_pdf

from cairn.ingestion.base import ParseContext
from cairn.ingestion.ocr import OcrResult, OcrWord
from cairn.ingestion.ocr_cache import current_hooks, decode_ocr, encode_ocr
from cairn.ingestion.pdf import PdfParser


async def test_interrupted_scan_reuses_completed_page_and_reports_actual_progress():
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument.new()
    for rotation in (0, 90):
        with pdfium.PdfDocument(_image_pdf(rotation=rotation)) as page:
            document.import_pages(page)
    output = io.BytesIO()
    document.save(output)
    document.close()

    class Engine:
        calls = 0

        async def recognize(self, data, *, language=None):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("second page interrupted")
            return OcrResult("complete text", (), 100, 100, "eng", "test", "1")

    class Hooks:
        def __init__(self):
            self.cache = {}
            self.progress_values = []

        async def get(self, identity):
            return self.cache.get(identity)

        async def put(self, identity, result):
            self.cache[identity] = result

        async def progress(self, completed, total, phase):
            self.progress_values.append((completed, total, phase))

    engine, hooks = Engine(), Hooks()
    token = current_hooks.set(hooks)
    try:
        with pytest.raises(RuntimeError):
            await PdfParser(ocr=engine).parse(output.getvalue(), ParseContext())
        assert len(hooks.cache) == 1
        parsed = await PdfParser(ocr=engine).parse(output.getvalue(), ParseContext())
    finally:
        current_hooks.reset(token)
    assert parsed.pages == 2 and engine.calls == 3
    assert hooks.progress_values[-1] == (2, 2, "layout")


def test_checkpoint_checks_identity_checksum_and_geometry():
    result = OcrResult(
        "hello", (OcrWord("hello", 99, (0, 0, 10, 10)),), 20, 20, "eng", "tesseract", "5"
    )
    raw = encode_ocr(result, identity="a" * 64)
    assert decode_ocr(raw, identity="a" * 64) == result
    with pytest.raises(ValueError):
        decode_ocr(raw, identity="b" * 64)
    with pytest.raises(ValueError):
        decode_ocr(raw.replace(b"checksum", b"checksxm"), identity="a" * 64)
