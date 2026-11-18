"""
backup_routes.py (Modul "backup") - API im Proxmox-Stil.

Speicherziele  /api/backup/storages          (nur Admin)
Backup-Jobs    /api/backup/jobs              (nur Admin)
Pro Client     /api/backup/clients/{cid}/... (Recht "use_terminal" am Client)
  backups      Liste (Agent-Repos + Proxmox-vzdump), sichern, umbenennen,
               Notizen, schuetzen, loeschen, wiederherstellen, Dateien ansehen
  snapshots    Proxmox-Snapshots (VM/LXC) oder Betriebssystem-Snapshots
               (Windows VSS, Linux btrfs/ZFS/LVM)
Werkzeuge      /api/backup/tools/restic      (nur Agents, per Agent-Token)
"""

from __future__ import annotations

import asyncio
import hmac
import re
import time

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from app import bgjobs, db
from app.auth import get_current_user, is_super_admin, require_perm
from app.config import AGENT_TOKEN

from rmm_mod_backup import agentlink, drivers, engine, store
from rmm_mod_backup.engine import EngineError

router = APIRouter(prefix="/api/backup", tags=["backup"])

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")


def _admin(user):
    if not is_super_admin(user):
        raise HTTPException(403, "Nur Admins")


def _client_perm(user, cid):
    require_perm(user, "use_terminal", cid)


def _e(exc: Exception) -> HTTPException:
    return HTTPException(400 if isinstance(exc, (EngineError, ValueError)) else 502, str(exc))


# ================================================================== Speicherziele

class StorageBody(BaseModel):
    id: str = ""
    label: str = ""
    type: str
    enabled: bool = True
    # SMB
    server: str = ""
    share: str = ""
    domain: str = ""
    # SFTP / FTP
    host: str = ""
    port: int | None = None
    tls: bool = False
    # gemeinsam
    path: str = ""
    user: str = ""
    password: str = ""
    # PBS
    datastore: str = ""
    secret: str = ""
    fingerprint: str = ""
    namespace: str = ""


@router.get("/storages")
def list_storages(user: dict = Depends(get_current_user)):
    _admin(user)
    return [store.storage_public(sid, s) for sid, s in store.storages().items()]


def _storage_data(body: StorageBody) -> dict:
    if body.type not in store.STORAGE_TYPES:
        raise HTTPException(400, "Unbekannter Speicherziel-Typ")
    fields = {
        "backend": ("path",),
        "smb": ("server", "share", "path", "user", "password", "domain"),
        "sftp": ("host", "port", "path", "user", "password"),
        "ftp": ("host", "port", "path", "user", "password", "tls"),
        "pbs": ("server", "port", "datastore", "user", "secret", "fingerprint", "namespace"),
    }[body.type]
    data = {"type": body.type, "label": body.label.strip()[:60] or body.type,
            "enabled": body.enabled}
    for f in fields:
        data[f] = getattr(body, f)
    need = {"smb": ("server", "share"), "sftp": ("host", "user"), "ftp": ("host",),
            "pbs": ("server", "datastore", "user")}.get(body.type, ())
    for f in need:
        if not str(data.get(f) or "").strip():
            raise HTTPException(400, f"Feld „{f}“ fehlt")
    if body.type == "backend" and body.path:
        raise HTTPException(400, "Pfad für das Backend-Ziel ist fest (storage/backup)")
    return data


@router.post("/storages")
def create_storage(body: StorageBody, user: dict = Depends(get_current_user)):
    _admin(user)
    sid = body.id.strip().lower()
    if not ID_RE.match(sid):
        raise HTTPException(400, "ID: 2-32 Zeichen, a-z, 0-9, - und _")
    if sid in store.storages():
        raise HTTPException(409, "ID ist bereits vergeben")
    s = store.save_storage(sid, _storage_data(body))
    db.add_audit_entry(user["username"], "backup.storage.create", None, f"{sid} ({body.type})")
    return store.storage_public(sid, s)


