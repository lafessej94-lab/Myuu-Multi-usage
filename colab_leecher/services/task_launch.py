"""
colab_leecher/services/task_launch.py

The plain leech / CC-direct-on-source launcher — the very first branch of
the old callbacks(): "normal", "zip", "unzip", "undzip", "cc_convert",
"cc_resize", "cc_compress". Extracted verbatim.
"""
from __future__ import annotations

from asyncio import get_event_loop
from datetime import datetime

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from colab_leecher.services import on_exact
from colab_leecher.status_slideshow import StatusSlideshow
from colab_leecher.utility.task_manager import taskScheduler
from colab_leecher.utility.variables import BOT, MSG, BotTimes, TaskInfo


@on_exact("normal", "zip", "unzip", "undzip", "cc_convert", "cc_resize", "cc_compress")
async def handle_task_launch(client, cq, data):
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
