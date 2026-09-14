"""
colab_leecher/services/claude_menu.py

Everything tied to the Claude agent (/Relève, /Arise, /search_claude)
button flow. The pending-state dicts and keyboard builders here are also
used directly by the releve_cmd / search_claude_cmd command handlers that
stay in __main__.py — import them from this module rather than
re-declaring them there.

Extracted verbatim from __main__.py (kb builders + the claude_* branches
of callbacks()).
"""
from __future__ import annotations

from asyncio import get_event_loop

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from colab_leecher.claude_agent import run_manual_hardsub, run_pipeline_for_entry
from colab_leecher.services import on_exact, on_prefix
from colab_leecher.services.hardsub_flow import FC_RESOLUTIONS
from colab_leecher.utility.variables import BOT

# _pending_claude_search : message_id -> {"entry": NyaaEntry} — porte
# l'entrée trouvée à travers les 2 étapes (Oui/Non puis choix de qualité).
pending_claude_search: dict[int, dict] = {}

# _pending_claude_picker : message_id -> {"entries": list[NyaaEntry],
#   "selected": NyaaEntry|None}.
pending_claude_picker: dict[int, dict] = {}


def claude_picker_kb(entries: list) -> InlineKeyboardMarkup:
    rows = []
    for i, entry in enumerate(entries):
        label = entry.title[:60] + ("…" if len(entry.title) > 60 else "")
        rows.append([InlineKeyboardButton(f"🎬 {label}", callback_data=f"claude_pick|{i}")])
    rows.append([InlineKeyboardButton("❌ Fermer", callback_data="claude_pick_close")])
    return InlineKeyboardMarkup(rows)


def claude_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Done", callback_data="claude_pick_done"),
        InlineKeyboardButton("❌ Cancel", callback_data="claude_pick_cancel"),
    ]])


def claude_quality_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 Qualité d'origine", callback_data="claude_res|orig")],
        [InlineKeyboardButton("360p", callback_data="claude_res|360"),
         InlineKeyboardButton("480p", callback_data="claude_res|480")],
        [InlineKeyboardButton("720p", callback_data="claude_res|720")],
    ])


@on_exact("claude_search_no")
async def handle_claude_search_no(client, cq, data):
    pending_claude_search.pop(cq.message.id, None)
    await cq.answer()
    await cq.message.edit_text("❌ Encodage annulé.")


@on_exact("claude_search_yes")
async def handle_claude_search_yes(client, cq, data):
    pending = pending_claude_search.get(cq.message.id)
    if not pending:
        await cq.answer("Session expirée, relance /search_claude.", show_alert=True)
        return
    await cq.answer()
    await cq.message.edit_text(
        "📦 <b>Release trouvée</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<code>{pending['entry'].title}</code>\n\n"
        "Choisis la qualité de sortie :",
        reply_markup=claude_quality_kb(),
    )


@on_prefix("claude_res|")
async def handle_claude_res(client, cq, data):
    code = data.split("|", 1)[1]
    pending = pending_claude_search.pop(cq.message.id, None)
    if not pending:
        await cq.answer("Session expirée, relance /search_claude.", show_alert=True)
        return
    if not BOT.Options.fc_api_keys:
        await cq.answer("FreeConvert API key missing — use /addfc YOUR_KEY.", show_alert=True)
        return

    entry = pending["entry"]
    resize = FC_RESOLUTIONS.get(code)
    quality_label_txt = {
        "orig": "Qualité d'origine", "360": "360p", "480": "480p", "720": "720p",
    }.get(code, code)

    await cq.answer()
    await cq.message.edit_text(
        f"🤖 <b>Claude démarre l'encodage</b>\n\n"
        f"<code>{entry.title}</code>\n"
        f"Qualité : <code>{quality_label_txt}</code>\n\n"
        "Tu recevras une notification à chaque étape."
    )
    get_event_loop().create_task(run_manual_hardsub(entry, resize, quality_label_txt))


@on_exact("claude_pick_close")
async def handle_claude_pick_close(client, cq, data):
    pending_claude_picker.pop(cq.message.id, None)
    await cq.answer()
    await cq.message.edit_text("🤖 Claude reste réveillé — surveillance active en arrière-plan.")


@on_prefix("claude_pick|")
async def handle_claude_pick(client, cq, data):
    idx = int(data.split("|", 1)[1])
    pending = pending_claude_picker.get(cq.message.id)
    if not pending or idx >= len(pending["entries"]):
        await cq.answer("Session expirée, relance /Relève.", show_alert=True)
        return
    entry = pending["entries"][idx]
    pending["selected"] = entry
    await cq.answer()
    await cq.message.edit_text(
        "🤖 <b>Confirmation</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<code>{entry.title}</code>\n\n"
        "Lancer l'encodage (360p + 720p, style B) ?",
        reply_markup=claude_confirm_kb(),
    )


@on_exact("claude_pick_cancel")
async def handle_claude_pick_cancel(client, cq, data):
    pending = pending_claude_picker.get(cq.message.id)
    if not pending:
        await cq.answer("Session expirée, relance /Relève.", show_alert=True)
        return
    pending["selected"] = None
    await cq.answer()
    await cq.message.edit_text(
        "🤖 <b>Claude est réveillé</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>{len(pending['entries'])} épisode(s)</b> détecté(s) dans les 15 dernières minutes.\n"
        "Choisis lequel encoder (360p + 720p, style B) :",
        reply_markup=claude_picker_kb(pending["entries"]),
    )


@on_exact("claude_pick_done")
async def handle_claude_pick_done(client, cq, data):
    pending = pending_claude_picker.pop(cq.message.id, None)
    entry = pending.get("selected") if pending else None
    if not entry:
        await cq.answer("Session expirée, relance /Relève.", show_alert=True)
        return
    if not BOT.Options.fc_api_keys:
        await cq.answer("FreeConvert API key missing — use /addfc YOUR_KEY.", show_alert=True)
        return

    await cq.answer()
    await cq.message.edit_text(
        f"🤖 <b>Claude démarre l'encodage</b>\n\n"
        f"<code>{entry.title}</code>\n"
        "360p puis 720p, style B.\n\n"
        "Tu recevras une notification à chaque étape."
    )
    get_event_loop().create_task(run_pipeline_for_entry(entry))
