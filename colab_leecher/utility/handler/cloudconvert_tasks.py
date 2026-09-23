"""
colab_leecher/utility/handler/cloudconvert_tasks.py

CloudConvert_Handler — convert/resize/compress sur des fichiers déjà
téléchargés localement (pas de Seedr, pas de lien distant — voir
seedr_tasks.py pour ça).

Extrait de handler.py — voir __init__.py de ce package pour la liste
complète des réexports.
"""
from __future__ import annotations

import os
import pathlib
import shutil
from os import makedirs, path as ospath
from time import time

from natsort import natsorted

from colab_leecher.engines.cloudconvert import (
    cc_mode_label,
    compress_file,
    convert_file,
    quality_label,
    resize_file,
    resize_label,
)
from colab_leecher.utility.handler.leech import Leech
from colab_leecher.utility.handler.task_control import cancelTask
from colab_leecher.utility.helper import fileType, keyboard, sysINFO
from colab_leecher.utility.variables import BOT, MSG, Messages, Paths, TaskInfo


async def CloudConvert_Handler(folder_path: str, remove: bool):
    if not BOT.Options.cc_api_keys:
        await cancelTask("CloudConvert API key is missing in your Colab launcher.")
        return

    files = natsorted([str(p) for p in pathlib.Path(folder_path).glob("**/*") if p.is_file()])
    video_files = [f for f in files if fileType(f) == "video"]
    if not video_files:
        await cancelTask("CloudConvert mode needs at least one video file.")
        return

    if ospath.exists(Paths.temp_cc_path):
        shutil.rmtree(Paths.temp_cc_path)
    makedirs(Paths.temp_cc_path)

    for f in files:
        if fileType(f) == "video":
            continue
        rel = ospath.relpath(f, folder_path)
        dest = ospath.join(Paths.temp_cc_path, rel)
        os.makedirs(ospath.dirname(dest), exist_ok=True)
        shutil.copy2(f, dest)

    total_videos = len(video_files)

    for idx, video_path in enumerate(video_files):
        rel = ospath.relpath(video_path, folder_path)
        out_dir = ospath.join(Paths.temp_cc_path, ospath.dirname(rel))
        makedirs(out_dir, exist_ok=True)
        display_name = ospath.basename(video_path)
        chunk_start = idx / total_videos * 100.0
        chunk_end = (idx + 1) / total_videos * 100.0
        stage_state = {"last": 0.0}

        async def _cc_update(stage: str, pct: float, detail: str) -> None:
            now = time()
            if now - stage_state["last"] < 2 and pct < 100:
                return
            stage_state["last"] = now
            overall = chunk_start + ((chunk_end - chunk_start) * max(0.0, min(pct, 100.0)) / 100.0)
            TaskInfo.set(
                phase="process",
                engine="CloudConvert",
                filename=display_name,
                percentage=overall,
                speed=detail,
                eta="-",
            )
            text = (
                f"☁️ <b>CLOUDCONVERT</b>\n\n"
                f"<code>{display_name}</code>\n\n"
                f"<b>Stage</b>  <code>{stage}</code>\n"
                f"<b>Progress</b>  <code>{overall:.1f}%</code>\n"
                f"<b>Mode</b>  <code>{cc_mode_label(BOT.Options.cc_engine_mode)}</code>\n"
                f"<b>Preset</b>  <code>{quality_label(BOT.Options.cc_quality_profile)}</code>\n"
                f"<b>Detail</b>  <code>{detail}</code>"
            )
            try:
                await MSG.status_msg.edit_text(
                    text=Messages.task_msg + text + sysINFO(),
                    reply_markup=keyboard(),
                    disable_web_page_preview=True,
                )
            except Exception:
                pass

        upload_cb = lambda pct, detail: _cc_update("Upload", pct * 0.35, detail)
        process_cb = lambda pct, detail: _cc_update("Process", 35.0 + (pct * 0.5), detail)
        download_cb = lambda pct, detail: _cc_update("Download", 85.0 + (pct * 0.15), detail)

        try:
            if BOT.Mode.type == "cc_convert":
                await _cc_update("Convert", 0.0, "Preparing CloudConvert job")
                await convert_file(
                    ",".join(BOT.Options.cc_api_keys),
                    video_path,
                    out_dir,
                    output_ext=BOT.Options.video_out,
                    cc_mode=BOT.Options.cc_engine_mode,
                    quality_profile=BOT.Options.cc_quality_profile,
                    upload_cb=upload_cb,
                    process_cb=process_cb,
                    download_cb=download_cb,
                )
            elif BOT.Mode.type == "cc_resize":
                await _cc_update("Resize", 0.0, f"Target {resize_label(BOT.Options.cc_resize)}")
                await resize_file(
                    ",".join(BOT.Options.cc_api_keys),
                    video_path,
                    out_dir,
                    height=BOT.Options.cc_resize,
                    output_ext=BOT.Options.video_out,
                    cc_mode=BOT.Options.cc_engine_mode,
                    quality_profile=BOT.Options.cc_quality_profile,
                    upload_cb=upload_cb,
                    process_cb=process_cb,
                    download_cb=download_cb,
                )
            else:
                await _cc_update("Compress", 0.0, f"Target {BOT.Setting.cc_target_size}")
                await compress_file(
                    ",".join(BOT.Options.cc_api_keys),
                    video_path,
                    out_dir,
                    target_mb=BOT.Options.cc_target_size_mb,
                    cc_mode=BOT.Options.cc_engine_mode,
                    upload_cb=upload_cb,
                    process_cb=process_cb,
                    download_cb=download_cb,
                )
        except Exception as exc:
            await cancelTask(f"CloudConvert failed: {display_name}\n\n{exc}")
            return

    await Leech(Paths.temp_cc_path, True, convert_videos=False)
    if remove and ospath.exists(folder_path):
        shutil.rmtree(folder_path)
