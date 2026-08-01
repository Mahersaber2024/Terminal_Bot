# ====================== On-demand server restart & disk cleanup ======================
# Used by the "🔄 Restart" / "🧹 Cleanup" buttons on the health card
# (server_manager_handlers.py). Kept as its own module rather than folded
# into server_manager_engine.py, since engine.py is the generic SSH/SFTP
# primitives layer (connect, run_command, ...) while this module is a
# feature built on top of it - same separation as server_manager_health.py.

import re
from typing import Dict

from . import engine

REBOOT_TIMEOUT = 15    # seconds - just long enough to fire the command; the reboot itself happens after we've disconnected
CLEANUP_TIMEOUT = 120  # seconds - cleanup does real work (apt/docker/journal), give it room

# Backgrounded + detached (nohup ... & disown) so the reboot - which kills
# our own SSH session - never leaves run_command() hanging on a channel
# that will never send back an exit status. The command itself returns
# immediately; the actual reboot fires a moment later, after we've already
# closed the connection cleanly.
_REBOOT_CMD = "nohup sh -c 'sleep 1; reboot' >/dev/null 2>&1 & disown; echo REBOOT_SCHEDULED"

# Same "safe junk cleanup" steps as server_maintenance.py's scheduled remote
# cleanup: apt cache, journald, docker logs/prune, /tmp, rotated logs, user
# caches, pip/npm caches, crash dumps. Never touches running services or data.
# Available space on / is measured before/after to report bytes freed.
_CLEANUP_CMD = (
    "AVAIL_BEFORE=$(df --output=avail -B1 / | tail -1); "
    "apt-get clean 2>/dev/null; apt-get autoclean 2>/dev/null; apt-get autoremove -y 2>/dev/null; "
    "journalctl --vacuum-time=3d 2>/dev/null; "
    "command -v docker >/dev/null 2>&1 && docker system prune -f 2>/dev/null; "
    "find /var/lib/docker/containers -name '*-json.log' -exec truncate -s 0 {} \\; 2>/dev/null; "
    "find /var/log -type f \\( -name '*.gz' -o -name '*.[0-9]' \\) -delete 2>/dev/null; "
    "rm -rf /tmp/* /var/tmp/* 2>/dev/null; "
    "rm -rf /root/.cache/* 2>/dev/null; "
    "for h in /home/*; do rm -rf \"$h/.cache\"/* 2>/dev/null; done; "
    "command -v pip3 >/dev/null 2>&1 && pip3 cache purge 2>/dev/null; "
    "command -v npm >/dev/null 2>&1 && npm cache clean --force 2>/dev/null; "
    "rm -rf /var/crash/* 2>/dev/null; "
    "AVAIL_AFTER=$(df --output=avail -B1 / | tail -1); "
    "echo FREED_BYTES:$((AVAIL_AFTER - AVAIL_BEFORE))"
)
_FREED_BYTES_RE = re.compile(r"FREED_BYTES:(-?\d+)")


def restart_server(server: Dict, timeout: int = REBOOT_TIMEOUT) -> Dict:
    """Schedules an immediate reboot on the remote server. Success here means
    "the reboot was scheduled", not "the server has finished rebooting" -
    there's no way to confirm that without a follow-up health check once it's
    back up."""
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


def cleanup_server(server: Dict, timeout: int = CLEANUP_TIMEOUT) -> Dict:
    """Runs the same safe disk-cleanup routine as server_maintenance.py's
    scheduled job, but on-demand for a single server. Returns freed bytes on
    / as measured before/after - never touches running services or data."""
    result = {"ok": False, "error": None, "freed_bytes": None}
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

    match = _FREED_BYTES_RE.search(cmd_result.get("stdout", ""))
    if not match:
        stderr_tail = (cmd_result.get("stderr") or "").strip()[-300:]
        result["error"] = stderr_tail or "cleanup script produced no output"
        return result

    result["ok"] = True
    result["freed_bytes"] = max(0, int(match.group(1)))
    return result
