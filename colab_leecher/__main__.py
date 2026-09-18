import logging
import os
import platform
import psutil
from datetime import datetime
from asyncio import sleep, get_event_loop

from pyrogram import filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from colab_leecher import CC_API_KEY, FC_API_KEY, DUMP_ID, colab_bot, OWNER
from colab_leecher.access import is_allowed as access_is_allowed, is_banned as access_is_banned
from colab_leecher.claude_agent import (
    start_agent, stop_agent, is_agent_running,
    search_nyaa, get_recent_entries,
)
from colab_leecher.utility.variables import BOT, Paths
from colab_leecher.utility.helper import (
    isLink, setThumbnail, message_deleter, send_settings,
    sizeUnit, getTime, is_ytdl_link, _pct_bar,
)

# ── Sectioned link menu (Download / Inspect / Process / Cloud), mirroring
#    services/url_views.py's navigation pattern. Every leaf button below
#    still carries the SAME callback_data ("normal", "cc_convert",
#    "seedr_fc_hardsub", "sx_open", ...) that colab_leecher/services/ handles
#    — only the pre-menu navigation changed.
#    See colab_leecher/link_menu.py for the pure text/keyboard builders and
#    colab_leecher/services/link_flow.py for the lk|sec|/lk|cancel handlers.
from colab_leecher.link_menu import (
    kind_label as _lk_kind_label,
    link_header as _lk_header,
    main_kb as _lk_main_kb,
)

# Everything else that used to be inline in this file's callbacks() — the
# Claude picker, hardsub flows, stream extractor, settings screens, status
# dashboard, dumps/apikeys management, etc — now lives under
# colab_leecher/services/. Each submodule registers its own callback_data
# handlers against the shared dispatcher at import time (see
# colab_leecher/services/__init__.py for how routing works); some also
# register their own Pyrogram message handlers directly (hardsub_flow.py's
# subtitle-document intake).
#
# These are imported explicitly (rather than from services/__init__.py
# itself) because colab_leecher.claude_agent — imported above — pulls in
# colab_leecher.services.subtitle_probe, and importing any submodule of a
# package runs that package's __init__.py first. Auto-importing claude_menu
# from there would try to import claude_agent again while it's still
# mid-load. Importing everything here, after claude_agent has already
# finished loading, sidesteps that entirely.
import colab_leecher.services as _services
from colab_leecher.services import (
    start_menu,      # noqa: F401
    status_flow,      # noqa: F401
    task_launch,      # noqa: F401
    link_flow,
    hardsub_flow,     # noqa: F401 (must come before claude_menu: it defines FC_RESOLUTIONS)
    claude_menu,
    stream_flow,      # noqa: F401
    settings_menu,
)
from colab_leecher.services.access_gate import REQUIRED_CHANNEL, is_subscribed as _is_subscribed, join_gate_kb as _join_gate_kb


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

_AUDIO_EXTS = (".mp3", ".m4a", ".flac", ".wav", ".ogg", ".aac", ".opus")


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
        "  /Arise     — rendort Claude\n"
        "  /search_claude <anime> — recherche + hardsub manuel (Claude réveillé)\n\n"
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
#  (panel builders now live in colab_leecher/services/status_flow.py)
# ══════════════════════════════════════════════

