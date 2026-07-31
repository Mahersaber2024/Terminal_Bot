# ====================== Server Manager (public feature - every user manages their own server(s)) ======================
import asyncio
import logging
import queue as queue_mod
import re
import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.error import BadRequest
from telegram.ext import ContextTypes, ConversationHandler

from . import server_manager_settings as settings
from . import server_manager_engine as engine

logger = logging.getLogger(__name__)

# ====================== Conversation States ======================
SERVERMGR_LABEL_INPUT = 600
SERVERMGR_HOST_INPUT = 601
SERVERMGR_PORT_INPUT = 602
SERVERMGR_USERNAME_INPUT = 603
SERVERMGR_PASSWORD_INPUT = 604
SERVERMGR_CMD_INPUT = 605
SERVERMGR_QUICKPING_INPUT = 606

CANCEL_BUTTON_TEXT = "❌ Cancel"
CMD_DONE_TEXT = "🔚 End Session"
MENU_BUTTON_TEXT = "🖥 Server Manager"   # matches the KeyboardButton added to handlers.get_main_menu()
CONNECT_TIMEOUT = 15   # seconds, for opening the SSH connection
COMMAND_TIMEOUT = 6 * 60 * 60   # seconds (6 hours), for running one command (incl. paramiko's own exec timeout) - long because there's a manual Cancel button, so no need to auto-kill quickly

# Live "terminal" view while a command is streaming
CMD_CANCEL_CALLBACK = "servermgr_cmdcancel"
TERMINAL_EDIT_INTERVAL = 1.2   # min seconds between Telegram message edits (stay well under rate limits)
TERMINAL_MAX_IDLE_EDIT = 4.0   # force a heartbeat edit at least this often even with no new output
TERMINAL_BODY_CHARS = 3200     # tail of output kept inside the code block (Telegram caps messages at 4096 chars)

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[=>]")

def _strip_ansi(text: str) -> str:
    text = _ANSI_ESCAPE_RE.sub("", text)
    # A pty ends every line with "\r\n" - that trailing \r is just part of the
    # normal line ending, not a mid-line overwrite, so normalize it away first.
    text = text.replace("\r\n", "\n")
    # Any \r still left is a genuine mid-line overwrite (e.g. a progress bar
    # redrawing the same line) - keep only what was printed last on that line.
    if "\r" in text:
        text = "\n".join(line.split("\r")[-1] for line in text.split("\n"))
    return text

# ====================== External deps (set from main.py) ======================
_get_main_menu_func = None


def set_get_main_menu(func):
    global _get_main_menu_func
    _get_main_menu_func = func


def get_main_menu():
    if _get_main_menu_func:
        return _get_main_menu_func()
    return None


def _uid(update: Update) -> int:
    return update.effective_user.id


def _cancel_keyboard():
    return ReplyKeyboardMarkup([[CANCEL_BUTTON_TEXT]], resize_keyboard=True, one_time_keyboard=True)


def _ssh_session_keyboard():
    return ReplyKeyboardMarkup([[CMD_DONE_TEXT]], resize_keyboard=True)


async def _run_with_timeout(func, *args, timeout: int):
    try:
        result = await asyncio.wait_for(asyncio.to_thread(func, *args), timeout=timeout)
        return result, False
    except asyncio.TimeoutError:
        return None, True
    except Exception as e:
        logger.warning(f"servermgr background call failed: {e}")
        return e, False


def _terminal_cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=CMD_CANCEL_CALLBACK)]])


def _format_terminal(label: str, command: str, output: str, status_line: str) -> str:
    body = _strip_ansi(output)
    body = body[-TERMINAL_BODY_CHARS:]
    if len(output) > TERMINAL_BODY_CHARS:
        body = "…(older output trimmed)…\n" + body
    header = f"🖥️ *Terminal* — `{label}`\n`$ {command}`"
    return f"{header}\n```\n{body}\n```\n{status_line}"


