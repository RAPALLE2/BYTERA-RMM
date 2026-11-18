"""
webproxy.py (Modul "bmc") - native Web-UI und Remote-Konsole der BMC erreichbar machen.

Zwei Wege, beide liefern die ECHTE iLO-/iDRAC-Oberflaeche samt HTML5-Konsole
(nichts nachgebaut - alles, was die BMC kann, ist da):

  direct  Der Browser des Admins erreicht die BMC selbst -> neuer Tab auf
          https://<host>. Einfachster, zuverlaessigster Weg, wenn das
          Management-Netz vom Arbeitsplatz aus erreichbar ist.

  proxy   Modul-Einstellung "proxy_host" (z.B. bmc-*.example.com) gesetzt: Das
          Backend stellt die BMC unter einem eigenen Hostnamen bereit und leitet
          HTTP + WebSocket durch (wie der Browser-Proxy des Browser-Moduls).
          Nur das Backend muss die BMC erreichen (z.B. ueber das WireGuard-VPN).
          Hinweis: gegen echte Hardware noch nicht getestet - selbst signierte
          Zertifikate werden akzeptiert.

Reine Redfish-Aktionen (Power, Health, Inventar, Logs, Boot, Virtual Media ...)
laufen NICHT hier, sondern ueber redfish.py (Backend oder Node).
"""

from __future__ import annotations

import re

from app import modules

from . import store

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "content-length", "content-encoding", "host"}


def pattern() -> str:
    p = str(modules.setting("bmc", "proxy_host", "") or "").strip().lower()
    p = re.sub(r"^https?://", "", p).rstrip("/")
    return p if p.count("*") == 1 and "." in p and re.match(r"^[a-z0-9.*-]+(:\d+)?$", p) else ""


def enabled() -> bool:
    return bool(pattern())


def _rx():
    p = pattern()
    if not p:
        return None
    p = re.sub(r":\d+$", "", p)
    a, b = p.split("*")
    return re.compile("^" + re.escape(a) + r"([a-z0-9]{6,20})" + re.escape(b) + "$")


def match_host(host: str) -> bool:
    return bool(_rx() and _rx().match(host.split(":")[0]))


def host_for(label: str) -> str:
    return pattern().replace("*", label)


def _label(host: str) -> str:
    m = _rx() and _rx().match(host.split(":")[0])
    return m.group(1) if m else ""


def _origin(c: dict) -> str:
    scheme = "http" if c.get("insecure_http") else "https"
    port = c.get("port")
    return f"{scheme}://{c.get('host')}" + (f":{int(port)}" if port else "")


def console_access(c: dict) -> dict:
    direct = _origin(c)
    out = {"vendor": c.get("vendor") or "", "direct_url": direct + "/", "proxy_url": None,
           "mode": "direct",
           "hint": "Öffnet die native iLO/iDRAC-Oberfläche inkl. HTML5-Remote-Konsole. "
                   "„Direkt“ setzt voraus, dass dein Browser die BMC erreicht."}
    if enabled():
        out["proxy_url"] = f"https://{host_for(c['id'])}/"
        out["mode"] = "proxy"
        out["hint"] += " „Über Backend“ leitet alles durch das RMM (nur das Backend muss die BMC erreichen)."
    return out


# ------------------------------------------------------------------ ASGI reverse proxy
async def app(scope, receive, send):
    if scope["type"] not in ("http", "websocket"):
        return
    host = ""
    for k, v in scope.get("headers") or []:
        if k == b"host":
            host = v.decode("latin1")
            break
    label = _label(host)
    c = store.get(label) if label else None
    if not c:
        return await _deny(scope, send, 404, "Unbekannte BMC")
    origin = _origin(c)
    if scope["type"] == "websocket":
        return await _ws(scope, receive, send, c, origin)
    return await _http(scope, receive, send, c, origin)


async def _deny(scope, send, status, msg):
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 4404})
        return
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
    await send({"type": "http.response.body", "body": msg.encode()})


