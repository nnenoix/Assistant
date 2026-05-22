"""Smoke tests for all new marketplace / Russian-integration / infra tools.

These are intentionally LIGHT — they verify:
  - module imports cleanly (no syntax errors, no missing deps at load time)
  - every new tool is registered in the registry
  - tools with `requires_external_lib=True` return a structured «not installed»
    error rather than crashing
  - tools with pure-Python logic return the expected shape on a happy path

Real integration tests live in tests/integration/* and are gated by
LIVE_GOOGLE_TESTS=1 plus per-vendor credential env vars.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from src.tools import registry


# ---------- registry coverage ----------

EXPECTED_NEW_TOOL_PREFIXES = (
    "wb_stocks_v2", "wb_orders_recent", "wb_warehouses",
    "ozon_check_credentials", "ozon_stocks_fbo", "ozon_finance_realization",
    "yamarket_campaigns_list", "yamarket_stocks_list",
    "cdek_auth", "cdek_calculator", "boxberry_list_parcels", "pochta_track",
    "moysklad_products_list", "moysklad_profit_byproduct",
    "smsru_send", "smsc_send", "tg_send_message", "imap_recent",
    "yookassa_payments_list", "tinkoff_get_state",
    "avito_auth", "vk_users_get",
    "sbis_auth", "diadoc_authenticate",
    "nlp_extract_inns", "nlp_extract_phones", "dadata_suggest_address",
    "embed_texts", "cosine_similarity", "ocr_image",
    "duckdb_query", "duckdb_list_tables",
    "onec_odata_query", "onec_contractors",
    "mdm_table_get", "mdm_record_upsert", "approval_request",
    "audit_log", "bi_dashboard_render", "scheduler_enqueue",
    "skill_register", "zpl_render_label",
    "webhook_log", "webhook_verify_signature", "lock_acquire",
    "trace_span_log", "notify_route", "report_render_markdown",
    "team_channel_send",
)


def _by_name() -> dict:
    return {t["name"]: t for t in registry.TOOLS}


def test_total_tool_count_is_403():
    """The user's explicit ask: 403 tools end-to-end."""
    assert len(registry.TOOLS) == 403


def test_all_expected_new_tools_registered():
    by = _by_name()
    missing = [n for n in EXPECTED_NEW_TOOL_PREFIXES if n not in by]
    assert not missing, f"Missing from registry: {missing}"


def test_every_registered_tool_has_callable_fn():
    """Catches typos in the registry where someone forgets `fn=foo` or points to a wrong symbol."""
    bad: list[str] = []
    for t in registry.TOOLS:
        fn = t.get("fn")
        if not callable(fn):
            bad.append(t["name"])
    assert not bad, f"Tools with non-callable fn: {bad[:10]}"


def test_every_registered_tool_has_schema():
    """Each spec must expose name + description + input_schema."""
    missing: list[str] = []
    for t in registry.TOOLS:
        sch = t.get("schema") or {}
        if not sch.get("name") or not sch.get("description") or "input_schema" not in sch:
            missing.append(t["name"])
    assert not missing, f"Tools missing schema bits: {missing[:10]}"


# ---------- ML helpers (pure-Python branches) ----------

def test_nlp_extract_inns_keeps_valid_only():
    """Real INN 7707083893 (Сбербанк) is valid; '1234567890' isn't."""
    from src.tools import mlhelpers
    out = mlhelpers.nlp_extract_inns("Платёж 7707083893, ещё 1234567890", validate=True)
    assert out["ok"]
    values = [r["value"] for r in out["data"]["inns"]]
    assert "7707083893" in values
    assert "1234567890" not in values


def test_nlp_extract_phones_normalizes():
    from src.tools import mlhelpers
    out = mlhelpers.nlp_extract_phones("звонок +7 (999) 123-45-67 или 89991234567")
    assert out["ok"]
    norm = [p["normalized"] for p in out["data"]["phones"]]
    assert "79991234567" in norm


