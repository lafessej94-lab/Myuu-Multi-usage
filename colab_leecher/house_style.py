r"""
services/subtitle_style.py

Pré-stylage des sous-titres avant hardsub (tous moteurs : CC, FC, FFmpeg local).

FreeConvert ne propose pas d'option "force_style" comme ffmpeg (contrairement
à CloudConvert où on peut injecter n'importe quelle commande ffmpeg). Leur API
se contente de brûler le fichier .ass/.srt tel quel, avec le style déjà écrit
dedans.

Solution : on réécrit nous-mêmes le bloc [V4+ Styles] du fichier .ass avant
de l'envoyer — le moteur applique alors CE style au moment du burn, peu importe
sa capacité à accepter des options de style en paramètre.

Si le fichier source est un .srt (pas de style), on le convertit d'abord en
.ass via ffmpeg pour obtenir un header standard, qu'on écrase ensuite.

House style basée sur le fichier ASS Crunchyroll de Mushoku Tensei S3E9
(PlayRes 640x360). Contrairement à l'ancienne version qui appliquait un style
UNIQUE et uniforme à tous les noms de style trouvés dans la source (ce qui
cassait le positionnement des lignes non-dialogue, ex: TopCenter réécrit en
bas d'écran), cette version conserve un profil par position ASS standard
(TopLeft, TopCenter, ..., BottomRight) et ne retombe sur le profil dialogue
bas-centré que pour un nom de style inconnu (raw non-CR, style "Italique",
"Sign", etc.).

MAJ 1 : le profil visuel (police, taille, contour, ombre, marges) est
désormais IDENTIQUE quelle que soit la position ASS. Seuls l'alignment
(numpad) et les marges associées changent selon le nom de style trouvé dans
la source. Avant ce correctif, les lignes non-BottomCenter/Default (ex:
TopCenter) recevaient un profil "accent" différent (taille 23 au lieu de 22,
outline 2 sans ombre au lieu de outline 1 + ombre 1), ce qui rendait ces
lignes visiblement différentes du reste du sous-titre (rendu plus épais et
sans ombre) alors qu'elles font pourtant partie du même sous-titrage. Le but
recherché est un seul et même "style maison" partout, avec juste le
positionnement à l'écran qui change.

MAJ 2 : on ne réécrit plus PlayResX/PlayResY à une valeur fixe (640x360).
Sur des sources autres que Mushoku Tensei S3E9 (raws/fansubs calibrés en
1920x1080 ou autre), forcer le PlayRes cassait le positionnement de toute
ligne utilisant des tags \pos()/\move() en coordonnées absolues : ces
coordonnées sont écrites par rapport au PlayRes DÉCLARÉ par la source, donc
changer ce PlayRes après coup décale le texte à l'écran, indépendamment du
style appliqué.

Le moteur de rendu ASS scale de toute façon le rendu (texte ET \pos())
proportionnellement au ratio "résolution vidéo réelle / PlayRes déclaré".
On garde donc le PlayRes d'origine du fichier source intact (les \pos()
restent justes), et on scale à la place fontsize/outline/shadow/margins de
HOUSE_STYLE selon le ratio "PlayResY_source / 360" (360 = référence
Mushoku Tensei) pour obtenir la même taille apparente à l'écran, quelle que
soit la résolution dans laquelle la source a été calibrée.
"""
import os
import re
import subprocess
from dataclasses import dataclass, replace
from os import path as ospath


@dataclass(frozen=True)
class AssStyle:
    fontname: str = "Trebuchet MS"
    fontsize: int = 22
    primary_colour: str = "&H00FFFFFF"   # blanc pur (format ASS: &HAABBGGRR)
    secondary_colour: str = "&H00FFFFFF"
    outline_colour: str = "&H00000000"   # contour noir
    back_colour: str = "&H00000000"      # couleur de l'ombre portée (utilisée aussi comme back-color de la boîte en BorderStyle=3)
    bold: int = -1                       # -1 = gras activé en ASS (0 = désactivé)
    italic: int = 0
    border_style: int = 1                # 1 = contour + ombre, 3 = boîte pleine
    outline: float = 1
    shadow: float = 1
    alignment: int = 2                   # numpad ASS (voir STYLE_NAME_ALIGNMENT)
    margin_l: int = 20
    margin_r: int = 20
    margin_v: int = 20


