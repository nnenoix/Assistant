"""Gmail tools.

Uses the same OAuth credentials as Drive/Sheets but requires the
gmail.modify scope (added in src/config.py). After this scope is added,
existing tokens may need to be reauthorized — old token.json files lacking
gmail.modify will fail with insufficient_scope when these tools are called.
The agent should call auth_add_account again or the user can delete the
token and re-login.
"""
import base64
from email.mime.text import MIMEText
from functools import lru_cache
from pathlib import Path

from googleapiclient.discovery import build

from src.auth import get_credentials


DEFAULT_ACCOUNT = "main"


@lru_cache(maxsize=8)
def _service(account: str = DEFAULT_ACCOUNT):
    return build("gmail", "v1", credentials=get_credentials(account), cache_discovery=False)


def search(query: str, max_results: int = 20, account: str = DEFAULT_ACCOUNT) -> list[dict]:
    """Search emails using Gmail's query syntax (same as the search bar):
    'from:elena', 'has:attachment', 'subject:invoice', 'newer_than:7d', etc.
    Returns slim metadata per match (id, threadId, snippet, from, subject, date).
    """
    svc = _service(account)
    resp = svc.users().messages().list(
        userId="me", q=query, maxResults=min(max_results, 100)
    ).execute()
    out = []
    for m in resp.get("messages", []):
        msg = svc.users().messages().get(
            userId="me", id=m["id"], format="metadata",
            metadataHeaders=["From", "To", "Subject", "Date"],
        ).execute()
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        out.append({
            "id": msg["id"],
            "thread_id": msg["threadId"],
            "snippet": msg.get("snippet", ""),
            "from": headers.get("From"),
            "to": headers.get("To"),
            "subject": headers.get("Subject"),
            "date": headers.get("Date"),
            "labels": msg.get("labelIds", []),
        })
    return out


def _walk_parts(parts):
    """Recursively yield (mime_type, body_bytes, filename, attachment_id) tuples."""
    for p in parts or []:
        body = p.get("body", {})
        data = body.get("data")
        filename = p.get("filename") or None
        att_id = body.get("attachmentId")
        if data:
            yield p.get("mimeType"), base64.urlsafe_b64decode(data), filename, att_id
        elif att_id:
            yield p.get("mimeType"), None, filename, att_id
        if p.get("parts"):
            yield from _walk_parts(p["parts"])


def get_message(message_id: str, account: str = DEFAULT_ACCOUNT) -> dict:
    """Full message: headers, plain-text body, and list of attachments."""
    svc = _service(account)
    msg = svc.users().messages().get(userId="me", id=message_id, format="full").execute()
    payload = msg.get("payload", {})
    headers = {h["name"]: h["value"] for h in payload.get("headers", [])}

    text_body, html_body, attachments = "", "", []
    parts = payload.get("parts") or [payload]
    for mime_type, raw, filename, att_id in _walk_parts(parts):
        if filename:
            attachments.append({
                "filename": filename,
                "mime_type": mime_type,
                "attachment_id": att_id,
            })
        elif mime_type == "text/plain" and raw and not text_body:
            try: text_body = raw.decode("utf-8", errors="replace")
            except Exception: pass
        elif mime_type == "text/html" and raw and not html_body:
            try: html_body = raw.decode("utf-8", errors="replace")
            except Exception: pass

    return {
        "id": msg["id"],
        "thread_id": msg["threadId"],
        "from": headers.get("From"),
        "to": headers.get("To"),
        "cc": headers.get("Cc"),
        "subject": headers.get("Subject"),
        "date": headers.get("Date"),
        "labels": msg.get("labelIds", []),
        "snippet": msg.get("snippet", ""),
        "body_text": text_body[:20000],  # cap to keep token-friendly
        "body_html_present": bool(html_body),
        "attachments": attachments,
    }


def download_attachment(
    message_id: str,
    attachment_id: str,
    dest_path: str,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Save an attachment to a local file."""
    svc = _service(account)
    att = svc.users().messages().attachments().get(
        userId="me", messageId=message_id, id=attachment_id
    ).execute()
    raw = base64.urlsafe_b64decode(att.get("data", ""))
    p = Path(dest_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(raw)
    return {"path": str(p.resolve()), "bytes_written": len(raw)}


def create_draft(
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    bcc: str | None = None,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Create a draft email (does NOT send). Returns the draft id and url."""
    msg = MIMEText(body, _charset="utf-8")
    msg["to"] = to
    msg["subject"] = subject
    if cc:
        msg["cc"] = cc
    if bcc:
        msg["bcc"] = bcc

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    svc = _service(account)
    draft = svc.users().drafts().create(
        userId="me", body={"message": {"raw": raw}}
    ).execute()
    return {
        "draft_id": draft["id"],
        "message_id": draft["message"]["id"],
    }


def send_draft(draft_id: str, account: str = DEFAULT_ACCOUNT) -> dict:
    """Send a previously created draft. SEPARATE from create_draft so the
    user can review before this is invoked — policy requires approval.
    """
    svc = _service(account)
    sent = svc.users().drafts().send(userId="me", body={"id": draft_id}).execute()
    return {
        "sent_message_id": sent["id"],
        "thread_id": sent.get("threadId"),
    }


def list_labels(account: str = DEFAULT_ACCOUNT) -> list[dict]:
    svc = _service(account)
    resp = svc.users().labels().list(userId="me").execute()
    return [{"id": l["id"], "name": l["name"], "type": l.get("type")} for l in resp.get("labels", [])]
