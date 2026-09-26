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

from colab_leecher.house_style import apply_hardsub_style, DEFAULT_STYLE_KEY
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


def _task_export_url(task: dict) -> str:
    result = task.get("result") or {}
    url = result.get("url")
    if url:
        return str(url)
    files = result.get("files") or []
    if files and files[0].get("url"):
        return str(files[0]["url"])
    return ""


def _export_url(job: dict) -> str:
    export_task = _find_task(job, "export")
    if not export_task:
        return ""
    return _task_export_url(export_task)


def _job_failure_reason(job: dict) -> str:
    for task in job.get("tasks", []):
        if str(task.get("status") or "").lower() in {"error", "failed"}:
            msg = task.get("message") or (task.get("result") or {}).get("message")
            name = task.get("name") or task.get("operation") or "task"
            return f"{name}: {msg}" if msg else f"{name} failed"
    return str(job.get("message") or "Unknown FreeConvert error")


def _stage_pct(status: str) -> float:
    """Convertit le statut (grossier) d'une tâche FreeConvert en un
    pourcentage indicatif. L'API FreeConvert ne renvoie qu'un statut par
    tâche (waiting/processing/completed), jamais de pourcentage continu
    -- ce n'est donc qu'une estimation par palier (0/50/100), pas une
    progression fluide comme pour le téléchargement du résultat."""
    status = (status or "").lower()
    if status == "completed":
        return 100.0
    if status in {"processing", "running"}:
        return 50.0
    return 0.0


def _detailed_progress(job: dict) -> tuple[float, str]:
    """Construit un message de progression détaillé "Import X% ·
    Compression Y%, Z%" à partir des tâches du job. Générique : marche
    aussi bien pour un job hardsub_remote_url/convert_remote_url (une
    seule tâche "hardsub"/"convert") que pour convert_local_file_multi
    (plusieurs tâches "convert_{quality}"). Les tâches "export_*"
    (préparation du lien de téléchargement côté FreeConvert, rapide) ne
    sont pas affichées séparément, elles comptent juste dans le calcul
    du pourcentage global.
    """
    tasks = job.get("tasks") or []

    import_status = None
    convert_stages: list[tuple[str, str]] = []  # (label, status)

    for task in tasks:
        name = str(task.get("name") or "")
        status = str(task.get("status") or "")

        if name == "import-video":
            import_status = status
        elif name == "hardsub" or name == "convert" or name.startswith("convert_"):
            label = name.split("_", 1)[1] if "_" in name else "vidéo"
            convert_stages.append((label, status))

    parts = []
    if import_status is not None:
        parts.append(f"Import {_stage_pct(import_status):.0f}%")
    if convert_stages:
        detail = ", ".join(f"{label} {_stage_pct(status):.0f}%" for label, status in convert_stages)
        parts.append(f"Compression {detail}")

    msg = " · ".join(parts) if parts else str(job.get("status") or "FreeConvert")

    finished = sum(1 for t in tasks if str(t.get("status")).lower() == "completed")
    overall_pct = min(95.0, (finished / len(tasks) * 100.0)) if tasks else 0.0
    return overall_pct, msg


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
                await progress_cb(100.0, "Import 100% · Compression 100%")
            return job
        if status in {"failed", "error"}:
            raise RuntimeError(_job_failure_reason(job))

        pct, detail_msg = _detailed_progress(job)
        if progress_cb:
            await progress_cb(pct, detail_msg)
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
    output_filename: str,
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
                "filename": os.path.basename(output_filename),
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
    style_key: str = DEFAULT_STYLE_KEY,
    process_cb: ProgressCB = None,
    download_cb: ProgressCB = None,
    url_cb: Optional[Callable[[str], Awaitable[None]]] = None,
) -> str:
    """
    Brûle des sous-titres dans une vidéo via FreeConvert, en forçant le style
    ASS (police/contour/ombre) avant envoi puisque FreeConvert applique
    tel quel le style écrit dans le fichier sous-titre reçu.

    `style_key` sélectionne le preset de rendu ("a" ou "b", voir
    house_style.STYLE_PRESETS).

    resize : optionnel — (largeur, hauteur) cible.

    url_cb : optionnel — appelé avec le lien de téléchargement direct dès
    que FreeConvert a fini son job, AVANT qu'on commence à télécharger le
    résultat.
    """
    keys = parse_api_keys(api_keys)
    api_key = await pick_working_key(keys)
    cfg = QUALITY_PROFILES[normalize_quality_profile(quality_profile)]

    clean_source_name = unquote(source_name)

    base = os.path.splitext(os.path.basename(clean_source_name))[0]
    input_format = os.path.splitext(clean_source_name)[1].lstrip(".").lower() or "mkv"

    quality_override = resolution_label(resize[1]) if resize else None
    output_name = build_final_name(clean_source_name, override_quality=quality_override, output_ext="mp4")
    output_path = os.path.join(dest_dir, output_name)

    styled_sub_path = os.path.join(dest_dir, f"{base}.VOSTFR.ass")
    apply_hardsub_style(subtitle_path, styled_sub_path, style_key=style_key)
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
        output_filename=output_name,
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


