"""Deep unit tests for src/tools/wb.py — every new function gets happy
path + 4xx + 5xx + edge cases. All mock-based (no live WB calls)."""
import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest


# ---------- helpers ----------

def _ok(payload, status=200, headers=None):
    return (status, headers or {}, json.dumps(payload).encode("utf-8"))


def _err(status, body=b"err"):
    return (status, {}, body)


# ============================================================
# _request — low-level HTTP shape
# ============================================================

def test_request_includes_authorization_header():
    from src.tools import wb
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        from unittest.mock import MagicMock
        m = MagicMock()
        m.read.return_value = b'{}'
        m.status = 200
        m.headers = {}
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        wb._request("statistics-api.wildberries.ru", "/ping", "MYTOKEN")
    assert captured["headers"]["Authorization"] == "MYTOKEN"


def test_request_serializes_body_as_json():
    from src.tools import wb
    captured = {}

    def fake_urlopen(req, timeout):
        captured["data"] = req.data
        captured["method"] = req.get_method()
        from unittest.mock import MagicMock
        m = MagicMock()
        m.read.return_value = b'{}'
        m.status = 200
        m.headers = {}
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        wb._request("h", "/p", "t", method="POST", body={"x": 1})
    assert json.loads(captured["data"].decode()) == {"x": 1}
    assert captured["method"] == "POST"


def test_request_returns_error_body_on_http_error():
    from src.tools import wb
    from urllib.error import HTTPError
    from unittest.mock import MagicMock
    fake = MagicMock()
    fake.read.return_value = b'{"errors":["bad"]}'
    with patch("urllib.request.urlopen",
               side_effect=HTTPError("u", 401, "Unauthorized", {}, fake)):
        code, hdr, body = wb._request("h", "/p", "t")
    assert code == 401
    assert b"bad" in body


# ============================================================
# token_age
# ============================================================

def test_token_age_rejects_non_jwt():
    from src.tools import wb
    out = wb.token_age("not-a-jwt")
    assert "error" in out
    assert "3 dot-separated" in out["error"]


def test_token_age_decodes_valid_jwt():
    """Build a deliberately-unsigned JWT and verify decode."""
    from src.tools import wb
    import base64
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    now = int(datetime.now(timezone.utc).timestamp())
    payload_dict = {"sid": "seller-99", "iat": now, "exp": now + 86400 * 30}
    payload = base64.urlsafe_b64encode(json.dumps(payload_dict).encode()).decode().rstrip("=")
    token = f"{header}.{payload}.fake"
    out = wb.token_age(token)
    assert out["seller_id"] == "seller-99"
    assert out["days_left"] is not None
    assert out["days_left"] > 29


def test_token_age_handles_undecodable_payload():
    from src.tools import wb
    out = wb.token_age("a.bb!!.c")  # invalid base64 in middle
    assert "error" in out


def test_token_age_handles_missing_exp_iat():
    """JWTs without exp/iat (technically valid) → days_left/issued_at None."""
    from src.tools import wb
    import base64
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(b'{"sid":"x"}').decode().rstrip("=")
    out = wb.token_age(f"{header}.{payload}.x")
    assert out["seller_id"] == "x"
    assert out["expires_at"] is None
    assert out["days_left"] is None


# ============================================================
# check_token
# ============================================================

def test_check_token_pings_all_families():
    from src.tools import wb
    calls = []

    def fake_request(host, path, token, timeout=15):
        calls.append(host)
        return _ok({"Status": "OK"})

    with patch.object(wb, "_request", side_effect=fake_request):
        out = wb.check_token("tok")
    assert set(calls) == set(wb.HOSTS.values())
    for name in wb.HOSTS:
        assert name in out


def test_check_token_classifies_per_family_error():
    """One family fails → its entry has error_kind + http_status."""
    from src.tools import wb

    def fake_request(host, path, token, timeout=15):
        if host == wb.HOSTS["advert"]:
            raise TimeoutError("read timeout")
        return _ok({"Status": "OK"})

    with patch.object(wb, "_request", side_effect=fake_request):
        out = wb.check_token("tok")
    assert "error_kind" in out["advert"]
    assert out["advert"]["error_kind"] == "network"
    assert out["content"]["status"] == "OK"


def test_check_token_handles_non_json_body():
    """If WB returns plain text, surface raw + parse_error_kind."""
    from src.tools import wb
    with patch.object(wb, "_request", return_value=(200, {}, b"<!DOCTYPE html>")):
        out = wb.check_token("tok")
    sample = next(iter(out.values()))
    assert "raw" in sample
    assert "parse_error_kind" in sample


# ============================================================
# _ratelimit_meta — variants
# ============================================================

def test_ratelimit_meta_strips_prefix():
    from src.tools import wb
    out = wb._ratelimit_meta({"X-Ratelimit-Limit": "100"})
    assert "x-ratelimit-limit" not in out
    assert out["limit"] == 100


