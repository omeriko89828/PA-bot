"""
PA bot — Phase 3: read Google Calendar.

The bot chats via Gemini (with memory) and can now list upcoming calendar
events with the /agenda command. Writing events comes in Phase 4.

Run it with:   ./.venv/bin/python bot.py
Stop it with:  Ctrl+C
"""

import asyncio
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

# Log to the console so we can see what the bot is doing.
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
# The telegram library is chatty at DEBUG; keep it at WARNING.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# How many past messages (user + model combined) to keep per chat.
MAX_HISTORY = 20


# --- Handlers -------------------------------------------------------------
# Each handler is an async function that receives:
#   update  -> what just happened (a message, a command, ...)
#   context -> tools for responding, plus per-chat storage in context.chat_data


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs when the user sends /start."""
    context.chat_data["history"] = []
    await update.message.reply_text(
        "Hi! I'm your assistant. Ask me anything. "
        "(I can't see your calendar yet — that's the next phase.)"
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs when the user sends /reset — forget the conversation so far."""
    context.chat_data["history"] = []
    await update.message.reply_text("Okay, I've forgotten our conversation.")


async def agenda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs when the user sends /agenda — list events in the next 7 days."""
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        # The Google client is synchronous; run it in a thread so it doesn't
        # freeze the bot's event loop.
        events = await asyncio.to_thread(gcal.upcoming_events, 25, 7)
    except Exception:
        logger.exception("calendar read failed")
        await update.message.reply_text(
            "Couldn't read your calendar. Is token.json set up? "
            "(Run `./.venv/bin/python gcal.py` once to authorize.)"
        )
        return
    await update.message.reply_text(gcal.format_events(events))


async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs for any plain text message that isn't a command."""
    user_text = update.message.text
    logger.info("Message from %s: %s", update.effective_user.first_name, user_text)

    history = context.chat_data.setdefault("history", [])

    # Show "typing..." in Telegram while we wait for the model.
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

    try:
        answer = await brain.reply(history, user_text)
    except brain.ModelBusy:
        logger.warning("Gemini stayed busy after retries")
        await update.message.reply_text(
            "The model is overloaded right now (free tier). Give it a minute and resend."
        )
        return
    except Exception:
        logger.exception("brain.reply failed")
        await update.message.reply_text(
            "Something went wrong talking to the model. Try again in a moment."
        )
        return

    # Remember this exchange for next time, then trim to the last MAX_HISTORY.
    history.append({"role": "user", "text": user_text})
    history.append({"role": "model", "text": answer})
    del history[:-MAX_HISTORY]

    await update.message.reply_text(answer)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Central place to handle anything that goes wrong in a handler."""
    if isinstance(context.error, Conflict):
        # This means another copy of the bot is polling Telegram at the same
        # time. Only ONE instance can run per token. Stop the other one.
        logger.error(
            "Conflict: another instance of this bot is already running. "
            "Stop it (Ctrl+C in the other terminal), then restart just one."
        )
        return
    logger.error("Unhandled error while processing update", exc_info=context.error)


# --- Wiring -------------------------------------------------------------------


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Register handlers. Order matters: more specific ones first.
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("agenda", agenda))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
    app.add_error_handler(on_error)

    logger.info("Bot starting. Press Ctrl+C to stop.")
    # run_polling() = keep asking Telegram "any new messages?" forever.
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
