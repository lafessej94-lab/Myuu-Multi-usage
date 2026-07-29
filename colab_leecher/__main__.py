import logging
import os
import platform
import pathlib
import psutil
import shutil
import json
import subprocess
from datetime import datetime
from asyncio import sleep, get_event_loop
from urllib.parse import urlparse
from uuid import uuid4
from pyrogram import filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from colab_leecher import CC_API_KEY, FC_API_KEY, DUMP_ID, SEEDR_PASSWORD, SEEDR_USERNAME, colab_bot, OWNER
from colab_leecher.cloudconvert import cc_mode_label, quality_label, resize_label
from colab_leecher.utility.handler import (
    Direct_FC_Hardsub_Handler,
    Local_Merge_Handler,
    Local_Video_Convert_Handler,
    Seedr_CC_Convert_Handler,
    Seedr_CC_Hardsub_Handler,
    Seedr_FC_Hardsub_Handler,
    cancelTask,
)
from colab_leecher.utility.variables import (
    BOT, MSG, BotTimes, Paths, Messages, ProcessTracker, TaskInfo, Aria2c,
)
from colab_leecher.utility.task_manager import taskScheduler
from colab_leecher.utility.helper import (
    isLink, setThumbnail, message_deleter, send_settings,
    sizeUnit, getTime, is_ytdl_link, fileType, _pct_bar, _speed_emoji,
)
from colab_leecher.downlader.aria2 import aria2_Download
from colab_leecher.stream_extractor import (
    analyse, get_session, clear_session,
    kb_type, kb_video, kb_audio, kb_subs,
    dl_video, dl_audio, dl_sub,
)


_initial_dump = str(DUMP_ID or "").strip()
if _initial_dump not in ("", "0"):
    try:
        BOT.Options.dump_ids = [int(_initial_dump)]
    except ValueError:
        BOT.Options.dump_ids = [_initial_dump]
BOT.Options.auto_forward = bool(BOT.Options.dump_ids)
BOT.Setting.auto_forward = "On" if BOT.Options.auto_forward else "Off"

# Clés API CloudConvert/FreeConvert : reprend celles du launcher Colab comme
# point de départ, puis gérables en plus depuis le bot via /addcc et /addfc.
BOT.Options.cc_api_keys = [k.strip() for k in str(CC_API_KEY or "").split(",") if k.strip()]
BOT.Options.fc_api_keys = [k.strip() for k in str(FC_API_KEY or "").split(",") if k.strip()]

# ── État en mémoire pour le hardsub FreeConvert concurrent ──────────────────
# _link_sessions : message_id (du message "Choose mode:") -> liste de sources.
#   Nécessaire pour que plusieurs liens envoyés d'affilée ne se marchent pas
#   dessus sur le global BOT.SOURCE — chaque bouton "mode" retrouve SON lien
#   via le message auquel il est attaché, pas via BOT.SOURCE (qui ne reflète
#   que le tout dernier lien envoyé).
# _pending_fc_subtitle : message_id (du message "Envoie le sous-titre...") ->
#   {"url":..., "name":...}. Permet plusieurs hardsub FC en attente de
#   sous-titre en même temps — l'utilisateur répond (reply) au bon message
#   avec le bon fichier pour lever l'ambiguïté.
_link_sessions: dict[int, list[str]] = {}
_pending_fc_subtitle: dict[int, dict] = {}

# _pending_video : message_id (du menu affiché après réception d'une vidéo)
# -> {"source_message": Message, "name": str}. Nécessaire pour retrouver la
# vidéo d'origine une fois qu'un outil (ex: Video Converter) est choisi.
_pending_video: dict[int, dict] = {}

# _pending_merge : message_id (du prompt "envoie l'audio") -> {"source_message": Message}
_pending_merge: dict[int, dict] = {}

LOCAL_RESOLUTIONS: dict[str, int] = {"480": 480, "720": 720, "1080": 1080}
_AUDIO_EXTS = (".mp3", ".m4a", ".flac", ".wav", ".ogg", ".aac", ".opus")


def _video_tools_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎞 Video Converter", callback_data="vidtool_convert")],
        [InlineKeyboardButton("🔊 Merge Audio+Vidéo", callback_data="vidtool_merge")],
    ])


def _video_res_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("480p", callback_data="vidres|480"),
         InlineKeyboardButton("720p", callback_data="vidres|720")],
        [InlineKeyboardButton("1080p", callback_data="vidres|1080")],
        [InlineKeyboardButton("✖ Annuler", callback_data="vidtool_cancel")],
    ])


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


async def _startup_welcome() -> None:
    for _ in range(6):
        try:
            await sleep(2)
            owner = await colab_bot.get_users(OWNER)
            first = owner.first_name or owner.username or str(OWNER)
            display = first.replace("<", "&lt;").replace(">", "&gt;")
            text = (
                f"👋 <b>Welcome back, {display}</b>\n"
                "⚡ <b>Zilong is online</b>\n\n"
                "Send a link, magnet, or path to begin.\n"
                "Use /start for the full menu and /status for the live dashboard."
            )
            await colab_bot.send_message(chat_id=OWNER, text=text)
            return
        except Exception as exc:
            logging.warning("Startup welcome attempt failed: %s", exc)


def _owner(m): return m.chat.id == OWNER
def _ring(p):  return "🟢" if p < 40 else ("🟡" if p < 70 else "🔴")

REQUIRED_CHANNEL = "@hebdos"


async def _is_subscribed(client, user_id: int) -> bool:
    try:
        member = await client.get_chat_member(REQUIRED_CHANNEL, user_id)
        return str(member.status).lower() not in ("left", "banned", "kicked")
    except Exception as exc:
        logging.debug(f"Subscription check failed: {exc}")
        return False


def _join_gate_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Rejoindre " + REQUIRED_CHANNEL, url=f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}")],
        [InlineKeyboardButton("✅ J'ai rejoint", callback_data="check_sub")],
    ])


# Résolutions proposées avant un hardsub FreeConvert — format (largeur, hauteur).
# None = garde la résolution d'origine du fichier (comportement historique).
FC_RESOLUTIONS: dict[str, tuple[int, int] | None] = {
    "orig": None,
    "360":  (640, 360),
    "480":  (854, 480),
    "720":  (1280, 720),
}


def _fc_quality_kb(flow: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 Qualité d'origine", callback_data=f"fc_res|{flow}|orig")],
        [InlineKeyboardButton("360p", callback_data=f"fc_res|{flow}|360"),
         InlineKeyboardButton("480p", callback_data=f"fc_res|{flow}|480")],
        [InlineKeyboardButton("720p", callback_data=f"fc_res|{flow}|720")],
    ])


# ══════════════════════════════════════════════
#  /start
# ══════════════════════════════════════════════

