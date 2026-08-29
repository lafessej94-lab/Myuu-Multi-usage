"""
colab_leecher/access.py

Contrôle d'accès persistant pour myuu, porté depuis plugins/admin.py de
zilong et adapté à l'architecture myuu (pas de core.config/core.session,
un seul OWNER — pas de tier ADMIN séparé, colab_bot déjà instancié dans
colab_leecher/__init__.py).

Remplace l'ancien système /adduser /deluser /users qui vivait en mémoire
dans BOT.Options.allowed_users (perdu à chaque redémarrage Colab) :
  • /allow (alias /adduser)   — accorde l'accès, persistant sur disque
  • /deny  (alias /deluser)   — retire l'accès
  • /allowed (alias /users)   — liste les autorisés
  • /ban / /unban             — bloque un utilisateur même s'il était autorisé
  • /banned                   — liste les bannis
  • /broadcast                — diffuse un message (reply) à tous les autorisés

_can_use()/_owner() dans __main__.py doivent appeler is_allowed()/is_banned()
d'ici plutôt que BOT.Options.allowed_users (supprimé).
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import time
from dataclasses import dataclass
from threading import RLock
from typing import Optional

from pyrogram import enums, filters
from pyrogram.errors import FloodWait
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from colab_leecher import OWNER, colab_bot
from colab_leecher.utility.variables import Paths

log = logging.getLogger(__name__)

_ACCESS_PATH = f"{Paths.DATA_DIR}/access.json"
_lock = RLock()
_allowed: set[int] = set()
_banned: set[int] = set()
_users: dict[int, dict] = {}  # uid -> {"name": str, "username": str}


# ─────────────────────────────────────────────────────────────
# Persistence
# ─────────────────────────────────────────────────────────────

def _coerce_uid(value: object) -> Optional[int]:
    try:
        raw = str(value).strip()
        if raw.startswith("+"):
            raw = raw[1:]
        if raw.lstrip("-").isdigit():
            return int(raw)
    except Exception:
        return None
    return None


def _load() -> None:
    global _allowed, _banned, _users
    allowed, banned, users = set(), set(), {}
    try:
        with open(_ACCESS_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
        for item in raw.get("allowed", []):
            uid = _coerce_uid(item)
            if uid is not None:
                allowed.add(uid)
        for item in raw.get("banned", []):
            uid = _coerce_uid(item)
            if uid is not None:
                banned.add(uid)
        for uid_s, meta in raw.get("users", {}).items():
            uid = _coerce_uid(uid_s)
            if uid is not None and isinstance(meta, dict):
                users[uid] = {"name": meta.get("name", ""), "username": meta.get("username", "")}
    except FileNotFoundError:
        pass
    except Exception as exc:
        log.warning("[Access] load error: %s", exc)

    with _lock:
        _allowed, _banned, _users = allowed - {OWNER}, banned - {OWNER}, users

    log.info("[Access] loaded: %d allowed, %d banned, %d known users", len(_allowed), len(_banned), len(_users))


def _save_locked() -> None:
    """Écriture atomique. L'appelant doit tenir _lock."""
    try:
        os.makedirs(os.path.dirname(_ACCESS_PATH), exist_ok=True)
        payload = {
            "allowed": sorted(_allowed - {OWNER}),
            "banned": sorted(_banned - {OWNER}),
            "users": {str(uid): meta for uid, meta in _users.items()},
            "updated_at": int(time.time()),
        }
        tmp = f"{_ACCESS_PATH}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, _ACCESS_PATH)
    except Exception as exc:
        log.warning("[Access] save error: %s", exc)


_load()


# ─────────────────────────────────────────────────────────────
# API publique (utilisée par __main__.py _can_use/_owner)
# ─────────────────────────────────────────────────────────────

def is_admin(uid: int) -> bool:
    return uid == OWNER


def is_banned(uid: int) -> bool:
    if is_admin(uid):
        return False
    with _lock:
        return uid in _banned


def is_allowed(uid: int) -> bool:
    """True si l'utilisateur peut passer le contrôle d'accès (OWNER, ou
    whitelisté ET pas banni)."""
    if is_admin(uid):
        return True
    with _lock:
        return uid in _allowed and uid not in _banned


def whitelist_add(uid: int) -> bool:
    if is_admin(uid):
        return False
    with _lock:
        before = len(_allowed)
        _allowed.add(uid)
        changed = len(_allowed) != before
        if changed:
            _save_locked()
        return changed


def whitelist_remove(uid: int) -> bool:
    if is_admin(uid):
        return False
    with _lock:
        changed = uid in _allowed
        _allowed.discard(uid)
        if changed:
            _save_locked()
        return changed


