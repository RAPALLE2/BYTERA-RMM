"""
store.py (Modul "bmc") - BMC-Verbindungen (iLO/iDRAC), verschluesselt.

Eine Verbindung je Geraet: Adresse, Zugangsdaten (verschluesselt, Fernet),
TLS-Pruefung/Fingerabdruck, Ausfuehrungsweg (Backend oder Node) und optionale
Verknuepfung zu einem Client (client_id). Ablage:
  data/modules/bmc/connections.json
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
import uuid

from app.config import JWT_SECRET
from app.paths import MODULES_DATA_DIR

DIR = MODULES_DATA_DIR / "bmc"
FILE = DIR / "connections.json"
_lock = threading.RLock()

PUBLIC_FIELDS = ("id", "name", "host", "port", "username", "vendor", "verify_tls", "fingerprint",
                 "insecure_http", "client_id", "via", "created", "updated")


def _fernet():
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("rmm-bmc:" + JWT_SECRET).encode()).digest()))


def enc(s: str) -> str:
    return _fernet().encrypt((s or "").encode()).decode() if s else ""


def dec(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode() if token else ""
    except Exception:
        return ""


def _load() -> dict:
    with _lock:
        try:
            d = json.loads(FILE.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) and "conns" in d else {"conns": {}}
        except Exception:
            return {"conns": {}}


def _save(d: dict) -> None:
    with _lock:
        DIR.mkdir(parents=True, exist_ok=True)
        tmp = FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(FILE)


def list_conns() -> list[dict]:
    return sorted(_load()["conns"].values(), key=lambda c: (c.get("name") or "").lower())


def get(cid: str) -> dict | None:
    return _load()["conns"].get(cid)


def by_client(client_id: str) -> dict | None:
    for c in _load()["conns"].values():
        if c.get("client_id") == client_id:
            return c
    return None


def links() -> dict:
    """client_id -> {id, name, vendor} fuer Topologie/Aktionsknopf."""
    out = {}
    for c in _load()["conns"].values():
        if c.get("client_id"):
            out[c["client_id"]] = {"id": c["id"], "name": c.get("name"), "vendor": c.get("vendor") or ""}
    return out


def public(c: dict) -> dict:
    d = {k: c.get(k) for k in PUBLIC_FIELDS}
    d["has_password"] = bool(c.get("password_enc"))
    return d


def update(fn):
    with _lock:
        d = _load()
        res = fn(d)
        _save(d)
        return res


def creds(c: dict) -> dict:
    """Vollstaendige Verbindungsdaten inkl. entschluesseltem Passwort (fuer den Redfish-Client)."""
    return {"host": c.get("host"), "port": c.get("port"), "username": c.get("username"),
            "password": dec(c.get("password_enc", "")), "verify_tls": c.get("verify_tls"),
            "fingerprint": c.get("fingerprint"), "insecure_http": c.get("insecure_http")}


def create(data: dict) -> dict:
    cid = uuid.uuid4().hex[:12]
    c = {"id": cid, "created": int(time.time())}
    _apply(data, c, keep_pw=False)
    update(lambda d: d["conns"].__setitem__(cid, c))
    return c


def edit(cid: str, data: dict) -> dict | None:
    def fn(d):
        c = d["conns"].get(cid)
        if not c:
            return None
        _apply(data, c, keep_pw=True)
        return c
    return update(fn)


def delete(cid: str) -> None:
    update(lambda d: d["conns"].pop(cid, None))


def _apply(data: dict, c: dict, keep_pw: bool) -> None:
    c["name"] = (data.get("name") or c.get("name") or "").strip()
    c["host"] = (data.get("host") or "").strip()
    c["port"] = int(data["port"]) if data.get("port") else None
    c["username"] = (data.get("username") or "").strip()
    c["vendor"] = (data.get("vendor") or c.get("vendor") or "").strip()
    c["verify_tls"] = bool(data.get("verify_tls"))
    c["fingerprint"] = (data.get("fingerprint") or "").strip()
    c["insecure_http"] = bool(data.get("insecure_http"))
    c["client_id"] = (data.get("client_id") or "").strip() or None
    c["via"] = (data.get("via") or "").strip()
    if data.get("password"):
        c["password_enc"] = enc(data["password"])
    elif not keep_pw:
        c["password_enc"] = ""
    c["updated"] = int(time.time())
