"""
colab_leecher/services/settings_menu.py

The /settings screens, réorganisés en 3 catégories (même style boutons que
video_menu.py : titre + séparateur, boutons emoji par paires, rangée finale
Retour/Fermer) :

  🎥 Video    -> Video Settings, CloudConvert
  🧰 Utility  -> Caption, Thumbnail, API Keys
  📤 Upload   -> AutoFwd, Dumps, Upload mode, Prefix, Suffix

Navigation : Menu principal -> Catégorie -> Écran de réglage.
Le bouton "🔙 Retour" d'un écran de réglage remonte au MENU PRINCIPAL
(comme demandé), pas à la catégorie. Le bouton "🔙 Retour" d'une catégorie
remonte lui au menu principal aussi (un seul niveau au-dessus).

dumps_kb/dumps_text/apikeys_kb/apikeys_text/mask_key restent exportées pour
les commandes /add, /dumps, /addcc, /addfc, /apikeys dans __main__.py.
"""
from __future__ import annotations

import os

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from colab_leecher.engines.cloudconvert import cc_mode_label, quality_label, resize_label
from colab_leecher.services import on_exact, on_prefix
from colab_leecher.utility.handler import cancelTask
from colab_leecher.utility.helper import send_settings
from colab_leecher.utility.variables import BOT, ActiveJobs, Paths

DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━━━"


def mask_key(key: str) -> str:
    if len(key) <= 10:
        return "•" * len(key)
    return f"{key[:4]}…{key[-4:]}"


# ─────────────────────────────────────────────────────────────
# Menu principal — /settings
# ─────────────────────────────────────────────────────────────

def main_settings_text() -> str:
    return f"⚙️ <b>SETTINGS</b>\n{DIVIDER}\n\n<b>Choisis une catégorie :</b>"


def main_settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎥 Video",   callback_data="settings-video")],
        [InlineKeyboardButton("🧰 Utility", callback_data="settings-utility")],
        [InlineKeyboardButton("📤 Upload",  callback_data="settings-upload")],
        [InlineKeyboardButton("✖ Fermer",  callback_data="close")],
    ])


@on_exact("settings-main")
async def handle_settings_main(client, cq, data):
    await cq.message.edit_text(main_settings_text(), reply_markup=main_settings_kb())


# ─────────────────────────────────────────────────────────────
# Catégorie Video — Video Settings + CloudConvert
# ─────────────────────────────────────────────────────────────

def video_category_text() -> str:
    return f"🎥 <b>VIDEO</b>\n{DIVIDER}\n\n<b>Choisis :</b>"


def video_category_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 Video Settings", callback_data="video")],
        [InlineKeyboardButton("☁️ CloudConvert",   callback_data="cc")],
        [InlineKeyboardButton("🔙 Retour",          callback_data="settings-main")],
    ])


@on_exact("settings-video")
async def handle_settings_video_category(client, cq, data):
    await cq.message.edit_text(video_category_text(), reply_markup=video_category_kb())


def render_video_settings_text() -> str:
    return (
        f"🎥 <b>VIDEO SETTINGS</b>\n{DIVIDER}\n\n"
        f"Convert  <code>{BOT.Setting.convert_video}</code>\n"
        f"Split    <code>{BOT.Setting.split_video}</code>\n"
        f"Format   <code>{BOT.Options.video_out.upper()}</code>\n"
        f"Quality  <code>{BOT.Setting.convert_quality}</code>"
    )


def render_video_settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✂️ Split",   callback_data="split-true"),
         InlineKeyboardButton("🗜 Zip",     callback_data="split-false")],
        [InlineKeyboardButton("🔄 Convert", callback_data="convert-true"),
         InlineKeyboardButton("🚫 No",      callback_data="convert-false")],
        [InlineKeyboardButton("🎬 MP4",     callback_data="mp4"),
         InlineKeyboardButton("📦 MKV",     callback_data="mkv")],
        [InlineKeyboardButton("🔝 High",    callback_data="q-High"),
         InlineKeyboardButton("📉 Low",     callback_data="q-Low")],
        [InlineKeyboardButton("🔙 Retour",  callback_data="settings-main"),
         InlineKeyboardButton("✖ Fermer",  callback_data="close")],
    ])


