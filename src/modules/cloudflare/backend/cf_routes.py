# cf_routes.py (Modul "cloudflare") - Verwaltung (Recht manage_cloudflare)
# ------------------------------------------------------------------------
#   GET    /api/cloudflare/state            settings, routes, connector, services, nodes
#   POST   /api/cloudflare/auth             {type: token|key, token|key, email} -> check + accounts
#   POST   /api/cloudflare/account          {account_id, public_base} -> Zonen + Team-Domain
#   POST   /api/cloudflare/refresh          Zonen/Team-Domain neu holen
#   POST   /api/cloudflare/routes           create/modify a route (+ sync immediately)
#   DELETE /api/cloudflare/routes/{rid}     remove the route + everything in Cloudflare
#   POST   /api/cloudflare/sync             re-sync everything
#   POST   /api/cloudflare/connector/{act}  start | stop | restart
#   DELETE /api/cloudflare/nodes/{cid}      remove the node tunnel
#   DELETE /api/cloudflare/all              remove EVERYTHING (routes, tunnels, access)
#   GET    /api/cloudflare/directory        Benutzer + Gruppen (Freigabe RMM-Login)

from __future__ import annotations

import secrets
import time

import asyncio
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import db
from app import bgjobs
from app.auth import get_current_user, require_perm

from . import connector, services, store, sync
from .cfapi import Cf, CfError

router = APIRouter(prefix="/api/cloudflare", tags=["cloudflare"])


def _perm(user):
    require_perm(user, "manage_cloudflare")


def _err(e: Exception):
    if isinstance(e, CfError):
        raise HTTPException(502, str(e))
    raise HTTPException(400, str(e))


def _route_view(r: dict) -> dict:
    out = {k: v for k, v in r.items() if k not in ("oidc", "service_token")}
    out["proto"] = services.proto_of(r)
    out["url"] = (f"https://{r['hostname']}" + (f"/{r['ref'].get('site')}/" if r["kind"] == "website" else "")
                  if r.get("hostname") else "")
    return out


def _nodes() -> list[dict]:
    try:
        from app.node_manager import list_states
        return [{"client_id": n["client_id"], "online": n.get("online"), "hostname": n.get("hostname"),
                 "docker": bool((n.get("docker") or {}).get("available") or (n.get("docker") or {}).get("version"))}
                for n in sync._run(list_states(), 20)]
    except Exception:
        return []


@router.get("/state")
def state(user: dict = Depends(get_current_user)):
    _perm(user)
    data = store.load()
    return {**store.public_view(data), "remote": {"zone": (data.get("remote") or {}).get("zone", "")},
            "routes": [_route_view(r) for r in data["routes"]],
            "connector": connector.status(), "catalog": services.catalog(), "nodes": _nodes(),
            "modes": services.MODES, "proto_ports": services.PROTO_PORT}


class AuthBody(BaseModel):
    type: str = "token"
    token: str = ""
    key: str = ""
    email: str = ""


@router.post("/auth")
def set_auth(body: AuthBody, user: dict = Depends(get_current_user)):
    _perm(user)
    if body.type not in ("token", "key"):
        raise HTTPException(400, "type: token or key")
    secret = (body.token if body.type == "token" else body.key).strip()
    cur = store.load().get("auth") or {}
    if not secret:                          # leer = gespeichertes Geheimnis behalten
        secret = store.dec(cur.get("token_enc") if body.type == "token" else cur.get("key_enc"))
    if not secret:
        raise HTTPException(400, "Give an API token or API key")
    if body.type == "key" and "@" not in body.email:
        raise HTTPException(400, "The Global API Key needs the email of the Cloudflare account")
    auth = {"type": body.type, "email": body.email.strip(),
            ("token_enc" if body.type == "token" else "key_enc"): store.enc(secret)}
    try:
        cf = Cf(auth)
        info = cf.verify()
        accounts = cf.accounts()
    except Exception as e:      # noqa: BLE001
        _err(e)

    def upd(d):
        d["auth"] = auth
    store.update(upd)
    db.add_audit_entry(user["username"], "cloudflare.auth", details=info.get("kind", ""))
    return {"ok": True, **info, "accounts": accounts}