def ban_add(uid: int) -> bool:
    if is_admin(uid):
        return False
    with _lock:
        before = len(_banned)
        _banned.add(uid)
        _allowed.discard(uid)
        changed = len(_banned) != before
        _save_locked()
        return changed


def ban_remove(uid: int) -> bool:
    with _lock:
        changed = uid in _banned
        _banned.discard(uid)
        if changed:
            _save_locked()
        return changed


def allowed_snapshot() -> list[int]:
    with _lock:
        return sorted(_allowed - {OWNER})


def banned_snapshot() -> list[int]:
    with _lock:
        return sorted(_banned - {OWNER})


def known_users() -> dict[int, dict]:
    with _lock:
        return dict(_users)


def register_user(uid: int, name: str = "", username: str = "") -> None:
    """Best-effort : mémorise qui a déjà parlé au bot (pour /allowed,
    /banned, /broadcast). Jamais bloquant, jamais lié à l'autorisation."""
    with _lock:
        prev = _users.get(uid, {})
        new_meta = {"name": name or prev.get("name", ""), "username": username or prev.get("username", "")}
        if new_meta != prev:
            _users[uid] = new_meta
            _save_locked()


def user_label(uid: int, name: str = "", username: str = "") -> str:
    if not name or not username:
        meta = _users.get(uid, {})
        name = name or meta.get("name", "")
        username = username or meta.get("username", "")
    safe_name = html.escape(name or "User")
    safe_username = html.escape(username or "")
    link = f"<a href='tg://user?id={uid}'>{safe_name}</a>"
    if safe_username:
        return f"{link} (@{safe_username}) · <code>{uid}</code>"
    return f"{link} · <code>{uid}</code>"


# ─────────────────────────────────────────────────────────────
# Helpers internes
# ─────────────────────────────────────────────────────────────

def _display_name(u) -> str:
    if not u:
        return ""
    parts = [getattr(u, "first_name", "") or "", getattr(u, "last_name", "") or ""]
    return " ".join(p for p in parts if p).strip()


def _usage(command: str) -> str:
    return (
        f"Usage : <code>/{command} &lt;user_id | @username&gt;</code>\n"
        f"ou réponds (reply) au message d'un utilisateur avec <code>/{command}</code>."
    )


@dataclass(frozen=True)
class TargetUser:
    uid: int
    name: str = ""
    username: str = ""

    @property
    def label(self) -> str:
        return user_label(self.uid, self.name, self.username)


async def _resolve_target(client, msg: Message) -> tuple[Optional[TargetUser], Optional[str]]:
    """Résout la cible d'une commande depuis un argument ou un reply."""
    args = list(msg.command[1:] if msg.command else [])

    if args:
        token = args[0].strip()
        uid = _coerce_uid(token)
        if uid is not None:
            meta = _users.get(uid, {})
            return TargetUser(uid=uid, name=meta.get("name", "")), None

        username = token.lstrip("@").strip()
        if username:
            try:
                tg_user = await client.get_users(username)
                return TargetUser(
                    uid=tg_user.id,
                    name=_display_name(tg_user),
                    username=tg_user.username or "",
                ), None
            except Exception as exc:
                log.info("[Access] could not resolve target %r: %s", token, exc)
                return None, f"❌ Impossible de résoudre <code>{html.escape(token)}</code>."

    reply = msg.reply_to_message
    if reply:
        if reply.from_user:
            tg_user = reply.from_user
            return TargetUser(
                uid=tg_user.id,
                name=_display_name(tg_user),
                username=tg_user.username or "",
            ), None
        forward_from = getattr(reply, "forward_from", None)
        if forward_from:
            return TargetUser(
                uid=forward_from.id,
                name=_display_name(forward_from),
                username=forward_from.username or "",
            ), None
        return None, (
            "❌ Impossible de lire l'ID de l'expéditeur (paramètres de confidentialité "
            "Telegram). Utilise plutôt l'ID numérique."
        )

    return None, None


async def _safe_notify(client, uid: int, text: str) -> None:
    """Notification best-effort : échoue silencieusement si l'utilisateur
    n'a jamais démarré de conversation privée avec le bot."""
    try:
        await client.send_message(uid, text, parse_mode=enums.ParseMode.HTML)
    except FloodWait as fw:
        await asyncio.sleep(int(fw.value) + 1)
        try:
            await client.send_message(uid, text, parse_mode=enums.ParseMode.HTML)
        except Exception:
            pass
    except Exception:
        pass


def _stop(msg: Message) -> None:
    try:
        msg.stop_propagation()
    except Exception:
        pass


