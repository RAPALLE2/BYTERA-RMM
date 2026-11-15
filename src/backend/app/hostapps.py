"""
hostapps.py (core)
==================
Lets a module serve its OWN hostnames with its own ASGI app - e.g. the web
browser module serves proxied websites under "bx-<id>.example.com", completely
separated from the dashboard (other origin = proxied pages can never read the
dashboard's login token).

    from app import hostapps
    hostapps.register("browser-proxy", lambda host: host.endswith(".example.com"), asgi_app)
    hostapps.unregister("browser-proxy")

The dispatcher sits outermost in the middleware stack (main.py). Requests for
other hosts go to the dashboard unchanged.
"""
from __future__ import annotations

from typing import Awaitable, Callable

_apps: dict[str, tuple[Callable[[str], bool], Callable]] = {}


def register(name: str, match: Callable[[str], bool], app: Callable[..., Awaitable[None]]) -> None:
    _apps[name] = (match, app)


def unregister(name: str) -> None:
    _apps.pop(name, None)


def _host(scope) -> str:
    for k, v in scope.get("headers") or []:
        if k == b"host":
            h = v.decode("latin-1").strip().lower()
            if h.startswith("["):
                return h.split("]")[0] + "]"
            return h.rsplit(":", 1)[0] if h.count(":") == 1 else h
    return ""


class HostDispatch:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if _apps and scope.get("type") in ("http", "websocket"):
            host = _host(scope)
            if host:
                for match, app in list(_apps.values()):
                    try:
                        hit = match(host)
                    except Exception:
                        hit = False
                    if hit:
                        await app(scope, receive, send)
                        return
        await self.app(scope, receive, send)
