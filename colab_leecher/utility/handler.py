import asyncio
import json
import os
import shutil
import logging

log = logging.getLogger(__name__)
import pathlib
import uuid
from asyncio import sleep
from time import time
from colab_leecher import OWNER, SEEDR_PASSWORD, SEEDR_USERNAME, colab_bot
from natsort import natsorted
from datetime import datetime
from os import makedirs, path as ospath
from colab_leecher.cloudconvert import (
    cc_mode_label,
    compress_file,
    convert_file,
    convert_remote_url,
    hardsub_remote_url,
    quality_label,
    resize_file,
    resize_label,
)
from colab_leecher.freeconvert import (
    hardsub_remote_url as fc_hardsub_remote_url,
    quality_label as fc_quality_label,
)
from colab_leecher.local_convert import convert_resolution, merge_audio_video
from colab_leecher.house_style import apply_house_style
from colab_leecher.local_video_tools import (
    burn_subtitles,
    burn_text_overlay,
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
from colab_leecher.downlader.aria2 import aria2_Download
from colab_leecher.seedr import SeedrError, _del_folder, fetch_urls_via_seedr
from colab_leecher.uploader.telegram import upload_file
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from colab_leecher.utility.variables import (
    BOT, MSG, ActiveJobs, BotTimes, Messages, Paths, Transfer, ProcessTracker, TaskInfo,
)
from colab_leecher.utility.converters import archive, extract, videoConverter, sizeChecker
from colab_leecher.utility.helper import (
    fileType, getSize, getTime, keyboard,
    render_task_status, shortFileName, sizeUnit, sysINFO,
)


async def Leech(folder_path: str, remove: bool, convert_videos: bool = True, status_msg=None):
    """
    status_msg optionnel : si fourni (cas des jobs FreeConvert concurrents),
    on édite UNIQUEMENT ce message local, sans jamais toucher au MSG.status_msg
    global — évite que 2 jobs FC en parallèle ne se marchent dessus sur le
    message de statut. Si absent, comportement historique inchangé (pipeline
    leech normal, single-task, utilise le global MSG.status_msg).
    """
    is_global = status_msg is None
    target_msg = status_msg or MSG.status_msg

    files = [str(p) for p in pathlib.Path(folder_path).glob("**/*") if p.is_file()]
    if not files:
        raise RuntimeError(f"No files were produced in {folder_path}.")
    for f in natsorted(files):
        fp = ospath.join(folder_path, f)
        if convert_videos and BOT.Options.convert_video and fileType(fp) == "video":
            await videoConverter(fp)

    Transfer.total_down_size = getSize(folder_path)

    files = natsorted([str(p) for p in pathlib.Path(folder_path).glob("**/*") if p.is_file()])
    upload_queue = []

    for f in files:
        file_path = ospath.join(folder_path, f)
        leech = await sizeChecker(file_path, remove)
        if leech:
            if ospath.exists(file_path) and remove:
                os.remove(file_path)
            for part in natsorted(os.listdir(Paths.temp_zpath)):
                upload_queue.append(("split", ospath.join(Paths.temp_zpath, part)))
        else:
            upload_queue.append(("single", file_path))

    total_uploads    = len(upload_queue)
    if total_uploads == 0:
        raise RuntimeError("Nothing to upload after processing.")
    split_cleaned    = False

    for idx, (kind, file_path) in enumerate(upload_queue):
        is_last = (idx == total_uploads - 1)

        # Update TaskInfo for /status panel
        TaskInfo.set(
            phase="upload", engine="Pyrofork",
            filename=ospath.basename(file_path),
        )

        if kind == "split":
            file_name = ospath.basename(file_path)
            new_path  = shortFileName(file_path)
            os.rename(file_path, new_path)
            BotTimes.current_time = time()
            Messages.status_head  = (
                f"📤 <b>UPLOADING</b>  <i>{idx+1} / {total_uploads}</i>\n\n"
                f"<code>{file_name}</code>\n"
            )
            try:
                edited = await target_msg.edit_text(
                    text=Messages.task_msg + Messages.status_head
                    + "\n⏳ <i>Starting...</i>" + sysINFO(),
                    reply_markup=keyboard(),
                )
                target_msg = edited
                if is_global:
                    MSG.status_msg = edited
            except Exception: pass
            await upload_file(new_path, file_name, is_last=is_last, status_msg=target_msg)
            Transfer.up_bytes.append(os.stat(new_path).st_size)
            if is_last and not split_cleaned:
                if ospath.exists(Paths.temp_zpath): shutil.rmtree(Paths.temp_zpath)
                split_cleaned = True
        else:
            if not ospath.exists(Paths.temp_files_dir): makedirs(Paths.temp_files_dir)
            if not remove: file_path = shutil.copy(file_path, Paths.temp_files_dir)
            file_name = ospath.basename(file_path)
            new_path  = shortFileName(file_path)
            os.rename(file_path, new_path)
            BotTimes.current_time = time()
            Messages.status_head  = f"📤 <b>UPLOADING</b>\n\n<code>{file_name}</code>\n"
            try:
                edited = await target_msg.edit_text(
                    text=Messages.task_msg + Messages.status_head
                    + "\n⏳ <i>Starting...</i>" + sysINFO(),
                    reply_markup=keyboard(),
                )
                target_msg = edited
                if is_global:
                    MSG.status_msg = edited
            except Exception: pass
            file_size = os.stat(new_path).st_size
            await upload_file(new_path, file_name, is_last=is_last, status_msg=target_msg)
            Transfer.up_bytes.append(file_size)
            if remove and ospath.exists(new_path): os.remove(new_path)
            elif not remove:
                for fi in os.listdir(Paths.temp_files_dir):
                    os.remove(ospath.join(Paths.temp_files_dir, fi))

    if remove and ospath.exists(folder_path): shutil.rmtree(folder_path)
    if is_global:
        for d in (Paths.thumbnail_ytdl, Paths.temp_files_dir):
            if ospath.exists(d): shutil.rmtree(d)


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
        os.makedirs(out_dir, exist_ok=True)
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


def _seedr_ready() -> bool:
    return bool((SEEDR_USERNAME or os.environ.get("SEEDR_USERNAME", "")).strip()) and bool(
        (SEEDR_PASSWORD or os.environ.get("SEEDR_PASSWORD", "")).strip()
    )


def _seedr_video_files(files: list[dict]) -> list[dict]:
    videos = [f for f in files if fileType(f.get("name", "")) == "video" and f.get("url")]
    return sorted(videos, key=lambda item: int(item.get("size", 0) or 0), reverse=True)


async def _run_tracked_process(args: list[str], label: str) -> tuple[str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    ProcessTracker.register(proc.pid, label)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=1800)
    except asyncio.TimeoutError as exc:
        proc.kill()
        raise RuntimeError(f"{label} timed out after 1800 seconds") from exc
    finally:
        ProcessTracker.unregister(proc.pid)
    out = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        detail = err.strip() or out.strip() or f"{label} failed with code {proc.returncode}"
        raise RuntimeError(detail)
    return out, err


def _tail_log(lines: int = 80) -> str:
    try:
        if not ospath.exists(Paths.LOG_PATH):
            return ""
        with open(Paths.LOG_PATH, "r", encoding="utf-8", errors="replace") as fh:
            chunk = fh.readlines()[-lines:]
        return "".join(chunk).strip()
    except Exception:
        return ""


async def _seedr_status(kind: str, stage: str, pct: float, detail: str, filename: str = "") -> None:
    pct = max(0.0, min(float(pct), 100.0))
    TaskInfo.set(
        phase="process",
        engine="Seedr+CloudConvert",
        filename=filename or TaskInfo.filename or Messages.download_name,
        percentage=pct,
        speed=detail,
        eta="-",
    )
    text = (
        f"☁️ <b>{kind}</b>\n\n"
        f"<code>{filename or Messages.download_name or 'Seedr job'}</code>\n\n"
        f"<b>Stage</b>  <code>{stage}</code>\n"
        f"<b>Progress</b>  <code>{pct:.1f}%</code>\n"
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


# ═════════════════════════════════════════════════════════════
# Concurrence FreeConvert Hardsub — jusqu'à 3 jobs en vrai parallèle.
#
# Le hardsub FreeConvert se fait sur les serveurs de FreeConvert (pas sur
# Colab), donc plusieurs jobs peuvent tourner en même temps sans se marcher
# dessus au niveau CPU — il suffit juste que chaque job ait :
#   - son propre message de statut Telegram (pas MSG.status_msg, partagé)
#   - son propre dossier de travail (pas Paths.temp_cc_path, partagé)
# Le reste du pipeline (leech normal, CloudConvert, zip...) reste séquentiel
# comme avant, gated par BOT.State.task_going — on ne touche pas à ça.
# ═════════════════════════════════════════════════════════════

FC_HARDSUB_CONCURRENCY = 5
_fc_hardsub_semaphore = asyncio.Semaphore(FC_HARDSUB_CONCURRENCY)

# Même principe pour le hardsub CloudConvert sur lien direct — traitement
# côté serveurs CloudConvert, pas sur Colab, donc parallélisable pareil.
CC_HARDSUB_CONCURRENCY = 5
_cc_hardsub_semaphore = asyncio.Semaphore(CC_HARDSUB_CONCURRENCY)


_KIND_EMOJI = {
    "FreeConvert Hardsub": "🆓",
    "CloudConvert Hardsub": "☁️",
    "Seedr + FreeConvert Hardsub": "🆓",
    "Burn Subs": "🖥️",
    "Mux Subs": "🖥️",
}


async def _fc_job_status(status_msg, kind: str, stage: str, pct: float, detail: str, filename: str = "", job_id: str = "") -> None:
    """Comme _seedr_status, mais édite un message dédié à CE job précis
    plutôt que le MSG.status_msg global — permet à plusieurs jobs FreeConvert
    de tourner en parallèle sans que leurs messages de statut ne s'écrasent.

    `job_id`, quand fourni, ajoute un bouton ❌ Cancel branché sur
    ActiveJobs.cancel(job_id) — pour les jobs lancés en asyncio.create_task
    (FC hardsub direct-link, FFmpeg local burn/mux) qui ne passent pas par
    BOT.TASK/cancelTask() du pipeline leech classique."""
    pct = max(0.0, min(float(pct), 100.0))
    emoji = _KIND_EMOJI.get(kind, "⚙️")
    text = render_task_status(
        emoji=emoji,
        title=kind.upper(),
        filename=filename or "job",
        pct=pct,
        lines=[
            ("Stage", stage),
            ("Detail", detail),
            ("Preset", fc_quality_label(BOT.Options.cc_quality_profile)),
            ("Engine", kind),
        ],
        stop_hint=f"/canceljob_{job_id}" if job_id else "Tap ❌ Cancel below",
    )
    kb = (
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"canceljob_{job_id}")]])
        if job_id else None
    )
    try:
        await status_msg.edit_text(text, disable_web_page_preview=True, reply_markup=kb)
    except Exception:
        pass


