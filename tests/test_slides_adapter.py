"""Unit tests for src/tools/slides.py."""
from unittest.mock import MagicMock, patch

import pytest

from src.tools import slides


@pytest.fixture
def fake_service():
    svc = MagicMock()
    with patch.object(slides, "_service", return_value=svc):
        yield svc


def test_create_returns_id_and_url(fake_service):
    fake_service.presentations().create().execute.return_value = {
        "presentationId": "pr_1", "title": "X", "slides": [{"objectId": "s1"}],
    }
    result = slides.create("X")
    assert result["presentation_id"] == "pr_1"
    assert "/presentation/d/pr_1/" in result["url"]
    assert result["slide_count"] == 1


def test_read_extracts_per_slide_text(fake_service):
    fake_service.presentations().get().execute.return_value = {
        "title": "Q1 report",
        "slides": [
            {
                "objectId": "s1",
                "pageElements": [
                    {"shape": {"text": {"textElements": [
                        {"textRun": {"content": "Header line\n"}},
                    ]}}},
                ],
            },
            {
                "objectId": "s2",
                "pageElements": [
                    {"shape": {"text": {"textElements": [
                        {"textRun": {"content": "Bullet A\n"}},
                        {"textRun": {"content": "Bullet B"}},
                    ]}}},
                ],
            },
        ],
    }
    result = slides.read("pr_1")
    assert result["title"] == "Q1 report"
    assert result["_meta"]["slide_count"] == 2
    assert result["slides"][0]["text"].strip() == "Header line"
    assert "Bullet A" in result["slides"][1]["text"]


def test_read_empty_flag(fake_service):
    fake_service.presentations().get().execute.return_value = {"title": "empty", "slides": []}
    result = slides.read("pr_1")
    assert result["_meta"]["empty_reason"] == "no_slides"


def test_replace_placeholders_builds_requests(fake_service):
    fake_service.presentations().batchUpdate().execute.return_value = {
        "replies": [
            {"replaceAllText": {"occurrencesChanged": 3}},
            {"replaceAllText": {"occurrencesChanged": 1}},
        ],
    }
    result = slides.replace_placeholders("pr", {"{title}": "Q1", "{client}": "Иван"})
    body = fake_service.presentations().batchUpdate.call_args.kwargs["body"]
    assert len(body["requests"]) == 2
    assert body["requests"][0]["replaceAllText"]["containsText"]["text"] == "{title}"
    assert result["replaced_count"] == 4


def test_replace_placeholders_empty_short_circuits(fake_service):
    result = slides.replace_placeholders("pr", {})
    assert result["replaced_count"] == 0
    fake_service.presentations().batchUpdate.assert_not_called()


def test_create_from_template_copies_then_replaces(fake_service):
    """create_from_template should call drive.copy then replace_placeholders."""
    fake_service.presentations().batchUpdate().execute.return_value = {
        "replies": [{"replaceAllText": {"occurrencesChanged": 2}}],
    }
    with patch("src.tools.drive.copy") as mock_copy:
        mock_copy.return_value = {"id": "new_pr_id", "name": "Copy"}
        result = slides.create_from_template(
            "template_id_42",
            {"{title}": "Q1 2026"},
            dest_title="Q1 Report",
            dest_folder_id="folder_x",
        )
    mock_copy.assert_called_once_with(
        "template_id_42", new_name="Q1 Report", parent_id="folder_x", account="main",
    )
    assert result["presentation_id"] == "new_pr_id"
    assert result["replaced_count"] == 2


def test_add_slide_rejects_unknown_layout(fake_service):
    with pytest.raises(ValueError, match="unknown layout"):
        slides.add_slide("pr", layout="MEGA_FANCY")


def test_add_slide_with_position(fake_service):
    fake_service.presentations().batchUpdate().execute.return_value = {
        "replies": [{"createSlide": {"objectId": "new_slide"}}],
    }
    result = slides.add_slide("pr", layout="TITLE_AND_BODY", position=2)
    body = fake_service.presentations().batchUpdate.call_args.kwargs["body"]
    req = body["requests"][0]["createSlide"]
    assert req["slideLayoutReference"]["predefinedLayout"] == "TITLE_AND_BODY"
    assert req["insertionIndex"] == 2
    assert result["slide_id"] == "new_slide"


def test_replace_image_calls_batch_update(fake_service):
    fake_service.presentations().batchUpdate().execute.return_value = {}
    slides.replace_image("pr", "img_obj_1", "https://example.com/new.png")
    body = fake_service.presentations().batchUpdate.call_args.kwargs["body"]
    req = body["requests"][0]["replaceImage"]
    assert req["imageObjectId"] == "img_obj_1"
    assert req["url"] == "https://example.com/new.png"
