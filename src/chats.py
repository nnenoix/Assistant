"""Conversation persistence.

Every server-side chat run is captured to `.data/chats/<chat_id>.json`. One file
per chat. ChatLog is appended to as the user sends messages and the agent emits
events; the file is rewritten atomically (small enough that this is fine).

Exposes list_chats / read_chat / search_chats as the read side — wrapped as
agent tools in src/tools/chats.py so Claude can recall its own history.
"""
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from src.config import DATA_DIR


CHATS_DIR = DATA_DIR / "chats"
CHATS_DIR.mkdir(exist_ok=True)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _chat_path(chat_id: str) -> Path:
    return CHATS_DIR / f"{chat_id}.json"


class ChatLog:
    """Append-only log for a single conversation. Persists on every change."""

    def __init__(self, path: Path, data: dict[str, Any]):
        self.path = path
        self.data = data

    @classmethod
    def start_new(cls) -> "ChatLog":
        now = datetime.now()
        chat_id = now.strftime("%Y-%m-%dT%H-%M-%S")
        data: dict[str, Any] = {
            "id": chat_id,
            "started_at": now.isoformat(timespec="seconds"),
            "title": None,
            "messages": [],
        }
        log = cls(_chat_path(chat_id), data)
        log._save()
        return log

    def append_user(self, text: str) -> None:
        self.data["messages"].append(
            {"role": "user", "text": text, "ts": _now_iso()}
        )
        if not self.data.get("title"):
            self.data["title"] = text.strip().splitlines()[0][:60]
        self._save()

    def append_event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "done":  # purely a stream-control event
            return
        msgs = self.data["messages"]
        if not msgs or msgs[-1].get("role") != "assistant":
            msgs.append({"role": "assistant", "events": [], "ts": _now_iso()})
        msgs[-1].setdefault("events", []).append(event)
        self._save()

    def _save(self) -> None:
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


# ── read-only API used by tools and HTTP endpoints ────────────────────


def list_chats(limit: int = 30) -> list[dict]:
    """Recent chats, newest first."""
    files = sorted(CHATS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for p in files[:limit]:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({
            "id": data.get("id", p.stem),
            "title": data.get("title") or "(без названия)",
            "started_at": data.get("started_at"),
            "message_count": len(data.get("messages", [])),
        })
    return out


def read_chat(chat_id: str) -> dict:
    p = _chat_path(chat_id)
    if not p.exists():
        raise FileNotFoundError(f"chat {chat_id} not found")
    return json.loads(p.read_text(encoding="utf-8"))


def load_chat_log(chat_id: str) -> "ChatLog":
    """Wrap an existing chat file as a writable ChatLog so the UI can
    continue a previous conversation. Raises FileNotFoundError if the
    chat doesn't exist.

    The whole-chat flow:
        - frontend POSTs /chat with `chat_id` → load_chat_log(chat_id)
          gives us a ChatLog that appends to the SAME file. Old messages
          stay; new turn gets appended below.
        - frontend POSTs /chat with NO chat_id → ChatLog.start_new()
          creates a fresh file with timestamp-id.
    """
    p = _chat_path(chat_id)
    if not p.exists():
        raise FileNotFoundError(f"chat {chat_id} not found")
    data = json.loads(p.read_text(encoding="utf-8"))
    return ChatLog(p, data)


def rename_chat(chat_id: str, new_title: str) -> dict:
    """Rewrite the `title` field. Returns {ok, title} on success."""
    p = _chat_path(chat_id)
    if not p.exists():
        return {"ok": False, "error": f"chat {chat_id} not found"}
    title = (new_title or "").strip()[:80]
    if not title:
        return {"ok": False, "error": "title cannot be empty"}
    data = json.loads(p.read_text(encoding="utf-8"))
    data["title"] = title
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "title": title}


def delete_chat(chat_id: str) -> dict:
    """Delete the chat file. Idempotent — returns ok=True even if missing."""
    p = _chat_path(chat_id)
    existed = p.exists()
    if existed:
        p.unlink()
    return {"ok": True, "deleted": existed, "chat_id": chat_id}


def render_history_for_resume(chat_id: str, max_chars: int = 8000) -> str:
    """Compact text dump of a chat's recent history — fed to a fresh
    Claude session when the user reopens a chat. Format:

        ## Previous conversation (recap)
        [user] question one
        [assistant] short reply
        [tool] drive_resolve_link({...}) → ok
        ...

    Tool-result previews are truncated. Total output capped at
    `max_chars`; if a chat is longer, we keep the TAIL (most recent
    messages — what the user is most likely to follow up on).
    """
    try:
        data = json.loads(_chat_path(chat_id).read_text(encoding="utf-8"))
    except Exception:
        return ""
    lines: list[str] = ["## Previous conversation (recap):"]
    for msg in data.get("messages", []):
        role = msg.get("role")
        if role == "user":
            text = (msg.get("text") or "").strip()
            if text:
                lines.append(f"[user] {text[:500]}")
        elif role == "assistant":
            for ev in msg.get("events", []):
                t = ev.get("type")
                if t == "text":
                    txt = (ev.get("text") or "").strip()
                    if txt:
                        lines.append(f"[assistant] {txt[:500]}")
                elif t == "tool_call":
                    inp = json.dumps(ev.get("input", {}), ensure_ascii=False)[:120]
                    lines.append(f"[tool] {ev.get('name')}({inp})")
                elif t == "tool_result":
                    preview = (ev.get("result_preview") or "")[:150]
                    if preview:
                        lines.append(f"[tool_result] {preview}")
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    # Keep tail — most recent content matters most for follow-up
    tail = text[-max_chars:]
    first_newline = tail.find("\n")
    if first_newline > 0:
        tail = tail[first_newline + 1:]
    return "## Previous conversation (recap, truncated to most recent):\n" + tail


def _assistant_text(message: dict) -> str:
    parts = []
    for ev in message.get("events", []):
        t = ev.get("type")
        if t == "text":
            parts.append(ev.get("text", ""))
        elif t == "tool_call":
            inp = json.dumps(ev.get("input", {}), ensure_ascii=False)
            parts.append(f"[{ev.get('name')}({inp})]")
        elif t in ("tool_result", "tool_error"):
            preview = ev.get("result_preview", "")
            parts.append(preview)
    return " ".join(parts)


def search_chats(query: str, limit: int = 10) -> list[dict]:
    """Substring search across all chats. Returns matches with short snippets."""
    q = query.lower().strip()
    if not q:
        return []
    files = sorted(CHATS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for p in files:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        snippets = []
        for m in data.get("messages", []):
            text = m.get("text", "") if m.get("role") == "user" else _assistant_text(m)
            lower = text.lower()
            if q not in lower:
                continue
            idx = lower.index(q)
            start = max(0, idx - 40)
            end = min(len(text), idx + len(q) + 40)
            snippets.append({
                "role": m.get("role"),
                "snippet": ("…" if start > 0 else "") + text[start:end] + ("…" if end < len(text) else ""),
            })
            if len(snippets) >= 3:
                break
        if snippets:
            out.append({
                "id": data.get("id", p.stem),
                "title": data.get("title"),
                "started_at": data.get("started_at"),
                "matches": snippets,
            })
            if len(out) >= limit:
                break
    return out
