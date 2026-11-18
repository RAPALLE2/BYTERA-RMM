"""
usbguard_routes.py (module "usbguard") - decisions and trust list.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from app import db
from app.auth import get_current_user, is_super_admin, require_perm

from rmm_mod_usbguard import guard, trust

router = APIRouter(prefix="/api/usbguard", tags=["usbguard"])


def _admin(user: dict) -> None:
    if not is_super_admin(user):
        raise HTTPException(403, "Only admins may change the USB trust list")


class DecideBody(BaseModel):
    client_id: str
    dev: str
    allow: bool


@router.post("/decide")
async def usb_decide(body: DecideBody, user: dict = Depends(get_current_user)):
    # Allowing/blocking is an action on the device -> same permission as terminal.
    require_perm(user, "use_terminal", body.client_id)
    ok = await guard.decide(body.client_id, body.dev, body.allow, user.get("username"))
    if not ok:
        raise HTTPException(409, "Client ist offline - Entscheidung konnte nicht zugestellt werden")
    return {"ok": True}


@router.get("/trust")
def trust_list(user: dict = Depends(get_current_user)):
    _admin(user)
    return trust.list_all()


class IdBody(BaseModel):
    label: str = ""
    vid: str
    pid: str = ""
    serial: str = ""


@router.post("/trust/ids")
async def trust_add_id(body: IdBody, user: dict = Depends(get_current_user)):
    _admin(user)
    try:
        entry = trust.add_id(body.label, body.vid, body.pid, body.serial, user.get("username"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    db.add_audit_entry(user.get("username"), "usbguard.trust.add", None,
                       f"{entry['label']} {entry['vid']}:{entry['pid'] or '*'} {entry['serial']}")
    await guard.broadcast_config()
    return entry


@router.delete("/trust/ids/{entry_id}")
async def trust_remove_id(entry_id: str, user: dict = Depends(get_current_user)):
    _admin(user)
    trust.remove_id(entry_id)
    db.add_audit_entry(user.get("username"), "usbguard.trust.remove", None, entry_id)
    await guard.broadcast_config()
    return {"ok": True}


class KeyBody(BaseModel):
    label: str = ""
    vid: str
    pid: str
    serial: str


@router.post("/trust/keys")
def trust_issue_key(body: KeyBody, user: dict = Depends(get_current_user)):
    """Generate a signed key file for ONE stick and deliver it as a download."""
    _admin(user)
    try:
        meta, content = trust.issue_key(body.label, body.vid, body.pid, body.serial,
                                        user.get("username"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    db.add_audit_entry(user.get("username"), "usbguard.key.issue", None,
                       f"{meta['label']} ({meta['vid']}:{meta['pid']} {meta['serial']})")
    return Response(content, media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename=".bytera-usbkey"',
                             "X-Key-Id": meta["kid"]})


@router.post("/trust/keys/{kid}/revoke")
async def trust_revoke_key(kid: str, user: dict = Depends(get_current_user)):
    _admin(user)
    trust.revoke_key(kid)
    db.add_audit_entry(user.get("username"), "usbguard.key.revoke", None, kid)
    await guard.broadcast_config()
    return {"ok": True}
