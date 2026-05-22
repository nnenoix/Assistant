"""Service-layer helpers: webhook hooks, distributed locks, OTel spans,
notification routing, markdown report renderer.

These are the «командный сервис» primitives from the architecture roadmap
(Phase 1). They run in-process on top of file storage so the agent can
exercise the patterns before the FastAPI/Postgres/arq stack lands.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config import DATA_DIR


_SERVICE_DIR = DATA_DIR / "service"
_SERVICE_DIR.mkdir(parents=True, exist_ok=True)
_WEBHOOKS_PATH = _SERVICE_DIR / "webhooks.jsonl"
_LOCKS_DIR = _SERVICE_DIR / "locks"
_LOCKS_DIR.mkdir(parents=True, exist_ok=True)
_TRACE_PATH = _SERVICE_DIR / "traces.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_jsonl(path: Path, record: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


# ============================================================
# Webhook log + signature verification
# ============================================================

def webhook_log(source: str, payload: dict, headers: dict | None = None,
                signature_valid: bool | None = None) -> dict:
    """Append an incoming webhook payload to the log. source: yookassa,
    tinkoff, telegram, wb_finance_notify, etc."""
    record = {
        "webhook_id": uuid.uuid4().hex,
        "received_at": _now_iso(),
        "source": source,
        "payload": payload,
        "headers": headers or {},
        "signature_valid": signature_valid,
    }
    _append_jsonl(_WEBHOOKS_PATH, record)
    return {"ok": True, "data": record}


def webhook_recent(source: str | None = None, limit: int = 50) -> dict:
    """Recent webhooks, latest first."""
    records = _read_jsonl(_WEBHOOKS_PATH)
    if source:
        records = [r for r in records if r.get("source") == source]
    records.sort(key=lambda r: r.get("received_at") or "", reverse=True)
    return {"ok": True, "data": {"rows": records[:limit], "count": len(records)}}


def webhook_verify_signature(secret: str, raw_body: str, received_signature: str,
                             algorithm: str = "sha256") -> dict:
    """Verify HMAC-{algorithm} signature on a raw body. algorithm: sha256, sha1.
    Used to validate ЮKassa / Tinkoff / WB callbacks before trusting payload."""
    if algorithm not in {"sha256", "sha1"}:
        return {"ok": False, "error": f"unsupported algorithm {algorithm!r}"}
    h = hmac.new(secret.encode("utf-8"), raw_body.encode("utf-8"),
                 hashlib.sha256 if algorithm == "sha256" else hashlib.sha1)
    expected = h.hexdigest()
    return {
        "ok": True,
        "data": {
            "valid": hmac.compare_digest(expected, received_signature.lower()),
            "expected_signature": expected,
        },
    }


# ============================================================
# Distributed locks — file-based, single-machine
# ============================================================

_lock_registry: dict[str, threading.Lock] = {}
_registry_lock = threading.Lock()


def _proc_lock(name: str) -> threading.Lock:
    with _registry_lock:
        if name not in _lock_registry:
            _lock_registry[name] = threading.Lock()
        return _lock_registry[name]


def lock_acquire(name: str, ttl_seconds: int = 300,
                 wait_seconds: int = 0, owner: str = "agent") -> dict:
    """Acquire a named lock. Combines in-process threading.Lock with a
    file-marker for cross-process visibility. ttl_seconds: stale lock
    cleanup. wait_seconds=0 fails immediately if locked; >0 polls until."""
    path = _LOCKS_DIR / f"{name}.lock"
    plock = _proc_lock(name)
    deadline = time.monotonic() + wait_seconds
    while True:
        if plock.acquire(blocking=False):
            # File guard for cross-process
            if path.exists():
                try:
                    meta = json.loads(path.read_text(encoding="utf-8"))
                    age = time.time() - meta.get("acquired_ts", 0)
                    if age < ttl_seconds:
                        plock.release()
                        if time.monotonic() < deadline:
                            time.sleep(0.2)
                            continue
                        return {"ok": False, "error": "locked",
                                "data": {"held_by": meta.get("owner"), "age_s": age}}
                except Exception:
                    pass
            token = uuid.uuid4().hex
            path.write_text(json.dumps({
                "owner": owner, "token": token,
                "acquired_ts": time.time(), "ttl_s": ttl_seconds,
            }), encoding="utf-8")
            return {"ok": True, "data": {"lock": name, "token": token, "owner": owner}}
        if time.monotonic() >= deadline:
            return {"ok": False, "error": "locked"}
        time.sleep(0.05)


def lock_release(name: str, token: str) -> dict:
    """Release a lock by its token. Mismatched tokens are rejected so a
    stale process can't steal someone else's lock."""
    path = _LOCKS_DIR / f"{name}.lock"
    plock = _proc_lock(name)
    if path.exists():
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
        if meta.get("token") != token:
            return {"ok": False, "error": "token mismatch"}
        try:
            path.unlink()
        except Exception:
            pass
    try:
        plock.release()
    except RuntimeError:
        pass
    return {"ok": True, "data": {"released": name}}


def lock_status(name: str) -> dict:
    """Inspect a lock without acquiring it."""
    path = _LOCKS_DIR / f"{name}.lock"
    if not path.exists():
        return {"ok": True, "data": {"locked": False}}
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"ok": True, "data": {"locked": True, "meta_unreadable": True}}
    age = time.time() - meta.get("acquired_ts", 0)
    return {"ok": True, "data": {
        "locked": True,
        "owner": meta.get("owner"),
        "age_s": age,
        "ttl_s": meta.get("ttl_s"),
        "stale": age > meta.get("ttl_s", 0),
    }}


# ============================================================
# Tracing / span logging
# ============================================================

def trace_span_log(span_name: str, duration_ms: float, attributes: dict | None = None,
                   parent_span_id: str | None = None) -> dict:
    """Append a span to the local trace log. Substitute for OpenTelemetry
    until the real OTel collector + Langfuse is wired up."""
    record = {
        "span_id": uuid.uuid4().hex[:16],
        "parent_span_id": parent_span_id,
        "name": span_name,
        "ts": _now_iso(),
        "duration_ms": duration_ms,
        "attributes": attributes or {},
    }
    _append_jsonl(_TRACE_PATH, record)
    return {"ok": True, "data": record}


def trace_recent(name_like: str | None = None, since_iso: str | None = None,
                 limit: int = 100) -> dict:
    """Recent spans. Optional substring filter on name + since-timestamp."""
    records = _read_jsonl(_TRACE_PATH)
    if name_like:
        records = [r for r in records if name_like in (r.get("name") or "")]
    if since_iso:
        records = [r for r in records if (r.get("ts") or "") >= since_iso]
    records.sort(key=lambda r: r.get("ts") or "", reverse=True)
    return {"ok": True, "data": {"spans": records[:limit], "count": len(records)}}


# ============================================================
# Notification routing
# ============================================================

def notify_route(level: str, message: str, channels: list[str] | None = None) -> dict:
    """Stage a notification for routing. level: info | warning | error | critical.
    channels: a list of names like 'telegram_ops', 'email_finance' — the
    actual sender is configured per-channel by the agent. Returns
    {notification_id} so the agent can chain a real send if needed."""
    record = {
        "notification_id": uuid.uuid4().hex,
        "level": level,
        "message": message,
        "channels": channels or ["default"],
        "ts": _now_iso(),
        "delivered": False,
    }
    _append_jsonl(_SERVICE_DIR / "notifications.jsonl", record)
    return {"ok": True, "data": record}


def notify_mark_delivered(notification_id: str, channel: str,
                           result: str | None = None) -> dict:
    """Record that a notification was actually sent on a channel."""
    records = _read_jsonl(_SERVICE_DIR / "notifications.jsonl")
    target = next((r for r in records if r.get("notification_id") == notification_id), None)
    if target is None:
        return {"ok": False, "error": "notification_id not found"}
    delivery = {**target, "delivered": True, "delivered_at": _now_iso(),
                "delivered_via": channel, "result": result}
    _append_jsonl(_SERVICE_DIR / "notifications.jsonl", delivery)
    return {"ok": True, "data": delivery}


# ============================================================
# Markdown report renderer
# ============================================================

def report_render_markdown(title: str, sections: list[dict], out_path: str) -> dict:
    """Render a markdown report. sections = [{heading, body}] — body can be
    plain markdown (tables, lists, etc.). Writes UTF-8 file. Returns
    {path, bytes, section_count}."""
    parts: list[str] = [f"# {title}\n", f"_Generated: {_now_iso()}_\n"]
    for s in sections:
        h = s.get("heading", "")
        b = s.get("body", "")
        if h:
            parts.append(f"\n## {h}\n")
        if b:
            parts.append(b + "\n")
    text = "\n".join(parts)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(text, encoding="utf-8")
    return {"ok": True, "data": {"path": out_path, "bytes": len(text.encode("utf-8")),
                                 "section_count": len(sections)}}


def team_channel_send(channel: str, message: str, level: str = "info",
                      attachments: list[dict] | None = None) -> dict:
    """Unified team-channel dispatcher. Stages a notification + audit entry
    + returns a routing decision (telegram_ops → tg_send_message, email_X →
    gmail_create_draft, slack_X → external webhook). Caller chains the real
    send tool with `routing.next_tool`. Centralizes the «куда писать команде»
    decision so the agent doesn't hand-pick per call."""
    notif = notify_route(level, message, channels=[channel])
    routing = {"channel": channel}
    if channel.startswith("telegram_"):
        routing["next_tool"] = "tg_send_message"
        routing["hint"] = "Look up chat_id from your local channel config and pass it as `chat_id` to tg_send_message."
    elif channel.startswith("email_"):
        routing["next_tool"] = "gmail_create_draft"
        routing["hint"] = "Build the draft with gmail_create_draft + gmail_send_draft (requires approval per policy)."
    elif channel.startswith("sms_"):
        routing["next_tool"] = "smsru_send"
        routing["hint"] = "For high-urgency alerts only — SMS is expensive."
    else:
        routing["next_tool"] = None
        routing["hint"] = f"Unknown channel prefix {channel!r}; staged in notifications log only."
    return {"ok": True, "data": {"notification": notif["data"], "routing": routing}}


def report_render_csv(headers: list[str], rows: list[list], out_path: str) -> dict:
    """Render a CSV. headers + rows. Returns {path, bytes, row_count}."""
    import csv as _csv
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = _csv.writer(f)
        w.writerow(headers)
        for r in rows:
            w.writerow(r)
    size = Path(out_path).stat().st_size
    return {"ok": True, "data": {"path": out_path, "bytes": size, "row_count": len(rows)}}
