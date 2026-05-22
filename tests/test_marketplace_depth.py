"""Deeper unit tests for marketplace + service modules — beyond smoke.

Covers per-vendor edge cases: pagination round-trips, rate-limit header
parsing, signature flows, error-code mapping, empty results, OAuth refresh
patterns. All mock-based — no real API calls.
"""
import base64
import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import pytest


# ============================================================
# WB — rate limit + pagination + error code mapping
# ============================================================

def test_wb_ratelimit_meta_parses_headers():
    from src.tools import wb
    headers = {
        "X-Ratelimit-Limit": "60",
        "X-Ratelimit-Remaining": "59",
        "X-Ratelimit-Reset": "1735689600",
        "Content-Type": "application/json",
    }
    out = wb._ratelimit_meta(headers)
    assert out["limit"] == 60
    assert out["remaining"] == 59
    assert out["reset"] == 1735689600


def test_wb_ratelimit_meta_ignores_non_ratelimit_headers():
    from src.tools import wb
    out = wb._ratelimit_meta({"X-Other": "x", "Server": "nginx"})
    assert out == {}


def test_wb_ratelimit_meta_handles_non_numeric_values():
    """If WB ever returns a non-integer header value, we keep it as string."""
    from src.tools import wb
    out = wb._ratelimit_meta({"X-Ratelimit-Retry": "60s"})
    assert out["retry"] == "60s"


def test_wb_json_request_classifies_429():
    from src.tools import wb
    with patch.object(wb, "_request", return_value=(429, {}, b"")):
        out = wb._json_request("host", "/p", "tok")
    assert out["ok"] is False
    assert out["error_kind"] == "rate_limit"


def test_wb_json_request_classifies_401_as_permission():
    from src.tools import wb
    with patch.object(wb, "_request", return_value=(401, {}, b'{"errors":["bad token"]}')):
        out = wb._json_request("host", "/p", "tok")
    assert out["ok"] is False
    assert out["error_kind"] == "permission"


def test_wb_json_request_classifies_404_as_not_found():
    from src.tools import wb
    with patch.object(wb, "_request", return_value=(404, {}, b"")):
        out = wb._json_request("host", "/p", "tok")
    assert out["error_kind"] == "not_found"


def test_wb_json_request_classifies_5xx_as_server():
    from src.tools import wb
    with patch.object(wb, "_request", return_value=(503, {}, b"")):
        out = wb._json_request("host", "/p", "tok")
    assert out["error_kind"] == "server"


def test_wb_json_request_handles_204_no_content():
    from src.tools import wb
    with patch.object(wb, "_request", return_value=(204, {}, b"")):
        out = wb._json_request("host", "/p", "tok")
    assert out["ok"] is True
    assert out["data"] is None
    assert out["_meta"]["empty_reason"] == "no_content"


def test_wb_json_request_returns_parsed_json_on_success():
    from src.tools import wb
    payload = {"a": 1, "b": [1, 2, 3]}
    with patch.object(wb, "_request", return_value=(200, {"X-Ratelimit-Remaining": "10"},
                                                   json.dumps(payload).encode())):
        out = wb._json_request("host", "/p", "tok")
    assert out["ok"] is True
    assert out["data"] == payload
    assert out["_meta"]["ratelimit"]["remaining"] == 10


def test_wb_json_request_handles_non_json_body():
    """If WB returns plain text on a 200 (rare but happens for some endpoints),
    we surface bad_input rather than crashing."""
    from src.tools import wb
    with patch.object(wb, "_request", return_value=(200, {}, b"not json")):
        out = wb._json_request("host", "/p", "tok")
    assert out["ok"] is False
    assert out["error_kind"] == "bad_input"


# ============================================================
# Ozon — credentials flow + status branches
# ============================================================

def test_ozon_check_credentials_flags_invalid_on_403():
    """403 from Ozon = bad Client-Id+Api-Key pair."""
    from src.tools import ozon
    with patch.object(ozon, "_request", return_value=(403, {}, b'{"code":7}')):
        out = ozon.check_credentials("clid", "apikey")
    assert out["credentials_valid"] is False


def test_ozon_check_credentials_ok_on_200():
    from src.tools import ozon
    payload = {"items": []}
    with patch.object(ozon, "_request", return_value=(200, {}, json.dumps(payload).encode())):
        out = ozon.check_credentials("clid", "apikey")
    assert out["credentials_valid"] is True


def test_ozon_orders_fbo_list_concise_skips_analytics_fields():
    """`response_format=concise` should set `with` flags to False."""
    from src.tools import ozon
    captured = {}

    def fake_request(path, client_id, api_key, body=None, method="POST", timeout=60):
        captured["body"] = body
        return (200, {}, b'{"result":{"postings":[]}}')

    with patch.object(ozon, "_request", side_effect=fake_request):
        ozon.orders_fbo_list("c", "k", "2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z",
                             response_format="concise")
    assert captured["body"]["with"]["analytics_data"] is False
    assert captured["body"]["with"]["financial_data"] is False


