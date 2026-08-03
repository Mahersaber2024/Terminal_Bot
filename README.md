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

## Backup / Restore

Two scripts live in the `backups/` folder and are installed there automatically by `install.sh`:

```
/opt/terminal-bot/backups/full_backup.sh
/opt/terminal-bot/backups/restore_backup.sh
```

`/opt/terminal-bot` is the default install path used above; substitute your own if you installed elsewhere. They back up (or restore) the database, `.env`, `bot_settings.json`, and the `ServerManager/` directory (which holds the encrypted server credentials).

**Take a full backup:**
```bash
cd /opt/terminal-bot/backups
sudo bash full_backup.sh /opt/terminal-bot /opt/backups
```
The first argument is the bot's install path, the second is the output directory. The result is a single `terminalbot_backup_*.tar.gz` file in `/opt/backups` — its exact path is printed at the end of the run. Since it contains the bot token and encrypted server credentials, transfer it only over a secure channel (`scp`/`sftp`).

**Restore on a new server:**
```bash
# 1) Install the project on the new server first (so backups/restore_backup.sh exists there):
bash <(curl -fsSL https://raw.githubusercontent.com/Mahersaber2024/Terminal_Bot/main/install.sh)

# 2) Copy the backup archive over:
scp /opt/backups/terminalbot_backup_*.tar.gz root@NEW_SERVER_IP:/root/

# 3) On the new server:
cd /opt/terminal-bot/backups
sudo bash restore_backup.sh /root/terminalbot_backup_*.tar.gz /opt/terminal-bot
```
`restore_backup.sh` stops and restarts the `terminal-bot` service itself, so no manual `systemctl` steps are needed.

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
- The full backup archive (`terminalbot_backup_*.tar.gz`) contains the bot token and encrypted server credentials — only transfer it over a secure channel, never commit it.
