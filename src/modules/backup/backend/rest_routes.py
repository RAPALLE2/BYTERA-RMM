"""
rest_routes.py (Modul "backup") - restic-REST-Server des Backends.

Fuer die Wege "Backend" und "ueber Backend": Der Agent startet restic mit
  RESTIC_REPOSITORY = rest:https://<backend>/api/backup/rest/<token>/
Das Token steht fuer genau EIN Repo (Client x Speicherziel). Die Daten
kommen bereits von restic VERSCHLUESSELT an (das Passwort kennt nur das
Repo-Geheimnis) - das Backend speichert/leitet nur weiter: lokal oder per
SMB/SFTP/FTP auf ein NAS (drivers.py).

Umgesetzt ist das restic-REST-Protokoll v1/v2:
  POST  /?create=true            Repo anlegen
  HEAD/GET/POST  /config
  GET   /{type}/                 Liste (v2: [{name,size}])
  HEAD/GET/POST/DELETE /{type}/{name}   (GET mit Range-Unterstuetzung)
"""

from __future__ import annotations

import asyncio
import re
import tempfile

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from rmm_mod_backup import store
from rmm_mod_backup.drivers import DriverError, driver_for

router = APIRouter(tags=["backup-rest"])

TYPES = ("data", "keys", "locks", "snapshots", "index")
NAME_RE = re.compile(r"^[0-9a-f]{64}$")
V2 = "application/vnd.x.restic.rest.v2"


def _resolve(token: str):
    hit = store.repo_by_token(token)
    if not hit:
        raise HTTPException(404, "Unbekanntes Repository")
    cid, sid = hit
    st = store.storage(sid)
    if not st:
        raise HTTPException(404, "Speicherziel existiert nicht mehr")
    try:
        return driver_for(st), f"bytera-backup/{cid}"
    except DriverError as exc:
        raise HTTPException(503, str(exc))


def _obj(repo: str, typ: str, name: str) -> str:
    if typ not in TYPES or not NAME_RE.match(name):
        raise HTTPException(400, "Ungültiger Objektname")
    if typ == "data":
        return f"{repo}/data/{name[:2]}/{name}"
    return f"{repo}/{typ}/{name}"


async def _body_to_file(request: Request):
    tmp = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024)
    async for chunk in request.stream():
        tmp.write(chunk)
    tmp.seek(0)
    return tmp


def _range(request: Request, size: int):
    h = request.headers.get("range")
    if not h:
        return None
    m = re.match(r"bytes=(\d*)-(\d*)$", h.strip())
    if not m:
        return None
    a, b = m.group(1), m.group(2)
    if a == "":                                   # bytes=-N (letzte N Bytes)
        n = int(b)
        return max(0, size - n), size - 1
    start = int(a)
    end = int(b) if b else size - 1
    return start, min(end, size - 1)


async def _get_file(request: Request, drv, path: str, head: bool):
    size = await asyncio.to_thread(drv.stat, path)
    if size is None:
        raise HTTPException(404, "Nicht gefunden")
    if head:
        return Response(status_code=200, headers={"Content-Length": str(size)})
    rng = _range(request, size)
    if rng:
        start, end = rng
        data = await asyncio.to_thread(drv.read, path, start, end - start + 1)
        return Response(data, status_code=206, media_type="application/octet-stream",
                        headers={"Content-Range": f"bytes {start}-{end}/{size}"})
    data = await asyncio.to_thread(drv.read, path)
    return Response(data, media_type="application/octet-stream")


@router.api_route("/api/backup/rest/{token}/", methods=["POST", "DELETE"],
                  include_in_schema=False)
async def repo_root(token: str, request: Request):
    drv, repo = _resolve(token)
    if request.method == "DELETE":
        raise HTTPException(403, "Repository löschen ist über diesen Weg nicht erlaubt")
    if request.query_params.get("create") == "true":
        for t in TYPES:
            await asyncio.to_thread(drv.mkdirs, f"{repo}/{t}")
        return Response(status_code=200)
    raise HTTPException(400, "Unbekannte Anfrage")


@router.api_route("/api/backup/rest/{token}/config", methods=["GET", "HEAD", "POST"],
                  include_in_schema=False)
async def repo_config(token: str, request: Request):
    drv, repo = _resolve(token)
    path = f"{repo}/config"
    if request.method == "POST":
        if await asyncio.to_thread(drv.stat, path) is not None:
            raise HTTPException(403, "config existiert bereits")
        f = await _body_to_file(request)
        await asyncio.to_thread(drv.write, path, f)
        return Response(status_code=200)
    return await _get_file(request, drv, path, request.method == "HEAD")


@router.get("/api/backup/rest/{token}/{typ}/", include_in_schema=False)
async def repo_list(token: str, typ: str, request: Request):
    if typ not in TYPES:
        raise HTTPException(404, "Unbekannter Typ")
    drv, repo = _resolve(token)
    items = await asyncio.to_thread(drv.list, f"{repo}/{typ}")
    items = [(n, s) for n, s in items if NAME_RE.match(n)]
    if V2 in (request.headers.get("accept") or ""):
        return JSONResponse([{"name": n, "size": s} for n, s in items], media_type=V2)
    return JSONResponse([n for n, _ in items])


@router.api_route("/api/backup/rest/{token}/{typ}/{name}",
                  methods=["GET", "HEAD", "POST", "DELETE"], include_in_schema=False)
async def repo_object(token: str, typ: str, name: str, request: Request):
    drv, repo = _resolve(token)
    path = _obj(repo, typ, name)
    if request.method == "POST":
        f = await _body_to_file(request)
        await asyncio.to_thread(drv.write, path, f)
        return Response(status_code=200)
    if request.method == "DELETE":
        await asyncio.to_thread(drv.delete, path)
        return Response(status_code=200)
    return await _get_file(request, drv, path, request.method == "HEAD")
