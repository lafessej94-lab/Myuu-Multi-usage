"""
colab_leecher/video_menu.py

Menu vidéo unifié pour les fichiers envoyés directement au bot.

Remplace :
  - l'ancien _video_tools_kb() de __main__.py (1 seul menu plat, 16 boutons)
  - les 8 dicts globaux _pending_video / _pending_merge / _pending_trim /
    _pending_subs / _pending_manualshot / _pending_split / _pending_sample /
    _pending_rename (indexés par message.id)

Par :
  - un seul objet VideoSession (comme FileSession dans l'autre repo à plugins)
  - des callbacks structurés vid|<action>|<key> avec sous-menus
    (Info & Shots / Edit / Subs & Audio / Convert / Streams)

Toutes les fonctionnalités sont conservées à l'identique — seul le plomberie
boutons/state change. Le travail réel (ffmpeg, upload...) passe toujours par
les Local_*_Handler existants dans colab_leecher/utility/handler.py.

INTÉGRATION dans __main__.py :
  1. Supprimer : _video_tools_kb(), _video_res_kb(), LOCAL_RESOLUTIONS,
     les 8 dicts _pending_* listés ci-dessus, le handler
     handle_incoming_video(), et tous les blocs `if data == "vidtool_..."`
     / `if data.startswith("vidres|")` dans callbacks().
  2. Dans setFix() (filters.reply & filters.private), tout en haut, ajouter :
         from colab_leecher.video_menu import handle_video_text_reply
         if await handle_video_text_reply(client, message):
             return
     (avant les checks BOT.State.prefix / BOT.State.suffix existants — ou
     après, l'ordre n'a pas d'importance tant que c'est avant le `elif
     message.reply_to_message_id in _pending_trim` etc. qui disparaissent).
  3. En bas de __main__.py, ajouter :
         import colab_leecher.video_menu  # noqa: F401  (enregistre les handlers)
  4. Les blocs restants dans callbacks() qui NE bougent PAS : "style_yes"/
     "style_no" (_pending_style_sub — flow sous-titre "à froid", indépendant
     du menu vidéo, inchangé), et tout le Stream Extractor sur lien (sx_*),
     inchangé aussi.
"""
from __future__ import annotations

import os
import uuid
from asyncio import get_event_loop, sleep
from dataclasses import dataclass, field
from typing import Optional

from pyrogram import filters
from pyrogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from colab_leecher import OWNER, colab_bot
from colab_leecher.access import is_allowed as access_is_allowed, is_banned as access_is_banned
from colab_leecher.status_slideshow import StatusSlideshow
from colab_leecher.utility.variables import BOT, Paths
from colab_leecher.utility.handler import (
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
)

_AUDIO_EXTS = (".mp3", ".m4a", ".flac", ".wav", ".ogg", ".aac", ".opus")
_VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".ts", ".m2ts", ".flv", ".wmv")
_SUB_EXTS = (".ass", ".srt", ".ssa")

LOCAL_RESOLUTIONS: dict[str, int] = {"480": 480, "720": 720, "1080": 1080}


def _can_use(m) -> bool:
    return m.chat.id == OWNER or (access_is_allowed(m.chat.id) and not access_is_banned(m.chat.id))


# ─────────────────────────────────────────────────────────────
# Session — 1 objet par vidéo en attente, remplace les 8 dicts _pending_*
# ─────────────────────────────────────────────────────────────

@dataclass
class VideoSession:
    key: str
    source_message: Message
    name: str
    waiting: Optional[str] = None       # action qui attend une réponse (texte/document)
    payload: dict = field(default_factory=dict)


_sessions: dict[str, VideoSession] = {}


def _new_session(source_message: Message, name: str) -> VideoSession:
    key = uuid.uuid4().hex[:10]
    sess = VideoSession(key=key, source_message=source_message, name=name)
    _sessions[key] = sess
    return sess


def _get(key: str) -> Optional[VideoSession]:
    return _sessions.get(key)


def _pop(key: str) -> Optional[VideoSession]:
    return _sessions.pop(key, None)


def _waiting_session(*actions: str) -> Optional[VideoSession]:
    """Fallback 'pas besoin de reply exact' : s'il n'y a qu'UNE session en
    attente sur une de ces actions, on l'accepte même sans reply explicite —
    même UX que l'ancien système (len(_pending_x) == 1)."""
    candidates = [s for s in _sessions.values() if s.waiting in actions]
    return candidates[0] if len(candidates) == 1 else None


