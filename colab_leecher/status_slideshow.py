"""
colab_leecher/status_slideshow.py

Diaporama d'images sur les messages de statut : purement décoratif — le
message devient une photo avec le texte de progression en légende, et
l'image change toutes les 5s tant que le job tourne. Aucun lien avec le
contenu du job (pas des thumbnails de la vidéo traitée).

Usage : remplace `await colab_bot.send_message(chat_id, text=...)` par
`await StatusSlideshow().start(chat_id, text=...)`. L'objet retourné se
comporte comme un Message Pyrogram pour tout ce qui est déjà utilisé
ailleurs dans le repo (.edit_text(), .delete(), .chat.id, .id) — donc
_fc_job_status(), status_bar(), upload_file() etc. n'ont rien à changer.
"""
from __future__ import annotations

import asyncio
import glob
import logging
import os
import random

from pyrogram.types import InputMediaPhoto

from colab_leecher import colab_bot

log = logging.getLogger(__name__)

_ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets", "status_pics")
_INTERVAL_S = 5


def _pics() -> list[str]:
    return sorted(glob.glob(os.path.join(_ASSETS_DIR, "*.jpg")))


class StatusSlideshow:
    """Message de statut avec image de fond qui tourne toutes les 5s.

    Se comporte comme un pyrogram.types.Message pour le reste du code
    (délégation via __getattr__) — seuls edit_text() et delete() sont
    interceptés pour piloter le diaporama en plus de mettre à jour le
    texte/la légende.
    """

    def __init__(self) -> None:
        self.message = None
        self._task: asyncio.Task | None = None
        self._caption: str = ""
        self._kb = None

    async def start(self, chat_id, text: str, reply_markup=None):
        self._caption = text
        self._kb = reply_markup
        pics = _pics()
        if pics:
            try:
                self.message = await colab_bot.send_photo(
                    chat_id, photo=random.choice(pics),
                    caption=text, reply_markup=reply_markup,
                )
                self._task = asyncio.create_task(self._loop())
                return self
            except Exception as exc:
                log.warning("[Slideshow] send_photo a échoué, repli texte: %s", exc)
        # Pas d'images dispo (ou erreur d'envoi) -> repli sur un message
        # texte classique, comportement identique à avant cette feature.
        self.message = await colab_bot.send_message(chat_id, text=text, reply_markup=reply_markup)
        return self

    async def _loop(self) -> None:
        pics = _pics()
        if len(pics) < 2:
            return
        try:
            while True:
                await asyncio.sleep(_INTERVAL_S)
                pic = random.choice(pics)
                try:
                    self.message = await self.message.edit_media(
                        InputMediaPhoto(pic, caption=self._caption),
                        reply_markup=self._kb,
                    )
                except Exception:
                    # Message supprimé entre-temps, rate limit, etc. — on
                    # continue la boucle, le prochain tick réessaiera.
                    pass
        except asyncio.CancelledError:
            pass

    async def edit_text(self, text: str, reply_markup=None, **kwargs):
        """Compatible avec l'appel Message.edit_text() déjà utilisé partout
        (_fc_job_status, status_bar...) — mais édite la LÉGENDE si le
        message est une photo (diaporama actif)."""
        self._caption = text
        if reply_markup is not None:
            self._kb = reply_markup
        try:
            if getattr(self.message, "photo", None):
                self.message = await self.message.edit_caption(text, reply_markup=self._kb)
            else:
                self.message = await self.message.edit_text(text, reply_markup=self._kb)
        except Exception:
            pass
        return self.message

    async def delete(self):
        if self._task:
            self._task.cancel()
            self._task = None
        try:
            await self.message.delete()
        except Exception:
            pass

    def __getattr__(self, name):
        # Délègue tout le reste (.chat, .id, .from_user, ...) au Message
        # pyrogram réel — garde StatusSlideshow "duck-type compatible"
        # sans avoir à modifier tous les call sites existants.
        return getattr(self.message, name)