@on_exact("video")
async def handle_video_settings(client, cq, data):
    await cq.message.edit_text(render_video_settings_text(), reply_markup=render_video_settings_kb())


def render_cc_settings_text() -> str:
    cc_ready = "Ready" if BOT.Options.cc_api_keys else "Missing"
    return (
        f"☁️ <b>CLOUDCONVERT SETTINGS</b>\n{DIVIDER}\n\n"
        f"API Key  <code>{cc_ready}</code>\n"
        f"Mode     <code>{cc_mode_label(BOT.Options.cc_engine_mode)}</code>\n"
        f"Preset   <code>{quality_label(BOT.Options.cc_quality_profile)}</code>\n"
        f"Resize   <code>{resize_label(BOT.Options.cc_resize)}</code>\n"
        f"Target   <code>{BOT.Setting.cc_target_size}</code>\n\n"
        "These settings are used by CC Convert, CC Resize, and CC Compress."
    )


def render_cc_settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚖️ CC Mode", callback_data="cc-mode"),
         InlineKeyboardButton("🎚 Preset",  callback_data="cc-quality")],
        [InlineKeyboardButton("📐 Resize", callback_data="cc-resize"),
         InlineKeyboardButton("🗜 Target", callback_data="cc-target")],
        [InlineKeyboardButton("🔙 Retour", callback_data="settings-main"),
         InlineKeyboardButton("✖ Fermer", callback_data="close")],
    ])


@on_exact("cc")
async def handle_cc_settings(client, cq, data):
    await cq.message.edit_text(render_cc_settings_text(), reply_markup=render_cc_settings_kb())


# ─────────────────────────────────────────────────────────────
# Catégorie Utility — Caption + Thumbnail + API Keys
# ─────────────────────────────────────────────────────────────

def utility_category_text() -> str:
    return f"🧰 <b>UTILITY</b>\n{DIVIDER}\n\n<b>Choisis :</b>"


def utility_category_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Caption",  callback_data="caption")],
        [InlineKeyboardButton("🖼 Thumbnail", callback_data="thumb")],
        [InlineKeyboardButton("🔑 API Keys",  callback_data="apikeys")],
        [InlineKeyboardButton("🔙 Retour",    callback_data="settings-main")],
    ])


@on_exact("settings-utility")
async def handle_settings_utility_category(client, cq, data):
    await cq.message.edit_text(utility_category_text(), reply_markup=utility_category_kb())


def render_caption_settings_text() -> str:
    return f"✏️ <b>CAPTION STYLE</b>\n{DIVIDER}\n\nCurrent: <code>{BOT.Setting.caption}</code>"


def render_caption_settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Monospace", callback_data="code-Monospace"),
         InlineKeyboardButton("Bold",      callback_data="b-Bold")],
        [InlineKeyboardButton("Italic",    callback_data="i-Italic"),
         InlineKeyboardButton("Underline", callback_data="u-Underlined")],
        [InlineKeyboardButton("Plain",     callback_data="p-Regular")],
        [InlineKeyboardButton("🔙 Retour", callback_data="settings-main"),
         InlineKeyboardButton("✖ Fermer", callback_data="close")],
    ])


@on_exact("caption")
async def handle_caption_settings(client, cq, data):
    await cq.message.edit_text(render_caption_settings_text(), reply_markup=render_caption_settings_kb())


def render_thumb_settings_text() -> str:
    status = "✅ Set" if BOT.Setting.thumbnail else "❌ None"
    return f"🖼 <b>THUMBNAIL</b>\n{DIVIDER}\n\nStatus: {status}\n\nSend a photo to update."


def render_thumb_settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Delete", callback_data="del-thumb")],
        [InlineKeyboardButton("🔙 Retour", callback_data="settings-main"),
         InlineKeyboardButton("✖ Fermer", callback_data="close")],
    ])


