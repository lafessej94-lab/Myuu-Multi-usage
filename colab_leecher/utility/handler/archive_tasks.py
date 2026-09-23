"""
colab_leecher/utility/handler/archive_tasks.py

Zip_Handler / Unzip_Handler — compression/extraction sur le dossier de
téléchargement, avant leech.

Extrait de handler.py — voir __init__.py de ce package pour la liste
complète des réexports.
"""
from __future__ import annotations

import logging
import pathlib
import shutil
from asyncio import sleep
from os import makedirs, path as ospath

from natsort import natsorted

from colab_leecher.utility.converters import archive, extract
from colab_leecher.utility.helper import getSize, keyboard, sysINFO
from colab_leecher.utility.variables import MSG, Messages, Paths, TaskInfo, Transfer

log = logging.getLogger(__name__)


async def Zip_Handler(down_path: str, is_split: bool, remove: bool):
    Messages.status_head = f"🗜 <b>COMPRESSING</b>\n\n<code>{Messages.download_name}</code>\n"
    TaskInfo.set(phase="process", engine="zip", filename=Messages.download_name)
    try:
        MSG.status_msg = await MSG.status_msg.edit_text(
            text=Messages.task_msg + Messages.status_head + sysINFO(),
            reply_markup=keyboard(),
        )
    except Exception:
        pass
    if not ospath.exists(Paths.temp_zpath):
        makedirs(Paths.temp_zpath)
    await archive(down_path, is_split, remove)
    await sleep(2)
    Transfer.total_down_size = getSize(Paths.temp_zpath)
    if remove and ospath.exists(down_path):
        shutil.rmtree(down_path)


async def Unzip_Handler(down_path: str, remove: bool):
    Messages.status_head = f"📂 <b>EXTRACTING</b>\n\n<code>{Messages.download_name}</code>\n"
    TaskInfo.set(phase="process", engine="unzip", filename=Messages.download_name)
    try:
        MSG.status_msg = await MSG.status_msg.edit_text(
            text=Messages.task_msg + Messages.status_head
            + "\n⏳ <i>Starting...</i>" + sysINFO(),
            reply_markup=keyboard(),
        )
    except Exception:
        pass
    filenames = natsorted([str(p) for p in pathlib.Path(down_path).glob("**/*") if p.is_file()])
    for f in filenames:
        short_path = ospath.join(down_path, f)
        if not ospath.exists(Paths.temp_unzip_path):
            makedirs(Paths.temp_unzip_path)
        _, ext = ospath.splitext(ospath.basename(f).lower())
        try:
            if ospath.exists(short_path):
                if ext in [".7z", ".gz", ".zip", ".rar", ".001", ".tar", ".z01"]:
                    await extract(short_path, remove)
                else:
                    shutil.copy(short_path, Paths.temp_unzip_path)
        except Exception as e:
            log.warning(f"Unzip error: {e}")
    if remove:
        shutil.rmtree(down_path)
