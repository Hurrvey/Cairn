"""Private standalone worker, runnable with the isolated PDF Python environment.

One-shot mode (default): the configuration is ``argv[1]``, the PDF arrives on stdin
and the JSON payload leaves on stdout; the exit code classifies failures.

Persistent mode (``config["persistent"]``): the converter and its models are built
once, a ready frame is written, and the worker then serves length-prefixed
request/response frames until stdin reaches EOF. A request is a JSON meta frame
followed by a PDF frame; the response is one JSON frame carrying either the usual
payload or ``{"request_id", "error"}`` with the one-shot exit-code semantics.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import runpy
import shutil
import struct
import sys
from pathlib import Path
from typing import Any, BinaryIO

EXIT_CORRUPT = 2
EXIT_UNAVAILABLE = 3
EXIT_TOO_LARGE = 4


class _WorkerError(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def _prepare(config: dict[str, Any]) -> tuple[Any, Path]:
    """Verify the manifest, packages and assets, then build the converter once."""
    manifest = json.loads(Path(config["manifest"]).read_text(encoding="utf-8"))
    if manifest["identity"] != config["identity"]:
        raise _WorkerError(EXIT_UNAVAILABLE)
    canonical = {key: value for key, value in manifest.items() if key != "identity"}
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if digest != manifest["identity"]:
        raise _WorkerError(EXIT_UNAVAILABLE)
    root = Path(config["assets"]).resolve()
    for entry in manifest["files"]:
        asset = (root / entry["path"]).resolve()
        if not asset.is_relative_to(root) or asset.stat().st_size != entry["bytes"]:
            raise _WorkerError(EXIT_UNAVAILABLE)
    import importlib.metadata

    for name, expected in manifest.get("packages", {}).items():
        if importlib.metadata.version(name) != expected:
            raise _WorkerError(EXIT_UNAVAILABLE)
    try:
        version = importlib.metadata.version("docling-slim")
    except importlib.metadata.PackageNotFoundError:
        version = importlib.metadata.version("docling")
    if version != "2.135.0":
        raise _WorkerError(EXIT_UNAVAILABLE)
    from docling.datamodel.base_models import InputFormat  # type: ignore[import-not-found]
    from docling.datamodel.pipeline_options import (  # type: ignore[import-not-found]
        PdfPipelineOptions,
        TesseractCliOcrOptions,
    )
    from docling.document_converter import (  # type: ignore[import-not-found]
        DocumentConverter,
        PdfFormatOption,
    )

    options = PdfPipelineOptions(
        artifacts_path=root,
        do_ocr=config["ocr"],
        enable_remote_services=False,
        allow_external_plugins=False,
    )
    options.table_structure_options.do_cell_matching = True
    options.generate_picture_images = True
    options.images_scale = 1.0
    options.generate_parsed_pages = True
    if config["ocr"]:
        if shutil.which("tesseract") is None:
            raise _WorkerError(EXIT_UNAVAILABLE)
        helper = Path(__file__).with_name("ocr_environment.py")
        if not helper.exists():
            helper = Path(__file__).parents[1] / "core" / "ocr_environment.py"
        expected = manifest.get("ocr_environment")
        if not isinstance(expected, dict):
            raise _WorkerError(EXIT_UNAVAILABLE)
        actual = runpy.run_path(str(helper))["inspect_ocr"](tuple(expected["languages"]))
        if actual != expected or any(
            language not in actual["traineddata"] for language in config["ocr_languages"]
        ):
            raise _WorkerError(EXIT_UNAVAILABLE)
        options.ocr_options = TesseractCliOcrOptions(
            lang=config["ocr_languages"], psm=config["ocr_psm"]
        )
    # Docling's default PDF backend. The explicit pypdfium2 backend duplicated
    # overlapping text runs inside table cells.
    # One converter serves PDF (layout/table models) and Office formats (XML backends,
    # no models), so a persistent worker loads weights once for every format.
    converter = DocumentConverter(
        allowed_formats=[InputFormat.PDF, InputFormat.DOCX, InputFormat.PPTX],
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)},
    )
    return converter, root


OFFICE_FORMATS = ("docx", "pptx")


def _convert(converter: Any, source: bytes, config: dict[str, Any], fmt: str) -> dict[str, Any]:
    if fmt == "pdf":
        return _convert_pdf(converter, source, config)
    if fmt in OFFICE_FORMATS:
        return _convert_office(converter, source, config, fmt)
    raise _WorkerError(EXIT_CORRUPT)


def _convert_office(
    converter: Any, source: bytes, config: dict[str, Any], fmt: str
) -> dict[str, Any]:
    """Convert a DOCX or PPTX through Docling's XML backends: no models, no page geometry."""
    if len(source) > config["max_input_bytes"]:
        raise _WorkerError(EXIT_TOO_LARGE)
    from docling.datamodel.base_models import (  # type: ignore[import-not-found, unused-ignore]
        ConversionStatus,
        DocumentStream,
    )

    result = converter.convert(DocumentStream(name=f"source.{fmt}", stream=io.BytesIO(source)))
    if result.status != ConversionStatus.SUCCESS:
        raise _WorkerError(EXIT_CORRUPT)
    document = result.document
    payload: dict[str, Any] = document.export_to_dict()
    if len(payload.get("pages", {})) > config["max_pages"]:
        raise _WorkerError(EXIT_TOO_LARGE)
    payload["pdf_pages"] = []
    payload["items"] = [
        item.model_dump(mode="json")
        for item, _level in document.iterate_items()
        if hasattr(item, "prov")
    ]
    if (
        len(payload["items"]) > config["max_elements"]
        or sum(
            item.get("data", {}).get("num_rows", 0) * item.get("data", {}).get("num_cols", 0)
            for item in payload["items"]
        )
        > config["max_table_cells"]
    ):
        raise _WorkerError(EXIT_TOO_LARGE)
    payload["pdf_budget_facts"] = {"schema": 1, "pages": [], "output_bytes": 0}
    payload["ocr_transcripts"] = {}
    payload["ocr_transcription_status"] = "disabled"
    _collect_assets(document, payload, config)
    payload["format"] = fmt
    payload["units"] = "emu" if fmt == "pptx" else "points"
    payload["identity"] = config["identity"]
    payload["worker_pid"] = os.getpid()
    return payload


