from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import aiohttp

from colab_leecher.subtitle_style import DEFAULT_HARDSUB_STYLE, AssStyle, apply_hardsub_style

log = logging.getLogger(__name__)

CC_API = "https://api.cloudconvert.com/v2"
_TIMEOUT_SHORT = aiohttp.ClientTimeout(total=30)
_TIMEOUT_UPLOAD = aiohttp.ClientTimeout(total=7200)
_TIMEOUT_DOWNLOAD = aiohttp.ClientTimeout(total=7200)

ProgressCB = Optional[Callable[[float, str], Awaitable[None]]]


@dataclass(frozen=True)
class QualityProfile:
    key: str
    label: str
    crf: int
    preset: str


QUALITY_PROFILES = {
    "fast": QualityProfile("fast", "Fast", 25, "veryfast"),
    "balanced": QualityProfile("balanced", "Balanced", 23, "medium"),
    "small": QualityProfile("small", "Small", 28, "faster"),
    "best": QualityProfile("best", "Best", 21, "slow"),
}


def normalize_cc_mode(mode: str | None) -> str:
    mode = (mode or "balanced").strip().lower()
    aliases = {
        "cpu": "balanced",
        "default": "balanced",
        "stable": "balanced",
        "save": "economy",
        "saver": "economy",
        "credit": "economy",
        "credits": "economy",
    }
    return aliases.get(mode, mode) if aliases.get(mode, mode) in {"balanced", "economy"} else "balanced"


def normalize_quality_profile(profile: str | None) -> str:
    profile = (profile or "balanced").strip().lower()
    return profile if profile in QUALITY_PROFILES else "balanced"


def cc_mode_label(mode: str | None) -> str:
    return {
        "balanced": "Balanced CPU",
        "economy": "Economy CPU",
    }.get(normalize_cc_mode(mode), "Balanced CPU")


def quality_label(profile: str | None) -> str:
    return QUALITY_PROFILES[normalize_quality_profile(profile)].label


def resize_label(height: int) -> str:
    return "Original" if int(height or 0) <= 0 else f"{int(height)}p"


RIP_TYPES = ["", "BDRip", "WEBRip", "WEB-DL", "HDRip", "DVDRip"]


def rip_label(rip_type: str | None) -> str:
    rip_type = (rip_type or "").strip()
    return rip_type if rip_type else "Auto"


def _rip_tag(rip_type: str | None) -> str:
    """Renvoie '.BDRip' (avec le point) ou '' si Auto — à insérer dans le nom de fichier final."""
    rip_type = (rip_type or "").strip()
    return f".{rip_type}" if rip_type else ""


def profile_options(profile: str | None, mode: str | None) -> tuple[int, str]:
    cfg = QUALITY_PROFILES[normalize_quality_profile(profile)]
    if normalize_cc_mode(mode) == "economy":
        return max(cfg.crf, 24), "veryfast"
    return cfg.crf, cfg.preset


def parse_api_keys(raw: str) -> list[str]:
    return [key.strip() for key in (raw or "").split(",") if key.strip()]


def _arg_safe(name: str) -> str:
    base = re.sub(r"\s+", "_", os.path.basename(name or "file"))
    return re.sub(r"[^A-Za-z0-9._-]", "_", base)


def _find_task(job: dict, name: str) -> Optional[dict]:
    for task in job.get("tasks", []):
        if task.get("name") == name:
            return task
    return None


def _upload_form(task: dict) -> tuple[str, dict]:
    result = task.get("result") or {}
    form = result.get("form") or {}
    return str(form.get("url") or ""), form.get("parameters") or {}


def _export_url(job: dict) -> str:
    for task in job.get("tasks", []):
        if task.get("operation") == "export/url" and task.get("status") == "finished":
            files = (task.get("result") or {}).get("files") or []
            if files and files[0].get("url"):
                return str(files[0]["url"])
    return ""


