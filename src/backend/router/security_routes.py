"""
security_routes.py (core) - encryption towards agents and client apps
====================================================================
  GET  /api/security/info       public: server PUBLIC key + TLS fingerprint (trust on first use)
  GET  /api/security/status     admin: settings + encryption state of every connected agent
  PUT  /api/security/settings   admin: {"require_e2e": bool}
  POST /api/e2e                 end-to-end encrypted REST envelope (client apps, agents):
                                body  = b"BXE1" kid(8) epk(65) salt(16) seal(c2s, request json)
                                reply = seal(s2c, response json)
The envelope carries a normal API request (method, path, headers incl.
Authorization / X-Agent-Token, body) - so reverse proxies only see ciphertext.
"""
from __future__ import annotations

import base64
import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from app import db, security
from app.auth import get_current_user, is_super_admin

router = APIRouter(tags=["security"])


@router.get("/api/security/info")
def info():
    return security.public_info()


def _admin(user):
    if not is_super_admin(user) and user.get("role") != "admin":
        raise HTTPException(403, "Administrators only")


@router.get("/api/security/status")
def status(user: dict = Depends(get_current_user)):
    _admin(user)
    from app import sockets
    agents = []
    for c in db.list_clients():
        cid = c["id"]
        sec = sockets.security_of(cid)
        agents.append({"id": cid, "hostname": c.get("hostname") or cid[:8], "online": sec is not None,
                       "e2e": bool(sec and sec.get("e2e")), "tls": bool(sec and sec.get("tls")),
                       "agent_version": c.get("agent_version") or ""})
    agents.sort(key=lambda a: (not a["online"], a["e2e"], a["hostname"].lower()))
    on = [a for a in agents if a["online"]]
    return {"info": security.public_info(), "tls": dict(security.TLS_STATUS),
            "require_e2e": security.require_e2e(), "agents": agents,
            "summary": {"online": len(on), "e2e": sum(a["e2e"] for a in on), "tls": sum(a["tls"] for a in on),
                        "plain": sum(not a["e2e"] for a in on)}}


class SecSettings(BaseModel):
    require_e2e: bool


@router.put("/api/security/settings")
def put_settings(body: SecSettings, user: dict = Depends(get_current_user)):
    _admin(user)
    db.set_setting("security_require_e2e", "1" if body.require_e2e else "0")
    db.add_audit_entry(user["username"], "security.settings", details=f"require_e2e={body.require_e2e}")
    return {"require_e2e": body.require_e2e}


# ------------------------------------------------------------------ REST envelope
_SKIP_RESP = {"content-length", "transfer-encoding", "connection", "content-encoding"}


@router.post("/api/e2e")
async def envelope(request: Request):
    raw = await request.body()
    if len(raw) < 4 + 8 + 65 + 16 + 28 or raw[:4] != b"BXE1":
        raise HTTPException(400, "bad envelope")
    kid = raw[4:12].hex()
    if kid != security.key_id():
        # the device has an old/foreign server key - it must re-fetch /api/security/info
        raise HTTPException(409, "unknown server key")
    try:
        ch = security.server_channel(raw[12:77], raw[77:93], kid)
        req = json.loads(ch.open(raw[93:], aad=b"req"))
    except Exception:
        raise HTTPException(400, "envelope could not be decrypted")
    if abs(time.time() - float(req.get("ts") or 0)) > security.MAX_SKEW:
        raise HTTPException(400, "envelope expired (check the device clock)")
    method = str(req.get("m") or "GET").upper()
    path = str(req.get("p") or "/")
    if not path.startswith("/api/") or path.startswith("/api/e2e"):
        raise HTTPException(400, "path not allowed")
    headers = {str(k): str(v) for k, v in (req.get("h") or {}).items()
               if str(k).lower() not in ("host", "content-length", "transfer-encoding")}
    body = base64.b64decode(req.get("b") or "") if req.get("b") else b""

    import httpx
    from app.main import socket_app
    client_ip = request.client.host if request.client else "127.0.0.1"
    transport = httpx.ASGITransport(app=socket_app, client=(client_ip, 0))
    host = request.headers.get("host") or "localhost"         # same Host as outside (host lock)
    async with httpx.AsyncClient(transport=transport, base_url=f"http://{host}") as c:
        r = await c.request(method, path, headers=headers, content=body or None, timeout=300)
    out = {"s": r.status_code,
           "h": {k: v for k, v in r.headers.items() if k.lower() not in _SKIP_RESP},
           "b": base64.b64encode(r.content).decode()}
    return Response(ch.seal(json.dumps(out).encode(), aad=b"resp"), media_type="application/octet-stream",
                    headers={"Cache-Control": "no-store"})
