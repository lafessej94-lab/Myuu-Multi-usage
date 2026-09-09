"""
services/subtitle_probe.py

Remote-URL subtitle detection and extraction, plus the bounded subprocess
runner they depend on. Extracted from handler.py's _run_tracked_process /
_probe_remote_video / _pick_french_text_subtitle / _extract_subtitle_from_url.

Mirrors services/ffmpeg.py's conventions: bounded subprocess timeouts,
process tracking so /status and cancelTask() can kill stray ffmpeg/ffprobe,
and pure scoring/parsing functions kept separate from the subprocess calls.
"""
from __future__ import annotations

import asyncio
import json
import os
from os import path as ospath

from colab_leecher.utility.variables import ProcessTracker

_PROCESS_TIMEOUT_SECONDS = 1800

_TEXT_SUBTITLE_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text"}


async def run_tracked_process(args: list[str], label: str) -> tuple[str, str]:
    """Run a subprocess with a bounded timeout, registered with
    ProcessTracker so a /status kill-all or cancelTask() can reap it.
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    ProcessTracker.register(proc.pid, label)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_PROCESS_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        proc.kill()
        raise RuntimeError(f"{label} timed out after {_PROCESS_TIMEOUT_SECONDS} seconds") from exc
    finally:
        ProcessTracker.unregister(proc.pid)
    out = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        detail = err.strip() or out.strip() or f"{label} failed with code {proc.returncode}"
        raise RuntimeError(detail)
    return out, err


async def probe_remote_video(url: str) -> dict:
    """ffprobe a remote URL (Seedr CDN link, etc.) and return the parsed JSON."""
    out, _ = await run_tracked_process(
        [
            "ffprobe", "-v", "error",
            "-print_format", "json",
            "-show_streams", "-show_format",
            url,
        ],
        "ffprobe",
    )
    return json.loads(out or "{}")


def pick_french_text_subtitle(info: dict) -> dict | None:
    """Score every text-based subtitle stream and return the best French
    candidate, or None if nothing scores above zero.

    Scoring favors an explicit fr/fra/fre language tag, then a "vostfr" or
    "french"/"francais"/"français" hint in the track title, with small
    bonuses for "full" and "forced" tags.
    """
    best = None
    best_score = -1
    for stream in info.get("streams") or []:
        if str(stream.get("codec_type") or "").lower() != "subtitle":
            continue
        codec = str(stream.get("codec_name") or "").lower()
        if codec not in _TEXT_SUBTITLE_CODECS:
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


async def extract_subtitle_from_url(video_url: str, stream: dict, dest_dir: str, stem: str) -> str:
    """Extract one subtitle stream from a remote video URL via ffmpeg -map."""
    os.makedirs(dest_dir, exist_ok=True)
    codec = str(stream.get("codec_name") or "").lower()
    ext = ".ass" if codec in {"ass", "ssa"} else ".srt"
    out_path = ospath.join(dest_dir, f"{stem}.fr{ext}")
    sub_codec = "ass" if ext == ".ass" else "srt"
    stream_index = int(stream.get("index"))
    await run_tracked_process(
        [
            "ffmpeg", "-y",
            "-i", video_url,
            "-map", f"0:{stream_index}",
            "-c:s", sub_codec,
            out_path,
        ],
        "ffmpeg-subtitle",
    )
    if not ospath.exists(out_path) or ospath.getsize(out_path) == 0:
        raise RuntimeError("Subtitle extraction produced an empty file.")
    return out_path
