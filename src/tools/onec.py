"""1С Бухгалтерия / УТ via OData REST.

1С exposes OData by default at `<host>/<infobase>/odata/standard.odata/`.
Auth is HTTP Basic (login from the 1С user account). Endpoints
match metadata names: Document_РеализацияТоваровУслуг,
Catalog_Контрагенты, AccumulationRegister_Взаиморасчеты, etc.

We provide thin wrappers over the most-asked entities. For arbitrary
queries use `onec_odata_query` with a raw OData URL fragment.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request


def _odata_get(base_url: str, login: str, password: str, path: str,
               query: dict | None = None, timeout: int = 60) -> dict:
    """GET against /odata/standard.odata/<path>. base_url like
    `https://1c.example.ru/buh3/odata/standard.odata`."""
    auth = base64.b64encode(f"{login}:{password}".encode("utf-8")).decode("ascii")
    url = f"{base_url}/{path}"
    qp = {"$format": "json"}
    if query:
        qp.update(query)
    url += "?" + urllib.parse.urlencode(qp)
    req = urllib.request.Request(url, method="GET", headers={
        "Authorization": f"Basic {auth}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"ok": True, "data": json.loads(resp.read().decode("utf-8")),
                    "_meta": {"http_status": resp.status}}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": e.read()[:300].decode("utf-8", errors="replace"),
                "_meta": {"http_status": e.code}}


def onec_odata_query(base_url: str, login: str, password: str,
                     path: str, filter_: str | None = None,
                     top: int = 100, skip: int = 0,
                     select: str | None = None) -> dict:
    """Generic OData GET. `path` example: `Catalog_Контрагенты`.
    `filter_` is OData filter syntax, e.g. `Description eq 'ООО Ромашка'`."""
    q: dict = {"$top": top, "$skip": skip}
    if filter_:
        q["$filter"] = filter_
    if select:
        q["$select"] = select
    return _odata_get(base_url, login, password, path, query=q)


def onec_contractors(base_url: str, login: str, password: str,
                     name_like: str | None = None, top: int = 100) -> dict:
    """Catalog_Контрагенты с фильтром по имени."""
    f = f"substringof('{name_like}', Description)" if name_like else None
    return onec_odata_query(base_url, login, password, "Catalog_Контрагенты",
                            filter_=f, top=top)


def onec_products(base_url: str, login: str, password: str,
                  top: int = 100, skip: int = 0) -> dict:
    """Catalog_Номенклатура."""
    return onec_odata_query(base_url, login, password, "Catalog_Номенклатура",
                            top=top, skip=skip)


def onec_documents(base_url: str, login: str, password: str,
                   doc_type: str, date_from: str | None = None,
                   top: int = 100) -> dict:
    """List documents. doc_type example: `Document_РеализацияТоваровУслуг`.
    `date_from` OData datetime: `2026-05-01T00:00:00`."""
    f = f"Date ge datetime'{date_from}'" if date_from else None
    return onec_odata_query(base_url, login, password, doc_type, filter_=f, top=top)


def onec_money_balance(base_url: str, login: str, password: str,
                       date_iso: str | None = None) -> dict:
    """AccumulationRegister_ДенежныеСредстваБалансе — cash balance snapshot.

    Without date_iso → last known. With → at-date balance."""
    path = "AccumulationRegister_ДенежныеСредстваБалансе"
    q = {"$top": 1000}
    if date_iso:
        q["$filter"] = f"Period le datetime'{date_iso}'"
    return _odata_get(base_url, login, password, path, query=q)
