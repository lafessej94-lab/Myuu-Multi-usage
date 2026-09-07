"""
Rename automatique du fichier final avant upload.

Reconstruit un nom de fichier propre et cohérent (titre, saison/épisode,
langue, qualité, plateforme, source, codecs) à partir du vrai nom du
fichier réel téléchargé, et :
  - normalise la langue en VOSTFR (sauf si MULTI/DUAL détecté)
  - remplace TOUJOURS le tag de groupe par Myuus-Raws (la plateforme CR,
    ADN, DSNP, NF, AMZN... au milieu du nom n'est elle jamais modifiée)

Trois familles de formats sont reconnues :

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

  3) Underscore style ( _Tag_ Titre [lang] ), typique des fichiers sans
     aucune info de saison/qualité/plateforme :
       _Meda-Arcedo_ Virgin Punk Clockwork Girl VOSTFR

     Le tag entre underscores en tête est TOUJOURS supprimé (comme le
     groupe des autres formats, remplacé par Myuus-Raws). Comme il n'y a
     ici aucune info de source (WEB-DL/WEBRip/...), "WEB-DL" est ajouté
     par défaut pour ne pas livrer un nom totalement sans provenance.

Si aucun format ne correspond, on renvoie le nom d'origine sans y toucher
(mieux vaut un nom non modifié qu'un nom cassé).

Exemples :
    RILAKKUMA S01E23 SUBFRENCH 1080p CR WEB-DL AAC2.0 H.264-Tsundere-Raws
    -> RILAKKUMA S01E23 VOSTFR 1080p CR WEB-DL AAC2.0 H.264-Myuus-Raws

    _Meda-Arcedo_ Virgin Punk Clockwork Girl VOSTFR
    -> Virgin Punk Clockwork Girl VOSTFR WEB-DL-Myuus-Raws
"""

from __future__ import annotations

import logging
import os
import re

log = logging.getLogger(__name__)

NEW_GROUP_TAG = "Myuus-Raws"

_MULTI_RE = re.compile(r"multi", re.IGNORECASE)

_LANG_ALT = r"VOSTFR|VFF?Q?|MULTI\w*|SUBFRENCH|FRENCH|TRUEFRENCH"
_SOURCE_ALT = r"WEB-?DL|WEBRip|BDRip|Blu-?Ray|HDTV"
_AUDIO_ALT = r"AAC(?:\d(?:[.\s]?\d)?)?|AC-?3|DDP?\d(?:\.\d)?|FLAC|OPUS|DTS|MP3"
_VIDEO_ALT = r"H\.?26[45]|x26[45]|HEVC|AVC|AV1"
_PLATFORM_ALT = (
    r"CR|ADN|DSNP|NF|AMZN|HULU|HIDIVE|FUNI|ABEMA|VRV|WAKANIM|B-?Global|iQ(?:iyi)?|U-?NEXT"
)

_SCENE_RE = re.compile(
    r"^(?P<title>.+?)[\s.]+(?P<se>S\d{2}E\d{2,4})"
    r"(?:[\s.]+(?P<middle>.+?))?[\s.]+"
    r"(?P<quality>\d{3,4}p)[\s.]+"
    r"(?:(?P<platform>" + _PLATFORM_ALT + r")[\s.]+)?"
    r"(?P<source>" + _SOURCE_ALT + r")[\s.]+"
    r"(?:(?P<dual>DUAL)[\s.]+)?"
    r"(?:(?P<audio>" + _AUDIO_ALT + r")[\s.]+)?"
    r"(?P<video>" + _VIDEO_ALT + r")"
    r"(?:[\s.]M?Subs?)?"
    r"-(?P<group>.+)$",
    re.IGNORECASE,
)
# NOTE: platform et audio sont désormais optionnels (ex: Liar.Game...WEBRiP.x265-KAF
# n'a ni plateforme ni tag audio).

_LANG_TOKEN_FULL_RE = re.compile(r"^(?:" + _LANG_ALT + r")$", re.IGNORECASE)

_FANSUB_RE = re.compile(
    r"^\[(?P<fansub_group>[^\]]+)\]\s*"
    r"(?P<title>.+?)\s*-\s*(?P<episode>\d{1,4})\s*"
    r"(?P<meta>.*)$",
    re.IGNORECASE,
)

_HEX_HASH_RE = re.compile(r"^[0-9A-Fa-f]{6,10}$")

_UNDERSCORE_RE = re.compile(r"^_(?P<tag>[^_]+)_\s*(?P<rest>.+)$")

