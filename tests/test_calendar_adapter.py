"""Unit tests for src/tools/calendar.py (Phase 6 group ops)."""
from unittest.mock import MagicMock, patch

import pytest

from src.tools import calendar


@pytest.fixture
def fake_service():
    svc = MagicMock()
    with patch.object(calendar, "_service", return_value=svc):
        yield svc


def test_freebusy_passes_emails_to_query(fake_service):
    fake_service.freebusy().query().execute.return_value = {
        "calendars": {
            "a@x.com": {"busy": [{"start": "2026-05-20T10:00:00Z", "end": "2026-05-20T11:00:00Z"}]},
            "b@x.com": {"busy": []},
        },
    }
    result = calendar.freebusy(["a@x.com", "b@x.com"], "2026-05-20", "2026-05-21")
    body = fake_service.freebusy().query.call_args.kwargs["body"]
    assert body["items"] == [{"id": "a@x.com"}, {"id": "b@x.com"}]
    assert body["timeMin"] == "2026-05-20T00:00:00Z"
    assert body["timeMax"] == "2026-05-21T00:00:00Z"
    # per_email always echoes the requested order
    assert [e["email"] for e in result["per_email"]] == ["a@x.com", "b@x.com"]
    assert len(result["per_email"][0]["busy"]) == 1


def test_freebusy_handles_missing_calendar(fake_service):
    fake_service.freebusy().query().execute.return_value = {"calendars": {}}
    result = calendar.freebusy(["x@y.com"], "2026-05-20", "2026-05-21")
    assert result["per_email"][0]["busy"] == []
    assert result["per_email"][0]["errors"] == []


def test_find_meeting_slot_returns_first_free(fake_service):
    """A's busy slot is 10:00-11:00 on Mon; B is busy 09:00-10:00.
    A 30-min slot should be at 11:00. Test asserts in UTC (tz='UTC') so the
    working-hours filter is also evaluated in UTC — keeping the assertion
    independent of the local-tz fix (bug_013)."""
    # Use a Monday in May 2026: 2026-05-18 is a Monday
    fake_service.freebusy().query().execute.return_value = {
        "calendars": {
            "a@x.com": {"busy": [{"start": "2026-05-18T10:00:00+00:00", "end": "2026-05-18T11:00:00+00:00"}]},
            "b@x.com": {"busy": [{"start": "2026-05-18T09:00:00+00:00", "end": "2026-05-18T10:00:00+00:00"}]},
        },
    }
    result = calendar.find_meeting_slot(
        ["a@x.com", "b@x.com"],
        duration_minutes=30,
        time_min="2026-05-18",
        time_max="2026-05-19",
        tz="UTC",
    )
    assert result["found"] is True
    assert "T11:00" in result["slot"]["start"]


def test_find_meeting_slot_no_overlap_returns_not_found(fake_service):
    """Both fully busy across the window."""
    fake_service.freebusy().query().execute.return_value = {
        "calendars": {
            "a@x.com": {"busy": [{"start": "2026-05-18T00:00:00+00:00", "end": "2026-05-19T00:00:00+00:00"}]},
        },
    }
    result = calendar.find_meeting_slot(
        ["a@x.com"],
        duration_minutes=30,
        time_min="2026-05-18",
        time_max="2026-05-19",
        tz="UTC",
    )
    assert result["found"] is False
    assert result["slot"] is None


def test_find_meeting_slot_skips_weekends(fake_service):
    """If the only free time is Saturday and weekdays_only=True, return not found."""
    fake_service.freebusy().query().execute.return_value = {
        "calendars": {"a@x.com": {"busy": []}},
    }
    # 2026-05-23 is Saturday, 2026-05-24 is Sunday
    result = calendar.find_meeting_slot(
        ["a@x.com"],
        duration_minutes=30,
        time_min="2026-05-23",
        time_max="2026-05-25",  # ends on Sunday
        weekdays_only=True,
        tz="UTC",
    )
    assert result["found"] is False


# ---------- bug_013 lockdown: working-hours filter is local-tz, not UTC ----------

