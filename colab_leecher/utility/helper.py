import math
import logging
import os
import psutil
import random
import subprocess
from time import time
from datetime import datetime
from os import path as ospath
from urllib.parse import urlparse
from asyncio import get_event_loop

from PIL import Image
from pyrogram.errors import BadRequest
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto

from colab_leecher import SEEDR_PASSWORD, SEEDR_USERNAME, colab_bot
from colab_leecher.cloudconvert import cc_mode_label, quality_label, resize_label
from colab_leecher.utility.variables import BOT, MSG, BotTimes, Messages, Paths


def _pct_bar(percentage: float, length: int = 12) -> str:
    filled = int(min(percentage, 100) / 100 * length)
    return "█" * filled + "░" * (length - filled)


def _speed_emoji(speed_str: str) -> str:
    if "GiB" in speed_str or "TiB" in speed_str:
        return "🚀"
    if "MiB" in speed_str:
        try:
            value = float(speed_str.split()[0])
            if value >= 50:
                return "⚡️"
            if value >= 10:
                return "🔥"
        except Exception:
            pass
        return "🏃"
    return "🐢"


def isLink(_, __, update):
    if update.text:
        if "/content/" in str(update.text) or "/home" in str(update.text):
            return True
        if update.text.startswith("magnet:?xt=urn:btih:"):
            return True
        parsed = urlparse(update.text)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return True
    return False


def is_google_drive(link):
    return "drive.google.com" in link


def is_mega(link):
    return "mega.nz" in link


def is_terabox(link):
    return "terabox" in link or "1024tera" in link


def is_ytdl_link(link):
    return "youtube.com" in link or "youtu.be" in link


def is_telegram(link):
    return "t.me" in link


def is_torrent(link):
    return "magnet" in link or "torrent" in link


def getTime(seconds):
    seconds = int(seconds)
    d = seconds // 86400
    seconds %= 86400
    h = seconds // 3600
    seconds %= 3600
    m = seconds // 60
    seconds %= 60
    if d:
        return f"{d}d {h}h {m}m {seconds}s"
    if h:
        return f"{h}h {m}m {seconds}s"
    if m:
        return f"{m}m {seconds}s"
    return f"{seconds}s"


def sizeUnit(size):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PiB"


def fileType(file_path: str):
    ext_map = {
        ".mp4": "video",
        ".avi": "video",
        ".mkv": "video",
        ".m2ts": "video",
        ".mov": "video",
        ".ts": "video",
        ".m3u8": "video",
        ".webm": "video",
        ".mpg": "video",
        ".mpeg": "video",
        ".mpeg4": "video",
        ".vob": "video",
        ".m4v": "video",
        ".mp3": "audio",
        ".wav": "audio",
        ".flac": "audio",
        ".aac": "audio",
        ".ogg": "audio",
        ".jpg": "photo",
        ".jpeg": "photo",
        ".png": "photo",
        ".bmp": "photo",
        ".gif": "photo",
    }
    _, ext = ospath.splitext(file_path)
    return ext_map.get(ext.lower(), "document")


def shortFileName(path):
    if ospath.isfile(path):
        dname, fname = ospath.split(path)
        if len(fname) > 60:
            base, ext = ospath.splitext(fname)
            fname = base[: 60 - len(ext)] + ext
            path = ospath.join(dname, fname)
    elif ospath.isdir(path):
        dname, dirname = ospath.split(path)
        if len(dirname) > 60:
            path = ospath.join(dname, dirname[:60])
    elif len(path) > 60:
        path = path[:60]
    return path


def getSize(path):
    if ospath.isfile(path):
        return ospath.getsize(path)
    total = 0
    for dpath, _, fnames in os.walk(path):
        for fname in fnames:
            total += ospath.getsize(ospath.join(dpath, fname))
    return total


def videoExtFix(file_path: str):
    if file_path.endswith(".mp4") or file_path.endswith(".mkv"):
        return file_path
    new_path = file_path + ".mp4"
    os.rename(file_path, new_path)
    return new_path


