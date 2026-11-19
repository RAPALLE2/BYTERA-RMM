"""
diag_routes.py
--------------
The maintenance mode in the dashboard (Settings -> Source -> Diagnostics).

  GET  /api/diag/status              is it running? which files exist?
  POST /api/diag/enable              turn on (optionally with a time limit)
  POST /api/diag/disable             turn off
  GET  /api/diag/tail?lines=300      the last lines live
  GET  /api/diag/bundle              download everything as a ZIP
  POST /api/diag/clear               clear logs

Everything behind 'see_source' - whoever may see the sources may also operate
diagnostics. The logs may contain paths, host names and error messages from the
customer network; that is not for ordinary users.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel

from app import db, diagnostics
from app.auth import get_current_user, user_has_permission

router = APIRouter()


def _require(user: dict) -> None:
    if not user_has_permission(user, "see_source"):
        raise HTTPException(403, "This requires the 'see sources' permission")


class EnableBody(BaseModel):
    # 0 = no end. A time limit is the default so that a forgotten maintenance
    # mode does not fill the disk for weeks.
    minutes: int = 120
    reason: str = ""
    # Should the agents also record everything and upload it?
    include_agents: bool = True


@router.get("/api/diag/status")
def diag_status(user: dict = Depends(get_current_user)):
    _require(user)
    data = diagnostics.status()
    data["agents_included"] = db.get_setting("maintenance_agents", "0") == "1"
    return data


@router.post("/api/diag/enable")
async def diag_enable(body: EnableBody, user: dict = Depends(get_current_user)):
    _require(user)
    result = diagnostics.enable(body.minutes, body.reason)
    db.set_setting("maintenance_agents", "1" if body.include_agents else "0")
    db.add_audit_entry(user["username"], "diag.enabled",
                       details=f"{body.minutes} min - {body.reason}")
    if body.include_agents:
        await _broadcast_agents(True, body.minutes)
    result["agents_included"] = body.include_agents
    return result


@router.post("/api/diag/disable")
async def diag_disable(user: dict = Depends(get_current_user)):
    _require(user)
    result = diagnostics.disable()
    db.set_setting("maintenance_agents", "0")
    db.add_audit_entry(user["username"], "diag.disabled")
    await _broadcast_agents(False, 0)
    return result


@router.get("/api/diag/tail", response_class=PlainTextResponse)
def diag_tail(lines: int = 300, user: dict = Depends(get_current_user)):
    _require(user)
    return "\n".join(diagnostics.tail(lines))


@router.get("/api/diag/bundle")
def diag_bundle(user: dict = Depends(get_current_user)):
    _require(user)
    data = diagnostics.bundle()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    db.add_audit_entry(user["username"], "diag.downloaded")
    return Response(
        content=data, media_type="application/zip",
        headers={"Content-Disposition":
                 f'attachment; filename="rmm-diagnose-{stamp}.zip"'})


@router.post("/api/diag/clear")
def diag_clear(user: dict = Depends(get_current_user)):
    _require(user)
    diagnostics.clear()
    db.add_audit_entry(user["username"], "diag.cleared")
    return diagnostics.status()


async def _broadcast_agents(enabled: bool, minutes: int) -> None:
    """
    Notifies all agents.

    No error if an agent is currently offline: it is told the state on its next
    registration anyway (see sockets.py).
    """
    try:
        from app.sockets import sio
        await sio.emit("diag-mode", {"enabled": enabled, "minutes": minutes},
                       namespace="/agent")
    except Exception as e:
        print(f"[diag] agents not reached: {e}")