@colab_bot.on_message(filters.command("start") & filters.private)
async def start(client, message):
    await message.delete()

    if _owner(message):
        await message.reply_text(
            "⚡ <b>ZILONG BOT</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🟢 Online &amp; Ready\n\n"
            "Send a <b>link</b>, <b>magnet</b> or <b>path</b>.\n\n"
            "📥 Direct links · Magnet · GDrive\n"
            "🎬 YouTube · Mega · Terabox\n"
            "☁️ CloudConvert convert · resize · compress\n"
            "🧲 Seedr + CloudConvert convert · hardsub\n"
            "🧲 Seedr + FreeConvert hardsub\n"
            "🎞 Stream Extractor (any link)\n"
            "📊 /status — live dashboard\n"
            "📡 /nyaa_search — anime search\n\n"
            "💡 /help for all commands",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📖 Help",     callback_data="cb_help"),
                InlineKeyboardButton("⚙️ Settings", callback_data="cb_settings"),
            ], [
                InlineKeyboardButton("📊 Status",   callback_data="status_refresh"),
            ]])
        )
        return

    if not await _is_subscribed(client, message.from_user.id):
        await message.reply_text(
            "🔒 <b>Accès restreint</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Pour utiliser ce bot, abonne-toi d'abord à {REQUIRED_CHANNEL}.\n\n"
            "Une fois fait, tape sur « J'ai rejoint » ci-dessous.",
            reply_markup=_join_gate_kb(),
        )
        return

    await message.reply_text(
        "⚡ <b>ZILONG BOT</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "✅ Abonnement vérifié\n\n"
        "Tu peux consulter les réglages du bot, mais seul le propriétaire "
        "peut lancer des téléchargements.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⚙️ Voir les réglages", callback_data="cb_settings"),
        ]])
    )


# ══════════════════════════════════════════════
#  /help
# ══════════════════════════════════════════════

@colab_bot.on_message(filters.command("help") & filters.private)
async def help_cmd(client, message):
    text = (
        "📖 <b>HELP</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔗 <b>Supported Sources</b>\n"
        "  · HTTP/HTTPS  · Magnet  · Torrent\n"
        "  · Google Drive  · Mega.nz  · Terabox\n"
        "  · YouTube / YTDL  · Telegram links\n"
        "  · Local paths (/content/...)\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚙️ <b>Commands</b>\n"
        "  /settings  — bot preferences\n"
        "  /status    — <b>live task dashboard + cancel</b>\n"
        "  /stats     — system resources\n"
        "  /ping      — latency test\n"
        "  /cancel    — cancel running task\n"
        "  /stop      — shutdown bot\n"
        "  /setname   — custom filename\n"
        "  /rename    — rename after download\n"
        "  /add       — add a dump channel\n"
        "  /dumps     — list/remove dump channels\n"
        "  /addcc     — add a CloudConvert API key\n"
        "  /addfc     — add a FreeConvert API key\n"
        "  /apikeys   — list/remove API keys\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📡 <b>Nyaa Anime Search</b>\n"
        "  /nyaa_search <query> — search Nyaa.si\n"
        "  /nyaa_add <title>    — track anime\n"
        "  /nyaa_list           — watchlist\n"
        "  /nyaa_check          — poll now\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🎛 <b>Options (after link)</b>\n"
        "  <code>[name.ext]</code>  — custom filename\n"
        "  <code>{pass}</code>     — zip password\n"
        "  <code>(pass)</code>     — unzip password\n\n"
        "☁️ <b>CloudConvert</b> — use CC Convert / Resize / Compress buttons\n"
        "🧲 <b>Seedr + CC</b> — on magnet links, use Seedr+CC Convert / Hardsub\n"
        "🧲 <b>Seedr + FreeConvert</b> — on magnet links, use Seedr+FC Hardsub\n"
        "🎞 <b>Stream Extractor</b> — tap 🎞 Streams on any link\n"
        "🖼 Send a <b>photo</b> to set thumbnail"
    )
    msg = await message.reply_text(text)
    await sleep(120)
    await message_deleter(message, msg)


@colab_bot.on_message(filters.command("logs") & filters.private)
async def logs_cmd(client, message):
    if not _owner(message):
        return
    await message.delete()
    if not os.path.exists(Paths.LOG_PATH):
        await message.reply_text("❌ No log file found yet.")
        return
    try:
        with open(Paths.LOG_PATH, "r", encoding="utf-8", errors="replace") as fh:
            tail = "".join(fh.readlines()[-80:]).strip()
        if tail:
            await message.reply_text(f"📜 <b>Recent Logs</b>\n\n<code>{tail[-3500:]}</code>")
        await client.send_document(chat_id=OWNER, document=Paths.LOG_PATH, caption="Zilong runtime log")
    except Exception as exc:
        await message.reply_text(f"❌ Could not send logs: <code>{exc}</code>")


# ══════════════════════════════════════════════
#  /status — LIVE TASK DASHBOARD WITH CANCEL
# ══════════════════════════════════════════════

def _status_panel() -> str:
    """Build the /status panel text — shows task state + system + cancel info."""
    cpu  = psutil.cpu_percent(interval=0)
    ram  = psutil.virtual_memory()
    disk = psutil.disk_usage("/")

    cpu_bar  = _pct_bar(cpu, 10)
    ram_bar  = _pct_bar(ram.percent, 10)
    disk_bar = _pct_bar(disk.percent, 10)

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "⚡  <b>ZILONG BOT — STATUS</b>",
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


def _status_kb() -> InlineKeyboardMarkup:
    rows = []
    if BOT.State.task_going:
        rows.append([
            InlineKeyboardButton("⛔ CANCEL TASK", callback_data="status_cancel"),
            InlineKeyboardButton("🔄 Refresh",     callback_data="status_refresh"),
        ])
        # Kill individual processes
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


@colab_bot.on_message(filters.command("status") & filters.private)
async def cmd_status(client, message):
    await message.delete()
    await message.reply_text(
        _status_panel(),
        reply_markup=_status_kb(),
    )


# ══════════════════════════════════════════════
#  /stats — system info (unchanged)
# ══════════════════════════════════════════════

@colab_bot.on_message(filters.command("stats") & filters.private)
async def stats(client, message):
    if not _owner(message): return
    await message.delete()
    cpu  = psutil.cpu_percent(interval=1)
    ram  = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    net  = psutil.net_io_counters()
    up_s = int((datetime.now() - datetime.fromtimestamp(psutil.boot_time())).total_seconds())
    text = (
        "📊 <b>SERVER STATS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🖥  <b>OS</b>      <code>{platform.system()} {platform.release()}</code>\n"
        f"🐍  <b>Python</b>  <code>v{platform.python_version()}</code>\n"
        f"⏱  <b>Uptime</b>  <code>{getTime(up_s)}</code>\n"
        f"🤖  <b>Task</b>    {'🟠 Running' if BOT.State.task_going else '⚪ Idle'}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{_ring(cpu)}  CPU  <code>[{_pct_bar(cpu,12)}]</code>  <b>{cpu:.1f}%</b>\n\n"
        f"{_ring(ram.percent)}  RAM  <code>[{_pct_bar(ram.percent,12)}]</code>  <b>{ram.percent:.1f}%</b>\n"
        f"    Used <code>{sizeUnit(ram.used)}</code>  ·  Free <code>{sizeUnit(ram.available)}</code>\n\n"
        f"{_ring(disk.percent)}  Disk <code>[{_pct_bar(disk.percent,12)}]</code>  <b>{disk.percent:.1f}%</b>\n"
        f"    Used <code>{sizeUnit(disk.used)}</code>  ·  Free <code>{sizeUnit(disk.free)}</code>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"    ⬆️  <code>{sizeUnit(net.bytes_sent)}</code>\n"
        f"    ⬇️  <code>{sizeUnit(net.bytes_recv)}</code>"
    )
    await message.reply_text(text, reply_markup=InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Refresh", callback_data="stats_refresh"),
        InlineKeyboardButton("❌ Close",    callback_data="close"),
    ]]))


# ══════════════════════════════════════════════
#  /ping
# ══════════════════════════════════════════════

