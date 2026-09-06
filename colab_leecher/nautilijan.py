"""
nautilijan.py — Recherche d'animes via scraping de Nautiljon.com
Utilisé par la commande /Naut_Anime dans anime_search.py

Pas d'API officielle : ce module scrape les pages HTML publiques du site.
Nautiljon change parfois sa mise en page, donc ce parsing s'appuie sur le
LIBELLÉ des champs ("Titre original :", "Diffusion :", etc.) plutôt que sur
des classes CSS précises, pour rester robuste aux changements mineurs de style.

⚠️ Non testé en conditions réelles (le bac à sable de développement n'a pas
accès à nautiljon.com). À valider et ajuster une fois lancé côté Colab.
"""

import re
import aiohttp
from bs4 import BeautifulSoup

BASE_URL = "https://www.nautiljon.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}


async def _fetch_html(url: str, params: dict | None = None) -> str | None:
    """Récupère le HTML d'une page avec une session neuve à chaque appel
    (évite tout état de recherche/filtre persistant côté serveur)."""
    try:
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            async with session.get(url, params=params, timeout=15) as resp:
                if resp.status != 200:
                    return None
                return await resp.text()
    except Exception:
        return None


async def search_anime(query: str, limit: int = 8) -> list[dict]:
    """
    Recherche des animes sur Nautiljon.
    Retourne une liste pour construire le menu inline :
    [{"url": str, "title": str, "thumb": str}, ...]
    Liste vide si aucun résultat ou erreur réseau.
    """
    html = await _fetch_html(f"{BASE_URL}/animes/", params={"q": query})
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")

    # Le tableau de résultats contient un en-tête avec "Titre" et "Format"
    result_table = None
    for table in soup.find_all("table"):
        header_text = table.get_text(" ", strip=True)[:200]
        if "Titre" in header_text and "Format" in header_text:
            result_table = table
            break
    if result_table is None:
        return []

    results = []
    for row in result_table.find_all("tr"):
        img = row.find("img")
        link = row.find("a", href=re.compile(r"/animes/[^/]+\.html$"))
        if not img or not link:
            continue
        title = link.get_text(strip=True)
        if not title:
            continue
        url = link["href"]
        if url.startswith("/"):
            url = BASE_URL + url
        thumb = img.get("src") or ""
        if thumb.startswith("/"):
            thumb = BASE_URL + thumb
        results.append({"url": url, "title": title, "thumb": thumb})
        if len(results) >= limit:
            break

    return results


def _find_field(soup: BeautifulSoup, label: str) -> str | None:
    """Trouve le texte suivant un libellé donné (ex: 'Titre original :')
    dans la liste d'infos de la fiche."""
    item = soup.find(string=re.compile(re.escape(label)))
    if not item:
        return None
    container = item.find_parent("li") or item.find_parent()
    if not container:
        return None
    text = container.get_text(" ", strip=True)
    text = re.sub(re.escape(label), "", text, count=1).strip(" :\u00a0")
    return text or None


def _find_field_links(soup: BeautifulSoup, label: str) -> list[str]:
    """Comme _find_field mais retourne la liste des libellés des liens
    contenus dans le champ (ex: genres, thèmes)."""
    item = soup.find(string=re.compile(re.escape(label)))
    if not item:
        return []
    container = item.find_parent("li") or item.find_parent()
    if not container:
        return []
    return [a.get_text(strip=True) for a in container.find_all("a") if a.get_text(strip=True)]


async def get_anime_details(url: str) -> dict | None:
    """
    Récupère la fiche complète d'un anime à partir de son URL Nautiljon
    et la normalise au format commun partagé avec my_anime_liste.py.
    Retourne None en cas d'erreur.
    """
    html = await _fetch_html(url)
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")

    title_tag = soup.find("h1")
    title = title_tag.get_text(strip=True) if title_tag else None

    title_original = _find_field(soup, "Titre original")

    serie_info = _find_field(soup, "Série") or _find_field(soup, "Genre unitaire")
    type_origin = _find_field(soup, "Origine") or "Autre"

    diffusion_raw = _find_field(soup, "Diffusion") or ""
    # Format typique : "du 20/10/1999 au ? (en cours)"
    dates_match = re.search(
        r"du?\s*([0-9/?]+)\s*au\s*([0-9/?]+)?\s*(?:\(([^)]+)\))?", diffusion_raw
    )
    aired_from, aired_to, status_hint = "?", "?", ""
    if dates_match:
        aired_from = dates_match.group(1) or "?"
        aired_to = dates_match.group(2) or "?"
        status_hint = dates_match.group(3) or ""

    status_hint = status_hint.lower()
    if "cours" in status_hint:
        status = "En cours"
    elif "venir" in status_hint:
        status = "À venir"
    elif "pause" in status_hint:
        status = "En pause"
    elif "annul" in status_hint:
        status = "Annulé"
    elif aired_to not in ("?", ""):
        status = "Terminé"
    else:
        status = "Inconnu"

    genres = _find_field_links(soup, "Genres")
    themes = _find_field_links(soup, "Thèmes")
    studio = _find_field(soup, "Studio d'animation") or "?"
    producers = _find_field(soup, "Groupe") or "?"

    official_site_field = soup.find(string=re.compile("Site web officiel"))
    official_site = None
    if official_site_field:
        container = official_site_field.find_parent("li") or official_site_field.find_parent()
        if container:
            link = container.find("a")
            if link and link.get("href"):
                official_site = link["href"]

    # Épisodes : "? épisodes × 24 min" apparaît dans le même bloc que "Série"
    episodes_match = re.search(r"([0-9?]+)\s*épisodes?\s*×\s*([0-9?]+)\s*min", serie_info or "")
    if episodes_match:
        episodes = f"{episodes_match.group(1)} épisodes × {episodes_match.group(2)} min"
    else:
        episodes = "? épisodes × ? min"

    # Synopsis : premier paragraphe du bloc "Synopsis", avant tout sous-titre d'arc
    synopsis = "Synopsis non disponible."
    synopsis_header = soup.find(["h2", "h3"], string=re.compile("Synopsis"))
    if synopsis_header:
        for sibling in synopsis_header.find_next_siblings():
            if sibling.name in ("h2", "h3", "h4"):
                break
            if sibling.name == "p":
                text = sibling.get_text(" ", strip=True)
                if text:
                    synopsis = text
                    break

    image_tag = soup.find("img", src=re.compile(r"/images/anime/"))
    image_url = None
    if image_tag and image_tag.get("src"):
        image_url = image_tag["src"]
        if image_url.startswith("/"):
            image_url = BASE_URL + image_url

    return {
        "title_original": title_original or title,
        "type_origin": type_origin,
        "episodes": episodes,
        "aired_from": aired_from,
        "aired_to": aired_to,
        "season": "?",
        "genres": genres,
        "themes": themes,
        "studio": studio,
        "official_site": official_site,
        "producers": producers,
        "synopsis": synopsis,
        "image_url": image_url,
        "status": status,
        "source_url": url,
    }
