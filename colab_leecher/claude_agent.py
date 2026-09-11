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
# 15s était trop court pour nyaa.si depuis Colab (TimeoutError en boucle
# observé en prod) ; nyaa.si peut répondre lentement, surtout sans UA.
NYAA_TIMEOUT = aiohttp.ClientTimeout(total=30)
NYAA_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MyuuBot/1.0)"}

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


def _parse_nyaa_rss(text: str) -> list[NyaaEntry]:
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


async def fetch_nyaa_entries(session: aiohttp.ClientSession) -> list[NyaaEntry]:
    """Récupère et parse le flux RSS Erai-raws (poll auto), ne garde que les 480p."""
    async with session.get(NYAA_RSS_URL, timeout=NYAA_TIMEOUT, headers=NYAA_HEADERS) as resp:
        resp.raise_for_status()
        text = await resp.text()
    return _parse_nyaa_rss(text)


async def search_nyaa(query: str) -> Optional[NyaaEntry]:
    """
    Recherche manuelle (commande /search_claude) : filtre le flux Erai-raws
    par mot-clé côté serveur nyaa.si, ne garde que les 480p, retourne la
    release la plus récente qui correspond.

    IMPORTANT : nyaa.si traite un "-" dans la query comme un opérateur
    D'EXCLUSION (ex: "Azur Lane - Ni - 10" est compris comme "Azur Lane"
    SANS "Ni" SANS "10", ce qui exclut la release elle-même puisqu'elle
    contient justement ces mots). On remplace donc les "-" et ":" par des
    espaces avant d'envoyer la query à nyaa.si, puis on refiltre localement
    en vérifiant que chaque mot significatif de la requête d'origine
    apparaît bien dans le titre — pour rester précis malgré la
    simplification envoyée au serveur.
    """
    from urllib.parse import quote

    sanitized = re.sub(r"[:\-]", " ", query)
    sanitized = re.sub(r"\s+", " ", sanitized).strip()

    url = f"https://nyaa.si/?page=rss&u=Erai-raws&q={quote(sanitized)}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=NYAA_TIMEOUT, headers=NYAA_HEADERS) as resp:
            resp.raise_for_status()
            text = await resp.text()

    entries = _parse_nyaa_rss(text)

    # Refiltrage local : chaque mot significatif (>=2 caractères) de la
    # requête d'origine doit apparaître dans le titre, insensible à la casse.
    query_words = [w.lower() for w in re.findall(r"\w+", query) if len(w) >= 2]
    matching = [
        e for e in entries
        if all(w in e.title.lower() for w in query_words)
    ]

    return matching[0] if matching else (entries[0] if entries else None)


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


@dataclass
class PreparedSource:
    video_url: str
    name: str
    subtitle_path: str
    job_dir: str
    subtitle_dir: str
    folder_id: Optional[str]
    seedr_user: str
    seedr_pwd: str


async def _prepare_source_and_subtitle(entry: NyaaEntry, job_tag: str) -> PreparedSource:
    """
    Seedr (magnet -> lien direct) + sonde/extraction de la piste de
    sous-titres FR. Commun aux deux flows (auto et /search_claude) : le
    sous-titre n'est extrait qu'une seule fois ici, quel que soit le
    nombre de hardsub lancés ensuite dessus.
    """
    job_dir = f"{Paths.temp_cc_path}_{job_tag}"
    subtitle_dir = os.path.join(Paths.WORK_PATH, f"subs_{job_tag}")
    os.makedirs(job_dir, exist_ok=True)
    os.makedirs(subtitle_dir, exist_ok=True)

    await _notify_owner(f"🧲 <code>{entry.title}</code>\nEnvoi vers seedr.cc...")
    files, folder_id, seedr_user, seedr_pwd = await fetch_urls_via_seedr(entry.magnet)
    video = _pick_video_file(files)
    if not video:
        raise SeedrError("Seedr terminé, mais aucun fichier vidéo trouvé dans le torrent.")

    video_url = video["url"]
    name = video["name"]
    stem = os.path.splitext(os.path.basename(name))[0]

    await _notify_owner(f"🔎 <code>{name}</code>\nAnalyse des pistes de sous-titres...")
    probe = await probe_remote_video(video_url)
    sub_stream = pick_french_text_subtitle(probe)
    if not sub_stream:
        raise RuntimeError(f"Aucune piste de sous-titres FR (texte) trouvée dans {name}")

    subtitle_path = await extract_subtitle_from_url(video_url, sub_stream, subtitle_dir, stem)
    await _notify_owner(f"💬 Sous-titre FR extrait pour <code>{name}</code>")

    return PreparedSource(
        video_url=video_url, name=name, subtitle_path=subtitle_path,
        job_dir=job_dir, subtitle_dir=subtitle_dir,
        folder_id=folder_id, seedr_user=seedr_user, seedr_pwd=seedr_pwd,
    )


