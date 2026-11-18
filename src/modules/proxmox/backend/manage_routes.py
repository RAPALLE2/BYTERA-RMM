# manage_routes.py (Modul "proxmox") - Erweiterte Verwaltung
# ------------------------------------------------------------
# Alles, was man auf der Proxmox-Oberfläche im Alltag tut, über das Modul:
#   Storage/ISOs/Templates verwalten (Download per URL, pveam, löschen),
#   VMs & Container erstellen, migrieren, klonen, in Vorlage wandeln, Disk
#   vergrößern, Node-Netzwerk/Pools ansehen. Nutzt dieselben Helfer + Rechte
#   wie proxmox_routes.py (pve_storage, pve_config, pve_access, pve_nodes).
from __future__ import annotations

import asyncio
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import bgjobs, db
from app.auth import get_current_user

from . import pve_client, pve_store
from .proxmox_routes import (_audit, _check_ids, _follow_task, _gpath, _need, _pve)

router = APIRouter(prefix="/api/proxmox", tags=["proxmox-manage"])

_NODE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_STORE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_VOLID = re.compile(r"^[A-Za-z0-9._\-]+:[A-Za-z0-9._\-/]+$")


def _cl(user, cid, perm):
    _need(user, cid, perm)
    return _pve(pve_client.client_for, cid)


def _node(node):
    if not _NODE.match(node or ""):
        raise HTTPException(400, "Ungültiger Node-Name")
    return node


def _vmid(vmid) -> int:
    v = int(vmid)
    if not (100 <= v <= 999999999):
        raise HTTPException(400, "VMID muss zwischen 100 und 999999999 liegen")
    return v


# ============================================================ Picker / Infos
@router.get("/{cid}/nextid")
def next_id(cid: str, user: dict = Depends(get_current_user)):
    cl = _cl(user, cid, "pve_access")
    return {"vmid": int(_pve(cl.get, "/cluster/nextid"))}


@router.get("/{cid}/node-names")
def node_names(cid: str, user: dict = Depends(get_current_user)):
    """Nur Node-Namen (für Auswahlfelder) – braucht nur pve_access, nicht pve_nodes."""
    cl = _cl(user, cid, "pve_access")
    return {"nodes": [n.get("node") for n in (_pve(cl.get, "/nodes") or []) if n.get("node")]}


@router.get("/{cid}/nodes/{node}/storages")
def node_storages(cid: str, node: str, content: str = "", user: dict = Depends(get_current_user)):
    cl = _cl(user, cid, "pve_access")
    rows = _pve(cl.get, f"/nodes/{_node(node)}/storage", content=content or None) or []
    return {"storages": [{"storage": r.get("storage"), "type": r.get("type"), "content": r.get("content"),
                          "avail": r.get("avail"), "total": r.get("total"), "used": r.get("used"),
                          "active": r.get("active")} for r in rows]}


@router.get("/{cid}/nodes/{node}/bridges")
def node_bridges(cid: str, node: str, user: dict = Depends(get_current_user)):
    cl = _cl(user, cid, "pve_access")
    rows = _pve(cl.get, f"/nodes/{_node(node)}/network", type="any_bridge") or []
    return {"bridges": [{"iface": r.get("iface"), "comments": (r.get("comments") or "").strip(),
                         "active": r.get("active")} for r in rows if r.get("type") in ("bridge", "OVSBridge")]}


@router.get("/{cid}/nodes/{node}/content")
def storage_content(cid: str, node: str, store: str, content: str = "", user: dict = Depends(get_current_user)):
    cl = _cl(user, cid, "pve_access")
    if not _STORE.match(store):
        raise HTTPException(400, "Ungültiger Storage")
    rows = _pve(cl.get, f"/nodes/{_node(node)}/storage/{store}/content", content=content or None) or []
    return {"content": [{"volid": r.get("volid"), "format": r.get("format"), "size": r.get("size"),
                         "content": r.get("content"), "vmid": r.get("vmid"), "ctime": r.get("ctime"),
                         "notes": r.get("notes")} for r in rows]}


