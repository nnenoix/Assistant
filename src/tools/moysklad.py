"""МойСклад REST API client (JSON 1.2).

Endpoints under `https://api.moysklad.ru/api/remap/1.2`. Auth: Bearer
token from Профиль → Сервисные настройки → Доступ к API. Returns
{ok, data, _meta:{http_status, rate_limit_*}}.

МойСклад is the most-common "lightweight 1С" for Russian sellers — it
handles inventory, orders, customers, suppliers, money. The 1С Бухгалтерия
proper has a separate OData adapter (`src/tools/onec.py`).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


_BASE = "https://api.moysklad.ru/api/remap/1.2"


def _request(path: str, token: str, method: str = "GET",
             params: dict | None = None, body: dict | None = None,
             timeout: int = 60) -> tuple[int, dict, bytes]:
    url = f"{_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json;charset=utf-8",
            "Accept-Encoding": "gzip",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()


def _call(path: str, token: str, **kwargs) -> dict:
    code, hdr, raw = _request(path, token, **kwargs)
    rl: dict = {}
    for k, v in (hdr or {}).items():
        lk = k.lower()
        # МойСклад returns BOTH `X-RateLimit-*` (per their public docs) and the
        # vendor-specific `X-Lognex-Retry-TimeInterval`. Accept both.
        if lk.startswith("x-ratelimit-") or lk.startswith("x-rate-limit-") or lk == "x-lognex-retry-timeinterval":
            try:
                rl[lk] = int(v)
            except (ValueError, TypeError):
                rl[lk] = v
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
        data = json.loads(raw.decode("utf-8")) if raw else None
    except json.JSONDecodeError as e:
        return {"ok": False, "error_kind": "bad_input", "error": f"non-JSON: {e}", "_meta": meta}
    return {"ok": True, "data": data, "_meta": meta}


# ---------- entity lists ----------

def products_list(token: str, limit: int = 1000, offset: int = 0,
                  filter_str: str | None = None) -> dict:
    """Товары — products. `filter_str` is the МС filter DSL, e.g.
    `name~Шланг;archived=false`."""
    params: dict = {"limit": limit, "offset": offset}
    if filter_str:
        params["filter"] = filter_str
    return _call("/entity/product", token, params=params)


def variants_list(token: str, limit: int = 1000, offset: int = 0) -> dict:
    """Модификации (size/color SKU variants) — `entity/variant`."""
    return _call("/entity/variant", token, params={"limit": limit, "offset": offset})


def services_list(token: str, limit: int = 1000, offset: int = 0) -> dict:
    """Услуги."""
    return _call("/entity/service", token, params={"limit": limit, "offset": offset})


def counterparties_list(token: str, limit: int = 1000, offset: int = 0,
                        filter_str: str | None = None) -> dict:
    """Контрагенты — customers and suppliers."""
    params: dict = {"limit": limit, "offset": offset}
    if filter_str:
        params["filter"] = filter_str
    return _call("/entity/counterparty", token, params=params)


def stores_list(token: str) -> dict:
    """Склады."""
    return _call("/entity/store", token)


def organizations_list(token: str) -> dict:
    """Юр.лица (legal entities the user operates under)."""
    return _call("/entity/organization", token)


def customerorders_list(token: str, limit: int = 1000, offset: int = 0,
                        moment_from: str | None = None,
                        moment_to: str | None = None) -> dict:
    """Заказы покупателей. moment_from/to in МС format: `2026-05-01 00:00:00`."""
    params: dict = {"limit": limit, "offset": offset}
    filters: list[str] = []
    if moment_from:
        filters.append(f"moment>={moment_from}")
    if moment_to:
        filters.append(f"moment<={moment_to}")
    if filters:
        params["filter"] = ";".join(filters)
    return _call("/entity/customerorder", token, params=params)


def demands_list(token: str, limit: int = 1000, offset: int = 0,
                 moment_from: str | None = None) -> dict:
    """Отгрузки (shipments → revenue recognition events)."""
    params: dict = {"limit": limit, "offset": offset}
    if moment_from:
        params["filter"] = f"moment>={moment_from}"
    return _call("/entity/demand", token, params=params)


def supplies_list(token: str, limit: int = 1000, offset: int = 0,
                  moment_from: str | None = None) -> dict:
    """Приёмки (incoming goods from suppliers)."""
    params: dict = {"limit": limit, "offset": offset}
    if moment_from:
        params["filter"] = f"moment>={moment_from}"
    return _call("/entity/supply", token, params=params)


def stock_all(token: str, limit: int = 1000, offset: int = 0) -> dict:
    """Остатки по всем складам — `report/stock/all`. Use for "what do I
    have in stock right now"."""
    return _call("/report/stock/all", token, params={"limit": limit, "offset": offset})


def stock_bystore(token: str, store_id: str, limit: int = 1000, offset: int = 0) -> dict:
    """Остатки в одном складе."""
    return _call(f"/report/stock/bystore", token,
                 params={"limit": limit, "offset": offset, "filter": f"store={store_id}"})


def cashflow_report(token: str, moment_from: str, moment_to: str) -> dict:
    """Cashflow report — money in/out by category."""
    return _call("/report/money/plotseries", token,
                 params={"momentFrom": moment_from, "momentTo": moment_to, "interval": "day"})


def profit_byproduct(token: str, moment_from: str, moment_to: str,
                     limit: int = 1000) -> dict:
    """Прибыль по товарам — `report/profit/byproduct`. Margin per SKU
    over a period. The closest-to-truth unit-econ report МС provides."""
    return _call("/report/profit/byproduct", token,
                 params={"momentFrom": moment_from, "momentTo": moment_to, "limit": limit})


def expenses_list(token: str, limit: int = 1000, offset: int = 0,
                  moment_from: str | None = None) -> dict:
    """Расходы — outgoing cash operations (cashout)."""
    params: dict = {"limit": limit, "offset": offset}
    if moment_from:
        params["filter"] = f"moment>={moment_from}"
    return _call("/entity/cashout", token, params=params)
