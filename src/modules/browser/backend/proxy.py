# proxy.py (module "browser") - pass-through mode: the DASHBOARD renders the pages
# ------------------------------------------------------------------------------
# Instead of rendering in the backend and streaming pictures, every target origin
# gets its own hostname under the proxy domain (module setting "proxy_host",
# e.g. "bx-*.example.com"):
#
#     https://192.168.1.10:8006  (network "agent:abc")  ->  https://bx-k3j9...example.com/
#
# The dashboard shows that hostname in an iframe. The backend only forwards
# bytes: requests (GET/POST/uploads), responses, cookies, WebSockets - directly
# or through the agent (same SOCKS exits as the stream mode, same permission
# checks). Because the PATH stays the same, relative links, single-page apps
# and JavaScript work unchanged; only absolute links to other hosts are mapped
# (small script /__bx/inject.js + service worker /__bx/sw.js).
#
# Security: every proxied site lives on its OWN origin, never on the dashboard's
# origin - a proxied page can't read the dashboard login. Access needs a
# session cookie (HttpOnly, per hostname) that is created from a short-lived
# signed ticket issued to the logged-in dashboard user.
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import http.cookiejar
import json
import re
import ssl
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlsplit

from app import db, modules
from app.config import JWT_SECRET
from app.paths import MODULES_DATA_DIR

from . import netsel
from .socks import SocksServer

MAP_FILE = MODULES_DATA_DIR / "browser" / "proxymap.json"
COOKIE = "__bx_s"
SESSION_TTL = 12 * 3600
TICKET_TTL = 300
CLIENT_IDLE = 15 * 60
_KEY = hashlib.sha256(b"rmm-browser-proxy:" + str(JWT_SECRET).encode()).digest()
_lock = threading.RLock()
_map: dict | None = None

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
       "transfer-encoding", "upgrade", "host", "content-length"}
DROP_RESP = {"content-security-policy", "content-security-policy-report-only", "x-frame-options",
             "strict-transport-security", "cross-origin-opener-policy", "cross-origin-embedder-policy",
             "cross-origin-resource-policy", "public-key-pins", "alt-svc", "permissions-policy", "report-to", "nel"}
# Sites whose logins refuse to run inside a proxy - always use the stream there
STREAM_ONLY = ("accounts.google.com", "login.microsoftonline.com", "login.live.com", "appleid.apple.com",
               "github.com/login", "challenges.cloudflare.com")


# ------------------------------------------------------------------ configuration
def pattern() -> str:
    p = str(modules.setting("browser", "proxy_host", "") or "").strip().lower()
    p = re.sub(r"^https?://", "", p).rstrip("/")
    return p if p.count("*") == 1 and "." in p and re.match(r"^[a-z0-9.*-]+(:\d+)?$", p) else ""


def enabled() -> bool:
    return bool(pattern())


def _rx():
    p = pattern()
    if not p:
        return None
    p = re.sub(r":\d+$", "", p)                      # the Host header is compared without the port
    a, b = p.split("*")
    return re.compile("^" + re.escape(a) + r"([a-z0-9]{8,30})" + re.escape(b) + "$")


def match_host(host: str) -> bool:
    rx = _rx()
    return bool(rx and rx.match(host))


def host_regex_js() -> str:
    """Same check for the dashboard (which messages come from proxy pages)."""
    p = re.sub(r":\d+$", "", pattern())
    if not p:
        return ""
    a, b = p.split("*")
    return "^https?://" + re.escape(a) + "[a-z0-9]{8,30}" + re.escape(b) + "(:\\d+)?$"


def host_for(label: str) -> str:
    return pattern().replace("*", label)


# ------------------------------------------------------------------ label <-> target
def _load() -> dict:
    global _map
    with _lock:
        if _map is None:
            try:
                _map = json.loads(MAP_FILE.read_text(encoding="utf-8"))
            except Exception:
                _map = {}
        return _map


