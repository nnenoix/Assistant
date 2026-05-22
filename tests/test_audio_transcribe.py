"""Unit tests for src/tools/_audio_transcribe.py (Phase 15E)."""
from unittest.mock import MagicMock, patch

import pytest

from src.tools import _audio_transcribe


# ============== missing key ==============

def test_missing_api_key_raises_actionable_error(monkeypatch, tmp_path):
    monkeypatch.delenv(_audio_transcribe.ENV_VAR, raising=False)
    p = tmp_path / "x.mp3"
    p.write_bytes(b"fake")
    with pytest.raises(_audio_transcribe.AudioConfigError, match="OPENAI_API_KEY"):
        _audio_transcribe.transcribe(str(p))


def test_missing_key_error_includes_fallback_advice(monkeypatch, tmp_path):
    monkeypatch.delenv(_audio_transcribe.ENV_VAR, raising=False)
    p = tmp_path / "x.mp3"
    p.write_bytes(b"fake")
    try:
        _audio_transcribe.transcribe(str(p))
    except _audio_transcribe.AudioConfigError as e:
        msg = str(e)
        assert "paste pre-transcribed text" in msg.lower()


def test_empty_key_treated_as_missing(monkeypatch, tmp_path):
    monkeypatch.setenv(_audio_transcribe.ENV_VAR, "  ")  # whitespace only
    p = tmp_path / "x.mp3"
    p.write_bytes(b"fake")
    with pytest.raises(_audio_transcribe.AudioConfigError):
        _audio_transcribe.transcribe(str(p))


# ============== file checks ==============

def test_missing_file_raises(monkeypatch):
    monkeypatch.setenv(_audio_transcribe.ENV_VAR, "sk-test")
    with pytest.raises(FileNotFoundError):
        _audio_transcribe.transcribe("/nonexistent.mp3")


def test_file_too_large_raises(monkeypatch, tmp_path):
    monkeypatch.setenv(_audio_transcribe.ENV_VAR, "sk-test")
    p = tmp_path / "big.mp3"
    # Make file 26 MB
    p.write_bytes(b"x" * (26 * 1024 * 1024))
    with pytest.raises(ValueError, match="too large"):
        _audio_transcribe.transcribe(str(p))


# ============== happy path via mock OpenAI ==============

def test_transcribe_returns_text_and_meta(monkeypatch, tmp_path):
    monkeypatch.setenv(_audio_transcribe.ENV_VAR, "sk-test-key")

    # Build a small fake audio file
    p = tmp_path / "zoom_call.mp3"
    p.write_bytes(b"x" * 1024)

    # Mock OpenAI client
    fake_response = MagicMock()
    fake_response.text = "Это транскрипция русского созвона."
    fake_response.language = "ru"
    fake_response.duration = 145.7

    fake_client = MagicMock()
    fake_client.audio.transcriptions.create.return_value = fake_response

    with patch("openai.OpenAI", return_value=fake_client):
        r = _audio_transcribe.transcribe(str(p))

    assert r["text"] == "Это транскрипция русского созвона."
    assert r["_meta"]["language"] == "ru"
    assert r["_meta"]["duration_s"] == 145.7
    assert r["_meta"]["file_name"] == "zoom_call.mp3"
    assert r["_meta"]["model"] == "whisper-1"
    assert r["_meta"]["bytes"] == 1024


def test_transcribe_passes_language_hint(monkeypatch, tmp_path):
    monkeypatch.setenv(_audio_transcribe.ENV_VAR, "sk-test")
    p = tmp_path / "x.mp3"
    p.write_bytes(b"x" * 100)

    fake_resp = MagicMock(text="", language="en", duration=10.0)
    fake_client = MagicMock()
    fake_client.audio.transcriptions.create.return_value = fake_resp

    with patch("openai.OpenAI", return_value=fake_client):
        _audio_transcribe.transcribe(str(p), language="en")

    call = fake_client.audio.transcriptions.create.call_args
    assert call.kwargs["language"] == "en"


def test_transcribe_auto_detect_language(monkeypatch, tmp_path):
    """language=None means auto-detect; no language kwarg sent to API."""
    monkeypatch.setenv(_audio_transcribe.ENV_VAR, "sk-test")
    p = tmp_path / "x.mp3"
    p.write_bytes(b"x" * 100)

    fake_resp = MagicMock(text="", language="de", duration=5.0)
    fake_client = MagicMock()
    fake_client.audio.transcriptions.create.return_value = fake_resp

    with patch("openai.OpenAI", return_value=fake_client):
        _audio_transcribe.transcribe(str(p), language=None)

    call = fake_client.audio.transcriptions.create.call_args
    assert "language" not in call.kwargs
