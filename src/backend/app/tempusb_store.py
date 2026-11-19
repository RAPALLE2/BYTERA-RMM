"""
tempusb_store.py (Core) - digital USB stick
---------------------------------------------
A "USB area" is a folder under /storage/temp-usb/<id> that belongs to exactly
ONE owner: a user OR a group. Files in it can be "attached" to a device: the
agent mirrors them into a local folder (folder sync, default C:\\bytera or
/mnt/bytera) or - if a driver is installed - as a virtual USB stick. Whatever
the user puts into the folder on the device comes back into the area and is
visible in the relay.

Management (who sees which area) runs via owner + the manage_temp_usb
permission; the actual transfer via the socket events temp-usb-* (see
router/tempusb_routes.py and agent/main.py).

Everything is stored as JSON next to the folders (_areas.json). Idempotent,
thread-safe.
"""
from __future__ import annotations

import json
import secrets
import shutil
import threading
import time
from pathlib import Path

from app.paths import TEMP_USB_DIR

_INDEX = TEMP_USB_DIR / "_areas.json"
_lock = threading.RLock()

# Default target folder on the device (configurable per attachment; this is the default).
DEFAULT_MOUNT = {"windows": r"C:\bytera", "posix": "/mnt/bytera"}


def ensure_dirs() -> None:
    TEMP_USB_DIR.mkdir(parents=True, exist_ok=True)
    if not _INDEX.exists():
        _save({})


def _load() -> dict:
    try:
        return json.loads(_INDEX.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict) -> None:
    TEMP_USB_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _INDEX.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(_INDEX)


def area_dir(area_id: str) -> Path:
    d = TEMP_USB_DIR / area_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def get(area_id: str) -> dict | None:
    a = _load().get(area_id)
    return {**a, "id": area_id} if a else None


def all_areas() -> list[dict]:
    return [{**a, "id": i} for i, a in _load().items()]


def visible(user: dict, group_ids: set[str], can_manage_all: bool) -> list[dict]:
    """Areas the user may see: their own, those of their groups, or all (admin/permission)."""
    out = []
    for a in all_areas():
        if (can_manage_all
                or (a["owner_type"] == "user" and a["owner_id"] == user["id"])
                or (a["owner_type"] == "group" and a["owner_id"] in group_ids)):
            out.append(a)
    return out


def may_access(area: dict, user: dict, group_ids: set[str], can_manage_all: bool) -> bool:
    return bool(area) and (
        can_manage_all
        or (area["owner_type"] == "user" and area["owner_id"] == user["id"])
        or (area["owner_type"] == "group" and area["owner_id"] in group_ids))


def create(label: str, owner_type: str, owner_id: str, created_by: str) -> dict:
    if owner_type not in ("user", "group"):
        raise ValueError("owner_type must be user or group")
    area_id = "u" + secrets.token_hex(8)
    with _lock:
        d = _load()
        d[area_id] = {"label": (label or "USB stick").strip()[:80], "owner_type": owner_type,
                      "owner_id": owner_id, "created": int(time.time()), "created_by": created_by,
                      "attached": {}}
        _save(d)
    area_dir(area_id)
    return get(area_id)


def set_label(area_id: str, label: str) -> None:
    with _lock:
        d = _load()
        if area_id in d:
            d[area_id]["label"] = (label or "").strip()[:80] or d[area_id]["label"]
            _save(d)


def delete(area_id: str) -> None:
    with _lock:
        d = _load()
        d.pop(area_id, None)
        _save(d)
    shutil.rmtree(TEMP_USB_DIR / area_id, ignore_errors=True)


def set_attached(area_id: str, client_id: str, info: dict | None) -> None:
    """info=None -> detached. Otherwise {mode, path, ts}."""
    with _lock:
        d = _load()
        a = d.get(area_id)
        if not a:
            return
        att = a.setdefault("attached", {})
        if info is None:
            att.pop(client_id, None)
        else:
            att[client_id] = info
        _save(d)


# ------------------------------------------------------------------ files in the area
def _safe(name: str) -> str:
    name = Path(str(name or "").replace("\\", "/")).name.strip()
    bad = '<>:"/\\|?*'
    name = "".join("_" if (ch in bad or ord(ch) < 32) else ch for ch in name)
    return name[:180] or "file"


def _resolve(area_id: str, rel: str) -> Path:
    base = area_dir(area_id).resolve()
    p = (base / rel.replace("\\", "/").strip("/")).resolve() if rel else base
    if base != p and base not in p.parents:
        raise ValueError("path outside the area")
    return p


def listdir(area_id: str, rel: str = "") -> list[dict]:
    p = _resolve(area_id, rel)
    if not p.is_dir():
        return []
    out = []
    for c in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
        st = c.stat()
        out.append({"name": c.name, "dir": c.is_dir(), "size": st.st_size, "mtime": int(st.st_mtime)})
    return out


def write_file(area_id: str, rel_dir: str, name: str, data: bytes) -> Path:
    d = _resolve(area_id, rel_dir)
    d.mkdir(parents=True, exist_ok=True)
    p = d / _safe(name)
    p.write_bytes(data)
    return p


def file_path(area_id: str, rel: str) -> Path:
    p = _resolve(area_id, rel)
    if not p.is_file():
        raise FileNotFoundError(rel)
    return p


def delete_entry(area_id: str, rel: str) -> None:
    p = _resolve(area_id, rel)
    if p.is_dir():
        shutil.rmtree(p, ignore_errors=True)
    elif p.exists():
        p.unlink()


def mkdir(area_id: str, rel: str) -> None:
    _resolve(area_id, rel).mkdir(parents=True, exist_ok=True)
