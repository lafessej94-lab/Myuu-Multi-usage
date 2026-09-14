"""
colab_leecher/services/link_flow.py

link_sessions is the shared state populated by handle_url() (still in
__main__.py, since it's a message handler on any recognized link/magnet)
and read by every hardsub flow that needs "which link was THIS button
attached to" (services/hardsub_flow.py) as well as by the lk|sec|/lk|cancel
section-menu navigation below.

The pure text/keyboard builders for the section menu itself stay in
colab_leecher/link_menu.py (unchanged, per the instruction to leave
video_menu.py and link_menu.py where they are) — this module only owns the
mutable session dict and the callback handlers that read/write it.
"""
from __future__ import annotations

from colab_leecher.link_menu import (
    kind_label as lk_kind_label,
    link_header as lk_header,
    main_kb as lk_main_kb,
    download_kb as lk_download_kb,
    inspect_kb as lk_inspect_kb,
    process_kb as lk_process_kb,
    cloud_kb as lk_cloud_kb,
)
from colab_leecher.services import on_exact, on_prefix
from colab_leecher.utility.variables import BOT

# message_id (du message "Choisis une section :") -> liste de sources.
# Nécessaire pour que plusieurs liens envoyés d'affilée ne se marchent pas
# dessus sur le global BOT.SOURCE — chaque bouton retrouve SON lien via le
# message auquel il est attaché. Sert aussi de "token" implicite pour la
# navigation par sections (lk|sec|...).
link_sessions: dict[int, list[str]] = {}


async def _handle_link_nav(client, cq, data):
    session_src = link_sessions.get(cq.message.id, BOT.SOURCE or [""])
    first = (session_src or [""])[0].strip()
    is_magnet = first.startswith("magnet:?xt=urn:btih:")
    is_http = first.startswith("http://") or first.startswith("https://")
    n = len([l for l in session_src if l.strip()])
    label = lk_kind_label(BOT.Mode.ytdl, is_magnet)

    if data == "lk|cancel":
        link_sessions.pop(cq.message.id, None)
        await cq.answer()
        await cq.message.delete()
        return

    section = data.split("|", 2)[2]
    await cq.answer()
    if section == "main":
        await cq.message.edit_text(lk_header(label, n), reply_markup=lk_main_kb(is_magnet))
    elif section == "download":
        await cq.message.edit_text(
            lk_header(label, n, "Download", "Choisis le format de sortie."),
            reply_markup=lk_download_kb(),
        )
    elif section == "inspect":
        await cq.message.edit_text(
            lk_header(label, n, "Inspect", "Analyse la source avant de lancer quoi que ce soit."),
            reply_markup=lk_inspect_kb(),
        )
    elif section == "process":
        await cq.message.edit_text(
            lk_header(label, n, "Process", "CloudConvert / hardsub sur ce lien."),
            reply_markup=lk_process_kb(is_http),
        )
    elif section == "cloud":
        if not is_magnet:
            await cq.answer("Cloud needs a magnet link.", show_alert=True)
            return
        await cq.message.edit_text(
            lk_header(label, n, "Cloud", "Seedr + FreeConvert / CloudConvert."),
            reply_markup=lk_cloud_kb(),
        )


on_prefix("lk|sec|")(_handle_link_nav)
on_exact("lk|cancel")(_handle_link_nav)
