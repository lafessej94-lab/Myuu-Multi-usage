"""
colab_leecher/utility/handler/local_video_tasks.py

Les 14 handlers du menu vidéo local (FFmpeg sur le CPU de Colab, pas
d'appel API cloud) : Convert, Merge, Thumb, Screenshots, Trim, Compress,
Mux/Burn Subs, Manual Shot, Split, Sample, Rename, To Audio, Mute,
Metadata.

Extrait de handler.py — voir __init__.py de ce package pour la liste
complète des réexports.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from os import makedirs, path as ospath

from pyrogram.types import InputMediaPhoto

from colab_leecher import colab_bot
from colab_leecher.engines.local_convert import convert_resolution, merge_audio_video
from colab_leecher.engines.local_video_tools import (
    burn_subtitles,
    compress_video,
    extract_audio,
    extract_random_thumbnail,
    mute_video,
    mux_subtitles,
    probe_media_info_text,
    sample_clip,
    screenshot_at,
    split_video,
    take_screenshots,
    trim_video,
)
from colab_leecher.house_style import apply_house_style
from colab_leecher.smart_rename import build_final_name
from colab_leecher.uploader.telegram import upload_file
from colab_leecher.utility.handler.shared import _fc_job_status, _finish_status
from colab_leecher.utility.variables import ActiveJobs, BOT, Paths

LOCAL_CONVERT_CONCURRENCY = 5
_local_convert_semaphore = asyncio.Semaphore(LOCAL_CONVERT_CONCURRENCY)


async def Local_Video_Convert_Handler(source_message, height: int, status_msg) -> None:
    job_id = uuid.uuid4().hex[:8]
    job_dir = f"{Paths.temp_cc_path}_local_{job_id}"
    makedirs(job_dir, exist_ok=True)

    await _fc_job_status(status_msg, "Video Converter", "Queue", 0.0, "En attente d'un slot disponible...")

    async with _local_convert_semaphore:
        try:
            await _fc_job_status(status_msg, "Video Converter", "Download", 0.0, "Téléchargement depuis Telegram...")
            input_path = await source_message.download(file_name=ospath.join(job_dir, "source_input"))

            async def _progress_cb(pct: float, detail: str) -> None:
                overall = 10.0 + (max(0.0, min(pct, 100.0)) * 0.80)
                await _fc_job_status(status_msg, "Video Converter", "Encodage", overall, detail)

            base = ospath.splitext(ospath.basename(input_path))[0]
            output_path = ospath.join(job_dir, f"{base}.{height}p.mp4")

            await _fc_job_status(status_msg, "Video Converter", "Encodage", 10.0, f"ffmpeg -> {height}p")
            await convert_resolution(input_path, output_path, height, progress_cb=_progress_cb)

            await _fc_job_status(status_msg, "Video Converter", "Upload", 95.0, "Uploading to Telegram")
            await upload_file(output_path, ospath.basename(output_path), is_last=True, status_msg=status_msg)
            try:
                await status_msg.delete()
            except Exception:
                pass
        except Exception as exc:
            await _finish_status(status_msg, f"❌ <b>Video Converter failed</b>\n\n<code>{exc}</code>")
        finally:
            if ospath.exists(job_dir):
                shutil.rmtree(job_dir, ignore_errors=True)


async def Local_Merge_Handler(video_message, audio_path: str, status_msg) -> None:
    job_id = uuid.uuid4().hex[:8]
    job_dir = f"{Paths.temp_cc_path}_merge_{job_id}"
    makedirs(job_dir, exist_ok=True)

    await _fc_job_status(status_msg, "Merge Audio+Vidéo", "Queue", 0.0, "En attente d'un slot disponible...")

    async with _local_convert_semaphore:
        try:
            await _fc_job_status(status_msg, "Merge Audio+Vidéo", "Download", 0.0, "Téléchargement de la vidéo...")
            video_path = await video_message.download(file_name=ospath.join(job_dir, "source_video"))

            async def _progress_cb(pct: float, detail: str) -> None:
                overall = 20.0 + (max(0.0, min(pct, 100.0)) * 0.70)
                await _fc_job_status(status_msg, "Merge Audio+Vidéo", "Fusion", overall, detail)

            base = ospath.splitext(ospath.basename(video_path))[0]
            output_path = ospath.join(job_dir, f"{base}.merged.mp4")

            await _fc_job_status(status_msg, "Merge Audio+Vidéo", "Fusion", 15.0, "ffmpeg -> fusion audio/vidéo")
            await merge_audio_video(video_path, audio_path, output_path, progress_cb=_progress_cb)

            await _fc_job_status(status_msg, "Merge Audio+Vidéo", "Upload", 95.0, "Uploading to Telegram")
            await upload_file(output_path, ospath.basename(output_path), is_last=True, status_msg=status_msg)
            try:
                await status_msg.delete()
            except Exception:
                pass
        except Exception as exc:
            await _finish_status(status_msg, f"❌ <b>Merge failed</b>\n\n<code>{exc}</code>")
        finally:
            if ospath.exists(audio_path):
                try:
                    os.remove(audio_path)
                except Exception:
                    pass
            if ospath.exists(job_dir):
                shutil.rmtree(job_dir, ignore_errors=True)


async def Local_Thumb_Handler(source_message, status_msg) -> None:
    job_id = uuid.uuid4().hex[:8]
    job_dir = f"{Paths.temp_cc_path}_thumb_{job_id}"
    makedirs(job_dir, exist_ok=True)

    await _fc_job_status(status_msg, "Thumb", "Queue", 0.0, "En attente d'un slot disponible...")

    async with _local_convert_semaphore:
        try:
            await _fc_job_status(status_msg, "Thumb", "Download", 0.0, "Téléchargement depuis Telegram...")
            input_path = await source_message.download(file_name=ospath.join(job_dir, "source_input"))

            await _fc_job_status(status_msg, "Thumb", "Extraction", 60.0, "ffmpeg -> frame aléatoire")
            base = ospath.splitext(ospath.basename(input_path))[0]
            thumb_path = ospath.join(job_dir, f"{base}.thumb.jpg")
            await extract_random_thumbnail(input_path, thumb_path)

            await _fc_job_status(status_msg, "Thumb", "Upload", 95.0, "Uploading to Telegram")
            await colab_bot.send_photo(chat_id=status_msg.chat.id, photo=thumb_path, caption=f"🖼 {ospath.basename(input_path)}")
            try:
                await status_msg.delete()
            except Exception:
                pass
        except Exception as exc:
            await _finish_status(status_msg, f"❌ <b>Thumb failed</b>\n\n<code>{exc}</code>")
        finally:
            if ospath.exists(job_dir):
                shutil.rmtree(job_dir, ignore_errors=True)


async def Local_Screenshots_Handler(source_message, status_msg, count: int = 5) -> None:
    job_id = uuid.uuid4().hex[:8]
    job_dir = f"{Paths.temp_cc_path}_shots_{job_id}"
    makedirs(job_dir, exist_ok=True)

    await _fc_job_status(status_msg, "Screenshots", "Queue", 0.0, "En attente d'un slot disponible...")

    async with _local_convert_semaphore:
        try:
            await _fc_job_status(status_msg, "Screenshots", "Download", 0.0, "Téléchargement depuis Telegram...")
            input_path = await source_message.download(file_name=ospath.join(job_dir, "source_input"))

            await _fc_job_status(status_msg, "Screenshots", "Extraction", 50.0, f"ffmpeg -> {count} frames")
            shots = await take_screenshots(input_path, job_dir, count=count)

            await _fc_job_status(status_msg, "Screenshots", "Upload", 90.0, "Uploading to Telegram")
            media = [InputMediaPhoto(p) for p in shots]
            await colab_bot.send_media_group(chat_id=status_msg.chat.id, media=media)
            try:
                await status_msg.delete()
            except Exception:
                pass
        except Exception as exc:
            await _finish_status(status_msg, f"❌ <b>Screenshots failed</b>\n\n<code>{exc}</code>")
        finally:
            if ospath.exists(job_dir):
                shutil.rmtree(job_dir, ignore_errors=True)


async def Local_Trim_Handler(source_message, start: str, end: str, status_msg) -> None:
    job_id = uuid.uuid4().hex[:8]
    job_dir = f"{Paths.temp_cc_path}_trim_{job_id}"
    makedirs(job_dir, exist_ok=True)

    await _fc_job_status(status_msg, "Trim", "Queue", 0.0, "En attente d'un slot disponible...")

    async with _local_convert_semaphore:
        try:
            await _fc_job_status(status_msg, "Trim", "Download", 0.0, "Téléchargement depuis Telegram...")
            input_path = await source_message.download(file_name=ospath.join(job_dir, "source_input"))

            async def _progress_cb(pct: float, detail: str) -> None:
                overall = 10.0 + (max(0.0, min(pct, 100.0)) * 0.80)
                await _fc_job_status(status_msg, "Trim", "Découpe", overall, detail)

            base = ospath.splitext(ospath.basename(input_path))[0]
            ext = ospath.splitext(ospath.basename(input_path))[1] or ".mp4"
            output_path = ospath.join(job_dir, f"{base}.trim{ext}")

            await _fc_job_status(status_msg, "Trim", "Découpe", 10.0, f"ffmpeg -> {start} → {end}")
            await trim_video(input_path, output_path, start, end, progress_cb=_progress_cb)

            await _fc_job_status(status_msg, "Trim", "Upload", 95.0, "Uploading to Telegram")
            await upload_file(output_path, ospath.basename(output_path), is_last=True, status_msg=status_msg)
            try:
                await status_msg.delete()
            except Exception:
                pass
        except Exception as exc:
            await _finish_status(status_msg, f"❌ <b>Trim failed</b>\n\n<code>{exc}</code>")
        finally:
            if ospath.exists(job_dir):
                shutil.rmtree(job_dir, ignore_errors=True)


async def Local_Compress_Handler(source_message, status_msg, crf: int = 28) -> None:
    job_id = uuid.uuid4().hex[:8]
    job_dir = f"{Paths.temp_cc_path}_compress_{job_id}"
    makedirs(job_dir, exist_ok=True)

    await _fc_job_status(status_msg, "Compress", "Queue", 0.0, "En attente d'un slot disponible...")

    async with _local_convert_semaphore:
        try:
            await _fc_job_status(status_msg, "Compress", "Download", 0.0, "Téléchargement depuis Telegram...")
            input_path = await source_message.download(file_name=ospath.join(job_dir, "source_input"))

            async def _progress_cb(pct: float, detail: str) -> None:
                overall = 10.0 + (max(0.0, min(pct, 100.0)) * 0.80)
                await _fc_job_status(status_msg, "Compress", "Compression", overall, detail)

            base = ospath.splitext(ospath.basename(input_path))[0]
            output_path = ospath.join(job_dir, f"{base}.compressed.mp4")

            await _fc_job_status(status_msg, "Compress", "Compression", 10.0, f"ffmpeg -> crf {crf}")
            await compress_video(input_path, output_path, crf=crf, progress_cb=_progress_cb)

            await _fc_job_status(status_msg, "Compress", "Upload", 95.0, "Uploading to Telegram")
            await upload_file(output_path, ospath.basename(output_path), is_last=True, status_msg=status_msg)
            try:
                await status_msg.delete()
            except Exception:
                pass
        except Exception as exc:
            await _finish_status(status_msg, f"❌ <b>Compress failed</b>\n\n<code>{exc}</code>")
        finally:
            if ospath.exists(job_dir):
                shutil.rmtree(job_dir, ignore_errors=True)


async def Local_Subs_Handler(video_message, sub_path: str, status_msg, burn: bool) -> None:
    job_id = uuid.uuid4().hex[:8]
    job_dir = f"{Paths.temp_cc_path}_subs_{job_id}"
    makedirs(job_dir, exist_ok=True)
    kind = "Burn Subs" if burn else "Mux Subs"
    ActiveJobs.register(job_id, asyncio.current_task())

    real_name = (
        getattr(video_message.video, "file_name", None)
        or getattr(video_message.document, "file_name", None)
        or "video.mp4"
    )

    renamed_output_ext = "mp4" if burn else "mkv"
    if BOT.Options.custom_name:
        has_ext = bool(ospath.splitext(BOT.Options.custom_name)[1])
        upload_name = (
            BOT.Options.custom_name if has_ext
            else f"{BOT.Options.custom_name}.{renamed_output_ext}"
        )
    else:
        built_name = build_final_name(real_name, output_ext=renamed_output_ext)
        real_base = ospath.splitext(real_name)[0]
        upload_name = built_name if built_name != real_name else f"{real_base}.{renamed_output_ext}"
    metadata_title = ospath.splitext(upload_name)[0]

    await _fc_job_status(status_msg, kind, "Queue", 0.0, "En attente d'un slot disponible...", job_id=job_id)

    async with _local_convert_semaphore:
        try:
            await _fc_job_status(status_msg, kind, "Download", 0.0, "Téléchargement de la vidéo...", job_id=job_id)
            video_path = await video_message.download(file_name=ospath.join(job_dir, "source_video"))

            base = ospath.splitext(ospath.basename(video_path))[0]

            await _fc_job_status(status_msg, kind, "Style", 8.0, "Application du house style", job_id=job_id)
            sub_path = await apply_house_style(sub_path, job_dir)

            if burn:
                async def _progress_cb(pct: float, detail: str) -> None:
                    overall = 15.0 + (max(0.0, min(pct, 100.0)) * 0.75)
                    await _fc_job_status(status_msg, kind, "Hardsub", overall, detail, job_id=job_id)

                output_path = ospath.join(job_dir, f"{base}.hardsub.mp4")
                await _fc_job_status(status_msg, kind, "Hardsub", 10.0, "ffmpeg -> incrustation", job_id=job_id)
                await burn_subtitles(video_path, sub_path, output_path, progress_cb=_progress_cb, title=metadata_title)
            else:
                output_path = ospath.join(job_dir, f"{base}.muxed.mkv")
                await _fc_job_status(status_msg, kind, "Mux", 40.0, "ffmpeg -> ajout de la piste", job_id=job_id)
                await mux_subtitles(video_path, sub_path, output_path, title=metadata_title)

            await _fc_job_status(status_msg, kind, "Upload", 95.0, "Uploading to Telegram", job_id=job_id)
            await upload_file(output_path, upload_name, is_last=True, status_msg=status_msg)
            try:
                await status_msg.delete()
            except Exception:
                pass
        except asyncio.CancelledError:
            await _finish_status(status_msg, f"⛔ <b>{kind} cancelled</b>", reply_markup=None)
        except Exception as exc:
            await _finish_status(status_msg, f"❌ <b>{kind} failed</b>\n\n<code>{exc}</code>")
        finally:
            ActiveJobs.unregister(job_id)
            if ospath.exists(sub_path):
                try:
                    os.remove(sub_path)
                except Exception:
                    pass
            if ospath.exists(job_dir):
                shutil.rmtree(job_dir, ignore_errors=True)


async def Local_ManualShot_Handler(source_message, timestamp: str, status_msg) -> None:
    job_id = uuid.uuid4().hex[:8]
    job_dir = f"{Paths.temp_cc_path}_shot_{job_id}"
    makedirs(job_dir, exist_ok=True)

    await _fc_job_status(status_msg, "Manual Shot", "Queue", 0.0, "En attente d'un slot disponible...")

    async with _local_convert_semaphore:
        try:
            await _fc_job_status(status_msg, "Manual Shot", "Download", 0.0, "Téléchargement depuis Telegram...")
            input_path = await source_message.download(file_name=ospath.join(job_dir, "source_input"))

            await _fc_job_status(status_msg, "Manual Shot", "Extraction", 60.0, f"ffmpeg -> {timestamp}")
            base = ospath.splitext(ospath.basename(input_path))[0]
            shot_path = ospath.join(job_dir, f"{base}.shot.jpg")
            await screenshot_at(input_path, shot_path, timestamp)

            await _fc_job_status(status_msg, "Manual Shot", "Upload", 95.0, "Uploading to Telegram")
            await colab_bot.send_photo(chat_id=status_msg.chat.id, photo=shot_path, caption=f"🖼 {timestamp} — {ospath.basename(input_path)}")
            try:
                await status_msg.delete()
            except Exception:
                pass
        except Exception as exc:
            await _finish_status(status_msg, f"❌ <b>Manual Shot failed</b>\n\n<code>{exc}</code>")
        finally:
            if ospath.exists(job_dir):
                shutil.rmtree(job_dir, ignore_errors=True)


async def Local_Split_Handler(source_message, parts: int, status_msg) -> None:
    job_id = uuid.uuid4().hex[:8]
    job_dir = f"{Paths.temp_cc_path}_split_{job_id}"
    makedirs(job_dir, exist_ok=True)

    await _fc_job_status(status_msg, "Split", "Queue", 0.0, "En attente d'un slot disponible...")

    async with _local_convert_semaphore:
        try:
            await _fc_job_status(status_msg, "Split", "Download", 0.0, "Téléchargement depuis Telegram...")
            input_path = await source_message.download(file_name=ospath.join(job_dir, "source_input"))

            await _fc_job_status(status_msg, "Split", "Découpe", 40.0, f"ffmpeg -> {parts} parties")
            files = await split_video(input_path, ospath.join(job_dir, "parts"), parts=parts)

            for i, fp in enumerate(files, start=1):
                await _fc_job_status(status_msg, "Split", "Upload", 60.0 + (i / len(files)) * 35.0, f"Partie {i}/{len(files)}")
                await upload_file(fp, ospath.basename(fp), is_last=(i == len(files)), status_msg=status_msg)
            try:
                await status_msg.delete()
            except Exception:
                pass
        except Exception as exc:
            await _finish_status(status_msg, f"❌ <b>Split failed</b>\n\n<code>{exc}</code>")
        finally:
            if ospath.exists(job_dir):
                shutil.rmtree(job_dir, ignore_errors=True)


async def Local_Sample_Handler(source_message, duration: int, status_msg) -> None:
    job_id = uuid.uuid4().hex[:8]
    job_dir = f"{Paths.temp_cc_path}_sample_{job_id}"
    makedirs(job_dir, exist_ok=True)

    await _fc_job_status(status_msg, "Sample", "Queue", 0.0, "En attente d'un slot disponible...")

    async with _local_convert_semaphore:
        try:
            await _fc_job_status(status_msg, "Sample", "Download", 0.0, "Téléchargement depuis Telegram...")
            input_path = await source_message.download(file_name=ospath.join(job_dir, "source_input"))

            base = ospath.splitext(ospath.basename(input_path))[0]
            ext = ospath.splitext(ospath.basename(input_path))[1] or ".mp4"
            output_path = ospath.join(job_dir, f"{base}.sample{ext}")

            await _fc_job_status(status_msg, "Sample", "Extraction", 50.0, f"ffmpeg -> {duration}s")
            await sample_clip(input_path, output_path, duration=duration)

            await _fc_job_status(status_msg, "Sample", "Upload", 90.0, "Uploading to Telegram")
            await upload_file(output_path, ospath.basename(output_path), is_last=True, status_msg=status_msg)
            try:
                await status_msg.delete()
            except Exception:
                pass
        except Exception as exc:
            await _finish_status(status_msg, f"❌ <b>Sample failed</b>\n\n<code>{exc}</code>")
        finally:
            if ospath.exists(job_dir):
                shutil.rmtree(job_dir, ignore_errors=True)


async def Local_Rename_Handler(source_message, new_name: str, status_msg) -> None:
    job_id = uuid.uuid4().hex[:8]
    job_dir = f"{Paths.temp_cc_path}_rename_{job_id}"
    makedirs(job_dir, exist_ok=True)

    await _fc_job_status(status_msg, "Rename", "Queue", 0.0, "En attente d'un slot disponible...")

    async with _local_convert_semaphore:
        try:
            await _fc_job_status(status_msg, "Rename", "Download", 0.0, "Téléchargement depuis Telegram...")
            input_path = await source_message.download(file_name=ospath.join(job_dir, "source_input"))

            await _fc_job_status(status_msg, "Rename", "Upload", 60.0, f"-> {new_name}")
            await upload_file(input_path, new_name, is_last=True, status_msg=status_msg)
            try:
                await status_msg.delete()
            except Exception:
                pass
        except Exception as exc:
            await _finish_status(status_msg, f"❌ <b>Rename failed</b>\n\n<code>{exc}</code>")
        finally:
            if ospath.exists(job_dir):
                shutil.rmtree(job_dir, ignore_errors=True)


async def Local_ToAudio_Handler(source_message, status_msg) -> None:
    job_id = uuid.uuid4().hex[:8]
    job_dir = f"{Paths.temp_cc_path}_toaudio_{job_id}"
    makedirs(job_dir, exist_ok=True)

    await _fc_job_status(status_msg, "To Audio", "Queue", 0.0, "En attente d'un slot disponible...")

    async with _local_convert_semaphore:
        try:
            await _fc_job_status(status_msg, "To Audio", "Download", 0.0, "Téléchargement depuis Telegram...")
            input_path = await source_message.download(file_name=ospath.join(job_dir, "source_input"))

            base = ospath.splitext(ospath.basename(input_path))[0]
            output_path = ospath.join(job_dir, f"{base}.mp3")

            await _fc_job_status(status_msg, "To Audio", "Extraction", 50.0, "ffmpeg -> mp3")
            await extract_audio(input_path, output_path)

            await _fc_job_status(status_msg, "To Audio", "Upload", 90.0, "Uploading to Telegram")
            await upload_file(output_path, ospath.basename(output_path), is_last=True, status_msg=status_msg)
            try:
                await status_msg.delete()
            except Exception:
                pass
        except Exception as exc:
            await _finish_status(status_msg, f"❌ <b>To Audio failed</b>\n\n<code>{exc}</code>")
        finally:
            if ospath.exists(job_dir):
                shutil.rmtree(job_dir, ignore_errors=True)


async def Local_Mute_Handler(source_message, status_msg) -> None:
    job_id = uuid.uuid4().hex[:8]
    job_dir = f"{Paths.temp_cc_path}_mute_{job_id}"
    makedirs(job_dir, exist_ok=True)

    await _fc_job_status(status_msg, "Mute", "Queue", 0.0, "En attente d'un slot disponible...")

    async with _local_convert_semaphore:
        try:
            await _fc_job_status(status_msg, "Mute", "Download", 0.0, "Téléchargement depuis Telegram...")
            input_path = await source_message.download(file_name=ospath.join(job_dir, "source_input"))

            base = ospath.splitext(ospath.basename(input_path))[0]
            ext = ospath.splitext(ospath.basename(input_path))[1] or ".mp4"
            output_path = ospath.join(job_dir, f"{base}.mute{ext}")

            await _fc_job_status(status_msg, "Mute", "Traitement", 50.0, "ffmpeg -> retrait audio")
            await mute_video(input_path, output_path)

            await _fc_job_status(status_msg, "Mute", "Upload", 90.0, "Uploading to Telegram")
            await upload_file(output_path, ospath.basename(output_path), is_last=True, status_msg=status_msg)
            try:
                await status_msg.delete()
            except Exception:
                pass
        except Exception as exc:
            await _finish_status(status_msg, f"❌ <b>Mute failed</b>\n\n<code>{exc}</code>")
        finally:
            if ospath.exists(job_dir):
                shutil.rmtree(job_dir, ignore_errors=True)


async def Local_Metadata_Handler(source_message, status_msg) -> None:
    job_id = uuid.uuid4().hex[:8]
    job_dir = f"{Paths.temp_cc_path}_meta_{job_id}"
    makedirs(job_dir, exist_ok=True)
    try:
        await status_msg.edit_text("⏳ <i>Téléchargement depuis Telegram...</i>")
        input_path = await source_message.download(file_name=ospath.join(job_dir, "source_input"))
        text = await probe_media_info_text(input_path)
        await status_msg.edit_text(text)
    except Exception as exc:
        try:
            await status_msg.edit_text(f"❌ <b>Metadata failed</b>\n\n<code>{exc}</code>")
        except Exception:
            pass
    finally:
        if ospath.exists(job_dir):
            shutil.rmtree(job_dir, ignore_errors=True)
