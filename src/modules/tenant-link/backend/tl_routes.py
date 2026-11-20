# tl_routes.py (module "tenant-link") - counterpart on the TENANT backend
# ------------------------------------------------------------------------
# Lets an approved main RMM embed this backend and open it via auto-login.
# Auto-login itself runs through the normal login API (every user stores their
# own credentials in the main RMM); here only the trust relationship is managed:
#   GET  /api/tenant-link/info                confirms embeddability + display name
#   GET  /api/tenant-link/admin/pairings      list pairings (admin)
#   POST /api/tenant-link/admin/pairings      issue a one-time pairing code (admin)
#   POST /api/tenant-link/admin/pairings/{id}/approve   approve a request (admin)
#   DELETE /api/tenant-link/admin/pairings/{id}         revoke/remove (admin)
#   POST /api/tenant-link/pair/claim          a main RMM redeems a code -> pair_key
#   POST /api/tenant-link/pair/request        a main RMM asks for pairing (needs approval)
#   GET  /api/tenant-link/status              the paired main RMM checks its link (pair_key)
#
# Pairing with confirmation: either the tenant admin issues a code up front
# ("code" mode) or the admin approves an incoming request ("request" mode).
# Active pairings drive the frame-ancestors header (see module.py).
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from app import db
from app.auth import get_current_user, require_perm

from . import store

router = APIRouter(prefix="/api/tenant-link", tags=["tenant-link"])

ADMIN_PERM = "manage_modules"


def _branding() -> str:
    return db.get_setting("branding_name") or db.get_setting("server_name") or "RMM"


@router.get("/info")
def info(user: dict = Depends(get_current_user)):
    return {"ok": True, "embeddable": True, "name": _branding(), "user": user["username"]}


# ====================================================== administration
@router.get("/admin/pairings")
def admin_list(user: dict = Depends(get_current_user)):
    require_perm(user, ADMIN_PERM)
    return {"name": _branding(),
            "frame_ancestors": store.active_origins(),
            "pairings": [store.public(p) for p in store.list_pairings()]}


class CodeBody(BaseModel):
    name: str = ""


@router.post("/admin/pairings")
def admin_create_code(body: CodeBody, user: dict = Depends(get_current_user)):
    require_perm(user, ADMIN_PERM)
    p = store.create_code(body.name, created_by=user["username"])
    db.add_audit_entry(user["username"], "tenant_link.code_issued", details=p.get("name", ""))
    return {"ok": True, "pairing": store.public(p)}


@router.post("/admin/pairings/{pid}/approve")
def admin_approve(pid: str, user: dict = Depends(get_current_user)):
    require_perm(user, ADMIN_PERM)
    p = store.set_status(pid, "active")
    if not p:
        raise HTTPException(404, "Pairing not found")
    db.add_audit_entry(user["username"], "tenant_link.approved", details=p.get("name", ""))
    return {"ok": True, "pairing": store.public(p)}


@router.post("/admin/pairings/{pid}/revoke")
def admin_revoke(pid: str, user: dict = Depends(get_current_user)):
    require_perm(user, ADMIN_PERM)
    p = store.set_status(pid, "revoked")
    if not p:
        raise HTTPException(404, "Pairing not found")
    db.add_audit_entry(user["username"], "tenant_link.revoked", details=p.get("name", ""))
    return {"ok": True, "pairing": store.public(p)}


@router.delete("/admin/pairings/{pid}")
def admin_delete(pid: str, user: dict = Depends(get_current_user)):
    require_perm(user, ADMIN_PERM)
    p = store.get(pid)
    store.delete(pid)
    db.add_audit_entry(user["username"], "tenant_link.deleted", details=(p or {}).get("name", ""))
    return {"ok": True}


# ====================================================== pairing (called by a main RMM)
class ClaimBody(BaseModel):
    code: str
    name: str = ""
    parent_url: str = ""


@router.post("/pair/claim")
def pair_claim(body: ClaimBody):
    """A main RMM redeems a one-time code. No user auth - the code is the secret."""
    res = store.claim_code(body.code, body.name, body.parent_url)
    if not res:
        raise HTTPException(400, "Invalid or expired pairing code")
    return {"ok": True, "pair_key": res["pair_key"], "name": _branding(),
            "pairing_id": res["pairing"]["id"], "status": "active"}


class RequestBody(BaseModel):
    name: str = ""
    parent_url: str = ""


@router.post("/pair/request")
def pair_request(body: RequestBody):
    """A main RMM asks for a pairing. Stays pending until an admin approves it."""
    res = store.create_request(body.name, body.parent_url)
    return {"ok": True, "pair_key": res["pair_key"], "name": _branding(),
            "pairing_id": res["pairing"]["id"], "status": "pending"}


@router.get("/status")
def pair_status(x_tenant_link_key: str = Header(default="")):
    """The paired main RMM checks whether its link is still active (by pair_key)."""
    p = store.find_by_key(x_tenant_link_key)
    if not p:
        raise HTTPException(401, "Unknown pairing key")
    store.touch(p["id"])
    return {"ok": True, "status": p.get("status", "pending"),
            "embeddable": p.get("status") == "active", "name": _branding()}
