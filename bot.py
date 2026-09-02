"""
PA bot — Phase 4: create calendar events (with a confirmation step).

The bot chats via Gemini (with memory), lists events with /agenda, and can
create events: you say "add dentist Tuesday 3pm", it shows you exactly what it
plans to add, and only writes it after you reply "yes".

Run it with:   ./.venv/bin/python bot.py
Stop it with:  Ctrl+C
"""

import asyncio
import datetime as dt
import logging
import os

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.error import Conflict
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Load .env into the environment BEFORE importing brain, which reads the key
# at import time.
load_dotenv()
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

import brain  # noqa: E402  (must come after load_dotenv above)
import gcal  # noqa: E402

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# How many past messages (user + model combined) to keep per chat.
MAX_HISTORY = 20

_YES = {"yes", "y", "yeah", "yep", "ok", "okay", "sure", "do it", "confirm", "כן"}
_NO = {"no", "n", "nope", "cancel", "stop", "don't", "dont", "לא", "ביטול"}


# --- Handlers ---------------------------------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.chat_data["history"] = []
    context.chat_data.pop("pending_event", None)
    await update.message.reply_text(
        "Hi! I'm your assistant. Ask me things, check /agenda, or tell me to "
        "add something to your calendar."
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.chat_data["history"] = []
    context.chat_data.pop("pending_event", None)
    await update.message.reply_text("Okay, I've forgotten our conversation.")


async def agenda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List events in the next 7 days."""
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        events = await asyncio.to_thread(gcal.upcoming_events, 25, 7)
    except Exception:
        logger.exception("calendar read failed")
        await update.message.reply_text(
            "Couldn't read your calendar. Run `./.venv/bin/python gcal.py` once "
            "to authorize."
        )
        return
    await update.message.reply_text(gcal.format_events(events))


async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Any plain text message that isn't a command."""
    user_text = update.message.text
    logger.info("Message from %s: %s", update.effective_user.first_name, user_text)

    history = context.chat_data.setdefault("history", [])

    # If we're waiting for a yes/no on a proposed event, handle that first.
    if context.chat_data.get("pending_event"):
        await _handle_confirmation(update, context, history, user_text)
        return

    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

    try:
        answer = await brain.reply(history, user_text)
    except brain.RateLimited:
        await update.message.reply_text(
            "Hit the Gemini free-tier limit. Wait a minute and resend — if it "
            "keeps happening, the daily quota is used up (resets ~10:00 Israel time)."
        )
        return
    except brain.ModelBusy:
        await update.message.reply_text(
            "The model is briefly overloaded. Give it a few seconds and resend."
        )
        return
    except Exception:
        logger.exception("brain.reply failed")
        await update.message.reply_text(
            "Something went wrong talking to the model. Try again in a moment."
        )
        return

    if answer.action and answer.action["name"] == "create_event":
        await _propose_event(update, context, history, user_text, answer.action["args"])
        return

    # Plain conversational reply.
    _remember(history, user_text, answer.text)
    await update.message.reply_text(answer.text)


async def _propose_event(update, context, history, user_text, args) -> None:
    """Validate the model's proposed event and ask the user to confirm it."""
    try:
        start = dt.datetime.fromisoformat(args["start"])
        end = dt.datetime.fromisoformat(args["end"])
        summary = args["summary"].strip()
        assert summary
    except (KeyError, ValueError, AssertionError):
        logger.warning("bad create_event args from model: %s", args)
        msg = "I couldn't work out the time for that. Can you say it another way?"
        _remember(history, user_text, msg)
        await update.message.reply_text(msg)
        return

    context.chat_data["pending_event"] = {
        "summary": summary,
        "start": start.isoformat(),
        "end": end.isoformat(),
    }
    preview = (
        f"📅 Create this event?\n\n"
        f"{summary}\n"
        f"{gcal.pretty(start.isoformat())} – {end.strftime('%H:%M')}\n\n"
        f"Reply 'yes' to add it, 'no' to cancel."
    )
    _remember(history, user_text, preview)
    await update.message.reply_text(preview)


async def _handle_confirmation(update, context, history, user_text) -> None:
    answer = user_text.strip().lower()
    pending = context.chat_data["pending_event"]

    if answer in _NO:
        context.chat_data.pop("pending_event")
        msg = "Cancelled — nothing added."
        _remember(history, user_text, msg)
        await update.message.reply_text(msg)
        return

    if answer not in _YES:
        await update.message.reply_text("Please reply 'yes' or 'no'.")
        return

    context.chat_data.pop("pending_event")
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        event = await asyncio.to_thread(
            gcal.create_event, pending["summary"], pending["start"], pending["end"]
        )
    except Exception:
        logger.exception("create_event failed")
        msg = "Couldn't create the event — something went wrong with Google Calendar."
        _remember(history, user_text, msg)
        await update.message.reply_text(msg)
        return

    msg = f"Added ✅ {event.get('summary')} — {gcal.pretty(pending['start'])}"
    _remember(history, user_text, msg)
    await update.message.reply_text(msg)


def _remember(history: list, user_text: str, model_text: str) -> None:
    history.append({"role": "user", "text": user_text})
    history.append({"role": "model", "text": model_text})
    del history[:-MAX_HISTORY]


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, Conflict):
        logger.error(
            "Conflict: another instance of this bot is already running. "
            "Stop it (Ctrl+C in the other terminal), then restart just one."
        )
        return
    logger.error("Unhandled error while processing update", exc_info=context.error)


# --- Wiring ---------------------------------------------------------------


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("agenda", agenda))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
    app.add_error_handler(on_error)

    logger.info("Bot starting. Press Ctrl+C to stop.")
    # drop_pending_updates: ignore messages that arrived while the bot was down,
    # so a restart doesn't reply to a backlog.
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
