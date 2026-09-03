"""
PA bot — Telegram personal assistant.

Chats via Gemini (with memory), merges Google + iCloud calendars (/agenda),
creates and deletes events behind a Yes / No / Chat button confirmation, and
sends a daily briefing every morning.

Run it with:   ./.venv/bin/python bot.py
Stop it with:  Ctrl+C
"""

import datetime as dt
import logging
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import Conflict
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

import brain  # noqa: E402  (must come after load_dotenv above)
import cal  # noqa: E402  (Google + iCloud merged behind one interface)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)
logger = logging.getLogger(__name__)

MAX_HISTORY = 20
CHAT_ID_FILE = Path("chat_id.txt")

BRIEFING_TIME = os.environ.get("BRIEFING_TIME", "08:00")   # HH:MM, local
BRIEFING_TZ = os.environ.get("BRIEFING_TZ", "Asia/Jerusalem")

_YES = {"yes", "y", "yeah", "yep", "ok", "okay", "sure", "do it", "confirm", "כן"}
_NO = {"no", "n", "nope", "cancel", "stop", "don't", "dont", "keep it", "לא", "ביטול"}

# Buttons shown under every confirmation prompt.
_CONFIRM_KB = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("✅ Yes", callback_data="confirm:yes"),
            InlineKeyboardButton("❌ No", callback_data="confirm:no"),
            InlineKeyboardButton("💬 Chat", callback_data="confirm:chat"),
        ]
    ]
)


# --- chat id persistence --------------------------------------------------
# The daily briefing job needs to know which chat to send to. We save the id
# whenever the owner messages the bot.


def _save_chat_id(chat_id: int) -> None:
    if not CHAT_ID_FILE.exists() or CHAT_ID_FILE.read_text().strip() != str(chat_id):
        CHAT_ID_FILE.write_text(str(chat_id))


