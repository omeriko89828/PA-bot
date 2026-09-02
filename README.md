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
| 4 | Write to Google Calendar (create/update events, with confirm step) | **next** |
| 5 | Apple / iCloud calendar via CalDAV | todo |
| 6 | Deploy so it runs 24/7 | todo |

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

Commands: `/agenda` (next 7 days), `/reset` (clear conversation memory).
`Ctrl+C` stops the bot.

**Only ever run one copy at a time** (per bot token).

## Files

- `bot.py` — Telegram wiring: receives messages, keeps per-chat history, replies.
- `brain.py` — the only file that knows about the language model. Swappable.
- `gcal.py` — Google Calendar auth + reading events. Run directly to authorize.
