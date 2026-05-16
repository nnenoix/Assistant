from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient

import src.app as app_module


def test_chat_returns_run_id():
    fake_session = MagicMock()
    async def fake_run_turn(msg, emit):
        await emit({"type": "text", "text": "ok"})
        await emit({"type": "done"})
    fake_session.run_turn = fake_run_turn

    with patch.object(app_module, "_session", fake_session):
        client = TestClient(app_module.app)
        resp = client.post("/chat", json={"message": "hi"})
    assert resp.status_code == 200
    assert "run_id" in resp.json()


def test_stream_emits_events_in_sse_format():
    fake_session = MagicMock()
    async def fake_run_turn(msg, emit):
        await emit({"type": "text", "text": "ok"})
        await emit({"type": "done"})
    fake_session.run_turn = fake_run_turn

    with patch.object(app_module, "_session", fake_session):
        client = TestClient(app_module.app)
        run_id = client.post("/chat", json={"message": "hi"}).json()["run_id"]
        with client.stream("GET", f"/stream/{run_id}") as resp:
            assert resp.status_code == 200
            body = b"".join(resp.iter_bytes()).decode()
    assert 'data: {"type": "text"' in body
    assert 'data: {"type": "done"}' in body


def test_approve_resolves_pending():
    fake_session = MagicMock()
    with patch.object(app_module, "_session", fake_session):
        client = TestClient(app_module.app)
        resp = client.post("/approve/abc-123", json={"approved": True})
    assert resp.status_code == 200
    fake_session.resolve_approval.assert_called_with("abc-123", True)
