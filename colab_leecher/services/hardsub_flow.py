"""
colab_leecher/services/hardsub_flow.py

Every hardsub button flow that isn't the video-tools menu: Seedr+CC
convert/hardsub, Seedr+FC hardsub (magnet), FC/CC hardsub on a direct link
with a manually-supplied subtitle, the resolution → style (→ speed, for CC)
picker shared by all four flows, and the standalone "Add Style Sub" cold
path (apply the house style to a subtitle sent with no hardsub in
progress).

Also owns handle_subtitle_document(), the `filters.document` message
handler that resolves an uploaded .ass/.srt/.ssa against whichever of the
three pending dicts below is waiting for it — kept together with the
callback flow that creates those pending entries, since the two are only
meaningful as one pipeline.

Extracted verbatim from __main__.py.
"""
from __future__ import annotations

import os
from asyncio import get_event_loop
from datetime import datetime
from urllib.parse import urlparse
from uuid import uuid4

from pyrogram import filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from colab_leecher import OWNER, SEEDR_PASSWORD, SEEDR_USERNAME, colab_bot
from colab_leecher.house_style import STYLE_PRESET_LABELS, apply_house_style
from colab_leecher.services import on_exact, on_prefix
from colab_leecher.services.link_flow import link_sessions
from colab_leecher.status_slideshow import StatusSlideshow
from colab_leecher.utility.handler import (
    Direct_CC_Hardsub_Handler,
    Direct_FC_Hardsub_Handler,
    Seedr_CC_Convert_Handler,
    Seedr_CC_Hardsub_Handler,
    Seedr_FC_Hardsub_Handler,
)
from colab_leecher.utility.variables import BOT, MSG, BotTimes, Paths, TaskInfo

# Résolutions proposées avant un hardsub FreeConvert — format (largeur, hauteur).
# None = garde la résolution d'origine du fichier (comportement historique).
FC_RESOLUTIONS: dict[str, tuple[int, int] | None] = {
    "orig": None,
    "360":  (640, 360),
    "480":  (854, 480),
    "720":  (1280, 720),
}

# code du menu ("orig"/"360"/"480"/"720") -> chaîne attendue par
# hardsub_remote_url() de CloudConvert ("original"/"360p"/"480p"/"720p").
_CC_RES_CODE_TO_LABEL: dict[str, str] = {
    "orig": "original", "360": "360p", "480": "480p", "720": "720p",
}

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

# Styles proposés dans le menu (ordre d'affichage = ordre de ce dict) et
# clés acceptées par handle_hs_style(). Pour ajouter un futur Style E :
# l'ajouter ici + dans STYLE_PRESETS / STYLE_PRESET_LABELS (house_style.py).
_STYLE_BUTTON_EMOJIS: dict[str, str] = {
    "a": "🅰️",
    "b": "🅱️",
    "c": "🅲️",
    "d": "🅳️",
}
_VALID_STYLE_KEYS: tuple[str, ...] = tuple(_STYLE_BUTTON_EMOJIS)

# ── État en mémoire ──────────────────────────────────────────────────────
# _cc_direct_sessions : token (8 hex chars, embedded dans callback_data) ->
# url. Volontairement PAS indexé par message_id (contrairement à
# link_sessions) — le token voyage dans le bouton lui-même.
_cc_direct_sessions: dict[str, str] = {}

# message_id (du prompt "envoie le sous-titre") -> {"url", "name",
# "resolution"|"resize", "style_key"}. Deux dicts séparés (FC/CC) pour ne
# pas mélanger les deux flows si les deux tournent en même temps.
_pending_fc_subtitle: dict[int, dict] = {}
_pending_cc_subtitle: dict[int, dict] = {}

# message_id (du prompt Oui/Non) -> {"path": str, "name": str}. Flow
# indépendant de tout hardsub — un sous-titre envoyé "à froid".
_pending_style_sub: dict[int, dict] = {}

# message_id (du prompt "Choisis le style") -> {"flow": ..., ...données}.
# Étape intermédiaire insérée après le choix de résolution (FC magnet, FC
# direct, CC direct) — "cc_seedr" n'en a pas besoin, il stocke directement
# dans _cc_hardsub_session (déjà keyé par message_id).
_pending_style_choice: dict[int, dict] = {}

# message_id -> {"magnet": str, "resolution": str|None, "style_key": str|None}
_cc_hardsub_session: dict[int, dict] = {}


def _style_rows(callback_for) -> list[list[InlineKeyboardButton]]:
    # Une ligne par style (liste verticale) : plus lisible et le label
    # complet ("Style C (Asakura)"...) n'est plus tronqué. `callback_for`
    # reçoit la clé du style ("a".."d") et renvoie le callback_data.
    return [
        [InlineKeyboardButton(
            f"{_STYLE_BUTTON_EMOJIS[key]} {STYLE_PRESET_LABELS.get(key, f'Style {key.upper()}')}",
            callback_data=callback_for(key),
        )]
        for key in _VALID_STYLE_KEYS
    ]


