# dist_routes.py (Core, formerly module "dist") - unified distribution under /dist
# ------------------------------------------------------------------
# PUBLIC (no login), released per folder via setting (default: on):
#   GET /dist                      overview of all public folders (HTML)
#   GET /dist/{folder}             file list of a folder (HTML)
#   GET /dist/{folder}/{file}      download file
# MANAGEMENT (permission manage_dist):
#   GET    /api/dist/folders               all folders (including private) + metadata
#   POST   /api/dist/folders               create {name,label,public}
#   PATCH  /api/dist/folders/{name}        {public?,label?}
#   DELETE /api/dist/folders/{name}        (non-core folders only)
#   GET    /api/dist/folders/{name}/files  files
#   POST   /api/dist/folders/{name}/upload upload files
#   DELETE /api/dist/folders/{name}/files/{file}
from __future__ import annotations

import html
import time

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from starlette.responses import FileResponse, HTMLResponse, PlainTextResponse
from pydantic import BaseModel

from app import db
from app.auth import get_current_user, require_perm

from app import dist_store as store

router = APIRouter(tags=["dist"])
_OFF = PlainTextResponse("This area is not released.", status_code=403)


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n/1:.0f} {unit}" if n < 1024 else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _sz(n: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == "GB":
            return (f"{int(f)} {u}" if u == "B" else f"{f:.1f} {u}")
        f /= 1024
    return f"{f:.1f} GB"


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>:root{{color-scheme:dark light}}body{{margin:0;font:15px system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
background:#0b1220;color:#e6edf6}}@media(prefers-color-scheme:light){{body{{background:#f5f7fb;color:#10233f}}}}
.wrap{{max-width:820px;margin:0 auto;padding:28px 18px}}h1{{font-size:22px;display:flex;align-items:center;gap:10px}}
.logo{{width:34px;height:34px;border-radius:9px;background:linear-gradient(135deg,#2dd4bf,#38bdf8);display:grid;
place-items:center;color:#052521;font-weight:900}}a{{color:#38bdf8;text-decoration:none}}a:hover{{text-decoration:underline}}
.card{{display:flex;align-items:center;gap:12px;border:1px solid #243249;border-radius:11px;padding:12px 14px;margin:8px 0;
background:#121c2e}}@media(prefers-color-scheme:light){{.card{{background:#fff;border-color:#d9e1ec}}}}
.card .i{{font-size:22px}}.card .t{{flex:1}}.card .t b{{display:block}}.card .m{{color:#8fa1ba;font-size:12.5px}}
.dl{{background:#2dd4bf;color:#052521;border-radius:8px;padding:6px 12px;font-weight:700}}
.bc{{color:#8fa1ba;font-size:13px;margin-bottom:10px}}.empty{{color:#8fa1ba}}</style></head>
<body><div class="wrap"><h1><span class="logo">B</span>{title}</h1>{body}</div></body></html>"""


def _render(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(title=html.escape(title), body=body))


@router.get("/dist", include_in_schema=False)
@router.get("/dist/", include_in_schema=False)
def dist_index():
    pub = [f for f in store.folders() if f["public"]]
    if not pub:
        rows = '<p class="empty">No public areas.</p>'
    else:
        rows = "".join(
            f'<a class="card" href="/dist/{html.escape(f["name"])}"><span class="i"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><path d="M3.27 6.96 12 12.01l8.73-5.05"/><path d="M12 22.08V12"/></svg></span>'
            f'<span class="t"><b>{html.escape(f["label"])}</b>'
            f'<span class="m">{f["files"]} file(s) · {_sz(f["size"])}</span></span>'
            f'<span class="dl">Open</span></a>' for f in pub)
    return _render("BYTERA RMM · Downloads", rows)


# Old address /dist/enrollment/... -> /dist/enroll/... (folder was renamed)
@router.get("/dist/enrollment", include_in_schema=False)
@router.get("/dist/enrollment/", include_in_schema=False)
def _dist_enrollment_old():
    from starlette.responses import RedirectResponse
    return RedirectResponse("/dist/enroll/", status_code=308)


@router.get("/dist/enrollment/{filename}", include_in_schema=False)
def _dist_enrollment_old_file(filename: str):
    from starlette.responses import RedirectResponse
    return RedirectResponse(f"/dist/enroll/{filename}", status_code=308)


@router.get("/dist/{folder}", include_in_schema=False)
@router.get("/dist/{folder}/", include_in_schema=False)
def dist_folder(folder: str):
    if not store.is_public(folder):
        return _OFF
    m = store.meta(folder)
    items = store.listdir(folder)
    if not items:
        body = '<div class="bc"><a href="/dist">← Downloads</a></div><p class="empty">This area is still empty.</p>'
    else:
        rows = "".join(
            f'<a class="card" href="/dist/{html.escape(folder)}/{html.escape(it["name"])}">'
            f'<span class="i"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg></span><span class="t"><b>{html.escape(it["name"])}</b>'
            f'<span class="m">{_sz(it["size"])} · {time.strftime("%d.%m.%Y", time.localtime(it["mtime"]))}</span></span>'
            f'<span class="dl"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></svg> Download</span></a>' for it in items)
        body = f'<div class="bc"><a href="/dist">← Downloads</a></div>{rows}'
    return _render(m["label"], body)


@router.get("/dist/{folder}/{filename}", include_in_schema=False)
def dist_file(folder: str, filename: str):
    if not store.is_public(folder):
        return _OFF
    p = store.file_path(folder, filename)
    if not p:
        return PlainTextResponse("Not found", status_code=404)
    return FileResponse(str(p), filename=p.name, media_type="application/octet-stream")


# --- short aliases: /enrollment /agent /client point to /dist/<name> ---
from starlette.responses import RedirectResponse as _Redirect

def _alias(folder):
    def idx():
        return _Redirect(f"/dist/{folder}", status_code=307)
    def sub(filename: str):
        return _Redirect(f"/dist/{folder}/{filename}", status_code=307)
    return idx, sub

# (No alias "/enroll": that address belongs to the enrollment links /enroll/<token>.)
for _name, _target in (("enrollment", "enroll"), ("agent", "agent"), ("client", "client")):
    _idx, _sub = _alias(_target)
    router.add_api_route(f"/{_name}", _idx, methods=["GET"], include_in_schema=False)
    router.add_api_route(f"/{_name}/", _idx, methods=["GET"], include_in_schema=False)
    router.add_api_route(f"/{_name}/{{filename}}", _sub, methods=["GET"], include_in_schema=False)


# ====================================================== management
def _perm(user):
    require_perm(user, "manage_dist")


@router.get("/api/dist/folders")
def api_folders(user: dict = Depends(get_current_user)):
    _perm(user)
    return {"folders": store.folders()}


class FolderBody(BaseModel):
    name: str = ""
    label: str = ""
    public: bool = True


@router.post("/api/dist/folders")
def api_create(body: FolderBody, user: dict = Depends(get_current_user)):
    _perm(user)
    if not store.valid_name(body.name):
        raise HTTPException(400, "Invalid folder name")
    if not store.create_folder(body.name, body.label, body.public):
        raise HTTPException(400, "Could not be created")
    db.add_audit_entry(user["username"], "dist.folder_created", details=body.name)
    return {"ok": True}


class MetaBody(BaseModel):
    public: bool | None = None
    label: str | None = None


@router.patch("/api/dist/folders/{name}")
def api_meta(name: str, body: MetaBody, user: dict = Depends(get_current_user)):
    _perm(user)
    if not store.folder_path(name):
        raise HTTPException(404, "Folder not found")
    store.set_meta(name, public=body.public, label=body.label)
    return {"ok": True, "meta": store.meta(name)}


@router.delete("/api/dist/folders/{name}")
def api_delete(name: str, user: dict = Depends(get_current_user)):
    _perm(user)
    if not store.delete_folder(name):
        raise HTTPException(400, "Core folders cannot be deleted")
    db.add_audit_entry(user["username"], "dist.folder_deleted", details=name)
    return {"ok": True}


@router.get("/api/dist/folders/{name}/files")
def api_files(name: str, request: Request, user: dict = Depends(get_current_user)):
    _perm(user)
    if not store.folder_path(name):
        raise HTTPException(404, "Folder not found")
    try:
        from router.enrollment_routes import _backend_url
        base = _backend_url(request).rstrip("/")
    except Exception:
        base = ""
    items = store.listdir(name)
    for it in items:
        it["url"] = f"{base}/dist/{name}/{it['name']}"
    return {"files": items, "meta": store.meta(name)}


@router.post("/api/dist/folders/{name}/upload")
async def api_upload(name: str, files: list[UploadFile] = File(...), user: dict = Depends(get_current_user)):
    _perm(user)
    if not store.folder_path(name) and not store.valid_name(name):
        raise HTTPException(404, "Folder not found")
    saved = []
    for f in files:
        data = await f.read()
        if store.save_file(name, f.filename, data):
            saved.append(f.filename)
    db.add_audit_entry(user["username"], "dist.upload", details=f"{name}: {', '.join(saved)}"[:300])
    return {"ok": True, "saved": saved}


@router.delete("/api/dist/folders/{name}/files/{filename}")
def api_delete_file(name: str, filename: str, user: dict = Depends(get_current_user)):
    _perm(user)
    if not store.delete_file(name, filename):
        raise HTTPException(404, "File not found")
    return {"ok": True}
