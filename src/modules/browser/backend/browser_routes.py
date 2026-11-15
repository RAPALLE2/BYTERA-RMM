# browser_routes.py (Modul "browser") - REST + Live-Stream (WebSocket)
# --------------------------------------------------------------------
#   GET    /api/browser/status                  bereit? Installation? Rechte?
#   POST   /api/browser/install                 Chromium installieren (Backend-Job)
#   GET    /api/browser/networks                Netzwerke (direkt + Agent-Zugaenge aus dem VPN-Modul)
#   GET    /api/browser/bookmarks               Lesezeichen {items, folders}
#   POST   /api/browser/bookmarks               {title, url, folder}
#   PUT    /api/browser/bookmarks/{bid}         {title, url, folder, index}
#   DELETE /api/browser/bookmarks/{bid}
#   POST   /api/browser/folders                 {name}
#   DELETE /api/browser/folders/{name}
#   GET    /api/browser/history?q=              Verlauf
#   DELETE /api/browser/history                 Verlauf loeschen
#   GET    /api/browser/settings                Startseite, Suchmaschine, Wiederherstellen, Standard-Netzwerk
#   PUT    /api/browser/settings
#   DELETE /api/browser/cookies                 Cookies/Anmeldungen aller Netzwerke loeschen
#   GET    /api/browser/downloads               Liste
#   GET    /api/browser/downloads/{did}         Datei holen
#   DELETE /api/browser/downloads/{did}
#   POST   /api/browser/upload/{cid}            Dateien fuer eine Dateiauswahl der Seite
#   WS     /api/browser/ws                      Live-Browser (Token als Unterprotokoll rmmtoken.<JWT>)
#
# WS (?win=main | app:<key>): Befehle {cmd: ...} vom Dashboard; zurueck JSON {t: ...} und BINAER die
# Bilder des aktiven Tabs: [1 Byte Laenge][Tab-ID][JPEG].
from __future__ import annotations

import asyncio
import json
import shutil

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, WebSocket
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app import db
from app.auth import decode_access_token, get_current_user, user_has_permission, is_super_admin

from . import install, netsel, proxy, store
from . import session as _bs

router = APIRouter(prefix="/api/browser", tags=["browser"])


def _perm(user) -> bool:
    return is_super_admin(user) or user_has_permission(user, "use_browser")


def _need(user):
    if not _perm(user):
        raise HTTPException(403, "Missing permission: use the server browser")


# ------------------------------------------------------------------ Status / Installation
_auto_try = {"at": 0.0}
AUTO_RETRY = 10 * 60          # failed automatic installation: try again after 10 minutes


@router.get("/status")
async def status(user: dict = Depends(get_current_user)):
    from app import bgjobs
    import time
    ok, why = _bs.available()
    job = bgjobs.active("browser.install", "chromium")
    if proxy.enabled():                 # pass-through works without Chromium; stream tabs install it on demand
        return {"ready": True, "reason": "", "may_use": _perm(user), "proxy": True,
                "proxy_origin_rx": proxy.host_regex_js(),
                "stream": install.status()["status"] == "ready", "install": install.status(),
                "install_job": job.id if job else None,
                "may_install": is_super_admin(user) or user_has_permission(user, "manage_modules"),
                "stats": _bs.MANAGER.stats() if is_super_admin(user) else None}
    # not ready -> install automatically (no button needed), at most every AUTO_RETRY
    if install.status()["status"] != "ready" and not job and time.time() - _auto_try["at"] > AUTO_RETRY:
        _auto_try["at"] = time.time()
        job = install.start_install_job(None)
        ok, why = False, "The browser engine is being installed …"
    return {"ready": ok and install.status()["status"] == "ready", "reason": why, "may_use": _perm(user),
            "proxy": False, "stream": True,
            "install": install.status(), "install_job": job.id if job else None,
            "may_install": is_super_admin(user) or user_has_permission(user, "manage_modules"),
            "stats": _bs.MANAGER.stats() if is_super_admin(user) else None}


@router.post("/install")
async def start_install(user: dict = Depends(get_current_user)):
    if not (is_super_admin(user) or user_has_permission(user, "manage_modules")):
        raise HTTPException(403, "Only administrators can install the browser")
    job = install.start_install_job(user)
    return {"job_id": job.id}


@router.get("/networks")
def networks(user: dict = Depends(get_current_user)):
    _need(user)
    return {"networks": netsel.list_for(user)}


# ------------------------------------------------------------------ Lesezeichen
class BmBody(BaseModel):
    title: str = ""
    url: str = ""
    folder: str | None = None
    index: int | None = None


@router.get("/bookmarks")
def list_bm(user: dict = Depends(get_current_user)):
    _need(user)
    return store.bookmarks(user["id"])