# ── Profil unique ─────────────────────────────────────────────────────────
# Un seul et même rendu visuel (police, taille, contour, ombre) pour TOUTES
# les positions ASS. Seul l'alignment (et donc la position à l'écran) varie
# selon le nom de style trouvé dans la source — voir _profile_for_style_name.
HOUSE_STYLE = AssStyle(fontsize=22, outline=1, shadow=1, alignment=2)

# Alias conservés pour compat (ancien code/imports qui référencerait encore
# ces noms) : les deux profils "dialogue" et "accent" sont désormais
# strictement identiques.
DIALOGUE_STYLE = HOUSE_STYLE
ACCENT_STYLE = HOUSE_STYLE

# Style par défaut utilisé pour tout nom de style non reconnu (fallback sûr,
# identique au comportement de l'ancienne version).
DEFAULT_HARDSUB_STYLE = HOUSE_STYLE

# Alignment ASS (numpad layout) par nom de style CR standard.
STYLE_NAME_ALIGNMENT = {
    "TopLeft": 7, "TopCenter": 8, "TopRight": 9,
    "CenterLeft": 4, "CenterCenter": 5, "CenterRight": 6,
    "BottomLeft": 1, "BottomCenter": 2, "BottomRight": 3,
    "Default": 2,
}

# Noms de style (hors les 9 positions CR standard) qui désignent en réalité
# des incrustations à l'écran (cartons de titre, panneaux, texte visible
# dans l'image) plutôt que du dialogue -- ex: "Sign" chez Erai-raws et la
# plupart des fansubs. Gardé comme signal supplémentaire en complément de la
# détection par tags (voir _classify_style_names) : un nom qui matche ici est
# toujours traité comme overlay même si ses lignes n'ont, par coïncidence,
# aucun tag graphique (fichier très simple, ex: juste "SIGN" en texte brut).
_SIGN_STYLE_MARKERS = ("sign", "signe", "carton", "panneau", "onscreen", "on-screen")
_OVERLAY_ALIGNMENT = 8  # top-center — évite la collision avec le dialogue en bas

# Tags ASS qui trahissent une incrustation stylée "à la main" (statut de jeu,
# nom de compétence/monstre, carton de titre...) plutôt qu'une simple ligne
# de dialogue : changement de couleur, taille de police custom, flou,
# masque de révélation, fondu, animation, échelle horizontale/verticale.
# Une ligne de dialogue normale (même "Italique" ou "TiretsDefault") n'a
# jamais ces tags — au pire un simple {\i1}/{\i0}.
_OVERLAY_TAG_PATTERN = re.compile(
    r"\\c&|\\[1234]c&|\\fs\d|\\blur|\\clip\(|\\fad\(|\\t\(|\\fscx|\\fscy"
)
# \pos()/\move() sans \an accompagnant sur la même ligne : la position réelle
# à l'écran dépend alors de l'Alignment du Style (c'est lui qui définit quel
# point du texte correspond aux coordonnées données). Changer l'alignment
# d'un tel style casserait le placement calculé par le fansubber -- il faut
# impérativement garder l'alignment déclaré par la source pour ces noms-là.
_POS_TAG_PATTERN = re.compile(r"\\pos\(|\\move\(")
_AN_TAG_PATTERN = re.compile(r"\\an[0-9]")

