"""
services/job_views.py

Pure text/keyboard builders for job status panels — no Telegram I/O, no
ffmpeg calls, no exception swallowing. Mirrors services/url_views.py:
handlers call these to get (text, keyboard), then are responsible for the
actual status_msg.edit_text(...) + try/except themselves.

Extracted from handler.py's _fc_job_status / _seedr_status. The rendered
strings are byte-for-byte identical to the originals — only the
construction was pulled out of the function that also performed the edit.
"""
from __future__ import annotations

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

_KIND_EMOJI = {
    "FreeConvert Hardsub": "🆓",
    "CloudConvert Hardsub": "☁️",
    "Seedr + FreeConvert Hardsub": "🆓",
    "Burn Subs": "🖥️",
    "Mux Subs": "🖥️",
}


def fc_job_status_view(
    kind: str,
    stage: str,
    pct: float,
    detail: str,
    filename: str = "",
    job_id: str = "",
    *,
    quality_label: str,
    render_task_status,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Build (text, keyboard) for a per-job FC/CC/local status panel.

    `render_task_status` and `quality_label` are passed in rather than
    imported here so this module has zero dependency on
    colab_leecher.utility.helper / cloudconvert / freeconvert — callers
    already have those in scope and pass the resolved string/callable.
    """
    pct = max(0.0, min(float(pct), 100.0))
    emoji = _KIND_EMOJI.get(kind, "⚙️")
    text = render_task_status(
        emoji=emoji,
        title=kind.upper(),
        filename=filename or "job",
        pct=pct,
        lines=[
            ("Stage", stage),
            ("Detail", detail),
            ("Preset", quality_label),
            ("Engine", kind),
        ],
        stop_hint=f"/canceljob_{job_id}" if job_id else "Tap ❌ Cancel below",
    )
    keyboard = (
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"canceljob_{job_id}")]])
        if job_id else None
    )
    return text, keyboard


def seedr_status_view(
    kind: str,
    stage: str,
    pct: float,
    detail: str,
    filename: str,
    *,
    task_msg_prefix: str,
    engine_mode_label: str,
    quality_label: str,
    sys_info: str,
) -> str:
    """Build the text for the shared MSG.status_msg Seedr+CC/FC panel.

    Identical layout to the original _seedr_status body. Callers resolve
    task_msg_prefix (Messages.task_msg), engine_mode_label
    (cc_mode_label(BOT.Options.cc_engine_mode)), quality_label
    (quality_label(BOT.Options.cc_quality_profile)) and sys_info
    (sysINFO()) before calling, since those read live global state that
    doesn't belong in a pure view builder.
    """
    pct = max(0.0, min(float(pct), 100.0))
    body = (
        f"☁️ <b>{kind}</b>\n\n"
        f"<code>{filename or 'Seedr job'}</code>\n\n"
        f"<b>Stage</b>  <code>{stage}</code>\n"
        f"<b>Progress</b>  <code>{pct:.1f}%</code>\n"
        f"<b>Mode</b>  <code>{engine_mode_label}</code>\n"
        f"<b>Preset</b>  <code>{quality_label}</code>\n"
        f"<b>Detail</b>  <code>{detail}</code>"
    )
    return task_msg_prefix + body + sys_info
