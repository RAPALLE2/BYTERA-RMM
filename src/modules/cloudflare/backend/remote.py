# remote.py (module "cloudflare") - client-app remote access over the tunnel
# ----------------------------------------------------------------------------
# The client app wants to open RDP/VNC/SSH to an agent, but no port is reachable and
# there is (yet) no VPN: the backend creates an "Agent via RMM server" route
# (permanent forward RMM -> agent in the network module + tunnel address), protected with
# an OWN service token per route. The client app connects with
#   cloudflared access tcp --hostname <address> --url 127.0.0.1:<port>  (+ service token)
# and starts the local program on 127.0.0.1:<port>.
#
# Lifetime per agent + protocol (like the relay in the Explorer):
#   "session"   until end of use: gone as soon as no client app is connected anymore (lease)
#   10|30|60|120 minutes after opening (does not extend; ongoing use stays)
#   0           permanent (permission relay_unlimited or c_relay_unlimited)
# Leases: every connected client app checks in every 60 s (ping); after 3 minutes without
# a check-in the use is considered ended.

from __future__ import annotations

import re
import secrets
import threading
import time

from app import db

from . import store, sync

LEASE_TIMEOUT = 180
SESSION_GRACE = 90           # "until end of use": how long it stays open after the last use
_lock = threading.Lock()


def ready() -> dict:
    """Is remote access via Cloudflare possible? -> {ok, reason, zone}"""
    data = store.load()
    if not (data.get("account") or {}).get("id"):
        return {"ok": False, "reason": "Cloudflare access is not set up"}
    zone = (data.get("remote") or {}).get("zone") or (data.get("zones") or [{}])[0].get("name")
    if not zone:
        return {"ok": False, "reason": "No zone selected for remote access"}
    return {"ok": True, "reason": "", "zone": zone}


def _rid(client_id: str, proto: str) -> str:
    return "rx-" + re.sub(r"[^a-z0-9]", "", client_id.lower())[:12] + "-" + proto


def _hostname(client_id: str, proto: str, zone: str) -> str:
    short = re.sub(r"[^a-z0-9]", "", client_id.lower())[:10] or secrets.token_hex(4)
    return f"rmm-{proto}-{short}.{zone}"


def open_tunnel(user: dict, client_id: str, proto: str, port: int, target_host: str, lifetime) -> dict:
    """Create/reuse a route, assign a lease. -> {hostname, lease, token_id, token_secret, expires, lifetime}"""
    rd = ready()
    if not rd["ok"]:
        raise ValueError(rd["reason"])
    life = "session" if lifetime in (None, "", "session") else int(lifetime)
    c = db.get_client(client_id) or {}
    rid = _rid(client_id, proto)
    now = time.time()
    lease = secrets.token_urlsafe(18)
    with _lock:
        def upd(d):
            r = store.route(d, rid)
            ref = {"client_id": client_id, "proto": proto, "port": int(port)}
            if target_host and target_host not in ("127.0.0.1", "localhost"):
                ref.update(host_mode="custom", host=target_host)
            if not r:
                zone = (d.get("remote") or {}).get("zone") or rd["zone"]
                r = {"id": rid, "created": int(now), "created_by": user["username"], "state": {},
                     "name": f"Client-App: {c.get('hostname') or client_id[:8]} {proto.upper()}",
                     "kind": "agent", "endpoint": "server", "node_id": "", "mode": "client",
                     "hostname": _hostname(client_id, proto, zone), "access": "service", "allow": [], "emails": [],
                     "remote": {"lifetime": life, "expires": 0, "leases": {}, "last_used": now}}
                d["routes"].append(r)
            rm = r.setdefault("remote", {"leases": {}})
            if r.get("ref") != ref:
                r["ref"] = ref
            # Lifetime: the longer one wins (permanent > minutes > end of use)
            cur = rm.get("lifetime", "session")
            rank = lambda v: 10**9 if v == 0 else (-1 if v == "session" else int(v))   # noqa: E731
            if rank(life) >= rank(cur) or rm.get("expires", 0) and rm["expires"] < now:
                rm["lifetime"] = life
                rm["expires"] = int(now + life * 60) if isinstance(life, int) and life > 0 else 0
            rm.setdefault("leases", {})[lease] = {"user": user["username"], "ping": now}
            rm["last_used"] = now
            return r
        store.update(upd)
        rep = sync.apply(user["username"])
    data = store.load()
    r = store.route(data, rid)
    err = (r.get("state") or {}).get("error")
    if err:
        raise ValueError(f"Tunnel konnte nicht eingerichtet werden: {err}")
    stk = r.get("service_token") or {}
    if not stk.get("id"):
        raise ValueError("Service-Token fehlt – " + "; ".join(rep.get("errors") or []))
    return {"hostname": r["hostname"], "lease": lease, "route_id": rid, "token_id": stk["client_id"],
            "token_secret": store.dec(stk["secret_enc"]), "lifetime": r["remote"]["lifetime"],
            "expires": r["remote"].get("expires") or 0, "service": r["state"].get("service")}