class AccountBody(BaseModel):
    account_id: str
    public_base: str = ""


def _load_account(acc_id: str, public_base: str | None) -> dict:
    data = store.load()
    cf = Cf.from_store(data)
    accs = {a["id"]: a for a in cf.accounts()}
    if acc_id not in accs:
        raise ValueError("Account not found")
    zones = cf.zones(acc_id)
    team = cf.team_domain(acc_id)

    def upd(d):
        if (d.get("account") or {}).get("id") not in (None, acc_id) and d["routes"]:
            raise ValueError("There are still routes in the previous account – remove them first")
        d["account"], d["zones"], d["team_domain"] = accs[acc_id], zones, team
        if public_base is not None:
            pb = public_base.strip().rstrip("/")
            if pb and not pb.startswith("https://"):
                raise ValueError("Please give the public RMM address with https://")
            d["public_base"] = pb
        return {"account": accs[acc_id], "zones": zones, "team_domain": team}
    return store.update(upd)


@router.post("/account")
def set_account(body: AccountBody, user: dict = Depends(get_current_user)):
    _perm(user)
    try:
        return _load_account(body.account_id, body.public_base)
    except Exception as e:      # noqa: BLE001
        _err(e)


class RemoteBody(BaseModel):
    zone: str = ""


@router.post("/remote")
def set_remote(body: RemoteBody, user: dict = Depends(get_current_user)):
    """Zone for the client-app remote access (addresses rmm-<proto>-<agent>.<zone>)."""
    _perm(user)

    def upd(d):
        if body.zone and body.zone not in [z["name"] for z in d.get("zones") or []]:
            raise ValueError("Zone does not belong to this account")
        d["remote"] = {**(d.get("remote") or {}), "zone": body.zone}
    try:
        store.update(upd)
    except Exception as e:      # noqa: BLE001
        _err(e)
    return {"ok": True}


class LifeBody(BaseModel):
    lifetime: str = "session"


@router.post("/routes/{rid}/lifetime")
def route_lifetime(rid: str, body: LifeBody, user: dict = Depends(get_current_user)):
    _perm(user)
    from . import remote
    try:
        return remote.set_lifetime(rid, body.lifetime)
    except Exception as e:      # noqa: BLE001
        _err(e)


@router.post("/refresh")
def refresh(user: dict = Depends(get_current_user)):
    _perm(user)
    acc = (store.load().get("account") or {}).get("id")
    if not acc:
        raise HTTPException(409, "No account selected yet")
    try:
        return _load_account(acc, None)
    except Exception as e:      # noqa: BLE001
        _err(e)


class RouteBody(BaseModel):
    id: str = ""
    name: str
    kind: str
    ref: dict = {}
    endpoint: str = "server"
    node_id: str = ""
    mode: str = ""
    hostname: str = ""
    access: str = "none"
    allow: list = []
    emails: list = []


KINDS = ("dashboard", "webdav", "sftp", "ftp", "console_ssh", "website", "docker", "agent", "custom")


@router.post("/routes")
async def save_route(body: RouteBody, background: bool = False, user: dict = Depends(get_current_user)):
    """Als BACKEND-JOB (app/bgjobs.py): Neuladen/Schliessen bricht nichts ab,
    Progress + result in the task view. ?background=1 -> job immediately."""
    _perm(user)
    async def _work(job):
        job.update(phase='cloudflare.sync', detail="Cloudflare: Route speichern + abgleichen")
        return await asyncio.to_thread(_save_route_impl, body=body, user=user)
    return await bgjobs.run('cloudflare.sync', None, title="Cloudflare: Route speichern + abgleichen", user=user, work=_work,
                            background=background, perm='manage_cloudflare')


