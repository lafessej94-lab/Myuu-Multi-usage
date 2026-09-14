"""
colab_leecher/services/start_menu.py

Callback handlers for the buttons shown by /start: cb_help, cb_settings,
check_sub, cb_back_start. Extracted verbatim from __main__.py's callbacks().
"""
from __future__ import annotations

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from colab_leecher.services import on_exact
from colab_leecher.services.access_gate import is_subscribed
from colab_leecher.utility.helper import send_settings


@on_exact("cb_help")
async def handle_cb_help(client, cq, data):
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


@on_exact("cb_settings")
async def handle_cb_settings(client, cq, data):
    await cq.answer()
    from colab_leecher import OWNER
    is_owner = cq.from_user and cq.from_user.id == OWNER
    await send_settings(client, cq.message, cq.message.id, False, readonly=not is_owner)


@on_exact("check_sub")
async def handle_check_sub(client, cq, data):
    if await is_subscribed(client, cq.from_user.id):
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
        from colab_leecher.services.access_gate import REQUIRED_CHANNEL
        await cq.answer(f"❌ Toujours pas abonné à {REQUIRED_CHANNEL}", show_alert=True)


@on_exact("cb_back_start")
async def handle_cb_back_start(client, cq, data):
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
