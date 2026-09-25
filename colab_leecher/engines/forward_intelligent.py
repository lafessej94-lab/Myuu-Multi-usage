"""
colab_leecher/engines/forward_intelligent.py

Moteur pur : matching entre un nom de fichier uploadé (ex "One Piece
Ep1080 VOSTFR 1080p CR WEB-DL AAC2.0 H.264-Myuus-Raws") et la liste des
canaux dump configurés, pour ne transférer QUE vers les canaux dont le
nom correspond à l'anime — au lieu du forward actuel qui envoie vers
TOUS les canaux dump.

Règles confirmées :
- Matching en "contains" (substring), pas en exact match.
- Insensible à la casse (ONE PIECE = One Piece = one piece).
- Les tags d'épisode/qualité/langue (Ep1080, VOSTFR, 1080p, etc.) sont
  ignorés des deux côtés : on ne compare que le nom "nu" de l'anime.

À BRANCHER : je n'ai pas le code qui gère la liste actuelle des canaux
dump (probablement dans variables.py ou un fichier de config JSON, lié à
/add). Montre-le moi pour que je câble get_dump_channels() correctement
au lieu du stub ci-dessous.
"""
from __future__ import annotations

import re
import unicodedata

# Réutilise la même logique de nettoyage que tsundere_rss.anime_name(),
# généralisée à un nom de fichier release "classique" (pas juste les
# titres du flux RSS tsundere.to).

_TAG_RE = re.compile(
    r"""
    \bhardsub\b | \bsoftsub\b |
    \b(720p|1080p|480p|2160p|4k)\b |
    \b(cr|adn|dsnp|nf|amzn|web-dl|webrip|bluray|bdrip)\b |
    \baac\d?(?:\.\d+)?\b | \bx264\b | \bh\.?264\b | \bh\.?265\b | \bhevc\b |
    \b(vostfr|vf|vfr|vfq|multi|subfrench|french)\b |
    \bs\d{1,2}e\d{1,4}\b |
    \bepisode\s*\d{1,4}\b |
    \bep\.?\s*\d{1,4}\b |
    \be\d{1,4}\b |
    -\s*\d{1,4}\b
    """,
    re.IGNORECASE | re.VERBOSE,
)


def normalize(text: str) -> str:
    """Minuscule, sans accents, ponctuation/espaces réduits à un seul
    espace. Base de comparaison commune pour le nom de fichier ET le nom
    de canal."""
    text = str(text or "")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[._]+", " ", text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_anime_name(filename: str) -> str:
    """Réduit un nom de fichier release complet à son nom d'anime nu.
    Ex : "One Piece Ep1080 VOSTFR 1080p CR WEB-DL AAC2.0 H.264-Myuus-Raws"
      -> "one piece" """
    value = str(filename or "")
    value = _TAG_RE.sub(" ", value)
    value = re.sub(r"-[A-Za-z0-9]+$", "", value)  # tag de groupe final (ex "-Myuus-Raws")
    return normalize(value)


def match_dump_channels(filename: str, dump_channels: list[dict]) -> list[dict]:
    """
    dump_channels : liste de dicts avec au moins {"id": ..., "name": ...}
    (à adapter selon la vraie structure — voir note en tête de fichier).

    Renvoie la sous-liste des canaux dont le nom (normalisé) apparaît en
    substring dans le nom de fichier normalisé, ou inversement (au cas où
    le nom de canal serait plus long/plus précis que le nom extrait).
    """
    anime = extract_anime_name(filename)
    if not anime:
        return []

    matches = []
    for channel in dump_channels:
        channel_name = normalize(channel.get("name", ""))
        if not channel_name:
            continue
        if channel_name in anime or anime in channel_name:
            matches.append(channel)
    return matches


# ============================================================
# STUB — à remplacer par le vrai accès à la liste des dumps
# ============================================================

def get_dump_channels() -> list[dict]:
    """
    PLACEHOLDER. Doit renvoyer la liste réelle des canaux dump configurés
    (probablement BOT.Options.dumps ou équivalent dans variables.py).
    Montre-moi la structure réelle pour que je remplace ce stub.
    """
    raise NotImplementedError(
        "get_dump_channels() n'est pas encore câblé — montre-moi où/comment "
        "myuu stocke la liste des canaux dump (variables.py ou data/*.json)."
    )
