# TikZoom — Telegram Bot Hosting Platform

A platform that lets users upload Telegram bot files (Python / PHP / Node.js)
and runs them 24/7 as managed processes.  Includes a referral system, speed
tiers (1–5), force-subscribe gate, contact-share gate, admin notifications,
and a Telegram Mini App for self-service.

## Features

- 🤖 **Multi-language hosting**: `.py`, `.php`, `.js` (and archives extracted on upload).
- 🔗 **Webhook routing**: each hosted bot gets a stable HTTPS webhook URL on the platform.
- 💎 **Referrals**: every user has a unique `?start=<code>` link; +1 point per first-time referral.
- 🎚 **Speed tiers**:
  - T1 — Free (1 file)
  - T2 — 5 points (1 file)
  - T3 — 10 points (1 file)
  - T4 — 20 points (1 file)
  - T5 — VIP only (up to 20 files; admins unlimited)
- 📱 **Contact-share gate** — users must share their phone before uploading.
- 🔔 **Force-subscribe** — admins can require users to join channels.
- 🌐 **Telegram Mini App** under `/app/` for browsing your bots.
- 🎨 **Bot API 9.4 styled buttons** — the new `style` field (`success` / `danger` / `primary`)
  and `icon_custom_emoji_id` are sent on every inline keyboard.
- 🛠 **Admin tools**: change main bot token at runtime, manage VIPs/admins/bans, list all bots.

## Architecture

```
  Telegram users
        │
        ▼
   ┌──────────────┐    HTTPS (Caddy / reverse proxy)
   │   Caddy      │────────────────────────────┐
   │  :8443       │                            │
   └──────┬───────┘                            │
          │ http://127.0.0.1:8000              │
          ▼                                    │
   ┌──────────────────────────────────────────┐│
   │ FastAPI app (this project)               ││
   │  • POST /tg/<secret>  → main-bot router  ││
   │  • POST /wh/<hash>    → forward to host  ││
   │  • GET  /app/         → Mini App         ││
   │  • GET  /app/api/me   → user state       ││
   └──────────────┬─────────────────────┬─────┘│
                  │                     │      │
        ┌─────────▼────────┐   ┌────────▼──────▼──────────┐
        │ SQLite (sqlmodel)│   │  BotRunner (subprocesses)│
        └──────────────────┘   │   • python bot1.py       │
                               │   • node bot2.js         │
                               │   • php  bot3.php        │
                               └──────────────────────────┘
```

## Local development

```bash
git clone <this repo>
cd tikzoom-bot-host
cp .env.example .env
# fill in BOT_TOKEN, ADMIN_IDS, FERNET_KEY (run the snippet inside .env.example)
./scripts/run_dev.sh
```

The dev runner enables long-polling mode so you don't need a public HTTPS endpoint.

## Production deployment (Windows Server VPS)

A turnkey installer is in `deploy/windows/install.ps1`. From an elevated
PowerShell on the VPS, with the project tree at `C:\TikZoom\tikzoom-bot-host`:

```powershell
PowerShell -ExecutionPolicy Bypass -File C:\TikZoom\tikzoom-bot-host\deploy\windows\install.ps1 `
    -BotToken "8633510294:AAF47_jGJyVGdfNdxljD76CdOHl_swbevN4" `
    -AdminIds "6472365461" `
    -PublicHost "your.public.host.or.ip"
```

What it does:

1. Installs Chocolatey + Python 3.12 + Node.js LTS + PHP + NSSM + Caddy + OpenSSH.
2. Creates `.venv`, installs dependencies, generates `FERNET_KEY` and `WEBHOOK_SECRET`.
3. Writes `Caddyfile` that terminates HTTPS (`tls internal` self-signed) on **8443**
   and reverse-proxies to the FastAPI app on **8000**.
4. Registers two Windows services with NSSM that start at boot:
   - `TikZoomApp`   → `uvicorn app.main:app`
   - `TikZoomCaddy` → `caddy run`
5. Opens firewall ports 8443 and 8000.
6. Calls Telegram's `setWebhook` to point your main bot at this VPS.

After install, manage with:

```powershell
nssm restart TikZoomApp
nssm restart TikZoomCaddy
Get-Content C:\TikZoom\data\logs\TikZoomApp.log -Wait
```

## Hosted-bot conventions

When the platform launches a user-uploaded bot, it sets:

| env var       | meaning                                             |
| ------------- | --------------------------------------------------- |
| `BOT_TOKEN`   | the bot's Telegram token (already extracted)         |
| `PORT`        | local TCP port to bind on (loopback)                |
| `WEBHOOK_URL` | full public URL Telegram delivers to                |
| `WEBHOOK_PATH`| path component (e.g. `/webhook`)                    |
| `PLATFORM`    | `tikzoom`                                           |

Reference templates live under `runtimes/` (`python_template/bot_template.py`,
`php_template/bot_template.php`, `node_template/bot_template.js`).

## Security notes

This platform runs **arbitrary user-uploaded code** in the same OS user as the
platform. Treat that as untrusted. Recommended hardening before opening to the
public:

- Run each hosted bot under a separate Windows / POSIX user with minimal rights.
- Restrict outbound network with Windows Firewall rules per service.
- Rotate `BOT_TOKEN` and `FERNET_KEY` if the host is ever compromised.

## License

Private — internal use for TikZoom.
