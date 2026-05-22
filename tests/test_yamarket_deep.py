"""Deep tests for src/tools/yamarket.py."""
import json
from unittest.mock import patch
import pytest


def _resp(body, status=200):
    return (status, {}, json.dumps(body).encode("utf-8"))


def test_request_includes_api_key_header():
    from src.tools import yamarket
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
        yamarket._request("/campaigns", "MYKEY")
    assert captured["headers"]["Api-key"] == "MYKEY"


def test_request_serializes_body_only_when_present():
    from src.tools import yamarket
    captured = {}

    def fake_urlopen(req, timeout):
        captured["data"] = req.data
        captured["ct"] = dict(req.headers).get("Content-type")
        from unittest.mock import MagicMock
        m = MagicMock()
        m.read.return_value = b"{}"
        m.status = 200
        m.headers = {}
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        yamarket._request("/p", "k", body={"x": 1}, method="POST")
    assert captured["data"] == b'{"x": 1}'
    assert captured["ct"] == "application/json"


def test_json_call_429_rate_limit():
    from src.tools import yamarket
    with patch.object(yamarket, "_request", return_value=(429, {}, b"")):
        out = yamarket._json_call("/p", "k")
    assert out["error_kind"] == "rate_limit"


def test_json_call_401_permission():
    from src.tools import yamarket
    with patch.object(yamarket, "_request", return_value=(401, {}, b'{"errors":[]}')):
        out = yamarket._json_call("/p", "k")
    assert out["error_kind"] == "permission"


def test_campaigns_list_no_params():
    from src.tools import yamarket
    captured = {}

    def fake(path, k, params=None, body=None, method="GET", timeout=60):
        captured["path"] = path
        captured["method"] = method
        return _resp({"campaigns": []})

    with patch.object(yamarket, "_request", side_effect=fake):
        yamarket.campaigns_list("k")
    assert captured["path"] == "/campaigns"
    assert captured["method"] == "GET"


def test_businesses_list_endpoint():
    from src.tools import yamarket
    captured = {}

    def fake(path, k, **kw):
        captured["path"] = path
        return _resp({"businesses": []})

    with patch.object(yamarket, "_request", side_effect=fake):
        yamarket.businesses_list("k")
    assert captured["path"] == "/businesses"


def test_stocks_list_uses_post():
    from src.tools import yamarket
    captured = {}

    def fake(path, k, params=None, body=None, method="GET", timeout=60):
        captured["method"] = method
        return _resp({"result": []})

    with patch.object(yamarket, "_request", side_effect=fake):
        yamarket.stocks_list("k", 12345)
    assert captured["method"] == "POST"


def test_stocks_list_with_turnover_flag():
    from src.tools import yamarket
    captured = {}

    def fake(path, k, params=None, body=None, **kw):
        captured["body"] = body
        return _resp({"r": []})

    with patch.object(yamarket, "_request", side_effect=fake):
        yamarket.stocks_list("k", 12345, with_turnover=True)
    assert captured["body"]["withTurnover"] is True


def test_stocks_list_page_token_pagination():
    from src.tools import yamarket
    captured = {}

    def fake(path, k, params=None, **kw):
        captured["params"] = params
        return _resp({"r": []})

    with patch.object(yamarket, "_request", side_effect=fake):
        yamarket.stocks_list("k", 100, page_token="cursor-abc")
    assert captured["params"]["page_token"] == "cursor-abc"


def test_orders_list_date_format_dd_mm_yyyy():
    """Yandex Market quirk: dates as DD-MM-YYYY."""
    from src.tools import yamarket
    captured = {}

    def fake(path, k, params=None, **kw):
        captured["params"] = params
        return _resp({"orders": []})

    with patch.object(yamarket, "_request", side_effect=fake):
        yamarket.orders_list("k", 100, "01-05-2026", "31-05-2026")
    assert captured["params"]["fromDate"] == "01-05-2026"
    assert captured["params"]["toDate"] == "31-05-2026"


def test_orders_list_status_filter_optional():
    from src.tools import yamarket
    captured = {}

    def fake(path, k, params=None, **kw):
        captured["params"] = params
        return _resp({"orders": []})

    with patch.object(yamarket, "_request", side_effect=fake):
        yamarket.orders_list("k", 100, "01-05-2026", "31-05-2026", status="DELIVERED")
    assert captured["params"]["status"] == "DELIVERED"