# --- Format 4 : bracket-group + SxxExx explicite + LANG + blob (...) ------
# Découpe : [Groupe] Titre SxxExx LANG (source qualité vidéo audio ...)
#           ex: [Team Arcedo] Onee-chan Gokko S01E01 VOSTFR (WEB-DL 1080p H.264 AAC)
# Contrairement au format fansub (pas de SxxExx, "Titre - N"), ici la
# saison/épisode et la langue sont déjà explicites dans le nom ; seul le
# bloc entre parenthèses doit être analysé (ordre non garanti -> recherche
# de token comme pour le fansub). La source reste entre parenthèses dans
# le nom final, le reste (qualité/plateforme/vidéo/audio) en sort.
_BRACKET_SCENE_RE = re.compile(
    r"^\[(?P<group>[^\]]+)\]\s*"
    r"(?P<title>.+?)[\s.]+(?P<se>S\d{2}E\d{2,4})[\s.]+"
    r"(?P<lang>" + _LANG_ALT + r")[\s.]+"
    r"\((?P<meta>[^)]*)\)\s*$",
    re.IGNORECASE,
)

# --- Format 5 : bracket-group + SxxExx SANS langue explicite --------------
# Découpe : [Groupe] Titre SxxExx [source qualité vidéo] (indice Multi/Dual Subs)
#           ex: [Trix] Grand Blue Dreaming S03E10 [WEBRip 1080p AV1] (Multi Subs)
# Contrairement au format 4, aucune langue n'apparaît en clair juste après
# SxxExx : elle doit être déduite d'un indice "Multi Subs"/"Dual Subs" trouvé
# dans les blocs entre crochets/parenthèses, et placée en FIN de nom (juste
# avant le groupe), sous la forme "Multi-sub"/"Dual-sub". Le codec vidéo
# détecté est toujours conservé tel quel ; si aucun tag audio n'est trouvé,
# "AAC" est ajouté par défaut (contrairement au format fansub qui n'ajoute
# jamais rien par défaut).
_BRACKET_HINT_RE = re.compile(
    r"^\[(?P<group>[^\]]+)\]\s*"
    r"(?P<title>.+?)[\s.]+(?P<se>S\d{2}E\d{2,4})\s*"
    r"(?P<meta>.*)$",
    re.IGNORECASE,
)

_MULTI_DUAL_SUB_HINT_RE = re.compile(r"\b(?P<kind>multi|dual)[\s-]*sub", re.IGNORECASE)

# --- Format 6 : titre nu + SxxExx (+ titre d'épisode), sans aucun tag -----
# Découpe : Titre[_ ou espaces]SxxExx[_ ou espaces]TitreEpisode(optionnel)
#           ex: Mushoku_Tensei_Jobless_Reincarnation_S03E08_The_Flying_Fortress
#               The Apothecary Diaries S01E01 Maomao
# Aucune info de langue/qualité/plateforme/source/codec n'est présente : le
# titre d'épisode éventuel est ignoré (comme le "middle" du format scene),
# et on force systématiquement "MULTI (WEB-DL) H.264 AAC" en l'absence de
# toute autre info exploitable.
_BARE_SE_RE = re.compile(
    r"^(?P<title>.+?)[\s_]+(?P<se>S\d{2}E\d{2,4})"
    r"(?:[\s_]+(?P<episode_title>.+))?$",
    re.IGNORECASE,
)


def resolution_label(height: int) -> str:
    return f"{height}p"


def _clean_title(raw: str) -> str:
    # Underscore ajouté aux séparateurs normalisés (format 6 : titres en
    # underscores style "Mushoku_Tensei_Jobless_Reincarnation").
    return re.sub(r"[._\s]+", " ", raw).strip(" .-_")


def _search_token(text: str, alt: str) -> str | None:
    m = re.search(r"\b(?:" + alt + r")\b", text, re.IGNORECASE)
    return m.group(0) if m else None


def _final_lang(lang: str | None, dual: bool, multi_hint: bool) -> str:
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

    middle = (parts.get("middle") or "").strip(" .")
    lang_tag = middle if middle and _LANG_TOKEN_FULL_RE.match(middle) else None

    lang = _final_lang(lang_tag, bool(parts.get("dual")), False)
    platform = parts["platform"].upper() if parts["platform"] else None

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

    meta_clean = re.sub(
        r"[\[(]([^\])]*)[\])]",
        lambda m: "" if _HEX_HASH_RE.match(m.group(1).strip()) else f" {m.group(1)} ",
        parts["meta"] or "",
    )

    multi_hint = bool(_MULTI_RE.search(meta_clean))
    lang = _final_lang(None, False, multi_hint)

    quality = _search_token(meta_clean, r"\d{3,4}p")
    platform = _search_token(meta_clean, _PLATFORM_ALT)
    source = _search_token(meta_clean, _SOURCE_ALT)
    audio = _search_token(meta_clean, _AUDIO_ALT)
    video = _search_token(meta_clean, _VIDEO_ALT)

    new_base = _join_parts(
        title, se, lang, quality,
        platform.upper() if platform else None,
        source, audio, video,
    )
    return f"{new_base}-{NEW_GROUP_TAG}"


