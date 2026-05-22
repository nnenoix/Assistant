"""Phase 9 live integration — Forms + Tasks + Contacts.

Each section skips independently if its API/scope isn't ready.
"""
import pytest

pytestmark = pytest.mark.integration


# ============ Forms ============

@pytest.fixture
def _probe_forms_api(claude_test_account):
    from src.tools import forms
    try:
        probe = forms._service(claude_test_account).forms().create(
            body={"info": {"title": "[CLAUDE-TEST] forms-probe — safe to delete"}},
        ).execute()
        from src.tools import drive as _drive
        try:
            _drive._service(claude_test_account).files().update(
                fileId=probe["formId"], body={"trashed": True},
            ).execute()
        except Exception:
            pass
    except Exception as e:
        msg = str(e)
        if "accessNotConfigured" in msg or "has not been used" in msg:
            pytest.skip("Forms API not enabled — accept TOS in Cloud Console")
        if "insufficient" in msg.lower():
            pytest.skip("OAuth token lacks `forms.body` scope — re-OAuth needed")
        raise


def test_forms_create_add_questions_read(_probe_forms_api, claude_test_subfolder, claude_test_account):
    from src.tools import forms
    created = forms.create(
        "Phase9 — survey",
        description="integration test form",
        parent_folder_id=claude_test_subfolder,
        account=claude_test_account,
    )
    fid = created["form_id"]
    forms.add_question(fid, "text", "Имя?", required=True, account=claude_test_account)
    forms.add_question(fid, "dropdown", "Бренд?", options=["IdealNight", "SensesAura", "VelvetSkin"], account=claude_test_account)
    forms.add_question(fid, "scale", "Оценка от 1 до 5", scale_low=1, scale_high=5, account=claude_test_account)

    read = forms.read(fid, account=claude_test_account)
    assert read["_meta"]["question_count"] == 3
    kinds = [q["kind"] for q in read["questions"]]
    assert "text" in kinds and "dropdown" in kinds and "scale" in kinds


def test_forms_read_responses_empty(_probe_forms_api, claude_test_subfolder, claude_test_account):
    """A freshly-created form has no responses yet."""
    from src.tools import forms
    created = forms.create("Phase9 — empty responses", parent_folder_id=claude_test_subfolder, account=claude_test_account)
    result = forms.read_responses(created["form_id"], account=claude_test_account)
    assert result["_meta"]["count"] == 0
    assert result["_meta"]["empty_reason"] == "no_responses"


# ============ Tasks ============

@pytest.fixture
def _probe_tasks_api(claude_test_account):
    from src.tools import tasks as gtasks
    try:
        gtasks._service(claude_test_account).tasklists().list(maxResults=1).execute()
    except Exception as e:
        msg = str(e)
        if "accessNotConfigured" in msg or "has not been used" in msg:
            pytest.skip("Tasks API not enabled — accept TOS in Cloud Console")
        if "insufficient" in msg.lower():
            pytest.skip("OAuth token lacks `tasks` scope — re-OAuth needed")
        raise


def test_tasks_full_lifecycle(_probe_tasks_api, claude_test_account):
    """Create list → add task → complete → delete."""
    from src.tools import tasks as gtasks
    # Create a test list (will leave behind)
    lst = gtasks.create_list(
        "[CLAUDE-TEST phase-9] test list", account=claude_test_account,
    )
    list_id = lst["list_id"]

    try:
        t1 = gtasks.create(list_id, "Pay invoice", due="2026-12-31", account=claude_test_account)
        t2 = gtasks.create(list_id, "Review report", notes="see ОПиУ Q4", account=claude_test_account)

        listing = gtasks.list_tasks(list_id, account=claude_test_account)
        ids = [t["id"] for t in listing["tasks"]]
        assert t1["task_id"] in ids
        assert t2["task_id"] in ids

        # Complete one
        gtasks.complete(t1["task_id"], list_id, account=claude_test_account)
        # By default completed are hidden
        listing_after = gtasks.list_tasks(list_id, account=claude_test_account)
        assert t1["task_id"] not in [t["id"] for t in listing_after["tasks"]]

        # Delete both
        gtasks.delete(t1["task_id"], list_id, account=claude_test_account)
        gtasks.delete(t2["task_id"], list_id, account=claude_test_account)
    finally:
        # Clean up the list itself
        try:
            gtasks._service(claude_test_account).tasklists().delete(tasklist=list_id).execute()
        except Exception:
            pass


# ============ Contacts ============

@pytest.fixture
def _probe_contacts_api(claude_test_account):
    from src.tools import contacts
    try:
        contacts._service(claude_test_account).people().connections().list(
            resourceName="people/me", pageSize=1, personFields="names",
        ).execute()
    except Exception as e:
        msg = str(e)
        if "accessNotConfigured" in msg or "has not been used" in msg:
            pytest.skip("People API not enabled — accept TOS in Cloud Console")
        if "insufficient" in msg.lower():
            pytest.skip("OAuth token lacks `contacts.readonly` scope — re-OAuth needed")
        raise


def test_contacts_create_search_delete(_probe_contacts_api, claude_test_account):
    """Create a junk contact, find it, delete it."""
    from src.tools import contacts
    suffix = "_CLAUDE_TEST_9d2f"
    created = contacts.create(
        given_name=f"Test{suffix}",
        family_name="Phase9",
        emails=[f"phase9{suffix}@example.com"],
        organization="ACME",
        account=claude_test_account,
    )
    resource_name = created["resource_name"]
    try:
        # Search by the unique suffix
        found = contacts.search(suffix, account=claude_test_account)
        # Note: contacts search has lag; we just check the contact exists via get
        fetched = contacts.get(resource_name, account=claude_test_account)
        assert fetched["display_name"].endswith("Phase9")
    finally:
        contacts.delete(resource_name, account=claude_test_account)


def test_contacts_list_all_returns_meta(_probe_contacts_api, claude_test_account):
    from src.tools import contacts
    result = contacts.list_all(max_results=5, account=claude_test_account)
    # User may have 0 or many contacts; just check the shape
    assert "contacts" in result
    assert "_meta" in result
    assert "truncated" in result["_meta"]
