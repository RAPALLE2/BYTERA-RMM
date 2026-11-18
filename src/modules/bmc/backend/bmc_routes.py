# bmc_routes.py (Modul "bmc") - iLO/iDRAC ueber Redfish verwalten
# ---------------------------------------------------------------
# Rechte: use_bmc (ansehen/steuern), manage_bmc (Verbindungen anlegen/aendern).
# Ausfuehrung je Verbindung: Backend direkt ODER ueber eine Node im
# Management-Netz (Feld "via"). Die Logik steckt in redfish.py - EINE Datei fuer
# Backend und Node.
#
#   GET    /api/bmc/connections            Verbindungen (ohne Geheimnisse)
#   POST   /api/bmc/connections            anlegen            (manage_bmc)
#   PUT    /api/bmc/connections/{id}        aendern            (manage_bmc)
#   DELETE /api/bmc/connections/{id}        loeschen           (manage_bmc)
#   GET    /api/bmc/links                   client_id -> BMC   (fuer Topologie/Knopf)
#   GET    /api/bmc/nodes                   verfuegbare Nodes
#   POST   /api/bmc/connections/{id}/test   Verbindung testen
#   POST   /api/bmc/{id}/call/{op}          Redfish-Operation (power, sensors, ...)
#   GET    /api/bmc/{id}/console            Zugang zur nativen Web-UI/Konsole
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import db
from app.auth import get_current_user, require_perm, user_has_permission

from . import redfish, store

router = APIRouter(prefix="/api/bmc", tags=["bmc"])

WRITE_OPS = {"power", "set_boot", "identify", "vm_insert", "vm_eject", "log_clear", "manager_reset"}


def _use(user):
    require_perm(user, "use_bmc")


def _manage(user):
    require_perm(user, "manage_bmc")


def _conn(cid) -> dict:
    c = store.get(cid)
    if not c:
        raise HTTPException(404, "Verbindung nicht gefunden")
    return c


def _may_see(user, c) -> bool:
    # Globales Recht use_bmc reicht; bei verknuepftem Client zusaetzlich Client-Sicht pruefen
    if not user_has_permission(user, "use_bmc"):
        return False
    if c.get("client_id"):
        return user_has_permission(user, "use_bmc", c["client_id"]) or user_has_permission(user, "c_power", c["client_id"])
    return True


# ------------------------------------------------ Verbindungen (CRUD)
@router.get("/connections")
def list_connections(user: dict = Depends(get_current_user)):
    _use(user)
    return {"connections": [store.public(c) for c in store.list_conns() if _may_see(user, c)]}


class ConnBody(BaseModel):
    name: str = ""
    host: str = ""
    port: int | None = None
    username: str = ""
    password: str | None = None
    vendor: str = ""
    verify_tls: bool = False
    fingerprint: str = ""
    insecure_http: bool = False
    client_id: str = ""
    via: str = ""


def _validate(body: ConnBody):
    if not body.host.strip():
        raise HTTPException(400, "Adresse (Host/IP) fehlt")
    if body.client_id and not db.get_client(body.client_id):
        raise HTTPException(400, "Verknüpfter Client existiert nicht")


@router.post("/connections")
def create_connection(body: ConnBody, user: dict = Depends(get_current_user)):
    _manage(user)
    _validate(body)
    c = store.create(body.model_dump())
    db.add_audit_entry(user["username"], "bmc.created", target=c["id"], details=f"{c.get('name')} ({c.get('host')})")
    return store.public(c)


@router.put("/connections/{cid}")
def edit_connection(cid: str, body: ConnBody, user: dict = Depends(get_current_user)):
    _manage(user)
    _validate(body)
    c = store.edit(cid, body.model_dump())
    if not c:
        raise HTTPException(404, "Verbindung nicht gefunden")
    db.add_audit_entry(user["username"], "bmc.updated", target=cid, details=c.get("name"))
    return store.public(c)


@router.delete("/connections/{cid}")
def delete_connection(cid: str, user: dict = Depends(get_current_user)):
    _manage(user)
    store.delete(cid)
    db.add_audit_entry(user["username"], "bmc.deleted", target=cid)
    return {"ok": True}


@router.get("/links")
def get_links(user: dict = Depends(get_current_user)):
    _use(user)
    out = {}
    for cid, info in store.links().items():
        if user_has_permission(user, "use_bmc", cid) or user_has_permission(user, "c_power", cid):
            out[cid] = info
    return {"links": out}


@router.get("/nodes")
def get_nodes(user: dict = Depends(get_current_user)):
    _use(user)
    try:
        from app.node_manager import node_choices
        return {"nodes": node_choices("bmc", user)}
    except Exception:
        return {"nodes": []}


# ------------------------------------------------ Ausfuehrung (Backend oder Node)
async def _run(c: dict, op: str, args: dict):
    via = (c.get("via") or "").strip()
    conn = store.creds(c)
    if not via:
        def work():
            client = redfish.RedfishClient(conn).connect()
            try:
                return client.dispatch(op, args)
            finally:
                client.close()
        try:
            return await asyncio.to_thread(work)
        except redfish.RedfishError as exc:
            raise HTTPException(exc.status or 400, str(exc))
    from app import node_manager
    try:
        node_manager.require_module(via, "bmc")
        res = await node_manager.command(via, "bmc-call", {"conn": conn, "op": op, "args": args}, timeout=120)
    except Exception as exc:
        raise HTTPException(502, f"Node: {exc}")
    res = res or {}
    if res.get("error"):
        raise HTTPException(400, res["error"])
    return res.get("result")


@router.post("/connections/{cid}/test")
async def test_connection(cid: str, user: dict = Depends(get_current_user)):
    _use(user)
    c = _conn(cid)
    if not _may_see(user, c):
        raise HTTPException(403, "Keine Berechtigung")
    s = await _run(c, "summary", {})
    # Hersteller merken (fuer Liste/Topologie), ohne Geheimnisse offenzulegen
    vendor = (s or {}).get("vendor")
    if vendor and vendor != c.get("vendor"):
        store.edit(cid, {**{k: c.get(k) for k in store.PUBLIC_FIELDS if k not in ("id",)}, "vendor": vendor})
    return {"ok": True, "summary": s}


class CallBody(BaseModel):
    args: dict = {}


@router.post("/{cid}/call/{op}")
async def call_op(cid: str, op: str, body: CallBody | None = None, user: dict = Depends(get_current_user)):
    _use(user)
    c = _conn(cid)
    if not _may_see(user, c):
        raise HTTPException(403, "Keine Berechtigung")
    if op not in redfish.RedfishClient.OPS:
        raise HTTPException(404, "Unbekannte Operation")
    args = (body.args if body else {}) or {}
    res = await _run(c, op, args)
    if op in WRITE_OPS:
        short = {k: v for k, v in args.items() if k not in ("body",)}
        db.add_audit_entry(user["username"], f"bmc.{op}", target=cid, details=f"{c.get('name')}: {short}"[:400])
    return {"result": res}


@router.get("/{cid}/console")
def console(cid: str, user: dict = Depends(get_current_user)):
    """Zugang zur nativen Web-UI/Remote-Konsole der BMC.
    direct = Browser erreicht die BMC selbst (neuer Tab auf https://host).
    proxy  = ueber das Backend (Modul-Einstellung proxy_host gesetzt)."""
    _use(user)
    c = _conn(cid)
    if not _may_see(user, c):
        raise HTTPException(403, "Keine Berechtigung")
    from . import webproxy
    return webproxy.console_access(c)