@router.post("/bookmarks")
def add_bm(body: BmBody, user: dict = Depends(get_current_user)):
    _need(user)
    if not body.url.strip():
        raise HTTPException(400, "Address is missing")
    return store.add_bookmark(user["id"], body.title, body.url.strip(), body.folder or "toolbar")


@router.put("/bookmarks/{bid}")
def edit_bm(bid: str, body: BmBody, user: dict = Depends(get_current_user)):
    _need(user)
    b = store.edit_bookmark(user["id"], bid, title=body.title or None, url=body.url or None,
                            folder=body.folder, index=body.index)
    if not b:
        raise HTTPException(404, "Bookmark not found")
    return b


@router.delete("/bookmarks/{bid}")
def del_bm(bid: str, user: dict = Depends(get_current_user)):
    _need(user)
    store.delete_bookmark(user["id"], bid)
    return {"ok": True}


class FolderBody(BaseModel):
    name: str


@router.post("/folders")
def add_folder(body: FolderBody, user: dict = Depends(get_current_user)):
    _need(user)
    return store.add_folder(user["id"], body.name)


@router.delete("/folders/{name}")
def del_folder(name: str, user: dict = Depends(get_current_user)):
    _need(user)
    return store.delete_folder(user["id"], name)


# ------------------------------------------------------------------ Verlauf / Einstellungen / Cookies
@router.get("/history")
def history(q: str = "", limit: int = 200, user: dict = Depends(get_current_user)):
    _need(user)
    return {"history": store.history(user["id"], q, min(max(limit, 1), 1000))}


@router.delete("/history")
def clear_history(user: dict = Depends(get_current_user)):
    _need(user)
    store.clear_history(user["id"])
    return {"ok": True}


class SettingsBody(BaseModel):
    engine: str | None = None
    homepage: str | None = None
    search: str | None = None
    restore: bool | None = None
    default_network: str | None = None


@router.get("/settings")
def get_settings(user: dict = Depends(get_current_user)):
    _need(user)
    return {"settings": store.settings(user["id"]), "proxy": proxy.enabled(),
            "engines": [{"id": k, "name": v[0]} for k, v in store.SEARCH_ENGINES.items()]}


@router.put("/settings")
def put_settings(body: SettingsBody, user: dict = Depends(get_current_user)):
    _need(user)
    ch = body.model_dump(exclude_none=True) if hasattr(body, "model_dump") else body.dict(exclude_none=True)
    if "default_network" in ch and not netsel.valid(user, ch["default_network"]):
        raise HTTPException(400, "Unknown network")
    return {"settings": store.save_settings(user["id"], ch)}


@router.delete("/cookies")
async def clear_cookies(user: dict = Depends(get_current_user)):
    _need(user)
    await _bs.MANAGER.drop_user(user["id"])       # erst schliessen, sonst schreibt er sie zurueck
    store.clear_cookies(user["id"])
    return {"ok": True}


# ------------------------------------------------------------------ Downloads / Uploads
@router.get("/downloads")
def downloads(user: dict = Depends(get_current_user)):
    _need(user)
    return {"downloads": store.list_downloads(user["id"])}


@router.get("/downloads/{did}")
def download_file(did: str, user: dict = Depends(get_current_user)):
    _need(user)
    f = store.download_path(user["id"], did)
    if not f or not f.is_file():
        raise HTTPException(404, "Download not found")
    return FileResponse(f, filename=f.name.split("__", 1)[-1])


@router.delete("/downloads/{did}")
def delete_download(did: str, user: dict = Depends(get_current_user)):
    _need(user)
    f = store.download_path(user["id"], did)
    if f:
        f.unlink(missing_ok=True)
    return {"ok": True}


@router.post("/upload/{cid}")
async def upload(cid: str, files: list[UploadFile] = File(default=[]), user: dict = Depends(get_current_user)):
    _need(user)
    ub = _bs.MANAGER.users.get(user["id"])
    if not ub:
        raise HTTPException(409, "No browser session")
    tmp = store.download_dir(user["id"]) / ".uploads" / cid
    tmp.mkdir(parents=True, exist_ok=True)
    paths = []
    for f in files[:20]:
        name = "".join(c for c in (f.filename or "file") if c not in '\\/:*?"<>|')[:180] or "file"
        p = tmp / name
        with p.open("wb") as out:
            shutil.copyfileobj(f.file, out)
        paths.append(p)
    ok = await ub.fill_chooser(cid, paths)

    async def later():                             # nach dem Hochladen in die Seite aufraeumen
        await asyncio.sleep(600)
        shutil.rmtree(tmp, ignore_errors=True)
    asyncio.get_running_loop().create_task(later())
    if not ok:
        raise HTTPException(409, "The page's file chooser is no longer open")
    return {"ok": True, "count": len(paths)}


