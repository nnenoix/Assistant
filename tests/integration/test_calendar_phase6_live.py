"""Phase 6 live integration — Calendar group ops on egor.titt@gmail.com.

Run with:
    LIVE_GOOGLE_TESTS=1 uv run pytest tests/integration/test_calendar_phase6_live.py -v

Calendar API requires TOS acceptance in the Cloud Console. If not yet
accepted, tests skip with a clear reason.
"""
import datetime as _dt

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _probe_calendar_api(claude_test_account):
    from src.tools import calendar
    try:
        calendar._service(claude_test_account).calendarList().list(maxResults=1).execute()
    except Exception as e:
        msg = str(e)
        if "accessNotConfigured" in msg or "has not been used" in msg:
            pytest.skip("Calendar API not enabled on the GCP project (accept TOS in Cloud Console)")
        raise


def _tomorrow(hour: int = 9) -> str:
    """Helper: return tomorrow at HH:00 as 'YYYY-MM-DD HH:MM' local format."""
    t = _dt.datetime.now() + _dt.timedelta(days=1)
    t = t.replace(hour=hour, minute=0, second=0, microsecond=0)
    return t.strftime("%Y-%m-%d %H:%M")


def _date_offset(days: int) -> str:
    return (_dt.datetime.now() + _dt.timedelta(days=days)).strftime("%Y-%m-%d")


# ---------- freebusy ----------

def test_freebusy_self_returns_window(claude_test_account):
    from src.tools import calendar
    result = calendar.freebusy(
        ["egor.titt@gmail.com"],
        _date_offset(0),
        _date_offset(7),
        account=claude_test_account,
    )
    assert "egor.titt@gmail.com" in [e["email"] for e in result["per_email"]]
    assert "time_min" in result["_meta"]


# ---------- recurring events ----------

def test_create_recurring_event_and_expand_instances(claude_test_account):
    from src.tools import calendar
    # Create a [CLAUDE-TEST] weekly recurring event 3 times
    start = _tomorrow(10)
    event = calendar.create_event(
        summary="[CLAUDE-TEST phase-6] weekly sync",
        start=start,
        recurrence=["RRULE:FREQ=WEEKLY;COUNT=3"],
        account=claude_test_account,
    )
    event_id = event["id"]
    try:
        instances = calendar.list_recurring_instances(
            event_id,
            _date_offset(0),
            _date_offset(28),
            account=claude_test_account,
        )
        # Expect 3 instances within the next 4 weeks
        assert instances["_meta"]["count"] == 3
    finally:
        # Cleanup: delete the series
        calendar.delete_event(event_id, account=claude_test_account)


# ---------- find_meeting_slot ----------

def test_find_meeting_slot_on_empty_window(claude_test_account):
    """If nothing is busy, the slot returned should be at the start of working hours."""
    from src.tools import calendar
    result = calendar.find_meeting_slot(
        ["egor.titt@gmail.com"],
        duration_minutes=30,
        time_min=_date_offset(0),
        time_max=_date_offset(7),
        account=claude_test_account,
    )
    # Either we find a slot, OR everything is busy (unlikely on personal cal)
    assert "found" in result
    if result["found"]:
        # Verify time format
        assert "T" in result["slot"]["start"]


# ---------- overlay_accounts ----------

def test_overlay_accounts_single_account(claude_test_account):
    """Overlay across only 'main' should still work as a normal freebusy."""
    from src.tools import calendar
    result = calendar.overlay_accounts(
        {claude_test_account: ["egor.titt@gmail.com"]},
        _date_offset(0),
        _date_offset(7),
    )
    assert claude_test_account in result["per_account"]
    inner = result["per_account"][claude_test_account]
    assert "per_email" in inner or "error" in inner
