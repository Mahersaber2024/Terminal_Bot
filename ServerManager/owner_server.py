"""ServerManager/owner_server.py The "Owner Server" - the host machine this bot process is itself running on."""
import fcntl
import grp
import logging
import os
import platform
import pty
import pwd
import re
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tarfile
import termios
import threading
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import datetime
from typing import Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from . import engine as _engine
from . import maintenance as _maintenance
import bot_settings

logger = logging.getLogger(__name__)

_PROC_START_TIME = time.time()
_HZ = 100
try:
    _HZ = os.sysconf("SC_CLK_TCK")
except (AttributeError, ValueError, OSError):
    pass


# ====================== Low-level /proc readers ======================

def _read(path: str) -> str:
    try:
        with open(path, "r") as f:
            return f.read()
    except Exception:
        return ""


def _cpu_times():
    for line in _read("/proc/stat").splitlines():
        if line.startswith("cpu "):
            parts = [int(x) for x in line.split()[1:]]
            idle = parts[3] + (parts[4] if len(parts) > 4 else 0)  # idle + iowait
            total = sum(parts)
            return idle, total
    return None, None


def cpu_percent(interval: float = 0.3) -> Optional[float]:
    """Same idle/total delta-over-a-beat approach as engine._HEALTH_SNAPSHOT_CMD uses remotely - a single /proc/stat read only gives cumulative ticks since boot, which isn't a percentage on its own."""
    i1, t1 = _cpu_times()
    if i1 is None:
        return None
    time.sleep(interval)
    i2, t2 = _cpu_times()
    dt, di = t2 - t1, i2 - i1
    if dt <= 0:
        return 0.0
    return round((1 - di / dt) * 100, 1)


def cpu_count() -> int:
    return os.cpu_count() or 1


def _proc_cpu_ticks(pid: int) -> Optional[int]:
    stat = _read(f"/proc/{pid}/stat")
    if not stat:
        return None
    try:
        rest = stat[stat.rindex(")") + 2:].split()
        return int(rest[11]) + int(rest[12])  # utime + stime, in clock ticks
    except Exception:
        return None


def process_cpu_percent(pid: int, interval: float = 0.3) -> Optional[float]:
    """Same delta-over-a-beat technique as cpu_percent() above, scoped to one
    process's own utime+stime ticks instead of the whole host's /proc/stat."""
    t1 = _proc_cpu_ticks(pid)
    if t1 is None:
        return None
    time.sleep(interval)
    t2 = _proc_cpu_ticks(pid)
    if t2 is None:
        return None
    return round(max(0.0, (t2 - t1) / _HZ / interval * 100), 1)


def load_average() -> Optional[tuple]:
    try:
        return os.getloadavg()
    except (AttributeError, OSError):
        return None


def memory() -> Dict:
    data = {}
    for line in _read("/proc/meminfo").splitlines():
        m = re.match(r"(\w+):\s+(\d+)", line)
        if m:
            data[m.group(1)] = int(m.group(2))  # kB
    total = data.get("MemTotal", 0)
    free = data.get("MemFree", 0)
    buffers = data.get("Buffers", 0)
    cached = data.get("Cached", 0)
    available = data.get("MemAvailable", free + buffers + cached)
    used = max(0, total - available)
    swap_total = data.get("SwapTotal", 0)
    swap_free = data.get("SwapFree", 0)
    swap_used = max(0, swap_total - swap_free)
    return {
        "total_mb": round(total / 1024, 1),
        "used_mb": round(used / 1024, 1),
        "percent": round(used / total * 100, 1) if total else None,
        "swap_total_mb": round(swap_total / 1024, 1),
        "swap_used_mb": round(swap_used / 1024, 1),
        "swap_percent": round(swap_used / swap_total * 100, 1) if swap_total else None,
    }


def _candidate_mounts() -> List[str]:
    """/ plus any of the common separately-mounted paths that actually exist as real mount points on this host - so the card only shows disks that are meaningfully distinct from each other."""
    candidates = ["/", "/home", "/var", "/data", "/mnt"]
    out = []
    for m in candidates:
        if m == "/" or os.path.ismount(m):
            out.append(m)
    return out


def disk_usage(paths: List[str] = None) -> List[Dict]:
    out = []
    for p in (paths or _candidate_mounts()):
        try:
            usage = shutil.disk_usage(p)
        except Exception:
            continue
        used = usage.total - usage.free
        out.append({
            "path": p,
            "total_gb": round(usage.total / (1024 ** 3), 1),
            "used_gb": round(used / (1024 ** 3), 1),
            "percent": round(used / usage.total * 100, 1) if usage.total else None,
        })
    return out


def uptime_seconds() -> Optional[int]:
    try:
        return int(float(_read("/proc/uptime").split()[0]))
    except Exception:
        return None


def network_io() -> Dict:
    """Cumulative rx/tx bytes since boot, summed across every non-loopback interface."""
    rx_total = tx_total = 0
    for line in _read("/proc/net/dev").splitlines()[2:]:
        if ":" not in line:
            continue
        iface, rest = line.split(":", 1)
        if iface.strip() == "lo":
            continue
        fields = rest.split()
        try:
            rx_total += int(fields[0])
            tx_total += int(fields[8])
        except (IndexError, ValueError):
            continue
    return {"rx_bytes": rx_total, "tx_bytes": tx_total}


def hostname() -> str:
    return socket.gethostname()


