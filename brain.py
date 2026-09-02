"""
The "brain" — everything about talking to the language model lives here.

The rest of the bot doesn't know or care which model we use. It just awaits
`reply(history, user_message)` and gets back a string. If we ever swap Gemini
for Claude or something else, this is the only file that changes.
"""

import asyncio
import logging
import os

from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai import errors as genai_errors

# Also load .env here so this module works on its own (e.g. quick tests), not
# only when imported from bot.py.
load_dotenv()

logger = logging.getLogger(__name__)

# One client, reused for every request. Reads GEMINI_API_KEY from the environment.
_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

# Free-tier friendly, fast, good enough for chat.
MODEL = "gemini-3.6-flash"

# The free tier sometimes returns 503 (busy) or 429 (rate limit). Retry a few
# times with a growing pause before giving up.
RETRY_ON = (503, 429)
MAX_ATTEMPTS = 4

SYSTEM_PROMPT = (
    "You are a personal assistant running inside a Telegram bot. "
    "The user just finished their first year of a CS degree and is building "
    "you as a learning project. "
    "Keep replies short and conversational — this is a phone chat, not an essay. "
    "You cannot see calendars or take real actions yet; that comes in a later "
    "phase. If asked to do something you can't do, say so plainly."
)


class ModelBusy(Exception):
    """Raised when the model stays unavailable after all retries."""


async def reply(history: list[dict], user_message: str) -> str:
    """
    history: list of {"role": "user"|"model", "text": str}, oldest first.
    user_message: the new message from the user.
    Returns the model's reply as plain text.
    Raises ModelBusy if the service is overloaded even after retrying.
    """
    contents = [
        types.Content(role=turn["role"], parts=[types.Part.from_text(text=turn["text"])])
        for turn in history
    ]
    contents.append(
        types.Content(role="user", parts=[types.Part.from_text(text=user_message)])
    )

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        # We don't give the model any tools/functions (yet), so turn off the
        # SDK's automatic function-calling machinery. Silences a warning.
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            # .aio = async SDK, so we don't block the bot's event loop.
            response = await _client.aio.models.generate_content(
                model=MODEL, contents=contents, config=config
            )
            return (response.text or "").strip() or "(the model returned nothing)"
        except genai_errors.APIError as e:
            if e.code in RETRY_ON and attempt < MAX_ATTEMPTS:
                wait = 2 * attempt  # 2s, 4s, 6s
                logger.warning(
                    "Gemini returned %s (attempt %d/%d), retrying in %ds",
                    e.code, attempt, MAX_ATTEMPTS, wait,
                )
                await asyncio.sleep(wait)
                continue
            if e.code in RETRY_ON:
                raise ModelBusy from e
            raise
