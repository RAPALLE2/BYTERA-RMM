# session.py (module "browser") - server-side browser with live streaming
# ----------------------------------------------------------------------
# A real Chromium (Playwright, headless) runs inside the backend. Because it is
# a real browser everything simply works: images, JavaScript, cookies/logins,
# forms with POST, uploads, downloads, popups.
#
# Layout:
#   Manager        one Chromium process for everybody
#   UserBrowser    ONE browser per user. It keeps living in the backend when the
#                  dashboard window is closed or the page is reloaded. Idle
#                  browsers (no viewer) are closed after IDLE_CLOSE; the tabs of
#                  the main window are saved and restored next time.
#   Win            a browser window, like a Firefox window: own tab strip, own
#                  active tab, own viewport. "main" is the normal browser (saved,
#                  identical on every device of the user); every web app gets its
#                  own window "app:<key>" (not saved).
#   Container      one BrowserContext per network ("direct" or an agent link from
#                  the VPN module) with its own SOCKS5 exit (socks.py) and its own
#                  cookies - like Firefox containers. Cookies are stored encrypted.
#   Tab            one page. Only the ACTIVE tab of a watched window streams
#                  (CDP screencast, JPEG).
#
# Headless Chromium draws no native popups (select lists, context menu,
# alert/confirm, file chooser). They are detected here and rebuilt by the
# dashboard (messages "select", "ctx", "dialog", "filechooser").
from __future__ import annotations

import asyncio
import base64
import re
import secrets
import time
from pathlib import Path

from . import install, netsel, proxy, store
from .socks import SocksServer

try:
    from playwright.async_api import async_playwright
    _PW_OK = True
except Exception:
    _PW_OK = False

IDLE_CLOSE = 30 * 60          # no viewer for 30 min -> close the browser (main tabs stay saved)
APP_WIN_GRACE = 90            # web-app window without viewer -> closed after 90 s
MAX_TABS = 60                 # per user, all windows
DEFAULT_VP = (1280, 800)
JPEG_QUALITY = 55             # live frames while something moves (small = low latency)
JPEG_SHARP = 85               # one sharp frame once the picture stands still
SHARP_AFTER = 0.35            # seconds without a new frame -> send the sharp frame
NEWTAB = "about:newtab"       # drawn by the dashboard itself (new-tab page)
MAIN = "main"


def available() -> tuple[bool, str]:
    if not _PW_OK:
        return False, "Python package 'playwright' is missing (re-enable the module to install it)."
    st = install.status()
    if st["status"] == "missing":
        return False, st.get("detail") or "Chromium is not installed"
    return True, ""


# ------------------------------------------------------------------ page scripts
JS_AT = r"""([x, y]) => {
  let el = document.elementFromPoint(x, y);
  if (!el) return null;
  const a = el.closest && el.closest('a[href]');
  const img = el.closest && el.closest('img');
  const sel = el.closest && el.closest('select');
  const ed = el.closest && el.closest('input, textarea, [contenteditable=""], [contenteditable="true"]');
  const s = String(window.getSelection ? window.getSelection() : '');
  let cursor = 'default';
  try { cursor = getComputedStyle(el).cursor || 'default'; } catch (e) {}
  if (cursor === 'auto') cursor = a ? 'pointer' : (ed ? 'text' : 'default');
  return {
    link: a ? a.href : '', linkText: a ? (a.innerText || '').trim().slice(0, 200) : '',
    img: img ? (img.currentSrc || img.src || '') : '',
    select: !!sel && !sel.disabled, editable: !!ed, selection: s.slice(0, 5000), cursor,
  };
}"""

JS_SELECT_INFO = r"""([x, y]) => {
  const el = document.elementFromPoint(x, y);
  const sel = el && el.closest && el.closest('select');
  if (!sel || sel.disabled) return null;
  const r = sel.getBoundingClientRect();
  const opts = [];
  for (const o of sel.options) opts.push({ label: o.label || o.text, value: o.value, selected: o.selected,
                                           disabled: o.disabled, group: o.parentElement && o.parentElement.tagName === 'OPTGROUP' ? o.parentElement.label : '' });
  sel.focus();
  return { multiple: sel.multiple, options: opts, rect: { x: r.left, y: r.top, w: r.width, h: r.height } };
}"""

JS_SELECT_SET = r"""([x, y, idxs]) => {
  const el = document.elementFromPoint(x, y);
  const sel = el && el.closest && el.closest('select');
  if (!sel) return false;
  for (let i = 0; i < sel.options.length; i++) sel.options[i].selected = idxs.includes(i);
  sel.dispatchEvent(new Event('input', { bubbles: true }));
  sel.dispatchEvent(new Event('change', { bubbles: true }));
  return true;
}"""

JS_SELECTION = r"""() => {
  const a = document.activeElement;
  if (a && (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA') && typeof a.selectionStart === 'number')
    return a.value.substring(a.selectionStart, a.selectionEnd);
  return String(window.getSelection ? window.getSelection() : '');
}"""

JS_ICON = r"""() => {
  const l = document.querySelector('link[rel~="icon"], link[rel="shortcut icon"], link[rel="apple-touch-icon"]');
  return l ? l.href : (location.protocol.startsWith('http') ? location.origin + '/favicon.ico' : '');
}"""

JS_FIND_COUNT = r"""(q) => {
  if (!q) return 0;
  const t = (document.body && document.body.innerText || '').toLowerCase();
  let n = 0, i = 0; q = q.toLowerCase();
  while ((i = t.indexOf(q, i)) !== -1) { n++; i += q.length; }
  return n;
}"""


def _modmask(ev: dict) -> int:
    return (1 if ev.get("alt") else 0) | (2 if ev.get("ctrl") else 0) | (4 if ev.get("meta") else 0) | (8 if ev.get("shift") else 0)


def _task(coro):
    return asyncio.get_running_loop().create_task(coro)