def _save() -> None:
    with _lock:
        MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = MAP_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_map or {}), encoding="utf-8")
        tmp.replace(MAP_FILE)


def split_url(url: str) -> tuple[str, str, int, str]:
    """-> (scheme, host, port, path+query+fragment)"""
    u = urlsplit(url)
    scheme = (u.scheme or "https").lower()
    if scheme not in ("http", "https"):
        raise ValueError("only http and https can be proxied")
    host = (u.hostname or "").lower()
    if not host:
        raise ValueError("address without host")
    port = u.port or (443 if scheme == "https" else 80)
    rest = (u.path or "/") + (("?" + u.query) if u.query else "") + (("#" + u.fragment) if u.fragment else "")
    return scheme, host, port, rest


def label_for(scheme: str, host: str, port: int, net: str) -> str:
    """Stable hostname label per (origin, network) - cookies of a site survive."""
    key = f"{scheme}|{host}|{port}|{net}".encode()
    lab = "x" + base64.b32encode(hmac.new(_KEY, key, hashlib.sha256).digest()).decode().lower().rstrip("=")[:17]
    m = _load()
    with _lock:
        if lab not in m:
            m[lab] = {"scheme": scheme, "host": host, "port": port, "net": net, "ts": int(time.time())}
            _save()
    return lab


def target(label: str) -> dict | None:
    return _load().get(label)


def origin_of(t: dict) -> str:
    default = 443 if t["scheme"] == "https" else 80
    h = f"[{t['host']}]" if ":" in t["host"] else t["host"]
    return f"{t['scheme']}://{h}" + ("" if t["port"] == default else f":{t['port']}")


def stream_only(url: str) -> bool:
    low = url.lower()
    return any(s in low for s in STREAM_ONLY)