@colab_bot.on_message(filters.command("ping") & filters.private)
async def ping(client, message):
    t0  = datetime.now()
    msg = await message.reply_text("⏳")
    ms  = (datetime.now() - t0).microseconds // 1000
    if ms < 100:   q, fill = "🟢 Excellent", 12
    elif ms < 300: q, fill = "🟡 Good",       8
    elif ms < 700: q, fill = "🟠 Average",     4
    else:          q, fill = "🔴 Poor",         1
    bar = "█" * fill + "░" * (12 - fill)
    await msg.edit_text(
        f"🏓 <b>PONG</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<code>[{bar}]</code>\n\n"
        f"⚡ <b>Latency</b>  <code>{ms} ms</code>\n"
        f"📶 <b>Quality</b>  {q}"
    )
    await sleep(20)
    await message_deleter(message, msg)


# ══════════════════════════════════════════════
#  /cancel, /stop, /settings, /setname, /rename
# ══════════════════════════════════════════════

@colab_bot.on_message(filters.command("cancel") & filters.private)
async def cancel_cmd(client, message):
    if not _owner(message): return
    await message.delete()
    if BOT.State.task_going:
        await cancelTask("Cancelled via /cancel")
    else:
        msg = await message.reply_text("⚠️ No active task.")
        await sleep(8); await msg.delete()


@colab_bot.on_message(filters.command("stop") & filters.private)
async def stop_bot(client, message):
    if not _owner(message): return
    await message.delete()
    if BOT.State.task_going:
        await cancelTask("Bot shutdown")
    await message.reply_text("🛑 <b>Shutting down...</b> 👋")
    await sleep(2); await client.stop(); os._exit(0)


@colab_bot.on_message(filters.command("settings") & filters.private)
async def settings_cmd(client, message):
    await message.delete()
    if _owner(message):
        await send_settings(client, message, message.id, True)
        return
    if not await _is_subscribed(client, message.from_user.id):
        await message.reply_text(
            f"🔒 Abonne-toi à {REQUIRED_CHANNEL} pour voir les réglages.",
            reply_markup=_join_gate_kb(),
        )
        return
    await send_settings(client, message, message.id, True, readonly=True)


@colab_bot.on_message(filters.command("setname") & filters.private)
async def custom_name(client, message):
    if len(message.command) != 2:
        msg = await message.reply_text("Usage: <code>/setname file.ext</code>", quote=True)
    else:
        BOT.Options.custom_name = message.command[1]
        msg = await message.reply_text(f"✅ Name → <code>{BOT.Options.custom_name}</code>", quote=True)
    await sleep(15); await message_deleter(message, msg)


@colab_bot.on_message(filters.command("rename") & filters.private)
async def rename_cmd(client, message):
    """Minimal rename — set name for next upload."""
    if len(message.command) < 2:
        return await message.reply_text(
            "✏️ <b>Rename</b>\n\nUsage: <code>/rename New Name.mkv</code>",
            quote=True,
        )
    new_name = " ".join(message.command[1:])
    BOT.Options.custom_name = new_name
    await message.reply_text(
        f"✅ Next file will be named: <code>{new_name}</code>",
        quote=True,
    )


@colab_bot.on_message(filters.command("zipaswd") & filters.private)
async def zip_pswd(client, message):
    if len(message.command) != 2:
        msg = await message.reply_text("Usage: <code>/zipaswd password</code>", quote=True)
    else:
        BOT.Options.zip_pswd = message.command[1]
        msg = await message.reply_text("✅ Zip password set 🔐", quote=True)
    await sleep(15); await message_deleter(message, msg)


@colab_bot.on_message(filters.command("unzipaswd") & filters.private)
async def unzip_pswd(client, message):
    if len(message.command) != 2:
        msg = await message.reply_text("Usage: <code>/unzipaswd password</code>", quote=True)
    else:
        BOT.Options.unzip_pswd = message.command[1]
        msg = await message.reply_text("✅ Unzip password set 🔓", quote=True)
    await sleep(15); await message_deleter(message, msg)


def _dumps_kb() -> InlineKeyboardMarkup:
    rows = []
    for cid in BOT.Options.dump_ids:
        rows.append([InlineKeyboardButton(f"🗑 {cid}", callback_data=f"dump_remove|{cid}")])
    rows.append([InlineKeyboardButton("⏎ Back", callback_data="back")])
    return InlineKeyboardMarkup(rows)


def _dumps_text() -> str:
    if not BOT.Options.dump_ids:
        return (
            "📦 <b>CANAUX DUMP</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Aucun canal configuré.\n\n"
            "Ajoute-en un avec :\n"
            "<code>/add @mon_channel</code>\n"
            "<code>/add -1001234567890</code>"
        )
    lines = "\n".join(f"· <code>{cid}</code>" for cid in BOT.Options.dump_ids)
    return (
        "📦 <b>CANAUX DUMP</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{lines}\n\n"
        "Ajoute-en un autre avec <code>/add @channel</code>\n"
        "Tape sur 🗑 pour en retirer un."
    )


@colab_bot.on_message(filters.command("add") & filters.private)
async def add_dump_cmd(client, message):
    if not _owner(message): return
    await message.delete()
    if len(message.command) != 2:
        msg = await message.reply_text(
            "Usage: <code>/add @channel</code> ou <code>/add -1001234567890</code>",
            quote=True,
        )
        await sleep(10); await msg.delete()
        return

    target = message.command[1].strip()
    try:
        chat = await client.get_chat(target)
        chat_id = chat.id
        title = chat.title or chat.first_name or str(chat_id)
    except Exception as exc:
        msg = await message.reply_text(
            f"❌ Impossible de trouver <code>{target}</code>\n<code>{exc}</code>",
            quote=True,
        )
        await sleep(10); await msg.delete()
        return

    if chat_id in BOT.Options.dump_ids:
        msg = await message.reply_text(f"⚠️ <b>{title}</b> est déjà dans la liste.", quote=True)
    else:
        BOT.Options.dump_ids.append(chat_id)
        BOT.Options.auto_forward = True
        BOT.Setting.auto_forward = "On"
        msg = await message.reply_text(
            f"✅ Canal ajouté : <b>{title}</b>\n<code>{chat_id}</code>",
            quote=True,
        )
    await sleep(10); await msg.delete()


@colab_bot.on_message(filters.command("dumps") & filters.private)
async def dumps_cmd(client, message):
    if not _owner(message): return
    await message.delete()
    await message.reply_text(_dumps_text(), reply_markup=_dumps_kb())


def _mask_key(key: str) -> str:
    if len(key) <= 10:
        return "•" * len(key)
    return f"{key[:4]}…{key[-4:]}"


def _apikeys_kb() -> InlineKeyboardMarkup:
    rows = []
    for i, key in enumerate(BOT.Options.cc_api_keys):
        rows.append([InlineKeyboardButton(f"🗑 CC · {_mask_key(key)}", callback_data=f"apikey_remove|cc|{i}")])
    for i, key in enumerate(BOT.Options.fc_api_keys):
        rows.append([InlineKeyboardButton(f"🗑 FC · {_mask_key(key)}", callback_data=f"apikey_remove|fc|{i}")])
    rows.append([InlineKeyboardButton("⏎ Back", callback_data="back")])
    return InlineKeyboardMarkup(rows)