class Tab:
    """mode "stream": Chromium page in the backend, picture streamed.
    mode "proxy": no page here - the dashboard renders the site in an iframe and
    the backend only passes the bytes through (proxy.py)."""

    def __init__(self, tid: str, page, container: "Container | None", win: "Win",
                 network: str = "direct", mode: str = "stream"):
        self.id = tid
        self.page = page
        self.container = container
        self.network = container.network if container else network
        self.mode = mode
        self.nav_seq = 0                   # proxy: +1 = dashboard must load tab.url into the iframe
        self.win = win
        self.title = ""
        self.url = ""
        self.intended = ""                 # last requested address (kept on error pages)
        self.error_shown = False
        self.favicon = ""
        self.loading = False
        self.can_back = False
        self.can_fwd = False
        self.zoom = 1.0
        self.pending_url: str | None = None  # restored but not loaded yet ("sleeping" tab)
        self.cdp = None
        self.main_frame: str | None = None
        self.streaming = False
        self.last_frame = 0.0
        self.crashed = False
        self.socks_err_mark = 0
        self.sharp_timer: asyncio.TimerHandle | None = None
        self.buttons = 0                   # pressed mouse buttons (bit mask) for drag moves
        self.hovering = False

    def view(self) -> dict:
        v = {"id": self.id, "title": self.title or self._fallback_title(), "url": self.pending_url or self.url,
             "favicon": self.favicon, "loading": self.loading, "canBack": self.can_back,
             "canForward": self.can_fwd, "network": self.network, "zoom": self.zoom,
             "crashed": self.crashed, "sleeping": bool(self.pending_url), "mode": self.mode}
        if self.mode == "proxy":
            v["navSeq"] = self.nav_seq
            u = self.url
            if u.startswith(("http://", "https://")) and not self.pending_url:
                try:
                    v["src"] = proxy.src_for(self.win.ub.user, u, self.network, self.win.ub.parent)
                except Exception as e:
                    v["src"] = ""
                    v["srcError"] = str(e)
        return v

    def _fallback_title(self) -> str:
        u = self.pending_url or self.url
        if not u or u in ("about:blank", NEWTAB):
            return "New Tab"
        return re.sub(r"^https?://", "", u)[:60]


class Container:
    """One network = one BrowserContext + its own SOCKS5 exit."""

    def __init__(self, ub: "UserBrowser", network: str):
        self.ub = ub
        self.network = network
        self.context = None
        self.socks: SocksServer | None = None

    async def start(self) -> None:
        await MANAGER.ensure_browser()                  # Chromium only when a stream tab needs it
        self.socks = SocksServer(netsel.make_router(self.ub.user, self.network), self.network)
        port = await self.socks.start()
        kw = dict(viewport={"width": DEFAULT_VP[0], "height": DEFAULT_VP[1]}, ignore_https_errors=True,
                  accept_downloads=True, locale="en-US",
                  proxy={"server": f"socks5://127.0.0.1:{port}", "bypass": "<-loopback>"})
        st = store.load_storage_state(self.ub.uid, self.network)
        if st:
            kw["storage_state"] = st
        try:
            self.context = await MANAGER.browser.new_context(**kw)
        except Exception:
            kw.pop("storage_state", None)                 # broken saved state -> start without
            self.context = await MANAGER.browser.new_context(**kw)
        self.context.on("page", self.ub._on_context_page)

    async def save_state(self) -> None:
        if not self.context:
            return
        try:
            store.save_storage_state(self.ub.uid, self.network, await self.context.storage_state())
        except Exception:
            pass

    async def close(self) -> None:
        await self.save_state()
        try:
            if self.context:
                await self.context.close()
        except Exception:
            pass
        if self.socks:
            await self.socks.stop()


class Win:
    """A browser window (own tabs, own active tab, own viewers)."""

    def __init__(self, ub: "UserBrowser", wid: str):
        self.ub = ub
        self.id = wid
        self.persist = wid == MAIN
        self.tabs: list[Tab] = []
        self.active: str | None = None
        self.viewers: set = set()
        self.vp = DEFAULT_VP
        self.select_hold: tuple | None = None     # mouse-down on a <select> intercepted
        self.hover_at = 0.0
        self.hover_last = None
        self.empty_since = 0.0

    def emit(self, obj: dict) -> None:
        for v in list(self.viewers):
            v.send_json(obj)

    def state(self) -> dict:
        return {"t": "state", "win": self.id, "tabs": [t.view() for t in self.tabs], "active": self.active,
                "networks": netsel.list_for(self.ub.user)}

    def emit_state(self) -> None:
        self.emit(self.state())
        if self.persist:
            self.ub._schedule_save()

    def emit_tab(self, tab: Tab) -> None:
        self.emit({"t": "tab", "tab": tab.view()})
        if self.persist:
            self.ub._schedule_save()


