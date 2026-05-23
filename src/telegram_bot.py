"""Telegram bot — approval workflow + alert delivery.

A SMALL state-machine that polls `/getUpdates` for inbound user replies,
and on `/approve <approval_id>` or `/deny <approval_id>` commands flips
the corresponding `infra.approval_*` record. Alerts go out via
`alert(level, text, chat_id)`.

Designed to run as a sidecar in `docker-compose` (worker service) or as
an arq cron job (every 30s). NOT yet wired into the build — exists as
ready-to-deploy code with tests.

Authorization model: when env `TG_AUTHORIZED_CHATS` is set (comma-
separated chat_ids), ONLY those chats can issue approval commands and
`/start` will not auto-authorize anyone. When unset (dev mode), the
first chat to `/start` is auto-authorized.

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


def _env_allowlist() -> list[int] | None:
    """Parse TG_AUTHORIZED_CHATS env var. Returns a list of chat_ids or
    None when the env is unset / empty (dev mode)."""
    raw = os.environ.get("TG_AUTHORIZED_CHATS", "").strip()
    if not raw:
        return None
    out: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(int(chunk))
        except ValueError:
            logger.warning("TG_AUTHORIZED_CHATS: skipping non-int %r", chunk)
    return out


def _is_authorized(chat_id, state: dict) -> bool:
    """A chat is authorized if it's in the env allowlist (when set), or
    in the dynamic `authorized_chats` list (dev fallback)."""
    env = _env_allowlist()
    if env is not None:
        return chat_id in env
    return chat_id in (state.get("authorized_chats") or [])


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


# /approve and /deny share the same prefix-match-then-decide flow; the only
# differences are the verdict string and the success badge. Drive both from
# a single table so the code path is identical.
_DECIDE_COMMANDS = {
    "/approve ": ("approved", "✅ Одобрено"),
    "/deny ":    ("denied",   "❌ Отклонено"),
}


def _decide_command(bot_token: str, chat_id, text: str, prefix: str,
                    verdict: str, badge: str, user_label: str) -> None:
    from src.tools import messaging, infra
    aid = text[len(prefix):].strip()
    # Empty arg would let `"".startswith("")` silently approve the first
    # pending request — reject it explicitly.
    if not aid:
        messaging.tg_send_message(
            bot_token, chat_id,
            f"Укажи id: `{prefix.strip()} <approval_id>` (минимум 4 символа).",
            parse_mode="MarkdownV2",
        )
        return
    if len(aid) < 4:
        messaging.tg_send_message(
            bot_token, chat_id,
            f"Слишком короткий id `{aid}` — минимум 4 символа во избежание коллизий.",
            parse_mode="MarkdownV2",
        )
        return
    # Allow short-prefix match (first 8 chars) since IDs are long hex
    matches = infra.approval_list(status="pending", limit=200)["data"]["approvals"]
    full = next((a["approval_id"] for a in matches if a["approval_id"].startswith(aid)), None)
    if full is None:
        messaging.tg_send_message(bot_token, chat_id, f"Не нашёл pending запрос с id {aid}.")
        return
    infra.approval_decide(full, verdict, decided_by=user_label)
    messaging.tg_send_message(bot_token, chat_id, f"{badge}: {full[:8]}.")


def _handle_message(bot_token: str, msg: dict, state: dict) -> None:
    """Process one inbound message — supports /approve <id> and /deny <id>.

    Authorization: when `TG_AUTHORIZED_CHATS` is set, ONLY those chats
    can use approval / list commands AND `/start` will no longer add new
    chats to the dynamic allowlist. When the env is unset (dev mode),
    `/start` falls back to the historical auto-authorize behavior."""
    from src.tools import messaging, infra
    text = (msg.get("text") or "").strip()
    chat_id = msg.get("chat", {}).get("id")
    user = msg.get("from", {})
    user_label = user.get("username") or str(user.get("id"))

    if not text:
        return

    env_allowlist = _env_allowlist()

    if text.startswith("/start"):
        if env_allowlist is not None:
            # Hardened mode: env decides who's authorized, /start cannot
            # self-authorize.
            if chat_id in env_allowlist:
                messaging.tg_send_message(
                    bot_token, chat_id,
                    "Готов. Команды:\n"
                    "/pending — список запросов на одобрение\n"
                    "/approve <id> — одобрить\n"
                    "/deny <id> — отклонить",
                )
            else:
                messaging.tg_send_message(
                    bot_token, chat_id,
                    "Этот чат не в allowlist (TG_AUTHORIZED_CHATS).",
                )
            return
        # Dev mode: auto-authorize the first chat that says /start.
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

    # Below here: every command mutates state or leaks pending requests —
    # gate them on authorization.
    if not _is_authorized(chat_id, state):
        # Silent drop. We deliberately don't echo a "denied" message: an
        # attacker scanning bots would otherwise learn this chat exists
        # and that it serves approval workflows.
        logger.info("dropping unauthorized telegram command from chat_id=%s", chat_id)
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

    for prefix, (verdict, badge) in _DECIDE_COMMANDS.items():
        if text.startswith(prefix):
            _decide_command(bot_token, chat_id, text, prefix, verdict, badge, user_label)
            return


def poll_once(bot_token: str, timeout_s: int = 25) -> dict:
    """One getUpdates tick. Returns {processed: N, last_update_id: ...}.
    Call repeatedly (every ~30s via arq cron) for a long-lived poller."""
    from src.tools import messaging
    state = _load_state()
    # Snapshot the only fields _save_state actually persists so we can skip
    # writing the file when nothing changed (typical when getUpdates returns
    # an empty list — ~every poll on a quiet chat).
    initial_signature = (state.get("last_update_id", 0),
                         tuple(state.get("authorized_chats") or []))
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
            except Exception:
                logger.exception("telegram handler error")
            processed += 1
    final_signature = (state.get("last_update_id", 0),
                       tuple(state.get("authorized_chats") or []))
    if final_signature != initial_signature:
        _save_state(state)
    return {"ok": True, "processed": processed, "last_update_id": state["last_update_id"]}
