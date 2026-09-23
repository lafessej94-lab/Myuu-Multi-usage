from __future__ import annotations

import asyncio
import logging
import os
import re as _re
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)

_OAUTH_URL = "https://www.seedr.cc/oauth_test/token"
_API_RESOURCE = "https://www.seedr.cc/oauth_test/resource.php"
_CLIENT_ID = "seedr_xbmc"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_MIN_FREE = 256 * 1024 * 1024
_CLIENT_CACHE: dict[str, object] = {}


class SeedrError(RuntimeError):
    pass


class SeedrAuthError(SeedrError):
    pass


class SeedrQuotaError(SeedrError):
    pass


def _accounts() -> list[tuple[str, str]]:
    users = [u.strip() for u in os.environ.get("SEEDR_USERNAME", "").split(",") if u.strip()]
    pwds = [p.strip() for p in os.environ.get("SEEDR_PASSWORD", "").split(",") if p.strip()]
    if not users or not pwds:
        raise SeedrAuthError(
            "Seedr credentials missing. Set SEEDR_USERNAME and SEEDR_PASSWORD."
        )
    if len(pwds) == 1:
        pwds = pwds * len(users)
    return list(zip(users, pwds))


def _proxy() -> Optional[str]:
    return os.environ.get("SEEDR_PROXY", "").strip() or None