def _apikeys_text() -> str:
    cc_lines = "\n".join(f"· <code>{_mask_key(k)}</code>" for k in BOT.Options.cc_api_keys) or "Aucune"
    fc_lines = "\n".join(f"· <code>{_mask_key(k)}</code>" for k in BOT.Options.fc_api_keys) or "Aucune"
    return (
        "🔑 <b>CLÉS API</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>☁️ CloudConvert</b>\n{cc_lines}\n\n"
        f"<b>🆓 FreeConvert</b>\n{fc_lines}\n\n"
        "Ajoute-en une avec :\n"
        "<code>/addcc TA_CLE</code>\n"
        "<code>/addfc TA_CLE</code>\n\n"
        "Tape sur 🗑 pour en retirer une."
    )


@colab_bot.on_message(filters.command("addcc") & filters.private)
async def add_cc_key_cmd(client, message):
    if not _owner(message): return
    await message.delete()
    if len(message.command) != 2:
        msg = await message.reply_text("Usage: <code>/addcc TA_CLE_CLOUDCONVERT</code>", quote=True)
        await sleep(10); await msg.delete()
        return
    key = message.command[1].strip()
    if key in BOT.Options.cc_api_keys:
        msg = await message.reply_text("⚠️ Cette clé est déjà enregistrée.", quote=True)
    else:
        BOT.Options.cc_api_keys.append(key)
        msg = await message.reply_text(f"✅ Clé CloudConvert ajoutée : <code>{_mask_key(key)}</code>", quote=True)
    await sleep(10); await msg.delete()


@colab_bot.on_message(filters.command("addfc") & filters.private)
async def add_fc_key_cmd(client, message):
    if not _owner(message): return
    await message.delete()
    if len(message.command) != 2:
        msg = await message.reply_text("Usage: <code>/addfc TA_CLE_FREECONVERT</code>", quote=True)
        await sleep(10); await msg.delete()
        return
    key = message.command[1].strip()
    if key in BOT.Options.fc_api_keys:
        msg = await message.reply_text("⚠️ Cette clé est déjà enregistrée.", quote=True)
    else:
        BOT.Options.fc_api_keys.append(key)
        msg = await message.reply_text(f"✅ Clé FreeConvert ajoutée : <code>{_mask_key(key)}</code>", quote=True)
    await sleep(10); await msg.delete()


@colab_bot.on_message(filters.command("apikeys") & filters.private)
async def apikeys_cmd(client, message):
    if not _owner(message): return
    await message.delete()
    await message.reply_text(_apikeys_text(), reply_markup=_apikeys_kb())


@colab_bot.on_message(filters.reply & filters.private)
async def setFix(client, message):
    if BOT.State.prefix:
        BOT.Setting.prefix = message.text; BOT.State.prefix = False
        await send_settings(client, message, message.reply_to_message_id, False)
        await message.delete()
    elif BOT.State.suffix:
        BOT.Setting.suffix = message.text; BOT.State.suffix = False
        await send_settings(client, message, message.reply_to_message_id, False)
        await message.delete()


# ══════════════════════════════════════════════
#  Link handler — mode selection
# ══════════════════════════════════════════════

def _mode_keyboard():
    first = (BOT.SOURCE or [""])[0].strip()
    is_magnet = first.startswith("magnet:?xt=urn:btih:")
    is_http = first.startswith("http://") or first.startswith("https://")

    rows = [
        [InlineKeyboardButton("── 📦 Fichier ──", callback_data="noop")],
        [InlineKeyboardButton("📄 Normal",      callback_data="normal"),
         InlineKeyboardButton("🗜 Compresser",  callback_data="zip")],
        [InlineKeyboardButton("📂 Extraire",    callback_data="unzip"),
         InlineKeyboardButton("♻️ Ré-archiver", callback_data="undzip")],
        [InlineKeyboardButton("── ☁️ CloudConvert ──", callback_data="noop")],
        [InlineKeyboardButton("🔄 Convertir",       callback_data="cc_convert"),
         InlineKeyboardButton("📐 Redimensionner",  callback_data="cc_resize")],
        [InlineKeyboardButton("🧱 Compresser", callback_data="cc_compress")],
    ]

    if is_magnet:
        rows.append([InlineKeyboardButton("── 🧲 Seedr + Hardsub ──", callback_data="noop")])
        rows.append([InlineKeyboardButton("☁️ Seedr+CC Convert", callback_data="seedr_cc_convert")])
        rows.append([
            InlineKeyboardButton("☁️ CC Hardsub", callback_data="seedr_cc_hardsub"),
            InlineKeyboardButton("🆓 FC Hardsub", callback_data="seedr_fc_hardsub"),
        ])
    elif is_http:
        rows.append([InlineKeyboardButton("── 🧲 Hardsub ──", callback_data="noop")])
        rows.append([InlineKeyboardButton("🆓 FC Hardsub", callback_data="fc_hardsub_manual")])

    rows.append([InlineKeyboardButton("── 🎞 Autre ──", callback_data="noop")])
    rows.append([InlineKeyboardButton("🎞 Extraire pistes (streams)", callback_data="sx_open")])

    return InlineKeyboardMarkup(rows)


@colab_bot.on_message(filters.create(isLink) & ~filters.photo & filters.private)
async def handle_url(client, message):
    if not _owner(message):
        if await _is_subscribed(client, message.from_user.id):
            msg = await message.reply_text(
                "⛔ Seul le propriétaire du bot peut lancer des téléchargements.\n"
                "Tape /settings pour consulter les réglages.",
                quote=True,
            )
        else:
            msg = await message.reply_text(
                f"🔒 Abonne-toi à {REQUIRED_CHANNEL} pour utiliser ce bot.",
                reply_markup=_join_gate_kb(),
                quote=True,
            )
        await sleep(10); await msg.delete()
        return
    BOT.Options.custom_name = ""
    BOT.Options.zip_pswd    = ""
    BOT.Options.unzip_pswd  = ""

    if BOT.State.task_going:
        msg = await message.reply_text("⚠️ Task running — /cancel first.", quote=True)
        await sleep(8); await msg.delete()
        return

    src = message.text.splitlines()
    for _ in range(3):
        if not src: break
        last = src[-1].strip()
        if   last.startswith("[") and last.endswith("]"): BOT.Options.custom_name = last[1:-1]; src.pop()
        elif last.startswith("{") and last.endswith("}"): BOT.Options.zip_pswd    = last[1:-1]; src.pop()
        elif last.startswith("(") and last.endswith(")"): BOT.Options.unzip_pswd  = last[1:-1]; src.pop()
        else: break

    BOT.SOURCE    = src
    BOT.Mode.ytdl = all(is_ytdl_link(l) for l in src if l.strip())
    BOT.Mode.mode = "leech"
    BOT.State.started = True

    n = len([l for l in src if l.strip()])
    first_src = (src or [""])[0].strip()
    if BOT.Mode.ytdl:
        kind_label = "🏮 Lien YTDL"
    elif first_src.startswith("magnet:?xt=urn:btih:"):
        kind_label = "🧲 Magnet détecté"
    else:
        kind_label = "🔗 Lien détecté"

    sent = await message.reply_text(
        f"{kind_label}\n<code>{n}</code> source(s) · <b>Choisis un mode :</b>",
        reply_markup=_mode_keyboard(), quote=True,
    )
    _link_sessions[sent.id] = src


# ══════════════════════════════════════════════
#  ALL CALLBACKS
# ══════════════════════════════════════════════

