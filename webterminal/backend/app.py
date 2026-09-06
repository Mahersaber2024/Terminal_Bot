"""
webterminal/backend/app.py

A real, xterm.js-compatible SSH terminal for the servers a Telegram user has
already registered in the bot - opened as a Telegram Mini App instead of the
line-by-line inline-keyboard terminal in ServerManager/handlers.py.

Deliberately reuses the SAME modules the bot already uses (ServerManager.
settings for "does this server belong to this user", ServerManager.engine for
the actual SSH connection) so there is exactly one source of truth for
servers and credentials - this is a new front door, not a new backend.

Run (from the project root, next to main.py):
    pip install fastapi "uvicorn[standard]"
    export BOT_TOKEN=123456:ABC...          # same token the bot uses
    uvicorn webterminal.backend.app:app --host 0.0.0.0 --port 8081

In production put this behind nginx/caddy with a real TLS certificate -
Telegram refuses to open a Mini App at a non-https URL. See ../README.md.
"""
import asyncio
import json
import logging
import os
import sys
import threading

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

# So `from ServerManager import ...` resolves when this file is run directly
# from webterminal/backend/. If your project layout differs, adjust this.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ServerManager import settings as sm_settings  # noqa: E402
from ServerManager import engine as sm_engine  # noqa: E402

from telegram_auth import verify_init_data  # noqa: E402

logger = logging.getLogger("webterminal")
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
if not BOT_TOKEN:
    logger.warning("BOT_TOKEN is not set - every connection will be rejected. "
                    "Set it to the same token main.py uses.")

app = FastAPI(title="ServerManager Web Terminal")

# Tighten this to your actual Mini App origin before going to production -
# "*" is only fine while you're developing locally.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

READ_CHUNK = 4096
IDLE_POLL_SECONDS = 0.03  # how often the reader thread checks for new output


def _reader_thread(channel, loop: asyncio.AbstractEventLoop, websocket: WebSocket, stop_event: threading.Event):
    """Runs in a plain thread because paramiko's Channel is a blocking/
    poll-style object, not an asyncio one. Bridges it into the websocket via
    run_coroutine_threadsafe so the async side never touches paramiko
    directly (and vice versa)."""
    try:
        while not stop_event.is_set():
            if channel.closed:
                break
            try:
                if channel.recv_ready():
                    data = channel.recv(READ_CHUNK)
                    if not data:
                        break
                    text = data.decode(errors="replace")
                    fut = asyncio.run_coroutine_threadsafe(
                        websocket.send_text(json.dumps({"type": "output", "data": text})), loop,
                    )
                    fut.result(timeout=5)
                else:
                    stop_event.wait(IDLE_POLL_SECONDS)
            except Exception:
                logger.debug("reader thread: channel read/send failed", exc_info=True)
                break
    finally:
        asyncio.run_coroutine_threadsafe(
            websocket.send_text(json.dumps({"type": "closed"})), loop,
        )


@app.websocket("/ws/terminal/{server_id}")
async def terminal_ws(websocket: WebSocket, server_id: str):
    await websocket.accept()

    # ---- Step 1: the client MUST prove who it is before anything else ----
    # We don't trust server_id/user_id from the URL alone - Telegram's
    # initData is the only thing that's actually signed by Telegram.
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=15)
        first_msg = json.loads(raw)
    except (asyncio.TimeoutError, ValueError):
        await websocket.close(code=4001, reason="auth timeout")
        return

    if first_msg.get("type") != "auth":
        await websocket.close(code=4001, reason="expected auth message first")
        return

    ok, tg_user = verify_init_data(first_msg.get("init_data", ""), BOT_TOKEN)
    if not ok or not tg_user or "id" not in tg_user:
        await websocket.close(code=4003, reason="invalid init_data")
        return

    user_id = tg_user["id"]
    server = sm_settings.get_server(user_id, server_id)
    if server is None:
        await websocket.close(code=4004, reason="server not found for this user")
        return

    cols = int(first_msg.get("cols") or 120)
    rows = int(first_msg.get("rows") or 32)

    # ---- Step 2: open the real SSH shell (same helpers engine.py already exposes) ----
    await websocket.send_text(json.dumps({"type": "status", "data": f"Connecting to {server['label']}..."}))
    try:
        client = await asyncio.to_thread(sm_engine.connect, server)
        channel = await asyncio.to_thread(sm_engine.open_shell, client, "xterm-256color", cols, rows)
    except Exception as e:
        await websocket.send_text(json.dumps({"type": "error", "data": str(e)[:300]}))
        await websocket.close(code=4005, reason="ssh connect failed")
        return

    await websocket.send_text(json.dumps({"type": "status", "data": "connected"}))

    loop = asyncio.get_event_loop()
    stop_event = threading.Event()
    reader = threading.Thread(target=_reader_thread, args=(channel, loop, websocket, stop_event), daemon=True)
    reader.start()

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            mtype = msg.get("type")

            if mtype == "input":
                # Raw keystrokes from xterm.js's onData - forward byte-for-byte.
                await asyncio.to_thread(channel.send, msg.get("data", ""))
            elif mtype == "resize":
                try:
                    await asyncio.to_thread(
                        channel.resize_pty, int(msg.get("cols", cols)), int(msg.get("rows", rows)),
                    )
                except Exception:
                    logger.debug("resize_pty failed", exc_info=True)
            elif mtype == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("terminal_ws loop error")
    finally:
        stop_event.set()
        await asyncio.to_thread(sm_engine.close_shell, channel)
        try:
            client.close()
        except Exception:
            pass


@app.get("/api/health")
async def health():
    return {"ok": True}