async def _probe_remote_video(url: str) -> dict:
    out, _ = await _run_tracked_process(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            url,
        ],
        "ffprobe",
    )
    return json.loads(out or "{}")


def _pick_french_text_subtitle(info: dict) -> dict | None:
    allowed = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text"}
    best = None
    best_score = -1
    for stream in info.get("streams") or []:
        if str(stream.get("codec_type") or "").lower() != "subtitle":
            continue
        codec = str(stream.get("codec_name") or "").lower()
        if codec not in allowed:
            continue
        tags = {str(k).lower(): str(v).lower() for k, v in (stream.get("tags") or {}).items()}
        lang = tags.get("language", "")
        title = " ".join(filter(None, [tags.get("title", ""), tags.get("handler_name", "")]))
        score = 0
        if lang in {"fr", "fra", "fre"}:
            score += 100
        elif "fr" in lang or "french" in lang:
            score += 70
        if "vostfr" in title:
            score += 40
        if "french" in title or "francais" in title or "français" in title:
            score += 30
        if "full" in title:
            score += 5
        if "forced" in title:
            score += 3
        if score > best_score:
            best = stream
            best_score = score
    return best if best_score > 0 else None


async def _extract_subtitle_from_url(video_url: str, stream: dict, dest_dir: str, stem: str) -> str:
    os.makedirs(dest_dir, exist_ok=True)
    codec = str(stream.get("codec_name") or "").lower()
    ext = ".ass" if codec in {"ass", "ssa"} else ".srt"
    out_path = ospath.join(dest_dir, f"{stem}.fr{ext}")
    sub_codec = "ass" if ext == ".ass" else "srt"
    stream_index = int(stream.get("index"))
    await _run_tracked_process(
        [
            "ffmpeg",
            "-y",
            "-i",
            video_url,
            "-map",
            f"0:{stream_index}",
            "-c:s",
            sub_codec,
            out_path,
        ],
        "ffmpeg-subtitle",
    )
    if not ospath.exists(out_path) or ospath.getsize(out_path) == 0:
        raise RuntimeError("Subtitle extraction produced an empty file.")
    return out_path


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