# ------------------------------------------------------------------ Live-Browser (WebSocket)
MAX_INFLIGHT = 2               # frames sent but not yet drawn by the dashboard


class Viewer:
    """One connected view. Flow control like remote desktop: the dashboard
    acknowledges every frame it has drawn ({"cmd": "ack"}); at most MAX_INFLIGHT
    frames are on the way. Meanwhile only the NEWEST frame is kept - older ones
    are dropped instead of piling up in network buffers (= no growing delay on
    slow links)."""

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self._json: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._frame: bytes | None = None
        self.inflight = 0
        self.last_sent = 0.0
        self._wake = asyncio.Event()
        self.alive = True
        self.task = asyncio.get_running_loop().create_task(self._sender())

    def send_json(self, obj: dict) -> None:
        if not self.alive:
            return
        try:
            self._json.put_nowait(json.dumps(obj))
            self._wake.set()
        except asyncio.QueueFull:
            pass

    def send_frame(self, blob: bytes) -> None:
        if not self.alive:
            return
        self._frame = blob                              # replaces an unsent older frame
        self._wake.set()

    def ack(self) -> None:
        self.inflight = max(0, self.inflight - 1)
        self._wake.set()

    async def _sender(self) -> None:
        import time
        try:
            while self.alive:
                await self._wake.wait()
                self._wake.clear()
                while not self._json.empty():
                    await self.ws.send_text(self._json.get_nowait())
                if self._frame is not None:
                    # safety net: an old dashboard without acks / a lost ack must not freeze the picture
                    if self.inflight >= MAX_INFLIGHT and time.time() - self.last_sent > 3:
                        self.inflight = 0
                    if self.inflight < MAX_INFLIGHT:
                        blob, self._frame = self._frame, None
                        self.inflight += 1
                        self.last_sent = time.time()
                        await self.ws.send_bytes(blob)
                    else:
                        asyncio.get_running_loop().call_later(0.5, self._wake.set)
        except Exception:
            self.alive = False

    def kick(self) -> None:
        """Server session was reset (e.g. cookies cleared): let the dashboard reconnect."""
        try:
            asyncio.get_running_loop().create_task(self.ws.close(code=4000))
        except Exception:
            pass

    def close(self) -> None:
        self.alive = False
        self._wake.set()
        self.task.cancel()


class InputLane:
    """Ordered input events. Mouse moves and wheel ticks that are still waiting
    are merged (only the newest position counts, wheel deltas are added up) -
    a slow page can never make the mouse lag behind."""

    def __init__(self):
        self.items: list[dict] = []
        self.ev = asyncio.Event()

    def put(self, m: dict) -> None:
        ev = m.get("ev") or {}
        last = self.items[-1] if self.items else None
        lev = (last or {}).get("ev") or {}
        if last and last.get("tab") == m.get("tab"):
            if ev.get("type") == "mouse" and ev.get("action") == "move" and lev.get("type") == "mouse" \
                    and lev.get("action") == "move":
                self.items[-1] = m
                return
            if ev.get("type") == "wheel" and lev.get("type") == "wheel":
                lev["dx"] = float(lev.get("dx", 0)) + float(ev.get("dx", 0))
                lev["dy"] = float(lev.get("dy", 0)) + float(ev.get("dy", 0))
                lev["x"], lev["y"] = ev.get("x", lev.get("x")), ev.get("y", lev.get("y"))
                return
        if len(self.items) < 1000:
            self.items.append(m)
        self.ev.set()

    async def get(self) -> dict:
        while not self.items:
            self.ev.clear()
            await self.ev.wait()
        return self.items.pop(0)


