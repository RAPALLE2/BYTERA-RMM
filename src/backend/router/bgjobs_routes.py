"""
bgjobs_routes.py - REST access to the background jobs (Core, see app/bgjobs.py)
----------------------------------------------------------------------------------
  GET  /api/bgjobs?kind=&key=&running=1&history=1   jobs the user may see
                                                    (history=1: also finished ones from the log)
  GET  /api/bgjobs/{id}                             one job incl. result + log
  POST /api/bgjobs/{id}/cancel                      request a cancel

Jobs are started by the respective domain endpoints (e.g. network scan),
because only they know what to do and which right is needed for it.
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
            raise HTTPException(404, "Job not found")
        return job.public(with_result=True)
    r = bgjobs.history_get(job_id)
    if not r or not bgjobs.row_visible_to(r, user):
        raise HTTPException(404, "Job not found (possibly expired)")
    return bgjobs._row_public(r, with_result=True)


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: str, user: dict = Depends(get_current_user)):
    job = bgjobs.get(job_id)
    if not job or not job.visible_to(user):
        raise HTTPException(404, "Job not found")
    if not await bgjobs.cancel(job, user):
        raise HTTPException(409, "The job is no longer running")
    db.add_audit_entry(user["username"], "bgjob.cancel", target=f"{job.kind}:{job.key}",
                       details=job.title)
    return job.public()
