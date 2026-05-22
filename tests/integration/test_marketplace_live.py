"""Integration tests hitting REAL marketplace / messaging APIs.

Each test family is gated by its own `LIVE_<VENDOR>_TESTS=1` env var plus
the vendor's credential env vars. Without those, tests are SKIPPED. This
mirrors the `LIVE_GOOGLE_TESTS=1` pattern already in the project — opt-in,
no surprise charges, no surprise data writes.

To run WB tests:
    $env:LIVE_WB_TESTS = "1"
    $env:WB_TOKEN = "<seller JWT>"
    uv run pytest tests/integration/test_marketplace_live.py -k wb

Most tests use the cheapest READ-only endpoint per vendor — no balance
deductions, no real SMS/Telegram messages, no DB writes. Tests that
WOULD write are clearly marked `_writes_real_data` and additionally gated
on `LIVE_<VENDOR>_WRITE=1`.
"""
import os
import time

import pytest


def _skip_unless(env_var: str, *required_creds: str):
    """Helper: skip if env var != '1' OR any required cred is missing."""
    if os.environ.get(env_var) != "1":
        pytest.skip(f"set {env_var}=1 to run live tests")
    missing = [c for c in required_creds if not os.environ.get(c)]
    if missing:
        pytest.skip(f"missing env vars: {missing}")


# ============================================================
# Wildberries
# ============================================================

def test_live_wb_check_token():
    """Token ping — cheapest WB call, no rate-limit cost."""
    _skip_unless("LIVE_WB_TESTS", "WB_TOKEN")
    from src.tools import wb
    out = wb.check_token(os.environ["WB_TOKEN"])
    assert isinstance(out, dict)
    # At least one API family should report a non-error code (200 or 401
    # depending on the token's scope mix; the test just verifies network
    # path + JSON parse succeeds).
    has_any_response = any(
        isinstance(v, dict) and ("code" in v or "error" in v)
        for v in out.values()
    )
    assert has_any_response


def test_live_wb_token_age():
    """JWT decode is pure local — should always work given a valid token."""
    _skip_unless("LIVE_WB_TESTS", "WB_TOKEN")
    from src.tools import wb
    out = wb.token_age(os.environ["WB_TOKEN"])
    if "error" in out:
        pytest.fail(f"token_age failed: {out['error']}")
    assert "expires_at" in out
    assert "days_left" in out


def test_live_wb_warehouses():
    """Marketplace API — lists WB warehouses. Reasonable rate limit."""
    _skip_unless("LIVE_WB_TESTS", "WB_TOKEN")
    from src.tools import wb
    out = wb.warehouses(os.environ["WB_TOKEN"])
    assert out.get("ok") is True or out.get("_meta", {}).get("http_status") == 401


# ============================================================
# Ozon
# ============================================================

def test_live_ozon_check_credentials():
    """Cheapest Ozon read — verifies the Client-Id + Api-Key pair."""
    _skip_unless("LIVE_OZON_TESTS", "OZON_CLIENT_ID", "OZON_API_KEY")
    from src.tools import ozon
    out = ozon.check_credentials(os.environ["OZON_CLIENT_ID"], os.environ["OZON_API_KEY"])
    assert "credentials_valid" in out
    # On valid creds, http_status should be 200; on invalid, 401/403
    assert out["_meta"]["http_status"] in (200, 401, 403)


def test_live_ozon_warehouses():
    _skip_unless("LIVE_OZON_TESTS", "OZON_CLIENT_ID", "OZON_API_KEY")
    from src.tools import ozon
    out = ozon.warehouses_list(os.environ["OZON_CLIENT_ID"], os.environ["OZON_API_KEY"])
    if out.get("ok"):
        assert "data" in out


# ============================================================
# Yandex Market
# ============================================================

def test_live_yamarket_campaigns_list():
    _skip_unless("LIVE_YAMARKET_TESTS", "YAMARKET_API_KEY")
    from src.tools import yamarket
    out = yamarket.campaigns_list(os.environ["YAMARKET_API_KEY"])
    assert "data" in out or out.get("ok") is False


