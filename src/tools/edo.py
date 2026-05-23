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
import urllib.parse
from typing import Any

from src.tools._vendor_http import get_json as _get_json, post_json as _post_json


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


def diadoc_sign_xml_skeleton(xml_bytes: bytes, cert_path: str,
                             pin: str | None = None) -> dict:
    """SCAFFOLD — outbound document signing for Контур.Диадок.

    Реальный поток в России (ФЗ-63 + Приказ ФНС): подпись через CryptoPro CSP
    либо OpenSSL+GOST engine. Получатель: CAdES-BES (XMLDSig with GOST 2012).
    Этот скелет:
      - Принимает байты XML + путь к сертификату + PIN
      - Возвращает {ok, signed_xml | error, signature_format}
      - При установленном `pycades` (CryptoPro Python binding) — реально подписывает
      - Без `pycades` возвращает структурированный fix_hint

    Для PROD реализации нужен установленный CryptoPro CSP + лицензия на
    GOST-движок и Python обёртка `pycades` (или вызов capicom.exe через
    subprocess). Здесь — каркас для следующего инкремента."""
    try:
        import pycades  # type: ignore
    except ImportError:
        return {
            "ok": False, "error": "pycades not installed",
            "fix_hint": (
                "Install CryptoPro CSP first (Windows: CSP installer + ГОСТ-провайдер; "
                "Linux: cprocsp-rdr-gui + cprocsp-curl). Then `pip install pycades`."
            ),
            "signature_format": "CAdES-BES (XMLDSig GOST 2012)",
            "_meta": {"native_preview": False, "scaffold": True},
        }
    try:
        # Real signing happens here in production. The pycades API is:
        #   store = pycades.Store(); store.Open(...)
        #   cert = store.Certificates.Find(...)
        #   signer = pycades.Signer(); signer.Certificate = cert
        #   signed_data = pycades.SignedData(); signed_data.Content = xml_bytes.decode("utf-8")
        #   sig = signed_data.SignCades(signer, pycades.CADESCOM_CADES_BES, True)
        # Returning a placeholder so the scaffold compiles.
        return {
            "ok": False,
            "error": "pycades available but signing flow not yet implemented",
            "fix_hint": "Implement CAdES-BES signing in src/tools/edo.py:diadoc_sign_xml_skeleton.",
            "_meta": {"scaffold": True, "next_step": "cades_bes_impl"},
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "_meta": {}}
