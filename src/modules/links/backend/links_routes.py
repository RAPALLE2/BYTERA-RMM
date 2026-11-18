"""
links_routes.py (Modul "links") - URL-Shortener & QR-Codes
----------------------------------------------------------
Kurzlinks im Stil von rmm.url/t (Tracking), nur eben:

  * rmm.url/s/<code>   -> 302 auf das Ziel, zaehlt KLICKS
  * rmm.url/qr/<code>  -> 302 auf das Ziel, zaehlt QR-SCANS getrennt
                          (dieser Pfad gehoert in QR-Codes, die "ueber das RMM
                          laufen" sollen - das Ziel bleibt damit nachtraeglich
                          aenderbar und Scans sind messbar)

Die QR-Grafik selbst entsteht komplett im Browser (frontend/qrcode.js,
lokal gebuendelt) - wahlweise mit dem RMM-Link oder direkt mit der Zielseite
("sonst halt direkt zur Seite", dann ohne Zaehlung/ohne RMM-Abhaengigkeit).

Ablage: data/modules/links/data.json (kein DB-Schema noetig).
Jeder angemeldete Benutzer verwaltet SEINE Links; Admins sehen alle.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
import urllib.parse

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app import db
from app.auth import get_current_user
from app.paths import MODULES_DATA_DIR

router = APIRouter(tags=["links"])

DIR = MODULES_DATA_DIR / "links"
FILE = DIR / "data.json"
_lock = threading.RLock()

CODE_RE = re.compile(r"^[A-Za-z0-9_-]{2,32}$")
# Ohne leicht verwechselbare Zeichen (0/O, 1/l/I) - Codes werden abgetippt.
ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"
RESERVED = {"s", "qr", "t", "api", "dist", "web", "spaces", "docker", "modules", "js", "css", "image"}


def _load() -> dict:
    with _lock:
        try:
            return json.loads(FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"links": {}}


def _save(data: dict) -> None:
    with _lock:
        DIR.mkdir(parents=True, exist_ok=True)
        tmp = FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(FILE)


def _check_url(url: str) -> str:
    url = (url or "").strip()
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise HTTPException(400, "Ziel muss mit http:// oder https:// beginnen")
    if len(url) > 2000:
        raise HTTPException(400, "Ziel-URL ist zu lang")
    return url


def _new_code(links: dict) -> str:
    for length in (5, 6, 7, 8):
        for _ in range(50):
            code = "".join(secrets.choice(ALPHABET) for _ in range(length))
            if code not in links:
                return code
    raise HTTPException(500, "Kein freier Code gefunden")


# ------------------------------------------------------------------
# OEFFENTLICH: die eigentlichen Weiterleitungen (ohne Anmeldung)
# ------------------------------------------------------------------

def _redirect(code: str, counter: str):
    data = _load()
    link = data["links"].get(code)
    if not link:
        raise HTTPException(404, "Unbekannter Kurzlink")
    link[counter] = int(link.get(counter) or 0) + 1
    link["last_used"] = int(time.time() * 1000)
    _save(data)
    return RedirectResponse(link["url"], status_code=302)


@router.get("/s/{code}")
def short_redirect(code: str):
    """Kurzlink: rmm.url/s/<code> -> Ziel (zaehlt Klicks)."""
    return _redirect(code, "clicks")


@router.get("/qr/{code}")
def qr_redirect(code: str):
    """QR-Weg: rmm.url/qr/<code> -> Ziel (zaehlt Scans getrennt von Klicks)."""
    return _redirect(code, "qr_scans")


# ------------------------------------------------------------------
# API (angemeldet): Links verwalten
# ------------------------------------------------------------------

def _public(link: dict, code: str) -> dict:
    return {"code": code, **{k: link.get(k) for k in
            ("url", "name", "created_by", "created_at", "clicks", "qr_scans", "last_used")}}


@router.get("/api/links")
def list_links(user: dict = Depends(get_current_user)):
    data = _load()
    admin = (user or {}).get("role") == "admin"
    out = [_public(l, c) for c, l in data["links"].items()
           if admin or l.get("created_by") == user.get("username")]
    out.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    return out


class LinkBody(BaseModel):
    url: str
    name: str = ""
    code: str = ""      # optionaler Wunsch-Code, sonst Zufall


@router.post("/api/links")
def create_link(body: LinkBody, user: dict = Depends(get_current_user)):
    url = _check_url(body.url)
    data = _load()
    links = data["links"]
    code = (body.code or "").strip()
    if code:
        if not CODE_RE.fullmatch(code):
            raise HTTPException(400, "Eigener Code: 2-32 Zeichen, nur Buchstaben/Zahlen/-/_")
        if code.lower() in RESERVED:
            raise HTTPException(400, f"Der Code „{code}“ ist reserviert")
        if code in links:
            raise HTTPException(409, f"Der Code „{code}“ ist schon vergeben")
    else:
        code = _new_code(links)
    links[code] = {
        "url": url, "name": (body.name or "").strip()[:120],
        "created_by": user.get("username"), "created_at": int(time.time() * 1000),
        "clicks": 0, "qr_scans": 0, "last_used": None,
    }
    _save(data)
    db.add_audit_entry(user.get("username"), "links.create", None, f"/s/{code} -> {url}")
    return _public(links[code], code)


class LinkPatch(BaseModel):
    url: str | None = None
    name: str | None = None


@router.patch("/api/links/{code}")
def update_link(code: str, body: LinkPatch, user: dict = Depends(get_current_user)):
    data = _load()
    link = data["links"].get(code)
    if not link:
        raise HTTPException(404, "Unbekannter Kurzlink")
    if link.get("created_by") != user.get("username") and user.get("role") != "admin":
        raise HTTPException(403, "Nur der Ersteller (oder ein Admin) darf das")
    if body.url is not None:
        link["url"] = _check_url(body.url)
    if body.name is not None:
        link["name"] = body.name.strip()[:120]
    _save(data)
    return _public(link, code)


@router.delete("/api/links/{code}")
def delete_link(code: str, user: dict = Depends(get_current_user)):
    data = _load()
    link = data["links"].get(code)
    if not link:
        return {"ok": True}
    if link.get("created_by") != user.get("username") and user.get("role") != "admin":
        raise HTTPException(403, "Nur der Ersteller (oder ein Admin) darf das")
    del data["links"][code]
    _save(data)
    db.add_audit_entry(user.get("username"), "links.delete", None, f"/s/{code}")
    return {"ok": True}