@router.get("/{cid}/pools")
def pools(cid: str, user: dict = Depends(get_current_user)):
    cl = _cl(user, cid, "pve_access")
    return {"pools": _pve(cl.get, "/pools") or []}


# ============================================================ ISOs / Templates
class DownloadBody(BaseModel):
    content: str          # "iso" | "vztmpl"
    url: str
    filename: str = ""
    checksum: str = ""
    checksum_algorithm: str = ""


@router.post("/{cid}/nodes/{node}/storage/{store}/download-url")
async def download_url(cid: str, node: str, store: str, body: DownloadBody, background: bool = False,
                       user: dict = Depends(get_current_user)):
    _need(user, cid, "pve_storage")
    if not _STORE.match(store):
        raise HTTPException(400, "Ungültiger Storage")
    if body.content not in ("iso", "vztmpl"):
        raise HTTPException(400, "content muss iso oder vztmpl sein")
    if not re.match(r"^https?://", body.url or ""):
        raise HTTPException(400, "Ungültige URL")
    fn = body.filename or body.url.rstrip("/").split("/")[-1]
    if not re.match(r"^[A-Za-z0-9._\-]+$", fn):
        raise HTTPException(400, "Ungültiger Dateiname")

    def impl():
        cl = _pve(pve_client.client_for, cid)
        params = {"content": body.content, "url": body.url, "filename": fn, "verify-certificates": 1}
        if body.checksum:
            params["checksum"] = body.checksum
            params["checksum-algorithm"] = body.checksum_algorithm or "sha256"
        upid = _pve(cl.call, "POST", f"/nodes/{_node(node)}/storage/{store}/download-url", params)
        _audit(user, cid, "storage.download", f"{fn} -> {store}")
        return {"upid": upid}

    async def work(job):
        job.update(phase="proxmox.task", detail=f"Lade {fn} nach {store} …")
        return await _follow_task(job, cid, node, await asyncio.to_thread(impl))
    return await bgjobs.run("proxmox.task", f"{cid}:dl:{fn}", title=f"Proxmox: {fn} herunterladen",
                            user=user, work=work, background=background, perm="pve_storage",
                            client_id=cid if db.get_client(cid) else None)


class DeleteVolBody(BaseModel):
    volid: str


@router.post("/{cid}/nodes/{node}/storage/{store}/delete")
def delete_volume(cid: str, node: str, store: str, body: DeleteVolBody, user: dict = Depends(get_current_user)):
    cl = _cl(user, cid, "pve_storage")
    if not _STORE.match(store) or not _VOLID.match(body.volid or ""):
        raise HTTPException(400, "Ungültige Angabe")
    import urllib.parse
    _pve(cl.call, "DELETE", f"/nodes/{_node(node)}/storage/{store}/content/{urllib.parse.quote(body.volid, safe='')}")
    _audit(user, cid, "storage.delete", body.volid)
    return {"ok": True}


@router.get("/{cid}/nodes/{node}/aplinfo")
def aplinfo(cid: str, node: str, user: dict = Depends(get_current_user)):
    cl = _cl(user, cid, "pve_access")
    rows = _pve(cl.get, f"/nodes/{_node(node)}/aplinfo") or []
    return {"templates": [{"template": r.get("template"), "os": r.get("os"), "version": r.get("version"),
                           "section": r.get("section"), "description": (r.get("description") or "")[:200],
                           "type": r.get("type")} for r in rows]}


class AplBody(BaseModel):
    storage: str
    template: str


@router.post("/{cid}/nodes/{node}/aplinfo/download")
async def apl_download(cid: str, node: str, body: AplBody, background: bool = False, user: dict = Depends(get_current_user)):
    _need(user, cid, "pve_storage")
    if not _STORE.match(body.storage):
        raise HTTPException(400, "Ungültiger Storage")

    def impl():
        cl = _pve(pve_client.client_for, cid)
        upid = _pve(cl.call, "POST", f"/nodes/{_node(node)}/aplinfo",
                    {"storage": body.storage, "template": body.template})
        _audit(user, cid, "template.download", body.template)
        return {"upid": upid}

    async def work(job):
        job.update(phase="proxmox.task", detail=f"Lade Vorlage {body.template} …")
        return await _follow_task(job, cid, node, await asyncio.to_thread(impl))
    return await bgjobs.run("proxmox.task", f"{cid}:apl", title=f"Proxmox: Vorlage {body.template}",
                            user=user, work=work, background=background, perm="pve_storage",
                            client_id=cid if db.get_client(cid) else None)


