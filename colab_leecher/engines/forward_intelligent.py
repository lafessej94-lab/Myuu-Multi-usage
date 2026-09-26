"""
colab_leecher/engines/forward_intelligent.py

Routage "intelligent" des sorties (HD/480p/360p) d'un épisode vers les
bons canaux dump, en fonction du nom de l'anime — utilisé par
engines/unreal_engine4.py.

Les canaux dump eux-mêmes sont configurés via /add (BOT.Options.dump_ids,
une simple liste d'IDs Telegram — voir colab_leecher/__main__.py et
services/settings_menu.py) : aucun fichier séparé, aucune métadonnée
persistée à part l'ID. Le TITRE de chaque canal est donc récupéré via
l'API Telegram à CHAQUE appel de get_dump_channels() (pas de cache), pour
toujours matcher sur le nom actuel du canal même s'il a été renommé entre
temps.

Convention attendue : nomme chaque canal dump d'après l'anime qu'il doit
recevoir (ex un canal "One Piece" pour recevoir les épisodes de One
Piece). Le matching est volontairement souple (normalisation + inclusion
dans les deux sens, même principe que le matching de la watchlist dans
unreal_engine4.py) pour tolérer les variations de casse/ponctuation.

Fallback : si AUCUN canal ne matche le nom de l'anime, cette version
renvoie TOUS les canaux dump configurés plutôt que de perdre
silencieusement la sortie — mieux vaut un envoi dans le mauvais canal
qu'un épisode jamais livré nulle part. Si tu préfères ne RIEN envoyer
dans ce cas (routage strict), dis-le-moi.
"""
from __future__ import annotations

import logging
import re

from colab_leecher import colab_bot
from colab_leecher.utility.variables import BOT

log = logging.getLogger(__name__)


def _normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


async def get_dump_channels() -> list[dict]:
    """Renvoie [{"id": int, "name": str}, ...] pour chaque canal dump
    configuré (BOT.Options.dump_ids), en récupérant le titre actuel via
    l'API Telegram à chaque appel.

    Si le titre est introuvable (canal supprimé, bot pas admin, etc.),
    le canal est quand même inclus avec son ID en guise de nom, pour ne
    pas le faire disparaître silencieusement du routage."""
    channels: list[dict] = []
    for chat_id in list(getattr(BOT.Options, "dump_ids", []) or []):
        name = str(chat_id)
        try:
            chat = await colab_bot.get_chat(chat_id)
            name = chat.title or getattr(chat, "first_name", None) or str(chat_id)
        except Exception:
            log.warning("⚠️ Impossible de récupérer le titre du canal dump %s", chat_id)
        channels.append({"id": chat_id, "name": name})
    return channels


def match_dump_channels(title: str, channels: list[dict]) -> list[dict]:
    """Renvoie les canaux dont le nom matche (normalisé, inclusion dans
    les deux sens) le titre de l'épisode. Si aucun ne matche, renvoie
    TOUS les canaux configurés (fallback — voir docstring du module)."""
    if not channels:
        return []

    candidate = _normalize(title)
    matches: list[dict] = []
    if candidate:
        for channel in channels:
            norm_name = _normalize(channel.get("name", ""))
            if not norm_name:
                continue
            if norm_name in candidate or candidate in norm_name:
                matches.append(channel)

    if matches:
        return matches

    log.warning(
        "⚠️ Aucun canal dump ne correspond à %r — fallback : envoi à tous les canaux dump configurés.",
        title,
    )
    return channels
