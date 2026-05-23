"""Tests for /api/onboarding/state + /api/updates/* — the endpoints
the .exe's first-run wizard + update banner consume.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from src.app import app
    return TestClient(app)


# ============================================================
# /api/onboarding/state
# ============================================================

def test_onboarding_state_first_run(client):
    """Fresh install: Claude not installed, no Google accounts.
    Both steps `done: false`, current_step = first incomplete."""
    fake_status = {"claude_installed": False, "claude_authenticated": False}
    with patch("src.setup.check_setup_status", return_value=fake_status), \
         patch("src.auth.list_accounts_with_identity", return_value={"accounts": []}):
        r = client.get("/api/onboarding/state")
    assert r.status_code == 200
    body = r.json()
    assert body["complete"] is False
    assert body["current_step"] == "install_claude"
    assert all(s["done"] is False for s in body["steps"])
    assert body["accounts"] == []


def test_onboarding_state_claude_done_google_pending(client):
    """Claude is set up, but user hasn't OAuth'd yet → next step
    is google_signin."""
    with patch("src.setup.check_setup_status", return_value={
        "claude_installed": True, "claude_authenticated": True,
    }), patch("src.auth.list_accounts_with_identity", return_value={"accounts": []}):
        r = client.get("/api/onboarding/state")
    body = r.json()
    assert body["complete"] is False
    assert body["current_step"] == "google_signin"
    install_step = next(s for s in body["steps"] if s["id"] == "install_claude")
    google_step = next(s for s in body["steps"] if s["id"] == "google_signin")
    assert install_step["done"] is True
    assert google_step["done"] is False


def test_onboarding_state_fully_ready(client):
    """Everything set up — UI hands off to chat."""
    with patch("src.setup.check_setup_status", return_value={
        "claude_installed": True, "claude_authenticated": True,
    }), patch("src.auth.list_accounts_with_identity", return_value={
        "accounts": [{"alias": "main", "email": "egor@example.com"}]
    }):
        r = client.get("/api/onboarding/state")
    body = r.json()
    assert body["complete"] is True
    assert body["current_step"] == "ready"
    assert len(body["accounts"]) == 1


def test_onboarding_state_recovers_from_identity_lookup_failure(client):
    """`list_accounts_with_identity` makes a Drive API call that may
    fail (network, scope) — UI should still get a response."""
    with patch("src.setup.check_setup_status", return_value={
        "claude_installed": True, "claude_authenticated": True,
    }), patch("src.auth.list_accounts_with_identity",
              side_effect=RuntimeError("identity probe failed")):
        r = client.get("/api/onboarding/state")
    assert r.status_code == 200
    body = r.json()
    assert body["accounts"] == []
    # Google step shows as not done because we couldn't enumerate
    assert body["current_step"] == "google_signin"


# ============================================================
# /api/updates/check
# ============================================================

def test_updates_check_returns_no_update_when_manifest_url_unset(client, monkeypatch):
    monkeypatch.delenv("UPDATE_MANIFEST_URL", raising=False)
    r = client.get("/api/updates/check")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["update_available"] is False
    assert "manifest URL not configured" in body["_meta"]["reason"]


def test_updates_check_calls_updater_when_manifest_url_set(client, monkeypatch):
    monkeypatch.setenv("UPDATE_MANIFEST_URL", "https://example.com/manifest.json")
    fake = {"ok": True, "update_available": True,
            "current_version": "0.1.0", "latest_version": "0.2.0",
            "download_url": "https://example.com/agent-0.2.0.exe",
            "_meta": {"http_status": 200}}
    with patch("src.updater.check_for_updates", return_value=fake) as mock_check:
        r = client.get("/api/updates/check")
    assert r.status_code == 200
    assert r.json()["update_available"] is True
    assert mock_check.called
    args = mock_check.call_args.args
    assert args[1] == "https://example.com/manifest.json"


# ============================================================
# /api/updates/apply
# ============================================================

def test_updates_apply_downloads_then_swaps(client):
    """Happy path: download succeeds, apply_update succeeds — endpoint
    returns the apply envelope."""
    with patch("src.updater.download_update", return_value={
        "ok": True, "data": {"bytes": 1234, "sha256": "abc", "path": "/tmp/new"},
    }), patch("src.updater.apply_update", return_value={
        "ok": True, "data": {"applied": True, "backup_path": "/tmp/old"},
    }):
        r = client.post("/api/updates/apply", json={
            "download_url": "https://example.com/agent.exe",
            "expected_sha256": None,
        })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["data"]["applied"] is True


def test_updates_apply_short_circuits_on_download_failure(client):
    """If download fails, apply_update is never called."""
    apply_called = {"n": 0}

    def fake_apply(*a, **kw):
        apply_called["n"] += 1
        return {"ok": True}

    with patch("src.updater.download_update", return_value={
        "ok": False, "error": "404", "error_kind": "not_found",
    }), patch("src.updater.apply_update", side_effect=fake_apply):
        r = client.post("/api/updates/apply", json={
            "download_url": "https://example.com/missing.exe",
        })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error_kind"] == "not_found"
    assert apply_called["n"] == 0
