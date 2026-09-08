"""
colab_leecher/link_menu.py

Sectioned "link received" menu — same visual pattern as
services/url_views.py / url_handler.py (main -> Download / Inspect /
Process / Cloud -> Back), but every button below dispatches the EXACT
same callback_data that _mode_keyboard() already produced ("normal",
"zip", "cc_convert", "seedr_fc_hardsub", "sx_open", ...).

Nothing about job execution, StatusSlideshow, or hardsub progress text
changes — this file only reorganizes navigation. The only new
callback_data values are "lk|sec|<section>" and "lk|cancel", handled by
a small block added at the top of callbacks() in colab_leecher (see the
integration notes at the bottom of this file).
"""
from __future__ import annotations

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def link_header(kind_label: str, n: int, section_title: str = "", section_hint: str = "") -> str:
    head = f"{kind_label}\n<code>{n}</code> source(s)"
    if section_title:
        head += f" · <b>{section_title}</b>"
    else:
        head += " · <b>Choisis une section :</b>"
    if section_hint:
        head += f"\n<i>{section_hint}</i>"
    return head


def kind_label(is_ytdl: bool, is_magnet: bool) -> str:
    if is_ytdl:
        return "🏮 Lien YTDL"
    if is_magnet:
        return "🧲 Magnet détecté"
    return "🔗 Lien détecté"


def main_kb(is_magnet: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📥 Download", callback_data="lk|sec|download")],
        [InlineKeyboardButton("🎞 Inspect", callback_data="lk|sec|inspect")],
        [InlineKeyboardButton("🛠 Process", callback_data="lk|sec|process")],
    ]
    if is_magnet:
        rows.append([InlineKeyboardButton("☁️ Cloud", callback_data="lk|sec|cloud")])
    rows.append([InlineKeyboardButton("❌ Annuler", callback_data="lk|cancel")])
    return InlineKeyboardMarkup(rows)


def download_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Normal", callback_data="normal"),
         InlineKeyboardButton("🗜 Compresser", callback_data="zip")],
        [InlineKeyboardButton("📂 Extraire", callback_data="unzip"),
         InlineKeyboardButton("♻️ Ré-archiver", callback_data="undzip")],
        [InlineKeyboardButton("🔙 Retour", callback_data="lk|sec|main"),
         InlineKeyboardButton("❌ Annuler", callback_data="lk|cancel")],
    ])


def inspect_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎞 Extraire pistes (streams)", callback_data="sx_open")],
        [InlineKeyboardButton("🔙 Retour", callback_data="lk|sec|main"),
         InlineKeyboardButton("❌ Annuler", callback_data="lk|cancel")],
    ])


def process_kb(is_http: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🔄 Convertir", callback_data="cc_convert"),
         InlineKeyboardButton("📐 Redimensionner", callback_data="cc_resize")],
        [InlineKeyboardButton("🧱 Compresser", callback_data="cc_compress")],
    ]
    if is_http:
        rows.append([
            InlineKeyboardButton("☁️ CC Hardsub", callback_data="cc_hardsub_manual"),
            InlineKeyboardButton("🆓 FC Hardsub", callback_data="fc_hardsub_manual"),
        ])
    rows.append([InlineKeyboardButton("🔙 Retour", callback_data="lk|sec|main"),
                 InlineKeyboardButton("❌ Annuler", callback_data="lk|cancel")])
    return InlineKeyboardMarkup(rows)


def cloud_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("☁️ Seedr+CC Convert", callback_data="seedr_cc_convert")],
        [InlineKeyboardButton("☁️ CC Hardsub", callback_data="seedr_cc_hardsub"),
         InlineKeyboardButton("🆓 FC Hardsub", callback_data="seedr_fc_hardsub")],
        [InlineKeyboardButton("🔙 Retour", callback_data="lk|sec|main"),
         InlineKeyboardButton("❌ Annuler", callback_data="lk|cancel")],
    ])


# ─────────────────────────────────────────────────────────────
# INTEGRATION NOTES (3 edits in colab_leecher's link handler file)
# ─────────────────────────────────────────────────────────────
#
# 1) Replace the body of `_mode_keyboard()` with a call to `main_kb()`,
#    or just delete `_mode_keyboard()` entirely and use `main_kb()` directly.
#
# 2) In `handle_url()`, where it currently does:
#
#       sent = await message.reply_text(
#           f"{kind_label}\n<code>{n}</code> source(s) · <b>Choisis un mode :</b>",
#           reply_markup=_mode_keyboard(), quote=True,
#       )
#
#    replace with:
#
#       from colab_leecher.link_menu import kind_label as _lk_label, link_header, main_kb
#       is_magnet = first_src.startswith("magnet:?xt=urn:btih:")
#       label = _lk_label(BOT.Mode.ytdl, is_magnet)
#       sent = await message.reply_text(
#           link_header(label, n),
#           reply_markup=main_kb(is_magnet), quote=True,
#       )
#
# 3) In `callbacks()`, add this block right after the `if data == "noop":`
#    check (before the Help/Settings section) — it only ever reacts to
#    the two new callback_data values, everything else falls through to
#    your existing if/elif chain untouched:
#
#       if data.startswith("lk|sec|") or data == "lk|cancel":
#           from colab_leecher.link_menu import (
#               kind_label as _lk_label, link_header, main_kb,
#               download_kb, inspect_kb, process_kb, cloud_kb,
#           )
#           session_src = _link_sessions.get(cq.message.id, BOT.SOURCE or [""])
#           first = (session_src or [""])[0].strip()
#           is_magnet = first.startswith("magnet:?xt=urn:btih:")
#           is_http = first.startswith("http://") or first.startswith("https://")
#           n = len([l for l in session_src if l.strip()])
#           label = _lk_label(BOT.Mode.ytdl, is_magnet)
#
#           if data == "lk|cancel":
#               _link_sessions.pop(cq.message.id, None)
#               await cq.answer()
#               await cq.message.delete()
#               return
#
#           section = data.split("|", 2)[2]
#           await cq.answer()
#           if section == "main":
#               await cq.message.edit_text(link_header(label, n), reply_markup=main_kb(is_magnet))
#           elif section == "download":
#               await cq.message.edit_text(
#                   link_header(label, n, "Download", "Choisis le format de sortie."),
#                   reply_markup=download_kb(),
#               )
#           elif section == "inspect":
#               await cq.message.edit_text(
#                   link_header(label, n, "Inspect", "Analyse la source avant de lancer quoi que ce soit."),
#                   reply_markup=inspect_kb(),
#               )
#           elif section == "process":
#               await cq.message.edit_text(
#                   link_header(label, n, "Process", "CloudConvert / hardsub sur ce lien."),
#                   reply_markup=process_kb(is_http),
#               )
#           elif section == "cloud":
#               if not is_magnet:
#                   await cq.answer("Cloud needs a magnet link.", show_alert=True)
#                   return
#               await cq.message.edit_text(
#                   link_header(label, n, "Cloud", "Seedr + FreeConvert / CloudConvert."),
#                   reply_markup=cloud_kb(),
#               )
#           return
#
# Everything downstream (StatusSlideshow, _fc_job_status-equivalent texts,
# Seedr_*_Handler, Direct_*_Hardsub_Handler, sx_open flow, ...) is
# untouched: the buttons at the leaves ("normal", "cc_convert",
# "seedr_fc_hardsub", "sx_open", ...) still carry the identical
# callback_data your existing if/elif chain already handles.
