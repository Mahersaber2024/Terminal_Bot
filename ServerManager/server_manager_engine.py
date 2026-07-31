import os
import re
import shlex
import time
import logging
import subprocess
from typing import Callable, Dict, Optional

try:
    import paramiko
except ImportError:  # pragma: no cover - surfaced as a clear runtime error instead
    paramiko = None

logger = logging.getLogger(__name__)

SSH_CONNECT_TIMEOUT = 10  # seconds
DEFAULT_CMD_TIMEOUT = int(os.getenv("SERVERMGR_CMD_TIMEOUT", "30"))  # seconds per command
MAX_OUTPUT_CHARS = 3500  # keep replies under Telegram's message size limit
STREAM_POLL_INTERVAL = 0.15  # seconds between recv_ready()/cancel checks while a command is running
SHELL_QUIET_SECONDS = float(os.getenv("SERVERMGR_SHELL_QUIET_SECONDS", "1.5"))  # no new output for this long -> hand control back to the user, like a real terminal sitting at a prompt (or mid-way through an interactive program)
CTRL_C = "\x03"


class ServerManagerError(Exception):
    pass


# ==================================================
# Local ping (no SSH / no credentials needed)
# ==================================================
_PING_LOSS_RE = re.compile(r"(\d+)%\s*packet loss")
_PING_AVG_RE = re.compile(r"=\s*[\d.]+/([\d.]+)/")  # rtt min/avg/max/mdev = a/b/c/d


def ping_host(host: str, count: int = 4, timeout: int = 2) -> Dict:
    result = {"host": host, "ok": False, "loss_percent": None, "avg_ms": None, "error": None, "raw": ""}
    cmd = ["ping", "-c", str(int(count)), "-W", str(int(timeout)), host]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=count * timeout + 10,
        )
        out = proc.stdout or ""
        err = proc.stderr or ""
        result["raw"] = out.strip()[:1000]

        loss_match = _PING_LOSS_RE.search(out)
        avg_match = _PING_AVG_RE.search(out)
        if loss_match:
            loss = int(loss_match.group(1))
            result["loss_percent"] = loss
            result["ok"] = loss < 100
        if avg_match:
            result["avg_ms"] = float(avg_match.group(1))
        if not loss_match and not avg_match:
            result["error"] = (err or out or "no output from ping").strip()[:300]
    except subprocess.TimeoutExpired:
        result["error"] = f"ping timed out after {count * timeout + 10}s"
    except Exception as e:
        result["error"] = str(e)[:300]
    return result


# ==================================================
# SSH connect / run command
# ==================================================
def connect(server: Dict) -> "paramiko.SSHClient":
    if paramiko is None:
        raise ServerManagerError("paramiko is not installed. Run: pip install paramiko")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        server["host"],
        port=int(server.get("port", 22) or 22),
        username=server["username"],
        password=server["password"],
        timeout=SSH_CONNECT_TIMEOUT,
        banner_timeout=SSH_CONNECT_TIMEOUT,
        auth_timeout=SSH_CONNECT_TIMEOUT,
        look_for_keys=False,
        allow_agent=False,
    )
    return client


def is_alive(client: Optional["paramiko.SSHClient"]) -> bool:
    if client is None:
        return False
    transport = client.get_transport()
    return bool(transport and transport.is_active())


def run_command(client: "paramiko.SSHClient", command: str, timeout: int = DEFAULT_CMD_TIMEOUT) -> Dict:
    result = {"command": command, "ok": False, "stdout": "", "stderr": "", "exit_status": None, "error": None}
    try:
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout, get_pty=False)
        out = stdout.read().decode(errors="ignore")
        err = stderr.read().decode(errors="ignore")
        exit_status = stdout.channel.recv_exit_status()
        result["stdout"] = out[:MAX_OUTPUT_CHARS]
        result["stderr"] = err[:MAX_OUTPUT_CHARS]
        result["exit_status"] = exit_status
        result["ok"] = exit_status == 0
    except Exception as e:
        result["error"] = str(e)[:300]
    return result


# ==================================================
# SSH streaming command execution (real-time output + manual cancel)
# ==================================================
class CommandHandle:

    def __init__(self, channel: "paramiko.Channel"):
        self._channel = channel
        self._cancelled = False

    def cancel(self):
        self._cancelled = True
        try:
            self._channel.close()
        except Exception:
            pass

    @property
    def cancelled(self) -> bool:
        return self._cancelled