def _task_error_detail(task: dict) -> str:
    result = task.get("result") or {}
    message = (
        task.get("message")
        or result.get("message")
        or result.get("error")
        or task.get("code")
        or result.get("code")
        or ""
    )
    detail = str(message or "").strip()
    if detail and detail != "Input task has failed":
        return detail

    output = str(result.get("output") or "").strip()
    if output:
        for line in reversed(output.splitlines()):
            line = line.strip()
            if not line:
                continue
            low = line.lower()
            if any(tok in low for tok in ("error", "invalid", "failed", "unsupported", "cannot")):
                return line
        return output.splitlines()[-1].strip()

    return detail


def describe_cc_failure(job: dict) -> str:
    failed = [
        task for task in (job.get("tasks") or [])
        if str(task.get("status") or "").lower() in {"error", "failed", "cancelled", "canceled"}
    ]
    if not failed:
        return str(job.get("message") or "Unknown CloudConvert error").strip()

    def _priority(task: dict) -> tuple[int, int]:
        op = str(task.get("operation") or "")
        generic = _task_error_detail(task) in {"", "Input task has failed"}
        if op == "command":
            return (0, int(generic))
        if op.startswith("import/"):
            return (1, int(generic))
        if op.startswith("export/"):
            return (3, int(generic))
        return (2, int(generic))

    task = sorted(failed, key=_priority)[0]
    detail = _task_error_detail(task)
    label = task.get("name") or task.get("operation") or "task"
    return f"{label}: {detail}" if detail else f"{label} failed"


async def get_account_info(api_key: str) -> dict:
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT_SHORT) as sess:
            async with sess.get(f"{CC_API}/users/me", headers=headers) as resp:
                if resp.status != 200:
                    return {"credits": -1, "error": f"HTTP {resp.status}"}
                data = (await resp.json()).get("data", {})
                return {
                    "credits": int(data.get("credits", 0)),
                    "username": data.get("username", ""),
                    "error": None,
                }
    except Exception as exc:
        return {"credits": -1, "error": str(exc)}


async def pick_best_key(api_keys: list[str]) -> tuple[str, int]:
    if not api_keys:
        raise RuntimeError("CloudConvert API key is missing.")

    results = await asyncio.gather(*(get_account_info(key) for key in api_keys))
    best_key = ""
    best_credits = -1
    for key, info in zip(api_keys, results):
        credits = int(info.get("credits", -1))
        if credits > best_credits:
            best_key = key
            best_credits = credits

    if best_credits <= 0:
        raise RuntimeError("CloudConvert has no usable credits on the configured API keys.")
    return best_key, best_credits


async def _post_job(api_key: str, payload: dict) -> dict:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession(timeout=_TIMEOUT_SHORT) as sess:
        async with sess.post(f"{CC_API}/jobs", json=payload, headers=headers) as resp:
            data = await resp.json()
            if resp.status not in (200, 201):
                raise RuntimeError(data.get("message") or f"CloudConvert job creation failed ({resp.status})")
    return data.get("data", data)


async def _job_status(api_key: str, job_id: str) -> dict:
    headers = {"Authorization": f"Bearer {api_key}"}
    async with aiohttp.ClientSession(timeout=_TIMEOUT_SHORT) as sess:
        async with sess.get(f"{CC_API}/jobs/{job_id}", headers=headers) as resp:
            data = await resp.json()
            if resp.status != 200:
                raise RuntimeError(data.get("message") or f"CloudConvert job fetch failed ({resp.status})")
    return data.get("data", data)


async def _wait_for_upload_task(api_key: str, job_id: str, task_name: str, timeout_s: int = 180) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        job = await _job_status(api_key, job_id)
        task = _find_task(job, task_name)
        if not task:
            raise RuntimeError(f"CloudConvert task '{task_name}' is missing.")
        if task.get("status") == "waiting":
            url, _ = _upload_form(task)
            if url:
                return task
        if task.get("status") in {"error", "failed"}:
            raise RuntimeError(_task_error_detail(task) or f"CloudConvert task '{task_name}' failed.")
        await asyncio.sleep(3)
    raise RuntimeError(f"CloudConvert task '{task_name}' did not become ready in time.")


