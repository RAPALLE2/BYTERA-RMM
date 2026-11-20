# store.py (module "tenant-link") - pairings that allow a main RMM to connect
# ----------------------------------------------------------------------------
# data/modules/tenant-link/data.json:
#   {"pairings": { id: {
#        id, name,            # label of the paired main RMM
#        parent_url,          # origin of the main RMM (for frame-ancestors)
#        key_hash,            # sha256 hex of the pair_key (we never store the key itself)
#        status,              # "pending" | "active" | "revoked"
#        mode,                # "code"  (admin issued a one-time code)
#                             # "request" (the main RMM asked, admin must approve)
#        code,                # one-time pairing code while pending (mode "code"); cleared after claim
#        code_expires,        # unix ts, 0 = no code
#        created, approved_at, last_seen, created_by
#   }}}
#
# This backend is a standalone RMM. "tenant-link" only records WHICH main RMMs
# were approved to embed/open it and keeps a revocable trust record per partner.
# The pair_key proves the identity of the paired main RMM on tenant-link calls;
# its sha256 hash is stored, the key itself is handed out exactly once.
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time

from app.paths import MODULES_DATA_DIR

DIR = MODULES_DATA_DIR / "tenant-link"
FILE = DIR / "data.json"
_lock = threading.RLock()

CODE_TTL = 30 * 60      # a freshly issued pairing code is valid for 30 minutes


def _load() -> dict:
    try:
        d = json.loads(FILE.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    d.setdefault("pairings", {})
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


# ------------------------------------------------------------------ helpers
def new_id() -> str:
    return secrets.token_hex(6)


def gen_code() -> str:
    return secrets.token_hex(4).upper()           # 8 hex chars, e.g. "A1B2C3D4"


def norm_code(code: str) -> str:
    return "".join(ch for ch in str(code or "") if ch.isalnum()).upper()


def gen_key() -> str:
    return secrets.token_urlsafe(32)


def key_hash(key: str) -> str:
    return hashlib.sha256((key or "").encode()).hexdigest()


def norm_origin(url: str) -> str:
    """Reduce a URL to scheme://host[:port] - that is what frame-ancestors needs."""
    u = (url or "").strip().rstrip("/")
    if not u:
        return ""
    if "://" not in u:
        u = "https://" + u
    try:
        from urllib.parse import urlsplit
        p = urlsplit(u)
        if not p.scheme or not p.netloc:
            return ""
        return f"{p.scheme}://{p.netloc}"
    except Exception:
        return ""


# ------------------------------------------------------------------ pairings
def list_pairings() -> list[dict]:
    return list(_load()["pairings"].values())


def get(pid: str) -> dict | None:
    return _load()["pairings"].get(pid)


def public(p: dict) -> dict:
    """A pairing without secrets - safe for the admin UI."""
    return {"id": p["id"], "name": p.get("name", ""), "parent_url": p.get("parent_url", ""),
            "status": p.get("status", "pending"), "mode": p.get("mode", ""),
            "has_code": bool(p.get("code") and (p.get("code_expires", 0) > time.time())),
            "code": p.get("code", "") if (p.get("code") and p.get("code_expires", 0) > time.time()) else "",
            "code_expires": p.get("code_expires", 0),
            "created": p.get("created", 0), "approved_at": p.get("approved_at", 0),
            "last_seen": p.get("last_seen", 0), "created_by": p.get("created_by", "")}


def create_code(name: str, created_by: str = "") -> dict:
    """Admin issues a one-time pairing code (pre-authorized, mode 'code')."""
    def upd(d):
        pid = new_id()
        p = {"id": pid, "name": (name or "").strip()[:80], "parent_url": "", "key_hash": "",
             "status": "pending", "mode": "code", "code": gen_code(),
             "code_expires": int(time.time()) + CODE_TTL,
             "created": int(time.time()), "approved_at": 0, "last_seen": 0,
             "created_by": created_by}
        d["pairings"][pid] = p
        return p
    return update(upd)


def claim_code(code: str, name: str, parent_url: str) -> dict | None:
    """A main RMM redeems a code -> pairing becomes active, returns {pairing, pair_key}."""
    want = norm_code(code)
    origin = norm_origin(parent_url)

    def upd(d):
        now = int(time.time())
        for p in d["pairings"].values():
            if p.get("mode") == "code" and p.get("status") == "pending" \
                    and norm_code(p.get("code", "")) == want and p.get("code_expires", 0) > now:
                key = gen_key()
                p.update(status="active", code="", code_expires=0, key_hash=key_hash(key),
                         parent_url=origin or p.get("parent_url", ""),
                         name=(name or "").strip()[:80] or p.get("name", ""),
                         approved_at=now, last_seen=now)
                return {"pairing": p, "pair_key": key}
        return None
    return update(upd)


def create_request(name: str, parent_url: str) -> dict:
    """A main RMM asks for a pairing -> pending, admin must approve. Returns {pairing, pair_key}."""
    origin = norm_origin(parent_url)

    def upd(d):
        now = int(time.time())
        pid = new_id()
        key = gen_key()
        p = {"id": pid, "name": (name or "").strip()[:80] or "Main RMM", "parent_url": origin,
             "key_hash": key_hash(key), "status": "pending", "mode": "request", "code": "",
             "code_expires": 0, "created": now, "approved_at": 0, "last_seen": now, "created_by": ""}
        d["pairings"][pid] = p
        return {"pairing": p, "pair_key": key}
    return update(upd)


def set_status(pid: str, status: str) -> dict | None:
    def upd(d):
        p = d["pairings"].get(pid)
        if not p:
            return None
        p["status"] = status
        if status == "active" and not p.get("approved_at"):
            p["approved_at"] = int(time.time())
        return p
    return update(upd)


def delete(pid: str) -> None:
    update(lambda d: d["pairings"].pop(pid, None))


def find_by_key(key: str) -> dict | None:
    if not key:
        return None
    h = key_hash(key)
    for p in _load()["pairings"].values():
        if p.get("key_hash") and p["key_hash"] == h:
            return p
    return None


def touch(pid: str) -> None:
    def upd(d):
        p = d["pairings"].get(pid)
        if p:
            p["last_seen"] = int(time.time())
    update(upd)


def active_origins() -> list[str]:
    """Parent origins of all active pairings - for frame-ancestors."""
    out = []
    for p in _load()["pairings"].values():
        if p.get("status") == "active" and p.get("parent_url"):
            o = norm_origin(p["parent_url"])
            if o and o not in out:
                out.append(o)
    return out