@router.websocket("/ws")
async def browser_ws(websocket: WebSocket):
    """?win=main (the normal browser) or ?win=app:<key>&url=...&network=... (web-app window)."""
    offered = websocket.scope.get("subprotocols") or []
    token = next((p[len("rmmtoken."):] for p in offered if p.startswith("rmmtoken.")), "")
    try:
        user = db.get_user_by_id(decode_access_token(token).get("sub"))
    except Exception:
        user = None
    if not user or not _perm(user):
        await websocket.close(code=4403)
        return
    await websocket.accept(subprotocol="rmmtoken." + token if token else None)
    qp = websocket.query_params
    wid = (qp.get("win") or _bs.MAIN)[:200]
    if wid != _bs.MAIN and not wid.startswith("app:"):
        wid = _bs.MAIN
    viewer = Viewer(websocket)
    viewer.send_json({"t": "starting"})
    try:
        ub = await _bs.MANAGER.for_user(user)
        win = await ub.window(wid, url=qp.get("url") or None, network=qp.get("network") or None)
    except Exception as e:
        viewer.send_json({"t": "error", "msg": str(e)[:500], "fatal": True})
        await asyncio.sleep(0.3)
        viewer.close()
        try:
            await websocket.close()
        except Exception:
            pass
        return
    origin = websocket.headers.get("origin") or ""
    if origin.startswith(("http://", "https://")):
        ub.parent = origin                          # proxy pages may only be framed by the dashboard
    ub.add_viewer(win, viewer)
    viewer.send_json(win.state())

    # Two ordered lanes + an express lane: a page action can block (e.g. a click
    # that opens alert() waits until the dialog is answered) - the answer must
    # not queue up behind it.
    lanes = {"input": InputLane(), "ctl": asyncio.Queue()}

    async def run(m):
        try:
            await _command(ub, win, viewer, m)
        except Exception as e:
            viewer.send_json({"t": "error", "msg": str(e)[:300]})

    async def worker(q):
        while True:
            m = await q.get()
            await run(m)

    workers = [asyncio.get_running_loop().create_task(worker(q)) for q in lanes.values()]
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                m = json.loads(raw)
            except Exception:
                continue
            cmd = m.get("cmd")
            if cmd == "ack":
                viewer.ack()
            elif cmd in EXPRESS:
                asyncio.get_running_loop().create_task(run(m))
            elif cmd == "input":
                lanes["input"].put(m)
            else:
                lanes["ctl"].put_nowait(m)
    except Exception:
        pass
    finally:
        for w in workers:
            w.cancel()
        ub.remove_viewer(win, viewer)
        viewer.close()


EXPRESS = {"dialog", "select", "chooser-cancel", "ctx", "find", "copy", "stop", "refresh-networks"}


async def _command(ub: _bs.UserBrowser, win: _bs.Win, viewer: Viewer, m: dict) -> None:
    cmd = m.get("cmd")
    tab = ub.tab(m.get("tab"), win)
    if cmd == "hello":
        await ub.resize(win, m.get("w", 1280), m.get("h", 800))
        await ub.snapshot(ub.tab(win.active))
    elif cmd == "resize":
        await ub.resize(win, m.get("w", 1280), m.get("h", 800))
    elif cmd == "input":
        await ub.input(tab, m.get("ev") or {})
    elif cmd == "new-tab":
        act = ub.tab(win.active)
        idx = (win.tabs.index(act) + 1) if m.get("next") and act in win.tabs else None
        await ub.new_tab(win, m.get("url") or None, network=m.get("network") or None,
                         activate=not m.get("background"), index=idx)
    elif cmd == "close-tab":
        await ub.close_tab(tab)
    elif cmd == "close-others":
        for t in [t for t in win.tabs if tab and t is not tab]:
            await ub.close_tab(t)
    elif cmd == "activate":
        await ub.activate(tab)
    elif cmd == "move-tab":
        await ub.move_tab(tab, int(m.get("index") or 0))
    elif cmd == "duplicate":
        await ub.duplicate(tab)
    elif cmd == "navigate":
        await ub.navigate(tab, m.get("url") or "")
    elif cmd == "back":
        await ub.history_go(tab, -1)
    elif cmd == "forward":
        await ub.history_go(tab, 1)
    elif cmd == "reload":
        await ub.reload(tab, bool(m.get("hard")))
    elif cmd == "stop":
        await ub.stop(tab)
    elif cmd == "home":
        await ub.navigate(tab, store.settings(ub.uid).get("homepage") or _bs.NEWTAB)
    elif cmd == "zoom":
        await ub.set_zoom(tab, m.get("zoom", 1.0))
    elif cmd == "find":
        r = await ub.find(tab, str(m.get("text") or ""), bool(m.get("backwards")))
        viewer.send_json({"t": "find", **r})
    elif cmd == "ctx":
        info = await ub.element_at(tab, float(m.get("x", 0)), float(m.get("y", 0)))
        viewer.send_json({"t": "ctx", "x": m.get("x"), "y": m.get("y"), "info": info})
    elif cmd == "select":
        await ub.choose_select(win, m.get("values"))
    elif cmd == "dialog":
        await ub.answer_dialog(str(m.get("id") or ""), bool(m.get("accept")), str(m.get("text") or ""))
    elif cmd == "chooser-cancel":
        ub.cancel_chooser(str(m.get("id") or ""))
    elif cmd == "copy":
        await ub.copy_selection(tab)
    elif cmd == "select-all":
        await ub.select_all(tab)
    elif cmd == "save-url":
        await ub.save_url(tab, str(m.get("url") or ""))
    elif cmd == "network":
        await ub.set_network(tab, str(m.get("network") or "direct"))
    elif cmd == "refresh-networks":
        win.emit_state()
    elif cmd == "proxy-state":
        ub.proxy_state(tab, m.get("state") or {})
    elif cmd == "mode":
        await ub.set_mode(tab, str(m.get("mode") or ""))