@router.put("/storages/{sid}")
def update_storage(sid: str, body: StorageBody, user: dict = Depends(get_current_user)):
    _admin(user)
    old = store.storages().get(sid)
    if not old:
        raise HTTPException(404, "Speicherziel nicht gefunden")
    data = _storage_data(body)
    for k in ("private_key", "public_key"):          # SFTP-Schluessel behalten
        if old.get(k):
            data[k] = old[k]
    s = store.save_storage(sid, data)
    return store.storage_public(sid, s)


@router.delete("/storages/{sid}")
def delete_storage(sid: str, user: dict = Depends(get_current_user)):
    _admin(user)
    if sid == "local":
        raise HTTPException(400, "Das Backend-Ziel lässt sich nicht löschen")
    if any(j.get("storage") == sid for j in store.jobs().values()):
        raise HTTPException(409, "Speicherziel wird noch von einem Backup-Job benutzt")
    store.delete_storage(sid)
    db.add_audit_entry(user["username"], "backup.storage.delete", None, sid)
    return {"ok": True}


@router.post("/storages/{sid}/test")
async def test_storage(sid: str, user: dict = Depends(get_current_user)):
    _admin(user)
    st = store.storage(sid)
    if not st:
        raise HTTPException(404, "Speicherziel nicht gefunden")
    if st["type"] == "pbs":
        from rmm_mod_backup import pbs
        try:
            return await asyncio.to_thread(pbs.test, st)
        except Exception as exc:
            raise HTTPException(502, f"PBS nicht erreichbar: {exc}")
    try:
        return await asyncio.to_thread(drivers.test, st)
    except Exception as exc:
        raise HTTPException(502, f"Verbindung fehlgeschlagen: {exc}")


@router.post("/storages/{sid}/sftp-key")
def sftp_key(sid: str, user: dict = Depends(get_current_user)):
    """SSH-Schluesselpaar fuer SFTP erzeugen (restic braucht Schluessel-Login)."""
    _admin(user)
    d = store.storages()
    st = d.get(sid)
    if not st or st.get("type") != "sftp":
        raise HTTPException(404, "SFTP-Speicherziel nicht gefunden")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    st["private_key"] = key.private_bytes(serialization.Encoding.PEM,
                                          serialization.PrivateFormat.OpenSSH,
                                          serialization.NoEncryption()).decode()
    st["public_key"] = key.public_key().public_bytes(serialization.Encoding.OpenSSH,
                                                     serialization.PublicFormat.OpenSSH).decode() \
        + " bytera-backup"
    store.save_storage(sid, st)
    return {"public_key": st["public_key"]}


# ================================================================== Jobs

class JobBody(BaseModel):
    name: str
    enabled: bool = True
    engine: str = "agent"               # agent | pve
    clients: list[str] = []
    tags: list[str] = []
    storage: str = ""
    route: str = "backend"              # direct | backend | via_backend
    mode: str = "files"                 # files | image
    paths: list[str] = []
    excludes: list[str] = []
    image_device: str = ""
    compression: str = "zstd"           # none | fast | good | zstd
    schedule: dict = {"type": "daily", "time": "02:00"}
    repeat_missed: bool = True
    retention: dict = {"keep_last": 3}
    name_template: str = "{{client}} {{date}} {{time}}"
    pve_storage: str = ""
    pve_mode: str = "snapshot"          # snapshot | suspend | stop


def _job_data(body: JobBody) -> dict:
    if body.engine not in ("agent", "pve"):
        raise HTTPException(400, "Ungültige Engine")
    if body.mode not in ("files", "image") or body.compression not in engine.COMPRESSION:
        raise HTTPException(400, "Ungültiger Modus oder Komprimierung")
    if body.engine == "agent":
        st = store.storage(body.storage)
        if not st:
            raise HTTPException(400, "Speicherziel fehlt")
        try:
            engine.check_route(st, body.route)
        except EngineError as exc:
            raise HTTPException(400, str(exc))
    elif not body.pve_storage or body.pve_mode not in ("snapshot", "suspend", "stop"):
        raise HTTPException(400, "Proxmox-Storage und -Modus angeben")
    if not body.clients and not body.tags:
        raise HTTPException(400, "Mindestens einen Client oder Tag auswählen")
    return {**body.model_dump(), "name": body.name.strip()[:80] or "Backup-Job"}


