"""
colab_leecher/utility/handler/seedr_tasks.py

Les 3 flows Seedr : Seedr+CloudConvert Convert, Seedr+CloudConvert Hardsub
(séquentiels, gated par BOT.State.task_going), et Seedr+FreeConvert Hardsub
(concurrent, jusqu'à FC_HARDSUB_CONCURRENCY jobs en parallèle — voir
shared.py pour le semaphore, partagé avec direct_hardsub_tasks.py).

Extrait de handler.py — voir __init__.py de ce package pour la liste
complète des réexports.
"""
from __future__ import annotations

import os
import shutil
import uuid
from os import makedirs, path as ospath

from colab_leecher import SEEDR_PASSWORD, SEEDR_USERNAME, colab_bot
from colab_leecher.engines.cloudconvert import (
    cc_mode_label,
    convert_remote_url,
    hardsub_remote_url,
    quality_label,
)
from colab_leecher.engines.freeconvert import (
    hardsub_remote_url as fc_hardsub_remote_url,
)
from colab_leecher.engines.seedr import SeedrError, _del_folder, fetch_urls_via_seedr
from colab_leecher.services.job_views import seedr_status_view
from colab_leecher.services.subtitle_probe import (
    extract_subtitle_from_url,
    pick_french_text_subtitle,
    probe_remote_video,
)
from colab_leecher.smart_rename import build_final_name, resolution_label
from colab_leecher.utility.handler.leech import Leech
from colab_leecher.utility.handler.shared import (
    _burn_prefix_suffix_in_dir,
    _fc_hardsub_semaphore,
    _fc_job_status,
    _finish_status,
)
from colab_leecher.utility.handler.task_control import cancelTask
from colab_leecher.utility.helper import fileType, keyboard, sysINFO
from colab_leecher.utility.variables import BOT, MSG, Messages, Paths, TaskInfo


def _seedr_ready() -> bool:
    return bool((SEEDR_USERNAME or os.environ.get("SEEDR_USERNAME", "")).strip()) and bool(
        (SEEDR_PASSWORD or os.environ.get("SEEDR_PASSWORD", "")).strip()
    )


def _seedr_video_files(files: list[dict]) -> list[dict]:
    videos = [f for f in files if fileType(f.get("name", "")) == "video" and f.get("url")]
    return sorted(videos, key=lambda item: int(item.get("size", 0) or 0), reverse=True)


async def _seedr_status(kind: str, stage: str, pct: float, detail: str, filename: str = "") -> None:
    """Thin wrapper: texte vient de services.job_views.seedr_status_view
    (pure, pas d'I/O) ; seul le TaskInfo.set() + edit_text() vivent ici."""
    pct_clamped = max(0.0, min(float(pct), 100.0))
    TaskInfo.set(
        phase="process",
        engine="Seedr+CloudConvert",
        filename=filename or TaskInfo.filename or Messages.download_name,
        percentage=pct_clamped,
        speed=detail,
        eta="-",
    )
    text = seedr_status_view(
        kind, stage, pct, detail, filename,
        task_msg_prefix=Messages.task_msg,
        engine_mode_label=cc_mode_label(BOT.Options.cc_engine_mode),
        quality_label=quality_label(BOT.Options.cc_quality_profile),
        sys_info=sysINFO(),
    )
    try:
        await MSG.status_msg.edit_text(
            text=text,
            reply_markup=keyboard(),
            disable_web_page_preview=True,
        )
    except Exception:
        pass


