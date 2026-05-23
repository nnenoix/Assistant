"""Outbound messaging: SMS gateways + Telegram Bot API + IMAP read.

SMS.ru and SMSC.ru both expose simple GET-based APIs with JSON or
plaintext responses. Telegram Bot API is the standard webhook-friendly
bot endpoint. IMAP is for reading supplier email (поставщики, ОФД, банки
which still email statements).
"""
from __future__ import annotations

import imaplib
import json
import urllib.error
import urllib.parse
import urllib.request
from email import message_from_bytes
from email.header import decode_header
from typing import Any


def _http_get_json(url: str, timeout: int = 30) -> dict:
    """GET → JSON. Returns {ok, data, error?, _meta:{http_status}}.

    Tolerates non-JSON 2xx bodies (SMSC.ru sometimes returns plain text
    or HTML on success when `fmt` isn't set) — the body lands under
    `data.raw` so the caller can still inspect it."""
    from src.tools._vendor_http import request_raw
    code, _hdr, raw = request_raw("GET", url, timeout=timeout)
    if code >= 400:
        return {"ok": False, "error": raw[:300].decode("utf-8", errors="replace"),
                "_meta": {"http_status": code}}
    try:
        return {"ok": True, "data": json.loads(raw.decode("utf-8")),
                "_meta": {"http_status": code}}
    except json.JSONDecodeError:
        return {"ok": True,
                "data": {"raw": raw.decode("utf-8", errors="replace")},
                "_meta": {"http_status": code}}


# ============================================================
# SMS.ru
# ============================================================

def smsru_send(api_id: str, to: str, msg: str, from_: str | None = None,
               test: int = 0, dry_run: bool = False) -> dict:
    """SMS.ru `/sms/send`. `to` E.164 (79999999999). `test=1` simulates on
    SMS.ru side (balance untouched). `dry_run=True` returns a local preview
    without contacting SMS.ru at all — useful to verify the recipient + message
    before approving the real send."""
    if dry_run:
        return {
            "ok": True, "dry_run": True, "executed": False,
            "plan": {
                "would_call": "sms.ru /sms/send",
                "to": to, "msg": msg, "msg_length": len(msg),
                "estimated_segments": (len(msg) + 69) // 70,  # cyrillic: 70 chars/segment
                "from": from_,
                "test_mode": bool(test),
                "reversibility": "NOT REVERSIBLE — once sent SMS.ru bills your balance and delivers.",
            },
            "_meta": {"native_preview": True},
        }
    params = {"api_id": api_id, "to": to, "msg": msg, "json": 1, "test": test}
    if from_:
        params["from"] = from_
    return _http_get_json("https://sms.ru/sms/send?" + urllib.parse.urlencode(params))


def smsru_balance(api_id: str) -> dict:
    """Current SMS.ru balance in RUB."""
    return _http_get_json(f"https://sms.ru/my/balance?api_id={api_id}&json=1")


def smsru_status(api_id: str, sms_id: str) -> dict:
    """Delivery status of one SMS."""
    return _http_get_json(f"https://sms.ru/sms/status?api_id={api_id}&sms_id={sms_id}&json=1")


# ============================================================
# SMSC.ru
# ============================================================

def smsc_send(login: str, password: str, phones: str, mes: str,
              sender: str | None = None, dry_run: bool = False) -> dict:
    """SMSC.ru `/send`. `phones` comma-separated. Optional `sender` (alphanumeric
    sender id, must be pre-approved by SMSC). `dry_run=True` returns a preview
    without contacting SMSC."""
    if dry_run:
        phone_list = [p.strip() for p in phones.split(",") if p.strip()]
        return {
            "ok": True, "dry_run": True, "executed": False,
            "plan": {
                "would_call": "smsc.ru /sys/send.php",
                "phones": phone_list,
                "recipient_count": len(phone_list),
                "msg": mes, "msg_length": len(mes),
                "estimated_segments_per_recipient": (len(mes) + 69) // 70,
                "sender": sender,
                "reversibility": "NOT REVERSIBLE — once sent SMSC bills + delivers.",
            },
            "_meta": {"native_preview": True},
        }
    params = {"login": login, "psw": password, "phones": phones, "mes": mes, "fmt": 3}
    if sender:
        params["sender"] = sender
    return _http_get_json("https://smsc.ru/sys/send.php?" + urllib.parse.urlencode(params))


def smsc_balance(login: str, password: str) -> dict:
    """SMSC.ru balance."""
    return _http_get_json(
        f"https://smsc.ru/sys/balance.php?login={login}&psw={password}&fmt=3"
    )


def smsc_status(login: str, password: str, phone: str, sms_id: str) -> dict:
    """Per-message status."""
    return _http_get_json(
        f"https://smsc.ru/sys/status.php?login={login}&psw={password}&phone={phone}&id={sms_id}&fmt=3"
    )


# ============================================================
# Telegram Bot API
# ============================================================

_TG_BASE = "https://api.telegram.org/bot"


def _tg_post(bot_token: str, method: str, payload: dict, timeout: int = 30) -> dict:
    """POST to Telegram Bot API. The bot_token is part of the URL path
    (NOT a header) so we build the URL here and delegate transport."""
    from src.tools._vendor_http import post_json
    return post_json(f"{_TG_BASE}{bot_token}/{method}", payload, timeout=timeout)


