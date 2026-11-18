# gpodns_routes.py (Modul "addir") - DNS-Einträge und Gruppenrichtlinien (Recht manage_ad)
# ----------------------------------------------------------------------------------------
# Agentenlos: LDAP (vorhandene AD-Verbindung) + SYSVOL per SMB mit demselben Konto.
# Alternativ ueber eine BYTERA-NODE auf dem Domaenencontroller (Node-Teil "addir"):
# dann laeuft alles dort (LDAP lokal, SYSVOL als Ordner, ADMX aus C:\Windows).
# Die Logik steckt in node/adcore.py - EINE Datei fuer Backend und Node.
#
#   GET  /api/ad/{id}/exec            Ausfuehrungsweg + verfuegbare Nodes
#   PUT  /api/ad/{id}/exec            {via: "" (Backend) | node-client-id}
#   POST /api/ad/{id}/core/{op}       Operation des Kerns (DNS, GPO, ADMX, Registry ...)
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import db
from app.auth import get_current_user, require_perm

from . import ldap_client, store

router = APIRouter(prefix="/api/ad", tags=["active-directory"])

_spec = importlib.util.spec_from_file_location(
    "rmm_addir_adcore", Path(__file__).resolve().parent.parent / "node" / "adcore.py")
adcore = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(adcore)

# Operationen, die etwas veraendern (-> Audit-Log)
WRITE_OPS = {"dns_add", "dns_update", "dns_delete", "gpo_create", "gpo_delete", "gpo_rename", "gpo_status",
             "gpo_link", "gpo_unlink", "gpo_link_set", "gpo_block", "gpo_filter_set", "registry_set",
             "admx_set", "security_set"}


def _perm(user):
    require_perm(user, "manage_ad")


def _conn(cid: str) -> dict:
    c = store.get(cid)
    if not c:
        raise HTTPException(404, "Verbindung nicht gefunden")
    return c


def _host(conn: dict) -> str:
    h = (conn.get("server") or "").strip()
    for p in ("ldaps://", "ldap://"):
        if h.startswith(p):
            h = h[len(p):]
    return h.split("/")[0].split(":")[0]


def _smb_user(ad, conn: dict) -> str:
    """SMB braucht DOMAENE\\user oder user@domaene - aus dem Bind-Benutzer ableiten."""
    u = (conn.get("bind_user") or "").strip()
    if "\\" in u or "@" in u or not u:
        return u
    if u.upper().startswith("CN="):
        try:
            r = ad._search(u, "(objectClass=*)", ("sAMAccountName",), scope=__import__("ldap3").BASE)
            sam = str(r[0]["sAMAccountName"]) if r else ""
            dom = ".".join(p.split("=", 1)[1] for p in ad.base_dn.split(",") if p.strip().upper().startswith("DC="))
            if sam:
                return f"{sam}@{dom}"
        except Exception:
            pass
    return u


def _run_backend(conn: dict, op: str, args: dict):
    with ldap_client.Ad(conn) as ad:
        host = _host(conn)
        user = _smb_user(ad, conn)
        pw = store.dec(conn.get("bind_pw_enc", ""))
        dom = ".".join(p.split("=", 1)[1] for p in ad.base_dn.split(",") if p.strip().upper().startswith("DC="))
        needs_fs = not op.startswith("dns_")
        fs = adcore.SmbFs(f"\\\\{host}\\SYSVOL\\{dom}", host, user, pw) if needs_fs else None
        fallback = None
        if op in ("admx_tree", "admx_policy", "admx_set", "configured"):
            try:   # Rueckfall ohne Central Store: PolicyDefinitions des DC (Admin-Freigabe C$)
                fallback = adcore.SmbFs(f"\\\\{host}\\C$\\Windows\\PolicyDefinitions", host, user, pw)
            except Exception:
                fallback = None
        core = adcore.Core(ad._conn, ad.base_dn, fs, fallback)
        return core.dispatch(op, args)


async def _run(cid: str, op: str, args: dict):
    conn = _conn(cid)
    via = (conn.get("via") or "").strip()
    if not via:
        try:
            return await asyncio.to_thread(_run_backend, conn, op, args)
        except (adcore.CoreError, ldap_client.AdError) as exc:
            raise HTTPException(400, str(exc))
        except OSError as exc:
            raise HTTPException(502, f"SYSVOL/SMB nicht erreichbar: {exc}")
    from app import node_manager
    try:
        node_manager.require_module(via, "addir")
        node_conn = {k: conn.get(k) for k in ("server", "port", "use_ssl", "start_tls", "verify_cert",
                                              "base_dn", "bind_user")}
        node_conn["bind_password"] = store.dec(conn.get("bind_pw_enc", ""))
        res = await node_manager.command(via, "ad-call", {"conn": node_conn, "op": op, "args": args},
                                         timeout=600)
    except Exception as exc:
        raise HTTPException(502, f"Node: {exc}")
    res = res or {}
    if res.get("error"):
        raise HTTPException(400, res["error"])
    return res.get("result")


@router.get("/{cid}/exec")
def get_exec(cid: str, user: dict = Depends(get_current_user)):
    _perm(user)
    conn = _conn(cid)
    try:
        from app.node_manager import node_choices
        nodes = node_choices("addir", user)
    except Exception:
        nodes = []
    return {"via": conn.get("via") or "", "nodes": nodes}


class ExecBody(BaseModel):
    via: str = ""


@router.put("/{cid}/exec")
def set_exec(cid: str, body: ExecBody, user: dict = Depends(get_current_user)):
    _perm(user)

    def upd(d):
        c = d["conns"].get(cid)
        if c is not None:
            c["via"] = (body.via or "").strip()
        return c
    if not store.update(upd):
        raise HTTPException(404, "Verbindung nicht gefunden")
    return {"ok": True, "via": body.via}


@router.post("/{cid}/core/{op}")
async def core_op(cid: str, op: str, args: dict | None = None, user: dict = Depends(get_current_user)):
    _perm(user)
    if op not in adcore.Core.OPS:
        raise HTTPException(404, "Unbekannte Operation")
    res = await _run(cid, op, args or {})
    if op in WRITE_OPS:
        try:
            short = {k: v for k, v in (args or {}).items() if k not in ("values", "value")}
            db.add_audit_entry(user["username"], f"ad.{op}", details=f"{_conn(cid).get('name')}: {short}"[:500])
        except Exception:
            pass
    return {"result": res}
