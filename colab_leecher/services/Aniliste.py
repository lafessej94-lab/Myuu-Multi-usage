"""
services/Aniliste.py — Commande /anime pour myuu
Recherche d'animes sur AniList (GraphQL, sans clé) : menu de sélection
inline, puis envoi de la fiche formatée (photo + caption).

Remplace entièrement l'ancien trio anime_search.py + nautilijan.py +
my_anime_liste.py :
- nautilijan.py (scraping Nautiljon, jamais validé en conditions réelles)
  et my_anime_liste.py (Jikan/MAL, erreurs 504 systématiques — même
  problème déjà rencontré et réglé sur le site en repassant sur AniList)
  sont supprimés.
- Les deux commandes /Naut_Anime et /Mal_Anime sont supprimées et
  remplacées par une seule commande /anime.

Indépendant du pipeline hardsub — feature du repo myuu.

Suit le même pattern que colab_leecher/nyaa_tracker.py : ce module importe
l'instance partagée `colab_bot` et s'auto-enregistre via ses décorateurs.
Il suffit d'importer ce module une fois dans __main__.py pour que /anime
et son callback fonctionnent — voir tout en bas de ce fichier pour la
ligne à ajouter :

    try:
        import colab_leecher.services.Aniliste
        logging.info("🔎 Anime search (AniList) loaded")
    except Exception as e:
        logging.warning(f"Anime search not loaded: {e}")

(remplace l'ancien bloc `import colab_leecher.anime_search` dans
__main__.py par celui-ci)
"""

import html
import json
import os
import re

import aiohttp
from pyrogram import filters
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from colab_leecher import colab_bot
from colab_leecher.access import is_allowed as access_is_allowed, is_banned as access_is_banned

# ⚠️ Le "callbacks()" de __main__.py est un @colab_bot.on_callback_query()
# SANS filtre, enregistré dans le groupe par défaut (0) : il appelle
# _services.dispatch() puis, si rien n'a matché, log un warning et
# répond au callback. Notre callback tourne dans un groupe séparé pour
# être bien évalué malgré ça — Pyrogram traite tous les groupes pour
# chaque update, pas juste le premier qui matche.
CALLBACK_GROUP = 15

# Cache persistant sur disque (même logique que data/access.json) :
# {cache_key: [{"id": ..., "title": ..., "thumb": ...}, ...]}
# cache_key = str(message_id) de la commande d'origine, pour garder des
# callback_data courts (limite Telegram : 64 octets).
_CACHE_PATH = "data/anime_cache.json"


