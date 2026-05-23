"""Direct Wildberries API client.

Bypasses Apps Script entirely — most WB endpoints are plain HTTPS with a
Bearer token. Running from Python instead of Apps Script:
  - No 50MB UrlFetchApp limit (we can handle 500MB+ responses)
  - Full stack traces on errors, not opaque SyntaxError
  - Real retry/backoff control
  - Pagination loops can run for hours, not 6 minutes

The WB token typically lives in a `getToken()` function inside a bound Apps
Script — use apps_script_api_get_bound_script_token to extract it once.
"""
from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone
from typing import Iterator

import urllib.request
import urllib.error
import urllib.parse


# Public host map for WB API families.
HOSTS = {
    "content": "content-api.wildberries.ru",
    "analytics": "seller-analytics-api.wildberries.ru",
    "statistics": "statistics-api.wildberries.ru",
    "advert": "advert-api.wildberries.ru",
    "marketplace": "marketplace-api.wildberries.ru",
    "common": "common-api.wildberries.ru",
}


def _request(host: str, path: str, token: str, params: dict | None = None,
             method: str = "GET", body: dict | None = None, timeout: int = 60) -> tuple[int, dict, bytes]:
    """One HTTP request to WB. Returns (status_code, response_headers, raw_body).
    Caller decides whether to parse JSON, retry on 429, etc.
    """
    url = f"https://{host}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": token,
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()


def check_token(token: str) -> dict:
    """Ping every WB API family with `token`. Returns {family: status}.
    A working token should return Status='OK' for the families it has access to.

    Pings run in parallel — 6 hosts × ~300 ms each in serial is ~1.8 s and
    blocks the agent on every health check. A ThreadPoolExecutor drops
    that to roughly one round-trip.
    """
    from concurrent.futures import ThreadPoolExecutor
    from src.tools._errors import _classify_exception

    def _probe_one(name_host: tuple[str, str]) -> tuple[str, dict]:
        name, host = name_host
        try:
            code, _hdr, body = _request(host, "/ping", token, timeout=15)
            try:
                payload = json.loads(body.decode("utf-8"))
                return name, {"code": code, "status": payload.get("Status") or payload.get("status")}
            except Exception as parse_e:
                kind, _ = _classify_exception(parse_e)
                return name, {
                    "code": code,
                    "raw": body[:120].decode("utf-8", errors="replace"),
                    "parse_error_kind": kind,
                }
        except Exception as e:
            kind, status = _classify_exception(e)
            return name, {
                "error": str(e)[:200],
                "error_kind": kind,
                "http_status": status,
                "exception_type": type(e).__name__,
            }

    with ThreadPoolExecutor(max_workers=len(HOSTS)) as pool:
        results = pool.map(_probe_one, HOSTS.items())
    return dict(results)