def _probe_duration(file_path: str) -> float:
    """Récupère la durée de la vidéo via ffprobe (rapide, pas de décodage complet)."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                file_path,
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30,
        )
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def _extract_frame_ffmpeg(file_path: str, output_path: str, timestamp: float) -> bool:
    """Extrait une frame réduite (200px max) via ffmpeg, avec une bonne qualité JPEG.
    On réduit la résolution plutôt que d'écraser la qualité, pour garder une
    thumb nette tout en restant légère en poids."""
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(int(timestamp)),
        "-i", file_path,
        "-vframes", "1",
        "-vf", "scale=200:200:force_original_aspect_ratio=decrease",
        "-q:v", "4",
        output_path,
    ]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        return result.returncode == 0 and ospath.exists(output_path)
    except Exception as exc:
        logging.warning(f"ffmpeg thumb extraction failed: {exc}")
        return False


def thumbMaintainer(file_path):
    """
    Génère/retourne la thumbnail à utiliser pour l'upload.
    Ordre de priorité: thumbnail custom utilisateur > thumbnail ytdl > extraction ffmpeg (320px, norme Telegram).
    La frame est prise à un instant ALÉATOIRE de la vidéo (entre 10% et 90%
    de la durée, pour éviter les génériques/écrans noirs en tout début/fin)
    plutôt que toujours au milieu exact — chaque upload a donc une thumbnail
    différente même pour un même fichier.
    """
    if ospath.exists(Paths.VIDEO_FRAME):
        os.remove(Paths.VIDEO_FRAME)

    if ospath.exists(Paths.THMB_PATH):
        return Paths.THMB_PATH, _probe_duration(file_path)

    fname, _ = ospath.splitext(ospath.basename(file_path))
    ytdl_thumb = f"{Paths.WORK_PATH}/ytdl_thumbnails/{fname}.webp"
    duration = _probe_duration(file_path)

    if ospath.exists(ytdl_thumb):
        return convertIMG(ytdl_thumb), duration

    timestamp = random.uniform(duration * 0.1, duration * 0.9) if duration > 1 else 1
    if _extract_frame_ffmpeg(file_path, Paths.VIDEO_FRAME, timestamp):
        return Paths.VIDEO_FRAME, duration

    logging.warning("Thumb error: ffmpeg extraction failed, falling back")
    fallback = Paths.THMB_PATH if ospath.exists(Paths.THMB_PATH) else Paths.HERO_IMAGE
    return fallback, duration


async def setThumbnail(message):
    try:
        if ospath.exists(Paths.THMB_PATH):
            os.remove(Paths.THMB_PATH)
        loop = get_event_loop()
        await loop.create_task(message.download(file_name=Paths.THMB_PATH))
        BOT.Setting.thumbnail = True
        if BOT.State.task_going and MSG.status_msg:
            await MSG.status_msg.edit_media(
                InputMediaPhoto(Paths.THMB_PATH),
                reply_markup=keyboard(),
            )
        return True
    except Exception as exc:
        BOT.Setting.thumbnail = False
        logging.warning(f"Thumbnail error: {exc}")
        return False


def isYtdlComplete():
    for _, _, filenames in os.walk(Paths.down_path):
        for fname in filenames:
            _, ext = ospath.splitext(fname)
            if ext in [".part", ".ytdl"]:
                return False
    return True


def convertIMG(image_path):
    img = Image.open(image_path)
    if img.mode != "RGB":
        img = img.convert("RGB")
    out = ospath.splitext(image_path)[0] + ".jpg"
    img.save(out, "JPEG")
    os.remove(image_path)
    return out


def applyCustomName():
    if len(BOT.Options.custom_name) != 0 and BOT.Mode.type not in ["zip", "undzip"]:
        for file_ in os.listdir(Paths.down_path):
            os.rename(
                ospath.join(Paths.down_path, file_),
                ospath.join(Paths.down_path, BOT.Options.custom_name),
            )


def speedETA(start, done, total):
    percentage = min((done / total) * 100, 100) if total else 0
    elapsed = (datetime.now() - start).seconds
    if done > 0 and elapsed:
        raw_speed = done / elapsed
        speed = f"{sizeUnit(raw_speed)}/s"
        eta = (total - done) / raw_speed
    else:
        speed, eta = "N/A", 0
    return speed, eta, percentage


def isTimeOver():
    passed = time() - BotTimes.current_time >= 3
    if passed:
        BotTimes.current_time = time()
    return passed


async def message_deleter(m1, m2):
    for message in (m1, m2):
        try:
            await message.delete()
        except Exception as exc:
            logging.debug(f"Delete failed: {exc}")


def multipartArchive(path: str, type: str, remove: bool):
    dirname, filename = ospath.split(path)
    name, _ = ospath.splitext(filename)
    count, size, real_name = 1, 0, name
    if type == "rar":
        name_, _ = ospath.splitext(name)
        real_name = name_
        next_name = name_ + ".part" + str(count) + ".rar"
        next_path = ospath.join(dirname, next_name)
        while ospath.exists(next_path):
            if remove:
                os.remove(next_path)
            size += getSize(next_path)
            count += 1
            next_name = name_ + ".part" + str(count) + ".rar"
            next_path = ospath.join(dirname, next_name)
    elif type == "7z":
        next_name = name + "." + str(count).zfill(3)
        next_path = ospath.join(dirname, next_name)
        while ospath.exists(next_path):
            if remove:
                os.remove(next_path)
            size += getSize(next_path)
            count += 1
            next_name = name + "." + str(count).zfill(3)
            next_path = ospath.join(dirname, next_name)
    elif type == "zip":
        next_name = name + ".zip"
        next_path = ospath.join(dirname, next_name)
        if ospath.exists(next_path):
            if remove:
                os.remove(next_path)
            size += getSize(next_path)
        next_name = name + ".z" + str(count).zfill(2)
        next_path = ospath.join(dirname, next_name)
        while ospath.exists(next_path):
            if remove:
                os.remove(next_path)
            size += getSize(next_path)
            count += 1
            next_name = name + ".z" + str(count).zfill(2)
            next_path = ospath.join(dirname, next_name)
        if real_name.endswith(".zip"):
            real_name, _ = ospath.splitext(real_name)
    return real_name, size


def sysINFO():
    ram = psutil.Process(os.getpid()).memory_info().rss
    disk = psutil.disk_usage("/")
    cpu = psutil.cpu_percent()
    return (
        "\n<code>———————————————</code>\n"
        f"🖥 CPU {cpu:.0f}%   💾 RAM {sizeUnit(ram)}   💿 {sizeUnit(disk.free)} libres"
        f"{Messages.caution_msg}"
    )


async def status_bar(down_msg, speed, percentage, eta, done, left, engine, status_msg=None):
    """
    status_msg optionnel : si fourni, édite CE message précis plutôt que le
    MSG.status_msg global — nécessaire pour que les jobs FreeConvert
    concurrents affichent chacun leur propre progression sans se marcher
    dessus. Si absent, comportement inchangé (pipeline normal single-task).
    """
    target_msg = status_msg or MSG.status_msg
    pct_f = max(0.0, min(float(percentage), 100.0))
    bar = _pct_bar(pct_f, 16)
    elapsed = getTime((datetime.now() - BotTimes.start_time).seconds)

    text = (
        f"\n<code>[{bar}]</code>\n"
        f"<code>{done} / {left}</code>   <b>{pct_f:.0f}%</b>\n\n"
        f"⚡️ <code>{speed}</code>   ⏳ <code>{eta}</code>\n"
        f"🕐 <code>{elapsed}</code>   ⚙️ <code>{engine}</code>\n"
    )
    try:
        if isTimeOver():
            await target_msg.edit_text(
                text=Messages.task_msg + down_msg + text + sysINFO(),
                disable_web_page_preview=True,
                reply_markup=keyboard(),
            )
    except BadRequest as exc:
        logging.debug(f"Status not modified: {exc}")
    except Exception as exc:
        logging.warning(f"Status bar error: {exc}")


async def send_settings(client, message, msg_id, command: bool, readonly: bool = False):
    up_mode = "document" if BOT.Options.stream_upload else "media"
    up_toggle = "📄 -> Media" if not BOT.Options.stream_upload else "🎞 -> Document"
    pr = "—" if BOT.Setting.prefix == "" else BOT.Setting.prefix
    su = "—" if BOT.Setting.suffix == "" else BOT.Setting.suffix
    thmb = "✅ Définie" if BOT.Setting.thumbnail else "❌ Non définie"
    cc_ready = f"✅ {len(BOT.Options.cc_api_keys)} clé(s)" if BOT.Options.cc_api_keys else "❌ Aucune"
    fc_ready = f"✅ {len(BOT.Options.fc_api_keys)} clé(s)" if BOT.Options.fc_api_keys else "❌ Aucune"
    seedr_ready = "✅ Prêt" if str(SEEDR_USERNAME or "").strip() and str(SEEDR_PASSWORD or "").strip() else "❌ Manquant"
    n_dumps = len(BOT.Options.dump_ids)
    dump_status = f"✅ {n_dumps} configuré(s)" if n_dumps else "❌ Aucun"
    autofwd_toggle = "📨 Transfert : ON" if BOT.Options.auto_forward else "📨 Transfert : OFF"

    if readonly:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Fermer", callback_data="close")]])
    else:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(up_toggle, callback_data=up_mode),
             InlineKeyboardButton("🎥 Vidéo", callback_data="video")],
            [InlineKeyboardButton("☁️ CloudConvert", callback_data="cc"),
             InlineKeyboardButton("🖼 Miniature", callback_data="thumb")],
            [InlineKeyboardButton(autofwd_toggle, callback_data="autofwd"),
             InlineKeyboardButton("📦 Dumps", callback_data="dumps")],
            [InlineKeyboardButton("🔑 Clés API", callback_data="apikeys")],
            [InlineKeyboardButton("✏️ Légende", callback_data="caption")],
            [InlineKeyboardButton("⬅️ Préfixe", callback_data="set-prefix"),
             InlineKeyboardButton("Suffixe ➡️", callback_data="set-suffix")],
            [InlineKeyboardButton("✖️ Fermer", callback_data="close")],
        ])

    text = (
        "⚙️ <b>Réglages du bot</b>"
        + (" <i>(lecture seule)</i>" if readonly else "")
        + "\n\n"
        "<b>🎬 Vidéo</b>\n"
        f"Conversion       <code>{BOT.Setting.convert_video}</code>\n"
        f"Découpage        <code>{BOT.Setting.split_video}</code>\n\n"
        "<b>☁️ Conversion</b>\n"
        f"Mode · Preset    <code>{cc_mode_label(BOT.Options.cc_engine_mode)} · {quality_label(BOT.Options.cc_quality_profile)}</code>\n"
        f"Resize · Cible   <code>{resize_label(BOT.Options.cc_resize)} · {BOT.Setting.cc_target_size}</code>\n"
        f"CloudConvert     {cc_ready}\n"
        f"FreeConvert      {fc_ready}\n\n"
        "<b>🔌 Intégrations</b>\n"
        f"Seedr            {seedr_ready}\n"
        f"Transfert auto   <code>{BOT.Setting.auto_forward}</code>\n"
        f"Canaux dump      {dump_status}\n\n"
        "<b>📤 Upload</b>\n"
        f"Légende          <code>{BOT.Setting.caption}</code>\n"
        f"Préfixe · Suffixe <code>{pr} · {su}</code>\n"
        f"Miniature        {thmb}"
    )
    try:
        if command:
            await message.reply_text(text=text, reply_markup=kb)
        else:
            await colab_bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=msg_id,
                text=text,
                reply_markup=kb,
            )
    except BadRequest as exc:
        logging.debug(f"Settings not modified: {exc}")
    except Exception as exc:
        logging.warning(f"Settings error: {exc}")


def keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel Task", callback_data="cancel"),
    ]])
