"""
anime_search.py — Commandes /Naut_Anime et /Mal_Anime pour myuu
Gère : la recherche, le menu de sélection inline, et l'envoi de la fiche
formatée (photo + caption) à partir des moteurs nautilijan.py et
my_anime_liste.py.

Indépendant du pipeline hardsub — nouvelle feature du repo myuu.
"""

from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

import nautilijan
import my_anime_liste

# Cache en mémoire : {cache_key: [{"id": ..., "title": ..., "thumb": ...}, ...]}
# cache_key = str(message_id) de la commande d'origine, pour garder des
# callback_data courts (limite Telegram : 64 octets).
_SEARCH_CACHE: dict[str, list[dict]] = {}


def _format_caption(data: dict, source_label: str) -> str:
    """Construit la caption façon 'Visuel Animes' à partir du dict normalisé
    renvoyé par nautilijan.py ou my_anime_liste.py."""
    genres = ", ".join(data["genres"]) if data["genres"] else "?"
    themes = ", ".join(data["themes"]) if data["themes"] else "?"

    lines = [
        f"**{data['title_original']}**",
        "",
        f"Titre original : {data['title_original']}",
        f"Série · {data['type_origin']} · {data['episodes']}",
        f"Diffusion : du {data['aired_from']} au {data['aired_to']} ({data['status']})"
        + (f" · Saison : {data['season']}" if data.get("season") not in (None, "?") else ""),
        f"Genre : {genres}",
        f"Thèmes : {themes}",
        f"Studio d'animation : {data['studio']}",
    ]

    if data.get("official_site"):
        lines.append(f"Site web officiel : {data['official_site']}")

    lines.append(f"Groupe : {data['producers']}")
    lines.append("")
    lines.append("Synopsis")
    lines.append(data["synopsis"])
    lines.append("")
    lines.append(f"Source : {source_label} — {data['source_url']}")

    return "\n".join(lines)


async def _send_menu(message: Message, results: list[dict], prefix: str, query: str):
    """Envoie le menu inline de sélection à partir d'une liste de résultats
    déjà normalisés en {'id': ..., 'title': ..., 'thumb': ...}."""
    if not results:
        await message.reply_text(f"❌ Aucun résultat trouvé pour **{query}**.")
        return

    cache_key = str(message.id)
    _SEARCH_CACHE[cache_key] = results

    buttons = [
        [InlineKeyboardButton(r["title"], callback_data=f"{prefix}:{cache_key}:{i}")]
        for i, r in enumerate(results)
    ]

    await message.reply_text(
        f"Résultats pour **{query}** :",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@Client.on_message(filters.command("Naut_Anime"))
async def naut_anime_command(client: Client, message: Message):
    if len(message.command) < 2:
        await message.reply_text("Utilisation : `/Naut_Anime <nom de l'anime>`")
        return

    query = " ".join(message.command[1:])
    status_msg = await message.reply_text("🔎 Recherche sur Nautiljon...")

    raw_results = await nautilijan.search_anime(query)
    # Normalise vers {"id": url, "title": ..., "thumb": ...}
    results = [{"id": r["url"], "title": r["title"], "thumb": r["thumb"]} for r in raw_results]

    await status_msg.delete()
    await _send_menu(message, results, "naut_sel", query)


@Client.on_message(filters.command("Mal_Anime"))
async def mal_anime_command(client: Client, message: Message):
    if len(message.command) < 2:
        await message.reply_text("Utilisation : `/Mal_Anime <nom de l'anime>`")
        return

    query = " ".join(message.command[1:])
    status_msg = await message.reply_text("🔎 Recherche sur MyAnimeList...")

    raw_results = await my_anime_liste.search_anime(query)
    # Normalise vers {"id": mal_id, "title": ..., "thumb": ...}
    results = [{"id": r["mal_id"], "title": r["title"], "thumb": r["thumb"]} for r in raw_results]

    await status_msg.delete()
    await _send_menu(message, results, "mal_sel", query)


@Client.on_callback_query(filters.regex(r"^(naut_sel|mal_sel):"))
async def anime_selection_callback(client: Client, callback: CallbackQuery):
    try:
        prefix, cache_key, idx_str = callback.data.split(":", 2)
        idx = int(idx_str)
    except (ValueError, AttributeError):
        await callback.answer("Sélection invalide.", show_alert=True)
        return

    results = _SEARCH_CACHE.get(cache_key)
    if results is None or idx >= len(results):
        await callback.answer("Cette recherche a expiré, relance la commande.", show_alert=True)
        return

    selected = results[idx]
    await callback.answer("Chargement de la fiche...")
    await callback.message.edit_text(f"⏳ Récupération de la fiche pour **{selected['title']}**...")

    if prefix == "naut_sel":
        data = await nautilijan.get_anime_details(selected["id"])
        source_label = "Nautiljon"
    else:
        data = await my_anime_liste.get_anime_details(selected["id"])
        source_label = "MyAnimeList"

    if data is None:
        await callback.message.edit_text("❌ Impossible de récupérer cette fiche, réessaie plus tard.")
        return

    caption = _format_caption(data, source_label)

    if data.get("image_url"):
        await callback.message.delete()
        await client.send_photo(
            chat_id=callback.message.chat.id,
            photo=data["image_url"],
            caption=caption,
        )
    else:
        await callback.message.edit_text(caption)

    # Nettoyage du cache une fois la fiche envoyée
    _SEARCH_CACHE.pop(cache_key, None)
