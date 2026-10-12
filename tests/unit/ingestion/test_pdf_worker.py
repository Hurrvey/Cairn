from __future__ import annotations

import builtins
import json
from types import SimpleNamespace

import pypdfium2
import pypdfium2.raw
import pytest

from cairn.ingestion import pdf_worker


def test_nested_form_inverse_rotations_do_not_inflate_image_bounds() -> None:
    from pypdfium2 import PdfMatrix

    angle = 2**-0.5
    outer = SimpleNamespace(
        container=None,
        get_matrix=lambda: PdfMatrix(angle, -angle, angle, angle, 0, 0),
    )
    inner = SimpleNamespace(
        container=outer,
        get_matrix=lambda: PdfMatrix(angle, angle, -angle, angle, 0, 0),
    )
    image = SimpleNamespace(container=inner, get_bounds=lambda: (0.0, 0.0, 10.0, 10.0))

    assert pdf_worker._page_rectangle(image) == pytest.approx((0.0, 0.0, 10.0, 10.0))


@pytest.mark.parametrize(
    "error_code",
    [pypdfium2.raw.FPDF_ERR_PASSWORD, pypdfium2.raw.FPDF_ERR_SECURITY],
)
def test_worker_classifies_encryption_from_pdfium_code_and_sanitizes_stderr(
    error_code: int,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    def fail_to_open(data: bytes) -> None:
        del data
        raise pypdfium2.PdfiumError("secret native detail", err_code=error_code)

    monkeypatch.setattr(
        pypdfium2,
        "PdfDocument",
        fail_to_open,
    )

    with pytest.raises(SystemExit) as caught:
        pdf_worker._load_pdf(b"%PDF-1.7")

    assert caught.value.code == 2
    error = capfd.readouterr().err
    assert json.loads(error) == {"code": "encrypted"}
    assert "secret" not in error


def test_worker_reports_missing_pdfium_without_import_detail(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    original_import = builtins.__import__

    def import_without_pdfium(
        name: str,
        globals: object = None,
        locals: object = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "pypdfium2":
            raise ModuleNotFoundError("deployment path must stay private")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", import_without_pdfium)
    with pytest.raises(SystemExit) as caught:
        pdf_worker._load_pdf(b"%PDF-1.7")

    assert caught.value.code == 5
    error = capfd.readouterr().err
    assert json.loads(error) == {"code": "unavailable"}
    assert "deployment" not in error