# Noms de style qui désignent une variante EN ITALIQUE du dialogue (pensées,
# narration, voix off...) -- ex: "Italique" chez Erai-raws et la plupart des
# fansubs FR. HOUSE_STYLE a italic=0 par défaut (texte droit) : sans ce
# correctif, un style nommé "Italique" perdrait son italique après passage
# dans notre outil, quelle que soit la valeur Italic déclarée par la source,
# puisqu'on réécrit tout le bloc [V4+ Styles] avec un seul profil commun.
# Détection par sous-chaîne insensible à la casse -> couvre "Italique",
# "Italic", "ItaliqueDefault", etc.
_ITALIC_STYLE_MARKERS = ("italiq", "italic")

# Résolution de référence du script — DOIT matcher celle du fichier source
# (640x360), sinon la taille de police ne sera pas à l'échelle correcte une
# fois le style appliqué à une vraie vidéo 1080p (ASS scale le rendu selon
# le ratio actual_resolution / PlayRes).
PLAY_RES_X = 640
PLAY_RES_Y = 360


def _scale_house_style(source_play_res_y: int) -> AssStyle:
    """
    Renvoie HOUSE_STYLE mis à l'échelle pour que sa taille apparente à
    l'écran reste identique quel que soit le PlayResY déclaré par la
    source (au lieu de forcer un PlayRes fixe, ce qui casserait les
    \\pos()/\\move() en coordonnées absolues déjà présents dans le fichier).

    Le ratio est calculé par rapport à PLAY_RES_Y (360, la résolution de
    référence de Mushoku Tensei) : une source calibrée en 1080p (PlayResY
    ~1080) aura donc un fontsize/outline/shadow/margins x3 par rapport aux
    valeurs de base de HOUSE_STYLE, pour un rendu visuellement équivalent.
    """
    if not source_play_res_y or source_play_res_y <= 0:
        scale = 1.0
    else:
        scale = source_play_res_y / PLAY_RES_Y

    return replace(
        HOUSE_STYLE,
        fontsize=max(1, round(HOUSE_STYLE.fontsize * scale)),
        outline=round(HOUSE_STYLE.outline * scale, 2),
        shadow=round(HOUSE_STYLE.shadow * scale, 2),
        margin_l=max(0, round(HOUSE_STYLE.margin_l * scale)),
        margin_r=max(0, round(HOUSE_STYLE.margin_r * scale)),
        margin_v=max(0, round(HOUSE_STYLE.margin_v * scale)),
    )


def _classify_style_names(lines: list[str]) -> tuple[dict[str, int], dict[str, bool], dict[str, bool]]:
    """
    Analyse le fichier source (styles déclarés + lignes [Events]) pour
    déterminer, pour chaque nom de style :
    - son alignment déclaré à l'origine dans [V4+ Styles] (source_alignment) ;
    - si au moins une de ses lignes utilise \\pos()/\\move() SANS \\an
      correspondant (has_unanchored_pos) -> alignment à préserver tel quel ;
    - si au moins une de ses lignes porte des tags d'incrustation stylée
      (has_overlay_tags) -> candidat à un repositionnement en haut d'écran
      pour éviter toute collision avec le dialogue.

    Ça permet de distinguer automatiquement, sans liste de noms à maintenir
    à la main :
    - un vrai variant de dialogue (ex: "Italique", "TiretsDefault") : pas de
      tags graphiques -> reste aligné comme le dialogue.
    - une incrustation positionnée à la main via \\pos() (ex: un carton de
      titre) : alignment déclaré préservé tel quel, pour ne pas casser le
      point d'ancrage utilisé par le fansubber.
    - une incrustation sans \\pos() du tout (ex: un encart de statut/jeu) :
      remontée en haut d'écran pour ne jamais chevaucher le dialogue.
    """
    source_alignment: dict[str, int] = {}
    in_styles = False
    for line in lines:
        stripped = line.strip()
        if stripped.lower() in ("[v4+ styles]", "[v4 styles]"):
            in_styles = True
            continue
        if in_styles:
            if stripped.startswith("["):
                in_styles = False
                continue
            if stripped.lower().startswith("style:"):
                fields = stripped.split(":", 1)[1].split(",")
                if len(fields) > 18:
                    name = fields[0].strip()
                    try:
                        source_alignment[name] = int(float(fields[18].strip()))
                    except ValueError:
                        pass

    has_unanchored_pos: dict[str, bool] = {}
    has_overlay_tags: dict[str, bool] = {}
    in_events = False
    for line in lines:
        stripped = line.strip()
        if stripped.lower() == "[events]":
            in_events = True
            continue
        if in_events and stripped.startswith("[") and stripped.lower() != "[events]":
            in_events = False
        if not in_events or not stripped.lower().startswith("dialogue:"):
            continue
        rest = stripped[len("dialogue:"):].strip()
        fields = rest.split(",", 9)
        if len(fields) < 10:
            continue
        style_name = fields[3].strip()
        text = fields[9]

        if _POS_TAG_PATTERN.search(text) and not _AN_TAG_PATTERN.search(text):
            has_unanchored_pos[style_name] = True
        if _OVERLAY_TAG_PATTERN.search(text):
            has_overlay_tags[style_name] = True

    return source_alignment, has_unanchored_pos, has_overlay_tags


