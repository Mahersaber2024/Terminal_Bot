import io
import json
import os
import re
import shlex
import stat
import threading
import time
import logging
import subprocess
import uuid
from typing import Callable, Dict, Optional, Tuple

try:
    import paramiko
except ImportError:  # pragma: no cover - surfaced as a clear runtime error instead
    paramiko = None

logger = logging.getLogger(__name__)

SSH_CONNECT_TIMEOUT = 10  # seconds
DEFAULT_CMD_TIMEOUT = int(os.getenv("SERVERMGR_CMD_TIMEOUT", "30"))  # seconds per command
MAX_OUTPUT_CHARS = 3500  # keep replies under Telegram's message size limit
STREAM_POLL_INTERVAL = 0.15  # seconds between recv_ready()/cancel checks while a command is running
CTRL_C = "\x03"

_SHELL_PROMPT_MARKER = f"@@SM_READY_{uuid.uuid4().hex[:10]}@@"
_SHELL_PROMPT_SETUP_CMD = f"unset PROMPT_COMMAND 2>/dev/null; PS1='{_SHELL_PROMPT_MARKER}'\n"
_PROMPT_PRIME_TIMEOUT = 10  # seconds to wait for the very first prompt right after opening the shell


class ServerManagerError(Exception):
    pass


class HostKeyChangedError(ServerManagerError):
    """Raised when a server's SSH host key doesn't match what we pinned on a
    previous connection - either the server was reinstalled/rekeyed, or this
    is a man-in-the-middle attempt. Either way, we refuse to connect silently."""
    pass

KNOWN_HOSTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "known_hosts.json")
_known_hosts_lock = threading.Lock()
_known_hosts_cache = None


