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


# ── Manual shot — capture à un timestamp précis fourni par l'utilisateur ──
async def screenshot_at(input_path: str, output_path: str, timestamp: str) -> str:
    """timestamp au format HH:MM:SS, MM:SS ou secondes (ex: '90')."""
    cmd = [
        "ffmpeg", "-y",
        "-ss", timestamp,
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
        raise RuntimeError(f"ffmpeg screenshot failed (code {code}) — timestamp invalide ?")
    return output_path


# ── Split — découpe en N parts à peu près égales ──────────────────────
async def split_video(input_path: str, out_dir: str, parts: int = 3) -> list[str]:
    duration = await _probe_duration(input_path)
    if duration <= 0:
        raise RuntimeError("Impossible de déterminer la durée de la vidéo.")
    parts = max(2, min(parts, 20))
    segment_time = max(1, int(duration / parts))
    ext = os.path.splitext(input_path)[1] or ".mp4"
    os.makedirs(out_dir, exist_ok=True)
    pattern = os.path.join(out_dir, f"part_%02d{ext}")

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-c:v", "libx264", "-crf", "23", "-preset", "veryfast",
        "-force_key_frames", f"expr:gte(t,n_forced*{segment_time})",
        "-c:a", "aac", "-b:a", "128k",
        "-f", "segment",
        "-segment_time", str(segment_time),
        "-reset_timestamps", "1",
        pattern,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    code = await proc.wait()
    if code != 0:
        raise RuntimeError(f"ffmpeg split failed (code {code})")

    files = sorted(
        os.path.join(out_dir, f) for f in os.listdir(out_dir) if f.startswith("part_")
    )
    if not files:
        raise RuntimeError("ffmpeg split produced no output files")
    return files


# ── Sample — extrait court à un point aléatoire de la vidéo ───────────
async def sample_clip(input_path: str, output_path: str, duration: int = 30) -> str:
    total = await _probe_duration(input_path)
    duration = max(5, min(duration, 120))
    if total > duration:
        start = random.uniform(0, max(0.0, total - duration))
    else:
        start = 0.0

    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start:.2f}",
        "-i", input_path,
        "-t", str(duration),
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        output_path,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    code = await proc.wait()
    if code != 0 or not os.path.exists(output_path):
        raise RuntimeError(f"ffmpeg sample extraction failed (code {code})")
    return output_path


# ── To Audio — extrait la piste audio en mp3 ───────────────────────────
async def extract_audio(input_path: str, output_path: str) -> str:
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vn",
        "-c:a", "libmp3lame", "-q:a", "2",
        output_path,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    code = await proc.wait()
    if code != 0 or not os.path.exists(output_path):
        raise RuntimeError(f"ffmpeg audio extraction failed (code {code})")
    return output_path


# ── Mute — retire la piste audio ────────────────────────────────────────
async def mute_video(input_path: str, output_path: str) -> str:
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-c:v", "copy",
        "-an",
        output_path,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    code = await proc.wait()
    if code != 0 or not os.path.exists(output_path):
        raise RuntimeError(f"ffmpeg mute failed (code {code})")
    return output_path


# ── Metadata — texte MediaInfo lisible (ffprobe) ───────────────────────
async def probe_media_info_text(path: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    import json as _json
    try:
        data = _json.loads(out.decode("utf-8", errors="replace") or "{}")
    except Exception:
        return "❌ Impossible de lire les métadonnées."

    fmt = data.get("format", {}) or {}
    streams = data.get("streams", []) or []
    size = int(fmt.get("size") or os.path.getsize(path))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            size_s = f"{size:.1f}{unit}" if unit != "B" else f"{size}{unit}"
            break
        size = size / 1024
    else:
        size_s = f"{size:.1f}TB"

    lines = [
        "MEDIA INFO",
        f"FILE  <code>{os.path.basename(path)}</code>",
        f"SIZE  <code>{size_s}</code>",
    ]
    duration = float(fmt.get("duration") or 0.0)
    if duration > 0:
        h, rem = divmod(int(duration), 3600)
        m, s = divmod(rem, 60)
        dur_s = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
        lines.append(f"DURATION  <code>{dur_s}</code>")

    for stream in streams:
        stype = str(stream.get("codec_type") or "").lower()
        codec = str(stream.get("codec_name") or "?").upper()
        tags = stream.get("tags", {}) or {}
        lang = (tags.get("language") or "").lower()
        lang_s = f" [{lang}]" if lang else ""
        if stype == "video":
            w, h2 = stream.get("width", 0), stream.get("height", 0)
            lines.append(f"VIDEO  <code>{codec}  {w}x{h2}</code>")
        elif stype == "audio":
            ch = int(stream.get("channels") or 0)
            ch_s = {1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}.get(ch, f"{ch}ch" if ch else "")
            lines.append(f"AUDIO  <code>{codec}  {ch_s}{lang_s}</code>")
        elif stype == "subtitle":
            lines.append(f"SUB  <code>{codec}{lang_s}</code>")

    return "\n".join(lines[:14])


# ── Burn prefix/suffix — texte incrusté en dur dans l'image (pas juste ──
# ── dans le nom de fichier/caption, ré-encodage vidéo) ─────────────────
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def _drawtext_font() -> str | None:
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def _drawtext_escape(text: str) -> str:
    # Échappe les caractères qui cassent le parsing du filtre drawtext.
    return (
        text.replace("\\", "\\\\\\\\")
        .replace(":", "\\:")
        .replace("'", "\u2019")  # apostrophe typographique, évite de casser le quoting
        .replace("%", "\\%")
    )


async def burn_text_overlay(
    input_path: str,
    output_path: str,
    prefix: str = "",
    suffix: str = "",
    progress_cb: ProgressCB = None,
) -> str:
    """Grave prefix (haut-gauche) et/ou suffix (bas-droite) directement
    dans l'image vidéo. Appelé uniquement si prefix/suffix non vides —
    sinon rien à graver, autant garder le fichier tel quel (voir handler.py)."""
    prefix = (prefix or "").strip()
    suffix = (suffix or "").strip()
    if not prefix and not suffix:
        raise ValueError("burn_text_overlay called with empty prefix and suffix")

    duration = await _probe_duration(input_path)
    font = _drawtext_font()
    font_opt = f"fontfile='{font}':" if font else ""

    filters = []
    if prefix:
        txt = _drawtext_escape(prefix)
        filters.append(
            f"drawtext={font_opt}text='{txt}':fontsize=h*0.045:fontcolor=white:"
            "borderw=2:bordercolor=black@0.7:x=20:y=20"
        )
    if suffix:
        txt = _drawtext_escape(suffix)
        filters.append(
            f"drawtext={font_opt}text='{txt}':fontsize=h*0.045:fontcolor=white:"
            "borderw=2:bordercolor=black@0.7:x=w-tw-20:y=h-th-20"
        )
    vf = ",".join(filters)

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vf", vf,
        "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
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
            await progress_cb(pct, f"Overlay {int(t)}s / {int(duration)}s")

    code = await proc.wait()
    if code != 0 or not os.path.exists(output_path):
        raise RuntimeError(f"ffmpeg text overlay failed (code {code})")
    if progress_cb:
        await progress_cb(100.0, "Overlay terminé")
    return output_path
