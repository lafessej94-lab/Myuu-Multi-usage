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
from colab_leecher.access import is_allowed as access_is_allowed, is_banned as access_is_banned
from colab_leecher.claude_agent import start_agent, stop_agent, is_agent_running
from colab_leecher.status_slideshow import StatusSlideshow
from colab_leecher.cloudconvert import cc_mode_label, quality_label, resize_label
from colab_leecher.house_style import STYLE_PRESET_LABELS
from colab_leecher.utility.handler import (
    Direct_CC_Hardsub_Handler,
    Direct_FC_Hardsub_Handler,
    Local_Compress_Handler,
    Local_ManualShot_Handler,
    Local_Merge_Handler,
    Local_Metadata_Handler,
    Local_Mute_Handler,
    Local_Rename_Handler,
    Local_Sample_Handler,
    Local_Screenshots_Handler,
    Local_Split_Handler,
    Local_Subs_Handler,
    Local_Thumb_Handler,
    Local_ToAudio_Handler,
    Local_Trim_Handler,
    Local_Video_Convert_Handler,
    Seedr_CC_Convert_Handler,
    Seedr_CC_Hardsub_Handler,
    Seedr_FC_Hardsub_Handler,
    cancelTask,
)
from colab_leecher.utility.variables import (
    BOT, MSG, ActiveJobs, BotTimes, Paths, Messages, ProcessTracker, TaskInfo, Aria2c,
)
from colab_leecher.utility.task_manager import taskScheduler
from colab_leecher.utility.helper import (
    isLink, setThumbnail, message_deleter, send_settings,
    sizeUnit, getTime, is_ytdl_link, fileType, _pct_bar, _speed_emoji,
)
from colab_leecher.downlader.aria2 import aria2_Download
from colab_leecher.house_style import apply_house_style
from colab_leecher.stream_extractor import (
    analyse, get_session, clear_session,
    kb_type, kb_video, kb_audio, kb_subs,
    dl_video, dl_audio, dl_sub, track_flags,
)

# ── Sectioned link menu (Download / Inspect / Process / Cloud), mirroring
#    services/url_views.py's navigation pattern. Every leaf button below
#    still carries the SAME callback_data ("normal", "cc_convert",
#    "seedr_fc_hardsub", "sx_open", ...) that the existing callbacks()
#    if/elif chain already handles — only the pre-menu navigation changed.
#    See colab_leecher/link_menu.py for the pure text/keyboard builders.
from colab_leecher.link_menu import (
    kind_label as _lk_kind_label,
    link_header as _lk_header,
    main_kb as _lk_main_kb,
    download_kb as _lk_download_kb,
    inspect_kb as _lk_inspect_kb,
    process_kb as _lk_process_kb,
    cloud_kb as _lk_cloud_kb,
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
# _link_sessions : message_id (du message "Choisis une section :") -> liste
#   de sources. Nécessaire pour que plusieurs liens envoyés d'affilée ne se
#   marchent pas dessus sur le global BOT.SOURCE — chaque bouton retrouve SON
#   lien via le message auquel il est attaché, pas via BOT.SOURCE (qui ne
#   reflète que le tout dernier lien envoyé). Sert aussi de "token" implicite
#   pour la navigation par sections (lk|sec|...).
# _pending_fc_subtitle : message_id (du message "Envoie le sous-titre...") ->
#   {"url":..., "name":..., "resize":..., "style_key":...}. Permet plusieurs
#   hardsub FC en attente de sous-titre en même temps — l'utilisateur répond
#   (reply) au bon message avec le bon fichier pour lever l'ambiguïté.
_link_sessions: dict[int, list[str]] = {}
_pending_fc_subtitle: dict[int, dict] = {}

# _pending_style_sub : message_id (du prompt Oui/Non) -> {"path": str, "ext": str}
# Flow indépendant de tout hardsub — un sous-titre envoyé "à froid" au bot,
# on propose juste d'appliquer le house style (Trebuchet MS 22) et de le
# renvoyer, sans lancer aucun job vidéo.
_pending_style_sub: dict[int, dict] = {}

# _pending_style_choice : message_id (du prompt "Choisis le style") ->
#   {"flow": "fc_magnet"|"fc_direct"|"cc_direct", ...données du flow...}.
# Étape intermédiaire insérée après le choix de résolution (FC magnet, FC
# direct, CC direct) et avant l'étape suivante (lancement direct, ou prompt
# sous-titre) — le flow "cc_seedr" n'en a PAS besoin, il stocke directement
# le style choisi dans _cc_hardsub_session (déjà keyé par message_id).
_pending_style_choice: dict[int, dict] = {}

_AUDIO_EXTS = (".mp3", ".m4a", ".flac", ".wav", ".ogg", ".aac", ".opus")


def _style_kb(flow: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"🅰️ {STYLE_PRESET_LABELS.get('a', 'Style A')}", callback_data=f"hs_style|{flow}|a"),
        InlineKeyboardButton(f"🅱️ {STYLE_PRESET_LABELS.get('b', 'Style B')}", callback_data=f"hs_style|{flow}|b"),
    ]])


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
                f"👋 <b>Heyo back, {display}</b>\n"
                "💖 <b>Myuu࣪ ☾ is online</b>\n\n"
                "Send a link, magnet, or path to begin.\n"
                "Use /start for the full menu and /status for the live dashboard."
            )
            await colab_bot.send_message(chat_id=OWNER, text=text)
            return
        except Exception as exc:
            logging.warning("Startup welcome attempt failed: %s", exc)


