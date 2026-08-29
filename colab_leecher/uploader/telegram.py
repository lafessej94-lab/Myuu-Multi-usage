import logging
from PIL import Image
from asyncio import sleep
from os import path as ospath
from datetime import datetime
from pyrogram.errors import FloodWait
from colab_leecher import colab_bot, OWNER
from colab_leecher.utility.variables import BOT, Transfer, BotTimes, Messages, MSG, Paths
from colab_leecher.utility.helper import (
    sizeUnit, fileType, getTime, status_bar, thumbMaintainer, videoExtFix,
)


async def progress_bar(current, total, status_msg=None):
    upload_speed = 4 * 1024 * 1024
    elapsed = (datetime.now() - BotTimes.task_start).seconds
    if current > 0 and elapsed > 0:
        upload_speed = current / elapsed
    eta        = (Transfer.total_down_size - current - sum(Transfer.up_bytes)) / max(upload_speed, 1)
    percentage = (current + sum(Transfer.up_bytes)) / max(Transfer.total_down_size, 1) * 100
    await status_bar(
        down_msg=Messages.status_head,
        speed=f"{sizeUnit(upload_speed)}/s",
        percentage=percentage,
        eta=getTime(eta),
        done=sizeUnit(current + sum(Transfer.up_bytes)),
        left=sizeUnit(Transfer.total_down_size),
        engine="Pyrofork 💥",
        status_msg=status_msg,
    )


async def upload_file(file_path, real_name, is_last: bool = False, status_msg=None):
    """
    Upload one file directly to the owner's private chat.

    is_last     — when True the caption shows ✅ Done and the
                  progress status message is deleted afterwards.
    status_msg  — message Telegram à éditer pour la progression. Si None,
                  utilise le MSG.status_msg global (comportement historique,
                  pipeline leech normal). Les jobs FreeConvert concurrents
                  passent leur propre message dédié pour ne pas se marcher
                  dessus.
    """
    global Transfer, MSG
    BotTimes.task_start = datetime.now()
    target_msg = status_msg or MSG.status_msg

    # Caption : nom propre, identique pour tous les fichiers (plus de "✅ Done ·" sur le dernier)
    name_part = f"{BOT.Setting.prefix} {real_name} {BOT.Setting.suffix}".strip()
    caption = f"<{BOT.Options.caption}>{name_part}</{BOT.Options.caption}>"

    type_  = fileType(file_path)
    f_type = type_ if BOT.Options.stream_upload else "document"
    # Priorité au chat du message de statut DE CE JOB (déjà correctement
    # scopé à sa création, robuste même si BOT.TargetChat a été réécrit
    # entre-temps par un autre job lancé en parallèle) ; sinon repli sur
    # BOT.TargetChat (pipeline leech classique, un seul job à la fois).
    target_chat = target_msg.chat.id if target_msg else BOT.TargetChat

    async def _progress(current, total):
        await progress_bar(current, total, target_msg)

    try:
        if f_type == "video":
            if not BOT.Options.stream_upload:
                file_path = videoExtFix(file_path)
            thmb_path, seconds = thumbMaintainer(file_path)
            with Image.open(thmb_path) as img:
                width, height = img.size
            sent = await colab_bot.send_video(
                chat_id=target_chat,
                video=file_path,
                supports_streaming=True,
                width=width, height=height,
                caption=caption,
                thumb=thmb_path,   # petite vignette liste de chat (Telegram plafonne à 320px, hors de notre contrôle)
                cover=thmb_path,   # preview grand format affichée à l'ouverture — pas de limite de résolution
                duration=int(seconds),
                progress=_progress,
            )

        elif f_type == "audio":
            thmb_path = Paths.THMB_PATH if ospath.exists(Paths.THMB_PATH) else None
            sent = await colab_bot.send_audio(
                chat_id=target_chat,
                audio=file_path,
                caption=caption,
                thumb=thmb_path,
                progress=_progress,
            )

        elif f_type == "photo":
            sent = await colab_bot.send_photo(
                chat_id=target_chat,
                photo=file_path,
                caption=caption,
                progress=_progress,
            )

        else:  # document
            if ospath.exists(Paths.THMB_PATH):
                thmb_path = Paths.THMB_PATH
            elif type_ == "video":
                thmb_path, _ = thumbMaintainer(file_path)
            else:
                thmb_path = None
            sent = await colab_bot.send_document(
                chat_id=target_chat,
                document=file_path,
                caption=caption,
                thumb=thmb_path,
                progress=_progress,
            )

        MSG.sent_msg = sent
        Transfer.sent_file.append(sent)
        Transfer.sent_file_names.append(real_name)
        await maybe_autoforward(sent)

        # Delete the progress status message once the last file lands
        if is_last:
            try:
                await target_msg.delete()
            except Exception:
                pass

    except FloodWait as e:
        logging.warning(f"FloodWait {e.value}s")
        await sleep(e.value)
        await upload_file(file_path, real_name, is_last, status_msg=status_msg)

    except Exception as e:
        logging.exception(f"Upload error: {e}")
        raise RuntimeError(f"Telegram upload failed for {real_name}: {e}") from e


async def maybe_autoforward(message) -> None:
    if not BOT.Options.auto_forward or not BOT.Options.dump_ids:
        return
    for dump_target in list(BOT.Options.dump_ids):
        await _forward_to(message, dump_target)


async def _forward_to(message, dump_target, retries: int = 0) -> None:
    try:
        await colab_bot.copy_message(
            chat_id=dump_target,
            from_chat_id=BOT.TargetChat,
            message_id=message.id,
        )
    except FloodWait as e:
        if retries >= 3:
            logging.warning(f"Autoforward to {dump_target} gave up after {retries} FloodWaits")
            return
        logging.warning(f"Autoforward FloodWait {e.value}s (target {dump_target})")
        await sleep(e.value)
        await _forward_to(message, dump_target, retries + 1)
    except Exception as exc:
        logging.warning(f"Autoforward to {dump_target} skipped: {exc}")
