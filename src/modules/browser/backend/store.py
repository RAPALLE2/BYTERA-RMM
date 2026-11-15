# store.py (Modul "browser") - alles, was der Browser je Benutzer behaelt
# ---------------------------------------------------------------------
# data/modules/browser/users/<user-id>/
#   bookmarks.json   {"items": [{id, title, url, folder, ts}], "folders": ["toolbar", "menu", ...]}
#                    folder "toolbar" = Lesezeichen-Symbolleiste, "menu" = Lesezeichen-Menue
#   history.json     [{url, title, ts}]   (neueste zuerst, max. HISTORY_MAX)
#   settings.json    {homepage, search, restore, default_network}
#   session.json     {tabs: [{url, title, network}], active}   (Tabs wiederherstellen)
#   cookies-<netz>.bin  Playwright storage_state (Cookies/LocalStorage) je Netzwerk,
#                    VERSCHLUESSELT (Fernet, Schluessel aus JWT_SECRET)
# Downloads liegen als Dateien unter storage/browser/<user-id>/ (siehe session.py).
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path

from app.config import JWT_SECRET
from app.paths import MODULES_DATA_DIR, STORAGE_DIR

ROOT = MODULES_DATA_DIR / "browser"
USERS = ROOT / "users"
DOWNLOADS = STORAGE_DIR / "browser"
HISTORY_MAX = 1000
_lock = threading.RLock()

SEARCH_ENGINES = {
    "duckduckgo": ("DuckDuckGo", "https://duckduckgo.com/?q={q}"),
    "google": ("Google", "https://www.google.com/search?q={q}"),
    "bing": ("Bing", "https://www.bing.com/search?q={q}"),
    "startpage": ("Startpage", "https://www.startpage.com/do/search?q={q}"),
    "ecosia": ("Ecosia", "https://www.ecosia.org/search?q={q}"),
}
DEFAULT_SETTINGS = {"homepage": "about:newtab", "search": "duckduckgo", "restore": True,
                    "default_network": "direct"}


def _udir(uid: str) -> Path:
    safe = "".join(c for c in str(uid) if c.isalnum() or c in "-_")[:64] or "x"
    d = USERS / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


# ------------------------------------------------------------------ Einstellungen
def settings(uid: str) -> dict:
    with _lock:
        return {**DEFAULT_SETTINGS, **_read(_udir(uid) / "settings.json", {})}


def save_settings(uid: str, changes: dict) -> dict:
    with _lock:
        cur = settings(uid)
        for k in DEFAULT_SETTINGS:
            if k in changes and changes[k] is not None:
                cur[k] = changes[k]
        if cur["search"] not in SEARCH_ENGINES:
            cur["search"] = "duckduckgo"
        cur["homepage"] = str(cur["homepage"] or "about:newtab")[:2000]
        _write(_udir(uid) / "settings.json", cur)
        return cur


def search_url(uid: str, query: str) -> str:
    from urllib.parse import quote_plus
    eng = SEARCH_ENGINES.get(settings(uid)["search"]) or SEARCH_ENGINES["duckduckgo"]
    return eng[1].replace("{q}", quote_plus(query))


# ------------------------------------------------------------------ Lesezeichen
def _migrate_old_bookmarks(uid: str, path: Path) -> None:
    """Fruehere Fassung: ein gemeinsames bookmarks.json {user_id: [..]}."""
    old = ROOT / "bookmarks.json"
    if path.exists() or not old.exists():
        return
    lst = (_read(old, {}) or {}).get(uid) or []
    items = [{"id": b.get("id") or secrets.token_hex(6), "title": b.get("title") or b.get("url"),
              "url": b.get("url"), "folder": "toolbar", "ts": b.get("ts") or int(time.time())} for b in lst if b.get("url")]
    _write(path, {"items": items, "folders": ["toolbar", "menu"]})


def bookmarks(uid: str) -> dict:
    with _lock:
        path = _udir(uid) / "bookmarks.json"
        _migrate_old_bookmarks(uid, path)
        d = _read(path, {"items": [], "folders": ["toolbar", "menu"]})
        d.setdefault("items", [])
        d.setdefault("folders", ["toolbar", "menu"])
        for f in ("toolbar", "menu"):
            if f not in d["folders"]:
                d["folders"].insert(0, f)
        return d


def add_bookmark(uid: str, title: str, url: str, folder: str = "toolbar") -> dict:
    with _lock:
        d = bookmarks(uid)
        folder = (folder or "toolbar").strip()[:60] or "toolbar"
        if folder not in d["folders"]:
            d["folders"].append(folder)
        b = {"id": secrets.token_hex(6), "title": (title or url)[:200], "url": url[:4000],
             "folder": folder, "ts": int(time.time())}
        d["items"].append(b)
        _write(_udir(uid) / "bookmarks.json", d)
        return b


