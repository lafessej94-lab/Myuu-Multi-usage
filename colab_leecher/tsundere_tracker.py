"""
colab_leecher/tsundere_tracker.py

Surveillance du flux RSS tsundere.to pour myuu.

Contrairement à la version d'origine, ce module NE démarre PLUS tout seul
à l'import (plus de boucle de fond continue par défaut). Il ne fait
quelque chose que sur commande explicite :

  /online_tsundere  -> récupère le flux UNE FOIS, affiche la liste des
                        animes HARDSUB dispo (dédupliqués, sans tags
                        d'épisode) avec un bouton par anime (sélection
                        multiple), plus Done/Cancel. La liste est figée :
                        elle ne se rafraîchit pas pendant que le menu est
                        ouvert.
  /off_tsundere     -> annule toute sélection en cours et repasse le
                        tracker à OFF.

Le travail (parsing RSS, extraction de source, téléchargement, validation)
vit dans colab_leecher/engines/tsundere_rss.py — ce module-ci ne fait que
l'orchestration Telegram.

Filtre HARDSUB uniquement : le flux RSS renvoie aussi bien du softsub
(piste de sous-titres séparée) que du hardsub (sous-titres incrustés).
Seules les releases HARDSUB apparaissent dans la liste de sélection.

Historique JSON (data/tsundere_processed.json) à deux clés :
  - "processed" : guid -> {title, url, at} — épisode réellement envoyé.
  - "seen"      : guid -> timestamp — guid déjà rencontré, traité avec
    succès ou non. Sert à ne jamais retraiter un guid déjà vu.

NOTE : les fonctions _poll_loop()/_ensure_tracker() de la version d'origine
(boucle de surveillance continue + auto-traitement de tout ce qui sort)
sont conservées ci-dessous mais NE SONT PLUS appelées automatiquement.
Elles restent disponibles si tu veux un jour relancer un mode 100%
automatique en plus du menu manuel — dis-moi si c'est ce que tu veux pour
le moteur Unreal Engine 4, ou si celui-ci doit avoir sa propre boucle
séparée (c'est ce que j'ai fait dans engines/unreal_engine4.py).
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
    anime_name,
    check_file_size,
    episode_key,
    extract_video_url,
    fetch_feed,
    get_title,
    is_hardsub,
    list_available_animes,
    prepare_file,
    source_priority,
)
from colab_leecher.engines.tsundere_rss import download_video as _download_video
from colab_leecher.utility.handler import Leech
from colab_leecher.utility.variables import Paths

# À VÉRIFIER : nom réel de la fonction de compression dans
# engines/freeconvert.py — deviné par analogie avec cloudconvert.py
# (convert_file/resize_file/compress_file). Montre-moi ce fichier pour
# que je corrige l'import si besoin.
from colab_leecher.engines.freeconvert import compress_file

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
# Notification (DM owner)
# ═════════════════════════════════════════════════════════════

async def _notify(text: str, msg=None):
    """Crée ou édite un message de statut dans le DM du owner."""
    try:
        if msg is None:
            return await colab_bot.send_message(chat_id=OWNER, text=text)
        return await msg.edit_text(text)
    except Exception:
        return msg


# ═════════════════════════════════════════════════════════════
# Toggle + session de sélection manuelle (/online_tsundere)
# ═════════════════════════════════════════════════════════════

_online: bool = False


class _SelectSession:
    __slots__ = ("names", "entries_by_name", "selected", "message")

    def __init__(self, names: list[str], entries_by_name: dict):
        self.names = names
        self.entries_by_name = entries_by_name  # anime_name -> entry (meilleure source trouvée)
        self.selected: set[str] = set()
        self.message = None


_session: "_SelectSession | None" = None


def _build_menu_markup(session: "_SelectSession") -> InlineKeyboardMarkup:
    rows = []
    for name in session.names:
        checked = "✅" if name in session.selected else "⬜"
        rows.append([InlineKeyboardButton(f"{checked} {name}", callback_data=f"tsundere_toggle:{name}")])
    rows.append([
        InlineKeyboardButton("✅ Done", callback_data="tsundere_select_done"),
        InlineKeyboardButton("❌ Cancel", callback_data="tsundere_select_cancel"),
    ])
    return InlineKeyboardMarkup(rows)


def _menu_text(session: "_SelectSession") -> str:
    return (
        "🍥 <b>Animes disponibles sur tsundere.to</b>\n\n"
        "Sélectionne un ou plusieurs animes à récupérer, puis Done "
        "(ou Cancel pour ne rien faire).\n\n"
        f"Sélectionnés : <b>{len(session.selected)}</b>"
    )


@colab_bot.on_message(filters.command("online_tsundere") & filters.private, group=-1)
async def cmd_online_tsundere(client, message):
    global _online, _session
    if message.chat.id != OWNER:
        return

    _online = True

    await message.reply_text("📡 Récupération du flux tsundere.to...")
    try:
        feed = await fetch_feed()
    except Exception as exc:
        log.exception("❌ Erreur /online_tsundere pendant fetch_feed")
        await message.reply_text(f"❌ Impossible de récupérer le flux : {exc}")
        return

    entries = list(getattr(feed, "entries", []) or [])
    names = list_available_animes(entries)

    if not names:
        await message.reply_text("❌ Aucun anime HARDSUB disponible dans le flux actuellement.")
        return

    entries_by_name: dict[str, object] = {}
    for entry in entries:
        title = get_title(entry)
        if not is_hardsub(title):
            continue
        name = anime_name(title)
        if name not in entries_by_name:
            entries_by_name[name] = entry
        else:
            current_url = extract_video_url(entries_by_name[name])
            candidate_url = extract_video_url(entry)
            if candidate_url and (
                not current_url or source_priority(candidate_url) < source_priority(current_url)
            ):
                entries_by_name[name] = entry

    _session = _SelectSession(names, entries_by_name)
    markup = _build_menu_markup(_session)
    _session.message = await message.reply_text(_menu_text(_session), reply_markup=markup)


@colab_bot.on_message(filters.command("off_tsundere") & filters.private, group=-1)
async def cmd_off_tsundere(client, message):
    global _online, _session
    if message.chat.id != OWNER:
        return

    _online = False

    if _session is not None and _session.message is not None:
        try:
            await _session.message.edit_text("🛑 Tracker tsundere désactivé, sélection annulée.")
        except Exception:
            pass
    _session = None

    await message.reply_text("🛑 tsundere_rss est maintenant OFF.")


@colab_bot.on_callback_query(filters.regex(r"^tsundere_toggle:"))
async def cb_tsundere_toggle(client, callback_query):
    global _session
    if callback_query.from_user.id != OWNER or _session is None:
        await callback_query.answer()
        return

    name = callback_query.data.split(":", 1)[1]
    if name in _session.selected:
        _session.selected.discard(name)
    else:
        _session.selected.add(name)

    try:
        await callback_query.message.edit_text(_menu_text(_session), reply_markup=_build_menu_markup(_session))
    except Exception:
        pass
    await callback_query.answer()


@colab_bot.on_callback_query(filters.regex(r"^tsundere_select_cancel$"))
async def cb_tsundere_select_cancel(client, callback_query):
    global _session
    if callback_query.from_user.id != OWNER:
        await callback_query.answer()
        return
    _session = None
    try:
        await callback_query.message.edit_text("❌ Sélection annulée, rien n'a été lancé.")
    except Exception:
        pass
    await callback_query.answer()


@colab_bot.on_callback_query(filters.regex(r"^tsundere_select_done$"))
async def cb_tsundere_select_done(client, callback_query):
    global _session
    if callback_query.from_user.id != OWNER or _session is None:
        await callback_query.answer()
        return

    if not _session.selected:
        await callback_query.answer("Rien de sélectionné — choisis un anime ou clique Cancel.", show_alert=True)
        return

    chosen = [(name, _session.entries_by_name[name]) for name in _session.selected]
    try:
        await callback_query.message.edit_text(
            "⏳ Traitement de " + ", ".join(n for n, _ in chosen) + " ..."
        )
    except Exception:
        pass
    await callback_query.answer()

    _session = None

    for name, entry in chosen:
        await _process_selected_anime(name, entry)


async def _process_selected_anime(name: str, entry) -> None:
    """Téléchargement + compression FreeConvert simple (pas de hardsub
    burn, la source tsundere.to est déjà hardsub) + upload normal via
    Leech (forward vers les dumps configurés par /add, comme d'habitude)."""
    title = get_title(entry)
    video_url = extract_video_url(entry)

    if not video_url or "1fichier.com" in video_url.lower():
        await _notify(f"❌ <b>{name}</b>\n\nAucune source exploitable (1fichier exclu ou vide).")
        return

    status_msg = await _notify(f"🍥 <b>{name}</b>\n\n<code>{title}</code>\n\n⏳ Téléchargement...")

    job_id = uuid.uuid4().hex[:8]
    job_dir = Path(f"{Paths.temp_cc_path}_tsundere_manual_{job_id}")

    try:
        file_path = await asyncio.to_thread(_download_video, video_url, job_dir)

        if not check_file_size(file_path):
            raise RuntimeError("Fichier trop volumineux ou invalide.")

        prepared = await asyncio.to_thread(prepare_file, file_path)

        await _notify(f"🍥 <b>{name}</b>\n\n🗜️ Compression FreeConvert...", status_msg)
        compressed_path = await asyncio.to_thread(compress_file, prepared)

        await _notify(f"🍥 <b>{name}</b>\n\n📤 Envoi vers Telegram...", status_msg)
        await Leech(str(compressed_path.parent), True, convert_videos=False, status_msg=status_msg)

        try:
            await status_msg.delete()
        except Exception:
            pass
        log.info("✅ Traitement manuel terminé : %s", name)

    except Exception as exc:
        log.exception("❌ Erreur traitement manuel : %s", name)
        await _notify(f"❌ <b>{name}</b>\n\n<code>{exc}</code>", status_msg)
    finally:
        if job_dir.exists():
            import shutil
            shutil.rmtree(job_dir, ignore_errors=True)


# ═════════════════════════════════════════════════════════════
# Ancien mode 100% automatique (conservé, non démarré par défaut)
# ═════════════════════════════════════════════════════════════
#
# Ces fonctions restent utilisables (par ex. appelées manuellement, ou
# reliées à une future commande) mais rien ne les déclenche plus tout
# seul à l'import — voir la note en haut de fichier.

_processing_keys: set[str] = set()
_active_title: str | None = None
_waiting_jobs: dict[str, "_WaitingJob"] = {}


class _WaitingJob:
    __slots__ = ("token", "title", "event", "priority")

    def __init__(self, title: str):
        self.token = uuid.uuid4().hex[:8]
        self.title = title
        self.event = asyncio.Event()
        self.priority = False


async def _acquire_download_slot(title: str) -> "_WaitingJob | None":
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
    global _active_title
    _active_title = None
    if job is not None:
        _waiting_jobs.pop(job.token, None)
    if not _waiting_jobs:
        return
    next_job = min(_waiting_jobs.values(), key=lambda j: (0 if j.priority else 1, j.token))
    next_job.event.set()


async def _ask_priority() -> None:
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
        await colab_bot.send_message(chat_id=OWNER, text="\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))
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
            f"✅ <b>Priorité donnée</b>\n\n<code>{job.title}</code>\n\nSera traité juste après le téléchargement en cours."
        )
    except Exception:
        pass


def _pick_untested_hardsub_entry(entries, store: dict):
    for entry in entries:
        guid = _entry_guid(entry) or get_title(entry)
        if _is_known(store, guid):
            continue
        if not is_hardsub(get_title(entry)):
            continue
        return guid, entry
    return None, None


async def _process_entry(guid: str, entry, store: dict, *, _lock: bool = True) -> None:
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


async def _poll_loop() -> None:
    await asyncio.sleep(15)
    log.info("📡 Surveillance RSS tsundere.to démarrée")
    store = _load_store()
    bootstrap = not store["seen"] and not store["processed"]

    while True:
        try:
            feed = await fetch_feed()
            entries = list(getattr(feed, "entries", []) or [])
            entries.reverse()
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

            new_entries = [(g, e) for e in entries if (g := _entry_guid(e)) and not _is_known(store, g)]
            if not new_entries:
                await asyncio.sleep(CHECK_INTERVAL)
                continue

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


# Plus de démarrage automatique ici — /online_tsundere déclenche le menu
# manuel ; _ensure_tracker() reste dispo si besoin d'un mode 100% auto.
