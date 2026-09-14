"""
colab_leecher/services/stream_flow.py

The Stream Extractor button flow (sx_open, sx_type, sx_video, sx_audio,
sx_subs, sx_back, sx_dl_*) plus its private helpers (_show_type_menu,
_prepare_stream_source, _pick_stream_source_file, media-info probing).
Extracted verbatim from __main__.py.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import shutil
import subprocess
from datetime import datetime

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from colab_leecher import colab_bot
from colab_leecher.downlader.aria2 import aria2_Download
from colab_leecher.link_menu import kind_label as lk_kind_label, link_header as lk_header, main_kb as lk_main_kb
from colab_leecher.services import on_exact, on_prefix
from colab_leecher.stream_extractor import (
    analyse, get_session, clear_session,
    kb_type, kb_video, kb_audio, kb_subs,
    dl_video, dl_audio, dl_sub, track_flags,
)
from colab_leecher.utility.helper import fileType, sizeUnit
from colab_leecher.utility.variables import BOT, MSG, Aria2c, Paths, TaskInfo


def _fmt_hms(seconds: float) -> str:
    total = int(seconds or 0)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _probe_media_info(path: str) -> str:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                path,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return ""
        data = json.loads(result.stdout)
    except Exception as exc:
        logging.warning("Media info probe failed: %s", exc)
        return ""

    fmt = data.get("format", {}) or {}
    streams = data.get("streams", []) or []
    lines = [
        "MEDIA INFO",
        f"FILE  <code>{os.path.basename(path)}</code>",
        f"SIZE  <code>{sizeUnit(os.path.getsize(path))}</code>",
    ]
    duration = float(fmt.get("duration") or 0.0)
    if duration > 0:
        lines.append(f"DURATION  <code>{_fmt_hms(duration)}</code>")

    for stream in streams:
        stype = str(stream.get("codec_type") or "").lower()
        codec = str(stream.get("codec_name") or "?").upper()
        tags = stream.get("tags", {}) or {}
        lang = (tags.get("language") or "").lower()
        lang_s = f" [{lang}]" if lang else ""
        if stype == "video":
            w = stream.get("width", 0)
            h = stream.get("height", 0)
            fr = str(stream.get("r_frame_rate") or "0/1")
            try:
                fn, fd = fr.split("/")
                fps = float(fn) / max(float(fd), 1.0)
                fps_s = f"{fps:.3f}fps"
            except Exception:
                fps_s = "?"
            lines.append(f"VIDEO  <code>{codec}  {w}x{h}  {fps_s}</code>")
        elif stype == "audio":
            ch = int(stream.get("channels") or 0)
            ch_s = {1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}.get(ch, f"{ch}ch" if ch else "")
            lines.append(f"AUDIO  <code>{codec}  {ch_s}{lang_s}</code>")
        elif stype == "subtitle":
            lines.append(f"SUB  <code>{codec}{lang_s}</code>")
    return "\n".join(lines[:12])


def _pick_stream_source_file(root: str) -> str | None:
    files = [str(p) for p in pathlib.Path(root).glob("**/*") if p.is_file()]
    if not files:
        return None
    videos = [f for f in files if fileType(f) == "video"]
    pool = videos or files
    return max(pool, key=lambda p: os.path.getsize(p))


async def _prepare_stream_source(url: str) -> str:
    if not url.startswith("magnet:?xt=urn:btih:"):
        return url

    if os.path.exists(Paths.WORK_PATH):
        shutil.rmtree(Paths.WORK_PATH)
    os.makedirs(Paths.WORK_PATH, exist_ok=True)
    os.makedirs(Paths.down_path, exist_ok=True)

    Aria2c.link_info = False
    TaskInfo.reset()
    TaskInfo.set(phase="download", engine="Aria2c", filename="magnet", started_at=datetime.now().timestamp())
    await aria2_Download(url, 1)

    source_file = _pick_stream_source_file(Paths.down_path)
    if not source_file:
        raise RuntimeError("Torrent download finished but no media file was found for stream extraction.")
    return source_file


async def _show_type_menu(msg, session):
    v = len(session["video"])
    a = len(session["audio"])
    s = len(session["subs"])
    title = session["title"]
    v_flags = track_flags(session["video"])
    a_flags = track_flags(session["audio"])
    s_flags = track_flags(session["subs"])
    await msg.edit_text(
        "🎞 <b>STREAM EXTRACTOR</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📌  <b>{title}</b>\n\n"
        f"🎬  Video tracks     <code>{v}</code>  {v_flags}\n"
        f"🎵  Audio tracks     <code>{a}</code>  {a_flags}\n"
        f"💬  Subtitles        <code>{s}</code>  {s_flags}\n\n"
        "Choose track type:",
        reply_markup=kb_type(session)
    )


@on_exact("sx_open")
async def handle_sx_open(client, cq, data):
    chat_id = cq.message.chat.id
    url = (BOT.SOURCE or [None])[0]
    if not url:
        await cq.answer("No URL found.", show_alert=True)
        return

    source_url = url
    if url.startswith("magnet:?xt=urn:btih:"):
        await cq.message.edit_text(
            "STREAM EXTRACTOR\n\nDownloading magnet first...\nThe stream menu will open once the main video is local."
        )
        MSG.status_msg = cq.message
        BOT.State.task_going = True
        try:
            source_url = await _prepare_stream_source(url)
        except Exception as exc:
            BOT.State.task_going = False
            await cq.message.edit_text(
                f"STREAM EXTRACTOR\n\nFailed to prepare source:\n<code>{exc}</code>",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="sx_back")]])
            )
            return
        BOT.State.task_going = False
    else:
        await cq.message.edit_text(
            "STREAM EXTRACTOR\n\n"
            f"Analyzing streams...\n"
            f"<code>{url[:70]}{'...' if len(url)>70 else ''}</code>"
        )

    session = await analyse(source_url, chat_id)

    if not session or (not session["video"] and not session["audio"] and not session["subs"]):
        await cq.message.edit_text(
            "STREAM EXTRACTOR\n\n"
            "Could not extract streams.\n"
            "<i>Only yt-dlp compatible sources are supported.</i>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Back", callback_data="sx_back")
            ]])
        )
        return

    await _show_type_menu(cq.message, session)


@on_exact("sx_type")
async def handle_sx_type(client, cq, data):
    session = get_session(cq.message.chat.id)
    if not session:
        await cq.answer("Session expired.", show_alert=True)
        return
    await _show_type_menu(cq.message, session)


@on_exact("sx_video")
async def handle_sx_video(client, cq, data):
    session = get_session(cq.message.chat.id)
    if not session:
        await cq.answer("Session expired.", show_alert=True); return
    if not session["video"]:
        await cq.answer("No video tracks.", show_alert=True); return
    await cq.message.edit_text(
        "🎬 <b>VIDEO TRACKS</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>flag  resolution  [codec]  size</i>\n\nTap to download:",
        reply_markup=kb_video(session)
    )


@on_exact("sx_audio")
async def handle_sx_audio(client, cq, data):
    session = get_session(cq.message.chat.id)
    if not session:
        await cq.answer("Session expired.", show_alert=True); return
    if not session["audio"]:
        await cq.answer("No audio tracks.", show_alert=True); return
    await cq.message.edit_text(
        "🎵 <b>AUDIO TRACKS</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>flag  language  [codec]  bitrate  size</i>\n\nTap to download:",
        reply_markup=kb_audio(session)
    )


@on_exact("sx_subs")
async def handle_sx_subs(client, cq, data):
    session = get_session(cq.message.chat.id)
    if not session:
        await cq.answer("Session expired.", show_alert=True); return
    if not session["subs"]:
        await cq.answer("No subtitles.", show_alert=True); return
    await cq.message.edit_text(
        "💬 <b>SUBTITLES</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>flag  language  [format]</i>\n\nTap to download:",
        reply_markup=kb_subs(session)
    )


@on_exact("sx_back")
async def handle_sx_back(client, cq, data):
    clear_session(cq.message.chat.id)
    n = len([l for l in (BOT.SOURCE or []) if l.strip()])
    first_src = (BOT.SOURCE or [""])[0].strip()
    is_magnet = first_src.startswith("magnet:?xt=urn:btih:")
    label = lk_kind_label(BOT.Mode.ytdl, is_magnet)
    await cq.message.edit_text(
        lk_header(label, n),
        reply_markup=lk_main_kb(is_magnet)
    )


@on_prefix("sx_dl_")
async def handle_sx_dl(client, cq, data):
    chat_id = cq.message.chat.id
    session = get_session(chat_id)
    if not session:
        await cq.answer("Session expired.", show_alert=True); return

    parts = data.split("_")
    kind  = parts[2]
    idx   = int(parts[3])

    stream = (session["video"] if kind == "video"
              else session["audio"] if kind == "audio"
              else session["subs"])[idx]

    await cq.message.edit_text(
        f"🎞 <b>STREAM EXTRACTOR</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⬇️ <i>Downloading {kind}...</i>\n\n"
        f"<code>{stream['label']}</code>",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⛔ Cancel", callback_data="cancel")
        ]])
    )
    MSG.status_msg = cq.message

    os.makedirs(Paths.down_path, exist_ok=True)
    try:
        if kind == "video":
            fp = await dl_video(session, idx, Paths.down_path)
        elif kind == "audio":
            fp = await dl_audio(session, idx, Paths.down_path)
        else:
            fp = await dl_sub(session, idx, Paths.down_path)

        from colab_leecher.uploader.telegram import upload_file
        await upload_file(fp, os.path.basename(fp), is_last=True)
        media_info = _probe_media_info(fp)
        if media_info:
            await colab_bot.send_message(chat_id=BOT.TargetChat, text=media_info)
        clear_session(chat_id)

    except Exception as e:
        logging.error(f"[StreamDL] {e}")
        try:
            await cq.message.edit_text(
                f"🎞 <b>STREAM EXTRACTOR</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"❌ <b>Error:</b> <code>{e}</code>"
            )
        except Exception:
            pass