async def Seedr_CC_Convert_Handler(magnet: str) -> None:
    if not _seedr_ready():
        await cancelTask("Seedr credentials are missing in your Colab launcher.")
        return
    if not BOT.Options.cc_api_keys:
        await cancelTask("CloudConvert API key is missing in your Colab launcher.")
        return

    if ospath.exists(Paths.temp_cc_path):
        shutil.rmtree(Paths.temp_cc_path)
    makedirs(Paths.temp_cc_path)

    folder_id = None
    seedr_user = seedr_pwd = ""
    try:
        await _seedr_status("Seedr + CloudConvert Convert", "Seedr", 0.0, "Preparing Seedr job")

        async def _seedr_cb(stage: str, pct: float, detail: str) -> None:
            await _seedr_status("Seedr + CloudConvert Convert", f"Seedr/{stage}", pct * 0.35, detail)

        files, folder_id, seedr_user, seedr_pwd = await fetch_urls_via_seedr(magnet, progress_cb=_seedr_cb)
        videos = _seedr_video_files(files)
        if not videos:
            raise SeedrError("Seedr completed, but no video file was found in the torrent.")

        total = len(videos)
        for idx, video in enumerate(videos):
            name = video["name"]
            chunk_start = 35.0 + ((idx / total) * 50.0)
            chunk_end = 35.0 + (((idx + 1) / total) * 50.0)

            async def _process_cb(pct: float, detail: str, filename: str = name) -> None:
                overall = chunk_start + ((chunk_end - chunk_start) * max(0.0, min(pct, 100.0)) / 100.0)
                await _seedr_status("Seedr + CloudConvert Convert", "CloudConvert", overall, detail, filename)

            async def _download_cb(pct: float, detail: str, filename: str = name) -> None:
                overall = 85.0 + ((idx + (max(0.0, min(pct, 100.0)) / 100.0)) / total * 15.0)
                await _seedr_status("Seedr + CloudConvert Convert", "Download", overall, detail, filename)

            await _seedr_status("Seedr + CloudConvert Convert", "Queue", chunk_start, "Submitting CloudConvert job", name)
            await convert_remote_url(
                ",".join(BOT.Options.cc_api_keys),
                video["url"],
                name,
                Paths.temp_cc_path,
                output_ext=BOT.Options.video_out,
                scale_height=0,
                cc_mode=BOT.Options.cc_engine_mode,
                quality_profile=BOT.Options.cc_quality_profile,
                process_cb=_process_cb,
                download_cb=_download_cb,
            )

        await _seedr_status("Seedr + CloudConvert Convert", "Upload", 100.0, "Uploading to Telegram")
        await Leech(Paths.temp_cc_path, True, convert_videos=False)
    except Exception as exc:
        await cancelTask(f"Seedr+CC convert failed\n\n{exc}")
    finally:
        if folder_id and seedr_user and seedr_pwd:
            await _del_folder(seedr_user, seedr_pwd, folder_id)