def _owner_filter(_, __, msg: Message) -> bool:
    return bool(msg.from_user and msg.from_user.id == OWNER)


OWNER_ONLY = filters.create(_owner_filter)


# ─────────────────────────────────────────────────────────────
# Enregistrement passif des utilisateurs connus
# ─────────────────────────────────────────────────────────────
# group=-50 : tourne tôt, mais ne bloque JAMAIS rien (pas de
# stop_propagation) — sert juste à peupler known_users() pour que
# /allowed, /banned, /broadcast affichent de vrais noms.

@colab_bot.on_message(filters.private, group=-50)
async def _register_seen_user(_, msg: Message) -> None:
    if not msg.from_user:
        return
    try:
        register_user(msg.from_user.id, _display_name(msg.from_user), msg.from_user.username or "")
    except Exception as exc:
        log.debug("[Access] register failed for %s: %s", msg.from_user.id, exc)


# ─────────────────────────────────────────────────────────────
# Commandes
# ─────────────────────────────────────────────────────────────

@colab_bot.on_message(filters.command(["allow", "adduser"]) & filters.private & OWNER_ONLY)
async def cmd_allow(client, msg: Message) -> None:
    target, error = await _resolve_target(client, msg)
    if error:
        await msg.reply(error, parse_mode=enums.ParseMode.HTML)
        _stop(msg)
        return
    if not target:
        await msg.reply(_usage("allow"), parse_mode=enums.ParseMode.HTML)
        _stop(msg)
        return
    if is_admin(target.uid):
        await msg.reply(f"ℹ️ {target.label} est déjà le propriétaire.", parse_mode=enums.ParseMode.HTML)
        _stop(msg)
        return

    changed = whitelist_add(target.uid)
    ban_remove(target.uid)

    await msg.reply(
        "✅ <b>Accès accordé</b>\n\n"
        f"{target.label} peut maintenant utiliser le bot.\n"
        "Le ban (s'il existait) a aussi été retiré.",
        parse_mode=enums.ParseMode.HTML,
    )
    log.info("[Access] allow uid=%d changed=%s by owner=%d", target.uid, changed, msg.from_user.id)
    await _safe_notify(client, target.uid, "✅ <b>Accès accordé !</b>\n\nTu peux maintenant utiliser ce bot. Envoie /start.")
    _stop(msg)


@colab_bot.on_message(filters.command(["deny", "deluser", "removeuser", "disallow"]) & filters.private & OWNER_ONLY)
async def cmd_deny(client, msg: Message) -> None:
    target, error = await _resolve_target(client, msg)
    if error:
        await msg.reply(error, parse_mode=enums.ParseMode.HTML)
        _stop(msg)
        return
    if not target:
        await msg.reply(_usage("deny"), parse_mode=enums.ParseMode.HTML)
        _stop(msg)
        return
    if is_admin(target.uid):
        await msg.reply("❌ Impossible de retirer l'accès au propriétaire.")
        _stop(msg)
        return

    changed = whitelist_remove(target.uid)
    await msg.reply(
        "✅ <b>Accès retiré</b>\n\n"
        f"{target.label} n'est plus autorisé.\n"
        "<i>Ça ne le bannit pas — utilise /ban pour ça.</i>",
        parse_mode=enums.ParseMode.HTML,
    )
    log.info("[Access] deny uid=%d changed=%s by owner=%d", target.uid, changed, msg.from_user.id)
    await _safe_notify(client, target.uid, "🔒 <b>Accès retiré</b>\n\nTu n'as plus accès à ce bot.")
    _stop(msg)


def _allowed_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"🗑 {user_label(uid).split('·')[-1].strip() or uid}", callback_data=f"acc_remove|{uid}")]
            for uid in allowed_snapshot()]
    rows.append([InlineKeyboardButton("⏎ Fermer", callback_data="close")])
    return InlineKeyboardMarkup(rows)


def _allowed_text() -> str:
    allowed = allowed_snapshot()
    lines = ["👥 <b>UTILISATEURS AUTORISÉS</b>\n━━━━━━━━━━━━━━━━━━━━━━━━", ""]
    lines.append(f"👑 <code>{OWNER}</code>  <i>propriétaire</i>")
    if allowed:
        for uid in allowed:
            lines.append(f"• {user_label(uid)}")
    else:
        lines.append("<i>Aucun utilisateur ajouté.</i>")
    lines.append("\nAjoute-en un avec <code>/allow id</code>, tape 🗑 pour en retirer un.")
    return "\n".join(lines)