async def Seedr_CC_Hardsub_Handler(magnet: str, resolution: str | None = None, encode_speed: str | None = None) -> None:
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
            probe = await _probe_remote_video(video_url)
            sub_stream = _pick_french_text_subtitle(probe)
            if not sub_stream:
                raise RuntimeError(f"No French text subtitle stream found in {name}")

            await _seedr_status("Seedr + CloudConvert Hardsub", "Extract", base_start + 6.0, "Extracting French subtitles", name)
            subtitle_path = await _extract_subtitle_from_url(video_url, sub_stream, subtitle_dir, stem)

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


async def Seedr_FC_Hardsub_Handler(magnet: str, status_msg, resize: tuple[int, int] | None = None) -> None:
    """
    Équivalent de Seedr_CC_Hardsub_Handler mais via FreeConvert au lieu de
    CloudConvert. Même pipeline : Seedr -> sonde la piste FR -> extrait le
    sous-titre -> hardsub -> upload Telegram.

    Conçu pour tourner en PARALLÈLE avec d'autres jobs FC hardsub (jusqu'à
    FC_HARDSUB_CONCURRENCY à la fois) : dossier de travail et message de
    statut dédiés à ce job, pas de dépendance à MSG.status_msg/BOT.State.
    """
    if not _seedr_ready():
        try:
            await status_msg.edit_text("❌ Seedr credentials are missing in your Colab launcher.")
        except Exception:
            pass
        return
    if not BOT.Options.fc_api_keys:
        try:
            await status_msg.edit_text("❌ FreeConvert API key is missing in your Colab launcher.")
        except Exception:
            pass
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
                probe = await _probe_remote_video(video_url)
                sub_stream = _pick_french_text_subtitle(probe)
                if not sub_stream:
                    raise RuntimeError(f"No French text subtitle stream found in {name}")

                await _fc_job_status(status_msg, "Seedr + FreeConvert Hardsub", "Extract", base_start + 6.0, "Extracting French subtitles", name)
                subtitle_path = await _extract_subtitle_from_url(video_url, sub_stream, subtitle_dir, stem)

                async def _process_cb(pct: float, detail: str, filename: str = name) -> None:
                    overall = (base_start + 10.0) + ((base_end - (base_start + 10.0)) * max(0.0, min(pct, 100.0)) / 100.0)
                    await _fc_job_status(status_msg, "Seedr + FreeConvert Hardsub", "FreeConvert", overall, detail, filename)

                async def _download_cb(pct: float, detail: str, filename: str = name) -> None:
                    overall = 85.0 + ((idx + (max(0.0, min(pct, 100.0)) / 100.0)) / total * 15.0)
                    await _fc_job_status(status_msg, "Seedr + FreeConvert Hardsub", "Download", overall, detail, filename)

                await _fc_job_status(status_msg, "Seedr + FreeConvert Hardsub", "Queue", base_start + 10.0, "Submitting FreeConvert hardsub job", name)

                async def _url_cb(url: str, filename: str = name) -> None:
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
            try:
                await status_msg.edit_text(f"❌ <b>Seedr+FC hardsub failed</b>\n\n<code>{exc}</code>")
            except Exception:
                pass
        finally:
            if folder_id and seedr_user and seedr_pwd:
                await _del_folder(seedr_user, seedr_pwd, folder_id)
            for d in (job_dir, subtitle_dir):
                if ospath.exists(d):
                    shutil.rmtree(d, ignore_errors=True)


