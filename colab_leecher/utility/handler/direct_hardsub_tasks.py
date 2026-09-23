"""
colab_leecher/utility/handler/direct_hardsub_tasks.py

Hardsub FC/CC sur un lien direct (Seedr HTTP, lien HTTP classique) avec un
sous-titre fourni manuellement par l'utilisateur — pas d'extraction
automatique de piste, pas de sonde ffprobe. Les deux tournent en parallèle
via les semaphores partagés définis dans shared.py.

Extrait de handler.py — voir __init__.py de ce package pour la liste
complète des réexports.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from os import makedirs, path as ospath

from colab_leecher import colab_bot
from colab_leecher.engines.cloudconvert import hardsub_remote_url
from colab_leecher.engines.freeconvert import (
    hardsub_remote_url as fc_hardsub_remote_url,
)
from colab_leecher.smart_rename import build_final_name, resolution_label
from colab_leecher.utility.handler.leech import Leech
from colab_leecher.utility.handler.shared import (
    _burn_prefix_suffix_in_dir,
    _cc_hardsub_semaphore,
    _fc_hardsub_semaphore,
    _fc_job_status,
    _finish_status,
)
from colab_leecher.utility.variables import ActiveJobs, BOT, Paths


async def Direct_CC_Hardsub_Handler(video_url: str, name: str, subtitle_path: str, status_msg, resolution: str | None = None, style_key: str = "a") -> None:
    """Équivalent CloudConvert de Direct_FC_Hardsub_Handler. Conçu pour
    tourner en PARALLÈLE avec d'autres jobs CC hardsub (jusqu'à
    CC_HARDSUB_CONCURRENCY à la fois) : dossier de travail et message de
    statut dédiés à ce job."""
    if not BOT.Options.cc_api_keys:
        await _finish_status(status_msg, "❌ CloudConvert API key is missing in your Colab launcher.")
        return

    job_id = uuid.uuid4().hex[:8]
    job_dir = f"{Paths.temp_cc_path}_{job_id}"
    makedirs(job_dir, exist_ok=True)

    await _fc_job_status(status_msg, "CloudConvert Hardsub", "Queue", 0.0, "En attente d'un slot disponible...", name)

    async with _cc_hardsub_semaphore:
        try:
            async def _process_cb(pct: float, detail: str) -> None:
                overall = 10.0 + (max(0.0, min(pct, 100.0)) * 0.75)
                await _fc_job_status(status_msg, "CloudConvert Hardsub", "CloudConvert", overall, detail, name)

            async def _download_cb(pct: float, detail: str) -> None:
                overall = 85.0 + (max(0.0, min(pct, 100.0)) * 0.15)
                await _fc_job_status(status_msg, "CloudConvert Hardsub", "Download", overall, detail, name)

            await _fc_job_status(status_msg, "CloudConvert Hardsub", "Queue", 5.0, "Submitting CloudConvert hardsub job", name)

            _res = (resolution or "").strip().lower()
            _quality_override = resolution if _res and _res != "original" else None
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

            await hardsub_remote_url(
                ",".join(BOT.Options.cc_api_keys),
                video_url,
                name,
                subtitle_path,
                job_dir,
                cc_mode=BOT.Options.cc_engine_mode,
                quality_profile=BOT.Options.cc_quality_profile,
                resolution=resolution,
                style_key=style_key,
                process_cb=_process_cb,
                download_cb=_download_cb,
                url_cb=_url_cb,
            )

            await _fc_job_status(status_msg, "CloudConvert Hardsub", "Upload", 100.0, "Uploading to Telegram", name)
            await Leech(job_dir, True, convert_videos=False, status_msg=status_msg)
            try:
                await status_msg.delete()
            except Exception:
                pass
        except Exception as exc:
            await _finish_status(status_msg, f"❌ <b>CloudConvert hardsub failed</b>\n\n<code>{exc}</code>")
        finally:
            if ospath.exists(subtitle_path):
                try:
                    os.remove(subtitle_path)
                except Exception:
                    pass
            if ospath.exists(job_dir):
                shutil.rmtree(job_dir, ignore_errors=True)


async def Direct_FC_Hardsub_Handler(video_url: str, name: str, subtitle_path: str, status_msg, resize: tuple[int, int] | None = None, style_key: str = "a") -> None:
    """Hardsub FreeConvert sur un lien direct, sous-titre fourni
    manuellement. Conçu pour tourner en PARALLÈLE (jusqu'à
    FC_HARDSUB_CONCURRENCY à la fois, semaphore partagé avec
    Seedr_FC_Hardsub_Handler)."""
    if not BOT.Options.fc_api_keys:
        await _finish_status(status_msg, "❌ FreeConvert API key is missing in your Colab launcher.")
        return

    job_id = uuid.uuid4().hex[:8]
    job_dir = f"{Paths.temp_cc_path}_{job_id}"
    makedirs(job_dir, exist_ok=True)
    ActiveJobs.register(job_id, asyncio.current_task())

    await _fc_job_status(status_msg, "FreeConvert Hardsub", "Queue", 0.0, "En attente d'un slot disponible...", name, job_id=job_id)

    async with _fc_hardsub_semaphore:
        try:
            async def _process_cb(pct: float, detail: str) -> None:
                overall = 10.0 + (max(0.0, min(pct, 100.0)) * 0.75)
                await _fc_job_status(status_msg, "FreeConvert Hardsub", "FreeConvert", overall, detail, name, job_id=job_id)

            async def _download_cb(pct: float, detail: str) -> None:
                overall = 85.0 + (max(0.0, min(pct, 100.0)) * 0.15)
                await _fc_job_status(status_msg, "FreeConvert Hardsub", "Download", overall, detail, name, job_id=job_id)

            await _fc_job_status(status_msg, "FreeConvert Hardsub", "Queue", 5.0, "Submitting FreeConvert hardsub job", name, job_id=job_id)

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

            await _fc_job_status(status_msg, "FreeConvert Hardsub", "Upload", 100.0, "Uploading to Telegram", name, job_id=job_id)
            await _burn_prefix_suffix_in_dir(job_dir, status_msg, "FreeConvert Hardsub")
            await Leech(job_dir, True, convert_videos=False, status_msg=status_msg)
            try:
                await status_msg.delete()
            except Exception:
                pass
        except asyncio.CancelledError:
            await _finish_status(status_msg, "⛔ <b>FreeConvert hardsub cancelled</b>", reply_markup=None)
        except Exception as exc:
            await _finish_status(status_msg, f"❌ <b>FreeConvert hardsub failed</b>\n\n<code>{exc}</code>")
        finally:
            ActiveJobs.unregister(job_id)
            if ospath.exists(subtitle_path):
                try:
                    os.remove(subtitle_path)
                except Exception:
                    pass
            if ospath.exists(job_dir):
                shutil.rmtree(job_dir, ignore_errors=True)
