"""
software_routes.py (Modul "software") - Pakete verwalten und ausrollen.

Dateien liegen unter storage/dist/software/<paket>/<datei> und werden den
Agents ueber ihren authentifizierten Backend-Kanal ausgeliefert. Rollout an
mehrere Clients laeuft als Hintergrund-Job: online = sofort installieren,
offline = Eintrag in der Offline-Warteschlange (laeuft beim naechsten
Online-Gehen, siehe app/sockets.py).
"""

from __future__ import annotations

import re
import shutil
import time
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from app import bgjobs, db
from app.auth import get_current_user, require_perm

from rmm_mod_software import rollout
from rmm_mod_software.rollout import FILES_DIR, load_packages, save_packages

router = APIRouter(prefix="/api/software", tags=["software"])

NAME_RE = re.compile(r"[^A-Za-z0-9._ äöüÄÖÜß()+-]")


def _perm(user):
    require_perm(user, "use_terminal")   # ausrollen = Befehle ausfuehren


@router.get("/packages")
def list_packages(user: dict = Depends(get_current_user)):
    _perm(user)
    data = load_packages()["packages"]
    out = [{"id": pid, **p} for pid, p in data.items()]
    out.sort(key=lambda p: (p.get("name") or "").lower())
    return out


class PackageBody(BaseModel):
    name: str
    commands: dict = {}       # {"win": "...", "linux": "...", "all": "..."}
    note: str = ""


@router.post("/packages")
def create_package(body: PackageBody, user: dict = Depends(get_current_user)):
    _perm(user)
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "Name fehlt")
    data = load_packages()
    pid = uuid.uuid4().hex[:10]
    data["packages"][pid] = {"name": name[:120], "files": [],
                             "commands": {k: str(v)[:1000] for k, v in (body.commands or {}).items()
                                          if k in ("win", "linux", "all")},
                             "note": (body.note or "")[:500],
                             "created_by": user.get("username"),
                             "created_at": int(time.time() * 1000)}
    save_packages(data)
    db.add_audit_entry(user.get("username"), "software.package.create", None, name)
    return {"id": pid, **data["packages"][pid]}


@router.put("/packages/{pid}")
def update_package(pid: str, body: PackageBody, user: dict = Depends(get_current_user)):
    _perm(user)
    data = load_packages()
    pkg = data["packages"].get(pid)
    if not pkg:
        raise HTTPException(404, "Paket nicht gefunden")
    pkg["name"] = (body.name or pkg["name"]).strip()[:120]
    pkg["commands"] = {k: str(v)[:1000] for k, v in (body.commands or {}).items()
                       if k in ("win", "linux", "all")}
    pkg["note"] = (body.note or "")[:500]
    save_packages(data)
    return {"id": pid, **pkg}


@router.delete("/packages/{pid}")
def delete_package(pid: str, user: dict = Depends(get_current_user)):
    _perm(user)
    data = load_packages()
    if data["packages"].pop(pid, None) is not None:
        save_packages(data)
        shutil.rmtree(FILES_DIR / pid, ignore_errors=True)
        db.add_audit_entry(user.get("username"), "software.package.delete", None, pid)
    return {"ok": True}


@router.post("/packages/{pid}/files")
async def upload_file(pid: str, file: UploadFile = File(...),
                      user: dict = Depends(get_current_user)):
    _perm(user)
    data = load_packages()
    pkg = data["packages"].get(pid)
    if not pkg:
        raise HTTPException(404, "Paket nicht gefunden")
    name = NAME_RE.sub("_", (file.filename or "datei").rsplit("/", 1)[-1])[:120]
    if not name:
        raise HTTPException(400, "Ungültiger Dateiname")
    target_dir = FILES_DIR / pid
    target_dir.mkdir(parents=True, exist_ok=True)
    size = 0
    with open(target_dir / name, "wb") as fh:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > 4 * 1024 * 1024 * 1024:
                fh.close()
                (target_dir / name).unlink(missing_ok=True)
                raise HTTPException(413, "Datei zu groß (max. 4 GB)")
            fh.write(chunk)
    pkg["files"] = [f for f in pkg.get("files", []) if f["name"] != name]
    pkg["files"].append({"name": name, "size": size})
    save_packages(data)
    return {"ok": True, "name": name, "size": size}


@router.delete("/packages/{pid}/files/{name}")
def delete_file(pid: str, name: str, user: dict = Depends(get_current_user)):
    _perm(user)
    data = load_packages()
    pkg = data["packages"].get(pid)
    if not pkg:
        raise HTTPException(404, "Paket nicht gefunden")
    safe = NAME_RE.sub("_", name)[:120]
    (FILES_DIR / pid / safe).unlink(missing_ok=True)
    pkg["files"] = [f for f in pkg.get("files", []) if f["name"] != safe]
    save_packages(data)
    return {"ok": True}


class DeployBody(BaseModel):
    package_id: str
    clients: list[str]
    queue_offline: bool = True     # Offline-Clients in die Warteschlange legen


@router.post("/deploy")
async def deploy(body: DeployBody, background: bool = False,
                 user: dict = Depends(get_current_user)):
    _perm(user)
    pkg = load_packages()["packages"].get(body.package_id or "")
    if not pkg:
        raise HTTPException(404, "Paket nicht gefunden")
    clients = list(dict.fromkeys(body.clients or []))
    if not clients:
        raise HTTPException(400, "Keine Clients ausgewählt")

    async def work(job):
        from app.sockets import state
        results = []
        for cid in clients:
            client = await db.call(db.get_client, cid)
            label = (client or {}).get("short_name") or (client or {}).get("hostname") or cid
            if client is None:
                results.append({"client": label, "status": "failed", "info": "unbekannt"})
                job.log(f"❌ {label}: Client unbekannt")
                continue
            if state.is_online(cid):
                job.log(f"▶ {label}: installiere {pkg['name']} …")
                job.detail = f"{label}: {pkg['name']}"
                res = await rollout.install(cid, body.package_id)
                if res.get("ok"):
                    results.append({"client": label, "status": "done", "info": ""})
                    job.log(f"✅ {label}: fertig")
                else:
                    results.append({"client": label, "status": "failed",
                                    "info": str(res.get("error") or "")})
                    job.log(f"❌ {label}: {res.get('error')} "
                            f"{str(res.get('output') or '')[:300]}")
            elif body.queue_offline:
                await db.call(db.queue_add, cid, "software",
                              f"Software installieren: {pkg['name']}",
                              {"package_id": body.package_id}, user.get("username"))
                results.append({"client": label, "status": "queued", "info": ""})
                job.log(f"⏳ {label}: offline - in Warteschlange gelegt "
                        "(installiert beim nächsten Online-Gehen)")
            else:
                results.append({"client": label, "status": "skipped", "info": "offline"})
                job.log(f"⤼ {label}: offline - übersprungen")
        db.add_audit_entry(user.get("username"), "software.deploy", None,
                           f"{pkg['name']} -> {len(clients)} Clients")
        return {"results": results}

    return await bgjobs.run("software", f"deploy-{body.package_id}",
                            title=f"Rollout: {pkg['name']} ({len(clients)} Clients)",
                            user=user, work=work, background=background)
