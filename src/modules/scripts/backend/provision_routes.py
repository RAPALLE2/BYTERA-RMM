# provision_routes.py (Modul "scripts", Teilfunktion "provision") - Clients einrichten
# ------------------------------------------------------------------------------------
#   GET    /api/scripts/provision/catalog           Optionen fuer die Oberflaeche + Standardwerte
#   GET    /api/scripts/provision/presets           alle Presets
#   POST   /api/scripts/provision/presets           Preset anlegen   (create_scripts)
#   PUT    /api/scripts/provision/presets/{id}      Preset aendern   (create_scripts)
#   DELETE /api/scripts/provision/presets/{id}      Preset loeschen  (create_scripts)
#   POST   /api/scripts/provision/render            Skript-Vorschau  {os, config}
#   GET    /api/scripts/provision/presets/{id}/file Skript herunterladen (zum manuellen Ausfuehren)
#   POST   /api/scripts/provision/run               ausrollen (Backend-Job; pro Client c_terminal)
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from app import bgjobs, db
from app.auth import get_current_user, require_perm, user_has_permission

from . import provision as pv

router = APIRouter(prefix="/api/scripts/provision", tags=["scripts-provision"])


def _host(cid: str) -> str:
    return (db.get_client(cid) or {}).get("hostname") or cid[:8]


@router.get("/catalog")
def get_catalog(user: dict = Depends(get_current_user)):
    require_perm(user, "use_scripts")
    return {"catalog": pv.catalog(), "defaults": {"windows": pv.defaults("windows"), "linux": pv.defaults("linux")}}


@router.get("/presets")
def get_presets(user: dict = Depends(get_current_user)):
    require_perm(user, "use_scripts")
    return {"presets": pv.list_presets()}


class PresetBody(BaseModel):
    name: str
    os: str
    description: str = ""
    config: dict = {}


def _save(pid, body, user):
    require_perm(user, "create_scripts")
    try:
        p = pv.save_preset(pid, body.name, body.os, body.description, body.config, user["username"])
    except pv.ProvisionError as exc:
        raise HTTPException(400, str(exc))
    db.add_audit_entry(user["username"], "provision.preset_saved", target=p["id"], details=p["name"])
    return p


@router.post("/presets")
def create_preset(body: PresetBody, user: dict = Depends(get_current_user)):
    return _save(None, body, user)


@router.put("/presets/{pid}")
def update_preset(pid: str, body: PresetBody, user: dict = Depends(get_current_user)):
    return _save(pid, body, user)


@router.delete("/presets/{pid}")
def delete_preset(pid: str, user: dict = Depends(get_current_user)):
    require_perm(user, "create_scripts")
    pv.delete_preset(pid)
    db.add_audit_entry(user["username"], "provision.preset_deleted", target=pid)
    return {"ok": True}


class RenderBody(BaseModel):
    os: str
    config: dict = {}


@router.post("/render")
def render(body: RenderBody, user: dict = Depends(get_current_user)):
    require_perm(user, "use_scripts")
    try:
        return {"script": pv.render(body.os, body.config)}
    except pv.ProvisionError as exc:
        raise HTTPException(400, str(exc))


