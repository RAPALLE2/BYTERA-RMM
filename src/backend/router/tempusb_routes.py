# tempusb_routes.py (Core) - digital USB stick
# -----------------------------------------------
#   GET    /api/temp-usb                         visible areas (+ can_manage)
#   POST   /api/temp-usb                         {label, owner_type:user|group, owner_id}
#   PATCH  /api/temp-usb/{id}                     {label}
#   DELETE /api/temp-usb/{id}
#   GET    /api/temp-usb/{id}/files?path=
#   POST   /api/temp-usb/{id}/upload             multipart path, files[]
#   POST   /api/temp-usb/{id}/mkdir              {path}
#   POST   /api/temp-usb/{id}/delete             {path}
#   GET    /api/temp-usb/{id}/download?path=
#   GET    /api/temp-usb/directory               users + groups (for owner selection)
#   POST   /api/temp-usb/{id}/attach             {clientId, mode:folder|usb, path?}
#   POST   /api/temp-usb/{id}/detach             {clientId}
#
# "attach" starts the folder sync (resp. virtual USB) on the device and pushes
# the current content of the area over. Returning files land back in the area
# (see sockets.py: temp-usb-upload).
from __future__ import annotations

import base64

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel
from starlette.responses import FileResponse

from app import db
from app import tempusb_store as store
from app.auth import get_current_user, is_super_admin, user_has_permission

router = APIRouter(prefix="/api/temp-usb", tags=["temp-usb"])

MAX_UPLOAD = 1024 * 1024 * 1024        # 1 GB per file
PUT_CHUNK = 256 * 1024                  # chunk size towards the agent


def _ctx(user: dict):
    gids = set(db.get_user_group_ids(user["id"]))
    manage = is_super_admin(user) or user_has_permission(user, "manage_temp_usb")
    return gids, manage


def _area_or_404(area_id: str, user: dict):
    a = store.get(area_id)
    gids, manage = _ctx(user)
    if not a or not store.may_access(a, user, gids, manage):
        raise HTTPException(404, "USB area not found")
    return a


# ------------------------------------------------------------------ areas
@router.get("")
def list_areas(user: dict = Depends(get_current_user)):
    gids, manage = _ctx(user)
    return {"areas": store.visible(user, gids, manage), "can_manage": manage}


@router.get("/directory")
def directory(user: dict = Depends(get_current_user)):
    _, manage = _ctx(user)
    if not manage:
        raise HTTPException(403, "No permission")
    users = [{"id": u["id"], "name": u.get("display_name") or u["username"]} for u in db.list_users()]
    groups = [{"id": g["id"], "name": g["name"]} for g in db.list_groups()]
    return {"users": users, "groups": groups}


class CreateBody(BaseModel):
    label: str = "USB stick"
    owner_type: str = "user"
    owner_id: str = ""