def _find_by_prompt(reply_id: int, *actions: str) -> Optional[VideoSession]:
    for s in _sessions.values():
        if s.waiting in actions and s.payload.get("prompt_id") == reply_id:
            return s
    return None


# ─────────────────────────────────────────────────────────────
# Keyboards — 4 sous-menus + Streams, même découpage que video.py
# ─────────────────────────────────────────────────────────────

def video_menu_text(name: str) -> str:
    return f"📹 <code>{name}</code>\n\n<b>Choisis une catégorie :</b>"


def video_menu_kb(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Info & Shots", callback_data=f"vid|info_menu|{key}"),
         InlineKeyboardButton("✂️ Edit",        callback_data=f"vid|edit_menu|{key}")],
        [InlineKeyboardButton("💬 Subs & Audio", callback_data=f"vid|subs_menu|{key}"),
         InlineKeyboardButton("🔄 Convert",      callback_data=f"vid|convert_menu|{key}")],
        [InlineKeyboardButton("🎞 Streams",      callback_data=f"vid|streams|{key}")],
        [InlineKeyboardButton("✖ Annuler",       callback_data=f"vid|cancel|{key}")],
    ])


def _section_kb(key: str, section: str) -> InlineKeyboardMarkup:
    if section == "info":
        rows = [
            [InlineKeyboardButton("🖼 Thumb", callback_data=f"vid|thumb|{key}"),
             InlineKeyboardButton("📸 Shots", callback_data=f"vid|shots|{key}")],
            [InlineKeyboardButton("🎯 Manual shot", callback_data=f"vid|manualshot|{key}"),
             InlineKeyboardButton("📋 Metadata",    callback_data=f"vid|metadata|{key}")],
        ]
    elif section == "edit":
        rows = [
            [InlineKeyboardButton("✂️ Trim",  callback_data=f"vid|trim|{key}"),
             InlineKeyboardButton("🔪 Split", callback_data=f"vid|split|{key}")],
            [InlineKeyboardButton("🎬 Sample", callback_data=f"vid|sample|{key}"),
             InlineKeyboardButton("✏️ Rename", callback_data=f"vid|rename|{key}")],
            [InlineKeyboardButton("🔊 Merge Audio+Vidéo", callback_data=f"vid|merge|{key}")],
        ]
    elif section == "subs":
        rows = [
            [InlineKeyboardButton("💬 Mux subs",  callback_data=f"vid|muxsubs|{key}"),
             InlineKeyboardButton("🔥 Burn subs", callback_data=f"vid|burnsubs|{key}")],
            [InlineKeyboardButton("🎵 To Audio", callback_data=f"vid|toaudio|{key}"),
             InlineKeyboardButton("🔇 Mute",     callback_data=f"vid|mute|{key}")],
        ]
    elif section == "convert":
        rows = [
            [InlineKeyboardButton("🎞 Video Converter", callback_data=f"vid|convert|{key}")],
            [InlineKeyboardButton("🗜 Compress",        callback_data=f"vid|compress|{key}")],
        ]
    else:
        rows = []
    rows.append([
        InlineKeyboardButton("🔙 Retour", callback_data=f"vid|menu|{key}"),
        InlineKeyboardButton("✖ Annuler", callback_data=f"vid|cancel|{key}"),
    ])
    return InlineKeyboardMarkup(rows)


def _res_kb(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("480p", callback_data=f"vidres|480|{key}"),
         InlineKeyboardButton("720p", callback_data=f"vidres|720|{key}")],
        [InlineKeyboardButton("1080p", callback_data=f"vidres|1080|{key}")],
        [InlineKeyboardButton("✖ Annuler", callback_data=f"vid|cancel|{key}")],
    ])