@router.get("/presets/{pid}/file")
def download(pid: str, user: dict = Depends(get_current_user)):
    require_perm(user, "use_scripts")
    p = pv.get_preset(pid)
    if not p:
        raise HTTPException(404, "Preset nicht gefunden")
    script = pv.render(p["os"], p["config"])
    win = p["os"] == "windows"
    data = (b"\xef\xbb\xbf" + script.encode("utf-8")) if win else script.encode("utf-8")
    fname = "bytera-provision-" + "".join(ch if ch.isalnum() else "-" for ch in p["name"]).strip("-")[:40] \
        + (".ps1" if win else ".sh")
    return Response(data, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


class RunBody(BaseModel):
    client_ids: list[str]
    preset_windows: str | None = None      # Preset fuer Windows-Clients
    preset_linux: str | None = None        # Preset fuer Linux-Clients
    queue_offline: bool = True             # Offline-Clients in die Warteschlange


@router.post("/run")
async def run(body: RunBody, background: bool = False, user: dict = Depends(get_current_user)):
    require_perm(user, "use_scripts")
    presets = {"windows": pv.get_preset(body.preset_windows) if body.preset_windows else None,
               "linux": pv.get_preset(body.preset_linux) if body.preset_linux else None}
    if not presets["windows"] and not presets["linux"]:
        raise HTTPException(400, "Kein Preset gewählt")
    for k, p in presets.items():
        if p and p["os"] != k:
            raise HTTPException(400, f"Preset „{p['name']}“ ist nicht für {k}")
    from app.sockets import state
    targets, skipped, queued = [], [], []
    for cid in dict.fromkeys(body.client_ids):
        c = db.get_client(cid)
        if not c:
            continue
        if not user_has_permission(user, "c_terminal", cid):
            skipped.append(f"{c.get('hostname') or cid}: keine Berechtigung")
            continue
        os_ = pv.client_os(c)
        p = presets[os_]
        if not p:
            skipped.append(f"{c.get('hostname')}: kein {os_}-Preset gewählt")
            continue
        if not state.is_online(cid):
            if body.queue_offline:
                db.queue_add(cid, "mod:scripts:provision", f"Einrichtung: {p['name']}",
                             {"os": os_, "config": p["config"], "preset": p["id"]}, created_by=user["username"])
                queued.append(c.get("hostname") or cid)
            else:
                skipped.append(f"{c.get('hostname')}: offline")
            continue
        targets.append((cid, os_, p))
    if not targets:
        msg = []
        if queued:
            msg.append(f"In Warteschlange (offline): {', '.join(queued)}")
        if skipped:
            msg.append("Übersprungen: " + "; ".join(skipped))
        if queued:
            return {"result": {"queued": queued, "skipped": skipped, "results": {}}, "message": " · ".join(msg)}
        raise HTTPException(400, " · ".join(msg) or "Keine passenden Clients")
    names = ", ".join(sorted({p["name"] for _, _, p in targets}))
    db.add_audit_entry(user["username"], "provision.run", target=",".join(t[0] for t in targets), details=names)

    async def work(job):
        total = len(targets)
        stat = {"done": 0, "ok": 0, "failed": 0}
        steps: dict = {}
        for q in queued:
            job.log(f"{q}: offline – in die Warteschlange gestellt")
        for s in skipped:
            job.log(f"übersprungen: {s}")
        sem = asyncio.Semaphore(20)

        def refresh():
            fr = [steps.get(cid, 0.0) for cid, _, _ in targets]
            job.update(pct=sum(fr) / max(1, total) * 100,
                       detail=f"{stat['done']}/{total} fertig · {stat['ok']} ok · {stat['failed']} Fehler", **stat)

        async def one(cid, os_, p):
            host = _host(cid)
            async with sem:
                script = pv.render(os_, p["config"])
                job.log(f"{host}: starte „{p['name']}“")

                def prog(st):
                    steps[cid] = min(0.99, (st.get("n") or 0) / max(1, st.get("total") or 1))
                    refresh()
                res = await pv.run_on_client(cid, os_, script, lambda ln: job.log(f"{host}: {ln}"),
                                             progress=prog, cancelled=lambda: job.cancelled)
            stat["done"] += 1
            steps[cid] = 1.0
            if res.get("ok"):
                stat["ok"] += 1
                job.log(f"{host}: ✓ fertig ({res.get('errors', 0)} Warnungen){' – startet neu' if res.get('reboot') else ''}")
            else:
                stat["failed"] += 1
                job.log(f"{host}: ✗ {res.get('error')}")
            refresh()
            return cid, res

        job.update(phase="provision", pct=0, detail=f"0/{total} fertig", total=total, **stat)
        results = dict(await asyncio.gather(*(one(*t) for t in targets)))
        return {"results": results, "queued": queued, "skipped": skipped}

    return await bgjobs.run("scripts.provision", None, title=f"Einrichtung ({names}) auf {len(targets)} Client(s)",
                            user=user, perm="use_scripts", work=work, background=background,
                            meta={"clients": [t[0] for t in targets]})