@colab_bot.on_callback_query()
async def callbacks(client, cq):
    data    = cq.data
    chat_id = cq.message.chat.id

    # ── Labels de section non-cliquables (juste des repères visuels) ──
    if data == "noop":
        await cq.answer()
        return

    # ── Help/Settings from /start ──────────────
    if data == "cb_help":
        await cq.answer()
        text = (
            "📖 <b>Quick Guide</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Send any link to download.\n"
            "/status — live dashboard + cancel\n"
            "/nyaa_search — anime torrents\n"
            "/settings — preferences\n"
            "/help — full command list"
        )
        await cq.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Back", callback_data="cb_back_start"),
        ]]))
        return

    if data == "cb_settings":
        await cq.answer()
        is_owner = cq.from_user and cq.from_user.id == OWNER
        await send_settings(client, cq.message, cq.message.id, False, readonly=not is_owner)
        return

    if data == "check_sub":
        if await _is_subscribed(client, cq.from_user.id):
            await cq.answer("✅ Abonnement confirmé !", show_alert=True)
            await cq.message.edit_text(
                "⚡ <b>ZILONG BOT</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "✅ Abonnement vérifié\n\n"
                "Tu peux consulter les réglages du bot, mais seul le propriétaire "
                "peut lancer des téléchargements.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⚙️ Voir les réglages", callback_data="cb_settings"),
                ]])
            )
        else:
            await cq.answer(f"❌ Toujours pas abonné à {REQUIRED_CHANNEL}", show_alert=True)
        return

    if data == "cb_back_start":
        await cq.answer()
        await cq.message.edit_text(
            "⚡ <b>ZILONG BOT</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n🟢 Online",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📖 Help",     callback_data="cb_help"),
                InlineKeyboardButton("⚙️ Settings", callback_data="cb_settings"),
            ], [
                InlineKeyboardButton("📊 Status", callback_data="status_refresh"),
            ]])
        )
        return

    # ── Status panel callbacks ─────────────────

    if data == "status_refresh":
        await cq.answer("🔄 Refreshed")
        try:
            await cq.message.edit_text(
                _status_panel(),
                reply_markup=_status_kb(),
            )
        except Exception:
            pass
        return

    if data == "status_cancel":
        await cq.answer("⛔ Cancelling ALL tasks…")
        await cancelTask("Cancelled via /status panel")
        try:
            await cq.message.edit_text(
                _status_panel(),
                reply_markup=_status_kb(),
            )
        except Exception:
            pass
        return

    if data.startswith("status_kill|"):
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
            await cq.message.edit_text(_status_panel(), reply_markup=_status_kb())
        except Exception:
            pass
        return

    # ── Stats refresh ──────────────────────────
    if data == "stats_refresh":
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
        return

    # ── Task launch ────────────────────────────
    if data in ["normal", "zip", "unzip", "undzip", "cc_convert", "cc_resize", "cc_compress"]:
        if data.startswith("cc_") and not BOT.Options.cc_api_keys:
            await cq.answer("CloudConvert API key missing — use /addcc YOUR_KEY.", show_alert=True)
            return
        BOT.Mode.type = data
        await cq.message.delete()
        MSG.status_msg = await colab_bot.send_message(
            chat_id=OWNER, text="⏳ <i>Starting...</i>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⛔ Cancel", callback_data="cancel"),
                InlineKeyboardButton("📊 Status", callback_data="status_refresh"),
            ]]),
        )
        BOT.State.task_going = True
        BOT.State.started    = False
        BotTimes.start_time  = datetime.now()
        TaskInfo.reset()
        TaskInfo.set(phase="download", started_at=datetime.now().timestamp())
        BOT.TASK = get_event_loop().create_task(taskScheduler())
        await BOT.TASK
        BOT.State.task_going = False
        TaskInfo.reset()
        return

    if data in ["seedr_cc_convert", "seedr_cc_hardsub"]:
        if not BOT.Options.cc_api_keys:
            await cq.answer("CloudConvert API key missing — use /addcc YOUR_KEY.", show_alert=True)
            return
        if not str(SEEDR_USERNAME or "").strip() or not str(SEEDR_PASSWORD or "").strip():
            await cq.answer("Seedr credentials are missing in your Colab launcher.", show_alert=True)
            return
        magnet = _link_sessions.get(cq.message.id, BOT.SOURCE or [""])[0].strip()
        if not magnet.startswith("magnet:?xt=urn:btih:"):
            await cq.answer("Seedr mode currently needs a magnet link.", show_alert=True)
            return
        if BOT.State.task_going:
            await cq.answer("A task is already running — /cancel first.", show_alert=True)
            return

        await cq.message.delete()
        MSG.status_msg = await colab_bot.send_message(
            chat_id=OWNER,
            text="⏳ <i>Starting Seedr job...</i>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⛔ Cancel", callback_data="cancel"),
                InlineKeyboardButton("📊 Status", callback_data="status_refresh"),
            ]]),
        )
        BOT.State.task_going = True
        BOT.State.started = False
        BotTimes.start_time = datetime.now()
        TaskInfo.reset()
        TaskInfo.set(phase="process", engine="Seedr+CloudConvert", started_at=datetime.now().timestamp())
        BOT.Mode.type = data
        if data == "seedr_cc_convert":
            BOT.TASK = get_event_loop().create_task(Seedr_CC_Convert_Handler(magnet))
        else:
            BOT.TASK = get_event_loop().create_task(Seedr_CC_Hardsub_Handler(magnet))
        await BOT.TASK
        BOT.State.task_going = False
        TaskInfo.reset()
        return

    # ── FreeConvert Hardsub (magnet) — CONCURRENT, jusqu'à 3 en parallèle ──
    # Ne bloque pas sur BOT.State.task_going : peut tourner en même temps
    # qu'un autre hardsub FC, ou même pendant un leech normal en cours.
    if data == "seedr_fc_hardsub":
        if not BOT.Options.fc_api_keys:
            await cq.answer("FreeConvert API key missing — use /addfc YOUR_KEY.", show_alert=True)
            return
        if not str(SEEDR_USERNAME or "").strip() or not str(SEEDR_PASSWORD or "").strip():
            await cq.answer("Seedr credentials are missing in your Colab launcher.", show_alert=True)
            return
        magnet = _link_sessions.get(cq.message.id, BOT.SOURCE or [""])[0].strip()
        if not magnet.startswith("magnet:?xt=urn:btih:"):
            await cq.answer("Seedr mode currently needs a magnet link.", show_alert=True)
            return

        await cq.message.edit_text(
            "🆓 <b>FREECONVERT HARDSUB</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Choisis la qualité de sortie :\n"
            "<i>Une résolution plus basse = traitement plus rapide.</i>",
            reply_markup=_fc_quality_kb("magnet"),
        )
        _link_sessions[cq.message.id] = [magnet]
        return

    if data.startswith("fc_res|magnet|"):
        code = data.split("|", 2)[2]
        resize = FC_RESOLUTIONS.get(code)
        session = _link_sessions.pop(cq.message.id, None)
        magnet = (session or (BOT.SOURCE or [""]))[0].strip()
        if not magnet.startswith("magnet:?xt=urn:btih:"):
            await cq.answer("Session expirée ou déjà lancé.", show_alert=True)
            return

        await cq.answer("🆓 Hardsub FreeConvert démarré (en parallèle)")
        await cq.message.delete()
        job_status_msg = await colab_bot.send_message(
            chat_id=OWNER,
            text="⏳ <i>Starting Seedr + FreeConvert hardsub job...</i>",
        )
        get_event_loop().create_task(Seedr_FC_Hardsub_Handler(magnet, job_status_msg, resize=resize))
        return

    # ── FreeConvert Hardsub sur lien direct (sous-titre fourni manuellement) ──
    # Concurrent lui aussi. Le sous-titre est associé via reply-to-message,
    # pour supporter plusieurs demandes en attente simultanément.
    if data == "fc_hardsub_manual":
        if not BOT.Options.fc_api_keys:
            await cq.answer("FreeConvert API key missing — use /addfc YOUR_KEY.", show_alert=True)
            return
        url = _link_sessions.get(cq.message.id, BOT.SOURCE or [""])[0].strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            await cq.answer("This option needs a direct HTTP(S) link.", show_alert=True)
            return

        await cq.message.edit_text(
            "🆓 <b>FREECONVERT HARDSUB</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Choisis la qualité de sortie :\n"
            "<i>Une résolution plus basse = traitement plus rapide.</i>",
            reply_markup=_fc_quality_kb("direct"),
        )
        _link_sessions[cq.message.id] = [url]
        return

    if data.startswith("fc_res|direct|"):
        code = data.split("|", 2)[2]
        resize = FC_RESOLUTIONS.get(code)
        session = _link_sessions.pop(cq.message.id, None)
        url = (session or (BOT.SOURCE or [""]))[0].strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            await cq.answer("Session expirée ou déjà lancé.", show_alert=True)
            return

        name = os.path.basename(urlparse(url).path) or "video.mp4"

        prompt = await cq.message.edit_text(
            "🆓 <b>FREECONVERT HARDSUB</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"<code>{name}</code>\n\n"
            "📎 <b>Réponds à ce message</b> (reply) avec le fichier de sous-titres "
            "(<code>.ass</code> ou <code>.srt</code>) à utiliser.\n\n"
            "<i>Le style (police, gras, contour...) sera appliqué automatiquement. "
            "Tu peux lancer un autre lien pendant que celui-ci tourne.</i>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✖ Annuler", callback_data="fc_hardsub_cancel"),
            ]]),
        )
        _pending_fc_subtitle[prompt.id] = {"url": url, "name": name, "resize": resize}
        return

    if data == "fc_hardsub_cancel":
        _pending_fc_subtitle.pop(cq.message.id, None)
        await cq.message.edit_text("❌ Hardsub annulé.")
        return

    # ── Video Converter local (ffmpeg) ─────────────────────────
    if data == "vidtool_convert":
        pending = _pending_video.get(cq.message.id)
        if not pending:
            await cq.answer("Session expirée, renvoie la vidéo.", show_alert=True)
            return
        await cq.message.edit_text(
            f"📹 <code>{pending['name']}</code>\n\n<b>Choisis la résolution de sortie :</b>",
            reply_markup=_video_res_kb(),
        )
        return

    if data.startswith("vidres|"):
        code = data.split("|", 1)[1]
        height = LOCAL_RESOLUTIONS.get(code)
        pending = _pending_video.pop(cq.message.id, None)
        if not pending or not height:
            await cq.answer("Session expirée ou déjà lancé.", show_alert=True)
            return

        await cq.answer(f"🎞 Conversion {height}p démarrée")
        await cq.message.delete()
        job_status_msg = await colab_bot.send_message(
            chat_id=OWNER,
            text=f"⏳ <i>Starting local video conversion ({height}p)...</i>",
        )
        get_event_loop().create_task(
            Local_Video_Convert_Handler(pending["source_message"], height, job_status_msg)
        )
        return

    if data == "vidtool_cancel":
        _pending_video.pop(cq.message.id, None)
        _pending_merge.pop(cq.message.id, None)
        await cq.message.edit_text("❌ Annulé.")
        return

    if data == "vidtool_merge":
        pending = _pending_video.pop(cq.message.id, None)
        if not pending:
            await cq.answer("Session expirée, renvoie la vidéo.", show_alert=True)
            return
        prompt = await cq.message.edit_text(
            f"🔊 <code>{pending['name']}</code>\n\n"
            "📎 <b>Réponds à ce message</b> (reply) avec le fichier audio "
            "à fusionner avec cette vidéo.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✖ Annuler", callback_data="vidtool_cancel"),
            ]]),
        )
        _pending_merge[prompt.id] = {"source_message": pending["source_message"]}
        return

    # ════════════════════════════════════════════
    #  STREAM EXTRACTOR
    # ════════════════════════════════════════════

    if data == "sx_open":
        url = (BOT.SOURCE or [None])[0]
        if not url:
            await cq.answer("No URL found.", show_alert=True); return

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
        return

    if data == "sx_type":
        session = get_session(chat_id)
        if not session:
            await cq.answer("Session expired.", show_alert=True); return
        await _show_type_menu(cq.message, session)
        return

    if data == "sx_video":
        session = get_session(chat_id)
        if not session: await cq.answer("Session expired.", show_alert=True); return
        if not session["video"]: await cq.answer("No video tracks.", show_alert=True); return
        await cq.message.edit_text(
            "🎬 <b>VIDEO TRACKS</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<i>flag  resolution  [codec]  size</i>\n\nTap to download:",
            reply_markup=kb_video(session)
        )
        return

    if data == "sx_audio":
        session = get_session(chat_id)
        if not session: await cq.answer("Session expired.", show_alert=True); return
        if not session["audio"]: await cq.answer("No audio tracks.", show_alert=True); return
        await cq.message.edit_text(
            "🎵 <b>AUDIO TRACKS</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<i>flag  language  [codec]  bitrate  size</i>\n\nTap to download:",
            reply_markup=kb_audio(session)
        )
        return

    if data == "sx_subs":
        session = get_session(chat_id)
        if not session: await cq.answer("Session expired.", show_alert=True); return
        if not session["subs"]: await cq.answer("No subtitles.", show_alert=True); return
        await cq.message.edit_text(
            "💬 <b>SUBTITLES</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<i>flag  language  [format]</i>\n\nTap to download:",
            reply_markup=kb_subs(session)
        )
        return

    if data == "sx_back":
        clear_session(chat_id)
        n     = len([l for l in (BOT.SOURCE or []) if l.strip()])
        label = "🏮 Lien YTDL" if BOT.Mode.ytdl else "🔗 Lien détecté"
        await cq.message.edit_text(
            f"{label}\n<code>{n}</code> source(s) · <b>Choisis un mode :</b>",
            reply_markup=_mode_keyboard()
        )
        return

    # ── Stream download ────────────────────────
    if data.startswith("sx_dl_"):
        session = get_session(chat_id)
        if not session: await cq.answer("Session expired.", show_alert=True); return

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
                await colab_bot.send_message(chat_id=OWNER, text=media_info)
            clear_session(chat_id)

        except Exception as e:
            logging.error(f"[StreamDL] {e}")
            try:
                await cq.message.edit_text(
                    f"🎞 <b>STREAM EXTRACTOR</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"❌ <b>Error:</b> <code>{e}</code>"
                )
            except Exception: pass
        return

    # ── Settings callbacks ─────────────────────
    if data == "video":
        await cq.message.edit_text(
            "🎥 <b>VIDEO SETTINGS</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Convert  <code>{BOT.Setting.convert_video}</code>\n"
            f"Split    <code>{BOT.Setting.split_video}</code>\n"
            f"Format   <code>{BOT.Options.video_out.upper()}</code>\n"
            f"Quality  <code>{BOT.Setting.convert_quality}</code>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✂️ Split",   callback_data="split-true"),
                 InlineKeyboardButton("🗜 Zip",     callback_data="split-false")],
                [InlineKeyboardButton("🔄 Convert", callback_data="convert-true"),
                 InlineKeyboardButton("🚫 No",      callback_data="convert-false")],
                [InlineKeyboardButton("🎬 MP4",     callback_data="mp4"),
                 InlineKeyboardButton("📦 MKV",     callback_data="mkv")],
                [InlineKeyboardButton("🔝 High",    callback_data="q-High"),
                 InlineKeyboardButton("📉 Low",     callback_data="q-Low")],
                [InlineKeyboardButton("⏎ Back",     callback_data="back")],
            ]))
    elif data == "cc":
        cc_ready = "Ready" if BOT.Options.cc_api_keys else "Missing"
        await cq.message.edit_text(
            "☁️ <b>CLOUDCONVERT SETTINGS</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"API Key  <code>{cc_ready}</code>\n"
            f"Mode     <code>{cc_mode_label(BOT.Options.cc_engine_mode)}</code>\n"
            f"Preset   <code>{quality_label(BOT.Options.cc_quality_profile)}</code>\n"
            f"Resize   <code>{resize_label(BOT.Options.cc_resize)}</code>\n"
            f"Target   <code>{BOT.Setting.cc_target_size}</code>\n\n"
            "These settings are used by CC Convert, CC Resize, and CC Compress.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⚖️ CC Mode", callback_data="cc-mode"),
                 InlineKeyboardButton("🎚 Preset", callback_data="cc-quality")],
                [InlineKeyboardButton("📐 Resize", callback_data="cc-resize"),
                 InlineKeyboardButton("🗜 Target", callback_data="cc-target")],
                [InlineKeyboardButton("⏮ Back", callback_data="back")],
            ]))
    elif data == "caption":
        await cq.message.edit_text(
            f"✏️ <b>CAPTION STYLE</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Current: <code>{BOT.Setting.caption}</code>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Monospace", callback_data="code-Monospace"),
                 InlineKeyboardButton("Bold",      callback_data="b-Bold")],
                [InlineKeyboardButton("Italic",    callback_data="i-Italic"),
                 InlineKeyboardButton("Underline", callback_data="u-Underlined")],
                [InlineKeyboardButton("Plain",     callback_data="p-Regular")],
                [InlineKeyboardButton("⏎ Back",    callback_data="back")],
            ]))
    elif data == "thumb":
        await cq.message.edit_text(
            f"🖼 <b>THUMBNAIL</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Status: {'✅ Set' if BOT.Setting.thumbnail else '❌ None'}\n\n"
            "Send a photo to update.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 Delete", callback_data="del-thumb")],
                [InlineKeyboardButton("⏎ Back",   callback_data="back")],
            ]))
    elif data == "del-thumb":
        if BOT.Setting.thumbnail:
            try: os.remove(Paths.THMB_PATH)
            except Exception: pass
        BOT.Setting.thumbnail = False
        await send_settings(client, cq.message, cq.message.id, False)
    elif data == "set-prefix":
        await cq.message.edit_text("Reply with your <b>prefix</b> text:")
        BOT.State.prefix = True
    elif data == "set-suffix":
        await cq.message.edit_text("Reply with your <b>suffix</b> text:")
        BOT.State.suffix = True
    elif data in ["code-Monospace","p-Regular","b-Bold","i-Italic","u-Underlined"]:
        r = data.split("-"); BOT.Options.caption = r[0]; BOT.Setting.caption = r[1]
        await send_settings(client, cq.message, cq.message.id, False)
    elif data in ["split-true","split-false"]:
        BOT.Options.is_split    = data == "split-true"
        BOT.Setting.split_video = "Split" if data == "split-true" else "Zip"
        await send_settings(client, cq.message, cq.message.id, False)
    elif data in ["convert-true","convert-false","mp4","mkv","q-High","q-Low"]:
        if   data == "convert-true":  BOT.Options.convert_video = True;  BOT.Setting.convert_video = "Yes"
        elif data == "convert-false": BOT.Options.convert_video = False; BOT.Setting.convert_video = "No"
        elif data == "q-High": BOT.Setting.convert_quality = "High"; BOT.Options.convert_quality = True
        elif data == "q-Low":  BOT.Setting.convert_quality = "Low";  BOT.Options.convert_quality = False
        else: BOT.Options.video_out = data
        await send_settings(client, cq.message, cq.message.id, False)
    elif data == "cc-mode":
        cycle = ["balanced", "economy"]
        cur = str(BOT.Options.cc_engine_mode or "balanced").lower()
        nxt = cycle[(cycle.index(cur) + 1) % len(cycle)] if cur in cycle else "balanced"
        BOT.Options.cc_engine_mode = nxt
        BOT.Setting.cc_engine_mode = cc_mode_label(nxt)
        await cq.answer(BOT.Setting.cc_engine_mode, show_alert=True)
        await send_settings(client, cq.message, cq.message.id, False)
    elif data == "cc-quality":
        cycle = ["fast", "balanced", "small", "best"]
        cur = str(BOT.Options.cc_quality_profile or "balanced").lower()
        nxt = cycle[(cycle.index(cur) + 1) % len(cycle)] if cur in cycle else "balanced"
        BOT.Options.cc_quality_profile = nxt
        BOT.Setting.cc_quality_profile = quality_label(nxt)
        await cq.answer(BOT.Setting.cc_quality_profile, show_alert=True)
        await send_settings(client, cq.message, cq.message.id, False)
    elif data == "cc-resize":
        cycle = [0, 480, 720, 1080]
        cur = int(BOT.Options.cc_resize or 0)
        nxt = cycle[(cycle.index(cur) + 1) % len(cycle)] if cur in cycle else 720
        BOT.Options.cc_resize = nxt
        BOT.Setting.cc_resize = resize_label(nxt)
        await cq.answer(BOT.Setting.cc_resize, show_alert=True)
        await send_settings(client, cq.message, cq.message.id, False)
    elif data == "cc-target":
        cycle = [50, 100, 200, 500]
        cur = int(BOT.Options.cc_target_size_mb or 100)
        nxt = cycle[(cycle.index(cur) + 1) % len(cycle)] if cur in cycle else 100
        BOT.Options.cc_target_size_mb = nxt
        BOT.Setting.cc_target_size = f"{nxt} MB"
        await cq.answer(BOT.Setting.cc_target_size, show_alert=True)
        await send_settings(client, cq.message, cq.message.id, False)
    elif data == "autofwd":
        if not BOT.Options.dump_ids:
            await cq.answer("Ajoute d'abord un canal avec /add @channel", show_alert=True)
        else:
            BOT.Options.auto_forward = not BOT.Options.auto_forward
            BOT.Setting.auto_forward = "On" if BOT.Options.auto_forward else "Off"
            await cq.answer(f"AutoFwd {BOT.Setting.auto_forward}", show_alert=True)
            await send_settings(client, cq.message, cq.message.id, False)
    elif data == "dumps":
        await cq.message.edit_text(_dumps_text(), reply_markup=_dumps_kb())
    elif data.startswith("dump_remove|"):
        raw_id = data.split("|", 1)[1]
        try:
            target = int(raw_id)
        except ValueError:
            target = raw_id
        if target in BOT.Options.dump_ids:
            BOT.Options.dump_ids.remove(target)
            if not BOT.Options.dump_ids:
                BOT.Options.auto_forward = False
                BOT.Setting.auto_forward = "Off"
            await cq.answer("🗑 Retiré")
        else:
            await cq.answer("Déjà retiré.")
        await cq.message.edit_text(_dumps_text(), reply_markup=_dumps_kb())
    elif data == "apikeys":
        await cq.message.edit_text(_apikeys_text(), reply_markup=_apikeys_kb())
    elif data.startswith("apikey_remove|"):
        _, kind, idx_str = data.split("|")
        idx = int(idx_str)
        target_list = BOT.Options.cc_api_keys if kind == "cc" else BOT.Options.fc_api_keys
        if 0 <= idx < len(target_list):
            target_list.pop(idx)
            await cq.answer("🗑 Retirée")
        else:
            await cq.answer("Déjà retirée.")
        await cq.message.edit_text(_apikeys_text(), reply_markup=_apikeys_kb())
    elif data in ["media","document"]:
        BOT.Options.stream_upload = data == "media"
        BOT.Setting.stream_upload = "Media" if data == "media" else "Document"
        await send_settings(client, cq.message, cq.message.id, False)
    elif data == "close":
        await cq.message.delete()
    elif data == "back":
        await send_settings(client, cq.message, cq.message.id, False)
    elif data == "cancel":
        await cancelTask("Cancelled by user")