@on_exact("thumb")
async def handle_thumb_settings(client, cq, data):
    await cq.message.edit_text(render_thumb_settings_text(), reply_markup=render_thumb_settings_kb())


@on_exact("del-thumb")
async def handle_del_thumb(client, cq, data):
    if BOT.Setting.thumbnail:
        try:
            os.remove(Paths.THMB_PATH)
        except Exception:
            pass
    BOT.Setting.thumbnail = False
    await cq.answer("🗑 Thumbnail supprimé")
    await cq.message.edit_text(render_thumb_settings_text(), reply_markup=render_thumb_settings_kb())


def dumps_kb() -> InlineKeyboardMarkup:
    rows = []
    for cid in BOT.Options.dump_ids:
        rows.append([InlineKeyboardButton(f"🗑 {cid}", callback_data=f"dump_remove|{cid}")])
    rows.append([InlineKeyboardButton("🔙 Retour", callback_data="settings-main")])
    return InlineKeyboardMarkup(rows)


def dumps_text() -> str:
    if not BOT.Options.dump_ids:
        return (
            f"📦 <b>CANAUX DUMP</b>\n{DIVIDER}\n\n"
            "Aucun canal configuré.\n\n"
            "Ajoute-en un avec :\n"
            "<code>/add @mon_channel</code>\n"
            "<code>/add -1001234567890</code>"
        )
    lines = "\n".join(f"· <code>{cid}</code>" for cid in BOT.Options.dump_ids)
    return (
        f"📦 <b>CANAUX DUMP</b>\n{DIVIDER}\n\n"
        f"{lines}\n\n"
        "Ajoute-en un autre avec <code>/add @channel</code>\n"
        "Tape sur 🗑 pour en retirer un."
    )


@on_exact("dumps")
async def handle_dumps(client, cq, data):
    await cq.message.edit_text(dumps_text(), reply_markup=dumps_kb())


@on_prefix("dump_remove|")
async def handle_dump_remove(client, cq, data):
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
    await cq.message.edit_text(dumps_text(), reply_markup=dumps_kb())


def apikeys_kb() -> InlineKeyboardMarkup:
    rows = []
    for i, key in enumerate(BOT.Options.cc_api_keys):
        rows.append([InlineKeyboardButton(f"🗑 CC · {mask_key(key)}", callback_data=f"apikey_remove|cc|{i}")])
    for i, key in enumerate(BOT.Options.fc_api_keys):
        rows.append([InlineKeyboardButton(f"🗑 FC · {mask_key(key)}", callback_data=f"apikey_remove|fc|{i}")])
    rows.append([InlineKeyboardButton("🔙 Retour", callback_data="settings-main")])
    return InlineKeyboardMarkup(rows)


def apikeys_text() -> str:
    cc_lines = "\n".join(f"· <code>{mask_key(k)}</code>" for k in BOT.Options.cc_api_keys) or "Aucune"
    fc_lines = "\n".join(f"· <code>{mask_key(k)}</code>" for k in BOT.Options.fc_api_keys) or "Aucune"
    return (
        f"🔑 <b>CLÉS API</b>\n{DIVIDER}\n\n"
        f"<b>☁️ CloudConvert</b>\n{cc_lines}\n\n"
        f"<b>🆓 FreeConvert</b>\n{fc_lines}\n\n"
        "Ajoute-en une avec :\n"
        "<code>/addcc TA_CLE</code>\n"
        "<code>/addfc TA_CLE</code>\n\n"
        "Tape sur 🗑 pour en retirer une."
    )


@on_exact("apikeys")
async def handle_apikeys(client, cq, data):
    await cq.message.edit_text(apikeys_text(), reply_markup=apikeys_kb())


@on_prefix("apikey_remove|")
async def handle_apikey_remove(client, cq, data):
    _, kind, idx_str = data.split("|")
    idx = int(idx_str)
    target_list = BOT.Options.cc_api_keys if kind == "cc" else BOT.Options.fc_api_keys
    if 0 <= idx < len(target_list):
        target_list.pop(idx)
        await cq.answer("🗑 Retirée")
    else:
        await cq.answer("Déjà retirée.")
    await cq.message.edit_text(apikeys_text(), reply_markup=apikeys_kb())


