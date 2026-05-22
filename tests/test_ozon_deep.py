"""Deep unit tests for src/tools/ozon.py — every function, every status branch."""
import base64
import json
from unittest.mock import patch

import pytest


def _resp(body, status=200, headers=None):
    return (status, headers or {}, json.dumps(body).encode("utf-8"))


# ============================================================
# _request / _json_call — auth + status mapping
# ============================================================

def test_request_sends_client_id_and_api_key_headers():
    from src.tools import ozon
    captured = {}

    def fake_urlopen(req, timeout):
        captured["headers"] = dict(req.headers)
        from unittest.mock import MagicMock
        m = MagicMock()
        m.read.return_value = b"{}"
        m.status = 200
        m.headers = {}
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        ozon._request("/v3/product/list", "CLIENT99", "APIKEY", body={"x": 1})
    assert captured["headers"]["Client-id"] == "CLIENT99"
    assert captured["headers"]["Api-key"] == "APIKEY"
    assert captured["headers"]["Content-type"] == "application/json"


def test_request_get_method_has_no_body():
    from src.tools import ozon
    captured = {}

    def fake_urlopen(req, timeout):
        captured["data"] = req.data
        from unittest.mock import MagicMock
        m = MagicMock()
        m.read.return_value = b"{}"
        m.status = 200
        m.headers = {}
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        ozon._request("/p", "c", "k", method="GET")
    assert captured["data"] is None


def test_json_call_429_classified_rate_limit():
    from src.tools import ozon
    with patch.object(ozon, "_request", return_value=(429, {}, b"")):
        out = ozon._json_call("/p", "c", "k")
    assert out["error_kind"] == "rate_limit"


def test_json_call_403_classified_permission():
    from src.tools import ozon
    with patch.object(ozon, "_request", return_value=(403, {}, b'{"code":7}')):
        out = ozon._json_call("/p", "c", "k")
    assert out["error_kind"] == "permission"


def test_json_call_404_classified_not_found():
    from src.tools import ozon
    with patch.object(ozon, "_request", return_value=(404, {}, b"")):
        out = ozon._json_call("/p", "c", "k")
    assert out["error_kind"] == "not_found"


def test_json_call_400_classified_bad_input():
    from src.tools import ozon
    with patch.object(ozon, "_request", return_value=(400, {}, b'{"message":"bad"}')):
        out = ozon._json_call("/p", "c", "k")
    assert out["error_kind"] == "bad_input"


def test_json_call_500_classified_server():
    from src.tools import ozon
    with patch.object(ozon, "_request", return_value=(503, {}, b"")):
        out = ozon._json_call("/p", "c", "k")
    assert out["error_kind"] == "server"


def test_json_call_parses_ratelimit_headers():
    from src.tools import ozon
    with patch.object(ozon, "_request",
                      return_value=_resp({"r": 1}, headers={"X-RateLimit-Remaining": "42"})):
        out = ozon._json_call("/p", "c", "k")
    assert out["ok"] is True
    assert out["_meta"]["ratelimit"]["remaining"] == 42


def test_json_call_non_json_returns_bad_input():
    from src.tools import ozon
    with patch.object(ozon, "_request", return_value=(200, {}, b"plain text")):
        out = ozon._json_call("/p", "c", "k")
    assert out["ok"] is False
    assert out["error_kind"] == "bad_input"


# ============================================================
# check_credentials
# ============================================================

def test_check_credentials_marks_invalid_on_403():
    from src.tools import ozon
    with patch.object(ozon, "_request", return_value=(403, {}, b'{"code":7}')):
        out = ozon.check_credentials("c", "k")
    assert out["credentials_valid"] is False


def test_check_credentials_marks_valid_on_200():
    from src.tools import ozon
    with patch.object(ozon, "_request", return_value=_resp({"items": []})):
        out = ozon.check_credentials("c", "k")
    assert out["credentials_valid"] is True


def test_check_credentials_marks_invalid_on_500():
    """Server errors don't mean creds are bad, but check_credentials still flags
    the call as failed — agent should re-try later."""
    from src.tools import ozon
    with patch.object(ozon, "_request", return_value=(503, {}, b"")):
        out = ozon.check_credentials("c", "k")
    assert out["credentials_valid"] is False


def test_check_credentials_uses_product_info_list_endpoint():
    from src.tools import ozon
    captured = {}

    def fake(path, client_id, api_key, body=None, method="POST", timeout=60):
        captured["path"] = path
        captured["body"] = body
        return _resp({"items": []})

    with patch.object(ozon, "_request", side_effect=fake):
        ozon.check_credentials("c", "k")
    assert "/v3/product/info/list" in captured["path"]


