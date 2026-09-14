"""
colab_leecher/services/status_flow.py

The /status live dashboard: panel_text()/panel_kb() are used both by the
/status command (still in __main__.py) and by the status_refresh /
status_cancel / status_kill| / stats_refresh callbacks below — extracted
verbatim from __main__.py's _status_panel() / _status_kb() and the matching
branches of callbacks().
"""
from __future__ import annotations

import os
from datetime import datetime

import psutil
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from colab_leecher.services import on_exact, on_prefix
from colab_leecher.utility.handler import cancelTask
from colab_leecher.utility.variables import BOT, BotTimes, Messages, ProcessTracker, TaskInfo
from colab_leecher.utility.helper import sizeUnit, getTime, _pct_bar, _speed_emoji


def _ring(p):
    return "🟢" if p < 40 else ("🟡" if p < 70 else "🔴")


def panel_text() -> str:
    """Build the /status panel text — shows task state + system + cancel info."""
    cpu  = psutil.cpu_percent(interval=0)
    ram  = psutil.virtual_memory()
    disk = psutil.disk_usage("/")

    cpu_bar  = _pct_bar(cpu, 10)
    ram_bar  = _pct_bar(ram.percent, 10)
    disk_bar = _pct_bar(disk.percent, 10)

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "💖  <b>Myuu࣪ ☾ BOT — STATUS</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
    ]

    # ── Active task section ───────────────────
    if BOT.State.task_going:
        phase_icons = {
            "download": "📥", "upload": "📤", "process": "⚙️",
            "zip": "🗜", "extract": "📂",
        }
        icon   = phase_icons.get(TaskInfo.phase, "⏳")
        engine = TaskInfo.engine or "—"
        fname  = TaskInfo.filename or Messages.download_name or "—"
        fname  = (fname[:35] + "…") if len(fname) > 35 else fname
        pct    = TaskInfo.percentage
        speed  = TaskInfo.speed or "—"
        eta    = TaskInfo.eta or "—"
        spd_e  = _speed_emoji(speed)
        bar    = _pct_bar(pct, 14)

        elapsed = getTime((datetime.now() - BotTimes.task_start).seconds)

        lines += [
            f"{icon}  <b>{TaskInfo.phase.upper()}</b>  ·  <code>{engine}</code>",
            f"🏷  <code>{fname}</code>",
            "",
            f"<code>[{bar}]</code>  <b>{pct:.1f}%</b>",
            "",
            f"{spd_e}  <b>Speed</b>   <code>{speed}</code>",
            f"⏳  <b>ETA</b>     <code>{eta}</code>",
            f"🕰  <b>Elapsed</b> <code>{elapsed}</code>",
        ]

        procs = ProcessTracker.active()
        if procs:
            lines.append("")
            lines.append(f"🔧  <b>Processes</b>  <code>{len(procs)}</code>")
            for pid, label in procs[:5]:
                lines.append(f"   · PID {pid}  <code>{label[:25]}</code>")
    else:
        lines += [
            "💤  <b>No active task</b>",
            "",
            "<i>Send a link to start a download.</i>",
        ]

    # ── System section ────────────────────────
    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"{_ring(cpu)}  CPU   <code>[{cpu_bar}]</code>  <b>{cpu:.0f}%</b>",
        f"{_ring(ram.percent)}  RAM   <code>[{ram_bar}]</code>  <b>{ram.percent:.0f}%</b>",
        f"   Used <code>{sizeUnit(ram.used)}</code>  ·  Free <code>{sizeUnit(ram.available)}</code>",
        f"{_ring(disk.percent)}  Disk  <code>[{disk_bar}]</code>  <b>{disk.percent:.0f}%</b>",
        f"   Free <code>{sizeUnit(disk.free)}</code>",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    return "\n".join(lines)


def panel_kb() -> InlineKeyboardMarkup:
    rows = []
    if BOT.State.task_going:
        rows.append([
            InlineKeyboardButton("⛔ CANCEL TASK", callback_data="status_cancel"),
            InlineKeyboardButton("🔄 Refresh",     callback_data="status_refresh"),
        ])
        procs = ProcessTracker.active()
        if procs:
            row = []
            for pid, label in procs[:4]:
                short = label[:10] if label else str(pid)
                row.append(InlineKeyboardButton(
                    f"💀 {short}", callback_data=f"status_kill|{pid}",
                ))
                if len(row) == 2:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)
    else:
        rows.append([
            InlineKeyboardButton("🔄 Refresh", callback_data="status_refresh"),
            InlineKeyboardButton("❌ Close",    callback_data="close"),
        ])
    return InlineKeyboardMarkup(rows)


@on_exact("status_refresh")
async def handle_status_refresh(client, cq, data):
    await cq.answer("🔄 Refreshed")
    try:
        await cq.message.edit_text(panel_text(), reply_markup=panel_kb())
    except Exception:
        pass


@on_exact("status_cancel")
async def handle_status_cancel(client, cq, data):
    await cq.answer("⛔ Cancelling ALL tasks…")
    await cancelTask("Cancelled via /status panel")
    try:
        await cq.message.edit_text(panel_text(), reply_markup=panel_kb())
    except Exception:
        pass


@on_prefix("status_kill|")
async def handle_status_kill(client, cq, data):
    pid = int(data.split("|")[1])
    import signal
    try:
        os.kill(pid, signal.SIGTERM)
        ProcessTracker.unregister(pid)
        await cq.answer(f"💀 Killed PID {pid}")
    except ProcessLookupError:
        ProcessTracker.unregister(pid)
        await cq.answer("Process already dead.")
    except Exception as e:
        await cq.answer(f"Kill failed: {e}", show_alert=True)
    try:
        await cq.message.edit_text(panel_text(), reply_markup=panel_kb())
    except Exception:
        pass


@on_exact("stats_refresh")
async def handle_stats_refresh(client, cq, data):
    await cq.answer("🔄")
    cpu  = psutil.cpu_percent(interval=0)
    ram  = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    net  = psutil.net_io_counters()
    text = (
        "📊 <b>SERVER STATS</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{_ring(cpu)}  CPU  <code>[{_pct_bar(cpu,12)}]</code>  <b>{cpu:.1f}%</b>\n\n"
        f"{_ring(ram.percent)}  RAM  <code>[{_pct_bar(ram.percent,12)}]</code>  <b>{ram.percent:.1f}%</b>\n"
        f"    Used <code>{sizeUnit(ram.used)}</code>  ·  Free <code>{sizeUnit(ram.available)}</code>\n\n"
        f"{_ring(disk.percent)}  Disk <code>[{_pct_bar(disk.percent,12)}]</code>  <b>{disk.percent:.1f}%</b>\n"
        f"    Free <code>{sizeUnit(disk.free)}</code>\n\n"
        f"    ⬆️ <code>{sizeUnit(net.bytes_sent)}</code>  ⬇️ <code>{sizeUnit(net.bytes_recv)}</code>"
    )
    try:
        await cq.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Refresh", callback_data="stats_refresh"),
            InlineKeyboardButton("❌ Close",    callback_data="close"),
        ]]))
    except Exception:
        pass