def edit_bookmark(uid: str, bid: str, title=None, url=None, folder=None, index=None) -> dict | None:
    with _lock:
        d = bookmarks(uid)
        b = next((x for x in d["items"] if x["id"] == bid), None)
        if not b:
            return None
        if title is not None:
            b["title"] = str(title)[:200]
        if url is not None:
            b["url"] = str(url)[:4000]
        if folder:
            b["folder"] = str(folder)[:60]
            if b["folder"] not in d["folders"]:
                d["folders"].append(b["folder"])
        if index is not None:                    # innerhalb des Ordners verschieben
            d["items"].remove(b)
            same = [i for i, x in enumerate(d["items"]) if x["folder"] == b["folder"]]
            pos = same[int(index)] if 0 <= int(index) < len(same) else (same[-1] + 1 if same else len(d["items"]))
            d["items"].insert(pos, b)
        _write(_udir(uid) / "bookmarks.json", d)
        return b


def delete_bookmark(uid: str, bid: str) -> None:
    with _lock:
        d = bookmarks(uid)
        d["items"] = [x for x in d["items"] if x["id"] != bid]
        _write(_udir(uid) / "bookmarks.json", d)


def add_folder(uid: str, name: str) -> dict:
    with _lock:
        d = bookmarks(uid)
        name = (name or "").strip()[:60]
        if name and name not in d["folders"]:
            d["folders"].append(name)
            _write(_udir(uid) / "bookmarks.json", d)
        return d


def delete_folder(uid: str, name: str) -> dict:
    with _lock:
        d = bookmarks(uid)
        if name in ("toolbar", "menu"):
            return d
        d["folders"] = [f for f in d["folders"] if f != name]
        for b in d["items"]:
            if b["folder"] == name:
                b["folder"] = "menu"
        _write(_udir(uid) / "bookmarks.json", d)
        return d


# ------------------------------------------------------------------ Verlauf
def history(uid: str, q: str = "", limit: int = 200) -> list[dict]:
    with _lock:
        h = _read(_udir(uid) / "history.json", [])
    if q:
        ql = q.lower()
        h = [e for e in h if ql in (e.get("url") or "").lower() or ql in (e.get("title") or "").lower()]
    return h[:limit]


def add_history(uid: str, url: str, title: str) -> None:
    if not url or url.startswith(("about:", "data:", "chrome-error:")):
        return
    with _lock:
        path = _udir(uid) / "history.json"
        h = _read(path, [])
        if h and h[0].get("url") == url:
            h[0]["title"] = title or h[0].get("title")
            h[0]["ts"] = int(time.time())
        else:
            h.insert(0, {"url": url[:4000], "title": (title or "")[:200], "ts": int(time.time())})
        _write(path, h[:HISTORY_MAX])


def clear_history(uid: str) -> None:
    with _lock:
        _write(_udir(uid) / "history.json", [])


# ------------------------------------------------------------------ Sitzung (Tabs wiederherstellen)
def load_session(uid: str) -> dict:
    with _lock:
        return _read(_udir(uid) / "session.json", {"tabs": [], "active": 0})


def save_session(uid: str, tabs: list[dict], active: int) -> None:
    with _lock:
        _write(_udir(uid) / "session.json", {"tabs": tabs[:50], "active": active})


# ------------------------------------------------------------------ Cookies je Netzwerk (verschluesselt)
def _fernet():
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("rmm-browser:" + JWT_SECRET).encode()).digest()))


def _cookie_file(uid: str, network: str) -> Path:
    safe = "".join(c for c in network if c.isalnum() or c in "-_") or "direct"
    return _udir(uid) / f"cookies-{safe}.bin"


def load_storage_state(uid: str, network: str) -> dict | None:
    f = _cookie_file(uid, network)
    if not f.exists():
        return None
    try:
        return json.loads(_fernet().decrypt(f.read_bytes()).decode())
    except Exception:
        return None


def save_storage_state(uid: str, network: str, state: dict) -> None:
    f = _cookie_file(uid, network)
    data = _fernet().encrypt(json.dumps(state).encode())
    tmp = f.with_suffix(".tmp")
    tmp.write_bytes(data)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(f)


def clear_cookies(uid: str) -> None:
    for f in _udir(uid).glob("cookies-*.bin"):
        try:
            f.unlink()
        except OSError:
            pass


# ------------------------------------------------------------------ Downloads
def download_dir(uid: str) -> Path:
    safe = "".join(c for c in str(uid) if c.isalnum() or c in "-_")[:64] or "x"
    d = DOWNLOADS / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_downloads(uid: str) -> list[dict]:
    d = download_dir(uid)
    out = []
    for f in sorted(d.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not f.is_file() or f.name.endswith(".part"):
            continue
        did, _, name = f.name.partition("__")
        out.append({"id": did, "name": name or f.name, "size": f.stat().st_size, "ts": int(f.stat().st_mtime)})
    return out


def download_path(uid: str, did: str) -> Path | None:
    for f in download_dir(uid).glob(f"{did}__*"):
        return f
    return None


def cleanup_downloads(max_age_days: int = 7) -> None:
    cutoff = time.time() - max_age_days * 86400
    if not DOWNLOADS.exists():
        return
    for f in DOWNLOADS.rglob("*"):
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass
