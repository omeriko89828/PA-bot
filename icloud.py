"""
iCloud calendar access via CalDAV.

Needs two values in .env:
  ICLOUD_USERNAME       your Apple ID email
  ICLOUD_APP_PASSWORD   an app-specific password from appleid.apple.com
Optional:
  ICLOUD_CALENDAR       exact name of the calendar NEW iCloud events go to
                        (default: the first event calendar)

Reading (/agenda, briefing) merges every iCloud calendar that holds events —
Reminders lists and other to-do-only calendars are skipped.

If ICLOUD_USERNAME is not set, is_configured() returns False and the rest of
the bot simply skips iCloud.

Run directly to check the connection and list your calendars:
    ./.venv/bin/python icloud.py
"""

import datetime as dt
import os
import uuid

import caldav
from dotenv import load_dotenv

load_dotenv()

_URL = "https://caldav.icloud.com/"

_client = None
_event_calendars = None  # cached list of caldav.Calendar that support VEVENT


def is_configured() -> bool:
    return bool(os.environ.get("ICLOUD_USERNAME") and os.environ.get("ICLOUD_APP_PASSWORD"))


def _get_client() -> caldav.DAVClient:
    global _client
    if _client is None:
        _client = caldav.DAVClient(
            url=_URL,
            username=os.environ["ICLOUD_USERNAME"],
            password=os.environ["ICLOUD_APP_PASSWORD"],
        )
    return _client


def _name(calendar) -> str:
    try:
        return calendar.get_display_name()
    except Exception:
        return str(calendar)


def _all_calendars():
    return _get_client().principal().calendars()


def _get_event_calendars():
    global _event_calendars
    if _event_calendars is not None:
        return _event_calendars

    keep = []
    for c in _all_calendars():
        try:
            comps = c.get_supported_components()
        except Exception:
            comps = []
        if "VEVENT" in comps:
            keep.append(c)
    _event_calendars = keep
    return keep


def _write_calendar():
    """The calendar NEW iCloud events are saved to."""
    wanted = os.environ.get("ICLOUD_CALENDAR")
    cals = _get_event_calendars()
    if not cals:
        raise RuntimeError("no iCloud event calendars found")
    if wanted:
        for c in cals:
            if _name(c) == wanted:
                return c
        raise RuntimeError(
            f"iCloud calendar {wanted!r} not found; have: {[_name(c) for c in cals]}"
        )
    return cals[0]


def calendar_names() -> list[str]:
    return [_name(c) for c in _all_calendars()]


def _cal_segment(url: str) -> str | None:
    """'.../calendars/<segment>/...'  ->  '<segment>' (lowercased)."""
    parts = str(url).rstrip("/").split("/calendars/")
    if len(parts) < 2:
        return None
    return parts[1].split("/")[0].lower()


def _calendar_for(event_url: str):
    """Which of our event calendars an event URL belongs to (matches on the
    calendar's path segment, ignoring scheme/port/case)."""
    seg = _cal_segment(event_url)
    for c in _get_event_calendars():
        if _cal_segment(str(c.url)) == seg:
            return c
    return None


def _normalize(event) -> dict:
    # `event` is a caldav.Event; its .url is the stable handle we use to edit/delete.
    component = event.icalendar_component
    start = component.get("dtstart").dt
    end_prop = component.get("dtend")
    end = end_prop.dt if end_prop is not None else start
    all_day = not isinstance(start, dt.datetime)
    return {
        "source": "icloud",
        "id": str(event.url),  # the event's CalDAV URL
        "summary": str(component.get("summary", "(no title)")),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "all_day": all_day,
    }


def _reset() -> None:
    global _client, _event_calendars
    _client = None
    _event_calendars = None


def _search(start: dt.datetime, end: dt.datetime) -> list[dict]:
    for attempt in (1, 2):
        try:
            out = []
            for c in _get_event_calendars():
                for e in c.search(start=start, end=end, event=True, expand=True):
                    out.append(_normalize(e))
            out.sort(key=lambda ev: ev["start"])
            return out
        except Exception:
            if attempt == 2:
                raise
            _reset()  # drop the stale connection and reconnect once


def upcoming_events(within_days: int = 2) -> list[dict]:
    now = dt.datetime.now().astimezone()
    return _search(now, now + dt.timedelta(days=within_days))


def create_event(summary: str, start_iso: str, end_iso: str) -> dict:
    uid = str(uuid.uuid4())
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    start = dt.datetime.fromisoformat(start_iso).strftime("%Y%m%dT%H%M%S")
    end = dt.datetime.fromisoformat(end_iso).strftime("%Y%m%dT%H%M%S")
    ical = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//PA bot//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"DTSTAMP:{stamp}\r\n"
        f"DTSTART:{start}\r\n"
        f"DTEND:{end}\r\n"
        f"SUMMARY:{summary}\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    saved = _write_calendar().save_event(ical)
    return {
        "source": "icloud",
        "id": str(saved.url),
        "summary": summary,
        "start": start_iso,
        "end": end_iso,
        "all_day": False,
    }


def update_event(url: str, summary: str, start_iso: str, end_iso: str) -> dict:
    """Replace an iCloud event's title/time. `url` is the `id` from _normalize."""
    parent = _calendar_for(url)
    if parent is None:
        raise RuntimeError(f"can't find the calendar for iCloud event {url}")
    event = parent.event_by_url(url)  # a real, loaded resource
    comp = event.icalendar_component
    comp["summary"] = summary
    comp["dtstart"].dt = dt.datetime.fromisoformat(start_iso)
    if "dtend" in comp:
        comp["dtend"].dt = dt.datetime.fromisoformat(end_iso)
    else:
        comp.add("dtend", dt.datetime.fromisoformat(end_iso))
    event.save()
    return {
        "source": "icloud",
        "id": url,
        "summary": summary,
        "start": start_iso,
        "end": end_iso,
        "all_day": False,
    }


def delete_event(url: str) -> None:
    """Delete an iCloud event by its CalDAV URL (the `id` from _normalize)."""
    caldav.Event(client=_get_client(), url=url).delete()


if __name__ == "__main__":
    if not is_configured():
        print("ICLOUD_USERNAME / ICLOUD_APP_PASSWORD not set in .env")
    else:
        print("All calendars:", calendar_names())
        print("Event calendars:", [_name(c) for c in _get_event_calendars()])
        print("New iCloud events go to:", _name(_write_calendar()))
        print("\nNext 7 days:")
        for e in upcoming_events(7):
            print(f"  {e['start']}  {e['summary']}")
