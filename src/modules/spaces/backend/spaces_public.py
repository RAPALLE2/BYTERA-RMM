# spaces_public.py (Modul "spaces") - oeffentliche Adresse rmm.url.de/spaces/<name>
# Dieselben Spaces wie im Relay, aber direkt verlinkbar. Anmeldung ist IMMER
# noetig (Spaces sind nicht oeffentlich). <name> -> Teams/<n>, Öffentlich/<n>
# oder "Mein Space".
from __future__ import annotations

import html as _html
import mimetypes
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from app.auth import get_current_user
from . import store, vfs

router = APIRouter(prefix="/spaces", tags=["spaces-public"])


def _send(p: Path, inline: bool):
    mt = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    return FileResponse(p, media_type=mt, filename=p.name,
                        content_disposition_type="inline" if inline else "attachment")


def _space_path_for(user, name: str):
    n = (name or "").strip().lower()
    if n in ("mein space", "my", "personal", "meinspace"):
        return [vfs.MY]
    for sp in store.list_spaces():
        if (sp.get("name") or "").lower() == n:
            return (["Teams", sp["name"]] if sp["kind"] == "team" else ["Öffentlich", sp["name"]])
    return None





@router.get("")
@router.get("/")
def spaces_root(user: dict = Depends(get_current_user)):
    # Uebersicht -> Dashboard-App "Spaces"
    return RedirectResponse("/?go=" + "bytera://app/spaces", status_code=307)


@router.get("/{name}")
@router.get("/{name}/{sub:path}")
def spaces_public(name: str, sub: str = "", request: Request = None,
                  user: dict = Depends(get_current_user)):
    base = _space_path_for(user, name)
    if base is None:
        raise HTTPException(404, "Space nicht gefunden")
    parts = base + [p for p in (sub or "").replace("\\", "/").split("/") if p]
    # Datei?
    try:
        p = vfs.file_path(user, parts)
        return _send(p, inline=True)
    except (FileNotFoundError, IsADirectoryError):
        pass
    except PermissionError as e:
        raise HTTPException(403, str(e))
    # Sonst: Ordnerliste als schlichte HTML-Seite
    try:
        entries = vfs.listdir(user, parts)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except Exception:
        raise HTTPException(404, "Nicht gefunden")
    import html as _html
    here = "/spaces/" + "/".join([name] + [p for p in (sub or "").split("/") if p]).rstrip("/")
    rows = []
    for e in sorted(entries, key=lambda x: (not x.get("dir"), (x.get("name") or "").lower())):
        nm = e.get("name") or ""
        href = here.rstrip("/") + "/" + nm
        icon = "📁" if e.get("dir") else "📄"
        rows.append(f'<li>{icon} <a href="{_html.escape(href)}">{_html.escape(nm)}</a></li>')
    body = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{_html.escape(name)} – Spaces</title>"
            "<style>body{font-family:system-ui,sans-serif;max-width:760px;margin:40px auto;padding:0 16px}"
            "li{line-height:1.9;list-style:none}a{text-decoration:none}a:hover{text-decoration:underline}"
            "h1{font-size:20px}</style>"
            f"<h1>{_html.escape(name)}</h1><ul>{''.join(rows) or '<li>(leer)</li>'}</ul>")
    return HTMLResponse(body)