async def _upload_to_task(
    api_key: str,
    job_id: str,
    task_name: str,
    file_path: str,
    progress_cb: ProgressCB = None,
) -> None:
    task = await _wait_for_upload_task(api_key, job_id, task_name)
    url, params = _upload_form(task)
    if not url:
        raise RuntimeError("CloudConvert did not return an upload URL.")

    file_size = os.path.getsize(file_path)
    if progress_cb:
        await progress_cb(0.0, "Uploading to CloudConvert")

    with open(file_path, "rb") as fh:
        data = aiohttp.FormData()
        for key, value in params.items():
            data.add_field(key, str(value))
        data.add_field("file", fh, filename=_arg_safe(os.path.basename(file_path)))
        async with aiohttp.ClientSession(timeout=_TIMEOUT_UPLOAD) as sess:
            async with sess.post(url, data=data, allow_redirects=True) as resp:
                if resp.status not in (200, 201, 204, 301, 302):
                    body = await resp.text()
                    raise RuntimeError(f"CloudConvert upload failed ({resp.status}): {body[:200]}")

    if progress_cb:
        await progress_cb(100.0, f"Uploaded {os.path.basename(file_path)} ({file_size} bytes)")


async def _wait_for_job(
    api_key: str,
    job_id: str,
    progress_cb: ProgressCB = None,
    timeout_s: int = 7200,
) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        job = await _job_status(api_key, job_id)
        status = str(job.get("status") or "")
        if status == "finished":
            if progress_cb:
                await progress_cb(100.0, "CloudConvert finished")
            return job
        if status in {"error", "failed", "cancelled", "canceled"}:
            raise RuntimeError(describe_cc_failure(job))

        tasks = job.get("tasks") or []
        finished = sum(1 for task in tasks if task.get("status") == "finished")
        pct = min(95.0, (finished / len(tasks) * 100.0)) if tasks else 0.0
        if progress_cb:
            await progress_cb(pct, f"CloudConvert {status}")
        await asyncio.sleep(5)
    raise RuntimeError(f"CloudConvert job {job_id} timed out.")


async def _download_file(url: str, dest_path: str, progress_cb: ProgressCB = None) -> str:
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    async with aiohttp.ClientSession(timeout=_TIMEOUT_DOWNLOAD) as sess:
        async with sess.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"CloudConvert export download failed ({resp.status}).")
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            if progress_cb:
                await progress_cb(0.0, "Downloading result")
            with open(dest_path, "wb") as fh:
                async for chunk in resp.content.iter_chunked(1024 * 512):
                    fh.write(chunk)
                    done += len(chunk)
                    if progress_cb and total > 0:
                        await progress_cb(min(100.0, done / total * 100.0), "Downloading result")
    if progress_cb:
        await progress_cb(100.0, "Download complete")
    return dest_path


def _compress_bitrate_kbps(target_mb: float, source_mb: float = 0.0, duration_s: float = 0.0) -> tuple[int, int]:
    audio_k = 96
    if duration_s > 0:
        total_k = int((target_mb * 8 * 1024) / duration_s)
    elif source_mb > 0:
        est_dur = (source_mb * 8 * 1024) / 1500
        total_k = int((target_mb * 8 * 1024) / max(est_dur, 1))
    else:
        total_k = int((target_mb * 8 * 1024) / 300)
    video_k = max(150, min(8000, total_k - audio_k))
    return video_k, audio_k


def media_duration_seconds(path: str) -> float:
    try:
        cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", path,
        ]
        info = json.loads(subprocess.check_output(cmd))
        return float((info.get("format") or {}).get("duration") or 0.0)
    except Exception:
        return 0.0