@router.get("/jobs")
def list_jobs(user: dict = Depends(get_current_user)):
    _admin(user)
    from rmm_mod_backup.scheduler import last_slot
    out = []
    for jid, j in store.jobs().items():
        out.append({**j, "id": jid, "targets": engine.job_targets({**j, "id": jid}),
                    "last_slot": last_slot(j.get("schedule") or {})})
    return sorted(out, key=lambda j: j.get("name", "").lower())


@router.post("/jobs")
def create_job(body: JobBody, user: dict = Depends(get_current_user)):
    _admin(user)
    data = _job_data(body)
    data["created"] = int(time.time() * 1000)
    j = store.save_job(None, data)
    db.add_audit_entry(user["username"], "backup.job.create", None, j["name"])
    return j


@router.put("/jobs/{jid}")
def update_job(jid: str, body: JobBody, user: dict = Depends(get_current_user)):
    _admin(user)
    old = store.job(jid)
    if not old:
        raise HTTPException(404, "Job nicht gefunden")
    data = _job_data(body)
    data["created"] = old.get("created") or int(time.time() * 1000)
    data["runs"] = old.get("runs") or {}
    return store.save_job(jid, data)


@router.delete("/jobs/{jid}")
def delete_job(jid: str, user: dict = Depends(get_current_user)):
    _admin(user)
    store.delete_job(jid)
    db.add_audit_entry(user["username"], "backup.job.delete", None, jid)
    return {"ok": True}


@router.post("/jobs/{jid}/run")
async def run_job_now(jid: str, background: bool = False, user: dict = Depends(get_current_user)):
    _admin(user)
    job = store.job(jid)
    if not job:
        raise HTTPException(404, "Job nicht gefunden")

    async def work(bj):
        from app.sockets import state
        results = []
        for cid in engine.job_targets(job):
            c = db.get_client(cid) or {}
            host = c.get("short_name") or c.get("hostname") or cid
            if job.get("engine") == "agent" and not state.is_online(cid):
                bj.log(f"⤼ {host}: offline - übersprungen")
                results.append({"client": host, "status": "offline"})
                continue
            bj.log(f"▶ {host}")
            bj.detail = host
            try:
                await engine.run_job(job, cid, log=lambda line, h=host: bj.log(f"  [{h}] {line}"))
                bj.log(f"✅ {host}")
                results.append({"client": host, "status": "ok"})
            except Exception as exc:
                bj.log(f"❌ {host}: {exc}")
                results.append({"client": host, "status": "failed", "error": str(exc)})
        store.update_job_state(jid, last_run=int(time.time() * 1000),
                               last_status="ok" if all(r["status"] != "failed" for r in results)
                               else "teilweise fehlgeschlagen")
        return {"results": results}

    return await bgjobs.run("backup", f"job:{jid}", title=f"Backup-Job „{job['name']}“ jetzt",
                            user=user, work=work, background=background)


# ================================================================== Pro Client: Backups

def _catalog_for(cid: str) -> list[dict]:
    out = []
    for key, entry in store.catalog().items():
        if entry.get("client_id") != cid:
            continue
        st = store.storages().get(entry["storage"], {})
        for s in entry.get("snapshots", []):
            out.append({**s, "storage": entry["storage"], "storage_label": st.get("label", entry["storage"]),
                        "engine": entry.get("engine"), "route": entry.get("route", "")})
    return out


