"""Phase 8 live integration — Google Slides against CLAUDE-TEST/phase-8/."""
import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _probe_slides_api(claude_test_account):
    from src.tools import slides
    try:
        probe = slides._service(claude_test_account).presentations().create(
            body={"title": "[CLAUDE-TEST] probe — safe to delete"},
        ).execute()
        from src.tools import drive as _drive
        try:
            _drive._service(claude_test_account).files().update(
                fileId=probe["presentationId"], body={"trashed": True},
            ).execute()
        except Exception:
            pass
    except Exception as e:
        msg = str(e)
        if "accessNotConfigured" in msg or "has not been used" in msg:
            pytest.skip("Slides API not enabled — accept TOS in Cloud Console")
        if "insufficient" in msg.lower():
            pytest.skip("OAuth token lacks `presentations` scope — re-OAuth needed")
        raise


def test_create_read_round_trip(claude_test_subfolder, claude_test_account):
    from src.tools import slides
    created = slides.create(
        "Phase8 — empty deck",
        parent_folder_id=claude_test_subfolder,
        account=claude_test_account,
    )
    assert created["presentation_id"]
    pres = slides.read(created["presentation_id"], account=claude_test_account)
    assert pres["title"] == "Phase8 — empty deck"
    assert pres["_meta"]["slide_count"] >= 1


def test_add_slide_increases_count(claude_test_subfolder, claude_test_account):
    from src.tools import slides
    created = slides.create("Phase8 — add slide", parent_folder_id=claude_test_subfolder, account=claude_test_account)
    pid = created["presentation_id"]
    before = slides.read(pid, account=claude_test_account)["_meta"]["slide_count"]
    slides.add_slide(pid, layout="TITLE_AND_BODY", account=claude_test_account)
    after = slides.read(pid, account=claude_test_account)["_meta"]["slide_count"]
    assert after == before + 1


def test_replace_placeholders_round_trip(claude_test_subfolder, claude_test_account):
    """Create deck with text containing placeholders, replace, verify."""
    from src.tools import slides
    created = slides.create("Phase8 — placeholders", parent_folder_id=claude_test_subfolder, account=claude_test_account)
    pid = created["presentation_id"]

    # Add text to the default slide via batchUpdate (insertText)
    svc = slides._service(claude_test_account)
    pres = svc.presentations().get(presentationId=pid).execute()
    first_slide_id = pres["slides"][0]["objectId"]
    # Find a text-bearing shape on slide 1 (or skip if none)
    # Simpler: createShape with text "Q1 report by {client} on {date}"
    box_id = "test_box_1"
    svc.presentations().batchUpdate(
        presentationId=pid,
        body={"requests": [
            {"createShape": {
                "objectId": box_id,
                "shapeType": "TEXT_BOX",
                "elementProperties": {
                    "pageObjectId": first_slide_id,
                    "size": {"height": {"magnitude": 100, "unit": "PT"},
                             "width": {"magnitude": 300, "unit": "PT"}},
                    "transform": {"scaleX": 1, "scaleY": 1, "translateX": 50,
                                  "translateY": 50, "unit": "PT"},
                },
            }},
            {"insertText": {"objectId": box_id, "text": "Q1 report by {client} on {date}"}},
        ]},
    ).execute()

    result = slides.replace_placeholders(
        pid,
        {"{client}": "Иван Иванов", "{date}": "2026-05-20"},
        account=claude_test_account,
    )
    assert result["replaced_count"] == 2

    read = slides.read(pid, account=claude_test_account)
    full_text = " ".join(s["text"] for s in read["slides"])
    assert "Иван Иванов" in full_text
    assert "{client}" not in full_text


def test_export_pdf(claude_test_subfolder, claude_test_account, tmp_path):
    from src.tools import slides
    created = slides.create("Phase8 — pdf export", parent_folder_id=claude_test_subfolder, account=claude_test_account)
    dest = tmp_path / "deck.pdf"
    result = slides.export_pdf(created["presentation_id"], str(dest), account=claude_test_account)
    assert result["ok"]
    assert dest.exists()
    assert dest.read_bytes()[:5] == b"%PDF-"
