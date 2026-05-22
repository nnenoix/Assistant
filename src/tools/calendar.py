"""Google Calendar tools — account-aware via our OAuth tokens.

Requires `https://www.googleapis.com/auth/calendar` scope (added in
config.SCOPES). Existing tokens without it will fail with insufficient_scope
on first call — re-OAuth via /accounts UI.
"""
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

from googleapiclient.discovery import build

from src.auth import RetryingHttpRequest, get_credentials


DEFAULT_ACCOUNT = "main"
DEFAULT_TZ = "Europe/Moscow"


@lru_cache(maxsize=8)
def _service(account: str = DEFAULT_ACCOUNT):
    return build(
        "calendar", "v3",
        credentials=get_credentials(account),
        cache_discovery=False,
        requestBuilder=RetryingHttpRequest,
    )


def _parse_when(value: str, tz: str = DEFAULT_TZ) -> dict[str, Any]:
    """Convert a user-friendly time string into a Google Calendar Event time block.
    Accepts:
      'YYYY-MM-DD'             → all-day event
      'YYYY-MM-DD HH:MM'       → timed in `tz`
      RFC3339 (with 'T' and TZ) → passed through
    """
    if "T" in value:
        return {"dateTime": value, "timeZone": tz}
    if " " in value:
        d, t = value.split(" ", 1)
        return {"dateTime": f"{d}T{t}:00", "timeZone": tz}
    return {"date": value}


def list_calendars(account: str = DEFAULT_ACCOUNT) -> list[dict]:
    """All calendars the user has access to. Identifies the primary one."""
    resp = _service(account).calendarList().list().execute()
    return [
        {
            "id": c["id"],
            "summary": c.get("summary"),
            "primary": c.get("primary", False),
            "access_role": c.get("accessRole"),
            "timezone": c.get("timeZone"),
        }
        for c in resp.get("items", [])
    ]


