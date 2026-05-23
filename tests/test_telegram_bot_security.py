"""Security regressions for src/telegram_bot.py.

The bot mediates approval / denial of pending actions; any chat being able
to issue /approve, /deny or /pending is a privilege-escalation bug
(SEC C1). The /start auto-authorize path is a related bootstrap hazard
(SEC H1). Empty-prefix approval match is the silent-approve-first
trap (SEC L1).
"""
from __future__ import annotations

import pytest


@pytest.fixture
def patched_messaging(monkeypatch):
    """Capture every outbound tg_send_message + every approval_decide call
    so the test can assert the bot did / didn't act."""
    sent: list[tuple] = []
    decided: list[tuple] = []

    from src.tools import messaging, infra

    def fake_send(token, chat_id, text, **kw):
        sent.append((chat_id, text))
        return {"ok": True}

    def fake_list(status="pending", limit=20):
        return {"ok": True, "data": {"approvals": [
            {"approval_id": "deadbeef1234567890", "action": "drive.delete",
             "reason": "test"},
            {"approval_id": "cafef00d0987654321", "action": "gmail.send",
             "reason": "test"},
        ]}}

    def fake_decide(approval_id, status, decided_by=None, note=None):
        decided.append((approval_id, status, decided_by))
        return {"ok": True}

    monkeypatch.setattr(messaging, "tg_send_message", fake_send)
    monkeypatch.setattr(infra, "approval_list", fake_list)
    monkeypatch.setattr(infra, "approval_decide", fake_decide)
    return {"sent": sent, "decided": decided}


# ============================================================
# C1: unauthorized chats can't approve / deny / list pending
# ============================================================

def test_unauthorized_chat_cannot_approve(patched_messaging, monkeypatch):
    monkeypatch.delenv("TG_AUTHORIZED_CHATS", raising=False)
    from src import telegram_bot
    state = {"authorized_chats": [111]}  # 111 is authorized; 999 is not
    msg = {"text": "/approve deadbeef", "chat": {"id": 999},
           "from": {"username": "attacker"}}
    telegram_bot._handle_message("tok", msg, state)
    assert patched_messaging["decided"] == []  # no decision was made
    # Bot stayed silent — no leak of "you're not authorized" hint
    assert patched_messaging["sent"] == []


def test_unauthorized_chat_cannot_deny(patched_messaging, monkeypatch):
    monkeypatch.delenv("TG_AUTHORIZED_CHATS", raising=False)
    from src import telegram_bot
    state = {"authorized_chats": [111]}
    msg = {"text": "/deny deadbeef", "chat": {"id": 999},
           "from": {"username": "attacker"}}
    telegram_bot._handle_message("tok", msg, state)
    assert patched_messaging["decided"] == []
    assert patched_messaging["sent"] == []


def test_unauthorized_chat_cannot_list_pending(patched_messaging, monkeypatch):
    monkeypatch.delenv("TG_AUTHORIZED_CHATS", raising=False)
    from src import telegram_bot
    state = {"authorized_chats": [111]}
    msg = {"text": "/pending", "chat": {"id": 999},
           "from": {"username": "attacker"}}
    telegram_bot._handle_message("tok", msg, state)
    # Attacker shouldn't see the list of pending approval ids
    assert patched_messaging["sent"] == []


def test_authorized_chat_can_approve(patched_messaging, monkeypatch):
    monkeypatch.delenv("TG_AUTHORIZED_CHATS", raising=False)
    from src import telegram_bot
    state = {"authorized_chats": [111]}
    msg = {"text": "/approve deadbeef", "chat": {"id": 111},
           "from": {"username": "alice"}}
    telegram_bot._handle_message("tok", msg, state)
    assert len(patched_messaging["decided"]) == 1
    aid, status, decided_by = patched_messaging["decided"][0]
    assert aid == "deadbeef1234567890"
    assert status == "approved"
    assert decided_by == "alice"


# ============================================================
# H1: TG_AUTHORIZED_CHATS env locks down auto-auth
# ============================================================