def _load_chat_id() -> int | None:
    try:
        return int(CHAT_ID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


# --- command handlers ---------------------------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _save_chat_id(update.effective_chat.id)
    context.chat_data["history"] = []
    context.chat_data.pop("pending_action", None)
    await update.message.reply_text(
        "Hi! Ask me things, check /agenda, tell me to add or delete calendar "
        f"events, and I'll send a briefing every day at {BRIEFING_TIME}."
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.chat_data["history"] = []
    context.chat_data.pop("pending_action", None)
    await update.message.reply_text("Okay, I've forgotten our conversation.")


async def agenda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    List upcoming events. Default: today + tomorrow.
    "/agenda 5" -> next 5 days.
    """
    days = 2
    if context.args:
        try:
            days = max(1, min(int(context.args[0]), 30))
        except ValueError:
            pass
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        events = await cal.upcoming(days)
    except Exception:
        logger.exception("calendar read failed")
        await update.message.reply_text("Couldn't read your calendar right now.")
        return
    await update.message.reply_text(cal.format_events(events))


async def briefing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send today's briefing on demand."""
    _save_chat_id(update.effective_chat.id)
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    await update.message.reply_text(await _briefing_text())


# --- the scheduled job ------------------------------------------------------


async def _briefing_text() -> str:
    try:
        events = await cal.today()
    except Exception:
        logger.exception("briefing calendar read failed")
        return "☀️ Good morning! (Couldn't reach your calendar right now.)"
    if not events:
        return "☀️ Good morning! Nothing on the calendar for the rest of today."
    return "☀️ Good morning! Here's the rest of your day:\n\n" + cal.format_events(events)


async def briefing_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = _load_chat_id()
    if chat_id is None:
        logger.warning("Daily briefing: no chat id saved yet, skipping.")
        return
    await context.bot.send_message(chat_id, await _briefing_text())


# --- conversation ---------------------------------------------------------


async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Any plain text message that isn't a command."""
    user_text = update.message.text
    _save_chat_id(update.effective_chat.id)
    logger.info("Message from %s: %s", update.effective_user.first_name, user_text)

    history = context.chat_data.setdefault("history", [])

    if context.chat_data.get("pending_action"):
        await _handle_pending(update, context, history, user_text)
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

    if answer.action:
        name = answer.action["name"]
        args = answer.action["args"]
        if name == "create_event":
            await _propose_create(update, context, history, user_text, args)
            return
        if name == "delete_event":
            await _propose_delete(update, context, history, user_text, args)
            return

    _remember(history, user_text, answer.text)
    await update.message.reply_text(answer.text)


async def _propose_create(update, context, history, user_text, args) -> None:
    try:
        start = dt.datetime.fromisoformat(args["start"])
        end = dt.datetime.fromisoformat(args["end"])
        summary = args["summary"].strip()
        assert summary
    except (KeyError, ValueError, AssertionError):
        logger.warning("bad create_event args: %s", args)
        msg = "I couldn't work out the time for that. Can you say it another way?"
        _remember(history, user_text, msg)
        await update.message.reply_text(msg)
        return

    context.chat_data["pending_action"] = {
        "kind": "create",
        "summary": summary,
        "start": start.isoformat(),
        "end": end.isoformat(),
    }
    preview = (
        f"📅 Create this event?\n\n{summary}\n"
        f"{cal.pretty(start.isoformat())} – {end.strftime('%H:%M')}"
    )
    _remember(history, user_text, preview)
    await update.message.reply_text(preview, reply_markup=_CONFIRM_KB)


async def _propose_delete(update, context, history, user_text, args) -> None:
    try:
        events = await cal.upcoming(60)
    except Exception:
        logger.exception("calendar read failed")
        await update.message.reply_text("Couldn't read your calendar just now.")
        return

    if not events:
        msg = "You have no upcoming events to delete."
        _remember(history, user_text, msg)
        await update.message.reply_text(msg)
        return

    try:
        idxs = await brain.choose_events(events, user_text)
    except brain.RateLimited:
        await update.message.reply_text("Hit the Gemini free-tier limit — try again in a minute.")
        return
    except Exception:
        logger.exception("choose_events failed")
        await update.message.reply_text("Couldn't work out which event you meant.")
        return

    matches = [events[i] for i in idxs]

    if not matches:
        msg = "I couldn't find an event matching that. Try naming the day or time."
        _remember(history, user_text, msg)
        await update.message.reply_text(msg)
        return

    if len(matches) > 1:
        listing = "\n".join(f"• {cal.describe(e)}" for e in matches[:8])
        msg = (
            f"That matches {len(matches)} events:\n\n{listing}\n\n"
            f"Which one? Add the day or time."
        )
        _remember(history, user_text, msg)
        await update.message.reply_text(msg)
        return

    event = matches[0]
    context.chat_data["pending_action"] = {
        "kind": "delete",
        "event": event,
        "label": cal.describe(event),
    }
    preview = f"🗑 Delete this event?\n\n{cal.describe(event)}"
    _remember(history, user_text, preview)
    await update.message.reply_text(preview, reply_markup=_CONFIRM_KB)


async def _handle_pending(update, context, history, user_text) -> None:
    """Typed reply to a confirmation prompt (the buttons are the main path)."""
    answer = user_text.strip().lower()

    if answer in _NO:
        context.chat_data.pop("pending_action", None)
        msg = "Cancelled — nothing changed."
        _remember(history, user_text, msg)
        await update.message.reply_text(msg)
        return

    if answer not in _YES:
        await update.message.reply_text("Tap a button above, or type 'yes' / 'no'.")
        return

    await _run_pending(update, context, history, user_text)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A tap on one of the confirmation buttons."""
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]  # yes | no | chat
    history = context.chat_data.setdefault("history", [])

    # Take the buttons off the prompt so they can't be tapped again.
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    if not context.chat_data.get("pending_action"):
        await query.message.reply_text("That's no longer waiting for a decision.")
        return

    if choice == "no":
        context.chat_data.pop("pending_action", None)
        msg = "Cancelled — nothing changed."
        _remember(history, "(tapped No)", msg)
        await query.message.reply_text(msg)
        return

    if choice == "chat":
        context.chat_data.pop("pending_action", None)
        msg = "Okay, dropped that. What's up?"
        _remember(history, "(tapped Chat)", msg)
        await query.message.reply_text(msg)
        return

    await _run_pending(update, context, history, "(tapped Yes)")


async def _run_pending(update, context, history, user_text) -> None:
    """Execute the pending create/delete. Works from a typed 'yes' or a button."""
    pending = context.chat_data.pop("pending_action", None)
    if pending is None:
        return
    reply = update.effective_message.reply_text
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

    try:
        if pending["kind"] == "create":
            event = await cal.create(
                pending["summary"], pending["start"], pending["end"]
            )
            msg = f"Added ✅ {cal.describe(event)}"
        else:  # delete
            await cal.delete(pending["event"])
            msg = f"Deleted ✅ {pending['label']}"
    except Exception:
        logger.exception("%s failed", pending["kind"])
        msg = "Couldn't do that — something went wrong with the calendar."

    _remember(history, user_text, msg)
    await reply(msg)


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


# --- wiring -------------------------------------------------------------------


def _briefing_time() -> dt.time:
    hour, minute = (int(x) for x in BRIEFING_TIME.split(":"))
    return dt.time(hour, minute, tzinfo=ZoneInfo(BRIEFING_TZ))


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("agenda", agenda))
    app.add_handler(CommandHandler("briefing", briefing))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^confirm:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
    app.add_error_handler(on_error)

    app.job_queue.run_daily(briefing_job, time=_briefing_time(), name="daily_briefing")
    logger.info("Daily briefing scheduled for %s %s", BRIEFING_TIME, BRIEFING_TZ)

    logger.info("Bot starting. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
