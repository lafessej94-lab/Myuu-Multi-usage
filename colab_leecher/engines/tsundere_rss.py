"""
colab_leecher/engines/tsundere_rss.py

Moteur pur (aucun Pyrogram, aucune notion de bot) pour le flux RSS
tsundere.to : parsing du feed, extraction/priorisation des sources
(Transfer.it > Mega > URL vidéo directe > 1fichier, NekoBT/Nyaa exclus),
téléchargement (yt-dlp / Transfer.it), validation et réparation ffmpeg.

L'orchestration (boucle de surveillance, upload Telegram, persistance de
l'historique) vit dans colab_leecher/tsundere_tracker.py.
"""
from __future__ import annotations

import html
import logging
import re
import subprocess
from pathlib import Path

import aiohttp
import feedparser
import yt_dlp
from transferit import Transferit

log = logging.getLogger(__name__)

RSS_URL = (
    "https://tsundere.to/api/v1/feed.xml"
    "?codec=H.264"
    "&quality=720p"
    "&quality=480p"
    "&language=FRENCH"
    "&language=SUBFRENCH"
    "&language=MULTI"
    "&provider=transfer.it"
)

CHECK_INTERVAL = 30

MAX_FILE_SIZE = 1_950 * 1024 * 1024

_HARDSUB_RE = re.compile(r"\bhardsub\b", re.IGNORECASE)


# ============================================================
# UTILITAIRES TEXTE
# ============================================================

def clean_html(text: str) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_urls(text: str) -> list[str]:
    if not text:
        return []
    text = html.unescape(text)
    urls = re.findall(r'https?://[^\s<>"\']+', text)
    result = []
    for url in urls:
        url = url.rstrip(".,);]}>")
        if url not in result:
            result.append(url)
    return result


def is_video_url(url: str) -> bool:
    if not url:
        return False
    lower = url.lower()
    video_extensions = (
        ".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".ts", ".m3u8", ".mpd",
    )
    if lower.split("?")[0].endswith(video_extensions):
        return True
    video_keywords = (
        ".m3u8?", "/video/", "/stream/", "/download/", "master.m3u8", "playlist.m3u8",
    )
    return any(x in lower for x in video_keywords)


def is_hardsub(title: str) -> bool:
    return bool(_HARDSUB_RE.search(str(title or "")))


# ============================================================
# EXTRACTION DE LA VIDÉO
# ============================================================

def extract_video_url(entry) -> str | None:
    candidates: list[str] = []

    def add_url(value):
        if not value:
            return
        value = html.unescape(str(value)).strip()
        if not value.startswith(("http://", "https://")):
            return
        if value not in candidates:
            candidates.append(value)

    for enclosure in getattr(entry, "enclosures", []) or []:
        add_url(enclosure.get("href") or enclosure.get("url"))

    for media in getattr(entry, "media_content", []) or []:
        add_url(media.get("url") or media.get("href"))

    add_url(getattr(entry, "link", None))
    add_url(getattr(entry, "guid", None))

    texts = []
    for attr in ("description", "summary"):
        value = getattr(entry, attr, None)
        if value:
            texts.append(str(value))
    for content in getattr(entry, "content", []) or []:
        value = content.get("value")
        if value:
            texts.append(str(value))
    for text in texts:
        for url in extract_urls(text):
            add_url(url)

    if not candidates:
        return None

    valid_candidates = []
    for url in candidates:
        u = url.lower()
        if "nekobt.to" in u:
            log.info("🚫 NekoBT ignoré : %s", url)
            continue
        if "nyaa.si" in u:
            log.info("🚫 Nyaa ignoré : %s", url)
            continue
        valid_candidates.append(url)

    if not valid_candidates:
        log.error("❌ Aucune source exploitable après exclusion de NekoBT/Nyaa")
        return None

    for url in valid_candidates:
        if "transfer.it/t/" in url.lower():
            log.info("🎯 Transfer.it prioritaire : %s", url)
            return url

    for url in valid_candidates:
        u = url.lower()
        if "mega.nz/" in u or "mega.co.nz/" in u:
            log.info("🎯 Mega sélectionné : %s", url)
            return url

    for url in valid_candidates:
        if is_video_url(url):
            log.info("🎯 URL vidéo sélectionnée : %s", url)
            return url

    for url in valid_candidates:
        if "1fichier.com" in url.lower():
            log.info("🎯 1fichier sélectionné : %s", url)
            return url

    log.info("🎯 Première URL exploitable sélectionnée : %s", valid_candidates[0])
    return valid_candidates[0]