def _close_ssh_session(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("servermgr_cmd_handle_box", None)
    context.user_data.pop("servermgr_cmd_cancel_requested", None)
    context.user_data.pop("servermgr_terminal_state", None)
    channel = context.user_data.pop("servermgr_shell_channel", None)
    engine.close_shell(channel)
    client = context.user_data.pop("servermgr_ssh_client", None)
    context.user_data.pop("servermgr_ssh_server_id", None)
    if client is not None:
        try:
            client.close()
        except Exception:
            pass


async def servermgr_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _close_ssh_session(context)
    context.user_data.pop("servermgr_new_server", None)
    # Clear whatever custom reply-keyboard was showing (Cancel / End Session).
    # The main menu keyboard gets restored centrally in _reply_with_servermgr_menu()
    # below - see the comment there for why that step can't be skipped.
    await update.message.reply_text("❌ Operation cancelled.", reply_markup=ReplyKeyboardRemove())
    await _reply_with_servermgr_menu(update, "")
    return ConversationHandler.END


# ====================== Helpers ======================
def _servers_text_and_keyboard(user_id):
    servers = settings.get_servers(user_id)
    text = (
        "🖥 Server Manager\n\n"
        "Register your servers here, then run any command on them over SSH.\n"
        "To quickly ping an IP/domain (no server registration needed), use the button below.\n\n"
    )
    keyboard = []
    if servers:
        # Two servers per row
        for i in range(0, len(servers), 2):
            row = servers[i:i + 2]
            keyboard.append([
                InlineKeyboardButton(f"🖥 {s['label']} ({s['host']})", callback_data=f"servermgr_srv_{s['id']}")
                for s in row
            ])
    else:
        text += "❌ No servers registered yet."
    keyboard.append([
        InlineKeyboardButton("📶 Quick Ping", callback_data="servermgr_quickping_start"),
        InlineKeyboardButton("➕ Add Server", callback_data="servermgr_srv_add"),
    ])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="servermgr_back_to_main")])
    return text, InlineKeyboardMarkup(keyboard)


def _server_detail_text_and_keyboard(server: dict):
    text = (
        f"🖥 {server['label']}\n\n"
        f"Address: `{server['host']}:{server.get('port', 22)}`\n"
        f"Username: `{server['username']}`"
    )
    keyboard = [
        [
            InlineKeyboardButton("💻 Run Command (SSH)", callback_data=f"servermgr_ssh_start_{server['id']}"),
        ],
        [
            InlineKeyboardButton("🗑 Delete Server", callback_data=f"servermgr_del_{server['id']}"),
            InlineKeyboardButton("🔙 Back", callback_data="servermgr_menu"),
        ],
    ]
    return text, InlineKeyboardMarkup(keyboard)


async def _reply_with_servermgr_menu(update: Update, message: str):
    user_id = _uid(update)
    text, reply_markup = _servers_text_and_keyboard(user_id)
    await update.message.reply_text(message or "Server Manager:", reply_markup=get_main_menu())
    try:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    except Exception as e:
        logger.debug(f"Markdown reply failed, falling back to plain text: {e}")
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=None)


# ====================== Entry point from the main reply-keyboard menu ======================
async def servermgr_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Triggered by the '🖥 Server Manager' button on the bot's main menu (any user)."""
    user_id = _uid(update)
    text, reply_markup = _servers_text_and_keyboard(user_id)
    try:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    except Exception as e:
        logger.debug(f"Markdown reply failed, falling back to plain text: {e}")
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=None)


# ====================== List / detail navigation ======================
async def servermgr_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text, reply_markup = _servers_text_and_keyboard(query.from_user.id)
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    except Exception as e:
        logger.debug(f"Markdown edit failed, falling back to plain text: {e}")
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=None)


async def servermgr_back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await query.edit_message_text("🔙 Returned to the main menu.")
    except Exception:
        pass
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="Main menu:",
        reply_markup=get_main_menu(),
    )


async def servermgr_srv_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    server_id = query.data.replace("servermgr_srv_", "", 1)
    server = settings.get_server(query.from_user.id, server_id)
    if not server:
        text, reply_markup = _servers_text_and_keyboard(query.from_user.id)
        await query.edit_message_text("❌ Server not found.", reply_markup=reply_markup)
        return
    text, reply_markup = _server_detail_text_and_keyboard(server)
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")


# ====================== Quick ping (no server registration needed) ======================
async def servermgr_quickping_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("📶 Quick Ping\n\nSend the IP or domain you want to ping:")
    await context.bot.send_message(chat_id=query.message.chat_id, text="Address:", reply_markup=_cancel_keyboard())
    return SERVERMGR_QUICKPING_INPUT