def _prompt_kb(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✖ Annuler", callback_data=f"vid|cancel|{key}")]])


# ─────────────────────────────────────────────────────────────
# Point d'entrée — vidéo/audio/voice/document envoyé directement au bot
# ─────────────────────────────────────────────────────────────

@colab_bot.on_message(
    (filters.video | filters.audio | filters.voice | filters.document) & filters.private,
    group=-1,
)
async def handle_incoming_video(client, message: Message):
    if not _can_use(message):
        message.continue_propagation()
        return

    reply_id = message.reply_to_message_id

    # ── Cas 1 : audio attendu pour un merge en cours ──────────────────────
    merge_sess = _find_by_prompt(reply_id, "merge") if reply_id else None
    if merge_sess is None:
        merge_sess = _waiting_session("merge")

    if merge_sess:
        is_audio, audio_name = False, "audio"
        if message.audio:
            is_audio, audio_name = True, message.audio.file_name or "audio.mp3"
        elif message.voice:
            is_audio, audio_name = True, "voice.ogg"
        elif message.document:
            mime = message.document.mime_type or ""
            name = message.document.file_name or ""
            if mime.startswith("audio/") or name.lower().endswith(_AUDIO_EXTS):
                is_audio, audio_name = True, name or "audio"

        if is_audio:
            _pop(merge_sess.key)
            status_msg = await StatusSlideshow().start(
                message.chat.id, text="⏳ <i>Audio reçu, démarrage de la fusion...</i>",
            )
            await message.delete()
            os.makedirs(Paths.WORK_PATH, exist_ok=True)
            ext = os.path.splitext(audio_name)[1] or ".mp3"
            audio_path = os.path.join(Paths.WORK_PATH, f"merge_audio_{uuid.uuid4().hex[:8]}{ext}")
            await message.download(file_name=audio_path)
            get_event_loop().create_task(
                Local_Merge_Handler(merge_sess.source_message, audio_path, status_msg)
            )
            return
        # reply présent mais pas un fichier audio -> on continue l'analyse normale

    # ── Cas 2 : sous-titre attendu pour Mux/Burn subs ──────────────────────
    subs_sess = _find_by_prompt(reply_id, "muxsubs", "burnsubs") if reply_id else None
    if subs_sess is None:
        subs_sess = _waiting_session("muxsubs", "burnsubs")

    if subs_sess and message.document:
        file_name = message.document.file_name or ""
        ext = os.path.splitext(file_name)[1].lower()
        if ext in _SUB_EXTS:
            _pop(subs_sess.key)
            burn = subs_sess.waiting == "burnsubs"
            status_msg = await StatusSlideshow().start(
                message.chat.id, text="⏳ <i>Sous-titre reçu, démarrage...</i>",
            )
            await message.delete()
            os.makedirs(Paths.WORK_PATH, exist_ok=True)
            subtitle_path = os.path.join(Paths.WORK_PATH, f"vidtool_sub_{uuid.uuid4().hex[:8]}{ext}")
            await message.download(file_name=subtitle_path)
            get_event_loop().create_task(
                Local_Subs_Handler(subs_sess.source_message, subtitle_path, status_msg, burn)
            )
            return
        else:
            msg = await message.reply_text(
                "❌ Envoie un fichier <code>.ass</code> ou <code>.srt</code> valide.", quote=True,
            )
            await sleep(8); await msg.delete()
            return

    # ── Cas 3 : nouvelle vidéo -> ouvre le menu principal ──────────────────
    is_video, display_name = False, "video.mp4"
    if message.video:
        is_video, display_name = True, message.video.file_name or "video.mp4"
    elif message.document:
        mime = message.document.mime_type or ""
        name = message.document.file_name or ""
        if mime.startswith("video/") or name.lower().endswith(_VIDEO_EXTS):
            is_video, display_name = True, name or "video.mp4"

    if not is_video:
        message.continue_propagation()
        return

    sess = _new_session(message, display_name)
    await message.reply_text(
        video_menu_text(display_name),
        reply_markup=video_menu_kb(sess.key),
        quote=True,
    )


# ─────────────────────────────────────────────────────────────
# Callback principal — vid|<action>|<key>
# ─────────────────────────────────────────────────────────────

@colab_bot.on_callback_query(filters.regex(r"^vid\|"), group=-1)
async def video_menu_cb(client, cq: CallbackQuery):
    parts = cq.data.split("|", 2)
    if len(parts) < 3:
        return await cq.answer("Invalid data.", show_alert=True)
    _, action, key = parts

    if action == "cancel":
        _pop(key)
        await cq.answer()
        await cq.message.edit_text("❌ Annulé.")
        return

    sess = _get(key)
    if not sess:
        return await cq.answer("Session expirée, renvoie la vidéo.", show_alert=True)

    if action == "menu":
        sess.waiting = None
        await cq.answer()
        await cq.message.edit_text(video_menu_text(sess.name), reply_markup=video_menu_kb(key))
        return

    if action in ("info_menu", "edit_menu", "subs_menu", "convert_menu"):
        section = action.split("_")[0]
        titles = {
            "info": "📊 <b>Info &amp; Shots</b>",
            "edit": "✂️ <b>Edit</b>",
            "subs": "💬 <b>Subs &amp; Audio</b>",
            "convert": "🔄 <b>Convert</b>",
        }
        await cq.answer()
        await cq.message.edit_text(
            f"{titles[section]}\n<code>{sess.name}</code>",
            reply_markup=_section_kb(key, section),
        )
        return

    if action == "streams":
        await cq.answer()
        await _open_stream_extractor(cq.message, sess)
        return

    if action == "convert":
        await cq.answer()
        await cq.message.edit_text(
            f"🎞 <code>{sess.name}</code>\n\n<b>Choisis la résolution de sortie :</b>",
            reply_markup=_res_kb(key),
        )
        return

    # Jobs directs (pas d'input supplémentaire requis)
    one_shot = {
        "thumb":    ("🖼 Extraction du thumb...", Local_Thumb_Handler),
        "shots":    ("📸 Extraction des screenshots...", Local_Screenshots_Handler),
        "compress": ("🗜 Compression démarrée...", Local_Compress_Handler),
        "toaudio":  ("🎵 Extraction audio démarrée...", Local_ToAudio_Handler),
        "mute":     ("🔇 Retrait audio démarré...", Local_Mute_Handler),
    }
    if action in one_shot:
        toast, handler = one_shot[action]
        await cq.answer(toast)
        await cq.message.delete()
        job_status_msg = await StatusSlideshow().start(BOT.TargetChat, text=f"⏳ <i>{toast}</i>")
        get_event_loop().create_task(handler(sess.source_message, job_status_msg))
        _pop(key)
        return

    if action == "metadata":
        await cq.answer()
        status_msg = await cq.message.edit_text("⏳ <i>Lecture des métadonnées...</i>")
        get_event_loop().create_task(Local_Metadata_Handler(sess.source_message, status_msg))
        _pop(key)
        return

    # Jobs qui attendent une réponse texte/document
    prompts = {
        "trim":       "✂️ <code>{name}</code>\n\n📎 <b>Réponds</b> avec :\n<code>début fin</code>\n\nExemple : <code>00:01:30 00:04:10</code>",
        "split":      "🔪 <code>{name}</code>\n\n📎 <b>Réponds</b> avec le nombre de parties.\n\nExemple : <code>3</code>",
        "sample":     "🎬 <code>{name}</code>\n\n📎 <b>Réponds</b> avec la durée en secondes.\n\nExemple : <code>30</code>",
        "rename":     "✏️ <code>{name}</code>\n\n📎 <b>Réponds</b> avec le nouveau nom (avec extension).\n\nExemple : <code>Episode 05.mkv</code>",
        "manualshot": "🎯 <code>{name}</code>\n\n📎 <b>Réponds</b> avec le timestamp exact.\n\nExemple : <code>00:02:15</code>",
        "merge":      "🔊 <code>{name}</code>\n\n📎 <b>Réponds</b> avec le fichier audio à fusionner.",
        "muxsubs":    "💬 Mux subs (piste)\n<code>{name}</code>\n\n📎 <b>Réponds</b> avec le fichier de sous-titres (<code>.ass</code>/<code>.srt</code>).",
        "burnsubs":   "🔥 Burn subs (incrusté)\n<code>{name}</code>\n\n📎 <b>Réponds</b> avec le fichier de sous-titres (<code>.ass</code>/<code>.srt</code>).",
    }
    if action in prompts:
        await cq.answer()
        text = prompts[action].format(name=sess.name)
        prompt = await cq.message.edit_text(text, reply_markup=_prompt_kb(key))
        sess.waiting = action
        sess.payload["prompt_id"] = prompt.id
        return

    await cq.answer("Action inconnue.", show_alert=True)


@colab_bot.on_callback_query(filters.regex(r"^vidres\|"), group=-1)
async def video_resolution_cb(client, cq: CallbackQuery):
    parts = cq.data.split("|", 2)
    if len(parts) < 3:
        return await cq.answer("Invalid data.", show_alert=True)
    _, code, key = parts
    height = LOCAL_RESOLUTIONS.get(code)
    sess = _pop(key)
    if not sess or not height:
        return await cq.answer("Session expirée ou déjà lancé.", show_alert=True)

    await cq.answer(f"🎞 Conversion {height}p démarrée")
    await cq.message.delete()
    job_status_msg = await StatusSlideshow().start(
        BOT.TargetChat, text=f"⏳ <i>Starting local video conversion ({height}p)...</i>",
    )
    get_event_loop().create_task(Local_Video_Convert_Handler(sess.source_message, height, job_status_msg))


# ─────────────────────────────────────────────────────────────
# Réponses texte — trim / split / sample / rename / manualshot
# (à appeler en tout début de setFix() dans __main__.py)
# ─────────────────────────────────────────────────────────────

_TEXT_ACTIONS = ("trim", "split", "sample", "rename", "manualshot")


async def handle_video_text_reply(client, message: Message) -> bool:
    """Retourne True si le message a été consommé par une réponse en attente
    du menu vidéo (trim/split/sample/rename/manualshot)."""
    reply_id = message.reply_to_message_id
    sess = _find_by_prompt(reply_id, *_TEXT_ACTIONS) if reply_id else None
    if sess is None:
        sess = _waiting_session(*_TEXT_ACTIONS)
    if sess is None:
        return False

    action = sess.waiting
    text = (message.text or "").strip()

    if action == "trim":
        parts = text.split()
        if len(parts) != 2:
            msg = await message.reply_text(
                "❌ Format invalide. Exemple : <code>00:01:30 00:04:10</code>", quote=True,
            )
            await sleep(8); await msg.delete()
            return True
        start, end = parts
        _pop(sess.key)
        await message.delete()
        job_status_msg = await StatusSlideshow().start(BOT.TargetChat, text="⏳ <i>Starting trim...</i>")
        get_event_loop().create_task(Local_Trim_Handler(sess.source_message, start, end, job_status_msg))
        return True

    if action == "manualshot":
        if not text:
            return True
        _pop(sess.key)
        await message.delete()
        job_status_msg = await StatusSlideshow().start(BOT.TargetChat, text="⏳ <i>Starting manual shot...</i>")
        get_event_loop().create_task(Local_ManualShot_Handler(sess.source_message, text, job_status_msg))
        return True

    if action == "split":
        try:
            n = int(text)
        except ValueError:
            n = 0
        if n < 2:
            msg = await message.reply_text("❌ Envoie un nombre de parties (min 2), ex: <code>3</code>", quote=True)
            await sleep(8); await msg.delete()
            return True
        _pop(sess.key)
        await message.delete()
        job_status_msg = await StatusSlideshow().start(BOT.TargetChat, text="⏳ <i>Starting split...</i>")
        get_event_loop().create_task(Local_Split_Handler(sess.source_message, n, job_status_msg))
        return True

    if action == "sample":
        try:
            dur = int(text)
        except ValueError:
            dur = 0
        if dur < 5:
            msg = await message.reply_text("❌ Envoie une durée en secondes (min 5), ex: <code>30</code>", quote=True)
            await sleep(8); await msg.delete()
            return True
        _pop(sess.key)
        await message.delete()
        job_status_msg = await StatusSlideshow().start(BOT.TargetChat, text="⏳ <i>Starting sample...</i>")
        get_event_loop().create_task(Local_Sample_Handler(sess.source_message, dur, job_status_msg))
        return True

    if action == "rename":
        if not text:
            return True
        _pop(sess.key)
        await message.delete()
        job_status_msg = await StatusSlideshow().start(BOT.TargetChat, text="⏳ <i>Starting rename...</i>")
        get_event_loop().create_task(Local_Rename_Handler(sess.source_message, text, job_status_msg))
        return True

    return False


# ─────────────────────────────────────────────────────────────
# Stream extractor sur une vidéo déjà envoyée au bot
# ─────────────────────────────────────────────────────────────

async def _open_stream_extractor(msg, sess: VideoSession) -> None:
    from colab_leecher.stream_extractor import analyse, kb_type

    await msg.edit_text("🎞 <b>STREAM EXTRACTOR</b>\n\nTéléchargement depuis Telegram...")
    os.makedirs(Paths.WORK_PATH, exist_ok=True)
    local_path = os.path.join(Paths.WORK_PATH, f"sx_{uuid.uuid4().hex[:8]}_{sess.name}")
    await sess.source_message.download(file_name=local_path)

    session = await analyse(local_path, msg.chat.id)
    if not session or (not session["video"] and not session["audio"] and not session["subs"]):
        await msg.edit_text("🎞 <b>STREAM EXTRACTOR</b>\n\nAucune piste détectée sur ce fichier.")
        return

    v, a, s = len(session["video"]), len(session["audio"]), len(session["subs"])
    await msg.edit_text(
        "🎞 <b>STREAM EXTRACTOR</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📌  <b>{session['title']}</b>\n\n"
        f"🎬  Video tracks     <code>{v}</code>\n"
        f"🎵  Audio tracks     <code>{a}</code>\n"
        f"💬  Subtitles        <code>{s}</code>\n\n"
        "Choose track type:",
        reply_markup=kb_type(v, a, s),
    )
    # NOTE : la suite (sx_video / sx_audio / sx_subs / sx_dl_*) est déjà gérée
    # par les callbacks sx_* existants dans __main__.py — inchangés.
