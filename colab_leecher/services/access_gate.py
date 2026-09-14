"""
colab_leecher/services/access_gate.py

Subscription-gate helpers (REQUIRED_CHANNEL, _is_subscribed, _join_gate_kb)
extracted verbatim from __main__.py. Several command handlers that stay in
__main__.py (/start, handle_url, /settings) use these too, so they're kept
here as plain importable functions rather than wired into the callback
dispatcher directly.
"""
from __future__ import annotations

import logging

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

REQUIRED_CHANNEL = "@hebdos"


async def is_subscribed(client, user_id: int) -> bool:
    try:
        member = await client.get_chat_member(REQUIRED_CHANNEL, user_id)
        return str(member.status).lower() not in ("left", "banned", "kicked")
    except Exception as exc:
        logging.debug(f"Subscription check failed: {exc}")
        return False


def join_gate_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Rejoindre " + REQUIRED_CHANNEL, url=f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}")],
        [InlineKeyboardButton("✅ J'ai rejoint", callback_data="check_sub")],
    ])