def _profile_for_style_name(
    name: str,
    scaled_style: AssStyle,
    source_alignment: dict[str, int],
    has_unanchored_pos: dict[str, bool],
    has_overlay_tags: dict[str, bool],
) -> AssStyle:
    """
    Retourne l'AssStyle à utiliser pour un nom de style donné, à partir du
    HOUSE_STYLE déjà mis à l'échelle (scaled_style) pour ce fichier source.

    Le rendu (police, taille, contour, ombre, marges) est toujours celui de
    scaled_style. Seul l'alignment change, décidé dans cet ordre :
    1. Nom reconnu parmi les 9 positions CR + Default -> alignment correspondant.
    2. Au moins une ligne de ce style utilise \\pos()/\\move() sans \\an
       -> on garde l'alignment D'ORIGINE déclaré dans la source, pour ne pas
       casser le point d'ancrage calculé par le fansubber.
    3. Sinon, si le style porte des tags d'incrustation stylée OU que son nom
       ressemble à un panneau/carton (_SIGN_STYLE_MARKERS) -> haut d'écran,
       pour ne jamais chevaucher le dialogue.
    4. Sinon (vrai variant de dialogue, ex: "Italique") -> alignment par
       défaut du dialogue (bas-centré), comme avant.
    """
    if name in STYLE_NAME_ALIGNMENT:
        profile = replace(scaled_style, alignment=STYLE_NAME_ALIGNMENT[name])
    elif has_unanchored_pos.get(name):
        alignment = source_alignment.get(name, scaled_style.alignment)
        profile = replace(scaled_style, alignment=alignment)
    else:
        lname = name.lower()
        is_overlay = has_overlay_tags.get(name, False) or any(
            marker in lname for marker in _SIGN_STYLE_MARKERS
        )
        if is_overlay:
            profile = replace(scaled_style, alignment=_OVERLAY_ALIGNMENT)
        else:
            profile = replace(scaled_style, alignment=scaled_style.alignment)

    # Le flag Italic est ensuite ajusté indépendamment de l'alignment : un
    # nom de style "Italique" doit garder son rendu en italique même s'il a
    # aussi été classé "overlay" ou "position préservée" ci-dessus -- les
    # deux logiques (position et italique) sont orthogonales.
    if any(marker in name.lower() for marker in _ITALIC_STYLE_MARKERS):
        profile = replace(profile, italic=-1)

    return profile


