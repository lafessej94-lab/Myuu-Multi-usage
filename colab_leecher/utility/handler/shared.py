"""
colab_leecher/utility/handler/shared.py

Helpers utilisés par plusieurs modules de tâches ci-dessous : le rendu de
statut pour les jobs qui ont leur PROPRE message dédié (par opposition au
MSG.status_msg global géré par task_control.py), la passe de gravure
prefix/suffix lancée après les hardsub, le tail de log partagé entre
cancelTask() et SendLogs(), et les deux semaphores de concurrence hardsub
partagés entre Seedr+FreeConvert (seedr_tasks.py) et les hardsub sur lien
direct FC/CC (direct_hardsub_tasks.py).

Extrait de handler.py (devenu trop long) — voir __init__.py de ce package
pour la liste complète des réexports, qui garde tous les imports externes
existants (`from colab_leecher.utility.handler import ...`) inchangés.
"""
from __future__ import annotations

import asyncio
import logging
import os
import pathlib

from os import path as ospath

from colab_leecher.engines.freeconvert import quality_label as fc_quality_label
from colab_leecher.engines.local_video_tools import burn_text_overlay
from colab_leecher.services.job_views import fc_job_status_view
from colab_leecher.utility.helper import fileType, render_task_status
from colab_leecher.utility.variables import BOT, Paths

log = logging.getLogger(__name__)

# ── Concurrence hardsub — partagée entre Seedr+FreeConvert (seedr_tasks.py)
# et les hardsub sur lien direct FC/CC (direct_hardsub_tasks.py). Traitement
# côté serveurs FreeConvert/CloudConvert (pas sur Colab), donc parallélisable.
FC_HARDSUB_CONCURRENCY = 5
_fc_hardsub_semaphore = asyncio.Semaphore(FC_HARDSUB_CONCURRENCY)

CC_HARDSUB_CONCURRENCY = 5
_cc_hardsub_semaphore = asyncio.Semaphore(CC_HARDSUB_CONCURRENCY)


async def _finish_status(status_msg, text: str, reply_markup=None) -> None:
    """Affiche le texte FINAL d'un job (succès/échec/annulation) et arrête
    proprement le diaporama associé, s'il y en a un.

    À utiliser à la place d'un simple `await status_msg.edit_text(...)`
    partout où un handler termine sa tâche : un edit_text() seul ne coupe
    jamais la boucle _loop() de StatusSlideshow, qui continuerait alors à
    changer l'image toutes les 5s indéfiniment, même après que le job soit
    terminé/annulé/en échec. `status_msg` peut être un StatusSlideshow OU un
    Message Pyrogram classique (pas de diaporama) : dans ce 2e cas .stop()
    est simplement absent et on ignore, edit_text() se comporte normalement.
    """
    if hasattr(status_msg, "stop"):
        try:
            await status_msg.stop()
        except Exception:
            pass
    try:
        await status_msg.edit_text(text, reply_markup=reply_markup)
    except Exception:
        pass


def _tail_log(lines: int = 80) -> str:
    try:
        if not ospath.exists(Paths.LOG_PATH):
            return ""
        with open(Paths.LOG_PATH, "r", encoding="utf-8", errors="replace") as fh:
            chunk = fh.readlines()[-lines:]
        return "".join(chunk).strip()
    except Exception:
        return ""


async def _fc_job_status(
    status_msg, kind: str, stage: str, pct: float, detail: str,
    filename: str = "", job_id: str = "",
) -> None:
    """Thin wrapper: texte/clavier viennent de services.job_views.fc_job_status_view
    (pure, pas d'I/O) ; seul le edit_text() + try/except vivent ici.

    `job_id`, si fourni, ajoute le bouton ❌ Cancel branché sur
    ActiveJobs.cancel(job_id) — pour les jobs lancés via asyncio.create_task
    (FC hardsub direct-link, FFmpeg local burn/mux) qui ne passent pas par
    BOT.TASK/cancelTask() du pipeline leech classique."""
    text, kb = fc_job_status_view(
        kind, stage, pct, detail, filename, job_id,
        quality_label=fc_quality_label(BOT.Options.cc_quality_profile),
        render_task_status=render_task_status,
    )
    try:
        await status_msg.edit_text(text, disable_web_page_preview=True, reply_markup=kb)
    except Exception:
        pass


async def _burn_prefix_suffix_in_dir(job_dir: str, status_msg, label: str) -> None:
    prefix = (BOT.Setting.prefix or "").strip()
    suffix = (BOT.Setting.suffix or "").strip()
    if not prefix and not suffix:
        return

    video_files = [
        f for f in pathlib.Path(job_dir).glob("**/*")
        if f.is_file() and fileType(str(f)) == "video"
    ]
    for i, vf in enumerate(video_files, start=1):
        try:
            if status_msg is not None:
                await _fc_job_status(
                    status_msg, label, "Overlay", 90.0 + (i / max(1, len(video_files))) * 5.0,
                    f"Gravure prefix/suffix {i}/{len(video_files)}",
                )
            else:
                log.info("Burning prefix/suffix into %s (%d/%d)", vf.name, i, len(video_files))
            tmp_out = str(vf) + ".burned.mp4"
            await burn_text_overlay(str(vf), tmp_out, prefix=prefix, suffix=suffix)
            os.remove(str(vf))
            os.rename(tmp_out, str(vf))
        except Exception as exc:
            log.warning("Prefix/suffix burn-in failed for %s: %s", vf, exc)