def test_find_meeting_slot_working_hours_filter_in_moscow(fake_service):
    """REGRESSION: default tz='Europe/Moscow'. A free slot at 06:00 UTC
    (= 09:00 Moscow) on a Monday should be returned as the first working-hours
    slot. Previously the hours filter compared candidate.hour (UTC) against
    work_hours_start=9, so the first slot was forced to 09:00 UTC = 12:00 Moscow."""
    fake_service.freebusy().query().execute.return_value = {
        "calendars": {"a@x.com": {"busy": []}},
    }
    result = calendar.find_meeting_slot(
        ["a@x.com"],
        duration_minutes=30,
        time_min="2026-05-18T00:00:00+00:00",
        time_max="2026-05-18T23:59:59+00:00",
        tz="Europe/Moscow",
    )
    assert result["found"] is True
    # 09:00 Moscow == 06:00 UTC
    assert "T06:00" in result["slot"]["start"]


def test_find_meeting_slot_does_not_cross_midnight(fake_service):
    """REGRESSION: a slot whose end falls into the next local day must be
    rejected — previously the working-hours check only compared end.hour to
    work_hours_end, missing the case where end was past midnight."""
    fake_service.freebusy().query().execute.return_value = {
        "calendars": {"a@x.com": {"busy": []}},
    }
    # Window starts at 22:00 UTC = 01:00 next-day Moscow on Mon → Tue.
    # A 4h slot starting late Tuesday in Moscow that would span past midnight
    # must be pushed to next working-day morning.
    result = calendar.find_meeting_slot(
        ["a@x.com"],
        duration_minutes=240,
        time_min="2026-05-18T13:00:00+00:00",  # 16:00 Moscow Mon
        time_max="2026-05-20T23:59:59+00:00",
        tz="Europe/Moscow",
        work_hours_start=9,
        work_hours_end=19,
    )
    assert result["found"] is True
    # The slot must start within working hours and not cross midnight.
    # 16:00 Moscow + 4h = 20:00 Moscow → past 19:00 → skipped. Next morning
    # 09:00 Moscow Tue = 06:00 UTC Tue.
    assert result["slot"]["start"].startswith("2026-05-19T06:00")


def test_list_recurring_instances_calls_events_instances(fake_service):
    fake_service.events().instances().execute.return_value = {
        "items": [
            {"id": "ev_1@1", "start": {"dateTime": "2026-05-18T10:00:00Z"}, "end": {"dateTime": "2026-05-18T11:00:00Z"}, "status": "confirmed", "recurringEventId": "ev"},
            {"id": "ev_1@2", "start": {"dateTime": "2026-05-25T10:00:00Z"}, "end": {"dateTime": "2026-05-25T11:00:00Z"}, "status": "confirmed", "recurringEventId": "ev"},
        ],
    }
    result = calendar.list_recurring_instances("ev", "2026-05-18", "2026-06-01")
    assert result["_meta"]["count"] == 2
    assert result["instances"][0]["recurringEventId"] == "ev"


def test_create_event_with_recurrence_sets_rrule(fake_service):
    fake_service.events().insert().execute.return_value = {
        "id": "ev_x", "summary": "X", "start": {"dateTime": "2026-05-18T10:00:00+03:00"},
        "end": {"dateTime": "2026-05-18T11:00:00+03:00"},
    }
    calendar.create_event(
        summary="weekly sync",
        start="2026-05-18 10:00",
        recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=10"],
    )
    body = fake_service.events().insert.call_args.kwargs["body"]
    assert body["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=10"]


def test_overlay_accounts_aggregates_per_account(fake_service):
    # The same fake _service is used for both account aliases (since we
    # patch the module-level _service). Call returns same payload twice.
    fake_service.freebusy().query().execute.return_value = {
        "calendars": {"a@x.com": {"busy": []}},
    }
    result = calendar.overlay_accounts(
        {"main": ["a@x.com"], "shared": ["a@x.com"]},
        time_min="2026-05-20",
        time_max="2026-05-21",
    )
    assert set(result["per_account"]) == {"main", "shared"}
    assert result["_meta"]["accounts"] == ["main", "shared"]
