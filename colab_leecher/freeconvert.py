from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional
from urllib.parse import unquote

import aiohttp

from colab_leecher.house_style import apply_hardsub_style
from colab_leecher.smart_rename import build_final_name, resolution_label

log = logging.getLogger(__name__)

FC_API = "https://api.freeconvert.com/v1"
_TIMEOUT_SHORT = aiohttp.ClientTimeout(total=30)
_TIMEOUT_DOWNLOAD = aiohttp.ClientTimeout(total=7200)

ProgressCB = Optional[Callable[[float, str], Awaitable[None]]]


@dataclass(frozen=True)
class QualityProfile:
    key: str
    label: str
    crf: int
    speed: str


QUALITY_PROFILES = {
    "fast": QualityProfile("fast", "Fast", 25, "veryfast"),
    "balanced": QualityProfile("balanced", "Balanced", 23, "medium"),
    "small": QualityProfile("small", "Small", 28, "faster"),
    "best": QualityProfile("best", "Best", 21, "slow"),
}


def normalize_quality_profile(profile: str | None) -> str:
    profile = (profile or "balanced").strip().lower()
    return profile if profile in QUALITY_PROFILES else "balanced"


def quality_label(profile: str | None) -> str:
    return QUALITY_PROFILES[normalize_quality_profile(profile)].label


def parse_api_keys(raw: str) -> list[str]:
    return [key.strip() for key in (raw or "").split(",") if key.strip()]


async def get_account_info(api_key: str) -> dict:
    """
    FreeConvert n'a pas d'endpoint 'credits' aussi direct que CloudConvert ;
    on teste juste que la clé est valide via un ping léger sur /process/jobs.
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT_SHORT) as sess:
            async with sess.get(f"{FC_API}/process/jobs?per_page=1", headers=headers) as resp:
                if resp.status == 200:
                    return {"valid": True, "error": None}
                return {"valid": False, "error": f"HTTP {resp.status}"}
    except Exception as exc:
        return {"valid": False, "error": str(exc)}


async def pick_working_key(api_keys: list[str]) -> str:
    if not api_keys:
        raise RuntimeError("FreeConvert API key is missing.")
    if len(api_keys) == 1:
        return api_keys[0]

    results = await asyncio.gather(*(get_account_info(key) for key in api_keys))
    for key, info in zip(api_keys, results):
        if info.get("valid"):
            return key
    raise RuntimeError("Aucune clé FreeConvert valide/disponible.")


async def _post_job(api_key: str, payload: dict) -> dict:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession(timeout=_TIMEOUT_SHORT) as sess:
        async with sess.post(f"{FC_API}/process/jobs", json=payload, headers=headers) as resp:
            data = await resp.json()
            if resp.status not in (200, 201):
                raise RuntimeError(data.get("message") or f"FreeConvert job creation failed ({resp.status})")
    return data


async def _job_status(api_key: str, job_id: str) -> dict:
    headers = {"Authorization": f"Bearer {api_key}"}
    async with aiohttp.ClientSession(timeout=_TIMEOUT_SHORT) as sess:
        async with sess.get(f"{FC_API}/process/jobs/{job_id}", headers=headers) as resp:
            data = await resp.json()
            if resp.status != 200:
                raise RuntimeError(data.get("message") or f"FreeConvert job fetch failed ({resp.status})")
    return data


def _find_task(job: dict, name: str) -> Optional[dict]:
    for task in job.get("tasks", []):
        if task.get("name") == name:
            return task
    return None


def _export_url(job: dict) -> str:
    export_task = _find_task(job, "export")
    if not export_task:
        return ""
    result = export_task.get("result") or {}
    url = result.get("url")
    if url:
        return str(url)
    files = result.get("files") or []
    if files and files[0].get("url"):
        return str(files[0]["url"])
    return ""


def _job_failure_reason(job: dict) -> str:
    for task in job.get("tasks", []):
        if str(task.get("status") or "").lower() in {"error", "failed"}:
            msg = task.get("message") or (task.get("result") or {}).get("message")
            name = task.get("name") or task.get("operation") or "task"
            return f"{name}: {msg}" if msg else f"{name} failed"
    return str(job.get("message") or "Unknown FreeConvert error")


async def _wait_for_job(
    api_key: str,
    job_id: str,
    progress_cb: ProgressCB = None,
    timeout_s: int = 3600,
) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        job = await _job_status(api_key, job_id)
        status = str(job.get("status") or "")
        if status == "completed":
            if progress_cb:
                await progress_cb(100.0, "FreeConvert terminé")
            return job
        if status in {"failed", "error"}:
            raise RuntimeError(_job_failure_reason(job))

        tasks = job.get("tasks") or []
        finished = sum(1 for t in tasks if str(t.get("status")).lower() == "completed")
        pct = min(95.0, (finished / len(tasks) * 100.0)) if tasks else 0.0
        if progress_cb:
            await progress_cb(pct, f"FreeConvert {status}")
        await asyncio.sleep(3)
    raise RuntimeError(f"FreeConvert job {job_id} timed out.")


async def _download_file_aiohttp(url: str, dest_path: str, progress_cb: ProgressCB = None) -> str:
    """Fallback mono-connexion (utilisé seulement si aria2c est indisponible)."""
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    async with aiohttp.ClientSession(timeout=_TIMEOUT_DOWNLOAD) as sess:
        async with sess.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"FreeConvert export download failed ({resp.status}).")
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            if progress_cb:
                await progress_cb(0.0, "Téléchargement du résultat (mono-connexion)")
            with open(dest_path, "wb") as fh:
                async for chunk in resp.content.iter_chunked(1024 * 512):
                    fh.write(chunk)
                    done += len(chunk)
                    if progress_cb and total > 0:
                        await progress_cb(min(100.0, done / total * 100.0), "Téléchargement du résultat")
    if progress_cb:
        await progress_cb(100.0, "Téléchargement terminé")
    return dest_path


def _parse_aria2_pct(line: str) -> Optional[float]:
    """Extrait le pourcentage d'une ligne de log aria2c du style '12MiB/345MiB(3%)'."""
    if "ETA:" not in line:
        return None
    try:
        parts = line.split()
        token = next((p for p in parts if "(" in p and ")" in p and "/" in p), None)
        if not token:
            return None
        pct_str = token[token.find("(") + 1: token.find(")")]
        match = re.findall(r"\d+\.\d+|\d+", pct_str)
        if not match:
            return None
        return max(0.0, min(100.0, float(match[0])))
    except Exception:
        return None


