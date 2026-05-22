"""Phase 7 live integration — Google Docs against CLAUDE-TEST/phase-7/.

Run with:
    LIVE_GOOGLE_TESTS=1 uv run pytest tests/integration/test_docs_phase7_live.py -v

Docs API needs:
  1. `docs.googleapis.com` enabled (done in Phase 0).
  2. OAuth scope `documents` (added in Phase 0 — requires re-OAuth on the
     `main` account; tests probe-skip otherwise).
"""
import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _probe_docs_api(claude_test_account):
    from src.tools import docs
    try:
        # Cheapest probe: create + immediate delete via Drive
        svc = docs._service(claude_test_account)
        # Lightweight: just calling documents() construct is OK; need an actual API hit
        # to verify scope. Create a throwaway doc.
        probe = svc.documents().create(body={"title": "[CLAUDE-TEST] probe — safe to delete"}).execute()
        # Move to trash to clean up
        from src.tools import drive as _drive
        try:
            _drive._service(claude_test_account).files().update(
                fileId=probe["documentId"], body={"trashed": True},
            ).execute()
        except Exception:
            pass
    except Exception as e:
        msg = str(e)
        if "accessNotConfigured" in msg or "has not been used" in msg:
            pytest.skip("Docs API not enabled on the GCP project (accept TOS in Cloud Console)")
        if "insufficient" in msg.lower():
            pytest.skip("OAuth token lacks `documents` scope — re-OAuth via /accounts UI")
        raise


def _make_doc(claude_test_subfolder, account, title):
    from src.tools import docs
    return docs.create(title, parent_folder_id=claude_test_subfolder, account=account)


# ---------- create + read round trip ----------

def test_create_read_round_trip(claude_test_subfolder, claude_test_account):
    from src.tools import docs
    created = _make_doc(claude_test_subfolder, claude_test_account, "Phase7 — empty doc")
    assert created["document_id"]
    assert "/document/d/" in created["url"]

    read = docs.read(created["document_id"], account=claude_test_account)
    assert read["title"] == "Phase7 — empty doc"
    assert read["_meta"]["heading_count"] == 0


# ---------- append + read ----------

def test_append_heading_then_paragraph(claude_test_subfolder, claude_test_account):
    from src.tools import docs
    created = _make_doc(claude_test_subfolder, claude_test_account, "Phase7 — append")
    doc_id = created["document_id"]

    docs.append_text(doc_id, "Section 1", style="h1", account=claude_test_account)
    docs.append_text(doc_id, "Some paragraph text with цифры 12 345.", account=claude_test_account)
    docs.append_text(doc_id, "Section 2", style="h2", account=claude_test_account)

    read = docs.read(doc_id, account=claude_test_account)
    assert "Section 1" in read["body_text"]
    assert "цифры" in read["body_text"]
    heading_texts = [h["text"] for h in read["headings"]]
    assert "Section 1" in heading_texts
    assert "Section 2" in heading_texts


# ---------- replace_text ----------

def test_replace_text_placeholders(claude_test_subfolder, claude_test_account):
    from src.tools import docs
    created = _make_doc(claude_test_subfolder, claude_test_account, "Phase7 — contract")
    doc_id = created["document_id"]

    docs.append_text(doc_id, "Договор № {contract_no} от {date} с {client}.", account=claude_test_account)
    result = docs.replace_text(doc_id, {
        "{contract_no}": "2026-001",
        "{date}": "2026-05-20",
        "{client}": "Иван Иванов",
    }, account=claude_test_account)
    assert result["replaced_count"] == 3

    after = docs.read(doc_id, account=claude_test_account)
    assert "2026-001" in after["body_text"]
    assert "Иван Иванов" in after["body_text"]
    # Placeholders should be gone
    assert "{contract_no}" not in after["body_text"]
    assert "{client}" not in after["body_text"]


# ---------- insert_table ----------

def test_insert_table(claude_test_subfolder, claude_test_account):
    from src.tools import docs
    created = _make_doc(claude_test_subfolder, claude_test_account, "Phase7 — table")
    doc_id = created["document_id"]
    docs.append_text(doc_id, "Финансовая сводка:", style="h1", account=claude_test_account)
    result = docs.insert_table(doc_id, rows=3, cols=4, account=claude_test_account)
    assert result["rows"] == 3
    assert result["cols"] == 4


# ---------- export_pdf ----------

def test_export_pdf_round_trip(claude_test_subfolder, claude_test_account, tmp_path):
    from src.tools import docs
    created = _make_doc(claude_test_subfolder, claude_test_account, "Phase7 — pdf export")
    doc_id = created["document_id"]
    docs.append_text(doc_id, "Test content for PDF export.", account=claude_test_account)

    dest = tmp_path / "exported.pdf"
    result = docs.export_pdf(doc_id, str(dest), account=claude_test_account)
    assert result["ok"]
    assert dest.exists()
    # PDF magic header
    head = dest.read_bytes()[:5]
    assert head == b"%PDF-"