def run_command_stream(
    client: "paramiko.SSHClient",
    command: str,
    timeout: int = DEFAULT_CMD_TIMEOUT,
    chunk_cb: Optional[Callable[["CommandHandle", str], None]] = None,
) -> Dict:
    result = {"command": command, "ok": False, "stdout": "", "stderr": "",
              "exit_status": None, "error": None, "cancelled": False, "timed_out": False}
    try:
        transport = client.get_transport()
        if transport is None or not transport.is_active():
            raise ServerManagerError("SSH connection is not active")
        channel = transport.open_session(timeout=SSH_CONNECT_TIMEOUT)
    except Exception as e:
        result["error"] = str(e)[:300]
        return result

    handle = CommandHandle(channel)
    if chunk_cb:
        try:
            chunk_cb(handle, "")
        except Exception:
            logger.debug("chunk_cb raised on initial handle hand-off", exc_info=True)

    stdout_parts, stderr_parts = [], []
    try:
        channel.get_pty()  # so .cancel()'s channel.close() actually kills a foreground process
        channel.exec_command(command)
        start = time.monotonic()

        while True:
            got_data = False
            if channel.recv_ready():
                chunk = channel.recv(4096).decode(errors="ignore")
                if chunk:
                    stdout_parts.append(chunk)
                    got_data = True
                    if chunk_cb:
                        chunk_cb(handle, chunk)
            if channel.recv_stderr_ready():
                chunk = channel.recv_stderr(4096).decode(errors="ignore")
                if chunk:
                    stderr_parts.append(chunk)
                    got_data = True
                    if chunk_cb:
                        chunk_cb(handle, chunk)

            if handle.cancelled:
                result["cancelled"] = True
                break
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                result["exit_status"] = channel.recv_exit_status()
                break
            if time.monotonic() - start > timeout:
                result["timed_out"] = True
                break
            if not got_data:
                time.sleep(STREAM_POLL_INTERVAL)
    except Exception as e:
        result["error"] = str(e)[:300]
    finally:
        try:
            channel.close()
        except Exception:
            pass

    result["stdout"] = "".join(stdout_parts)[-MAX_OUTPUT_CHARS:]
    result["stderr"] = "".join(stderr_parts)[-MAX_OUTPUT_CHARS:]
    result["ok"] = result["exit_status"] == 0
    return result


# ==================================================
# Persistent interactive shell (real terminal behaviour)
#
# run_command_stream() above opens a brand new channel + exec_command per
# message, so shell state (cwd, env vars, and - crucially - a foreground
# program like an interactive menu script that's waiting on stdin) is gone
# the moment one command finishes. That's why sending "20" to answer a menu
# prompt from a previous message looked like typing "20" into a fresh shell
# ("command not found", exit 127) instead of feeding it to the still-running
# program, and why familiar things like the shell prompt never showed up.
#
# open_shell() below opens ONE pty-backed shell (paramiko's invoke_shell)
# per SSH session. Every subsequent message is just sent as a line of
# keystrokes into that same channel - exactly like typing into a real
# terminal - so prompts, banners, and interactive programs all behave
# normally across messages.
# ==================================================
def open_shell(client: "paramiko.SSHClient", term: str = "xterm", width: int = 120, height: int = 32) -> "paramiko.Channel":
    channel = client.invoke_shell(term=term, width=width, height=height)
    channel.settimeout(0.0)
    return channel


def close_shell(channel: Optional["paramiko.Channel"]):
    if channel is None:
        return
    try:
        channel.close()
    except Exception:
        pass


class ShellHandle:
    """Lets a Cancel button interrupt whatever is running in the shell (Ctrl-C)
    without closing the channel - unlike CommandHandle.cancel(), closing here
    would end the whole persistent session, not just the current command."""

    def __init__(self, channel: "paramiko.Channel"):
        self._channel = channel
        self._interrupt_requested = False

    def cancel(self):
        self._interrupt_requested = True
        try:
            self._channel.send(CTRL_C)
        except Exception:
            pass

    @property
    def cancelled(self) -> bool:
        return self._interrupt_requested


def run_shell_input(
    channel: "paramiko.Channel",
    text: str,
    timeout: int = DEFAULT_CMD_TIMEOUT,
    quiet_seconds: float = SHELL_QUIET_SECONDS,
    chunk_cb: Optional[Callable[["ShellHandle", str], None]] = None,
) -> Dict:
    """Send one line of input into an already-open shell (see open_shell()) and
    stream back whatever the pty prints, the same way a real terminal client
    would. There's no single "exit status" for a persistent shell, so instead
    output is streamed until it goes quiet for `quiet_seconds` (the shell/program
    is now idle and waiting on the user again) or `timeout` is hit."""
    result = {"input": text, "output": "", "error": None, "cancelled": False, "timed_out": False}

    handle = ShellHandle(channel)
    if chunk_cb:
        try:
            chunk_cb(handle, "")
        except Exception:
            logger.debug("chunk_cb raised on initial handle hand-off", exc_info=True)

    try:
        channel.send((text or "") + "\n")
    except Exception as e:
        result["error"] = str(e)[:300]
        return result

    parts = []
    start = time.monotonic()
    last_data_at = start
    try:
        while True:
            got_data = False
            if channel.recv_ready():
                chunk = channel.recv(4096).decode(errors="ignore")
                if chunk:
                    parts.append(chunk)
                    got_data = True
                    last_data_at = time.monotonic()
                    if chunk_cb:
                        chunk_cb(handle, chunk)
            if channel.recv_stderr_ready():
                chunk = channel.recv_stderr(4096).decode(errors="ignore")
                if chunk:
                    parts.append(chunk)
                    got_data = True
                    last_data_at = time.monotonic()
                    if chunk_cb:
                        chunk_cb(handle, chunk)

            if channel.closed:
                break
            now = time.monotonic()
            if now - start > timeout:
                result["timed_out"] = True
                break
            if now - last_data_at > quiet_seconds:
                break
            if not got_data:
                time.sleep(STREAM_POLL_INTERVAL)
    except Exception as e:
        result["error"] = str(e)[:300]

    result["output"] = "".join(parts)[-MAX_OUTPUT_CHARS:]
    result["cancelled"] = handle.cancelled
    return result
