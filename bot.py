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
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
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
import habits  # noqa: E402

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

# Persistent button bar above the text box. Tapping a button sends its label as
# a normal message, which chat() routes to the matching handler.
_BTN_AGENDA = "🗓 Agenda"
_BTN_BRIEFING = "🌤 Briefing"
_BTN_PATTERNS = "📊 Patterns"
_MENU_KB = ReplyKeyboardMarkup(
    [[_BTN_AGENDA, _BTN_BRIEFING, _BTN_PATTERNS]],
    resize_keyboard=True,
    is_persistent=True,
)

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
        "Hi! Use the buttons below or just talk to me — add / move / delete "
        f"calendar events, ask about your week or your habits. Daily briefing at "
        f"{BRIEFING_TIME}.",
        reply_markup=_MENU_KB,
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.chat_data["history"] = []
    context.chat_data.pop("pending_action", None)
    await update.message.reply_text(
        "Okay, I've forgotten our conversation.", reply_markup=_MENU_KB
    )


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


PATTERN_WEEKS = 8


async def _patterns_report() -> str:
    past = await cal.recall(PATTERN_WEEKS * 7)
    upcoming = await cal.upcoming(7)
    routines = habits.analyse(past, upcoming)
    if not routines:
        return f"Not enough history yet to spot patterns (looked back {PATTERN_WEEKS} weeks)."
    prose = ""
    try:
        prose = await brain.describe_patterns(routines, PATTERN_WEEKS)
    except Exception:
        logger.exception("describe_patterns failed")
    return prose or habits.format_report(routines, PATTERN_WEEKS)