def test_nlp_extract_bik():
    from src.tools import mlhelpers
    out = mlhelpers.nlp_extract_bik("реквизиты БИК 044525225 для перевода")
    assert out["data"]["bik"] == ["044525225"]


def test_cosine_similarity_orthogonal_vectors():
    from src.tools import mlhelpers
    out = mlhelpers.cosine_similarity([1.0, 0.0], [0.0, 1.0])
    assert abs(out["data"]["similarity"]) < 1e-9


def test_cosine_similarity_identical_vectors():
    from src.tools import mlhelpers
    out = mlhelpers.cosine_similarity([0.5, 0.5], [0.5, 0.5])
    assert abs(out["data"]["similarity"] - 1.0) < 1e-9


def test_cosine_similarity_length_mismatch_returns_error():
    from src.tools import mlhelpers
    out = mlhelpers.cosine_similarity([1, 2, 3], [1, 2])
    assert out["ok"] is False
    assert "length mismatch" in out["error"]


# ---------- infra: MDM / approvals / audit / scheduler / skills ----------

def test_mdm_round_trip(tmp_path, monkeypatch):
    from src.tools import infra
    monkeypatch.setattr(infra, "_MDM_DIR", tmp_path)

    up = infra.mdm_record_upsert("products", "p1", {"name": "Шланг"},
                                  external_ids={"wb_nm": "12345"})
    assert up["ok"] and up["data"]["action"] == "created"

    # Resolve by wb_nm
    r = infra.mdm_resolve("products", "wb_nm", "12345")
    assert r["data"]["found"] is True
    assert r["data"]["record"]["fields"]["name"] == "Шланг"

    # Update
    up2 = infra.mdm_record_upsert("products", "p1", {"price": 199})
    assert up2["data"]["action"] == "updated"

    # Delete
    d = infra.mdm_delete("products", "p1")
    assert d["ok"]


def test_approval_lifecycle(tmp_path, monkeypatch):
    from src.tools import infra
    monkeypatch.setattr(infra, "_APPROVALS_PATH", tmp_path / "appr.jsonl")

    req = infra.approval_request("drive_delete", {"file_id": "X"}, reason="cleanup")
    aid = req["data"]["approval_id"]

    s1 = infra.approval_status(aid)
    assert s1["data"]["status"] == "pending"

    dec = infra.approval_decide(aid, "approved", decided_by="egor")
    assert dec["ok"]
    s2 = infra.approval_status(aid)
    assert s2["data"]["status"] == "approved"


def test_approval_decide_rejects_invalid_status(tmp_path, monkeypatch):
    from src.tools import infra
    monkeypatch.setattr(infra, "_APPROVALS_PATH", tmp_path / "appr.jsonl")
    req = infra.approval_request("x", {})
    out = infra.approval_decide(req["data"]["approval_id"], "maybe")
    assert out["ok"] is False


def test_audit_log_and_search(tmp_path, monkeypatch):
    from src.tools import infra
    monkeypatch.setattr(infra, "_AUDIT_PATH", tmp_path / "audit.jsonl")

    infra.audit_log("delete", "drive_delete", {"file_id": "F1"}, actor="agent")
    infra.audit_log("send", "gmail_send_draft", {"draft_id": "D1"}, actor="agent")

    out = infra.audit_search(tool="drive_delete")
    assert out["data"]["matched"] == 1
    assert out["data"]["rows"][0]["action"] == "delete"


def test_scheduler_enqueue_due_and_complete(tmp_path, monkeypatch):
    from src.tools import infra
    monkeypatch.setattr(infra, "_SCHED_PATH", tmp_path / "sched.jsonl")

    t = infra.scheduler_enqueue("verify_inventory", "2020-01-01T00:00:00+00:00")
    tid = t["data"]["task_id"]

    due = infra.scheduler_due()
    assert any(r["task_id"] == tid for r in due["data"]["due"])

    infra.scheduler_complete(tid, result_note="ok")
    due2 = infra.scheduler_due()
    assert not any(r["task_id"] == tid for r in due2["data"]["due"])