def _to_dict(obj) -> dict:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return {k: _to_dict(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict") and callable(obj.dict):
        return obj.dict()
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        return obj.to_dict()
    if hasattr(obj, "__dict__"):
        return {k: _to_dict(v) for k, v in vars(obj).items() if not k.startswith("_")}
    return obj


def _attr(obj, *keys, default=None):
    for key in keys:
        if isinstance(obj, dict):
            value = obj.get(key)
        else:
            value = getattr(obj, key, None)
        if value is not None:
            return value
    return default


def _list_of(obj, *keys) -> list:
    for key in keys:
        if isinstance(obj, dict):
            value = obj.get(key)
        else:
            value = getattr(obj, key, None)
        if value is not None:
            return list(value) if not isinstance(value, list) else value
    return []


def _parse_progress(raw) -> float:
    if raw is None:
        return 0.0
    try:
        return float(str(raw).replace("%", "").strip())
    except (ValueError, TypeError):
        return 0.0


def _file_id(f) -> Optional[int]:
    for key in ("folder_file_id", "id", "file_id"):
        value = _attr(f, key)
        if value is not None:
            try:
                return int(value)
            except (ValueError, TypeError):
                pass
    return None


def _normalise_contents(raw) -> dict:
    actual = raw
    for unwrap_key in ("result", "folder", "data"):
        candidate = _attr(raw, unwrap_key)
        if candidate is not None and (
            hasattr(candidate, "__dict__")
            or (isinstance(candidate, dict) and ("files" in candidate or "folders" in candidate))
        ):
            actual = candidate
            break

    folders_raw = _list_of(actual, "folders")
    files_raw = _list_of(actual, "files", "folder_files")
    torrents_raw = _list_of(actual, "torrents")

    folders: list[dict] = []
    for folder in folders_raw:
        folder_id = _attr(folder, "id")
        if folder_id is None:
            continue
        try:
            folders.append({
                "id": int(folder_id),
                "name": _attr(folder, "name", default=""),
                "size": int(_attr(folder, "size", default=0) or 0),
            })
        except (ValueError, TypeError):
            pass

    files: list[dict] = []
    for file_obj in files_raw:
        file_id = _file_id(file_obj)
        if file_id is None:
            continue
        files.append({
            "id": file_id,
            "name": _attr(file_obj, "name", default="file"),
            "size": int(_attr(file_obj, "size", default=0) or 0),
            "url": (
                _attr(file_obj, "url")
                or _attr(file_obj, "download_url")
                or _attr(file_obj, "stream_url")
                or _attr(file_obj, "link")
                or ""
            ),
        })

    torrents: list[dict] = []
    for torrent in torrents_raw:
        torrent_id = _attr(torrent, "id")
        try:
            torrent_id = int(torrent_id) if torrent_id is not None else None
        except (ValueError, TypeError):
            torrent_id = None
        torrents.append({
            "id": torrent_id,
            "name": _attr(torrent, "name", default=""),
            "progress": _parse_progress(_attr(torrent, "progress")),
            "size": int(_attr(torrent, "size", default=0) or 0),
        })

    return {
        "folders": folders,
        "files": files,
        "torrents": torrents,
        "space_used": _attr(actual, "space_used"),
        "space_max": _attr(actual, "space_max"),
    }


async def _get_client(username: str, password: str):
    from seedrcc import AsyncSeedr

    cached = _CLIENT_CACHE.get(username)
    if cached is not None:
        try:
            await cached.get_settings()
            return cached
        except Exception:
            _CLIENT_CACHE.pop(username, None)

    try:
        client = await AsyncSeedr.from_password(username, password)
        _CLIENT_CACHE[username] = client
        return client
    except Exception as exc:
        raise SeedrAuthError(f"Seedr login failed ({username[:25]}): {exc}") from exc


def _invalidate(username: str) -> None:
    _CLIENT_CACHE.pop(username, None)


async def _root(username: str, password: str) -> dict:
    client = await _get_client(username, password)
    try:
        raw = await client.list_contents(folder_id="0")
        return _normalise_contents(raw)
    except Exception as exc:
        _invalidate(username)
        raise SeedrError(f"list_contents(root) failed: {exc}") from exc


async def _list_folder(username: str, password: str, folder_id: int) -> dict:
    client = await _get_client(username, password)
    try:
        raw = await client.list_contents(folder_id=str(folder_id))
        return _normalise_contents(raw)
    except Exception as exc:
        _invalidate(username)
        raise SeedrError(f"list_contents({folder_id}) failed: {exc}") from exc


async def _storage(username: str, password: str) -> dict:
    total_keys = ("space_max", "storage_max", "storage_total", "space_total", "quota", "quota_total")
    used_keys = ("space_used", "storage_used", "used_space", "quota_used")

    def _read_storage(data: dict) -> tuple[int, int]:
        total = used = 0
        sources = [data]
        account = data.get("account")
        if isinstance(account, dict):
            sources.append(account)

        for src in sources:
            for key in total_keys:
                value = src.get(key)
                if value:
                    try:
                        total = int(value)
                        break
                    except (ValueError, TypeError):
                        pass
            if total:
                break

        for src in sources:
            for key in used_keys:
                value = src.get(key)
                if value is not None:
                    try:
                        used = int(value)
                        break
                    except (ValueError, TypeError):
                        pass
            if used:
                break

        return total, used

    client = await _get_client(username, password)
    try:
        raw = await client.get_settings()
        total, used = _read_storage(_to_dict(raw))
        if total > 0:
            return {"total": total, "used": used, "free": max(0, total - used), "unknown": False}
    except Exception as exc:
        log.warning("[Seedr] get_settings failed: %s", exc)

    try:
        root = await _root(username, password)
        total = int(root.get("space_max", 0) or 0)
        used = int(root.get("space_used", 0) or 0)
        if total > 0:
            return {"total": total, "used": used, "free": max(0, total - used), "unknown": False}
    except Exception as exc:
        log.warning("[Seedr] storage fallback failed: %s", exc)

    return {"total": 0, "used": 0, "free": 9_999_999_999, "unknown": True}


async def _ensure_free(username: str, password: str, needed: int = 0) -> int:
    want = max(needed, _MIN_FREE)
    info = await _storage(username, password)
    if info.get("unknown") or info["free"] >= want:
        return info["free"]

    root = await _root(username, password)
    for folder in root["folders"]:
        await _del_folder(username, password, folder["id"])

    info = await _storage(username, password)
    if not info.get("unknown") and info["free"] < want:
        raise SeedrQuotaError(
            f"{username[:25]}: {info['free'] // 1024 // 1024} MB free (need >= {want // 1024 // 1024} MB)"
        )
    return info["free"]


async def _pick_account(needed: int = 0) -> tuple[str, str, int]:
    best_user = best_pwd = None
    best_free = -1
    last_err: Optional[Exception] = None
    for user, pwd in _accounts():
        try:
            free = await _ensure_free(user, pwd, needed)
            if free > best_free:
                best_user, best_pwd, best_free = user, pwd, free
        except Exception as exc:
            last_err = exc
            log.warning("[Seedr] account %s skipped: %s", user[:25], exc)
    if best_user is None or best_pwd is None:
        raise last_err or SeedrQuotaError("All Seedr accounts are full or unreachable.")
    return best_user, best_pwd, best_free


async def _fresh_token(username: str, password: str, proxy: bool = True) -> str:
    import httpx

    async with httpx.AsyncClient(
        proxy=_proxy() if proxy else None,
        headers={"User-Agent": _UA},
        timeout=30,
        follow_redirects=True,
    ) as client:
        resp = await client.post(_OAUTH_URL, data={
            "grant_type": "password",
            "client_id": _CLIENT_ID,
            "username": username,
            "password": password,
        })
        resp.raise_for_status()
        return resp.json().get("access_token", "")


def _extract_token(client) -> str:
    for attr in ("token", "_token", "access_token", "_access_token"):
        value = getattr(client, attr, None)
        if isinstance(value, str) and value:
            return value
    for container in ("_auth", "auth", "_session", "session"):
        obj = getattr(client, container, None)
        if obj:
            for attr in ("token", "access_token", "_token"):
                value = getattr(obj, attr, None)
                if isinstance(value, str) and value:
                    return value
    return ""


async def _submit_magnet(username: str, password: str, magnet: str) -> Optional[int]:
    try:
        client = await _get_client(username, password)
        raw = await client.add_torrent(magnet_link=magnet)
        result = _to_dict(raw)
        rv = result.get("result")
        if rv is False or str(rv).lower() == "false":
            raise SeedrError(result.get("error") or result.get("message") or "add_torrent rejected")
        tid = (
            result.get("user_torrent_id")
            or result.get("torrent_id")
            or result.get("id")
            or (result.get("data") or {}).get("user_torrent_id")
            or (result.get("data") or {}).get("torrent_id")
        )
        return int(tid) if tid else None
    except SeedrError:
        raise
    except Exception as exc:
        log.warning("[Seedr] seedrcc add_torrent failed: %s", exc)

    import httpx

    token = await _fresh_token(username, password, proxy=True)
    if not token:
        raise SeedrAuthError("Could not obtain OAuth token for Seedr proxied add_torrent")

    async with httpx.AsyncClient(
        proxy=_proxy(),
        headers={"User-Agent": _UA, "Authorization": f"Bearer {token}"},
        timeout=120,
        follow_redirects=True,
    ) as client:
        resp = await client.post(_API_RESOURCE, data={
            "access_token": token,
            "func": "add_torrent",
            "torrent_magnet": magnet,
        })
        resp.raise_for_status()
        body = resp.json()

    rv = body.get("result")
    if rv is False or str(rv).lower() == "false":
        raise SeedrError(body.get("error") or body.get("message") or "add_torrent blocked")
    tid = body.get("torrent_id") or body.get("id")
    return int(tid) if tid else None


async def _del_folder(username: str, password: str, folder_id: int) -> None:
    try:
        client = await _get_client(username, password)
        await client.delete_folder(folder_id=str(folder_id))
    except Exception as exc:
        log.warning("[Seedr] delete folder %d failed (non-fatal): %s", folder_id, exc)


def _clean_seedr_name(name: str) -> str:
    base, ext = os.path.splitext(name)
    base = _re.sub(r"\[[^\]]*\]|\{[^}]*\}|\([^)]*\)", "", base)
    base = _re.sub(r"[^\w\-]", "_", base)
    base = _re.sub(r"_+", "_", base).strip("_")
    return (base or "video") + ext.lower()


async def _rename_file(username: str, password: str, file_id: int, new_name: str) -> bool:
    import httpx

    client_obj = await _get_client(username, password)
    token = _extract_token(client_obj) or await _fresh_token(username, password, proxy=False)
    if not token:
        return False
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": _UA},
            timeout=20,
            follow_redirects=True,
        ) as client:
            resp = await client.post(_API_RESOURCE, data={
                "access_token": token,
                "func": "rename",
                "folder_file_id": str(file_id),
                "rename_to": new_name,
            })
            resp.raise_for_status()
            body = resp.json()
        result = body.get("result")
        return result is True or str(result).lower() in ("true", "1")
    except Exception as exc:
        log.warning("[Seedr] rename %d -> %s failed: %s", file_id, new_name, exc)
        return False


async def _file_url_fallback(username: str, password: str, file_id: int) -> str:
    import httpx

    try:
        client = await _get_client(username, password)
        raw = await client.fetch_file(file_id=str(file_id))
        data = _to_dict(raw)
        url = data.get("url") or data.get("download_url") or ""
        if not url and isinstance(data.get("result"), dict):
            url = data["result"].get("url") or data["result"].get("download_url") or ""
        if not url and isinstance(data.get("data"), dict):
            url = data["data"].get("url") or data["data"].get("download_url") or ""
        if not url:
            result = data.get("result")
            if isinstance(result, str) and result.startswith("http"):
                url = result
        if url:
            return url
    except Exception as exc:
        log.warning("[Seedr] fetch_file fallback for %d: %s", file_id, exc)
        _invalidate(username)

    client_obj = await _get_client(username, password)
    token = _extract_token(client_obj) or await _fresh_token(username, password, proxy=False)
    if not token:
        raise SeedrError("No token available for fetch_file fallback")

    async with httpx.AsyncClient(
        headers={"User-Agent": _UA, "Authorization": f"Bearer {token}"},
        timeout=20,
        follow_redirects=True,
    ) as client:
        resp = await client.post(_API_RESOURCE, data={
            "access_token": token,
            "func": "fetch_file",
            "folder_file_id": str(file_id),
        })
        resp.raise_for_status()
        body = resp.json()

    url = body.get("url") or body.get("download_url") or ""
    if isinstance(body.get("result"), str) and body["result"].startswith("http"):
        url = body["result"]
    if not url:
        raise SeedrError(f"Seedr fetch_file returned no URL for file {file_id}")
    return url


async def _collect_files(username: str, password: str, folder_id: int) -> list[dict]:
    result: list[dict] = []

    async def _walk(fid: int, depth: int = 0) -> None:
        if depth > 5:
            return
        contents = await _list_folder(username, password, fid)

        for file_info in contents["files"]:
            orig_name = file_info.get("name", "file")
            size = int(file_info.get("size", 0) or 0)
            file_id = file_info.get("id")
            emb_url = file_info.get("url", "")

            clean_name = _clean_seedr_name(orig_name)
            needs_rename = clean_name != orig_name
            if needs_rename and file_id:
                renamed = await _rename_file(username, password, file_id, clean_name)
                if renamed:
                    emb_url = ""
                else:
                    clean_name = orig_name

            if emb_url and not needs_rename:
                url = emb_url
            elif file_id:
                url = await _file_url_fallback(username, password, file_id)
            else:
                continue

            result.append({
                "id": file_id,
                "name": clean_name,
                "orig_name": orig_name,
                "original_name": orig_name,
                "url": url,
                "size": size,
                "clean_name": clean_name,
            })

        for subfolder in contents["folders"]:
            sid = subfolder.get("id")
            if sid:
                await _walk(sid, depth + 1)

    await _walk(folder_id)
    return result


async def _poll(
    username: str,
    password: str,
    torrent_id: Optional[int],
    pre_existing_folder_ids: set[int],
    timeout_s: int = 7200,
    progress_cb: Optional[Callable[[float, str], None]] = None,
    interval: float = 10.0,
) -> dict:
    deadline = time.monotonic() + timeout_s
    last_pct = -1.0
    last_hb = time.monotonic()
    ever_seen = False
    gone_polls = 0

    while time.monotonic() < deadline:
        root = await _root(username, password)
        folders = root["folders"]
        torrents = root["torrents"]
        now = time.monotonic()

        active = None
        if torrent_id is not None:
            for torrent in torrents:
                if torrent.get("id") == torrent_id:
                    active = torrent
                    break
        else:
            active = torrents[0] if torrents else None

        if active is not None:
            ever_seen = True
            pct = float(active.get("progress", 0.0))
            name = active.get("name") or "Seedr torrent"
            if progress_cb and (pct != last_pct or now - last_hb >= 20):
                await progress_cb(min(max(pct, 0.0), 99.0), f"{name[:48]} [{pct:.1f}%]")
                last_pct = pct
                last_hb = now
            gone_polls = 0
        else:
            if ever_seen:
                gone_polls += 1
            elif progress_cb and now - last_hb >= 20:
                await progress_cb(2.0, "Waiting for Seedr to create folder...")
                last_hb = now

        new_folders = [f for f in folders if f["id"] not in pre_existing_folder_ids]
        if new_folders:
            best = max(new_folders, key=lambda f: int(f.get("size", 0) or 0))
            if int(best.get("size", 0) or 0) > 0 and (active is None or float(active.get("progress", 0.0)) >= 100 or gone_polls >= 2):
                return best

        await asyncio.sleep(interval)

    raise SeedrError("Seedr timed out while waiting for torrent completion.")


async def fetch_urls_via_seedr(
    magnet: str,
    progress_cb: Optional[Callable[[str, float, str], None]] = None,
    timeout_s: int = 7200,
) -> tuple:
    if progress_cb:
        await progress_cb("selecting", 0.0, "Selecting Seedr account...")
    user, pwd, _ = await _pick_account()

    if progress_cb:
        await progress_cb("submitting", 3.0, "Snapshotting account state...")
    pre_ids = {f["id"] for f in (await _root(user, pwd))["folders"]}

    if progress_cb:
        await progress_cb("submitting", 5.0, "Submitting magnet to Seedr...")
    torrent_id = await _submit_magnet(user, pwd, magnet)

    if progress_cb:
        await progress_cb("waiting", 5.0, "Seedr is fetching torrent...")

    async def _pcb(pct: float, name: str) -> None:
        if progress_cb:
            await progress_cb("downloading" if pct > 0.5 else "waiting", min(pct, 99.0), name or "Downloading...")

    folder = await _poll(user, pwd, torrent_id, pre_ids, timeout_s, _pcb)
    folder_id = int(folder["id"])

    if progress_cb:
        await progress_cb("fetching", 99.0, "Renaming and getting CDN links...")
    files = await _collect_files(user, pwd, folder_id)
    if not files:
        raise SeedrError("Seedr produced no downloadable files.")

    for file_info in files:
        file_info.setdefault("clean_name", file_info["name"])

    return files, folder_id, user, pwd


async def check_credentials() -> bool:
    try:
        await _pick_account()
        return True
    except Exception as exc:
        log.warning("[Seedr] credential check failed: %s", exc)
        return False