def _owner(m): return m.chat.id == OWNER
def _can_use(m): return m.chat.id == OWNER or (access_is_allowed(m.chat.id) and not access_is_banned(m.chat.id))
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


# Mêmes codes/labels que FreeConvert — juste préfixé cc_res pour ce flow-ci.
# _cc_direct_sessions : token (8 hex chars, embedded dans callback_data) ->
# url. Volontairement PAS indexé par message_id (contrairement à
# _link_sessions) — le token voyage dans le bouton lui-même, donc aucune
# dépendance à ce que message.id reste stable entre les callbacks.
_cc_direct_sessions: dict[str, str] = {}


def _cc_direct_quality_kb(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 Qualité d'origine", callback_data=f"ccdirect_res|{token}|orig")],
        [InlineKeyboardButton("360p", callback_data=f"ccdirect_res|{token}|360"),
         InlineKeyboardButton("480p", callback_data=f"ccdirect_res|{token}|480")],
        [InlineKeyboardButton("720p", callback_data=f"ccdirect_res|{token}|720")],
    ])


# code du menu ("orig"/"360"/"480"/"720") -> chaîne attendue par
# hardsub_remote_url() de CloudConvert ("original"/"360p"/"480p"/"720p").
_CC_RES_CODE_TO_LABEL: dict[str, str] = {
    "orig": "original", "360": "360p", "480": "480p", "720": "720p",
}

# _pending_cc_subtitle : message_id (du prompt "envoie le sous-titre") ->
# {"url": str, "name": str, "resolution": str|None, "style_key": str}.
# Namespace séparé de _pending_fc_subtitle pour ne pas mélanger les deux
# flows si les deux flows tournent en même temps.
_pending_cc_subtitle: dict[int, dict] = {}


# ── CC Hardsub : résolution puis style puis vitesse d'encodage, choisis avant de lancer ──
# _cc_hardsub_session : message_id -> {"magnet": str, "resolution": str|None, "style_key": str|None}
_cc_hardsub_session: dict[int, dict] = {}

CC_RESOLUTION_LABELS: dict[str, str] = {
    "original": "🎬 Qualité d'origine",
    "480p": "480p",
    "720p": "720p",
    "1080p": "1080p",
}

CC_SPEED_LABELS: dict[str, str] = {
    "superfast": "⚡ Superfast",
    "veryfast": "🚀 Veryfast",
    "fast": "🏃 Fast",
}


def _cc_res_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(CC_RESOLUTION_LABELS["original"], callback_data="cc_res|original")],
        [InlineKeyboardButton("480p", callback_data="cc_res|480p"),
         InlineKeyboardButton("720p", callback_data="cc_res|720p")],
        [InlineKeyboardButton("1080p", callback_data="cc_res|1080p")],
    ])


