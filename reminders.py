"""
Standalone reminders — a 🔔 message at a chosen time.

Stored in reminders.json (gitignored) so they survive a restart; bot.py
re-schedules the future ones on startup and fires anything overdue.
"""

import datetime as dt
import json
import uuid
from pathlib import Path

_FILE = Path("reminders.json")


def _load() -> list[dict]:
    try:
        return json.loads(_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return []


def _save(items: list[dict]) -> None:
    _FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2))


def all_for(chat_id: int) -> list[dict]:
    """Upcoming reminders for a chat, soonest first."""
    return sorted(
        (r for r in _load() if r["chat_id"] == chat_id), key=lambda r: r["when"]
    )


def add(text: str, when_iso: str, chat_id: int) -> dict:
    items = _load()
    r = {
        "id": uuid.uuid4().hex[:8],
        "text": text.strip(),
        "when": when_iso,
        "chat_id": chat_id,
    }
    items.append(r)
    _save(items)
    return r


def get(reminder_id: str) -> dict | None:
    return next((r for r in _load() if r["id"] == reminder_id), None)


def remove(reminder_id: str) -> bool:
    items = _load()
    kept = [r for r in items if r["id"] != reminder_id]
    _save(kept)
    return len(kept) != len(items)


def split_by_time(now: dt.datetime) -> tuple[list[dict], list[dict]]:
    """(overdue, upcoming) across all chats."""
    overdue, upcoming = [], []
    for r in _load():
        when = dt.datetime.fromisoformat(r["when"])
        (overdue if when <= now else upcoming).append(r)
    return overdue, upcoming
