# power.py (module "proxmox") - start an agent's guest via Proxmox
# --------------------------------------------------------------------
# For other modules (e.g. "wol"): if an agent runs as a VM/CT in Proxmox and is
# mapped (guest_map), it can be powered on directly via the Proxmox API - without
# Wake-on-LAN, MAC or broadcast.
#   guest_of(client_id)          -> {host, node, type, vmid, name, host_name} | None
#   may_start(user, client_id)   -> bool  (permission pve_power on the host + guest visible)
#   start(user, client_id)       -> {ok, upid, ...}   (raises HTTPException)
from __future__ import annotations

from fastapi import HTTPException

from app import db

from . import match, pve_store


def guest_of(client_id: str) -> dict | None:
    m = match.mapping(client_id)
    if not m or not pve_store.is_connected(m.get("host") or ""):
        return None
    host = db.get_client(m["host"]) or {}
    return {**m, "host_name": host.get("hostname") or m["host"]}


def may_start(user: dict, client_id: str) -> bool:
    g = guest_of(client_id)
    if not g:
        return False
    from . import proxmox_routes as pr
    try:
        pr._need(user, g["host"], "pve_power")
    except HTTPException:
        return False
    return pr._guest_visible(user, g["host"], g["type"], g["vmid"])


def start(user: dict, client_id: str) -> dict:
    g = guest_of(client_id)
    if not g:
        raise HTTPException(409, "This device is not mapped to a Proxmox VM/CT.")
    from . import proxmox_routes as pr
    cl = pr._guest(user, g["host"], g["node"], g["type"], int(g["vmid"]), "pve_power")
    upid = pr._pve(cl.post, pr._gpath(g["node"], g["type"], int(g["vmid"])) + "/status/start")
    pr._audit(user, g["host"], "guest.start", f"{g['type']}/{g['vmid']} auf {g['node']} (Einschalten)")
    return {"ok": True, "upid": upid, "via": "proxmox",
            "detail": f"{'VM' if g['type'] == 'qemu' else 'CT'} {g['vmid']} auf {g['host_name']} (Node {g['node']}) gestartet"}
