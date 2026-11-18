# console_routes.py (module "proxmox") - console of VMs/LXC
# ---------------------------------------------------------
# 1) Console IN THE DASHBOARD (noVNC, tunneled through the RMM)
#    POST /api/proxmox/{cid}/guests/{node}/{type}/{vmid}/console
#         -> Proxmox: POST .../vncproxy {websocket: 1}  => VNC port + VNC ticket
#         -> one-time session (60 s) {sid, ws, password}
#    WS   /api/proxmox/{cid}/console/ws?sid=...   dashboard session in the WebSocket protocol
#         "rmmtoken.<JWT>" (NOT in the address - that ends up in the server log)
#         -> backend connects to wss://<proxmox>/api2/json/nodes/../vncwebsocket
#            (cookie PVEAuthCookie) and forwards the data in both directions.
#    Requires user + password login: Proxmox only issues consoles for user
#    sessions (ticket), not for API tokens.
#
# 2) Proxmox WEB UI in the browser
#    POST /api/proxmox/{cid}/guests/{node}/{type}/{vmid}/webui {via: direct|forward}
#         direct  -> address of the web UI (only for "local"/"custom address"; your browser
#                    must be able to reach Proxmox)
#         forward -> forwarding via the RMM server, reachable ONLY from your IP (60 min)
#    In the web UI you sign in with your Proxmox account.
#
# Permission: pve_console (+ guest visible: pve_vms_all/pve_lxc_all or guest share).
from __future__ import annotations

import asyncio
import ipaddress
import secrets
import ssl
import time
import urllib.parse

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from pydantic import BaseModel

from app import db, modules
from app.auth import decode_access_token, get_current_user

from . import pve_client, pve_store
from .proxmox_routes import _audit, _check_ids, _guest, _guest_visible, _need, _pve

router = APIRouter(prefix="/api/proxmox", tags=["proxmox-console"])

_sessions: dict[str, dict] = {}
SESSION_TTL = 60


def _purge():
    now = time.time()
    for k in [k for k, v in _sessions.items() if v["expires"] < now]:
        _sessions.pop(k, None)


# ------------------------------------------------------------------ Request console
@router.post("/{cid}/guests/{node}/{gtype}/{vmid}/console")
def console_start(cid: str, node: str, gtype: str, vmid: int, user: dict = Depends(get_current_user)):
    cl = _guest(user, cid, node, gtype, vmid, "pve_console")
    _pve(cl.ticket)                                          # password login required (409 with token)
    res = _pve(cl.call, "POST", f"/nodes/{node}/{gtype}/{int(vmid)}/vncproxy", {"websocket": 1})
    if not res or "port" not in res or "ticket" not in res:
        raise HTTPException(502, "Proxmox hat keinen Konsolen-Zugang geliefert")
    _purge()
    sid = secrets.token_urlsafe(24)
    _sessions[sid] = {"user_id": user["id"], "cid": cid, "node": node, "gtype": gtype, "vmid": int(vmid),
                      "port": int(res["port"]), "vncticket": res["ticket"], "expires": time.time() + SESSION_TTL}
    _audit(user, cid, "console", f"{gtype}/{vmid} auf {node}")
    return {"sid": sid, "ws": f"/api/proxmox/{cid}/console/ws?sid={sid}", "password": res["ticket"]}