def _collect_assets(document: Any, payload: dict[str, Any], config: dict[str, Any]) -> None:
    """Encode extracted pictures as PNG assets within the configured budgets."""
    assets: list[dict[str, object]] = []
    total_asset_bytes = 0
    for picture in document.pictures:
        image = picture.get_image(document)
        if image is None:
            continue
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        value = buffer.getvalue()
        if (
            len(assets) >= config["max_assets"]
            or len(value) > config["max_asset_bytes"]
            or total_asset_bytes + len(value) > config["max_total_asset_bytes"]
        ):
            continue
        total_asset_bytes += len(value)
        identity = hashlib.sha256(value).hexdigest()
        assets.append(
            {
                "name": identity,
                "mime_type": "image/png",
                "data": base64.b64encode(value).decode(),
                "page": picture.prov[0].page_no if picture.prov else None,
            }
        )
        for item in payload["items"]:
            if item["self_ref"] == picture.self_ref:
                item["asset_id"] = identity
    payload["assets"] = assets


def _convert_pdf(converter: Any, source: bytes, config: dict[str, Any]) -> dict[str, Any]:
    """Convert one PDF into the worker payload, enforcing every configured bound."""
    if len(source) > config["max_input_bytes"]:
        raise _WorkerError(EXIT_TOO_LARGE)
    import pypdfium2 as pdfium  # type: ignore[import-untyped]
    from docling.datamodel.base_models import (  # type: ignore[import-not-found, unused-ignore]
        ConversionStatus,
        DocumentStream,
    )

    page_geometry = []
    with pdfium.PdfDocument(source) as original:
        if len(original) > config["max_pages"]:
            raise _WorkerError(EXIT_TOO_LARGE)
        for original_page in original:
            crop = original_page.get_bbox()
            width, height = crop[2] - crop[0], crop[3] - crop[1]
            if width > config["max_page_width_points"] or height > config["max_page_height_points"]:
                raise _WorkerError(EXIT_TOO_LARGE)
            pixels = math.ceil(width) * math.ceil(height)
            if pixels > config["max_page_pixels"]:
                raise _WorkerError(EXIT_TOO_LARGE)
            objects = rasters = 0
            for obj in original_page.get_objects(max_depth=15):
                objects += 1
                rasters += int(obj.type == 3)
                if (
                    objects > config["max_page_objects"]
                    or rasters > config["max_raster_rectangles"]
                ):
                    raise _WorkerError(EXIT_TOO_LARGE)
            page_geometry.append(
                {
                    "width": width,
                    "height": height,
                    "crop": list(crop),
                    "rotation": original_page.get_rotation(),
                    "objects": objects,
                    "rasters": rasters,
                }
            )
            original_page.close()
    result = converter.convert(
        DocumentStream(name="source.pdf", stream=io.BytesIO(source)),
        max_num_pages=config["max_pages"],
    )
    if result.status != ConversionStatus.SUCCESS:
        raise _WorkerError(EXIT_CORRUPT)
    document = result.document
    payload: dict[str, Any] = document.export_to_dict()
    if len(payload["pages"]) != len(page_geometry):
        raise _WorkerError(EXIT_CORRUPT)
    payload["pdf_pages"] = page_geometry
    payload["items"] = [
        item.model_dump(mode="json")
        for item, _level in document.iterate_items()
        if hasattr(item, "prov")
    ]
    if (
        len(payload["items"]) > config["max_elements"]
        or sum(
            item.get("data", {}).get("num_rows", 0) * item.get("data", {}).get("num_cols", 0)
            for item in payload["items"]
        )
        > config["max_table_cells"]
    ):
        raise _WorkerError(EXIT_TOO_LARGE)
    resource_pages = []
    for original_page in result.pages:
        parsed_page = original_page.parsed_page
        cells = list(parsed_page.textline_cells) if parsed_page is not None else []
        image = original_page.get_image(scale=1.0)
        geometry = page_geometry[original_page.page_no - 1]
        pixels = (
            image.width * image.height
            if image is not None
            else math.ceil(geometry["width"]) * math.ceil(geometry["height"])
        )
        image_bytes = pixels * 3
        if image is not None:
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            image_bytes = len(buffer.getvalue())
        chars = sum(len(cell.text) for cell in cells)
        words = sum(len(cell.text.split()) for cell in cells)
        if (
            pixels > config["max_page_pixels"]
            or image_bytes > config["max_render_bytes"]
            or chars > config["max_page_chars"]
            or words > config["max_layout_words"]
        ):
            raise _WorkerError(EXIT_TOO_LARGE)
        resource_pages.append(
            {
                "width": geometry["width"],
                "height": geometry["height"],
                "characters": chars,
                "words": words,
                "objects": geometry["objects"],
                "rasters": geometry["rasters"],
                "layout_blocks": sum(
                    any(prov["page_no"] == original_page.page_no for prov in item.get("prov", []))
                    for item in payload["items"]
                ),
                "render_pixels": pixels,
                "render_bytes": image_bytes,
            }
        )
    payload["pdf_budget_facts"] = {"schema": 1, "pages": resource_pages, "output_bytes": 0}
    # Docling exposes positioned OCR cells, but no authoritative page
    # transcription or line breaks. Joining cells would manufacture text
    # for CER scoring, so report the missing evidence explicitly.
    payload["ocr_transcripts"] = {}
    payload["ocr_transcription_status"] = "unsupported" if config["ocr"] else "disabled"
    _collect_assets(document, payload, config)
    payload["format"] = "pdf"
    payload["units"] = "points"
    payload["identity"] = config["identity"]
    payload["worker_pid"] = os.getpid()
    return payload


