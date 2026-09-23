"""
colab_leecher/utility/handler/leech.py

Le pipeline d'upload final (Leech), la réécriture du tag title du
conteneur, et la logique de sticker de fin de batch.

NOTE : _BatchJobTracker / _batch_job / _maybe_send_batch_sticker sont
repris tels quels de l'ancien handler.py, mais je ne vois aucun appel à
`async with _batch_job():` dans le reste du code partagé jusqu'ici — à
vérifier avant de considérer ce bloc comme actif (peut-être branché
ailleurs, ou vestige).

Extrait de handler.py — voir __init__.py de ce package pour la liste
complète des réexports.
"""
from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import random
import shutil
from contextlib import asynccontextmanager
from os import makedirs, path as ospath
from time import time

from natsort import natsorted
from pyrogram import raw

from colab_leecher import colab_bot
from colab_leecher.services.subtitle_probe import run_tracked_process
from colab_leecher.smart_rename import build_final_name
from colab_leecher.uploader.telegram import upload_file
from colab_leecher.utility.converters import sizeChecker, videoConverter
from colab_leecher.utility.helper import (
    fileType, getSize, keyboard, shortFileName, sysINFO,
)
from colab_leecher.utility.variables import (
    BOT, MSG, BotTimes, Messages, Paths, TaskInfo, Transfer,
)

log = logging.getLogger(__name__)

BATCH_STICKER_PACK_SHORT_NAME = "Cosmic_Princess_Kaguya_Pack2"

_sticker_pack_cache: list | None = None


async def _get_sticker_pack_documents() -> list:
    """Récupère (et met en cache pour la durée de vie du process) les
    documents du pack de stickers de fin de batch. Un seul appel réseau
    au total -- le pack ne change pas en cours de route."""
    global _sticker_pack_cache
    if _sticker_pack_cache is not None:
        return _sticker_pack_cache
    try:
        result = await colab_bot.invoke(
            raw.functions.messages.GetStickerSet(
                stickerset=raw.types.InputStickerSetShortName(
                    short_name=BATCH_STICKER_PACK_SHORT_NAME
                ),
                hash=0,
            )
        )
        _sticker_pack_cache = list(result.documents)
    except Exception as exc:
        log.warning(
            "Impossible de charger le pack de stickers %s: %s",
            BATCH_STICKER_PACK_SHORT_NAME, exc,
        )
        _sticker_pack_cache = []
    return _sticker_pack_cache


async def _send_batch_sticker_to(chat_id) -> None:
    documents = await _get_sticker_pack_documents()
    if not documents:
        return
    doc = random.choice(documents)
    try:
        peer = await colab_bot.resolve_peer(chat_id)
        input_doc = raw.types.InputDocument(
            id=doc.id, access_hash=doc.access_hash, file_reference=doc.file_reference,
        )
        await colab_bot.invoke(
            raw.functions.messages.SendMedia(
                peer=peer,
                media=raw.types.InputMediaDocument(id=input_doc),
                message="",
                random_id=random.randint(-(2**63), 2**63 - 1),
            )
        )
    except Exception as exc:
        log.warning("Envoi du sticker de fin de batch échoué (chat %s): %s", chat_id, exc)


async def _maybe_send_batch_sticker() -> None:
    """N'envoie le sticker QUE si l'auto-forward est actif -- cette
    fonctionnalité est rattachée au forward, pas indépendante. Envoyé dans
    le(s) salon(s) de dump, jamais dans le chat principal."""
    if not BOT.Options.auto_forward or not BOT.Options.dump_ids:
        return
    for dump_target in list(BOT.Options.dump_ids):
        await _send_batch_sticker_to(dump_target)


class _BatchJobTracker:
    """Compte les jobs FC/CC/local (hardsub, convert, resize, compress,
    tous les Local_* ffmpeg) actuellement en cours, tous types confondus
    dans un seul groupe commun. Le DERNIER à se terminer (le compteur
    retombe à 0) déclenche l'envoi du sticker de fin de batch."""

    def __init__(self):
        self._active = 0
        self._lock = asyncio.Lock()

    async def enter(self) -> None:
        async with self._lock:
            self._active += 1

    async def exit(self) -> None:
        async with self._lock:
            self._active = max(0, self._active - 1)
            is_last = self._active == 0
        if is_last:
            await _maybe_send_batch_sticker()


_batch_tracker = _BatchJobTracker()


@asynccontextmanager
async def _batch_job():
    """À utiliser en 'async with _batch_job():' autour du corps complet
    d'un handler FC/CC/local."""
    await _batch_tracker.enter()
    try:
        yield
    finally:
        await _batch_tracker.exit()


async def _rewrite_video_title(path: str, title: str) -> None:
    """Réécrit le tag "title" des métadonnées du conteneur pour qu'il
    corresponde au nom final smart_rename, en simple REMUX (-c copy,
    aucun ré-encodage)."""
    ext = ospath.splitext(path)[1]
    tmp_path = f"{path}.retitled.tmp{ext}"
    try:
        await run_tracked_process(
            [
                "ffmpeg", "-y",
                "-i", path,
                "-map", "0",
                "-c", "copy",
                "-metadata", f"title={title}",
                tmp_path,
            ],
            "ffmpeg-retitle",
        )
    except Exception as exc:
        log.warning("Rewrite du titre conteneur échoué pour %s: %s", path, exc)
        if ospath.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        return

    if ospath.exists(tmp_path) and ospath.getsize(tmp_path) > 0:
        os.replace(tmp_path, path)
    elif ospath.exists(tmp_path):
        os.remove(tmp_path)