def _ass_style_line(style: AssStyle, name: str = "Default") -> str:
    """Construit la ligne 'Style:' au format ASS v4+."""
    # Certains moteurs de burn-in "simplifiés" (dont FreeConvert) ignorent le
    # flag Bold du style et se contentent de chercher la police par son nom
    # exact. On ajoute donc "Bold" au nom de la police en plus du flag —
    # double sécurité qui ne casse rien pour les moteurs qui respectent le
    # flag normalement (testé/confirmé : forcer le nom donne le même rendu
    # gras qu'un vrai Bold=-1, indépendamment du flag).
    fontname = f"{style.fontname} Bold" if style.bold else style.fontname
    fields = [
        name, fontname, str(style.fontsize),
        style.primary_colour, style.secondary_colour,
        style.outline_colour, style.back_colour,
        str(style.bold), str(style.italic),
        "0", "0",              # Underline, StrikeOut
        "100", "100",          # ScaleX, ScaleY
        "0", "0",               # Spacing, Angle
        str(style.border_style), str(style.outline), str(style.shadow),
        str(style.alignment),
        str(style.margin_l), str(style.margin_r), str(style.margin_v),
        "1",                    # Encoding
    ]
    return "Style: " + ",".join(fields)


_STYLE_FORMAT_HEADER = (
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
    "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
    "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
    "Alignment, MarginL, MarginR, MarginV, Encoding"
)


def _srt_to_ass(srt_path: str, ass_path: str) -> None:
    """Convertit un .srt en .ass basique via ffmpeg (header par défaut, sera écrasé après)."""
    cmd = ["ffmpeg", "-y", "-i", srt_path, ass_path]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    if result.returncode != 0 or not ospath.exists(ass_path):
        raise RuntimeError(f"Échec conversion srt->ass: {result.stderr.decode(errors='ignore')[:300]}")


