# ====================== Advanced Tools text formatting ======================
# Telegram-facing Markdown formatters for the "⚡ Advanced Tools" screens in
# ServerManager/handlers.py (remote process manager, systemd services,
# crontab editor). Per-service live log tail (journalctl -f) is still
# available from inside a service's detail screen. Mirrors the style of
# health.py's format_health_text(). All the actual remote data-gathering
# lives in ServerManager/engine.py (remote_top_processes,
# remote_systemd_list_units, remote_get_crontab, remote_journal_tail, etc.)
# - this module only turns those results into text ready to send to Telegram.

import logging

logger = logging.getLogger(__name__)

# Keep replies under Telegram's ~4096 char message size limit, with room
# for the surrounding label/keyboard text.
MAX_TEXT_CHARS = 3500


def _truncate(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n... (truncated)"


def format_top_processes_text(rows: list, by: str, label: str) -> str:
    sort_label = "CPU" if by == "cpu" else "RAM"
    if not rows:
        return f"▤ *Processes* — `{label}`\n\n(no data)"

    lines = [f"▤ *Top processes by {sort_label}* — `{label}`", "", "```"]
    lines.append(f"{'PID':>7} {'CPU%':>6} {'MEM%':>6}  COMMAND")
    for r in rows[:10]:
        pid = r.get("pid", "-")
        cpu, mem = r.get("cpu"), r.get("mem")
        comm = (r.get("comm") or "-")[:28]
        cpu_s = f"{cpu:.1f}" if isinstance(cpu, (int, float)) else "-"
        mem_s = f"{mem:.1f}" if isinstance(mem, (int, float)) else "-"
        lines.append(f"{str(pid):>7} {cpu_s:>6} {mem_s:>6}  {comm}")
    lines.append("```")
    return _truncate("\n".join(lines))


SVC_FILTERS = (("all", "▤ All"), ("custom", "◦ Custom"), ("system", "▣ System"))


def format_units_text(units: list, label: str, filter_mode: str = "all") -> str:
    if not units:
        return f"⚙ *Services* — `{label}`\n\n(none found, or `systemctl` isn't available on this host)"

    custom_n = sum(1 for u in units if u.get("origin") == "custom")
    system_n = len(units) - custom_n
    shown_n = {"custom": custom_n, "system": system_n}.get(filter_mode, len(units))
    return (
        f"⚙ *Services* — `{label}`  ·  {shown_n} shown\n"
        f"◦ {custom_n} custom   ▣ {system_n} system\n\n"
        f"Tap one to view status and start/stop/restart it."
    )


def format_unit_status_text(unit: str, raw_text: str) -> str:
    body = (raw_text or "").strip() or "(no output)"
    return _truncate(f"⚙ *{unit}*\n\n```\n{body}\n```")


def format_crontab_text(content: str, label: str) -> str:
    body = (content or "").strip()
    if not body:
        return f"⏰ *Crontab* — `{label}`\n\n(empty - no scheduled jobs)"
    return _truncate(f"⏰ *Crontab* — `{label}`\n\n```\n{body}\n```")
