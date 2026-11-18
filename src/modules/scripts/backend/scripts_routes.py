"""
routers/scripts_routes.py
---------------------------
Management of saved scripts (Scripts app). A script has a name, a command (may
be multi-line, e.g. "apt update && apt upgrade -y") and a target OS
(windows/linux/any).

Endpoints:
  GET    /api/scripts        -> all scripts
  POST   /api/scripts        -> create a new script
  PUT    /api/scripts/{id}   -> edit a script
  DELETE /api/scripts/{id}   -> delete a script
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app import db
from app.auth import get_current_user, require_perm

router = APIRouter(prefix="/api/scripts", tags=["scripts"])


class ScriptBody(BaseModel):
    name: str
    command: str
    os: str = "any"   # "windows" | "linux" | "any"
    folder: str = ""  # folder in the Scripts app ("" = no folder)


@router.get("")
def list_scripts(user: dict = Depends(get_current_user)):
    require_perm(user, "use_scripts")
    return db.list_scripts()


@router.post("")
def create_script(body: ScriptBody, user: dict = Depends(get_current_user)):
    require_perm(user, "create_scripts")
    script = db.create_script(body.name, body.command, body.os, body.folder)
    db.add_audit_entry(user["username"], "script.created", target=script["id"], details=body.name)
    return script


@router.put("/{script_id}")
def update_script(script_id: str, body: ScriptBody, user: dict = Depends(get_current_user)):
    require_perm(user, "create_scripts")
    script = db.update_script(script_id, body.name, body.command, body.os, body.folder)
    db.add_audit_entry(user["username"], "script.updated", target=script_id, details=body.name)
    return script


@router.delete("/{script_id}")
def delete_script(script_id: str, user: dict = Depends(get_current_user)):
    require_perm(user, "create_scripts")
    db.delete_script(script_id)
    db.add_audit_entry(user["username"], "script.deleted", target=script_id)
    return {"ok": True}
