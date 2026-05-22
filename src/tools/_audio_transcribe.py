"""Audio transcription via OpenAI Whisper API (Phase 15E).

Why Whisper (vs local whisper.cpp / Deepgram / Yandex SpeechKit):
  - Best Russian-language transcription accuracy in the price/quality range
  - ~$0.006/minute, no infra to manage
  - Simple synchronous API via `openai` SDK (already in deps)
  - File sizes up to 25 MB per call (we'll chunk larger files)

API key:
  - OPENAI_API_KEY env var
  - If missing → AudioConfigError with setup hint AND fallback advice
    (paste pre-transcribed text instead)

Format support:
  - .mp3, .m4a, .wav, .ogg, .flac, .mp4, .mpeg, .mpga, .webm
  - Whisper handles them natively — no ffmpeg conversion needed
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


class AudioConfigError(RuntimeError):
    """Raised when audio transcription deps/keys are missing."""
    pass


# Whisper accepts files up to 25 MB. Larger files would need chunking
# (out of scope for v1 — error out with clear message).
MAX_FILE_BYTES = 25 * 1024 * 1024

ENV_VAR = "OPENAI_API_KEY"
_DEFAULT_MODEL = "whisper-1"


def transcribe(path: str, language: Optional[str] = "ru") -> dict:
    """Transcribe an audio file to text via Whisper API.

    Args:
      path: local audio file path
      language: ISO-639-1 hint ("ru", "en", None for auto-detect). Russian
                default since target user works with Russian Zoom calls.

    Returns:
      {text, _meta: {language, duration_s?, file_name, model, bytes}}

    Raises:
      AudioConfigError: OPENAI_API_KEY not set, with setup hint
      FileNotFoundError: path doesn't exist
      ValueError: file too large (>25 MB)
    """
    api_key = os.environ.get(ENV_VAR, "").strip()
    if not api_key:
        raise AudioConfigError(
            f"{ENV_VAR} not set. Whisper transcription needs an OpenAI API key.\n"
            f"Either:\n"
            f"  1. Set {ENV_VAR}=sk-... and retry, OR\n"
            f"  2. Paste pre-transcribed text (.txt) instead — Zoom provides "
            f"transcripts under Settings → Cloud Recordings → Audio Transcript."
        )

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Audio file not found: {path}")
    size = p.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ValueError(
            f"File too large for Whisper: {size:,} bytes (max {MAX_FILE_BYTES:,}). "
            f"Split the audio (e.g. ffmpeg -i input.mp3 -f segment -segment_time 600 "
            f"out_%03d.mp3) and transcribe each part separately."
        )

    # Lazy import — openai SDK is in deps but its import is heavy.
    from openai import OpenAI

    client = OpenAI(api_key=api_key)

    kwargs: dict = {"model": _DEFAULT_MODEL, "response_format": "verbose_json"}
    if language:
        kwargs["language"] = language

    with p.open("rb") as f:
        result = client.audio.transcriptions.create(file=f, **kwargs)

    # verbose_json returns object with .text, .language, .duration
    text = getattr(result, "text", "") or ""
    return {
        "text": text,
        "_meta": {
            "language": getattr(result, "language", language),
            "duration_s": getattr(result, "duration", None),
            "file_name": p.name,
            "model": _DEFAULT_MODEL,
            "bytes": size,
        },
    }
