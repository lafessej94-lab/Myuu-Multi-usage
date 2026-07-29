"""
Conversion vidéo LOCALE (ffmpeg sur le CPU de Colab) — pas d'appel à une API
cloud. Premier module du pipeline "traitement local" du bot.
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Awaitable, Callable, Optional

ProgressCB = Optional[Callable[[float, str], Awaitable[None]]]

# Hauteur cible en pixels — la largeur est calculée automatiquement pour
# garder le ratio d'origine (scale=-2:H, le -2 garantit une largeur paire,
# requise par le codec libx264).
RESOLUTIONS: dict[str, int] = {
    "480": 480,
    "720": 720,
    "1080": 1080,
}


async def _probe_duration(path: str) -> float:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    try:
        return float(out.decode().strip())
    except ValueError:
        return 0.0


def _parse_ffmpeg_time(line: str) -> Optional[float]:
    """Extrait le 'time=HH:MM:SS.ss' des logs ffmpeg pour suivre la progression."""
    m = re.search(r"time=(\d+):(\d+):(\d+\.\d+)", line)
    if not m:
        return None
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


async def convert_resolution(
    input_path: str,
    output_path: str,
    height: int,
    progress_cb: ProgressCB = None,
) -> str:
    """
    Convertit une vidéo à la résolution cible (hauteur en pixels) via ffmpeg
    local. Preset "veryfast" par défaut — le CPU partagé de Colab n'a pas la
    puissance d'un serveur cloud dédié, donc on privilégie la vitesse.
    """
    duration = await _probe_duration(input_path)

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vf", f"scale=-2:{height}",
        "-c:v", "libx264", "-crf", "23", "-preset", "veryfast",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        output_path,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    assert proc.stdout is not None
    while True:
        line_bytes = await proc.stdout.readline()
        if not line_bytes:
            break
        line = line_bytes.decode("utf-8", errors="replace")
        t = _parse_ffmpeg_time(line)
        if t is not None and duration > 0 and progress_cb:
            pct = min(100.0, (t / duration) * 100.0)
            await progress_cb(pct, f"Encodage {int(t)}s / {int(duration)}s")

    code = await proc.wait()
    if code != 0 or not os.path.exists(output_path):
        raise RuntimeError(f"ffmpeg conversion failed (code {code})")

    if progress_cb:
        await progress_cb(100.0, "Conversion terminée")
    return output_path
