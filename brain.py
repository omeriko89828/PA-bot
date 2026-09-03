"""
The "brain" — everything about talking to the language model lives here.

`reply(history, user_message)` returns an `Answer`:
  - Answer(text="...")             a plain conversational reply
  - Answer(action={"name","args"}) the model wants to create / delete / edit an
                                   event (bot confirms first) or recall past
                                   events to answer a question (read-only).

Helpers: choose_events() picks which event a request refers to, plan_edit()
works out an event's new values, answer_about_events() answers a question from a
list of past events, briefing_summary() writes the morning briefing.

If we ever swap Gemini for another model, this is the only file that changes.
"""

import asyncio
import dataclasses
import datetime as dt
import json
import logging
import os

from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai import errors as genai_errors

load_dotenv()

logger = logging.getLogger(__name__)

_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

# Fast and consistent (~0.7s), function calling works, generous-ish free tier.
MODEL = "gemini-3.5-flash-lite"

# Language for the daily briefing (chat replies just match whatever you write).
BRIEFING_LANG = os.environ.get("BRIEFING_LANG", "Hebrew")

# Only retry on 503 (server briefly overloaded). A 429 means we hit a rate/quota
# limit — retrying just freezes the bot, so we surface it instead.
RETRY_ON = (503,)
MAX_ATTEMPTS = 3

# --- Tools the model is allowed to request -------------------------------------

_CREATE_EVENT_TOOL = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="create_event",
            description=(
                "Create a calendar event. Call this whenever the user asks to "
                "add, schedule, book, set up, or remind them of something at a "
                "particular time."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "summary": types.Schema(
                        type="STRING",
                        description="Short event title, e.g. 'Dentist appointment'.",
                    ),
                    "start": types.Schema(
                        type="STRING",
                        description=(
                            "Start time as ISO 8601 with timezone offset, "
                            "e.g. '2026-09-09T15:00:00+03:00'. Resolve relative "
                            "dates ('tuesday', 'tomorrow') using the current "
                            "date given in the system instructions."
                        ),
                    ),
                    "end": types.Schema(
                        type="STRING",
                        description=(
                            "End time, same format as start. If the user gives "
                            "no duration, use one hour after start."
                        ),
                    ),
                },
                required=["summary", "start", "end"],
            ),
        )
    ]
)

_DELETE_EVENT_TOOL = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="delete_event",
            description=(
                "Delete/cancel/remove an existing calendar event. Call this when "
                "the user asks to delete, cancel, remove, or clear an event. The "
                "bot will figure out which event and confirm before deleting — "
                "you don't need to identify it."
            ),
            parameters=types.Schema(type="OBJECT", properties={}),
        )
    ]
)

_EDIT_EVENT_TOOL = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="edit_event",
            description=(
                "Change an existing event — move it to a different time, make it "
                "longer/shorter, or rename it. Call this when the user asks to "
                "move, reschedule, postpone, rename, or shift an event. The bot "
                "figures out which event and the new details, and confirms — you "
                "don't need to identify it."
            ),
            parameters=types.Schema(type="OBJECT", properties={}),
        )
    ]
)

_PATTERNS_TOOL = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="find_patterns",
            description=(
                "Analyse the user's recurring routines and habits from the last "
                "couple of months of events. Call this when they ask about their "
                "patterns, routines, or habits, how often they do something, or "
                "whether they're keeping up with a regular activity."
            ),
            parameters=types.Schema(type="OBJECT", properties={}),
        )
    ]
)

_RECALL_TOOL = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="recall",
            description=(
                "Look at PAST (or a wider range of) calendar events to answer a "
                "question about the user's history or habits — 'when did I last "
                "see the dentist', 'how many times did I work out last month', "
                "'what did I do last week'. The date table only covers the "
                "future, so use this whenever the question is about the past."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "days_back": types.Schema(
                        type="INTEGER",
                        description="How many days into the past to look (e.g. 7, 30, 90).",
                    ),
                    "days_forward": types.Schema(
                        type="INTEGER",
                        description="How many days ahead to also include (default 0).",
                    ),
                },
                required=["days_back"],
            ),
        )
    ]
)


def _date_table() -> tuple[dt.datetime, str]:
    """(now, a plain-text list of the next 15 dates by weekday)."""
    now = dt.datetime.now().astimezone()
    lines = []
    for i in range(15):
        day = now.date() + dt.timedelta(days=i)
        label = {0: " (today)", 1: " (tomorrow)"}.get(i, "")
        lines.append(f"  {day.strftime('%A %Y-%m-%d')}{label}")
    return now, "\n".join(lines)


