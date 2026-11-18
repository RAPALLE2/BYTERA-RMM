"""
routers/privacy_routes.py
-------------------------
Data subject rights and retention periods as an API.

Two levels:
  * Self-service (every logged-in user): view, export your own data, request
    erasure. Art. 15, 20 and 17 require that this is possible directly.
  * Administration (permission 'manage_privacy'): set periods, view the
    inventory, export others' data, handle erasure requests, perform erasure.

Every access to OTHERS' data lands in the audit log - whoever pulls access
about others must be traceable.
"""

import json
import time

import asyncio
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from app import db
from app import bgjobs
from rmm_mod_privacy import privacy
from app.auth import get_current_user, require_perm

router = APIRouter(prefix="/api/privacy", tags=["privacy"])


# ==================================================================
# SELBSTBEDIENUNG - eigene Daten
# ==================================================================

@router.get("/me")
def my_data_overview(user: dict = Depends(get_current_user)):
    """Quick overview: which kinds of data exist about me."""
    data = privacy.export_user_data(user["id"])
    return {
        "person": data.get("person", {}),
        "categories": [{"label": k, "count": len(v)}
                       for k, v in (data.get("data") or {}).items()],
    }


@router.get("/me/export")
def export_my_data(user: dict = Depends(get_current_user)):
    """Full access report as a JSON download (Art. 15 / Art. 20)."""
    data = privacy.export_user_data(user["id"])
    db.add_audit_entry(user.get("username"), "privacy.self_export",
                       details="Eigene Daten exportiert")
    name = f"auskunft-{user.get('username', 'benutzer')}-{time.strftime('%Y%m%d')}.json"
    return Response(
        content=json.dumps(data, ensure_ascii=False, indent=2, default=str),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{name}"'})


class ErasureBody(BaseModel):
    # 'account' = gesamtes Konto, 'content' = nur eigene Inhalte
    kind: str = "account"
    reason: str = ""


@router.post("/me/erasure-request")
def request_erasure(body: ErasureBody, user: dict = Depends(get_current_user)):
    """
    Request erasure (Art. 17). Reviewed by an admin, because Art. 17(3) has
    exceptions (e.g. evidence duties) and erasing an active account has
    operational consequences.
    """
    if body.kind not in ("account", "content"):
        raise HTTPException(400, "Unknown kind of request")
    open_already = [r for r in privacy.list_erasure_requests("open")
                    if r["user_id"] == user["id"]]
    if open_already:
        raise HTTPException(400, "Es liegt bereits ein offener Antrag vor")
    rid = privacy.create_erasure_request(user, body.kind, body.reason or "")
    return {"id": rid, "status": "open",
            "hinweis": "Der Antrag wird innerhalb eines Monats bearbeitet (Art. 12 Abs. 3 DSGVO)."}


@router.get("/me/erasure-request")
def my_erasure_requests(user: dict = Depends(get_current_user)):
    return [r for r in privacy.list_erasure_requests() if r["user_id"] == user["id"]]


# ==================================================================
# VERWALTUNG
# ==================================================================

@router.get("/report")
def data_report(user: dict = Depends(get_current_user)):
    """Inventory overview - basis for the record under Art. 30."""
    require_perm(user, "manage_privacy")
    return privacy.report()


class RetentionBody(BaseModel):
    values: dict = {}


@router.put("/retention")
def set_retention(body: RetentionBody, user: dict = Depends(get_current_user)):
    """Aufbewahrungsfristen setzen. 0 = unbegrenzt (bewusste Entscheidung)."""
    require_perm(user, "manage_privacy")
    known = {r["key"] for r in privacy.RETENTION}
    changed = []
    for key, value in (body.values or {}).items():
        if key not in known:
            continue
        try:
            v = max(0, int(value))
        except (TypeError, ValueError):
            raise HTTPException(400, f"Invalid value for {key}")
        if str(db.get_setting(key, "")) != str(v):
            db.set_setting(key, v)
            changed.append(f"{key}={v}")
    if changed:
        db.add_audit_entry(user.get("username"), "privacy.retention_changed",
                           details=", ".join(changed))
    return {"ok": True, "changed": changed}


@router.post("/purge")
async def run_purge(background: bool = False, user: dict = Depends(get_current_user)):
    """As a BACKEND JOB (app/bgjobs.py): reloading/closing aborts nothing,
    progress + result in the task view. ?background=1 -> job immediately."""
    require_perm(user, "manage_privacy")
    async def _work(job):
        job.update(phase='privacy.task', detail="Privacy: apply retention periods (purge run)")
        return await asyncio.to_thread(_run_purge_impl, user=user)
    return await bgjobs.run('privacy.task', 'purge', title="Privacy: apply retention periods (purge run)", user=user, work=_work,
                            background=background, perm='manage_privacy')


def _run_purge_impl(user: dict = Depends(get_current_user)):
    """Apply retention periods immediately instead of waiting for the nightly job."""
    require_perm(user, "manage_privacy")
    result = privacy.purge()
    db.add_audit_entry(user.get("username"), "privacy.purge_manual",
                       details=str(result))
    return result


@router.get("/users/{user_id}/export")
def export_user(user_id: str, user: dict = Depends(get_current_user)):
    """Provide access for another person (e.g. on a written request)."""
    require_perm(user, "manage_privacy")
    target = db.get_user_by_id(user_id)
    if not target:
        raise HTTPException(404, "User not found")
    data = privacy.export_user_data(user_id)
    db.add_audit_entry(user.get("username"), "privacy.export_other",
                       target=target.get("username"),
                       details="Access report about another person created")
    name = f"access-{target.get('username')}-{time.strftime('%Y%m%d')}.json"
    return Response(
        content=json.dumps(data, ensure_ascii=False, indent=2, default=str),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{name}"'})


class EraseBody(BaseModel):
    # 'anonymize' = Nachweise bleiben ohne Personenbezug, 'hard' = alles weg
    mode: str = "anonymize"
    confirm_username: str = ""


@router.post("/users/{user_id}/erase")
async def erase_user(user_id: str, body: EraseBody,
                     background: bool = False, user: dict = Depends(get_current_user)):
    """As a BACKEND JOB (app/bgjobs.py): reloading/closing aborts nothing,
    progress + result in the task view. ?background=1 -> job immediately."""
    require_perm(user, "manage_privacy")
    async def _work(job):
        job.update(phase='privacy.task', detail="Privacy: erase user data (Art. 17)")
        return await asyncio.to_thread(_erase_user_impl, user_id=user_id, body=body, user=user)
    return await bgjobs.run('privacy.task', user_id, title="Privacy: erase user data (Art. 17)", user=user, work=_work,
                            background=background, perm='manage_privacy')


def _erase_user_impl(user_id: str, body: EraseBody,
                     user: dict = Depends(get_current_user)):
    """
    Perform erasure (Art. 17). Irreversible, so the username must be sent along
    for confirmation.
    """
    require_perm(user, "manage_privacy")
    target = db.get_user_by_id(user_id)
    if not target:
        raise HTTPException(404, "User not found")
    if target["id"] == user["id"]:
        raise HTTPException(400, "Your own account cannot be deleted here")
    if body.confirm_username != target.get("username"):
        raise HTTPException(400, "Confirmation username does not match")
    if body.mode not in ("anonymize", "hard"):
        raise HTTPException(400, "Unknown mode")

    result = privacy.erase_user(user_id, body.mode)
    # Mark this person's open requests as done.
    db._conn.execute(
        "UPDATE erasure_requests SET status = 'done', handled_at = ?, handled_by = ?"
        " WHERE user_id = ? AND status = 'open'",
        (int(time.time() * 1000), user.get("username", ""), user_id))
    db._conn.commit()
    db.add_audit_entry(user.get("username"), "privacy.erase_executed",
                       details=f"Mode {body.mode}, {len(result)} areas")
    return result


@router.get("/erasure-requests")
def erasure_requests(status: str | None = None,
                           user: dict = Depends(get_current_user)):
    require_perm(user, "manage_privacy")
    return privacy.list_erasure_requests(status)


class ResolveBody(BaseModel):
    status: str = "rejected"     # 'rejected' | 'done'
    note: str = ""


@router.post("/erasure-requests/{request_id}/resolve")
def resolve_request(request_id: str, body: ResolveBody,
                          user: dict = Depends(get_current_user)):
    """
    Close a request. A rejection must be justified - Art. 12(4) requires that
    data subjects learn the reasons.
    """
    require_perm(user, "manage_privacy")
    if body.status not in ("rejected", "done"):
        raise HTTPException(400, "Invalid status")
    if body.status == "rejected" and not body.note.strip():
        raise HTTPException(400, "A rejection must be justified (Art. 12(4) GDPR)")
    row = db._conn.execute("SELECT * FROM erasure_requests WHERE id = ?",
                           (request_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Request not found")
    db._conn.execute(
        "UPDATE erasure_requests SET status = ?, handled_at = ?, handled_by = ?,"
        " note = ? WHERE id = ?",
        (body.status, int(time.time() * 1000), user.get("username", ""),
         body.note.strip()[:2000], request_id))
    db._conn.commit()
    db.add_audit_entry(user.get("username"), "privacy.erasure_resolved",
                       target=row["username"], details=body.status)
    return {"ok": True}