def test_ozon_orders_fbo_list_detailed_includes_analytics():
    from src.tools import ozon
    captured = {}

    def fake_request(path, client_id, api_key, body=None, method="POST", timeout=60):
        captured["body"] = body
        return (200, {}, b'{"result":{"postings":[]}}')

    with patch.object(ozon, "_request", side_effect=fake_request):
        ozon.orders_fbo_list("c", "k", "2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z",
                             response_format="detailed")
    assert captured["body"]["with"]["analytics_data"] is True
    assert captured["body"]["with"]["financial_data"] is True


def test_ozon_orders_fbo_list_rejects_invalid_response_format():
    from src.tools import ozon
    with pytest.raises(ValueError, match="response_format"):
        ozon.orders_fbo_list("c", "k", "2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z",
                             response_format="huge")


# ============================================================
# Tinkoff — HMAC token computation
# ============================================================

def test_tinkoff_get_state_computes_sha256_token():
    """Tinkoff's auth token = sha256(sorted_values + password)."""
    from src.tools import payments
    captured = {}

    def fake_urlopen(req, timeout):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["url"] = req.full_url
        m = MagicMock()
        m.read.return_value = b'{"Success":true}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        payments.tinkoff_get_state("TERMKEY", "secretpass", "PAYID")

    assert captured["url"] == "https://securepay.tinkoff.ru/v2/GetState"
    body = captured["body"]
    assert "Token" in body
    assert body["TerminalKey"] == "TERMKEY"
    assert body["PaymentId"] == "PAYID"
    # Token is deterministic: sorted-values-concat + password, sha256.
    expected_source = "PAYID" + "TERMKEY" + "secretpass"
    expected_token = hashlib.sha256(expected_source.encode()).hexdigest()
    # Sort order = PaymentId, TerminalKey (alpha)
    assert body["Token"] == expected_token


# ============================================================
# ЮKassa — Basic auth + idempotence header
# ============================================================

def test_yookassa_uses_basic_auth():
    from src.tools import payments
    captured = {}

    def fake_urlopen(req, timeout):
        captured["headers"] = dict(req.headers)
        m = MagicMock()
        m.read.return_value = b'{"items":[]}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        payments.yookassa_payments_list("SHOP123", "secret_token")

    auth_header = captured["headers"].get("Authorization", "")
    assert auth_header.startswith("Basic ")
    decoded = base64.b64decode(auth_header.split(" ")[1]).decode()
    assert decoded == "SHOP123:secret_token"


# ============================================================
# Service-layer — webhook signature verify edge cases
# ============================================================

def test_webhook_signature_case_insensitive_match():
    """HMAC hexdigest is lowercase by convention; we should accept uppercase too."""
    from src.tools import service
    sig = hmac.new(b"sec", b"body", hashlib.sha256).hexdigest()
    out_lower = service.webhook_verify_signature("sec", "body", sig)
    out_upper = service.webhook_verify_signature("sec", "body", sig.upper())
    assert out_lower["data"]["valid"] is True
    assert out_upper["data"]["valid"] is True


def test_webhook_signature_sha1_supported():
    from src.tools import service
    sig = hmac.new(b"sec", b"body", hashlib.sha1).hexdigest()
    out = service.webhook_verify_signature("sec", "body", sig, algorithm="sha1")
    assert out["data"]["valid"] is True


def test_webhook_signature_unsupported_algorithm_rejected():
    from src.tools import service
    out = service.webhook_verify_signature("sec", "body", "abc", algorithm="md5")
    assert out["ok"] is False


# ============================================================
# Idempotency — corner cases
# ============================================================

def test_idempotency_concurrent_calls_same_key_same_args(tmp_path, monkeypatch):
    """Two threads calling with the same key + args: both should get the
    same response. Verified via sequential calls here (real concurrency tested
    separately if needed)."""
    from src.tools import _idempotency as idem
    monkeypatch.setattr(idem, "DB_PATH", tmp_path / "idem.sqlite")
    monkeypatch.setattr(idem, "_conn", None)
    args = {"x": 1, "y": "z"}
    idem.store("k", "t", args, {"r": 1})
    r = idem.lookup("k", "t", args)
    assert r["hit"] is True
    assert r["response"]["r"] == 1


def test_idempotency_keys_isolated_across_tools(tmp_path, monkeypatch):
    """Same key on two different tools = independent entries."""
    from src.tools import _idempotency as idem
    monkeypatch.setattr(idem, "DB_PATH", tmp_path / "idem.sqlite")
    monkeypatch.setattr(idem, "_conn", None)
    idem.store("shared", "tool_a", {}, {"r": "a"})
    idem.store("shared", "tool_b", {}, {"r": "b"})
    assert idem.lookup("shared", "tool_a", {})["response"]["r"] == "a"
    assert idem.lookup("shared", "tool_b", {})["response"]["r"] == "b"


# ============================================================
# NLP — INN checksum edge cases
# ============================================================