async def _create_convert_job(
    api_key: str,
    *,
    video_url: Optional[str] = None,
    video_filename: str,
    output_filename: str,
    crf: int,
    preset: str,
    scale_height: int = 0,
) -> dict:
    v_safe = _arg_safe(video_filename)
    o_safe = _arg_safe(output_filename)
    vf = f'-vf "scale=-2:{scale_height}" ' if scale_height > 0 else ""
    ffmpeg_args = (
        f"-i /input/import-video/{v_safe} "
        f"{vf}"
        f"-c:v libx264 -crf {crf} -preset {preset} -threads 0 "
        f"-c:a aac -b:a 128k -movflags +faststart "
        f"/output/{o_safe}"
    )
    payload = {
        "tag": "zilong-convert",
        "tasks": {
            "import-video": (
                {"operation": "import/url", "url": video_url, "filename": v_safe}
                if video_url else
                {"operation": "import/upload"}
            ),
            "convert": {
                "operation": "command",
                "input": ["import-video"],
                "engine": "ffmpeg",
                "command": "ffmpeg",
                "arguments": ffmpeg_args,
                "capture_output": True,
            },
            "export": {"operation": "export/url", "input": ["convert"]},
        },
    }
    return await _post_job(api_key, payload)


async def _create_compress_job(
    api_key: str,
    *,
    video_url: Optional[str] = None,
    video_filename: str,
    output_filename: str,
    target_mb: float,
    source_mb: float,
    duration_s: float,
    mode: str,
) -> dict:
    v_safe = _arg_safe(video_filename)
    o_safe = _arg_safe(output_filename)
    video_k, audio_k = _compress_bitrate_kbps(target_mb, source_mb, duration_s)
    preset = "veryfast" if normalize_cc_mode(mode) == "economy" else "medium"
    ffmpeg_args = (
        f"-i /input/import-video/{v_safe} "
        f"-c:v libx264 -b:v {video_k}k -maxrate {video_k * 2}k "
        f"-bufsize {video_k * 4}k -preset {preset} -threads 0 "
        f"-c:a aac -b:a {audio_k}k -movflags +faststart "
        f"/output/{o_safe}"
    )
    payload = {
        "tag": "zilong-compress",
        "tasks": {
            "import-video": (
                {"operation": "import/url", "url": video_url, "filename": v_safe}
                if video_url else
                {"operation": "import/upload"}
            ),
            "compress": {
                "operation": "command",
                "input": ["import-video"],
                "engine": "ffmpeg",
                "command": "ffmpeg",
                "arguments": ffmpeg_args,
                "capture_output": True,
            },
            "export": {"operation": "export/url", "input": ["compress"]},
        },
    }
    return await _post_job(api_key, payload)


async def _create_hardsub_job(
    api_key: str,
    *,
    video_url: str,
    video_filename: str,
    subtitle_path: str,
    subtitle_filename: str,
    output_filename: str,
    crf: int,
    preset: str,
    style: AssStyle = DEFAULT_HARDSUB_STYLE,
) -> dict:
    v_safe = _arg_safe(video_filename)
    s_safe = _arg_safe(subtitle_filename)
    o_safe = _arg_safe(output_filename)

    # Force notre style (police/gras/contour/ombre) avant l'envoi, comme
    # pour FreeConvert — sinon ffmpeg utiliserait le style brut du fichier
    # source, qui varie selon d'où vient le sous-titre.
    styled_sub_path = subtitle_path + ".styled.ass"
    apply_hardsub_style(subtitle_path, styled_sub_path, style=style)
    with open(styled_sub_path, "rb") as fh:
        subtitle_b64 = base64.b64encode(fh.read()).decode("ascii")
    try:
        os.remove(styled_sub_path)
    except OSError:
        pass

    sub_path_in_cc = f"/input/import-sub/{s_safe}"
    escaped = sub_path_in_cc.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
    ext = os.path.splitext(subtitle_filename)[1].lower()
    filter_name = "ass" if ext in {".ass", ".ssa"} else "subtitles"
    vf = f"{filter_name}='{escaped}'"

    ffmpeg_args = (
        f"-i /input/import-video/{v_safe} "
        f"-i /input/import-sub/{s_safe} "
        f"-map 0:v:0 -map 0:a? "
        f'-vf "{vf}" '
        f"-c:v libx264 -crf {crf} -preset {preset} -threads 0 "
        f"-c:a aac -b:a 128k -sn -movflags +faststart "
        f"/output/{o_safe}"
    )
    payload = {
        "tag": "zilong-hardsub",
        "tasks": {
            "import-video": {"operation": "import/url", "url": video_url, "filename": v_safe},
            "import-sub": {"operation": "import/base64", "file": subtitle_b64, "filename": s_safe},
            "hardsub": {
                "operation": "command",
                "input": ["import-video", "import-sub"],
                "engine": "ffmpeg",
                "command": "ffmpeg",
                "arguments": ffmpeg_args,
                "capture_output": True,
            },
            "export": {"operation": "export/url", "input": ["hardsub"]},
        },
    }
    return await _post_job(api_key, payload)


