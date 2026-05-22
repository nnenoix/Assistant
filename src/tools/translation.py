"""Offline translation via Argos Translate.

Argos uses OPUS-MT models — first call downloads the model (~100MB
per language pair), subsequent calls are fast and offline.

If argostranslate isn't installed, every call returns a structured
error rather than raising at import time.
"""
from functools import lru_cache


def _check_argos() -> tuple[bool, str]:
    try:
        import argostranslate.package  # type: ignore
        import argostranslate.translate  # type: ignore
        return True, "ok"
    except ImportError:
        return False, "argostranslate not installed: pip install argostranslate"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def probe() -> dict:
    ok, info = _check_argos()
    return {"available": ok, "info": info}


@lru_cache(maxsize=32)
def _ensure_pair(source_lang: str, target_lang: str) -> bool:
    """Install the language pair if missing. Returns True if usable."""
    import argostranslate.package as pkg

    pkg.update_package_index()
    available = pkg.get_available_packages()
    needed = [
        p for p in available
        if p.from_code == source_lang and p.to_code == target_lang
    ]
    if not needed:
        return False
    # Skip if already installed
    installed = pkg.get_installed_packages()
    if any(p.from_code == source_lang and p.to_code == target_lang for p in installed):
        return True
    # Install (this downloads ~100MB)
    download_path = needed[0].download()
    pkg.install_from_path(download_path)
    return True


def translate(
    text: str,
    target_lang: str,
    source_lang: str | None = None,
) -> dict:
    """Translate `text` to `target_lang`. If `source_lang` is None, defaults
    to 'auto' which currently means: probe — assume 'ru' if any Cyrillic
    character, else 'en'.

    Returns {translated, source_lang, target_lang, _meta:{model_installed}}.
    First call to a new pair downloads ~100MB of model.
    """
    ok, info = _check_argos()
    if not ok:
        return {
            "translated": None,
            "_meta": {"error": info, "argos_available": False},
        }
    import argostranslate.translate as t

    if source_lang is None:
        has_cyr = any(0x0400 <= ord(c) <= 0x04FF for c in text)
        source_lang = "ru" if has_cyr else "en"

    if source_lang == target_lang:
        return {
            "translated": text,
            "source_lang": source_lang,
            "target_lang": target_lang,
            "_meta": {"noop": True},
        }

    try:
        installed = _ensure_pair(source_lang, target_lang)
    except Exception as e:
        return {
            "translated": None,
            "_meta": {"error": f"failed to install pair: {type(e).__name__}: {e}"},
        }
    if not installed:
        return {
            "translated": None,
            "_meta": {
                "error": f"no Argos package for {source_lang}→{target_lang}",
                "hint": "try installing manually via argostranslate-cli, or pivot through English",
            },
        }
    result = t.translate(text, source_lang, target_lang)
    return {
        "translated": result,
        "source_lang": source_lang,
        "target_lang": target_lang,
        "_meta": {"char_count": len(result)},
    }