def _save_route_impl(body: RouteBody, user: dict = Depends(get_current_user)):
    _perm(user)
    if body.kind not in KINDS:
        raise HTTPException(400, "Unbekannte Art")
    if not body.name.strip():
        raise HTTPException(400, "Name fehlt")
    if body.kind == "agent" and not db.get_client(body.ref.get("client_id", "")):
        raise HTTPException(400, "Agent unbekannt")

    def upd(d):
        old = store.route(d, body.id) if body.id else None
        r = old or {"id": secrets.token_hex(6), "created": int(time.time()), "created_by": user["username"], "state": {}}
        prev_host = (old or {}).get("hostname")
        r.update({"name": body.name.strip()[:60], "kind": body.kind, "ref": body.ref or {},
                  "endpoint": body.endpoint if body.endpoint in ("server", "node") else "server",
                  "node_id": body.node_id, "mode": body.mode, "hostname": body.hostname, "access": body.access,
                  "allow": [a for a in body.allow if isinstance(a, dict) and a.get("type") in ("user", "group")],
                  "emails": [str(e).strip() for e in body.emails if str(e).strip()]})
        sync.validate(r, d)
        if old and prev_host and prev_host != r.get("hostname"):
            r["state"]["old_dns"] = {"zone": old.get("zone_id"), "id": r["state"].pop("dns_id", None)}
        if not old:
            d["routes"].append(r)
        return r
    try:
        r = store.update(upd)
    except Exception as e:      # noqa: BLE001
        _err(e)
    # Clean up the old address (address changed)
    od = (r.get("state") or {}).get("old_dns")
    if od and od.get("id"):
        try:
            Cf.from_store().dns_delete(od["zone"], od["id"])
        except CfError:
            pass
    db.add_audit_entry(user["username"], "cloudflare.route_saved", details=f"{r['name']} {r.get('hostname') or 'WARP'}")
    try:
        rep = sync.apply(user["username"])
    except Exception as e:      # noqa: BLE001
        _err(e)
    return {"ok": True, "route": _route_view(store.route(store.load(), r["id"]) or r), "report": rep}


@router.delete("/routes/{rid}")
async def delete_route(rid: str, background: bool = False, user: dict = Depends(get_current_user)):
    """Als BACKEND-JOB (app/bgjobs.py): Neuladen/Schliessen bricht nichts ab,
    Progress + result in the task view. ?background=1 -> job immediately."""
    _perm(user)
    async def _work(job):
        job.update(phase='cloudflare.sync', detail="Cloudflare: delete route + sync")
        return await asyncio.to_thread(_delete_route_impl, rid=rid, user=user)
    return await bgjobs.run('cloudflare.sync', None, title="Cloudflare: delete route + sync", user=user, work=_work,
                            background=background, perm='manage_cloudflare')


def _delete_route_impl(rid: str, user: dict = Depends(get_current_user)):
    _perm(user)
    r = store.route(store.load(), rid)
    if not r:
        raise HTTPException(404, "Route nicht gefunden")
    sync.remove_route(rid)
    db.add_audit_entry(user["username"], "cloudflare.route_deleted", details=r["name"])
    try:
        rep = sync.apply(user["username"])
    except Exception as e:      # noqa: BLE001
        rep = {"errors": [str(e)]}
    return {"ok": True, "report": rep}


@router.post("/sync")
async def do_sync(background: bool = False, user: dict = Depends(get_current_user)):
    """Als BACKEND-JOB (app/bgjobs.py): Neuladen/Schliessen bricht nichts ab,
    Progress + result in the task view. ?background=1 -> job immediately."""
    _perm(user)
    async def _work(job):
        job.update(phase='cloudflare.sync', detail="Cloudflare: Abgleich")
        return await asyncio.to_thread(_do_sync_impl, user=user)
    return await bgjobs.run('cloudflare.sync', 'sync', title="Cloudflare: Abgleich", user=user, work=_work,
                            background=background, perm='manage_cloudflare')


def _do_sync_impl(user: dict = Depends(get_current_user)):
    _perm(user)
    try:
        return sync.apply(user["username"])
    except Exception as e:      # noqa: BLE001
        _err(e)