# ─────────────────────────────────────────────────────────────
# Catégorie Upload — AutoFwd + Dumps + Upload mode + Prefix + Suffix
# ─────────────────────────────────────────────────────────────

def upload_category_text() -> str:
    return (
        f"📤 <b>UPLOAD</b>\n{DIVIDER}\n\n"
        f"AutoFwd      <code>{BOT.Setting.auto_forward}</code>\n"
        f"Upload mode  <code>{BOT.Setting.stream_upload}</code>\n\n"
        "<b>Choisis :</b>"
    )


def upload_category_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔀 AutoFwd",     callback_data="autofwd"),
         InlineKeyboardButton("📤 Upload mode", callback_data="upload-mode")],
        [InlineKeyboardButton("📦 Dumps", callback_data="dumps")],
        [InlineKeyboardButton("🔤 Prefix", callback_data="set-prefix"),
         InlineKeyboardButton("🔤 Suffix", callback_data="set-suffix")],
        [InlineKeyboardButton("🔙 Retour", callback_data="settings-main")],
    ])


@on_exact("settings-upload")
async def handle_settings_upload_category(client, cq, data):
    await cq.message.edit_text(upload_category_text(), reply_markup=upload_category_kb())


@on_exact("autofwd")
async def handle_autofwd(client, cq, data):
    if not BOT.Options.dump_ids:
        await cq.answer("Ajoute d'abord un canal avec /add @channel", show_alert=True)
        return
    BOT.Options.auto_forward = not BOT.Options.auto_forward
    BOT.Setting.auto_forward = "On" if BOT.Options.auto_forward else "Off"
    await cq.answer(f"AutoFwd {BOT.Setting.auto_forward}")
    await cq.message.edit_text(upload_category_text(), reply_markup=upload_category_kb())


def upload_mode_text() -> str:
    return f"📤 <b>UPLOAD MODE</b>\n{DIVIDER}\n\nCurrent: <code>{BOT.Setting.stream_upload}</code>"


def upload_mode_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📹 Media",    callback_data="media"),
         InlineKeyboardButton("📄 Document", callback_data="document")],
        [InlineKeyboardButton("🔙 Retour", callback_data="settings-main")],
    ])


@on_exact("upload-mode")
async def handle_upload_mode(client, cq, data):
    await cq.message.edit_text(upload_mode_text(), reply_markup=upload_mode_kb())


@on_exact("media", "document")
async def handle_stream_upload_choice(client, cq, data):
    BOT.Options.stream_upload = data == "media"
    BOT.Setting.stream_upload = "Media" if data == "media" else "Document"
    await cq.answer(BOT.Setting.stream_upload)
    await cq.message.edit_text(upload_mode_text(), reply_markup=upload_mode_kb())


@on_exact("set-prefix")
async def handle_set_prefix(client, cq, data):
    await cq.message.edit_text("Reply with your <b>prefix</b> text:")
    BOT.State.prefix = True


@on_exact("set-suffix")
async def handle_set_suffix(client, cq, data):
    await cq.message.edit_text("Reply with your <b>suffix</b> text:")
    BOT.State.suffix = True


# ─────────────────────────────────────────────────────────────
# Choix inline partagés (rafraîchissent l'écran de réglage courant)
# ─────────────────────────────────────────────────────────────

@on_exact("code-Monospace", "p-Regular", "b-Bold", "i-Italic", "u-Underlined")
async def handle_caption_choice(client, cq, data):
    r = data.split("-")
    BOT.Options.caption = r[0]
    BOT.Setting.caption = r[1]
    await cq.answer()
    await cq.message.edit_text(render_caption_settings_text(), reply_markup=render_caption_settings_kb())


@on_exact("split-true", "split-false")
async def handle_split_choice(client, cq, data):
    BOT.Options.is_split    = data == "split-true"
    BOT.Setting.split_video = "Split" if data == "split-true" else "Zip"
    await cq.answer()
    await cq.message.edit_text(render_video_settings_text(), reply_markup=render_video_settings_kb())


