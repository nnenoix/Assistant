"""Yandex Market Partner API client.

Endpoints under `api.partner.market.yandex.ru`. Auth: single `Api-Key`
header (OAuth-style 64-char token from partner.market.yandex.ru -> Settings
-> API access). Most endpoints scoped to `businessId` (legal entity) or
`campaignId` (a specific shop). One business → many campaigns.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


_BASE = "https://api.partner.market.yandex.ru"


def _request(path: str, api_key: str,
             params: dict | None = None, body: dict | None = None,
             method: str = "GET", timeout: int = 60) -> tuple[int, dict, bytes]:
    """One HTTP call to Yandex.Market via shared `_vendor_http.request_raw`."""
    from src.tools._vendor_http import request_raw
    url = f"{_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    return request_raw(
        method, url,
        headers={
            "Api-Key": api_key,
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
        body=data,
        timeout=timeout,
    )


def _json_call(path: str, api_key: str, params: dict | None = None,
               body: dict | None = None, method: str = "GET",
               timeout: int = 60) -> dict:
    """Returns {ok, data, error_kind?, error?, _meta:{http_status, ratelimit}}."""
    code, hdr, raw = _request(path, api_key, params=params, body=body, method=method, timeout=timeout)
    rl: dict = {}
    for k, v in (hdr or {}).items():
        lk = k.lower()
        if lk.startswith("x-ratelimit-"):
            try:
                rl[lk.replace("x-ratelimit-", "")] = int(v)
            except (ValueError, TypeError):
                rl[lk.replace("x-ratelimit-", "")] = v
    meta = {"http_status": code, "ratelimit": rl}
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
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as e:
        return {"ok": False, "error_kind": "bad_input", "error": f"non-JSON: {e}", "_meta": meta}
    return {"ok": True, "data": data, "_meta": meta}


# ---------- Yandex Market read-only tools ----------

def campaigns_list(api_key: str) -> dict:
    """List campaigns (shops) the API key has access to. Returns
    {data:{campaigns:[{id, domain, business, ...}]}}. campaignId is what
    every per-shop endpoint takes."""
    return _json_call("/campaigns", api_key)


def businesses_list(api_key: str) -> dict:
    """List businesses (legal entities). businessId scopes products/inventory.

    Returns {data:{businesses:[{id, name, fintech:{...}}]}}."""
    return _json_call("/businesses", api_key)


def stocks_list(api_key: str, campaign_id: int, limit: int = 200,
                page_token: str = "", with_turnover: bool = False) -> dict:
    """Stocks for a campaign via POST /campaigns/{id}/offers/stocks. Pagination
    via `paging.nextPageToken`."""
    params: dict = {"limit": limit}
    if page_token:
        params["page_token"] = page_token
    body: dict = {"withTurnover": with_turnover}
    return _json_call(f"/campaigns/{campaign_id}/offers/stocks", api_key,
                      params=params, body=body, method="POST")


def orders_list(api_key: str, campaign_id: int,
                from_date: str, to_date: str,
                page: int = 1, page_size: int = 50,
                status: str | None = None,
                response_format: str = "concise") -> dict:
    """Orders for a campaign via /campaigns/{id}/orders. Dates DD-MM-YYYY.
    status: PROCESSING / DELIVERY / DELIVERED / CANCELLED / PICKUP.

    `response_format='concise'` (default) trims each order to {id, status,
    creationDate, total, buyer.firstName}. 'detailed' returns the full row
    (items array, shipments, delivery, etc.) — tens of KB per order."""
    if response_format not in {"concise", "detailed"}:
        raise ValueError(f"response_format must be 'concise' or 'detailed', got {response_format!r}")
    params: dict = {"fromDate": from_date, "toDate": to_date, "page": page, "pageSize": page_size}
    if status:
        params["status"] = status
    out = _json_call(f"/campaigns/{campaign_id}/orders", api_key, params=params)
    if out.get("ok") and response_format == "concise":
        orders = ((out.get("data") or {}).get("orders") or [])
        out["data"]["orders"] = [
            {"id": o.get("id"), "status": o.get("status"),
             "creationDate": o.get("creationDate"), "total": o.get("itemsTotal"),
             "buyer_firstName": (o.get("buyer") or {}).get("firstName")}
            for o in orders
        ]
    return out


def order_get(api_key: str, campaign_id: int, order_id: int) -> dict:
    """Single order detail."""
    return _json_call(f"/campaigns/{campaign_id}/orders/{order_id}", api_key)


def returns_list(api_key: str, campaign_id: int,
                 from_date: str, to_date: str,
                 page_token: str = "", limit: int = 50) -> dict:
    """Returns via /campaigns/{id}/orders/returns. Dates DD-MM-YYYY."""
    params: dict = {"fromDate": from_date, "toDate": to_date, "limit": limit}
    if page_token:
        params["page_token"] = page_token
    return _json_call(f"/campaigns/{campaign_id}/orders/returns", api_key, params=params)


def prices_list(api_key: str, campaign_id: int, page_token: str = "",
                limit: int = 100) -> dict:
    """Current shop prices via POST /campaigns/{id}/offer-prices."""
    params: dict = {"limit": limit}
    if page_token:
        params["page_token"] = page_token
    return _json_call(f"/campaigns/{campaign_id}/offer-prices", api_key,
                      params=params, body={}, method="POST")


def offers_list(api_key: str, business_id: int, page_token: str = "",
                limit: int = 200) -> dict:
    """Business-level offer catalog (across all campaigns) via
    POST /businesses/{id}/offer-mappings."""
    params: dict = {"limit": limit}
    if page_token:
        params["page_token"] = page_token
    return _json_call(f"/businesses/{business_id}/offer-mappings", api_key,
                      params=params, body={}, method="POST")


def warehouses_list(api_key: str, business_id: int) -> dict:
    """Business warehouses via GET /businesses/{id}/warehouses."""
    return _json_call(f"/businesses/{business_id}/warehouses", api_key)
