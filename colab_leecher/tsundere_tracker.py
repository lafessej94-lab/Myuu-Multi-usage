"""
colab_leecher/tsundere_tracker.py

Surveillance automatique du flux RSS tsundere.to pour myuu — même esprit
que nyaa_tracker.py : module auto-enregistré (importé une fois dans
__main__.py, s'abonne lui-même à colab_bot), toujours actif en fond dès le
démarrage du bot, aucune commande requise pour l'activer.

Le travail (parsing RSS, extraction de source, téléchargement, validation)
vit dans colab_leecher/engines/tsundere_rss.py — ce module-ci ne fait que
l'orchestration Telegram : boucle de surveillance, persistance JSON de
l'historique, et l'upload via le pipeline existant (Leech — renommage
smart_rename + forward automatique vers les dumps configurés via /add,
exactement comme n'importe quel autre leech du bot).

Historique JSON (data/tsundere_processed.json) à deux clés :
  - "processed" : guid -> {title, url, at} — épisode réellement envoyé.
  - "seen"      : guid -> timestamp — guid déjà rencontré dans le flux,
    qu'il ait été traité avec succès ou non. Sert à ne JAMAIS retraiter un
    guid déjà vu, y compris après un redémarrage du bot — contrairement au
    script d'origine, dont le "premier passage" ignorait en bloc tout ce
    qui se trouvait dans le flux à CHAQUE démarrage (pas seulement le tout
    premier), ce qui pouvait faire perdre silencieusement un épisode sorti
    pile au moment d'un redémarrage. Ici, le flag "premier passage" ne se
    déclenche que si l'historique est totalement vide (aucun guid jamais vu).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path

from pyrogram import filters

from colab_leecher import OWNER, colab_bot
from colab_leecher.engines.tsundere_rss import (
    CHECK_INTERVAL,
    check_file_size,
    episode_key,
    extract_video_url,
    fetch_feed,
    get_title,
    prepare_file,
    source_priority,
)
from colab_leecher.engines.tsundere_rss import download_video as _download_video
from colab_leecher.utility.handler import Leech
from colab_leecher.utility.variables import Paths

log = logging.getLogger(__name__)

_STORE_PATH = "data/tsundere_processed.json"


# ═════════════════════════════════════════════════════════════
# Persistance JSON
# ═════════════════════════════════════════════════════════════

def _load_store() -> dict:
    if not os.path.exists(_STORE_PATH):
        return {"processed": {}, "seen": {}}
    try:
        with open(_STORE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("processed", {})
        data.setdefault("seen", {})
        return data
    except Exception:
        log.warning("⚠️ Historique tsundere illisible, on repart de zéro.")
        return {"processed": {}, "seen": {}}


def _save_store(store: dict) -> None:
    os.makedirs(os.path.dirname(_STORE_PATH) or ".", exist_ok=True)
    try:
        with open(_STORE_PATH, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
    except Exception:
        log.warning("⚠️ Impossible d'écrire l'historique tsundere.")


def _is_known(store: dict, guid: str) -> bool:
    return guid in store["seen"] or guid in store["processed"]


def _mark_seen(store: dict, guid: str) -> None:
    store["seen"][guid] = int(time.time())


def _mark_processed(store: dict, guid: str, title: str, url: str) -> None:
    store["processed"][guid] = {"title": title, "url": url, "at": int(time.time())}
    store["seen"][guid] = store["processed"][guid]["at"]


def _entry_guid(entry) -> str:
    return str(
        entry.get("id") or entry.get("guid") or entry.get("link") or ""
    ).strip()


# ═════════════════════════════════════════════════════════════
# Traitement d'une publication
# ═════════════════════════════════════════════════════════════

async def _notify(text: str, msg=None):
    """Crée ou édite un message de statut dans le DM du owner. Jamais dans
    le chat global (pas de MSG.status_msg touché) — ce tracker tourne en
    fond, indépendamment de toute tâche utilisateur en cours."""
    try:
        if msg is None:
            return await colab_bot.send_message(chat_id=OWNER, text=text)
        return await msg.edit_text(text)
    except Exception:
        return msg


async def _process_entry(guid: str, entry, store: dict) -> None:
    title = get_title(entry)
    video_url = extract_video_url(entry)

    if not video_url or "1fichier.com" in video_url.lower():
        if video_url:
            log.info("⏭️ 1fichier ignoré : %s", video_url)
        else:
            log.warning("⚠️ Aucune URL exploitable pour : %s", title)
        if await _try_alternative_source(title, guid, store):
            return
        _mark_seen(store, guid)
        _save_store(store)
        return

    log.info("🔗 URL trouvée : %s", video_url)
    status_msg = await _notify(f"🍥 <b>Nouvel épisode détecté</b>\n\n<code>{title}</code>\n\n⏳ Téléchargement...")

    job_id = uuid.uuid4().hex[:8]
    job_dir = Path(f"{Paths.temp_cc_path}_tsundere_{job_id}")

    try:
        file_path = await asyncio.to_thread(_download_video, video_url, job_dir)
        log.info("📥 Téléchargement terminé : %s", file_path.name)

        if not check_file_size(file_path):
            raise RuntimeError("Fichier trop volumineux ou invalide.")

        prepared = await asyncio.to_thread(prepare_file, file_path)

        await _notify(f"🍥 <b>{title}</b>\n\n📤 Envoi vers Telegram...", status_msg)
        await Leech(str(job_dir), True, convert_videos=False, status_msg=status_msg)

        _mark_processed(store, guid, title, video_url)
        _save_store(store)
        log.info("✅ Publication traitée : %s", title)
        try:
            await status_msg.delete()
        except Exception:
            pass

    except Exception as exc:
        log.exception("❌ Erreur pendant le traitement : %s", title)
        await _notify(f"❌ <b>{title}</b>\n\n<code>{exc}</code>", status_msg)
        # Pas de mark_processed : le guid reste hors de "processed", mais
        # une source alternative peut encore être tentée juste en dessous.
        if not await _try_alternative_source(title, guid, store):
            _mark_seen(store, guid)
            _save_store(store)
    finally:
        if job_dir.exists():
            import shutil
            shutil.rmtree(job_dir, ignore_errors=True)


async def _try_alternative_source(title: str, guid: str, store: dict) -> bool:
    """Recherche, dans le flux courant, une autre publication du même
    épisode (même episode_key) dont la source n'a pas déjà été essayée."""
    try:
        feed = await fetch_feed()
        current_key = episode_key(title)
        candidates = []

        for other in getattr(feed, "entries", []) or []:
            other_title = get_title(other)
            if episode_key(other_title) != current_key:
                continue
            other_guid = _entry_guid(other) or other_title
            if other_guid == guid or _is_known(store, other_guid):
                continue

            other_url = extract_video_url(other)
            if not other_url:
                continue
            lower_url = other_url.lower()
            if "nekobt.to" in lower_url or "nyaa.si" in lower_url or "1fichier.com" in lower_url:
                continue

            candidates.append((source_priority(other_url), other, other_guid))

        if not candidates:
            log.warning("⚠️ Aucune source alternative disponible pour : %s", title)
            return False

        candidates.sort(key=lambda x: x[0])
        _, selected_entry, selected_guid = candidates[0]
        log.info("🎯 Source alternative trouvée pour %s", title)
        await _process_entry(selected_guid, selected_entry, store)
        return True

    except Exception:
        log.exception("❌ Erreur pendant la recherche de source alternative")
        return False