def token_age(token: str) -> dict:
    """Decode the WB JWT (no signature verification — we just want the claims)
    and report exp/iat/days_left. Returns {issued_at, expires_at, days_left,
    seller_id?, raw_payload}.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return {"error": "not a JWT (expected 3 dot-separated parts)"}
    # Base64-url decode the payload (middle part)
    payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8"))
    except Exception as e:
        return {"error": f"could not decode payload: {e}"}

    now = datetime.now(timezone.utc).timestamp()
    exp = payload.get("exp")
    iat = payload.get("iat")
    return {
        "issued_at": datetime.fromtimestamp(iat, timezone.utc).isoformat() if iat else None,
        "expires_at": datetime.fromtimestamp(exp, timezone.utc).isoformat() if exp else None,
        "days_left": round((exp - now) / 86400, 1) if exp else None,
        "seller_id": payload.get("sid") or payload.get("oid"),
        "raw_payload": payload,
    }


def finance_detail(
    token: str,
    date_from: str,
    date_to: str | None = None,
    limit: int = 10000,
    start_rrd_id: int = 0,
    sleep_sec: int = 65,
    max_pages: int | None = None,
    on_page: callable = None,
) -> Iterator[list[dict]]:
    """Generator over WB `reportDetailByPeriod` pages. Yields each page (list
    of dicts) as it arrives.

    Same endpoint as Mylib.finance3_API_v8 but no Apps Script involved:
    - `limit=10000` is the documented max
    - 65-second pause between requests (WB rate limit: 1 req/min)
    - Auto-retries on 429 honoring `X-Ratelimit-Retry` header
    - Stops cleanly on 204 (no more data) or empty page

    Usage:
        for page in wb.finance_detail(token, "2026-03-01"):
            print(f"got {len(page)} rows; last rrd_id={page[-1]['rrd_id']}")
    """
    if not date_to:
        date_to = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    rrd_id = start_rrd_id
    page_num = 0
    while True:
        page_num += 1
        if max_pages and page_num > max_pages:
            break
        params = {"dateFrom": date_from, "dateTo": date_to, "rrdid": rrd_id, "limit": limit}
        # Retry loop for 429
        for attempt in range(5):
            code, hdr, body = _request(HOSTS["statistics"], "/api/v5/supplier/reportDetailByPeriod",
                                       token, params=params, timeout=120)
            if code == 200:
                break
            if code == 204:
                return
            if code == 429:
                wait = 60
                # X-Ratelimit-Retry header (case-insensitive)
                for k, v in hdr.items():
                    if k.lower() == "x-ratelimit-retry":
                        try:
                            wait = int(v) + 1
                        except Exception:
                            pass
                        break
                if wait > 120:
                    raise RuntimeError(f"WB rate-limit-retry asks {wait}s — likely 12h ban. Stopping.")
                time.sleep(wait)
                continue
            raise RuntimeError(f"WB returned {code}: {body[:300].decode('utf-8', errors='replace')}")
        else:
            raise RuntimeError("Exhausted retries on 429")

        try:
            rows = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise RuntimeError(f"WB returned non-JSON ({len(body)} bytes): {e}")

        if not rows:
            return

        yield rows

        if callable(on_page):
            on_page(page_num, rows)

        if len(rows) < limit:
            return
        rrd_id = rows[-1]["rrd_id"]
        time.sleep(sleep_sec)


def finance_detail_collect(
    token: str,
    date_from: str,
    date_to: str | None = None,
    limit: int = 10000,
    start_rrd_id: int = 0,
    sleep_sec: int = 65,
    max_pages: int | None = None,
    response_format: str = "concise",
) -> dict:
    """Like finance_detail but accumulates all rows into memory and returns
    summary stats. Useful for quick "how many transactions in March" checks.

    `response_format`:
      - "concise" (default): {rows_count, pages, last_rrd_id, sample_first,
        sample_last} — agent gets total + 4 sample rows; can re-call with
        `detailed` for the full grid if it needs every transaction.
      - "detailed": same + `rows: [...]` with EVERY row. Can be tens of MB.
    """
    if response_format not in {"concise", "detailed"}:
        raise ValueError(f"response_format must be 'concise' or 'detailed', got {response_format!r}")
    all_rows: list[dict] = []
    pages = 0
    for page in finance_detail(token, date_from, date_to, limit, start_rrd_id, sleep_sec, max_pages):
        all_rows.extend(page)
        pages += 1
    out: dict = {
        "rows_count": len(all_rows),
        "pages": pages,
        "last_rrd_id": all_rows[-1]["rrd_id"] if all_rows else None,
        "sample_first": all_rows[:2],
        "sample_last": all_rows[-2:],
        "_meta": {"response_format": response_format},
    }
    if response_format == "detailed":
        out["rows"] = all_rows
    return out


# ---------- Batch 1: WB marketplace read-only extensions ----------
# All return {rows | items, _meta}. Parse rate-limit headers, surface
# pagination state, mark `truncated` when WB caps the response.

def _ratelimit_meta(headers: dict) -> dict:
    """Extract X-Ratelimit-* headers so the agent sees remaining quota."""
    out: dict = {}
    for k, v in (headers or {}).items():
        lk = k.lower()
        if lk.startswith("x-ratelimit-"):
            try:
                out[lk.replace("x-ratelimit-", "")] = int(v)
            except (ValueError, TypeError):
                out[lk.replace("x-ratelimit-", "")] = v
    return out


def _json_request(host: str, path: str, token: str, params: dict | None = None,
                  method: str = "GET", body: dict | None = None, timeout: int = 60) -> dict:
    """One-shot WB call returning {ok, status, data, _meta}. Handles 429 by
    surfacing rate-limit headers; the agent decides retry."""
    code, hdr, raw = _request(host, path, token, params=params, method=method, body=body, timeout=timeout)
    meta = {"http_status": code, "ratelimit": _ratelimit_meta(hdr)}
    if code == 204:
        return {"ok": True, "data": None, "_meta": {**meta, "empty_reason": "no_content"}}
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
        return {"ok": False, "error_kind": "bad_input", "error": f"non-JSON response: {e}", "_meta": meta}
    return {"ok": True, "data": data, "_meta": meta}


def stocks_v2(token: str, date_from: str | None = None) -> dict:
    """WB FBO stocks via `/api/v1/supplier/stocks`. Returns full snapshot
    (no pagination — WB returns one big array up to ~50MB).

    `date_from` is the WB-quirky "snapshot date" (defaults to today).
    Returns {ok, data: [{barcode, brand, lastChangeDate, quantity, ...}], _meta}.
    """
    if not date_from:
        date_from = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _json_request(HOSTS["statistics"], "/api/v1/supplier/stocks", token,
                         params={"dateFrom": date_from}, timeout=300)


def orders_recent(token: str, date_from: str, flag: int = 0) -> dict:
    """Recent FBO+FBS orders since `date_from` (YYYY-MM-DD) via
    `/api/v1/supplier/orders`. `flag=0` returns delta since last call;
    `flag=1` returns the full window."""
    return _json_request(HOSTS["statistics"], "/api/v1/supplier/orders", token,
                         params={"dateFrom": date_from, "flag": flag}, timeout=120)


def sales_recent(token: str, date_from: str, flag: int = 0) -> dict:
    """Recent sales/returns since `date_from` via `/api/v1/supplier/sales`.
    `saleID` prefix: S = sale, R = return."""
    return _json_request(HOSTS["statistics"], "/api/v1/supplier/sales", token,
                         params={"dateFrom": date_from, "flag": flag}, timeout=120)


def warehouses(token: str) -> dict:
    """WB official warehouse list (the IDs you use in marketplace API)."""
    return _json_request(HOSTS["marketplace"], "/api/v3/warehouses", token, timeout=60)


def prices_list(token: str, limit: int = 1000, offset: int = 0) -> dict:
    """Get current seller prices + discounts via `/api/v2/list/goods/filter`.
    Returns {ok, data: {listGoods: [{nmID, vendorCode, sizes, ...}]}}."""
    return _json_request(HOSTS["content"], "/api/v2/list/goods/filter", token,
                         params={"limit": limit, "offset": offset}, timeout=60)


def questions_count(token: str, is_answered: bool | None = None) -> dict:
    """Count of buyer questions (FBO/FBS) waiting for a response. Used to
    monitor SLA. `is_answered=False` for backlog, omit for total."""
    params: dict = {}
    if is_answered is not None:
        params["isAnswered"] = "true" if is_answered else "false"
    return _json_request(HOSTS["common"], "/api/v1/questions/count", token,
                         params=params, timeout=30)


def questions_list(token: str, take: int = 100, skip: int = 0,
                   is_answered: bool | None = None,
                   date_from: int | None = None, date_to: int | None = None) -> dict:
    """List buyer questions. `date_from` / `date_to` are UNIX timestamps."""
    params: dict = {"take": take, "skip": skip}
    if is_answered is not None:
        params["isAnswered"] = "true" if is_answered else "false"
    if date_from is not None:
        params["dateFrom"] = date_from
    if date_to is not None:
        params["dateTo"] = date_to
    return _json_request(HOSTS["common"], "/api/v1/questions", token,
                         params=params, timeout=60)


def feedbacks_count(token: str, is_answered: bool | None = None) -> dict:
    """Count of customer reviews ('отзывы')."""
    params: dict = {}
    if is_answered is not None:
        params["isAnswered"] = "true" if is_answered else "false"
    return _json_request(HOSTS["common"], "/api/v1/feedbacks/count", token,
                         params=params, timeout=30)


def feedbacks_list(token: str, take: int = 100, skip: int = 0,
                   is_answered: bool | None = None, order: str = "dateDesc") -> dict:
    """List reviews. `order`: dateDesc | dateAsc."""
    params: dict = {"take": take, "skip": skip, "order": order}
    if is_answered is not None:
        params["isAnswered"] = "true" if is_answered else "false"
    return _json_request(HOSTS["common"], "/api/v1/feedbacks", token,
                         params=params, timeout=60)


def supplies_list(token: str, limit: int = 1000, next_id: int = 0) -> dict:
    """FBS supplies (заказы на отгрузку). `next_id` for pagination cursor."""
    return _json_request(HOSTS["marketplace"], "/api/v3/supplies", token,
                         params={"limit": limit, "next": next_id}, timeout=60)


def adverts_list(token: str, status: int | None = None, type_: int | None = None) -> dict:
    """List advertising campaigns. `status`: -1=pause,4=ready,7=done,8=draft,9=active,11=ready_to_start.
    `type_`: 4=catalog,5=cards,6=search,7=recommendation,8=auto,9=search-catalog."""
    params: dict = {}
    if status is not None:
        params["status"] = status
    if type_ is not None:
        params["type"] = type_
    return _json_request(HOSTS["advert"], "/adv/v1/promotion/count", token,
                         params=params, timeout=30)


def analytics_paid_storage(token: str, date_from: str, date_to: str) -> dict:
    """Paid-storage cost report (FBO). Dates YYYY-MM-DD."""
    return _json_request(HOSTS["analytics"], "/api/v1/paid_storage", token,
                         params={"dateFrom": date_from, "dateTo": date_to}, timeout=120)
