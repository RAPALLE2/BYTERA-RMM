# match_routes.py (module "proxmox") - agents <-> Proxmox guests
# --------------------------------------------------------------
#   GET    /api/proxmox/guest-map                    mappings + open suggestions (that I may accept)
#   POST   /api/proxmox/scan                         {host?} match now (pve_manage on the host)
#   POST   /api/proxmox/suggestions/{cid}/accept     accept suggestion: mark as VM/LXC + map to the host
#   POST   /api/proxmox/suggestions/{cid}/ignore     dismiss suggestion (does not come back)
#   DELETE /api/proxmox/guest-map/{cid}              remove mapping
#   PUT    /api/proxmox/heads                        {host, node, head} set the head of a Proxmox node
#                                                    (applies to all guests of that node, existing too)
# accept optionally takes {head, remember}: parent agent (head) + remember for the node.
# Accepting requires "manage Proxmox" on the host AND "manage clients" on the guest (as in the dashboard).

from __future__ import annotations

import asyncio
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import db
from app import bgjobs
from app.auth import can_access_client, get_current_user, is_super_admin, user_has_permission

from . import match, pve_store

router = APIRouter(prefix="/api/proxmox", tags=["proxmox-match"])


def _can(user, perm, cid) -> bool:
    return is_super_admin(user) or (can_access_client(user, cid) and user_has_permission(user, perm, cid))


def _may_apply(user, s: dict, cid: str) -> bool:
    return _can(user, "pve_manage", s["host"]) and _can(user, "manage_clients", cid)


def _hostname(cid: str) -> str:
    return (db.get_client(cid) or {}).get("hostname") or cid


def _head_candidates(user, sug: list[dict]) -> list[dict]:
    """Agents eligible as head (Proxmox node): physical devices I can see."""
    skip = {s["client_id"] for s in sug}
    out = []
    for c in db.list_clients():
        if c["id"] in skip or (c.get("device_type") or "physical") != "physical":
            continue
        if not can_access_client(user, c["id"]):
            continue
        out.append({"id": c["id"], "hostname": c.get("hostname") or c["id"],
                    "connected": pve_store.is_connected(c["id"])})
    return sorted(out, key=lambda x: (not x["connected"], x["hostname"].lower()))


@router.get("/guest-map")
def guest_map(user: dict = Depends(get_current_user)):
    d = match.data()
    mp = {cid: {**m, "host_name": _hostname(m["host"]), "can_open": _can(user, "pve_access", m["host"])}
          for cid, m in d["map"].items() if can_access_client(user, cid)}
    sug = [{"client_id": cid, "client_name": _hostname(cid), **s, "host_name": _hostname(s["host"]),
            "head_name": _hostname(s["head"])}
           for cid, s in match.suggestions_with_heads().items() if db.get_client(cid) and _may_apply(user, s, cid)]
    heads = {h: {n: {"id": c, "name": _hostname(c)} for n, c in nodes.items()}
             for h, nodes in (d.get("heads") or {}).items() if _can(user, "pve_access", h)}
    return {"map": mp, "suggestions": sorted(sug, key=lambda x: (x["host_name"], x["node"], x["vmid"])),
            "heads": heads, "candidates": _head_candidates(user, sug) if sug else [],
            "scanned": {h: t for h, t in d["scanned"].items() if _can(user, "pve_access", h)}}


class HeadBody(BaseModel):
    host: str
    node: str
    head: str


@router.put("/heads")
def set_head(body: HeadBody, user: dict = Depends(get_current_user)):
    if not pve_store.is_connected(body.host) or not _can(user, "pve_manage", body.host):
        raise HTTPException(403, "You need \"manage Proxmox\" on the host for this")
    if not _can(user, "manage_clients", body.head):
        raise HTTPException(403, "No permission for the chosen head")
    try:
        moved = match.set_head(body.host, body.node, body.head)
    except KeyError:
        raise HTTPException(404, "Agent not found")
    db.add_audit_entry(user["username"], "proxmox.head_set", target=body.head,
                       details=f"Node {body.node} von {_hostname(body.host)} -> {_hostname(body.head)}, {moved} umgehaengt")
    return {"ok": True, "moved": moved}


class AcceptBody(BaseModel):
    head: str = ""
    remember: bool = True


class ScanBody(BaseModel):
    host: str = ""


@router.post("/scan")
async def scan(body: ScanBody, background: bool = False, user: dict = Depends(get_current_user)):
    """Als BACKEND-JOB (app/bgjobs.py): Neuladen/Schliessen bricht nichts ab,
    Progress + result in the task view. ?background=1 -> job immediately."""
    async def _work(job):
        job.update(phase='proxmox.match', detail="Proxmox: matching device mapping")
        return await _scan_impl(body=body, user=user)
    return await bgjobs.run('proxmox.match', 'scan', title="Proxmox: match device mapping", user=user, work=_work,
                            background=background, private=True)


async def _scan_impl(body: ScanBody, user: dict = Depends(get_current_user)):
    hosts = [body.host] if body.host else pve_store.connected_ids()
    hosts = [h for h in hosts if pve_store.is_connected(h) and _can(user, "pve_manage", h)]
    if not hosts:
        raise HTTPException(403 if body.host else 404, "Kein Proxmox-Host, den du abgleichen darfst")
    res = {}
    for h in hosts:
        res[_hostname(h)] = await match.scan(h)        # manual: runs even when auto_match is off
    db.add_audit_entry(user["username"], "proxmox.scan", details=", ".join(res))
    return {"results": res}


@router.post("/suggestions/{cid}/{act}")
def suggestion(cid: str, act: str, body: AcceptBody | None = None, user: dict = Depends(get_current_user)):
    s = match.data()["suggestions"].get(cid)
    if not s:
        raise HTTPException(404, "No suggestion for this device")
    if not _may_apply(user, s, cid):
        raise HTTPException(403, "You need \"manage Proxmox\" on the host and \"manage clients\" on the device")
    if act == "accept":
        head = (body.head if body else "") or None
        if head and not can_access_client(user, head):
            raise HTTPException(403, "No permission for the chosen head")
        try:
            entry = match.accept(cid, head, body.remember if body else True)
        except ValueError as e:
            raise HTTPException(400, str(e))
        db.add_audit_entry(user["username"], "proxmox.guest_linked", target=cid,
                           details=f"{entry['type']} {entry['vmid']} auf {_hostname(entry['host'])} "
                                   f"(Node {entry['node']}, Head {_hostname(entry['head'])})")
        return {"ok": True, "entry": entry, "client": db.get_client(cid)}
    if act == "ignore":
        match.ignore(cid)
        return {"ok": True}
    raise HTTPException(400, "Unbekannte Aktion")


@router.delete("/guest-map/{cid}")
def unlink(cid: str, user: dict = Depends(get_current_user)):
    if not _can(user, "manage_clients", cid):
        raise HTTPException(403, "Keine Berechtigung")
    match.unlink(cid)
    return {"ok": True}
