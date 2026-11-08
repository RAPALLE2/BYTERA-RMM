# browser_routes.py (Modul "browser") - Sitzungssteuerung + Live-Stream (WS)
# ----------------------------------------------------------------------------
#   GET  /api/browser/status                      Playwright vorhanden?
#   GET  /api/browser/bookmarks                   Lesezeichen des Benutzers
#   POST /api/browser/bookmarks  {title,url}      Lesezeichen anlegen
#   DELETE /api/browser/bookmarks/{bid}           Lesezeichen loeschen
#   WS   /api/browser/ws                          Live-Browser (Token als Unterprotokoll rmmtoken.<JWT>)
#
# Die WS empfaengt Befehle {cmd:...} (new-tab, close-tab, navigate, back,
# forward, reload, input, active, resize) und sendet {t:"frame"|"meta"|"tabs"}.
from __future__ import annotations

import asyncio
import json
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, WebSocket
from pydantic import BaseModel

from app import db
from app.auth import decode_access_token, get_current_user, user_has_permission, is_super_admin
from app.paths import MODULES_DATA_DIR
from . import session as _bs

router = APIRouter(prefix="/api/browser", tags=["browser"])


def _perm(user) -> bool:
    return is_super_admin(user) or user_has_permission(user, "use_browser")


def _bm_file():
    d = MODULES_DATA_DIR / "browser"
    d.mkdir(parents=True, exist_ok=True)
    return d / "bookmarks.json"


def _load_bm() -> dict:
    f = _bm_file()
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_bm(d: dict):
    _bm_file().write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------------------------------------------ Status
@router.get("/status")
def status(user: dict = Depends(get_current_user)):
    ok, why = _bs.available()
    return {"ready": ok, "reason": why, "may_use": _perm(user)}


# ------------------------------------------------------------------ Lesezeichen (je Benutzer)
class BmBody(BaseModel):
    title: str = ""
    url: str


@router.get("/bookmarks")
def list_bm(user: dict = Depends(get_current_user)):
    return {"bookmarks": _load_bm().get(user["id"], [])}


@router.post("/bookmarks")
def add_bm(body: BmBody, user: dict = Depends(get_current_user)):
    if not _perm(user):
        raise HTTPException(403, "Keine Berechtigung")
    all_bm = _load_bm()
    lst = all_bm.setdefault(user["id"], [])
    bid = secrets.token_hex(6)
    lst.append({"id": bid, "title": (body.title or body.url)[:120], "url": body.url[:2000], "ts": int(time.time())})
    _save_bm(all_bm)
    return {"id": bid}


@router.delete("/bookmarks/{bid}")
def del_bm(bid: str, user: dict = Depends(get_current_user)):
    all_bm = _load_bm()
    lst = all_bm.get(user["id"], [])
    all_bm[user["id"]] = [b for b in lst if b.get("id") != bid]
    _save_bm(all_bm)
    return {"ok": True}


# ------------------------------------------------------------------ Live-Browser (WebSocket)
@router.websocket("/ws")
async def browser_ws(websocket: WebSocket):
    offered = websocket.scope.get("subprotocols") or []
    token = next((p[len("rmmtoken."):] for p in offered if p.startswith("rmmtoken.")), "")
    user = None
    try:
        user = db.get_user_by_id(decode_access_token(token).get("sub"))
    except Exception:
        user = None
    if not user or not _perm(user):
        await websocket.close(code=4403)
        return
    ok, _why = _bs.available()
    if not ok:
        await websocket.accept(subprotocol="rmmtoken." + token if token else None)
        await websocket.send_text(json.dumps({"t": "error", "msg": _why}))
        await websocket.close()
        return
    await websocket.accept(subprotocol="rmmtoken." + token if token else None)

    sid = "u" + user["id"] + "-" + secrets.token_hex(4)
    # Proxy/VPN-Egress aus den Modul-Einstellungen (leer = direkt).
    try:
        _bs.MANAGER.set_proxy(db.get_setting("browser_proxy") or None)
    except Exception:
        pass
    sess = await _bs.MANAGER.get_or_create(sid, user["username"])
    loop = asyncio.get_event_loop()

    async def send(obj):
        try:
            await websocket.send_text(json.dumps(obj))
        except Exception:
            pass

    async def on_frame(tid, b64):
        await send({"t": "frame", "tab": tid, "data": b64})

    def on_meta(tid, meta):
        loop.create_task(send({"t": "meta", "tab": tid, **meta}))
        loop.create_task(send_tabs())

    async def send_tabs():
        await send({"t": "tabs", "active": sess.active,
                    "tabs": [{"id": t.id, "title": t.title or t.url or "Neuer Tab", "url": t.url,
                              "loading": t.loading} for t in sess.tabs.values()]})

    sess.on_frame = on_frame
    sess.on_meta = on_meta

    await sess.new_tab("about:blank")
    await send_tabs()

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                m = json.loads(raw)
            except Exception:
                continue
            cmd = m.get("cmd")
            tid = m.get("tab") or sess.active
            if cmd == "new-tab":
                await sess.new_tab(m.get("url") or "about:blank")
                await send_tabs()
            elif cmd == "close-tab":
                await sess.close_tab(m.get("tab"))
                await send_tabs()
            elif cmd == "active":
                await sess.set_active(m.get("tab"))
                await send_tabs()
            elif cmd == "navigate":
                await sess.navigate(tid, m.get("url") or "")
            elif cmd == "back":
                await sess.back(tid)
            elif cmd == "forward":
                await sess.forward(tid)
            elif cmd == "reload":
                await sess.reload(tid)
            elif cmd == "resize":
                await sess.resize(m.get("w", 1280), m.get("h", 800))
            elif cmd == "input":
                await sess.input(tid, m.get("ev") or {})
    except Exception:
        pass
    finally:
        await _bs.MANAGER.drop(sid)
