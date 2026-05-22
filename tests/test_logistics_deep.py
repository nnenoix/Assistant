"""Deep tests for src/tools/logistics.py — СДЭК + Boxberry + Почта России."""
import base64
import json
from unittest.mock import MagicMock, patch


def _resp(body, status=200):
    return (status, {}, json.dumps(body).encode("utf-8"))


# ============================================================
# СДЭК
# ============================================================

def test_cdek_auth_posts_form_urlencoded():
    from src.tools import logistics
    captured = {}

    def fake_urlopen(req, timeout):
        captured["data"] = req.data
        captured["ct"] = dict(req.headers).get("Content-type")
        m = MagicMock()
        m.read.return_value = b'{"access_token":"X","expires_in":3600}'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        out = logistics.cdek_auth("ACCT", "SECR")
    body_str = captured["data"].decode()
    assert "grant_type=client_credentials" in body_str
    assert "client_id=ACCT" in body_str
    assert "client_secret=SECR" in body_str
    assert captured["ct"] == "application/x-www-form-urlencoded"
    assert out["ok"] is True
    assert out["data"]["access_token"] == "X"


def test_cdek_auth_handles_401():
    from src.tools import logistics
    from urllib.error import HTTPError
    fake = MagicMock()
    fake.read.return_value = b'{"error":"invalid_client"}'
    with patch("urllib.request.urlopen",
               side_effect=HTTPError("u", 401, "Unauthorized", {}, fake)):
        out = logistics.cdek_auth("bad", "creds")
    assert out["ok"] is False
    assert out["error_kind"] == "permission"


def test_cdek_request_bearer_token():
    from src.tools import logistics
    captured = {}

    def fake_urlopen(req, timeout):
        captured["headers"] = dict(req.headers)
        m = MagicMock()
        m.read.return_value = b"{}"
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        logistics._cdek_request("/orders", "TOKEN", method="GET")
    assert captured["headers"]["Authorization"] == "Bearer TOKEN"


def test_cdek_orders_list_dates_to_params():
    from src.tools import logistics
    captured = {}

    def fake(path, token, **kw):
        captured["params"] = kw.get("params")
        return (200, {}, b'{"items":[]}')

    with patch.object(logistics, "_cdek_request", side_effect=fake):
        logistics.cdek_orders_list("T", date_from="2026-05-01T00:00:00Z",
                                    date_to="2026-05-31T00:00:00Z", limit=100)
    assert captured["params"]["date_first"] == "2026-05-01T00:00:00Z"
    assert captured["params"]["date_last"] == "2026-05-31T00:00:00Z"
    assert captured["params"]["size"] == 100


def test_cdek_orders_list_page_from_offset():
    """СДЭК paginates by page numbers, derived from offset // limit."""
    from src.tools import logistics
    captured = {}

    def fake(path, token, **kw):
        captured["params"] = kw.get("params")
        return (200, {}, b'{"items":[]}')

    with patch.object(logistics, "_cdek_request", side_effect=fake):
        logistics.cdek_orders_list("T", limit=50, offset=150)
    assert captured["params"]["page"] == 3


def test_cdek_order_get_uses_uuid_in_path():
    from src.tools import logistics
    captured = {}

    def fake(path, token, **kw):
        captured["path"] = path
        return (200, {}, b'{"order":{}}')

    with patch.object(logistics, "_cdek_request", side_effect=fake):
        logistics.cdek_order_get("T", "UUID-99")
    assert "/orders/UUID-99" in captured["path"]


def test_cdek_calculator_default_tariff_136():
    from src.tools import logistics
    captured = {}

    def fake(path, token, **kw):
        captured["body"] = kw.get("body")
        return (200, {}, b'{"total_sum":500}')

    with patch.object(logistics, "_cdek_request", side_effect=fake):
        logistics.cdek_calculator("T", 270, 44, weight_g=2500)
    body = captured["body"]
    assert body["tariff_code"] == 136
    assert body["from_location"]["code"] == 270
    assert body["to_location"]["code"] == 44
    assert body["packages"][0]["weight"] == 2500


def test_cdek_locations_search_country_default_ru():
    from src.tools import logistics
    captured = {}

    def fake(path, token, **kw):
        captured["params"] = kw.get("params")
        return (200, {}, b"[]")

    with patch.object(logistics, "_cdek_request", side_effect=fake):
        logistics.cdek_locations_search("T", "Москва")
    assert captured["params"]["city"] == "Москва"
    assert captured["params"]["country_codes"] == "RU"


def test_cdek_call_429_rate_limit():
    from src.tools import logistics
    with patch.object(logistics, "_cdek_request", return_value=(429, {}, b"")):
        out = logistics._cdek_call("/p", "T")
    assert out["error_kind"] == "rate_limit"


def test_cdek_call_404_not_found():
    from src.tools import logistics
    with patch.object(logistics, "_cdek_request", return_value=(404, {}, b"")):
        out = logistics._cdek_call("/p", "T")
    assert out["error_kind"] == "not_found"


# ============================================================
# Boxberry
# ============================================================

def test_boxberry_request_uses_token_and_method_in_url():
    """Boxberry shoves both token + method name into the URL."""
    from src.tools import logistics
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        m = MagicMock()
        m.read.return_value = b"[]"
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        logistics._bb_request("ListParcels", "MYTOKEN")
    assert "token=MYTOKEN" in captured["url"]
    assert "method=ListParcels" in captured["url"]


def test_boxberry_error_response_recognized():
    """Boxberry returns errors as `[{"err": "..."}]`."""
    from src.tools import logistics
    err_resp = b'[{"err":"Bad parcel id"}]'

    def fake_urlopen(req, timeout):
        m = MagicMock()
        m.read.return_value = err_resp
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        out = logistics._bb_request("ParselCheck", "T")
    assert out["ok"] is False
    assert out["error"] == "Bad parcel id"


