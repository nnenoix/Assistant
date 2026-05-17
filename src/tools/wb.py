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
    """
    out = {}
    for name, host in HOSTS.items():
        try:
            code, _hdr, body = _request(host, "/ping", token, timeout=15)
            try:
                payload = json.loads(body.decode("utf-8"))
                out[name] = {"code": code, "status": payload.get("Status") or payload.get("status")}
            except Exception:
                out[name] = {"code": code, "raw": body[:120].decode("utf-8", errors="replace")}
        except Exception as e:
            out[name] = {"error": str(e)[:120]}
    return out


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
) -> dict:
    """Like finance_detail but accumulates all rows into memory and returns
    {rows_count, last_rrd_id, pages, sample_first, sample_last}.
    Useful for quick "how many transactions in March" checks.
    """
    all_rows: list[dict] = []
    pages = 0
    for page in finance_detail(token, date_from, date_to, limit, start_rrd_id, sleep_sec, max_pages):
        all_rows.extend(page)
        pages += 1
    return {
        "rows_count": len(all_rows),
        "pages": pages,
        "last_rrd_id": all_rows[-1]["rrd_id"] if all_rows else None,
        "sample_first": all_rows[:2],
        "sample_last": all_rows[-2:],
    }