# ============================================================ VM / CT erstellen
class VMCreate(BaseModel):
    node: str
    vmid: int | None = None
    name: str = ""
    cores: int = 2
    sockets: int = 1
    memory: int = 2048            # MiB
    ostype: str = "l26"
    iso: str = ""                 # volid einer ISO (ide2 cdrom)
    storage: str = ""             # Ziel-Storage für die Disk
    disk_gb: int = 32
    disk_format: str = ""         # qcow2|raw|vmdk (leer = Storage-Standard)
    bridge: str = "vmbr0"
    vlan: int | None = None
    net_model: str = "virtio"
    start: bool = False
    bios: str = "seabios"         # seabios|ovmf
    machine: str = ""             # z.B. q35
    agent: bool = True
    extra: dict = {}              # rohe zusätzliche Parameter (Profis)


@router.post("/{cid}/create/qemu")
async def create_vm(cid: str, body: VMCreate, background: bool = False, user: dict = Depends(get_current_user)):
    _need(user, cid, "pve_config")
    node = _node(body.node)
    if not body.storage or not _STORE.match(body.storage):
        raise HTTPException(400, "Ziel-Storage fehlt oder ungültig")
    if body.name and not re.match(r"^[A-Za-z0-9.-]{1,63}$", body.name):
        raise HTTPException(400, "Ungültiger Name (nur Buchstaben, Ziffern, . und -)")

    def impl():
        cl = _pve(pve_client.client_for, cid)
        vmid = _vmid(body.vmid) if body.vmid else int(_pve(cl.get, "/cluster/nextid"))
        disk = f"{body.storage}:{max(1, int(body.disk_gb))}"
        if body.disk_format:
            disk += f",format={body.disk_format}"
        net = f"{body.net_model},bridge={body.bridge}"
        if body.vlan:
            net += f",tag={int(body.vlan)}"
        params = {"vmid": vmid, "cores": int(body.cores), "sockets": int(body.sockets),
                  "memory": int(body.memory), "ostype": body.ostype, "scsihw": "virtio-scsi-single",
                  "scsi0": disk, "net0": net, "name": body.name or None, "bios": body.bios,
                  "agent": 1 if body.agent else None, "machine": body.machine or None,
                  "boot": "order=scsi0;ide2;net0"}
        if body.iso:
            if not _VOLID.match(body.iso):
                raise HTTPException(400, "Ungültige ISO")
            params["ide2"] = f"{body.iso},media=cdrom"
        for k, v in (body.extra or {}).items():
            if re.match(r"^[a-z0-9_]+$", str(k)):
                params[str(k)] = v
        upid = _pve(cl.call, "POST", f"/nodes/{node}/qemu", params)
        _audit(user, cid, "vm.create", f"{vmid} ({body.name})")
        return {"upid": upid, "vmid": vmid, "start": body.start, "node": node}

    async def work(job):
        job.update(phase="proxmox.task", detail="Erstelle VM …")
        res = await asyncio.to_thread(impl)
        out = await _follow_task(job, cid, node, res)
        if res.get("start"):
            job.log("Starte VM …")
            cl = _pve(pve_client.client_for, cid)
            await _follow_task(job, cid, node, {"upid": _pve(cl.post, f"/nodes/{node}/qemu/{res['vmid']}/status/start")})
        return {"vmid": res["vmid"], **(out or {})}
    return await bgjobs.run("proxmox.task", f"{cid}:create", title="Proxmox: VM erstellen", user=user,
                            work=work, background=background, perm="pve_config",
                            client_id=cid if db.get_client(cid) else None)