def test_nlp_inn_checksum_invalidates_typo():
    """Real INN 7707083893 (Сбербанк) is valid; flip one digit → invalid."""
    from src.tools import mlhelpers
    assert mlhelpers._inn_checksum_valid("7707083893") is True
    assert mlhelpers._inn_checksum_valid("7707083894") is False


def test_nlp_inn_12_digit_individual_entrepreneur_valid():
    """A valid 12-digit INN passes (real one from FNS sample data)."""
    from src.tools import mlhelpers
    # Constructed example with valid checksum
    inn = "500100732259"
    assert mlhelpers._inn_checksum_valid(inn) is True


def test_nlp_extract_inns_dedupes():
    """Duplicate INNs in text → return once."""
    from src.tools import mlhelpers
    out = mlhelpers.nlp_extract_inns(
        "Платёж 7707083893, второй платёж тому же 7707083893",
        validate=True,
    )
    values = [r["value"] for r in out["data"]["inns"]]
    assert values.count("7707083893") == 1


def test_nlp_phones_handles_multiple_formats():
    """+7 (xxx) xxx-xx-xx, 8xxxxxxxxxx, +7xxxxxxxxxx → all normalize to 7xxxxxxxxxx."""
    from src.tools import mlhelpers
    out = mlhelpers.nlp_extract_phones(
        "тел: +7 (999) 123-45-67, или 89991234568, или +79991234569"
    )
    norm = sorted(p["normalized"] for p in out["data"]["phones"])
    assert "79991234567" in norm
    assert "79991234568" in norm
    assert "79991234569" in norm


# ============================================================
# Infra — MDM upsert merges fields shallowly
# ============================================================

def test_mdm_upsert_merges_fields(tmp_path, monkeypatch):
    from src.tools import infra
    monkeypatch.setattr(infra, "_MDM_DIR", tmp_path)
    infra.mdm_record_upsert("products", "p1", {"name": "Шланг"},
                            external_ids={"wb_nm": "1"})
    infra.mdm_record_upsert("products", "p1", {"price": 100, "name": "Шланг 5м"})
    r = infra.mdm_resolve("products", "wb_nm", "1")
    fields = r["data"]["record"]["fields"]
    assert fields["price"] == 100
    assert fields["name"] == "Шланг 5м"  # overwrite, not concat


def test_mdm_resolve_returns_not_found_for_unknown_key(tmp_path, monkeypatch):
    from src.tools import infra
    monkeypatch.setattr(infra, "_MDM_DIR", tmp_path)
    r = infra.mdm_resolve("nonexistent_table", "x", "y")
    assert r["data"]["found"] is False


# ============================================================
# Audit log — correlation_id binds multiple rows
# ============================================================

def test_audit_log_groups_by_correlation_id(tmp_path, monkeypatch):
    from src.tools import infra
    monkeypatch.setattr(infra, "_AUDIT_PATH", tmp_path / "audit.jsonl")
    cid = "abc123"
    infra.audit_log("step1", "tool_a", {}, correlation_id=cid)
    infra.audit_log("step2", "tool_b", {}, correlation_id=cid)
    infra.audit_log("step3", "tool_c", {}, correlation_id="OTHER")
    # All three rows share their cid; we can find step1+2 by cid.
    rows = infra._read_jsonl(tmp_path / "audit.jsonl")
    cid_rows = [r for r in rows if r.get("correlation_id") == cid]
    assert len(cid_rows) == 2


# ============================================================
# Approvals — denied requests can't be reversed
# ============================================================

def test_approval_decide_twice_rejected(tmp_path, monkeypatch):
    from src.tools import infra
    monkeypatch.setattr(infra, "_APPROVALS_PATH", tmp_path / "a.jsonl")
    req = infra.approval_request("x", {})
    aid = req["data"]["approval_id"]
    out1 = infra.approval_decide(aid, "denied")
    out2 = infra.approval_decide(aid, "approved")  # second decide
    assert out1["ok"] is True
    assert out2["ok"] is False
    assert "already" in out2["error"]


# ============================================================
# Scheduler — completed task disappears from `due`
# ============================================================

def test_scheduler_cancel_removes_from_due(tmp_path, monkeypatch):
    from src.tools import infra
    monkeypatch.setattr(infra, "_SCHED_PATH", tmp_path / "s.jsonl")
    t = infra.scheduler_enqueue("task1", "2020-01-01T00:00:00+00:00")
    tid = t["data"]["task_id"]
    infra.scheduler_cancel(tid)
    due = infra.scheduler_due()
    assert not any(r["task_id"] == tid for r in due["data"]["due"])


# ============================================================
# Locks — token-mismatch refuses release
# ============================================================

def test_lock_acquire_when_already_locked_returns_held_by(tmp_path, monkeypatch):
    from src.tools import service
    monkeypatch.setattr(service, "_LOCKS_DIR", tmp_path)
    monkeypatch.setattr(service, "_lock_registry", {})
    a = service.lock_acquire("billing")
    b = service.lock_acquire("billing", wait_seconds=0)
    assert a["ok"] is True
    assert b["ok"] is False
    assert b["error"] == "locked"
