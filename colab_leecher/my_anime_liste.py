"""
my_anime_liste.py — Recherche d'animes via AniList (API GraphQL publique)
Utilisé par la commande /Mal_Anime dans anime_search.py

Remplace l'ancienne implémentation basée sur Jikan (MyAnimeList) qui
renvoyait des erreurs 504 systématiques — même problème déjà rencontré
sur le site myuus-raws, réglé là-bas en basculant sur AniList. Cette
version reprend exactement la même solution ici.

Garde volontairement la même interface publique (search_anime /
get_anime_details, mêmes clés dans les dicts retournés) que l'ancienne
version : anime_search.py n'a besoin d'aucune modification.

⚠️ Note branding : les données viennent maintenant d'AniList et non plus
de MyAnimeList. Le format de fiche reste identique, mais dans
anime_search.py la ligne :
    source_label = "MyAnimeList"
(dans anime_selection_callback) affiche encore "MyAnimeList" en bas de
la caption alors que la source réelle est AniList. À changer en
"AniList" pour rester honnête avec les utilisateurs du bot.

API publique, sans clé, limitée à 30 requêtes/min en anonyme :
https://docs.anilist.co
"""

import html
import re

import aiohttp

ANILIST_URL = "https://graphql.anilist.co"

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
    """Exécute une requête GraphQL contre AniList. Retourne le champ "data"
    de la réponse, ou None en cas d'erreur réseau/HTTP/GraphQL."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                ANILIST_URL,
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


async def search_anime(query: str, limit: int = 8) -> list[dict]:
    """
    Recherche des animes sur AniList.
    Retourne une liste pour construire le menu inline :
    [{"mal_id": int, "title": str, "thumb": str}, ...]
    (clé "mal_id" gardée pour compat avec anime_search.py — c'est en
    réalité l'id AniList, utilisé seulement comme identifiant interne)
    Liste vide si aucun résultat ou erreur réseau.
    """
    data = await _graphql(_SEARCH_QUERY, {"search": query, "perPage": limit})
    if not data:
        return []

    results = []
    for item in data.get("Page", {}).get("media", []):
        results.append({
            "mal_id": item.get("id"),
            "title": _best_title(item.get("title") or {}),
            "thumb": (item.get("coverImage") or {}).get("medium"),
        })
    return results


async def get_anime_details(mal_id: int) -> dict | None:
    """
    Récupère la fiche complète d'un anime à partir de son id AniList
    et la normalise au format commun partagé avec nautilijan.py.
    Retourne None en cas d'erreur.
    """
    data = await _graphql(_DETAILS_QUERY, {"id": mal_id})
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
    """Formate un objet {year, month, day} AniList en DD/MM/YYYY.
    Retourne None si la date est absente ou incomplète."""
    if not date_obj:
        return None
    y, m, d = date_obj.get("year"), date_obj.get("month"), date_obj.get("day")
    if not (y and m and d):
        return None
    return f"{d:02d}/{m:02d}/{y}"


def _format_season(season: str | None, year: int | None) -> str:
    """Formate saison + année en français, ex: 'automne 2026'."""
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