def server_time_info(tz_name: str = None) -> Dict:
    """Current date/time on the host, rendered in whichever timezone the admin has configured (bot_settings.get_owner_timezone(), default UTC) - a display-only setting, independent of whatever TZ the OS itself is actually set to."""
    tz_name = tz_name or bot_settings.get_owner_timezone()
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz_name, tz = "UTC", ZoneInfo("UTC")
    now = datetime.now(tz)
    offset = now.utcoffset()
    total_minutes = int(offset.total_seconds() // 60) if offset else 0
    sign = "+" if total_minutes >= 0 else "-"
    hh, mm = divmod(abs(total_minutes), 60)
    return {
        "timezone": tz_name,
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "weekday": now.strftime("%A"),
        "utc_offset": f"UTC{sign}{hh:02d}:{mm:02d}",
    }


def _open_fd_count(pid: int) -> Optional[int]:
    try:
        return len(os.listdir(f"/proc/{pid}/fd"))
    except Exception:
        return None


def bot_process_stats() -> Dict:
    """Footprint of the bot's own process, separate from the host-wide numbers - lets an admin tell "the host is under pressure" apart from "the bot itself is the one leaking memory"."""
    pid = os.getpid()
    result = {
        "pid": pid, "rss_mb": None, "vms_mb": None, "threads": None,
        "cpu_percent": None, "open_fds": None,
        "started_ago_seconds": int(time.time() - _PROC_START_TIME),
    }
    status = _read(f"/proc/{pid}/status")
    m = re.search(r"VmRSS:\s+(\d+)", status)
    if m:
        result["rss_mb"] = round(int(m.group(1)) / 1024, 1)
    m = re.search(r"VmSize:\s+(\d+)", status)
    if m:
        result["vms_mb"] = round(int(m.group(1)) / 1024, 1)
    m = re.search(r"Threads:\s+(\d+)", status)
    if m:
        result["threads"] = int(m.group(1))
    result["open_fds"] = _open_fd_count(pid)
    # last, since it sleeps ~0.3s to measure a delta - same as cpu_percent() for the host
    result["cpu_percent"] = process_cpu_percent(pid)
    return result


_WCHAN_LABELS = {
    "poll_schedule_timeout": "waiting (select/poll)",
    "do_sys_poll": "waiting (poll)",
    "ep_poll": "waiting (epoll)",
    "futex_wait_queue_me": "waiting (lock)",
    "sk_wait_data": "waiting (socket read)",
    "inet_csk_accept": "waiting (accepting connection)",
    "unix_stream_read_generic": "waiting (unix socket read)",
    "hrtimer_nanosleep": "sleeping (timer)",
    "pipe_read": "waiting (pipe)",
}

_STATE_LABELS = {
    "R": "running", "S": "sleeping", "D": "waiting on I/O",
    "Z": "zombie", "T": "stopped",
}


def _thread_cpu_ticks(pid: int, tid: int) -> Optional[int]:
    stat = _read(f"/proc/{pid}/task/{tid}/stat")
    if not stat:
        return None
    try:
        rest = stat[stat.rindex(")") + 2:].split()
        return int(rest[11]) + int(rest[12])  # utime + stime, in clock ticks
    except Exception:
        return None


def bot_thread_details(interval: float = 0.3) -> List[Dict]:
    """Per-thread breakdown of the bot's own process, read straight from
    /proc/<pid>/task/<tid>/. Threads created via Python's `threading` module
    don't get a custom OS-level name (comm) unless the code explicitly calls
    prctl(PR_SET_NAME), so `name` will usually be the same interpreter name
    for every thread - `state` and `wchan` are the useful signal instead,
    since they show whether a thread is actually running or just parked
    waiting on something (a socket read, a lock, a timer, ...).

    `cpu_percent` is sampled per-thread (two ticks-snapshots `interval`
    seconds apart, same delta technique as process_cpu_percent()) since CPU
    time is the one resource threads don't share. RAM/RSS is deliberately
    NOT reported per-thread: all threads of a process share the same
    address space, so a per-thread RSS number would just repeat the whole
    process's total under every thread rather than showing anything real -
    callers should use bot_process_stats()['rss_mb'] for that instead."""
    pid = os.getpid()
    task_dir = f"/proc/{pid}/task"
    try:
        tids = sorted(int(t) for t in os.listdir(task_dir) if t.isdigit())
    except Exception:
        return []

    ticks1 = {tid: _thread_cpu_ticks(pid, tid) for tid in tids}
    time.sleep(interval)

    out = []
    for tid in tids:
        base = f"{task_dir}/{tid}"
        comm = _read(f"{base}/comm").strip()
        stat = _read(f"{base}/stat")
        state = None
        if stat:
            try:
                rest = stat[stat.rindex(")") + 2:].split()
                state = rest[0]
            except Exception:
                pass
        wchan = _read(f"{base}/wchan").strip()

        cpu_percent = None
        t1 = ticks1.get(tid)
        t2 = _thread_cpu_ticks(pid, tid)
        if t1 is not None and t2 is not None:
            cpu_percent = round(max(0.0, (t2 - t1) / _HZ / interval * 100), 1)

        out.append({
            "tid": tid,
            "is_main": tid == pid,
            "name": comm or "?",
            "state": state,
            "wchan": wchan or None,
            "cpu_percent": cpu_percent,
        })
    return out


def format_bot_threads_text(threads: List[Dict]) -> str:
    """Renders bot_thread_details() output as a readable per-thread list."""
    lines = [_section("Bot threads")]
    if not threads:
        lines.append("(couldn't read /proc/<pid>/task)")
        return "\n".join(lines)
    for t in threads:
        state = _STATE_LABELS.get(t["state"], t["state"] or "?")
        tag = "main" if t["is_main"] else "worker"
        wchan = t["wchan"]
        detail = None
        if wchan and wchan != "0":
            detail = _WCHAN_LABELS.get(wchan, wchan)
        line = f"`{t['tid']}` ({tag}) — {state}"
        if detail:
            line += f", {detail}"
        if t.get("cpu_percent") is not None:
            line += f" · {t['cpu_percent']:.1f}% CPU"
        lines.append(line)
    return "\n".join(lines)


def _iter_processes():
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        stat = _read(f"/proc/{pid}/stat")
        if not stat:
            continue
        try:
            comm = stat[stat.index("(") + 1:stat.rindex(")")]
            rest = stat[stat.rindex(")") + 2:].split()
            utime, stime = int(rest[11]), int(rest[12])
        except Exception:
            continue
        status = _read(f"/proc/{pid}/status")
        m = re.search(r"VmRSS:\s+(\d+)", status)
        rss_mb = round(int(m.group(1)) / 1024, 1) if m else 0.0
        yield {"pid": pid, "name": comm, "cpu_ticks": utime + stime, "rss_mb": rss_mb}


def top_processes(by: str = "cpu", limit: int = 8, interval: float = 0.3) -> List[Dict]:
    """top(1)-style CPU sampling: a single /proc/<pid>/stat read only gives cumulative CPU ticks since the process started, so this takes two snapshots a beat apart and reports the delta as a percentage."""
    first = {p["pid"]: p for p in _iter_processes()}
    time.sleep(interval)
    rows = []
    for p in _iter_processes():
        prev = first.get(p["pid"])
        delta_ticks = (p["cpu_ticks"] - prev["cpu_ticks"]) if prev else 0
        cpu_pct = round(delta_ticks / _HZ / interval * 100, 1) if _HZ else 0.0
        rows.append({"pid": p["pid"], "name": p["name"], "cpu_percent": max(0.0, cpu_pct), "rss_mb": p["rss_mb"]})
    key = "cpu_percent" if by == "cpu" else "rss_mb"
    rows.sort(key=lambda r: r[key], reverse=True)
    return rows[:limit]


# ====================== Full snapshot ======================

def snapshot() -> Dict:
    """One-shot health snapshot of the host - the local equivalent of engine.check_health() for a remote server, minus the SSH round trip."""
    return {
        "ok": True,
        "hostname": hostname(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu_percent": cpu_percent(),
        "cpu_count": cpu_count(),
        "load_average": load_average(),
        "memory": memory(),
        "disks": disk_usage(),
        "network": network_io(),
        "uptime_seconds": uptime_seconds(),
        "bot_process": bot_process_stats(),
        "time_info": server_time_info(),
    }


# ====================== Formatting ======================

def _bar(pct, width: int = 10) -> str:
    if pct is None:
        return "?"
    filled = min(width, max(0, round(pct / 100 * width)))
    return "■" * filled + "□" * (width - filled)


def _fmt_uptime(seconds) -> str:
    if not seconds:
        return "-"
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if not days and minutes:
        parts.append(f"{minutes}m")
    return " ".join(parts) or "<1m"


def _fmt_bytes(n) -> str:
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


_MD_SPECIAL_RE = re.compile(r"([_*`\[])")


def _md_escape(text: str) -> str:
    """Escapes legacy-Markdown special chars (_ * ` [) in a dynamic value
    (hostname, platform string, a path, ...) before it's dropped into the
    message. Needed now that the status text is no longer wrapped in a
    ``` code fence - Telegram used to skip parsing everything in there for
    free, so something as ordinary as "x86_64" in platform.platform() could
    otherwise be read as an unterminated italic entity and reject the whole
    message."""
    return _MD_SPECIAL_RE.sub(r"\\\1", text)


def _section(title: str) -> str:
    return f"── {title.upper()} ──"


def format_snapshot_text(snap: Dict) -> str:
    """Status message with dash-style section headers. Values that benefit
    from being tap-to-copy (the platform string, the PID) are wrapped in
    backticks, and the whole RESOURCES block goes back inside a ``` fence
    so its columns line up and the block is copyable as one piece. A
    backtick span (or a ``` fence) already shields its contents from
    Telegram's Markdown parser, so nothing inside either needs escaping -
    only the bare hostname line still needs it."""
    mem = snap["memory"]
    ti = snap.get("time_info")

    lines = [
        f"▣ {_md_escape(snap['hostname'])}",
        f"◉ Online · up {_fmt_uptime(snap['uptime_seconds'])}",
        "",
        _section("System"),
        f"`{snap['platform']}`",
    ]
    if ti:
        lines.append(f"▤ {ti['date']} ({ti['weekday']}) {ti['time']}")
        lines.append(f"⊚ {ti['timezone']} ({ti['utc_offset']})")

    lines += ["", _section("Resources")]
    resource_rows = []
    cpu = snap["cpu_percent"]
    resource_rows.append(
        f"CPU   {_bar(cpu)} {cpu:>3.0f}% ({snap['cpu_count']} core{'s' if snap['cpu_count'] != 1 else ''})"
        if cpu is not None else "CPU   -"
    )
    la = snap["load_average"]
    if la:
        resource_rows.append(f"Load  {la[0]:.2f} / {la[1]:.2f} / {la[2]:.2f}")
    resource_rows.append(
        f"RAM   {_bar(mem['percent'])} {mem['percent']:>3.0f}% {mem['used_mb']:.0f}/{mem['total_mb']:.0f} MB"
        if mem.get("percent") is not None else "RAM   -"
    )
    if mem.get("swap_total_mb"):
        resource_rows.append(
            f"Swap  {_bar(mem['swap_percent'])} {mem['swap_percent']:>3.0f}% "
            f"{mem['swap_used_mb']:.0f}/{mem['swap_total_mb']:.0f} MB"
        )
    for d in snap["disks"]:
        label = d["path"] if len(d["path"]) <= 5 else d["path"][:5]
        field = f"Disk {label}"
        if len(field) < 6:
            field += " " * (6 - len(field))
        resource_rows.append(
            f"{field}{_bar(d['percent'])} {d['percent']:>3.0f}% "
            f"{d['used_gb']:.1f}/{d['total_gb']:.1f} GB"
        )
    lines += [f"`{row}`" for row in resource_rows]

    net = snap["network"]
    lines += ["", _section("Network"), f"↓ {_fmt_bytes(net['rx_bytes'])} ↑ {_fmt_bytes(net['tx_bytes'])}"]

    bp = snap["bot_process"]
    lines += ["", _section("Bot"), f"PID `{bp['pid']}` · up {_fmt_uptime(bp['started_ago_seconds'])}"]
    bot_rows = []
    bot_rows.append(
        f"CPU   {_bar(bp['cpu_percent'])} {bp['cpu_percent']:>3.0f}%"
        if bp.get("cpu_percent") is not None else "CPU   -"
    )
    host_ram_pct = (bp["rss_mb"] / mem["total_mb"] * 100) if bp.get("rss_mb") and mem.get("total_mb") else None
    bot_rows.append(
        f"RAM   {_bar(host_ram_pct)} {host_ram_pct:>3.0f}% {bp['rss_mb']:.1f} MB of host"
        if host_ram_pct is not None else "RAM   -"
    )
    bot_rows.append(f"VMS {bp['vms_mb']:.0f}MB · Threads {bp['threads']} · FDs {bp['open_fds']}")
    lines += [f"`{row}`" for row in bot_rows]

    return "\n".join(lines)


def format_top_processes_text(rows: List[Dict], by: str) -> str:
    label = "CPU" if by == "cpu" else "RAM"
    lines = [f"▤ *Top processes by {label}*", ""]
    if not rows:
        lines.append("(nothing to show)")
        return "\n".join(lines)
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. `{r['pid']}` {r['name']} — {r['cpu_percent']:.1f}% CPU, {r['rss_mb']:.0f}MB RAM")
    return "\n".join(lines)


# ====================== Actions ======================

def restart_bot(delay: float = 1.5) -> Dict:
    """Restarts the bot process itself."""
    service = os.getenv("OWNER_SERVICE_NAME", "").strip()

    def _do_restart():
        time.sleep(delay)
        if service:
            try:
                subprocess.Popen(["systemctl", "restart", service])
                return
            except Exception as e:
                logger.error(f"systemctl restart {service} failed, falling back to re-exec: {e}")
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            logger.error(f"owner_server restart re-exec failed: {e}")

    threading.Thread(target=_do_restart, daemon=True).start()
    return {"ok": True, "method": f"systemctl restart {service}" if service else "re-exec"}


def reboot_host(timeout: int = None) -> Dict:
    """Reboots the Owner Server host machine itself - the same nohup-detached
    `reboot` maintenance.restart_server() schedules on a remote server, just run
    locally with no SSH hop. Distinct from restart_bot() above, which only
    restarts the bot process and leaves the host running."""
    timeout = timeout or _maintenance.REBOOT_TIMEOUT
    result = {"ok": False, "error": None}
    try:
        proc = subprocess.run(
            ["bash", "-c", _maintenance._REBOOT_CMD],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        result["error"] = f"reboot command timed out after {timeout}s"
        return result
    except Exception as e:
        result["error"] = str(e)[:300]
        return result

    if proc.returncode != 0:
        stderr_tail = (proc.stderr or "").strip()[-300:]
        result["error"] = stderr_tail or f"reboot command exited with status {proc.returncode}"
        return result

    result["ok"] = True
    return result


# ====================== Local terminal (arbitrary command execution) ======================
# Host-side equivalent of engine.run_command(), run via local subprocess.
LOCAL_CMD_TIMEOUT = 30          # seconds
LOCAL_CMD_MAX_OUTPUT = 3500     # Telegram message size limit


def run_local_command(command: str, timeout: int = LOCAL_CMD_TIMEOUT) -> Dict:
    """Runs one shell command on the host and returns immediately - no persistent shell state between calls."""
    result = {"ok": False, "stdout": "", "stderr": "", "exit_status": None, "error": None}
    try:
        proc = subprocess.run(
            ["bash", "-c", command],
            capture_output=True, text=True, timeout=timeout,
        )
        result["stdout"] = (proc.stdout or "")[:LOCAL_CMD_MAX_OUTPUT]
        result["stderr"] = (proc.stderr or "")[:LOCAL_CMD_MAX_OUTPUT]
        result["exit_status"] = proc.returncode
        result["ok"] = proc.returncode == 0
    except subprocess.TimeoutExpired:
        result["error"] = f"command timed out after {timeout}s"
    except Exception as e:
        result["error"] = str(e)[:300]
    return result


# ====================== Local persistent shell (real-time terminal) ======================
LOCAL_SHELL_CMD_TIMEOUT = 6 * 60 * 60  # seconds (6h) - long since Cancel is manual


def _set_nonblocking(fd: int):
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


def _set_winsize(fd: int, width: int, height: int):
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", height, width, 0, 0))
    except Exception:
        pass


def resize_local_shell(master_fd: int, width: int, height: int):
    """Public wrapper around _set_winsize() - lets a live "↔ Resize" button (admin.py) change an already-open tab's pty window size on demand, e.g. switching to a wider layout so table-like output (`htop`, `docker ps` with many columns) stops getting cut off."""
    _set_winsize(master_fd, width, height)
    try:
        pgrp = os.tcgetpgrp(master_fd)
        os.killpg(pgrp, signal.SIGWINCH)
    except Exception:
        pass


def _prime_local_shell_prompt(master_fd: int, timeout: float = None):
    """Same trick as engine._prime_shell_prompt(): sets a unique PS1 marker so run_local_shell_input() below can tell "bash is done and waiting for input" apart from some program still running, and waits for it to print once, swallowing whatever MOTD/banner comes first."""
    timeout = _engine._PROMPT_PRIME_TIMEOUT if timeout is None else timeout
    try:
        os.write(master_fd, _engine._SHELL_PROMPT_SETUP_CMD.encode())
    except OSError:
        return
    buf = ""
    marker = _engine._SHELL_PROMPT_MARKER
    start = time.monotonic()
    last_data = start
    while True:
        now = time.monotonic()
        if now - last_data > timeout or now - start > _engine._PROMPT_PRIME_MAX_TOTAL:
            return
        try:
            r, _, _ = select.select([master_fd], [], [], _engine.STREAM_POLL_INTERVAL)
        except Exception:
            return
        if master_fd in r:
            try:
                buf += os.read(master_fd, 4096).decode(errors="ignore")
            except OSError:
                return
            last_data = time.monotonic()
            if marker in buf:
                return


def open_local_shell(width: int = 120, height: int = 32):
    """Opens one pty-backed interactive shell on the host, running the admin's login shell from their home dir."""
    pid, master_fd = pty.fork()
    if pid == 0:
        # Child: execvp() replaces this process image; os._exit() on failure avoids double cleanup
        try:
            os.chdir(local_home_dir())
        except Exception:
            pass
        shell = os.environ.get("SHELL", "/bin/bash")
        try:
            os.execvp(shell, [shell])
        except Exception:
            os._exit(1)
    _set_nonblocking(master_fd)
    _set_winsize(master_fd, width, height)
    _prime_local_shell_prompt(master_fd)
    return pid, master_fd


def close_local_shell(pid: Optional[int], master_fd: Optional[int]):
    """Terminates a shell opened by open_local_shell(), including its whole process group, and closes the master fd."""
    if pid:
        for sig in (signal.SIGHUP, signal.SIGKILL):
            try:
                os.killpg(pid, sig)
            except Exception:
                pass
        try:
            os.waitpid(pid, os.WNOHANG)
        except Exception:
            pass
    if master_fd is not None:
        try:
            os.close(master_fd)
        except Exception:
            pass


def is_local_shell_alive(pid: Optional[int]) -> bool:
    """True if a shell opened by open_local_shell() is still running."""
    if not pid:
        return False
    try:
        wpid, _ = os.waitpid(pid, os.WNOHANG)
        return wpid == 0
    except ChildProcessError:
        return False
    except Exception:
        return True


def strip_shell_marker(text: str) -> str:
    """Removes the internal PS1 completion marker (see _prime_local_shell_prompt()) from partial output that hasn't gone through run_local_shell_input()'s own return value yet - e.g. a live terminal message being updated mid-stream while a command is still running."""
    return (text or "").replace(_engine._SHELL_PROMPT_MARKER, "")


class LocalShellHandle:
    """Lets a live Cancel button inject Ctrl-C into whatever's running in a local shell right now, without tearing down the shell itself - same role as engine.ShellHandle, just writing straight to the pty's master fd instead of into an SSH channel."""

    def __init__(self, master_fd: int):
        self._master_fd = master_fd
        self._interrupt_requested = False

    def send_raw(self, data: str) -> bool:
        """Injects raw bytes into the pty mid-stream - e.g. a bare Enter for a program that's paused waiting on one."""
        try:
            os.write(self._master_fd, data.encode())
            return True
        except Exception:
            return False

    def cancel(self):
        self._interrupt_requested = True
        self.send_raw(_engine.CTRL_C)

    @property
    def cancelled(self) -> bool:
        return self._interrupt_requested


def run_local_shell_input(
    master_fd: int,
    text: str,
    timeout: int = LOCAL_SHELL_CMD_TIMEOUT,
    chunk_cb=None,
) -> Dict:
    """Send one line of input into an already-open local shell (see open_local_shell()) and stream back whatever the pty prints, in real time - the local equivalent of engine.run_shell_input()."""
    result = {
        "input": text, "output": "", "error": None,
        "cancelled": False, "timed_out": False, "shell_exited": False,
    }
    handle = LocalShellHandle(master_fd)
    if chunk_cb:
        try:
            chunk_cb(handle, "")
        except Exception:
            logger.debug("chunk_cb raised on initial handle hand-off", exc_info=True)

    # Defensive drain: discard any stale output/marker still sitting on
    # master_fd from a previous call before writing this command.
    try:
        while True:
            r, _, _ = select.select([master_fd], [], [], 0)
            if master_fd not in r:
                break
            try:
                if not os.read(master_fd, 4096):
                    break
            except OSError:
                break
    except Exception:
        pass

    try:
        os.write(master_fd, ((text or "") + "\n").encode())
    except OSError as e:
        result["error"] = str(e)[:300]
        return result

    marker = _engine._SHELL_PROMPT_MARKER
    parts = []
    tail = ""  # last decoded chunk, for marker checks
    start = time.monotonic()
    try:
        while True:
            got_data = False
            r, _, _ = select.select([master_fd], [], [], _engine.STREAM_POLL_INTERVAL)
            if master_fd in r:
                try:
                    chunk = os.read(master_fd, 4096).decode(errors="ignore")
                except OSError:
                    # Slave side closed - the shell process exited.
                    result["shell_exited"] = True
                    break
                if not chunk:
                    result["shell_exited"] = True
                    break
                parts.append(chunk)
                tail = (tail + chunk)[-len(marker) * 2:]
                got_data = True
                if chunk_cb:
                    chunk_cb(handle, chunk)

            if marker in tail:
                break
            if time.monotonic() - start > timeout:
                result["timed_out"] = True
                break
            if not got_data:
                # select() already waited STREAM_POLL_INTERVAL above with
                # nothing ready, so there's no need for an extra sleep here.
                pass
    except Exception as e:
        result["error"] = str(e)[:300]

    output = "".join(parts).replace(marker, "")
    result["output"] = output[-_engine.MAX_OUTPUT_CHARS:]
    result["cancelled"] = handle.cancelled
    return result


# ====================== Local file browser (SFTP equivalent) ======================
# Mirrors engine.py's sftp_* primitives, but talking straight to the local filesystem.
LOCAL_FILES_MAX_DOWNLOAD_BYTES = 45 * 1024 * 1024  # Telegram send-document ceiling
LOCAL_FILES_MAX_UPLOAD_BYTES = 19 * 1024 * 1024    # Telegram file-download ceiling
LOCAL_FILES_EDITOR_MAX_BYTES = 3500                # in-chat nano-style editor cap
# Fetched directly via urllib (no Telegram involved) - generous sanity ceiling
LOCAL_FILES_MAX_URL_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2GB


def local_home_dir() -> str:
    """Starting point for browsing - the bot process's own home directory,
    falling back to / if that can't be determined."""
    return os.path.expanduser("~") or "/"


def local_listdir(path: str) -> List[Dict]:
    """Directory listing, folders first then alphabetically, as [{"name", "is_dir", "size"}, ...] - same shape as engine.sftp_listdir()."""
    entries = []
    with os.scandir(path) as it:
        for e in it:
            try:
                is_dir = e.is_dir(follow_symlinks=False)
                size = 0 if is_dir else e.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            entries.append({"name": e.name, "is_dir": is_dir, "size": size})
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return entries


def local_join(cwd: str, name: str) -> str:
    return os.path.join(cwd, name)


def local_parent(cwd: str) -> str:
    if cwd in ("/", ""):
        return "/"
    parent = os.path.dirname(cwd.rstrip("/"))
    return parent or "/"


def local_normalize(path: str) -> str:
    """Resolves a manually-typed path (relative, with '..', a bare '~', etc.) to a canonical absolute path - same role as engine.sftp_normalize()."""
    return os.path.realpath(os.path.expanduser(path or "/"))


def local_rename(old_path: str, new_path: str):
    os.rename(old_path, new_path)


def local_mkdir(path: str):
    """Creates a new (empty) folder. Raises OSError if something with that
    name already exists there, or if the parent isn't writable."""
    os.mkdir(path)


def local_delete_recursive(path: str, is_dir: bool):
    """Deletes a single file, or a folder and everything inside it."""
    if is_dir:
        shutil.rmtree(path)
    else:
        os.remove(path)


def local_delete_many(entries: List[Dict]) -> Dict:
    """Bulk version of local_delete_recursive() for the Files multi-select action - deletes every entry given (each {"path", "is_dir"}) and keeps going even if one fails, so one locked/permission-denied item doesn't abort the rest of the batch."""
    deleted, failed = [], []
    for entry in entries:
        name = os.path.basename(entry["path"].rstrip("/")) or entry["path"]
        try:
            local_delete_recursive(entry["path"], entry.get("is_dir", False))
            deleted.append(name)
        except Exception as e:
            failed.append((name, str(e)[:200]))
    return {"deleted": deleted, "failed": failed}


def local_read_text(path: str, max_bytes: int = LOCAL_FILES_EDITOR_MAX_BYTES) -> str:
    """Reads a file in full for the in-chat editor."""
    with open(path, "rb") as f:
        data = f.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"File is over {max_bytes} bytes - too big to edit here. Download it instead.")
    if b"\x00" in data:
        raise ValueError("File looks like binary data, not text - editing here isn't supported.")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("File isn't valid UTF-8 text - editing here isn't supported.")


def local_write_text(path: str, content: str):
    """Overwrites a file with new text content (saved as UTF-8)."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def local_download_url(
    dest_dir: str, url: str, timeout: int = 300, max_bytes: int = LOCAL_FILES_MAX_URL_DOWNLOAD_BYTES,
    progress_callback: Optional[Callable[[int, Optional[int]], None]] = None,
) -> str:
    """Fetches `url` straight onto local disk under dest_dir - the "download into the owner server" counterpart to engine.remote_download_url(), but done with Python's own HTTP client instead of shelling out to wget/curl over SSH, since there's no remote host in between.
    `progress_callback(bytes_done, bytes_total)` is called after every chunk
    read (bytes_total is None if the server didn't send a Content-Length) -
    the same (done, total) shape paramiko's SFTP callback and
    engine.remote_download_url use, so callers can drive the same live
    progress bar for all three transfer kinds."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only http(s) URLs are supported.")

    filename = urllib.parse.unquote(os.path.basename(parsed.path)) or f"download_{uuid.uuid4().hex[:8]}"
    dest_path = os.path.join(dest_dir, filename)
    if os.path.exists(dest_path):
        stem, ext = os.path.splitext(filename)
        dest_path = os.path.join(dest_dir, f"{stem}_{uuid.uuid4().hex[:6]}{ext}")

    tmp_path = dest_path + ".part"
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            content_length = resp.headers.get("Content-Length")
            total = int(content_length) if content_length and content_length.isdigit() else None
            if total is not None and total > max_bytes:
                raise ValueError(f"Remote file is {total} bytes - over the {max_bytes} byte limit.")
            if progress_callback:
                progress_callback(0, total)
            written = 0
            with open(tmp_path, "wb") as out:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise ValueError(f"Download exceeded the {max_bytes} byte limit - aborted.")
                    out.write(chunk)
                    if progress_callback:
                        progress_callback(written, total)
            if progress_callback:
                progress_callback(written, total or written)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise

    os.rename(tmp_path, dest_path)
    return dest_path



# ====================== File permissions (chmod/chown) ======================

def local_stat_perms(path: str) -> Dict:
    """Current owner/group/mode - shown before prompting for new chmod/chown values, same idea as the rename prompt showing the current name."""
    st = os.stat(path)
    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        owner = str(st.st_uid)
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        group = str(st.st_gid)
    return {"mode": oct(st.st_mode & 0o777)[2:].zfill(3), "owner": owner, "group": group}


def local_chmod(path: str, mode_str: str):
    """Sets permissions from an octal string like '755' or '0644'."""
    mode_str = (mode_str or "").strip()
    if not re.fullmatch(r"0?[0-7]{3,4}", mode_str):
        raise ValueError("Not a valid octal mode - send 3 or 4 digits, e.g. 755 or 0644.")
    os.chmod(path, int(mode_str, 8))


def local_chown(path: str, owner_spec: str):
    """owner_spec is 'user' or 'user:group' (either side may be a name or a numeric id - same syntax as the `chown` command's first argument)."""
    owner_spec = (owner_spec or "").strip()
    if not owner_spec:
        raise ValueError("Send a user, or user:group, e.g. www-data or www-data:www-data.")
    user_part, _, group_part = owner_spec.partition(":")
    uid = -1
    if user_part:
        try:
            uid = int(user_part)
        except ValueError:
            try:
                uid = pwd.getpwnam(user_part).pw_uid
            except KeyError:
                raise ValueError(f"No such user: {user_part}")
    gid = -1
    if group_part:
        try:
            gid = int(group_part)
        except ValueError:
            try:
                gid = grp.getgrnam(group_part).gr_gid
            except KeyError:
                raise ValueError(f"No such group: {group_part}")
    os.chown(path, uid, gid)


# ====================== Archive extract / compress ======================
_ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")


def is_archive(name: str) -> bool:
    return name.lower().endswith(_ARCHIVE_SUFFIXES)


def _assert_within(dest_dir: str, member_path: str, member_name: str):
    dest_real = os.path.realpath(dest_dir)
    real = os.path.realpath(member_path)
    if real != dest_real and not real.startswith(dest_real + os.sep):
        raise ValueError(f"Refusing to extract - unsafe path in archive: {member_name}")


def local_extract_archive(archive_path: str, dest_dir: str):
    """Extracts a .zip/.tar(.gz/.bz2/.xz) archive into dest_dir (created if
    missing)."""
    os.makedirs(dest_dir, exist_ok=True)
    lower = archive_path.lower()
    if lower.endswith(".zip"):
        with zipfile.ZipFile(archive_path) as zf:
            for name in zf.namelist():
                _assert_within(dest_dir, os.path.join(dest_dir, name), name)
            zf.extractall(dest_dir)
    elif lower.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
        with tarfile.open(archive_path) as tf:
            for member in tf.getmembers():
                _assert_within(dest_dir, os.path.join(dest_dir, member.name), member.name)
            tf.extractall(dest_dir)
    else:
        raise ValueError("Unsupported archive format - only .zip and .tar(.gz/.bz2/.xz) are supported.")


def local_compress_entry(path: str, is_dir: bool, fmt: str = "zip") -> str:
    """Compresses a single file or folder into a new archive alongside it (e.g. 'project' -> 'project.zip')."""
    base = path.rstrip("/")
    parent, name = os.path.dirname(base), os.path.basename(base)
    if fmt == "zip":
        out_path = base + ".zip"
        if os.path.exists(out_path):
            raise FileExistsError(f"{os.path.basename(out_path)} already exists.")
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
            if is_dir:
                for root, _dirs, files in os.walk(base):
                    for f in files:
                        full = os.path.join(root, f)
                        zf.write(full, os.path.join(name, os.path.relpath(full, base)))
            else:
                zf.write(base, name)
    elif fmt == "tar.gz":
        out_path = base + ".tar.gz"
        if os.path.exists(out_path):
            raise FileExistsError(f"{os.path.basename(out_path)} already exists.")
        with tarfile.open(out_path, "w:gz") as tf:
            tf.add(base, arcname=name)
    else:
        raise ValueError(f"Unsupported compression format: {fmt}")
    return out_path


def local_compress_many(entries: List[Dict], dest_dir: str, archive_name: str, fmt: str = "zip") -> str:
    """Bulk version of local_compress_entry() for the Files multi-select action - packs several files/folders (each {"path", "is_dir"}, may be a mix of both) into ONE new archive at dest_dir/archive_name, each kept under its own top-level name inside the archive (so two selected items can never collide on write)."""
    if fmt == "zip":
        out_path = os.path.join(dest_dir, archive_name + ".zip")
        if os.path.exists(out_path):
            raise FileExistsError(f"{os.path.basename(out_path)} already exists.")
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for entry in entries:
                base = entry["path"].rstrip("/")
                name = os.path.basename(base)
                if entry.get("is_dir"):
                    for root, _dirs, files in os.walk(base):
                        for f in files:
                            full = os.path.join(root, f)
                            zf.write(full, os.path.join(name, os.path.relpath(full, base)))
                else:
                    zf.write(base, name)
    elif fmt == "tar.gz":
        out_path = os.path.join(dest_dir, archive_name + ".tar.gz")
        if os.path.exists(out_path):
            raise FileExistsError(f"{os.path.basename(out_path)} already exists.")
        with tarfile.open(out_path, "w:gz") as tf:
            for entry in entries:
                base = entry["path"].rstrip("/")
                tf.add(base, arcname=os.path.basename(base))
    else:
        raise ValueError(f"Unsupported compression format: {fmt}")
    return out_path


# ====================== Process management (kill) ======================

def kill_process(pid: int, force: bool = False) -> Dict:
    """Sends SIGTERM (or SIGKILL if force=True) to a single host process."""
    result = {"ok": False, "error": None}
    if pid <= 1:
        result["error"] = "Refusing to signal PID 1 (init) - that would take the whole host down."
        return result
    if pid == os.getpid():
        result["error"] = "That's the bot's own process - use \"Restart Bot\" instead."
        return result
    try:
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
        result["ok"] = True
    except ProcessLookupError:
        result["error"] = "No such process - it may have already exited."
    except PermissionError:
        result["error"] = "Permission denied - the bot doesn't own that process."
    except Exception as e:
        result["error"] = str(e)[:300]
    return result


def process_name(pid: int) -> Optional[str]:
    stat = _read(f"/proc/{pid}/stat")
    if not stat:
        return None
    try:
        return stat[stat.index("(") + 1:stat.rindex(")")]
    except Exception:
        return None


# ====================== systemd service control ======================
SYSTEMD_ACTION_TIMEOUT = 20


def _tag_origin(rows: List[Dict]):
    """Fills in row["origin"] ("custom" | "system") for every row in place, via one batched `systemctl show` call for all units (instead of one subprocess per unit). "custom" = the unit file's FragmentPath lives under /etc/systemd/system - hand-written units, or anything an app/admin installed there themselves (this bot's own unit included). "system" = shipped by the distro under /usr/lib or /lib/systemd/system - stock daemons like cron, dbus, ModemManager, etc."""
    if not rows:
        return
    for r in rows:
        r["origin"] = "system"
    try:
        proc = subprocess.run(
            ["systemctl", "show", *(r["unit"] for r in rows), "--property=Id,FragmentPath"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as e:
        logger.warning(f"systemd origin lookup failed: {e}")
        return
    by_id = {}
    cur_id, cur_path = None, ""
    for line in (proc.stdout or "").splitlines():
        if line.startswith("Id="):
            if cur_id:
                by_id[cur_id] = cur_path
            cur_id, cur_path = line[len("Id="):], ""
        elif line.startswith("FragmentPath="):
            cur_path = line[len("FragmentPath="):]
    if cur_id:
        by_id[cur_id] = cur_path
    for r in rows:
        if by_id.get(r["unit"], "").startswith("/etc/systemd/system/"):
            r["origin"] = "custom"


def systemd_list_units(name_filter: str = "", limit: int = 40) -> List[Dict]:
    """Every .service unit systemd knows about (running or not), optionally filtered by a case-insensitive substring of the unit name."""
    try:
        proc = subprocess.run(
            ["systemctl", "list-units", "--all", "--type=service", "--no-legend", "--no-pager", "--plain"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as e:
        logger.warning(f"systemd_list_units failed: {e}")
        return []
    rows = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        unit, load, active, sub = parts[0], parts[1], parts[2], parts[3]
        if not unit.endswith(".service"):
            continue
        if name_filter and name_filter.lower() not in unit.lower():
            continue
        rows.append({"unit": unit, "load": load, "active": active, "sub": sub})

    _tag_origin(rows)
    rows.sort(key=lambda r: (r["origin"] != "custom", r["active"] != "active", r["unit"]))

    custom = [r for r in rows if r["origin"] == "custom"]
    system = [r for r in rows if r["origin"] != "custom"]
    return (custom + system)[:max(limit, len(custom))]


def systemd_unit_status(unit: str) -> Dict:
    result = {"ok": False, "text": "", "error": None}
    try:
        proc = subprocess.run(
            ["systemctl", "status", unit, "--no-pager", "-l"],
            capture_output=True, text=True, timeout=SYSTEMD_ACTION_TIMEOUT,
        )
        result["text"] = ((proc.stdout or "") + (proc.stderr or ""))[:LOCAL_CMD_MAX_OUTPUT]
        result["ok"] = True
    except Exception as e:
        result["error"] = str(e)[:300]
    return result


def systemd_unit_action(unit: str, action: str) -> Dict:
    """action is one of start/stop/restart/enable/disable."""
    if action not in ("start", "stop", "restart", "enable", "disable"):
        return {"ok": False, "error": f"Unsupported action: {action}", "output": ""}
    result = {"ok": False, "error": None, "output": ""}
    try:
        proc = subprocess.run(
            ["systemctl", action, unit],
            capture_output=True, text=True, timeout=SYSTEMD_ACTION_TIMEOUT,
        )
        result["output"] = ((proc.stdout or "") + (proc.stderr or ""))[:LOCAL_CMD_MAX_OUTPUT]
        result["ok"] = proc.returncode == 0
        if not result["ok"] and not result["output"]:
            result["error"] = f"systemctl {action} exited with status {proc.returncode}"
    except subprocess.TimeoutExpired:
        result["error"] = f"{action} timed out after {SYSTEMD_ACTION_TIMEOUT}s"
    except Exception as e:
        result["error"] = str(e)[:300]
    return result


def systemd_unit_remove(unit: str) -> Dict:
    """Stops + disables the unit, deletes its unit file, then reloads the systemd manager and clears any failed-state record for it."""
    result = {"ok": False, "error": None, "output": ""}
    try:
        show = subprocess.run(
            ["systemctl", "show", unit, "--property=FragmentPath"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as e:
        result["error"] = str(e)[:300]
        return result

    frag_path = ""
    for line in (show.stdout or "").splitlines():
        if line.startswith("FragmentPath="):
            frag_path = line[len("FragmentPath="):].strip()
    if not frag_path.startswith("/etc/systemd/system/"):
        result["error"] = "Refusing to remove: not a custom unit (no unit file under /etc/systemd/system)."
        return result

    outputs = []
    try:
        for args in (["stop", unit], ["disable", unit]):
            proc = subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=SYSTEMD_ACTION_TIMEOUT)
            outputs.append(((proc.stdout or "") + (proc.stderr or "")).strip())
        os.remove(frag_path)
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True, text=True, timeout=SYSTEMD_ACTION_TIMEOUT)
        subprocess.run(["systemctl", "reset-failed"], capture_output=True, text=True, timeout=SYSTEMD_ACTION_TIMEOUT)
    except Exception as e:
        result["error"] = str(e)[:300]
        result["output"] = "\n".join(o for o in outputs if o)[:LOCAL_CMD_MAX_OUTPUT]
        return result

    result["ok"] = True
    result["output"] = "\n".join(o for o in outputs if o)[:LOCAL_CMD_MAX_OUTPUT]
    return result


# ====================== Crontab editor ======================
CRONTAB_EDITOR_MAX_BYTES = 3500


def get_crontab() -> str:
    """Current crontab text, or '' if none is set yet (`crontab -l` exits non-zero with "no crontab for <user>" in that case - not a real error)."""
    try:
        proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
    except Exception as e:
        raise RuntimeError(str(e)[:300])
    if proc.returncode != 0:
        return ""
    return proc.stdout


def set_crontab(content: str):
    """Replaces the whole crontab in one shot via `crontab -`, piping the
    new content in on stdin."""
    try:
        proc = subprocess.run(["crontab", "-"], input=content, capture_output=True, text=True, timeout=10)
    except Exception as e:
        raise RuntimeError(str(e)[:300])
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "crontab failed").strip()[:300])


# ====================== Live log tail (tail -F) ======================
TAIL_MAX_CHARS = 3200


def tail_start(path: str, lines: int = 50) -> subprocess.Popen:
    """Starts `tail -n <lines> -F <path>` in the background. -F (not -f) so a rotated log file (logrotate, etc.) is transparently re-opened by name instead of the view going stale."""
    proc = subprocess.Popen(
        ["tail", "-n", str(lines), "-F", path],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    _set_nonblocking(proc.stdout.fileno())
    return proc


def tail_read_available(proc: subprocess.Popen, max_chars: int = TAIL_MAX_CHARS) -> str:
    """Non-blocking read of whatever the tail process has printed since the last call - '' if there's nothing new right now."""
    chunks = []
    try:
        while True:
            data = os.read(proc.stdout.fileno(), 4096)
            if not data:
                break
            chunks.append(data.decode(errors="ignore"))
    except (BlockingIOError, OSError):
        pass
    text = "".join(chunks)
    return text[-max_chars:] if text else ""


def systemd_journal_tail_start(unit: str, lines: int = 50) -> subprocess.Popen:
    """Starts `journalctl -u <unit> -n <lines> -f` in the background - the live-log equivalent of tail_start() above for a systemd unit instead of a plain file."""
    proc = subprocess.Popen(
        ["journalctl", "-u", unit, "-n", str(lines), "-f", "--no-pager"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    _set_nonblocking(proc.stdout.fileno())
    return proc


def tail_is_alive(proc: subprocess.Popen) -> bool:
    return proc.poll() is None


def tail_stop(proc: subprocess.Popen):
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def cleanup_local(timeout: int = None) -> Dict:
    """Runs the exact same safe cleanup script maintenance.cleanup_server() runs
    remotely (apt/journald/docker/tmp/caches, RAM page cache - never running
    services, user data, or swap) but locally, with no SSH hop."""
    timeout = timeout or _maintenance.CLEANUP_TIMEOUT
    result = {"ok": False, "error": None, "freed_bytes": None, "freed_ram_bytes": None}
    try:
        proc = subprocess.run(
            ["bash", "-c", _maintenance._CLEANUP_CMD],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        result["error"] = f"cleanup timed out after {timeout}s"
        return result
    except Exception as e:
        result["error"] = str(e)[:300]
        return result

    parsed = _maintenance._parse_cleanup_output(proc.stdout or "")
    if parsed["freed_bytes"] is None:
        stderr_tail = (proc.stderr or "").strip()[-300:]
        result["error"] = stderr_tail or "cleanup script produced no output"
        return result
    result["ok"] = True
    result.update(parsed)
    return result