"""
hostlock.py
-----------
Restrict access to the configured addresses.

The Host header of every request is checked against the list of allowed
addresses. If the settings contain only a domain, a call via the local IP no
longer gets through.

WHAT THIS ACHIEVES - AND WHAT NOT
    It prevents accidental access via the "wrong" path, DNS rebinding and the
    discovery of the UI by scanners that simply sweep IP ranges.

    It is NOT real access control. The Host header is sent by the client and
    can be set freely - whoever reaches the port still gets through with a
    forged header. The service can really be sealed off only via a firewall or
    by having it listen only on the desired address. This lock is a hurdle, not
    a bolt.

LOCKOUT PROTECTION
    Loopback (localhost, 127.0.0.1, ::1) is ALWAYS allowed. Otherwise a typo
    could lock one out of one's own UI permanently - including the setting with
    which one would undo it. So from the server itself one always gets back in.
"""

import asyncio
import ipaddress
import time
from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse

from app import db

# These names always get through - see lockout protection above.
ALWAYS_ALLOWED = {"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"}

# Paths that stay reachable even with the lock active. Without them an
# upstream load balancer could no longer recognize the service as "healthy"
# and would take it out of rotation.
EXEMPT_PATHS = ("/api/health", "/health")


def _clean_host(value: str) -> str:
    """
    Extract the plain hostname from an entry. The settings may contain anything
    a human writes in there: 'https://rmm.foo.de', 'rmm.foo.de:4000' or just
    'rmm.foo.de'.
    """
    v = (value or "").strip().lower()
    if not v:
        return ""
    if "://" in v:
        v = urlparse(v).netloc or v.split("://", 1)[1]
    v = v.split("/", 1)[0]
    # Keep IPv6 in brackets, otherwise the port split breaks the address.
    if v.startswith("["):
        return v.split("]", 1)[0] + "]" if "]" in v else v
    return v.rsplit(":", 1)[0] if v.count(":") == 1 else v


def allowed_hosts() -> set[str]:
    """All currently allowed addresses from the settings."""
    hosts = set(ALWAYS_ALLOWED)
    for key in ("server_url", "server_domain", "server_host"):
        h = _clean_host(db.get_setting(key, "") or "")
        if h:
            hosts.add(h)
    for extra in (db.get_setting("host_lock_extra", "") or "").split(","):
        h = _clean_host(extra)
        if h:
            hosts.add(h)
    return hosts


def is_enabled() -> bool:
    return (db.get_setting("host_lock_enabled", "0") or "0") == "1"


# --- Config cache -----------------------------------------------------------
# The host lock is checked on EVERY request. Previously the middleware read the
# settings synchronously from the DB - in the event-loop thread. Each of these
# reads takes the global DB lock; if a worker thread holds it right now (e.g. a
# slow commit() on a VM disk), the entire event loop is BLOCKED until the
# watchdog ends the process. This happened even with the lock TURNED OFF (two
# get_setting calls per request). Therefore: keep the few settings in a
# short-lived cache and NEVER do the (rare) reload in the loop thread.
# localhost stays always allowed regardless (see allowed_hosts); a change takes
# effect after at most _CFG_TTL seconds.
_CFG_TTL = 3.0
_cfg_cache: dict = {"ts": 0.0, "enabled": False, "trust_proxy": False,
                    "scope": "ui", "hosts": frozenset(ALWAYS_ALLOWED)}


def _load_cfg() -> dict:
    """Reads the host-lock settings fresh from the DB.
    Call ONLY in a worker thread, never in the event-loop thread - the calls
    take the global DB lock and can block."""
    return {
        "ts": time.monotonic(),
        "enabled": (db.get_setting("host_lock_enabled", "0") or "0") == "1",
        "trust_proxy": (db.get_setting("host_lock_trust_proxy", "0") or "0") == "1",
        "scope": (db.get_setting("host_lock_scope", "ui") or "ui"),
        "hosts": frozenset(allowed_hosts()),
    }


def _cfg() -> dict:
    """Cached config; reloads at most every _CFG_TTL seconds.
    Also call only outside the event loop (see _load_cfg)."""
    if time.monotonic() - _cfg_cache["ts"] >= _CFG_TTL:
        try:
            _cfg_cache.update(_load_cfg())
        except Exception:
            pass        # on a DB error keep the last known state
    return _cfg_cache


