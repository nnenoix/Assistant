from unittest.mock import patch, MagicMock, AsyncMock

from fastapi.testclient import TestClient

import pytest

import src.app as app_module


@pytest.fixture(autouse=True)
def _reset_active_chat():
    """Each test starts with `_chat_log = None` so any state carried over
    from earlier tests (which now leak through the chat-switching machinery)
    doesn't trip up the MagicMock setup. Pytest tears the override down
    automatically when the test finishes."""
    with patch.object(app_module, "_chat_log", None):
        yield


def _make_fake_session(run_turn_impl=None):
    """`_switch_to_chat` may await `_session.close()` to break the SDK
    connection when the user moves between chats; MagicMock returns a
    plain MagicMock (not awaitable). Use AsyncMock for the few coroutine
    methods app.py calls on `_session`."""
    fake = MagicMock()
    fake.close = AsyncMock()
    fake._client = None
    if run_turn_impl is not None:
        fake.run_turn = run_turn_impl
    return fake


def test_chat_returns_run_id():
    async def fake_run_turn(msg, emit):
        await emit({"type": "text", "text": "ok"})
        await emit({"type": "done"})
    fake_session = _make_fake_session(fake_run_turn)

    with patch.object(app_module, "_session", fake_session):
        client = TestClient(app_module.app)
        resp = client.post("/chat", json={"message": "hi"})
    assert resp.status_code == 200
    assert "run_id" in resp.json()


def test_stream_emits_events_in_sse_format():
    async def fake_run_turn(msg, emit):
        await emit({"type": "text", "text": "ok"})
        await emit({"type": "done"})
    fake_session = _make_fake_session(fake_run_turn)

    with patch.object(app_module, "_session", fake_session):
        client = TestClient(app_module.app)
        run_id = client.post("/chat", json={"message": "hi"}).json()["run_id"]
        with client.stream("GET", f"/stream/{run_id}") as resp:
            assert resp.status_code == 200
            body = b"".join(resp.iter_bytes()).decode()
    assert 'data: {"type": "text"' in body
    assert 'data: {"type": "done"}' in body


def test_approve_resolves_pending():
    fake_session = _make_fake_session()
    with patch.object(app_module, "_session", fake_session):
        client = TestClient(app_module.app)
        resp = client.post("/approve/abc-123", json={"approved": True})
    assert resp.status_code == 200
    fake_session.resolve_approval.assert_called_with("abc-123", True)