def _load_cache() -> dict[str, list[dict]]:
    if not os.path.exists(_CACHE_PATH):
        return {}
    try:
        with open(_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(cache: dict[str, list[dict]]) -> None:
    os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
    try:
        with open(_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception:
        pass


_SEARCH_CACHE: dict[str, list[dict]] = _load_cache()


def _can_use(message: Message) -> bool:
    """Même règle d'accès que le reste du bot (voir _can_use dans __main__.py) :
    OWNER, ou autorisé ET pas banni."""
    from colab_leecher import OWNER
    if message.chat.id == OWNER:
        return True
    return access_is_allowed(message.chat.id) and not access_is_banned(message.chat.id)


# ══════════════════════════════════════════════
#  Backend AniList (GraphQL, sans clé — 30 req/min en anonyme)
# ══════════════════════════════════════════════

_ANILIST_URL = "https://graphql.anilist.co"

_SEARCH_QUERY = """
query ($search: String, $perPage: Int) {
  Page(perPage: $perPage) {
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
      id
      title { romaji english native }
      coverImage { medium }
    }
  }
}
"""

_DETAILS_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    id
    title { romaji english native }
    format
    episodes
    duration
    startDate { year month day }
    endDate { year month day }
    status
    season
    seasonYear
    genres
    tags(sort: RANK_DESC) { name isMediaSpoiler }
    studios { edges { isMain node { name } } }
    externalLinks { site url }
    description(asHtml: false)
    coverImage { large }
    siteUrl
  }
}
"""

_FORMAT_LABELS = {
    "TV": "TV",
    "TV_SHORT": "TV Court",
    "MOVIE": "Film",
    "SPECIAL": "Special",
    "OVA": "OVA",
    "ONA": "ONA",
    "MUSIC": "Music",
}

_STATUS_LABELS = {
    "FINISHED": "Terminé",
    "RELEASING": "En cours",
    "NOT_YET_RELEASED": "À venir",
    "CANCELLED": "Annulé",
    "HIATUS": "En pause",
}


async def _graphql(query: str, variables: dict) -> dict | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _ANILIST_URL,
                json={"query": query, "variables": variables},
                timeout=15,
            ) as resp:
                if resp.status != 200:
                    return None
                payload = await resp.json()
    except Exception:
        return None

    if "errors" in payload:
        return None
    return payload.get("data")


def _best_title(title: dict) -> str:
    return title.get("romaji") or title.get("english") or title.get("native") or "?"


async def _search_anime(query: str, limit: int = 8) -> list[dict]:
    """Retourne [{"id": int, "title": str, "thumb": str}, ...]."""
    data = await _graphql(_SEARCH_QUERY, {"search": query, "perPage": limit})
    if not data:
        return []

    results = []
    for item in data.get("Page", {}).get("media", []):
        results.append({
            "id": item.get("id"),
            "title": _best_title(item.get("title") or {}),
            "thumb": (item.get("coverImage") or {}).get("medium"),
        })
    return results


async def _get_anime_details(anilist_id: int) -> dict | None:
    data = await _graphql(_DETAILS_QUERY, {"id": anilist_id})
    if not data:
        return None

    item = data.get("Media")
    if not item:
        return None

    aired_from = _format_date(item.get("startDate"))
    aired_to = _format_date(item.get("endDate"))
    status = _STATUS_LABELS.get(item.get("status"), item.get("status") or "Inconnu")

    genres = item.get("genres") or []
    themes = [
        t["name"] for t in (item.get("tags") or [])
        if not t.get("isMediaSpoiler")
    ][:5]

    studio_edges = (item.get("studios") or {}).get("edges", [])
    studio = ", ".join(e["node"]["name"] for e in studio_edges if e.get("isMain")) or "?"
    producers = ", ".join(e["node"]["name"] for e in studio_edges if not e.get("isMain")) or "?"

    official_site = None
    for link in item.get("externalLinks") or []:
        if (link.get("site") or "").strip().lower() == "official site":
            official_site = link.get("url")
            break

    episodes = item.get("episodes")
    duration = item.get("duration")
    episodes_str = f"{episodes if episodes else '?'} épisodes × {duration if duration else '?'} min"

    synopsis = _clean_description(item.get("description")) or "Synopsis non disponible."

    return {
        "title_original": (item.get("title") or {}).get("native") or _best_title(item.get("title") or {}),
        "type_origin": _FORMAT_LABELS.get(item.get("format"), item.get("format") or "Autre"),
        "episodes": episodes_str,
        "aired_from": aired_from or "?",
        "aired_to": aired_to or "?",
        "season": _format_season(item.get("season"), item.get("seasonYear")),
        "genres": genres,
        "themes": themes,
        "studio": studio,
        "official_site": official_site,
        "producers": producers,
        "synopsis": synopsis,
        "image_url": (item.get("coverImage") or {}).get("large"),
        "status": status,
        "source_url": item.get("siteUrl"),
    }


def _format_date(date_obj: dict | None) -> str | None:
    if not date_obj:
        return None
    y, m, d = date_obj.get("year"), date_obj.get("month"), date_obj.get("day")
    if not (y and m and d):
        return None
    return f"{d:02d}/{m:02d}/{y}"


def _format_season(season: str | None, year: int | None) -> str:
    if not season and not year:
        return "?"
    season_fr = {
        "winter": "hiver",
        "spring": "printemps",
        "summer": "été",
        "fall": "automne",
    }.get((season or "").lower(), season or "")
    return f"{season_fr} {year}".strip() if year else season_fr


def _clean_description(raw: str | None) -> str | None:
    """AniList renvoie parfois du HTML même avec asHtml=false (ex: <br>,
    <i>...</i>). On retire les balises et on décode les entités HTML."""
    if not raw:
        return None
    text = re.sub(r"<br\s*/?>", "\n", raw)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


# ══════════════════════════════════════════════
#  Formatage / commande / menu / callback
# ══════════════════════════════════════════════

def _format_caption(data: dict) -> str:
    """Construit la caption façon 'Visuel Animes' à partir du dict normalisé
    renvoyé par _get_anime_details."""
    genres = ", ".join(data["genres"]) if data["genres"] else "?"
    themes = ", ".join(data["themes"]) if data["themes"] else "?"

    lines = [
        f"**{data['title_original']}**",
        "",
        f"Titre original : {data['title_original']}",
        f"Série · {data['type_origin']} · {data['episodes']}",
        f"Diffusion : du {data['aired_from']} au {data['aired_to']} ({data['status']})"
        + (f" · Saison : {data['season']}" if data.get("season") not in (None, "?") else ""),
        f"Genre : {genres}",
        f"Thèmes : {themes}",
        f"Studio d'animation : {data['studio']}",
    ]

    if data.get("official_site"):
        lines.append(f"Site web officiel : {data['official_site']}")

    lines.append(f"Groupe : {data['producers']}")
    lines.append("")
    lines.append("Synopsis")
    lines.append(data["synopsis"])
    lines.append("")
    lines.append(f"Source : AniList — {data['source_url']}")

    return "\n".join(lines)


@colab_bot.on_message(filters.command("anime") & filters.private)
async def anime_command(client, message: Message):
    from colab_leecher import OWNER
    await message.reply_text(f"DEBUG chat_id={message.chat.id} OWNER={OWNER} can_use={_can_use(message)}")
    if not _can_use(message):
        return
    if len(message.command) < 2:
        await message.reply_text("Utilisation : `/anime <nom de l'anime>`")
        return

    query = " ".join(message.command[1:])
    status_msg = await message.reply_text("🔎 Recherche sur AniList...")

    results = await _search_anime(query)

    await status_msg.delete()

    if not results:
        await message.reply_text(f"❌ Aucun résultat trouvé pour **{query}**.")
        return

    cache_key = str(message.id)
    _SEARCH_CACHE[cache_key] = results
    _save_cache(_SEARCH_CACHE)

    buttons = [
        [InlineKeyboardButton(r["title"], callback_data=f"anime_sel:{cache_key}:{i}")]
        for i, r in enumerate(results)
    ]

    await message.reply_text(
        f"Résultats pour **{query}** :",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@colab_bot.on_callback_query(filters.regex(r"^anime_sel:"), group=CALLBACK_GROUP)
async def anime_selection_callback(client, callback: CallbackQuery):
    try:
        _, cache_key, idx_str = callback.data.split(":", 2)
        idx = int(idx_str)
    except (ValueError, AttributeError):
        await callback.answer("Sélection invalide.", show_alert=True)
        return

    results = _SEARCH_CACHE.get(cache_key)
    if results is None or idx >= len(results):
        await callback.answer("Cette recherche a expiré, relance la commande.", show_alert=True)
        return

    selected = results[idx]
    await callback.answer("Chargement de la fiche...")
    await callback.message.edit_text(f"⏳ Récupération de la fiche pour **{selected['title']}**...")

    data = await _get_anime_details(selected["id"])

    if data is None:
        await callback.message.edit_text("❌ Impossible de récupérer cette fiche, réessaie plus tard.")
        return

    caption = _format_caption(data)

    if data.get("image_url"):
        await callback.message.delete()
        await client.send_photo(
            chat_id=callback.message.chat.id,
            photo=data["image_url"],
            caption=caption,
        )
    else:
        await callback.message.edit_text(caption)

    # Nettoyage du cache une fois la fiche envoyée
    _SEARCH_CACHE.pop(cache_key, None)
    _save_cache(_SEARCH_CACHE)
