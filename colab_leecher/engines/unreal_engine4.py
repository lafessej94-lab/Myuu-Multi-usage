"""
colab_leecher/engines/unreal_engine4.py

Le moteur "Myuu" — surnommé "Unreal Engine 4" par l'utilisateur : il
orchestre tsundere_rss (détection) + FreeConvert (double sortie 480p et
360p) + forward_intelligent (routage vers les bons canaux dump).

Flow : dès qu'une vidéo HARDSUB est trouvée sur tsundere.to, le bot la
télécharge, l'envoie vers FreeConvert pour sortir une 480p ET une 360p,
puis envoie chaque sortie vers les canaux dump correspondant au nom de
l'anime (via forward_intelligent), au lieu de tout envoyer partout.

Ce moteur a SA PROPRE boucle de surveillance, séparée de celle (désormais
manuelle) de tsundere_tracker.py — parce que son comportement est
entièrement automatique (pas de sélection manuelle), contrairement au
menu /online_tsundere. Il partage juste le même historique JSON pour ne
jamais traiter deux fois le même épisode.

À CONFIRMER / À BRANCHER avant de pouvoir tester ce fichier :

1. Nom des commandes toggle — j'ai mis /myuu_engine_on et
   /myuu_engine_off en attendant que tu me donnes les vrais noms voulus.
2. engines/freeconvert.py : signature réelle de la fonction de resize
   (deviné ci-dessous comme resize_file(input_path, quality="480p")
   d'après ce que tu avais dit sur cloudconvert.py — à corriger).
3. forward_intelligent.get_dump_channels() : pas encore câblé (voir ce
   fichier) — sans ça, ce moteur ne peut pas savoir où forwarder.
4. Historique JSON : je réutilise data/tsundere_processed.json (partagé
   avec tsundere_tracker.py) pour éviter les doublons entre le mode
   manuel et ce moteur automatique — à confirmer que c'est bien voulu
   (sinon je sépare avec un fichier dédié, ex data/unreal_engine4.json).
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
    anime_name,
    check_file_size,
    download_video,
    episode_key,
    extract_video_url,
    fetch_feed,
    get_title,
    is_hardsub,
    prepare_file,
    source_priority,
)
from colab_leecher.engines.forward_intelligent import get_dump_channels, match_dump_channels
from colab_leecher.utility.variables import Paths

# À VÉRIFIER : signature réelle dans engines/freeconvert.py
from colab_leecher.engines.freeconvert import resize_file

log = logging.getLogger(__name__)

_STORE_PATH = "data/tsundere_processed.json"  # partagé avec tsundere_tracker.py
_QUALITIES = ("480p", "360p")


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


def _mark_processed(store: dict, guid: str, title: str, url: str) -> None:
    store["processed"][guid] = {"title": title, "url": url, "at": int(time.time())}
    store["seen"][guid] = store["processed"][guid]["at"]


def _mark_seen(store: dict, guid: str) -> None:
    store["seen"][guid] = int(time.time())


def _entry_guid(entry) -> str:
    return str(entry.get("id") or entry.get("guid") or entry.get("link") or "").strip()


async def _notify(text: str, msg=None):
    try:
        if msg is None:
            return await colab_bot.send_message(chat_id=OWNER, text=text)
        return await msg.edit_text(text)
    except Exception:
        return msg


# ═════════════════════════════════════════════════════════════
# Traitement d'un épisode détecté : dl -> 480p + 360p -> forward intelligent
# ═════════════════════════════════════════════════════════════

async def _process_entry(guid: str, entry, store: dict) -> None:
    title = get_title(entry)
    name = anime_name(title)
    video_url = extract_video_url(entry)

    if not video_url or "1fichier.com" in video_url.lower():
        log.warning("⚠️ Aucune source exploitable pour : %s", title)
        _mark_seen(store, guid)
        _save_store(store)
        return

    status_msg = await _notify(f"🍥 <b>[Unreal Engine 4] {name}</b>\n\n<code>{title}</code>\n\n⏳ Téléchargement...")

    job_id = uuid.uuid4().hex[:8]
    job_dir = Path(f"{Paths.temp_cc_path}_unreal4_{job_id}")

    try:
        file_path = await asyncio.to_thread(download_video, video_url, job_dir)

        if not check_file_size(file_path):
            raise RuntimeError("Fichier trop volumineux ou invalide.")

        prepared = await asyncio.to_thread(prepare_file, file_path)

        dump_channels = get_dump_channels()
        matches = match_dump_channels(title, dump_channels)
        if not matches:
            log.warning("⚠️ Aucun canal dump ne correspond à : %s", name)

        for quality in _QUALITIES:
            await _notify(f"🍥 <b>[Unreal Engine 4] {name}</b>\n\n🗜️ FreeConvert {quality}...", status_msg)
            output_path = await asyncio.to_thread(resize_file, prepared, quality)

            for channel in matches:
                try:
                    await colab_bot.send_video(
                        chat_id=channel["id"],
                        video=str(output_path),
                        caption=f"{title} [{quality}]",
                    )
                except Exception:
                    log.exception("❌ Échec envoi vers le canal %s", channel.get("name"))

        _mark_processed(store, guid, title, video_url)
        _save_store(store)
        try:
            await status_msg.delete()
        except Exception:
            pass
        log.info("✅ [Unreal Engine 4] Traitement terminé : %s", title)

    except Exception as exc:
        log.exception("❌ [Unreal Engine 4] Erreur : %s", title)
        await _notify(f"❌ <b>[Unreal Engine 4] {name}</b>\n\n<code>{exc}</code>", status_msg)
        _mark_seen(store, guid)
        _save_store(store)
    finally:
        if job_dir.exists():
            import shutil
            shutil.rmtree(job_dir, ignore_errors=True)


# ═════════════════════════════════════════════════════════════
# Boucle de surveillance (propre à ce moteur)
# ═════════════════════════════════════════════════════════════

_enabled: bool = False
_task = None


async def _poll_loop() -> None:
    log.info("🟣 Unreal Engine 4 démarré")
    store = _load_store()

    while _enabled:
        try:
            feed = await fetch_feed()
            entries = list(getattr(feed, "entries", []) or [])
            entries.reverse()

            new_entries = [(g, e) for e in entries if (g := _entry_guid(e)) and not _is_known(store, g)]
            hardsub_entries = [(g, e) for g, e in new_entries if is_hardsub(get_title(e))]
            for g, e in new_entries:
                if not is_hardsub(get_title(e)):
                    _mark_seen(store, g)
            _save_store(store)

            groups: dict[str, list] = {}
            for guid, entry in hardsub_entries:
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
            log.exception("❌ [Unreal Engine 4] Erreur pendant le poll")

        await asyncio.sleep(CHECK_INTERVAL)

    log.info("🟣 Unreal Engine 4 arrêté")


@colab_bot.on_message(filters.command("myuu_engine_on") & filters.private, group=-1)
async def cmd_myuu_engine_on(client, message):
    # NOM DE COMMANDE À CONFIRMER
    global _enabled, _task
    if message.chat.id != OWNER:
        return
    if _enabled:
        await message.reply_text("🟣 Unreal Engine 4 est déjà actif.")
        return
    _enabled = True
    _task = asyncio.get_event_loop().create_task(_poll_loop())
    await message.reply_text("🟣 Unreal Engine 4 activé — surveillance automatique en cours.")


@colab_bot.on_message(filters.command("myuu_engine_off") & filters.private, group=-1)
async def cmd_myuu_engine_off(client, message):
    # NOM DE COMMANDE À CONFIRMER
    global _enabled
    if message.chat.id != OWNER:
        return
    _enabled = False
    await message.reply_text("🟣 Unreal Engine 4 désactivé.")