@router.post("")
def create_area(body: CreateBody, user: dict = Depends(get_current_user)):
    _, manage = _ctx(user)
    if not manage:
        raise HTTPException(403, "No permission")
    owner_id = body.owner_id or (user["id"] if body.owner_type == "user" else "")
    if not owner_id:
        raise HTTPException(400, "Owner missing")
    try:
        a = store.create(body.label, body.owner_type, owner_id, user["username"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.add_audit_entry(user["username"], "tempusb.create", target=a["id"], details=a["label"])
    return a


class LabelBody(BaseModel):
    label: str


@router.patch("/{area_id}")
def rename_area(area_id: str, body: LabelBody, user: dict = Depends(get_current_user)):
    _area_or_404(area_id, user)
    store.set_label(area_id, body.label)
    return store.get(area_id)


@router.delete("/{area_id}")
def delete_area(area_id: str, user: dict = Depends(get_current_user)):
    _area_or_404(area_id, user)
    store.delete(area_id)
    db.add_audit_entry(user["username"], "tempusb.delete", target=area_id)
    return {"ok": True}


# ------------------------------------------------------------------ files
@router.get("/{area_id}/files")
def files(area_id: str, path: str = "", user: dict = Depends(get_current_user)):
    _area_or_404(area_id, user)
    try:
        return {"path": path, "entries": store.listdir(area_id, path)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/{area_id}/upload")
async def upload(area_id: str, path: str = Form(""), files: list[UploadFile] = File(...),
                 user: dict = Depends(get_current_user)):
    _area_or_404(area_id, user)
    saved = []
    for f in files:
        data = await f.read()
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, f"{f.filename}: too large")
        store.write_file(area_id, path, f.filename or "file", data)
        saved.append(f.filename)
    return {"saved": saved}


class PathBody(BaseModel):
    path: str


@router.post("/{area_id}/mkdir")
def mkdir(area_id: str, body: PathBody, user: dict = Depends(get_current_user)):
    _area_or_404(area_id, user)
    try:
        store.mkdir(area_id, body.path)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.post("/{area_id}/delete")
def delete_entry(area_id: str, body: PathBody, user: dict = Depends(get_current_user)):
    _area_or_404(area_id, user)
    try:
        store.delete_entry(area_id, body.path)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.get("/{area_id}/download")
def download(area_id: str, path: str, user: dict = Depends(get_current_user)):
    _area_or_404(area_id, user)
    try:
        p = store.file_path(area_id, path)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "File not found")
    return FileResponse(str(p), filename=p.name, media_type="application/octet-stream")


# ------------------------------------------------------------------ attach to a device
class AttachBody(BaseModel):
    clientId: str
    mode: str = "folder"       # folder = folder sync, usb = virtual USB (driver required)
    path: str = ""             # target folder; empty -> default (C:\bytera or /mnt/bytera)


async def _push_area_files(client_id: str, area_id: str) -> int:
    """Send the current content of the area to the agent in chunks."""
    from app.sockets import send_to_agent
    sent = 0

    def walk(rel: str):
        for e in store.listdir(area_id, rel):
            r = (rel + "/" + e["name"]).strip("/")
            if e["dir"]:
                yield from walk(r)
            else:
                yield r
    for rel in walk(""):
        p = store.file_path(area_id, rel)
        data = p.read_bytes()
        n = max(1, (len(data) + PUT_CHUNK - 1) // PUT_CHUNK)
        for seq in range(n):
            await send_to_agent(client_id, "temp-usb-put", {
                "id": area_id, "name": rel, "seq": seq, "last": seq == n - 1,
                "data": base64.b64encode(data[seq * PUT_CHUNK:(seq + 1) * PUT_CHUNK]).decode(),
            })
        if n:
            sent += 1
    return sent


@router.post("/{area_id}/attach")
async def attach(area_id: str, body: AttachBody, user: dict = Depends(get_current_user)):
    a = _area_or_404(area_id, user)
    if body.mode not in ("folder", "usb"):
        raise HTTPException(400, "Invalid mode")
    from app.sockets import send_to_agent
    ok = await send_to_agent(body.clientId, "temp-usb-attach", {
        "id": area_id, "label": a["label"], "mode": body.mode, "path": body.path or "",
        "driver": db.get_setting("temp_usb_driver") or "",
    })
    if not ok:
        raise HTTPException(409, "Device is not online")
    await _push_area_files(body.clientId, area_id)
    store.set_attached(area_id, body.clientId, {"mode": body.mode, "path": body.path or "", "by": user["username"]})
    db.add_audit_entry(user["username"], "tempusb.attach", target=area_id, details=f"{body.clientId};{body.mode}")
    return {"ok": True}


@router.post("/{area_id}/detach")
async def detach(area_id: str, body: AttachBody, user: dict = Depends(get_current_user)):
    _area_or_404(area_id, user)
    from app.sockets import send_to_agent
    await send_to_agent(body.clientId, "temp-usb-detach", {"id": area_id})
    store.set_attached(area_id, body.clientId, None)
    db.add_audit_entry(user["username"], "tempusb.detach", target=area_id, details=body.clientId)
    return {"ok": True}
