# Deploying PA bot to a free Google Cloud VM

Runs the bot 24/7 on a Google Cloud `e2-micro` instance (always-free tier).
The bot uses long polling, so the VM needs no open ports / domain / HTTPS.

## 1. Create the VM

[console.cloud.google.com](https://console.cloud.google.com) → same project as the
Calendar API (`PA bot`).

- **Compute Engine → VM instances → Create instance**
  (enable the Compute Engine API if prompted)
- Name: `pabot`
- **Region**: must be an always-free region — `us-west1`, `us-central1`, or
  `us-east1`. Pick one, any zone.
- **Machine type**: `e2-micro` (under "E2", shared-core)
- **Boot disk**: Edit → OS `Debian 12`, disk type **Balanced or Standard**,
  size **30 GB** (the free-tier max)
- Leave firewall unchecked (no HTTP/HTTPS needed)
- **Create**. Wait for the green check.

> Always-free covers: 1 e2-micro in those regions + 30 GB standard disk. Staying
> within that = $0. You still must have billing enabled (a card on file).

## 2. Connect

On the instances list, click **SSH** next to `pabot`. A browser terminal opens.
Everything below runs in that terminal.

## 3. Install dependencies and the code

```bash
sudo timedatectl set-timezone Asia/Jerusalem
sudo apt update && sudo apt install -y python3 python3-venv git
git clone https://github.com/omeriko89828/PA-bot.git
cd PA-bot
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

## 4. Add the three secret files

These are gitignored, so they are NOT in the clone. Create them on the VM.

**`.env`** — run `nano .env`, paste (with your real values), Ctrl+O, Enter, Ctrl+X:

```
TELEGRAM_BOT_TOKEN=...
GEMINI_API_KEY=...
```

**`credentials.json`** and **`token.json`** — copy from your Mac. Easiest: in the
browser SSH window, top-right **gear ⚙ → Upload file**, upload both from the
`PA bot` folder. They land in your home dir; move them:

```bash
mv ~/credentials.json ~/token.json ~/PA-bot/
```

## 5. Test it

```bash
./.venv/bin/python bot.py
```

Message the bot from your phone — **stop the copy on your Mac first** (one
instance per token). `Ctrl+C` when it works.

## 6. Run it as a service (auto-start, auto-restart)

```bash
sudo tee /etc/systemd/system/pabot.service > /dev/null <<EOF
[Unit]
Description=PA bot (Telegram personal assistant)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$HOME/PA-bot
ExecStart=$HOME/PA-bot/.venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now pabot
```

Check it:

```bash
systemctl status pabot          # should say "active (running)"
journalctl -u pabot -f          # live logs; Ctrl+C to stop watching
```

## Updating the bot later

```bash
cd ~/PA-bot
git pull
./.venv/bin/pip install -r requirements.txt
sudo systemctl restart pabot
```

## If you re-authorize Google on your Mac

`token.json` changes — re-upload it to the VM and `sudo systemctl restart pabot`.