async def _run_job(
    api_key: str,
    *,
    source_path: str,
    output_path: str,
    create_job_cb,
    upload_cb: ProgressCB = None,
    process_cb: ProgressCB = None,
    download_cb: ProgressCB = None,
) -> str:
    job = await create_job_cb(api_key)
    job_id = job.get("id", "?")
    await _upload_to_task(api_key, job_id, "import-video", source_path, upload_cb)
    job = await _wait_for_job(api_key, job_id, process_cb)
    url = _export_url(job)
    if not url:
        raise RuntimeError("CloudConvert finished without an export URL.")
    return await _download_file(url, output_path, download_cb)


async def convert_file(
    api_keys: str,
    source_path: str,
    dest_dir: str,
    *,
    output_ext: str = "mp4",
    cc_mode: str = "balanced",
    quality_profile: str = "balanced",
    rip_type: str = "",
    upload_cb: ProgressCB = None,
    process_cb: ProgressCB = None,
    download_cb: ProgressCB = None,
) -> str:
    keys = parse_api_keys(api_keys)
    api_key, _ = await pick_best_key(keys)
    crf, preset = profile_options(quality_profile, cc_mode)
    base = os.path.splitext(os.path.basename(source_path))[0]
    output_name = f"{base}{_rip_tag(rip_type)}.{output_ext.lstrip('.') or 'mp4'}"
    output_path = os.path.join(dest_dir, output_name)
    return await _run_job(
        api_key,
        source_path=source_path,
        output_path=output_path,
        create_job_cb=lambda key: _create_convert_job(
            key,
            video_url=None,
            video_filename=os.path.basename(source_path),
            output_filename=output_name,
            crf=crf,
            preset=preset,
        ),
        upload_cb=upload_cb,
        process_cb=process_cb,
        download_cb=download_cb,
    )


async def resize_file(
    api_keys: str,
    source_path: str,
    dest_dir: str,
    *,
    height: int,
    output_ext: str = "mp4",
    cc_mode: str = "balanced",
    quality_profile: str = "balanced",
    rip_type: str = "",
    upload_cb: ProgressCB = None,
    process_cb: ProgressCB = None,
    download_cb: ProgressCB = None,
) -> str:
    keys = parse_api_keys(api_keys)
    api_key, _ = await pick_best_key(keys)
    crf, preset = profile_options(quality_profile, cc_mode)
    base = os.path.splitext(os.path.basename(source_path))[0]
    suffix = "orig" if int(height or 0) <= 0 else f"{int(height)}p"
    output_name = f"{base}{_rip_tag(rip_type)}.{suffix}.{output_ext.lstrip('.') or 'mp4'}"
    output_path = os.path.join(dest_dir, output_name)
    return await _run_job(
        api_key,
        source_path=source_path,
        output_path=output_path,
        create_job_cb=lambda key: _create_convert_job(
            key,
            video_url=None,
            video_filename=os.path.basename(source_path),
            output_filename=output_name,
            crf=crf,
            preset=preset,
            scale_height=max(int(height or 0), 0),
        ),
        upload_cb=upload_cb,
        process_cb=process_cb,
        download_cb=download_cb,
    )