async def Seedr_CC_Hardsub_Handler(magnet: str, resolution: str | None = None, encode_speed: str | None = None, style_key: str = "a") -> None:
    if not _seedr_ready():
        await cancelTask("Seedr credentials are missing in your Colab launcher.")
        return
    if not BOT.Options.cc_api_keys:
        await cancelTask("CloudConvert API key is missing in your Colab launcher.")
        return

    if ospath.exists(Paths.temp_cc_path):
        shutil.rmtree(Paths.temp_cc_path)
    makedirs(Paths.temp_cc_path)

    subtitle_dir = ospath.join(Paths.WORK_PATH, "seedr_subtitles")
    if ospath.exists(subtitle_dir):
        shutil.rmtree(subtitle_dir)
    makedirs(subtitle_dir)

    folder_id = None
    seedr_user = seedr_pwd = ""
    try:
        await _seedr_status("Seedr + CloudConvert Hardsub", "Seedr", 0.0, "Preparing Seedr job")

        async def _seedr_cb(stage: str, pct: float, detail: str) -> None:
            await _seedr_status("Seedr + CloudConvert Hardsub", f"Seedr/{stage}", pct * 0.30, detail)

        files, folder_id, seedr_user, seedr_pwd = await fetch_urls_via_seedr(magnet, progress_cb=_seedr_cb)
        videos = _seedr_video_files(files)
        if not videos:
            raise SeedrError("Seedr completed, but no video file was found in the torrent.")

        total = len(videos)
        for idx, video in enumerate(videos):
            name = video["name"]
            video_url = video["url"]
            stem = ospath.splitext(ospath.basename(name))[0]
            base_start = 30.0 + ((idx / total) * 55.0)
            base_end = 30.0 + (((idx + 1) / total) * 55.0)

            await _seedr_status("Seedr + CloudConvert Hardsub", "Probe", base_start, "Inspecting subtitle streams", name)
            probe = await probe_remote_video(video_url)
            sub_stream = pick_french_text_subtitle(probe)
            if not sub_stream:
                raise RuntimeError(f"No French text subtitle stream found in {name}")

            await _seedr_status("Seedr + CloudConvert Hardsub", "Extract", base_start + 6.0, "Extracting French subtitles", name)
            subtitle_path = await extract_subtitle_from_url(video_url, sub_stream, subtitle_dir, stem)

            async def _process_cb(pct: float, detail: str, filename: str = name) -> None:
                overall = (base_start + 10.0) + ((base_end - (base_start + 10.0)) * max(0.0, min(pct, 100.0)) / 100.0)
                await _seedr_status("Seedr + CloudConvert Hardsub", "CloudConvert", overall, detail, filename)

            async def _download_cb(pct: float, detail: str, filename: str = name) -> None:
                overall = 85.0 + ((idx + (max(0.0, min(pct, 100.0)) / 100.0)) / total * 15.0)
                await _seedr_status("Seedr + CloudConvert Hardsub", "Download", overall, detail, filename)

            await _seedr_status("Seedr + CloudConvert Hardsub", "Queue", base_start + 10.0, "Submitting CloudConvert hardsub job", name)
            await hardsub_remote_url(
                ",".join(BOT.Options.cc_api_keys),
                video_url,
                name,
                subtitle_path,
                Paths.temp_cc_path,
                resolution=resolution,
                cc_mode=BOT.Options.cc_engine_mode,
                quality_profile=BOT.Options.cc_quality_profile,
                encode_speed=encode_speed,
                style_key=style_key,
                process_cb=_process_cb,
                download_cb=_download_cb,
            )

        await _seedr_status("Seedr + CloudConvert Hardsub", "Upload", 100.0, "Uploading to Telegram")
        await _burn_prefix_suffix_in_dir(Paths.temp_cc_path, None, "Seedr + CloudConvert Hardsub")
        await Leech(Paths.temp_cc_path, True, convert_videos=False)
    except Exception as exc:
        await cancelTask(f"Seedr+CC hardsub failed\n\n{exc}")
    finally:
        if folder_id and seedr_user and seedr_pwd:
            await _del_folder(seedr_user, seedr_pwd, folder_id)