def test_orders_list_omits_status_when_none():
    from src.tools import yamarket
    captured = {}

    def fake(path, k, params=None, **kw):
        captured["params"] = params
        return _resp({"r": []})

    with patch.object(yamarket, "_request", side_effect=fake):
        yamarket.orders_list("k", 100, "01-05-2026", "31-05-2026")
    assert "status" not in captured["params"]


def test_order_get_uses_path_with_id():
    from src.tools import yamarket
    captured = {}

    def fake(path, k, **kw):
        captured["path"] = path
        return _resp({"order": {}})

    with patch.object(yamarket, "_request", side_effect=fake):
        yamarket.order_get("k", 100, 7777)
    assert "/orders/7777" in captured["path"]


def test_returns_list_page_token():
    from src.tools import yamarket
    captured = {}

    def fake(path, k, params=None, **kw):
        captured["params"] = params
        return _resp({"returns": []})

    with patch.object(yamarket, "_request", side_effect=fake):
        yamarket.returns_list("k", 100, "01-05-2026", "31-05-2026", page_token="X")
    assert captured["params"]["page_token"] == "X"


def test_prices_list_uses_post_with_empty_body():
    from src.tools import yamarket
    captured = {}

    def fake(path, k, params=None, body=None, method="GET", **kw):
        captured["method"] = method
        captured["body"] = body
        return _resp({"prices": []})

    with patch.object(yamarket, "_request", side_effect=fake):
        yamarket.prices_list("k", 100)
    assert captured["method"] == "POST"
    assert captured["body"] == {}


def test_offers_list_business_scope():
    from src.tools import yamarket
    captured = {}

    def fake(path, k, **kw):
        captured["path"] = path
        return _resp({"offers": []})

    with patch.object(yamarket, "_request", side_effect=fake):
        yamarket.offers_list("k", 555)
    assert "/businesses/555/offer-mappings" in captured["path"]


def test_warehouses_list_business_scope():
    from src.tools import yamarket
    captured = {}

    def fake(path, k, **kw):
        captured["path"] = path
        return _resp({"warehouses": []})

    with patch.object(yamarket, "_request", side_effect=fake):
        yamarket.warehouses_list("k", 555)
    assert "/businesses/555/warehouses" in captured["path"]


def test_json_call_parses_ratelimit_headers():
    from src.tools import yamarket
    with patch.object(yamarket, "_request",
                      return_value=(200, {"X-Ratelimit-Limit": "100",
                                          "X-Ratelimit-Remaining": "99"},
                                    b'{"r":1}')):
        out = yamarket._json_call("/p", "k")
    assert out["_meta"]["ratelimit"]["limit"] == 100
    assert out["_meta"]["ratelimit"]["remaining"] == 99


def test_json_call_handles_non_json_response():
    from src.tools import yamarket
    with patch.object(yamarket, "_request", return_value=(200, {}, b"<html>500</html>")):
        out = yamarket._json_call("/p", "k")
    assert out["ok"] is False
    assert out["error_kind"] == "bad_input"


def test_json_call_500_classified_server():
    from src.tools import yamarket
    with patch.object(yamarket, "_request", return_value=(503, {}, b"")):
        out = yamarket._json_call("/p", "k")
    assert out["error_kind"] == "server"


def test_json_call_404_classified_not_found():
    from src.tools import yamarket
    with patch.object(yamarket, "_request", return_value=(404, {}, b"")):
        out = yamarket._json_call("/p", "k")
    assert out["error_kind"] == "not_found"


def test_offers_list_pagination_default_limit_200():
    from src.tools import yamarket
    captured = {}

    def fake(path, k, params=None, **kw):
        captured["params"] = params
        return _resp({"r": []})

    with patch.object(yamarket, "_request", side_effect=fake):
        yamarket.offers_list("k", 555)
    assert captured["params"]["limit"] == 200


def test_returns_list_default_limit_50():
    from src.tools import yamarket
    captured = {}

    def fake(path, k, params=None, **kw):
        captured["params"] = params
        return _resp({"r": []})

    with patch.object(yamarket, "_request", side_effect=fake):
        yamarket.returns_list("k", 100, "01-05-2026", "31-05-2026")
    assert captured["params"]["limit"] == 50