def tg_send_message(bot_token: str, chat_id: int | str, text: str,
                    parse_mode: str | None = None,
                    disable_web_page_preview: bool = True,
                    dry_run: bool = False) -> dict:
    """Post a message to a Telegram chat. parse_mode: HTML or MarkdownV2.
    `dry_run=True` returns a preview without sending."""
    if dry_run:
        return {
            "ok": True, "dry_run": True, "executed": False,
            "plan": {
                "would_call": "telegram /sendMessage",
                "chat_id": chat_id,
                "text_length": len(text),
                "text_preview": text[:200] + ("..." if len(text) > 200 else ""),
                "parse_mode": parse_mode,
                "disable_web_page_preview": disable_web_page_preview,
                "reversibility": (
                    "Limited: Telegram allows edit/delete within 48h via "
                    "editMessageText / deleteMessage, but recipients may have "
                    "already seen the original."
                ),
            },
            "_meta": {"native_preview": True},
        }
    payload: dict = {"chat_id": chat_id, "text": text,
                     "disable_web_page_preview": disable_web_page_preview}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return _tg_post(bot_token, "sendMessage", payload)


def tg_send_photo(bot_token: str, chat_id: int | str, photo_url: str,
                  caption: str | None = None) -> dict:
    """Send a photo by URL. For files use multipart upload — out of scope here."""
    payload: dict = {"chat_id": chat_id, "photo": photo_url}
    if caption:
        payload["caption"] = caption
    return _tg_post(bot_token, "sendPhoto", payload)


def tg_get_updates(bot_token: str, offset: int = 0, timeout: int = 30) -> dict:
    """Poll for incoming bot updates. `offset` = last update_id + 1."""
    return _tg_post(bot_token, "getUpdates", {"offset": offset, "timeout": timeout},
                    timeout=timeout + 5)


def tg_get_me(bot_token: str) -> dict:
    """Verify bot token. Returns {result:{id, is_bot, first_name, username}}."""
    return _tg_post(bot_token, "getMe", {})


# ============================================================
# IMAP read
# ============================================================

def imap_recent(host: str, port: int, user: str, password: str,
                folder: str = "INBOX", since_days: int = 1,
                use_ssl: bool = True, limit: int = 20) -> dict:
    """List recent messages in an IMAP folder. Returns {messages: [{subject, from, date, uid}]}."""
    import datetime as _dt
    try:
        if use_ssl:
            mail = imaplib.IMAP4_SSL(host, port)
        else:
            mail = imaplib.IMAP4(host, port)
        mail.login(user, password)
        mail.select(folder, readonly=True)
        since = (_dt.datetime.utcnow() - _dt.timedelta(days=since_days)).strftime("%d-%b-%Y")
        status, data = mail.uid("SEARCH", None, f'(SINCE {since})')
        if status != "OK":
            return {"ok": False, "error": f"search failed: {status}"}
        uids = (data[0] or b"").split()[-limit:]
        out: list[dict] = []
        for uid in uids:
            status, msg_data = mail.uid("FETCH", uid, "(BODY[HEADER.FIELDS (SUBJECT FROM DATE)])")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            m = message_from_bytes(raw)
            def _decode(h):
                if h is None:
                    return None
                parts = decode_header(h)
                return "".join(
                    p.decode(enc or "utf-8", errors="replace") if isinstance(p, bytes) else p
                    for p, enc in parts
                )
            out.append({
                "uid": uid.decode(),
                "subject": _decode(m.get("Subject")),
                "from": _decode(m.get("From")),
                "date": m.get("Date"),
            })
        mail.logout()
        return {"ok": True, "messages": out, "_meta": {"folder": folder, "since_days": since_days}}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


def imap_fetch_body(host: str, port: int, user: str, password: str,
                    uid: str, folder: str = "INBOX", use_ssl: bool = True) -> dict:
    """Fetch one message body (text/plain preferred). Returns
    {subject, from, body_text, attachments:[{filename, size, content_type}]}."""
    try:
        mail = imaplib.IMAP4_SSL(host, port) if use_ssl else imaplib.IMAP4(host, port)
        mail.login(user, password)
        mail.select(folder, readonly=True)
        status, msg_data = mail.uid("FETCH", uid, "(RFC822)")
        if status != "OK":
            return {"ok": False, "error": f"fetch failed: {status}"}
        m = message_from_bytes(msg_data[0][1])
        body_text = None
        attachments: list[dict] = []
        for part in m.walk():
            ctype = part.get_content_type()
            disp = part.get("Content-Disposition", "")
            if "attachment" in disp:
                filename = part.get_filename()
                payload = part.get_payload(decode=True) or b""
                attachments.append({
                    "filename": filename, "content_type": ctype, "size": len(payload),
                })
            elif ctype == "text/plain" and body_text is None:
                payload = part.get_payload(decode=True) or b""
                body_text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        mail.logout()
        return {
            "ok": True,
            "uid": uid,
            "subject": m.get("Subject"),
            "from": m.get("From"),
            "body_text": body_text,
            "attachments": attachments,
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
