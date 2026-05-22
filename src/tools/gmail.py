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

from src.auth import RetryingHttpRequest, get_credentials


DEFAULT_ACCOUNT = "main"


@lru_cache(maxsize=8)
def _service(account: str = DEFAULT_ACCOUNT):
    return build(
        "gmail", "v1",
        credentials=get_credentials(account),
        cache_discovery=False,
        requestBuilder=RetryingHttpRequest,
    )


def search(query: str, max_results: int = 20, response_format: str = "concise",
           account: str = DEFAULT_ACCOUNT) -> dict:
    """Search emails using Gmail's query syntax (same as the search bar):
    'from:elena', 'has:attachment', 'subject:invoice', 'newer_than:7d', etc.

    Returns {messages, _meta}. `_meta.total_count` is Gmail's
    resultSizeEstimate (Google's own approximate match count — often the
    full count for the query, regardless of max_results). `_meta.truncated`
    is True when more results exist than were fetched.

    `response_format`:
      - "concise" (default): per-message `{id, from, subject, date, snippet[:120]}`.
        Saves ~70% tokens vs detailed; enough for triage / counting / replying.
      - "detailed": adds `to`, `thread_id`, full `snippet`, `labels`.
    """
    if response_format not in {"concise", "detailed"}:
        raise ValueError(f"response_format must be 'concise' or 'detailed', got {response_format!r}")
    svc = _service(account)
    capped = min(max_results, 100)
    resp = svc.users().messages().list(
        userId="me", q=query, maxResults=capped
    ).execute()
    msg_refs = resp.get("messages", []) or []
    out = []
    for m in msg_refs:
        msg = svc.users().messages().get(
            userId="me", id=m["id"], format="metadata",
            metadataHeaders=["From", "To", "Subject", "Date"],
        ).execute()
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        snippet = msg.get("snippet", "")
        if response_format == "concise":
            out.append({
                "id": msg["id"],
                "from": headers.get("From"),
                "subject": headers.get("Subject"),
                "date": headers.get("Date"),
                "snippet": snippet[:120],
            })
        else:
            out.append({
                "id": msg["id"],
                "thread_id": msg["threadId"],
                "snippet": snippet,
                "from": headers.get("From"),
                "to": headers.get("To"),
                "subject": headers.get("Subject"),
                "date": headers.get("Date"),
                "labels": msg.get("labelIds", []),
            })

    total_estimate = resp.get("resultSizeEstimate")
    has_more = bool(resp.get("nextPageToken")) or (
        total_estimate is not None and total_estimate > len(out)
    )
    return {
        "messages": out,
        "_meta": {
            "returned_count": len(out),
            "total_count": total_estimate,
            "truncated": has_more,
            "truncation_reason": (
                f"more results exist (estimate {total_estimate}); increase max_results (cap 100) or narrow query"
                if has_more else None
            ),
            "empty_reason": None if out else "no_matches",
            "max_results_used": capped,
        },
    }


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


def send_draft(draft_id: str, dry_run: bool = False, account: str = DEFAULT_ACCOUNT) -> dict:
    """Send a previously created draft. SEPARATE from create_draft so the
    user can review before this is invoked — policy requires approval.

    With `dry_run=True` fetches the draft's headers (To/Subject/etc.) and
    returns a preview WITHOUT sending — verify recipients + subject before
    approving the real send.
    """
    svc = _service(account)
    if dry_run:
        try:
            d = svc.users().drafts().get(
                userId="me", id=draft_id, format="metadata",
                metadataHeaders=["To", "Cc", "Bcc", "Subject", "From"],
            ).execute()
        except Exception as e:
            return {
                "dry_run": True,
                "executed": False,
                "plan": {
                    "would_call": "gmail.users.drafts.send",
                    "draft_id": draft_id,
                    "preview_error": str(e)[:200],
                    "note": "Could not fetch draft for preview; check the draft_id.",
                },
                "_meta": {"native_preview": True},
            }
        headers = {
            h["name"]: h["value"]
            for h in (d.get("message", {}).get("payload", {}).get("headers", []) or [])
        }
        return {
            "dry_run": True,
            "executed": False,
            "plan": {
                "would_call": "gmail.users.drafts.send",
                "draft_id": draft_id,
                "to": headers.get("To"),
                "cc": headers.get("Cc"),
                "bcc": headers.get("Bcc"),
                "subject": headers.get("Subject"),
                "from": headers.get("From"),
                "thread_id": d.get("message", {}).get("threadId"),
                "reversibility": (
                    "NOT REVERSIBLE — once sent the message is in the "
                    "recipient's mailbox. Cancel via gmail_delete_draft "
                    "before approving."
                ),
            },
            "_meta": {"native_preview": True},
        }
    sent = svc.users().drafts().send(userId="me", body={"id": draft_id}).execute()
    return {
        "sent_message_id": sent["id"],
        "thread_id": sent.get("threadId"),
    }