class UserBrowser:
    def __init__(self, user: dict):
        self.user = user
        self.uid = user["id"]
        self.containers: dict[str, Container] = {}
        self.wins: dict[str, Win] = {}
        self.last_seen = time.time()
        self._seq = 0
        self._own_pending: list[Win] = []          # windows waiting for their own new_page()
        self._dialogs: dict[str, tuple] = {}
        self._choosers: dict[str, object] = {}
        self._save_handle = None
        self._ticker: asyncio.Task | None = None
        self._wlock = asyncio.Lock()
        self.parent = ""                           # origin of the dashboard (frame-ancestors of proxy pages)
        self.closed = False

    # ---------------------------------------------------------------- helpers
    def all_tabs(self) -> list[Tab]:
        return [t for w in self.wins.values() for t in w.tabs]

    def tab(self, tid: str | None, win: Win | None = None) -> Tab | None:
        if not tid and win:
            tid = win.active
        return next((t for t in self.all_tabs() if t.id == tid), None)

    def viewers(self) -> int:
        return sum(len(w.viewers) for w in self.wins.values())

    # ---------------------------------------------------------------- windows / viewers
    async def window(self, wid: str, url: str | None = None, network: str | None = None) -> Win:
        async with self._wlock:
            w = self.wins.get(wid)
            if w:
                return w
            w = Win(self, wid)
            self.wins[wid] = w
            try:
                if wid == MAIN:
                    await self._restore(w)
                else:
                    await self.new_tab(w, url or NEWTAB, network=network)
            except Exception:
                self.wins.pop(wid, None)
                raise
            return w

    def add_viewer(self, win: Win, v) -> None:
        win.viewers.add(v)
        self.last_seen = time.time()
        if not self._ticker or self._ticker.done():
            self._ticker = _task(self._tick())
        act = self.tab(win.active)
        if act:
            _task(self._start_stream(act))

    def remove_viewer(self, win: Win, v) -> None:
        win.viewers.discard(v)
        self.last_seen = time.time()
        if not win.viewers:
            win.empty_since = time.time()
            act = self.tab(win.active)
            if act:
                _task(self._stop_stream(act))
            if not win.persist:
                _task(self._close_app_window_later(win))

    async def _close_app_window_later(self, win: Win) -> None:
        await asyncio.sleep(APP_WIN_GRACE)
        if win.viewers or self.wins.get(win.id) is not win:
            return
        await self.close_window(win)

    async def close_window(self, win: Win) -> None:
        self.wins.pop(win.id, None)
        for t in list(win.tabs):
            win.tabs.remove(t)
            if t.page is None:
                continue
            try:
                await t.page.close(run_before_unload=False)
            except Exception:
                pass

    # ---------------------------------------------------------------- start / end / save
    async def _restore(self, w: Win) -> None:
        s = store.settings(self.uid)
        sess = store.load_session(self.uid) if s.get("restore", True) else {"tabs": []}
        tabs = [t for t in (sess.get("tabs") or []) if t.get("url")][:MAX_TABS]
        if not tabs:
            await self.new_tab(w, s.get("homepage") or NEWTAB)
            return
        idx = min(int(sess.get("active") or 0), len(tabs) - 1)
        for i, t in enumerate(tabs):
            net = t.get("network") or "direct"
            if not netsel.valid(self.user, net):
                net = "direct"
            await self.new_tab(w, t["url"], network=net, activate=(i == idx), lazy=(i != idx),
                               title=t.get("title") or "", mode=t.get("mode"))

    async def close(self) -> None:
        self.closed = True
        self._save_now()
        for w in list(self.wins.values()):
            for v in list(w.viewers):
                getattr(v, "kick", lambda: None)()
        if self._ticker:
            self._ticker.cancel()
        for c in list(self.containers.values()):
            await c.close()
        self.containers.clear()
        self.wins.clear()

    def _schedule_save(self) -> None:
        try:
            if self._save_handle:
                self._save_handle.cancel()
            self._save_handle = asyncio.get_running_loop().call_later(2.0, self._save_now)
        except RuntimeError:
            pass

    def _save_now(self) -> None:
        w = self.wins.get(MAIN)
        if not w or not w.tabs:
            return
        tabs = []
        for t in w.tabs:
            url = t.pending_url or (t.intended if (t.url.startswith("chrome-error:") or t.error_shown) else t.url)
            tabs.append({"url": url or NEWTAB, "title": t.title, "network": t.network, "mode": t.mode})
        idx = next((i for i, t in enumerate(w.tabs) if t.id == w.active), 0)
        try:
            store.save_session(self.uid, tabs, idx)
        except Exception:
            pass

    # ---------------------------------------------------------------- containers / tabs
    async def container(self, network: str) -> Container:
        c = self.containers.get(network)
        if c:
            return c
        c = Container(self, network)
        await c.start()
        self.containers[network] = c
        return c

    def pick_mode(self, url: str | None, wanted: str | None = None) -> str:
        """proxy (dashboard renders) or stream (backend renders) for a new tab."""
        if wanted == "stream":
            return "stream"
        if not proxy.enabled():
            return "stream"
        if wanted == "proxy":
            return "proxy"
        if url and proxy.stream_only(url):
            return "stream"
        engine = store.settings(self.uid).get("engine") or "auto"
        return "stream" if engine == "stream" else "proxy"

    async def new_tab(self, win: Win, url: str | None = None, network: str | None = None, activate: bool = True,
                      index: int | None = None, lazy: bool = False, title: str = "", mode: str | None = None) -> Tab:
        if len(self.all_tabs()) >= MAX_TABS:
            raise RuntimeError(f"At most {MAX_TABS} tabs are allowed")
        network = network or store.settings(self.uid).get("default_network") or "direct"
        if not netsel.valid(self.user, network):
            network = "direct"
        url = url or NEWTAB
        if self.pick_mode(url, mode) == "proxy":
            self._seq += 1
            tab = Tab(f"t{self._seq}{secrets.token_hex(2)}", None, None, win, network=network, mode="proxy")
            if index is None or index < 0 or index > len(win.tabs):
                win.tabs.append(tab)
            else:
                win.tabs.insert(index, tab)
            if lazy:
                tab.pending_url, tab.title = url, title
            else:
                self._proxy_nav(tab, url)
            if activate:
                await self.activate(tab)
            else:
                win.emit_state()
            return tab
        c = await self.container(network)
        self._own_pending.append(win)
        try:
            page = await c.context.new_page()
        finally:
            if win in self._own_pending:
                self._own_pending.remove(win)
        tab = await self._register(page, c, win, index=index)
        url = url or NEWTAB
        if lazy:
            tab.pending_url = url
            tab.title = title
        elif url != NEWTAB:
            _task(self.navigate(tab, url))
        else:
            tab.url = NEWTAB
        if activate:
            await self.activate(tab)
        else:
            win.emit_state()
        return tab

    async def _register(self, page, c: Container, win: Win, index: int | None = None, url: str = "") -> Tab:
        self._seq += 1
        tab = Tab(f"t{self._seq}{secrets.token_hex(2)}", page, c, win)
        tab.url = url or page.url or ""
        try:
            await page.set_viewport_size({"width": win.vp[0], "height": win.vp[1]})
        except Exception:
            pass
        if index is None or index < 0 or index > len(win.tabs):
            win.tabs.append(tab)
        else:
            win.tabs.insert(index, tab)
        page.on("close", lambda _p, _t=tab: _task(self._on_closed(_t)))
        page.on("load", lambda _p, _t=tab: _task(self._on_load(_t)))
        page.on("framenavigated", lambda fr, _t=tab: self._on_nav(_t, fr))
        page.on("dialog", lambda d, _t=tab: _task(self._on_dialog(_t, d)))
        page.on("download", lambda d, _t=tab: _task(self._on_download(_t, d)))
        page.on("filechooser", lambda fc, _t=tab: self._on_chooser(_t, fc))
        page.on("crash", lambda _p, _t=tab: self._on_crash(_t))
        try:
            tab.cdp = await c.context.new_cdp_session(page)
            await tab.cdp.send("Page.enable")
            tree = await tab.cdp.send("Page.getFrameTree")
            tab.main_frame = tree["frameTree"]["frame"]["id"]
            tab.cdp.on("Page.frameStartedLoading", lambda p, _t=tab: self._on_loading(_t, p, True))
            tab.cdp.on("Page.frameStoppedLoading", lambda p, _t=tab: self._on_loading(_t, p, False))
            tab.cdp.on("Page.screencastFrame", lambda p, _t=tab: _task(self._frame(_t, p)))
        except Exception:
            tab.cdp = None
        return tab

    def _on_context_page(self, page) -> None:
        """New page in a context: our own (new_tab) or a popup/target=_blank -> new tab."""
        if self._own_pending:
            return
        _task(self._adopt_popup(page))

    async def _adopt_popup(self, page) -> None:
        await asyncio.sleep(0)
        if any(t.page is page for t in self.all_tabs()):
            return
        c = next((c for c in self.containers.values() if c.context is page.context), None)
        if not c:
            return
        opener = None
        try:
            opener = await page.opener()
        except Exception:
            pass
        ot = next((t for t in self.all_tabs() if opener is not None and t.page is opener), None)
        win = ot.win if ot else (self.wins.get(MAIN) or next(iter(self.wins.values()), None))
        if not win:
            return
        idx = win.tabs.index(ot) + 1 if ot else None
        tab = await self._register(page, c, win, index=idx, url=page.url)
        tab.loading = True
        await self.activate(tab)

    async def close_tab(self, tab: Tab | None) -> None:
        if not tab:
            return
        if tab.page is None:
            await self._on_closed(tab)
            return
        try:
            await tab.page.close(run_before_unload=False)
        except Exception:
            await self._on_closed(tab)

    async def _on_closed(self, tab: Tab) -> None:
        win = tab.win
        if tab not in win.tabs:
            return
        i = win.tabs.index(tab)
        win.tabs.remove(tab)
        if self.wins.get(win.id) is not win or self.closed:
            return
        if win.active == tab.id:
            win.active = None
            if win.tabs:
                await self.activate(win.tabs[min(i, len(win.tabs) - 1)])
                return
        if not win.tabs:
            await self.new_tab(win, NEWTAB)          # like Firefox: closing the last tab opens a new one
            return
        win.emit_state()

    async def move_tab(self, tab: Tab | None, index: int) -> None:
        if not tab:
            return
        w = tab.win
        w.tabs.remove(tab)
        w.tabs.insert(max(0, min(int(index), len(w.tabs))), tab)
        w.emit_state()

    async def duplicate(self, tab: Tab | None) -> None:
        if tab:
            await self.new_tab(tab.win, tab.pending_url or tab.intended or tab.url, network=tab.network,
                               index=tab.win.tabs.index(tab) + 1, mode=tab.mode)

    async def set_network(self, tab: Tab | None, network: str) -> None:
        """Move a tab into another network (reloads it there)."""
        if not tab or tab.network == network or not netsel.valid(self.user, network):
            return
        w = tab.win
        url = tab.pending_url or tab.intended or tab.url
        await self.new_tab(w, url, network=network, index=w.tabs.index(tab), activate=(w.active == tab.id),
                           mode=tab.mode)
        await self.close_tab(tab)

    async def set_mode(self, tab: Tab | None, mode: str) -> None:
        """Switch a tab between proxy (dashboard renders) and stream (backend renders)."""
        if not tab or tab.mode == mode or mode not in ("proxy", "stream"):
            return
        if mode == "proxy" and not proxy.enabled():
            raise RuntimeError("Pass-through mode is not set up (module setting “Proxy hostname”).")
        w = tab.win
        url = tab.pending_url or tab.intended or tab.url
        await self.new_tab(w, url, network=tab.network, index=w.tabs.index(tab), activate=(w.active == tab.id),
                           mode=mode)
        await self.close_tab(tab)

    # ---------------------------------------------------------------- proxy tabs
    def _proxy_nav(self, tab: Tab, url: str) -> None:
        tab.pending_url = None
        if url == NEWTAB:
            tab.url, tab.title, tab.favicon, tab.loading = NEWTAB, "", "", False
        else:
            tab.url = tab.intended = url
            tab.loading = True
            tab.title = ""
            tab.favicon = ""
        tab.nav_seq += 1

    def proxy_state(self, tab: Tab | None, d: dict) -> None:
        """Report of the page inside the dashboard iframe (inject.js)."""
        if not tab or tab.mode != "proxy":
            return
        url = str(d.get("url") or "")
        if url.startswith(("http://", "https://")):
            if url != tab.url:
                tab.url = tab.intended = url[:4000]
        if "title" in d:
            tab.title = str(d.get("title") or "")[:300]
        if d.get("icon"):
            tab.favicon = str(d["icon"])[:2000]
        if "loading" in d:
            was = tab.loading
            tab.loading = bool(d["loading"])
            if was and not tab.loading and tab.url.startswith(("http://", "https://")):
                store.add_history(self.uid, tab.url, tab.title)
        if "canBack" in d:
            tab.can_back = bool(d["canBack"])
        if "canForward" in d:
            tab.can_fwd = bool(d["canForward"])
        tab.win.emit_tab(tab)

    def _proxy_cmd(self, tab: Tab, **cmd) -> None:
        tab.win.emit({"t": "proxy-cmd", "tab": tab.id, **cmd})

    # ---------------------------------------------------------------- active tab + stream
    async def activate(self, tab: Tab | None) -> None:
        if not tab:
            return
        w = tab.win
        old = self.tab(w.active)
        if old and old is not tab:
            await self._stop_stream(old)
        w.active = tab.id
        if tab.pending_url:
            url, tab.pending_url = tab.pending_url, None
            if tab.mode == "proxy":
                self._proxy_nav(tab, url)
            elif url != NEWTAB:
                _task(self.navigate(tab, url))
            else:
                tab.url = NEWTAB
        if tab.page is not None:
            try:
                await tab.page.bring_to_front()
            except Exception:
                pass
        if w.viewers:
            await self._start_stream(tab)
        w.emit_state()

    async def _start_stream(self, tab: Tab) -> None:
        if not tab.cdp or tab.streaming:
            return
        try:
            vp = tab.win.vp
            await tab.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": JPEG_QUALITY,
                                                        "maxWidth": vp[0], "maxHeight": vp[1], "everyNthFrame": 1})
            tab.streaming = True
            started = time.time()

            async def nudge():                         # static page: force one picture
                await asyncio.sleep(0.7)
                if tab.streaming and tab.last_frame < started and tab.win.active == tab.id:
                    await self.snapshot(tab)
            _task(nudge())
        except Exception:
            tab.streaming = False

    async def _stop_stream(self, tab: Tab) -> None:
        if tab.cdp and tab.streaming:
            try:
                await tab.cdp.send("Page.stopScreencast")
            except Exception:
                pass
        tab.streaming = False

    async def _frame(self, tab: Tab, params: dict) -> None:
        # Ack at once: Chromium never waits for the network. Flow control happens
        # per viewer (browser_routes.Viewer): only the NEWEST frame is kept, and a
        # frame is only sent when the dashboard has drawn the previous ones - so no
        # stale pictures pile up in network buffers (that was the big lag).
        try:
            _task(tab.cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]}))
        except Exception:
            pass
        tab.last_frame = time.time()
        if tab.win.active == tab.id and tab.win.viewers:
            self._send_frame(tab, base64.b64decode(params["data"]))
            self._schedule_sharp(tab)

    def _send_frame(self, tab: Tab, jpeg: bytes) -> None:
        tid = tab.id.encode()
        blob = bytes([len(tid)]) + tid + jpeg
        for v in list(tab.win.viewers):
            v.send_frame(blob)

    def _schedule_sharp(self, tab: Tab) -> None:
        if tab.sharp_timer:
            tab.sharp_timer.cancel()
        tab.sharp_timer = asyncio.get_running_loop().call_later(SHARP_AFTER, lambda: _task(self._sharp(tab)))

    async def _sharp(self, tab: Tab) -> None:
        """The picture stands still: replace the light live frame by a sharp one."""
        tab.sharp_timer = None
        if tab.win.active != tab.id or not tab.win.viewers or time.time() - tab.last_frame < SHARP_AFTER * 0.9:
            return
        await self.snapshot(tab, JPEG_SHARP)

    async def snapshot(self, tab: Tab | None, quality: int = JPEG_SHARP) -> None:
        if not tab or tab.page is None or tab.url == NEWTAB or tab.pending_url:
            return
        try:
            if tab.cdp:                               # CDP directly: faster than page.screenshot
                r = await tab.cdp.send("Page.captureScreenshot", {"format": "jpeg", "quality": quality,
                                                                   "optimizeForSpeed": True})
                jpeg = base64.b64decode(r["data"])
            else:
                jpeg = await tab.page.screenshot(type="jpeg", quality=quality, timeout=5000)
            if tab.win.active == tab.id:
                self._send_frame(tab, jpeg)
        except Exception:
            pass

    async def resize(self, win: Win, w: int, h: int) -> None:
        w = max(320, min(3840, int(w)))
        h = max(200, min(2400, int(h)))
        if (w, h) == win.vp:
            return
        win.vp = (w, h)
        for t in win.tabs:
            try:
                await t.page.set_viewport_size({"width": w, "height": h})
            except Exception:
                pass
        act = self.tab(win.active)
        if act and act.streaming:                     # restart screencast with the new size
            await self._stop_stream(act)
            await self._start_stream(act)

    # ---------------------------------------------------------------- page events
    def _on_loading(self, tab: Tab, p: dict, started: bool) -> None:
        if p.get("frameId") != tab.main_frame:
            return
        tab.loading = started
        if started:
            tab.crashed = False
        tab.win.emit_tab(tab)

    def _on_nav(self, tab: Tab, frame) -> None:
        try:
            if frame != tab.page.main_frame:
                return
            if frame.url.startswith("chrome-error:"):
                tab.url = tab.intended or tab.url     # the address bar keeps the requested address
                return
            if frame.url == "about:blank" and tab.error_shown:
                return
            tab.error_shown = False
            tab.url = frame.url
            tab.favicon = ""
            _task(self._update_history(tab))
        except Exception:
            pass

    async def _update_history(self, tab: Tab) -> None:
        if tab.cdp:
            try:
                h = await tab.cdp.send("Page.getNavigationHistory")
                i, n = h["currentIndex"], len(h["entries"])
                first_real = 1 if n and h["entries"][0]["url"] == "about:blank" else 0
                tab.can_back = i > first_real
                tab.can_fwd = i < n - 1
            except Exception:
                pass
        tab.win.emit_tab(tab)

    async def _on_load(self, tab: Tab) -> None:
        if tab.page.url.startswith("chrome-error:"):
            if not tab.error_shown:                  # our own, readable error page
                tab.error_shown = True
                errs = tab.container.socks.errors if tab.container.socks else []
                hint = errs[-1] if len(errs) > tab.socks_err_mark else ""
                try:
                    code = await tab.page.evaluate("(document.querySelector('.error-code')||{}).textContent||''")
                except Exception:
                    code = ""
                await self._show_error(tab, tab.intended or tab.url, code.strip(), hint)
            tab.url = tab.intended or tab.url
            tab.title = "Problem loading page"
            tab.loading = False
            await self._update_history(tab)
            return
        if tab.error_shown and tab.page.url == "about:blank":
            return
        try:
            tab.title = await tab.page.title()
        except Exception:
            pass
        tab.url = tab.page.url
        tab.intended = tab.url
        tab.loading = False
        if tab.zoom != 1.0:
            await self._apply_zoom(tab)
        await self._update_history(tab)
        if tab.url.startswith(("http://", "https://")):
            store.add_history(self.uid, tab.url, tab.title)
        _task(self._favicon(tab))

    async def _favicon(self, tab: Tab) -> None:
        try:
            href = await tab.page.evaluate(JS_ICON)
            if not href:
                return
            if href.startswith("data:image"):
                tab.favicon = href[:60000]
            else:
                r = await tab.page.context.request.get(href, timeout=6000, max_redirects=3)
                body = await r.body() if r.ok else b""
                ctype = (r.headers.get("content-type") or "image/x-icon").split(";")[0]
                if body and len(body) < 120_000 and ("image" in ctype or href.endswith(".ico")):
                    tab.favicon = f"data:{ctype};base64," + base64.b64encode(body).decode()
            tab.win.emit_tab(tab)
        except Exception:
            pass

    def _on_crash(self, tab: Tab) -> None:
        tab.crashed = True
        tab.loading = False
        tab.win.emit_tab(tab)

    async def _on_dialog(self, tab: Tab, dialog) -> None:
        if dialog.type == "beforeunload":
            try:
                await dialog.accept()
            except Exception:
                pass
            return
        did = secrets.token_hex(5)
        self._dialogs[did] = (dialog, tab.win)
        tab.win.emit({"t": "dialog", "id": did, "tab": tab.id, "type": dialog.type,
                      "message": dialog.message[:4000], "default": dialog.default_value or ""})

        async def timeout():                         # nobody answers -> dismiss after 2 min
            await asyncio.sleep(120)
            d = self._dialogs.pop(did, None)
            if d:
                try:
                    await d[0].dismiss()
                except Exception:
                    pass
                d[1].emit({"t": "dialog-close", "id": did})
        _task(timeout())

    async def answer_dialog(self, did: str, accept: bool, text: str = "") -> None:
        d = self._dialogs.pop(did, None)
        if not d:
            return
        dialog, win = d
        try:
            if accept:
                await (dialog.accept(text) if dialog.type == "prompt" else dialog.accept())
            else:
                await dialog.dismiss()
        except Exception:
            pass
        win.emit({"t": "dialog-close", "id": did})

    def _on_chooser(self, tab: Tab, chooser) -> None:
        cid = secrets.token_hex(5)
        self._choosers[cid] = chooser
        tab.win.emit({"t": "filechooser", "id": cid, "tab": tab.id, "multiple": chooser.is_multiple()})

    async def fill_chooser(self, cid: str, paths: list[Path]) -> bool:
        ch = self._choosers.pop(cid, None)
        if not ch:
            return False
        try:
            await ch.set_files([str(p) for p in paths])
            return True
        except Exception:
            return False

    def cancel_chooser(self, cid: str) -> None:
        self._choosers.pop(cid, None)

    def _emit_all(self, obj: dict) -> None:
        for w in self.wins.values():
            w.emit(obj)

    async def _on_download(self, tab: Tab, dl) -> None:
        did = secrets.token_hex(6)
        name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", dl.suggested_filename or "download")[:180] or "download"
        self._emit_all({"t": "download", "id": did, "name": name, "state": "running"})
        target = store.download_dir(self.uid) / f"{did}__{name}"
        try:
            await dl.save_as(str(target))
            self._emit_all({"t": "download", "id": did, "name": name, "state": "done",
                            "size": target.stat().st_size})
        except Exception as e:
            self._emit_all({"t": "download", "id": did, "name": name, "state": "failed", "error": str(e)[:200]})

    # ---------------------------------------------------------------- navigation
    def resolve_url(self, raw: str, network: str = "direct") -> str:
        u = (raw or "").strip()
        if not u:
            return NEWTAB
        if u in ("about:newtab", "about:home"):
            return NEWTAB
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", u) and not re.match(r"^[\w.-]+:\d+(/|$)", u):
            low = u.lower()
            if low.startswith(("http:", "https:", "about:blank", "data:", "view-source:http")):
                return u
            if " " not in u and not low.startswith(("file:", "chrome:", "javascript:", "about:", "view-source:")):
                return store.search_url(self.uid, u)
            return NEWTAB                            # not allowed (server files / browser internals)
        if " " in u:
            return store.search_url(self.uid, u)
        host = u.split("/")[0]
        name, _, port = host.rpartition(":") if re.match(r"^[^:]+:\d+$", host) else (host, "", "")
        is_ip = bool(re.match(r"^\d{1,3}(\.\d{1,3}){3}$", name)) or name.startswith("[")
        if port in ("443", "8006", "8443", "9443", "10443"):
            return "https://" + u                    # typical TLS ports (e.g. Proxmox 8006)
        if port or is_ip or name == "localhost":
            return "http://" + u                     # IP / port: mostly internal devices without TLS
        if "." in name and re.match(r"^[\w.-]+$", name) and not name.endswith("."):
            return "https://" + u
        if "." not in name and len(name) < 40 and re.match(r"^[\w-]+$", name) and network.startswith("agent:"):
            return "http://" + u                     # intranet name via an agent ("nas", "intranet")
        return store.search_url(self.uid, u)

    async def navigate(self, tab: Tab | None, raw: str) -> None:
        if not tab:
            return
        url = self.resolve_url(raw, tab.network)
        if tab.mode == "proxy":
            if url != NEWTAB and proxy.stream_only(url):
                tab.pending_url, tab.url = url, url        # login pages that refuse proxies -> stream
                await self.set_mode(tab, "stream")
                return
            self._proxy_nav(tab, url)
            tab.win.emit_tab(tab)
            return
        tab.pending_url = None
        if url == NEWTAB:
            tab.url, tab.title, tab.favicon, tab.loading = NEWTAB, "", "", False
            try:
                await tab.page.goto("about:blank")
            except Exception:
                pass
            tab.url = NEWTAB
            tab.win.emit_tab(tab)
            return
        tab.loading = True
        tab.url = tab.intended = url
        tab.socks_err_mark = len(tab.container.socks.errors) if tab.container.socks else 0
        tab.win.emit_tab(tab)
        try:
            await tab.page.goto(url, wait_until="commit", timeout=45000)
        except Exception as e:
            msg = str(e).splitlines()[0][:300]
            tab.loading = False
            hint = ""
            errs = tab.container.socks.errors if tab.container.socks else []
            if len(errs) > tab.socks_err_mark:
                hint = errs[-1]
            if not tab.error_shown and "interrupted by another navigation" not in msg:
                tab.error_shown = True
                await self._show_error(tab, url, msg, hint)
            tab.url = url
            tab.win.emit_tab(tab)

    async def _show_error(self, tab: Tab, url: str, msg: str, hint: str) -> None:
        """Own error page. Chromium is still building its own -> wait a moment and retry."""
        html = _error_page(url, msg, hint, tab.network)
        for _ in range(6):
            await asyncio.sleep(0.25)
            try:
                await tab.page.set_content(html, timeout=5000)
                break
            except Exception:
                continue
        tab.title = "Problem loading page"
        tab.win.emit_tab(tab)

    async def history_go(self, tab: Tab | None, delta: int) -> None:
        if not tab:
            return
        if tab.mode == "proxy":
            self._proxy_cmd(tab, cmd="back" if delta < 0 else "forward")
            return
        try:
            if delta < 0:
                await tab.page.go_back(timeout=30000, wait_until="commit")
            else:
                await tab.page.go_forward(timeout=30000, wait_until="commit")
        except Exception:
            pass
        await self._update_history(tab)

    async def reload(self, tab: Tab | None, hard: bool = False) -> None:
        if not tab:
            return
        if tab.mode == "proxy":
            self._proxy_cmd(tab, cmd="reload")
            return
        if tab.crashed or tab.error_shown:
            url = tab.intended or tab.url
            if tab.crashed:
                w, net, idx = tab.win, tab.network, tab.win.tabs.index(tab)
                await self.new_tab(w, url, network=net, index=idx)
                await self.close_tab(tab)
            else:
                await self.navigate(tab, url)
            return
        try:
            if hard and tab.cdp:
                await tab.cdp.send("Page.reload", {"ignoreCache": True})
            else:
                await tab.page.reload(timeout=45000, wait_until="commit")
        except Exception:
            pass

    async def stop(self, tab: Tab | None) -> None:
        if tab and tab.mode == "proxy":
            self._proxy_cmd(tab, cmd="stop")
            return
        if tab and tab.cdp:
            try:
                await tab.cdp.send("Page.stopLoading")
            except Exception:
                pass

    async def set_zoom(self, tab: Tab | None, zoom: float) -> None:
        if not tab:
            return
        tab.zoom = max(0.3, min(3.0, round(float(zoom), 2)))
        if tab.mode == "proxy":
            self._proxy_cmd(tab, cmd="zoom", zoom=tab.zoom)
        else:
            await self._apply_zoom(tab)
        tab.win.emit_tab(tab)

    async def _apply_zoom(self, tab: Tab) -> None:
        try:
            await tab.page.evaluate(f"document.documentElement.style.zoom = '{tab.zoom}'")
        except Exception:
            pass

    async def find(self, tab: Tab | None, text: str, backwards: bool = False) -> dict:
        if not tab or not text or tab.page is None:
            return {"found": False, "count": 0}
        try:
            found = await tab.page.evaluate("([q, b]) => window.find(q, false, b, true, false, false, false)",
                                            [text, bool(backwards)])
            count = await tab.page.evaluate(JS_FIND_COUNT, text)
            return {"found": bool(found), "count": int(count or 0)}
        except Exception:
            return {"found": False, "count": 0}

    # ---------------------------------------------------------------- input
    async def input(self, tab: Tab | None, ev: dict) -> None:
        self.last_seen = time.time()
        if not tab or tab.page is None or tab.url == NEWTAB or tab.pending_url:
            return
        p = tab.page
        w = tab.win
        kind = ev.get("type")
        try:
            if kind == "mouse" and tab.cdp:
                # mouse straight to CDP (Playwright adds round trips). Moves are
                # fire-and-forget: CDP keeps the order, nothing waits for them.
                x, y = float(ev.get("x", 0)), float(ev.get("y", 0))
                act = ev.get("action")
                btn = ev.get("button") or "left"
                bit = {"left": 1, "right": 2, "middle": 4}.get(btn, 1)
                mods = _modmask(ev)
                if act == "move":
                    self._cdp(tab, {"type": "mouseMoved", "x": x, "y": y, "modifiers": mods,
                                    "button": "left" if tab.buttons & 1 else "none", "buttons": tab.buttons})
                    self._hover(tab, x, y)
                elif act == "down":
                    if btn == "left":
                        info = await p.evaluate(JS_SELECT_INFO, [x, y])
                        if info:                     # <select>: the dashboard draws its own list
                            w.select_hold = (tab.id, x, y)
                            w.emit({"t": "select", "tab": tab.id, "x": x, "y": y, **info})
                            return
                    w.select_hold = None
                    tab.buttons |= bit
                    await tab.cdp.send("Input.dispatchMouseEvent", {
                        "type": "mousePressed", "x": x, "y": y, "button": btn, "buttons": tab.buttons,
                        "clickCount": int(ev.get("clicks") or 1), "modifiers": mods})
                elif act == "up":
                    if w.select_hold:
                        return
                    tab.buttons &= ~bit
                    await tab.cdp.send("Input.dispatchMouseEvent", {
                        "type": "mouseReleased", "x": x, "y": y, "button": btn, "buttons": tab.buttons,
                        "clickCount": int(ev.get("clicks") or 1), "modifiers": mods})
            elif kind == "wheel" and tab.cdp:
                self._cdp(tab, {"type": "mouseWheel", "x": float(ev.get("x", 0)), "y": float(ev.get("y", 0)),
                                "deltaX": float(ev.get("dx", 0)), "deltaY": float(ev.get("dy", 0)),
                                "modifiers": _modmask(ev)})
            elif kind == "key":
                await self._key(tab, ev)
            elif kind in ("paste", "text"):
                txt = str(ev.get("text") or "")[:100000]
                if txt:
                    await p.keyboard.insert_text(txt)
        except Exception:
            pass

    async def _key(self, tab: Tab, ev: dict) -> None:
        """One key press (the dashboard only sends key-down; modifiers ride along).
        No separate modifier presses -> nothing can get stuck when the focus leaves."""
        kb = tab.page.keyboard
        key = str(ev.get("key") or "")
        ctrl, alt, shift, meta = bool(ev.get("ctrl")), bool(ev.get("alt")), bool(ev.get("shift")), bool(ev.get("meta"))
        if not key or key in ("Unidentified", "Dead", "Process", "Control", "Shift", "Alt", "Meta",
                              "AltGraph", "CapsLock", "OS"):
            return
        printable = len(key) == 1
        if printable and (not (ctrl or meta or alt) or (ctrl and alt)):
            # plain characters (also AltGr = Ctrl+Alt on Windows, e.g. "@")
            if ctrl and alt:
                await kb.insert_text(key)
            else:
                await kb.type(key)
            return
        mods = [m for m, on in (("Control", ctrl), ("Alt", alt), ("Meta", meta)) if on]
        if shift and not printable:
            mods.append("Shift")
        name = "Space" if key == " " else key
        if name == "+":
            name = "Equal"
        try:
            await kb.press("+".join(mods + [name]))
        except Exception:
            return                                    # unknown key - ignore
        if (ctrl or meta) and key.lower() in ("c", "x"):
            await asyncio.sleep(0.05)
            await self.copy_selection(tab)

    def _cdp(self, tab: Tab, params: dict) -> None:
        """Mouse event without waiting for the answer."""
        async def go():
            try:
                await tab.cdp.send("Input.dispatchMouseEvent", params)
            except Exception:
                pass
        _task(go())

    def _hover(self, tab: Tab, x: float, y: float) -> None:
        """Cursor shape + link under the mouse - in the background, never blocks the input."""
        w = tab.win
        now = time.time()
        if tab.hovering or now - w.hover_at < 0.15:
            return
        w.hover_at = now
        tab.hovering = True

        async def look():
            try:
                info = await tab.page.evaluate(JS_AT, [x, y])
            except Exception:
                info = None
            finally:
                tab.hovering = False
            if not info:
                return
            cur = (info.get("cursor"), info.get("link"))
            if cur != w.hover_last:
                w.hover_last = cur
                w.emit({"t": "hover", "cursor": cur[0], "link": cur[1]})
        _task(look())

    async def element_at(self, tab: Tab | None, x: float, y: float) -> dict:
        if not tab or tab.page is None or tab.url == NEWTAB or tab.pending_url:
            return {}
        try:
            return await tab.page.evaluate(JS_AT, [x, y]) or {}
        except Exception:
            return {}

    async def choose_select(self, win: Win, values: list[int] | None) -> None:
        hold, win.select_hold = win.select_hold, None
        if not hold or values is None:
            return
        tab = self.tab(hold[0])
        if tab:
            try:
                await tab.page.evaluate(JS_SELECT_SET, [hold[1], hold[2], [int(i) for i in values]])
            except Exception:
                pass

    async def copy_selection(self, tab: Tab | None) -> None:
        if tab and tab.page is not None:
            try:
                txt = await tab.page.evaluate(JS_SELECTION)
                tab.win.emit({"t": "clipboard", "text": (txt or "")[:100000]})
            except Exception:
                pass

    async def select_all(self, tab: Tab | None) -> None:
        if tab and tab.page is not None:
            try:
                await tab.page.keyboard.press("Control+A")
            except Exception:
                pass

    async def save_url(self, tab: Tab | None, url: str) -> None:
        """Save image/file (context menu): fetch it through the tab's context, store as a download."""
        if not tab or tab.page is None or not url.startswith(("http://", "https://")):
            return
        did = secrets.token_hex(6)
        name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", url.split("?")[0].rstrip("/").split("/")[-1] or "file")[:180]
        self._emit_all({"t": "download", "id": did, "name": name, "state": "running"})
        try:
            r = await tab.page.context.request.get(url, timeout=60000)
            body = await r.body()
            (store.download_dir(self.uid) / f"{did}__{name}").write_bytes(body)
            self._emit_all({"t": "download", "id": did, "name": name, "state": "done", "size": len(body)})
        except Exception as e:
            self._emit_all({"t": "download", "id": did, "name": name, "state": "failed", "error": str(e)[:200]})

    # ---------------------------------------------------------------- ticker
    async def _tick(self) -> None:
        """While somebody watches: keep the titles of active tabs current (JS changes)."""
        try:
            while self.viewers() and not self.closed:
                await asyncio.sleep(2.0)
                for w in list(self.wins.values()):
                    if not w.viewers:
                        continue
                    tab = self.tab(w.active)
                    if not tab or tab.page is None or tab.url == NEWTAB or tab.loading or tab.error_shown:
                        continue
                    try:
                        title = await tab.page.title()
                    except Exception:
                        continue
                    if title and title != tab.title:
                        tab.title = title
                        w.emit_tab(tab)
        except asyncio.CancelledError:
            pass


