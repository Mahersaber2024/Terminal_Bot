# Terminal Bot

Telegram bot that lets each user register their own server(s) and run SSH commands right from the chat — a live, interactive terminal. Access can be gated behind joining sponsor channel(s).

## Install

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Mahersaber2024/Terminal_Bot/main/install.sh)
```

> Run this as the `root` user (the default on most fresh VPS instances). If you're logged in as a non-root user, prefix it with `sudo`:
> ```bash
> sudo bash <(curl -fsSL https://raw.githubusercontent.com/Mahersaber2024/Terminal_Bot/main/install.sh)
> ```

This opens an interactive menu:

```
======================================
 🚀 Terminal Bot - Installer
======================================
1) Full installation
2) Update bot
3) Restart service
4) View logs
5) Service status
6) Complete uninstall
0) Exit
======================================
```
```
install path (default: /opt/terminal-bot
```
Choose **1) Full installation** the first time. It clones the project itself, asks for the install path (default `/opt/terminal-bot`), prompts for bot token, admin IDs, and optional sponsor-channel gate, then sets up a venv and a systemd service.

Run the same one-liner again any time to update, restart, check status, view logs, or uninstall — just pick the matching menu option.

You can still clone the repo and run it locally instead:

```bash
git clone https://github.com/Mahersaber2024/Terminal_Bot.git
cd Terminal_Bot
sudo bash install.sh
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

(Or use the installer's own menu — options 3–6 wrap these same actions.)

## Features

- **Server Manager**: add servers (host/port/username/password), stored with the password encrypted at rest (`crypto_utils.py`, key from `CRYPTO_SECRET`).
- **Live SSH terminal**: run commands with streaming output, or open a persistent interactive shell (menus/prompts work like a real terminal).
- **Quick ping**: check host reachability without SSH credentials.
- **Sponsor gate**: optionally require users to join configured channel(s) before using the bot.

## Config

Set in `.env`: `BOT_TOKEN`, `ADMIN_IDS`, `SPONSOR_CHANNELS`, `SPONSOR_CHANNEL_LINKS`, `CRYPTO_SECRET`.

## Backup & Restore

One-click scripts to move the whole bot (database, config, and encrypted
server credentials) to a new server.

**On the current server**, back everything up into a single archive:

```bash
sudo bash full_backup.sh /opt/terminal-bot /opt/backups
```

This dumps the PostgreSQL database and copies `.env`, `bot_settings.json`,
and the `ServerManager/` local data (`server_manager_settings.json`,
`server_manager_automation.json`, `known_hosts.json`) into one
`terminalbot_backup_TIMESTAMP.tar.gz`.

> ⚠️ The archive contains the bot token, DB password, and `CRYPTO_SECRET` —
> transfer it only over a secure channel (e.g. `scp`), and keep it somewhere safe.

**On the new server**, install the bot first (see [Install](#install)),
then restore:

```bash
scp terminalbot_backup_*.tar.gz root@NEW_SERVER_IP:/root/
sudo bash restore_backup.sh terminalbot_backup_*.tar.gz /opt/terminal-bot
```

This restores `.env`, `bot_settings.json`, and `ServerManager/` data,
recreates the database/role if needed, restores the dump, and restarts the
`terminal-bot` service.

Because `CRYPTO_SECRET` travels with the backup inside `.env`, previously
saved SSH server passwords/keys decrypt correctly on the new server too —
just don't restore an old backup's data files against a *different*,
already-configured `.env`, or decryption will fail.

## Security notes

- Server passwords are stored encrypted in `ServerManager/server_manager_settings.json` — losing `CRYPTO_SECRET` makes them unrecoverable.
- `.env` is created with `chmod 600`.