def test_skill_register_and_list(tmp_path, monkeypatch):
    from src.tools import infra
    monkeypatch.setattr(infra, "_SKILLS_PATH", tmp_path / "skills.jsonl")

    infra.skill_register("sla_monitor", "Check queues hourly",
                         tools=["wb_questions_count", "ozon_orders_fbs_list"],
                         tags=["ops"])
    out = infra.skill_list(tag="ops")
    assert out["data"]["count"] == 1
    assert out["data"]["skills"][0]["name"] == "sla_monitor"


def test_bi_dashboard_render_writes_html(tmp_path):
    from src.tools import infra
    out_path = tmp_path / "dash.html"
    out = infra.bi_dashboard_render(
        "Weekly KPIs",
        [{"label": "Revenue", "value": 1234567, "unit": "₽"},
         {"label": "Orders", "value": 42, "delta": "+8%"}],
        str(out_path),
    )
    assert out["ok"]
    html = out_path.read_text(encoding="utf-8")
    assert "Weekly KPIs" in html
    assert "Revenue" in html
    assert "1234567" in html
    assert "+8%" in html


def test_zpl_render_label_substitutes_fields(tmp_path):
    from src.tools import infra
    out_path = tmp_path / "label.zpl"
    template = "^XA^FO50,50^FD{name}: {qty}^FS^XZ"
    out = infra.zpl_render_label(template, {"name": "Шланг", "qty": 12}, str(out_path))
    assert out["ok"]
    raw = out_path.read_text(encoding="utf-8")
    assert "Шланг: 12" in raw
    assert "{name}" not in raw


# ---------- service: webhooks / locks / tracing ----------

def test_webhook_log_and_recent(tmp_path, monkeypatch):
    from src.tools import service
    monkeypatch.setattr(service, "_WEBHOOKS_PATH", tmp_path / "wh.jsonl")
    service.webhook_log("yookassa", {"event": "payment.succeeded", "id": "p1"})
    service.webhook_log("tinkoff", {"Status": "CONFIRMED"})
    out = service.webhook_recent(source="yookassa")
    assert out["data"]["count"] == 1
    assert out["data"]["rows"][0]["source"] == "yookassa"


def test_webhook_verify_signature_matches():
    from src.tools import service
    secret = "topsecret"
    body = '{"a":1}'
    import hashlib, hmac
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    out = service.webhook_verify_signature(secret, body, sig)
    assert out["data"]["valid"] is True


def test_webhook_verify_signature_rejects_tamper():
    from src.tools import service
    out = service.webhook_verify_signature("secret", '{"a":1}', "deadbeef")
    assert out["data"]["valid"] is False


def test_lock_acquire_release_round_trip(tmp_path, monkeypatch):
    from src.tools import service
    monkeypatch.setattr(service, "_LOCKS_DIR", tmp_path)
    # Clear in-process registry to isolate
    monkeypatch.setattr(service, "_lock_registry", {})

    a = service.lock_acquire("billing_run")
    assert a["ok"] is True
    token = a["data"]["token"]

    status = service.lock_status("billing_run")
    assert status["data"]["locked"] is True

    rel = service.lock_release("billing_run", token)
    assert rel["ok"] is True
    status2 = service.lock_status("billing_run")
    assert status2["data"]["locked"] is False


def test_lock_release_rejects_wrong_token(tmp_path, monkeypatch):
    from src.tools import service
    monkeypatch.setattr(service, "_LOCKS_DIR", tmp_path)
    monkeypatch.setattr(service, "_lock_registry", {})
    service.lock_acquire("foo")
    out = service.lock_release("foo", "wrong-token")
    assert out["ok"] is False


def test_trace_span_log_appends(tmp_path, monkeypatch):
    from src.tools import service
    monkeypatch.setattr(service, "_TRACE_PATH", tmp_path / "tr.jsonl")
    service.trace_span_log("sheets_query", 120.5, attributes={"rows": 50})
    service.trace_span_log("drive_search", 80.0)
    out = service.trace_recent(name_like="sheets")
    assert out["data"]["count"] == 1
    assert out["data"]["spans"][0]["duration_ms"] == 120.5


