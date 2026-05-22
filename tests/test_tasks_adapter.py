"""Unit tests for src/tools/tasks.py (Google Tasks)."""
from unittest.mock import MagicMock, patch

import pytest

from src.tools import tasks as gtasks


@pytest.fixture
def fake_service():
    svc = MagicMock()
    with patch.object(gtasks, "_service", return_value=svc):
        yield svc


def test_list_lists_returns_meta(fake_service):
    fake_service.tasklists().list().execute.return_value = {
        "items": [{"id": "L1", "title": "Personal"}, {"id": "L2", "title": "Work"}],
    }
    result = gtasks.list_lists()
    assert result["_meta"]["count"] == 2
    assert result["lists"][0]["title"] == "Personal"


def test_create_list(fake_service):
    fake_service.tasklists().insert().execute.return_value = {"id": "L_new", "title": "Shopping"}
    result = gtasks.create_list("Shopping")
    body = fake_service.tasklists().insert.call_args.kwargs["body"]
    assert body == {"title": "Shopping"}
    assert result["list_id"] == "L_new"


def test_list_tasks_default_hides_completed(fake_service):
    fake_service.tasks().list().execute.return_value = {"items": []}
    gtasks.list_tasks("L1")
    kwargs = fake_service.tasks().list.call_args.kwargs
    assert kwargs["showCompleted"] is False


def test_list_tasks_with_due_filters(fake_service):
    fake_service.tasks().list().execute.return_value = {
        "items": [
            {"id": "t1", "title": "Pay invoice", "due": "2026-05-25T00:00:00Z", "status": "needsAction"},
        ],
    }
    result = gtasks.list_tasks("L1", due_min="2026-05-20", due_max="2026-05-30")
    kwargs = fake_service.tasks().list.call_args.kwargs
    assert kwargs["dueMin"] == "2026-05-20"
    assert kwargs["dueMax"] == "2026-05-30"
    assert result["tasks"][0]["due"] == "2026-05-25T00:00:00Z"


def test_create_task_with_date_only_due(fake_service):
    fake_service.tasks().insert().execute.return_value = {
        "id": "t_new", "title": "x", "due": "2026-05-25T00:00:00Z",
    }
    gtasks.create("L1", "x", due="2026-05-25")
    body = fake_service.tasks().insert.call_args.kwargs["body"]
    assert body["due"] == "2026-05-25T00:00:00Z"


def test_create_task_with_notes(fake_service):
    fake_service.tasks().insert().execute.return_value = {"id": "t", "title": "x"}
    gtasks.create("L1", "x", notes="long description")
    body = fake_service.tasks().insert.call_args.kwargs["body"]
    assert body["notes"] == "long description"


def test_complete_sets_status(fake_service):
    fake_service.tasks().patch().execute.return_value = {"status": "completed"}
    result = gtasks.complete("t1", "L1")
    body = fake_service.tasks().patch.call_args.kwargs["body"]
    assert body == {"status": "completed"}
    assert result["status"] == "completed"


def test_uncomplete_resets_status(fake_service):
    fake_service.tasks().patch().execute.return_value = {"status": "needsAction"}
    gtasks.uncomplete("t1", "L1")
    body = fake_service.tasks().patch.call_args.kwargs["body"]
    assert body["status"] == "needsAction"
    assert body["completed"] is None


def test_delete_task(fake_service):
    fake_service.tasks().delete().execute.return_value = None
    gtasks.delete("t1", "L1")
    fake_service.tasks().delete.assert_called_with(tasklist="L1", task="t1")
