# store.py (module "tenants") - connected tenant backends + credentials per user
# ------------------------------------------------------------------------------------
# data/modules/tenants/data.json:
#   {"tenants": { id: {
#        id, name, label, url (base URL of the tenant RMM), verify_tls,
#        allow: [ {type:"user"|"group", id} ]   # who MAY see/open this tenant
#        created, created_by, color, note
#   }},
#    "creds": { "<tenant_id>:<user_id>": {username, realm, pw_enc, saved_at} }}
#
# Each tenant is a standalone RMM backend (its own DB/settings/permissions/spaces).
# The main RMM stores ONLY: which backends exist, who may see them, and the
# personal credentials per user (password encrypted, Fernet from JWT_SECRET).
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time

from app.config import JWT_SECRET
from app.paths import MODULES_DATA_DIR

DIR = MODULES_DATA_DIR / "tenants"
FILE = DIR / "data.json"
_lock = threading.RLock()


def _fernet():
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("rmm-tenants:" + JWT_SECRET).encode()).digest()))


def enc(text: str) -> str:
    return _fernet().encrypt((text or "").encode()).decode() if text else ""


def dec(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except Exception:
        return ""


def _load() -> dict:
    try:
        d = json.loads(FILE.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    d.setdefault("tenants", {})
    d.setdefault("creds", {})
    return d


def _save(d: dict) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        import os
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(FILE)


def update(fn):
    with _lock:
        d = _load()
        res = fn(d)
        _save(d)
        return res


def new_id() -> str:
    return secrets.token_hex(6)


# ------------------------------------------------------------------ Tenants
def list_tenants() -> list[dict]:
    return list(_load()["tenants"].values())


def get(tid: str) -> dict | None:
    return _load()["tenants"].get(tid)


def public(t: dict) -> dict:
    """Tenant record without secrets - safe for the admin UI."""
    out = {k: v for k, v in t.items() if k != "pair_key_enc"}
    out["paired"] = bool(t.get("pair_key_enc"))
    out["pair_status"] = t.get("pair_status", "")
    return out


# ------------------------------------------------------------------ pairing (tenant-link)
def set_pair(tid: str, pair_key: str, parent_url: str, pair_name: str, status: str) -> dict | None:
    def upd(d):
        t = d["tenants"].get(tid)
        if not t:
            return None
        if pair_key:
            t["pair_key_enc"] = enc(pair_key)
        if parent_url:
            t["parent_url"] = parent_url
        if pair_name:
            t["pair_name"] = pair_name
        t["pair_status"] = status
        return t
    return update(upd)


def set_pair_status(tid: str, status: str) -> None:
    def upd(d):
        t = d["tenants"].get(tid)
        if t:
            t["pair_status"] = status
    update(upd)


def clear_pair(tid: str) -> None:
    def upd(d):
        t = d["tenants"].get(tid)
        if t:
            t.pop("pair_key_enc", None)
            t["pair_status"] = ""
    update(upd)


def pair_key(tid: str) -> str:
    t = get(tid)
    return dec(t.get("pair_key_enc", "")) if t else ""


# ------------------------------------------------------------------ per-user credentials
def _ckey(tid: str, uid: str) -> str:
    return f"{tid}:{uid}"


def get_cred(tid: str, uid: str) -> dict | None:
    return _load()["creds"].get(_ckey(tid, uid))


def set_cred(tid: str, uid: str, username: str, password: str, realm: str = "local") -> None:
    def upd(d):
        d["creds"][_ckey(tid, uid)] = {"username": username, "realm": realm or "local",
                                       "pw_enc": enc(password), "saved_at": int(time.time())}
    update(upd)


def clear_cred(tid: str, uid: str) -> None:
    update(lambda d: d["creds"].pop(_ckey(tid, uid), None))


def cred_password(tid: str, uid: str) -> str:
    c = get_cred(tid, uid)
    return dec(c["pw_enc"]) if c else ""


def has_cred(tid: str, uid: str) -> bool:
    c = get_cred(tid, uid)
    return bool(c and c.get("pw_enc"))


def purge_tenant_creds(tid: str) -> None:
    def upd(d):
        for k in [k for k in d["creds"] if k.startswith(tid + ":")]:
            d["creds"].pop(k, None)
    update(upd)