# ------------------------------------------------------------------ tickets / session cookie
def _sign(obj: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode().rstrip("=")
    sig = base64.urlsafe_b64encode(hmac.new(_KEY, raw.encode(), hashlib.sha256).digest()[:18]).decode()
    return raw + "." + sig


def _verify(tok: str, kind: str) -> dict | None:
    try:
        raw, sig = tok.split(".", 1)
        good = base64.urlsafe_b64encode(hmac.new(_KEY, raw.encode(), hashlib.sha256).digest()[:18]).decode()
        if not hmac.compare_digest(sig, good):
            return None
        obj = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        if obj.get("k") != kind or obj.get("exp", 0) < time.time():
            return None
        return obj
    except Exception:
        return None


def ticket(uid: str, parent: str) -> str:
    return _sign({"k": "t", "u": uid, "p": parent, "exp": int(time.time()) + TICKET_TTL})


def proxy_scheme_for(parent: str) -> str:
    return "https" if parent.startswith("https:") else "http"


def src_for(user: dict, url: str, net: str, parent: str) -> str:
    """URL for the dashboard iframe: login hop on the mapped host, then the page."""
    scheme, host, port, rest = split_url(url)
    lab = label_for(scheme, host, port, net)
    return (f"{proxy_scheme_for(parent)}://{host_for(lab)}/__bx/login?t={quote(ticket(user['id'], parent))}"
            f"&next={quote(rest, safe='')}")


# ------------------------------------------------------------------ HTTP clients per (user, network)
class _NoJar(http.cookiejar.CookieJar):
    """Cookies are passed through to the user's browser - never kept here."""

    def set_cookie(self, cookie):
        pass

    def extract_cookies(self, response, request):
        pass


_clients: dict[tuple, dict] = {}
_clients_lock = asyncio.Lock()


async def _exit_for(user: dict, net: str) -> dict:
    """SOCKS exit + httpx client of this user/network (permission checks per connection)."""
    import httpx
    key = (user["id"], net)
    async with _clients_lock:
        c = _clients.get(key)
        if c:
            c["used"] = time.time()
            return c
        socks = SocksServer(netsel.make_router(user, net), f"proxy:{net}")
        port = await socks.start()
        client = httpx.AsyncClient(proxy=f"socks5://127.0.0.1:{port}", verify=False, follow_redirects=False,
                                   cookies=httpx.Cookies(_NoJar()), trust_env=False,
                                   timeout=httpx.Timeout(120, connect=25),
                                   limits=httpx.Limits(max_connections=64, max_keepalive_connections=16))
        c = {"socks": socks, "port": port, "client": client, "used": time.time()}
        _clients[key] = c
        return c


async def reap_clients() -> None:
    now = time.time()
    for key, c in list(_clients.items()):
        if now - c["used"] > CLIENT_IDLE:
            _clients.pop(key, None)
            try:
                await c["client"].aclose()
            except Exception:
                pass
            await c["socks"].stop()


async def close_all() -> None:
    for key in list(_clients):
        c = _clients.pop(key)
        try:
            await c["client"].aclose()
        except Exception:
            pass
        await c["socks"].stop()


# ------------------------------------------------------------------ ASGI helpers
def _headers(scope) -> list[tuple[str, str]]:
    return [(k.decode("latin-1").lower(), v.decode("latin-1")) for k, v in scope.get("headers") or []]


def _hget(hs, name, default=""):
    return next((v for k, v in hs if k == name), default)


def _cookies(hs) -> dict:
    out = {}
    for k, v in hs:
        if k == "cookie":
            for part in v.split(";"):
                if "=" in part:
                    a, b = part.split("=", 1)
                    out[a.strip()] = b.strip()
    return out


async def _respond(send, status: int, body: bytes | str = b"", headers: list | None = None,
                   ctype: str = "text/html; charset=utf-8") -> None:
    if isinstance(body, str):
        body = body.encode()
    hs = [(b"content-type", ctype.encode()), (b"content-length", str(len(body)).encode()),
          (b"cache-control", b"no-store")]
    for k, v in headers or []:
        hs.append((k.encode("latin-1"), v.encode("latin-1")))
    await send({"type": "http.response.start", "status": status, "headers": hs})
    await send({"type": "http.response.body", "body": body})


def _page(title: str, text: str, kind: str = "auth") -> str:
    """Small page; tells the dashboard what happened (auth = get a fresh link, fail = use the stream)."""
    return (f"<!doctype html><meta charset=utf-8><title>{title}</title><body style='font-family:system-ui;"
            f"padding:60px;color:#333'><h2>{title}</h2><p>{text}</p>"
            f"<script>try{{parent.postMessage({{bx:1,type:'{kind}'}},'*')}}catch(e){{}}</script>")


async def _read_body(receive) -> bytes:
    chunks = []
    while True:
        m = await receive()
        if m["type"] == "http.request":
            chunks.append(m.get("body", b""))
            if not m.get("more_body"):
                break
        elif m["type"] == "http.disconnect":
            break
    return b"".join(chunks)


# ------------------------------------------------------------------ the host app
async def app(scope, receive, send):
    hs = _headers(scope)
    host = _hget(hs, "host").split(":")[0].lower()
    rx = _rx()
    m = rx.match(host) if rx else None
    t = target(m.group(1)) if m else None
    if scope["type"] == "websocket":
        await _ws(scope, receive, send, hs, t, m.group(1) if m else "")
        return
    if not t:
        await _respond(send, 404, _page("Unknown address", "This proxy address is not known (anymore). "
                                                          "Open the page again from the RMM browser."))
        return
    label = m.group(1)
    path = scope.get("path") or "/"
    qs = parse_qs((scope.get("query_string") or b"").decode("latin-1"))
    secure = (_hget(hs, "x-forwarded-proto") or scope.get("scheme") or "http").split(",")[0].strip() == "https"

    if path == "/__bx/login":
        tk = _verify((qs.get("t") or [""])[0], "t")
        nxt = unquote((qs.get("next") or ["/"])[0])
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        if not tk:
            await _respond(send, 403, _page("Link expired", "Open the page again from the RMM browser."))
            return
        sess = _sign({"k": "s", "u": tk["u"], "p": tk.get("p", ""), "exp": int(time.time()) + SESSION_TTL})
        ck = f"{COOKIE}={sess}; Path=/; HttpOnly; Max-Age={SESSION_TTL}"
        ck += "; Secure; SameSite=None" if secure else "; SameSite=Lax"
        await _respond(send, 302, b"", [("location", nxt), ("set-cookie", ck)])
        return

    sess = _verify(_cookies(hs).get(COOKIE, ""), "s")
    user = db.get_user_by_id(sess["u"]) if sess else None
    if not user or not netsel.valid(user, t["net"]):
        await _respond(send, 401, _page("Session expired", "Reopen the tab in the RMM browser."))
        return
    parent = sess.get("p", "")

    if path == "/__bx/sw.js":
        await _respond(send, 200, SW_JS, [("service-worker-allowed", "/")], "text/javascript; charset=utf-8")
        return
    if path == "/__bx/inject.js":
        js = (INJECT_JS.replace("__TARGET__", json.dumps(origin_of(t)))
              .replace("__PARENT__", json.dumps(parent)))
        await _respond(send, 200, js, [], "text/javascript; charset=utf-8")
        return
    if path == "/__bx/go":
        url = unquote((qs.get("u") or [""])[0])
        try:
            sc, h, p, rest = split_url(url)
        except ValueError:
            await _respond(send, 400, _page("Unsupported address", "Only http and https can be opened."))
            return
        lab = label_for(sc, h, p, t["net"])
        if lab == label:
            loc = rest
        else:
            loc = (f"{'https' if secure else 'http'}://{host_for(lab)}/__bx/login?"
                   f"t={quote(ticket(user['id'], parent))}&next={quote(rest, safe='')}")
        await _respond(send, 302, b"", [("location", loc)])
        return
    if path == "/__bx/fetch":                       # cross-origin subresource via the service worker
        url = unquote((qs.get("u") or [""])[0])
        try:
            split_url(url)
        except ValueError:
            await _respond(send, 400, b"bad url", [], "text/plain")
            return
        await _forward(scope, receive, send, hs, user, t, url, label, secure, foreign=True)
        return

    q = scope.get("query_string") or b""
    url = origin_of(t) + (scope.get("raw_path") or path.encode()).decode("latin-1") + (("?" + q.decode("latin-1")) if q else "")
    await _forward(scope, receive, send, hs, user, t, url, label, secure, foreign=False, parent=parent)


async def _forward(scope, receive, send, hs, user, t, url, label, secure, foreign, parent=""):
    import httpx
    me = f"{'https' if secure else 'http'}://{_hget(hs, 'host')}"
    torigin = origin_of(t)
    out = []
    for k, v in hs:
        if k in HOP or k.startswith("x-forwarded") or k in ("x-real-ip", "forwarded", "cf-connecting-ip"):
            continue
        if k == "cookie":
            if foreign:
                continue
            v = "; ".join(p.strip() for p in v.split(";") if p.strip() and not p.strip().startswith(COOKIE + "="))
            if not v:
                continue
        if k in ("origin", "referer"):
            v = v.replace(me, torigin, 1)
        if k.startswith("sec-fetch") and foreign:
            continue
        out.append((k, v))
    body = await _read_body(receive) if scope.get("method", "GET") not in ("GET", "HEAD") else b""
    try:
        c = await _exit_for(user, t["net"])
        req = c["client"].build_request(scope.get("method", "GET"), url, headers=out, content=body or None)
        resp = await c["client"].send(req, stream=True)
    except Exception as e:
        msg = str(e) or e.__class__.__name__
        if isinstance(e, httpx.ProxyError) or "SOCKS" in msg.upper():
            msg = "The connection through the selected network failed – " + msg
        await _respond(send, 502, _page("Problem loading page", f"{_esc(url)}<br><br>{_esc(msg)}", "error"))
        return
    try:
        ctype = resp.headers.get("content-type", "")
        is_html = (not foreign and "text/html" in ctype.lower() and scope.get("method") != "HEAD"
                   and resp.status_code not in (204, 304))
        rh: list[tuple[bytes, bytes]] = []
        for k, v in resp.headers.multi_items():
            lk = k.lower()
            if lk in HOP or lk in DROP_RESP:
                continue
            if lk == "content-encoding" and is_html:
                continue
            if lk == "set-cookie":
                if foreign:
                    continue
                v = _rewrite_cookie(v, secure)
            elif lk == "location":
                v = _rewrite_location(v, url, torigin, me)
            elif lk == "access-control-allow-origin" and v.strip() == torigin:
                v = me
            rh.append((lk.encode("latin-1"), v.encode("latin-1", "replace")))
        if parent and not foreign:
            rh.append((b"content-security-policy", f"frame-ancestors {parent}".encode()))
        if is_html:
            raw = await resp.aread()
            data = _inject(raw, resp.charset_encoding)
            rh.append((b"content-length", str(len(data)).encode()))
            await send({"type": "http.response.start", "status": resp.status_code, "headers": rh})
            await send({"type": "http.response.body", "body": data})
            return
        cl = resp.headers.get("content-length")
        if cl:                                          # raw bytes are passed on unchanged
            rh.append((b"content-length", cl.encode()))
        await send({"type": "http.response.start", "status": resp.status_code, "headers": rh})
        async for chunk in resp.aiter_raw():
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
        await send({"type": "http.response.body", "body": b""})
    finally:
        await resp.aclose()


def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _rewrite_cookie(v: str, secure: bool) -> str:
    parts = [p.strip() for p in v.split(";")]
    keep = [parts[0]]
    same = ""
    for p in parts[1:]:
        lp = p.lower()
        if lp.startswith("domain="):
            continue                                    # host-only on the proxy hostname
        if lp == "secure":
            continue
        if lp.startswith("samesite="):
            same = p
            continue
        if lp == "partitioned":
            continue
        keep.append(p)
    if secure:
        keep += ["Secure", "SameSite=None"]
    elif same and same.lower() != "samesite=none":
        keep.append(same)
    return "; ".join(keep)


def _rewrite_location(v: str, req_url: str, torigin: str, me: str) -> str:
    try:
        from urllib.parse import urljoin
        absu = urljoin(req_url, v)
        if absu.startswith(torigin + "/") or absu == torigin:
            return absu[len(torigin):] or "/"
        if absu.startswith(("http://", "https://")):
            return "/__bx/go?u=" + quote(absu, safe="")
    except Exception:
        pass
    return v


_HEAD_RX = re.compile(rb"<head(\s[^>]*)?>", re.I)
_HTML_RX = re.compile(rb"<html(\s[^>]*)?>", re.I)
_TAG = b'<script src="/__bx/inject.js"></script>'


def _inject(raw: bytes, _charset) -> bytes:
    m = _HEAD_RX.search(raw[:20000]) or _HTML_RX.search(raw[:20000])
    if m:
        return raw[:m.end()] + _TAG + raw[m.end():]
    return _TAG + raw


# ------------------------------------------------------------------ WebSockets
async def _ws(scope, receive, send, hs, t, label):
    import websockets
    msg = await receive()
    if msg["type"] != "websocket.connect":
        return
    sess = _verify(_cookies(hs).get(COOKIE, ""), "s")
    user = db.get_user_by_id(sess["u"]) if sess else None
    if not t or not user or not netsel.valid(user, t["net"]):
        await send({"type": "websocket.close", "code": 4403})
        return
    ws_scheme = "wss" if t["scheme"] == "https" else "ws"
    q = scope.get("query_string") or b""
    url = (f"{ws_scheme}://{origin_of(t).split('://', 1)[1]}{scope.get('raw_path', b'/').decode('latin-1')}"
           + (("?" + q.decode("latin-1")) if q else ""))
    extra = []
    for k, v in hs:
        if k == "cookie":
            v = "; ".join(p.strip() for p in v.split(";") if p.strip() and not p.strip().startswith(COOKIE + "="))
            if v:
                extra.append(("Cookie", v))
        elif k in ("authorization", "user-agent", "accept-language"):
            extra.append((k, v))
    c = await _exit_for(user, t["net"])
    ctx = None
    if ws_scheme == "wss":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        up = await websockets.connect(url, proxy=f"socks5h://127.0.0.1:{c['port']}", ssl=ctx,
                                      additional_headers=extra, origin=origin_of(t),
                                      subprotocols=scope.get("subprotocols") or None,
                                      open_timeout=25, max_size=None, ping_interval=None)
    except Exception:
        await send({"type": "websocket.close", "code": 1011})
        return
    await send({"type": "websocket.accept", "subprotocol": up.subprotocol})

    async def down():
        try:
            async for m in up:
                if isinstance(m, bytes):
                    await send({"type": "websocket.send", "bytes": m})
                else:
                    await send({"type": "websocket.send", "text": m})
        except Exception:
            pass
        try:
            await send({"type": "websocket.close", "code": up.close_code or 1000})
        except Exception:
            pass

    task = asyncio.get_running_loop().create_task(down())
    try:
        while True:
            m = await receive()
            if m["type"] == "websocket.receive":
                if m.get("bytes") is not None:
                    await up.send(m["bytes"])
                elif m.get("text") is not None:
                    await up.send(m["text"])
            elif m["type"] == "websocket.disconnect":
                break
    except Exception:
        pass
    finally:
        task.cancel()
        await up.close()


# ------------------------------------------------------------------ scripts served to proxied pages
SW_JS = r"""
// RMM browser proxy: requests to OTHER hosts (CDNs, APIs) also go through the RMM
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', (e) => {
  const u = new URL(e.request.url);
  if (u.origin === self.location.origin || !/^https?:$/.test(u.protocol) || e.request.mode === 'navigate') return;
  e.respondWith((async () => {
    const r = e.request;
    const init = { method: r.method, headers: r.headers, credentials: 'same-origin', redirect: 'follow' };
    if (!['GET', 'HEAD'].includes(r.method)) init.body = await r.arrayBuffer();
    return fetch('/__bx/fetch?u=' + encodeURIComponent(u.href), init);
  })());
});
"""

INJECT_JS = r"""
(() => {
  if (window.__bx) return;
  const T = __TARGET__, P = __PARENT__;
  const MAIN = window.parent !== window && window.parent === window.top;
  window.__bx = { target: T };
  const post = (m) => { if (MAIN && P) try { window.parent.postMessage(Object.assign({ bx: 1 }, m), P); } catch (e) {} };
  const real = (href) => {
    try {
      const x = new URL(href, location.href);
      if (x.origin === location.origin) {
        if (x.pathname === '/__bx/go') return x.searchParams.get('u') || href;
        return T + x.pathname + x.search + x.hash;
      }
      return x.href;
    } catch (e) { return href; }
  };
  const map = (href) => {
    try {
      const x = new URL(href, location.href);
      if (x.origin === location.origin || !/^https?:$/.test(x.protocol)) return x.href;
      if (x.origin === T) return location.origin + x.pathname + x.search + x.hash;
      return location.origin + '/__bx/go?u=' + encodeURIComponent(x.href);
    } catch (e) { return href; }
  };
  if ('serviceWorker' in navigator && window.isSecureContext)
    navigator.serviceWorker.register('/__bx/sw.js', { scope: '/' }).catch(() => {});
  const openTab = (href) => post({ type: 'open', url: real(href) });
  const onClick = (e) => {
    const a = e.target && e.target.closest && e.target.closest('a[href]');
    if (!a || e.defaultPrevented) return;
    const raw = a.getAttribute('href') || '';
    if (!raw || raw[0] === '#' || /^(javascript|mailto|tel|data|blob):/i.test(raw) || a.hasAttribute('download')) return;
    const newTab = (a.target && a.target !== '_self' && a.target !== '_top' && a.target !== '_parent') || e.ctrlKey || e.metaKey || e.button === 1 || e.shiftKey;
    if (newTab && MAIN) { e.preventDefault(); openTab(a.href); return; }
    const x = new URL(a.href, location.href);
    if (x.origin !== location.origin) { e.preventDefault(); location.href = map(a.href); }
  };
  document.addEventListener('click', onClick, true);
  document.addEventListener('auxclick', (e) => { if (e.button === 1) onClick(e); }, true);
  document.addEventListener('submit', (e) => {
    const f = e.target;
    try {
      const x = new URL(f.action || location.href, location.href);
      if (x.origin !== location.origin) f.action = map(x.href);
      if (f.target === '_blank') f.target = '_self';
    } catch (err) {}
  }, true);
  const wopen = window.open;
  window.open = function (u, n, feat) {
    if (u && MAIN) { openTab(new URL(u, location.href).href); return null; }
    return wopen.call(window, u ? map(u) : u, n, feat);
  };
  const icon = () => { const l = document.querySelector('link[rel~="icon"], link[rel="shortcut icon"]'); return l ? l.href : location.origin + '/favicon.ico'; };
  const nav = window.navigation;
  let last = '';
  const report = (loading) => {
    const st = { type: 'state', url: real(location.href), title: document.title || '', icon: icon(),
                 loading: loading === undefined ? document.readyState !== 'complete' : loading,
                 canBack: nav ? nav.canGoBack : history.length > 1, canForward: nav ? nav.canGoForward : false };
    const k = JSON.stringify(st);
    if (k !== last) { last = k; post(st); }
  };
  for (const fn of ['pushState', 'replaceState']) {
    const o = history[fn];
    history[fn] = function () { const r = o.apply(this, arguments); setTimeout(report, 0); return r; };
  }
  addEventListener('popstate', () => report());
  addEventListener('hashchange', () => report());
  addEventListener('beforeunload', () => post({ type: 'state', loading: true }));
  document.addEventListener('DOMContentLoaded', () => {
    report();
    const t = document.querySelector('title');
    if (t) new MutationObserver(() => report()).observe(t, { childList: true, characterData: true, subtree: true });
  });
  addEventListener('load', () => report(false));
  setInterval(() => report(), 1500);
  addEventListener('message', (e) => {
    if (e.source !== window.parent || (P && e.origin !== P) || !e.data || !e.data.bxcmd) return;
    const c = e.data;
    switch (c.bxcmd) {
      case 'back': history.back(); break;
      case 'forward': history.forward(); break;
      case 'reload': location.reload(); break;
      case 'stop': window.stop(); report(false); break;
      case 'zoom': document.documentElement.style.zoom = c.zoom; break;
      case 'find': post({ type: 'find', found: c.text ? window.find(c.text, false, !!c.back, true) : false }); break;
      case 'ping': report(); break;
    }
  });
  addEventListener('keydown', (e) => {
    if (!MAIN) return;
    const k = (e.key || '').toLowerCase(), mod = e.ctrlKey || e.metaKey;
    if ((mod && !e.altKey && 'lfdhjr'.includes(k) && k.length === 1) || (mod && e.shiftKey && k === 'b') ||
        e.key === 'F5' || e.key === 'F6' || (e.altKey && (e.key === 'ArrowLeft' || e.key === 'ArrowRight'))) {
      e.preventDefault();
      post({ type: 'key', key: e.key, ctrl: e.ctrlKey, meta: e.metaKey, alt: e.altKey, shift: e.shiftKey });
    }
  }, true);
  post({ type: 'ready' });
  report(true);
})();
"""
