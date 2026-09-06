"""
my_anime_liste.py — Recherche d'animes via MyAnimeList (API Jikan v4)
Utilisé par la commande /Mal_Anime dans anime_search.py

API publique, sans clé : https://docs.api.jikan.moe/
"""

import aiohttp

JIKAN_BASE = "https://api.jikan.moe/v4"


async def search_anime(query: str, limit: int = 8) -> list[dict]:
    """
    Recherche des animes sur MyAnimeList.
    Retourne une liste pour construire le menu inline :
    [{"mal_id": int, "title": str, "thumb": str}, ...]
    Liste vide si aucun résultat ou erreur réseau.
    """
    url = f"{JIKAN_BASE}/anime"
    params = {"q": query, "limit": limit}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=15) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
    except Exception:
        return []

    results = []
    for item in data.get("data", []):
        results.append({
            "mal_id": item.get("mal_id"),
            "title": item.get("title"),
            "thumb": (item.get("images", {}).get("jpg", {}) or {}).get("image_url"),
        })
    return results


async def get_anime_details(mal_id: int) -> dict | None:
    """
    Récupère la fiche complète d'un anime à partir de son mal_id
    et la normalise au format commun partagé avec nautilijan.py.
    Retourne None en cas d'erreur.
    """
    url = f"{JIKAN_BASE}/anime/{mal_id}/full"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=15) as resp:
                if resp.status != 200:
                    return None
                payload = await resp.json()
    except Exception:
        return None

    item = payload.get("data")
    if not item:
        return None

    aired = item.get("aired", {}) or {}
    aired_from = _format_date(aired.get("from")) or "?"
    aired_to = _format_date(aired.get("to")) or "?"

    status_raw = (item.get("status") or "").lower()
    if "airing" in status_raw:
        status = "En cours"
    elif "finished" in status_raw:
        status = "Terminé"
    elif "not yet aired" in status_raw or "upcoming" in status_raw:
        status = "À venir"
    else:
        status = item.get("status") or "Inconnu"

    genres = [g["name"] for g in item.get("genres", [])]
    themes = [t["name"] for t in item.get("themes", [])]
    studio = ", ".join(s["name"] for s in item.get("studios", [])) or "?"
    producers = ", ".join(p["name"] for p in item.get("producers", [])) or "?"

    official_site = None
    for link in item.get("external", []) or []:
        if "official site" in (link.get("name") or "").lower():
            official_site = link.get("url")
            break

    episodes = item.get("episodes")
    duration = item.get("duration") or "? min"
    episodes_str = f"{episodes if episodes else '?'} épisodes × {duration}"

    images = item.get("images", {}).get("jpg", {}) or {}
    image_url = images.get("large_image_url") or images.get("image_url")

    return {
        "title_original": item.get("title_japanese") or item.get("title"),
        "type_origin": item.get("type") or "Autre",
        "episodes": episodes_str,
        "aired_from": aired_from,
        "aired_to": aired_to,
        "season": _format_season(item.get("season"), item.get("year")),
        "genres": genres,
        "themes": themes,
        "studio": studio,
        "official_site": official_site,
        "producers": producers,
        "synopsis": item.get("synopsis") or "Synopsis non disponible.",
        "image_url": image_url,
        "status": status,
        "source_url": item.get("url"),
    }


def _format_date(date_str: str | None) -> str | None:
    """Convertit une date ISO Jikan (ex: 2026-10-08T00:00:00+00:00) en DD/MM/YYYY."""
    if not date_str:
        return None
    try:
        return f"{date_str[8:10]}/{date_str[5:7]}/{date_str[0:4]}"
    except Exception:
        return None


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