def _fwd_headers(scope, host_header):
    out = {}
    for k, v in scope.get("headers") or []:
        name = k.decode("latin1").lower()
        if name in HOP or name == "host":
            continue
        out[k.decode("latin1")] = v.decode("latin1")
    out["Host"] = host_header
    out["Accept-Encoding"] = "identity"
    return out


def _rewrite_location(loc: str, origin: str, proxy_host: str) -> str:
    if loc.startswith(origin):
        return loc.replace(origin, f"https://{proxy_host}", 1)
    return loc


async def _http(scope, receive, send, c, origin):
    import httpx
    body = b""
    while True:
        msg = await receive()
        body += msg.get("body", b"") or b""
        if not msg.get("more_body"):
            break
    from urllib.parse import urlparse
    bmc_host = urlparse(origin).netloc
    path = scope["path"]
    qs = scope.get("query_string") or b""
    url = origin + path + (("?" + qs.decode("latin1")) if qs else "")
    headers = _fwd_headers(scope, bmc_host)
    proxy_host = host_for(c["id"])
    try:
        async with httpx.AsyncClient(verify=False, follow_redirects=False,
                                     timeout=httpx.Timeout(connect=8.0, read=60.0, write=60.0, pool=8.0)) as cl:
            r = await cl.request(scope["method"], url, headers=headers, content=body or None)
    except httpx.HTTPError as exc:
        return await _deny(scope, send, 502, f"BMC nicht erreichbar: {exc}")
    out_headers = []
    for k, v in r.headers.multi_items():
        lk = k.lower()
        if lk in HOP:
            continue
        if lk == "location":
            v = _rewrite_location(v, origin, proxy_host)
        if lk == "set-cookie":
            v = re.sub(r";\s*[Dd]omain=[^;]+", "", v)
        out_headers.append((k.encode("latin1"), v.encode("latin1")))
    data = r.content
    await send({"type": "http.response.start", "status": r.status_code, "headers": out_headers})
    await send({"type": "http.response.body", "body": data})


async def _ws(scope, receive, send, c, origin):
    import asyncio
    try:
        import websockets
    except ImportError:
        await send({"type": "websocket.close", "code": 1011})
        return
    import ssl
    from urllib.parse import urlparse
    # erste Nachricht muss websocket.connect sein
    msg = await receive()
    if msg["type"] != "websocket.connect":
        return
    bmc_host = urlparse(origin).netloc
    ws_scheme = "ws" if c.get("insecure_http") else "wss"
    url = f"{ws_scheme}://{bmc_host}" + scope["path"] + (("?" + scope["query_string"].decode("latin1")) if scope.get("query_string") else "")
    subprotos = scope.get("subprotocols") or []
    extra = {}
    for k, v in scope.get("headers") or []:
        if k.decode("latin1").lower() == "cookie":
            extra["Cookie"] = v.decode("latin1")
    ctx = ssl._create_unverified_context() if ws_scheme == "wss" else None
    try:
        up = await websockets.connect(url, ssl=ctx, additional_headers=extra,
                                      subprotocols=subprotos or None, max_size=None, open_timeout=10)
    except Exception:
        await send({"type": "websocket.close", "code": 1011})
        return
    await send({"type": "websocket.accept", "subprotocol": up.subprotocol})

    async def to_bmc():
        try:
            while True:
                m = await receive()
                if m["type"] == "websocket.disconnect":
                    break
                if m.get("bytes") is not None:
                    await up.send(m["bytes"])
                elif m.get("text") is not None:
                    await up.send(m["text"])
        except Exception:
            pass
        finally:
            await up.close()

    async def to_client():
        try:
            async for m in up:
                if isinstance(m, bytes):
                    await send({"type": "websocket.send", "bytes": m})
                else:
                    await send({"type": "websocket.send", "text": m})
        except Exception:
            pass
        finally:
            await send({"type": "websocket.close", "code": up.close_code or 1000})

    await asyncio.gather(to_bmc(), to_client())
