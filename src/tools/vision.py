"""Vision OCR via Tesseract (pytesseract) — extract text from images.

Tesseract binary must be installed separately. On Windows:
  https://github.com/UB-Mannheim/tesseract/wiki
  After install, add to PATH OR set `pytesseract.tesseract_cmd`.

Russian language support requires the `rus` traineddata file in
`tessdata/`. The Windows installer includes this when you check
"Additional language data".

If Tesseract isn't installed, every call returns a structured error
instead of raising at import time.
"""
from pathlib import Path


def _check_tesseract() -> tuple[bool, str]:
    """Returns (available, version_or_error)."""
    try:
        import pytesseract  # type: ignore
        version = pytesseract.get_tesseract_version()
        return True, str(version)
    except ImportError:
        return False, "pytesseract not installed: pip install pytesseract"
    except Exception as e:
        return False, f"Tesseract binary not found: {type(e).__name__}: {e}"


def probe() -> dict:
    """Check if Tesseract is reachable. Returns {available, version_or_error}."""
    ok, info = _check_tesseract()
    return {"available": ok, "info": info}


def ocr(
    image_path: str,
    lang: str = "rus+eng",
    structured: bool = False,
) -> dict:
    """OCR the image at `image_path`.

    `lang`: Tesseract language code (e.g. 'rus', 'eng', 'rus+eng' for multi).
    `structured=True` returns per-word entries with bounding boxes instead
    of just the flat text.

    Returns {text, words?, _meta:{lang, image_path, char_count}}.
    """
    ok, info = _check_tesseract()
    if not ok:
        return {
            "text": None,
            "_meta": {
                "image_path": image_path,
                "error": info,
                "tesseract_available": False,
            },
        }
    import pytesseract  # safe — checked above
    from PIL import Image

    p = Path(image_path)
    if not p.exists():
        raise FileNotFoundError(image_path)

    img = Image.open(p)
    if structured:
        data = pytesseract.image_to_data(img, lang=lang, output_type=pytesseract.Output.DICT)
        words = []
        for i, word in enumerate(data.get("text", [])):
            if word.strip():
                words.append({
                    "text": word,
                    "conf": int(data["conf"][i]) if data["conf"][i] != "-1" else None,
                    "bbox": {
                        "left": int(data["left"][i]),
                        "top": int(data["top"][i]),
                        "width": int(data["width"][i]),
                        "height": int(data["height"][i]),
                    },
                    "line": data["line_num"][i],
                })
        text = " ".join(w["text"] for w in words)
        return {
            "text": text,
            "words": words,
            "_meta": {
                "image_path": str(p.resolve()),
                "lang": lang,
                "char_count": len(text),
                "word_count": len(words),
                "tesseract_version": info,
            },
        }
    text = pytesseract.image_to_string(img, lang=lang)
    return {
        "text": text.strip(),
        "_meta": {
            "image_path": str(p.resolve()),
            "lang": lang,
            "char_count": len(text),
            "tesseract_version": info,
        },
    }