def _request_host(request, trust_proxy: bool) -> str:
    """
    Determine the requested host. Behind a reverse proxy the real name is in
    X-Forwarded-Host - but this header is trusted only if that is explicitly
    enabled. Otherwise anyone could send it and bypass the lock.
    """
    if trust_proxy:
        fwd = request.headers.get("x-forwarded-host")
        if fwd:
            return _clean_host(fwd.split(",")[0])
    return _clean_host(request.headers.get("host", ""))


def check(request, cfg: dict | None = None) -> tuple[bool, str]:
    """(allowed?, requested host) - also usable by the routes.
    Without cfg the settings are read fresh (for routes/diagnostics); the
    middleware passes in the cached state so the check does not block the event
    loop."""
    if cfg is None:
        cfg = _load_cfg()
    host = _request_host(request, cfg["trust_proxy"])
    if not cfg["enabled"]:
        return True, host
    if request.url.path in EXEMPT_PATHS:
        return True, host
    # Scope: 'ui' protects only the UI and the API, but leaves the agents
    # alone. This matters because agents are connected to the address they were
    # set up with - usually the IP. If one locks to 'domain only' and chooses
    # 'all', one loses the entire fleet at that moment, until every agent is
    # set up anew.
    if cfg["scope"] == "ui" and request.url.path.startswith("/socket.io"):
        return True, host
    if not host:
        # Requests without a Host header are HTTP/1.0 or bots. With the lock
        # active there is no reason to let them through.
        return False, host
    if host in cfg["hosts"]:
        return True, host
    # Additionally check loopback numerically (e.g. 127.0.0.5).
    try:
        if ipaddress.ip_address(host.strip("[]")).is_loopback:
            return True, host
    except ValueError:
        pass
    return False, host


class HostLockMiddleware(BaseHTTPMiddleware):
    """
    Checks every request. The settings live in a short-lived cache (see _cfg)
    and are reloaded when needed OUTSIDE the event loop - so the check can
    never block the loop, even when the DB is busy. A change takes effect after
    at most _CFG_TTL seconds; localhost stays always reachable (lockout
    protection).
    """

    async def dispatch(self, request, call_next):
        try:
            # Reload the config only when the cache has expired - and then in
            # the thread pool, so the global DB lock never blocks the event
            # loop. The actual check below is pure Python on the cached state
            # (no DB access).
            if time.monotonic() - _cfg_cache["ts"] >= _CFG_TTL:
                await asyncio.get_running_loop().run_in_executor(None, _cfg)
            ok, host = check(request, _cfg_cache)
        except Exception as e:
            # Previously requests were let through silently here. That is the
            # worst of all options: the lock looks enabled but has no effect,
            # and nobody learns of it. Now the error is logged loudly. Requests
            # are let through only while the lock is OFF - if it is on, in doubt
            # it blocks.
            print(f"[hostlock] check failed: {e!r}")
            if not _cfg_cache.get("enabled", False):
                return await call_next(request)
            return PlainTextResponse(
                "Host check currently not possible - access denied as a "
                "precaution. Details in the server log.", status_code=503)

        if ok:
            return await call_next(request)

        return PlainTextResponse(
            "Access via this address is not allowed.\n\n"
            f"Requested: {host or '(no Host header)'}\n"
            "Please use the address configured in the settings.\n\n"
            "Locked out? From the server itself the UI is always reachable via\n"
            "http://localhost; there the lock can be turned off again under\n"
            "Settings > General.",
            status_code=403)


def diagnostics(request) -> dict:
    """
    What does the server actually see? Without this info a lock that does not
    take effect is hard to debug: one knows neither which Host header the
    browser sends nor which addresses are currently allowed.
    """
    ok, host = check(request)
    return {
        "enabled": is_enabled(),
        "scope": db.get_setting("host_lock_scope", "ui"),
        "trust_proxy": db.get_setting("host_lock_trust_proxy", "0") == "1",
        "raw_host_header": request.headers.get("host", ""),
        "x_forwarded_host": request.headers.get("x-forwarded-host", ""),
        "seen_as": host,
        "allowed": sorted(allowed_hosts()),
        "would_pass": ok,
        "sources": {
            "server_url": db.get_setting("server_url", "") or "",
            "server_domain": db.get_setting("server_domain", "") or "",
            "server_host": db.get_setting("server_host", "") or "",
            "host_lock_extra": db.get_setting("host_lock_extra", "") or "",
        },
    }