async def servermgr_quickping_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text in ("/cancel", CANCEL_BUTTON_TEXT):
        await _reply_with_servermgr_menu(update, "❌ Operation cancelled.")
        return ConversationHandler.END
    if not text:
        await update.message.reply_text("❌ Send an IP or domain:")
        return SERVERMGR_QUICKPING_INPUT

    await update.message.reply_text("⏳ Pinging...", reply_markup=ReplyKeyboardRemove())
    result, timed_out = await _run_with_timeout(engine.ping_host, text, 4, 2, timeout=20)

    if timed_out:
        reply = f"⏱ Ping to \"{text}\" took too long."
    elif result.get("error") and result.get("loss_percent") is None:
        reply = f"❌ Ping to \"{text}\" failed:\n{result['error']}"
    elif result["ok"]:
        avg = f"{result['avg_ms']:.0f}ms" if result.get("avg_ms") is not None else "-"
        reply = f"✅ \"{text}\" responded.\nAvg latency: {avg} — Packet loss: {result['loss_percent']}%"
    else:
        reply = f"❌ \"{text}\" did not respond to ping (packet loss {result.get('loss_percent', 100)}%)."

    await update.message.reply_text(reply)
    await _reply_with_servermgr_menu(update, "")
    return ConversationHandler.END


# ====================== Add server (label -> host -> port -> username -> password) ======================
async def servermgr_srv_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["servermgr_new_server"] = {}
    await query.edit_message_text("➕ Add Server\n\nStep 1/5 — send a short label for this server (e.g. `DE-1`):", parse_mode="Markdown")
    await context.bot.send_message(chat_id=query.message.chat_id, text="Label:", reply_markup=_cancel_keyboard())
    return SERVERMGR_LABEL_INPUT


async def servermgr_srv_label_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in ("/cancel", CANCEL_BUTTON_TEXT):
        await _reply_with_servermgr_menu(update, "❌ Operation cancelled.")
        return ConversationHandler.END
    if not text:
        await update.message.reply_text("❌ Label cannot be empty. Send it again:")
        return SERVERMGR_LABEL_INPUT
    context.user_data["servermgr_new_server"]["label"] = text
    await update.message.reply_text("Step 2/5 — send the server's IP or hostname:", reply_markup=_cancel_keyboard())
    return SERVERMGR_HOST_INPUT


async def servermgr_srv_host_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in ("/cancel", CANCEL_BUTTON_TEXT):
        await _reply_with_servermgr_menu(update, "❌ Operation cancelled.")
        return ConversationHandler.END
    if not text:
        await update.message.reply_text("❌ Host cannot be empty. Send it again:")
        return SERVERMGR_HOST_INPUT
    context.user_data["servermgr_new_server"]["host"] = text
    await update.message.reply_text(
        "Step 3/5 — send the SSH port (or `-` for the default 22):",
        reply_markup=_cancel_keyboard(), parse_mode="Markdown"
    )
    return SERVERMGR_PORT_INPUT


async def servermgr_srv_port_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in ("/cancel", CANCEL_BUTTON_TEXT):
        await _reply_with_servermgr_menu(update, "❌ Operation cancelled.")
        return ConversationHandler.END
    if text == "-":
        port = 22
    else:
        try:
            port = int(text)
            if not (1 <= port <= 65535):
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Send a valid port number, or `-` for the default:", parse_mode="Markdown")
            return SERVERMGR_PORT_INPUT
    context.user_data["servermgr_new_server"]["port"] = port
    await update.message.reply_text(
        "Step 4/5 — send the SSH username (or `-` for `root`):",
        reply_markup=_cancel_keyboard(), parse_mode="Markdown"
    )
    return SERVERMGR_USERNAME_INPUT


async def servermgr_srv_username_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in ("/cancel", CANCEL_BUTTON_TEXT):
        await _reply_with_servermgr_menu(update, "❌ Operation cancelled.")
        return ConversationHandler.END
    username = "root" if text == "-" else text
    if not username:
        await update.message.reply_text("❌ Username cannot be empty. Send it again:")
        return SERVERMGR_USERNAME_INPUT
    context.user_data["servermgr_new_server"]["username"] = username
    await update.message.reply_text("Step 5/5 — send the SSH password:", reply_markup=_cancel_keyboard())
    return SERVERMGR_PASSWORD_INPUT


async def servermgr_srv_password_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    stripped = text.strip() if text else ""
    if stripped in ("/cancel", CANCEL_BUTTON_TEXT):
        await _reply_with_servermgr_menu(update, "❌ Operation cancelled.")
        return ConversationHandler.END
    if not stripped:
        await update.message.reply_text("❌ Password cannot be empty. Send it again:")
        return SERVERMGR_PASSWORD_INPUT

    new_server = context.user_data.get("servermgr_new_server", {})
    server = settings.add_server(
        _uid(update),
        label=new_server.get("label", ""),
        host=new_server.get("host", ""),
        port=new_server.get("port", 22),
        username=new_server.get("username", "root"),
        password=stripped,
    )
    context.user_data.pop("servermgr_new_server", None)

    # Delete the message containing the password from the chat so it doesn't stay in history
    try:
        await update.message.delete()
    except Exception:
        pass

    await _reply_with_servermgr_menu(update, f"✅ Server \"{server['label']}\" registered.")
    return ConversationHandler.END