@router.get("/clients/{cid}/backups")
async def client_backups(cid: str, user: dict = Depends(get_current_user)):
    _client_perm(user, cid)
    items = _catalog_for(cid)
    pve = engine.pve_target(cid)
    pve_items, pve_storages, pve_err = [], [], ""
    if pve:
        try:
            cl = engine._pve_client(pve["host"])
            stores = [s for s in (await asyncio.to_thread(cl.get, f"/nodes/{pve['node']}/storage",
                                                          content="backup") or []) if s.get("active", 1)]
            pve_storages = [s["storage"] for s in stores]
            for s in stores:
                for it in await asyncio.to_thread(cl.get, f"/nodes/{pve['node']}/storage/{s['storage']}/content",
                                                  content="backup", vmid=int(pve["vmid"])) or []:
                    pve_items.append({"id": it.get("volid"), "time": (it.get("ctime") or 0) * 1000,
                                      "size": it.get("size"), "name": it.get("notes") or "",
                                      "notes": it.get("notes") or "", "protected": bool(it.get("protected")),
                                      "storage": s["storage"], "storage_label": f"Proxmox: {s['storage']}",
                                      "engine": "pve", "mode": "vzdump"})
        except Exception as exc:
            pve_err = str(exc)
    all_items = sorted(items + pve_items, key=lambda x: x.get("time") or 0, reverse=True)
    return {"backups": all_items, "pve": pve, "pve_storages": pve_storages, "pve_error": pve_err}


class ManualBackup(BaseModel):
    engine: str = "agent"
    storage: str = ""
    route: str = ""
    mode: str = "files"
    paths: list[str] = []
    excludes: list[str] = []
    image_device: str = ""
    compression: str = "zstd"
    name: str = ""
    pve_storage: str = ""
    pve_mode: str = "snapshot"


@router.post("/clients/{cid}/backup")
async def client_backup_now(cid: str, body: ManualBackup, background: bool = False,
                            user: dict = Depends(get_current_user)):
    _client_perm(user, cid)
    c = db.get_client(cid) or {}
    host = c.get("short_name") or c.get("hostname") or cid
    st = store.storage(body.storage) if body.engine == "agent" else None
    route = body.route or (engine.default_route(st) if st else "")

    async def work(bj):
        def plog(line):
            bj.log(line)
            bj.detail = line
        return await engine.backup_client(
            cid, storage_id=body.storage, route=route, mode=body.mode, paths=body.paths,
            excludes=body.excludes, image_device=body.image_device, compression=body.compression,
            name=body.name or engine.render_name("", c), engine=body.engine,
            pve_storage=body.pve_storage, pve_mode=body.pve_mode, log=plog)

    db.add_audit_entry(user["username"], "backup.manual", cid, body.name or body.mode)
    return await bgjobs.run("backup", f"manual:{cid}", title=f"Backup jetzt: {host}",
                            user=user, work=work, background=background, client_id=cid)


def _repo_spec(cid: str, sid: str, route: str = "") -> tuple[dict, dict]:
    st = store.storage(sid)
    if not st:
        raise HTTPException(404, "Speicherziel nicht gefunden")
    entry = store.catalog().get(f"{cid}|{sid}", {})
    route = route or entry.get("route") or engine.default_route(st)
    try:
        return st, engine.agent_spec(cid, st, route)
    except EngineError as exc:
        raise HTTPException(400, str(exc))


class RefreshBody(BaseModel):
    storage: str


@router.post("/clients/{cid}/refresh")
async def client_refresh(cid: str, body: RefreshBody, user: dict = Depends(get_current_user)):
    """Snapshot-Liste vom Repo neu einlesen (Client muss online sein)."""
    _client_perm(user, cid)
    st, spec = _repo_spec(cid, body.storage)
    res = await agentlink.call(cid, "snapshots", spec, {}, timeout=600)
    if not res.get("ok"):
        raise HTTPException(502, res.get("error") or "Fehlgeschlagen")
    store.set_snapshots(cid, body.storage, spec["kind"], res.get("snapshots") or [])
    return {"ok": True, "count": len(res.get("snapshots") or [])}


class MetaBody(BaseModel):
    storage: str
    id: str
    name: str | None = None
    notes: str | None = None
    protected: bool | None = None