def test_report_render_csv(tmp_path):
    from src.tools import service
    out_path = tmp_path / "r.csv"
    out = service.report_render_csv(["sku", "qty"], [["A", 1], ["B", 2]], str(out_path))
    assert out["ok"]
    content = out_path.read_text(encoding="utf-8")
    assert "sku,qty" in content
    assert "B,2" in content


def test_report_render_markdown(tmp_path):
    from src.tools import service
    out_path = tmp_path / "r.md"
    out = service.report_render_markdown(
        "Weekly",
        [{"heading": "Revenue", "body": "**₽1.2M** week-over-week"}],
        str(out_path),
    )
    assert out["ok"]
    md = out_path.read_text(encoding="utf-8")
    assert md.startswith("# Weekly")
    assert "## Revenue" in md


def test_team_channel_send_routes_telegram_to_tg_send_message(tmp_path, monkeypatch):
    from src.tools import service
    monkeypatch.setattr(service, "_SERVICE_DIR", tmp_path)
    out = service.team_channel_send("telegram_ops", "deploy started", level="info")
    assert out["data"]["routing"]["next_tool"] == "tg_send_message"


def test_team_channel_send_routes_email_to_gmail_draft(tmp_path, monkeypatch):
    from src.tools import service
    monkeypatch.setattr(service, "_SERVICE_DIR", tmp_path)
    out = service.team_channel_send("email_finance", "month-end close pending")
    assert out["data"]["routing"]["next_tool"] == "gmail_create_draft"


def test_team_channel_send_unknown_channel_returns_no_next_tool(tmp_path, monkeypatch):
    from src.tools import service
    monkeypatch.setattr(service, "_SERVICE_DIR", tmp_path)
    out = service.team_channel_send("custom_xyz", "test")
    assert out["data"]["routing"]["next_tool"] is None


# ---------- DuckDB ----------

def test_duckdb_query_runs_simple_select(tmp_path, monkeypatch):
    from src.tools import analytics_local
    monkeypatch.setattr(analytics_local, "DB_PATH", tmp_path / "d.duckdb")
    out = analytics_local.duckdb_query("SELECT 1 AS x, 'hi' AS y")
    if not out.get("ok") and "not installed" in (out.get("error") or ""):
        pytest.skip("duckdb not installed")
    assert out["data"]["row_count"] == 1


def test_duckdb_list_tables_empty_on_fresh_db(tmp_path, monkeypatch):
    from src.tools import analytics_local
    monkeypatch.setattr(analytics_local, "DB_PATH", tmp_path / "d.duckdb")
    out = analytics_local.duckdb_list_tables()
    if not out.get("ok") and "not installed" in (out.get("error") or ""):
        pytest.skip("duckdb not installed")
    assert out["data"]["tables"] == []


# ---------- HTTP-shaped tools: structured error on 4xx ----------

def test_smsru_send_returns_structured_error_on_http_404():
    """Without hitting the real API, an HTTPError should come back as {ok=False, error, _meta.http_status}."""
    from src.tools import messaging
    from urllib.error import HTTPError
    fake_resp = MagicMock()
    fake_resp.read.return_value = b'{"status":"ERROR","status_code":201}'
    fake_resp.status = 404
    with patch("urllib.request.urlopen", side_effect=HTTPError("u", 404, "Not Found", {}, fake_resp)):
        out = messaging.smsru_send("api_id", "79991234567", "hi")
    assert out["ok"] is False
    assert out["_meta"]["http_status"] == 404


def test_wb_stocks_v2_handles_429_as_rate_limit():
    from src.tools import wb
    with patch.object(wb, "_request", return_value=(429, {"x-ratelimit-retry": "60"}, b"")):
        out = wb.stocks_v2("dummy_token")
    assert out["ok"] is False
    assert out["error_kind"] == "rate_limit"
    assert out["_meta"]["http_status"] == 429