# ====================== Delete server ======================
async def servermgr_del_confirm_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    server_id = query.data.replace("servermgr_del_", "", 1)
    server = settings.get_server(query.from_user.id, server_id)
    if not server:
        await query.answer("Server not found.", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, delete", callback_data=f"servermgr_delok_{server_id}"),
         InlineKeyboardButton("❌ Cancel", callback_data=f"servermgr_srv_{server_id}")],
    ])
    await query.edit_message_text(f"Are you sure you want to delete \"{server['label']}\"?", reply_markup=keyboard)


async def servermgr_del_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    server_id = query.data.replace("servermgr_delok_", "", 1)
    settings.remove_server(query.from_user.id, server_id)
    await query.answer("🗑 Deleted.")
    text, reply_markup = _servers_text_and_keyboard(query.from_user.id)
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")


# ====================== SSH command execution (loop of one-off commands over one connection) ======================
async def servermgr_ssh_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    server_id = query.data.replace("servermgr_ssh_start_", "", 1)
    server = settings.get_server(query.from_user.id, server_id)
    if not server:
        await query.answer("Server not found.", show_alert=True)
        return ConversationHandler.END

    await query.answer("⏳ Connecting...")
    client, timed_out = await _run_with_timeout(engine.connect, server, timeout=CONNECT_TIMEOUT)
    if timed_out:
        await query.edit_message_text(f"⏱ Connecting to \"{server['label']}\" took more than {CONNECT_TIMEOUT}s and was cancelled.")
        return ConversationHandler.END
    if client is None or isinstance(client, Exception):
        err = f"\n{client}" if isinstance(client, Exception) else ""
        await query.edit_message_text(f"❌ Failed to connect to \"{server['label']}\".{err}")
        return ConversationHandler.END

    channel, shell_timed_out = await _run_with_timeout(engine.open_shell, client, timeout=CONNECT_TIMEOUT)
    if shell_timed_out or channel is None or isinstance(channel, Exception):
        try:
            client.close()
        except Exception:
            pass
        err = f"\n{channel}" if isinstance(channel, Exception) else ""
        await query.edit_message_text(f"❌ Failed to open a shell on \"{server['label']}\".{err}")
        return ConversationHandler.END

    context.user_data["servermgr_ssh_client"] = client
    context.user_data["servermgr_ssh_server_id"] = server_id
    context.user_data["servermgr_shell_channel"] = channel

    await query.edit_message_text(
        f"💻 Connected to \"{server['label']}\" ({server['host']}).\n"
        f"This is one continuous shell session - shell state (cwd, env vars) carries over between "
        f"messages, and you can reply to prompts from an interactive program (e.g. answer a menu "
        f"with just \"20\") the same way you would in a real terminal.\n\n"
        f"To finish, tap \"{CMD_DONE_TEXT}\"."
    )
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="Send a command:",
        reply_markup=_ssh_session_keyboard(),
    )
    return SERVERMGR_CMD_INPUT


