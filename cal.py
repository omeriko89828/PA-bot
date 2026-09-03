"""
The calendar layer the bot talks to. Merges Google and (optionally) iCloud
behind one interface so the rest of the bot doesn't care where an event lives.

Common event shape used everywhere above this file:
    {
      "source":  "google" | "icloud",
      "id":      str,          # opaque, only meaningful to that source
      "summary": str,
      "start":   str,          # ISO 8601; date-only string if all_day
      "all_day": bool,
    }

New events are created on Google (see NEW_EVENTS_ON). Reads merge both sources.
`window()` / `recall()` can read the past — the building block for later habit
/ pattern analysis.
"""

import asyncio
import datetime as dt
import logging

import gcal
import icloud

logger = logging.getLogger(__name__)

NEW_EVENTS_ON = "google"  # where "add ..." puts events


# --- reading ----------------------------------------------------------------


def _merge(lists: list[list[dict]]) -> list[dict]:
    events = [e for sub in lists for e in sub]
    events.sort(key=lambda e: e["start"])
    # Drop duplicates: the same event synced to both Google and iCloud shows up
    # twice. Treat same title + same start-minute as one; keep the first.
    seen = set()
    unique = []
    for e in events:
        key = (e["summary"].strip().lower(), e["start"][:16])
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return unique


async def window(start: dt.datetime, end: dt.datetime) -> list[dict]:
    """Merged events between two datetimes — the past is allowed."""
    google = asyncio.to_thread(
        lambda: [gcal.normalize(e) for e in gcal.events_in_window(start, end)]
    )
    if icloud.is_configured():
        apple = asyncio.to_thread(icloud.events_in_window, start, end)
        g, a = await _gather_lenient(google, apple)
        return _merge([g, a])
    return _merge([await google])


async def upcoming(days: int = 2) -> list[dict]:
    now = dt.datetime.now().astimezone()
    return await window(now, now + dt.timedelta(days=days))


async def recall(days_back: int, days_forward: int = 0) -> list[dict]:
    """Merged events from `days_back` ago up to `days_forward` from now."""
    now = dt.datetime.now().astimezone()
    return await window(now - dt.timedelta(days=days_back), now + dt.timedelta(days=days_forward))


async def _gather_lenient(google_awaitable, apple_awaitable):
    """Return (google_events, apple_events); if one source errors, log and use []."""
    results = await asyncio.gather(google_awaitable, apple_awaitable, return_exceptions=True)
    out = []
    for name, res in zip(("google", "icloud"), results):
        if isinstance(res, Exception):
            logger.warning("%s read failed: %s", name, res)
            out.append([])
        else:
            out.append(res)
    return out[0], out[1]


# --- formatting -----------------------------------------------------------

_TAG = {"google": "📅", "icloud": "🍎"}


def pretty(iso: str) -> str:
    return _pretty(iso)


def _pretty(iso: str) -> str:
    today_ = dt.date.today()
    if len(iso) == 10:
        d = dt.date.fromisoformat(iso)
        fmt = "%a %d %b (all day)" if d.year == today_.year else "%d %b %Y (all day)"
        return d.strftime(fmt)
    d = dt.datetime.fromisoformat(iso)
    fmt = "%a %d %b %H:%M" if d.year == today_.year else "%d %b %Y %H:%M"
    return d.strftime(fmt)


def format_events(events: list[dict], show_source: bool = True) -> str:
    if not events:
        return "Nothing on the calendar."
    lines = []
    for e in events:
        tag = f"{_TAG.get(e['source'], '•')} " if show_source else "• "
        lines.append(f"{tag}{_pretty(e['start'])} — {e['summary']}")
    return "\n".join(lines)


def describe(event: dict) -> str:
    return f"{event['summary']} — {_pretty(event['start'])}"


# --- writing --------------------------------------------------------------


async def create(summary: str, start_iso: str, end_iso: str) -> dict:
    if NEW_EVENTS_ON == "icloud" and icloud.is_configured():
        return await asyncio.to_thread(icloud.create_event, summary, start_iso, end_iso)
    raw = await asyncio.to_thread(gcal.create_event, summary, start_iso, end_iso)
    return gcal.normalize(raw)


async def delete(event: dict) -> None:
    if event["source"] == "icloud":
        await asyncio.to_thread(icloud.delete_event, event["id"])
    else:
        await asyncio.to_thread(gcal.delete_event, event["id"])


async def update(event: dict, summary: str, start_iso: str, end_iso: str) -> dict:
    """Change an event's title/time in place, on whichever backend it lives."""
    if event["source"] == "icloud":
        return await asyncio.to_thread(
            icloud.update_event, event["id"], summary, start_iso, end_iso
        )
    raw = await asyncio.to_thread(
        gcal.update_event, event["id"], summary, start_iso, end_iso
    )
    return gcal.normalize(raw)
