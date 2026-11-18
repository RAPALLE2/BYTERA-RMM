# geotrack_routes.py (Modul "geotrack") - Standort-Tracker
# ---------------------------------------------------------
# Rechte: use_geotrack (Standorte ansehen), manage_geotrack (Tracking an/aus,
# Verlauf löschen). Standortbezogene Daten sind sensibel - nur mit Recht sichtbar.
#
#   GET  /api/geo/clients                 getrackte Clients + letzter Standort
#   GET  /api/geo/{cid}                    Status + letzter Standort eines Clients
#   GET  /api/geo/{cid}/history            Verlauf
#   POST /api/geo/{cid}/locate             jetzt orten (Backend-Job)
#   PUT  /api/geo/{cid}/enabled            Tracking an/aus
#   DELETE /api/geo/{cid}/history          Verlauf löschen
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import bgjobs, db
from app.auth import get_current_user, require_perm, user_has_permission

from . import module as mod
from . import store

router = APIRouter(prefix="/api/geo", tags=["geotrack"])


def _see(user, cid=None):
    require_perm(user, "use_geotrack")
    if cid and not (user_has_permission(user, "use_geotrack", cid) or user_has_permission(user, "c_view", cid)
                    or user_has_permission(user, "c_power", cid)):
        # globales Recht reicht; Client-Recht nur zusätzliche Absicherung
        if not user_has_permission(user, "use_geotrack"):
            raise HTTPException(403, "Keine Berechtigung")


def _manage(user):
    require_perm(user, "manage_geotrack")


def _loc(l):
    if not l:
        return None
    return {k: l[k] for k in ("id", "ts", "lat", "lon", "accuracy", "source", "city", "country", "label")}


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
        raise HTTPException(404, "Client nicht gefunden")
    row = store.get_client(cid)
    enabled = store.is_enabled(cid, c.get("detected_kind") or c.get("device_type") or "")
    return {"client_id": cid, "enabled": enabled, "set_by_user": bool(row and row["set_by_user"]),
            "last_ts": row["last_ts"] if row else 0, "last_error": row["last_error"] if row else "",
            "online": cid in _online(), "location": _loc(store.last_location(cid))}


def _online():
    try:
        from app.sockets import state
        return set(state.client_id_to_sid.keys())
    except Exception:
        return set()


@router.get("/{cid}/history")
def history(cid: str, limit: int = 500, user: dict = Depends(get_current_user)):
    _see(user, cid)
    return {"history": [_loc(l) for l in store.history(cid, limit)]}


@router.put("/{cid}/enabled")
def set_enabled(cid: str, body: dict, user: dict = Depends(get_current_user)):
    _manage(user)
    if not db.get_client(cid):
        raise HTTPException(404, "Client nicht gefunden")
    en = bool(body.get("enabled"))
    store.set_enabled(cid, en)
    db.add_audit_entry(user["username"], "geo.tracking", target=cid, details="an" if en else "aus")
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
        raise HTTPException(404, "Client nicht gefunden")
    if cid not in _online():
        raise HTTPException(409, "Client ist offline")

    async def work(job):
        job.log(f"Orte {c.get('hostname') or cid} …")
        try:
            loc = await mod.locate_now(cid, c)
        except Exception as exc:
            raise RuntimeError(str(exc))
        job.log(f"Standort: {loc.get('city') or ''} ({loc['lat']:.4f}, {loc['lon']:.4f}) · Quelle {loc['source']} · ±{int(loc.get('accuracy') or 0)} m")
        return {"location": _loc(loc)}

    return await bgjobs.run("geo.locate", cid, title=f"Standort orten: {c.get('hostname') or cid}",
                            user=user, perm="use_geotrack", work=work, background=background)