def _style_kb(flow: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(_style_rows(lambda key: f"hs_style|{flow}|{key}"))


def _style_sub_kb() -> InlineKeyboardMarkup:
    # Add Style Sub : les 4 styles + une sortie "garder tel quel".
    rows = _style_rows(lambda key: f"style_apply|{key}")
    rows.append([InlineKeyboardButton("❌ Non, garder tel quel", callback_data="style_no")])
    return InlineKeyboardMarkup(rows)


def _fc_quality_kb(flow: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 Qualité d'origine", callback_data=f"fc_res|{flow}|orig")],
        [InlineKeyboardButton("360p", callback_data=f"fc_res|{flow}|360"),
         InlineKeyboardButton("480p", callback_data=f"fc_res|{flow}|480")],
        [InlineKeyboardButton("720p", callback_data=f"fc_res|{flow}|720")],
    ])


def _cc_direct_quality_kb(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 Qualité d'origine", callback_data=f"ccdirect_res|{token}|orig")],
        [InlineKeyboardButton("360p", callback_data=f"ccdirect_res|{token}|360"),
         InlineKeyboardButton("480p", callback_data=f"ccdirect_res|{token}|480")],
        [InlineKeyboardButton("720p", callback_data=f"ccdirect_res|{token}|720")],
    ])


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
#  Seedr + CloudConvert
# ══════════════════════════════════════════════

@on_exact("seedr_cc_convert")
async def handle_seedr_cc_convert(client, cq, data):
    if not BOT.Options.cc_api_keys:
        await cq.answer("CloudConvert API key missing — use /addcc YOUR_KEY.", show_alert=True)
        return
    if not str(SEEDR_USERNAME or "").strip() or not str(SEEDR_PASSWORD or "").strip():
        await cq.answer("Seedr credentials are missing in your Colab launcher.", show_alert=True)
        return
    magnet = link_sessions.get(cq.message.id, BOT.SOURCE or [""])[0].strip()
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


@on_exact("seedr_cc_hardsub")
async def handle_seedr_cc_hardsub(client, cq, data):
    if not BOT.Options.cc_api_keys:
        await cq.answer("CloudConvert API key missing — use /addcc YOUR_KEY.", show_alert=True)
        return
    if not str(SEEDR_USERNAME or "").strip() or not str(SEEDR_PASSWORD or "").strip():
        await cq.answer("Seedr credentials are missing in your Colab launcher.", show_alert=True)
        return
    magnet = link_sessions.get(cq.message.id, BOT.SOURCE or [""])[0].strip()
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


@on_prefix("cc_res|")
async def handle_cc_res(client, cq, data):
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


# ── Choix du style (Style A / B / C / D) — inséré après le choix de
# résolution sur les 4 flows hardsub. "cc_seedr" stocke directement le choix
# dans _cc_hardsub_session (déjà keyé par message_id) puis affiche le clavier
# de vitesse ; les 3 autres flows utilisent _pending_style_choice et reprennent
# l'étape qui suivait la résolution (lancement direct pour fc_magnet, prompt
# sous-titre pour fc_direct/cc_direct).
@on_prefix("hs_style|")
async def handle_hs_style(client, cq, data):
    _, flow, style_key = data.split("|", 2)
    style_key = style_key if style_key in _VALID_STYLE_KEYS else "a"

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


@on_prefix("cc_speed|")
async def handle_cc_speed(client, cq, data):
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


# ══════════════════════════════════════════════
#  Seedr + FreeConvert (magnet) — CONCURRENT, jusqu'à 3 en parallèle
# ══════════════════════════════════════════════

@on_exact("seedr_fc_hardsub")
async def handle_seedr_fc_hardsub(client, cq, data):
    if not BOT.Options.fc_api_keys:
        await cq.answer("FreeConvert API key missing — use /addfc YOUR_KEY.", show_alert=True)
        return
    if not str(SEEDR_USERNAME or "").strip() or not str(SEEDR_PASSWORD or "").strip():
        await cq.answer("Seedr credentials are missing in your Colab launcher.", show_alert=True)
        return
    magnet = link_sessions.get(cq.message.id, BOT.SOURCE or [""])[0].strip()
    if not magnet.startswith("magnet:?xt=urn:btih:"):
        await cq.answer("Seedr mode currently needs a magnet link.", show_alert=True)
        return

    await cq.message.edit_text(
        "🆓 <b>FREECONVERT HARDSUB</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Choisis la qualité de sortie :\n"
        "<i>Une résolution plus basse = traitement plus rapide.</i>",
        reply_markup=_fc_quality_kb("magnet"),
    )
    link_sessions[cq.message.id] = [magnet]