def list_events(
    time_min: str | None = None,
    time_max: str | None = None,
    calendar_id: str = "primary",
    max_results: int = 50,
    query: str | None = None,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Events in a date range. `time_min`/`time_max` accept 'YYYY-MM-DD' or
    RFC3339. Defaults to today through next 7 days. `query` filters by text
    in event title/description/location.

    Returns {events, _meta}. `_meta.window` echoes the EXACT time range
    that was queried, plus `default_used: bool` flagging when the caller
    omitted dates and the 7-day default kicked in — surface this in the
    answer so users know what window was actually scanned.
    """
    now = datetime.now(timezone.utc)
    default_used = time_min is None and time_max is None
    if time_min is None:
        time_min = now.isoformat()
    elif "T" not in time_min:
        time_min = f"{time_min}T00:00:00Z"
    if time_max is None:
        time_max = (now + timedelta(days=7)).isoformat()
    elif "T" not in time_max:
        time_max = f"{time_max}T23:59:59Z"

    params: dict[str, Any] = {
        "calendarId": calendar_id,
        "timeMin": time_min,
        "timeMax": time_max,
        "maxResults": min(max(max_results, 1), 250),
        "singleEvents": True,
        "orderBy": "startTime",
    }
    if query:
        params["q"] = query

    resp = _service(account).events().list(**params).execute()
    out = []
    for e in resp.get("items", []):
        start = e["start"].get("dateTime", e["start"].get("date"))
        end = e["end"].get("dateTime", e["end"].get("date"))
        out.append({
            "id": e["id"],
            "summary": e.get("summary"),
            "start": start,
            "end": end,
            "all_day": "date" in e["start"],
            "location": e.get("location"),
            "description": (e.get("description") or "")[:300],
            "attendees": [a.get("email") for a in e.get("attendees", [])],
            "link": e.get("htmlLink"),
            "status": e.get("status"),
        })

    truncated = bool(resp.get("nextPageToken"))
    return {
        "events": out,
        "_meta": {
            "window": {
                "time_min": time_min,
                "time_max": time_max,
                "calendar_id": calendar_id,
                "default_used": default_used,
            },
            "returned_count": len(out),
            "truncated": truncated,
            "truncation_reason": (
                "more events exist past max_results; raise max_results (cap 250) or narrow the window"
                if truncated else None
            ),
            "empty_reason": None if out else "no_matches",
        },
    }


def get_event(event_id: str, calendar_id: str = "primary", account: str = DEFAULT_ACCOUNT) -> dict:
    """Full details of a single event."""
    return _service(account).events().get(calendarId=calendar_id, eventId=event_id).execute()


def create_event(
    summary: str,
    start: str,
    end: str | None = None,
    description: str | None = None,
    location: str | None = None,
    attendees: list[str] | None = None,
    calendar_id: str = "primary",
    reminder_minutes: int | None = 15,
    timezone_str: str = DEFAULT_TZ,
    recurrence: list[str] | None = None,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Create a new event.
    - start/end: 'YYYY-MM-DD' (all-day) or 'YYYY-MM-DD HH:MM' (timed)
    - end defaults to start + 1 hour (for timed) or same day (for all-day)
    - reminder_minutes adds a popup reminder N minutes before; None = no reminder
    - recurrence is a list of RFC5545 RRULE strings, e.g.
      ['RRULE:FREQ=WEEKLY;BYDAY=MO,WE;COUNT=10']. See calendar_freebusy and
      calendar_list_recurring_instances for working with recurring events.
    """
    start_obj = _parse_when(start, timezone_str)
    if end is None:
        if "dateTime" in start_obj:
            dt = datetime.fromisoformat(start_obj["dateTime"])
            end_obj = {"dateTime": (dt + timedelta(hours=1)).isoformat(), "timeZone": timezone_str}
        else:
            end_obj = dict(start_obj)
    else:
        end_obj = _parse_when(end, timezone_str)

    body: dict[str, Any] = {"summary": summary, "start": start_obj, "end": end_obj}
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if attendees:
        body["attendees"] = [{"email": e} for e in attendees]
    if reminder_minutes is not None:
        body["reminders"] = {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": max(0, reminder_minutes)}],
        }
    if recurrence:
        body["recurrence"] = recurrence

    resp = _service(account).events().insert(calendarId=calendar_id, body=body).execute()
    return {
        "id": resp["id"],
        "summary": resp.get("summary"),
        "start": resp["start"].get("dateTime", resp["start"].get("date")),
        "end": resp["end"].get("dateTime", resp["end"].get("date")),
        "link": resp.get("htmlLink"),
    }


def update_event(
    event_id: str,
    updates: dict[str, Any],
    calendar_id: str = "primary",
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Patch an event with the provided updates. `updates` can include any of:
    summary, description, location, start, end (use {date} or {dateTime,timeZone}),
    attendees, reminders, status ('confirmed'/'tentative'/'cancelled').
    """
    return _service(account).events().patch(
        calendarId=calendar_id, eventId=event_id, body=updates,
    ).execute()


def delete_event(
    event_id: str,
    calendar_id: str = "primary",
    dry_run: bool = False,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Delete a calendar event. With `dry_run=True` fetches event metadata
    (summary, start, attendees) and returns a preview WITHOUT deleting."""
    svc = _service(account)
    if dry_run:
        try:
            ev = svc.events().get(calendarId=calendar_id, eventId=event_id).execute()
        except Exception as e:
            return {
                "dry_run": True,
                "executed": False,
                "plan": {
                    "would_call": "calendar.events.delete",
                    "event_id": event_id,
                    "calendar_id": calendar_id,
                    "preview_error": str(e)[:200],
                },
                "_meta": {"native_preview": True},
            }
        return {
            "dry_run": True,
            "executed": False,
            "plan": {
                "would_call": "calendar.events.delete",
                "event_id": event_id,
                "calendar_id": calendar_id,
                "summary": ev.get("summary"),
                "start": ev.get("start"),
                "end": ev.get("end"),
                "attendees": [a.get("email") for a in (ev.get("attendees") or [])],
                "organizer": ev.get("organizer", {}).get("email"),
                "reversibility": (
                    "NOT REVERSIBLE via API. Deleted events go to Calendar "
                    "trash (visible in UI for ~30 days); restore manually if "
                    "needed. Attendees may receive cancellation notifications."
                ),
            },
            "_meta": {"native_preview": True},
        }
    svc.events().delete(calendarId=calendar_id, eventId=event_id).execute()
    return {"deleted": True, "event_id": event_id}


def find_free_time(
    start_date: str,
    end_date: str,
    duration_minutes: int = 60,
    work_hours_start: int = 9,
    work_hours_end: int = 19,
    weekdays_only: bool = True,
    calendar_id: str = "primary",
    timezone_str: str = DEFAULT_TZ,
    account: str = DEFAULT_ACCOUNT,
) -> list[dict]:
    """Find free slots of `duration_minutes` between `work_hours_start` and
    `work_hours_end` on the date range. Uses Calendar's free/busy endpoint.
    Returns up to 20 slots, sorted earliest first.
    """
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(timezone_str)

    body = {
        "timeMin": f"{start_date}T00:00:00Z",
        "timeMax": f"{end_date}T23:59:59Z",
        "items": [{"id": calendar_id}],
    }
    resp = _service(account).freebusy().query(body=body).execute()
    busy_raw = resp["calendars"][calendar_id].get("busy", [])
    busy = [
        (
            datetime.fromisoformat(b["start"].replace("Z", "+00:00")).astimezone(tz),
            datetime.fromisoformat(b["end"].replace("Z", "+00:00")).astimezone(tz),
        )
        for b in busy_raw
    ]

    free_slots: list[dict] = []
    cur_date = datetime.fromisoformat(f"{start_date}T00:00:00").replace(tzinfo=tz)
    end_dt = datetime.fromisoformat(f"{end_date}T23:59:59").replace(tzinfo=tz)
    delta = timedelta(minutes=duration_minutes)

    while cur_date.date() <= end_dt.date() and len(free_slots) < 20:
        if weekdays_only and cur_date.weekday() >= 5:
            cur_date += timedelta(days=1)
            continue

        day_start = cur_date.replace(hour=work_hours_start, minute=0, second=0, microsecond=0)
        day_end = cur_date.replace(hour=work_hours_end, minute=0, second=0, microsecond=0)

        day_busy = sorted([(s, e) for s, e in busy if s.date() == cur_date.date() or e.date() == cur_date.date()])
        cursor = day_start
        for bs, be in day_busy:
            bs_clamped = max(bs, day_start)
            be_clamped = min(be, day_end)
            if cursor + delta <= bs_clamped:
                free_slots.append({
                    "start": cursor.isoformat(),
                    "end": bs_clamped.isoformat(),
                    "duration_minutes": int((bs_clamped - cursor).total_seconds() // 60),
                })
                if len(free_slots) >= 20:
                    break
            cursor = max(cursor, be_clamped)
        if len(free_slots) < 20 and cursor + delta <= day_end:
            free_slots.append({
                "start": cursor.isoformat(),
                "end": day_end.isoformat(),
                "duration_minutes": int((day_end - cursor).total_seconds() // 60),
            })

        cur_date += timedelta(days=1)

    return free_slots


def quick_reminder(
    text: str,
    when: str,
    reminder_minutes: int = 0,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Shorthand for a single-purpose reminder: create a brief event at `when`
    with a popup reminder `reminder_minutes` before (0 = at the moment of the
    event). Use this for 'напомни мне в среду в 15:00 проверить X' patterns.
    """
    return create_event(
        summary=text,
        start=when,
        reminder_minutes=reminder_minutes,
        account=account,
    )


# -------- Phase 6: group / multi-attendee ops --------

def _to_rfc3339(value: str) -> str:
    """Normalize a date or datetime string to RFC3339 (UTC if no zone)."""
    if "T" in value:
        return value
    return f"{value}T00:00:00Z"


def freebusy(
    emails: list[str],
    time_min: str,
    time_max: str,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Query free/busy slots across one or more calendars (by email).

    `emails` is a list of calendar IDs (typically email addresses).
    `time_min`/`time_max` accept date or RFC3339. Returns:
        {per_email: [{email, busy: [{start, end}], errors: [...]}], _meta: {time_min, time_max}}.

    For one's own calendar pass the address (or 'primary' wouldn't work
    here — FreeBusy needs full IDs).
    """
    tmin = _to_rfc3339(time_min)
    tmax = _to_rfc3339(time_max)
    resp = _service(account).freebusy().query(body={
        "timeMin": tmin,
        "timeMax": tmax,
        "items": [{"id": e} for e in emails],
    }).execute()
    cals = resp.get("calendars", {}) or {}
    out = []
    for e in emails:
        info = cals.get(e, {})
        out.append({
            "email": e,
            "busy": info.get("busy", []),
            "errors": info.get("errors", []),
        })
    return {
        "per_email": out,
        "_meta": {
            "time_min": tmin,
            "time_max": tmax,
            "queried_emails": emails,
        },
    }


def find_meeting_slot(
    attendees: list[str],
    duration_minutes: int,
    time_min: str,
    time_max: str,
    working_hours_only: bool = True,
    work_hours_start: int = 9,
    work_hours_end: int = 19,
    weekdays_only: bool = True,
    tz: str = DEFAULT_TZ,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Find the FIRST common free slot of `duration_minutes` across all
    `attendees` (calendar IDs / emails) in [time_min, time_max].

    Builds on freebusy(): merges busy intervals from all attendees, walks
    the window with `duration_minutes` step and returns the first slot where
    everyone is free. Optionally restricts to working hours / weekdays.

    Working hours and weekday filters are evaluated in `tz` (default
    ``Europe/Moscow``). Without this the filter applies in UTC, so "9–19
    Moscow" silently becomes "12–22 Moscow" — wrong slots get returned and
    the midnight-rollover branch fires at the wrong instant.

    Returns {found: bool, slot?: {start, end}, candidates_checked, _meta}.
    """
    from zoneinfo import ZoneInfo
    target_tz = ZoneInfo(tz)

    fb = freebusy(attendees, time_min, time_max, account=account)
    # Merge all busy intervals across attendees
    busy: list[tuple[datetime, datetime]] = []
    for entry in fb["per_email"]:
        for b in entry["busy"]:
            busy.append((datetime.fromisoformat(b["start"].replace("Z", "+00:00")),
                         datetime.fromisoformat(b["end"].replace("Z", "+00:00"))))
    busy.sort()

    tmin = datetime.fromisoformat(_to_rfc3339(time_min).replace("Z", "+00:00"))
    tmax = datetime.fromisoformat(_to_rfc3339(time_max).replace("Z", "+00:00"))
    duration = timedelta(minutes=duration_minutes)

    candidate = tmin
    checked = 0
    while candidate + duration <= tmax:
        checked += 1
        end = candidate + duration
        # Working hours / weekday filter — evaluated in caller's tz, not UTC
        local = candidate.astimezone(target_tz)
        local_end = end.astimezone(target_tz)
        if weekdays_only and local.weekday() >= 5:
            # Skip to next Monday 09:00 local
            days_ahead = 7 - local.weekday()
            next_local = (local + timedelta(days=days_ahead)).replace(
                hour=work_hours_start, minute=0, second=0, microsecond=0,
            )
            candidate = next_local.astimezone(timezone.utc)
            continue
        if working_hours_only:
            if local.hour < work_hours_start:
                next_local = local.replace(
                    hour=work_hours_start, minute=0, second=0, microsecond=0,
                )
                candidate = next_local.astimezone(timezone.utc)
                continue
            crosses_midnight = local_end.date() != local.date()
            past_end = (
                local_end.hour > work_hours_end
                or (local_end.hour == work_hours_end and local_end.minute > 0)
            )
            if crosses_midnight or past_end:
                # Skip to next day's working start (local-tz)
                next_local = (local + timedelta(days=1)).replace(
                    hour=work_hours_start, minute=0, second=0, microsecond=0,
                )
                candidate = next_local.astimezone(timezone.utc)
                continue
        # Check overlap with any busy interval
        overlap = False
        for b_start, b_end in busy:
            if candidate < b_end and end > b_start:
                # Move candidate to b_end (rounded up to next 15-min mark)
                candidate = b_end
                # Round up to next 15-min
                discard = candidate.minute % 15
                if discard:
                    candidate += timedelta(minutes=(15 - discard))
                candidate = candidate.replace(second=0, microsecond=0)
                overlap = True
                break
        if overlap:
            continue
        # Found a free slot
        return {
            "found": True,
            "slot": {"start": candidate.isoformat(), "end": end.isoformat()},
            "candidates_checked": checked,
            "_meta": {
                "attendees": attendees,
                "duration_minutes": duration_minutes,
                "time_min": time_min,
                "time_max": time_max,
                "tz": tz,
            },
        }
    return {
        "found": False,
        "slot": None,
        "candidates_checked": checked,
        "_meta": {
            "attendees": attendees,
            "duration_minutes": duration_minutes,
            "time_min": time_min,
            "time_max": time_max,
            "tz": tz,
            "reason": "no overlapping free slot found within working hours",
        },
    }


def list_recurring_instances(
    event_id: str,
    time_min: str,
    time_max: str,
    calendar_id: str = "primary",
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Expand a recurring event series into individual instances within
    [time_min, time_max].

    Use after `calendar_create_event(..., recurrence=['RRULE:FREQ=WEEKLY...'])`
    to see WHEN exactly the future instances fall. Returns {instances, _meta}.
    """
    tmin = _to_rfc3339(time_min)
    tmax = _to_rfc3339(time_max)
    resp = _service(account).events().instances(
        calendarId=calendar_id,
        eventId=event_id,
        timeMin=tmin,
        timeMax=tmax,
        maxResults=250,
        showDeleted=False,
    ).execute()
    instances = []
    for e in resp.get("items", []):
        instances.append({
            "id": e.get("id"),
            "start": e.get("start", {}).get("dateTime", e.get("start", {}).get("date")),
            "end": e.get("end", {}).get("dateTime", e.get("end", {}).get("date")),
            "status": e.get("status"),
            "recurringEventId": e.get("recurringEventId"),
        })
    return {
        "instances": instances,
        "_meta": {
            "event_id": event_id,
            "time_min": tmin,
            "time_max": tmax,
            "count": len(instances),
            "empty_reason": None if instances else "no_instances_in_window",
        },
    }


def overlay_accounts(
    emails_per_account: dict[str, list[str]],
    time_min: str,
    time_max: str,
) -> dict:
    """Run FreeBusy across multiple configured `account` aliases AT ONCE,
    each with its own list of calendar IDs to query. Returns a single map
    keyed by account → emails → busy intervals.

    Useful when consolidating «свободно ли всё подразделение» across two
    Google accounts (e.g. main + shared).

    `emails_per_account` example: {"main": ["a@x.com"], "shared": ["b@y.com", "c@y.com"]}
    """
    from src.tools._errors import _classify_exception

    out: dict[str, dict] = {}
    errors: list[dict] = []
    for acct, emails in emails_per_account.items():
        try:
            out[acct] = freebusy(emails, time_min, time_max, account=acct)
        except Exception as e:
            kind, status = _classify_exception(e)
            err_payload = {
                "account": acct,
                "kind": type(e).__name__,
                "error_kind": kind,
                "http_status": status,
                "message": str(e)[:300],
            }
            errors.append(err_payload)
            out[acct] = {"error": err_payload, "per_email": []}
    meta: dict = {
        "accounts": list(emails_per_account.keys()),
        "time_min": _to_rfc3339(time_min),
        "time_max": _to_rfc3339(time_max),
    }
    if errors:
        meta["errors"] = errors[:5]
        meta["error_count"] = len(errors)
        meta["warning"] = (
            f"{len(errors)}/{len(emails_per_account)} accounts failed; "
            "per-account dicts under per_account[<acct>].error carry details."
        )
    return {"per_account": out, "_meta": meta}
