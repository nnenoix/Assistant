"""Deep tests for src/tools/moysklad.py."""
import json
from unittest.mock import MagicMock, patch
import pytest


def _resp(body, status=200, headers=None):
    return (status, headers or {}, json.dumps(body).encode("utf-8"))


# ---------- _request + _call ----------

def test_request_bearer_auth():
    from src.tools import moysklad
    captured = {}

    def fake_urlopen(req, timeout):
        captured["headers"] = dict(req.headers)
        m = MagicMock()
        m.read.return_value = b"{}"
        m.status = 200
        m.headers = {}
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        moysklad._request("/entity/product", "MSTOK")
    assert captured["headers"]["Authorization"] == "Bearer MSTOK"


def test_request_accept_gzip_encoding():
    """МойСклад responses are large — we ask for gzip."""
    from src.tools import moysklad
    captured = {}

    def fake_urlopen(req, timeout):
        captured["headers"] = dict(req.headers)
        m = MagicMock()
        m.read.return_value = b"{}"
        m.status = 200
        m.headers = {}
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        moysklad._request("/p", "t")
    assert "gzip" in captured["headers"]["Accept-encoding"]


def test_call_429_rate_limit():
    from src.tools import moysklad
    with patch.object(moysklad, "_request", return_value=(429, {}, b"")):
        out = moysklad._call("/p", "t")
    assert out["error_kind"] == "rate_limit"


def test_call_403_permission():
    from src.tools import moysklad
    with patch.object(moysklad, "_request", return_value=(403, {}, b'{"errors":[]}')):
        out = moysklad._call("/p", "t")
    assert out["error_kind"] == "permission"


def test_call_parses_ratelimit_headers():
    from src.tools import moysklad
    headers = {"X-RateLimit-Remaining": "50", "X-Lognex-Retry-TimeInterval": "1500"}
    with patch.object(moysklad, "_request", return_value=(200, headers, b'{"r":1}')):
        out = moysklad._call("/p", "t")
    # Both headers should be in ratelimit dict (lowercased keys)
    assert "x-ratelimit-remaining" in out["_meta"]["ratelimit"]
    assert out["_meta"]["ratelimit"]["x-ratelimit-remaining"] == 50


# ---------- entity lists ----------

def test_products_list_filter_dsl_passes_through():
    from src.tools import moysklad
    captured = {}

    def fake(path, t, **kw):
        captured["params"] = kw.get("params")
        return _resp({"rows": []})

    with patch.object(moysklad, "_request", side_effect=fake):
        moysklad.products_list("t", filter_str="name~Шланг;archived=false")
    assert captured["params"]["filter"] == "name~Шланг;archived=false"


def test_products_list_omits_filter_when_none():
    from src.tools import moysklad
    captured = {}

    def fake(path, t, **kw):
        captured["params"] = kw.get("params")
        return _resp({"rows": []})

    with patch.object(moysklad, "_request", side_effect=fake):
        moysklad.products_list("t")
    assert "filter" not in (captured["params"] or {})


def test_variants_list_path():
    from src.tools import moysklad
    captured = {}

    def fake(path, t, **kw):
        captured["path"] = path
        return _resp({"rows": []})

    with patch.object(moysklad, "_request", side_effect=fake):
        moysklad.variants_list("t")
    assert captured["path"] == "/entity/variant"


def test_services_list_path():
    from src.tools import moysklad
    captured = {}

    def fake(path, t, **kw):
        captured["path"] = path
        return _resp({"rows": []})

    with patch.object(moysklad, "_request", side_effect=fake):
        moysklad.services_list("t")
    assert captured["path"] == "/entity/service"


def test_counterparties_list_filter():
    from src.tools import moysklad
    captured = {}

    def fake(path, t, **kw):
        captured["params"] = kw.get("params")
        return _resp({"rows": []})

    with patch.object(moysklad, "_request", side_effect=fake):
        moysklad.counterparties_list("t", filter_str="inn=7707083893")
    assert captured["params"]["filter"] == "inn=7707083893"


def test_stores_list_no_params():
    from src.tools import moysklad
    captured = {}

    def fake(path, t, **kw):
        captured["path"] = path
        captured["params"] = kw.get("params")
        return _resp({"rows": []})

    with patch.object(moysklad, "_request", side_effect=fake):
        moysklad.stores_list("t")
    assert captured["path"] == "/entity/store"
    assert captured["params"] is None


def test_organizations_list_no_params():
    from src.tools import moysklad
    captured = {}

    def fake(path, t, **kw):
        captured["path"] = path
        return _resp({"rows": []})

    with patch.object(moysklad, "_request", side_effect=fake):
        moysklad.organizations_list("t")
    assert captured["path"] == "/entity/organization"


# ---------- customerorders / demands / supplies ----------