def list_labels(account: str = DEFAULT_ACCOUNT) -> list[dict]:
    svc = _service(account)
    resp = svc.users().labels().list(userId="me").execute()
    return [{"id": l["id"], "name": l["name"], "type": l.get("type")} for l in resp.get("labels", [])]


# -------- Phase 5: write-ops --------

def get_thread(thread_id: str, account: str = DEFAULT_ACCOUNT) -> dict:
    """Read every message in a thread. Returns {thread_id, messages, _meta}.

    Each message has id, from, to, subject, date, snippet, body_text,
    body_html_present. Order: oldest → newest (Gmail's default).
    """
    svc = _service(account)
    resp = svc.users().threads().get(userId="me", id=thread_id, format="full").execute()
    messages = []
    for m in resp.get("messages", []) or []:
        payload = m.get("payload", {})
        headers = {h["name"]: h["value"] for h in payload.get("headers", [])}
        text_body, html_body = "", ""
        parts = payload.get("parts") or [payload]
        for mime_type, raw, _filename, _att_id in _walk_parts(parts):
            if mime_type == "text/plain" and raw and not text_body:
                try: text_body = raw.decode("utf-8", errors="replace")
                except Exception: pass
            elif mime_type == "text/html" and raw and not html_body:
                try: html_body = raw.decode("utf-8", errors="replace")
                except Exception: pass
        messages.append({
            "id": m["id"],
            "from": headers.get("From"),
            "to": headers.get("To"),
            "subject": headers.get("Subject"),
            "date": headers.get("Date"),
            "snippet": m.get("snippet", ""),
            "labels": m.get("labelIds", []),
            "body_text": text_body[:8000],
            "body_html_present": bool(html_body),
        })
    return {
        "thread_id": thread_id,
        "messages": messages,
        "_meta": {
            "message_count": len(messages),
            "empty_reason": None if messages else "no_messages",
        },
    }


def _build_reply_draft(
    *,
    original_message_id: str,
    to: str,
    cc: str | None,
    subject: str,
    body: str,
    in_reply_to: str | None,
    references: str | None,
    thread_id: str | None,
) -> tuple[dict, MIMEText]:
    """Construct a draft body dict + the MIMEText (for inspection in tests).

    Carries In-Reply-To / References headers so Gmail threads it correctly.
    """
    msg = MIMEText(body, _charset="utf-8")
    msg["to"] = to
    msg["subject"] = subject
    if cc:
        msg["cc"] = cc
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    inner = {"raw": raw}
    if thread_id:
        inner["threadId"] = thread_id
    return {"message": inner}, msg