# ============================================================
# stocks_fbo / stocks_fbs
# ============================================================

def test_stocks_fbo_pagination_cursor():
    from src.tools import ozon
    captured = {}

    def fake(path, c, k, body=None, **kw):
        captured["body"] = body
        return _resp({"result": {"items": []}})

    with patch.object(ozon, "_request", side_effect=fake):
        ozon.stocks_fbo("c", "k", limit=500, cursor="abc123")
    assert captured["body"]["limit"] == 500
    assert captured["body"]["cursor"] == "abc123"


def test_stocks_fbo_no_cursor_omits_field():
    from src.tools import ozon
    captured = {}

    def fake(path, c, k, body=None, **kw):
        captured["body"] = body
        return _resp({"r": 1})

    with patch.object(ozon, "_request", side_effect=fake):
        ozon.stocks_fbo("c", "k")
    assert "cursor" not in captured["body"]


def test_stocks_fbs_sends_sku_array():
    from src.tools import ozon
    captured = {}

    def fake(path, c, k, body=None, **kw):
        captured["body"] = body
        return _resp({"r": 1})

    with patch.object(ozon, "_request", side_effect=fake):
        ozon.stocks_fbs("c", "k", sku=["A", "B", "C"])
    assert captured["body"]["sku"] == ["A", "B", "C"]


def test_stocks_fbs_handles_none_sku():
    from src.tools import ozon
    captured = {}

    def fake(path, c, k, body=None, **kw):
        captured["body"] = body
        return _resp({"r": 1})

    with patch.object(ozon, "_request", side_effect=fake):
        ozon.stocks_fbs("c", "k")
    assert captured["body"]["sku"] == []


# ============================================================
# orders_fbo_list / orders_fbs_list
# ============================================================

def test_orders_fbo_list_filter_includes_dates():
    from src.tools import ozon
    captured = {}

    def fake(path, c, k, body=None, **kw):
        captured["body"] = body
        return _resp({"result": {"postings": []}})

    with patch.object(ozon, "_request", side_effect=fake):
        ozon.orders_fbo_list("c", "k", "2026-05-01T00:00:00Z", "2026-05-31T23:59:59Z")
    assert captured["body"]["filter"]["since"] == "2026-05-01T00:00:00Z"
    assert captured["body"]["filter"]["to"] == "2026-05-31T23:59:59Z"


def test_orders_fbs_list_status_filter_optional():
    from src.tools import ozon
    captured = {}

    def fake(path, c, k, body=None, **kw):
        captured["body"] = body
        return _resp({"r": 1})

    with patch.object(ozon, "_request", side_effect=fake):
        ozon.orders_fbs_list("c", "k", "2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z")
    assert "status" not in captured["body"]["filter"]


def test_orders_fbs_list_status_passed_through():
    from src.tools import ozon
    captured = {}

    def fake(path, c, k, body=None, **kw):
        captured["body"] = body
        return _resp({"r": 1})

    with patch.object(ozon, "_request", side_effect=fake):
        ozon.orders_fbs_list("c", "k", "2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z",
                             status="awaiting_deliver")
    assert captured["body"]["filter"]["status"] == "awaiting_deliver"


def test_orders_fbs_list_response_format_concise_skips_analytics():
    from src.tools import ozon
    captured = {}

    def fake(path, c, k, body=None, **kw):
        captured["body"] = body
        return _resp({"r": 1})

    with patch.object(ozon, "_request", side_effect=fake):
        ozon.orders_fbs_list("c", "k", "2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z",
                             response_format="concise")
    assert captured["body"]["with"]["analytics_data"] is False
    assert captured["body"]["with"]["financial_data"] is False


def test_orders_fbs_list_rejects_invalid_response_format():
    from src.tools import ozon
    with pytest.raises(ValueError):
        ozon.orders_fbs_list("c", "k", "2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z",
                             response_format="x")


# ============================================================
# returns_list / finance_realization / finance_transactions
# ============================================================

def test_returns_list_passes_filter_dates():
    from src.tools import ozon
    captured = {}

    def fake(path, c, k, body=None, **kw):
        captured["body"] = body
        return _resp({"r": []})

    with patch.object(ozon, "_request", side_effect=fake):
        ozon.returns_list("c", "k", "2026-05-01T00:00:00Z", "2026-05-31T00:00:00Z")
    assert captured["body"]["filter"]["return_date"]["time_from"] == "2026-05-01T00:00:00Z"


def test_finance_realization_year_month():
    from src.tools import ozon
    captured = {}

    def fake(path, c, k, body=None, **kw):
        captured["body"] = body
        return _resp({"r": []})

    with patch.object(ozon, "_request", side_effect=fake):
        ozon.finance_realization("c", "k", 2026, 5)
    assert captured["body"]["year"] == 2026
    assert captured["body"]["month"] == 5