def test_customerorders_list_builds_moment_filter():
    from src.tools import moysklad
    captured = {}

    def fake(path, t, **kw):
        captured["params"] = kw.get("params")
        return _resp({"rows": []})

    with patch.object(moysklad, "_request", side_effect=fake):
        moysklad.customerorders_list("t",
                                     moment_from="2026-05-01 00:00:00",
                                     moment_to="2026-05-31 23:59:59")
    assert "moment>=2026-05-01 00:00:00" in captured["params"]["filter"]
    assert "moment<=2026-05-31 23:59:59" in captured["params"]["filter"]


def test_customerorders_list_no_filter_when_no_dates():
    from src.tools import moysklad
    captured = {}

    def fake(path, t, **kw):
        captured["params"] = kw.get("params")
        return _resp({"rows": []})

    with patch.object(moysklad, "_request", side_effect=fake):
        moysklad.customerorders_list("t")
    assert "filter" not in captured["params"]


def test_demands_list_path():
    from src.tools import moysklad
    captured = {}

    def fake(path, t, **kw):
        captured["path"] = path
        return _resp({"rows": []})

    with patch.object(moysklad, "_request", side_effect=fake):
        moysklad.demands_list("t")
    assert captured["path"] == "/entity/demand"


def test_supplies_list_path():
    from src.tools import moysklad
    captured = {}

    def fake(path, t, **kw):
        captured["path"] = path
        return _resp({"rows": []})

    with patch.object(moysklad, "_request", side_effect=fake):
        moysklad.supplies_list("t")
    assert captured["path"] == "/entity/supply"


# ---------- reports ----------

def test_stock_all_uses_report_endpoint():
    from src.tools import moysklad
    captured = {}

    def fake(path, t, **kw):
        captured["path"] = path
        return _resp({"rows": []})

    with patch.object(moysklad, "_request", side_effect=fake):
        moysklad.stock_all("t")
    assert captured["path"] == "/report/stock/all"


def test_stock_bystore_filter_store_id():
    from src.tools import moysklad
    captured = {}

    def fake(path, t, **kw):
        captured["params"] = kw.get("params")
        return _resp({"rows": []})

    with patch.object(moysklad, "_request", side_effect=fake):
        moysklad.stock_bystore("t", "STORE-UUID-1")
    assert "store=STORE-UUID-1" in captured["params"]["filter"]


def test_cashflow_report_period_params():
    from src.tools import moysklad
    captured = {}

    def fake(path, t, **kw):
        captured["params"] = kw.get("params")
        return _resp({"series": []})

    with patch.object(moysklad, "_request", side_effect=fake):
        moysklad.cashflow_report("t", "2026-05-01 00:00:00", "2026-05-31 23:59:59")
    assert captured["params"]["momentFrom"] == "2026-05-01 00:00:00"
    assert captured["params"]["momentTo"] == "2026-05-31 23:59:59"
    assert captured["params"]["interval"] == "day"


def test_profit_byproduct_endpoint():
    from src.tools import moysklad
    captured = {}

    def fake(path, t, **kw):
        captured["path"] = path
        captured["params"] = kw.get("params")
        return _resp({"rows": []})

    with patch.object(moysklad, "_request", side_effect=fake):
        moysklad.profit_byproduct("t", "2026-05-01 00:00:00", "2026-05-31 23:59:59")
    assert captured["path"] == "/report/profit/byproduct"
    assert captured["params"]["momentFrom"] == "2026-05-01 00:00:00"


def test_expenses_list_uses_cashout_entity():
    from src.tools import moysklad
    captured = {}

    def fake(path, t, **kw):
        captured["path"] = path
        return _resp({"rows": []})

    with patch.object(moysklad, "_request", side_effect=fake):
        moysklad.expenses_list("t")
    assert captured["path"] == "/entity/cashout"


# ---------- pagination ----------

def test_products_list_limit_offset_params():
    from src.tools import moysklad
    captured = {}

    def fake(path, t, **kw):
        captured["params"] = kw.get("params")
        return _resp({"rows": []})

    with patch.object(moysklad, "_request", side_effect=fake):
        moysklad.products_list("t", limit=500, offset=1000)
    assert captured["params"]["limit"] == 500
    assert captured["params"]["offset"] == 1000


def test_call_handles_non_json_response():
    from src.tools import moysklad
    with patch.object(moysklad, "_request", return_value=(200, {}, b"<html>")):
        out = moysklad._call("/p", "t")
    assert out["ok"] is False
    assert out["error_kind"] == "bad_input"


def test_call_5xx_classified_server():
    from src.tools import moysklad
    with patch.object(moysklad, "_request", return_value=(503, {}, b"")):
        out = moysklad._call("/p", "t")
    assert out["error_kind"] == "server"