async def patterns(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/patterns — recurring routines from the last PATTERN_WEEKS weeks."""
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        await update.message.reply_text(await _patterns_report())
    except Exception:
        logger.exception("patterns failed")
        await update.message.reply_text("Couldn't work out your patterns right now.")


# --- the scheduled job ------------------------------------------------------


async def _briefing_text() -> str:
    try:
        two_days = await cal.upcoming(2)
    except Exception:
        logger.exception("briefing calendar read failed")
        return "☀️ בוקר טוב! (לא הצלחתי להגיע ליומן כרגע.)"

    now = dt.datetime.now().astimezone()
    today_str = now.date().isoformat()
    tmr_str = (now.date() + dt.timedelta(days=1)).isoformat()
    today_events = [e for e in two_days if e["start"][:10] == today_str]
    tmr_events = [e for e in two_days if e["start"][:10] == tmr_str]

    try:
        summary = await brain.briefing_summary(
            today_events, tmr_events[0] if tmr_events else None
        )
    except Exception:
        logger.exception("briefing_summary failed")
        summary = ""

    if summary:
        return "☀️ " + summary

    # Fallback: plain list.
    if not today_events:
        return "☀️ בוקר טוב! אין עוד אירועים ביומן להיום."
    return "☀️ בוקר טוב! מה שנשאר להיום:\n\n" + cal.format_events(today_events)


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

    # Persistent menu buttons arrive as ordinary messages — route them.
    menu = {_BTN_AGENDA: agenda, _BTN_BRIEFING: briefing, _BTN_PATTERNS: patterns}
    if user_text in menu and not context.chat_data.get("pending_action"):
        await menu[user_text](update, context)
        return

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
        if name == "create_events":
            await _propose_creates(update, context, history, user_text, args)
            return
        if name == "delete_event":
            await _propose_change(update, context, history, user_text, "delete")
            return
        if name == "edit_event":
            await _propose_change(update, context, history, user_text, "edit")
            return
        if name == "recall":
            await _handle_recall(update, context, history, user_text, args)
            return
        if name == "find_patterns":
            await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
            try:
                msg = await _patterns_report()
            except Exception:
                logger.exception("find_patterns failed")
                msg = "Couldn't work out your patterns right now."
            _remember(history, user_text, msg)
            await update.message.reply_text(msg)
            return

    _remember(history, user_text, answer.text)
    await update.message.reply_text(answer.text)


async def _handle_recall(update, context, history, user_text, args) -> None:
    """Answer a question about past events. Read-only, no confirmation."""
    try:
        days_back = int(args.get("days_back", 30))
        days_forward = int(args.get("days_forward", 0))
    except (TypeError, ValueError):
        days_back, days_forward = 30, 0
    days_back = max(1, min(days_back, 366))

    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        events = await cal.recall(days_back, days_forward)
        msg = await brain.answer_about_events(events, user_text)
    except brain.RateLimited:
        await update.message.reply_text("Hit the Gemini free-tier limit — try again in a minute.")
        return
    except Exception:
        logger.exception("recall failed")
        await update.message.reply_text("Couldn't look that up in your calendar.")
        return

    _remember(history, user_text, msg)
    await update.message.reply_text(msg)


_LOCAL_TZ = dt.datetime.now().astimezone().tzinfo


def _with_offset(iso: str) -> str:
    """The model sometimes drops the timezone offset; add the local one back."""
    d = dt.datetime.fromisoformat(iso)
    if d.tzinfo is None:
        d = d.replace(tzinfo=_LOCAL_TZ)
    return d.isoformat()


def _rrule_english(rrule: str) -> str:
    """A rough human phrasing of an RRULE for the preview."""
    parts = dict(
        p.split("=", 1) for p in rrule.upper().removeprefix("RRULE:").split(";") if "=" in p
    )
    freq = parts.get("FREQ", "").lower()
    interval = parts.get("INTERVAL")
    base = {"daily": "every day", "weekly": "weekly", "monthly": "monthly", "yearly": "yearly"}.get(freq, freq)
    if interval and interval != "1":
        base = f"every {interval} {freq[:-2] if freq.endswith('LY') else freq}s".lower()
    days = parts.get("BYDAY")
    if days:
        base += f" on {days.replace(',', '/')}"
    if parts.get("COUNT"):
        base += f", {parts['COUNT']} times"
    if parts.get("UNTIL"):
        base += f", until {parts['UNTIL'][:8]}"
    return base


async def _propose_creates(update, context, history, user_text, args) -> None:
    raw = args.get("events") or []
    events = []
    for item in raw:
        try:
            summary = str(item["summary"]).strip()
            assert summary
            start = _with_offset(item["start"])
            if item.get("end"):
                end = _with_offset(item["end"])
            else:  # model sometimes omits it — default to +1h
                end = (dt.datetime.fromisoformat(start) + dt.timedelta(hours=1)).isoformat()
        except (KeyError, ValueError, AssertionError, TypeError):
            logger.warning("bad event in create_events: %s", item)
            continue
        events.append(
            {
                "summary": summary,
                "start": start,
                "end": end,
                "recurrence": (item.get("recurrence") or "").strip() or None,
                "attendees": [a for a in (item.get("attendees") or []) if "@" in str(a)],
            }
        )

    if not events:
        msg = "I couldn't work out the details for that. Can you say it another way?"
        _remember(history, user_text, msg)
        await update.message.reply_text(msg)
        return

    context.chat_data["pending_action"] = {"kind": "create_multi", "events": events}

    def _one_line(e, i=None):
        head = f"{i}. " if i else ""
        line = f"{head}{e['summary']} — {cal.pretty(e['start'])}"
        if not e["recurrence"]:
            line += f"–{dt.datetime.fromisoformat(e['end']).strftime('%H:%M')}"
        extras = []
        if e["recurrence"]:
            extras.append(_rrule_english(e["recurrence"]))
        if e["attendees"]:
            extras.append("invite " + ", ".join(e["attendees"]))
        if extras:
            line += f"  ({'; '.join(extras)})"
        return line

    if len(events) == 1:
        preview = "📅 Create this event?\n\n" + _one_line(events[0])
    else:
        preview = f"📅 Create these {len(events)} events?\n\n" + "\n".join(
            _one_line(e, i + 1) for i, e in enumerate(events)
        )
    _remember(history, user_text, preview)
    await update.message.reply_text(preview, reply_markup=_CONFIRM_KB)


async def _propose_change(update, context, history, user_text, kind) -> None:
    """kind is 'delete' or 'edit'. Find the event, then set up a confirmation."""
    try:
        events = await cal.upcoming(60)
    except Exception:
        logger.exception("calendar read failed")
        await update.message.reply_text("Couldn't read your calendar just now.")
        return

    if not events:
        msg = "You have no upcoming events."
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
        msg = f"That matches {len(matches)} events:\n\n{listing}\n\nWhich one? Add the day or time."
        _remember(history, user_text, msg)
        await update.message.reply_text(msg)
        return

    event = matches[0]

    if kind == "delete":
        context.chat_data["pending_action"] = {
            "kind": "delete",
            "event": event,
            "label": cal.describe(event),
        }
        preview = f"🗑 Delete this event?\n\n{cal.describe(event)}"
        _remember(history, user_text, preview)
        await update.message.reply_text(preview, reply_markup=_CONFIRM_KB)
        return

    # kind == "edit"
    try:
        plan = await brain.plan_edit(event, user_text)
    except brain.RateLimited:
        await update.message.reply_text("Hit the Gemini free-tier limit — try again in a minute.")
        return
    if plan is None:
        msg = "I couldn't work out the change. Try being specific, e.g. 'move it to Friday 4pm'."
        _remember(history, user_text, msg)
        await update.message.reply_text(msg)
        return

    try:
        new_start = dt.datetime.fromisoformat(plan["start"])
        new_end = dt.datetime.fromisoformat(plan["end"])
    except ValueError:
        logger.warning("plan_edit bad times: %s", plan)
        await update.message.reply_text("I couldn't work out the new time. Say it another way?")
        return

    after = {
        "source": event["source"],
        "summary": plan["summary"],
        "start": plan["start"],
        "end": plan["end"],
        "all_day": False,
    }
    context.chat_data["pending_action"] = {
        "kind": "edit",
        "event": event,
        "summary": plan["summary"],
        "start": new_start.isoformat(),
        "end": new_end.isoformat(),
        "label": cal.describe(after),
    }
    preview = f"✏️ Make this change?\n\nfrom:  {cal.describe(event)}\nto:    {cal.describe(after)}"
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
        if pending["kind"] == "create_multi":
            done, failed = [], 0
            for e in pending["events"]:
                try:
                    ev = await cal.create(
                        e["summary"], e["start"], e["end"], e["recurrence"], e["attendees"]
                    )
                    done.append(ev)
                except Exception:
                    logger.exception("create failed for %s", e["summary"])
                    failed += 1
            if done and not failed:
                msg = "Added ✅\n" + "\n".join(cal.describe(ev) for ev in done)
            elif done:
                msg = f"Added {len(done)}, {failed} failed:\n" + "\n".join(
                    cal.describe(ev) for ev in done
                )
            else:
                msg = "Couldn't create any of them — something went wrong."
        elif pending["kind"] == "edit":
            event = await cal.update(
                pending["event"], pending["summary"], pending["start"], pending["end"]
            )
            msg = f"Updated ✅ {cal.describe(event)}"
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


async def _post_init(app: Application) -> None:
    """Runs once on startup — set the command menu shown when you type '/'."""
    await app.bot.set_my_commands(
        [
            ("agenda", "Today + tomorrow (/agenda 5 for more)"),
            ("briefing", "Today's briefing now"),
            ("patterns", "Your recurring routines"),
            ("reset", "Forget the conversation"),
        ]
    )


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(_post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("agenda", agenda))
    app.add_handler(CommandHandler("briefing", briefing))
    app.add_handler(CommandHandler("patterns", patterns))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^confirm:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
    app.add_error_handler(on_error)

    app.job_queue.run_daily(briefing_job, time=_briefing_time(), name="daily_briefing")
    logger.info("Daily briefing scheduled for %s %s", BRIEFING_TIME, BRIEFING_TZ)

    logger.info("Bot starting. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
