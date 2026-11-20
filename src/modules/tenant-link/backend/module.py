# module.py (module "tenant-link") - allow embedding by an approved main RMM
# --------------------------------------------------------------------------
# Important: a standard RMM instance sets NO X-Frame-Options/CSP, so the dashboard
# is embeddable anyway and the auto-login runs through the normal login API.
# This module keeps a revocable trust record per main RMM (see store.py) and, as
# a best effort - only if it runs early enough during start - installs a middleware
# that RESTRICTS frame-ancestors to the approved parents (harder security).
from app import modules

from . import store

_tried = {"done": False}


def parents() -> list[str]:
    """Approved parent origins (active pairings) plus any legacy manual entries."""
    out = list(store.active_origins())
    try:
        raw = (modules.setting("tenant-link", "parents", "") or "").strip()
    except Exception:
        raw = ""
    for p in raw.replace("\n", ",").split(","):
        o = store.norm_origin(p)
        if o and o not in out:
            out.append(o)
    return out


def _try_middleware(ctx):
    """Best effort: only possible while the app has not started yet."""
    if _tried["done"]:
        return
    _tried["done"] = True
    api = modules._api
    if api is None:
        return
    try:
        @api.middleware("http")
        async def _frame(request, call_next):
            resp = await call_next(request)
            if not modules.is_enabled("tenant-link"):
                return resp
            ps = parents()
            anc = "frame-ancestors 'self'" + ("" if not ps else " " + " ".join(ps))
            csp = resp.headers.get("Content-Security-Policy", "")
            if "frame-ancestors" not in csp:
                resp.headers["Content-Security-Policy"] = (csp + " " + anc).strip()
            resp.headers.pop("X-Frame-Options", None)
            return resp
        ctx.log("Embedding actively controlled (frame-ancestors set)")
    except Exception:
        # The app is already running -> the middleware can no longer be installed.
        # Embedding still works because a standard RMM instance sets no
        # X-Frame-Options/CSP.
        pass


def on_startup(ctx):
    _try_middleware(ctx)
    ps = parents()
    ctx.log("Tenant sharing active. Embedding by: "
            + (", ".join(ps) if ps else "no approved main RMM yet"))
