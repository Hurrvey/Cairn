"""Private PDF preview child: source bytes in, bounded PNG bytes out."""

import io
import json
import os
import sys


def main() -> int:
    config = json.loads(sys.argv[1])
    try:
        if os.name != "nt":
            import resource

            resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))  # type: ignore[attr-defined]
        import pypdfium2 as pdfium  # type: ignore[import-untyped]

        data = sys.stdin.buffer.read(50 * 1024**2 + 1)
        if len(data) > 50 * 1024**2:
            return 4
        with pdfium.PdfDocument(data) as document:
            if len(document) > config["max_pages"] or not 1 <= config["page"] <= len(document):
                return 4
            page = document[config["page"] - 1]
            width, height = page.get_size()
            scale = min(1.5, (config["pixels"] / (width * height)) ** 0.5)
            bitmap = page.render(scale=scale)
            image = bitmap.to_pil()
            output = io.BytesIO()
            image.save(output, format="PNG")
            value = output.getvalue()
            if len(value) > config["bytes"]:
                return 4
            sys.stdout.buffer.write(value)
            image.close()
            bitmap.close()
            page.close()
        return 0
    except Exception:
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
