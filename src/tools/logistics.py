"""Russian courier / logistics APIs: СДЭК, Boxberry, Почта России.

All three follow the same shape:
  - SDEK v2: OAuth2 (account, secret) → access token, REST under api.cdek.ru
  - Boxberry: api_token in URL, REST under api.boxberry.ru
  - Russian Post: api_token in header, REST under tracking.pochta.ru and otpravka-api.pochta.ru

Returns are uniformly {ok, data, error_kind?, error?, _meta:{http_status}}
so the agent can branch on outcome without parsing per-vendor error JSON.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


# ============================================================
# СДЭК v2.0
# ============================================================

_CDEK_BASE = "https://api.cdek.ru/v2"
_CDEK_AUTH_PATH = "/oauth/token?parameters"


def _cdek_request(path: str, token: str, method: str = "GET",
                  params: dict | None = None, body: dict | None = None,
                  timeout: int = 60) -> tuple[int, dict, bytes]:
    """CDEK via shared `_vendor_http.request_raw` (Bearer auth)."""
    from src.tools._vendor_http import request_raw
    url = f"{_CDEK_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    return request_raw(
        method, url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
        body=data,
        timeout=timeout,
    )


def _cdek_call(path: str, token: str, **kwargs) -> dict:
    code, hdr, raw = _cdek_request(path, token, **kwargs)
    meta = {"http_status": code}
    if code == 429:
        return {"ok": False, "error_kind": "rate_limit", "data": None, "_meta": meta}
    if code >= 400:
        return {
            "ok": False,
            "error_kind": "permission" if code in (401, 403) else ("not_found" if code == 404 else "server" if code >= 500 else "bad_input"),
            "error": raw[:300].decode("utf-8", errors="replace"),
            "data": None,
            "_meta": meta,
        }
    try:
        data = json.loads(raw.decode("utf-8")) if raw else None
    except json.JSONDecodeError as e:
        return {"ok": False, "error_kind": "bad_input", "error": f"non-JSON: {e}", "_meta": meta}
    return {"ok": True, "data": data, "_meta": meta}


def cdek_auth(account: str, secret: str) -> dict:
    """Get SDEK OAuth2 access token (lifetime: 1 hour). Pass as `token` to
    other cdek_* tools. Returns {ok, data:{access_token, expires_in, ...}}."""
    url = f"{_CDEK_BASE}/oauth/token?parameters"
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": account,
        "client_secret": secret,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return {"ok": True, "data": data, "_meta": {"http_status": resp.status}}
    except urllib.error.HTTPError as e:
        body = e.read()
        return {
            "ok": False,
            "error_kind": "permission" if e.code in (401, 403) else "bad_input",
            "error": body[:300].decode("utf-8", errors="replace"),
            "_meta": {"http_status": e.code},
        }


def cdek_orders_list(token: str, date_from: str | None = None,
                     date_to: str | None = None, limit: int = 50,
                     offset: int = 0) -> dict:
    """List shipments via /orders. Dates ISO8601."""
    params: dict = {"size": limit, "page": offset // limit if limit else 0}
    if date_from:
        params["date_first"] = date_from
    if date_to:
        params["date_last"] = date_to
    return _cdek_call("/orders", token, params=params)


def cdek_order_get(token: str, uuid: str) -> dict:
    """Single shipment by UUID."""
    return _cdek_call(f"/orders/{uuid}", token)


def cdek_calculator(token: str, from_code: int, to_code: int,
                    tariff_code: int = 136, weight_g: int = 1000,
                    length_cm: int = 10, width_cm: int = 10,
                    height_cm: int = 10) -> dict:
    """Cost calculator via /calculator/tariff. `from_code`/`to_code` are
    SDEK location codes (use cdek_locations_search to find them).
    `tariff_code` 136 = склад-склад (warehouse-to-warehouse)."""
    body = {
        "tariff_code": tariff_code,
        "from_location": {"code": from_code},
        "to_location": {"code": to_code},
        "packages": [{
            "weight": weight_g,
            "length": length_cm,
            "width": width_cm,
            "height": height_cm,
        }],
    }
    return _cdek_call("/calculator/tariff", token, method="POST", body=body)


def cdek_locations_search(token: str, query: str, country_code: str = "RU",
                          size: int = 20) -> dict:
    """Search SDEK locations by name. Returns location codes for use in
    other endpoints."""
    return _cdek_call("/location/cities", token,
                      params={"city": query, "country_codes": country_code, "size": size})


# ============================================================
# Boxberry
# ============================================================

_BOXBERRY_BASE = "https://api.boxberry.ru"
_BOXBERRY_NEW_BASE = "https://api.boxberry.ru/json.php"  # legacy entry; the new methods sit under /api/


def _bb_request(method_name: str, token: str, params: dict | None = None,
                timeout: int = 60) -> dict:
    """Boxberry uses GET with `token` and `method` as URL params. Most
    return JSON. Errors come back as `[{"err":"..."}]` or `{"error":...}`
    even on HTTP 200 — the 200-but-error path lives here, not in the
    shared transport."""
    from src.tools._vendor_http import request_raw
    qp = {"token": token, "method": method_name}
    if params:
        qp.update(params)
    url = f"{_BOXBERRY_BASE}/json.php?" + urllib.parse.urlencode(qp)
    code, _hdr, raw = request_raw("GET", url, headers={"Accept": "application/json"},
                                  timeout=timeout)
    if code >= 400:
        return {"ok": False,
                "error_kind": "permission" if code in (401, 403) else "bad_input",
                "error": raw[:300].decode("utf-8", errors="replace"),
                "_meta": {"http_status": code}}
    try:
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as e:
        return {"ok": False, "error_kind": "bad_input",
                "error": f"non-JSON: {e}", "_meta": {"http_status": code}}
    # Boxberry returns errors as a list with `err` even on 200
    if isinstance(data, list) and data and isinstance(data[0], dict) and "err" in data[0]:
        return {"ok": False, "error_kind": "bad_input",
                "error": data[0]["err"], "data": None,
                "_meta": {"http_status": code}}
    return {"ok": True, "data": data, "_meta": {"http_status": code}}


def boxberry_list_parcels(token: str, from_id: str = "") -> dict:
    """List uploaded parcels via ListParcels. `from_id` = cursor (parcel id)
    to resume from."""
    return _bb_request("ListParcels", token, {"from": from_id} if from_id else None)


def boxberry_parcel_check(token: str, im_id: str) -> dict:
    """Check a single parcel by your internal id via ParselCheck."""
    return _bb_request("ParselCheck", token, {"ImId": im_id})


def boxberry_list_statuses(token: str, im_id: str) -> dict:
    """Status history for one parcel via ListStatuses."""
    return _bb_request("ListStatuses", token, {"ImId": im_id})


def boxberry_list_services(token: str, im_id: str) -> dict:
    """Cost breakdown (delivery, insurance, ...) for one parcel."""
    return _bb_request("ListServices", token, {"ImId": im_id})


def boxberry_courier_list_cities(token: str) -> dict:
    """Cities where Boxberry courier pickup is available."""
    return _bb_request("CourierListCities", token)


def boxberry_list_points(token: str, city_code: str = "") -> dict:
    """List Boxberry pickup points. `city_code` optional filter."""
    return _bb_request("ListPoints", token, {"CityCode": city_code} if city_code else None)


# ============================================================
# Почта России
# ============================================================

_POCHTA_TRACKING_BASE = "https://tracking.pochta.ru/api/v1"
_POCHTA_OTPRAVKA_BASE = "https://otpravka-api.pochta.ru/1.0"


def _pochta_request(base_url: str, path: str, token: str, login_pass_b64: str | None = None,
                    method: str = "POST", body: dict | None = None,
                    params: dict | None = None, timeout: int = 60) -> tuple[int, dict, bytes]:
    """Pochta Russia OTPRAVKA API via shared transport. Two-header auth
    (`AccessToken` + optional `X-User-Authorization` for personal accounts)."""
    from src.tools._vendor_http import request_raw
    url = f"{base_url}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Authorization": f"AccessToken {token}",
        "Accept": "application/json;charset=UTF-8",
        **({"Content-Type": "application/json"} if body is not None else {}),
    }
    if login_pass_b64:
        headers["X-User-Authorization"] = f"Basic {login_pass_b64}"
    return request_raw(method, url, headers=headers, body=data, timeout=timeout)


def _pochta_call(base_url: str, path: str, token: str, login_pass_b64: str | None = None,
                 **kwargs) -> dict:
    code, hdr, raw = _pochta_request(base_url, path, token, login_pass_b64, **kwargs)
    meta = {"http_status": code}
    if code == 429:
        return {"ok": False, "error_kind": "rate_limit", "data": None, "_meta": meta}
    if code >= 400:
        return {
            "ok": False,
            "error_kind": "permission" if code in (401, 403) else ("not_found" if code == 404 else "server" if code >= 500 else "bad_input"),
            "error": raw[:300].decode("utf-8", errors="replace"),
            "data": None,
            "_meta": meta,
        }
    try:
        data = json.loads(raw.decode("utf-8")) if raw else None
    except json.JSONDecodeError as e:
        return {"ok": False, "error_kind": "bad_input", "error": f"non-JSON: {e}", "_meta": meta}
    return {"ok": True, "data": data, "_meta": meta}


def pochta_track(token: str, login: str, password: str, barcode: str) -> dict:
    """Single-barcode tracking history via tracking.pochta.ru. login+password
    are your account credentials; we base64-encode them per the API spec."""
    lp = base64.b64encode(f"{login}:{password}".encode("utf-8")).decode("ascii")
    body = {"barcode": barcode, "messageType": 0, "language": "RUS"}
    return _pochta_call(_POCHTA_TRACKING_BASE, "/trackingpointers/by-barcodes",
                        token, lp, body=body)


def pochta_orders_search(token: str, query: str, limit: int = 50) -> dict:
    """Search orders by name/recipient/order-num via otpravka /backlog/search."""
    return _pochta_call(_POCHTA_OTPRAVKA_BASE, "/backlog/search", token,
                        method="GET", params={"query": query, "size": limit})


def pochta_order_get(token: str, order_id: int) -> dict:
    """Single order detail."""
    return _pochta_call(_POCHTA_OTPRAVKA_BASE, f"/backlog/{order_id}", token, method="GET")


def pochta_tariff_calc(token: str, mass_g: int, index_from: str, index_to: str,
                       mail_category: str = "ORDINARY",
                       mail_type: str = "POSTAL_PARCEL") -> dict:
    """Tariff calculator via otpravka /tariff. Mass grams, indexes are 6-digit
    postal codes. mail_type: POSTAL_PARCEL / ONLINE_PARCEL / EMS / ..."""
    body = {
        "mass": mass_g,
        "index-from": index_from,
        "index-to": index_to,
        "mail-category": mail_category,
        "mail-type": mail_type,
    }
    return _pochta_call(_POCHTA_OTPRAVKA_BASE, "/tariff", token, method="POST", body=body)


def pochta_normalize_address(token: str, address: str) -> dict:
    """Address normalizer via /clean/address. Returns parsed components +
    delivery-area metadata."""
    return _pochta_call(_POCHTA_OTPRAVKA_BASE, "/clean/address", token,
                        method="POST", body=[{"id": "1", "original-address": address}])
