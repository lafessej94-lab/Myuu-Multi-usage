"""
colab_leecher/claude_agent.py

Module d'intégration Claude au bot myuu.
Commandes associées : /Relève (démarre l'agent) et /Arise (l'arrête),
utilisables uniquement par l'OWNER — l'agent n'agit que pour lui.

Workflow automatisé une fois l'agent actif :
  1. Poll RSS nyaa.si/Erai-raws toutes les 20s
  2. Détecte un nouvel épisode (480p) non encore traité
  3. Seedr récupère le magnet -> lien de stream direct
  4. Sonde le fichier, extrait la piste de sous-titres FR (texte)
  5. FC hardsub 360p, style B, avec ce sous-titre
  6. FC hardsub 720p (même lien seedr, même sous-titre déjà extrait
     -> pas de re-extraction), style B
  7. Chaque résultat est uploadé sur Telegram (chat de l'OWNER)

Réutilise les briques déjà existantes du repo plutôt que de les
réimplémenter : fetch_urls_via_seedr (colab_leecher.seedr),
probe_remote_video / pick_french_text_subtitle / extract_subtitle_from_url
(colab_leecher.services.subtitle_probe), hardsub_remote_url
(colab_leecher.freeconvert), upload_file (colab_leecher.uploader.telegram).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import aiohttp

from colab_leecher import OWNER, colab_bot
from colab_leecher.utility.variables import BOT, Paths
from colab_leecher.seedr import SeedrError, _del_folder, fetch_urls_via_seedr
from colab_leecher.services.subtitle_probe import (
    extract_subtitle_from_url,
    pick_french_text_subtitle,
    probe_remote_video,
)
from colab_leecher.freeconvert import hardsub_remote_url as fc_hardsub_remote_url
from colab_leecher.uploader.telegram import upload_file
from colab_leecher.utility.helper import fileType

log = logging.getLogger("claude_agent")

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

NYAA_RSS_URL = "https://nyaa.si/?page=rss&u=Erai-raws"
POLL_INTERVAL_SECONDS = 20

DATA_DIR = Path("data")
SEEN_IDS_FILE = DATA_DIR / "nyaa_seen.json"

NYAA_VIEW_ID_RE = re.compile(r"nyaa\.si/view/(\d+)")
QUALITY_480P_RE = re.compile(r"\[480p\b", re.IGNORECASE)

# Style et qualités fixés par le workflow demandé
HARDSUB_STYLE_KEY = "b"
HARDSUB_QUALITIES: list[tuple[str, Optional[tuple[int, int]]]] = [
    ("360p", (640, 360)),
    ("720p", (1280, 720)),
]


# --------------------------------------------------------------------------
# État persistant : IDs nyaa.si déjà traités
# --------------------------------------------------------------------------

def _load_seen_ids() -> set[int]:
    if not SEEN_IDS_FILE.exists():
        return set()
    try:
        data = json.loads(SEEN_IDS_FILE.read_text(encoding="utf-8"))
        return set(data.get("seen_ids", []))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Impossible de lire %s: %s", SEEN_IDS_FILE, e)
        return set()


def _save_seen_ids(seen_ids: set[int]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SEEN_IDS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"seen_ids": sorted(seen_ids)}, indent=2), encoding="utf-8")
    tmp.replace(SEEN_IDS_FILE)


# --------------------------------------------------------------------------
# Parsing RSS nyaa.si
# --------------------------------------------------------------------------

@dataclass
class NyaaEntry:
    id: int
    title: str
    magnet: str


async def fetch_nyaa_entries(session: aiohttp.ClientSession) -> list[NyaaEntry]:
    """Récupère et parse le flux RSS Erai-raws, ne garde que les 480p."""
    async with session.get(NYAA_RSS_URL, timeout=15) as resp:
        resp.raise_for_status()
        text = await resp.text()

    root = ET.fromstring(text)
    entries: list[NyaaEntry] = []

    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or "").strip()
        magnet = (item.findtext("{https://nyaa.si/xmlns/nyaa}magnetURI") or link).strip()

        if not QUALITY_480P_RE.search(title):
            continue

        id_match = NYAA_VIEW_ID_RE.search(guid or link)
        if not id_match or not magnet.startswith("magnet:"):
            continue

        entries.append(NyaaEntry(id=int(id_match.group(1)), title=title, magnet=magnet))

    return entries


# --------------------------------------------------------------------------
# Pipeline : seedr -> sonde/extrait sub FR -> FC hardsub (360p puis 720p)
# --------------------------------------------------------------------------

def _pick_video_file(files: list[dict]) -> Optional[dict]:
    videos = [f for f in files if fileType(f.get("name", "")) == "video" and f.get("url")]
    if not videos:
        return None
    return max(videos, key=lambda f: int(f.get("size", 0) or 0))


async def _notify_owner(text: str) -> None:
    try:
        await colab_bot.send_message(chat_id=OWNER, text=text, disable_web_page_preview=True)
    except Exception as exc:
        log.warning("Notif OWNER échouée: %s", exc)


async def run_pipeline_for_entry(entry: NyaaEntry) -> None:
    """
    Pipeline complet pour un nouvel épisode Erai-raws détecté.
    Réutilise les briques du repo (seedr, subtitle_probe, freeconvert)
    plutôt que de les réimplémenter.
    """
    log.info("Nouvel épisode détecté: %s (id=%s)", entry.title, entry.id)
    await _notify_owner(f"🤖 <b>Claude a détecté un nouvel épisode</b>\n\n<code>{entry.title}</code>")

    job_id = uuid.uuid4().hex[:8]
    job_dir = f"{Paths.temp_cc_path}_claude_{job_id}"
    subtitle_dir = os.path.join(Paths.WORK_PATH, f"claude_subs_{job_id}")
    os.makedirs(job_dir, exist_ok=True)
    os.makedirs(subtitle_dir, exist_ok=True)

    folder_id = None
    seedr_user = seedr_pwd = ""
    try:
        # 1. Seedr : magnet -> lien de stream direct
        await _notify_owner(f"🧲 <code>{entry.title}</code>\nEnvoi vers seedr.cc...")
        files, folder_id, seedr_user, seedr_pwd = await fetch_urls_via_seedr(entry.magnet)
        video = _pick_video_file(files)
        if not video:
            raise SeedrError("Seedr terminé, mais aucun fichier vidéo trouvé dans le torrent.")

        video_url = video["url"]
        name = video["name"]
        stem = os.path.splitext(os.path.basename(name))[0]

        # 2. Sonde + extrait la piste de sous-titres FR (une seule fois,
        #    réutilisée pour les deux hardsub qui suivent)
        await _notify_owner(f"🔎 <code>{name}</code>\nAnalyse des pistes de sous-titres...")
        probe = await probe_remote_video(video_url)
        sub_stream = pick_french_text_subtitle(probe)
        if not sub_stream:
            raise RuntimeError(f"Aucune piste de sous-titres FR (texte) trouvée dans {name}")

        subtitle_path = await extract_subtitle_from_url(video_url, sub_stream, subtitle_dir, stem)
        await _notify_owner(f"💬 Sous-titre FR extrait pour <code>{name}</code>")

        # 3 & 4. FC hardsub 360p puis 720p, même style, même sous-titre
        for quality_label, resize in HARDSUB_QUALITIES:
            await _notify_owner(f"🆓 Hardsub FreeConvert {quality_label} (style B) démarré...")

            output_path = await fc_hardsub_remote_url(
                ",".join(BOT.Options.fc_api_keys),
                video_url,
                name,
                subtitle_path,
                job_dir,
                quality_profile=BOT.Options.cc_quality_profile,
                resize=resize,
                style_key=HARDSUB_STYLE_KEY,
            )

            await upload_file(output_path, os.path.basename(output_path), is_last=True)
            await _notify_owner(f"✅ {quality_label} envoyé : <code>{os.path.basename(output_path)}</code>")

        log.info("Pipeline terminé pour %s", entry.title)

    except Exception as exc:
        log.exception("Pipeline échoué pour %s", entry.title)
        await _notify_owner(f"❌ <b>Pipeline échoué</b>\n<code>{entry.title}</code>\n\n<code>{exc}</code>")
    finally:
        if folder_id and seedr_user and seedr_pwd:
            await _del_folder(seedr_user, seedr_pwd, folder_id)
        for d in (job_dir, subtitle_dir):
            if os.path.exists(d):
                shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------
# Boucle de surveillance (agent "réveillé")
# --------------------------------------------------------------------------

@dataclass
class AgentState:
    running: bool = False
    _task: Optional[asyncio.Task] = field(default=None, repr=False)


_state = AgentState()


async def _watch_loop() -> None:
    seen_ids = _load_seen_ids()
    log.info("Agent Claude démarré - surveillance Erai-raws toutes les %ss", POLL_INTERVAL_SECONDS)

    async with aiohttp.ClientSession() as session:
        if not seen_ids:
            # Premier démarrage : marque tout l'existant comme déjà vu pour
            # éviter de traiter l'historique complet d'un coup.
            initial_entries = await fetch_nyaa_entries(session)
            seen_ids = {e.id for e in initial_entries}
            _save_seen_ids(seen_ids)
            log.info("Init seen_ids: %d entrées marquées comme déjà vues", len(seen_ids))

        while _state.running:
            try:
                entries = await fetch_nyaa_entries(session)
                new_entries = [e for e in entries if e.id not in seen_ids]

                for entry in new_entries:
                    await run_pipeline_for_entry(entry)
                    seen_ids.add(entry.id)
                    _save_seen_ids(seen_ids)

            except Exception:
                log.exception("Erreur pendant le poll nyaa.si")

            await asyncio.sleep(POLL_INTERVAL_SECONDS)


def start_agent() -> bool:
    """Appelé par /Relève (OWNER uniquement). Retourne False si déjà actif."""
    if _state.running:
        return False
    _state.running = True
    _state._task = asyncio.get_event_loop().create_task(_watch_loop())
    return True


def stop_agent() -> bool:
    """Appelé par /Arise (OWNER uniquement). Retourne False si déjà inactif."""
    if not _state.running:
        return False
    _state.running = False
    if _state._task:
        _state._task.cancel()
        _state._task = None
    return True


def is_agent_running() -> bool:
    return _state.running


# --------------------------------------------------------------------------
# Commandes Telegram — à enregistrer dans __main__.py comme les autres
# commandes owner-only du fichier (voir _owner() dans __main__.py) :
#
#   from colab_leecher.claude_agent import start_agent, stop_agent, is_agent_running
#
#   @colab_bot.on_message(filters.command("Relève") & filters.private)
#   async def releve_cmd(client, message):
#       if not _owner(message):
#           return
#       await message.delete()
#       if start_agent():
#           await message.reply_text("🤖 <b>Claude est réveillé</b> — surveillance Erai-raws active.")
#       else:
#           await message.reply_text("⚠️ Claude est déjà actif.")
#
#   @colab_bot.on_message(filters.command("Arise") & filters.private)
#   async def arise_cmd(client, message):
#       if not _owner(message):
#           return
#       await message.delete()
#       if stop_agent():
#           await message.reply_text("💤 <b>Claude se rendort.</b>")
#       else:
#           await message.reply_text("⚠️ Claude n'était pas actif.")
# --------------------------------------------------------------------------