def test_boxberry_list_parcels_optional_from_cursor():
    from src.tools import logistics
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        m = MagicMock()
        m.read.return_value = b"[]"
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        logistics.boxberry_list_parcels("T", from_id="parcel-123")
    assert "from=parcel-123" in captured["url"]


def test_boxberry_list_parcels_no_cursor_when_empty():
    from src.tools import logistics
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        m = MagicMock()
        m.read.return_value = b"[]"
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        logistics.boxberry_list_parcels("T")
    assert "from=" not in captured["url"]


def test_boxberry_parcel_check_includes_imid():
    from src.tools import logistics
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        m = MagicMock()
        m.read.return_value = b"[]"
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        logistics.boxberry_parcel_check("T", "ORDER-99")
    assert "ImId=ORDER-99" in captured["url"]


def test_boxberry_list_services_uses_correct_method():
    from src.tools import logistics
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        m = MagicMock()
        m.read.return_value = b"[]"
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        logistics.boxberry_list_services("T", "ORD1")
    assert "method=ListServices" in captured["url"]


def test_boxberry_list_points_filter_city_code():
    from src.tools import logistics
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        m = MagicMock()
        m.read.return_value = b"[]"
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        logistics.boxberry_list_points("T", city_code="77")
    assert "CityCode=77" in captured["url"]


def test_boxberry_request_returns_data_on_success():
    from src.tools import logistics

    def fake_urlopen(req, timeout):
        m = MagicMock()
        m.read.return_value = b'[{"id": "p1"}, {"id": "p2"}]'
        m.status = 200
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        out = logistics._bb_request("ListParcels", "T")
    assert out["ok"] is True
    assert isinstance(out["data"], list)
    assert len(out["data"]) == 2


# ============================================================
# Почта России
# ============================================================

def test_pochta_request_includes_access_token_header():
    from src.tools import logistics
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
        logistics._pochta_request("https://x.pochta.ru/", "/p", "TOKEN")
    assert captured["headers"]["Authorization"] == "AccessToken TOKEN"


def test_pochta_request_adds_user_authorization_when_provided():
    from src.tools import logistics
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
        logistics._pochta_request("https://x.pochta.ru/", "/p", "TOKEN",
                                   login_pass_b64="dXNlcjpwYXNz")
    assert captured["headers"]["X-user-authorization"] == "Basic dXNlcjpwYXNz"


def test_pochta_track_builds_login_pass_b64():
    from src.tools import logistics
    captured = {}

    def fake_pochta_call(base, path, token, lp, **kw):
        captured["lp"] = lp
        return {"ok": True, "data": {"history": []}}

    with patch.object(logistics, "_pochta_call", side_effect=fake_pochta_call):
        logistics.pochta_track("T", "user1", "pass1", "BARCODE")
    decoded = base64.b64decode(captured["lp"]).decode()
    assert decoded == "user1:pass1"


def test_pochta_tariff_calc_body_shape():
    from src.tools import logistics
    captured = {}

    def fake_pochta_call(base, path, token, lp=None, **kw):
        captured["body"] = kw.get("body")
        return {"ok": True, "data": {"sum": 250}}

    with patch.object(logistics, "_pochta_call", side_effect=fake_pochta_call):
        logistics.pochta_tariff_calc("T", 500, "101000", "200000")
    body = captured["body"]
    assert body["mass"] == 500
    assert body["index-from"] == "101000"
    assert body["index-to"] == "200000"
    assert body["mail-type"] == "POSTAL_PARCEL"


def test_pochta_normalize_address_uses_list_wrapper():
    """The /clean/address endpoint expects a list of address records."""
    from src.tools import logistics
    captured = {}

    def fake_pochta_call(base, path, token, lp=None, **kw):
        captured["body"] = kw.get("body")
        return {"ok": True, "data": []}

    with patch.object(logistics, "_pochta_call", side_effect=fake_pochta_call):
        logistics.pochta_normalize_address("T", "Москва Тверская 1")
    assert isinstance(captured["body"], list)
    assert captured["body"][0]["original-address"] == "Москва Тверская 1"


def test_pochta_call_403_classified_permission():
    from src.tools import logistics
    with patch.object(logistics, "_pochta_request", return_value=(403, {}, b'{"e":"x"}')):
        out = logistics._pochta_call("base", "/p", "t")
    assert out["error_kind"] == "permission"


def test_pochta_call_429_rate_limit():
    from src.tools import logistics
    with patch.object(logistics, "_pochta_request", return_value=(429, {}, b"")):
        out = logistics._pochta_call("base", "/p", "t")
    assert out["error_kind"] == "rate_limit"


def test_pochta_orders_search_get_method():
    from src.tools import logistics
    captured = {}

    def fake_pochta_call(base, path, token, lp=None, **kw):
        captured["method"] = kw.get("method")
        captured["params"] = kw.get("params")
        return {"ok": True}

    with patch.object(logistics, "_pochta_call", side_effect=fake_pochta_call):
        logistics.pochta_orders_search("T", "Иванов", limit=20)
    assert captured["method"] == "GET"
    assert captured["params"]["query"] == "Иванов"
    assert captured["params"]["size"] == 20


def test_pochta_order_get_path_with_id():
    from src.tools import logistics
    captured = {}

    def fake_pochta_call(base, path, token, lp=None, **kw):
        captured["path"] = path
        return {"ok": True}

    with patch.object(logistics, "_pochta_call", side_effect=fake_pochta_call):
        logistics.pochta_order_get("T", 12345)
    assert "/backlog/12345" in captured["path"]