@router.patch("/clients/{cid}/backups")
async def client_backup_meta(cid: str, body: MetaBody, user: dict = Depends(get_current_user)):
    """Umbenennen / Notizen / Schuetzen (wie bei Proxmox)."""
    _client_perm(user, cid)
    if body.storage in store.storages():
        st, spec = _repo_spec(cid, body.storage)
        if body.protected is not None or (spec["kind"] == "pbs" and body.notes is not None):
            res = await agentlink.call(cid, "meta", spec, {"id": body.id, "protected": body.protected,
                                                           "notes": body.notes}, timeout=300)
            if not res.get("ok"):
                raise HTTPException(502, res.get("error") or "Client nicht erreichbar - Schutz "
                                         "lässt sich nur bei online Client ändern")
        s = store.update_snapshot_meta(cid, body.storage, body.id, name=body.name,
                                       notes=body.notes, protected=body.protected)
        if s is None:
            raise HTTPException(404, "Backup nicht gefunden")
        return s
    # Proxmox-vzdump: Notizen/Schutz direkt am Proxmox-Storage-Volume
    pve = engine.pve_target(cid)
    if not pve:
        raise HTTPException(404, "Speicherziel nicht gefunden")
    cl = engine._pve_client(pve["host"])
    params = {}
    if body.notes is not None or body.name is not None:
        params["notes"] = (body.notes if body.notes is not None else body.name) or ""
    if body.protected is not None:
        params["protected"] = int(body.protected)
    await asyncio.to_thread(cl.call, "PUT",
                            f"/nodes/{pve['node']}/storage/{body.storage}/content/{body.id}", params)
    return {"ok": True}


class DeleteBody(BaseModel):
    storage: str
    id: str


@router.post("/clients/{cid}/backups/delete")
async def client_backup_delete(cid: str, body: DeleteBody, user: dict = Depends(get_current_user)):
    _client_perm(user, cid)
    if body.storage in store.storages():
        meta = next((s for s in _catalog_for(cid) if s["id"] == body.id and s["storage"] == body.storage), None)
        if meta and meta.get("protected"):
            raise HTTPException(409, "Backup ist geschützt - zuerst den Schutz aufheben")
        st, spec = _repo_spec(cid, body.storage)
        res = await agentlink.call(cid, "forget", spec, {"ids": [body.id]}, timeout=3600)
        if not res.get("ok"):
            raise HTTPException(502, res.get("error") or "Löschen fehlgeschlagen")
        store.set_snapshots(cid, body.storage, spec["kind"], res.get("snapshots") or [])
    else:
        pve = engine.pve_target(cid)
        if not pve:
            raise HTTPException(404, "Speicherziel nicht gefunden")
        cl = engine._pve_client(pve["host"])
        upid = await asyncio.to_thread(cl.call, "DELETE",
                                       f"/nodes/{pve['node']}/storage/{body.storage}/content/{body.id}")
        if upid:
            await engine.pve_wait(pve["host"], pve["node"], upid)
    db.add_audit_entry(user["username"], "backup.delete", cid, f"{body.storage}: {body.id}")
    return {"ok": True}


class RestoreBody(BaseModel):
    storage: str
    id: str
    target_client: str = ""           # leer = derselbe Client
    target: str = "original"          # original | path
    path: str = ""                    # Zielordner bei target=path
    include: list[str] = []           # nur diese Dateien/Ordner
    image_dest: str = ""              # Image: Datei- oder Datentraeger-Pfad
    image_to_device: bool = False
    pve_vmid: int | None = None       # Proxmox: Ziel-VMID (leer = gleiche, überschreibt)
    pve_start: bool = False