async def _show_type_menu(msg, session):
    v = len(session["video"])
    a = len(session["audio"])
    s = len(session["subs"])
    title = session["title"]
    await msg.edit_text(
        "🎞 <b>STREAM EXTRACTOR</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📌  <b>{title}</b>\n\n"
        f"🎬  Video tracks     <code>{v}</code>\n"
        f"🎵  Audio tracks     <code>{a}</code>\n"
        f"💬  Subtitles        <code>{s}</code>\n\n"
        "Choose track type:",
        reply_markup=kb_type(v, a, s)
    )


# ══════════════════════════════════════════════
#  Photo → thumbnail
# ══════════════════════════════════════════════

@colab_bot.on_message(filters.photo & filters.private)
async def handle_photo(client, message):
    msg = await message.reply_text("⏳ <i>Saving thumbnail...</i>")
    if await setThumbnail(message):
        await msg.edit_text("✅ Thumbnail updated.")
        await message.delete()
    else:
        await msg.edit_text("❌ Could not set thumbnail.")
    await sleep(10)
    await message_deleter(message, msg)


# ══════════════════════════════════════════════
#  Vidéo envoyée directement → menu d'outils locaux
# ══════════════════════════════════════════════

_VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".ts", ".m2ts", ".flv", ".wmv")