@on_exact("convert-true", "convert-false", "mp4", "mkv", "q-High", "q-Low")
async def handle_convert_choice(client, cq, data):
    if data == "convert-true":
        BOT.Options.convert_video = True
        BOT.Setting.convert_video = "Yes"
    elif data == "convert-false":
        BOT.Options.convert_video = False
        BOT.Setting.convert_video = "No"
    elif data == "q-High":
        BOT.Setting.convert_quality = "High"
        BOT.Options.convert_quality = True
    elif data == "q-Low":
        BOT.Setting.convert_quality = "Low"
        BOT.Options.convert_quality = False
    else:
        BOT.Options.video_out = data
    await cq.answer()
    await cq.message.edit_text(render_video_settings_text(), reply_markup=render_video_settings_kb())


@on_exact("cc-mode")
async def handle_cc_mode(client, cq, data):
    cycle = ["balanced", "economy"]
    cur = str(BOT.Options.cc_engine_mode or "balanced").lower()
    nxt = cycle[(cycle.index(cur) + 1) % len(cycle)] if cur in cycle else "balanced"
    BOT.Options.cc_engine_mode = nxt
    BOT.Setting.cc_engine_mode = cc_mode_label(nxt)
    await cq.answer(BOT.Setting.cc_engine_mode)
    await cq.message.edit_text(render_cc_settings_text(), reply_markup=render_cc_settings_kb())


@on_exact("cc-quality")
async def handle_cc_quality(client, cq, data):
    cycle = ["fast", "balanced", "small", "best"]
    cur = str(BOT.Options.cc_quality_profile or "balanced").lower()
    nxt = cycle[(cycle.index(cur) + 1) % len(cycle)] if cur in cycle else "balanced"
    BOT.Options.cc_quality_profile = nxt
    BOT.Setting.cc_quality_profile = quality_label(nxt)
    await cq.answer(BOT.Setting.cc_quality_profile)
    await cq.message.edit_text(render_cc_settings_text(), reply_markup=render_cc_settings_kb())


@on_exact("cc-resize")
async def handle_cc_resize(client, cq, data):
    cycle = [0, 480, 720, 1080]
    cur = int(BOT.Options.cc_resize or 0)
    nxt = cycle[(cycle.index(cur) + 1) % len(cycle)] if cur in cycle else 720
    BOT.Options.cc_resize = nxt
    BOT.Setting.cc_resize = resize_label(nxt)
    await cq.answer(BOT.Setting.cc_resize)
    await cq.message.edit_text(render_cc_settings_text(), reply_markup=render_cc_settings_kb())


@on_exact("cc-target")
async def handle_cc_target(client, cq, data):
    cycle = [50, 100, 200, 500]
    cur = int(BOT.Options.cc_target_size_mb or 100)
    nxt = cycle[(cycle.index(cur) + 1) % len(cycle)] if cur in cycle else 100
    BOT.Options.cc_target_size_mb = nxt
    BOT.Setting.cc_target_size = f"{nxt} MB"
    await cq.answer(BOT.Setting.cc_target_size)
    await cq.message.edit_text(render_cc_settings_text(), reply_markup=render_cc_settings_kb())


# ─────────────────────────────────────────────────────────────
# Boutons génériques utilisés ailleurs dans le bot
# ─────────────────────────────────────────────────────────────

@on_exact("close")
async def handle_close(client, cq, data):
    await cq.message.delete()


@on_exact("back")
async def handle_back(client, cq, data):
    await send_settings(client, cq.message, cq.message.id, False)


@on_exact("cancel")
async def handle_cancel(client, cq, data):
    await cancelTask("Cancelled by user")


@on_prefix("canceljob_")
async def handle_canceljob(client, cq, data):
    job_id = data.split("_", 1)[1]
    ok = ActiveJobs.cancel(job_id)
    await cq.answer(
        "⛔ Annulation en cours..." if ok else "Ce job est déjà terminé.",
        show_alert=not ok,
    )
