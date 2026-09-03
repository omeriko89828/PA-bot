"""
Google Calendar access — authorization + reading events.

First run opens a browser asking you to authorize. After that a `token.json`
file is saved and reused, and refreshed automatically when it expires.

Run this file directly to authorize and print your upcoming events:
    ./.venv/bin/python gcal.py
"""

import datetime as dt
import os.path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# calendar.events allows both reading and creating/editing events (not calendar
# settings). Changing this list invalidates an existing token.json — delete it
# and re-run this file to re-authorize.
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"


def _get_credentials() -> Credentials:
    """Load saved credentials, refreshing or running the browser flow as needed."""
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return creds


def _service():
    return build("calendar", "v3", credentials=_get_credentials())


def _list(time_min: dt.datetime, time_max: dt.datetime | None, max_results: int) -> list[dict]:
    params = dict(
        calendarId="primary",
        timeMin=time_min.astimezone(dt.timezone.utc).isoformat(),
        maxResults=max_results,
        singleEvents=True,      # expand recurring events into individual ones
        orderBy="startTime",
    )
    if time_max is not None:
        params["timeMax"] = time_max.astimezone(dt.timezone.utc).isoformat()
    return _service().events().list(**params).execute().get("items", [])


def upcoming_events(max_results: int = 25, within_days: int | None = None) -> list[dict]:
    """Upcoming events on the primary calendar, starting now."""
    now = dt.datetime.now(dt.timezone.utc)
    time_max = now + dt.timedelta(days=within_days) if within_days is not None else None
    return _list(now, time_max, max_results)


def events_today() -> list[dict]:
    """Events from now until the end of today (local time)."""
    now = dt.datetime.now().astimezone()
    end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0)
    return _list(now, end_of_day, 25)


def create_event(
    summary: str, start_iso: str, end_iso: str, description: str | None = None
) -> dict:
    """
    Create an event on the primary calendar and return it.

    start_iso / end_iso: ISO 8601 datetimes *with* a timezone offset,
    e.g. "2026-09-09T15:00:00+03:00".
    """
    body = {
        "summary": summary,
        "start": {"dateTime": start_iso},
        "end": {"dateTime": end_iso},
    }
    if description:
        body["description"] = description

    return _service().events().insert(calendarId="primary", body=body).execute()


def delete_event(event_id: str) -> None:
    """Delete an event by its id."""
    _service().events().delete(calendarId="primary", eventId=event_id).execute()


def event_when(event: dict) -> str:
    """Human-readable start time of an event dict."""
    start = event["start"].get("dateTime") or event["start"].get("date")
    return _pretty(start)


def format_events(events: list[dict]) -> str:
    """Turn a list of event dicts into a short human-readable summary."""
    if not events:
        return "Nothing on the calendar coming up."
    lines = []
    for e in events:
        # Timed events have start.dateTime; all-day events have start.date.
        start = e["start"].get("dateTime") or e["start"].get("date")
        lines.append(f"• {_pretty(start)} — {e.get('summary', '(no title)')}")
    return "\n".join(lines)


def pretty(iso: str) -> str:
    """Public: format an ISO date/datetime string for humans."""
    return _pretty(iso)


def _pretty(iso: str) -> str:
    today = dt.date.today()
    if len(iso) == 10:  # "2026-09-03" -> all-day
        d = dt.date.fromisoformat(iso)
        fmt = "%a %d %b (all day)" if d.year == today.year else "%d %b %Y (all day)"
        return d.strftime(fmt)
    d = dt.datetime.fromisoformat(iso)
    fmt = "%a %d %b %H:%M" if d.year == today.year else "%d %b %Y %H:%M"
    return d.strftime(fmt)


if __name__ == "__main__":
    print(format_events(upcoming_events(within_days=7)))