# ⚠️ IMPORTANT — limite de l'approche "import URL distante" (option 2) :
# le task FreeConvert "import/url" fait une requête HTTP simple côté
# serveur FreeConvert. Ça marche pour une vraie URL de fichier vidéo
# (is_video_url() == True dans engines/tsundere_rss.py), mais PAS pour
# Transfer.it ou Mega, qui sont des pages/liens chiffrés nécessitant la
# lib `Transferit` ou un client Mega dédié — FreeConvert ne peut pas les
# récupérer tout seul. Comme Transfer.it > Mega sont justement les
# sources PRIORITAIRES du flux tsundere.to (avant "URL vidéo directe"),
# ça veut dire concrètement :
#   - Si la source choisie est une vraie URL vidéo directe -> OK, cette
#     fonction marche telle quelle, aucun download local nécessaire.
#   - Si la source est Transfer.it/Mega -> il faut d'abord télécharger
#     localement (download_video() existant dans engines/tsundere_rss.py),
#     puis uploader ce fichier local vers FreeConvert. Je n'ai vu aucune
#     fonction "import/upload" dans ce que tu m'as partagé — dis-moi si
#     elle existe ailleurs dans freeconvert.py (fichier tronqué ?) ou si
#     c'est à écrire.
# En attendant, les call sites (tsundere_tracker.py / unreal_engine4.py)
# testent is_video_url(video_url) et n'utilisent convert_remote_url() que
# dans ce cas ; sinon ils lèvent une erreur claire au lieu de planter
# silencieusement.

def _create_convert_payload(
    *,
    video_url: str,
    input_format: str,
    output_format: str,
    output_filename: str,
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
            "convert": {
                "operation": "convert",
                "input": "import-video",
                "input_format": input_format,
                "output_format": output_format,
                "filename": os.path.basename(output_filename),
                "options": options,
            },
            "export": {
                "operation": "export/url",
                "input": ["convert"],
            },
        }
    }