class Manager:
    def __init__(self):
        self._pw = None
        self.browser = None
        self.users: dict[str, UserBrowser] = {}
        self._lock = asyncio.Lock()
        self._reaper: asyncio.Task | None = None

    async def ensure_browser(self) -> None:
        ok, why = available()
        if not ok:
            raise RuntimeError(why)
        await self._ensure_browser()

    async def _ensure_browser(self) -> None:
        if self.browser and self.browser.is_connected():
            return
        async with self._lock:
            if self.browser and self.browser.is_connected():
                return
            install.prepare_env()
            if not self._pw:
                self._pw = await async_playwright().start()
            args = ["--no-sandbox", "--disable-dev-shm-usage", "--proxy-bypass-list=<-loopback>",
                    "--disable-features=Translate,MediaRouter", "--autoplay-policy=no-user-gesture-required"]
            launch = {"headless": True, "args": args,
                      # required so that EVERY context can get its own proxy
                      "proxy": {"server": "http://per-context"}}
            exe = install.status().get("executable")
            try:
                if exe:
                    self.browser = await self._pw.chromium.launch(executable_path=exe, **launch)
                else:
                    self.browser = await self._pw.chromium.launch(**launch)
            except Exception as first:
                exe = install.system_browser()
                if not exe:
                    raise RuntimeError(f"Chromium does not start: {str(first).splitlines()[0][:300]}")
                self.browser = await self._pw.chromium.launch(executable_path=exe, **launch)
            for ub in self.users.values():            # contexts of a dead browser are gone
                ub.containers.clear()
            if self._reaper is None or self._reaper.done():
                self._reaper = asyncio.get_running_loop().create_task(self._reap())

    async def for_user(self, user: dict) -> UserBrowser:
        if not proxy.enabled():                         # stream only -> Chromium is needed right away
            await self.ensure_browser()
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.get_running_loop().create_task(self._reap())
        ub = self.users.get(user["id"])
        if ub and not ub.closed:
            ub.user = user
            return ub
        ub = UserBrowser(user)
        self.users[user["id"]] = ub
        return ub

    async def drop_user(self, uid: str) -> None:
        ub = self.users.pop(uid, None)
        if ub:
            await ub.close()

    async def _reap(self) -> None:
        while True:
            await asyncio.sleep(60)
            now = time.time()
            for uid, ub in list(self.users.items()):
                if not ub.viewers() and now - ub.last_seen > IDLE_CLOSE:
                    await self.drop_user(uid)
                else:
                    for c in list(ub.containers.values()):      # save cookies regularly
                        await c.save_state()
            try:
                store.cleanup_downloads()
                await proxy.reap_clients()
            except Exception:
                pass

    async def shutdown(self) -> None:
        for uid in list(self.users):
            await self.drop_user(uid)
        try:
            if self.browser:
                await self.browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self.browser = None
        self._pw = None
        await proxy.close_all()

    def stats(self) -> dict:
        return {"users": len(self.users), "tabs": sum(len(u.all_tabs()) for u in self.users.values()),
                "viewers": sum(u.viewers() for u in self.users.values())}


