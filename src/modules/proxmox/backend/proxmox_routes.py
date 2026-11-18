# proxmox_routes.py (module "proxmox") - Proxmox control per agent
# -------------------------------------------------------------------
# All routes apply to ONE agent ({cid}) with a configured Proxmox connection.
# Permissions (client permissions, also grantable via "standard Proxmox" pve:*):
#   pve_access   dashboard/overview          pve_vms_all / pve_lxc_all  all VMs / LXCs
#   pve_power    start/stop/...               pve_console  console
#   pve_snapshot snapshots                    pve_backup   backups
#   pve_config   change configuration         pve_storage  storage
#   pve_users    Proxmox users/ACL            pve_nodes    nodes/tasks
#   pve_api      arbitrary API calls          pve_manage   connection + guest shares
# "Specific VMs/LXCs": guest shares per user/group (GET/PUT /{cid}/acl).
#
#   GET    /api/proxmox/clients                         agents with Proxmox (visible to me)
#   GET    /api/proxmox/{cid}/connection   PUT  DELETE  connection (pve_manage)
#   POST   /api/proxmox/{cid}/test                      test connection (pve_manage)
#   POST   /api/proxmox/{cid}/fingerprint               fetch certificate fingerprint (pve_manage)
#   GET    /api/proxmox/{cid}/overview                  version, nodes, counters
#   GET    /api/proxmox/{cid}/guests?type=qemu|lxc      guests (filtered by permissions/shares)
#   GET    /api/proxmox/{cid}/guests/{node}/{type}/{vmid}           status + configuration
#   POST   .../action/{start|stop|shutdown|reboot|suspend|resume|reset}
#   PUT    .../config                                   change configuration
#   GET POST .../snapshots   POST .../snapshots/{name}/rollback   DELETE .../snapshots/{name}
#   GET POST .../backups                                vzdump backups
#   GET    /api/proxmox/{cid}/storage   .../storage/{node}/{storage}/content
#   GET    /api/proxmox/{cid}/users     .../nodes     .../tasks     .../tasks/{node}/{upid}
#   GET PUT /api/proxmox/{cid}/acl                      guest shares
#   *      /api/proxmox/{cid}/api/{path}                API explorer (pve_api)

from __future__ import annotations

import re

import asyncio
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app import db
from app import bgjobs
from app.auth import can_access_client, get_current_user, is_super_admin, require_perm, user_has_permission

from . import pve_client, pve_store
from .pve_client import PveError

router = APIRouter(prefix="/api/proxmox", tags=["proxmox"])

_NODE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_STORE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SNAP = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,39}$")
ACTIONS = {"start", "stop", "shutdown", "reboot", "suspend", "resume", "reset"}