async def Seedr_FC_Hardsub_Handler(magnet: str, status_msg, resize: tuple[int, int] | None = None, style_key: str = "a") -> None:
    """
    Équivalent de Seedr_CC_Hardsub_Handler mais via FreeConvert. Conçu pour
    tourner en PARALLÈLE avec d'autres jobs FC hardsub (jusqu'à
    FC_HARDSUB_CONCURRENCY à la fois, semaphore partagé — voir shared.py) :
    dossier de travail et message de statut dédiés à ce job.
    """
    if not _seedr_ready():
        await _finish_status(status_msg, "❌ Seedr credentials are missing in your Colab launcher.")
        return
    if not BOT.Options.fc_api_keys:
        await _finish_status(status_msg, "❌ FreeConvert API key is missing in your Colab launcher.")
        return

    job_id = uuid.uuid4().hex[:8]
    job_dir = f"{Paths.temp_cc_path}_{job_id}"
    subtitle_dir = ospath.join(Paths.WORK_PATH, f"seedr_subtitles_{job_id}")
    makedirs(job_dir, exist_ok=True)
    makedirs(subtitle_dir, exist_ok=True)

    await _fc_job_status(status_msg, "Seedr + FreeConvert Hardsub", "Queue", 0.0, "En attente d'un slot disponible...")

    async with _fc_hardsub_semaphore:
        folder_id = None
        seedr_user = seedr_pwd = ""
        try:
            await _fc_job_status(status_msg, "Seedr + FreeConvert Hardsub", "Seedr", 0.0, "Preparing Seedr job")

            async def _seedr_cb(stage: str, pct: float, detail: str) -> None:
                await _fc_job_status(status_msg, "Seedr + FreeConvert Hardsub", f"Seedr/{stage}", pct * 0.30, detail)

            files, folder_id, seedr_user, seedr_pwd = await fetch_urls_via_seedr(magnet, progress_cb=_seedr_cb)
            videos = _seedr_video_files(files)
            if not videos:
                raise SeedrError("Seedr completed, but no video file was found in the torrent.")

            total = len(videos)
            for idx, video in enumerate(videos):
                name = video["name"]
                video_url = video["url"]
                stem = ospath.splitext(ospath.basename(name))[0]
                base_start = 30.0 + ((idx / total) * 55.0)
                base_end = 30.0 + (((idx + 1) / total) * 55.0)

                await _fc_job_status(status_msg, "Seedr + FreeConvert Hardsub", "Probe", base_start, "Inspecting subtitle streams", name)
                probe = await probe_remote_video(video_url)
                sub_stream = pick_french_text_subtitle(probe)
                if not sub_stream:
                    raise RuntimeError(f"No French text subtitle stream found in {name}")

                await _fc_job_status(status_msg, "Seedr + FreeConvert Hardsub", "Extract", base_start + 6.0, "Extracting French subtitles", name)
                subtitle_path = await extract_subtitle_from_url(video_url, sub_stream, subtitle_dir, stem)

                async def _process_cb(pct: float, detail: str, filename: str = name) -> None:
                    overall = (base_start + 10.0) + ((base_end - (base_start + 10.0)) * max(0.0, min(pct, 100.0)) / 100.0)
                    await _fc_job_status(status_msg, "Seedr + FreeConvert Hardsub", "FreeConvert", overall, detail, filename)

                async def _download_cb(pct: float, detail: str, filename: str = name) -> None:
                    overall = 85.0 + ((idx + (max(0.0, min(pct, 100.0)) / 100.0)) / total * 15.0)
                    await _fc_job_status(status_msg, "Seedr + FreeConvert Hardsub", "Download", overall, detail, filename)

                await _fc_job_status(status_msg, "Seedr + FreeConvert Hardsub", "Queue", base_start + 10.0, "Submitting FreeConvert hardsub job", name)

                _quality_override = resolution_label(resize[1]) if resize else None
                _renamed_name = build_final_name(name, override_quality=_quality_override, output_ext="mp4")

                async def _url_cb(url: str, filename: str = _renamed_name) -> None:
                    try:
                        await colab_bot.send_message(
                            chat_id=status_msg.chat.id,
                            text=(
                                "🔗 <b>Lien direct disponible</b>\n\n"
                                f"<code>{filename}</code>\n\n"
                                f"{url}\n\n"
                                "<i>Le bot va maintenant le télécharger et l'uploader. "
                                "Si ça plante, tu as déjà ce lien pour le récupérer toi-même.</i>"
                            ),
                            disable_web_page_preview=True,
                        )
                    except Exception:
                        pass

                await fc_hardsub_remote_url(
                    ",".join(BOT.Options.fc_api_keys),
                    video_url,
                    name,
                    subtitle_path,
                    job_dir,
                    quality_profile=BOT.Options.cc_quality_profile,
                    resize=resize,
                    style_key=style_key,
                    process_cb=_process_cb,
                    download_cb=_download_cb,
                    url_cb=_url_cb,
                )

            await _fc_job_status(status_msg, "Seedr + FreeConvert Hardsub", "Upload", 100.0, "Uploading to Telegram")
            await _burn_prefix_suffix_in_dir(job_dir, status_msg, "Seedr + FreeConvert Hardsub")
            await Leech(job_dir, True, convert_videos=False, status_msg=status_msg)
            try:
                await status_msg.delete()
            except Exception:
                pass
        except Exception as exc:
            await _finish_status(status_msg, f"❌ <b>Seedr+FC hardsub failed</b>\n\n<code>{exc}</code>")
        finally:
            if folder_id and seedr_user and seedr_pwd:
                await _del_folder(seedr_user, seedr_pwd, folder_id)
            for d in (job_dir, subtitle_dir):
                if ospath.exists(d):
                    shutil.rmtree(d, ignore_errors=True)
