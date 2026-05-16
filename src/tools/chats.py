"""Agent-facing tools for reading saved chat history.

Persistence happens automatically in src/chats.py — these are the read side.
"""
from src import chats as _impl


def list_recent(limit: int = 30) -> list[dict]:
    """List recent chats, newest first. Each entry has id, title, started_at, message_count."""
    return _impl.list_chats(limit=limit)


def read(chat_id: str) -> dict:
    """Read the full conversation for a given chat id."""
    return _impl.read_chat(chat_id)


def search(query: str, limit: int = 10) -> list[dict]:
    """Substring search across all saved chats. Returns matches with snippets."""
    return _impl.search_chats(query=query, limit=limit)