class CTCreate(BaseModel):
    node: str
    vmid: int | None = None
    hostname: str = ""
    ostemplate: str = ""          # volid eines CT-Templates (storage:vztmpl/...)
    storage: str = ""             # rootfs-Storage
    rootfs_gb: int = 8
    memory: int = 512
    swap: int = 512
    cores: int = 1
    password: str = ""
    ssh_public_keys: str = ""
    unprivileged: bool = True
    nesting: bool = False
    bridge: str = "vmbr0"
    vlan: int | None = None
    ip: str = "dhcp"              # "dhcp" oder CIDR z.B. 192.168.1.50/24
    gw: str = ""
    start: bool = False
    extra: dict = {}


@router.post("/{cid}/create/lxc")
async def create_ct(cid: str, body: CTCreate, background: bool = False, user: dict = Depends(get_current_user)):
    _need(user, cid, "pve_config")
    node = _node(body.node)
    if not body.ostemplate or not _VOLID.match(body.ostemplate):
        raise HTTPException(400, "CT-Template (ostemplate) fehlt oder ungültig")
    if not body.storage or not _STORE.match(body.storage):
        raise HTTPException(400, "rootfs-Storage fehlt oder ungültig")
    if body.hostname and not re.match(r"^[A-Za-z0-9.-]{1,63}$", body.hostname):
        raise HTTPException(400, "Ungültiger Hostname")
    if not body.password and not body.ssh_public_keys:
        raise HTTPException(400, "Passwort oder SSH-Schlüssel nötig")

    def impl():
        cl = _pve(pve_client.client_for, cid)
        vmid = _vmid(body.vmid) if body.vmid else int(_pve(cl.get, "/cluster/nextid"))
        net = f"name=eth0,bridge={body.bridge}"
        if body.vlan:
            net += f",tag={int(body.vlan)}"
        if body.ip and body.ip != "dhcp":
            net += f",ip={body.ip}"
            if body.gw:
                net += f",gw={body.gw}"
        else:
            net += ",ip=dhcp"
        params = {"vmid": vmid, "ostemplate": body.ostemplate, "storage": body.storage,
                  "rootfs": f"{body.storage}:{max(1, int(body.rootfs_gb))}", "memory": int(body.memory),
                  "swap": int(body.swap), "cores": int(body.cores), "hostname": body.hostname or None,
                  "unprivileged": 1 if body.unprivileged else 0, "net0": net,
                  "password": body.password or None, "ssh-public-keys": body.ssh_public_keys or None,
                  "features": "nesting=1" if body.nesting else None, "start": 1 if body.start else 0}
        for k, v in (body.extra or {}).items():
            if re.match(r"^[a-z0-9_\-]+$", str(k)):
                params[str(k)] = v
        upid = _pve(cl.call, "POST", f"/nodes/{node}/lxc", params)
        _audit(user, cid, "ct.create", f"{vmid} ({body.hostname})")
        return {"upid": upid, "vmid": vmid}

    async def work(job):
        job.update(phase="proxmox.task", detail="Erstelle Container …")
        res = await asyncio.to_thread(impl)
        out = await _follow_task(job, cid, node, res)
        return {"vmid": res["vmid"], **(out or {})}
    return await bgjobs.run("proxmox.task", f"{cid}:create", title="Proxmox: Container erstellen", user=user,
                            work=work, background=background, perm="pve_config",
                            client_id=cid if db.get_client(cid) else None)


# ============================================================ Migrieren / Klonen / Vorlage / Resize
class MigrateBody(BaseModel):
    target: str
    online: bool = True
    with_local_disks: bool = True
    target_storage: str = ""


