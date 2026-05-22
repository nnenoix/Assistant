"""Telegram bot — approval workflow + alert delivery.

A SMALL state-machine that polls `/getUpdates` for inbound user replies,
and on `/approve <approval_id>` or `/deny <approval_id>` commands flips
the corresponding `infra.approval_*` record. Alerts go out via
`alert(level, text, chat_id)`.

Designed to run as a sidecar in `docker-compose` (worker service) or as
an arq cron job (every 30s). NOT yet wired into the build — exists as
ready-to-deploy code with tests.

Usage:
    from src.telegram_bot import poll_once
    poll_once(bot_token=os.environ['TG_BOT_TOKEN'])  # one tick
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from src.config import DATA_DIR

logger = logging.getLogger(__name__)
_STATE_PATH = DATA_DIR / "telegram_bot_state.json"


def _load_state() -> dict:
    if not _STATE_PATH.exists():
        return {"last_update_id": 0, "authorized_chats": []}
    try:
        return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"last_update_id": 0, "authorized_chats": []}


def _save_state(state: dict) -> None:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                            encoding="utf-8")


def alert(bot_token: str, level: str, text: str, chat_id: int | str | None = None) -> dict:
    """Send an alert to Telegram. `chat_id` defaults to the FIRST authorized
    chat in state. level prefixes the message (🔵 info / ⚠️ warning / 🛑 error /
    🚨 critical)."""
    from src.tools import messaging
    state = _load_state()
    target = chat_id
    if target is None:
        chats = state.get("authorized_chats") or []
        if not chats:
            return {"ok": False, "error": "no authorized chat — DM the bot first"}
        target = chats[0]
    prefix = {"info": "🔵", "warning": "⚠️", "error": "🛑", "critical": "🚨"}.get(level, "📌")
    return messaging.tg_send_message(bot_token, target, f"{prefix} *{level.upper()}*\n{text}",
                                      parse_mode="MarkdownV2")


def _handle_message(bot_token: str, msg: dict, state: dict) -> None:
    """Process one inbound message — supports /approve <id> and /deny <id>."""
    from src.tools import messaging, infra
    text = (msg.get("text") or "").strip()
    chat_id = msg.get("chat", {}).get("id")
    user = msg.get("from", {})
    user_label = user.get("username") or str(user.get("id"))

    if not text:
        return

    # Auto-authorize the first chat that says /start
    if text.startswith("/start"):
        chats = state.setdefault("authorized_chats", [])
        if chat_id not in chats:
            chats.append(chat_id)
        messaging.tg_send_message(
            bot_token, chat_id,
            "Готов. Команды:\n"
            "/pending — список запросов на одобрение\n"
            "/approve <id> — одобрить\n"
            "/deny <id> — отклонить",
        )
        return

    if text.startswith("/pending"):
        out = infra.approval_list(status="pending", limit=20)
        items = out["data"]["approvals"]
        if not items:
            messaging.tg_send_message(bot_token, chat_id, "Нет ожидающих запросов.")
            return
        lines = [f"`{a['approval_id'][:8]}` {a['action']} ({a.get('reason') or '—'})"
                 for a in items]
        messaging.tg_send_message(bot_token, chat_id,
                                   "Ожидают одобрения:\n" + "\n".join(lines),
                                   parse_mode="MarkdownV2")
        return

    if text.startswith("/approve "):
        aid = text.split(" ", 1)[1].strip()
        # Allow short-prefix match (first 8 chars) since IDs are long hex
        matches = infra.approval_list(status="pending", limit=200)["data"]["approvals"]
        full = next((a["approval_id"] for a in matches if a["approval_id"].startswith(aid)), None)
        if full is None:
            messaging.tg_send_message(bot_token, chat_id, f"Не нашёл pending запрос с id {aid}.")
            return
        infra.approval_decide(full, "approved", decided_by=user_label)
        messaging.tg_send_message(bot_token, chat_id, f"✅ Одобрено: {full[:8]}.")
        return

    if text.startswith("/deny "):
        aid = text.split(" ", 1)[1].strip()
        matches = infra.approval_list(status="pending", limit=200)["data"]["approvals"]
        full = next((a["approval_id"] for a in matches if a["approval_id"].startswith(aid)), None)
        if full is None:
            messaging.tg_send_message(bot_token, chat_id, f"Не нашёл pending запрос с id {aid}.")
            return
        infra.approval_decide(full, "denied", decided_by=user_label)
        messaging.tg_send_message(bot_token, chat_id, f"❌ Отклонено: {full[:8]}.")
        return


def poll_once(bot_token: str, timeout_s: int = 25) -> dict:
    """One getUpdates tick. Returns {processed: N, last_update_id: ...}.
    Call repeatedly (every ~30s via arq cron) for a long-lived poller."""
    from src.tools import messaging
    state = _load_state()
    offset = state.get("last_update_id", 0) + 1
    resp = messaging.tg_get_updates(bot_token, offset=offset, timeout=timeout_s)
    if not resp.get("ok"):
        return {"ok": False, "error": resp.get("error")}
    updates = (resp.get("data") or {}).get("result") or []
    processed = 0
    for upd in updates:
        state["last_update_id"] = max(state.get("last_update_id", 0), upd.get("update_id", 0))
        msg = upd.get("message")
        if msg:
            try:
                _handle_message(bot_token, msg, state)
            except Exception as e:
                logger.exception("telegram handler error")
            processed += 1
    _save_state(state)
    return {"ok": True, "processed": processed, "last_update_id": state["last_update_id"]}