# ═════════════════════════════════════════════════════════════
# Boucle de surveillance
# ═════════════════════════════════════════════════════════════

async def _poll_loop() -> None:
    await asyncio.sleep(15)
    log.info("📡 Surveillance RSS tsundere.to démarrée")

    store = _load_store()
    bootstrap = not store["seen"] and not store["processed"]

    while True:
        try:
            feed = await fetch_feed()
            entries = list(getattr(feed, "entries", []) or [])
            entries.reverse()  # plus ancien -> plus récent
            log.info("📡 %d publication(s) dans le flux", len(entries))

            if bootstrap:
                log.info("🛑 Premier lancement : backlog actuel ignoré (%d entrée(s))", len(entries))
                for entry in entries:
                    guid = _entry_guid(entry)
                    if guid:
                        _mark_seen(store, guid)
                _save_store(store)
                bootstrap = False
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            new_entries = [
                (g, e) for e in entries
                if (g := _entry_guid(e)) and not _is_known(store, g)
            ]
            if not new_entries:
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            groups: dict[str, list] = {}
            for guid, entry in new_entries:
                key = episode_key(get_title(entry))
                groups.setdefault(key, []).append((guid, entry))

            for key, group in groups.items():
                def _prio(item):
                    url = extract_video_url(item[1])
                    return source_priority(url) if url else 999

                group.sort(key=_prio)
                guid, selected_entry = group[0]

                for other_guid, _ in group:
                    if other_guid != guid:
                        _mark_seen(store, other_guid)
                _save_store(store)

                await _process_entry(guid, selected_entry, store)

        except Exception:
            log.exception("❌ Erreur pendant la vérification RSS tsundere.to")

        await asyncio.sleep(CHECK_INTERVAL)


