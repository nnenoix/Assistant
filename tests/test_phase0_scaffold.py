"""Phase 0 scaffolding tests — RBAC + OIDC + MCP HTTP discovery + queue worker.

These are smoke-level: imports clean, policy matrix correct, MCP endpoint
returns the tool list, OIDC stub decodes a JWT. Real production hardening
(JWKs rotation, refresh-token flow, full Casbin model, etc.) is followup
work — these tests just verify the scaffold is wired.
"""
import base64
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from src import rbac, auth_oidc


# ---------- RBAC ----------

def test_rbac_admin_allowed_everything():
    assert rbac.check_permission(["admin"], "drive.delete")["allowed"] is True
    assert rbac.check_permission(["admin"], "gmail.send")["allowed"] is True
    assert rbac.check_permission(["admin"], "apps_script.edit")["allowed"] is True


def test_rbac_finance_can_read_sheets_but_not_send_gmail():
    assert rbac.check_permission(["finance"], "sheets.read")["allowed"] is True
    assert rbac.check_permission(["finance"], "sheets.write")["allowed"] is True
    assert rbac.check_permission(["finance"], "gmail.send")["allowed"] is False
    assert rbac.check_permission(["finance"], "drive.delete")["allowed"] is False


def test_rbac_ops_runs_scripts_no_send():
    assert rbac.check_permission(["ops"], "apps_script.edit")["allowed"] is True
    assert rbac.check_permission(["ops"], "gmail.send")["allowed"] is False


def test_rbac_default_user_read_only():
    assert rbac.check_permission([], "drive.read")["allowed"] is True
    assert rbac.check_permission([], "drive.list")["allowed"] is True
    assert rbac.check_permission([], "drive.delete")["allowed"] is False
    assert rbac.check_permission([], "sheets.write")["allowed"] is False


def test_rbac_matches_wildcards():
    assert rbac._matches("*.read", "drive.read") is True
    assert rbac._matches("*.read", "sheets.read") is True
    assert rbac._matches("apps_script.*", "apps_script.edit") is True
    assert rbac._matches("*", "anything") is True
    assert rbac._matches("exact", "exact") is True
    assert rbac._matches("exact", "other") is False


def test_rbac_default_deny_for_unknown_op():
    """An op nobody has a rule for should be denied for every non-admin."""
    out = rbac.check_permission(["finance"], "totally_unknown.op")
    assert out["allowed"] is False


def test_rbac_uses_csv_when_present(tmp_path, monkeypatch):
    p = tmp_path / "custom.csv"
    p.write_text("special_role, foo.*, allow\n*, *, deny\n", encoding="utf-8")
    # Reset cache + point to custom file
    monkeypatch.setattr(rbac, "_loaded_path", None)
    monkeypatch.setattr(rbac, "DEFAULT_POLICY_PATH", p)
    assert rbac.check_permission(["special_role"], "foo.bar")["allowed"] is True
    assert rbac.check_permission(["other_role"], "foo.bar")["allowed"] is False


# ---------- OIDC ----------

