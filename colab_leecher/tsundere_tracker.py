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

Filtre HARDSUB uniquement : le flux RSS (RSS_URL dans engines/tsundere_rss.py)
filtre déjà par langue (FRENCH / SUBFRENCH / MULTI) mais renvoie aussi bien
du softsub (piste de sous-titres séparée) que du hardsub (sous-titres
incrustés). Le tracker ignore toute release dont le titre ne contient pas
"HARDSUB" — elle est marquée "seen" (pour ne pas être réévaluée à chaque
poll) mais jamais traitée/envoyée. Si la version hardsub du même épisode
sort ensuite (guid différent), elle sera traitée normalement au poll
suivant. Ça évite d'envoyer la version softsub en premier, puis un doublon
quand le hardsub arrive.

Historique JSON (data/tsundere_processed.json) à deux clés :
  - "processed" : guid -> {title, url, at} — épisode réellement envoyé.
  - "seen"      : guid -> timestamp — guid déjà rencontré dans le flux,
    qu'il ait été traité avec succès, ignoré (softsub) ou échoué. Sert à
    ne JAMAIS retraiter un guid déjà vu, y compris après un redémarrage du
    bot — contrairement au script d'origine, dont le "premier passage"
    ignorait en bloc tout ce qui se trouvait dans le flux à CHAQUE
    démarrage (pas seulement le tout premier), ce qui pouvait faire perdre
    silencieusement un épisode sorti pile au moment d'un redémarrage. Ici,
    le flag "premier passage" ne se déclenche que si l'historique est
    totalement vide (aucun guid jamais vu).
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
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from colab_leecher import OWNER, colab_bot
from colab_leecher.engines.tsundere_rss import (
    CHECK_INTERVAL,
    check_file_size,
    episode_key,
    extract_video_url,
    fetch_feed,
    get_title,
    is_hardsub,
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


def _pick_untested_hardsub_entry(entries, store: dict):
    """Sélectionne, dans les entrées du flux (ordre brut, plus récent en
    premier), la première release HARDSUB pas encore connue (ni "seen" ni
    "processed"). Utilisée par /tsundere_test pour ne JAMAIS tomber sur une
    version softsub que le tracker automatique ignorerait de toute façon —
    c'est cette divergence qui causait le double téléchargement : le test
    prenait entries[0] à l'aveugle (parfois softsub), pendant que le
    tracker traitait en parallèle la vraie version hardsub avec un guid
    différent, sous un autre nom de fichier."""
    for entry in entries:
        guid = _entry_guid(entry) or get_title(entry)
        if _is_known(store, guid):
            continue
        if not is_hardsub(get_title(entry)):
            continue
        return guid, entry
    return None, None


# ═════════════════════════════════════════════════════════════
# Verrou anti-doublon par épisode
# ═════════════════════════════════════════════════════════════
#
# Empêche de traiter deux fois le MÊME épisode en parallèle (softsub et
# hardsub d'un même épisode ont des guids différents mais la même
# episode_key). Ce cas-là n'a pas besoin de demander à l'owner : le
# hardsub gagne toujours, silencieusement, comme le fait déjà le tri par
# source_priority. Skip silencieux, pas de bouton.

_processing_keys: set[str] = set()


# ═════════════════════════════════════════════════════════════
# File d'attente des téléchargements + choix de priorité par boutons
# ═════════════════════════════════════════════════════════════
#
# Un seul téléchargement/upload à la fois (bande passante et disque
# partagés sur Colab). Si une deuxième tâche (test manuel OU poll
# automatique) arrive alors qu'une autre tourne déjà — ou qu'une autre
# attend déjà — au lieu de la lancer en parallèle ou de choisir un ordre
# silencieusement, on envoie à l'owner une liste avec un bouton par
# épisode en attente ("Anime 1", "Anime 2", ...) pour qu'il choisisse
# lequel passer en premier une fois le slot libéré. Sans réponse, l'ordre
# d'arrivée (FIFO) s'applique par défaut.
#
# Dans le cas courant (un seul épisode à la fois, ce qui est la quasi
# totalité du temps), aucun message n'est envoyé : le slot est libre, la
# tâche démarre immédiatement, exactement comme avant.

_active_title: str | None = None
_waiting_jobs: dict[str, "_WaitingJob"] = {}


class _WaitingJob:
    __slots__ = ("token", "title", "event", "priority")

    def __init__(self, title: str):
        self.token = uuid.uuid4().hex[:8]
        self.title = title
        self.event = asyncio.Event()
        self.priority = False  # True une fois choisi par l'owner via bouton


async def _acquire_download_slot(title: str) -> "_WaitingJob | None":
    """Bloque jusqu'à ce que ce soit le tour de `title`. Retourne le
    _WaitingJob créé (à repasser à _release_download_slot), ou None si le
    slot était libre immédiatement — cas normal, pas de file à gérer."""
    global _active_title

    if _active_title is None and not _waiting_jobs:
        _active_title = title
        return None

    job = _WaitingJob(title)
    _waiting_jobs[job.token] = job
    log.info("⏸️ Mis en attente (slot occupé par « %s ») : %s", _active_title, title)
    await _ask_priority()

    await job.event.wait()
    _active_title = title
    return job


def _release_download_slot(job: "_WaitingJob | None") -> None:
    """À appeler dans le `finally` du téléchargement/upload. Libère le
    slot et réveille le prochain job en attente : celui marqué priority
    (choisi via bouton) en premier, sinon le plus ancien arrivé (FIFO)."""
    global _active_title
    _active_title = None

    if job is not None:
        _waiting_jobs.pop(job.token, None)

    if not _waiting_jobs:
        return

    next_job = min(
        _waiting_jobs.values(),
        key=lambda j: (0 if j.priority else 1, j.token),
    )
    next_job.event.set()


async def _ask_priority() -> None:
    """Envoie un message avec un bouton par épisode actuellement en
    attente, pour que l'owner choisisse lequel traiter en priorité dès que
    le téléchargement en cours se termine. N'est appelé que lors d'une
    vraie collision (voir _acquire_download_slot)."""
    jobs = list(_waiting_jobs.values())
    if not jobs:
        return

    lines = ["⏸️ <b>Plusieurs épisodes prêts en même temps.</b>"]
    if _active_title:
        lines.append(f"▶️ En cours : <code>{_active_title}</code>")
    lines.append("")
    lines.append("Choisis lequel traiter en priorité ensuite (sinon, ordre d'arrivée) :")

    buttons = []
    for i, job in enumerate(jobs, start=1):
        lines.append(f"{i}️⃣ <code>{job.title}</code>")
        buttons.append([InlineKeyboardButton(f"Anime {i}", callback_data=f"tsundere_pick:{job.token}")])

    try:
        await colab_bot.send_message(
            chat_id=OWNER,
            text="\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    except Exception:
        log.exception("❌ Impossible d'envoyer le choix de priorité")


@colab_bot.on_callback_query(filters.regex(r"^tsundere_pick:"))
async def cb_tsundere_pick(client, callback_query):
    if callback_query.from_user.id != OWNER:
        await callback_query.answer("Pas autorisé.", show_alert=True)
        return

    token = callback_query.data.split(":", 1)[1]
    job = _waiting_jobs.get(token)
    if job is None:
        await callback_query.answer("Cet épisode n'est plus en attente.", show_alert=True)
        return

    job.priority = True
    await callback_query.answer(f"✅ Priorité donnée : {job.title}"[:200])
    try:
        await callback_query.message.edit_text(
            f"✅ <b>Priorité donnée</b>\n\n<code>{job.title}</code>\n\n"
            "Sera traité juste après le téléchargement en cours."
        )
    except Exception:
        pass


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


async def _process_entry(guid: str, entry, store: dict, *, _lock: bool = True) -> None:
    """_lock=False : utilisé uniquement par _try_alternative_source(), qui
    est déjà appelée depuis un contexte où la clé de l'épisode est verrouillée
    (elle réessaie une autre source pour le MÊME épisode) — reposer le
    verrou ici la ferait échouer à tort (elle se verrait déjà "prise")."""
    title = get_title(entry)
    key = episode_key(title)

    if _lock:
        if key in _processing_keys:
            log.info("⏭️ Épisode déjà en cours de traitement ailleurs, skip : %s", title)
            return
        _processing_keys.add(key)

    try:
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

        # File d'attente : bloque ici si un autre téléchargement tourne
        # déjà. En cas de collision (autre chose déjà en attente en même
        # temps), _acquire_download_slot envoie les boutons de choix à
        # l'owner. Cas normal (rien d'autre en attente) : retour immédiat.
        slot_job = await _acquire_download_slot(title)
        if slot_job is not None:
            log.info("▶️ Tour de : %s", title)

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
            _release_download_slot(slot_job)
    finally:
        if _lock:
            _processing_keys.discard(key)


async def _try_alternative_source(title: str, guid: str, store: dict) -> bool:
    """Recherche, dans le flux courant, une autre publication du même
    épisode (même episode_key) dont la source n'a pas déjà été essayée.
    Ne considère que des candidates HARDSUB, pour rester cohérent avec le
    filtre appliqué en amont dans _poll_loop()."""
    try:
        feed = await fetch_feed()
        current_key = episode_key(title)
        candidates = []

        for other in getattr(feed, "entries", []) or []:
            other_title = get_title(other)
            if episode_key(other_title) != current_key:
                continue
            if not is_hardsub(other_title):
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
        await _process_entry(selected_guid, selected_entry, store, _lock=False)
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

            # Filtre HARDSUB uniquement : les releases softsub (sans
            # "hardsub" dans le titre) sont marquées vues mais jamais
            # traitées — voir docstring en tête de fichier.
            hardsub_entries = []
            for guid, entry in new_entries:
                title = get_title(entry)
                if is_hardsub(title):
                    hardsub_entries.append((guid, entry))
                else:
                    log.info("⏭️ Softsub ignoré (attente HARDSUB) : %s", title)
                    _mark_seen(store, guid)
            _save_store(store)

            if not hardsub_entries:
                await asyncio.sleep(CHECK_INTERVAL)
                continue

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

        store = _load_store()

        # ⚠️ Avant : `entry = entries[0]` sans filtre HARDSUB ni check
        # _is_known -> pouvait tomber sur une release softsub que le
        # tracker automatique ignore de son côté, pendant que celui-ci
        # traitait en parallèle la vraie version hardsub (guid différent,
        # même épisode) -> téléchargement en double.
        # Maintenant : même logique de sélection que le tracker (HARDSUB
        # uniquement, jamais un guid déjà connu).
        guid, entry = _pick_untested_hardsub_entry(entries, store)
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
        await message.reply_text(f"✅ Trouvé : {title}\n\n🔗 {video_url}\n\n📥 Traitement...")
        await _process_entry(guid, entry, store)
        await message.reply_text("✅ Test terminé.")
    except Exception as exc:
        log.exception("Erreur /tsundere_test")
        await message.reply_text(f"❌ Erreur : {exc}")


# Démarrage automatique — toujours actif, pas besoin de commande.
_ensure_tracker()
