# Jade Tunnel Sales Bot (JadeTunnel-sales)

A Telegram VPN sales bot built on `python-telegram-bot` that creates V2Ray subscriptions through one or more **3x-UI** panels, handles wallet/card-to-card payments, and delivers a unified subscription link (combining all panels) to the user through a companion web service (`sub-api`).

With `database.py`, `requirements.txt`, and `LICENSE` added, the file set is now **complete** — aside from one dependency bug and one logic bug described in the [Known Issues](#-known-issues) section.

## Quick Install

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Mahersaber2024/JadeTunnel-sales/main/install.sh)
```

Choose `1) Full installation`. The installer will, in order:

1. Install system dependencies
2. Create the PostgreSQL database and its user (or take existing database credentials)
3. Ask for the bot token, admin IDs, and log group
4. Ask for the Iran-hosted domain and server IP for the subscription proxy
5. Clone the project, create the venv, and run `pip install -r requirements.txt`
6. Run `setup_db.py --auto` (tables are also created/migrated by `database.py` on its own first real run)
7. Create and start two systemd services (`sell-bot`, `sub-api`)
8. Generate `index.php` for upload to the Iran host

> Before installing, make sure to apply fix #1 in the [Known Issues](#-known-issues) section (adding `aiohttp` to `requirements.txt`), otherwise the `sub-api` service will fail to start after installation.

After installation, upload the generated `index.php` to the path you specified on the Iran host so the unified subscription link works.

## Uninstallation
```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Mahersaber2024/JadeTunnel-sales/main/install.sh)
```
Choose the Uninstall option from the installer menu.

---

## Backup / Restore

These two scripts live inside the repo's `backups/` folder, and the installer (`install.sh`) also places them in that same path on the server alongside the rest of the project:

```
/opt/terminal-bot/backups/full_backup.sh
/opt/terminal-bot/backups/restore_backup.sh
```

So on a server you've already installed with `install.sh`, **there's no need to download them again** — just go to that path and run them directly:

```bash
cd /opt/terminal-bot/backups

# Take a full backup (database + .env + bot_settings.json + ServerManager/)
sudo bash full_backup.sh /opt/terminal-bot /opt/backups
```

The first argument is the bot's install path and the second is the output path for the backup archive — change them if your bot is installed elsewhere. The final output is a `.tar.gz` file in `/opt/backups`, and its exact path is printed at the end of the script's run.

To restore on a new server, first run `install.sh` there as usual (this also puts `backups/` in place), then copy the backup archive over and run `restore_backup.sh` from that same path:
```bash
scp /opt/backups/terminalbot_backup_*.tar.gz root@NEW_SERVER_IP:/root/
# on the new server, after install.sh has been run:
cd /opt/terminal-bot/backups
sudo bash restore_backup.sh /root/terminalbot_backup_*.tar.gz /opt/terminal-bot
```

> ⚠️ **Service name mismatch:** these two scripts operate on a service named `terminal-bot` (`SERVICE_NAME="terminal-bot"` at the top of the file), while this project's actual services are named `sell-bot` and `sub-api` (see the "systemd services" section below). This means the stop/restart step during backup and restore actually targets a nonexistent service called `terminal-bot` and silently fails due to `|| true` — the database and config files are still backed up/restored correctly, but the real service (`sell-bot`) is not automatically stopped/restarted. Until this is fixed upstream, stop it manually before running `restore_backup.sh` and restart it manually afterward:
> ```bash
> sudo systemctl stop sell-bot sub-api
> sudo bash restore_backup.sh terminalbot_backup_*.tar.gz /opt/terminal-bot
> sudo systemctl start sell-bot sub-api
> ```

---

## systemd Services

```bash
systemctl start sell-bot sub-api
systemctl stop sell-bot sub-api
systemctl status sell-bot sub-api
journalctl -u sell-bot -f
journalctl -u sub-api -f
systemctl restart sell-bot sub-api
```

`sub-api` endpoints (port `2053`):

| Path | Output |
|---|---|
| `GET /sub/{token}` | Subscription links as base64 |
| `GET /sub/{token}?details=1` | JSON with per-subscription details (for HTML rendering by `index.php`) |
| `GET /api/raw/{token}` | Same details without base64 |
| `GET /api/info/{token}` | User info and balance |
| `GET /health` | Health check |

---

## 3x-UI Panel Management

From the bot's admin panel (`/admin` → Panel management) you can add/remove panels, change each panel's capacity, change the default panel, and specify which plan types each panel supports. `panel_manager.get_panel_for_subscription()` picks the right panel for each **new purchase** based on these settings and current capacity — with no automatic fallback to unrelated panels.

---

## Unified Subscription Flow

1. In the "📒 Subscriptions" section, the user receives a single link: `https://<iran-host>/sub/<token>`
2. If opened in a browser → `index.php` shows a Persian HTML page with a copy button for each config
3. If opened with a VPN client → the base64 text is imported directly
4. `index.php` does this by calling `sub_api.py` on the main server (`http://<SERVER_IP>:2053`); `sub_api.py` calls the correct panel (the `panel_id` stored on that record) for each of the user's subscriptions to fetch the actual link.