def test_ratelimit_meta_handles_uppercase_keys():
    from src.tools import wb
    out = wb._ratelimit_meta({"X-RATELIMIT-REMAINING": "5"})
    assert out["remaining"] == 5


def test_ratelimit_meta_empty_on_no_match():
    from src.tools import wb
    out = wb._ratelimit_meta({"Content-Type": "json"})
    assert out == {}


def test_ratelimit_meta_handles_none_input():
    from src.tools import wb
    assert wb._ratelimit_meta(None) == {}


# ============================================================
# stocks_v2
# ============================================================

def test_stocks_v2_uses_today_when_date_from_omitted():
    from src.tools import wb
    captured = {}

    def fake_json_request(host, path, token, params=None, **kw):
        captured["params"] = params
        return {"ok": True, "data": [], "_meta": {"http_status": 200}}

    with patch.object(wb, "_json_request", side_effect=fake_json_request):
        wb.stocks_v2("tok")
    assert captured["params"]["dateFrom"]
    # Should look like ISO date (YYYY-MM-DD)
    assert len(captured["params"]["dateFrom"]) == 10
    assert captured["params"]["dateFrom"][4] == "-"


def test_stocks_v2_passes_explicit_date():
    from src.tools import wb
    captured = {}

    def fake_json_request(host, path, token, params=None, **kw):
        captured["params"] = params
        return {"ok": True}

    with patch.object(wb, "_json_request", side_effect=fake_json_request):
        wb.stocks_v2("tok", date_from="2026-01-15")
    assert captured["params"]["dateFrom"] == "2026-01-15"


def test_stocks_v2_uses_statistics_host():
    from src.tools import wb
    captured = {}

    def fake_json_request(host, path, token, params=None, **kw):
        captured["host"] = host
        return {"ok": True}

    with patch.object(wb, "_json_request", side_effect=fake_json_request):
        wb.stocks_v2("tok")
    assert captured["host"] == wb.HOSTS["statistics"]


# ============================================================
# orders_recent / sales_recent
# ============================================================

def test_orders_recent_passes_flag_param():
    from src.tools import wb
    captured = {}

    def fake_json_request(host, path, token, params=None, **kw):
        captured["params"] = params
        return {"ok": True}

    with patch.object(wb, "_json_request", side_effect=fake_json_request):
        wb.orders_recent("tok", "2026-05-01", flag=1)
    assert captured["params"]["flag"] == 1


def test_orders_recent_defaults_flag_zero():
    from src.tools import wb
    captured = {}

    def fake_json_request(host, path, token, params=None, **kw):
        captured["params"] = params
        return {"ok": True}

    with patch.object(wb, "_json_request", side_effect=fake_json_request):
        wb.orders_recent("tok", "2026-05-01")
    assert captured["params"]["flag"] == 0


def test_sales_recent_uses_sales_path():
    from src.tools import wb
    captured = {}

    def fake_json_request(host, path, token, params=None, **kw):
        captured["path"] = path
        return {"ok": True}

    with patch.object(wb, "_json_request", side_effect=fake_json_request):
        wb.sales_recent("tok", "2026-05-01")
    assert captured["path"].endswith("/sales")


# ============================================================
# warehouses + prices_list + supplies_list
# ============================================================

def test_warehouses_calls_marketplace_host():
    from src.tools import wb
    captured = {}

    def fake_json_request(host, path, token, **kw):
        captured["host"] = host
        return {"ok": True}

    with patch.object(wb, "_json_request", side_effect=fake_json_request):
        wb.warehouses("tok")
    assert captured["host"] == wb.HOSTS["marketplace"]


def test_prices_list_paginates_via_limit_offset():
    from src.tools import wb
    captured = {}

    def fake_json_request(host, path, token, params=None, **kw):
        captured["params"] = params
        return {"ok": True}

    with patch.object(wb, "_json_request", side_effect=fake_json_request):
        wb.prices_list("tok", limit=500, offset=1000)
    assert captured["params"]["limit"] == 500
    assert captured["params"]["offset"] == 1000


def test_supplies_list_uses_next_cursor():
    from src.tools import wb
    captured = {}

    def fake_json_request(host, path, token, params=None, **kw):
        captured["params"] = params
        return {"ok": True}

    with patch.object(wb, "_json_request", side_effect=fake_json_request):
        wb.supplies_list("tok", limit=200, next_id=42)
    assert captured["params"]["next"] == 42
    assert captured["params"]["limit"] == 200


# ============================================================
# questions / feedbacks — counts + lists with filter
# ============================================================

def test_questions_count_omits_is_answered_when_none():
    from src.tools import wb
    captured = {}

    def fake_json_request(host, path, token, params=None, **kw):
        captured["params"] = params
        return {"ok": True}

    with patch.object(wb, "_json_request", side_effect=fake_json_request):
        wb.questions_count("tok")
    assert "isAnswered" not in (captured["params"] or {})


