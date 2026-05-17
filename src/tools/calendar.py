"""Google Calendar tools — account-aware via our OAuth tokens.

Requires `https://www.googleapis.com/auth/calendar` scope (added in
config.SCOPES). Existing tokens without it will fail with insufficient_scope
on first call — re-OAuth via /accounts UI.
"""
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

from googleapiclient.discovery import build

from src.auth import get_credentials


DEFAULT_ACCOUNT = "main"
DEFAULT_TZ = "Europe/Moscow"


@lru_cache(maxsize=8)
def _service(account: str = DEFAULT_ACCOUNT):
    return build("calendar", "v3", credentials=get_credentials(account), cache_discovery=False)


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
) -> list[dict]:
    """Events in a date range. `time_min`/`time_max` accept 'YYYY-MM-DD' or
    RFC3339. Defaults to today through next 7 days. `query` filters by text
    in event title/description/location.
    """
    now = datetime.now(timezone.utc)
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
    return out


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
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Create a new event.
    - start/end: 'YYYY-MM-DD' (all-day) or 'YYYY-MM-DD HH:MM' (timed)
    - end defaults to start + 1 hour (for timed) or same day (for all-day)
    - reminder_minutes adds a popup reminder N minutes before; None = no reminder
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
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    _service(account).events().delete(calendarId=calendar_id, eventId=event_id).execute()
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
