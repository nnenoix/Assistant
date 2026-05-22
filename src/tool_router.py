"""Keyword-based intent classifier — maps a user message to the set of
tool categories likely needed for the turn.

No LLM call: pure regex/keyword heuristic, sub-millisecond. The output is
a list of category names suitable for `registry.select_tools(...)`. When
the message is ambiguous or short, returns the BASELINE set rather than
the FULL set — let the agent's first action surface more if needed.

This module is intentionally NOT wired into `agent.py` yet — Phase 13D
is the toolkit; runtime integration is deferred (requires the SDK to
support per-turn tool subsetting). When that integration lands, the
caller will do:

    from src import tool_router
    from src.tools import registry
    cats = tool_router.classify_intent(user_message)
    tools_for_turn = registry.select_tools(cats)
    # ... pass into ClaudeAgentOptions.allowed_tools / mcp_servers ...
"""
from __future__ import annotations

import re


# Categories that are ALWAYS useful regardless of intent — auth/aliases
# resolve people, notes/chats give memory, self_heal lets agent fix itself.
BASELINE_CATEGORIES = {
    "auth", "aliases", "notes", "chats", "reports", "verify", "self",
    "reply",  # reply_self_check
}

# Keyword → category mapping. Keys are lowercased substrings; the user
# message is lowercased and searched. Russian + English variants.
_INTENT_KEYWORDS: dict[str, set[str]] = {
    "sheets": {
        "таблиц", "лист", "ячейк", "формул", "опиу", "ддс", "баланс", "колонк",
        "spreadsheet", "sheet", "cell", "formula", "named range", "rows",
        "столбц", "выручк", "прибыль", "маржинальност",
    },
    "drive": {
        "drive", "файл", "папк", "folder", "диск", "доступ", "поделит", "share",
        "переименова", "rename", "move",
    },
    "gmail": {
        "gmail", "почт", "письм", "email", "mail", "тред", "thread", "inbox",
        "archive", "архив", "отправ", "send",
    },
    "calendar": {
        "календар", "calendar", "встреч", "событи", "meeting", "event",
        "свободн", "слот", "free", "slot", "напомин", "remind",
    },
    "docs": {
        "docs", "документ", "контракт", "document", "word",
    },
    "slides": {
        "slides", "презентац", "presentation", "слайд", "deck",
    },
    "forms": {"forms", "форм"},
    "tasks": {"tasks", "задач", "todo", "to-do", "to do"},
    "contacts": {"contact", "контакт", "people api"},
    "apps": {  # apps_script_*
        "apps script", "apps_script", "скрипт", "макрос", "appscript",
        "библиотек", "library", "deploy", "версия", "version",
        "trigger", "триггер",
    },
    "browser": {"browser", "playwright", "chromium"},
    "watcher": {"watcher", "fail", "падал", "exception", "ошибк скрипт"},
    "wb": {"wildberries", "вб", "wb api", "rrd_id", "report detail"},
    "bank": {
        "банк", "bank", "выписк", "statement", "транзакц", "transaction",
        "счёт", "счет", "1с", "клиент-банк",
    },
    "analytics": {"abc", "анализ", "abc-анализ", "топ ", "топ-", "категори"},
    "excel": {"excel", "xlsx", "xls", "workbook"},
    "local": {"локальн", "local", "файл диск", "pdf", "ocr"},
    "web": {"web ", "сайт", "url", "ссылк", "википед", "duckduckgo", "search"},
    "vision": {"ocr", "распозна", "чек", "receipt", "скан"},
    "translate": {"переведи", "translate", "translation"},
    "pdf": {"pdf"},
    "fx": {"курс ", "rate", "rub", "доллар", "евро", "currency"},
    "open": {"открой", "open url", "браузер"},
    "gcp": {"gcp", "cloud", "project", "проект gcp"},
    "cloud": {"cloud logging", "stackdriver", "логи"},
}


def classify_intent(user_message: str) -> list[str]:
    """Return the list of tool categories likely needed for this message.

    Always includes BASELINE_CATEGORIES. Adds any category whose keyword
    set has a substring match against the lowercased message.

    For empty/very short messages → returns BASELINE ∪ {"sheets", "drive"}
    (most common everyday surfaces) as a sensible default.
    """
    text = (user_message or "").lower().strip()
    if not text:
        return sorted(BASELINE_CATEGORIES | {"sheets", "drive"})
    matched: set[str] = set(BASELINE_CATEGORIES)
    for cat, keywords in _INTENT_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                matched.add(cat)
                break
    # Short / ambiguous messages get sheets+drive as fallback baseline
    if len(text) < 25 and len(matched) <= len(BASELINE_CATEGORIES) + 1:
        matched.update({"sheets", "drive"})
    return sorted(matched)


def full_category_set() -> list[str]:
    """Convenience: return every registered category (for 'load everything')."""
    from src.tools import registry
    return sorted(registry.list_categories().keys())
