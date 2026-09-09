r"""
colab_leecher/house_style.py

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
la source.

MAJ 2 : on ne réécrit plus PlayResX/PlayResY à une valeur fixe (640x360).
Le moteur de rendu ASS scale de toute façon le rendu (texte ET \pos())
proportionnellement au ratio "résolution vidéo réelle / PlayRes déclaré".
On garde donc le PlayRes d'origine du fichier source intact, et on scale à
la place fontsize/outline/shadow/margins du profil choisi selon le ratio
"PlayResY_source / 360" (360 = référence Mushoku Tensei) pour obtenir la
même taille apparente à l'écran, quelle que soit la résolution dans laquelle
la source a été calibrée.

MAJ 3 (presets multiples) : deux profils de rendu nommés "a" et "b" sont
désormais disponibles dans STYLE_PRESETS, sélectionnables par l'utilisateur
avant chaque hardsub (menu Telegram, voir main.py) via le paramètre
`style_key` de apply_hardsub_style()/apply_house_style(). Actuellement les
deux presets utilisent la même police (Trebuchet MS) à la demande de
l'utilisateur — ils produisent donc un rendu visuellement identique. Pour
les différencier plus tard, il suffit de modifier la valeur de STYLE_B
ci-dessous (ex: fontname="Arial") ; tout le reste du pipeline (menu, burn
FC/CC) fonctionne déjà sans autre changement.
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


# ── Profils disponibles ──────────────────────────────────────────────────
# Style A : profil historique (inchangé) — un seul rendu uniforme
# (Trebuchet MS) appliqué à tous les noms de style trouvés dans la source,
# seul l'alignment change selon le nom (voir _profile_for_style_name).
STYLE_A = AssStyle(fontname="Trebuchet MS", fontsize=22, outline=1, shadow=1, alignment=2)

# Style B : sert de profil de repli pour Style B sur un nom de style non
# reconnu (voir RAW_CR_STYLE_PROFILES juste en dessous, qui prend le dessus
# pour les 7 noms standard Crunchyroll).
STYLE_B = STYLE_A

STYLE_PRESETS: dict[str, AssStyle] = {"a": STYLE_A, "b": STYLE_B}
DEFAULT_STYLE_KEY = "a"

STYLE_PRESET_LABELS: dict[str, str] = {
    "a": "Style A",
    "b": "Style B (CR)",
}

# ── Style B : reproduction fidèle d'un fichier ASS Crunchyroll de référence ──
# Contrairement à Style A (un seul rendu uniforme), Style B garde le rendu
# ORIGINAL par nom de style tel que fourni par Crunchyroll : police, taille,
# couleurs, contour, ombre et marges propres à chaque nom (Default,
# Italique, titre-ep, Sign, preview, preview-title, TiretsDefault). Valeurs
# reprises telles quelles du fichier de référence (PlayRes 640x360) ; comme
# pour Style A, elles sont mises à l'échelle selon le PlayResY de la source
# réelle (voir _scale_ass_style / _raw_profile_for_name).
RAW_CR_STYLE_PROFILES: dict[str, AssStyle] = {
    "Default": AssStyle(
        fontname="Trebuchet MS", fontsize=22,
        primary_colour="&H00FFFFFF", secondary_colour="&H000000FF",
        outline_colour="&H00000000", back_colour="&H00000000",
        bold=0, italic=0, border_style=1, outline=2, shadow=1,
        alignment=2, margin_l=2, margin_r=2, margin_v=25,
    ),
    "Italique": AssStyle(
        fontname="Trebuchet MS", fontsize=22,
        primary_colour="&H00FFFFFF", secondary_colour="&H000000FF",
        outline_colour="&H00000000", back_colour="&H00000000",
        bold=0, italic=-1, border_style=1, outline=2, shadow=1,
        alignment=2, margin_l=2, margin_r=2, margin_v=25,
    ),
    "titre-ep": AssStyle(
        fontname="Times New Roman", fontsize=18,
        primary_colour="&H00FFFFFF", secondary_colour="&H000000FF",
        outline_colour="&H00000000", back_colour="&H00000000",
        bold=-1, italic=0, border_style=1, outline=1, shadow=1,
        alignment=3, margin_l=10, margin_r=42, margin_v=70,
    ),
    "Sign": AssStyle(
        fontname="Arial", fontsize=18,
        primary_colour="&H00FFFFFF", secondary_colour="&H00CF002D",
        outline_colour="&H00212121", back_colour="&H00000000",
        bold=-1, italic=0, border_style=1, outline=2, shadow=0,
        alignment=8, margin_l=10, margin_r=10, margin_v=20,
    ),
    "preview": AssStyle(
        fontname="Times New Roman", fontsize=22,
        primary_colour="&H00FFFDFC", secondary_colour="&H000000FF",
        outline_colour="&H00FFFFFF", back_colour="&H00CB8F4B",
        bold=-1, italic=0, border_style=1, outline=0, shadow=0,
        alignment=8, margin_l=10, margin_r=10, margin_v=25,
    ),
    "preview-title": AssStyle(
        fontname="Times New Roman", fontsize=22,
        primary_colour="&H002B2C2A", secondary_colour="&H000000FF",
        outline_colour="&H00FFFFFF", back_colour="&H00CB8F4B",
        bold=0, italic=0, border_style=1, outline=0, shadow=0,
        alignment=8, margin_l=10, margin_r=10, margin_v=25,
    ),
    "TiretsDefault": AssStyle(
        fontname="Trebuchet MS", fontsize=22,
        primary_colour="&H00FFFFFF", secondary_colour="&H000000FF",
        outline_colour="&H00000000", back_colour="&H00000000",
        bold=0, italic=0, border_style=1, outline=2, shadow=1,
        alignment=1, margin_l=20, margin_r=2, margin_v=25,
    ),
}


def normalize_style_key(style_key: str | None) -> str:
    key = (style_key or DEFAULT_STYLE_KEY).strip().lower()
    return key if key in STYLE_PRESETS else DEFAULT_STYLE_KEY


# Alias conservés pour compat (ancien code/imports qui référencerait encore
# ces noms).
HOUSE_STYLE = STYLE_A
DIALOGUE_STYLE = STYLE_A
ACCENT_STYLE = STYLE_A
DEFAULT_HARDSUB_STYLE = STYLE_A

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
# plupart des fansubs.
_SIGN_STYLE_MARKERS = ("sign", "signe", "carton", "panneau", "onscreen", "on-screen")
_OVERLAY_ALIGNMENT = 8  # top-center — évite la collision avec le dialogue en bas

# Tags ASS qui trahissent une incrustation stylée "à la main" (statut de jeu,
# nom de compétence/monstre, carton de titre...) plutôt qu'une simple ligne
# de dialogue : changement de couleur, taille de police custom, flou,
# masque de révélation, fondu, animation, échelle horizontale/verticale.
_OVERLAY_TAG_PATTERN = re.compile(
    r"\\c&|\\[1234]c&|\\fs\d|\\blur|\\clip\(|\\fad\(|\\t\(|\\fscx|\\fscy"
)
# \pos()/\move() sans \an accompagnant sur la même ligne : la position réelle
# à l'écran dépend alors de l'Alignment du Style.
_POS_TAG_PATTERN = re.compile(r"\\pos\(|\\move\(")
_AN_TAG_PATTERN = re.compile(r"\\an[0-9]")

# Noms de style qui désignent une variante EN ITALIQUE du dialogue (pensées,
# narration, voix off...) -- ex: "Italique" chez Erai-raws et la plupart des
# fansubs FR.
_ITALIC_STYLE_MARKERS = ("italiq", "italic")

# Résolution de référence du script — DOIT matcher celle du fichier source
# (640x360), sinon la taille de police ne sera pas à l'échelle correcte une
# fois le style appliqué à une vraie vidéo 1080p (ASS scale le rendu selon
# le ratio actual_resolution / PlayRes).
PLAY_RES_X = 640
PLAY_RES_Y = 360


def _resolution_scale(source_play_res_y: int) -> float:
    """Ratio par rapport à PLAY_RES_Y (360, référence Mushoku Tensei/CR)."""
    if not source_play_res_y or source_play_res_y <= 0:
        return 1.0
    return source_play_res_y / PLAY_RES_Y


def _scale_ass_style(base_style: AssStyle, scale: float) -> AssStyle:
    """Met `base_style` à l'échelle (fontsize/outline/shadow/margins) pour
    obtenir la même taille apparente à l'écran quel que soit le PlayResY de
    la source réelle (voir _resolution_scale)."""
    return replace(
        base_style,
        fontsize=max(1, round(base_style.fontsize * scale)),
        outline=round(base_style.outline * scale, 2),
        shadow=round(base_style.shadow * scale, 2),
        margin_l=max(0, round(base_style.margin_l * scale)),
        margin_r=max(0, round(base_style.margin_r * scale)),
        margin_v=max(0, round(base_style.margin_v * scale)),
    )


def _scale_house_style(source_play_res_y: int, base_style: AssStyle = STYLE_A) -> AssStyle:
    """Compat wrapper — voir _scale_ass_style."""
    return _scale_ass_style(base_style, _resolution_scale(source_play_res_y))


def _raw_profile_for_name(name: str, scale: float) -> AssStyle | None:
    """Renvoie le profil Style B exact (RAW_CR_STYLE_PROFILES) pour ce nom
    de style, mis à l'échelle — ou None si ce nom n'est pas l'un des 7 noms
    standard Crunchyroll (fallback sur le comportement générique Style A
    dans ce cas, voir apply_hardsub_style)."""
    base = RAW_CR_STYLE_PROFILES.get(name)
    if base is None:
        return None
    return _scale_ass_style(base, scale)


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
    profil déjà mis à l'échelle (scaled_style) pour ce fichier source.

    Le rendu (police, taille, contour, ombre, marges) est toujours celui de
    scaled_style. Seul l'alignment change, décidé dans cet ordre :
    1. Nom reconnu parmi les 9 positions CR + Default -> alignment correspondant.
    2. Au moins une ligne de ce style utilise \\pos()/\\move() sans \\an
       -> on garde l'alignment D'ORIGINE déclaré dans la source.
    3. Sinon, si le style porte des tags d'incrustation stylée OU que son nom
       ressemble à un panneau/carton (_SIGN_STYLE_MARKERS) -> haut d'écran.
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

    if any(marker in name.lower() for marker in _ITALIC_STYLE_MARKERS):
        profile = replace(profile, italic=-1)

    return profile


def _ass_style_line(style: AssStyle, name: str = "Default") -> str:
    """Construit la ligne 'Style:' au format ASS v4+."""
    # Certains moteurs de burn-in "simplifiés" (dont FreeConvert) ignorent le
    # flag Bold du style et se contentent de chercher la police par son nom
    # exact. On ajoute donc "Bold" au nom de la police en plus du flag.
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


def apply_hardsub_style(subtitle_path: str, output_path: str, style_key: str = DEFAULT_STYLE_KEY) -> str:
    """
    Force le style de rendu d'un sous-titre (.srt ou .ass) et écrit le résultat
    en .ass prêt à être envoyé au burn-in (FC, CC, ou FFmpeg local).

    `style_key` sélectionne le profil de rendu à appliquer parmi
    STYLE_PRESETS ("a" ou "b", voir en haut du fichier). Chaque nom de style
    trouvé dans le fichier source reçoit ce même rendu visuel ; seul
    l'alignment change selon le nom (voir _profile_for_style_name).

    Retourne le chemin du fichier .ass stylé (= output_path).
    """
    base_style = STYLE_PRESETS[normalize_style_key(style_key)]

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

    scale = _resolution_scale(source_play_res_y or PLAY_RES_Y)
    scaled_style = _scale_ass_style(base_style, scale)
    normalized_key = normalize_style_key(style_key)

    source_alignment, has_unanchored_pos, has_overlay_tags = _classify_style_names(lines)

    def _resolve_profile(name: str) -> AssStyle:
        if normalized_key == "b":
            raw = _raw_profile_for_name(name, scale)
            if raw is not None:
                return raw
        return _profile_for_style_name(
            name, scaled_style, source_alignment, has_unanchored_pos, has_overlay_tags
        )

    out_lines: list[str] = []
    in_styles_section = False
    styles_written = False

    for line in lines:
        stripped = line.strip()

        if stripped.lower() in ("[v4+ styles]", "[v4 styles]"):
            in_styles_section = True
            out_lines.append("[V4+ Styles]\n")
            out_lines.append(_STYLE_FORMAT_HEADER + "\n")
            for name in style_names:
                profile = _resolve_profile(name)
                out_lines.append(_ass_style_line(profile, name=name) + "\n")
            styles_written = True
            continue

        if in_styles_section:
            if stripped.startswith("[") and stripped.lower() not in ("[v4+ styles]", "[v4 styles]"):
                in_styles_section = False
                out_lines.append(line)
            continue

        out_lines.append(line)

    if not styles_written:
        final_lines: list[str] = []
        inserted = False
        for line in out_lines:
            if line.strip().lower() == "[events]" and not inserted:
                final_lines.append("[V4+ Styles]\n")
                final_lines.append(_STYLE_FORMAT_HEADER + "\n")
                for name in style_names:
                    profile = _resolve_profile(name)
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
#     styled_path = await house_style.apply_house_style(sub_path, tmp_dir)
#
# Ce wrapper garde ce point d'entrée fonctionnel sans toucher aux call sites
# existants qui n'ont pas encore de sélection de style (ils utilisent alors
# le style par défaut "a"), en le faisant passer par la nouvelle logique
# apply_hardsub_style() ci-dessus.
async def apply_house_style(sub_path: str, tmp_dir: str, style_key: str = DEFAULT_STYLE_KEY) -> str:
    out_path = os.path.join(tmp_dir, "hs_house_styled.ass")
    try:
        return apply_hardsub_style(sub_path, out_path, style_key=style_key)
    except Exception:
        # Fallback silencieux comme l'ancien module : le job continue avec
        # le sous-titre original plutôt que de planter.
        return sub_path
