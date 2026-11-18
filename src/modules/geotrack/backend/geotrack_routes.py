# geotrack_routes.py (module "geotrack") - location tracker
# ----------------------------------------------------------
# Permissions: use_geotrack (view locations), manage_geotrack (toggle tracking,
# clear history). Location data is sensitive - only visible with permission.
#
#   GET  /api/geo/clients                 tracked clients + last location
#   GET  /api/geo/{cid}                    status + last location of a client
#   GET  /api/geo/{cid}/history            history
#   POST /api/geo/{cid}/locate             locate now (background job)
#   PUT  /api/geo/{cid}/enabled            tracking on/off
#   DELETE /api/geo/{cid}/history          clear history
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import bgjobs, db
from app.auth import get_current_user, require_perm

from . import module as mod
from . import store

router = APIRouter(prefix="/api/geo", tags=["geotrack"])


def _see(user, cid=None):
    require_perm(user, "use_geotrack")


def _manage(user):
    require_perm(user, "manage_geotrack")


def _loc(l):
    if not l:
        return None
    return {k: l[k] for k in ("id", "ts", "lat", "lon", "accuracy", "source", "city", "country", "label")}


def _online():
    try:
        from app.sockets import state
        return set(state.client_id_to_sid.keys())
    except Exception:
        return set()


@router.get("/clients")
def clients(user: dict = Depends(get_current_user)):
    _see(user)
    out = []
    for row in store.tracked_clients():
        c = db.get_client(row["client_id"])
        if not c:
            continue
        out.append({"client_id": row["client_id"], "hostname": c.get("hostname"),
                    "last_ts": row["last_ts"], "last_error": row["last_error"],
                    "location": _loc(store.last_location(row["client_id"]))})
    return {"clients": out}


@router.get("/{cid}")
def status(cid: str, user: dict = Depends(get_current_user)):
    _see(user, cid)
    c = db.get_client(cid)
    if not c:
        raise HTTPException(404, "Client not found")
    row = store.get_client(cid)
    enabled = store.is_enabled(cid, c.get("detected_kind") or c.get("device_type") or "")
    return {"client_id": cid, "enabled": enabled, "set_by_user": bool(row and row["set_by_user"]),
            "last_ts": row["last_ts"] if row else 0, "last_error": row["last_error"] if row else "",
            "online": cid in _online(), "location": _loc(store.last_location(cid))}


@router.get("/{cid}/history")
def history(cid: str, limit: int = 500, user: dict = Depends(get_current_user)):
    _see(user, cid)
    return {"history": [_loc(l) for l in store.history(cid, limit)]}


@router.put("/{cid}/enabled")
def set_enabled(cid: str, body: dict, user: dict = Depends(get_current_user)):
    _manage(user)
    if not db.get_client(cid):
        raise HTTPException(404, "Client not found")
    en = bool(body.get("enabled"))
    store.set_enabled(cid, en)
    db.add_audit_entry(user["username"], "geo.tracking", target=cid, details="on" if en else "off")
    return {"ok": True, "enabled": en}


@router.delete("/{cid}/history")
def clear(cid: str, user: dict = Depends(get_current_user)):
    _manage(user)
    store.clear_history(cid)
    db.add_audit_entry(user["username"], "geo.clear_history", target=cid)
    return {"ok": True}


@router.post("/{cid}/locate")
async def locate(cid: str, background: bool = False, user: dict = Depends(get_current_user)):
    _see(user, cid)
    c = db.get_client(cid)
    if not c:
        raise HTTPException(404, "Client not found")
    if cid not in _online():
        raise HTTPException(409, "Client is offline")

    async def work(job):
        job.log(f"Locating {c.get('hostname') or cid} …")
        try:
            loc = await mod.locate_now(cid, c)
        except Exception as exc:
            raise RuntimeError(str(exc))
        job.log(f"Location: {loc.get('city') or ''} ({loc['lat']:.4f}, {loc['lon']:.4f}) · source {loc['source']} · ±{int(loc.get('accuracy') or 0)} m")
        return {"location": _loc(loc)}

    return await bgjobs.run("geo.locate", cid, title=f"Locate: {c.get('hostname') or cid}",
                            user=user, perm="use_geotrack", work=work, background=background)