@colab_bot.on_message(filters.command("status") & filters.private)
async def cmd_status(client, message):
    from colab_leecher.services import status_flow
    await message.delete()
    await message.reply_text(
        status_flow.panel_text(),
        reply_markup=status_flow.panel_kb(),
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
        from colab_leecher.utility.handler import cancelTask
        await cancelTask("Cancelled via /cancel")
    else:
        msg = await message.reply_text("⚠️ No active task.")
        await sleep(8); await msg.delete()


@colab_bot.on_message(filters.command("stop") & filters.private)
async def stop_bot(client, message):
    if not _owner(message): return
    await message.delete()
    if BOT.State.task_going:
        from colab_leecher.utility.handler import cancelTask
        await cancelTask("Bot shutdown")
    await message.reply_text("🛑 <b>Shutting down...</b> 👋")
    await sleep(2); await client.stop(); os._exit(0)


# ── Intégration Claude : /Relève réveille l'agent (surveillance Erai-raws
#    + pipeline seedr/hardsub automatique), /Arise le rendort. Owner
#    uniquement — l'agent n'agit que pour lui, même si d'autres users ont
#    accès au bot par ailleurs (voir colab_leecher/claude_agent.py).
#    Les pending-dicts et kb builders utilisés ci-dessous vivent maintenant
#    dans colab_leecher/services/claude_menu.py, avec les callbacks qui les
#    consomment.
@colab_bot.on_message(filters.command("Relève") & filters.private)
async def releve_cmd(client, message):
    if not _owner(message):
        return
    await message.delete()

    if not start_agent():
        await message.reply_text("⚠️ Claude est déjà actif.")
        return

    status = await message.reply_text(
        "🤖 <b>Claude est réveillé</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Surveillance de nyaa.si/Erai-raws active (toutes les 20s).\n"
        "Vérification des épisodes des 15 dernières minutes..."
    )

    try:
        recent = await get_recent_entries()
    except Exception as exc:
        await status.edit_text(
            "🤖 <b>Claude est réveillé</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"⚠️ Vérification initiale échouée : <code>{exc}</code>\n"
            "La surveillance continue en arrière-plan."
        )
        return

    if not recent:
        await status.edit_text(
            "🤖 <b>Claude est réveillé</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Aucun épisode dans les 15 dernières minutes.\n"
            "Surveillance active — utilise /Arise pour l'arrêter."
        )
        return

    claude_menu.pending_claude_picker[status.id] = {"entries": recent, "selected": None}
    await status.edit_text(
        "🤖 <b>Claude est réveillé</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>{len(recent)} épisode(s)</b> détecté(s) dans les 15 dernières minutes.\n"
        "Choisis lequel encoder (360p + 720p, style B) :",
        reply_markup=claude_menu.claude_picker_kb(recent),
    )


@colab_bot.on_message(filters.command("Arise") & filters.private)
async def arise_cmd(client, message):
    if not _owner(message):
        return
    await message.delete()
    if stop_agent():
        await message.reply_text("💤 <b>Claude se rendort.</b>\nTu peux réutiliser le bot normalement.")
    else:
        await message.reply_text("⚠️ Claude n'était pas actif.")


@colab_bot.on_message(filters.command("search_claude") & filters.private)
async def search_claude_cmd(client, message):
    if not _owner(message):
        return
    await message.delete()

    if not is_agent_running():
        msg = await message.reply_text(
            "⚠️ Claude doit être réveillé pour ça — utilise /Relève d'abord.",
            quote=True,
        )
        await sleep(8); await msg.delete()
        return

    if len(message.command) < 2:
        msg = await message.reply_text(
            "Usage: <code>/search_claude nom de l'anime</code>", quote=True,
        )
        await sleep(8); await msg.delete()
        return

    query = " ".join(message.command[1:])
    status = await message.reply_text(f"🔎 Recherche de <code>{query}</code> sur nyaa.si/Erai-raws...")

    try:
        entry = await search_nyaa(query)
    except Exception as exc:
        await status.edit_text(f"❌ Recherche échouée\n\n<code>{exc}</code>")
        return

    if not entry:
        await status.edit_text(f"❌ Aucune release 480p trouvée pour <code>{query}</code>.")
        return

    claude_menu.pending_claude_search[status.id] = {"entry": entry}
    await status.edit_text(
        "📦 <b>Release trouvée</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<code>{entry.title}</code>\n\n"
        "Lancer l'encodage ?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Oui", callback_data="claude_search_yes"),
            InlineKeyboardButton("❌ Non", callback_data="claude_search_no"),
        ]]),
    )


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
    await message.reply_text(settings_menu.dumps_text(), reply_markup=settings_menu.dumps_kb())


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
        msg = await message.reply_text(f"✅ Clé CloudConvert ajoutée : <code>{settings_menu.mask_key(key)}</code>", quote=True)
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
        msg = await message.reply_text(f"✅ Clé FreeConvert ajoutée : <code>{settings_menu.mask_key(key)}</code>", quote=True)
    await sleep(10); await msg.delete()


@colab_bot.on_message(filters.command("apikeys") & filters.private)
async def apikeys_cmd(client, message):
    if not _owner(message): return
    await message.delete()
    await message.reply_text(settings_menu.apikeys_text(), reply_markup=settings_menu.apikeys_kb())


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
    link_flow.link_sessions[sent.id] = src


# ══════════════════════════════════════════════
#  ALL CALLBACKS — thin dispatcher; the actual handlers live under
#  colab_leecher/services/ (one module per button flow — see that
#  package's __init__.py for how callback_data gets routed).
# ══════════════════════════════════════════════

@colab_bot.on_callback_query()
async def callbacks(client, cq):
    BOT.TargetChat = cq.message.chat.id  # tout ce que cette tâche déclenche (statut, upload) part vers CE chat

    if cq.data == "noop":
        await cq.answer()
        return

    if await _services.dispatch(client, cq):
        return

    # callback_data non reconnu par aucun service — ne devrait plus arriver
    # une fois tous les boutons migrés, mais on répond quand même pour
    # éviter le petit spinner "loading" côté client Telegram.
    logging.warning("Unhandled callback_data: %r", cq.data)
    await cq.answer()


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
#  Import nyaa_tracker (registers its handlers)
# ══════════════════════════════════════════════

try:
    import colab_leecher.nyaa_tracker
    logging.info("📡 Nyaa tracker loaded")
except Exception as e:
    logging.warning(f"Nyaa tracker not loaded: {e}")

try:
    import colab_leecher.services.Aniliste
    logging.info("🔎 Anime search (AniList) loaded")
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