def _load_known_hosts() -> dict:
    global _known_hosts_cache
    with _known_hosts_lock:
        if _known_hosts_cache is not None:
            return _known_hosts_cache
        data = {}
        if os.path.exists(KNOWN_HOSTS_FILE):
            try:
                with open(KNOWN_HOSTS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                logger.error(f"Error loading known_hosts.json: {e}")
                data = {}
        _known_hosts_cache = data
        return data


def _save_known_hosts(data: dict):
    global _known_hosts_cache
    with _known_hosts_lock:
        with open(KNOWN_HOSTS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        _known_hosts_cache = data


def _host_entry_key(host: str, port: int) -> str:
    return f"{host}:{port}"


def forget_host_key(host: str, port: int = 22) -> bool:
    """Removes a pinned host key so the next connection re-pins whatever key
    the server presents. Used after a human has manually verified that a
    HostKeyChangedError is expected (e.g. the server was reinstalled)."""
    entry = _host_entry_key(host, int(port or 22))
    data = dict(_load_known_hosts())
    if entry in data:
        del data[entry]
        _save_known_hosts(data)
        return True
    return False


class _PinningHostKeyPolicy:
    """paramiko.MissingHostKeyPolicy implementation providing TOFU pinning."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = int(port or 22)

    def missing_host_key(self, client, hostname, key):
        entry = _host_entry_key(self.host, self.port)
        key_type = key.get_name()
        fingerprint = key.get_fingerprint().hex()
        data = _load_known_hosts()
        stored = data.get(entry)

        if stored is None:
            # First time we've ever connected to this host:port - trust it and
            # remember the key so future connections can be compared against it.
            data = dict(data)
            data[entry] = {"key_type": key_type, "fingerprint": fingerprint}
            _save_known_hosts(data)
            logger.info(f"Pinned new SSH host key for {entry} ({key_type} {fingerprint})")
            return

        if stored.get("key_type") != key_type or stored.get("fingerprint") != fingerprint:
            raise HostKeyChangedError(
                f"The SSH host key for {self.host}:{self.port} has changed since it was first "
                f"trusted!\nExpected: {stored.get('key_type')} {stored.get('fingerprint')}\n"
                f"Received: {key_type} {fingerprint}\n\n"
                f"This can happen if the server was reinstalled or its host key was rotated - "
                f"but it's also exactly what a man-in-the-middle attack looks like. Only proceed "
                f"if you're sure the change is expected."
            )

def _load_private_key(key_text: str, passphrase: Optional[str] = None) -> "paramiko.PKey":
    if paramiko is None:
        raise ServerManagerError("paramiko is not installed. Run: pip install paramiko")
    key_classes = [paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey, paramiko.DSSKey]
    last_error = None
    for cls in key_classes:
        try:
            return cls.from_private_key(io.StringIO(key_text), password=(passphrase or None))
        except Exception as e:
            last_error = e
            continue
    raise ServerManagerError(
        f"Could not load this private key - it may be in an unsupported format, corrupted, "
        f"or the passphrase may be wrong ({last_error})"
    )


def try_load_private_key(key_text: str, passphrase: Optional[str] = None) -> Tuple[Optional["paramiko.PKey"], Optional[str]]:
    """Non-raising variant used to validate a key (and passphrase) before saving it,
    so a typo doesn't only surface later when the user tries to actually connect."""
    try:
        return _load_private_key(key_text, passphrase), None
    except ServerManagerError as e:
        return None, str(e)


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

    host = server["host"]
    port = int(server.get("port", 22) or 22)

    client = paramiko.SSHClient()
    # TOFU host-key pinning (see _PinningHostKeyPolicy above) instead of
    # AutoAddPolicy, which used to accept any host key silently - including
    # one swapped in by a man-in-the-middle - without ever checking it again.
    client.set_missing_host_key_policy(_PinningHostKeyPolicy(host, port))

    connect_kwargs = dict(
        hostname=host,
        port=port,
        username=server["username"],
        timeout=SSH_CONNECT_TIMEOUT,
        banner_timeout=SSH_CONNECT_TIMEOUT,
        auth_timeout=SSH_CONNECT_TIMEOUT,
        look_for_keys=False,
        allow_agent=False,
    )

    if server.get("private_key"):
        connect_kwargs["pkey"] = _load_private_key(server["private_key"], server.get("key_passphrase") or None)
    else:
        connect_kwargs["password"] = server.get("password", "")

    client.connect(**connect_kwargs)
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
    _prime_shell_prompt(channel)
    return channel


def _prime_shell_prompt(channel: "paramiko.Channel", timeout: float = _PROMPT_PRIME_TIMEOUT):
    """Sets the unique PS1 marker on a freshly-opened shell and waits for it to
    print once, swallowing whatever login banner/MOTD comes before it. Best
    effort only: on the rare shell that won't accept a PS1 assignment (or is
    too slow), we just give up silently and run_shell_input() falls back to
    relying on its hard `timeout` instead of ever recognizing "done"."""
    try:
        channel.send(_SHELL_PROMPT_SETUP_CMD)
    except Exception:
        return
    buf = ""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if channel.recv_ready():
            try:
                buf += channel.recv(4096).decode(errors="ignore")
            except Exception:
                return
            if _SHELL_PROMPT_MARKER in buf:
                return
        else:
            time.sleep(STREAM_POLL_INTERVAL)


def close_shell(channel: Optional["paramiko.Channel"]):
    if channel is None:
        return
    try:
        channel.close()
    except Exception:
        pass


class ShellHandle:
    """Lets a live button (Cancel, Enter, ...) inject keystrokes into whatever
    is running in the shell right now, without closing the channel - unlike
    CommandHandle.cancel(), closing here would end the whole persistent
    session, not just the current command."""

    def __init__(self, channel: "paramiko.Channel"):
        self._channel = channel
        self._interrupt_requested = False

    def send_raw(self, data: str) -> bool:
        """Injects raw bytes into the channel mid-stream - e.g. a bare Enter
        for a program that's paused waiting on one (a pager, a "press any
        key" prompt, a confirmation with an empty default). The already-
        running run_shell_input() loop picks up whatever this produces on
        its own, same as any other output."""
        try:
            self._channel.send(data)
            return True
        except Exception:
            return False

    def cancel(self):
        self._interrupt_requested = True
        self.send_raw(CTRL_C)

    @property
    def cancelled(self) -> bool:
        return self._interrupt_requested


def run_shell_input(
    channel: "paramiko.Channel",
    text: str,
    timeout: int = DEFAULT_CMD_TIMEOUT,
    chunk_cb: Optional[Callable[["ShellHandle", str], None]] = None,
) -> Dict:
    """Send one line of input into an already-open shell (see open_shell()) and
    stream back whatever the pty prints, the same way a real terminal client
    would. There's no single "exit status" for a persistent shell, so instead
    output is streamed until the shell's own prompt marker (see
    _prime_shell_prompt()) reappears - meaning bash has genuinely finished and
    is reading input again, not just paused for a moment or waiting silently
    on some other program's prompt - or until the hard `timeout` is hit."""
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
    tail = ""  # last chunk decoded so far, used to check for the marker without re-scanning the whole buffer each time
    start = time.monotonic()
    try:
        while True:
            got_data = False
            if channel.recv_ready():
                chunk = channel.recv(4096).decode(errors="ignore")
                if chunk:
                    parts.append(chunk)
                    tail = (tail + chunk)[-len(_SHELL_PROMPT_MARKER) * 2:]
                    got_data = True
                    if chunk_cb:
                        chunk_cb(handle, chunk)
            if channel.recv_stderr_ready():
                chunk = channel.recv_stderr(4096).decode(errors="ignore")
                if chunk:
                    parts.append(chunk)
                    tail = (tail + chunk)[-len(_SHELL_PROMPT_MARKER) * 2:]
                    got_data = True
                    if chunk_cb:
                        chunk_cb(handle, chunk)

            if _SHELL_PROMPT_MARKER in tail:
                break
            if channel.closed:
                break
            now = time.monotonic()
            if now - start > timeout:
                result["timed_out"] = True
                break
            if not got_data:
                time.sleep(STREAM_POLL_INTERVAL)
    except Exception as e:
        result["error"] = str(e)[:300]

    output = "".join(parts).replace(_SHELL_PROMPT_MARKER, "")
    result["output"] = output[-MAX_OUTPUT_CHARS:]
    result["cancelled"] = handle.cancelled
    return result


# ==================================================
# Health snapshot (CPU / RAM / Disk / uptime) - powers both the on-demand
# "🩺 Health Check" button and the background monitor in
# server_manager_health.py. Deliberately just standard coreutils/procps
# (awk, free, df, /proc) - nothing to install on the target server, and it
# works the same over plain "sh" as it does over bash.
# ==================================================
_HEALTH_SNAPSHOT_CMD = """
CPU1_IDLE=$(awk '/^cpu /{print $5}' /proc/stat)
CPU1_TOTAL=$(awk '/^cpu /{s=0; for(i=2;i<=NF;i++) s+=$i; print s}' /proc/stat)
sleep 0.3
CPU2_IDLE=$(awk '/^cpu /{print $5}' /proc/stat)
CPU2_TOTAL=$(awk '/^cpu /{s=0; for(i=2;i<=NF;i++) s+=$i; print s}' /proc/stat)
CPU_PCT=$(awk -v i1="$CPU1_IDLE" -v t1="$CPU1_TOTAL" -v i2="$CPU2_IDLE" -v t2="$CPU2_TOTAL" 'BEGIN{dt=t2-t1; di=i2-i1; if (dt>0) printf "%.1f", (1-di/dt)*100; else print "0.0"}')
MEM_LINE=$(free -m | awk '/^Mem:/{printf "%d %d", $3, $2}')
DISK_LINE=$(df -kP / | awk 'NR==2{printf "%d %d %s", $3, $2, $5}')
UPTIME_S=$(awk '{printf "%d", $1}' /proc/uptime)
echo "CPU=$CPU_PCT"
echo "MEM=$MEM_LINE"
echo "DISK=$DISK_LINE"
echo "UPTIME=$UPTIME_S"
""".strip()


def _parse_health_output(stdout: str) -> Dict:
    values = {}
    for line in stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            values[k.strip()] = v.strip()

    out = {
        "cpu_percent": None, "mem_used_mb": None, "mem_total_mb": None,
        "disk_used_kb": None, "disk_total_kb": None, "disk_percent": None,
        "uptime_seconds": None,
    }
    try:
        if "CPU" in values:
            out["cpu_percent"] = float(values["CPU"])
        if "MEM" in values:
            used, total = values["MEM"].split()
            out["mem_used_mb"], out["mem_total_mb"] = int(used), int(total)
        if "DISK" in values:
            used_kb, total_kb, pct = values["DISK"].split()
            out["disk_used_kb"], out["disk_total_kb"] = int(used_kb), int(total_kb)
            out["disk_percent"] = int(pct.rstrip("%"))
        if "UPTIME" in values:
            out["uptime_seconds"] = int(float(values["UPTIME"]))
    except Exception as e:
        logger.debug(f"health snapshot output didn't parse as expected: {e}")
    return out


def check_health(server: Dict, timeout: int = 10) -> Dict:
    """One-shot CPU/RAM/Disk/uptime snapshot. Opens and closes its own
    short-lived connection - never reuses a live terminal/file-browser
    session - so a health check can never interfere with (or be interfered
    with by) something the user is actively doing in an open tab."""
    result = {
        "ok": False, "error": None, "cpu_percent": None, "mem_used_mb": None,
        "mem_total_mb": None, "disk_used_kb": None, "disk_total_kb": None,
        "disk_percent": None, "uptime_seconds": None,
    }
    try:
        client = connect(server)
    except Exception as e:
        result["error"] = str(e)[:300]
        return result
    try:
        cmd_result = run_command(client, _HEALTH_SNAPSHOT_CMD, timeout=timeout)
    finally:
        try:
            client.close()
        except Exception:
            pass

    if cmd_result.get("error"):
        result["error"] = cmd_result["error"]
        return result
    if not cmd_result.get("ok"):
        stderr_tail = (cmd_result.get("stderr") or "").strip()[-300:]
        result["error"] = stderr_tail or f"health check exited with status {cmd_result.get('exit_status')}"
        return result

    result.update(_parse_health_output(cmd_result.get("stdout", "")))
    result["ok"] = True
    return result

SFTP_MAX_DOWNLOAD_BYTES = 45 * 1024 * 1024  # stay under Telegram bots' ~50MB send-document limit
SFTP_MAX_UPLOAD_BYTES = 19 * 1024 * 1024    # stay under Telegram bots' ~20MB file-download limit


def open_sftp(client: "paramiko.SSHClient") -> "paramiko.SFTPClient":
    transport = client.get_transport()
    if transport is None or not transport.is_active():
        raise ServerManagerError("SSH connection is not active")
    return client.open_sftp()


def close_sftp(sftp: Optional["paramiko.SFTPClient"]):
    if sftp is None:
        return
    try:
        sftp.close()
    except Exception:
        pass


def sftp_home_dir(sftp: "paramiko.SFTPClient") -> str:
    """The directory a fresh SFTP session lands in - normally the user's
    home directory - used as the starting point for browsing."""
    try:
        return sftp.normalize(".")
    except Exception:
        return "/"


def sftp_listdir(sftp: "paramiko.SFTPClient", path: str) -> list:
    """Directory listing, folders first then alphabetically, as
    [{"name", "is_dir", "size"}, ...]."""
    entries = []
    for attr in sftp.listdir_attr(path):
        is_dir = stat.S_ISDIR(attr.st_mode or 0)
        entries.append({"name": attr.filename, "is_dir": is_dir, "size": attr.st_size or 0})
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return entries


def sftp_join(cwd: str, name: str) -> str:
    return f"{cwd.rstrip('/')}/{name}" if cwd != "/" else f"/{name}"


def sftp_parent(cwd: str) -> str:
    if cwd in ("/", ""):
        return "/"
    parent = cwd.rstrip("/").rsplit("/", 1)[0]
    return parent or "/"


def sftp_normalize(sftp: "paramiko.SFTPClient", path: str) -> str:
    """Resolves a manually-typed path (relative, with '..', a bare '~', etc.)
    to the canonical absolute path the server reports - so 'go to path' ends
    up with the same kind of cwd value that browsing by tapping folders does."""
    return sftp.normalize(path)


def sftp_download(sftp: "paramiko.SFTPClient", remote_path: str, local_path: str):
    sftp.get(remote_path, local_path)


def sftp_upload(sftp: "paramiko.SFTPClient", local_path: str, remote_path: str):
    sftp.put(local_path, remote_path)


def sftp_rename(sftp: "paramiko.SFTPClient", old_path: str, new_path: str):
    """Renames/moves a file or folder in one step - works for both since
    SFTP's rename() doesn't care which it is, only that the destination
    doesn't already exist (most servers reject overwriting silently)."""
    sftp.rename(old_path, new_path)


def sftp_delete_recursive(sftp: "paramiko.SFTPClient", path: str, is_dir: bool):
    """Deletes a single file, or a folder and everything inside it. SFTP's
    rmdir() only works on an already-empty directory, so for a folder this
    walks it depth-first - removing every file and recursing into every
    subdirectory - before finally removing the (now-empty) folder itself."""
    if not is_dir:
        sftp.remove(path)
        return
    for attr in sftp.listdir_attr(path):
        child = sftp_join(path, attr.filename)
        if stat.S_ISDIR(attr.st_mode or 0):
            sftp_delete_recursive(sftp, child, True)
        else:
            sftp.remove(child)
    sftp.rmdir(path)


# ==================================================
# In-chat text editor (nano-style: view the whole file, retype it, save) -
# reuses the same SFTP session as browsing above. Deliberately capped well
# under Telegram's ~4096-character message limit so a file's entire content,
# plus the header/instructions wrapped around it, always fits on one screen
# with no pagination needed.
# ==================================================
SFTP_EDITOR_MAX_BYTES = 3500


def sftp_read_text(sftp: "paramiko.SFTPClient", path: str, max_bytes: int = SFTP_EDITOR_MAX_BYTES) -> str:
    """Reads a file in full for the in-chat editor. Raises ServerManagerError
    if it's too big to fit on one screen or isn't valid UTF-8 text - either
    way the caller should fall back to a regular download instead of trying
    to open it here."""
    with sftp.open(path, "rb") as f:
        data = f.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ServerManagerError(f"File is over {max_bytes} bytes - too big to edit here. Download it instead.")
    if b"\x00" in data:
        raise ServerManagerError("File looks like binary data, not text - editing here isn't supported.")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ServerManagerError("File isn't valid UTF-8 text - editing here isn't supported.")


def sftp_write_text(sftp: "paramiko.SFTPClient", path: str, content: str):
    """Overwrites a file with new text content (saved as UTF-8)."""
    with sftp.open(path, "wb") as f:
        f.write(content.encode("utf-8"))

def remote_download_url(client: "paramiko.SSHClient", cwd: str, filename: str, url: str, timeout: int = 600) -> Dict:
    result = {"ok": False, "stdout": "", "stderr": "", "exit_status": None, "error": None}
    command = (
        f"cd {shlex.quote(cwd)} && "
        f"(command -v wget >/dev/null 2>&1 && wget -q -O {shlex.quote(filename)} {shlex.quote(url)} "
        f"|| curl -fsSL -o {shlex.quote(filename)} {shlex.quote(url)})"
    )
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