def _try_bracket_scene_format(base: str) -> str | None:
    """
    Format 4 : [Groupe] Titre SxxExx LANG (source qualité vidéo audio ...).

    La saison/épisode et la langue sont déjà explicites, seul le blob entre
    parenthèses est analysé par recherche de token (ordre non garanti). La
    source reste entre parenthèses dans le nom final ; qualité, plateforme,
    vidéo et audio en sortent, dans cet ordre fixe.
    """
    match = _BRACKET_SCENE_RE.match(base.strip())
    if not match:
        return None
    parts = match.groupdict()
    title = _clean_title(parts["title"])
    meta = parts["meta"] or ""

    lang = _final_lang(parts["lang"], False, False)

    source = _search_token(meta, _SOURCE_ALT)
    quality = _search_token(meta, r"\d{3,4}p")
    platform = _search_token(meta, _PLATFORM_ALT)
    video = _search_token(meta, _VIDEO_ALT)
    audio = _search_token(meta, _AUDIO_ALT)

    source_part = f"({source})" if source else None

    new_base = _join_parts(
        title, parts["se"], lang, source_part, quality,
        platform.upper() if platform else None,
        video, audio,
    )
    return f"{new_base}-{NEW_GROUP_TAG}"


def _try_bracket_hint_format(base: str) -> str | None:
    """
    Format 5 : [Groupe] Titre SxxExx [source qualité vidéo] (Multi/Dual Subs).

    Pas de langue explicite après SxxExx (sinon c'est le format 4). La
    langue est déduite d'un indice "Multi Subs"/"Dual Subs" cherché dans
    tout le blob restant, et placée en fin de nom ("Multi-sub"/"Dual-sub").
    Codec vidéo conservé tel quel ; audio par défaut "AAC" si absent.
    """
    match = _BRACKET_HINT_RE.match(base.strip())
    if not match:
        return None
    parts = match.groupdict()
    title = _clean_title(parts["title"])
    if not title:
        return None
    se = parts["se"].upper()
    meta = re.sub(r"[\[\]()]", " ", parts.get("meta") or "")

    hint_match = _MULTI_DUAL_SUB_HINT_RE.search(meta)
    if not hint_match:
        # Pas d'indice Multi/Dual Subs -> ce n'est pas ce format (évite de
        # capturer par erreur un nom qui devrait matcher un autre format,
        # ex: le format 4 avec langue explicite ou le fansub classique).
        return None

    source = _search_token(meta, _SOURCE_ALT)
    quality = _search_token(meta, r"\d{3,4}p")
    video = _search_token(meta, _VIDEO_ALT)
    audio = _search_token(meta, _AUDIO_ALT) or "AAC"
    lang_label = hint_match.group("kind").capitalize() + "-sub"

    new_base = _join_parts(title, se, source, quality, video, audio, lang_label)
    return f"{new_base}-{NEW_GROUP_TAG}"


def _try_bare_se_format(base: str) -> str | None:
    """
    Format 6 : Titre (espaces ou underscores) SxxExx [titre d'épisode].

    Aucun groupe, aucune langue/qualité/source/codec dans le nom source :
    le titre d'épisode éventuel est ignoré et "MULTI (WEB-DL) H.264 AAC"
    est toujours ajouté par défaut.
    """
    match = _BARE_SE_RE.match(base.strip())
    if not match:
        return None
    parts = match.groupdict()
    title = _clean_title(parts["title"])
    if not title:
        return None
    se = parts["se"].upper()

    new_base = _join_parts(title, se, "MULTI", "(WEB-DL)", "H.264", "AAC")
    return f"{new_base}-{NEW_GROUP_TAG}"