async def compress_file(
    api_keys: str,
    source_path: str,
    dest_dir: str,
    *,
    target_mb: float,
    cc_mode: str = "balanced",
    rip_type: str = "",
    upload_cb: ProgressCB = None,
    process_cb: ProgressCB = None,
    download_cb: ProgressCB = None,
) -> str:
    keys = parse_api_keys(api_keys)
    api_key, _ = await pick_best_key(keys)
    base = os.path.splitext(os.path.basename(source_path))[0]
    output_name = f"{base}{_rip_tag(rip_type)}.compressed.mp4"
    output_path = os.path.join(dest_dir, output_name)
    source_mb = os.path.getsize(source_path) / (1024 * 1024)
    duration_s = media_duration_seconds(source_path)
    return await _run_job(
        api_key,
        source_path=source_path,
        output_path=output_path,
        create_job_cb=lambda key: _create_compress_job(
            key,
            video_url=None,
            video_filename=os.path.basename(source_path),
            output_filename=output_name,
            target_mb=float(target_mb),
            source_mb=source_mb,
            duration_s=duration_s,
            mode=cc_mode,
        ),
        upload_cb=upload_cb,
        process_cb=process_cb,
        download_cb=download_cb,
    )


async def convert_remote_url(
    api_keys: str,
    video_url: str,
    source_name: str,
    dest_dir: str,
    *,
    output_ext: str = "mp4",
    scale_height: int = 0,
    cc_mode: str = "balanced",
    quality_profile: str = "balanced",
    rip_type: str = "",
    process_cb: ProgressCB = None,
    download_cb: ProgressCB = None,
) -> str:
    keys = parse_api_keys(api_keys)
    api_key, _ = await pick_best_key(keys)
    crf, preset = profile_options(quality_profile, cc_mode)
    base = os.path.splitext(os.path.basename(source_name))[0]
    tag = _rip_tag(rip_type)
    if int(scale_height or 0) > 0:
        output_name = f"{base}{tag}.{int(scale_height)}p.{output_ext.lstrip('.') or 'mp4'}"
    else:
        output_name = f"{base}{tag}.{output_ext.lstrip('.') or 'mp4'}"
    output_path = os.path.join(dest_dir, output_name)
    job = await _create_convert_job(
        api_key,
        video_url=video_url,
        video_filename=os.path.basename(source_name),
        output_filename=output_name,
        crf=crf,
        preset=preset,
        scale_height=max(int(scale_height or 0), 0),
    )
    job = await _wait_for_job(api_key, job.get("id", "?"), process_cb)
    url = _export_url(job)
    if not url:
        raise RuntimeError("CloudConvert finished without an export URL.")
    return await _download_file(url, output_path, download_cb)


async def hardsub_remote_url(
    api_keys: str,
    video_url: str,
    source_name: str,
    subtitle_path: str,
    dest_dir: str,
    *,
    cc_mode: str = "balanced",
    quality_profile: str = "balanced",
    rip_type: str = "",
    process_cb: ProgressCB = None,
    download_cb: ProgressCB = None,
) -> str:
    keys = parse_api_keys(api_keys)
    api_key, _ = await pick_best_key(keys)
    crf, preset = profile_options(quality_profile, cc_mode)
    base = os.path.splitext(os.path.basename(source_name))[0]
    output_name = f"{base}{_rip_tag(rip_type)}.VOSTFR.mp4"
    output_path = os.path.join(dest_dir, output_name)
    job = await _create_hardsub_job(
        api_key,
        video_url=video_url,
        video_filename=os.path.basename(source_name),
        subtitle_path=subtitle_path,
        subtitle_filename=os.path.basename(subtitle_path),
        output_filename=output_name,
        crf=crf,
        preset=preset,
    )
    job = await _wait_for_job(api_key, job.get("id", "?"), process_cb)
    url = _export_url(job)
    if not url:
        raise RuntimeError("CloudConvert finished without an export URL.")
    return await _download_file(url, output_path, download_cb)
