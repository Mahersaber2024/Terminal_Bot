# Web Terminal Mini App - prototype

A real, full xterm.js SSH terminal for your ServerManager bot's registered
servers, opened as a Telegram Mini App instead of the inline-keyboard
line-by-line terminal in `handlers.py`.

```
webterminal/
├── backend/
│   ├── app.py            FastAPI + websocket bridge to a paramiko shell
│   └── telegram_auth.py  Verifies Telegram's initData (HMAC per Telegram's spec)
├── frontend/
│   └── index.html        xterm.js UI, single file, no build step
└── bot_integration.md    Exact snippet to add the launcher button
```

## How it fits your existing code

- `backend/app.py` imports `ServerManager.settings` and `ServerManager.engine`
  directly - same server list, same encrypted credentials, same
  `engine.connect` / `engine.open_shell` you already use for the Telegram
  inline terminal. This is a new *frontend*, not a new backend - there's
  still exactly one place your SSH credentials live.
- Every websocket connection has to pass Telegram's own signature check
  (`telegram_auth.verify_init_data`) before it's allowed to open a shell.
  The server_id in the URL is never trusted by itself - it's checked against
  `settings.get_server(user_id, server_id)` using the user_id Telegram
  itself vouches for.

## Running it locally (before you have a domain)

```bash
cd webterminal/backend
pip install fastapi "uvicorn[standard]" paramiko
export BOT_TOKEN="<same token main.py uses>"
uvicorn app:app --host 0.0.0.0 --port 8081
```

Serve `frontend/index.html` from anywhere static (even `python -m http.server`)
and open it manually with `?server_id=<one of your server ids>` while testing
- outside of Telegram, `tg.initData` will be empty, so for local testing
  you'll want to temporarily stub `verify_init_data` to return `(True, {"id": <your_telegram_user_id>})`.
  **Do not ship that stub** - put the real check back before deploying.

## Deploying for real

You need one HTTPS domain. Telegram will not open a Mini App at a plain
`http://` URL, and browsers will not open a plain `ws://` websocket from an
`https://` page (mixed content), so both the page and the socket need TLS.

Simplest layout with nginx:

```nginx
server {
    listen 443 ssl;
    server_name terminal.yourdomain.com;
    ssl_certificate     /etc/letsencrypt/live/terminal.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/terminal.yourdomain.com/privkey.pem;

    location / {
        root /srv/webterminal/frontend;
        try_files $uri /index.html;
    }

    location /ws/ {
        proxy_pass http://127.0.0.1:8081;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;   # keep long-lived terminal sockets open
    }
}
```

Run the backend as a systemd service (same pattern you already use for the
bot itself), then set `WEBTERMINAL_URL=https://terminal.yourdomain.com` in
the bot's `.env` per `bot_integration.md`.

## Things worth doing before real users touch this

- **Rate-limit / cap concurrent shells per user** - right now nothing stops
  one user from opening many terminals at once against the backend.
- **Idle timeout** - the reader thread never times out an inactive session;
  add one so an abandoned tab doesn't hold an SSH connection open forever.
- **Restrict CORS** (`app.add_middleware(CORSMiddleware, ...)`) to your real
  Mini App origin instead of `*` once you're not testing locally anymore.
- **Logging** - pipe connect/disconnect events through the same
  `logger_bot.py` activity log the rest of the bot uses, so a web-terminal
  session shows up next to everything else.