def test_env_allowlist_overrides_dynamic_authorization(patched_messaging, monkeypatch):
    """With env set, only env chats are authorized — dynamic state is ignored."""
    monkeypatch.setenv("TG_AUTHORIZED_CHATS", "100,200")
    from src import telegram_bot
    # Dynamic state grants 999, but env says only 100/200.
    state = {"authorized_chats": [999]}
    msg = {"text": "/approve deadbeef", "chat": {"id": 999},
           "from": {"username": "alice"}}
    telegram_bot._handle_message("tok", msg, state)
    assert patched_messaging["decided"] == []


def test_env_allowlist_blocks_start_self_authorize(patched_messaging, monkeypatch):
    """In env-allowlist mode, /start cannot self-authorize an outsider."""
    monkeypatch.setenv("TG_AUTHORIZED_CHATS", "100")
    from src import telegram_bot
    state = {"authorized_chats": []}
    msg = {"text": "/start", "chat": {"id": 999},
           "from": {"username": "stranger"}}
    telegram_bot._handle_message("tok", msg, state)
    # State must NOT have grown
    assert state["authorized_chats"] == []
    # A polite refusal was sent (this one ISN'T silent — /start is benign,
    # an explanation helps an admin who typoed the env value)
    assert len(patched_messaging["sent"]) == 1
    assert "allowlist" in patched_messaging["sent"][0][1].lower()


def test_env_allowlist_permits_listed_chat(patched_messaging, monkeypatch):
    monkeypatch.setenv("TG_AUTHORIZED_CHATS", "100,200")
    from src import telegram_bot
    state = {"authorized_chats": []}
    msg = {"text": "/approve deadbeef", "chat": {"id": 100},
           "from": {"username": "alice"}}
    telegram_bot._handle_message("tok", msg, state)
    assert len(patched_messaging["decided"]) == 1


def test_dev_mode_start_still_auto_authorizes(patched_messaging, monkeypatch):
    """Without env, /start retains historical dev-friendly behavior."""
    monkeypatch.delenv("TG_AUTHORIZED_CHATS", raising=False)
    from src import telegram_bot
    state = {"authorized_chats": []}
    msg = {"text": "/start", "chat": {"id": 42},
           "from": {"username": "alice"}}
    telegram_bot._handle_message("tok", msg, state)
    assert 42 in state["authorized_chats"]


# ============================================================
# L1: empty / too-short /approve arg can't blanket-approve
# ============================================================

def test_approve_with_no_arg_does_not_decide(patched_messaging, monkeypatch):
    """`/approve` with no arg — upstream `.strip()` collapses trailing
    whitespace, so the prefix `/approve ` never matches. No decision is
    made, no leak."""
    monkeypatch.delenv("TG_AUTHORIZED_CHATS", raising=False)
    from src import telegram_bot
    state = {"authorized_chats": [111]}
    msg = {"text": "/approve", "chat": {"id": 111},
           "from": {"username": "alice"}}
    telegram_bot._handle_message("tok", msg, state)
    assert patched_messaging["decided"] == []


def test_approve_with_too_short_arg_does_not_decide(patched_messaging, monkeypatch):
    """An arg shorter than 4 chars hits the _decide_command length guard —
    rejection message sent, no approval flipped."""
    monkeypatch.delenv("TG_AUTHORIZED_CHATS", raising=False)
    from src import telegram_bot
    state = {"authorized_chats": [111]}
    msg = {"text": "/approve ab", "chat": {"id": 111},
           "from": {"username": "alice"}}
    telegram_bot._handle_message("tok", msg, state)
    assert patched_messaging["decided"] == []
    # Should have been told the id is too short
    assert any("корот" in s[1].lower() for s in patched_messaging["sent"])


def test_deny_with_no_arg_does_not_decide(patched_messaging, monkeypatch):
    monkeypatch.delenv("TG_AUTHORIZED_CHATS", raising=False)
    from src import telegram_bot
    state = {"authorized_chats": [111]}
    msg = {"text": "/deny", "chat": {"id": 111},
           "from": {"username": "alice"}}
    telegram_bot._handle_message("tok", msg, state)
    assert patched_messaging["decided"] == []