@colab_bot.on_message(
    (filters.video | filters.audio | filters.voice | filters.document) & filters.private,
    group=-1,
)
async def handle_incoming_video(client, message):
    if not _owner(message):
        message.continue_propagation()
        return

    # ── Cas 1 : c'est l'audio attendu pour un merge en cours ──────────────
    reply_id = message.reply_to_message_id
    pending_merge = _pending_merge.get(reply_id) if reply_id else None
    if pending_merge:
        is_audio = False
        audio_name = "audio"
        if message.audio:
            is_audio = True
            audio_name = message.audio.file_name or "audio.mp3"
        elif message.voice:
            is_audio = True
            audio_name = "voice.ogg"
        elif message.document:
            mime = (message.document.mime_type or "")
            name = (message.document.file_name or "")
            if mime.startswith("audio/") or name.lower().endswith(_AUDIO_EXTS):
                is_audio = True
                audio_name = name or "audio"

        if is_audio:
            _pending_merge.pop(reply_id, None)
            status_msg = await message.reply_text("⏳ <i>Audio reçu, démarrage de la fusion...</i>")
            await message.delete()

            os.makedirs(Paths.WORK_PATH, exist_ok=True)
            ext = os.path.splitext(audio_name)[1] or ".mp3"
            audio_path = os.path.join(Paths.WORK_PATH, f"merge_audio_{uuid4().hex[:8]}{ext}")
            await message.download(file_name=audio_path)

            get_event_loop().create_task(
                Local_Merge_Handler(pending_merge["source_message"], audio_path, status_msg)
            )
            return
        # Reply présent mais c'est pas un fichier audio -> on laisse tomber
        # ce cas précis et on continue l'analyse normale ci-dessous.

    # ── Cas 2 : c'est une vidéo -> affiche le menu d'outils ────────────────
    is_video = False
    display_name = "video.mp4"
    if message.video:
        is_video = True
        display_name = message.video.file_name or "video.mp4"
    elif message.document:
        mime = (message.document.mime_type or "")
        name = (message.document.file_name or "")
        if mime.startswith("video/") or name.lower().endswith(_VIDEO_EXTS):
            is_video = True
            display_name = name or "video.mp4"

    if not is_video:
        message.continue_propagation()
        return

    prompt = await message.reply_text(
        f"📹 <code>{display_name}</code>\n\n<b>Choisis une action :</b>",
        reply_markup=_video_tools_kb(),
        quote=True,
    )
    _pending_video[prompt.id] = {"source_message": message, "name": display_name}