# ------------------------------------------------------------------ Hilfen
def _pve(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except PveError as e:
        # Do not pass PROXMOX 401/403 through: the dashboard would read 401 as "RMM session
        # expired" (logout!) and 403 as a missing RMM permission.
        if e.status == 401:
            raise HTTPException(502, f"Proxmox rejected the login - check user/token/password ({e})")
        if e.status == 403:
            raise HTTPException(502, f"The Proxmox user lacks permissions in Proxmox ({e})")
        raise HTTPException(e.status if 400 <= e.status < 600 else 502, str(e))


def _need(user, cid: str, perm: str) -> None:
    if not pve_store.is_connected(cid):
        raise HTTPException(404, "No Proxmox connected for this agent")
    if not can_access_client(user, cid):
        raise HTTPException(404, "Device not found")
    require_perm(user, perm, cid)


def _check_ids(node: str, gtype: str, vmid: int) -> None:
    if not _NODE.match(node) or gtype not in ("qemu", "lxc") or not (100 <= int(vmid) <= 999999999):
        raise HTTPException(400, "Invalid guest specification")


def _groups(user) -> set[str]:
    try:
        return set(db.get_user_group_ids(user["id"]))
    except Exception:
        return set()


def _guest_visible(user, cid: str, gtype: str, vmid: int, acl_ids: set[int] | None = None) -> bool:
    if is_super_admin(user):
        return True
    if user_has_permission(user, "pve_vms_all" if gtype == "qemu" else "pve_lxc_all", cid):
        return True
    if acl_ids is None:
        acl_ids = pve_store.allowed_vmids(cid, user["id"], _groups(user))
    return int(vmid) in acl_ids


def _guest(user, cid: str, node: str, gtype: str, vmid: int, perm: str = "pve_access") -> pve_client.PveClient:
    _need(user, cid, perm)
    _check_ids(node, gtype, vmid)
    if not _guest_visible(user, cid, gtype, vmid):
        raise HTTPException(404, "Guest not found or not shared")
    return _pve(pve_client.client_for, cid)


def _gpath(node, gtype, vmid) -> str:
    return f"/nodes/{node}/{gtype}/{int(vmid)}"


def _audit(user, cid, action, detail=""):
    db.add_audit_entry(user["username"], f"proxmox.{action}", target=cid, details=detail[:300])


# ------------------------------------------------------------------ Agents
@router.get("/clients")
def pve_clients(user: dict = Depends(get_current_user)):
    out = []
    for cid in pve_store.connected_ids():
        if not can_access_client(user, cid) or not user_has_permission(user, "pve_access", cid):
            continue
        c = db.get_client(cid) or {}
        cfg = pve_store.get(cid) or {}
        out.append({"client_id": cid, "hostname": c.get("hostname"), "label": cfg.get("label") or "",
                    "addr_mode": cfg.get("addr_mode"), "manage": user_has_permission(user, "pve_manage", cid)})
    return {"clients": sorted(out, key=lambda x: (x["hostname"] or "").lower())}


# ------------------------------------------------------------------ Connection
class ConnBody(BaseModel):
    addr_mode: str = "local"          # local | custom | relay
    host: str = ""
    relay_host: str = "127.0.0.1"
    port: int = 8006
    auth: str = "token"               # token | password
    user: str = ""                    # z.B. rmm@pve
    token_id: str = ""                # z.B. rmm
    secret: str = ""                  # leer = gespeichertes behalten
    password: str = ""                # leer = gespeichertes behalten
    verify_tls: bool = False
    fingerprint: str = ""
    label: str = ""


def _conn_check(user, cid):
    if not can_access_client(user, cid):
        raise HTTPException(404, "Device not found")
    require_perm(user, "pve_manage", cid)


@router.get("/{cid}/connection")
def get_connection(cid: str, user: dict = Depends(get_current_user)):
    _conn_check(user, cid)
    c = db.get_client(cid) or {}
    return {"connection": pve_store.get(cid), "agent_ip": c.get("ip") or "", "hostname": c.get("hostname")}


@router.put("/{cid}/connection")
def put_connection(cid: str, body: ConnBody, user: dict = Depends(get_current_user)):
    _conn_check(user, cid)
    if body.addr_mode not in ("local", "custom", "relay") or body.auth not in ("token", "password"):
        raise HTTPException(400, "Invalid address or login type")
    if not 1 <= body.port <= 65535:
        raise HTTPException(400, "Invalid port")
    if body.addr_mode == "custom" and not re.match(r"^[A-Za-z0-9.\-:\[\]]{1,253}$", body.host.strip()):
        raise HTTPException(400, "Please enter a valid address")
    if not re.match(r"^[^@\s]+@[A-Za-z0-9._-]+$", body.user.strip()):
        raise HTTPException(400, "Please specify the user as name@realm (e.g. rmm@pve)")
    old = pve_store.get(cid) or {}
    if body.auth == "token":
        if not re.match(r"^[A-Za-z][A-Za-z0-9._-]{1,63}$", body.token_id.strip()):
            raise HTTPException(400, "Invalid token ID (only the name after the !, e.g. rmm)")
        if not body.secret and not old.get("has_secret"):
            raise HTTPException(400, "Token-Secret fehlt")
    elif not body.password and not old.get("has_password"):
        raise HTTPException(400, "Passwort fehlt")
    cfg = body.model_dump(exclude={"secret", "password"})
    cfg["host"], cfg["user"], cfg["token_id"] = body.host.strip(), body.user.strip(), body.token_id.strip()
    saved = pve_store.save(cid, cfg, body.secret or None, body.password or None)
    pve_client.forget(cid)
    _audit(user, cid, "connection_saved", f"{body.addr_mode} {body.auth} {body.user}")
    return {"connection": saved}


@router.delete("/{cid}/connection")
def delete_connection(cid: str, user: dict = Depends(get_current_user)):
    _conn_check(user, cid)
    pve_store.remove(cid)
    pve_client.forget(cid)
    from . import match
    match.forget_host(cid)                       # Zuordnungen/Vorschlaege dieses Hosts entfernen
    _audit(user, cid, "connection_removed")
    return {"ok": True}


@router.post("/{cid}/test")
async def test_connection(cid: str, background: bool = False, user: dict = Depends(get_current_user)):
    """Als BACKEND-JOB (app/bgjobs.py): Neuladen/Schliessen bricht nichts ab,
    Fortschritt + Ergebnis in der Aufgaben-Anzeige. ?background=1 -> Job sofort."""
    async def _work(job):
        job.update(phase='proxmox.task', detail="Proxmox: Verbindung testen")
        return await asyncio.to_thread(_test_connection_impl, cid=cid, user=user)
    return await bgjobs.run('proxmox.task', cid, title="Proxmox: Verbindung testen", user=user, work=_work,
                            background=background, client_id=cid if db.get_client(cid) else None)


def _test_connection_impl(cid: str, user: dict = Depends(get_current_user)):
    _conn_check(user, cid)
    if not pve_store.is_connected(cid):
        raise HTTPException(404, "Bitte zuerst die Verbindung speichern")
    return _pve(pve_client.test, cid)


@router.post("/{cid}/fingerprint")
def fingerprint(cid: str, user: dict = Depends(get_current_user)):
    """Fetch the Proxmox certificate fingerprint over the configured address."""
    _conn_check(user, cid)
    cl = _pve(pve_client.client_for, cid, fresh=True)
    from urllib.parse import urlparse
    u = urlparse(_pve(cl.base))
    try:
        return {"fingerprint": pve_client.fetch_fingerprint(u.hostname, u.port or 8006)}
    except OSError as e:
        raise HTTPException(502, f"Certificate could not be fetched: {e}")


# ------------------------------------------------------------------ Overview & guests
def _resources(cl):
    return _pve(cl.get, "/cluster/resources") or []


@router.get("/{cid}/overview")
def overview(cid: str, user: dict = Depends(get_current_user)):
    _need(user, cid, "pve_access")
    cl = _pve(pve_client.client_for, cid)
    res = _resources(cl)
    acl_ids = pve_store.allowed_vmids(cid, user["id"], _groups(user))
    guests = [r for r in res if r.get("type") in ("qemu", "lxc") and _guest_visible(user, cid, r["type"], r.get("vmid", 0), acl_ids)]
    nodes = [{k: r.get(k) for k in ("node", "status", "cpu", "maxcpu", "mem", "maxmem", "disk", "maxdisk", "uptime")}
             for r in res if r.get("type") == "node"]
    storage = []
    if user_has_permission(user, "pve_storage", cid):
        storage = [{k: r.get(k) for k in ("storage", "node", "status", "disk", "maxdisk", "plugintype", "content", "shared")}
                   for r in res if r.get("type") == "storage"]
    ver = _pve(cl.get, "/version") or {}
    cnt = lambda t, st=None: sum(1 for g in guests if g["type"] == t and (st is None or g.get("status") == st))   # noqa: E731
    return {"version": ver.get("version"), "nodes": nodes, "storage": storage,
            "counts": {"vms": cnt("qemu"), "vms_running": cnt("qemu", "running"),
                       "lxc": cnt("lxc"), "lxc_running": cnt("lxc", "running")},
            "perms": {p: user_has_permission(user, p, cid) for p in
                      ("pve_power", "pve_console", "pve_snapshot", "pve_backup", "pve_config", "pve_storage",
                       "pve_users", "pve_nodes", "pve_api", "pve_manage", "pve_vms_all", "pve_lxc_all")}}


@router.get("/{cid}/guests")
def guests(cid: str, type: str = "", user: dict = Depends(get_current_user)):
    _need(user, cid, "pve_access")
    cl = _pve(pve_client.client_for, cid)
    acl_ids = pve_store.allowed_vmids(cid, user["id"], _groups(user))
    out = []
    for r in _resources(cl):
        t = r.get("type")
        if t not in ("qemu", "lxc") or (type and t != type):
            continue
        if not _guest_visible(user, cid, t, r.get("vmid", 0), acl_ids):
            continue
        out.append({k: r.get(k) for k in ("vmid", "name", "type", "node", "status", "cpu", "maxcpu", "mem", "maxmem",
                                          "disk", "maxdisk", "uptime", "template", "tags", "lock", "netin", "netout")})
    return {"guests": sorted(out, key=lambda g: g.get("vmid") or 0)}


@router.get("/{cid}/guests/{node}/{gtype}/{vmid}")
def guest_detail(cid: str, node: str, gtype: str, vmid: int, user: dict = Depends(get_current_user)):
    cl = _guest(user, cid, node, gtype, vmid)
    p = _gpath(node, gtype, vmid)
    status = _pve(cl.get, p + "/status/current") or {}
    config = _pve(cl.get, p + "/config") or {}
    return {"status": status, "config": config}


@router.post("/{cid}/guests/{node}/{gtype}/{vmid}/action/{action}")
async def guest_action(cid: str, node: str, gtype: str, vmid: int, action: str, background: bool = False, user: dict = Depends(get_current_user)):
    """Als BACKEND-JOB (app/bgjobs.py): Neuladen/Schliessen bricht nichts ab,
    Fortschritt + Ergebnis in der Aufgaben-Anzeige. ?background=1 -> Job sofort."""
    async def _work(job):
        job.update(phase='proxmox.task', detail=f"Proxmox {gtype}/{vmid}: {action}")
        return await _follow_task(job, cid, node, await asyncio.to_thread(_guest_action_impl, cid=cid, node=node, gtype=gtype, vmid=vmid, action=action, user=user))
    return await bgjobs.run('proxmox.task', f'{cid}:{vmid}', title=f"Proxmox {gtype}/{vmid}: {action}", user=user, work=_work,
                            background=background, client_id=cid if db.get_client(cid) else None)


def _guest_action_impl(cid: str, node: str, gtype: str, vmid: int, action: str, user: dict = Depends(get_current_user)):
    if action not in ACTIONS or (action == "reset" and gtype != "qemu"):
        raise HTTPException(400, "Unbekannte Aktion")
    cl = _guest(user, cid, node, gtype, vmid, "pve_power")
    upid = _pve(cl.post, _gpath(node, gtype, vmid) + f"/status/{action}")
    _audit(user, cid, f"guest.{action}", f"{gtype}/{vmid} auf {node}")
    return {"ok": True, "upid": upid}


class ConfigBody(BaseModel):
    params: dict
    delete: list[str] = []


@router.put("/{cid}/guests/{node}/{gtype}/{vmid}/config")
async def guest_config(cid: str, node: str, gtype: str, vmid: int, body: ConfigBody, background: bool = False, user: dict = Depends(get_current_user)):
    """Als BACKEND-JOB (app/bgjobs.py): Neuladen/Schliessen bricht nichts ab,
    Fortschritt + Ergebnis in der Aufgaben-Anzeige. ?background=1 -> Job sofort."""
    async def _work(job):
        job.update(phase='proxmox.task', detail=f"Proxmox {gtype}/{vmid}: change configuration")
        return await asyncio.to_thread(_guest_config_impl, cid=cid, node=node, gtype=gtype, vmid=vmid, body=body, user=user)
    return await bgjobs.run('proxmox.task', None, title=f"Proxmox {gtype}/{vmid}: change configuration", user=user, work=_work,
                            background=background, client_id=cid if db.get_client(cid) else None)


def _guest_config_impl(cid: str, node: str, gtype: str, vmid: int, body: ConfigBody, user: dict = Depends(get_current_user)):
    cl = _guest(user, cid, node, gtype, vmid, "pve_config")
    params = {k: v for k, v in body.params.items() if re.match(r"^[a-z][a-z0-9_]*$", str(k))}
    if body.delete:
        params["delete"] = ",".join(d for d in body.delete if re.match(r"^[a-z][a-z0-9_]*$", d))
    res = _pve(cl.call, "PUT", _gpath(node, gtype, vmid) + "/config", params)
    _audit(user, cid, "guest.config", f"{gtype}/{vmid}: {', '.join(params)}")
    return {"ok": True, "result": res}


# --- Snapshots ---------------------------------------------------------
class SnapBody(BaseModel):
    name: str
    description: str = ""
    vmstate: bool = False


@router.get("/{cid}/guests/{node}/{gtype}/{vmid}/snapshots")
def snapshots(cid: str, node: str, gtype: str, vmid: int, user: dict = Depends(get_current_user)):
    cl = _guest(user, cid, node, gtype, vmid, "pve_snapshot")
    return {"snapshots": _pve(cl.get, _gpath(node, gtype, vmid) + "/snapshot") or []}


@router.post("/{cid}/guests/{node}/{gtype}/{vmid}/snapshots")
async def snapshot_create(cid: str, node: str, gtype: str, vmid: int, body: SnapBody, background: bool = False, user: dict = Depends(get_current_user)):
    """Als BACKEND-JOB (app/bgjobs.py): Neuladen/Schliessen bricht nichts ab,
    Fortschritt + Ergebnis in der Aufgaben-Anzeige. ?background=1 -> Job sofort."""
    async def _work(job):
        job.update(phase='proxmox.task', detail=f"Proxmox {gtype}/{vmid}: Snapshot „{body.name}“ anlegen")
        return await _follow_task(job, cid, node, await asyncio.to_thread(_snapshot_create_impl, cid=cid, node=node, gtype=gtype, vmid=vmid, body=body, user=user))
    return await bgjobs.run('proxmox.task', f'{cid}:{vmid}', title=f"Proxmox {gtype}/{vmid}: Snapshot „{body.name}“ anlegen", user=user, work=_work,
                            background=background, client_id=cid if db.get_client(cid) else None)


def _snapshot_create_impl(cid: str, node: str, gtype: str, vmid: int, body: SnapBody, user: dict = Depends(get_current_user)):
    if not _SNAP.match(body.name):
        raise HTTPException(400, "Snapshot-Name: Buchstabe am Anfang, danach Buchstaben/Ziffern/_/- (max. 40)")
    cl = _guest(user, cid, node, gtype, vmid, "pve_snapshot")
    params = {"snapname": body.name, "description": body.description}
    if gtype == "qemu" and body.vmstate:
        params["vmstate"] = 1
    upid = _pve(cl.call, "POST", _gpath(node, gtype, vmid) + "/snapshot", params)
    _audit(user, cid, "snapshot.create", f"{gtype}/{vmid} {body.name}")
    return {"ok": True, "upid": upid}


@router.post("/{cid}/guests/{node}/{gtype}/{vmid}/snapshots/{name}/rollback")
async def snapshot_rollback(cid: str, node: str, gtype: str, vmid: int, name: str, background: bool = False, user: dict = Depends(get_current_user)):
    """Als BACKEND-JOB (app/bgjobs.py): Neuladen/Schliessen bricht nichts ab,
    Fortschritt + Ergebnis in der Aufgaben-Anzeige. ?background=1 -> Job sofort."""
    async def _work(job):
        job.update(phase='proxmox.task', detail=f"Proxmox {gtype}/{vmid}: roll back snapshot '{name}'")
        return await _follow_task(job, cid, node, await asyncio.to_thread(_snapshot_rollback_impl, cid=cid, node=node, gtype=gtype, vmid=vmid, name=name, user=user))
    return await bgjobs.run('proxmox.task', f'{cid}:{vmid}', title=f"Proxmox {gtype}/{vmid}: roll back snapshot '{name}'", user=user, work=_work,
                            background=background, client_id=cid if db.get_client(cid) else None)


def _snapshot_rollback_impl(cid: str, node: str, gtype: str, vmid: int, name: str, user: dict = Depends(get_current_user)):
    if not _SNAP.match(name):
        raise HTTPException(400, "Invalid snapshot name")
    cl = _guest(user, cid, node, gtype, vmid, "pve_snapshot")
    upid = _pve(cl.post, _gpath(node, gtype, vmid) + f"/snapshot/{name}/rollback")
    _audit(user, cid, "snapshot.rollback", f"{gtype}/{vmid} {name}")
    return {"ok": True, "upid": upid}


@router.delete("/{cid}/guests/{node}/{gtype}/{vmid}/snapshots/{name}")
async def snapshot_delete(cid: str, node: str, gtype: str, vmid: int, name: str, background: bool = False, user: dict = Depends(get_current_user)):
    """Als BACKEND-JOB (app/bgjobs.py): Neuladen/Schliessen bricht nichts ab,
    Fortschritt + Ergebnis in der Aufgaben-Anzeige. ?background=1 -> Job sofort."""
    async def _work(job):
        job.update(phase='proxmox.task', detail=f"Proxmox {gtype}/{vmid}: delete snapshot '{name}'")
        return await _follow_task(job, cid, node, await asyncio.to_thread(_snapshot_delete_impl, cid=cid, node=node, gtype=gtype, vmid=vmid, name=name, user=user))
    return await bgjobs.run('proxmox.task', f'{cid}:{vmid}', title=f"Proxmox {gtype}/{vmid}: delete snapshot '{name}'", user=user, work=_work,
                            background=background, client_id=cid if db.get_client(cid) else None)


def _snapshot_delete_impl(cid: str, node: str, gtype: str, vmid: int, name: str, user: dict = Depends(get_current_user)):
    if not _SNAP.match(name):
        raise HTTPException(400, "Invalid snapshot name")
    cl = _guest(user, cid, node, gtype, vmid, "pve_snapshot")
    upid = _pve(cl.call, "DELETE", _gpath(node, gtype, vmid) + f"/snapshot/{name}")
    _audit(user, cid, "snapshot.delete", f"{gtype}/{vmid} {name}")
    return {"ok": True, "upid": upid}


# --- Backups -------------------------------------------------------------
class BackupBody(BaseModel):
    storage: str
    mode: str = "snapshot"            # snapshot | suspend | stop
    compress: str = "zstd"            # zstd | lzo | gzip | 0
    notes: str = ""


@router.get("/{cid}/guests/{node}/{gtype}/{vmid}/backups")
def backups(cid: str, node: str, gtype: str, vmid: int, user: dict = Depends(get_current_user)):
    cl = _guest(user, cid, node, gtype, vmid, "pve_backup")
    stores = [s for s in (_pve(cl.get, f"/nodes/{node}/storage", content="backup") or []) if s.get("active", 1)]
    out = []
    for s in stores:
        try:
            for item in cl.get(f"/nodes/{node}/storage/{s['storage']}/content", content="backup", vmid=int(vmid)) or []:
                out.append({**item, "storage": s["storage"]})
        except PveError:
            continue
    return {"backups": sorted(out, key=lambda b: b.get("ctime") or 0, reverse=True),
            "storages": [s["storage"] for s in stores]}


@router.post("/{cid}/guests/{node}/{gtype}/{vmid}/backups")
async def backup_create(cid: str, node: str, gtype: str, vmid: int, body: BackupBody, background: bool = False, user: dict = Depends(get_current_user)):
    """Als BACKEND-JOB (app/bgjobs.py): Neuladen/Schliessen bricht nichts ab,
    Fortschritt + Ergebnis in der Aufgaben-Anzeige. ?background=1 -> Job sofort."""
    async def _work(job):
        job.update(phase='proxmox.task', detail=f"Proxmox {gtype}/{vmid}: Backup nach {body.storage}")
        return await _follow_task(job, cid, node, await asyncio.to_thread(_backup_create_impl, cid=cid, node=node, gtype=gtype, vmid=vmid, body=body, user=user))
    return await bgjobs.run('proxmox.task', f'{cid}:{vmid}', title=f"Proxmox {gtype}/{vmid}: Backup nach {body.storage}", user=user, work=_work,
                            background=background, client_id=cid if db.get_client(cid) else None)


def _backup_create_impl(cid: str, node: str, gtype: str, vmid: int, body: BackupBody, user: dict = Depends(get_current_user)):
    if not _STORE.match(body.storage) or body.mode not in ("snapshot", "suspend", "stop") \
            or body.compress not in ("zstd", "lzo", "gzip", "0"):
        raise HTTPException(400, "Invalid backup parameters")
    cl = _guest(user, cid, node, gtype, vmid, "pve_backup")
    params = {"vmid": int(vmid), "storage": body.storage, "mode": body.mode, "compress": body.compress}
    if body.notes:
        params["notes-template"] = body.notes[:200]
    upid = _pve(cl.call, "POST", f"/nodes/{node}/vzdump", params)
    _audit(user, cid, "backup.create", f"{gtype}/{vmid} -> {body.storage}")
    return {"ok": True, "upid": upid}


# ------------------------------------------------------------------ Storage, users, Nodes, Aufgaben
@router.get("/{cid}/storage")
def storage(cid: str, user: dict = Depends(get_current_user)):
    _need(user, cid, "pve_storage")
    cl = _pve(pve_client.client_for, cid)
    return {"storage": [r for r in _resources(cl) if r.get("type") == "storage"], "config": _pve(cl.get, "/storage") or []}


@router.get("/{cid}/storage/{node}/{store}/content")
def storage_content(cid: str, node: str, store: str, content: str = "", user: dict = Depends(get_current_user)):
    _need(user, cid, "pve_storage")
    if not _NODE.match(node) or not _STORE.match(store):
        raise HTTPException(400, "Invalid input")
    cl = _pve(pve_client.client_for, cid)
    return {"content": _pve(cl.get, f"/nodes/{node}/storage/{store}/content", content=content or None) or []}


@router.get("/{cid}/users")
def users(cid: str, user: dict = Depends(get_current_user)):
    _need(user, cid, "pve_users")
    cl = _pve(pve_client.client_for, cid)
    return {"users": _pve(cl.get, "/access/users") or [], "groups": _pve(cl.get, "/access/groups") or [],
            "roles": _pve(cl.get, "/access/roles") or [], "acl": _pve(cl.get, "/access/acl") or []}


@router.get("/{cid}/nodes")
def nodes(cid: str, user: dict = Depends(get_current_user)):
    _need(user, cid, "pve_nodes")
    cl = _pve(pve_client.client_for, cid)
    out = []
    for n in _pve(cl.get, "/nodes") or []:
        try:
            st = cl.get(f"/nodes/{n['node']}/status") or {}
        except PveError:
            st = {}
        out.append({**n, "detail": {k: st.get(k) for k in ("pveversion", "kversion", "loadavg", "cpuinfo", "memory", "rootfs", "uptime")}})
    return {"nodes": out}


@router.get("/{cid}/tasks")
def tasks(cid: str, limit: int = 50, user: dict = Depends(get_current_user)):
    _need(user, cid, "pve_nodes")
    cl = _pve(pve_client.client_for, cid)
    return {"tasks": (_pve(cl.get, "/cluster/tasks") or [])[:max(1, min(limit, 500))]}


@router.get("/{cid}/tasks/{node}/{upid:path}")
def task_status(cid: str, node: str, upid: str, user: dict = Depends(get_current_user)):
    """Status + log of a task (e.g. after start/backup) - visible with pve_access."""
    _need(user, cid, "pve_access")
    if not _NODE.match(node) or not upid.startswith("UPID:"):
        raise HTTPException(400, "Invalid task")
    cl = _pve(pve_client.client_for, cid)
    from urllib.parse import quote
    u = quote(upid, safe="")
    return {"status": _pve(cl.get, f"/nodes/{node}/tasks/{u}/status") or {},
            "log": _pve(cl.get, f"/nodes/{node}/tasks/{u}/log", limit=200) or []}


# ------------------------------------------------------------------ Guest-Freigaben
class AclBody(BaseModel):
    subject: str            # user:<id> | group:<id>
    vmids: list[int] = []


@router.get("/{cid}/acl")
def get_acl(cid: str, user: dict = Depends(get_current_user)):
    _need(user, cid, "pve_manage")
    users_ = {u["id"]: u["username"] for u in db.list_users()}
    groups_ = {g["id"]: g["name"] for g in db.list_groups()}
    return {"acl": pve_store.acl(cid), "users": users_, "groups": groups_}


@router.put("/{cid}/acl")
def put_acl(cid: str, body: AclBody, user: dict = Depends(get_current_user)):
    _need(user, cid, "pve_manage")
    kind, _, sid = body.subject.partition(":")
    if kind not in ("user", "group") or not sid:
        raise HTTPException(400, "subject must be user:<id> or group:<id>")
    pve_store.set_acl(cid, body.subject, body.vmids)
    _audit(user, cid, "acl", f"{body.subject}: {body.vmids}")
    return {"ok": True, "acl": pve_store.acl(cid)}


# ------------------------------------------------------------------ API-Explorer
@router.api_route("/{cid}/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def api_explorer(cid: str, path: str, request: Request, user: dict = Depends(get_current_user)):
    """Beliebiger Proxmox-API-Aufruf (/api2/json/<pfad>) - Recht pve_api."""
    _need(user, cid, "pve_api")
    if ".." in path.split("/"):
        raise HTTPException(400, "Invalid path")
    params = dict(request.query_params)
    params.pop("background", None)        # dashboard control parameter (backend jobs), not for Proxmox
    if request.method in ("POST", "PUT"):
        try:
            body = await request.json()
            if isinstance(body, dict):
                params.update(body)
        except Exception:
            pass
    cl = _pve(pve_client.client_for, cid)
    import asyncio
    data = await asyncio.get_running_loop().run_in_executor(None, lambda: _pve(cl.call, request.method, "/" + path, params))
    if request.method != "GET":
        _audit(user, cid, "api", f"{request.method} /{path}")
    return {"data": data}


# ------------------------------------------------------------------ Proxmox tasks as a backend job
# Start/stop, snapshots and backups start a task (UPID) in Proxmox. The backend
# job follows this task until it finishes (status + task log), so the task view
# shows the real state - even after a reload and for other users. Cancelling
# stops the Proxmox task.
async def _follow_task(job, cid: str, node: str, res: dict) -> dict:
    upid = (res or {}).get("upid")
    if not isinstance(upid, str) or not upid.startswith("UPID"):
        return res
    import urllib.parse
    cl = _pve(pve_client.client_for, cid)
    tpath = f"/nodes/{node}/tasks/{urllib.parse.quote(upid, safe='')}"
    job.update(phase="task", detail="Proxmox task running", upid=upid)
    job.on_cancel = lambda j: asyncio.to_thread(lambda: cl.call("DELETE", tpath))
    seen = 0
    while True:
        await asyncio.sleep(2.0)
        try:
            st = await asyncio.to_thread(cl.get, tpath + "/status") or {}
            lines = await asyncio.to_thread(lambda: cl.get(tpath + "/log", start=seen, limit=500)) or []
        except Exception as e:                 # Proxmox briefly unreachable -> keep waiting
            job.update(detail=f"Proxmox unreachable ({e}) - waiting …")
            continue
        for ln in lines:
            if isinstance(ln, dict) and ln.get("t") not in (None, "no content"):
                job.log(str(ln.get("t")))
        seen += len(lines)
        if lines and isinstance(lines[-1], dict) and lines[-1].get("t"):
            txt = str(lines[-1]["t"])
            m = re.search(r"(\d{1,3})%", txt)
            job.update(detail=txt[:200], pct=float(m.group(1)) if m else None)
        if st.get("status") == "stopped":
            ex = str(st.get("exitstatus") or "")
            if ex != "OK":
                raise HTTPException(502, f"Proxmox-Task beendet: {ex or 'unbekannt'}")
            return {**res, "exitstatus": ex}