def reply(
    message_id: str,
    body: str,
    reply_all: bool = False,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Create a DRAFT reply to a message. Honors threading headers.

    Policy: always creates a draft; never sends. Caller uses gmail_send_draft
    after user approval. `reply_all=True` includes all original recipients.
    """
    svc = _service(account)
    # Fetch original headers + thread id
    original = svc.users().messages().get(
        userId="me", id=message_id, format="metadata",
        metadataHeaders=["From", "To", "Cc", "Subject", "Message-ID", "References"],
    ).execute()
    headers = {h["name"]: h["value"] for h in original.get("payload", {}).get("headers", [])}
    msg_id_header = headers.get("Message-ID") or headers.get("Message-Id")
    orig_subject = headers.get("Subject", "")
    reply_subject = orig_subject if orig_subject.lower().startswith("re:") else f"Re: {orig_subject}"
    references = (headers.get("References", "") + " " + (msg_id_header or "")).strip() or None

    to = headers.get("From", "")
    cc = None
    if reply_all:
        # Combine To + Cc of the original (excluding our own address best-effort —
        # Gmail's UI handles "self" gracefully and dedupes on send)
        orig_to = headers.get("To", "")
        orig_cc = headers.get("Cc", "")
        cc = ", ".join([x for x in [orig_to, orig_cc] if x]) or None

    body_dict, _mime = _build_reply_draft(
        original_message_id=message_id,
        to=to,
        cc=cc,
        subject=reply_subject,
        body=body,
        in_reply_to=msg_id_header,
        references=references,
        thread_id=original.get("threadId"),
    )
    draft = svc.users().drafts().create(userId="me", body=body_dict).execute()
    return {
        "draft_id": draft["id"],
        "thread_id": draft["message"].get("threadId"),
        "subject": reply_subject,
        "reply_all": reply_all,
    }


def forward(
    message_id: str,
    to: str,
    body: str | None = None,
    cc: str | None = None,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Create a DRAFT forwarding a message. Optional `body` is inserted before
    the quoted original. The original headers + body are included as a quote.
    """
    svc = _service(account)
    original = svc.users().messages().get(userId="me", id=message_id, format="full").execute()
    headers = {h["name"]: h["value"] for h in original.get("payload", {}).get("headers", [])}
    orig_subject = headers.get("Subject", "")
    fwd_subject = orig_subject if orig_subject.lower().startswith("fwd:") else f"Fwd: {orig_subject}"

    # Extract original plain text body
    orig_text = ""
    parts = original.get("payload", {}).get("parts") or [original.get("payload", {})]
    for mime_type, raw, _f, _a in _walk_parts(parts):
        if mime_type == "text/plain" and raw and not orig_text:
            try: orig_text = raw.decode("utf-8", errors="replace")
            except Exception: pass

    intro = (body + "\n\n") if body else ""
    quoted_header = (
        f"---------- Forwarded message ----------\n"
        f"From: {headers.get('From', '')}\n"
        f"Date: {headers.get('Date', '')}\n"
        f"Subject: {orig_subject}\n"
        f"To: {headers.get('To', '')}\n\n"
    )
    full_body = intro + quoted_header + orig_text

    msg = MIMEText(full_body, _charset="utf-8")
    msg["to"] = to
    msg["subject"] = fwd_subject
    if cc:
        msg["cc"] = cc
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    draft = svc.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
    return {
        "draft_id": draft["id"],
        "subject": fwd_subject,
        "forwarded_from": message_id,
    }


def modify_labels(
    message_id: str,
    add: list[str] | None = None,
    remove: list[str] | None = None,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Add/remove labels on a message. `add` and `remove` are lists of label
    IDs (NOT names — use gmail_list_labels to resolve names → ids).

    System label IDs: INBOX, UNREAD, STARRED, IMPORTANT, SPAM, TRASH,
    SENT, DRAFT. Plus user-defined label IDs which look like `Label_123`.
    """
    body = {}
    if add:
        body["addLabelIds"] = add
    if remove:
        body["removeLabelIds"] = remove
    if not body:
        raise ValueError("must pass either `add` or `remove` (or both)")
    resp = _service(account).users().messages().modify(
        userId="me", id=message_id, body=body,
    ).execute()
    return {
        "ok": True,
        "message_id": message_id,
        "labels_after": resp.get("labelIds", []),
        "added": add or [],
        "removed": remove or [],
    }


def archive(message_id: str, account: str = DEFAULT_ACCOUNT) -> dict:
    """Archive: remove INBOX label."""
    return modify_labels(message_id, remove=["INBOX"], account=account)


def mark_read(message_id: str, account: str = DEFAULT_ACCOUNT) -> dict:
    """Mark a message as read: remove UNREAD label."""
    return modify_labels(message_id, remove=["UNREAD"], account=account)


def mark_unread(message_id: str, account: str = DEFAULT_ACCOUNT) -> dict:
    """Mark a message as unread: add UNREAD label."""
    return modify_labels(message_id, add=["UNREAD"], account=account)


def batch_modify(
    message_ids: list[str],
    add: list[str] | None = None,
    remove: list[str] | None = None,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Bulk label modify across many messages in ONE call.

    Use for «архивировать все письма от X старше года»: gmail_search →
    extract ids → batch_modify(remove=['INBOX']).
    """
    if not message_ids:
        return {"ok": True, "count": 0, "empty_reason": "no_ids"}
    body = {"ids": message_ids}
    if add:
        body["addLabelIds"] = add
    if remove:
        body["removeLabelIds"] = remove
    if "addLabelIds" not in body and "removeLabelIds" not in body:
        raise ValueError("must pass either `add` or `remove`")
    _service(account).users().messages().batchModify(userId="me", body=body).execute()
    return {
        "ok": True,
        "count": len(message_ids),
        "added": add or [],
        "removed": remove or [],
    }


def list_filters(account: str = DEFAULT_ACCOUNT) -> dict:
    """List all Gmail filter rules. Returns {filters, _meta}."""
    resp = _service(account).users().settings().filters().list(userId="me").execute()
    filters = resp.get("filter", []) or []
    return {
        "filters": filters,
        "_meta": {
            "count": len(filters),
            "empty_reason": None if filters else "no_filters",
        },
    }


def create_filter(
    criteria: dict,
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
    forward_to: str | None = None,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Create a Gmail filter rule.

    `criteria` examples:
      {"from": "noreply@github.com"}
      {"subject": "invoice", "hasAttachment": True}
      {"query": "from:bank.com newer_than:30d"}  # any Gmail search query
    """
    action = {}
    if add_labels:
        action["addLabelIds"] = add_labels
    if remove_labels:
        action["removeLabelIds"] = remove_labels
    if forward_to:
        action["forward"] = forward_to
    if not action:
        raise ValueError("must specify at least one action (add_labels / remove_labels / forward_to)")
    resp = _service(account).users().settings().filters().create(
        userId="me",
        body={"criteria": criteria, "action": action},
    ).execute()
    return {"ok": True, "filter_id": resp.get("id"), "criteria": criteria, "action": action}


def delete_filter(filter_id: str, account: str = DEFAULT_ACCOUNT) -> dict:
    """Delete a Gmail filter by id."""
    _service(account).users().settings().filters().delete(
        userId="me", id=filter_id,
    ).execute()
    return {"ok": True, "filter_id": filter_id}
