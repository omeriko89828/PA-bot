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

# Phase 3 is read-only. Creating events (Phase 4) needs a wider scope, which will
# mean deleting token.json and authorizing once more.
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

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


def upcoming_events(max_results: int = 25, within_days: int | None = None) -> list[dict]:
    """
    Return upcoming events on the primary calendar, starting now.

    within_days: if given, only events starting in the next N days.
    """
    now = dt.datetime.now(dt.timezone.utc)
    params = dict(
        calendarId="primary",
        timeMin=now.isoformat(),
        maxResults=max_results,
        singleEvents=True,      # expand recurring events into individual ones
        orderBy="startTime",
    )
    if within_days is not None:
        params["timeMax"] = (now + dt.timedelta(days=within_days)).isoformat()

    result = _service().events().list(**params).execute()
    return result.get("items", [])


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
