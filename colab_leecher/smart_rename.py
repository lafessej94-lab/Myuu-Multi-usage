"""
Rename automatique du fichier final avant upload.

Reconstruit un nom de fichier propre et cohérent (titre, saison/épisode,
langue, qualité, plateforme, source, codecs) à partir du vrai nom du
fichier réel téléchargé, et :
  - normalise la langue en VOSTFR (sauf si MULTI/DUAL détecté)
  - remplace TOUJOURS le tag de groupe par Myuus-Raws (la plateforme CR,
    ADN, DSNP, NF, AMZN... au milieu du nom n'est elle jamais modifiée)

Deux familles de formats sont reconnues :

  1) Scene/release style, avec SxxExx, séparateurs espace OU point,
     langue et "DUAL" optionnels :
       RILAKKUMA S01E23 SUBFRENCH 1080p CR WEB-DL AAC2.0 H.264-Tsundere-Raws
       RILAKKUMA.S01E23.1080p.CR.WEB-DL.DUAL.AAC2.0.H.264.MSubs-ToonsHub
       RILAKKUMA S01E23 1080p CR WEB-DL DUAL AAC2.0 H.264-VARYG

  2) Fansub bracket style ([Groupe] Titre - Épisode ...), sans saison
     explicite (on suppose S01) :
       [Erai-raws] Rilakkuma - 23 [1080p CR WEB-DL AVC AAC][MultiSub][45752258]
       [SubsPlease] Rilakkuma - 23 (480p) [E2E141F1]

     Pour ce format, les champs manquants (plateforme, source, codecs)
     sont simplement omis du nom reconstruit plutôt que devinés.

Si aucun format ne correspond, on renvoie le nom d'origine sans y toucher
(mieux vaut un nom non modifié qu'un nom cassé).

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

_LANG_ALT = r"VOSTFR|VFF?Q?|MULTI\w*|SUBFRENCH|FRENCH|TRUEFRENCH"
_SOURCE_ALT = r"WEB-?DL|WEBRip|BDRip|Blu-?Ray|HDTV"
_AUDIO_ALT = r"AAC(?:\d(?:[.\s]?\d)?)?|AC-?3|DDP?\d(?:\.\d)?|FLAC|OPUS|DTS|MP3"
_VIDEO_ALT = r"H\.?26[45]|x26[45]|HEVC|AVC|AV1"
_PLATFORM_ALT = (
    r"CR|ADN|DSNP|NF|AMZN|HULU|HIDIVE|FUNI|ABEMA|VRV|WAKANIM|B-?Global|iQ(?:iyi)?|U-?NEXT"
)

# --- Format 1 : scene/release style, séparateurs espace OU point ----------
# Découpe : <titre> <SxxExx> [<lang>] <quality> <platform> <source>
#           [DUAL] <audio> <video> [Subs] -<group>
_SCENE_RE = re.compile(
    r"^(?P<title>.+?)[\s.]+(?P<se>S\d{2}E\d{2,4})[\s.]+"
    r"(?:(?P<lang>" + _LANG_ALT + r")[\s.]+)?"
    r"(?P<quality>\d{3,4}p)[\s.]+"
    r"(?P<platform>" + _PLATFORM_ALT + r")[\s.]+"
    r"(?P<source>" + _SOURCE_ALT + r")[\s.]+"
    r"(?:(?P<dual>DUAL)[\s.]+)?"
    r"(?P<audio>" + _AUDIO_ALT + r")[\s.]+"
    r"(?P<video>" + _VIDEO_ALT + r")"
    r"(?:[\s.]M?Subs?)?"
    r"-(?P<group>.+)$",
    re.IGNORECASE,
)

# --- Format 2 : fansub bracket style, pas de saison explicite -------------
# Découpe : [Groupe] Titre - Épisode <reste des tags entre crochets/parenthèses>
_FANSUB_RE = re.compile(
    r"^\[(?P<fansub_group>[^\]]+)\]\s*"
    r"(?P<title>.+?)\s*-\s*(?P<episode>\d{1,4})\s*"
    r"(?P<meta>.*)$",
    re.IGNORECASE,
)

# Un bloc entre crochets/parenthèses qui n'est que des caractères hex
# (6 à 10) est presque toujours un hash/CRC de fichier, pas une info utile.
_HEX_HASH_RE = re.compile(r"^[0-9A-Fa-f]{6,10}$")


def resolution_label(height: int) -> str:
    """Convertit une hauteur (ex: 480 dans un resize (854, 480)) en tag qualité (ex: '480p')."""
    return f"{height}p"


def _clean_title(raw: str) -> str:
    """Normalise les séparateurs (points, espaces multiples) d'un titre en espaces simples."""
    return re.sub(r"[.\s]+", " ", raw).strip(" .-_")


def _final_lang(lang: str | None, dual: bool, multi_hint: bool) -> str:
    """
    Détermine le tag de langue final.

    - Un tag de langue explicite contenant "multi" est conservé tel quel.
    - Un tag de langue explicite qui NE contient PAS "multi" est toujours
      remplacé par VOSTFR (comportement historique).
    - Sans tag de langue explicite : "DUAL" (audio double) ou un indice
      "MultiSub" détecté ailleurs dans le nom donnent MULTI ; sinon VOSTFR.
    """
    if lang:
        return lang if _MULTI_RE.search(lang) else "VOSTFR"
    if multi_hint:
        return "MULTI"
    if dual:
        return "DUAL"
    return "VOSTFR"


