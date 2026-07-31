# Terminal Bot

Telegram bot that lets each user register their own server(s) and run SSH commands right from the chat — a live, interactive terminal. Access can be gated behind joining sponsor channel(s).

## Install

```bash
sudo bash install.sh
```
Prompts for bot token, admin IDs, and optional sponsor-channel gate, then sets up a venv and a systemd service.

## Uninstall

```bash
sudo bash uninstall.sh
```

## Service

Installed as systemd service `terminal-bot`.

```bash
systemctl start terminal-bot
systemctl stop terminal-bot
systemctl restart terminal-bot
systemctl status terminal-bot
journalctl -u terminal-bot -f
```

## Features

- **Server Manager**: add servers (host/port/username/password), stored with the password encrypted at rest (`crypto_utils.py`, key from `CRYPTO_SECRET`).
- **Live SSH terminal**: run commands with streaming output, or open a persistent interactive shell (menus/prompts work like a real terminal).
- **Quick ping**: check host reachability without SSH credentials.
- **Sponsor gate**: optionally require users to join configured channel(s) before using the bot.

## Config

Set in `.env`: `BOT_TOKEN`, `ADMIN_IDS`, `SPONSOR_CHANNELS`, `SPONSOR_CHANNEL_LINKS`, `CRYPTO_SECRET`.

## Security notes

- Server passwords are stored encrypted in `ServerManager/server_manager_settings.json` — losing `CRYPTO_SECRET` makes them unrecoverable.
- `.env` is created with `chmod 600`.