# ══════════════════════════════════════════════
#  Document → sous-titre pour FC Hardsub manuel
# ══════════════════════════════════════════════

@colab_bot.on_message(filters.document & filters.private)
async def handle_subtitle_document(client, message):
    if not _owner(message):
        return
    if not _pending_fc_subtitle:
        return  # Aucun hardsub en attente de sous-titre, on ignore ce fichier

    # 1. Priorité au reply explicite (lève l'ambiguïté si plusieurs en attente)
    reply_id = message.reply_to_message_id
    pending = _pending_fc_subtitle.get(reply_id) if reply_id else None

    # 2. Fallback : s'il n'y a qu'UNE seule demande en attente, pas besoin de reply
    if pending is None:
        if len(_pending_fc_subtitle) == 1:
            reply_id, pending = next(iter(_pending_fc_subtitle.items()))
        else:
            await message.reply_text(
                "⚠️ Plusieurs hardsub sont en attente d'un sous-titre — "
                "réponds (reply) directement au message concerné avec ce fichier.",
                quote=True,
            )
            return

    file_name = message.document.file_name or ""
    ext = os.path.splitext(file_name)[1].lower()
    if ext not in (".ass", ".srt", ".ssa"):
        await message.reply_text(
            "❌ Envoie un fichier <code>.ass</code> ou <code>.srt</code> valide.",
            quote=True,
        )
        return

    _pending_fc_subtitle.pop(reply_id, None)

    status_msg = await message.reply_text("⏳ <i>Sous-titre reçu, démarrage du hardsub...</i>")
    await message.delete()

    os.makedirs(Paths.WORK_PATH, exist_ok=True)
    subtitle_path = os.path.join(Paths.WORK_PATH, f"manual_sub_{uuid4().hex[:8]}{ext}")
    await message.download(file_name=subtitle_path)

    # Fire-and-forget : ne bloque pas ce handler, donc le bot reste réactif
    # pour recevoir d'autres liens/sous-titres pendant que celui-ci tourne.
    get_event_loop().create_task(
        Direct_FC_Hardsub_Handler(pending["url"], pending["name"], subtitle_path, status_msg, resize=pending.get("resize"))
    )


# ══════════════════════════════════════════════
#  Import nyaa_tracker (registers its handlers)
# ══════════════════════════════════════════════

try:
    import colab_leecher.nyaa_tracker
    logging.info("📡 Nyaa tracker loaded")
except Exception as e:
    logging.warning(f"Nyaa tracker not loaded: {e}")


logging.info("⚡ Zilong started.")
get_event_loop().create_task(_startup_welcome())
colab_bot.run()