@router.post("/clients/{cid}/restore")
async def client_restore(cid: str, body: RestoreBody, background: bool = False,
                         user: dict = Depends(get_current_user)):
    _client_perm(user, cid)
    tgt = body.target_client or cid
    if tgt != cid:
        _client_perm(user, tgt)

    async def work(bj):
        def plog(line):
            bj.log(line)
            bj.detail = line
        if body.storage not in store.storages():
            pve = engine.pve_target(cid)
            if not pve:
                raise EngineError("Speicherziel nicht gefunden")
            cl = engine._pve_client(pve["host"])
            vmid = int(body.pve_vmid or pve["vmid"])
            params = {"vmid": vmid, "storage": "", "force": 1 if vmid == int(pve["vmid"]) else 0}
            params.pop("storage")
            if pve["type"] == "qemu":
                params["archive"] = body.id
            else:
                params.update({"ostemplate": body.id, "restore": 1})
            if body.pve_start:
                params["start"] = 1
            plog(f"Proxmox-Wiederherstellung {pve['type']}/{vmid} aus {body.id}")
            upid = await asyncio.to_thread(cl.call, "POST", f"/nodes/{pve['node']}/{pve['type']}", params)
            await engine.pve_wait(pve["host"], pve["node"], upid, plog)
            return {"ok": True}
        st, spec = _repo_spec(cid, body.storage)
        res = await agentlink.call(tgt, "restore", spec, {
            "id": body.id, "target": body.target, "path": body.path, "include": body.include,
            "image_dest": body.image_dest, "image_to_device": body.image_to_device},
            timeout=24 * 3600, progress=plog)
        if not res.get("ok"):
            raise EngineError(res.get("error") or "Wiederherstellung fehlgeschlagen")
        return res

    db.add_audit_entry(user["username"], "backup.restore", tgt, f"{body.storage}: {body.id}")
    return await bgjobs.run("backup", f"restore:{tgt}", title="Wiederherstellung", user=user,
                            work=work, background=background, client_id=tgt)


@router.get("/clients/{cid}/ls")
async def client_ls(cid: str, storage: str, id: str, path: str = "/",
                    user: dict = Depends(get_current_user)):
    """Dateien in einem Backup ansehen (fuer Einzel-Wiederherstellung)."""
    _client_perm(user, cid)
    st, spec = _repo_spec(cid, storage)
    res = await agentlink.call(cid, "ls", spec, {"id": id, "path": path}, timeout=600)
    if not res.get("ok"):
        raise HTTPException(502, res.get("error") or "Fehlgeschlagen")
    return {"entries": res.get("entries") or []}


# ================================================================== Pro Client: Snapshots

@router.get("/clients/{cid}/snapshots")
async def client_snapshots(cid: str, user: dict = Depends(get_current_user)):
    _client_perm(user, cid)
    pve = engine.pve_target(cid)
    if pve:
        cl = engine._pve_client(pve["host"])
        items = await asyncio.to_thread(cl.get, f"/nodes/{pve['node']}/{pve['type']}/{pve['vmid']}/snapshot") or []
        return {"kind": "pve", "pve": pve, "snapshots": [
            {"id": s["name"], "name": s["name"], "time": (s.get("snaptime") or 0) * 1000,
             "notes": s.get("description") or "", "vmstate": bool(s.get("vmstate")),
             "parent": s.get("parent")} for s in items if s.get("name") != "current"]}
    res = await agentlink.call(cid, "fs-snap-list", {}, {}, timeout=120)
    if not res.get("ok"):
        raise HTTPException(502, res.get("error") or "Client nicht erreichbar")
    names = store.fs_snapshot_names()
    snaps = [{**s, "name": names.get(f"{cid}|{s['id']}") or s.get("name") or ""}
             for s in res.get("snapshots") or []]
    return {"kind": res.get("kind"), "snapshots": snaps, "info": res.get("info", "")}


class SnapBody(BaseModel):
    name: str
    notes: str = ""
    vmstate: bool = False
    volume: str = ""


SNAP_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,39}$")