@colab_bot.on_message(filters.command(["allowed", "allowlist", "whitelist", "users"]) & filters.private & OWNER_ONLY)
async def cmd_allowed(_, msg: Message) -> None:
    await msg.reply(_allowed_text(), reply_markup=_allowed_kb(), parse_mode=enums.ParseMode.HTML)
    _stop(msg)


@colab_bot.on_callback_query(filters.regex(r"^acc_remove\|"))
async def cb_allowed_remove(_, cq) -> None:
    if cq.from_user.id != OWNER:
        await cq.answer("🔒 Réservé au propriétaire.", show_alert=True)
        return
    uid = _coerce_uid(cq.data.split("|", 1)[1])
    if uid is not None and whitelist_remove(uid):
        await cq.answer("🗑 Retiré")
    else:
        await cq.answer("Déjà retiré.")
    await cq.message.edit_text(_allowed_text(), reply_markup=_allowed_kb(), parse_mode=enums.ParseMode.HTML)


@colab_bot.on_message(filters.command(["ban", "ban_user"]) & filters.private & OWNER_ONLY)
async def cmd_ban(client, msg: Message) -> None:
    target, error = await _resolve_target(client, msg)
    if error:
        await msg.reply(error, parse_mode=enums.ParseMode.HTML)
        _stop(msg)
        return
    if not target:
        await msg.reply(_usage("ban"), parse_mode=enums.ParseMode.HTML)
        _stop(msg)
        return
    if is_admin(target.uid):
        await msg.reply("❌ Impossible de bannir le propriétaire.")
        _stop(msg)
        return

    ban_add(target.uid)
    await msg.reply(
        "🚫 <b>Utilisateur banni</b>\n\n"
        f"{target.label}\n"
        "Son accès à la whitelist a aussi été retiré.",
        parse_mode=enums.ParseMode.HTML,
    )
    log.warning("[Access] ban uid=%d by owner=%d", target.uid, msg.from_user.id)
    await _safe_notify(client, target.uid, "🚫 <b>Tu as été banni</b>\n\nTu ne peux plus utiliser ce bot.")
    _stop(msg)


@colab_bot.on_message(filters.command(["unban", "unban_user"]) & filters.private & OWNER_ONLY)
async def cmd_unban(client, msg: Message) -> None:
    target, error = await _resolve_target(client, msg)
    if error:
        await msg.reply(error, parse_mode=enums.ParseMode.HTML)
        _stop(msg)
        return
    if not target:
        await msg.reply(_usage("unban"), parse_mode=enums.ParseMode.HTML)
        _stop(msg)
        return

    ban_remove(target.uid)
    await msg.reply(
        "✅ <b>Utilisateur débanni</b>\n\n"
        f"{target.label}\n"
        "Utilise <code>/allow</code> pour lui redonner accès.",
        parse_mode=enums.ParseMode.HTML,
    )
    log.info("[Access] unban uid=%d by owner=%d", target.uid, msg.from_user.id)
    _stop(msg)


@colab_bot.on_message(filters.command(["banned", "banned_list"]) & filters.private & OWNER_ONLY)
async def cmd_banned(_, msg: Message) -> None:
    banned = banned_snapshot()
    if not banned:
        await msg.reply("✅ Aucun utilisateur banni.")
        _stop(msg)
        return
    lines = ["🚫 <b>UTILISATEURS BANNIS</b>", ""]
    for uid in banned:
        lines.append(f"• {user_label(uid)}")
    await msg.reply("\n".join(lines)[:4000], parse_mode=enums.ParseMode.HTML)
    _stop(msg)


@colab_bot.on_message(filters.command("broadcast") & filters.private & OWNER_ONLY)
async def cmd_broadcast(_, msg: Message) -> None:
    if not msg.reply_to_message:
        await msg.reply("Réponds (reply) à un message avec /broadcast pour le diffuser.")
        _stop(msg)
        return

    bcast = msg.reply_to_message
    st = await msg.reply("📡 Diffusion aux utilisateurs autorisés…")
    sent = failed = 0
    targets = set(allowed_snapshot())

    for uid in targets:
        if is_banned(uid):
            continue
        try:
            await bcast.copy(uid)
            sent += 1
        except FloodWait as fw:
            await asyncio.sleep(int(fw.value) + 1)
            try:
                await bcast.copy(uid)
                sent += 1
            except Exception:
                failed += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    await st.edit(
        "✅ <b>Diffusion terminée</b>\n\n"
        f"Envoyé : <code>{sent}</code>\n"
        f"Échoué : <code>{failed}</code>",
        parse_mode=enums.ParseMode.HTML,
    )
    _stop(msg)