@router.post("/connector/{act}")
async def connector_act(act: str, background: bool = False, user: dict = Depends(get_current_user)):
    """Als BACKEND-JOB (app/bgjobs.py): Neuladen/Schliessen bricht nichts ab,
    Progress + result in the task view. ?background=1 -> job immediately."""
    _perm(user)
    async def _work(job):
        job.update(phase='cloudflare.connector', detail=f"Cloudflare-Connector: {act}")
        return await asyncio.to_thread(_connector_act_impl, act=act, user=user)
    return await bgjobs.run('cloudflare.connector', 'connector', title=f"Cloudflare-Connector: {act}", user=user, work=_work,
                            background=background, perm='manage_cloudflare')


def _connector_act_impl(act: str, user: dict = Depends(get_current_user)):
    _perm(user)
    t = store.load()["tunnels"].get("server") or {}
    if act == "stop":
        connector.stop()
    elif act in ("start", "restart"):
        if not t.get("token_enc"):
            raise HTTPException(409, "There is no tunnel yet – first sync or create a route")
        (connector.restart if act == "restart" else connector.start)(store.dec(t["token_enc"]))
    else:
        raise HTTPException(400, "start | stop | restart")
    db.add_audit_entry(user["username"], f"cloudflare.connector_{act}")
    return connector.status()


@router.delete("/nodes/{cid}")
async def delete_node(cid: str, background: bool = False, user: dict = Depends(get_current_user)):
    """Als BACKEND-JOB (app/bgjobs.py): Neuladen/Schliessen bricht nichts ab,
    Progress + result in the task view. ?background=1 -> job immediately."""
    _perm(user)
    async def _work(job):
        job.update(phase='cloudflare.sync', detail="Cloudflare: Node-Tunnel entfernen")
        return await asyncio.to_thread(_delete_node_impl, cid=cid, user=user)
    return await bgjobs.run('cloudflare.sync', f'node:{cid}', title="Cloudflare: Node-Tunnel entfernen", user=user, work=_work,
                            background=background, perm='manage_cloudflare')


def _delete_node_impl(cid: str, user: dict = Depends(get_current_user)):
    _perm(user)
    try:
        sync.remove_node_tunnel(cid)
    except Exception as e:      # noqa: BLE001
        _err(e)
    return {"ok": True}


@router.delete("/all")
async def delete_all(background: bool = False, user: dict = Depends(get_current_user)):
    """Als BACKEND-JOB (app/bgjobs.py): Neuladen/Schliessen bricht nichts ab,
    Progress + result in the task view. ?background=1 -> job immediately."""
    _perm(user)
    async def _work(job):
        job.update(phase='cloudflare.sync', detail="Cloudflare: alles entfernen")
        return await asyncio.to_thread(_delete_all_impl, user=user)
    return await bgjobs.run('cloudflare.sync', 'all', title="Cloudflare: alles entfernen", user=user, work=_work,
                            background=background, perm='manage_cloudflare')


def _delete_all_impl(user: dict = Depends(get_current_user)):
    _perm(user)
    for r in list(store.load()["routes"]):
        sync.remove_route(r["id"])
    data = store.load()
    connector.stop()
    errs = []
    try:
        cf = Cf.from_store(data)
        acc = data["account"]["id"]
        for t in [data["tunnels"].get("server") or {}] + list(data["tunnels"]["nodes"].values()):
            if t.get("id"):
                try:
                    cf.tunnel_delete(acc, t["id"])
                except CfError as e:
                    errs.append(str(e))
        if data.get("oidc_bypass_app"):
            try:
                cf.app_delete(acc, data["oidc_bypass_app"])
            except CfError:
                pass
    except (CfError, KeyError):
        pass
    for cid in list(data["tunnels"]["nodes"]):
        try:
            sync._run(__import__("app.node_manager", fromlist=["NodesAPI"]).NodesAPI("cloudflare").remove(cid))
        except Exception:
            pass
    store.save(dict(store.DEFAULT, oidc=data.get("oidc") or {}))
    db.add_audit_entry(user["username"], "cloudflare.reset")
    return {"ok": True, "errors": errs}


@router.get("/directory")
def directory(user: dict = Depends(get_current_user)):
    _perm(user)
    return {"users": [{"id": u["id"], "username": u["username"]} for u in db.list_users()],
            "groups": [{"id": g["id"], "name": g["name"], "is_ad_group": g.get("is_ad_group")} for g in db.list_groups()]}
