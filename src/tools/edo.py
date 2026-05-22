"""Russian EDI (ЭДО) clients: СБИС and Контур.Диадок.

Both expose REST endpoints to list incoming/outgoing documents (УПД,
накладные, акты) and download their XML/PDF. Auth differs:
  - СБИС: login/password → session-cookie (use sbis_auth to get it)
  - Контур.Диадок: API key + auth-token header

Most users only need read access: «какие документы пришли», «скачать
УПД», «какие непринятые на нашей стороне».
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def _get_json(url: str, headers: dict | None = None, timeout: int = 60) -> dict:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"ok": True, "data": json.loads(resp.read().decode("utf-8")),
                    "_meta": {"http_status": resp.status}}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": e.read()[:300].decode("utf-8", errors="replace"),
                "_meta": {"http_status": e.code}}


def _post_json(url: str, body: dict, headers: dict | None = None, timeout: int = 60) -> dict:
    h = {"Content-Type": "application/json", "Accept": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 method="POST", headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"ok": True, "data": json.loads(resp.read().decode("utf-8")),
                    "_meta": {"http_status": resp.status}}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": e.read()[:300].decode("utf-8", errors="replace"),
                "_meta": {"http_status": e.code}}


# ============================================================
# СБИС (Tensor)
# ============================================================

_SBIS_BASE = "https://online.sbis.ru/service"


def sbis_auth(login: str, password: str) -> dict:
    """СБИС.Аутентифицировать. Returns session_id to use in subsequent calls.

    `params: {Логин, Пароль}`. JSON-RPC over HTTPS."""
    body = {
        "jsonrpc": "2.0", "id": 1,
        "method": "СБИС.Аутентифицировать",
        "params": {"Логин": login, "Пароль": password},
    }
    return _post_json(_SBIS_BASE + "/auth-service/service/", body)


def sbis_docs_list(session_id: str, doc_type: str = "ВходящийДокумент",
                   from_date: str | None = None, to_date: str | None = None,
                   limit: int = 50) -> dict:
    """СБИС.СписокДокументов. doc_type: ВходящийДокумент / ИсходящийДокумент.
    Dates DD.MM.YYYY."""
    filt: dict = {"Лимит": limit}
    if from_date:
        filt["с"] = from_date
    if to_date:
        filt["по"] = to_date
    body = {
        "jsonrpc": "2.0", "id": 1,
        "method": "СБИС.СписокДокументов",
        "params": {"Тип": doc_type, "Фильтр": filt},
    }
    return _post_json(_SBIS_BASE + "/sbis-doc/service/",
                      body, headers={"X-SBISSessionID": session_id})


def sbis_doc_get(session_id: str, doc_id: str) -> dict:
    """Single document detail."""
    body = {
        "jsonrpc": "2.0", "id": 1,
        "method": "СБИС.ПрочитатьДокумент",
        "params": {"Идентификатор": doc_id},
    }
    return _post_json(_SBIS_BASE + "/sbis-doc/service/",
                      body, headers={"X-SBISSessionID": session_id})


def sbis_changes_since(session_id: str, since_iso: str) -> dict:
    """СБИС.СписокИзменений — все изменения после `since_iso`. Use for delta
    sync. ISO8601 with timezone."""
    body = {
        "jsonrpc": "2.0", "id": 1,
        "method": "СБИС.СписокИзменений",
        "params": {"С": since_iso},
    }
    return _post_json(_SBIS_BASE + "/sbis-doc/service/",
                      body, headers={"X-SBISSessionID": session_id})


# ============================================================
# Контур.Диадок
# ============================================================

_DIADOC_BASE = "https://diadoc-api.kontur.ru"


def diadoc_authenticate(api_key: str, login: str, password: str) -> dict:
    """Get auth token via password authentication."""
    body = f"login={urllib.parse.quote(login)}&password={urllib.parse.quote(password)}"
    req = urllib.request.Request(
        f"{_DIADOC_BASE}/V3/Authenticate?type=password",
        data=body.encode("utf-8"), method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"DiadocAuth ddauth_api_client_id={api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            token = resp.read().decode("utf-8")
            return {"ok": True, "data": {"auth_token": token}, "_meta": {"http_status": resp.status}}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": e.read()[:300].decode("utf-8", errors="replace"),
                "_meta": {"http_status": e.code}}


def diadoc_my_organizations(api_key: str, auth_token: str) -> dict:
    """List orgs the user has access to."""
    return _get_json(
        f"{_DIADOC_BASE}/GetMyOrganizations",
        headers={"Authorization": f"DiadocAuth ddauth_api_client_id={api_key},ddauth_token={auth_token}"},
    )


def diadoc_docs_list(api_key: str, auth_token: str, box_id: str,
                     filter_category: str = "Any.Inbound",
                     from_date: str | None = None, to_date: str | None = None) -> dict:
    """List docs in a box. filter_category: Any.Inbound / Any.Outbound /
    UniversalTransferDocument.Inbound.NotFinished / etc.
    Dates `dd.MM.yyyy`."""
    params = {"boxId": box_id, "filterCategory": filter_category}
    if from_date:
        params["fromDocumentDate"] = from_date
    if to_date:
        params["toDocumentDate"] = to_date
    return _get_json(
        f"{_DIADOC_BASE}/V3/GetDocuments?" + urllib.parse.urlencode(params),
        headers={"Authorization": f"DiadocAuth ddauth_api_client_id={api_key},ddauth_token={auth_token}"},
    )


def diadoc_get_event(api_key: str, auth_token: str, box_id: str, message_id: str) -> dict:
    """Get one event (document delivery / signature). For UPD use box+message."""
    return _get_json(
        f"{_DIADOC_BASE}/V3/GetEvent?boxId={box_id}&messageId={message_id}",
        headers={"Authorization": f"DiadocAuth ddauth_api_client_id={api_key},ddauth_token={auth_token}"},
    )