async def Direct_CC_Hardsub_Handler(video_url: str, name: str, subtitle_path: str, status_msg, resolution: str | None = None) -> None:
    """
    Équivalent CloudConvert de Direct_FC_Hardsub_Handler : hardsub sur un
    lien direct (Seedr, HTTP classique...) avec sous-titre fourni
    manuellement. Mêmes paramètres/UX que le flow FreeConvert (choix de
    résolution avant d'envoyer le sous-titre) — juste le moteur qui change.

    Conçu pour tourner en PARALLÈLE avec d'autres jobs CC hardsub (jusqu'à
    CC_HARDSUB_CONCURRENCY à la fois) : dossier de travail et message de
    statut dédiés à ce job.
    """
    if not BOT.Options.cc_api_keys:
        try:
            await status_msg.edit_text("❌ CloudConvert API key is missing in your Colab launcher.")
        except Exception:
            pass
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

            async def _url_cb(url: str) -> None:
                try:
                    await colab_bot.send_message(
                        chat_id=status_msg.chat.id,
                        text=(
                            "🔗 <b>Lien direct disponible</b>\n\n"
                            f"<code>{name}</code>\n\n"
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
            try:
                await status_msg.edit_text(f"❌ <b>CloudConvert hardsub failed</b>\n\n<code>{exc}</code>")
            except Exception:
                pass
        finally:
            if ospath.exists(subtitle_path):
                try:
                    os.remove(subtitle_path)
                except Exception:
                    pass
            if ospath.exists(job_dir):
                shutil.rmtree(job_dir, ignore_errors=True)


async def Direct_FC_Hardsub_Handler(video_url: str, name: str, subtitle_path: str, status_msg, resize: tuple[int, int] | None = None) -> None:
    """
    Hardsub FreeConvert sur un lien direct (ex: lien Seedr, lien HTTP classique),
    avec un fichier de sous-titres fourni manuellement par l'utilisateur —
    pas de Seedr, pas d'extraction automatique de piste sub, pas de sonde
    ffprobe. On envoie juste video_url + le sous-titre reçu à FreeConvert.

    Conçu pour tourner en PARALLÈLE avec d'autres jobs FC hardsub (jusqu'à
    FC_HARDSUB_CONCURRENCY à la fois) : dossier de travail et message de
    statut dédiés à ce job.
    """
    if not BOT.Options.fc_api_keys:
        try:
            await status_msg.edit_text("❌ FreeConvert API key is missing in your Colab launcher.")
        except Exception:
            pass
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

            async def _url_cb(url: str) -> None:
                try:
                    await colab_bot.send_message(
                        chat_id=status_msg.chat.id,
                        text=(
                            "🔗 <b>Lien direct disponible</b>\n\n"
                            f"<code>{name}</code>\n\n"
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
            try:
                await status_msg.edit_text("⛔ <b>FreeConvert hardsub cancelled</b>", reply_markup=None)
            except Exception:
                pass
        except Exception as exc:
            try:
                await status_msg.edit_text(f"❌ <b>FreeConvert hardsub failed</b>\n\n<code>{exc}</code>")
            except Exception:
                pass
        finally:
            ActiveJobs.unregister(job_id)
            if ospath.exists(subtitle_path):
                try:
                    os.remove(subtitle_path)
                except Exception:
                    pass
            if ospath.exists(job_dir):
                shutil.rmtree(job_dir, ignore_errors=True)


# ═════════════════════════════════════════════════════════════
# Video Converter local (ffmpeg sur le CPU de Colab)
#
# Contrairement à FreeConvert (qui tourne sur leurs serveurs), l'encodage ici
# consomme le CPU partagé de Colab — on limite donc la concurrence à 2 jobs
# max (au lieu de 3 pour FreeConvert), sinon les encodages se marchent
# dessus et ralentissent tout le monde au lieu d'aider.
# ═════════════════════════════════════════════════════════════

LOCAL_CONVERT_CONCURRENCY = 5
_local_convert_semaphore = asyncio.Semaphore(LOCAL_CONVERT_CONCURRENCY)


async def Local_Video_Convert_Handler(source_message, height: int, status_msg) -> None:
    """
    Télécharge une vidéo envoyée directement au bot (message Telegram), la
    convertit en local à la résolution demandée via ffmpeg, puis l'upload.
    Job isolé (dossier + message de statut dédiés), comme les jobs FreeConvert.
    """
    job_id = uuid.uuid4().hex[:8]
    job_dir = f"{Paths.temp_cc_path}_local_{job_id}"
    makedirs(job_dir, exist_ok=True)

    await _fc_job_status(status_msg, "Video Converter", "Queue", 0.0, "En attente d'un slot disponible...")

    async with _local_convert_semaphore:
        try:
            await _fc_job_status(status_msg, "Video Converter", "Download", 0.0, "Téléchargement depuis Telegram...")
            input_path = await source_message.download(
                file_name=os.path.join(job_dir, "source_input")
            )

            async def _progress_cb(pct: float, detail: str) -> None:
                overall = 10.0 + (max(0.0, min(pct, 100.0)) * 0.80)
                await _fc_job_status(status_msg, "Video Converter", "Encodage", overall, detail)

            base = os.path.splitext(os.path.basename(input_path))[0]
            output_path = os.path.join(job_dir, f"{base}.{height}p.mp4")

            await _fc_job_status(status_msg, "Video Converter", "Encodage", 10.0, f"ffmpeg -> {height}p")
            await convert_resolution(input_path, output_path, height, progress_cb=_progress_cb)

            await _fc_job_status(status_msg, "Video Converter", "Upload", 95.0, "Uploading to Telegram")
            await upload_file(output_path, os.path.basename(output_path), is_last=True, status_msg=status_msg)
            try:
                await status_msg.delete()
            except Exception:
                pass
        except Exception as exc:
            try:
                await status_msg.edit_text(f"❌ <b>Video Converter failed</b>\n\n<code>{exc}</code>")
            except Exception:
                pass
        finally:
            if ospath.exists(job_dir):
                shutil.rmtree(job_dir, ignore_errors=True)


async def Local_Merge_Handler(video_message, audio_path: str, status_msg) -> None:
    """
    Fusionne une vidéo envoyée au bot avec un fichier audio séparé (envoyé
    ensuite en reply). Traitement local ffmpeg, même sémaphore CPU que le
    Video Converter (pas de course entre les deux pour le CPU Colab).
    """
    job_id = uuid.uuid4().hex[:8]
    job_dir = f"{Paths.temp_cc_path}_merge_{job_id}"
    makedirs(job_dir, exist_ok=True)

    await _fc_job_status(status_msg, "Merge Audio+Vidéo", "Queue", 0.0, "En attente d'un slot disponible...")

    async with _local_convert_semaphore:
        try:
            await _fc_job_status(status_msg, "Merge Audio+Vidéo", "Download", 0.0, "Téléchargement de la vidéo...")
            video_path = await video_message.download(file_name=os.path.join(job_dir, "source_video"))

            async def _progress_cb(pct: float, detail: str) -> None:
                overall = 20.0 + (max(0.0, min(pct, 100.0)) * 0.70)
                await _fc_job_status(status_msg, "Merge Audio+Vidéo", "Fusion", overall, detail)

            base = os.path.splitext(os.path.basename(video_path))[0]
            output_path = os.path.join(job_dir, f"{base}.merged.mp4")

            await _fc_job_status(status_msg, "Merge Audio+Vidéo", "Fusion", 15.0, "ffmpeg -> fusion audio/vidéo")
            await merge_audio_video(video_path, audio_path, output_path, progress_cb=_progress_cb)

            await _fc_job_status(status_msg, "Merge Audio+Vidéo", "Upload", 95.0, "Uploading to Telegram")
            await upload_file(output_path, os.path.basename(output_path), is_last=True, status_msg=status_msg)
            try:
                await status_msg.delete()
            except Exception:
                pass
        except Exception as exc:
            try:
                await status_msg.edit_text(f"❌ <b>Merge failed</b>\n\n<code>{exc}</code>")
            except Exception:
                pass
        finally:
            if ospath.exists(audio_path):
                try:
                    os.remove(audio_path)
                except Exception:
                    pass
            if ospath.exists(job_dir):
                shutil.rmtree(job_dir, ignore_errors=True)


async def Local_Thumb_Handler(source_message, status_msg) -> None:
    """Extrait un thumbnail à un timestamp aléatoire (10%-90% de la durée)."""
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
            try:
                await status_msg.edit_text(f"❌ <b>Thumb failed</b>\n\n<code>{exc}</code>")
            except Exception:
                pass
        finally:
            if ospath.exists(job_dir):
                shutil.rmtree(job_dir, ignore_errors=True)


async def Local_Screenshots_Handler(source_message, status_msg, count: int = 5) -> None:
    """Prend N screenshots répartis sur la durée (avec jitter aléatoire)."""
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
            from pyrogram.types import InputMediaPhoto
            media = [InputMediaPhoto(p) for p in shots]
            await colab_bot.send_media_group(chat_id=status_msg.chat.id, media=media)
            try:
                await status_msg.delete()
            except Exception:
                pass
        except Exception as exc:
            try:
                await status_msg.edit_text(f"❌ <b>Screenshots failed</b>\n\n<code>{exc}</code>")
            except Exception:
                pass
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
            try:
                await status_msg.edit_text(f"❌ <b>Trim failed</b>\n\n<code>{exc}</code>")
            except Exception:
                pass
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
            try:
                await status_msg.edit_text(f"❌ <b>Compress failed</b>\n\n<code>{exc}</code>")
            except Exception:
                pass
        finally:
            if ospath.exists(job_dir):
                shutil.rmtree(job_dir, ignore_errors=True)


async def Local_Subs_Handler(video_message, sub_path: str, status_msg, burn: bool) -> None:
    """burn=True -> hardsub (incrusté, ré-encodé) ; burn=False -> mux (piste, copy)."""
    job_id = uuid.uuid4().hex[:8]
    job_dir = f"{Paths.temp_cc_path}_subs_{job_id}"
    makedirs(job_dir, exist_ok=True)
    kind = "Burn Subs" if burn else "Mux Subs"
    ActiveJobs.register(job_id, asyncio.current_task())

    await _fc_job_status(status_msg, kind, "Queue", 0.0, "En attente d'un slot disponible...", job_id=job_id)

    async with _local_convert_semaphore:
        try:
            await _fc_job_status(status_msg, kind, "Download", 0.0, "Téléchargement de la vidéo...", job_id=job_id)
            video_path = await video_message.download(file_name=ospath.join(job_dir, "source_video"))

            base = ospath.splitext(ospath.basename(video_path))[0]

            if burn:
                async def _progress_cb(pct: float, detail: str) -> None:
                    overall = 15.0 + (max(0.0, min(pct, 100.0)) * 0.75)
                    await _fc_job_status(status_msg, kind, "Hardsub", overall, detail, job_id=job_id)

                await _fc_job_status(status_msg, kind, "Style", 8.0, "Application du house style", job_id=job_id)
                sub_path = await apply_house_style(sub_path, job_dir)

                output_path = ospath.join(job_dir, f"{base}.hardsub.mp4")
                await _fc_job_status(status_msg, kind, "Hardsub", 10.0, "ffmpeg -> incrustation", job_id=job_id)
                await burn_subtitles(video_path, sub_path, output_path, progress_cb=_progress_cb)
            else:
                output_path = ospath.join(job_dir, f"{base}.muxed.mkv")
                await _fc_job_status(status_msg, kind, "Mux", 40.0, "ffmpeg -> ajout de la piste", job_id=job_id)
                await mux_subtitles(video_path, sub_path, output_path)

            if BOT.Options.custom_name:
                out_ext = ospath.splitext(output_path)[1]
                has_ext = bool(ospath.splitext(BOT.Options.custom_name)[1])
                upload_name = BOT.Options.custom_name if has_ext else f"{BOT.Options.custom_name}{out_ext}"
            else:
                upload_name = ospath.basename(output_path)
            await _fc_job_status(status_msg, kind, "Upload", 95.0, "Uploading to Telegram", job_id=job_id)
            await upload_file(output_path, upload_name, is_last=True, status_msg=status_msg)
            try:
                await status_msg.delete()
            except Exception:
                pass
        except asyncio.CancelledError:
            try:
                await status_msg.edit_text(f"⛔ <b>{kind} cancelled</b>", reply_markup=None)
            except Exception:
                pass
        except Exception as exc:
            try:
                await status_msg.edit_text(f"❌ <b>{kind} failed</b>\n\n<code>{exc}</code>")
            except Exception:
                pass
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
            try:
                await status_msg.edit_text(f"❌ <b>Manual Shot failed</b>\n\n<code>{exc}</code>")
            except Exception:
                pass
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
            try:
                await status_msg.edit_text(f"❌ <b>Split failed</b>\n\n<code>{exc}</code>")
            except Exception:
                pass
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
            try:
                await status_msg.edit_text(f"❌ <b>Sample failed</b>\n\n<code>{exc}</code>")
            except Exception:
                pass
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
            try:
                await status_msg.edit_text(f"❌ <b>Rename failed</b>\n\n<code>{exc}</code>")
            except Exception:
                pass
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
            try:
                await status_msg.edit_text(f"❌ <b>To Audio failed</b>\n\n<code>{exc}</code>")
            except Exception:
                pass
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
            try:
                await status_msg.edit_text(f"❌ <b>Mute failed</b>\n\n<code>{exc}</code>")
            except Exception:
                pass
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



async def _burn_prefix_suffix_in_dir(job_dir: str, status_msg, label: str) -> None:
    """Grave BOT.Setting.prefix/suffix directement dans l'image de chaque
    vidéo trouvée dans job_dir, EN PLACE (remplace le fichier d'origine).
    No-op silencieux si prefix et suffix sont vides — c'est le comportement
    historique (juste dans le nom de fichier/caption) qui continue à
    s'appliquer dans ce cas. status_msg peut être None (pipeline Seedr+CC
    historique, pas de message dédié) : dans ce cas on grave sans notifier
    de progression détaillée, juste un log."""
    prefix = (BOT.Setting.prefix or "").strip()
    suffix = (BOT.Setting.suffix or "").strip()
    if not prefix and not suffix:
        return

    video_files = [
        f for f in pathlib.Path(job_dir).glob("**/*")
        if f.is_file() and fileType(str(f)) == "video"
    ]
    for i, vf in enumerate(video_files, start=1):
        try:
            if status_msg is not None:
                await _fc_job_status(
                    status_msg, label, "Overlay", 90.0 + (i / max(1, len(video_files))) * 5.0,
                    f"Gravure prefix/suffix {i}/{len(video_files)}",
                )
            else:
                log.info("Burning prefix/suffix into %s (%d/%d)", vf.name, i, len(video_files))
            tmp_out = str(vf) + ".burned.mp4"
            await burn_text_overlay(str(vf), tmp_out, prefix=prefix, suffix=suffix)
            os.remove(str(vf))
            os.rename(tmp_out, str(vf))
        except Exception as exc:
            log.warning("Prefix/suffix burn-in failed for %s: %s", vf, exc)


async def Zip_Handler(down_path: str, is_split: bool, remove: bool):
    Messages.status_head = f"🗜 <b>COMPRESSING</b>\n\n<code>{Messages.download_name}</code>\n"
    TaskInfo.set(phase="process", engine="zip", filename=Messages.download_name)
    try:
        MSG.status_msg = await MSG.status_msg.edit_text(
            text=Messages.task_msg + Messages.status_head + sysINFO(),
            reply_markup=keyboard(),
        )
    except Exception: pass
    if not ospath.exists(Paths.temp_zpath): makedirs(Paths.temp_zpath)
    await archive(down_path, is_split, remove)
    await sleep(2)
    Transfer.total_down_size = getSize(Paths.temp_zpath)
    if remove and ospath.exists(down_path): shutil.rmtree(down_path)


async def Unzip_Handler(down_path: str, remove: bool):
    Messages.status_head = f"📂 <b>EXTRACTING</b>\n\n<code>{Messages.download_name}</code>\n"
    TaskInfo.set(phase="process", engine="unzip", filename=Messages.download_name)
    try:
        MSG.status_msg = await MSG.status_msg.edit_text(
            text=Messages.task_msg + Messages.status_head
            + "\n⏳ <i>Starting...</i>" + sysINFO(),
            reply_markup=keyboard(),
        )
    except Exception: pass
    filenames = natsorted([str(p) for p in pathlib.Path(down_path).glob("**/*") if p.is_file()])
    for f in filenames:
        short_path = ospath.join(down_path, f)
        if not ospath.exists(Paths.temp_unzip_path): makedirs(Paths.temp_unzip_path)
        _, ext = ospath.splitext(ospath.basename(f).lower())
        try:
            if ospath.exists(short_path):
                if ext in [".7z", ".gz", ".zip", ".rar", ".001", ".tar", ".z01"]:
                    await extract(short_path, remove)
                else:
                    shutil.copy(short_path, Paths.temp_unzip_path)
        except Exception as e:
            logging.warning(f"Unzip error: {e}")
    if remove: shutil.rmtree(down_path)


def _kill_stray_processes():
    """Kill any aria2c/ffmpeg/yt-dlp that might have been missed."""
    import subprocess
    for name in ("aria2c", "ffmpeg", "ffprobe"):
        try:
            subprocess.run(
                ["pkill", "-f", name],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass


async def cancelTask(reason: str):
    spent = getTime((datetime.now() - BotTimes.start_time).seconds)
    killed = ProcessTracker.kill_all()

    if BOT.State.task_going:
        try:
            if BOT.TASK and not BOT.TASK.done():
                BOT.TASK.cancel()
        except Exception as exc:
            logging.warning("Task cancel: %s", exc)

    _kill_stray_processes()

    try:
        if ospath.exists(Paths.WORK_PATH):
            shutil.rmtree(Paths.WORK_PATH)
    except Exception as exc:
        logging.warning("Cancel cleanup: %s", exc)

    BOT.State.task_going = False
    TaskInfo.reset()

    text = (
        "⛔ <b>TASK CANCELLED</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"❓  <b>Reason</b>   <i>{reason}</i>\n"
        f"⏱  <b>Spent</b>    <code>{spent}</code>\n"
        f"💀  <b>Killed</b>   <code>{killed} process(es)</code>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>All downloads, uploads and processing stopped.</i>"
    )
    log_tail = _tail_log(60)

    try:
        await MSG.status_msg.edit_text(text)
    except Exception:
        try:
            await colab_bot.send_message(chat_id=BOT.TargetChat, text=text)
        except Exception:
            pass

    if log_tail and "Cancelled by user" not in reason and "Cancelled via" not in reason:
        try:
            await colab_bot.send_message(
                chat_id=BOT.TargetChat,
                text="📜 <b>Recent Log Tail</b>\n\n<code>" + log_tail[-3500:] + "</code>",
            )
        except Exception:
            pass

    logging.info("[Cancel] Task cancelled: %s - killed %s procs", reason, killed)


_OUT_MODE_TAGS = {
    "normal": "#Leech",
    "zip": "#Zip",
    "undzip": "#Unzip",
    "cc_convert": "#Convert",
    "cc_resize": "#Resize",
    "cc_compress": "#Compress",
}

_owner_tag_cache: str | None = None


def _in_mode_tag() -> str:
    if BOT.Mode.ytdl:
        return "#YTDL"
    src = (BOT.SOURCE or [""])[0] if BOT.SOURCE else ""
    if src.startswith("magnet:") or src.lower().endswith(".torrent"):
        return "#Torrent"
    if "drive.google.com" in src:
        return "#GDrive"
    if "mega.nz" in src or "mega.co.nz" in src:
        return "#Mega"
    return "#Aria2"


async def _owner_tag() -> str:
    """@username du propriétaire du bot, mis en cache après le 1er appel
    (évite un appel API get_users à chaque tâche terminée)."""
    global _owner_tag_cache
    if _owner_tag_cache is not None:
        return _owner_tag_cache
    try:
        user = await colab_bot.get_users(OWNER)
        _owner_tag_cache = f"@{user.username}" if user.username else (user.first_name or "Owner")
    except Exception:
        _owner_tag_cache = "Owner"
    return _owner_tag_cache


async def SendLogs(is_leech: bool):
    spent = getTime((datetime.now() - BotTimes.start_time).seconds)
    filename = Transfer.sent_file_names[-1] if Transfer.sent_file_names else "—"
    total_files = len(Transfer.sent_file_names)
    out_mode = _OUT_MODE_TAGS.get(BOT.Mode.type, "#Leech" if is_leech else "#Convert")

    summary = (
        f"<code>{filename}</code>\n"
        "│\n"
        f"┟ Task Size → <code>{sizeUnit(Transfer.total_down_size)}</code>\n"
        f"┠ Time Taken → <code>{spent}</code>\n"
        f"┠ In Mode → <code>{_in_mode_tag()}</code>\n"
        f"┠ Out Mode → <code>{out_mode}</code>\n"
        f"Total Files: <code>{total_files}</code>\n"
        f"┖ Task By → <code>{await _owner_tag()}</code>\n\n"
        "〶 <b>Action Performed :</b>\n"
        "⋗ File(s) have been sent to User PM"
    )
    if _tail_log(10):
        summary += "\n\n📜 <b>Need details?</b> Use <code>/logs</code>"

    try:
        await colab_bot.send_message(chat_id=BOT.TargetChat, text=summary)
    except Exception:
        pass

    BOT.State.started = False
    BOT.State.task_going = False
    TaskInfo.reset()