def _cc_speed_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(CC_SPEED_LABELS["superfast"], callback_data="cc_speed|superfast")],
        [InlineKeyboardButton(CC_SPEED_LABELS["veryfast"], callback_data="cc_speed|veryfast")],
        [InlineKeyboardButton(CC_SPEED_LABELS["fast"], callback_data="cc_speed|fast")],
    ])


# ══════════════════════════════════════════════
#  /start
# ══════════════════════════════════════════════

@colab_bot.on_message(filters.command("start") & filters.private)
async def start(client, message):
    await message.delete()

    if _owner(message):
        await message.reply_text(
            "💖 <b>Myuu࣪ ☾ BOT</b>\n"
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
        "💖 <b>Myuu࣪ ☾ BOT</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
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
        "  /apikeys   — list/remove API keys\n"
        "  /adduser   — give a user access to the bot (alias /allow)\n"
        "  /deluser   — remove a user's access (alias /deny)\n"
        "  /users     — list authorized users (alias /allowed)\n"
        "  /ban /unban — block/unblock a user entirely\n"
        "  /banned    — list banned users\n"
        "  /broadcast — reply to a message to send it to all authorized users\n"
        "  /Relève    — réveille Claude (agent auto, owner uniquement)\n"
        "  /Arise     — rendort Claude\n\n"
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
        await client.send_document(chat_id=OWNER, document=Paths.LOG_PATH, caption="Myuu runtime log")
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


# ── Intégration Claude : /Relève réveille l'agent (surveillance Erai-raws
#    + pipeline seedr/hardsub automatique), /Arise le rendort. Owner
#    uniquement — l'agent n'agit que pour lui, même si d'autres users ont
#    accès au bot par ailleurs (voir colab_leecher/claude_agent.py).
@colab_bot.on_message(filters.command("Relève") & filters.private)
async def releve_cmd(client, message):
    if not _owner(message):
        return
    await message.delete()
    if start_agent():
        await message.reply_text(
            "🤖 <b>Claude est réveillé</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Surveillance de nyaa.si/Erai-raws active (toutes les 20s).\n"
            "Nouvel épisode détecté → hardsub FC 360p puis 720p (style B) automatique.\n\n"
            "Utilise /Arise pour l'arrêter."
        )
    else:
        await message.reply_text("⚠️ Claude est déjà actif.")


@colab_bot.on_message(filters.command("Arise") & filters.private)
async def arise_cmd(client, message):
    if not _owner(message):
        return
    await message.delete()
    if stop_agent():
        await message.reply_text("💤 <b>Claude se rendort.</b>\nTu peux réutiliser le bot normalement.")
    else:
        await message.reply_text("⚠️ Claude n'était pas actif.")


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


@colab_bot.on_message(filters.command("removerename") & filters.private)
async def remove_rename_cmd(client, message):
    """Retire le custom_name en cours (posé via /rename ou /setname) —
    les prochains uploads (leech, hardsub FC/CC/local, zip) reprennent
    leur nom automatique par défaut."""
    if not BOT.Options.custom_name:
        msg = await message.reply_text("ℹ️ Aucun rename actif.", quote=True)
    else:
        old_name = BOT.Options.custom_name
        BOT.Options.custom_name = ""
        msg = await message.reply_text(
            f"🗑 Rename retiré (était : <code>{old_name}</code>)",
            quote=True,
        )
    await sleep(10); await message_deleter(message, msg)


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


# NOTE : /adduser, /deluser, /removeuser, /users vivent désormais dans
# colab_leecher/access.py (alias /allow, /deny, /allowed) — whitelist +
# ban persistants sur disque au lieu d'une liste en mémoire perdue à
# chaque redémarrage Colab. Voir aussi /ban, /unban, /banned, /broadcast.


@colab_bot.on_message(filters.reply & filters.private)
async def setFix(client, message):
    from colab_leecher.video_menu import handle_video_text_reply
    if await handle_video_text_reply(client, message):
        return

    BOT.TargetChat = message.chat.id  # trim/shot/split/sample/rename déclenchés ici partent vers CE chat
    if BOT.State.prefix:
        BOT.Setting.prefix = message.text; BOT.State.prefix = False
        await send_settings(client, message, message.reply_to_message_id, False)
        await message.delete()
    elif BOT.State.suffix:
        BOT.Setting.suffix = message.text; BOT.State.suffix = False
        await send_settings(client, message, message.reply_to_message_id, False)
        await message.delete()


# ══════════════════════════════════════════════
#  Link handler — sectioned menu (Download / Inspect / Process / Cloud)
# ══════════════════════════════════════════════

@colab_bot.on_message(filters.create(isLink) & ~filters.photo & filters.private)
async def handle_url(client, message):
    if not _can_use(message):
        if await _is_subscribed(client, message.from_user.id):
            msg = await message.reply_text(
                "⛔ Tu n'as pas accès aux téléchargements sur ce bot.\n"
                "Demande au propriétaire de t'ajouter avec /adduser.",
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
    is_magnet = first_src.startswith("magnet:?xt=urn:btih:")
    label = _lk_kind_label(BOT.Mode.ytdl, is_magnet)

    sent = await message.reply_text(
        _lk_header(label, n),
        reply_markup=_lk_main_kb(is_magnet), quote=True,
    )
    _link_sessions[sent.id] = src


# ══════════════════════════════════════════════
#  ALL CALLBACKS
# ══════════════════════════════════════════════

@colab_bot.on_callback_query()
async def callbacks(client, cq):
    data    = cq.data
    chat_id = cq.message.chat.id
    BOT.TargetChat = chat_id  # tout ce que cette tâche déclenche (statut, upload) part vers CE chat

    # ── Labels de section non-cliquables (juste des repères visuels) ──
    if data == "noop":
        await cq.answer()
        return

    # ── Navigation du menu de lien (Download / Inspect / Process / Cloud) ──
    # Ces deux entrées ("lk|sec|<section>" et "lk|cancel") sont les SEULES
    # nouvelles callback_data ajoutées par le menu sectionné. Elles ne font
    # que reconstruire le texte/clavier via colab_leecher.link_menu (pur,
    # aucun I/O) puis éditer le message — aucun job, aucun texte de statut
    # de job n'est touché. Tous les boutons feuilles de ces claviers
    # renvoient les callback_data historiques ("normal", "cc_convert",
    # "seedr_fc_hardsub", "sx_open", ...) traités plus bas, inchangés.
    if data.startswith("lk|sec|") or data == "lk|cancel":
        session_src = _link_sessions.get(cq.message.id, BOT.SOURCE or [""])
        first = (session_src or [""])[0].strip()
        is_magnet = first.startswith("magnet:?xt=urn:btih:")
        is_http = first.startswith("http://") or first.startswith("https://")
        n = len([l for l in session_src if l.strip()])
        label = _lk_kind_label(BOT.Mode.ytdl, is_magnet)

        if data == "lk|cancel":
            _link_sessions.pop(cq.message.id, None)
            await cq.answer()
            await cq.message.delete()
            return

        section = data.split("|", 2)[2]
        await cq.answer()
        if section == "main":
            await cq.message.edit_text(_lk_header(label, n), reply_markup=_lk_main_kb(is_magnet))
        elif section == "download":
            await cq.message.edit_text(
                _lk_header(label, n, "Download", "Choisis le format de sortie."),
                reply_markup=_lk_download_kb(),
            )
        elif section == "inspect":
            await cq.message.edit_text(
                _lk_header(label, n, "Inspect", "Analyse la source avant de lancer quoi que ce soit."),
                reply_markup=_lk_inspect_kb(),
            )
        elif section == "process":
            await cq.message.edit_text(
                _lk_header(label, n, "Process", "CloudConvert / hardsub sur ce lien."),
                reply_markup=_lk_process_kb(is_http),
            )
        elif section == "cloud":
            if not is_magnet:
                await cq.answer("Cloud needs a magnet link.", show_alert=True)
                return
            await cq.message.edit_text(
                _lk_header(label, n, "Cloud", "Seedr + FreeConvert / CloudConvert."),
                reply_markup=_lk_cloud_kb(),
            )
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
                "💖 <b>Myuu࣪ ☾ BOT</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
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
            "💖 <b>Myuu࣪ ☾ BOT</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n🟢 Online",
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
        MSG.status_msg = await StatusSlideshow().start(
            BOT.TargetChat, text="⏳ <i>Starting...</i>",
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

    if data == "seedr_cc_convert":
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
        MSG.status_msg = await StatusSlideshow().start(
            BOT.TargetChat,
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
        BOT.TASK = get_event_loop().create_task(Seedr_CC_Convert_Handler(magnet))
        await BOT.TASK
        BOT.State.task_going = False
        TaskInfo.reset()
        return

    if data == "seedr_cc_hardsub":
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

        await cq.message.edit_text(
            "☁️ <b>CLOUDCONVERT HARDSUB</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Choisis la résolution de sortie :",
            reply_markup=_cc_res_kb(),
        )
        _cc_hardsub_session[cq.message.id] = {"magnet": magnet}
        return

    if data.startswith("cc_res|"):
        resolution = data.split("|", 1)[1]
        session = _cc_hardsub_session.get(cq.message.id)
        if not session:
            await cq.answer("Session expirée, renvoie le lien.", show_alert=True)
            return
        session["resolution"] = resolution
        await cq.message.edit_text(
            "☁️ <b>CLOUDCONVERT HARDSUB</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Résolution : <code>{CC_RESOLUTION_LABELS.get(resolution, resolution)}</code>\n\n"
            "Choisis le style de sous-titre :",
            reply_markup=_style_kb("cc_seedr"),
        )
        return

    # ── Choix du style (Style A / Style B) — inséré après le choix de
    # résolution sur les 4 flows hardsub. "cc_seedr" stocke directement le
    # choix dans _cc_hardsub_session (déjà keyé par message.id) puis affiche
    # le clavier de vitesse ; les 3 autres flows utilisent _pending_style_choice
    # et reprennent l'étape qui suivait la résolution AVANT cet ajout
    # (lancement direct pour fc_magnet, prompt sous-titre pour fc_direct/cc_direct).
    if data.startswith("hs_style|"):
        _, flow, style_key = data.split("|", 2)
        style_key = style_key if style_key in ("a", "b") else "a"

        if flow == "cc_seedr":
            session = _cc_hardsub_session.get(cq.message.id)
            if not session:
                await cq.answer("Session expirée, renvoie le lien.", show_alert=True)
                return
            session["style_key"] = style_key
            await cq.answer()
            await cq.message.edit_text(
                "☁️ <b>CLOUDCONVERT HARDSUB</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Résolution : <code>{CC_RESOLUTION_LABELS.get(session.get('resolution'), session.get('resolution'))}</code>\n"
                f"Style : <code>{STYLE_PRESET_LABELS.get(style_key, style_key)}</code>\n\n"
                "Choisis la vitesse d'encodage :\n"
                "<i>Plus rapide = moins de compression, fichier un peu plus lourd.</i>",
                reply_markup=_cc_speed_kb(),
            )
            return

        pending = _pending_style_choice.pop(cq.message.id, None)
        if not pending or pending.get("flow") != flow:
            await cq.answer("Session expirée, renvoie le lien.", show_alert=True)
            return

        if flow == "fc_magnet":
            await cq.answer("🆓 Hardsub FreeConvert démarré (en parallèle)")
            await cq.message.delete()
            job_status_msg = await StatusSlideshow().start(
                BOT.TargetChat,
                text="⏳ <i>Starting Seedr + FreeConvert hardsub job...</i>",
            )
            get_event_loop().create_task(
                Seedr_FC_Hardsub_Handler(
                    pending["magnet"], job_status_msg,
                    resize=pending.get("resize"), style_key=style_key,
                )
            )
            return

        if flow == "fc_direct":
            await cq.answer()
            prompt = await cq.message.edit_text(
                "🆓 <b>FREECONVERT HARDSUB</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"<code>{pending['name']}</code>\n\n"
                "📎 <b>Réponds à ce message</b> (reply) avec le fichier de sous-titres "
                "(<code>.ass</code> ou <code>.srt</code>) à utiliser.\n\n"
                "<i>Le style sélectionné sera appliqué automatiquement. "
                "Tu peux lancer un autre lien pendant que celui-ci tourne.</i>",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✖ Annuler", callback_data="fc_hardsub_cancel"),
                ]]),
            )
            _pending_fc_subtitle[prompt.id] = {
                "url": pending["url"], "name": pending["name"],
                "resize": pending.get("resize"), "style_key": style_key,
            }
            return

        if flow == "cc_direct":
            await cq.answer()
            prompt = await cq.message.edit_text(
                "☁️ <b>CLOUDCONVERT HARDSUB</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"<code>{pending['name']}</code>\n\n"
                "📎 <b>Réponds à ce message</b> (reply) avec le fichier de sous-titres "
                "(<code>.ass</code> ou <code>.srt</code>) à utiliser.\n\n"
                "<i>Le style sélectionné sera appliqué automatiquement. "
                "Tu peux lancer un autre lien pendant que celui-ci tourne.</i>",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✖ Annuler", callback_data="cc_hardsub_cancel"),
                ]]),
            )
            _pending_cc_subtitle[prompt.id] = {
                "url": pending["url"], "name": pending["name"],
                "resolution": pending.get("resolution"), "style_key": style_key,
            }
            return

        await cq.answer("Flow inconnu.", show_alert=True)
        return

    if data.startswith("cc_speed|"):
        speed = data.split("|", 1)[1]
        session = _cc_hardsub_session.pop(cq.message.id, None)
        if not session:
            await cq.answer("Session expirée ou déjà lancé.", show_alert=True)
            return
        if BOT.State.task_going:
            await cq.answer("A task is already running — /cancel first.", show_alert=True)
            return

        magnet = session["magnet"]
        resolution = session.get("resolution")
        style_key = session.get("style_key", "a")

        await cq.message.delete()
        MSG.status_msg = await StatusSlideshow().start(
            BOT.TargetChat,
            text="⏳ <i>Starting Seedr + CloudConvert hardsub job...</i>",
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
        BOT.Mode.type = "seedr_cc_hardsub"
        BOT.TASK = get_event_loop().create_task(
            Seedr_CC_Hardsub_Handler(magnet, resolution=resolution, encode_speed=speed, style_key=style_key)
        )
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

        await cq.answer()
        await cq.message.edit_text(
            "🆓 <b>FREECONVERT HARDSUB</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Choisis le style de sous-titre :",
            reply_markup=_style_kb("fc_magnet"),
        )
        _pending_style_choice[cq.message.id] = {"flow": "fc_magnet", "magnet": magnet, "resize": resize}
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

        name = BOT.Options.custom_name or os.path.basename(urlparse(url).path) or "video.mp4"

        await cq.answer()
        await cq.message.edit_text(
            "🆓 <b>FREECONVERT HARDSUB</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"<code>{name}</code>\n\n"
            "Choisis le style de sous-titre :",
            reply_markup=_style_kb("fc_direct"),
        )
        _pending_style_choice[cq.message.id] = {"flow": "fc_direct", "url": url, "name": name, "resize": resize}
        return

    # ── CloudConvert Hardsub sur lien direct (sous-titre fourni manuellement) ──
    # Même UX que le flow FreeConvert ci-dessus, juste le moteur qui change.
    if data == "cc_hardsub_manual":
        if not BOT.Options.cc_api_keys:
            await cq.answer("CloudConvert API key missing — use /addcc YOUR_KEY.", show_alert=True)
            return
        url = _link_sessions.get(cq.message.id, BOT.SOURCE or [""])[0].strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            await cq.answer("This option needs a direct HTTP(S) link.", show_alert=True)
            return

        token = uuid4().hex[:8]
        _cc_direct_sessions[token] = url
        await cq.message.edit_text(
            "☁️ <b>CLOUDCONVERT HARDSUB</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Choisis la qualité de sortie :\n"
            "<i>Une résolution plus basse = traitement plus rapide.</i>",
            reply_markup=_cc_direct_quality_kb(token),
        )
        return

    if data.startswith("ccdirect_res|"):
        _, token, code = data.split("|", 2)
        resolution = _CC_RES_CODE_TO_LABEL.get(code)
        url = _cc_direct_sessions.pop(token, "")
        if not (url.startswith("http://") or url.startswith("https://")):
            await cq.answer("Session expirée ou déjà lancé.", show_alert=True)
            return

        name = BOT.Options.custom_name or os.path.basename(urlparse(url).path) or "video.mp4"

        await cq.answer()
        await cq.message.edit_text(
            "☁️ <b>CLOUDCONVERT HARDSUB</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"<code>{name}</code>\n\n"
            "Choisis le style de sous-titre :",
            reply_markup=_style_kb("cc_direct"),
        )
        _pending_style_choice[cq.message.id] = {
            "flow": "cc_direct", "url": url, "name": name, "resolution": resolution,
        }
        return

    if data == "cc_hardsub_cancel":
        _pending_cc_subtitle.pop(cq.message.id, None)
        await cq.message.edit_text("❌ Hardsub annulé.")
        return

    if data == "fc_hardsub_cancel":
        _pending_fc_subtitle.pop(cq.message.id, None)
        await cq.message.edit_text("❌ Hardsub annulé.")
        return

    if data in ("style_yes", "style_no"):
        pending = _pending_style_sub.pop(cq.message.id, None)
        if not pending:
            await cq.answer("Session expirée.", show_alert=True)
            return
        path, name = pending["path"], pending["name"]
        try:
            if data == "style_yes":
                await cq.answer("🎨 Application du style...")
                styled = await apply_house_style(path, Paths.WORK_PATH)
                out_name = os.path.splitext(name)[0] + ".styled.ass"
                await colab_bot.send_document(
                    chat_id=BOT.TargetChat, document=styled,
                    caption=f"✅ Style maison appliqué (Trebuchet MS 22)\n<code>{out_name}</code>",
                    file_name=out_name,
                )
                await cq.message.edit_text(f"✅ Style appliqué et renvoyé : <code>{out_name}</code>")
                if os.path.exists(styled) and styled != path:
                    os.remove(styled)
            else:
                await cq.answer()
                await colab_bot.send_document(
                    chat_id=BOT.TargetChat, document=path,
                    caption=f"↩️ Style inchangé\n<code>{name}</code>",
                    file_name=name,
                )
                await cq.message.edit_text(f"↩️ Style inchangé, fichier renvoyé tel quel : <code>{name}</code>")
        except Exception as exc:
            await cq.message.edit_text(f"❌ <b>Style sub failed</b>\n\n<code>{exc}</code>")
        finally:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass
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
        first_src = (BOT.SOURCE or [""])[0].strip()
        is_magnet = first_src.startswith("magnet:?xt=urn:btih:")
        label = _lk_kind_label(BOT.Mode.ytdl, is_magnet)
        await cq.message.edit_text(
            _lk_header(label, n),
            reply_markup=_lk_main_kb(is_magnet)
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
                await colab_bot.send_message(chat_id=BOT.TargetChat, text=media_info)
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

    elif data.startswith("canceljob_"):
        job_id = data.split("_", 1)[1]
        ok = ActiveJobs.cancel(job_id)
        await cq.answer(
            "⛔ Annulation en cours..." if ok else "Ce job est déjà terminé.",
            show_alert=not ok,
        )


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


# ══════════════════════════════════════════════
#  Document → sous-titre pour FC Hardsub manuel
# ══════════════════════════════════════════════

@colab_bot.on_message(filters.document & filters.private)
async def handle_subtitle_document(client, message):
    if not _owner(message):
        return

    # ── Sous-titre pour CloudConvert Hardsub sur lien direct ──────────────
    reply_id_cc = message.reply_to_message_id
    pending_cc = _pending_cc_subtitle.get(reply_id_cc) if reply_id_cc else None
    if pending_cc is None and len(_pending_cc_subtitle) == 1:
        reply_id_cc, pending_cc = next(iter(_pending_cc_subtitle.items()))
    if pending_cc:
        file_name = message.document.file_name or ""
        ext = os.path.splitext(file_name)[1].lower()
        if ext not in (".ass", ".srt", ".ssa"):
            await message.reply_text(
                "❌ Envoie un fichier <code>.ass</code> ou <code>.srt</code> valide.",
                quote=True,
            )
            return
        _pending_cc_subtitle.pop(reply_id_cc, None)
        status_msg = await StatusSlideshow().start(message.chat.id, text="⏳ <i>Sous-titre reçu, démarrage CloudConvert...</i>")
        await message.delete()
        os.makedirs(Paths.WORK_PATH, exist_ok=True)
        subtitle_path = os.path.join(Paths.WORK_PATH, f"cc_sub_{uuid4().hex[:8]}{ext}")
        await message.download(file_name=subtitle_path)
        get_event_loop().create_task(
            Direct_CC_Hardsub_Handler(
                pending_cc["url"], pending_cc["name"], subtitle_path, status_msg,
                resolution=pending_cc.get("resolution"),
                style_key=pending_cc.get("style_key", "a"),
            )
        )
        return

    if not _pending_fc_subtitle:
        # Aucun hardsub en attente : on propose le flow autonome "Add Style
        # Sub" — appliquer (ou pas) le house style et renvoyer le fichier,
        # sans lancer aucun job vidéo.
        file_name = message.document.file_name or ""
        ext = os.path.splitext(file_name)[1].lower()
        if ext not in (".ass", ".srt", ".ssa"):
            return  # fichier non reconnu, on ignore silencieusement
        os.makedirs(Paths.WORK_PATH, exist_ok=True)
        subtitle_path = os.path.join(Paths.WORK_PATH, f"style_sub_{uuid4().hex[:8]}{ext}")
        await message.download(file_name=subtitle_path)
        await message.delete()
        prompt = await colab_bot.send_message(
            chat_id=BOT.TargetChat,
            text=(
                f"🎨 <code>{file_name}</code>\n\n"
                "Appliquer le <b>house style</b> (Trebuchet MS 22) sur ce sous-titre ?"
            ),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Oui", callback_data="style_yes"),
                InlineKeyboardButton("❌ Non", callback_data="style_no"),
            ]]),
        )
        _pending_style_sub[prompt.id] = {"path": subtitle_path, "name": file_name}
        return

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

    status_msg = await StatusSlideshow().start(message.chat.id, text="⏳ <i>Sous-titre reçu, démarrage du hardsub...</i>")
    await message.delete()

    os.makedirs(Paths.WORK_PATH, exist_ok=True)
    subtitle_path = os.path.join(Paths.WORK_PATH, f"manual_sub_{uuid4().hex[:8]}{ext}")
    await message.download(file_name=subtitle_path)

    # Fire-and-forget : ne bloque pas ce handler, donc le bot reste réactif
    # pour recevoir d'autres liens/sous-titres pendant que celui-ci tourne.
    get_event_loop().create_task(
        Direct_FC_Hardsub_Handler(
            pending["url"], pending["name"], subtitle_path, status_msg,
            resize=pending.get("resize"), style_key=pending.get("style_key", "a"),
        )
    )


# ══════════════════════════════════════════════
#  Import nyaa_tracker (registers its handlers)
# ══════════════════════════════════════════════

try:
    import colab_leecher.nyaa_tracker
    logging.info("📡 Nyaa tracker loaded")
except Exception as e:
    logging.warning(f"Nyaa tracker not loaded: {e}")

try:
    import colab_leecher.anime_search
    logging.info("🔎 Anime search (Nautiljon/MAL) loaded")
except Exception as e:
    logging.warning(f"Anime search not loaded: {e}")

try:
    import colab_leecher.video_menu
    logging.info("🎬 Video menu (refactored) loaded")
except Exception as e:
    logging.warning(f"Video menu not loaded: {e}")


logging.info("💖 Myuu࣪ ☾ started.")
get_event_loop().create_task(_startup_welcome())
colab_bot.run()
