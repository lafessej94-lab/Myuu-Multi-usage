"""
colab_leecher/utility/handler/task_control.py

Cycle de vie de la tâche GLOBALE unique : annulation du pipeline gated par
BOT.State.task_going (leech normal / CloudConvert / Seedr+CC — par
opposition aux jobs à message de statut dédié dans direct_hardsub_tasks.py
et local_video_tasks.py, qui utilisent shared._finish_status), kill des
process orphelins, et le résumé de fin envoyé après un leech normal.

Extrait de handler.py — voir __init__.py de ce package pour la liste
complète des réexports.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from datetime import datetime
from os import path as ospath

from colab_leecher import OWNER, colab_bot
from colab_leecher.utility.handler.shared import _tail_log
from colab_leecher.utility.helper import getTime, sizeUnit
from colab_leecher.utility.variables import (
    BOT, MSG, BotTimes, Paths, ProcessTracker, TaskInfo, Transfer,
)

log = logging.getLogger(__name__)


def _kill_stray_processes():
    """Kill any aria2c/ffmpeg/yt-dlp that might have been missed."""
    for name in ("aria2c", "ffmpeg", "ffprobe"):
        try:
            subprocess.run(["pkill", "-f", name], capture_output=True, timeout=5)
        except Exception:
            pass


async def cancelTask(reason: str):
    spent = getTime((datetime.now() - BotTimes.start_time).seconds)
    killed = ProcessTracker.kill_all()

    if BOT.State.task_going:
        try:
            if BOT.TASK and not BOT.TASK.done():
                BOT.TASK.cancel()
        except Exception as exc:
            log.warning("Task cancel: %s", exc)

    _kill_stray_processes()

    try:
        if ospath.exists(Paths.WORK_PATH):
            shutil.rmtree(Paths.WORK_PATH)
    except Exception as exc:
        log.warning("Cancel cleanup: %s", exc)

    BOT.State.task_going = False
    TaskInfo.reset()

    text = (
        "⛔ <b>TASK CANCELLED</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"❓  <b>Reason</b>   <i>{reason}</i>\n"
        f"⏱  <b>Spent</b>    <code>{spent}</code>\n"
        f"💀  <b>Killed</b>   <code>{killed} process(es)</code>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>All downloads, uploads and processing stopped.</i>"
    )
    log_tail = _tail_log(60)

    if hasattr(MSG.status_msg, "stop"):
        try:
            await MSG.status_msg.stop()
        except Exception:
            pass

    try:
        await MSG.status_msg.edit_text(text)
    except Exception:
        try:
            await colab_bot.send_message(chat_id=BOT.TargetChat, text=text)
        except Exception:
            pass

    if log_tail and "Cancelled by user" not in reason and "Cancelled via" not in reason:
        try:
            await colab_bot.send_message(
                chat_id=BOT.TargetChat,
                text="📜 <b>Recent Log Tail</b>\n\n<code>" + log_tail[-3500:] + "</code>",
            )
        except Exception:
            pass

    log.info("[Cancel] Task cancelled: %s - killed %s procs", reason, killed)


_OUT_MODE_TAGS = {
    "normal": "#Leech",
    "zip": "#Zip",
    "undzip": "#Unzip",
    "cc_convert": "#Convert",
    "cc_resize": "#Resize",
    "cc_compress": "#Compress",
}

_owner_tag_cache: str | None = None


def _in_mode_tag() -> str:
    if BOT.Mode.ytdl:
        return "#YTDL"
    src = (BOT.SOURCE or [""])[0] if BOT.SOURCE else ""
    if src.startswith("magnet:") or src.lower().endswith(".torrent"):
        return "#Torrent"
    if "drive.google.com" in src:
        return "#GDrive"
    if "mega.nz" in src or "mega.co.nz" in src:
        return "#Mega"
    return "#Aria2"


async def _owner_tag() -> str:
    global _owner_tag_cache
    if _owner_tag_cache is not None:
        return _owner_tag_cache
    try:
        user = await colab_bot.get_users(OWNER)
        _owner_tag_cache = f"@{user.username}" if user.username else (user.first_name or "Owner")
    except Exception:
        _owner_tag_cache = "Owner"
    return _owner_tag_cache


async def SendLogs(is_leech: bool):
    spent = getTime((datetime.now() - BotTimes.start_time).seconds)
    filename = Transfer.sent_file_names[-1] if Transfer.sent_file_names else "—"
    total_files = len(Transfer.sent_file_names)
    out_mode = _OUT_MODE_TAGS.get(BOT.Mode.type, "#Leech" if is_leech else "#Convert")

    summary = (
        f"<code>{filename}</code>\n"
        "│\n"
        f"┟ Task Size → <code>{sizeUnit(Transfer.total_down_size)}</code>\n"
        f"┠ Time Taken → <code>{spent}</code>\n"
        f"┠ In Mode → <code>{_in_mode_tag()}</code>\n"
        f"┠ Out Mode → <code>{out_mode}</code>\n"
        f"Total Files: <code>{total_files}</code>\n"
        f"┖ Task By → <code>{await _owner_tag()}</code>\n\n"
        "〶 <b>Action Performed :</b>\n"
        "⋗ File(s) have been sent to User PM"
    )
    if _tail_log(10):
        summary += "\n\n📜 <b>Need details?</b> Use <code>/logs</code>"

    try:
        await colab_bot.send_message(chat_id=BOT.TargetChat, text=summary)
    except Exception:
        pass

    BOT.State.started = False
    BOT.State.task_going = False
    TaskInfo.reset()