def _try_underscore_format(base: str) -> str | None:
    match = _UNDERSCORE_RE.match(base.strip())
    if not match:
        return None
    rest = match.group("rest").strip(" .-_")
    if not rest:
        return None

    lang_tag = _search_token(rest, _LANG_ALT)
    title_part = rest
    if lang_tag:
        title_part = re.sub(r"\b" + re.escape(lang_tag) + r"\b", "", rest, count=1)
    title = _clean_title(title_part)
    if not title:
        return None

    lang = _final_lang(lang_tag, False, False)
    quality = _search_token(rest, r"\d{3,4}p")
    platform = _search_token(rest, _PLATFORM_ALT)
    source = _search_token(rest, _SOURCE_ALT) or "WEB-DL"
    audio = _search_token(rest, _AUDIO_ALT)
    video = _search_token(rest, _VIDEO_ALT)

    new_base = _join_parts(
        title, lang, quality,
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
    base, ext = os.path.splitext(real_filename)
    base = base.strip()

    new_base = (
        _try_scene_format(base)
        or _try_bracket_scene_format(base)
        or _try_bracket_hint_format(base)
        or _try_fansub_format(base)
        or _try_underscore_format(base)
        or _try_bare_se_format(base)
    )
    if new_base is None:
        return real_filename

    if override_quality:
        if re.search(r"\d{3,4}p", new_base):
            new_base = re.sub(r"\d{3,4}p", override_quality, new_base, count=1)
        else:
            new_base = new_base.replace(
                f"-{NEW_GROUP_TAG}", f" {override_quality}-{NEW_GROUP_TAG}"
            )

    final_ext = f".{output_ext.lstrip('.')}" if output_ext else ext
    return f"{new_base}{final_ext}"


def finalize_download(
    real_path: str,
    *,
    override_quality: str | None = None,
    output_ext: str | None = None,
) -> str:
    """
    Renomme SUR LE DISQUE un fichier déjà téléchargé sous son vrai nom pour
    qu'il porte le nom final reconstruit (voir build_final_name).

    Contrairement au pipeline FreeConvert (hardsub_remote_url dans
    free_convert.py), qui calcule le nom final AVANT de télécharger et
    écrit donc directement dedans (jamais de "vrai nom" sur le disque à
    supprimer), cette fonction sert aux pipelines qui téléchargent d'abord
    sous le nom réel de la source, puis doivent le remplacer après coup.

    real_path : chemin complet du fichier déjà sur le disque, sous son
    vrai nom (ex: "/downloads/RILAKKUMA S01E23 ... -Tsundere-Raws.mkv").

    Renvoie le nouveau chemin complet (identique à real_path si le nom
    n'a pas changé, ex: format non reconnu par build_final_name).

    Si le nom ne change pas, aucune opération disque n'est faite. Sinon,
    os.replace() est utilisé (rename atomique qui écrase une éventuelle
    destination déjà existante) plutôt qu'un remove()+rename() séparé,
    pour éviter de perdre le fichier en cas d'erreur entre les deux étapes.
    """
    real_dir = os.path.dirname(real_path)
    real_name = os.path.basename(real_path)

    new_name = build_final_name(
        real_name, override_quality=override_quality, output_ext=output_ext
    )
    if new_name == real_name:
        return real_path

    new_path = os.path.join(real_dir, new_name)

    if not os.path.exists(real_path):
        log.warning("finalize_download: fichier source introuvable: %s", real_path)
        return real_path

    try:
        os.replace(real_path, new_path)
    except OSError as exc:
        log.warning(
            "finalize_download: échec du renommage %s -> %s (%s), fichier conservé sous son vrai nom.",
            real_path, new_path, exc,
        )
        return real_path

    return new_path


if __name__ == "__main__":
    tests = [
        "RILAKKUMA S01E23 SUBFRENCH 1080p CR WEB-DL AAC2.0 H.264-Tsundere-Raws.mkv",
        "SOME ANIME S02E05 MULTI 1080p CR WEB-DL AAC2.0 H.264-SomeGroup.mkv",
        "OTHER ANIME S01E01 VOSTFR 1080p ADN WEB-DL AAC2.0 H.264-OldGroup.mkv",
        "OTHER ANIME S01E01 VOSTFR 1080p AMZN WEB-DL AAC2.0 H.264-OldGroup.mkv",
        "UNPARSEABLE NAME.mkv",
        "[Erai-raws] Rilakkuma - 23 [1080p CR WEB-DL AVC AAC][MultiSub][45752258].mkv",
        "[SubsPlease] Rilakkuma - 23 (480p) [E2E141F1].mkv",
        "RILAKKUMA.S01E23.1080p.CR.WEB-DL.DUAL.AAC2.0.H.264.MSubs-ToonsHub.mkv",
        "RILAKKUMA S01E23 1080p CR WEB-DL DUAL AAC2.0 H.264-VARYG.mkv",
        "Though.I.Am.an.Inept.Villainess.S01E09.I.Will.Make.Sure.to.Save.You.1080p.DSNP.WEB-DL.AAC2.0.H.264-VARYG.mkv",
        "_Meda-Arcedo_ Virgin Punk Clockwork Girl VOSTFR.mp4",
        "_SomeRipper_ Another Show Title MULTI.mkv",
        "_SomeRipper_ Another Show Title.mp4",
    ]
    for name in tests:
        print(f"{name}\n  -> {build_final_name(name)}\n")

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

    fc_underscore = "_Meda-Arcedo_ Virgin Punk Clockwork Girl VOSTFR.mp4"
    print(
        f"{fc_underscore}\n  -> "
        f"{build_final_name(fc_underscore, override_quality=resolution_label(480), output_ext='mp4')}\n"
    )
