"""Unit tests for src/tools/docs.py."""
from unittest.mock import MagicMock, patch

import pytest

from src.tools import docs


@pytest.fixture
def fake_service():
    svc = MagicMock()
    with patch.object(docs, "_service", return_value=svc):
        yield svc


def test_create_returns_url_and_id(fake_service):
    fake_service.documents().create().execute.return_value = {
        "documentId": "doc_123", "title": "Test",
    }
    result = docs.create("Test")
    assert result["document_id"] == "doc_123"
    assert "/document/d/doc_123/" in result["url"]


def test_create_with_parent_calls_drive_move(fake_service):
    fake_service.documents().create().execute.return_value = {
        "documentId": "doc_123", "title": "Test",
    }
    with patch("src.tools.drive.move") as mock_move:
        docs.create("Test", parent_folder_id="folder_abc")
        mock_move.assert_called_once_with("doc_123", "folder_abc", account="main")


def test_read_extracts_text_and_headings(fake_service):
    fake_service.documents().get().execute.return_value = {
        "title": "My Doc",
        "body": {"content": [
            {
                "startIndex": 1, "endIndex": 10,
                "paragraph": {
                    "paragraphStyle": {"namedStyleType": "HEADING_1"},
                    "elements": [{"textRun": {"content": "Chapter 1\n"}}],
                },
            },
            {
                "startIndex": 10, "endIndex": 40,
                "paragraph": {
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                    "elements": [{"textRun": {"content": "Hello world.\n"}}],
                },
            },
        ]},
    }
    result = docs.read("doc_123")
    assert result["title"] == "My Doc"
    assert "Chapter 1" in result["body_text"]
    assert "Hello world" in result["body_text"]
    assert result["headings"] == [{"level": 1, "text": "Chapter 1", "start_index": 1}]
    assert result["_meta"]["heading_count"] == 1
    assert result["_meta"]["body_truncated"] is False


def test_read_truncates_long_body(fake_service):
    # A document with 60 000 chars in one paragraph
    fake_service.documents().get().execute.return_value = {
        "title": "Big",
        "body": {"content": [{
            "startIndex": 1, "endIndex": 60001,
            "paragraph": {
                "paragraphStyle": {},
                "elements": [{"textRun": {"content": "x" * 60_000}}],
            },
        }]},
    }
    result = docs.read("doc_big")
    assert len(result["body_text"]) == 50_000
    assert result["_meta"]["body_truncated"] is True
    assert result["_meta"]["char_count_total"] == 60_000


def test_append_text_inserts_at_end_and_styles(fake_service):
    fake_service.documents().get().execute.return_value = {
        "body": {"content": [{"endIndex": 42}]},
    }
    fake_service.documents().batchUpdate().execute.return_value = {}
    result = docs.append_text("doc", "New heading", style="h2")
    body = fake_service.documents().batchUpdate.call_args.kwargs["body"]
    insert_req = body["requests"][0]["insertText"]
    style_req = body["requests"][1]["updateParagraphStyle"]
    assert insert_req["location"]["index"] == 41  # 42 - 1
    assert insert_req["text"] == "New heading\n"
    assert style_req["paragraphStyle"]["namedStyleType"] == "HEADING_2"
    assert result["style"] == "h2"


def test_append_text_unknown_style_raises(fake_service):
    fake_service.documents().get().execute.return_value = {"body": {"content": []}}
    with pytest.raises(ValueError, match="unknown style"):
        docs.append_text("doc", "x", style="caption")


def test_replace_text_builds_per_needle_requests(fake_service):
    fake_service.documents().batchUpdate().execute.return_value = {
        "replies": [
            {"replaceAllText": {"occurrencesChanged": 2}},
            {"replaceAllText": {"occurrencesChanged": 1}},
        ],
    }
    result = docs.replace_text("doc", {"{client}": "Иван", "{date}": "2026"})
    body = fake_service.documents().batchUpdate.call_args.kwargs["body"]
    assert len(body["requests"]) == 2
    assert body["requests"][0]["replaceAllText"]["containsText"]["text"] == "{client}"
    assert result["replaced_count"] == 3
    assert result["per_needle"][0]["needle"] == "{client}"
    assert result["per_needle"][0]["occurrences"] == 2


def test_replace_text_empty_short_circuits(fake_service):
    result = docs.replace_text("doc", {})
    assert result["replaced_count"] == 0
    assert result["_meta"]["empty_reason"] == "no_replacements"
    fake_service.documents().batchUpdate.assert_not_called()


def test_insert_table_appends_when_no_position(fake_service):
    fake_service.documents().get().execute.return_value = {
        "body": {"content": [{"endIndex": 30}]},
    }
    fake_service.documents().batchUpdate().execute.return_value = {}
    result = docs.insert_table("doc", rows=3, cols=4)
    body = fake_service.documents().batchUpdate.call_args.kwargs["body"]
    req = body["requests"][0]["insertTable"]
    assert req["rows"] == 3
    assert req["columns"] == 4
    assert req["location"]["index"] == 29  # 30 - 1
    assert result["inserted_at"] == 29


def test_export_pdf_calls_drive_export_media(tmp_path, fake_service):
    """export_pdf delegates to drive.files().export_media — verify that path."""
    dest = tmp_path / "out.pdf"
    with patch("src.tools.drive._service") as drive_svc, \
         patch("googleapiclient.http.MediaIoBaseDownload") as fake_dl_cls:
        # Have the downloader's next_chunk write something to the file handle
        # and return done=True on first iteration.
        def _next_chunk(num_retries=None):
            instance.fh.write(b"%PDF-1.4\nstub")
            return (None, True)
        instance = fake_dl_cls.return_value
        # The actual MediaIoBaseDownload.__init__(fh, request) — we can intercept
        # by side_effect-ing __init__ to capture the fh
        def _init(fh, request):
            instance.fh = fh
            return None
        fake_dl_cls.side_effect = lambda fh, request: (_init(fh, request) or instance)
        instance.next_chunk.side_effect = _next_chunk
        result = docs.export_pdf("doc_123", str(dest))
    drive_svc.return_value.files().export_media.assert_called_with(
        fileId="doc_123", mimeType="application/pdf",
    )
    assert result["ok"]
    assert result["bytes_written"] > 0