@router.post("/{cid}/guests/{node}/{gtype}/{vmid}/migrate")
async def migrate(cid: str, node: str, gtype: str, vmid: int, body: MigrateBody, background: bool = False,
                  user: dict = Depends(get_current_user)):
    _need(user, cid, "pve_config")
    _check_ids(node, gtype, vmid)
    if not _NODE.match(body.target):
        raise HTTPException(400, "Ungültiger Ziel-Node")

    def impl():
        cl = _pve(pve_client.client_for, cid)
        params = {"target": body.target}
        if gtype == "qemu":
            params["online"] = 1 if body.online else 0
            if body.with_local_disks:
                params["with-local-disks"] = 1
            if body.target_storage:
                params["targetstorage"] = body.target_storage
        else:
            params["restart"] = 1 if body.online else 0      # CTs: Live-Migration nicht möglich -> restart
        upid = _pve(cl.call, "POST", _gpath(node, gtype, vmid) + "/migrate", params)
        _audit(user, cid, "guest.migrate", f"{gtype}/{vmid} {node}->{body.target}")
        return {"upid": upid}

    async def work(job):
        job.update(phase="proxmox.task", detail=f"Migriere {gtype}/{vmid} nach {body.target} …")
        return await _follow_task(job, cid, node, await asyncio.to_thread(impl))
    return await bgjobs.run("proxmox.task", f"{cid}:{vmid}", title=f"Proxmox {gtype}/{vmid}: migrieren nach {body.target}",
                            user=user, work=work, background=background, perm="pve_config",
                            client_id=cid if db.get_client(cid) else None)


class CloneBody(BaseModel):
    newid: int | None = None
    name: str = ""
    full: bool = True
    target: str = ""
    storage: str = ""


@router.post("/{cid}/guests/{node}/{gtype}/{vmid}/clone")
async def clone(cid: str, node: str, gtype: str, vmid: int, body: CloneBody, background: bool = False,
                user: dict = Depends(get_current_user)):
    _need(user, cid, "pve_config")
    _check_ids(node, gtype, vmid)

    def impl():
        cl = _pve(pve_client.client_for, cid)
        newid = _vmid(body.newid) if body.newid else int(_pve(cl.get, "/cluster/nextid"))
        params = {"newid": newid, "full": 1 if body.full else 0}
        params["name" if gtype == "qemu" else "hostname"] = body.name or None
        if body.target:
            params["target"] = body.target
        if body.storage:
            params["storage"] = body.storage
        upid = _pve(cl.call, "POST", _gpath(node, gtype, vmid) + "/clone", params)
        _audit(user, cid, "guest.clone", f"{gtype}/{vmid} -> {newid}")
        return {"upid": upid, "newid": newid}

    async def work(job):
        job.update(phase="proxmox.task", detail=f"Klone {gtype}/{vmid} …")
        res = await asyncio.to_thread(impl)
        out = await _follow_task(job, cid, node, res)
        return {"newid": res["newid"], **(out or {})}
    return await bgjobs.run("proxmox.task", f"{cid}:{vmid}", title=f"Proxmox {gtype}/{vmid}: klonen",
                            user=user, work=work, background=background, perm="pve_config",
                            client_id=cid if db.get_client(cid) else None)


@router.post("/{cid}/guests/{node}/{gtype}/{vmid}/template")
def to_template(cid: str, node: str, gtype: str, vmid: int, user: dict = Depends(get_current_user)):
    cl = _cl(user, cid, "pve_config")
    _check_ids(node, gtype, vmid)
    _pve(cl.call, "POST", _gpath(node, gtype, vmid) + "/template")
    _audit(user, cid, "guest.template", f"{gtype}/{vmid}")
    return {"ok": True}


class ResizeBody(BaseModel):
    disk: str              # z.B. scsi0 / rootfs
    size: str              # z.B. "+8G" oder "40G"


@router.put("/{cid}/guests/{node}/{gtype}/{vmid}/resize")
def resize(cid: str, node: str, gtype: str, vmid: int, body: ResizeBody, user: dict = Depends(get_current_user)):
    cl = _cl(user, cid, "pve_config")
    _check_ids(node, gtype, vmid)
    if not re.match(r"^[a-z0-9]+$", body.disk) or not re.match(r"^\+?\d+[MGT]$", body.size):
        raise HTTPException(400, "Ungültige Disk/Größe (z.B. +8G)")
    _pve(cl.call, "PUT", _gpath(node, gtype, vmid) + "/resize", {"disk": body.disk, "size": body.size})
    _audit(user, cid, "guest.resize", f"{gtype}/{vmid} {body.disk} {body.size}")
    return {"ok": True}
