"""Unit tests for src/tools/forms.py."""
from unittest.mock import MagicMock, patch

import pytest

from src.tools import forms


@pytest.fixture
def fake_service():
    svc = MagicMock()
    with patch.object(forms, "_service", return_value=svc):
        yield svc


def test_create_simple(fake_service):
    fake_service.forms().create().execute.return_value = {"formId": "f_1"}
    result = forms.create("Survey")
    body = fake_service.forms().create.call_args.kwargs["body"]
    assert body["info"]["title"] == "Survey"
    assert result["form_id"] == "f_1"
    assert result["edit_url"].endswith("/edit")


def test_create_with_description_calls_batch_update(fake_service):
    fake_service.forms().create().execute.return_value = {"formId": "f_1"}
    forms.create("Survey", description="please fill")
    # batchUpdate should have been called to set description
    assert fake_service.forms().batchUpdate.called


def test_add_question_text(fake_service):
    fake_service.forms().get().execute.return_value = {"items": [{}, {}]}  # 2 existing
    fake_service.forms().batchUpdate().execute.return_value = {
        "replies": [{"createItem": {"itemId": "q_new"}}],
    }
    result = forms.add_question("f_1", "text", "Your name?", required=True)
    body = fake_service.forms().batchUpdate.call_args.kwargs["body"]
    req = body["requests"][0]["createItem"]
    assert req["item"]["title"] == "Your name?"
    assert req["item"]["questionItem"]["question"]["required"] is True
    assert "textQuestion" in req["item"]["questionItem"]["question"]
    assert req["location"]["index"] == 2  # appended after 2 existing
    assert result["item_id"] == "q_new"


def test_add_question_multiple_choice_needs_options(fake_service):
    fake_service.forms().get().execute.return_value = {"items": []}
    with pytest.raises(ValueError, match="needs `options`"):
        forms.add_question("f_1", "multiple_choice", "pick one")


def test_add_question_dropdown_builds_choice_question(fake_service):
    fake_service.forms().get().execute.return_value = {"items": []}
    fake_service.forms().batchUpdate().execute.return_value = {
        "replies": [{"createItem": {"itemId": "q1"}}],
    }
    forms.add_question("f_1", "dropdown", "Brand?", options=["IN", "SA", "VS"])
    body = fake_service.forms().batchUpdate.call_args.kwargs["body"]
    question = body["requests"][0]["createItem"]["item"]["questionItem"]["question"]
    assert question["choiceQuestion"]["type"] == "DROP_DOWN"
    assert len(question["choiceQuestion"]["options"]) == 3


def test_add_question_unknown_type_raises(fake_service):
    with pytest.raises(ValueError, match="unknown question_type"):
        forms.add_question("f_1", "neural_handwriting", "wat")


def test_read_parses_question_types(fake_service):
    fake_service.forms().get().execute.return_value = {
        "info": {"title": "Survey", "description": "ok"},
        "items": [
            {"itemId": "q1", "title": "Name?", "questionItem": {"question": {
                "textQuestion": {"paragraph": False}, "required": True,
            }}},
            {"itemId": "q2", "title": "Pick", "questionItem": {"question": {
                "choiceQuestion": {"type": "RADIO", "options": [{"value": "A"}]},
            }}},
            {"itemId": "q3", "title": "Date?", "questionItem": {"question": {
                "dateQuestion": {},
            }}},
        ],
    }
    result = forms.read("f_1")
    kinds = [q["kind"] for q in result["questions"]]
    assert kinds == ["text", "multiple_choice", "date"]
    assert result["_meta"]["question_count"] == 3


def test_read_responses_flattens_answers(fake_service):
    fake_service.forms().responses().list().execute.return_value = {
        "responses": [
            {
                "responseId": "r1",
                "lastSubmittedTime": "2026-05-20T10:00:00Z",
                "answers": {
                    "q_name": {"textAnswers": {"answers": [{"value": "Иван"}]}},
                    "q_pick": {"textAnswers": {"answers": [{"value": "IN"}]}},
                },
            },
        ],
    }
    result = forms.read_responses("f_1")
    assert result["_meta"]["count"] == 1
    assert result["responses"][0]["answers"]["q_name"] == ["Иван"]


def test_read_responses_with_since_passes_filter(fake_service):
    fake_service.forms().responses().list().execute.return_value = {"responses": []}
    forms.read_responses("f_1", since="2026-05-01T00:00:00Z")
    kwargs = fake_service.forms().responses().list.call_args.kwargs
    assert "filter" in kwargs
    assert "timestamp" in kwargs["filter"]