async def _hardsub_and_upload_once(
    prep: PreparedSource, resize: Optional[tuple[int, int]], quality_label: str,
) -> None:
    await _notify_owner(f"🆓 Hardsub FreeConvert {quality_label} (style B) démarré...")
    output_path = await fc_hardsub_remote_url(
        ",".join(BOT.Options.fc_api_keys),
        prep.video_url,
        prep.name,
        prep.subtitle_path,
        prep.job_dir,
        quality_profile=BOT.Options.cc_quality_profile,
        resize=resize,
        style_key=HARDSUB_STYLE_KEY,
    )
    await upload_file(output_path, os.path.basename(output_path), is_last=True)
    await _notify_owner(f"✅ {quality_label} envoyé : <code>{os.path.basename(output_path)}</code>")


async def _cleanup_prepared(prep: PreparedSource) -> None:
    if prep.folder_id and prep.seedr_user and prep.seedr_pwd:
        await _del_folder(prep.seedr_user, prep.seedr_pwd, prep.folder_id)
    for d in (prep.job_dir, prep.subtitle_dir):
        if os.path.exists(d):
            shutil.rmtree(d, ignore_errors=True)


async def run_pipeline_for_entry(entry: NyaaEntry) -> None:
    """Flow AUTO (agent réveillé) : 360p puis 720p, style B, sans choix."""
    log.info("Nouvel épisode détecté: %s (id=%s)", entry.title, entry.id)
    await _notify_owner(f"🤖 <b>Claude a détecté un nouvel épisode</b>\n\n<code>{entry.title}</code>")

    prep: Optional[PreparedSource] = None
    try:
        prep = await _prepare_source_and_subtitle(entry, f"claude_{uuid.uuid4().hex[:8]}")
        for quality_label, resize in HARDSUB_QUALITIES:
            await _hardsub_and_upload_once(prep, resize, quality_label)
        log.info("Pipeline terminé pour %s", entry.title)
    except Exception as exc:
        log.exception("Pipeline échoué pour %s", entry.title)
        await _notify_owner(f"❌ <b>Pipeline échoué</b>\n<code>{entry.title}</code>\n\n<code>{exc}</code>")
    finally:
        if prep:
            await _cleanup_prepared(prep)


async def run_manual_hardsub(
    entry: NyaaEntry, resize: Optional[tuple[int, int]], quality_label: str,
) -> None:
    """
    Flow MANUEL (/search_claude) : une seule qualité, choisie par
    l'utilisateur, contrairement au flow auto qui impose 360p ET 720p.
    """
    prep: Optional[PreparedSource] = None
    try:
        prep = await _prepare_source_and_subtitle(entry, f"search_{uuid.uuid4().hex[:8]}")
        await _hardsub_and_upload_once(prep, resize, quality_label)
    except Exception as exc:
        log.exception("Hardsub manuel échoué pour %s", entry.title)
        await _notify_owner(f"❌ <b>Hardsub échoué</b>\n<code>{entry.title}</code>\n\n<code>{exc}</code>")
    finally:
        if prep:
            await _cleanup_prepared(prep)


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