async def servermgr_cmd_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    if text in ("/cancel", CANCEL_BUTTON_TEXT, CMD_DONE_TEXT):
        _close_ssh_session(context)
        await update.message.reply_text("🔚 SSH session closed.", reply_markup=ReplyKeyboardRemove())
        await _reply_with_servermgr_menu(update, "Returned to the Server Manager menu.")
        return ConversationHandler.END

    client = context.user_data.get("servermgr_ssh_client")
    server_id = context.user_data.get("servermgr_ssh_server_id")
    server = settings.get_server(_uid(update), server_id) if server_id else None

    if not client or not server:
        _close_ssh_session(context)
        await update.message.reply_text("⚠️ Connection was lost. Please open the menu again.", reply_markup=ReplyKeyboardRemove())
        await _reply_with_servermgr_menu(update, "Server Manager menu:")
        return ConversationHandler.END

    if not text:
        await update.message.reply_text("Send a command:")
        return SERVERMGR_CMD_INPUT

    channel = context.user_data.get("servermgr_shell_channel")

    if not engine.is_alive(client) or channel is None or channel.closed:
        # Try reconnecting (and reopening a shell) once before fully closing the session
        new_client, timed_out = await _run_with_timeout(engine.connect, server, timeout=CONNECT_TIMEOUT)
        if timed_out or new_client is None or isinstance(new_client, Exception):
            _close_ssh_session(context)
            await update.message.reply_text(
                "❌ The connection dropped and reconnecting also failed. Session closed.",
                reply_markup=ReplyKeyboardRemove(),
            )
            await _reply_with_servermgr_menu(update, "Server Manager menu:")
            return ConversationHandler.END
        new_channel, shell_timed_out = await _run_with_timeout(engine.open_shell, new_client, timeout=CONNECT_TIMEOUT)
        if shell_timed_out or new_channel is None or isinstance(new_channel, Exception):
            try:
                new_client.close()
            except Exception:
                pass
            _close_ssh_session(context)
            await update.message.reply_text(
                "❌ The connection dropped and reopening a shell also failed. Session closed.",
                reply_markup=ReplyKeyboardRemove(),
            )
            await _reply_with_servermgr_menu(update, "Server Manager menu:")
            return ConversationHandler.END
        client = new_client
        channel = new_channel
        context.user_data["servermgr_ssh_client"] = client
        context.user_data["servermgr_shell_channel"] = channel

    # ---- Live "terminal" view: stream output in real time with a Cancel button ----
    chunk_queue = queue_mod.Queue()
    handle_box = {"handle": None}

    def on_chunk(handle, chunk_text):
        # Runs on the worker thread (see engine.run_command_stream). Just
        # hands the CommandHandle to the main coroutine and queues output -
        # all actual Telegram calls happen below, on the event loop.
        handle_box["handle"] = handle
        if chunk_text:
            chunk_queue.put(chunk_text)

    label = server["label"]
    msg = await update.message.reply_text(
        _format_terminal(label, text, "", "⏳ Running…"),
        parse_mode="Markdown",
        reply_markup=_terminal_cancel_keyboard(),
    )
    context.user_data["servermgr_cmd_handle_box"] = handle_box
    context.user_data.pop("servermgr_cmd_cancel_requested", None)
    # Kept up to date below so the Cancel button (once no command is actively
    # streaming) can still edit this same message right away - see
    # servermgr_cmd_cancel_button()'s idle-prompt branch.
    term_state = {"msg": msg, "label": label, "command": text, "output": ""}
    context.user_data["servermgr_terminal_state"] = term_state

    task = asyncio.create_task(
        asyncio.to_thread(engine.run_shell_input, channel, text, COMMAND_TIMEOUT, engine.SHELL_QUIET_SECONDS, on_chunk)
    )

    output_so_far = ""
    last_edit_at = 0.0
    last_sent_text = None
    while not task.done():
        await asyncio.sleep(0.3)
        got_new = False
        while True:
            try:
                output_so_far += chunk_queue.get_nowait()
                got_new = True
            except queue_mod.Empty:
                break

        now = time.monotonic()
        due_for_heartbeat = now - last_edit_at >= TERMINAL_MAX_IDLE_EDIT
        if (got_new and now - last_edit_at >= TERMINAL_EDIT_INTERVAL) or due_for_heartbeat:
            status = "⏳ Cancelling…" if context.user_data.get("servermgr_cmd_cancel_requested") else "⏳ Running…"
            new_text = _format_terminal(label, text, output_so_far, status)
            if new_text != last_sent_text:
                try:
                    await msg.edit_text(new_text, parse_mode="Markdown", reply_markup=_terminal_cancel_keyboard())
                    last_sent_text = new_text
                    term_state["output"] = output_so_far
                except BadRequest as e:
                    if "not modified" not in str(e).lower():
                        logger.debug(f"terminal live edit failed: {e}")
                except Exception as e:
                    logger.debug(f"terminal live edit failed: {e}")
            last_edit_at = now

    # Drain whatever arrived between the last poll and the task actually finishing
    while True:
        try:
            output_so_far += chunk_queue.get_nowait()
        except queue_mod.Empty:
            break

    result = task.result()
    context.user_data.pop("servermgr_cmd_handle_box", None)
    context.user_data.pop("servermgr_cmd_cancel_requested", None)

    if result.get("error"):
        status = f"❌ Execution error: {result['error']}"
    elif result.get("cancelled"):
        status = "🛑 Cancelled. Session is still open — send the next command."
    elif result.get("timed_out"):
        status = f"⏱ No prompt back after {COMMAND_TIMEOUT // 3600}h — still running in the background."
    else:
        status = "✅ Ready — send the next command."

    final_text = _format_terminal(label, text, output_so_far, status)
    try:
        # Keep the Cancel button attached even after the command finishes -
        # the shell session itself stays open (see run_shell_input()'s design
        # note above), so the user may want to send Ctrl-C at any moment even
        # while sitting idle at a prompt, not only while a command is running.
        # servermgr_cmd_cancel_button() below handles both cases.
        await msg.edit_text(final_text, parse_mode="Markdown", reply_markup=_terminal_cancel_keyboard())
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.debug(f"terminal final edit failed: {e}")
    except Exception as e:
        logger.debug(f"terminal final edit failed: {e}")

    term_state["output"] = output_so_far
    return SERVERMGR_CMD_INPUT