---

## ⚠️ Known Issues

### 1. `requirements.txt` is missing the `aiohttp` dependency (critical for `sub-api`)
`sub_api.py` has this line:
```python
from aiohttp import web
```
but `requirements.txt` only includes:
```
python-telegram-bot
psycopg2-binary
python-dotenv
requests
httpx
```
Without `aiohttp`, after `pip install -r requirements.txt`, the `sub-api` service crashes with `ModuleNotFoundError: No module named 'aiohttp'` (the `sell-bot` service is unaffected since it doesn't need `aiohttp`).

**Quick fix:**
```bash
echo "aiohttp" >> requirements.txt
cd /opt/sell-bot && source venv/bin/activate && pip install aiohttp && deactivate
systemctl restart sub-api
```

### 2. Extra data-volume updates always apply to the default panel, not the subscription's actual panel (logic bug, non-critical)
In four places in `handlers.py` (adding volume via wallet, adding volume via card-to-card, and the corresponding admin confirmations), this pattern repeats:
```python
subscription = db.get_subscription(subscription_id)
if subscription and subscription.get('email'):
    from client_manager import get_panel_client
    panel_client = get_panel_client()          # ← no panel_id, so always the default panel
    panel_client.update_client_volume(subscription['email'], subscription['remaining_volume'])
```
The root cause is that `Database.get_subscription()` doesn't select the `panel_id` column in its query at all:
```python
SELECT id, user_id, protocol, duration_days, email,
       plan_type, remaining_volume, start_date, end_date
FROM subscriptions WHERE id = %s
```
Since `panel_id` isn't in this query's result, even if it were passed through the `handlers.py` calls, it never reaches `get_panel_client()`. A full fix requires adding `panel_id` to the `SELECT` in `Database.get_subscription()` and passing it to `get_panel_client(panel_id)` in all four spots in `handlers.py`.

### 3. Service name mismatch in the backup/restore scripts (non-critical)
`full_backup.sh` and `restore_backup.sh` are written with `SERVICE_NAME="terminal-bot"`, while this project's actual services are `sell-bot` and `sub-api`. Details and a workaround are in the [Backup / Restore](#backup--restore) section above.

## Security Notes

- `config.json` contains the bot token and database password; the installer creates it with `chmod 600`.
- GitHub Personal Access Tokens should never be typed into clone/paste commands or logged; revoke immediately if exposed.
- Bank card info and the log group number live in `bot_settings.json`/`config.json` — don't commit these files.
- Enable `API_KEY` in `sub_api.py` via the `SUB_API_KEY` environment variable so endpoints aren't accessible without authentication (default is empty = no restriction).
- Communication between `index.php` (Iran host) and `sub_api.py` happens over plain, unauthenticated HTTP; in production, restrict it behind a firewall limited to the Iran host's IP, or add HTTPS/`SUB_API_KEY`.

## License

MIT License — © 2026 Mahersaber2024. Full text in the `LICENSE` file.
