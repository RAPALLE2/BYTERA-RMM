# pve_store.py (module "proxmox") - connections and guest shares
# -----------------------------------------------------------------
# Connection per agent (several agents = several Proxmox instances possible):
#   addr_mode  local   agent IP (reported by the agent) - the server reaches Proxmox directly
#              custom  custom address (IP/name), e.g. cluster IP or over VPN
#              relay   RMM relay: the connection runs through the agent (tunnel), Proxmox does
#                      NOT have to be reachable from the server. relay_host = target from the
#                      agent's point of view (usually 127.0.0.1 if the agent runs on the Proxmox host)
#   port       8006
#   auth       token    user@realm!tokenid + secret   (recommended, revocable in Proxmox)
#              password user@realm + password (ticket, renewed automatically)
#   verify_tls / fingerprint (SHA-256 of the certificate, for self-signed certificates)
#
# Secrets (token secret, password) are stored encrypted (Fernet) in
# data/modules/proxmox/connections.json.
#
# Guest shares ("specific VMs/LXCs"): per agent and user/group a list of
# VMIDs - in addition to the pve_vms_all / pve_lxc_all permissions.

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time

from app.config import JWT_SECRET
from app.paths import MODULES_DATA_DIR

DIR = MODULES_DATA_DIR / "proxmox"
CONN_FILE = DIR / "connections.json"
ACL_FILE = DIR / "guest_acl.json"
_lock = threading.RLock()
_cache: dict = {"mtime": None, "data": {}}


def _fernet():
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("rmm-proxmox:" + JWT_SECRET).encode()).digest()))


def _enc(s: str) -> str:
    return _fernet().encrypt(s.encode()).decode() if s else ""


def _dec(s: str) -> str:
    try:
        return _fernet().decrypt(s.encode()).decode() if s else ""
    except Exception:
        return ""


def _read(path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write(path, data: dict) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(path)


def _conns() -> dict:
    """With cache (queried on every pve_* permission check)."""
    try:
        mt = CONN_FILE.stat().st_mtime
    except FileNotFoundError:
        return {}
    if _cache["mtime"] != mt:
        _cache["data"], _cache["mtime"] = _read(CONN_FILE), mt
    return _cache["data"]


# ------------------------------------------------------------------ Verbindungen
def is_connected(client_id: str) -> bool:
    return client_id in _conns()


def connected_ids() -> list[str]:
    return list(_conns().keys())


def get(client_id: str, secrets: bool = False) -> dict | None:
    c = _conns().get(client_id)
    if not c:
        return None
    out = {k: v for k, v in c.items() if k not in ("secret_enc", "password_enc")}
    out["has_secret"] = bool(c.get("secret_enc"))
    out["has_password"] = bool(c.get("password_enc"))
    if secrets:
        out["secret"] = _dec(c.get("secret_enc", ""))
        out["password"] = _dec(c.get("password_enc", ""))
    return out


def save(client_id: str, cfg: dict, secret: str | None, password: str | None) -> dict:
    with _lock:
        data = _read(CONN_FILE)
        old = data.get(client_id, {})
        new = {k: cfg[k] for k in ("addr_mode", "host", "relay_host", "port", "auth", "user", "token_id",
                                   "verify_tls", "fingerprint", "label") if k in cfg}
        new["secret_enc"] = _enc(secret) if secret else old.get("secret_enc", "")
        new["password_enc"] = _enc(password) if password else old.get("password_enc", "")
        if new.get("auth") == "token":
            new["password_enc"] = ""
        else:
            new["secret_enc"] = ""
        new["updated"] = int(time.time())
        new["created"] = old.get("created") or new["updated"]
        data[client_id] = new
        _write(CONN_FILE, data)
        _cache["mtime"] = None
    return get(client_id)


def remove(client_id: str) -> bool:
    with _lock:
        data = _read(CONN_FILE)
        if data.pop(client_id, None) is None:
            return False
        _write(CONN_FILE, data)
        _cache["mtime"] = None
        acl = _read(ACL_FILE)
        if acl.pop(client_id, None) is not None:
            _write(ACL_FILE, acl)
    return True


# ------------------------------------------------------------------ Guest shares
def acl(client_id: str) -> dict:
    """{"user:<id>": [vmid, ...], "group:<id>": [...]}"""
    return _read(ACL_FILE).get(client_id, {})


def set_acl(client_id: str, subject: str, vmids: list[int]) -> None:
    with _lock:
        data = _read(ACL_FILE)
        node = data.setdefault(client_id, {})
        clean = sorted({int(v) for v in vmids if str(v).isdigit()})
        if clean:
            node[subject] = clean
        else:
            node.pop(subject, None)
        _write(ACL_FILE, data)


def allowed_vmids(client_id: str, user_id: str, group_ids: set[str]) -> set[int]:
    a = acl(client_id)
    out = set(a.get(f"user:{user_id}", []))
    for g in group_ids:
        out |= set(a.get(f"group:{g}", []))
    return out
