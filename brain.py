"""
The "brain" — everything about talking to the language model lives here.

`reply(history, user_message)` returns an `Answer`:
  - Answer(text="...")                  a plain conversational reply
  - Answer(action={"name","args"})      the model wants to create a calendar
                                        event; the bot must confirm with the
                                        user before doing anything.

If we ever swap Gemini for another model, this is the only file that changes.
"""

import asyncio
import dataclasses
import datetime as dt
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


def _system_prompt() -> str:
    now = dt.datetime.now().astimezone()
    # Spell out the next two weeks of dates so the model never has to do date
    # arithmetic (flash-lite is unreliable at it).
    calendar_lines = []
    for i in range(15):
        day = now.date() + dt.timedelta(days=i)
        label = {0: " (today)", 1: " (tomorrow)"}.get(i, "")
        calendar_lines.append(f"  {day.strftime('%A %Y-%m-%d')}{label}")
    date_table = "\n".join(calendar_lines)

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
        "You can create events with the create_event tool. The user reads their "
        "calendar with /agenda (you can't). You cannot edit or delete events yet."
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
        tools=[_CREATE_EVENT_TOOL],
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


class ModelBusy(Exception):
    """Raised when the model stays unavailable (503) after all retries."""


class RateLimited(Exception):
    """Raised on a 429 — per-minute rate limit or daily free-tier quota."""
