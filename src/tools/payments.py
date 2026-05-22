"""Russian payment acquiring: ЮKassa + Тинькофф Acquiring.

ЮKassa is the most-common online checkout for Russian sellers (formerly
Yandex Money). Тинькофф Acquiring is the bank's own payment gateway.
Both expose REST endpoints to query payments, refunds, payouts.
"""
from __future__ import annotations

import base64
import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request


# ============================================================
# ЮKassa
# ============================================================

_YK_BASE = "https://api.yookassa.ru/v3"


def _yk_request(path: str, shop_id: str, secret: str,
                method: str = "GET", params: dict | None = None,
                body: dict | None = None, idempotence_key: str | None = None,
                timeout: int = 60) -> tuple[int, dict, bytes]:
    url = f"{_YK_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    auth = base64.b64encode(f"{shop_id}:{secret}".encode("utf-8")).decode("ascii")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Authorization": f"Basic {auth}",
        "Accept": "application/json",
        **({"Content-Type": "application/json"} if body is not None else {}),
    }
    if idempotence_key:
        headers["Idempotence-Key"] = idempotence_key
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()


def _yk_call(path: str, shop_id: str, secret: str, **kwargs) -> dict:
    code, hdr, raw = _yk_request(path, shop_id, secret, **kwargs)
    meta = {"http_status": code}
    if code >= 400:
        return {
            "ok": False,
            "error_kind": "permission" if code in (401, 403) else ("not_found" if code == 404 else "rate_limit" if code == 429 else "server" if code >= 500 else "bad_input"),
            "error": raw[:300].decode("utf-8", errors="replace"),
            "_meta": meta,
        }
    try:
        return {"ok": True, "data": json.loads(raw.decode("utf-8")), "_meta": meta}
    except json.JSONDecodeError as e:
        return {"ok": False, "error_kind": "bad_input", "error": f"non-JSON: {e}", "_meta": meta}


def yookassa_payments_list(shop_id: str, secret: str,
                           created_gte: str | None = None,
                           created_lte: str | None = None,
                           status: str | None = None,
                           limit: int = 100,
                           cursor: str | None = None) -> dict:
    """List payments (`/payments`). Dates ISO8601. status: pending,
    waiting_for_capture, succeeded, canceled."""
    params: dict = {"limit": limit}
    if created_gte: params["created_at.gte"] = created_gte
    if created_lte: params["created_at.lte"] = created_lte
    if status: params["status"] = status
    if cursor: params["cursor"] = cursor
    return _yk_call("/payments", shop_id, secret, params=params)


def yookassa_payment_get(shop_id: str, secret: str, payment_id: str) -> dict:
    """One payment by id."""
    return _yk_call(f"/payments/{payment_id}", shop_id, secret)


def yookassa_refunds_list(shop_id: str, secret: str,
                          created_gte: str | None = None, limit: int = 100) -> dict:
    """List refunds."""
    params: dict = {"limit": limit}
    if created_gte: params["created_at.gte"] = created_gte
    return _yk_call("/refunds", shop_id, secret, params=params)


def yookassa_payouts_list(shop_id: str, secret: str, limit: int = 100) -> dict:
    """List payouts (settlement money out)."""
    return _yk_call("/payouts", shop_id, secret, params={"limit": limit})


def yookassa_receipts_list(shop_id: str, secret: str,
                           created_gte: str | None = None, limit: int = 100) -> dict:
    """Fiscal receipts (54-ФЗ)."""
    params: dict = {"limit": limit}
    if created_gte: params["created_at.gte"] = created_gte
    return _yk_call("/receipts", shop_id, secret, params=params)


def yookassa_verify_webhook(raw_body: str, signature_header: str,
                            cert_pem: str | None = None) -> dict:
    """Verify a ЮKassa webhook callback. ЮKassa uses RSA over the raw body
    (not HMAC). Pass the `cert_pem` content (ЮKassa publishes a rotating
    cert chain — caller fetches from https://yookassa.ru/files/ssl/...).
    Returns {ok, valid, error?}.

    Lazy-imports `cryptography` — returns a hint if not installed."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding as _padding
        from cryptography.x509 import load_pem_x509_certificate
    except ImportError:
        return {"ok": False, "error": "cryptography not installed",
                "fix_hint": "pip install cryptography"}
    if not cert_pem:
        return {"ok": False, "error": "cert_pem required for cert-based verification"}
    try:
        import base64
        cert = load_pem_x509_certificate(cert_pem.encode("utf-8"))
        pubkey = cert.public_key()
        sig_bytes = base64.b64decode(signature_header)
        pubkey.verify(
            sig_bytes,
            raw_body.encode("utf-8"),
            _padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return {"ok": True, "data": {"valid": True}}
    except Exception as e:
        return {"ok": True, "data": {"valid": False, "error": str(e)[:200]}}


# ============================================================
# Тинькофф Acquiring (E2C)
# ============================================================

_TINK_BASE = "https://securepay.tinkoff.ru/v2"


def _tinkoff_post(terminal_key: str, password: str, path: str,
                  body: dict, timeout: int = 60) -> dict:
    """Tinkoff requires SHA-256 token = sorted keys + password. We compute it."""
    payload = dict(body)
    payload["TerminalKey"] = terminal_key
    # Build token: sort keys, concat values, append password, sha256
    token_src = "".join(str(payload[k]) for k in sorted(payload) if not isinstance(payload[k], (dict, list)))
    token_src += password
    payload["Token"] = hashlib.sha256(token_src.encode("utf-8")).hexdigest()
    url = f"{_TINK_BASE}{path}"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"ok": True, "data": json.loads(resp.read().decode("utf-8")),
                    "_meta": {"http_status": resp.status}}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": e.read()[:300].decode("utf-8", errors="replace"),
                "_meta": {"http_status": e.code}}


def tinkoff_get_state(terminal_key: str, password: str, payment_id: str) -> dict:
    """Tinkoff `/GetState` — single-payment status. Returns Status + ErrorCode."""
    return _tinkoff_post(terminal_key, password, "/GetState", {"PaymentId": payment_id})


def tinkoff_get_customer(terminal_key: str, password: str, customer_key: str) -> dict:
    """`/GetCustomer` — saved-card / customer profile."""
    return _tinkoff_post(terminal_key, password, "/GetCustomer", {"CustomerKey": customer_key})


def tinkoff_check_order(terminal_key: str, password: str, order_id: str) -> dict:
    """`/CheckOrder` — every payment attempt for `OrderId`."""
    return _tinkoff_post(terminal_key, password, "/CheckOrder", {"OrderId": order_id})


def tinkoff_get_terminal_payouts(terminal_key: str, password: str,
                                 from_date: str, to_date: str) -> dict:
    """`/GetTerminalPayouts` — settlement money out for [from_date..to_date].
    Dates `2026-05-01`."""
    return _tinkoff_post(terminal_key, password, "/GetTerminalPayouts",
                         {"From": from_date, "To": to_date})