async def Leech(folder_path: str, remove: bool, convert_videos: bool = True, status_msg=None):
    """
    status_msg optionnel : si fourni (cas des jobs FreeConvert concurrents),
    on édite UNIQUEMENT ce message local, sans jamais toucher au
    MSG.status_msg global. Si absent, comportement historique inchangé.
    """
    is_global = status_msg is None
    target_msg = status_msg or MSG.status_msg

    files = [str(p) for p in pathlib.Path(folder_path).glob("**/*") if p.is_file()]
    if not files:
        raise RuntimeError(f"No files were produced in {folder_path}.")
    for f in natsorted(files):
        fp = ospath.join(folder_path, f)
        if convert_videos and BOT.Options.convert_video and fileType(fp) == "video":
            await videoConverter(fp)

    Transfer.total_down_size = getSize(folder_path)

    files = natsorted([str(p) for p in pathlib.Path(folder_path).glob("**/*") if p.is_file()])
    upload_queue = []

    for f in files:
        file_path = ospath.join(folder_path, f)
        leech = await sizeChecker(file_path, remove)
        if leech:
            if ospath.exists(file_path) and remove:
                os.remove(file_path)
            for part in natsorted(os.listdir(Paths.temp_zpath)):
                upload_queue.append(("split", ospath.join(Paths.temp_zpath, part)))
        else:
            upload_queue.append(("single", file_path))

    total_uploads = len(upload_queue)
    if total_uploads == 0:
        raise RuntimeError("Nothing to upload after processing.")
    split_cleaned = False

    for idx, (kind, file_path) in enumerate(upload_queue):
        is_last = (idx == total_uploads - 1)

        TaskInfo.set(
            phase="upload", engine="Pyrofork",
            filename=ospath.basename(file_path),
        )

        if kind == "split":
            file_name = ospath.basename(file_path)
            new_path = shortFileName(file_path)
            os.rename(file_path, new_path)
            BotTimes.current_time = time()
            Messages.status_head = (
                f"📤 <b>UPLOADING</b>  <i>{idx+1} / {total_uploads}</i>\n\n"
                f"<code>{file_name}</code>\n"
            )
            try:
                edited = await target_msg.edit_text(
                    text=Messages.task_msg + Messages.status_head
                    + "\n⏳ <i>Starting...</i>" + sysINFO(),
                    reply_markup=keyboard(),
                )
                target_msg = edited
                if is_global:
                    MSG.status_msg = edited
            except Exception:
                pass
            await upload_file(new_path, file_name, is_last=is_last, status_msg=target_msg)
            Transfer.up_bytes.append(os.stat(new_path).st_size)
            if is_last and not split_cleaned:
                if ospath.exists(Paths.temp_zpath):
                    shutil.rmtree(Paths.temp_zpath)
                split_cleaned = True
        else:
            if not ospath.exists(Paths.temp_files_dir):
                makedirs(Paths.temp_files_dir)
            if not remove:
                file_path = shutil.copy(file_path, Paths.temp_files_dir)
            file_name = ospath.basename(file_path)
            new_path = shortFileName(file_path)
            os.rename(file_path, new_path)

            if BOT.Options.custom_name:
                out_ext = ospath.splitext(new_path)[1]
                has_ext = bool(ospath.splitext(BOT.Options.custom_name)[1])
                upload_name = BOT.Options.custom_name if has_ext else f"{BOT.Options.custom_name}{out_ext}"
            elif fileType(new_path) == "video":
                upload_name = build_final_name(file_name)
            else:
                upload_name = file_name

            if upload_name != ospath.basename(new_path):
                renamed_path = ospath.join(ospath.dirname(new_path), upload_name)
                try:
                    os.replace(new_path, renamed_path)
                    new_path = renamed_path
                except OSError as exc:
                    log.warning(
                        "Impossible de renommer %s -> %s (%s), envoi sous le nom réel.",
                        new_path, renamed_path, exc,
                    )

            if fileType(new_path) == "video":
                await _rewrite_video_title(new_path, ospath.splitext(upload_name)[0])

            BotTimes.current_time = time()
            Messages.status_head = f"📤 <b>UPLOADING</b>\n\n<code>{upload_name}</code>\n"
            try:
                edited = await target_msg.edit_text(
                    text=Messages.task_msg + Messages.status_head
                    + "\n⏳ <i>Starting...</i>" + sysINFO(),
                    reply_markup=keyboard(),
                )
                target_msg = edited
                if is_global:
                    MSG.status_msg = edited
            except Exception:
                pass
            file_size = os.stat(new_path).st_size
            await upload_file(new_path, upload_name, is_last=is_last, status_msg=target_msg)
            Transfer.up_bytes.append(file_size)
            if remove and ospath.exists(new_path):
                os.remove(new_path)
            elif not remove:
                for fi in os.listdir(Paths.temp_files_dir):
                    os.remove(ospath.join(Paths.temp_files_dir, fi))

    if remove and ospath.exists(folder_path):
        shutil.rmtree(folder_path)
    if is_global:
        for d in (Paths.thumbnail_ytdl, Paths.temp_files_dir):
            if ospath.exists(d):
                shutil.rmtree(d)
