"""
colab_leecher/engines/unreal_engine4.py

Le moteur "Myuu" — surnommé "Unreal Engine 4" par l'utilisateur : il
surveille tsundere.to comme tsundere_tracker.py, mais fonctionne
entièrement en autonomie sur une WATCHLIST hebdomadaire au lieu de
proposer un menu à chaque nouvel épisode.

Principe :
  /myuu_add <nom>     -> ajoute un anime à la watchlist, suivi
                          automatiquement pendant 7 jours (expire tout
                          seul, à rajouter ensuite si tu veux continuer).
  /myuu_list          -> liste la watchlist active + temps restant.
  /myuu_remove <id>   -> retire un anime de la watchlist avant expiration.
  /myuu_engine_on     -> démarre la boucle de surveillance automatique.
  /myuu_engine_off    -> l'arrête.

Tant que le moteur est actif, à chaque cycle de poll il compare les
NOUVEAUX épisodes HARDSUB du flux tsundere.to (jamais le backlog — voir
le mécanisme de bootstrap plus bas) aux noms présents dans la watchlist
(matching simple sur le titre normalisé, comme anime_name() ailleurs
dans le projet — pas de résolution AniList ici). Un match => traitement
IMMÉDIAT et automatique, sans confirmation : la vidéo source est
d'abord téléchargée telle quelle et envoyée aux canaux dump (taguée
[HD], sert de version haute qualité/archive), puis FreeConvert sort en
plus une 480p ET une 360p — chaque sortie est routée vers les bons
canaux dump via forward_intelligent, selon le nom de l'anime.

Bootstrap (nouveau) : au tout premier démarrage (historique JSON
totalement vide), le moteur marque tout le backlog actuellement présent
dans le flux comme "déjà vu", SANS rien traiter — pour garantir que,
peu importe quand un anime est ajouté à la watchlist ensuite, seuls les
épisodes qui arrivent réellement APRÈS l'activation du moteur seront
jamais pris en compte, jamais les anciens déjà dans le flux.

Ce moteur a SA PROPRE boucle de surveillance, séparée de celle de
tsundere_tracker.py (qui ne démarre plus qu'à la demande via
/online_tsundere). Il partage le même historique JSON
(data/tsundere_processed.json) pour ne jamais traiter deux fois le même
épisode entre les deux modes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

from pyrogram import filters

from colab_leecher import OWNER, colab_bot
from colab_leecher.engines.tsundere_rss import (
    CHECK_INTERVAL,
    anime_name,
    check_file_size,
    episode_key,
    extract_video_url,
    fetch_feed,
    get_title,
    is_hardsub,
    is_video_url,
    prepare_file,
    source_priority,
)
from colab_leecher.engines.tsundere_rss import download_video as _download_video
from colab_leecher.engines.forward_intelligent import get_dump_channels, match_dump_channels
from colab_leecher.utility.variables import BOT, Paths

# Conversion simple (sans hardsub) par import d'URL distante — ne marche
# que pour une vraie URL vidéo directe, pas Transfer.it/Mega (voir
# l'avertissement dans engines/freeconvert.py).
from colab_leecher.engines.freeconvert import convert_remote_url

log = logging.getLogger(__name__)

_STORE_PATH = "data/tsundere_processed.json"  # partagé avec tsundere_tracker.py
_QUALITY_RESIZE = {"480p": (854, 480), "360p": (640, 360)}


# ═════════════════════════════════════════════════════════════
# Persistance JSON — historique des épisodes (identique au store de
# tsundere_tracker.py, pour dédupliquer entre les deux modes)
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


def _mark_processed(store: dict, guid: str, title: str, url: str) -> None:
    store["processed"][guid] = {"title": title, "url": url, "at": int(time.time())}
    store["seen"][guid] = store["processed"][guid]["at"]


def _mark_seen(store: dict, guid: str) -> None:
    store["seen"][guid] = int(time.time())


def _entry_guid(entry) -> str:
    return str(entry.get("id") or entry.get("guid") or entry.get("link") or "").strip()


def _pick_untested_hardsub_entry(entries, store: dict):
    """Renvoie le premier épisode HARDSUB non encore connu du store, ou
    (None, None) si rien de tel n'est disponible. Utilisé par
    /unreal_test sans argument."""
    for entry in entries:
        guid = _entry_guid(entry) or get_title(entry)
        if _is_known(store, guid):
            continue
        if not is_hardsub(get_title(entry)):
            continue
        return guid, entry
    return None, None


def _find_best_entry_by_name(entries, query: str):
    """Cherche dans le flux l'épisode HARDSUB le plus récent correspondant
    au nom donné (même matching normalisé que la watchlist), toutes
    sources dédupliquées par episode_key et triées par source_priority.
    Ignore volontairement le store (_is_known) : un test nommé doit
    pouvoir être relancé plusieurs fois d'affilée. Renvoie None si rien
    ne correspond."""
    norm_query = _normalize(query)
    if not norm_query:
        return None

    groups: dict[str, list] = {}
    order: list[str] = []
    for entry in entries:
        title = get_title(entry)
        if not is_hardsub(title):
            continue
        candidate = _normalize(anime_name(title))
        if not candidate:
            continue
        if not (norm_query == candidate or norm_query in candidate or candidate in norm_query):
            continue
        key = episode_key(title)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(entry)

    if not order:
        return None

    # Le flux liste généralement les publications les plus récentes en
    # premier -> le premier groupe rencontré = l'épisode le plus récent
    # disponible pour cet anime.
    group = groups[order[0]]
    group.sort(key=lambda e: source_priority(extract_video_url(e)) if extract_video_url(e) else 999)
    return group[0]


# Store partagé en mémoire pour ce module — chargé une fois, mis à jour au
# fil de l'eau. Tout tourne sur la même event loop asyncio (poll loop +
# commandes), donc pas de souci de concurrence.
_store: dict = _load_store()


async def _notify(text: str, msg=None):
    try:
        if msg is None:
            return await colab_bot.send_message(chat_id=OWNER, text=text)
        return await msg.edit_text(text)
    except Exception:
        return msg


# ═════════════════════════════════════════════════════════════
# Watchlist hebdomadaire — expire automatiquement après 7 jours
# ═════════════════════════════════════════════════════════════

_WATCHLIST_PATH = "data/unreal4_watchlist.json"
_EXPIRY_SECONDS = 7 * 24 * 3600


@dataclass
class WatchEntry:
    id: int
    name: str
    added_at: float
    expires_at: float


class _WL:
    _entries: dict[int, WatchEntry] = {}
    _nid: int = 1

    @classmethod
    def _load(cls) -> None:
        try:
            with open(_WATCHLIST_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for d in raw.get("e", {}).values():
                try:
                    e = WatchEntry(**d)
                    cls._entries[e.id] = e
                except TypeError:
                    pass
            cls._nid = raw.get("n", max(cls._entries.keys(), default=0) + 1)
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("[Unreal4 WL] %s", exc)

    @classmethod
    def _save(cls) -> None:
        os.makedirs(os.path.dirname(_WATCHLIST_PATH) or ".", exist_ok=True)
        with open(_WATCHLIST_PATH, "w", encoding="utf-8") as f:
            json.dump(
                {"e": {str(e.id): asdict(e) for e in cls._entries.values()}, "n": cls._nid},
                f, ensure_ascii=False, indent=2,
            )

    @classmethod
    def add(cls, name: str) -> int:
        now = time.time()
        entry = WatchEntry(id=cls._nid, name=name, added_at=now, expires_at=now + _EXPIRY_SECONDS)
        cls._entries[entry.id] = entry
        cls._nid += 1
        cls._save()
        return entry.id

    @classmethod
    def remove(cls, eid: int) -> bool:
        if eid in cls._entries:
            del cls._entries[eid]
            cls._save()
            return True
        return False

    @classmethod
    def all(cls) -> list[WatchEntry]:
        return sorted(cls._entries.values(), key=lambda e: e.id)

    @classmethod
    def purge_expired(cls) -> list[WatchEntry]:
        """Retire les entrées expirées et les renvoie (pour notification)."""
        now = time.time()
        expired = [e for e in cls._entries.values() if e.expires_at <= now]
        for e in expired:
            del cls._entries[e.id]
        if expired:
            cls._save()
        return expired


_WL._load()


def _normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _match_watchlist(title: str, watch_entries: list["WatchEntry"]) -> str | None:
    """Matching simple sur le titre RSS normalisé (anime_name()), pas de
    résolution AniList. Retourne le nom (tel que tapé dans /myuu_add) de
    la première entrée qui matche, ou None si aucun match."""
    candidate = _normalize(anime_name(title))
    if not candidate:
        return None
    for entry in watch_entries:
        norm_name = _normalize(entry.name)
        if not norm_name:
            continue
        if norm_name == candidate or norm_name in candidate or candidate in norm_name:
            return entry.name
    return None


@colab_bot.on_message(filters.command("myuu_add") & filters.private, group=-1)
async def cmd_myuu_add(client, message):
    if message.chat.id != OWNER:
        return
    name = " ".join(message.command[1:]).strip()
    if not name:
        await message.reply_text(
            "Usage : <code>/myuu_add Nom de l'anime</code>\n\n"
            "Suivi automatique pendant 7 jours — les nouveaux épisodes "
            "HARDSUB détectés seront traités et forward automatiquement, "
            "sans confirmation. Relance la commande pour renouveler après "
            "expiration."
        )
        return

    eid = _WL.add(name)
    expiry_str = datetime.fromtimestamp(time.time() + _EXPIRY_SECONDS).strftime("%d/%m %H:%M")
    await message.reply_text(
        f"✅ <b>#{eid}</b> ajouté à la watchlist Unreal Engine 4\n\n"
        f"📺 <code>{name}</code>\n"
        f"⏳ Suivi automatique jusqu'au <b>{expiry_str}</b> (7 jours)."
    )


@colab_bot.on_message(filters.command("myuu_list") & filters.private, group=-1)
async def cmd_myuu_list(client, message):
    if message.chat.id != OWNER:
        return
    _WL.purge_expired()
    entries = _WL.all()
    if not entries:
        await message.reply_text("🟣 Watchlist Unreal Engine 4 vide.\nUtilise /myuu_add <nom de l'anime>.")
        return

    now = time.time()
    lines = ["🟣 <b>Watchlist Unreal Engine 4</b>", "━━━━━━━━━━━━━━━━━━━━━━━━", ""]
    for e in entries:
        remaining = max(0, e.expires_at - now)
        days = int(remaining // 86400)
        hours = int((remaining % 86400) // 3600)
        lines.append(f"🔹 <b>#{e.id}</b>  <code>{e.name}</code>\n   ⏳ Expire dans {days}j {hours}h")
        lines.append("")
    await message.reply_text("\n".join(lines)[:4000])


@colab_bot.on_message(filters.command("myuu_remove") & filters.private, group=-1)
async def cmd_myuu_remove(client, message):
    if message.chat.id != OWNER:
        return
    args = message.command[1:]
    if not args or not args[0].isdigit():
        await message.reply_text("Usage : <code>/myuu_remove 1</code>")
        return
    eid = int(args[0])
    entries = {e.id: e for e in _WL.all()}
    e = entries.get(eid)
    if not e:
        await message.reply_text(f"❌ #{eid} introuvable.")
        return
    _WL.remove(eid)
    await message.reply_text(f"✅ Retiré de la watchlist : #{eid} — {e.name}")


# ═════════════════════════════════════════════════════════════
# Traitement automatique d'un épisode matché : FreeConvert 480p + 360p
# -> forward_intelligent vers les bons canaux dump
# ═════════════════════════════════════════════════════════════

async def _process_watchlist_hit(name: str, entry) -> None:
    title = get_title(entry)
    video_url = extract_video_url(entry)
    guid = _entry_guid(entry)

    if not video_url or "1fichier.com" in video_url.lower():
        log.warning("⚠️ Aucune source exploitable pour : %s", title)
        await _notify(f"❌ <b>[Unreal Engine 4] {name}</b>\n\nAucune source exploitable (1fichier exclu ou vide).")
        if guid:
            _mark_seen(_store, guid)
            _save_store(_store)
        return

    if not is_video_url(video_url):
        # Transfer.it/Mega : pas de chemin FreeConvert possible pour
        # l'instant (pas d'upload local -> FreeConvert écrit) — on
        # marque vu pour ne pas le retraiter indéfiniment.
        log.warning("⚠️ [Unreal Engine 4] Source Transfer.it/Mega non supportée pour l'instant : %s", title)
        await _notify(
            f"❌ <b>[Unreal Engine 4] {name}</b>\n\nSource Transfer.it/Mega : pas encore "
            "supporté sans upload local vers FreeConvert (fonction pas encore écrite)."
        )
        if guid:
            _mark_seen(_store, guid)
            _save_store(_store)
        return

    status_msg = await _notify(f"🟣 <b>[Unreal Engine 4] {name}</b>\n\n<code>{title}</code>\n\n⏳ Démarrage...")

    job_id = uuid.uuid4().hex[:8]
    job_dir = Path(f"{Paths.temp_cc_path}_unreal4_{job_id}")
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        dump_channels = await get_dump_channels()
        matches = match_dump_channels(title, dump_channels)
        if not matches:
            log.warning("⚠️ Aucun canal dump ne correspond à : %s", name)

        # ── Version d'origine (HD, non compressée) — envoyée en plus des
        # deux sorties FreeConvert, sert de version haute qualité/archive.
        await _notify(f"🟣 <b>[Unreal Engine 4] {name}</b>\n\n📥 Téléchargement HD (source)...", status_msg)
        try:
            hd_path = await asyncio.to_thread(_download_video, video_url, job_dir)
            hd_path = Path(hd_path)
            if check_file_size(hd_path):
                try:
                    hd_path = Path(await asyncio.to_thread(prepare_file, hd_path))
                except Exception:
                    log.exception("❌ [Unreal Engine 4] Validation/réparation HD échouée pour %s", title)
                    hd_path = None
                if hd_path is not None:
                    for channel in matches:
                        try:
                            await colab_bot.send_video(
                                chat_id=channel["id"],
                                video=str(hd_path),
                                caption=f"{title} [HD]",
                            )
                        except Exception:
                            log.exception("❌ Échec envoi HD vers le canal %s", channel.get("name"))
            else:
                log.warning("⚠️ Version HD trop lourde/invalide pour %s, non envoyée.", title)
        except Exception:
            log.exception("❌ [Unreal Engine 4] Échec téléchargement HD pour %s", title)
            await _notify(
                f"⚠️ <b>[Unreal Engine 4] {name}</b>\n\nÉchec du téléchargement HD — "
                "on continue quand même avec la compression 480p/360p...",
                status_msg,
            )

        fc_keys = ",".join(BOT.Options.fc_api_keys)

        for quality, resize in _QUALITY_RESIZE.items():
            await _notify(f"🟣 <b>[Unreal Engine 4] {name}</b>\n\n🗜️ FreeConvert {quality}...", status_msg)
            output_path = await convert_remote_url(
                fc_keys, video_url, title, str(job_dir),
                quality_profile="balanced", resize=resize,
            )
            output_path = Path(output_path)

            if not check_file_size(output_path):
                log.warning("⚠️ Sortie %s invalide/trop lourde pour %s", quality, title)
                continue
            try:
                output_path = Path(await asyncio.to_thread(prepare_file, output_path))
            except Exception:
                log.exception("❌ [Unreal Engine 4] Validation/réparation %s échouée pour %s", quality, title)
                continue

            for channel in matches:
                try:
                    await colab_bot.send_video(
                        chat_id=channel["id"],
                        video=str(output_path),
                        caption=f"{title} [{quality}]",
                    )
                except Exception:
                    log.exception("❌ Échec envoi vers le canal %s", channel.get("name"))

        if guid:
            _mark_processed(_store, guid, title, video_url)
            _save_store(_store)
        try:
            await status_msg.delete()
        except Exception:
            pass
        log.info("✅ [Unreal Engine 4] Traitement terminé : %s", title)

    except Exception as exc:
        log.exception("❌ [Unreal Engine 4] Erreur : %s", title)
        await _notify(f"❌ <b>[Unreal Engine 4] {name}</b>\n\n<code>{exc}</code>", status_msg)
        if guid:
            _mark_seen(_store, guid)
            _save_store(_store)
    finally:
        if job_dir.exists():
            import shutil
            shutil.rmtree(job_dir, ignore_errors=True)


# ═════════════════════════════════════════════════════════════
# Boucle de surveillance — 100% autonome sur la watchlist, pas de menu
# ═════════════════════════════════════════════════════════════

_enabled: bool = False
_task = None


async def _poll_loop() -> None:
    log.info("🟣 Unreal Engine 4 démarré")

    # Bootstrap : au tout premier lancement (historique totalement vide),
    # on ignore le backlog actuel du flux sans rien traiter, pour ne
    # jamais reprendre de vieux épisodes déjà présents au moment de
    # l'activation — même si un anime est ajouté à la watchlist plus tard.
    bootstrap = not _store["seen"] and not _store["processed"]

    while _enabled:
        try:
            expired = _WL.purge_expired()
            for e in expired:
                try:
                    await colab_bot.send_message(
                        OWNER,
                        f"⏳ <b>[Unreal Engine 4]</b> Suivi expiré : <code>{e.name}</code>\n"
                        "Retiré de la watchlist — relance /myuu_add pour continuer à le suivre.",
                    )
                except Exception:
                    pass

            feed = await fetch_feed()
            entries = list(getattr(feed, "entries", []) or [])
            entries.reverse()

            if bootstrap:
                log.info("🛑 [Unreal Engine 4] Premier lancement : backlog actuel ignoré (%d entrée(s))", len(entries))
                for entry in entries:
                    g = _entry_guid(entry)
                    if g:
                        _mark_seen(_store, g)
                _save_store(_store)
                bootstrap = False
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            new_entries = [e for e in entries if (g := _entry_guid(e)) and not _is_known(_store, g)]
            if not new_entries:
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            hardsub_new = []
            for entry in new_entries:
                if is_hardsub(get_title(entry)):
                    hardsub_new.append(entry)
                else:
                    g = _entry_guid(entry)
                    _mark_seen(_store, g)
            _save_store(_store)

            if not hardsub_new:
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            watch_entries = _WL.all()

            groups: dict[str, list] = {}
            for entry in hardsub_new:
                key = episode_key(get_title(entry))
                groups.setdefault(key, []).append(entry)

            for key, group in groups.items():
                def _prio(e):
                    url = extract_video_url(e)
                    return source_priority(url) if url else 999
                group.sort(key=_prio)
                selected = group[0]
                for other in group:
                    if other is not selected:
                        g = _entry_guid(other)
                        if g:
                            _mark_seen(_store, g)
                _save_store(_store)

                title = get_title(selected)
                guid = _entry_guid(selected)
                matched_name = _match_watchlist(title, watch_entries) if watch_entries else None

                if matched_name is None:
                    # Pas sur la watchlist -> ignoré, marqué vu pour ne
                    # jamais le reproposer.
                    if guid:
                        _mark_seen(_store, guid)
                        _save_store(_store)
                    continue

                await _process_watchlist_hit(matched_name, selected)

        except Exception:
            log.exception("❌ [Unreal Engine 4] Erreur pendant le poll")

        await asyncio.sleep(CHECK_INTERVAL)

    log.info("🟣 Unreal Engine 4 arrêté")


@colab_bot.on_message(filters.command("myuu_engine_on") & filters.private, group=-1)
async def cmd_myuu_engine_on(client, message):
    global _enabled, _task
    if message.chat.id != OWNER:
        return
    if _enabled:
        await message.reply_text("🟣 Unreal Engine 4 est déjà actif.")
        return
    _enabled = True
    _task = asyncio.get_event_loop().create_task(_poll_loop())

    watch_count = len(_WL.all())
    await message.reply_text(
        "🟣 Unreal Engine 4 activé — surveillance automatique en cours.\n\n"
        f"📋 {watch_count} anime(s) actuellement suivi(s) sur la watchlist.\n"
        "Utilise /myuu_add pour en ajouter, /myuu_list pour voir la liste."
    )


@colab_bot.on_message(filters.command("myuu_engine_off") & filters.private, group=-1)
async def cmd_myuu_engine_off(client, message):
    global _enabled
    if message.chat.id != OWNER:
        return
    _enabled = False
    await message.reply_text("🟣 Unreal Engine 4 désactivé.")


@colab_bot.on_message(filters.command("unreal_test") & filters.private, group=-1)
async def cmd_unreal_test(client, message):
    """Test manuel du pipeline complet (HD + FreeConvert 480p/360p +
    forward_intelligent), en BYPASSANT la watchlist.

    Usage :
      /unreal_test              -> prend le premier épisode HARDSUB non
                                    encore traité trouvé dans le flux.
      /unreal_test <nom anime>  -> cherche précisément cet anime dans le
                                    flux (matching normalisé, comme la
                                    watchlist), rejouable plusieurs fois
                                    d'affilée même si déjà testé avant.
    """
    if message.chat.id != OWNER:
        return

    query = " ".join(message.command[1:]).strip()

    await message.reply_text("🧪 [Unreal Engine 4] Test RSS tsundere.to en cours...")
    try:
        feed = await fetch_feed()
        entries = list(getattr(feed, "entries", []) or [])
        if not entries:
            await message.reply_text("❌ Aucun élément trouvé dans le flux.")
            return

        if query:
            entry = _find_best_entry_by_name(entries, query)
            if entry is None:
                await message.reply_text(
                    f"❌ Aucun épisode HARDSUB correspondant à <code>{query}</code> "
                    "trouvé dans le flux actuellement."
                )
                return
        else:
            _, entry = _pick_untested_hardsub_entry(entries, _store)
            if entry is None:
                await message.reply_text(
                    "❌ Aucun épisode HARDSUB non traité trouvé dans le flux "
                    "(soit tout est déjà connu, soit rien n'est encore hardsub)."
                )
                return

        title = get_title(entry)
        video_url = extract_video_url(entry)
        if not video_url:
            await message.reply_text(f"❌ Vidéo introuvable.\n\n📺 {title}")
            return

        name = anime_name(title)
        await message.reply_text(
            f"✅ Trouvé : <code>{title}</code>\n\n🔗 {video_url}\n\n"
            "📥 Traitement complet (HD + FreeConvert 480p/360p + forward), "
            "sans passer par la watchlist..."
        )
        await _process_watchlist_hit(name, entry)
        await message.reply_text("✅ Test terminé.")
    except Exception as exc:
        log.exception("Erreur /unreal_test")
        await message.reply_text(f"❌ Erreur : {exc}")