def test_finance_transactions_default_operation_type_empty():
    from src.tools import ozon
    captured = {}

    def fake(path, c, k, body=None, **kw):
        captured["body"] = body
        return _resp({"r": []})

    with patch.object(ozon, "_request", side_effect=fake):
        ozon.finance_transactions("c", "k", "2026-05-01T00:00:00Z", "2026-05-31T00:00:00Z")
    assert captured["body"]["filter"]["operation_type"] == []


def test_finance_transactions_passes_operation_type_filter():
    from src.tools import ozon
    captured = {}

    def fake(path, c, k, body=None, **kw):
        captured["body"] = body
        return _resp({"r": []})

    with patch.object(ozon, "_request", side_effect=fake):
        ozon.finance_transactions(
            "c", "k", "2026-05-01T00:00:00Z", "2026-05-31T00:00:00Z",
            operation_type=["OperationAgentDeliveredToCustomer"],
        )
    assert captured["body"]["filter"]["operation_type"] == ["OperationAgentDeliveredToCustomer"]


def test_finance_transactions_pagination():
    from src.tools import ozon
    captured = {}

    def fake(path, c, k, body=None, **kw):
        captured["body"] = body
        return _resp({"r": []})

    with patch.object(ozon, "_request", side_effect=fake):
        ozon.finance_transactions("c", "k", "2026-05-01T00:00:00Z", "2026-05-31T00:00:00Z",
                                   page=3, page_size=500)
    assert captured["body"]["page"] == 3
    assert captured["body"]["page_size"] == 500


# ============================================================
# products_list / prices_list / warehouses_list / analytics_data
# ============================================================

def test_products_list_visibility_default_all():
    from src.tools import ozon
    captured = {}

    def fake(path, c, k, body=None, **kw):
        captured["body"] = body
        return _resp({"r": []})

    with patch.object(ozon, "_request", side_effect=fake):
        ozon.products_list("c", "k")
    assert captured["body"]["filter"]["visibility"] == "ALL"


def test_products_list_visibility_filter():
    from src.tools import ozon
    captured = {}

    def fake(path, c, k, body=None, **kw):
        captured["body"] = body
        return _resp({"r": []})

    with patch.object(ozon, "_request", side_effect=fake):
        ozon.products_list("c", "k", visibility="EMPTY_STOCK")
    assert captured["body"]["filter"]["visibility"] == "EMPTY_STOCK"


def test_products_list_last_id_cursor():
    from src.tools import ozon
    captured = {}

    def fake(path, c, k, body=None, **kw):
        captured["body"] = body
        return _resp({"r": []})

    with patch.object(ozon, "_request", side_effect=fake):
        ozon.products_list("c", "k", last_id="cursor-xyz")
    assert captured["body"]["last_id"] == "cursor-xyz"


def test_prices_list_endpoint_v4():
    from src.tools import ozon
    captured = {}

    def fake(path, c, k, body=None, **kw):
        captured["path"] = path
        return _resp({"r": []})

    with patch.object(ozon, "_request", side_effect=fake):
        ozon.prices_list("c", "k")
    assert "/v4/product/info/prices" in captured["path"]


def test_warehouses_list_no_body():
    """Warehouses endpoint takes no body."""
    from src.tools import ozon
    captured = {}

    def fake(path, c, k, body=None, **kw):
        captured["body"] = body
        return _resp({"result": []})

    with patch.object(ozon, "_request", side_effect=fake):
        ozon.warehouses_list("c", "k")
    # body=None acceptable; or body={} for POSTs
    assert captured["body"] in (None, {})


def test_analytics_data_defaults_metrics_and_dimension():
    from src.tools import ozon
    captured = {}

    def fake(path, c, k, body=None, **kw):
        captured["body"] = body
        return _resp({"r": []})

    with patch.object(ozon, "_request", side_effect=fake):
        ozon.analytics_data("c", "k", "2026-05-01", "2026-05-31")
    assert "revenue" in captured["body"]["metrics"]
    assert "sku" in captured["body"]["dimension"]


def test_analytics_data_explicit_metrics_dimension():
    from src.tools import ozon
    captured = {}

    def fake(path, c, k, body=None, **kw):
        captured["body"] = body
        return _resp({"r": []})

    with patch.object(ozon, "_request", side_effect=fake):
        ozon.analytics_data("c", "k", "2026-05-01", "2026-05-31",
                            metrics=["delivered_units"], dimension=["day"])
    assert captured["body"]["metrics"] == ["delivered_units"]
    assert captured["body"]["dimension"] == ["day"]