_tracker_task = None


def _ensure_tracker():
    global _tracker_task
    if _tracker_task and not _tracker_task.done():
        return
    _tracker_task = asyncio.get_event_loop().create_task(_poll_loop())


# ═════════════════════════════════════════════════════════════
# Commande de debug (owner only)
# ═════════════════════════════════════════════════════════════
#
# IMPORTANT — group=-1 :
# nyaa_tracker.py enregistre un handler catch-all sur TOUT message texte
# privé (filters.text & filters.private & ~filters.command([...liste...])),
# utilisé pour capter la saisie de date/heure du mode "snipe". "tsundere_test"
# n'y figure pas. Sans group=-1, ce handler et cmd_tsundere_test seraient
# tous les deux dans le groupe Pyrogram par défaut (0), où UN SEUL handler
# par groupe traite chaque update (le premier dont le filtre matche, dans
# l'ordre d'enregistrement = ordre d'import des modules). Si nyaa_tracker
# est importé avant tsundere_tracker, son catch-all "avale" silencieusement
# /tsundere_test (pas de snipe en attente -> retourne sans rien faire) et
# cmd_tsundere_test n'est jamais atteint. group=-1 place ce handler dans un
# groupe traité avant le groupe 0, donc il répond quel que soit l'ordre
# d'import. Même classe de bug que celui déjà rencontré sur la commande
# Anilist, réglé à l'époque de la même façon.

@colab_bot.on_message(filters.command("tsundere_test") & filters.private, group=-1)
async def cmd_tsundere_test(client, message):
    if message.chat.id != OWNER:
        return
    await message.reply_text("🧪 Test RSS tsundere.to en cours...")
    try:
        feed = await fetch_feed()
        entries = list(getattr(feed, "entries", []) or [])
        if not entries:
            await message.reply_text("❌ Aucun élément trouvé dans le flux.")
            return
        entry = entries[0]
        title = get_title(entry)
        video_url = extract_video_url(entry)
        if not video_url:
            await message.reply_text(f"❌ Vidéo introuvable.\n\n📺 {title}")
            return
        await message.reply_text(f"✅ Trouvé : {title}\n\n🔗 {video_url}\n\n📥 Traitement...")
        store = _load_store()
        guid = _entry_guid(entry) or title
        await _process_entry(guid, entry, store)
        await message.reply_text("✅ Test terminé.")
    except Exception as exc:
        log.exception("Erreur /tsundere_test")
        await message.reply_text(f"❌ Erreur : {exc}")


# Démarrage automatique — toujours actif, pas besoin de commande.
_ensure_tracker()
