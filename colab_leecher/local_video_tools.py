"""
Outils vidéo LOCAUX supplémentaires (ffmpeg sur le CPU de Colab), dans le
même esprit que local_convert.py : pas d'appel à une API cloud, mêmes
conventions (progress_cb, parsing du temps ffmpeg).

Complète le menu par-vidéo de __main__.py (_video_tools_kb) avec :
  - Thumb (aléatoire)     -> extract_random_thumbnail
  - Screenshots           -> take_screenshots
  - Trim                  -> trim_video
  - Compress              -> compress_video
  - Mux subs (soft)       -> mux_subtitles
  - Burn subs (hardsub)   -> burn_subtitles
"""
from __future__ import annotations

import asyncio
import os
import random
from typing import Awaitable, Callable, Optional

from colab_leecher.local_convert import _parse_ffmpeg_time, _probe_duration

ProgressCB = Optional[Callable[[float, str], Awaitable[None]]]


# ── Thumbnail aléatoire ──────────────────────────────────────────────
# Même correctif que sur myuu (services/smart_thumbnail.py) et déjà en
# place ailleurs sur zilong (colab_leecher/utility/helper.py) : on tire
# le timestamp au hasard dans la fenêtre 10%-90% de la vidéo au lieu
# d'un point fixe, pour que deux vidéos ne donnent jamais un thumb pris
# au même endroit relatif.
async def extract_random_thumbnail(input_path: str, output_path: str) -> str:
    duration = await _probe_duration(input_path)
    if duration > 4:
        ts = random.uniform(duration * 0.10, duration * 0.90)
    else:
        ts = max(0.0, duration / 2)

    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{ts:.2f}",
        "-i", input_path,
        "-vframes", "1",
        "-q:v", "2",
        output_path,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    code = await proc.wait()
    if code != 0 or not os.path.exists(output_path):
        raise RuntimeError(f"ffmpeg thumbnail extraction failed (code {code})")
    return output_path


# ── Screenshots multiples ────────────────────────────────────────────
async def take_screenshots(input_path: str, out_dir: str, count: int = 5) -> list[str]:
    duration = await _probe_duration(input_path)
    os.makedirs(out_dir, exist_ok=True)

    if duration <= 1:
        timestamps = [0.0]
    else:
        # Un point par tranche égale de la vidéo, avec un peu de jitter
        # aléatoire à l'intérieur de chaque tranche pour éviter que les
        # shots tombent toujours pile sur les mêmes fractions (0%, 20%...).
        step = duration / count
        timestamps = []
        for i in range(count):
            lo = i * step
            hi = min(duration, lo + step)
            timestamps.append(random.uniform(lo, max(lo, hi - 0.1)))

    paths: list[str] = []
    for i, ts in enumerate(timestamps, start=1):
        out = os.path.join(out_dir, f"shot_{i:02d}.jpg")
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{ts:.2f}",
            "-i", input_path,
            "-vframes", "1",
            "-q:v", "2",
            out,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        code = await proc.wait()
        if code == 0 and os.path.exists(out):
            paths.append(out)

    if not paths:
        raise RuntimeError("ffmpeg screenshot extraction failed for every timestamp")
    return paths


# ── Trim ──────────────────────────────────────────────────────────────
async def trim_video(
    input_path: str,
    output_path: str,
    start: str,
    end: str,
    progress_cb: ProgressCB = None,
) -> str:
    """start/end au format HH:MM:SS (ou secondes). -c copy = pas de
    ré-encodage (rapide), la coupe peut être décalée de quelques frames
    sur certains conteneurs — acceptable pour un trim rapide."""
    duration = await _probe_duration(input_path)

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-ss", start,
        "-to", end,
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        output_path,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None
    while True:
        line_bytes = await proc.stdout.readline()
        if not line_bytes:
            break
        t = _parse_ffmpeg_time(line_bytes.decode("utf-8", errors="replace"))
        if t is not None and duration > 0 and progress_cb:
            pct = min(100.0, (t / duration) * 100.0)
            await progress_cb(pct, f"Trim {int(t)}s / {int(duration)}s")

    code = await proc.wait()
    if code != 0 or not os.path.exists(output_path):
        raise RuntimeError(f"ffmpeg trim failed (code {code})")
    if progress_cb:
        await progress_cb(100.0, "Trim terminé")
    return output_path


# ── Compress ──────────────────────────────────────────────────────────
async def compress_video(
    input_path: str,
    output_path: str,
    crf: int = 28,
    progress_cb: ProgressCB = None,
) -> str:
    duration = await _probe_duration(input_path)

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-c:v", "libx264", "-crf", str(crf), "-preset", "veryfast",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        output_path,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None
    while True:
        line_bytes = await proc.stdout.readline()
        if not line_bytes:
            break
        t = _parse_ffmpeg_time(line_bytes.decode("utf-8", errors="replace"))
        if t is not None and duration > 0 and progress_cb:
            pct = min(100.0, (t / duration) * 100.0)
            await progress_cb(pct, f"Compression {int(t)}s / {int(duration)}s")

    code = await proc.wait()
    if code != 0 or not os.path.exists(output_path):
        raise RuntimeError(f"ffmpeg compress failed (code {code})")
    if progress_cb:
        await progress_cb(100.0, "Compression terminée")
    return output_path


# ── Mux subs (soft — piste ajoutée, pas de ré-encodage vidéo) ─────────
async def mux_subtitles(video_path: str, sub_path: str, output_path: str) -> str:
    is_mkv = output_path.lower().endswith(".mkv")
    sub_codec = "copy" if is_mkv else "mov_text"

    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", sub_path,
        "-map", "0", "-map", "1",
        "-c", "copy",
        "-c:s", sub_codec,
        output_path,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    code = await proc.wait()
    if code != 0 or not os.path.exists(output_path):
        raise RuntimeError(f"ffmpeg subtitle mux failed (code {code})")
    return output_path


# ── Burn subs (hardsub — ré-encodage vidéo, sous-titres incrustés) ────
async def burn_subtitles(
    video_path: str,
    sub_path: str,
    output_path: str,
    progress_cb: ProgressCB = None,
) -> str:
    duration = await _probe_duration(video_path)

    # ffmpeg veut un chemin échappé pour le filtre subtitles= (les ':' et
    # "'" cassent le parsing du filtre sinon).
    escaped_sub = sub_path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")

    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vf", f"subtitles='{escaped_sub}'",
        "-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
        "-c:a", "copy",
        "-movflags", "+faststart",
        output_path,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None
    while True:
        line_bytes = await proc.stdout.readline()
        if not line_bytes:
            break
        t = _parse_ffmpeg_time(line_bytes.decode("utf-8", errors="replace"))
        if t is not None and duration > 0 and progress_cb:
            pct = min(100.0, (t / duration) * 100.0)
            await progress_cb(pct, f"Hardsub {int(t)}s / {int(duration)}s")

    code = await proc.wait()
    if code != 0 or not os.path.exists(output_path):
        raise RuntimeError(f"ffmpeg hardsub failed (code {code})")
    if progress_cb:
        await progress_cb(100.0, "Hardsub terminé")
    return output_path
