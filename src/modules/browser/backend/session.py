# session.py (Modul "browser") - serverseitige Browser-Sitzungen mit Live-Stream
# ----------------------------------------------------------------------------
# Ein echter Chromium (Playwright, headless) laeuft im Backend. Jede
# Dashboard-Sitzung bekommt einen eigenen BrowserContext; darin mehrere Seiten
# (Tabs). Der Bildschirm jeder Seite wird per CDP-Screencast als JPEG an das
# Dashboard gestreamt; Maus/Tastatur/Scroll gehen zurueck in die Seite.
# Weil es ein ECHTER Browser ist, funktionieren Bilder, JavaScript, Cookies und
# POST ohne Zusatzaufwand.
#
# Optional ueber einen Proxy (z.B. ein VPN-Endpunkt), damit interne Seiten im
# Netz des Backends/Agenten erreichbar sind (siehe set_proxy / Modul-Einstellung).
from __future__ import annotations

import asyncio
import base64
import time
from typing import Callable

try:
    from playwright.async_api import async_playwright
    _PW_OK = True
except Exception:                       # Playwright nicht installiert
    _PW_OK = False

_DEFAULT_VIEWPORT = {"width": 1280, "height": 800}
_IDLE_CLOSE = 15 * 60                   # Sitzung ohne Aktivitaet nach 15 min schliessen


def available() -> tuple[bool, str]:
    if not _PW_OK:
        return False, "Python-Paket 'playwright' fehlt (pip install playwright)."
    return True, ""


class Tab:
    def __init__(self, tab_id: str, page):
        self.id = tab_id
        self.page = page
        self.title = ""
        self.url = ""
        self.loading = False
        self.streaming = False
        self._cdp = None


class Session:
    """Alle Tabs EINER Dashboard-Sitzung. Frames gehen ueber on_frame an die WS."""

    def __init__(self, sid: str, context, owner: str):
        self.sid = sid
        self.context = context
        self.owner = owner
        self.tabs: dict[str, Tab] = {}
        self.active: str | None = None
        self._seq = 0
        self.last_active = time.time()
        self.on_frame: Callable | None = None     # (tab_id, b64_jpeg) -> coroutine/none
        self.on_meta: Callable | None = None       # (tab_id, dict) -> None
        self.quality = 55

    def touch(self):
        self.last_active = time.time()

    # ---------------------------------------------------------------- Tabs
    async def new_tab(self, url: str = "about:blank") -> str:
        self.touch()
        page = await self.context.new_page()
        self._seq += 1
        tid = f"t{self._seq}"
        tab = Tab(tid, page)
        self.tabs[tid] = tab
        self.active = tid
        page.on("framenavigated", lambda frame, _t=tab: self._nav(_t, frame))
        page.on("load", lambda _p, _t=tab: self._loaded(_t))
        page.on("close", lambda _p, _t=tab: self.tabs.pop(_t.id, None))
        await self._start_stream(tab)
        if url and url != "about:blank":
            await self.navigate(tid, url)
        return tid

    async def close_tab(self, tid: str):
        self.touch()
        tab = self.tabs.pop(tid, None)
        if not tab:
            return
        try:
            await tab.page.close()
        except Exception:
            pass
        if self.active == tid:
            self.active = next(iter(self.tabs), None)

    def _nav(self, tab: Tab, frame):
        try:
            if frame == tab.page.main_frame:
                tab.url = frame.url
                self._emit_meta(tab)
        except Exception:
            pass

    def _loaded(self, tab: Tab):
        async def go():
            try:
                tab.title = await tab.page.title()
                tab.url = tab.page.url
                tab.loading = False
                self._emit_meta(tab)
            except Exception:
                pass
        asyncio.get_event_loop().create_task(go())

    def _emit_meta(self, tab: Tab):
        if self.on_meta:
            try:
                self.on_meta(tab.id, {"url": tab.url, "title": tab.title, "loading": tab.loading,
                                      "canBack": True, "canForward": True})
            except Exception:
                pass

    # ---------------------------------------------------------------- Navigation
    async def navigate(self, tid: str, url: str):
        self.touch()
        tab = self.tabs.get(tid)
        if not tab:
            return
        url = _normalize_url(url)
        tab.loading = True
        self._emit_meta(tab)
        try:
            await tab.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            try:
                await tab.page.set_content(
                    f"<body style='font-family:sans-serif;padding:40px;color:#333'>"
                    f"<h2>Seite konnte nicht geladen werden</h2><p>{_esc(url)}</p>"
                    f"<pre style='color:#a00'>{_esc(str(e))}</pre></body>")
            except Exception:
                pass
        tab.loading = False
        self._loaded(tab)

    async def back(self, tid: str):
        self.touch()
        t = self.tabs.get(tid)
        if t:
            try: await t.page.go_back(timeout=15000)
            except Exception: pass

    async def forward(self, tid: str):
        self.touch()
        t = self.tabs.get(tid)
        if t:
            try: await t.page.go_forward(timeout=15000)
            except Exception: pass

    async def reload(self, tid: str):
        self.touch()
        t = self.tabs.get(tid)
        if t:
            try: await t.page.reload(timeout=30000)
            except Exception: pass

    # ---------------------------------------------------------------- Eingaben
    async def input(self, tid: str, ev: dict):
        self.touch()
        tab = self.tabs.get(tid)
        if not tab:
            return
        p = tab.page
        try:
            kind = ev.get("type")
            if kind == "move":
                await p.mouse.move(float(ev["x"]), float(ev["y"]))
            elif kind == "down":
                await p.mouse.move(float(ev["x"]), float(ev["y"]))
                await p.mouse.down(button=ev.get("button", "left"))
            elif kind == "up":
                await p.mouse.up(button=ev.get("button", "left"))
            elif kind == "click":
                await p.mouse.click(float(ev["x"]), float(ev["y"]), button=ev.get("button", "left"))
            elif kind == "dblclick":
                await p.mouse.dblclick(float(ev["x"]), float(ev["y"]))
            elif kind == "wheel":
                await p.mouse.wheel(float(ev.get("dx", 0)), float(ev.get("dy", 0)))
            elif kind == "key":
                await p.keyboard.press(ev["key"])
            elif kind == "keydown":
                await p.keyboard.down(ev["key"])
            elif kind == "keyup":
                await p.keyboard.up(ev["key"])
            elif kind == "text":
                await p.keyboard.insert_text(ev.get("text", ""))
        except Exception:
            pass

    async def resize(self, w: int, h: int):
        self.touch()
        vp = {"width": max(400, min(2560, int(w))), "height": max(300, min(1600, int(h)))}
        for tab in self.tabs.values():
            try:
                await tab.page.set_viewport_size(vp)
            except Exception:
                pass

    # ---------------------------------------------------------------- Screencast (CDP)
    async def _start_stream(self, tab: Tab):
        if tab.streaming:
            return
        try:
            cdp = await self.context.new_cdp_session(tab.page)
            tab._cdp = cdp

            def _on_frame(params):
                asyncio.get_event_loop().create_task(self._frame(tab, cdp, params))
            cdp.on("Page.screencastFrame", _on_frame)
            await cdp.send("Page.startScreencast", {
                "format": "jpeg", "quality": self.quality,
                "maxWidth": 1600, "maxHeight": 1000, "everyNthFrame": 1})
            tab.streaming = True
        except Exception:
            tab.streaming = False

    async def _frame(self, tab: Tab, cdp, params):
        try:
            await cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
        except Exception:
            pass
        if self.on_frame and tab.id == self.active:
            try:
                await self.on_frame(tab.id, params["data"])     # data = base64-JPEG
            except Exception:
                pass

    async def set_active(self, tid: str):
        self.touch()
        if tid in self.tabs:
            self.active = tid
            # beim Umschalten einmal das aktuelle Bild erzwingen
            t = self.tabs[tid]
            try:
                if t._cdp:
                    await t._cdp.send("Page.stopScreencast")
                    await t._cdp.send("Page.startScreencast", {"format": "jpeg", "quality": self.quality,
                                                               "maxWidth": 1600, "maxHeight": 1000, "everyNthFrame": 1})
            except Exception:
                pass

    async def close(self):
        for tab in list(self.tabs.values()):
            try: await tab.page.close()
            except Exception: pass
        try:
            await self.context.close()
        except Exception:
            pass
        self.tabs.clear()


