"""
store.py (Modul "backup") - Speicherziele, Jobs, Katalog und Repo-Geheimnisse.

Ablage in data/modules/backup/:
  storages.json  Speicherziele (wie storage.cfg bei Proxmox)
  jobs.json      Backup-Jobs (Zeitplan, Ziel, Modus, Aufbewahrung ...)
  catalog.json   Bekannte Backups je Client (vom Agenten gemeldet) +
                 Namen/Notizen/Schutz, die wir selbst verwalten
  secrets.json   restic-Passwoerter und REST-Zugangstoken je Repo
                 (Repo = Client x Speicherziel). Datei ist 0600.
"""

from __future__ import annotations

import json
import secrets
import threading
import time

from app.paths import MODULES_DATA_DIR, STORAGE_DIR

DIR = MODULES_DATA_DIR / "backup"
LOCAL_ROOT = STORAGE_DIR / "backup"          # Speicherziel "Backend"
_lock = threading.RLock()

STORAGE_TYPES = ("backend", "smb", "sftp", "ftp", "pbs")
SECRET_FIELDS = ("password", "secret", "private_key")
MASK = "••••••"


def _now() -> int:
    return int(time.time() * 1000)


def _read(name: str, default):
    with _lock:
        try:
            return json.loads((DIR / name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default


def _write(name: str, data) -> None:
    with _lock:
        DIR.mkdir(parents=True, exist_ok=True)
        p = DIR / name
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)
        if name == "secrets.json":
            try:
                p.chmod(0o600)
            except OSError:
                pass


# ------------------------------------------------------------------ Speicherziele

def storages() -> dict:
    d = _read("storages.json", {})
    if "local" not in d:                         # Standard-Ziel: das Backend selbst
        d["local"] = {"type": "backend", "label": "Backend (lokal)", "enabled": True}
        _write("storages.json", d)
    return d


def storage(sid: str) -> dict | None:
    s = storages().get(sid)
    return {**s, "id": sid} if s else None


def storage_public(sid: str, s: dict) -> dict:
    """Ohne Geheimnisse (fuers Dashboard)."""
    out = {k: (MASK if k in SECRET_FIELDS and v else v) for k, v in s.items()}
    if s.get("type") == "sftp" and s.get("private_key"):
        out["public_key"] = s.get("public_key", "")
    return {**out, "id": sid}


def save_storage(sid: str, data: dict) -> dict:
    d = storages()
    old = d.get(sid, {})
    for k in SECRET_FIELDS:                      # maskierte Felder nicht ueberschreiben
        if data.get(k) == MASK:
            data[k] = old.get(k, "")
    d[sid] = data
    _write("storages.json", d)
    return {**data, "id": sid}


def delete_storage(sid: str) -> None:
    d = storages()
    d.pop(sid, None)
    _write("storages.json", d)


# ------------------------------------------------------------------ Jobs

def jobs() -> dict:
    return _read("jobs.json", {})


def job(jid: str) -> dict | None:
    j = jobs().get(jid)
    return {**j, "id": jid} if j else None


def save_job(jid: str | None, data: dict) -> dict:
    d = jobs()
    jid = jid or secrets.token_hex(5)
    keep = {k: d.get(jid, {}).get(k) for k in ("last_run", "last_status", "missed")}
    d[jid] = {**{k: v for k, v in keep.items() if v is not None}, **data}
    _write("jobs.json", d)
    return {**d[jid], "id": jid}


def update_job_state(jid: str, **kw) -> None:
    d = jobs()
    if jid in d:
        d[jid].update(kw)
        _write("jobs.json", d)


def delete_job(jid: str) -> None:
    d = jobs()
    d.pop(jid, None)
    _write("jobs.json", d)


# ------------------------------------------------------------------ Repo-Geheimnisse

def repo_secret(client_id: str, sid: str) -> dict:
    """{password, token} fuer das Repo Client x Ziel (wird bei Bedarf erzeugt)."""
    key = f"{client_id}|{sid}"
    d = _read("secrets.json", {})
    if key not in d:
        d[key] = {"password": secrets.token_urlsafe(32), "token": secrets.token_urlsafe(32),
                  "created": _now()}
        _write("secrets.json", d)
    return d[key]


def repo_by_token(token: str) -> tuple[str, str] | None:
    """REST-Token -> (client_id, storage_id)."""
    import hmac
    for key, v in _read("secrets.json", {}).items():
        if hmac.compare_digest(str(v.get("token") or ""), token or ""):
            cid, sid = key.split("|", 1)
            return cid, sid
    return None


# ------------------------------------------------------------------ Katalog

def catalog() -> dict:
    return _read("catalog.json", {})


def set_snapshots(client_id: str, sid: str, engine: str, snaps: list[dict],
                  route: str = "") -> None:
    """Vom Agenten gemeldete Snapshot-Liste uebernehmen (Namen/Schutz bleiben)."""
    d = catalog()
    key = f"{client_id}|{sid}"
    old = {s["id"]: s for s in d.get(key, {}).get("snapshots", [])}
    merged = []
    for s in snaps:
        prev = old.get(s["id"], {})
        merged.append({**s, "name": prev.get("name") or s.get("name") or "",
                       "notes": prev.get("notes", s.get("notes", "")),
                       "protected": prev.get("protected", bool(s.get("protected")))})
    d[key] = {"client_id": client_id, "storage": sid, "engine": engine,
              "route": route or d.get(key, {}).get("route", ""),
              "updated": _now(), "snapshots": merged}
    _write("catalog.json", d)


def update_snapshot_meta(client_id: str, sid: str, snap_id: str, **kw) -> dict | None:
    d = catalog()
    entry = d.get(f"{client_id}|{sid}")
    if not entry:
        return None
    for s in entry["snapshots"]:
        if s["id"] == snap_id:
            s.update({k: v for k, v in kw.items() if v is not None})
            _write("catalog.json", d)
            return s
    return None


def protected_ids(client_id: str, sid: str) -> list[str]:
    entry = catalog().get(f"{client_id}|{sid}") or {}
    return [s["id"] for s in entry.get("snapshots", []) if s.get("protected")]


# ------------------------------------------------------------------ OS-Snapshot-Namen
# (VSS kennt keine Namen - wir merken sie uns hier)

def fs_snapshot_names() -> dict:
    return _read("fs_snapshot_names.json", {})


def set_fs_snapshot_name(client_id: str, snap_id: str, name: str) -> None:
    d = fs_snapshot_names()
    d[f"{client_id}|{snap_id}"] = name[:120]
    _write("fs_snapshot_names.json", d)