async def _download_file(url: str, dest_path: str, progress_cb: ProgressCB = None) -> str:
    """
    Télécharge le résultat FreeConvert.

    IMPORTANT : contrairement à un download classique, le lien d'export
    FreeConvert semble être à usage unique / ne pas supporter les requêtes
    multi-plages (multi-connexion) — ouvrir plusieurs connexions vers cette
    URL fait planter le download (voire invalide le lien). On force donc
    UNE SEULE connexion (-x1 -s1), avec un timeout de sécurité pour ne
    jamais rester bloqué indéfiniment si le lien pose problème.
    """
    dest_dir = os.path.dirname(dest_path) or "."
    dest_name = os.path.basename(dest_path)
    os.makedirs(dest_dir, exist_ok=True)

    cmd = [
        "aria2c",
        "-x1", "-s1",
        "--seed-time=0",
        "--summary-interval=1",
        "--max-tries=3",
        "--retry-wait=2",
        "--console-log-level=notice",
        "-d", dest_dir,
        "-o", dest_name,
        url,
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        log.warning("aria2c introuvable, fallback sur aiohttp mono-connexion.")
        return await _download_file_aiohttp(url, dest_path, progress_cb)

    if progress_cb:
        await progress_cb(0.0, "Téléchargement (aria2c mono-connexion)")

    assert proc.stdout is not None

    async def _read_output():
        while True:
            line_bytes = await proc.stdout.readline()
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8", errors="replace")
            pct = _parse_aria2_pct(line)
            if pct is not None and progress_cb:
                await progress_cb(pct, "Téléchargement (aria2c mono-connexion)")

    try:
        await asyncio.wait_for(_read_output(), timeout=1800)
        code = await asyncio.wait_for(proc.wait(), timeout=30)
    except asyncio.TimeoutError:
        log.warning("aria2c bloqué trop longtemps, on force l'arrêt et fallback sur aiohttp.")
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return await _download_file_aiohttp(url, dest_path, progress_cb)

    if code != 0 or not os.path.exists(dest_path):
        log.warning("aria2c a échoué (code %s), fallback sur aiohttp.", code)
        return await _download_file_aiohttp(url, dest_path, progress_cb)

    if progress_cb:
        await progress_cb(100.0, "Téléchargement terminé")
    return dest_path


def _encode_subtitle_b64(subtitle_path: str) -> str:
    with open(subtitle_path, "rb") as fh:
        return base64.b64encode(fh.read()).decode("ascii")


def _create_hardsub_payload(
    *,
    video_url: str,
    input_format: str,
    output_format: str,
    subtitle_b64: str,
    crf: int,
    speed: str,
    resize: Optional[tuple[int, int]] = None,
) -> dict:
    options = {
        "video_codec": "libx264",
        "video_rate_control_h264": "crf",
        "video_crf_h264": crf,
        "video_encoding_speed_h264_265": speed,
        "audio_codec": "aac",
        "audio_bitrate_aac": "128k",
        "subtitle_add": "upload",
        "subtitle": subtitle_b64,
        "subtitle_mode": "hard",
    }
    if resize:
        width, height = resize
        options["adjust_video_settings"] = "change-resolution"
        options["video_screen_size"] = f"{width}:{height}"

    return {
        "tasks": {
            "import-video": {
                "operation": "import/url",
                "url": video_url,
            },
            "hardsub": {
                "operation": "convert",
                "input": "import-video",
                "input_format": input_format,
                "output_format": output_format,
                "options": options,
            },
            "export": {
                "operation": "export/url",
                "input": ["hardsub"],
            },
        }
    }


async def hardsub_remote_url(
    api_keys: str,
    video_url: str,
    source_name: str,
    subtitle_path: str,
    dest_dir: str,
    *,
    quality_profile: str = "balanced",
    resize: Optional[tuple[int, int]] = None,
    process_cb: ProgressCB = None,
    download_cb: ProgressCB = None,
    url_cb: Optional[Callable[[str], Awaitable[None]]] = None,
) -> str:
    """
    Brûle des sous-titres dans une vidéo via FreeConvert, en forçant le style
    ASS (police/contour/ombre) avant envoi puisque FreeConvert applique
    tel quel le style écrit dans le fichier sous-titre reçu.

    Le style appliqué n'est plus un profil unique passé en paramètre : depuis
    la refonte de `house_style.apply_hardsub_style`, chaque nom de style
    trouvé dans le fichier source (TopLeft, TopCenter, ..., BottomCenter,
    Default, ou un nom tiers inconnu) reçoit automatiquement le bon profil
    (dialogue ou accent) avec le bon alignment — fidèle au fichier ASS
    Crunchyroll de référence. On ne force donc plus un `style=` ici.

    resize : optionnel — (largeur, hauteur) cible, ex (854, 480) pour du
    480p. Réduit le temps de traitement FreeConvert ET le poids du fichier
    final. None = garde la résolution d'origine.

    url_cb : optionnel — appelé avec le lien de téléchargement direct dès
    que FreeConvert a fini son job, AVANT qu'on commence à télécharger le
    résultat. Sert de filet de sécurité : si le download/upload plante
    ensuite, l'utilisateur a déjà le lien pour récupérer le fichier lui-même.
    """
    keys = parse_api_keys(api_keys)
    api_key = await pick_working_key(keys)
    cfg = QUALITY_PROFILES[normalize_quality_profile(quality_profile)]

    # source_name arrive parfois encore URL-encodé (ex: "%20" au lieu
    # d'espace) quand il provient d'une URL ou d'un nom de fichier extrait
    # d'un lien. On le décode AVANT de construire le nom final, sinon la
    # vidéo (et le sous-titre, voir plus bas) ressortent avec des "%20"
    # littéraux dans leur nom une fois envoyés sur Telegram.
    clean_source_name = unquote(source_name)

    base = os.path.splitext(os.path.basename(clean_source_name))[0]
    input_format = os.path.splitext(clean_source_name)[1].lstrip(".").lower() or "mkv"

    # Nom final : reprend le vrai nom (titre/saison-épisode/qualité/plateforme)
    # du fichier source, langue normalisée en VOSTFR et tag de fin remplacé
    # par Myuus-Raws (voir smart_rename.py). La qualité annoncée suit le
    # resize choisi (ex: 480p) plutôt que la qualité d'origine du fichier,
    # pour ne pas induire en erreur sur ce qui est réellement livré.
    quality_override = resolution_label(resize[1]) if resize else None
    output_name = build_final_name(clean_source_name, override_quality=quality_override, output_ext="mp4")
    output_path = os.path.join(dest_dir, output_name)

    # Pré-stylage : force le rendu, indépendamment de ce que contenait le fichier source.
    # apply_hardsub_style() n'accepte plus de paramètre `style` : elle applique
    # elle-même le bon profil par position (voir house_style.py).
    #
    # Ce fichier stylé n'est utilisé QUE pour construire le payload envoyé à
    # FreeConvert (encodage base64 ci-dessous) -- il n'est plus destiné à
    # être uploadé sur Telegram. On le supprime juste après l'avoir encodé :
    # le laisser dans dest_dir (job_dir) le ferait ramasser par Leech() comme
    # un fichier à envoyer en plus de la vidéo, ce qui double le nombre
    # d'appels d'envoi Telegram par job et augmente le risque de FloodWait.
    styled_sub_path = os.path.join(dest_dir, f"{base}.VOSTFR.ass")
    apply_hardsub_style(subtitle_path, styled_sub_path)
    try:
        subtitle_b64 = _encode_subtitle_b64(styled_sub_path)
    finally:
        if os.path.exists(styled_sub_path):
            try:
                os.remove(styled_sub_path)
            except Exception as exc:
                log.warning("Impossible de supprimer le sous-titre stylé temporaire: %s", exc)

    payload = _create_hardsub_payload(
        video_url=video_url,
        input_format=input_format,
        output_format="mp4",
        subtitle_b64=subtitle_b64,
        crf=cfg.crf,
        speed=cfg.speed,
        resize=resize,
    )

    job = await _post_job(api_key, payload)
    job_id = job.get("id", "?")
    job = await _wait_for_job(api_key, job_id, process_cb)
    url = _export_url(job)
    if not url:
        raise RuntimeError("FreeConvert a terminé sans URL d'export.")

    if url_cb:
        try:
            await url_cb(url)
        except Exception as exc:
            log.warning("url_cb a échoué (non bloquant): %s", exc)

    return await _download_file(url, output_path, download_cb)