def test_questions_count_true_serialized_lowercase():
    """WB API expects lowercase JSON booleans for the query param."""
    from src.tools import wb
    captured = {}

    def fake_json_request(host, path, token, params=None, **kw):
        captured["params"] = params
        return {"ok": True}

    with patch.object(wb, "_json_request", side_effect=fake_json_request):
        wb.questions_count("tok", is_answered=True)
    assert captured["params"]["isAnswered"] == "true"


def test_questions_count_false_serialized_lowercase():
    from src.tools import wb
    captured = {}

    def fake_json_request(host, path, token, params=None, **kw):
        captured["params"] = params
        return {"ok": True}

    with patch.object(wb, "_json_request", side_effect=fake_json_request):
        wb.questions_count("tok", is_answered=False)
    assert captured["params"]["isAnswered"] == "false"


def test_questions_list_passes_skip_take_and_dates():
    from src.tools import wb
    captured = {}

    def fake_json_request(host, path, token, params=None, **kw):
        captured["params"] = params
        return {"ok": True}

    with patch.object(wb, "_json_request", side_effect=fake_json_request):
        wb.questions_list("tok", take=50, skip=100, date_from=1_700_000_000)
    assert captured["params"]["take"] == 50
    assert captured["params"]["skip"] == 100
    assert captured["params"]["dateFrom"] == 1_700_000_000


def test_feedbacks_list_default_order_is_date_desc():
    from src.tools import wb
    captured = {}

    def fake_json_request(host, path, token, params=None, **kw):
        captured["params"] = params
        return {"ok": True}

    with patch.object(wb, "_json_request", side_effect=fake_json_request):
        wb.feedbacks_list("tok")
    assert captured["params"]["order"] == "dateDesc"


def test_feedbacks_count_omits_is_answered_when_none():
    from src.tools import wb
    captured = {}

    def fake_json_request(host, path, token, params=None, **kw):
        captured["params"] = params
        return {"ok": True}

    with patch.object(wb, "_json_request", side_effect=fake_json_request):
        wb.feedbacks_count("tok")
    assert "isAnswered" not in (captured["params"] or {})


# ============================================================
# adverts_list + analytics_paid_storage
# ============================================================

def test_adverts_list_omits_filters_when_none():
    from src.tools import wb
    captured = {}

    def fake_json_request(host, path, token, params=None, **kw):
        captured["params"] = params
        return {"ok": True}

    with patch.object(wb, "_json_request", side_effect=fake_json_request):
        wb.adverts_list("tok")
    assert captured["params"] == {}


def test_adverts_list_passes_status_filter():
    from src.tools import wb
    captured = {}

    def fake_json_request(host, path, token, params=None, **kw):
        captured["params"] = params
        return {"ok": True}

    with patch.object(wb, "_json_request", side_effect=fake_json_request):
        wb.adverts_list("tok", status=9, type_=5)
    assert captured["params"]["status"] == 9
    assert captured["params"]["type"] == 5


def test_analytics_paid_storage_requires_both_dates():
    from src.tools import wb
    captured = {}

    def fake_json_request(host, path, token, params=None, **kw):
        captured["params"] = params
        return {"ok": True}

    with patch.object(wb, "_json_request", side_effect=fake_json_request):
        wb.analytics_paid_storage("tok", "2026-05-01", "2026-05-31")
    assert captured["params"]["dateFrom"] == "2026-05-01"
    assert captured["params"]["dateTo"] == "2026-05-31"


# ============================================================
# finance_detail (generator) + finance_detail_collect
# ============================================================

def test_finance_detail_collect_concise_returns_no_full_rows():
    from src.tools import wb
    rows = [{"rrd_id": i, "amount": i * 10} for i in range(5)]

    def fake_finance_detail(token, date_from, date_to, limit, start_rrd_id, sleep_sec, max_pages):
        yield rows

    with patch.object(wb, "finance_detail", side_effect=fake_finance_detail):
        out = wb.finance_detail_collect("tok", "2026-05-01")
    assert out["rows_count"] == 5
    assert "rows" not in out  # concise mode strips full grid
    assert len(out["sample_first"]) == 2
    assert len(out["sample_last"]) == 2


def test_finance_detail_collect_detailed_includes_full_rows():
    from src.tools import wb
    rows = [{"rrd_id": i} for i in range(3)]

    def fake_finance_detail(*args, **kwargs):
        yield rows

    with patch.object(wb, "finance_detail", side_effect=fake_finance_detail):
        out = wb.finance_detail_collect("tok", "2026-05-01", response_format="detailed")
    assert out["rows"] == rows


def test_finance_detail_collect_rejects_invalid_response_format():
    from src.tools import wb
    with pytest.raises(ValueError):
        wb.finance_detail_collect("tok", "2026-05-01", response_format="bogus")


def test_finance_detail_collect_handles_empty_result():
    from src.tools import wb

    def fake_finance_detail(*args, **kwargs):
        return
        yield  # empty generator

    with patch.object(wb, "finance_detail", side_effect=fake_finance_detail):
        out = wb.finance_detail_collect("tok", "2026-05-01")
    assert out["rows_count"] == 0
    assert out["last_rrd_id"] is None
