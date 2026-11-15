"""
tls_listener.py (core) - the dashboard/agent endpoint additionally with TLS
==========================================================================
A second listener of the SAME app on RMM_TLS_PORT (default 4443, 0 = off) with
the backend's own self-signed certificate (security.ensure_tls_cert). Agents
and client apps pin its fingerprint. If the port can't be opened (in use, not
published by Docker) nothing breaks: devices simply stay on the normal address,
end-to-end encrypted anyway.
"""
from __future__ import annotations

import asyncio
import os

from app import security


async def run(app) -> None:
    port = security.tls_port()
    if not port:
        print("[tls] own TLS port disabled (RMM_TLS_PORT=0)")
        return
    try:
        import uvicorn
        cert, key = security.ensure_tls_cert()
        host = (os.getenv("HOST") or "0.0.0.0").strip() or "0.0.0.0"
        cfg = uvicorn.Config(app, host=host, port=port, ssl_certfile=str(cert), ssl_keyfile=str(key),
                             lifespan="off", log_level="warning", proxy_headers=True,
                             forwarded_allow_ips="127.0.0.1", ws_max_size=64 * 1024 * 1024)
        server = uvicorn.Server(cfg)
        server.install_signal_handlers = lambda: None       # the main server owns the signals
        security.TLS_STATUS.update(active=True, error="")
        print(f"[tls] encrypted endpoint on port {port} (self-signed, pin {security.tls_pin()[:16]}…)")
        serve = getattr(server, "_serve", None) or server.serve
        await serve()
    except asyncio.CancelledError:
        raise
    except BaseException as e:                               # uvicorn calls sys.exit on bind errors
        security.TLS_STATUS.update(active=False, error=str(e) or e.__class__.__name__)
        print(f"[tls] own TLS port {port} not available ({security.TLS_STATUS['error']}) - "
              "devices stay on the normal address (still end-to-end encrypted)")
    finally:
        if security.TLS_STATUS.get("active"):
            security.TLS_STATUS["active"] = False