def apply_hardsub_style(subtitle_path: str, output_path: str) -> str:
    """
    Force le style de rendu d'un sous-titre (.srt ou .ass) et écrit le résultat
    en .ass prêt à être envoyé au burn-in (FC, CC, ou FFmpeg local).

    Chaque nom de style trouvé dans le fichier source reçoit le même rendu
    visuel (HOUSE_STYLE) ; seul l'alignment change selon le nom (voir
    _profile_for_style_name) : un style "BottomCenter" ou "TopCenter" garde
    son positionnement d'origine mais un rendu strictement identique
    (police, taille, contour, ombre), un style non reconnu retombe sur
    l'alignment bas-centré par défaut.

    Retourne le chemin du fichier .ass stylé (= output_path).
    """
    ext = ospath.splitext(subtitle_path)[1].lower()
    work_path = subtitle_path

    if ext == ".srt":
        tmp_ass = output_path + ".tmp.ass"
        _srt_to_ass(subtitle_path, tmp_ass)
        work_path = tmp_ass
    elif ext not in (".ass", ".ssa"):
        raise ValueError(f"Format de sous-titre non supporté: {ext}")

    with open(work_path, "r", encoding="utf-8-sig", errors="replace") as fh:
        lines = fh.readlines()

    # 1er passage : on récupère les noms de tous les styles définis dans le
    # fichier source (pour l'alignment) ET le PlayResY déclaré par la source
    # (pour scaler HOUSE_STYLE à une taille apparente équivalente, sans
    # jamais réécrire le PlayRes lui-même -- voir _scale_house_style).
    style_names: list[str] = []
    source_play_res_y: int = 0
    in_styles_scan = False
    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith("playresy:"):
            try:
                source_play_res_y = int(float(stripped.split(":", 1)[1].strip()))
            except (ValueError, IndexError):
                pass
            continue
        if stripped.lower() in ("[v4+ styles]", "[v4 styles]"):
            in_styles_scan = True
            continue
        if in_styles_scan:
            if stripped.startswith("["):
                in_styles_scan = False
                continue
            if stripped.lower().startswith("style:"):
                name = stripped.split(":", 1)[1].split(",", 1)[0].strip()
                if name and name not in style_names:
                    style_names.append(name)
    if not style_names:
        style_names = ["Default"]

    # PlayResY absent/à 0 (rare, certains raws n'en déclarent pas) -> on
    # suppose la référence Mushoku Tensei (360), donc scale = 1.0, aucun
    # changement de taille par rapport au comportement actuel.
    scaled_style = _scale_house_style(source_play_res_y or PLAY_RES_Y)

    # Analyse des lignes [Events] pour détecter, par nom de style : un \pos()
    # non ancré (alignment source à préserver) ou des tags d'incrustation
    # stylée (candidat à un repositionnement en haut d'écran) -- voir
    # _classify_style_names pour le détail du raisonnement.
    source_alignment, has_unanchored_pos, has_overlay_tags = _classify_style_names(lines)

    # 2e passage : on reconstruit le fichier en remplaçant tout le bloc de
    # styles par une ligne "Style:" par nom trouvé, chacune avec son alignment.
    out_lines: list[str] = []
    in_styles_section = False
    styles_written = False

    for line in lines:
        stripped = line.strip()

        # PlayResX/PlayResY de la source ne sont PLUS réécrits : les
        # \pos()/\move() déjà présents dans le fichier sont calculés par
        # rapport à ce PlayRes d'origine. Le renderer ASS scale de toute
        # façon tout le rendu (texte + coordonnées) selon le ratio
        # résolution_vidéo/PlayRes, donc garder le PlayRes source préserve
        # le positionnement -- c'est le scaling de HOUSE_STYLE
        # (_scale_house_style) qui compense pour garder la même taille
        # apparente de police.

        if stripped.lower() in ("[v4+ styles]", "[v4 styles]"):
            in_styles_section = True
            out_lines.append("[V4+ Styles]\n")
            out_lines.append(_STYLE_FORMAT_HEADER + "\n")
            for name in style_names:
                profile = _profile_for_style_name(
                    name, scaled_style, source_alignment, has_unanchored_pos, has_overlay_tags
                )
                out_lines.append(_ass_style_line(profile, name=name) + "\n")
            styles_written = True
            continue

        if in_styles_section:
            # On saute tout l'ancien bloc de styles (Format: + toutes les Style:)
            if stripped.startswith("[") and stripped.lower() not in ("[v4+ styles]", "[v4 styles]"):
                in_styles_section = False
                out_lines.append(line)
            # sinon on ignore la ligne (ancien Format:/Style:)
            continue

        out_lines.append(line)

    if not styles_written:
        # Pas de section styles trouvée (rare) -> on l'ajoute avant [Events]
        final_lines: list[str] = []
        inserted = False
        for line in out_lines:
            if line.strip().lower() == "[events]" and not inserted:
                final_lines.append("[V4+ Styles]\n")
                final_lines.append(_STYLE_FORMAT_HEADER + "\n")
                for name in style_names:
                    profile = _profile_for_style_name(
                        name, scaled_style, source_alignment, has_unanchored_pos, has_overlay_tags
                    )
                    final_lines.append(_ass_style_line(profile, name=name) + "\n")
                final_lines.append("\n")
                inserted = True
            final_lines.append(line)
        out_lines = final_lines

    with open(output_path, "w", encoding="utf-8") as fh:
        fh.writelines(out_lines)

    if work_path != subtitle_path and ospath.exists(work_path):
        os.remove(work_path)

    return output_path


# ── Compatibilité avec l'ancien appelant ─────────────────────────────────────
# L'ancien module exposait `async def apply_house_style(sub_path, tmp_dir)`,
# appelé ainsi dans le reste du pipeline (hardsub CC/FC/local) :
#
#     styled_path = await subtitle_style.apply_house_style(sub_path, tmp_dir)
#
# Ce wrapper garde ce point d'entrée fonctionnel sans toucher aux call sites,
# en le faisant passer par la nouvelle logique apply_hardsub_style() ci-dessus.
async def apply_house_style(sub_path: str, tmp_dir: str) -> str:
    out_path = os.path.join(tmp_dir, "hs_house_styled.ass")
    try:
        return apply_hardsub_style(sub_path, out_path)
    except Exception:
        # Fallback silencieux comme l'ancien module : le job continue avec
        # le sous-titre original plutôt que de planter.
        return sub_path