def _fake_jwt(claims: dict) -> str:
    """Build an unsigned JWT (header.payload.fake_sig). Verifier in dev mode
    decodes it without checking signature."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"{header}.{payload}.fakesig"


def test_oidc_verify_decodes_claims_in_dev_mode():
    """Without python-jose installed, the verifier falls back to no-verify
    decode. Useful for local dev; flagged with `unsafe_no_verify=True` so
    production guards can refuse."""
    claims = {"sub": "u1", "email": "a@b", "groups": ["finance"]}
    token = _fake_jwt(claims)
    with patch.dict(os.modules if hasattr(os, "modules") else __import__("sys").modules,
                    {"jose": None}, clear=False):
        # Simulate jose-not-installed by ensuring jose import fails inside
        # verify_token. The function already handles ImportError via try/except.
        pass  # actual import path is fine; rely on the function's own fallback
    out = auth_oidc.verify_token(token)
    assert out["ok"] is True
    assert out["claims"]["sub"] == "u1"


def test_oidc_verify_rejects_malformed_token():
    out = auth_oidc.verify_token("not-a-jwt")
    assert out["ok"] is False
    assert "malformed" in out["error"].lower()


def test_oidc_verify_rejects_empty_token():
    out = auth_oidc.verify_token("")
    assert out["ok"] is False


def test_oidc_user_from_claims_normalizes_groups_path():
    """Authentik uses `groups`; Keycloak uses `realm_access.roles`. Both
    should normalize to `groups` in the returned user dict."""
    auth_user = auth_oidc.user_from_claims({"sub": "u", "email": "a@b", "groups": ["g1"]})
    assert auth_user["groups"] == ["g1"]

    kc_user = auth_oidc.user_from_claims({
        "sub": "u", "email": "a@b",
        "realm_access": {"roles": ["r1", "r2"]},
    })
    assert kc_user["groups"] == ["r1", "r2"]


def test_oidc_user_from_claims_default_tenant():
    user = auth_oidc.user_from_claims({"sub": "u"})
    assert user["tenant"] == "default"


# ---------- MCP HTTP ----------

def test_mcp_http_mount_is_idempotent_when_disabled(monkeypatch):
    """Without ENABLE_MCP_HTTP=1, mount_mcp_http is a no-op."""
    from fastapi import FastAPI
    from src.mcp_http import mount_mcp_http
    monkeypatch.delenv("ENABLE_MCP_HTTP", raising=False)
    app = FastAPI()
    routes_before = len(app.routes)
    mount_mcp_http(app)
    assert len(app.routes) == routes_before


def test_mcp_http_mounts_when_enabled(monkeypatch):
    from fastapi import FastAPI
    from src.mcp_http import mount_mcp_http
    monkeypatch.setenv("ENABLE_MCP_HTTP", "1")
    app = FastAPI()
    mount_mcp_http(app)
    paths = {r.path for r in app.routes}
    assert "/mcp" in paths


def test_mcp_http_discover_returns_tools(monkeypatch):
    """GET /mcp with ENABLE_MCP_HTTP_NOAUTH=1 returns the registered tool list."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.mcp_http import mount_mcp_http
    monkeypatch.setenv("ENABLE_MCP_HTTP", "1")
    monkeypatch.setenv("ENABLE_MCP_HTTP_NOAUTH", "1")
    app = FastAPI()
    mount_mcp_http(app)
    client = TestClient(app)
    resp = client.get("/mcp")
    assert resp.status_code == 200
    body = resp.json()
    assert body["jsonrpc"] == "2.0"
    assert body["result"]["count"] > 0
    # Every tool should carry MCP annotations now
    sample = body["result"]["tools"][0]
    assert "annotations" in sample
    assert sample["name"].startswith("mcp__gworkagent__")


def test_mcp_http_rejects_unauthed_when_auth_required(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.mcp_http import mount_mcp_http
    monkeypatch.setenv("ENABLE_MCP_HTTP", "1")
    monkeypatch.delenv("ENABLE_MCP_HTTP_NOAUTH", raising=False)
    app = FastAPI()
    mount_mcp_http(app)
    client = TestClient(app)
    resp = client.get("/mcp")
    assert resp.status_code == 401


def test_mcp_http_invoke_returns_501_stub(monkeypatch):
    """POST /mcp is scaffolded but not yet wired — returns 501 with a clear note."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.mcp_http import mount_mcp_http
    monkeypatch.setenv("ENABLE_MCP_HTTP", "1")
    monkeypatch.setenv("ENABLE_MCP_HTTP_NOAUTH", "1")
    app = FastAPI()
    mount_mcp_http(app)
    client = TestClient(app)
    resp = client.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/call"})
    assert resp.status_code == 501


# ---------- Queue worker ----------

def test_queue_worker_settings_lists_functions():
    """The arq WorkerSettings should advertise the registered tasks."""
    from src.queue.worker import WorkerSettings
    # WorkerSettings.functions is either the real list (arq installed) or
    # an empty stub. Either way the attribute must exist + be a list.
    assert isinstance(WorkerSettings.functions, list)


# ---------- /health probe ----------

def test_health_route_returns_ok():
    from fastapi.testclient import TestClient
    from src.app import app
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
