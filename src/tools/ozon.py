"""Ozon Seller API client.

Endpoints under `api-seller.ozon.ru`. Auth: TWO headers — `Client-Id`
(numeric seller ID) and `Api-Key` (32-char hex string from Settings ->
API keys). Most calls are POST with JSON bodies (Ozon's quirk — they
use POST for what feel like reads).

Rate limits are per-endpoint; the response headers include
`X-RateLimit-Remaining`. Retries are handled by surfacing `_meta.ratelimit`
to the caller — Ozon's 429 retry semantics are not as straightforward as
WB's `X-Ratelimit-Retry`, so we don't auto-backoff blindly.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import urllib.request
import urllib.error
import urllib.parse


_BASE = "https://api-seller.ozon.ru"


def _request(path: str, client_id: str, api_key: str,
             body: dict | None = None, method: str = "POST",
             timeout: int = 60) -> tuple[int, dict, bytes]:
    """One HTTP call to Ozon. Returns (status, headers, raw_body)."""
    url = f"{_BASE}{path}"
    data = json.dumps(body or {}).encode("utf-8") if method == "POST" else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Client-Id": str(client_id),
            "Api-Key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()


def _json_call(path: str, client_id: str, api_key: str,
               body: dict | None = None, method: str = "POST",
               timeout: int = 60) -> dict:
    """Returns {ok, data, error_kind?, error?, _meta:{http_status, ratelimit}}."""
    code, hdr, raw = _request(path, client_id, api_key, body=body, method=method, timeout=timeout)
    rl = {}
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


# ---------- read-only marketplace tools ----------

def check_credentials(client_id: str, api_key: str) -> dict:
    """Cheapest read-only call to verify credentials. Returns a small
    product-info batch with empty filter. {ok, seller_name?, _meta}."""
    out = _json_call("/v3/product/info/list", client_id, api_key,
                     body={"offer_id": [], "product_id": [], "sku": []})
    out["credentials_valid"] = out["ok"] and out["_meta"]["http_status"] != 403
    return out


def stocks_fbo(client_id: str, api_key: str, limit: int = 1000, cursor: str = "") -> dict:
    """FBO stocks via `/v4/product/info/stocks`. Paginated by cursor — pass
    response['data']['last_id'] as the next `cursor`."""
    body: dict = {"filter": {"visibility": "ALL"}, "limit": limit}
    if cursor:
        body["cursor"] = cursor
    return _json_call("/v4/product/info/stocks", client_id, api_key, body=body)


def stocks_fbs(client_id: str, api_key: str, sku: list[str] | None = None) -> dict:
    """FBS stocks for specific SKUs via `/v1/product/info/stocks-by-warehouse/fbs`."""
    return _json_call("/v1/product/info/stocks-by-warehouse/fbs", client_id, api_key,
                      body={"sku": sku or []})


def orders_fbo_list(client_id: str, api_key: str, date_from: str, date_to: str,
                    limit: int = 1000, offset: int = 0) -> dict:
    """FBO postings via `/v2/posting/fbo/list`. Dates RFC3339."""
    return _json_call("/v2/posting/fbo/list", client_id, api_key, body={
        "filter": {"since": date_from, "to": date_to},
        "limit": limit, "offset": offset,
        "with": {"analytics_data": True, "financial_data": True},
    })


def orders_fbs_list(client_id: str, api_key: str, date_from: str, date_to: str,
                    limit: int = 1000, offset: int = 0,
                    status: str | None = None) -> dict:
    """FBS postings via `/v3/posting/fbs/list`. Optional status filter:
    awaiting_packaging / awaiting_deliver / delivered / cancelled / ..."""
    body: dict = {
        "filter": {"since": date_from, "to": date_to},
        "limit": limit, "offset": offset,
        "with": {"analytics_data": True, "financial_data": True, "barcodes": False},
    }
    if status:
        body["filter"]["status"] = status
    return _json_call("/v3/posting/fbs/list", client_id, api_key, body=body)


def returns_list(client_id: str, api_key: str, date_from: str, date_to: str,
                 limit: int = 1000, offset: int = 0) -> dict:
    """Returns (возвраты) via `/v1/returns/company/fbo`. Dates RFC3339."""
    return _json_call("/v1/returns/company/fbo", client_id, api_key, body={
        "filter": {"return_date": {"time_from": date_from, "time_to": date_to}},
        "limit": limit, "offset": offset,
    })


def finance_realization(client_id: str, api_key: str, year: int, month: int) -> dict:
    """Monthly realization report (отчёт о реализации) via `/v2/finance/realization`."""
    return _json_call("/v2/finance/realization", client_id, api_key,
                      body={"year": year, "month": month})


def finance_transactions(client_id: str, api_key: str,
                         date_from: str, date_to: str,
                         page: int = 1, page_size: int = 1000,
                         operation_type: list[str] | None = None) -> dict:
    """Detailed transactions (детализация транзакций) via
    `/v3/finance/transaction/list`. Dates RFC3339. operation_type examples:
    OperationAgentDeliveredToCustomer, ClientReturnAgentOperation, ..."""
    body: dict = {
        "filter": {
            "date": {"from": date_from, "to": date_to},
            "operation_type": operation_type or [],
            "posting_number": "",
            "transaction_type": "all",
        },
        "page": page, "page_size": page_size,
    }
    return _json_call("/v3/finance/transaction/list", client_id, api_key, body=body)


def products_list(client_id: str, api_key: str, limit: int = 1000,
                  last_id: str = "", visibility: str = "ALL") -> dict:
    """Product list via `/v3/product/list`. visibility: ALL, VISIBLE,
    INVISIBLE, EMPTY_STOCK, NOT_MODERATED, ARCHIVED."""
    return _json_call("/v3/product/list", client_id, api_key, body={
        "filter": {"visibility": visibility},
        "limit": limit, "last_id": last_id,
    })


def prices_list(client_id: str, api_key: str, limit: int = 1000, last_id: str = "") -> dict:
    """Current prices via `/v4/product/info/prices`."""
    return _json_call("/v4/product/info/prices", client_id, api_key, body={
        "filter": {"visibility": "ALL"},
        "limit": limit, "last_id": last_id,
    })


def warehouses_list(client_id: str, api_key: str) -> dict:
    """FBS warehouses via `/v1/warehouse/list`."""
    return _json_call("/v1/warehouse/list", client_id, api_key)


def analytics_data(client_id: str, api_key: str, date_from: str, date_to: str,
                   metrics: list[str] | None = None,
                   dimension: list[str] | None = None) -> dict:
    """Daily analytics via `/v1/analytics/data`. metrics: revenue, ordered_units,
    delivered_units, returns, cancellations, hits_view_search, hits_view_pdp, ...
    dimension: day, week, month, sku, brand, category1-4."""
    return _json_call("/v1/analytics/data", client_id, api_key, body={
        "date_from": date_from, "date_to": date_to,
        "metrics": metrics or ["revenue", "ordered_units"],
        "dimension": dimension or ["sku"],
        "limit": 1000, "offset": 0,
    })
