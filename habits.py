"""
Habit detection from past calendar events — deterministic.

Groups past events by a normalised title and reports, for each thing that
recurs often enough: how many times, roughly per week, which weekday(s) and
time, and whether it's missing from the week ahead.

This is the raw layer; brain.describe_patterns() turns it into prose.
"""

import datetime as dt
import re
from collections import Counter

_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _norm(summary: str) -> str:
    s = summary.strip().lower()
    s = re.sub(r"^\d{1,2}:\d{2}\s*[-–]\s*", "", s)  # drop a leading "09:00 - "
    s = re.sub(r"\s+", " ", s)
    return s


_LOCAL_TZ = dt.datetime.now().astimezone().tzinfo


def _start(event: dict) -> dt.datetime:
    d = dt.datetime.fromisoformat(event["start"])
    return d if d.tzinfo else d.replace(tzinfo=_LOCAL_TZ)  # all-day -> naive


def analyse(past: list[dict], upcoming: list[dict], min_count: int = 3) -> list[dict]:
    """Recurring routines from `past`, most frequent first."""
    timed = [e for e in past if not e.get("all_day")]
    if len(timed) < min_count:
        return []

    starts = [_start(e) for e in past]
    weeks = max((max(starts) - min(starts)).days / 7, 1)

    groups: dict[str, list[dict]] = {}
    for e in past:
        groups.setdefault(_norm(e["summary"]), []).append(e)

    upcoming_norms = {_norm(e["summary"]) for e in upcoming}

    routines = []
    for key, evs in groups.items():
        if len(evs) < min_count:
            continue
        days = Counter()
        hours = []
        for e in evs:
            d = _start(e)
            days[d.weekday()] += 1
            if not e.get("all_day"):
                hours.append(d.hour)
        routines.append(
            {
                "title": evs[0]["summary"],
                "count": len(evs),
                "per_week": round(len(evs) / weeks, 1),
                "days": [_WEEKDAYS[i] for i, _ in days.most_common(2)],
                "typical_hour": Counter(hours).most_common(1)[0][0] if hours else None,
                "missing_next_week": key not in upcoming_norms,
            }
        )

    routines.sort(key=lambda r: r["count"], reverse=True)
    return routines


def format_report(routines: list[dict], weeks_back: int) -> str:
    if not routines:
        return f"Not enough history yet to spot patterns (looked back {weeks_back} weeks)."
    lines = [f"Patterns from the last {weeks_back} weeks:\n"]
    for r in routines[:10]:
        when = "/".join(r["days"]) if r["days"] else "no fixed day"
        at = f" ~{r['typical_hour']:02d}:00" if r["typical_hour"] is not None else ""
        rate = f"{r['per_week']}/wk" if r["per_week"] >= 1 else f"{r['count']}x total"
        flag = " -- nothing next week" if r["missing_next_week"] and r["per_week"] >= 0.75 else ""
        lines.append(f"- {r['title']} - {rate}, usually {when}{at}{flag}")
    return "\n".join(lines)
