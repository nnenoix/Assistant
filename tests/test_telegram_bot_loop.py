"""Tests for `src/telegram_bot_loop.py` — the docker-compose sidecar
entry-point that wraps `poll_once` in a long-running loop.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


def test_main_refuses_without_token(monkeypatch, capsys):
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
    from src import telegram_bot_loop
    rc = telegram_bot_loop.main()
    assert rc == 1
    err = capsys.readouterr().err
    assert "TG_BOT_TOKEN not set" in err


def test_main_exits_cleanly_on_stop_flag(monkeypatch):
    """Set the global _stop flag after one poll so the loop terminates
    on its own. Validates the SIGTERM handler shape."""
    monkeypatch.setenv("TG_BOT_TOKEN", "fake-token")
    from src import telegram_bot_loop

    poll_call_count = {"n": 0}

    def fake_poll(token, timeout_s=25):
        poll_call_count["n"] += 1
        # Flip the stop flag after the first poll so main() exits
        telegram_bot_loop._stop = True
        return {"ok": True, "processed": 0, "last_update_id": 1}

    monkeypatch.setattr(telegram_bot_loop, "poll_once", fake_poll)
    monkeypatch.setattr(telegram_bot_loop, "_stop", False)

    rc = telegram_bot_loop.main()
    assert rc == 0
    assert poll_call_count["n"] == 1


def test_main_backs_off_on_consecutive_errors(monkeypatch):
    """When poll_once returns ok:False, the loop should backoff and
    keep going (not crash). After a few errors + a successful poll, the
    backoff counter resets."""
    monkeypatch.setenv("TG_BOT_TOKEN", "fake-token")
    from src import telegram_bot_loop

    poll_results = iter([
        {"ok": False, "error": "boom1"},
        {"ok": False, "error": "boom2"},
        {"ok": True, "processed": 0, "last_update_id": 1},
    ])

    def fake_poll(token, timeout_s=25):
        result = next(poll_results)
        if "processed" in result:
            telegram_bot_loop._stop = True
        return result

    # No real sleep — patch out time.sleep so the backoff doesn't slow
    # the test. We only verify the loop survives consecutive ok:False.
    sleep_calls: list[float] = []
    monkeypatch.setattr(telegram_bot_loop.time, "sleep", lambda s: sleep_calls.append(s))
    monkeypatch.setattr(telegram_bot_loop, "poll_once", fake_poll)
    monkeypatch.setattr(telegram_bot_loop, "_stop", False)

    rc = telegram_bot_loop.main()
    assert rc == 0
    # Two backoffs were issued (one per consecutive error)
    assert len(sleep_calls) >= 2


def test_main_recovers_from_exception(monkeypatch):
    """A raising poll_once shouldn't take down the sidecar — backoff +
    retry on next iteration."""
    monkeypatch.setenv("TG_BOT_TOKEN", "fake-token")
    from src import telegram_bot_loop

    poll_call_count = {"n": 0}

    def flaky_poll(token, timeout_s=25):
        poll_call_count["n"] += 1
        if poll_call_count["n"] == 1:
            raise RuntimeError("network blip")
        telegram_bot_loop._stop = True
        return {"ok": True, "processed": 0, "last_update_id": 1}

    monkeypatch.setattr(telegram_bot_loop.time, "sleep", lambda s: None)
    monkeypatch.setattr(telegram_bot_loop, "poll_once", flaky_poll)
    monkeypatch.setattr(telegram_bot_loop, "_stop", False)

    rc = telegram_bot_loop.main()
    assert rc == 0
    assert poll_call_count["n"] == 2