def _join_parts(*parts: str | None) -> str:
    return " ".join(p for p in parts if p)


def _try_scene_format(base: str) -> str | None:
    match = _SCENE_RE.match(base.strip())
    if not match:
        return None
    parts = match.groupdict()
    title = _clean_title(parts["title"])
    lang = _final_lang(parts.get("lang"), bool(parts.get("dual")), False)
    platform = parts["platform"].upper()

    new_base = _join_parts(
        title, parts["se"], lang, parts["quality"], platform,
        parts["source"], parts["audio"], parts["video"],
    )
    return f"{new_base}-{NEW_GROUP_TAG}"


def _try_fansub_format(base: str) -> str | None:
    match = _FANSUB_RE.match(base.strip())
    if not match:
        return None
    parts = match.groupdict()
    title = _clean_title(parts["title"])
    episode = int(parts["episode"])
    se = f"S01E{episode:02d}"

    # Retire les blocs [xxx]/(xxx) qui ne sont qu'un hash/CRC, garde le reste
    # du texte entre crochets/parenthèses comme simple blob à analyser.
    meta_clean = re.sub(
        r"[\[(]([^\])]*)[\])]",
        lambda m: "" if _HEX_HASH_RE.match(m.group(1).strip()) else f" {m.group(1)} ",
        parts["meta"] or "",
    )

    multi_hint = bool(_MULTI_RE.search(meta_clean))
    lang = _final_lang(None, False, multi_hint)

    def _search(alt: str) -> str | None:
        m = re.search(r"\b(?:" + alt + r")\b", meta_clean, re.IGNORECASE)
        return m.group(0) if m else None

    quality = _search(r"\d{3,4}p")
    platform = _search(_PLATFORM_ALT)
    source = _search(_SOURCE_ALT)
    audio = _search(_AUDIO_ALT)
    video = _search(_VIDEO_ALT)

    new_base = _join_parts(
        title, se, lang, quality,
        platform.upper() if platform else None,
        source, audio, video,
    )
    return f"{new_base}-{NEW_GROUP_TAG}"


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
    qualité du nom source telle quelle. Si le nom source ne contient pas
    de tag de qualité (certains formats fansub), il est simplement ajouté.

    output_ext : à fournir quand le moteur réencode toujours vers un
    conteneur fixe (ex: "mp4" pour FreeConvert/CloudConvert, même si la
    source est un .mkv). Laisser None pour garder l'extension du fichier
    source telle quelle (cas FFmpeg local en mux/copy par exemple).

    Si le nom ne correspond à aucun format reconnu, on renvoie le nom
    d'origine sans y toucher (mieux vaut un nom non modifié qu'un nom cassé).
    """
    base, ext = os.path.splitext(real_filename)
    base = base.strip()

    new_base = _try_scene_format(base) or _try_fansub_format(base)
    if new_base is None:
        return real_filename

    if override_quality:
        if re.search(r"\d{3,4}p", new_base):
            new_base = re.sub(r"\d{3,4}p", override_quality, new_base, count=1)
        else:
            # Pas de tag qualité dans le nom reconstruit (ex: fansub sans
            # info de résolution) -> on l'insère juste avant le groupe final.
            new_base = new_base.replace(
                f"-{NEW_GROUP_TAG}", f" {override_quality}-{NEW_GROUP_TAG}"
            )

    final_ext = f".{output_ext.lstrip('.')}" if output_ext else ext
    return f"{new_base}{final_ext}"


if __name__ == "__main__":
    tests = [
        # Scene style historique
        "RILAKKUMA S01E23 SUBFRENCH 1080p CR WEB-DL AAC2.0 H.264-Tsundere-Raws.mkv",
        "SOME ANIME S02E05 MULTI 1080p CR WEB-DL AAC2.0 H.264-SomeGroup.mkv",
        "OTHER ANIME S01E01 VOSTFR 1080p ADN WEB-DL AAC2.0 H.264-OldGroup.mkv",
        "OTHER ANIME S01E01 VOSTFR 1080p AMZN WEB-DL AAC2.0 H.264-OldGroup.mkv",
        "UNPARSEABLE NAME.mkv",
        # Nouveaux formats
        "[Erai-raws] Rilakkuma - 23 [1080p CR WEB-DL AVC AAC][MultiSub][45752258].mkv",
        "[SubsPlease] Rilakkuma - 23 (480p) [E2E141F1].mkv",
        "RILAKKUMA.S01E23.1080p.CR.WEB-DL.DUAL.AAC2.0.H.264.MSubs-ToonsHub.mkv",
        "RILAKKUMA S01E23 1080p CR WEB-DL DUAL AAC2.0 H.264-VARYG.mkv",
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

    fc_no_quality = "[SubsPlease] Rilakkuma - 23 [E2E141F1].mkv"
    print(
        f"{fc_no_quality}\n  -> "
        f"{build_final_name(fc_no_quality, override_quality=resolution_label(480), output_ext='mp4')}\n"
    )