MANAGER = Manager()


def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _error_page(url: str, msg: str, hint: str, network: str) -> str:
    m = re.search(r"(?:net::)?(ERR_[A-Z_]+)", msg or "")
    code = m.group(1) if m else ""
    human = {
        "ERR_NAME_NOT_RESOLVED": "The server address could not be found.",
        "ERR_CONNECTION_REFUSED": "The server refused the connection.",
        "ERR_CONNECTION_TIMED_OUT": "The server is taking too long to respond.",
        "ERR_SOCKS_CONNECTION_FAILED": "The connection through the selected network failed.",
        "ERR_TUNNEL_CONNECTION_FAILED": "The connection through the selected network failed.",
        "ERR_ADDRESS_UNREACHABLE": "The address is unreachable.",
        "ERR_CERT_AUTHORITY_INVALID": "The certificate is not trusted.",
    }.get(code, "The page could not be loaded.")
    net = "through the agent link" if network.startswith("agent:") else "directly from the RMM server"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Problem loading page</title></head>
<body style="font-family:system-ui,sans-serif;background:#f9f9fb;color:#15141a;display:flex;justify-content:center;padding:80px 20px">
<div style="max-width:620px"><div style="font-size:42px">⚠️</div>
<h1 style="font-size:24px;font-weight:600">Hmm. We’re having trouble finding that site.</h1>
<p>{_esc(human)}</p><p style="color:#5b5b66">Address: <b>{_esc(url)}</b><br>Network: {net}</p>
{f'<p style="color:#5b5b66">Details: {_esc(hint)}</p>' if hint else ''}
<p style="color:#8f8f9d;font-size:13px">{_esc(code or msg)}</p></div></body></html>"""