def _system_prompt() -> str:
    now, date_table = _date_table()
    return (
        "You are a personal assistant running inside a Telegram bot. "
        "The user just finished their first year of a CS degree and is building "
        "you as a learning project. "
        "Keep replies short and conversational — this is a phone chat, not an essay.\n\n"
        f"The current time is {now.strftime('%H:%M')} and the timezone offset is "
        f"{now.strftime('%z')}. Upcoming dates:\n{date_table}\n\n"
        "When the user names a weekday ('thursday', 'next monday'), pick the "
        "nearest FUTURE date with that weekday from the list above. Build "
        "start/end times as ISO 8601 with the offset shown, e.g. "
        f"2026-09-04T16:00:00{now.strftime('%z')[:3]}:00.\n\n"
        "Tools: create_event (add), delete_event (remove), edit_event (move / "
        "rename / resize), recall (look at specific past events), find_patterns "
        "(recurring routines / habits). The user reads their upcoming calendar "
        "with /agenda."
    )


@dataclasses.dataclass
class Answer:
    text: str | None = None
    action: dict | None = None  # {"name": str, "args": dict}


async def reply(history: list[dict], user_message: str) -> Answer:
    """
    history: list of {"role": "user"|"model", "text": str}, oldest first.
    Returns an Answer (see module docstring).
    Raises ModelBusy if the service stays overloaded after retries.
    """
    contents = [
        types.Content(role=t["role"], parts=[types.Part.from_text(text=t["text"])])
        for t in history
    ]
    contents.append(
        types.Content(role="user", parts=[types.Part.from_text(text=user_message)])
    )

    config = types.GenerateContentConfig(
        system_instruction=_system_prompt(),
        tools=[
            _CREATE_EVENT_TOOL,
            _DELETE_EVENT_TOOL,
            _EDIT_EVENT_TOOL,
            _RECALL_TOOL,
            _PATTERNS_TOOL,
        ],
        # We handle tool calls manually (with a user confirmation step), so the
        # SDK must not execute anything automatically.
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = await _client.aio.models.generate_content(
                model=MODEL, contents=contents, config=config
            )
            break
        except genai_errors.APIError as e:
            if e.code == 429:
                logger.warning("Gemini rate/quota limit hit: %s", e)
                raise RateLimited from e
            if e.code in RETRY_ON and attempt < MAX_ATTEMPTS:
                wait = attempt  # 1s, 2s
                logger.warning(
                    "Gemini %s (attempt %d/%d), retrying in %ds",
                    e.code, attempt, MAX_ATTEMPTS, wait,
                )
                await asyncio.sleep(wait)
                continue
            if e.code in RETRY_ON:
                raise ModelBusy from e
            raise

    calls = response.function_calls or []
    if calls:
        call = calls[0]
        return Answer(action={"name": call.name, "args": dict(call.args or {})})

    return Answer(text=(response.text or "").strip() or "(the model returned nothing)")


async def _generate(contents, config=None) -> str:
    try:
        resp = await _client.aio.models.generate_content(
            model=MODEL, contents=contents, config=config
        )
    except genai_errors.APIError as e:
        if e.code == 429:
            raise RateLimited from e
        raise
    return (resp.text or "").strip()


async def choose_events(events: list[dict], user_request: str) -> list[int]:
    """
    Given the user's upcoming events and what they said, return the indexes of
    the events they're referring to (0-based). Empty list = no clear match.
    Handles different languages / loose wording.
    """
    lines = [f"{i}: {e['summary']} — {e['start']} [{e['source']}]" for i, e in enumerate(events)]
    prompt = (
        "Here are the user's upcoming calendar events:\n"
        + "\n".join(lines)
        + f'\n\nThe user is referring to an event. They said: "{user_request}"\n\n'
        "Which event(s) do they mean? Reply with ONLY the matching number(s), "
        "comma-separated (e.g. `2` or `0,3`). If nothing clearly matches, reply "
        "`none`. Match loosely and across languages — the user may describe an "
        "event in English that has a Hebrew title."
    )
    text = (await _generate(prompt)).lower()
    if "none" in text:
        return []
    out = []
    for chunk in text.replace(" ", "").split(","):
        try:
            idx = int(chunk)
        except ValueError:
            continue
        if 0 <= idx < len(events):
            out.append(idx)
    return out


async def plan_edit(event: dict, user_request: str) -> dict | None:
    """
    Work out the new summary/start/end for an event the user wants to change.
    Returns {"summary", "start", "end"} (ISO 8601 with offset), or None if the
    request can't be turned into a concrete change.
    """
    now, date_table = _date_table()
    prompt = (
        f"Current time: {now.strftime('%A %Y-%m-%d %H:%M')} (offset {now.strftime('%z')}).\n"
        f"Upcoming dates:\n{date_table}\n\n"
        "The user wants to change this calendar event:\n"
        f"  title: {event['summary']}\n"
        f"  start: {event['start']}\n"
        f"  end:   {event['end']}\n\n"
        f'The user said: "{user_request}"\n\n'
        "Reply with ONLY a JSON object with keys \"summary\", \"start\", \"end\" "
        "giving the event's values AFTER the change. Keep fields the user didn't "
        "mention unchanged. Use ISO 8601 with the timezone offset above. "
        "If the request doesn't describe a concrete change, reply exactly: null"
    )
    text = await _generate(prompt)
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    if text.lower() == "null":
        return None
    try:
        data = json.loads(text)
        return {
            "summary": str(data["summary"]),
            "start": str(data["start"]),
            "end": str(data["end"]),
        }
    except (ValueError, KeyError, TypeError):
        logger.warning("plan_edit: couldn't parse %r", text)
        return None


async def describe_patterns(routines: list[dict], weeks_back: int) -> str:
    """Turn habits.analyse() output into a short read-out in the briefing language."""
    if not routines:
        return ""
    lines = []
    for r in routines[:10]:
        when = "/".join(r["days"]) if r["days"] else "no fixed day"
        at = f" around {r['typical_hour']:02d}:00" if r["typical_hour"] is not None else ""
        gap = " (NOT on the calendar for the coming week)" if (
            r["missing_next_week"] and r["per_week"] >= 0.75
        ) else ""
        lines.append(
            f"- {r['title']}: {r['count']} times, ~{r['per_week']}/week, "
            f"mostly {when}{at}{gap}"
        )
    prompt = (
        f"Write a short summary in {BRIEFING_LANG} (3-6 sentences, friendly, no "
        "bullet points) of the user's routines over the last "
        f"{weeks_back} weeks, based on this data. Call out anything they usually "
        "do that isn't on the calendar for the coming week.\n\n" + "\n".join(lines)
    )
    try:
        return await _generate(prompt)
    except (RateLimited, genai_errors.APIError):
        return ""


async def answer_about_events(events: list[dict], question: str) -> str:
    """Answer a question using a list of (usually past) calendar events."""
    if not events:
        lines = "(no events in that period)"
    else:
        lines = "\n".join(
            f"  {e['start'][:16].replace('T', ' ')}  {e['summary']}  [{e['source']}]"
            for e in events
        )
    prompt = (
        "Answer the user's question using only these calendar events. Be brief "
        "and concrete (counts, dates). Reply in the user's language.\n\n"
        f"Events:\n{lines}\n\nQuestion: {question}"
    )
    return await _generate(prompt)


async def briefing_summary(events: list[dict], tomorrow_first: dict | None) -> str:
    """A short, natural morning briefing from today's events. Falls back to '' ."""
    if not events:
        lines = "(nothing scheduled)"
    else:
        lines = "\n".join(
            f"  {e['start'][11:16]}–{e['end'][11:16]} {e['summary']}"
            if not e["all_day"]
            else f"  all day: {e['summary']}"
            for e in events
        )
    extra = ""
    if tomorrow_first and not tomorrow_first["all_day"]:
        extra = f"\nTomorrow's first event: {tomorrow_first['start'][11:16]} {tomorrow_first['summary']}"

    prompt = (
        f"Write a short morning briefing in {BRIEFING_LANG} (2-4 sentences, "
        "friendly, no bullet points) based on the rest of today's schedule. "
        "Mention the shape of the day — how busy, the first thing coming up, any "
        "big gap or tight back-to-back stretch. Don't just list the events.\n\n"
        f"Today's remaining events:\n{lines}{extra}"
    )
    try:
        return await _generate(prompt)
    except (RateLimited, genai_errors.APIError):
        return ""


class ModelBusy(Exception):
    """Raised when the model stays unavailable (503) after all retries."""


class RateLimited(Exception):
    """Raised on a 429 — per-minute rate limit or daily free-tier quota."""