@on_prefix("fc_res|magnet|")
async def handle_fc_res_magnet(client, cq, data):
    code = data.split("|", 2)[2]
    resize = FC_RESOLUTIONS.get(code)
    session = link_sessions.pop(cq.message.id, None)
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


# ══════════════════════════════════════════════
#  FreeConvert / CloudConvert hardsub sur lien direct (sous-titre manuel)
# ══════════════════════════════════════════════

@on_exact("fc_hardsub_manual")
async def handle_fc_hardsub_manual(client, cq, data):
    if not BOT.Options.fc_api_keys:
        await cq.answer("FreeConvert API key missing — use /addfc YOUR_KEY.", show_alert=True)
        return
    url = link_sessions.get(cq.message.id, BOT.SOURCE or [""])[0].strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        await cq.answer("This option needs a direct HTTP(S) link.", show_alert=True)
        return

    await cq.message.edit_text(
        "🆓 <b>FREECONVERT HARDSUB</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Choisis la qualité de sortie :\n"
        "<i>Une résolution plus basse = traitement plus rapide.</i>",
        reply_markup=_fc_quality_kb("direct"),
    )
    link_sessions[cq.message.id] = [url]


@on_prefix("fc_res|direct|")
async def handle_fc_res_direct(client, cq, data):
    code = data.split("|", 2)[2]
    resize = FC_RESOLUTIONS.get(code)
    session = link_sessions.pop(cq.message.id, None)
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


@on_exact("cc_hardsub_manual")
async def handle_cc_hardsub_manual(client, cq, data):
    if not BOT.Options.cc_api_keys:
        await cq.answer("CloudConvert API key missing — use /addcc YOUR_KEY.", show_alert=True)
        return
    url = link_sessions.get(cq.message.id, BOT.SOURCE or [""])[0].strip()
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


@on_prefix("ccdirect_res|")
async def handle_ccdirect_res(client, cq, data):
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


@on_exact("cc_hardsub_cancel")
async def handle_cc_hardsub_cancel(client, cq, data):
    _pending_cc_subtitle.pop(cq.message.id, None)
    await cq.message.edit_text("❌ Hardsub annulé.")


@on_exact("fc_hardsub_cancel")
async def handle_fc_hardsub_cancel(client, cq, data):
    _pending_fc_subtitle.pop(cq.message.id, None)
    await cq.message.edit_text("❌ Hardsub annulé.")


# ══════════════════════════════════════════════
#  Add Style Sub — flow indépendant, sous-titre envoyé "à froid"
# ══════════════════════════════════════════════

@on_prefix("style_apply|")
async def handle_style_apply(client, cq, data):
    style_key = data.split("|", 1)[1]
    style_key = style_key if style_key in _VALID_STYLE_KEYS else "a"
    await _finish_style_sub(cq, style_key)


@on_exact("style_no")
async def handle_style_no(client, cq, data):
    await _finish_style_sub(cq, None)


async def _finish_style_sub(cq, style_key: str | None) -> None:
    """Termine le flow Add Style Sub : applique le style `style_key`
    (a/b/c/d) et renvoie le fichier, ou le renvoie tel quel si None."""
    pending = _pending_style_sub.pop(cq.message.id, None)
    if not pending:
        await cq.answer("Session expirée.", show_alert=True)
        return
    path, name = pending["path"], pending["name"]
    try:
        if style_key is not None:
            style_label = STYLE_PRESET_LABELS.get(style_key, style_key)
            await cq.answer(f"🎨 Application du {style_label}...")
            styled = await apply_house_style(path, Paths.WORK_PATH, style_key=style_key)
            if styled == path:
                # apply_house_style() renvoie le fichier d'origine en cas
                # d'erreur interne (fallback silencieux) : on ne veut pas
                # annoncer "style appliqué" dans ce cas.
                raise RuntimeError("Le style n'a pas pu être appliqué à ce fichier.")
            out_name = os.path.splitext(name)[0] + ".styled.ass"
            await colab_bot.send_document(
                chat_id=BOT.TargetChat, document=styled,
                caption=f"✅ {style_label} appliqué\n<code>{out_name}</code>",
                file_name=out_name,
            )
            await cq.message.edit_text(
                f"✅ {style_label} appliqué et renvoyé : <code>{out_name}</code>"
            )
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


# ══════════════════════════════════════════════
#  Document → sous-titre pour FC/CC Hardsub manuel, ou Add Style Sub
# ══════════════════════════════════════════════

@colab_bot.on_message(filters.document & filters.private)
async def handle_subtitle_document(client, message):
    if message.chat.id != OWNER:
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
                "Choisis le <b>style</b> à appliquer sur ce sous-titre :"
            ),
            reply_markup=_style_sub_kb(),
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