def _find_lease(data: dict, lease: str):
    for r in data["routes"]:
        if lease in ((r.get("remote") or {}).get("leases") or {}):
            return r
    return None


def ping(lease: str, user: dict) -> dict:
    def upd(d):
        r = _find_lease(d, lease)
        if not r:
            return None
        L = r["remote"]["leases"][lease]
        if L["user"] != user["username"]:
            return None
        L["ping"] = r["remote"]["last_used"] = time.time()
        return {"ok": True, "expires": r["remote"].get("expires") or 0}
    res = store.update(upd)
    if not res:
        raise LookupError("Connection not (no longer) known")
    return res


def release(lease: str, user: dict) -> None:
    def upd(d):
        r = _find_lease(d, lease)
        if r and r["remote"]["leases"][lease]["user"] == user["username"]:
            r["remote"]["leases"].pop(lease, None)
            r["remote"]["last_used"] = time.time()
    store.update(upd)


def set_lifetime(rid: str, lifetime) -> dict:
    life = "session" if lifetime in (None, "", "session") else int(lifetime)

    def upd(d):
        r = store.route(d, rid)
        if not r or "remote" not in r:
            raise LookupError("Remote access not found")
        r["remote"]["lifetime"] = life
        r["remote"]["expires"] = int(time.time() + life * 60) if isinstance(life, int) and life > 0 else 0
        return r["remote"]
    return store.update(upd)


def due() -> list[str]:
    """Routes whose time has expired (without ongoing use)."""
    now = time.time()
    out = []

    def upd(d):
        for r in d["routes"]:
            rm = r.get("remote")
            if not rm:
                continue
            leases = rm.get("leases") or {}
            for k in [k for k, v in leases.items() if now - v.get("ping", 0) > LEASE_TIMEOUT]:
                leases.pop(k)
            active = bool(leases)
            life = rm.get("lifetime", "session")
            if active or life == 0:
                continue
            if life == "session" and now - rm.get("last_used", 0) > SESSION_GRACE:
                out.append(r["id"])
            elif isinstance(life, int) and life > 0 and rm.get("expires") and rm["expires"] < now:
                out.append(r["id"])
    store.update(upd)
    return out


def cleanup() -> int:
    ids = due()
    for rid in ids:
        sync.remove_route(rid)
    if ids:
        try:
            sync.apply("")
        except Exception:
            pass
    return len(ids)


def list_for(client_id: str) -> list[dict]:
    out = []
    for r in store.load()["routes"]:
        rm = r.get("remote")
        if rm and (r.get("ref") or {}).get("client_id") == client_id:
            out.append({"route_id": r["id"], "proto": r["ref"].get("proto"), "hostname": r["hostname"],
                        "lifetime": rm.get("lifetime"), "expires": rm.get("expires") or 0,
                        "active": len(rm.get("leases") or {}), "error": (r.get("state") or {}).get("error", "")})
    return out