def _encode(payload: dict[str, Any], config: dict[str, Any]) -> bytes:
    for _ in range(3):
        output = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        payload["pdf_budget_facts"]["output_bytes"] = len(output)
    output = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
    if len(output) > config["max_output_bytes"]:
        raise _WorkerError(EXIT_TOO_LARGE)
    return output


def _read_frame(stream: BinaryIO, maximum: int) -> bytes | None:
    header = stream.read(4)
    if len(header) < 4:
        return None
    (length,) = struct.unpack(">I", header)
    if length > maximum:
        return None
    data = bytearray()
    while len(data) < length:
        chunk = stream.read(length - len(data))
        if not chunk:
            return None
        data.extend(chunk)
    return bytes(data)


def _write_frame(stream: BinaryIO, data: bytes) -> None:
    stream.write(struct.pack(">I", len(data)))
    stream.write(data)
    stream.flush()


def _serve(converter: Any, config: dict[str, Any]) -> int:
    """Serve conversions over stdin/stdout frames until the parent closes stdin."""
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    _write_frame(stdout, json.dumps({"ready": True, "identity": config["identity"]}).encode())
    while True:
        meta = _read_frame(stdin, 65536)
        if meta is None:
            return 0
        source = _read_frame(stdin, config["max_input_bytes"])
        if source is None:
            return 0
        request = json.loads(meta)
        request_id = request.get("request_id")
        fmt = str(request.get("format", "pdf"))
        try:
            payload = _convert(converter, source, config, fmt)
            payload["request_id"] = request_id
            response = _encode(payload, config)
        except _WorkerError as error:
            response = json.dumps({"request_id": request_id, "error": error.code}).encode()
        except MemoryError:
            response = json.dumps({"request_id": request_id, "error": EXIT_TOO_LARGE}).encode()
        except Exception:
            if config.get("diagnostics"):
                import traceback

                traceback.print_exc(file=sys.stderr)
            response = json.dumps({"request_id": request_id, "error": EXIT_CORRUPT}).encode()
        _write_frame(stdout, response)


def main() -> int:
    config = json.loads(sys.argv[1])
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", OMP_NUM_THREADS="2")
    try:
        if os.name != "nt":
            import resource

            resource.setrlimit(  # type: ignore[attr-defined]
                resource.RLIMIT_AS,  # type: ignore[attr-defined]
                (config["max_memory_bytes"], config["max_memory_bytes"]),
            )
        converter, _root = _prepare(config)
        if config.get("persistent"):
            return _serve(converter, config)
        source = sys.stdin.buffer.read(config["max_input_bytes"] + 1)
        payload = _convert(converter, source, config, str(config.get("format", "pdf")))
        sys.stdout.buffer.write(_encode(payload, config))
        return 0
    except _WorkerError as error:
        return error.code
    except MemoryError:
        return EXIT_TOO_LARGE
    except (ImportError, FileNotFoundError):
        return EXIT_UNAVAILABLE
    except Exception:
        if config.get("diagnostics"):
            import traceback

            traceback.print_exc(file=sys.stderr)
        return EXIT_CORRUPT


if __name__ == "__main__":
    raise SystemExit(main())
