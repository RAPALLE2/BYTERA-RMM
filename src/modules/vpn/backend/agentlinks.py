# agentlinks.py (Modul "vpn") - Netz-Zugang Backend <-> Agent
# ------------------------------------------------------------
# Ein "Agent-Link" macht das Netz HINTER einem Agenten fuer Dienste des
# Backends erreichbar - vor allem fuer den Server-Browser (Modul "browser"):
# interne Seiten (Router, NAS, Drucker, Intranet) laden direkt im Browser des
# Dashboards, als stuende der Server im Kunden-LAN.
#
# Technik: KEIN Kernel-WireGuard noetig. Jede Verbindung wird ueber die
# bestehende Agenten-Verbindung aufgebaut (app/agent_streams.py -> Agent
# "vpn-open"). Funktioniert damit auch im Docker-Container ohne NET_ADMIN und
# auf Agenten ohne Adminrechte. Hostnamen loest der Agent auf.
#
# Gespeichert in data/modules/vpn/agentlinks.json:
#   {"links": {lid: {id, name, client_id, networks: ["192.168.1.0/24", ...],
#                    domains: [".local", "nas.firma.lan", ...], mode: "split" | "all",
#                    created, created_by, expires (0 = unbegrenzt), shared (bool)}}}
#
#   mode "split": nur interne Ziele gehen ueber den Agenten (Netze aus
#                 "networks", Namen ohne Punkt und Endungen aus "domains"),
#                 alles andere direkt vom Server.
#   mode "all":   ALLES geht ueber den Agenten (auch Internet).
from __future__ import annotations

import ipaddress
import json
import secrets
import threading
import time

from app.paths import MODULES_DATA_DIR

FILE = MODULES_DATA_DIR / "vpn" / "agentlinks.json"
_lock = threading.RLock()
DEFAULT_DOMAINS = [".local", ".lan", ".intern", ".internal", ".home", ".fritz.box", ".localdomain"]


def _load() -> dict:
    try:
        d = json.loads(FILE.read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("links"), dict):
            return d
    except Exception:
        pass
    return {"links": {}}


def _save(d: dict) -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(FILE)


def all_links() -> list[dict]:
    with _lock:
        d = _load()
        now = time.time()
        return [l for l in d["links"].values() if not l.get("expires") or l["expires"] > now]


def get(lid: str) -> dict | None:
    with _lock:
        l = _load()["links"].get(lid)
    if l and l.get("expires") and l["expires"] < time.time():
        return None
    return l


def clean_networks(nets: list[str]) -> list[str]:
    out = []
    for n in nets or []:
        n = str(n).strip()
        if not n:
            continue
        try:
            out.append(str(ipaddress.ip_network(n, strict=False)))
        except ValueError:
            raise ValueError(f"Invalid network: {n}")
    return list(dict.fromkeys(out))[:32]


def clean_domains(doms: list[str]) -> list[str]:
    out = []
    for d in doms or []:
        d = str(d).strip().lower()
        if d and all(c.isalnum() or c in ".-_" for c in d):
            out.append(d)
    return list(dict.fromkeys(out))[:64]


def create(name: str, client_id: str, networks: list[str], domains: list[str], mode: str,
           expires: int, created_by: str, shared: bool) -> dict:
    lid = "al" + secrets.token_hex(5)
    rec = {"id": lid, "name": (name or "").strip()[:80] or "Agent network", "client_id": client_id,
           "networks": clean_networks(networks), "domains": clean_domains(domains),
           "mode": "all" if mode == "all" else "split", "created": int(time.time()),
           "created_by": created_by, "expires": int(expires or 0), "shared": bool(shared)}
    with _lock:
        d = _load()
        d["links"][lid] = rec
        _save(d)
    return rec


def update(lid: str, **fields) -> dict | None:
    with _lock:
        d = _load()
        rec = d["links"].get(lid)
        if not rec:
            return None
        for k, v in fields.items():
            if v is None:
                continue
            if k == "networks":
                v = clean_networks(v)
            elif k == "domains":
                v = clean_domains(v)
            elif k == "mode":
                v = "all" if v == "all" else "split"
            elif k == "name":
                v = str(v).strip()[:80] or rec["name"]
            rec[k] = v
        _save(d)
        return rec


def delete(lid: str) -> bool:
    with _lock:
        d = _load()
        gone = d["links"].pop(lid, None)
        _save(d)
    return bool(gone)


# ------------------------------------------------------------------ Rechte
def may_use(user: dict, link: dict) -> bool:
    """Darf dieser Benutzer den Link benutzen? Admins immer; sonst Zugriff auf
    den Client + Client-Recht c_vpn, und der Link ist eigener oder geteilt."""
    from app.auth import is_super_admin, user_has_permission, can_access_client
    if is_super_admin(user):
        return True
    cid = link["client_id"]
    if not (can_access_client(user, cid) and user_has_permission(user, "c_vpn", cid)):
        return False
    return link.get("shared") or link.get("created_by") == user.get("username")


def may_manage(user: dict, link: dict) -> bool:
    from app.auth import is_super_admin
    return is_super_admin(user) or link.get("created_by") == user.get("username")


def for_user(user: dict) -> list[dict]:
    return [l for l in all_links() if may_use(user, l)]


# ------------------------------------------------------------------ Routing (Split-Tunnel)
def via_agent(link: dict, host: str) -> bool:
    """Soll eine Verbindung zu host ueber den Agenten laufen?"""
    if link.get("mode") == "all":
        return True
    h = (host or "").strip().lower().rstrip(".")
    try:
        ip = ipaddress.ip_address(h.strip("[]"))
        return any(ip in ipaddress.ip_network(n, strict=False) for n in link.get("networks") or [])
    except ValueError:
        pass
    if "." not in h:                             # "nas", "intranet" -> nur intern aufloesbar
        return True
    for d in (link.get("domains") or []):
        if d.startswith("."):
            if h.endswith(d) or h == d[1:]:
                return True
        elif h == d or h.endswith("." + d):
            return True
    return False


def suggest_networks(client_id: str) -> list[str]:
    """Netze aus den Schnittstellen des Agenten (Live-Werte) vorschlagen."""
    out = []
    try:
        from app.sockets import state
        m = state.live_metrics.get(client_id) or {}
        for itf in m.get("interfaces") or []:
            # Agent meldet je Schnittstelle {"name", "mac", "ipv4"} (ohne Maske) -> /24 annehmen
            addr = str(itf.get("ipv4") or "").split("/")[0]
            try:
                ip = ipaddress.ip_address(addr)
            except ValueError:
                continue
            if ip.version != 4 or ip.is_loopback or ip.is_link_local or not ip.is_private:
                continue
            name = str(itf.get("name") or "").lower()
            if name.startswith(("docker", "br-", "veth", "virbr", "wg", "tun", "tap", "zt")):
                continue                              # virtuelle Netze nicht vorschlagen
            out.append(str(ipaddress.ip_network(f"{ip}/24", strict=False)))
    except Exception:
        pass
    if not out:
        try:
            from app import db
            ip = (db.get_client(client_id) or {}).get("ip") or ""
            if ip and ipaddress.ip_address(ip).is_private:
                out.append(str(ipaddress.ip_network(f"{ip}/24", strict=False)))
        except Exception:
            pass
    return list(dict.fromkeys(out))