# ============================================================
# RSS
# ============================================================

async def fetch_feed():
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 14) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0 Mobile Safari/537.36"
        ),
        "Accept": "application/rss+xml,application/xml,text/xml,*/*",
    }
    timeout = aiohttp.ClientTimeout(total=60)

    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        async with session.get(RSS_URL, allow_redirects=True) as response:
            response.raise_for_status()
            data = await response.read()

    feed = feedparser.parse(data)
    if feed.bozo:
        log.warning("⚠️ RSS parsé avec avertissement : %s", feed.bozo_exception)
    return feed


def get_title(entry) -> str:
    title = entry.get("title", "").strip()
    if title:
        return clean_html(title)
    return "Anime sans titre"


def episode_key(title: str) -> str:
    """Normalise un titre pour grouper les différents encodages/qualités
    d'un même épisode ensemble (évite les doublons). Clé brute, pas
    destinée à l'affichage — voir anime_name() pour ça."""
    value = str(title or "").lower()
    value = re.sub(r"\bhardsub\b", "", value)
    value = re.sub(r"\b(720p|1080p|480p|2160p|4k)\b", "", value)
    value = re.sub(r"\b(cr|web-dl|webrip|bluray|bdrip)\b", "", value)
    value = re.sub(r"\baac\d?(?:\.\d+)?\b", "", value)
    value = re.sub(r"\bx264\b", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


# Marqueurs d'épisode à retirer pour ne garder que le nom de l'anime :
# "Ep1080", "E1080", "Episode 1080", "S01E23", "- 12", etc.
_EPISODE_MARKERS_RE = re.compile(
    r"""
    \bs\d{1,2}e\d{1,4}\b       |   # S01E23
    \bepisode\s*\d{1,4}\b     |   # Episode 1080 / episode1080
    \bep\.?\s*\d{1,4}\b       |   # Ep1080 / Ep. 1080 / ep 12
    \be\d{1,4}\b              |   # E1080
    -\s*\d{1,4}\b                 # "- 12" en fin de titre (style Nyaa)
    """,
    re.IGNORECASE | re.VERBOSE,
)


def anime_name(title: str) -> str:
    """
    Réduit un titre de release à un nom d'anime "propre", pour l'affichage
    dans la liste de sélection manuelle (/online_tsundere) : retire le tag
    HARDSUB, la qualité, le codec/audio (comme episode_key), PUIS le
    marqueur d'épisode lui-même, et remet une casse "Title Case" lisible.

    Ex : "One Piece Ep1080 VOSTFR 1080p CR WEB-DL AAC2.0 H.264-Myuus-Raws"
      -> "One Piece"
    """
    value = episode_key(title)
    value = _EPISODE_MARKERS_RE.sub("", value)
    value = re.sub(r"[-_]{2,}.*$", "", value)  # coupe un tag de groupe résiduel
    value = re.sub(r"\s+", " ", value).strip(" -_")
    return value.title() if value else "Anime inconnu"


def list_available_animes(entries) -> list[str]:
    """À partir des entrées brutes du flux RSS, renvoie la liste
    dédupliquée des noms d'animes HARDSUB actuellement disponibles, triée
    alphabétiquement. Utilisée par /online_tsundere."""
    seen: set[str] = set()
    names: list[str] = []
    for entry in entries:
        title = get_title(entry)
        if not is_hardsub(title):
            continue
        name = anime_name(title)
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return sorted(names, key=str.lower)


def source_priority(url: str) -> int:
    url = str(url or "").lower()
    if "transfer.it/t/" in url:
        return 1
    if "mega.nz/" in url or "mega.co.nz/" in url:
        return 2
    if "1fichier.com" in url:
        return 9
    if "nyaa.si/" in url:
        return 10
    if "nekobt.to/" in url:
        return 11
    return 5


# ============================================================
# TÉLÉCHARGEMENT
# ============================================================

def download_video(url: str, dest_dir: Path) -> Path:
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    url = str(url).strip()
    log.info("🚀 URL reçue : %s", url)

    if "transfer.it/t/" in url.lower():
        log.info("🔗 Transfer.it détecté")
        tx = Transferit()

        log.info("📡 Lecture du transfert Transfer.it...")
        info = tx.info(url)
        if not info:
            raise RuntimeError("❌ Aucun fichier dans le transfert Transfer.it")
        log.info("📦 %d élément(s) trouvé(s)", len(info))

        tx.download(url, str(dest_dir))

        extensions = {".mp4", ".mkv", ".avi", ".webm", ".mov", ".m4v", ".ts"}
        candidates = [x for x in dest_dir.iterdir() if x.is_file() and x.suffix.lower() in extensions]
        if not candidates:
            candidates = [x for x in dest_dir.iterdir() if x.is_file() and x.stat().st_size > 100 * 1024]
        if not candidates:
            raise FileNotFoundError("❌ Transfer.it : aucun vrai fichier vidéo trouvé")

        file_path = max(candidates, key=lambda x: x.stat().st_mtime)
        log.info("✅ Transfer.it terminé : %s", file_path)
        return file_path

    if "1fichier.com" in url.lower():
        log.info("🔗 1fichier détecté")
        raise RuntimeError("❌ 1fichier détecté : téléchargement direct non disponible.")

    log.info("🚀 Téléchargement yt-dlp : %s", url)

    ydl_opts = {
        "outtmpl": str(dest_dir / "%(title).150s.%(ext)s"),
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "retries": 10,
        "fragment_retries": 10,
        "socket_timeout": 60,
        "quiet": False,
        "no_warnings": False,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = Path(ydl.prepare_filename(info))
        if filename.exists():
            return filename
        mp4 = filename.with_suffix(".mp4")
        if mp4.exists():
            return mp4

    raise FileNotFoundError("❌ Fichier téléchargé introuvable.")


# ============================================================
# TAILLE / VALIDATION / RÉPARATION
# ============================================================

def check_file_size(file_path) -> bool:
    try:
        size = Path(file_path).stat().st_size
        if size <= 0:
            log.error("❌ Fichier vide.")
            return False
        if size > MAX_FILE_SIZE:
            log.error(
                "❌ Fichier trop volumineux : %.2f Mo > %.2f Mo",
                size / (1024 * 1024), MAX_FILE_SIZE / (1024 * 1024),
            )
            return False
        log.info("📦 Taille du fichier : %.2f Mo", size / (1024 * 1024))
        return True
    except Exception:
        log.exception("❌ Impossible de vérifier la taille du fichier")
        return False


def validate_video(file_path) -> bool:
    file_path = Path(file_path)
    if not file_path.exists():
        return False
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(file_path),
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60,
        )
        if result.returncode != 0:
            log.error("❌ FFprobe : fichier vidéo invalide : %s", file_path.name)
            if result.stderr:
                log.error("%s", result.stderr.strip()[-500:])
            return False
        duration = result.stdout.strip()
        if not duration:
            log.error("❌ FFprobe : durée introuvable : %s", file_path.name)
            return False
        log.info("✅ Vidéo valide : %.2f secondes", float(duration))
        return True
    except FileNotFoundError:
        log.warning("⚠️ ffprobe introuvable, validation ignorée.")
        return True
    except Exception:
        log.exception("❌ Erreur pendant ffprobe")
        return False


def repair_mp4(file_path):
    file_path = Path(file_path)
    if file_path.suffix.lower() != ".mp4":
        return None

    repaired = file_path.with_name(file_path.stem + "_repaired.mp4")
    log.warning("🛠️ Tentative de réparation MP4 : %s", file_path.name)

    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(file_path),
                "-map", "0:v:0?", "-map", "0:a?",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart",
                str(repaired),
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=7200,
        )
        if result.returncode != 0:
            log.error("❌ Réparation MP4 impossible.")
            if result.stderr:
                log.error("%s", result.stderr.strip()[-1000:])
            if repaired.exists():
                repaired.unlink()
            return None

        if not repaired.exists():
            return None
        if not validate_video(repaired):
            repaired.unlink(missing_ok=True)
            return None

        log.info("✅ MP4 réparé : %s", repaired)
        return repaired
    except Exception:
        log.exception("❌ Erreur pendant la réparation MP4")
        if repaired.exists():
            repaired.unlink()
        return None


def prepare_file(file_path):
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"Fichier introuvable : {file_path}")

    if validate_video(file_path):
        return file_path

    if file_path.suffix.lower() == ".mp4":
        repaired = repair_mp4(file_path)
        if repaired:
            return repaired

    raise RuntimeError(f"❌ Fichier vidéo invalide : {file_path.name}")
