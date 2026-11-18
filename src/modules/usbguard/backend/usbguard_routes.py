"""
usbguard_routes.py (Modul "usbguard") - Entscheidung aus dem Dashboard.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import get_current_user, require_perm

from rmm_mod_usbguard import guard

router = APIRouter(prefix="/api/usbguard", tags=["usbguard"])


class DecideBody(BaseModel):
    client_id: str
    dev: str
    allow: bool


@router.post("/decide")
async def usb_decide(body: DecideBody, user: dict = Depends(get_current_user)):
    # Freigeben/Blockieren ist ein Eingriff am Gerät -> gleiches Recht wie Terminal.
    require_perm(user, "use_terminal", body.client_id)
    ok = await guard.decide(body.client_id, body.dev, body.allow, user.get("username"))
    if not ok:
        raise HTTPException(409, "Client ist offline - Entscheidung konnte nicht zugestellt werden")
    return {"ok": True}