class Manager:
    def __init__(self):
        self._pw = None
        self._browser = None
        self.sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()
        self._proxy: str | None = None
        self._reaper: asyncio.Task | None = None

    def set_proxy(self, proxy: str | None):
        self._proxy = (proxy or "").strip() or None

    async def _ensure_browser(self):
        if self._browser:
            return
        async with self._lock:
            if self._browser:
                return
            self._pw = await async_playwright().start()
            args = ["--no-sandbox", "--disable-dev-shm-usage"]
            launch = {"headless": True, "args": args}
            if self._proxy:
                launch["proxy"] = {"server": self._proxy}
            self._browser = await self._pw.chromium.launch(**launch)
            if self._reaper is None:
                self._reaper = asyncio.get_event_loop().create_task(self._reap())

    async def get_or_create(self, sid: str, owner: str) -> Session:
        ok, why = available()
        if not ok:
            raise RuntimeError(why)
        await self._ensure_browser()
        s = self.sessions.get(sid)
        if s:
            s.touch()
            return s
        ctx = await self._browser.new_context(viewport=dict(_DEFAULT_VIEWPORT), ignore_https_errors=True)
        s = Session(sid, ctx, owner)
        self.sessions[sid] = s
        return s

    async def drop(self, sid: str):
        s = self.sessions.pop(sid, None)
        if s:
            await s.close()

    async def _reap(self):
        while True:
            await asyncio.sleep(60)
            now = time.time()
            for sid in [k for k, v in self.sessions.items() if now - v.last_active > _IDLE_CLOSE]:
                await self.drop(sid)

    async def shutdown(self):
        for sid in list(self.sessions):
            await self.drop(sid)
        try:
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._browser = None


MANAGER = Manager()


# ------------------------------------------------------------------ Helfer
def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return "about:blank"
    if url.startswith(("http://", "https://", "about:", "file://", "data:")):
        return url
    # sieht es nach einer Suche aus (Leerzeichen, kein Punkt) -> Suchmaschine
    if (" " in url) or ("." not in url.split("/")[0]):
        from urllib.parse import quote
        return "https://duckduckgo.com/?q=" + quote(url)
    return "https://" + url


def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
