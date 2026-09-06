# ====================== On-demand server restart & disk cleanup ======================

import re
from typing import Dict

from . import engine

REBOOT_TIMEOUT = 15    # seconds - just long enough to fire the command; the reboot itself happens after we've disconnected
CLEANUP_TIMEOUT = 120  # seconds - cleanup does real work (apt/docker/journal), give it room

# Backgrounded + detached so the reboot doesn't leave run_command() hanging
_REBOOT_CMD = "nohup sh -c 'sleep 1; reboot' >/dev/null 2>&1 & disown; echo REBOOT_SCHEDULED"

# Safe junk cleanup: apt/journald/docker caches, tmp, logs, and RAM page
# cache. Freed disk/RAM measured before/after. Deliberately does NOT touch
# swap: clearing swap only works by paging everything back into RAM, so it
# isn't really "freeing" anything - it just moves used memory from disk to
# RAM, which sends RAM usage up instead of down.
_CLEANUP_CMD = (
    "AVAIL_BEFORE=$(df --output=avail -B1 / | tail -1); "
    "MEM_BEFORE=$(awk '/MemAvailable/{print $2}' /proc/meminfo 2>/dev/null || echo 0); "
    "apt-get clean 2>/dev/null; apt-get autoclean 2>/dev/null; "
    "apt-get autoremove --purge -y 2>/dev/null; "
    # apt's downloaded package-list metadata (Packages/Release indexes under
    # lists/), not the packages themselves - safe to delete, `apt-get update`
    # regenerates it. Can easily be tens of MB on a box that's never pruned it.
    "rm -rf /var/lib/apt/lists/* 2>/dev/null; "
    # Old/unused kernel packages left behind by past upgrades (autoremove
    # above only catches these if apt already flagged them "auto"-installed).
    # Never touches the kernel currently running (uname -r).
    "dpkg -l 2>/dev/null | awk '/^ii  linux-(image|headers|modules)-[0-9]/{print $2}' "
    "| grep -v \"$(uname -r)\" | xargs -r apt-get -y purge 2>/dev/null; "
    # Time AND size cap, whichever frees more - a 3-day window frees nothing
    # if that window itself is huge.
    "journalctl --vacuum-time=3d 2>/dev/null; journalctl --vacuum-size=200M 2>/dev/null; "
    # `-a` also removes unused *tagged* images/networks, not just dangling
    # ones - those are usually the biggest chunk on a small box that's been
    # redeployed a few times. Never touches images backing a running container.
    "command -v docker >/dev/null 2>&1 && docker system prune -af 2>/dev/null; "
    "find /var/lib/docker/containers -name '*-json.log' -exec truncate -s 0 {} \\; 2>/dev/null; "
    "find /var/log -type f \\( -name '*.gz' -o -name '*.[0-9]' \\) -delete 2>/dev/null; "
    "rm -rf /tmp/* /var/tmp/* 2>/dev/null; "
    "rm -rf /root/.cache/* 2>/dev/null; "
    "for h in /home/*; do rm -rf \"$h/.cache\"/* 2>/dev/null; done; "
    "command -v pip3 >/dev/null 2>&1 && pip3 cache purge 2>/dev/null; "
    "command -v npm >/dev/null 2>&1 && npm cache clean --force 2>/dev/null; "
    "rm -rf /var/crash/* 2>/dev/null; "
    # Free reclaimable page cache/dentries/inodes - the kernel would reclaim
    # these on demand anyway, so this is safe; just does it now.
    "sync; echo 1 > /proc/sys/vm/drop_caches 2>/dev/null; "
    "AVAIL_AFTER=$(df --output=avail -B1 / | tail -1); "
    "MEM_AFTER=$(awk '/MemAvailable/{print $2}' /proc/meminfo 2>/dev/null || echo 0); "
    "echo FREED_BYTES:$((AVAIL_AFTER - AVAIL_BEFORE)); "
    "echo RAM_FREED_KB:$((MEM_AFTER - MEM_BEFORE))"
)
_FREED_BYTES_RE = re.compile(r"FREED_BYTES:(-?\d+)")
_RAM_FREED_KB_RE = re.compile(r"RAM_FREED_KB:(-?\d+)")


def restart_server(server: Dict, timeout: int = REBOOT_TIMEOUT) -> Dict:
    """Schedules an immediate reboot on the remote server."""
    result = {"ok": False, "error": None}
    try:
        client = engine.connect(server)
    except Exception as e:
        result["error"] = str(e)[:300]
        return result
    try:
        cmd_result = engine.run_command(client, _REBOOT_CMD, timeout=timeout)
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
        result["error"] = stderr_tail or f"reboot command exited with status {cmd_result.get('exit_status')}"
        return result

    result["ok"] = True
    return result


def _parse_cleanup_output(stdout: str) -> Dict:
    """Pulls the FREED_BYTES / RAM_FREED_KB markers _CLEANUP_CMD prints at the
    end out of the command's stdout. Keys are None if the marker never printed
    (e.g. the script errored before reaching that line)."""
    out = {"freed_bytes": None, "freed_ram_bytes": None}
    m = _FREED_BYTES_RE.search(stdout)
    if m:
        out["freed_bytes"] = max(0, int(m.group(1)))
    m = _RAM_FREED_KB_RE.search(stdout)
    if m:
        out["freed_ram_bytes"] = max(0, int(m.group(1))) * 1024
    return out


def cleanup_server(server: Dict, timeout: int = CLEANUP_TIMEOUT) -> Dict:
    """Runs the safe disk/RAM-cache cleanup routine on-demand for a single server."""
    result = {"ok": False, "error": None, "freed_bytes": None, "freed_ram_bytes": None}
    try:
        client = engine.connect(server)
    except Exception as e:
        result["error"] = str(e)[:300]
        return result
    try:
        cmd_result = engine.run_command(client, _CLEANUP_CMD, timeout=timeout)
    finally:
        try:
            client.close()
        except Exception:
            pass

    if cmd_result.get("error"):
        result["error"] = cmd_result["error"]
        return result

    parsed = _parse_cleanup_output(cmd_result.get("stdout", ""))
    if parsed["freed_bytes"] is None:
        stderr_tail = (cmd_result.get("stderr") or "").strip()[-300:]
        result["error"] = stderr_tail or "cleanup script produced no output"
        return result

    result["ok"] = True
    result.update(parsed)
    return result


def format_freed_summary(result: Dict) -> str:
    """Turns a cleanup_server()/cleanup_local() result into 'X MB disk, Y MB
    RAM cache freed' - only mentioning RAM if the script actually reported a
    number for it."""
    parts = [f"{round((result.get('freed_bytes') or 0) / (1024 * 1024), 1)}MB disk"]
    if result.get("freed_ram_bytes") is not None:
        parts.append(f"{round(result['freed_ram_bytes'] / (1024 * 1024), 1)}MB RAM cache")
    return ", ".join(parts) + " freed"