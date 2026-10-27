"""Опциональный OCR для сканированных PDF.

Требует установленного Tesseract и пакетов из requirements-ocr.txt
(pytesseract, Pillow). Включается переменной TZ_CHECKER_OCR=1.
"""

from __future__ import annotations

import io


def availability() -> dict[str, object]:
    try:
        import pytesseract  # noqa: F401
        from PIL import Image  # noqa: F401

        return {"available": True}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": str(exc)}


def ocr_pdf_page(page) -> str:
    """Распознать текст на странице pypdf (через извлечённые изображения)."""
    try:
        import pytesseract
        from PIL import Image
    except Exception:
        return ""
    parts: list[str] = []
    try:
        images = list(page.images)
    except Exception:
        return ""
    for image_file in images:
        try:
            image = Image.open(io.BytesIO(image_file.data))
            parts.append(pytesseract.image_to_string(image, lang="rus+eng"))
        except Exception:
            continue
    return "\n".join(parts).strip()