@router.post("/clients/{cid}/snapshots")
async def client_snapshot_create(cid: str, body: SnapBody, user: dict = Depends(get_current_user)):
    _client_perm(user, cid)
    pve = engine.pve_target(cid)
    if pve:
        if not SNAP_RE.match(body.name):
            raise HTTPException(400, "Snapshot-Name: Buchstabe am Anfang, danach a-z, 0-9, - und _ (max. 40)")
        cl = engine._pve_client(pve["host"])
        params = {"snapname": body.name, "description": body.notes}
        if pve["type"] == "qemu" and body.vmstate:
            params["vmstate"] = 1
        upid = await asyncio.to_thread(cl.call, "POST",
                                       f"/nodes/{pve['node']}/{pve['type']}/{pve['vmid']}/snapshot", params)
        await engine.pve_wait(pve["host"], pve["node"], upid)
    else:
        res = await agentlink.call(cid, "fs-snap-create", {}, {"name": body.name, "volume": body.volume},
                                   timeout=600)
        if not res.get("ok"):
            raise HTTPException(502, res.get("error") or "Snapshot fehlgeschlagen")
        if res.get("id"):
            store.set_fs_snapshot_name(cid, res["id"], body.name)
    db.add_audit_entry(user["username"], "backup.snapshot.create", cid, body.name)
    return {"ok": True}


class SnapActionBody(BaseModel):
    id: str
    action: str = "rollback"         # rollback | restore-path | delete
    path: str = ""                   # restore-path: Datei/Ordner im Snapshot
    dest: str = ""                   # restore-path: Ziel (leer = Originalort)


@router.post("/clients/{cid}/snapshots/action")
async def client_snapshot_action(cid: str, body: SnapActionBody, user: dict = Depends(get_current_user)):
    _client_perm(user, cid)
    pve = engine.pve_target(cid)
    if pve:
        cl = engine._pve_client(pve["host"])
        base = f"/nodes/{pve['node']}/{pve['type']}/{pve['vmid']}/snapshot/{body.id}"
        if body.action == "rollback":
            upid = await asyncio.to_thread(cl.call, "POST", base + "/rollback", {})
        elif body.action == "delete":
            upid = await asyncio.to_thread(cl.call, "DELETE", base)
        else:
            raise HTTPException(400, "Für Proxmox-Snapshots gibt es Zurücksetzen oder Löschen")
        await engine.pve_wait(pve["host"], pve["node"], upid)
    else:
        op = {"rollback": "fs-snap-rollback", "restore-path": "fs-snap-restore",
              "delete": "fs-snap-delete"}.get(body.action)
        if not op:
            raise HTTPException(400, "Unbekannte Aktion")
        res = await agentlink.call(cid, op, {}, {"id": body.id, "path": body.path, "dest": body.dest},
                                   timeout=3600)
        if not res.get("ok"):
            raise HTTPException(502, res.get("error") or "Aktion fehlgeschlagen")
        if res.get("note"):
            db.add_audit_entry(user["username"], f"backup.snapshot.{body.action}", cid, f"{body.id}: {res['note']}")
            return {"ok": True, "note": res["note"]}
    db.add_audit_entry(user["username"], f"backup.snapshot.{body.action}", cid, body.id)
    return {"ok": True}


# ================================================================== Werkzeuge fuer Agents

@router.get("/tools/restic")
async def tool_restic(os: str, arch: str, x_agent_token: str | None = Header(default=None)):
    if not x_agent_token or not hmac.compare_digest(x_agent_token, AGENT_TOKEN):
        raise HTTPException(403, "Agent-Token fehlt oder ist falsch")
    try:
        data = await asyncio.to_thread(agentlink.restic_binary, os, arch)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"restic konnte nicht geladen werden: {exc}")
    return Response(data, media_type="application/octet-stream",
                    headers={"X-Restic-Version": agentlink.RESTIC_VERSION})


@router.get("/overview")
def overview(user: dict = Depends(get_current_user)):
    """Kleine Uebersicht fuer die Startseite des Moduls."""
    cat = store.catalog()
    return {"storages": len(store.storages()), "jobs": len(store.jobs()),
            "backups": sum(len(e.get("snapshots", [])) for e in cat.values()),
            "admin": is_super_admin(user)}

