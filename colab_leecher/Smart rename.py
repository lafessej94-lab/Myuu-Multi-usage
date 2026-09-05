"""
Rename automatique du fichier final avant upload.

Reprend le vrai nom (titre, saison/épisode, qualité, plateforme, source,
codecs) tel qu'il apparaît dans le nom du fichier réel téléchargé, et :
  - normalise la langue en VOSTFR (sauf si "MULTI" est déjà présent)
  - remplace TOUJOURS le tag de fin par Myuus-Raws (la plateforme CR,
    ADN, DSNP, NF, AMZN... au milieu du nom n'est elle jamais modifiée)

Exemple :
    RILAKKUMA S01E23 SUBFRENCH 1080p CR WEB-DL AAC2.0 H.264-Tsundere-Raws
    -> RILAKKUMA S01E23 VOSTFR 1080p CR WEB-DL AAC2.0 H.264-Myuus-Raws
"""

from __future__ import annotations

import os
import re

NEW_GROUP_TAG = "Myuus-Raws"

# Ne pas forcer VOSTFR si la langue contient déjà "MULTI" (MULTI, MULTi, VF-MULTI, etc.)
_MULTI_RE = re.compile(r"multi", re.IGNORECASE)

# Découpe : <prefix incluant SxxExx> <lang> <quality> <platform> <source> <audio> <video>-<group>
_FILENAME_RE = re.compile(
    r"^(?P<prefix>.+?\bS\d{2}E\d{2,4})\s+"
    r"(?P<lang>\S+)\s+"
    r"(?P<quality>\d{3,4}p)\s+"
    r"(?P<platform>[A-Za-z]{2,6})\s+"
    r"(?P<source>WEB-?DL|WEBRip|BDRip|Blu-?Ray|HDTV)\s+"
    r"(?P<audio>[A-Za-z0-9.]+)\s+"
    r"(?P<video>H\.?26[45]|x26[45]|AV1)"
    r"-(?P<group>.+)$",
    re.IGNORECASE,
)


def resolution_label(height: int) -> str:
    """Convertit une hauteur (ex: 480 dans un resize (854, 480)) en tag qualité (ex: '480p')."""
    return f"{height}p"


def build_final_name(
    real_filename: str,
    *,
    override_quality: str | None = None,
    output_ext: str | None = None,
) -> str:
    """
    Construit le nom final à uploader à partir du vrai nom de fichier source.

    override_quality : à fournir pour le pipeline FreeConvert/CloudConvert
    quand un resize est appliqué (ex: "480p") -- la qualité réellement
    encodée doit apparaître dans le nom, pas celle du fichier source
    d'origine, pour ne pas induire en erreur (ex: pas de "1080p" dans le
    nom si le fichier livré est en 480p). Laisser None pour garder la
    qualité du nom source telle quelle.

    output_ext : à fournir quand le moteur réencode toujours vers un
    conteneur fixe (ex: "mp4" pour FreeConvert/CloudConvert, même si la
    source est un .mkv). Laisser None pour garder l'extension du fichier
    source telle quelle (cas FFmpeg local en mux/copy par exemple).

    Si le nom ne correspond pas au format attendu, on renvoie le nom
    d'origine sans y toucher (mieux vaut un nom non modifié qu'un nom cassé).
    """
    base, ext = os.path.splitext(real_filename)
    match = _FILENAME_RE.match(base.strip())
    if not match:
        return real_filename

    parts = match.groupdict()

    # Langue : VOSTFR par défaut, sauf si "MULTI" est déjà présent dans le tag.
    lang = parts["lang"]
    if not _MULTI_RE.search(lang):
        lang = "VOSTFR"

    # Tag de fin : toujours remplacé par Myuus-Raws.
    # La plateforme (CR/ADN/DSNP/NF/AMZN) au milieu du nom, elle, n'est
    # jamais modifiée -- elle est simplement reprise telle quelle plus bas.
    group = NEW_GROUP_TAG

    quality = override_quality or parts["quality"]
    final_ext = f".{output_ext.lstrip('.')}" if output_ext else ext

    new_base = (
        f'{parts["prefix"]} {lang} {quality} {parts["platform"]} '
        f'{parts["source"]} {parts["audio"]} {parts["video"]}-{group}'
    )
    return f"{new_base}{final_ext}"


if __name__ == "__main__":
    tests = [
        "RILAKKUMA S01E23 SUBFRENCH 1080p CR WEB-DL AAC2.0 H.264-Tsundere-Raws.mkv",
        "SOME ANIME S02E05 MULTI 1080p CR WEB-DL AAC2.0 H.264-SomeGroup.mkv",
        "OTHER ANIME S01E01 VOSTFR 1080p ADN WEB-DL AAC2.0 H.264-OldGroup.mkv",
        "OTHER ANIME S01E01 VOSTFR 1080p AMZN WEB-DL AAC2.0 H.264-OldGroup.mkv",
        "UNPARSEABLE NAME.mkv",
    ]
    for name in tests:
        print(f"{name}\n  -> {build_final_name(name)}\n")

    # Cas FreeConvert/CloudConvert avec resize (854, 480) -> qualité annoncée
    # suit le resize, et l'extension est forcée en mp4 même si la source est un mkv.
    fc_source = "RILAKKUMA S01E23 SUBFRENCH 1080p CR WEB-DL AAC2.0 H.264-Tsundere-Raws.mkv"
    print(
        f"{fc_source}\n  -> "
        f"{build_final_name(fc_source, override_quality=resolution_label(480), output_ext='mp4')}\n"
    )