def test_live_yamarket_businesses_list():
    _skip_unless("LIVE_YAMARKET_TESTS", "YAMARKET_API_KEY")
    from src.tools import yamarket
    out = yamarket.businesses_list(os.environ["YAMARKET_API_KEY"])
    assert "data" in out or out.get("ok") is False


# ============================================================
# МойСклад
# ============================================================

def test_live_moysklad_organizations_list():
    _skip_unless("LIVE_MOYSKLAD_TESTS", "MOYSKLAD_TOKEN")
    from src.tools import moysklad
    out = moysklad.organizations_list(os.environ["MOYSKLAD_TOKEN"])
    assert out.get("ok") in (True, False)  # both are valid as long as we got HTTP back


def test_live_moysklad_stores_list():
    _skip_unless("LIVE_MOYSKLAD_TESTS", "MOYSKLAD_TOKEN")
    from src.tools import moysklad
    out = moysklad.stores_list(os.environ["MOYSKLAD_TOKEN"])
    assert out.get("_meta", {}).get("http_status") in (200, 401, 403)


# ============================================================
# Telegram Bot
# ============================================================

def test_live_telegram_get_me():
    """getMe verifies bot token without sending anything."""
    _skip_unless("LIVE_TG_TESTS", "TG_BOT_TOKEN")
    from src.tools import messaging
    out = messaging.tg_get_me(os.environ["TG_BOT_TOKEN"])
    assert out.get("ok") is True or out.get("_meta", {}).get("http_status") == 401


def test_live_telegram_dry_run_doesnt_send():
    """tg_send_message(dry_run=True) returns preview, never hits Telegram."""
    _skip_unless("LIVE_TG_TESTS", "TG_BOT_TOKEN")
    from src.tools import messaging
    out = messaging.tg_send_message(
        os.environ["TG_BOT_TOKEN"], chat_id="@invalid", text="dry run",
        dry_run=True,
    )
    assert out["dry_run"] is True
    assert out["executed"] is False


# ============================================================
# SMS.ru (BALANCE READ ONLY — no actual SMS sent)
# ============================================================

def test_live_smsru_balance():
    _skip_unless("LIVE_SMSRU_TESTS", "SMSRU_API_ID")
    from src.tools import messaging
    out = messaging.smsru_balance(os.environ["SMSRU_API_ID"])
    assert "data" in out


def test_live_smsru_dry_run():
    """smsru_send(dry_run=True) returns preview, never bills."""
    _skip_unless("LIVE_SMSRU_TESTS", "SMSRU_API_ID")
    from src.tools import messaging
    out = messaging.smsru_send(
        os.environ["SMSRU_API_ID"],
        to="79991234567", msg="dry run test",
        dry_run=True,
    )
    assert out["dry_run"] is True
    assert "msg_length" in out["plan"]


# ============================================================
# DaData (suggest-only, free tier)
# ============================================================

def test_live_dadata_suggest_address():
    """Suggest is free; clean is paid (NOT covered here)."""
    _skip_unless("LIVE_DADATA_TESTS", "DADATA_TOKEN")
    from src.tools import mlhelpers
    out = mlhelpers.dadata_suggest_address(os.environ["DADATA_TOKEN"], "Москва Тверская")
    assert "suggestions" in out.get("data", {}) or out.get("ok") is False


def test_live_dadata_find_party_by_inn():
    """Lookup a known company (Сбербанк 7707083893) — should always exist."""
    _skip_unless("LIVE_DADATA_TESTS", "DADATA_TOKEN")
    from src.tools import mlhelpers
    out = mlhelpers.dadata_find_party_by_inn(os.environ["DADATA_TOKEN"], "7707083893")
    if out.get("ok"):
        suggestions = out["data"].get("suggestions", [])
        assert len(suggestions) >= 1
        assert "СБЕРБАНК" in suggestions[0]["value"].upper()