async def servermgr_cmd_cancel_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    handle_box = context.user_data.get("servermgr_cmd_handle_box")
    handle = handle_box.get("handle") if handle_box else None
    if handle is not None:
        # A command is actively streaming - cancel it the normal way.
        context.user_data["servermgr_cmd_cancel_requested"] = True
        handle.cancel()
        await query.answer("🛑 Cancelling…")
        return

    # No command is currently running (the button is now shown at all times -
    # see servermgr_cmd_input()'s final edit above), but the shell session is
    # still open, so send Ctrl-C straight to it - e.g. to break out of a stuck
    # foreground program or an interactive menu the user no longer wants.
    channel = context.user_data.get("servermgr_shell_channel")
    if channel is None or channel.closed:
        await query.answer("Nothing to cancel right now.")
        return
    try:
        channel.send(engine.CTRL_C)
    except Exception:
        await query.answer("⚠️ Could not cancel - the session may have dropped.")
        return
    await query.answer("🛑 Ctrl-C sent.")

    # query.answer() only pops a small toast - it never touches the terminal
    # message, so without the block below the ^C and the fresh shell prompt
    # only became visible the next time the user sent a command (this was the
    # actual bug: the terminal looked unchanged right after tapping Cancel).
    # Give the pty a brief moment to echo the ^C and print its next prompt,
    # then fold that into the same terminal message right away.
    term_state = context.user_data.get("servermgr_terminal_state")
    if term_state is None or term_state.get("msg") is None:
        return

    extra_output = ""
    start = time.monotonic()
    last_data_at = start
    while True:
        got_data = False
        try:
            if channel.recv_ready():
                chunk = channel.recv(4096).decode(errors="ignore")
                if chunk:
                    extra_output += chunk
                    got_data = True
                    last_data_at = time.monotonic()
            if channel.recv_stderr_ready():
                chunk = channel.recv_stderr(4096).decode(errors="ignore")
                if chunk:
                    extra_output += chunk
                    got_data = True
                    last_data_at = time.monotonic()
        except Exception:
            break
        now = time.monotonic()
        if now - last_data_at > engine.SHELL_QUIET_SECONDS or now - start > 5:
            break
        if not got_data:
            await asyncio.sleep(0.15)

    if not extra_output:
        return

    term_state["output"] += extra_output
    new_text = _format_terminal(
        term_state["label"], term_state["command"], term_state["output"],
        "🛑 Ctrl-C sent — send the next command.",
    )
    try:
        await term_state["msg"].edit_text(new_text, parse_mode="Markdown", reply_markup=_terminal_cancel_keyboard())
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.debug(f"terminal cancel edit failed: {e}")
    except Exception as e:
        logger.debug(f"terminal cancel edit failed: {e}")


# ========== Handler registration (call this function from main.py) ==========
def register_handlers(application):
    from telegram.ext import CallbackQueryHandler, MessageHandler, filters
    application.add_handler(MessageHandler(filters.Regex(f"^{MENU_BUTTON_TEXT}$"), servermgr_open))
    application.add_handler(CallbackQueryHandler(servermgr_menu, pattern="^servermgr_menu$"))
    application.add_handler(CallbackQueryHandler(servermgr_back_to_main, pattern="^servermgr_back_to_main$"))
    application.add_handler(CallbackQueryHandler(servermgr_srv_detail, pattern="^servermgr_srv_(?!add$)"))
    application.add_handler(CallbackQueryHandler(servermgr_del_confirm_prompt, pattern="^servermgr_del_"))
    application.add_handler(CallbackQueryHandler(servermgr_del_execute, pattern="^servermgr_delok_"))