# ------------------------------------------------------------------ WebSocket-Tunnel
def _ssl_ctx(cfg: dict) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if cfg.get("fingerprint") or not cfg.get("verify_tls"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _check_fingerprint(host: str, port: int, expected: str) -> None:
    """Before sending the ticket: compare the peer certificate with the stored fingerprint."""
    got = pve_client.fetch_fingerprint(host, port).replace(":", "").lower()
    if got != expected.replace(":", "").replace(" ", "").lower():
        raise pve_client.PveError(495, "Certificate fingerprint does not match")


@router.websocket("/{cid}/console/ws")
async def console_ws(websocket: WebSocket, cid: str, sid: str = ""):
    # Auth: dashboard session + one-time console session (sid, 60 s, usable once).
    # Browser WebSockets cannot set headers -> the session arrives as a subprotocol
    # "rmmtoken.<JWT>" (not logged, unlike the address).
    offered = websocket.scope.get("subprotocols") or []
    token = next((p[len("rmmtoken."):] for p in offered if p.startswith("rmmtoken.")), "")
    try:
        payload = decode_access_token(token)
        user = db.get_user_by_id(payload.get("sub"))
    except Exception:
        user = None
    sess = _sessions.pop(sid, None)
    if not user or not sess or sess["user_id"] != user["id"] or sess["cid"] != cid or sess["expires"] < time.time():
        await websocket.close(code=4401)
        return
    try:
        _need(user, cid, "pve_console")
        if not _guest_visible(user, cid, sess["gtype"], sess["vmid"]):
            raise HTTPException(404, "Guest not shared")
    except HTTPException:
        await websocket.close(code=4403)
        return
    proto = "binary" if "binary" in offered else None
    await websocket.accept(subprotocol=proto)
    loop = asyncio.get_running_loop()
    try:
        cl = pve_client.client_for(cid)
        base = await loop.run_in_executor(None, cl.base)                 # (Relay-Tunnel ggf. aufbauen)
        pve_ticket, _ = await loop.run_in_executor(None, cl.ticket)
        u = urllib.parse.urlparse(base)
        cfg = cl.cfg
        if cfg.get("fingerprint"):
            await loop.run_in_executor(None, _check_fingerprint, u.hostname, u.port or 8006, cfg["fingerprint"])
        path = (f"/api2/json/nodes/{sess['node']}/{sess['gtype']}/{sess['vmid']}/vncwebsocket"
                f"?port={sess['port']}&vncticket={urllib.parse.quote(sess['vncticket'], safe='')}")
        uri = f"wss://{u.netloc}{path}"
        from websockets.asyncio.client import connect
        async with connect(uri, ssl=_ssl_ctx(cfg), subprotocols=["binary"], max_size=None, open_timeout=15,
                           additional_headers={"Cookie": f"PVEAuthCookie={pve_ticket}"}) as pve:

            async def browser_to_pve():
                while True:
                    msg = await websocket.receive()
                    if msg["type"] == "websocket.disconnect":
                        break
                    data = msg.get("bytes") if msg.get("bytes") is not None else (msg.get("text") or "").encode()
                    await pve.send(data)

            async def pve_to_browser():
                async for data in pve:
                    if isinstance(data, str):
                        data = data.encode()
                    await websocket.send_bytes(data)

            tasks = [asyncio.create_task(browser_to_pve()), asyncio.create_task(pve_to_browser())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
    except Exception as e:      # noqa: BLE001 - close the connection, reason to the log
        print(f"[proxmox] console {cid} {sess['gtype']}/{sess['vmid']}: {type(e).__name__}: {e}")
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# ------------------------------------------------------------------ Web UI
class WebUiBody(BaseModel):
    via: str = "direct"          # direct | forward


@router.post("/{cid}/guests/{node}/{gtype}/{vmid}/webui")
async def webui(cid: str, node: str, gtype: str, vmid: int, body: WebUiBody, request: Request,
                user: dict = Depends(get_current_user)):
    _need(user, cid, "pve_console")
    _check_ids(node, gtype, vmid)
    if not _guest_visible(user, cid, gtype, vmid):
        raise HTTPException(404, "Guest not found or not shared")
    cfg = pve_store.get(cid) or {}
    port = int(cfg.get("port") or 8006)
    query = urllib.parse.urlencode({"console": "kvm" if gtype == "qemu" else "lxc", "novnc": 1, "vmid": int(vmid),
                                    "node": node, "resize": "off"})
    if body.via == "direct":
        if cfg.get("addr_mode") == "relay":
            raise HTTPException(409, "With \"RMM relay\" Proxmox is not directly reachable for your browser - choose \"via forwarding\"")
        host = cfg.get("host") if cfg.get("addr_mode") == "custom" else ((db.get_client(cid) or {}).get("ip") or "")
        if not host:
            raise HTTPException(409, "Keine Adresse bekannt")
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        _audit(user, cid, "webui", f"direkt {gtype}/{vmid}")
        return {"url": f"https://{host}:{port}/?{query}", "via": "direct"}
    if body.via != "forward":
        raise HTTPException(400, "via must be direct or forward")
    pf = modules.use("network", "portforward") if modules.is_enabled("network") else None
    if pf is None or not modules.feature("network", "portforward"):
        raise HTTPException(409, "Forwarding needs the \"Network\" module with port forwarding")
    ip = request.client.host if request.client else ""
    try:
        a = ipaddress.ip_address(ip)
        allow = f"{a}/{32 if a.version == 4 else 128}"
    except ValueError:
        raise HTTPException(400, "Your IP address could not be determined")
    # target from the agent's point of view
    target = cfg.get("relay_host") if cfg.get("addr_mode") == "relay" else (
        cfg.get("host") if cfg.get("addr_mode") == "custom" else "127.0.0.1")
    try:
        rec = await pf.create(client_id=cid, target_port=port, username=user["username"], minutes=60,
                              target_host=target or "127.0.0.1", allow_from=allow, label="Proxmox web UI")
    except ValueError as e:
        raise HTTPException(409, f"Agent unreachable: {e}")
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    host = request.url.hostname or ""
    _audit(user, cid, "webui", f"forwarding {gtype}/{vmid} port {rec['listen_port']} only {allow}")
    return {"url": f"https://{host}:{rec['listen_port']}/?{query}", "via": "forward", "expires": rec.get("expires_at"),
            "note": "The forwarding is reachable only from your IP for 60 minutes. Proxmox shows its certificate "
                    "(self-signed) - confirm once in the browser and sign in with the Proxmox account."}
