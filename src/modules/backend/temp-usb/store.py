"""
store.py (Modul "temp-usb", frueher app/tempusb_store.py) - Digitaler USB-Stick
---------------------------------------------
Ein "USB-Bereich" ist ein Ordner unter /storage/temp-usb/<id>, der genau EINEM
Besitzer gehoert: einem Benutzer ODER einer Gruppe. Dateien darin lassen sich an
ein Geraet "anhaengen": Der Agent spiegelt sie in einen lokalen Ordner
(Ordner-Sync, Standard C:\\bytera bzw. /mnt/bytera) oder - wenn ein Treiber
hinterlegt ist - als virtuellen USB-Stick. Was der Benutzer am Geraet in den
Ordner legt, kommt zurueck in den Bereich und ist im Relay sichtbar.

Die Verwaltung (wer sieht welchen Bereich) laeuft ueber Besitzer + Recht
manage_temp_usb; die eigentliche Uebertragung ueber die Socket-Ereignisse
temp-usb-* (siehe router/tempusb_routes.py und agent/main.py).

Alles liegt als JSON neben den Ordnern (_areas.json). Idempotent, threadsicher.
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

# Standard-Zielordner am Geraet (einstellbar je Anhang; das ist die Vorgabe).
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
    """Bereiche, die der Benutzer sehen darf: eigene, die seiner Gruppen, oder alle (Admin/Recht)."""
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
        raise ValueError("owner_type muss user oder group sein")
    area_id = "u" + secrets.token_hex(8)
    with _lock:
        d = _load()
        d[area_id] = {"label": (label or "USB-Stick").strip()[:80], "owner_type": owner_type,
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
    """info=None -> geloest. Sonst {mode, path, ts}."""
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


# ------------------------------------------------------------------ Dateien im Bereich
def _safe(name: str) -> str:
    name = Path(str(name or "").replace("\\", "/")).name.strip()
    bad = '<>:"/\\|?*'
    name = "".join("_" if (ch in bad or ord(ch) < 32) else ch for ch in name)
    return name[:180] or "datei"


def _resolve(area_id: str, rel: str) -> Path:
    base = area_dir(area_id).resolve()
    p = (base / rel.replace("\\", "/").strip("/")).resolve() if rel else base
    if base != p and base not in p.parents:
        raise ValueError("Pfad ausserhalb des Bereichs")
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
