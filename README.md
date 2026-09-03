# PA bot

A personal-assistant Telegram bot that (eventually) chats about my tasks and
reads/writes my Google and Apple calendars.

## Roadmap

| Phase | Goal | Status |
|-------|------|--------|
| 0 | Project skeleton, virtualenv, secrets handling | done |
| 1 | Echo bot — prove Telegram <-> this machine works | done |
| 2 | Chat brain (Gemini API free tier) + conversation memory | done |
| 3 | Read Google Calendar (`/agenda`) | done |
| 4 | Create events via chat, with a confirmation step | done |
| 5 | Delete events via chat + scheduled daily briefing | done |
| 6 | Deploy so it runs 24/7 (Google Cloud e2-micro) | done |
| 7 | Apple / iCloud calendar via CalDAV (merged reads) | done |
| 8 | Yes/No/Chat buttons; edit events; smart briefing | done |
| 9 | Look up past events ("when did I last…") | done |
| 10 | Habit detection — `/patterns`, recurring routines, missing-this-week | done |
| 11 | Add several events at once, recurring events, invite attendees | done |

## Setup

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env      # then edit .env with real values
```

## Phase 1: get a bot token

1. Open Telegram, search for **@BotFather**, start a chat.
2. Send `/newbot`. Pick a display name, then a username ending in `bot`.
3. BotFather replies with a token like `8123456789:AAH...`. Copy it.
4. Paste it into `.env` as `TELEGRAM_BOT_TOKEN=...`.

## Phase 2: get a Gemini API key

1. Go to https://aistudio.google.com and sign in with your Google account.
2. Click **Get API key** → **Create API key**.
3. Copy the key and paste it into `.env` as `GEMINI_API_KEY=...`.

Free tier: rate-limited (a few requests/minute) but $0. On the free tier Google
may use prompts to improve their products.

## Phase 3: connect Google Calendar

1. In [console.cloud.google.com](https://console.cloud.google.com): create a
   project, enable the **Google Calendar API**, configure the OAuth consent
   screen (External, add yourself as a test user).
2. **Credentials → Create OAuth client ID → Desktop app**, download the JSON.
3. Rename it to `credentials.json` in the project folder (gitignored).
4. Authorize once — opens a browser:
   ```bash
   ./.venv/bin/python gcal.py
   ```
   This writes `token.json` (gitignored) and prints your next 7 days of events.
   In the bot, `/agenda` does the same.

## Run

```bash
./.venv/bin/python bot.py
```

Then message your bot in Telegram — it replies via Gemini and remembers the
last few turns.

Say things like "add dentist Thursday 3pm", "gym every Mon/Wed/Fri 7am",
"lunch with sara@example.com Friday 1pm", "move my lunch to 2pm", or "cancel the
workout" — one event or several at once, recurring or not, with attendees. The
bot shows what it will do with ✅ Yes / ❌ No / 💬 Chat buttons and only acts after
you confirm. Finding the event you mean for edits/deletes is semantic (works
across languages).

Commands: `/agenda` (today + tomorrow; `/agenda 5` for 5 days), `/briefing`
(today's events now), `/patterns` (recurring routines from the last 8 weeks),
`/reset` (clear conversation memory). A briefing is also sent automatically
every day at `BRIEFING_TIME` (default 08:00, `BRIEFING_TZ` default
Asia/Jerusalem). `Ctrl+C` stops the bot.

There's also a persistent button bar (🗓 Agenda / 🌤 Briefing / 📊 Patterns) above
the text box — same as the commands, just tappable.

You can also just ask in chat — "when did I last see the dentist", "what are my
routines", "am I keeping up with the gym".

`/agenda` and the briefing merge Google + iCloud events (deduped). New events
created via chat go to Google. The daily briefing is written by the model from
your schedule (busy/free shape, first event, gaps), not just a list.

Note: uses the Gemini free tier (`gemini-3.5-flash-lite`). If you hit the daily
request limit, wait for the reset or switch models in `brain.py`.

**Only ever run one copy at a time** (per bot token).

## Files

- `bot.py` — Telegram wiring: receives messages, keeps per-chat history, replies.
- `brain.py` — the only file that knows about the language model. Swappable.
- `cal.py` — merges Google + iCloud behind one interface (the bot only talks to this).
- `habits.py` — deterministic recurring-routine detection from past events.
- `gcal.py` — Google Calendar auth + read/create/update/delete. Run directly to authorize.
- `icloud.py` — iCloud calendar via CalDAV. Run directly to check the connection.

## Running 24/7

See [DEPLOY.md](DEPLOY.md) — deploys to a free Google Cloud `e2-micro` VM.
