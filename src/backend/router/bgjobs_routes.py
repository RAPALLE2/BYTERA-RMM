"""
bgjobs_routes.py - REST-Zugriff auf die Hintergrund-Jobs (Core, siehe app/bgjobs.py)
----------------------------------------------------------------------------------
  GET  /api/bgjobs?kind=&key=&running=1&history=1   Jobs, die der Benutzer sehen darf
                                                    (history=1: auch beendete aus dem Verlauf)
  GET  /api/bgjobs/{id}                             ein Job inkl. Ergebnis + Log
  POST /api/bgjobs/{id}/cancel                      Abbruch anfordern

Gestartet werden Jobs von den jeweiligen Fach-Endpunkten (z.B. Netzwerk-Scan),
weil nur die wissen, was zu tun ist und welches Recht dafuer noetig ist.
"""

from fastapi import APIRouter, Depends, HTTPException

from app import bgjobs, db
from app.auth import get_current_user

router = APIRouter(prefix="/api/bgjobs", tags=["bgjobs"])


@router.get("")
def list_jobs(kind: str | None = None, key: str | None = None, running: bool = False,
              history: bool = False, limit: int = 40,
              user: dict = Depends(get_current_user)):
    jobs = [j.public() for j in bgjobs.list_jobs(kind, key) if j.visible_to(user)]
    if running:
        jobs = [j for j in jobs if j["running"]]
    elif history:
        limit = max(1, min(200, limit))
        for r in bgjobs.history(kind, key, limit=limit * 2):
            if bgjobs.row_visible_to(r, user):
                jobs.append(bgjobs._row_public(r))
        jobs.sort(key=lambda j: j["started"], reverse=True)
        jobs = [j for j in jobs if j["running"]] + [j for j in jobs if not j["running"]][:limit]
    return {"jobs": jobs}


@router.get("/{job_id}")
def get_job(job_id: str, user: dict = Depends(get_current_user)):
    job = bgjobs.get(job_id)
    if job:
        if not job.visible_to(user):
            raise HTTPException(404, "Job nicht gefunden")
        return job.public(with_result=True)
    r = bgjobs.history_get(job_id)
    if not r or not bgjobs.row_visible_to(r, user):
        raise HTTPException(404, "Job nicht gefunden (evtl. abgelaufen)")
    return bgjobs._row_public(r, with_result=True)


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: str, user: dict = Depends(get_current_user)):
    job = bgjobs.get(job_id)
    if not job or not job.visible_to(user):
        raise HTTPException(404, "Job nicht gefunden")
    if not await bgjobs.cancel(job, user):
        raise HTTPException(409, "Der Job läuft nicht mehr")
    db.add_audit_entry(user["username"], "bgjob.cancel", target=f"{job.kind}:{job.key}",
                       details=job.title)
    return job.public()