async def convert_remote_url(
    api_keys: str,
    video_url: str,
    source_name: str,
    dest_dir: str,
    *,
    quality_profile: str = "balanced",
    resize: Optional[tuple[int, int]] = None,
    process_cb: ProgressCB = None,
    download_cb: ProgressCB = None,
    url_cb: Optional[Callable[[str], Awaitable[None]]] = None,
) -> str:
    """
    Conversion/compression simple (sans hardsub) via FreeConvert, par
    import d'URL distante — voir l'avertissement en tête de section sur
    les sources compatibles (URL vidéo directe uniquement, pas
    Transfer.it/Mega).

    resize : optionnel — (largeur, hauteur) cible (ex (854, 480) pour du
    480p, (640, 360) pour du 360p). None = pas de changement de résolution,
    juste une compression au profil de qualité choisi.
    """
    keys = parse_api_keys(api_keys)
    api_key = await pick_working_key(keys)
    cfg = QUALITY_PROFILES[normalize_quality_profile(quality_profile)]

    clean_source_name = unquote(source_name)
    input_format = os.path.splitext(clean_source_name)[1].lstrip(".").lower() or "mp4"

    quality_suffix = resolution_label(resize[1]) if resize else None
    output_name = build_final_name(clean_source_name, override_quality=quality_suffix, output_ext="mp4")
    output_path = os.path.join(dest_dir, output_name)

    payload = _create_convert_payload(
        video_url=video_url,
        input_format=input_format,
        output_format="mp4",
        output_filename=output_name,
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


# ============================================================
# CONVERSION PAR UPLOAD LOCAL (import/upload)
#
# Pour les sources que FreeConvert ne peut pas récupérer lui-même en
# HTTP simple (Transfer.it, Mega — voir l'avertissement plus haut) : on
# télécharge d'abord le fichier localement (download_video() dans
# tsundere_rss.py), puis on l'UPLOAD vers FreeConvert au lieu de lui
# donner une URL distante.
#
# Endpoint : POST /v1/process/import/upload (ou, comme ici, une tâche
# "operation": "import/upload" dans un job classique) renvoie un
# formulaire pré-signé à usage unique :
#   result.form.url        -> URL dynamique où poster le fichier
#   result.form.parameters -> champs à inclure tels quels (expires,
#                              signature, size_limit, max_file_count...)
# Le fichier s'envoie en multipart/form-data sur cette URL, avec TOUS
# les champs de "parameters" (dans l'ordre reçu) puis le champ "file"
# en dernier — comme un POST de policy S3 classique, l'ordre des champs
# compte, "file" doit être le dernier.
# ============================================================

async def _upload_file(form_url: str, form_parameters: dict, file_path: str) -> None:
    data = aiohttp.FormData()
    for key, value in (form_parameters or {}).items():
        data.add_field(key, str(value))

    with open(file_path, "rb") as fh:
        data.add_field("file", fh, filename=os.path.basename(file_path))
        async with aiohttp.ClientSession(timeout=_TIMEOUT_DOWNLOAD) as sess:
            async with sess.post(form_url, data=data) as resp:
                if resp.status not in (200, 201, 204):
                    text = await resp.text()
                    raise RuntimeError(f"Échec de l'upload FreeConvert ({resp.status}): {text[:300]}")


def _create_upload_multi_convert_payload(
    *,
    input_format: str,
    output_format: str,
    qualities: dict[str, Optional[tuple[int, int]]],
    output_filenames: dict[str, str],
    crf: int,
    speed: str,
) -> dict:
    """Un seul import/upload, plusieurs convert/export en parallèle (un
    par entrée de `qualities`) — le fichier n'est uploadé qu'une fois,
    peu importe le nombre de sorties demandées."""
    tasks: dict = {
        "import-video": {
            "operation": "import/upload",
        },
    }

    for quality, resize in qualities.items():
        options = {
            "video_codec": "libx264",
            "video_rate_control_h264": "crf",
            "video_crf_h264": crf,
            "video_encoding_speed_h264_265": speed,
            "audio_codec": "aac",
            "audio_bitrate_aac": "128k",
        }
        if resize:
            width, height = resize
            options["adjust_video_settings"] = "change-resolution"
            options["video_screen_size"] = f"{width}:{height}"

        convert_name = f"convert_{quality}"
        export_name = f"export_{quality}"

        tasks[convert_name] = {
            "operation": "convert",
            "input": "import-video",
            "input_format": input_format,
            "output_format": output_format,
            "filename": os.path.basename(output_filenames[quality]),
            "options": options,
        }
        tasks[export_name] = {
            "operation": "export/url",
            "input": [convert_name],
        }

    return {"tasks": tasks}


async def convert_local_file_multi(
    api_keys: str,
    file_path: str,
    dest_dir: str,
    *,
    qualities: dict[str, Optional[tuple[int, int]]],
    quality_profile: str = "balanced",
    process_cb: ProgressCB = None,
    upload_cb: ProgressCB = None,
    download_cb: ProgressCB = None,
) -> dict[str, str]:
    """
    Compression FreeConvert à partir d'un fichier LOCAL déjà téléchargé
    (typiquement Transfer.it/Mega, mais fonctionne pour n'importe quelle
    source) : un seul upload vers FreeConvert, puis une sortie par entrée
    de `qualities` (ex {"480p": (854, 480), "360p": (640, 360)}).

    Callbacks (tous optionnels) :
      upload_cb   -> progression de l'upload LOCAL vers FreeConvert
                     (0% au démarrage, 100% une fois posté -- pas de
                     suivi octet par octet, c'est un simple POST
                     multipart d'un coup).
      process_cb  -> progression du traitement CÔTÉ FreeConvert (import
                     + compression). Le message inclut le détail "Import
                     X% · Compression 480p Y%, 360p Z%" -- voir
                     _detailed_progress(), limité aux paliers 0/50/100
                     par tâche (l'API FreeConvert ne donne pas mieux).
      download_cb -> progression du téléchargement de CHAQUE sortie
                     déjà convertie, avec la qualité préfixée dans le
                     message ("480p — Téléchargement...").

    Retourne {quality: chemin_du_fichier_téléchargé}. Une qualité dont
    l'export a échoué est simplement absente du résultat (log warning),
    plutôt que de faire échouer tout le job.
    """
    keys = parse_api_keys(api_keys)
    api_key = await pick_working_key(keys)
    cfg = QUALITY_PROFILES[normalize_quality_profile(quality_profile)]

    file_path = str(file_path)
    source_name = os.path.basename(file_path)
    input_format = os.path.splitext(source_name)[1].lstrip(".").lower() or "mp4"

    output_filenames: dict[str, str] = {}
    output_paths: dict[str, str] = {}
    for quality, resize in qualities.items():
        suffix = resolution_label(resize[1]) if resize else quality
        name = build_final_name(source_name, override_quality=suffix, output_ext="mp4")
        output_filenames[quality] = name
        output_paths[quality] = os.path.join(dest_dir, name)

    payload = _create_upload_multi_convert_payload(
        input_format=input_format,
        output_format="mp4",
        qualities=qualities,
        output_filenames=output_filenames,
        crf=cfg.crf,
        speed=cfg.speed,
    )

    job = await _post_job(api_key, payload)

    import_task = _find_task(job, "import-video")
    if not import_task:
        raise RuntimeError("FreeConvert n'a pas renvoyé de tâche d'upload (import-video introuvable).")
    form = (import_task.get("result") or {}).get("form") or {}
    form_url = form.get("url")
    form_parameters = form.get("parameters") or {}
    if not form_url:
        raise RuntimeError("FreeConvert n'a pas renvoyé d'URL d'upload.")

    if upload_cb:
        await upload_cb(0.0, "Upload vers FreeConvert...")
    await _upload_file(form_url, form_parameters, file_path)
    if upload_cb:
        await upload_cb(100.0, "Upload terminé")

    job_id = job.get("id", "?")
    job = await _wait_for_job(api_key, job_id, process_cb)

    results: dict[str, str] = {}
    for quality in qualities:
        export_task = _find_task(job, f"export_{quality}")
        if not export_task:
            log.warning("⚠️ Tâche d'export introuvable pour la qualité %s", quality)
            continue
        url = _task_export_url(export_task)
        if not url:
            log.warning("⚠️ FreeConvert a terminé sans URL d'export pour la qualité %s", quality)
            continue

        async def _quality_download_cb(pct: float, msg: str, _quality: str = quality) -> None:
            if download_cb:
                await download_cb(pct, f"{_quality} — {msg}")

        results[quality] = await _download_file(
            url, output_paths[quality], _quality_download_cb if download_cb else None
        )

    return results
