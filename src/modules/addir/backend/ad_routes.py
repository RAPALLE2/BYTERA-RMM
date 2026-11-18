# ad_routes.py (module "addir") - management (permission manage_ad)
# -----------------------------------------------------------
# Connections:
#   GET/POST            /api/ad/connections
#   PUT/DELETE          /api/ad/connections/{id}
#   POST                /api/ad/connections/{id}/test
#   GET                 /api/ad/connections/for-sso      (without password, for SSO import)
# Reading (per connection):
#   GET  /api/ad/{id}/tree                                OUs/containers
#   GET  /api/ad/{id}/search?kind=user|group|ou|computer&q=&base=
#   GET  /api/ad/{id}/object?dn=                          one object (all attributes)
#   GET  /api/ad/{id}/group-members?dn=
# Users:
#   POST /api/ad/{id}/users                                create
#   POST /api/ad/{id}/users/bulk                           CSV/bulk import
#   PATCH /api/ad/{id}/object                              modify attributes {dn, attrs}
#   POST /api/ad/{id}/move                                 {dn, new_parent?, new_cn?}
#   POST /api/ad/{id}/user/enable | /unlock | /expiry | /password
#   DELETE /api/ad/{id}/object?dn=
# Groups:
#   POST /api/ad/{id}/groups                               create
#   POST /api/ad/{id}/group/member                         {group_dn, member_dn, add}
# OUs:
#   POST /api/ad/{id}/ous                                  create
from __future__ import annotations

import time

import asyncio
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app import db
from app import bgjobs
from app.auth import get_current_user, require_perm

from . import ldap_client, store
from .ldap_client import AdError

router = APIRouter(prefix="/api/ad", tags=["active-directory"])


def _perm(user):
    require_perm(user, "manage_ad")


def _err(e: Exception):
    raise HTTPException(400, str(e))


def _audit(user, action, details=""):
    try:
        db.add_audit_entry(user["username"], f"ad.{action}", details=details)
    except Exception:
        pass


# ====================================================== Verbindungen
class ConnBody(BaseModel):
    name: str = ""
    server: str = ""
    port: int | None = None
    use_ssl: bool = False
    start_tls: bool = False
    verify_cert: bool = False
    base_dn: str = ""
    bind_user: str = ""
    bind_password: str = ""
    pw_requires_tls: bool = True


@router.get("/connections")
def list_connections(user: dict = Depends(get_current_user)):
    _perm(user)
    return {"connections": [store.public(c) for c in store.list_conns()]}


@router.get("/connections/for-sso")
def connections_for_sso(user: dict = Depends(get_current_user)):
    """For the SSO module: connected ADs to choose from (without password)."""
    require_perm(user, "manage_sso")
    return {"connections": [{"id": c["id"], "name": c["name"], "server": c.get("server"),
                             "base_dn": c.get("base_dn"), "bind_user": c.get("bind_user"),
                             "port": c.get("port"), "use_ssl": bool(c.get("use_ssl"))}
                            for c in store.list_conns()]}


def _apply(body: ConnBody, c: dict, keep_pw: bool) -> dict:
    c.update(name=body.name.strip()[:80], server=body.server.strip(), port=body.port,
             use_ssl=bool(body.use_ssl), start_tls=bool(body.start_tls), verify_cert=bool(body.verify_cert),
             base_dn=body.base_dn.strip(), bind_user=body.bind_user.strip(),
             pw_requires_tls=bool(body.pw_requires_tls))
    if body.bind_password:
        c["bind_pw_enc"] = store.enc(body.bind_password)
    elif not keep_pw:
        c["bind_pw_enc"] = ""
    return c


@router.post("/connections")
async def create_connection(body: ConnBody, background: bool = False, user: dict = Depends(get_current_user)):
    """Als BACKEND-JOB (app/bgjobs.py): Neuladen/Schliessen bricht nichts ab,
    Fortschritt + Ergebnis in der Aufgaben-Anzeige. ?background=1 -> Job sofort."""
    _perm(user)
    async def _work(job):
        job.update(phase='ad.task', detail="Active Directory: create + test connection")
        return await asyncio.to_thread(_create_connection_impl, body=body, user=user)
    return await bgjobs.run('ad.task', None, title="Active Directory: create + test connection", user=user, work=_work,
                            background=background, perm='manage_ad')


def _create_connection_impl(body: ConnBody, user: dict = Depends(get_current_user)):
    _perm(user)
    if not body.name.strip() or not body.server.strip() or not body.base_dn.strip():
        raise HTTPException(400, "Name, server and base DN are required")

    def upd(d):
        cid = store.new_id()
        c = {"id": cid, "created": int(time.time()), "created_by": user["username"]}
        _apply(body, c, keep_pw=False)
        d["conns"][cid] = c
        return c
    c = store.update(upd)
    _audit(user, "conn_created", c["name"])
    return {"ok": True, "connection": store.public(c)}


@router.put("/connections/{cid}")
async def update_connection(cid: str, body: ConnBody, background: bool = False, user: dict = Depends(get_current_user)):
    """Als BACKEND-JOB (app/bgjobs.py): Neuladen/Schliessen bricht nichts ab,
    Fortschritt + Ergebnis in der Aufgaben-Anzeige. ?background=1 -> Job sofort."""
    _perm(user)
    async def _work(job):
        job.update(phase='ad.task', detail="Active Directory: save + test connection")
        return await asyncio.to_thread(_update_connection_impl, cid=cid, body=body, user=user)
    return await bgjobs.run('ad.task', None, title="Active Directory: save + test connection", user=user, work=_work,
                            background=background, perm='manage_ad')


def _update_connection_impl(cid: str, body: ConnBody, user: dict = Depends(get_current_user)):
    _perm(user)

    def upd(d):
        c = d["conns"].get(cid)
        if not c:
            return None
        _apply(body, c, keep_pw=True)
        return c
    c = store.update(upd)
    if not c:
        raise HTTPException(404, "Verbindung nicht gefunden")
    return {"ok": True, "connection": store.public(c)}


@router.delete("/connections/{cid}")
def delete_connection(cid: str, user: dict = Depends(get_current_user)):
    _perm(user)
    store.update(lambda d: d["conns"].pop(cid, None))
    _audit(user, "conn_deleted", cid)
    return {"ok": True}


@router.post("/connections/{cid}/test")
async def test_connection(cid: str, background: bool = False, user: dict = Depends(get_current_user)):
    """Als BACKEND-JOB (app/bgjobs.py): Neuladen/Schliessen bricht nichts ab,
    Fortschritt + Ergebnis in der Aufgaben-Anzeige. ?background=1 -> Job sofort."""
    _perm(user)
    async def _work(job):
        job.update(phase='ad.task', detail="Active Directory: Verbindung testen")
        return await asyncio.to_thread(_test_connection_impl, cid=cid, user=user)
    return await bgjobs.run('ad.task', cid, title="Active Directory: Verbindung testen", user=user, work=_work,
                            background=background, perm='manage_ad')


def _test_connection_impl(cid: str, user: dict = Depends(get_current_user)):
    _perm(user)
    c = store.get(cid)
    if not c:
        raise HTTPException(404, "Verbindung nicht gefunden")
    return ldap_client.test(c)


class TestNewBody(ConnBody):
    pass


@router.post("/connections-test")
async def test_new(body: TestNewBody, background: bool = False, user: dict = Depends(get_current_user)):
    """Als BACKEND-JOB (app/bgjobs.py): Neuladen/Schliessen bricht nichts ab,
    Fortschritt + Ergebnis in der Aufgaben-Anzeige. ?background=1 -> Job sofort."""
    _perm(user)
    async def _work(job):
        job.update(phase='ad.task', detail="Active Directory: neue Verbindung testen")
        return await asyncio.to_thread(_test_new_impl, body=body, user=user)
    return await bgjobs.run('ad.task', None, title="Active Directory: neue Verbindung testen", user=user, work=_work,
                            background=background, perm='manage_ad')


def _test_new_impl(body: TestNewBody, user: dict = Depends(get_current_user)):
    """Test a connection before it is saved."""
    _perm(user)
    c = {}
    _apply(body, c, keep_pw=False)
    return ldap_client.test(c)


# ====================================================== Lesen
def _ad(cid: str):
    try:
        return ldap_client.open_conn(cid)
    except AdError as e:
        raise HTTPException(404, str(e))


@router.get("/{cid}/dashboard")
def dashboard(cid: str, user: dict = Depends(get_current_user)):
    _perm(user)
    try:
        with _ad(cid) as ad:
            return ad.dashboard()
    except AdError as e:
        _err(e)


@router.get("/{cid}/tree")
def tree(cid: str, user: dict = Depends(get_current_user)):
    _perm(user)
    try:
        with _ad(cid) as ad:
            return {"tree": ad.tree()}
    except AdError as e:
        _err(e)


@router.get("/{cid}/search")
def search(cid: str, kind: str = "user", q: str = "", base: str = "",
           user: dict = Depends(get_current_user)):
    _perm(user)
    if kind not in ("user", "group", "ou", "computer"):
        raise HTTPException(400, "kind: user, group, ou oder computer")
    try:
        with _ad(cid) as ad:
            return {"results": ad.search(kind, q, base or None)}
    except AdError as e:
        _err(e)


@router.get("/{cid}/object")
def get_object(cid: str, dn: str, user: dict = Depends(get_current_user)):
    _perm(user)
    try:
        with _ad(cid) as ad:
            o = ad.get_by_dn(dn)
            if not o:
                raise HTTPException(404, "Objekt nicht gefunden")
            return {"object": o}
    except AdError as e:
        _err(e)


@router.get("/{cid}/group-members")
def group_members(cid: str, dn: str, user: dict = Depends(get_current_user)):
    _perm(user)
    try:
        with _ad(cid) as ad:
            return {"members": ad.members(dn)}
    except AdError as e:
        _err(e)


# ====================================================== Users
class UserBody(BaseModel):
    ou_dn: str
    sam: str
    password: str = ""
    enabled: bool = True
    fields: dict = {}


@router.post("/{cid}/users")
async def create_user(cid: str, body: UserBody, background: bool = False, user: dict = Depends(get_current_user)):
    """Als BACKEND-JOB (app/bgjobs.py): Neuladen/Schliessen bricht nichts ab,
    Fortschritt + Ergebnis in der Aufgaben-Anzeige. ?background=1 -> Job sofort."""
    _perm(user)
    async def _work(job):
        job.update(phase='ad.task', detail=f"Active Directory: create user \"{body.sam}\"")
        return await asyncio.to_thread(_create_user_impl, cid=cid, body=body, user=user)
    return await bgjobs.run('ad.task', None, title=f"Active Directory: create user \"{body.sam}\"", user=user, work=_work,
                            background=background, perm='manage_ad')


def _create_user_impl(cid: str, body: UserBody, user: dict = Depends(get_current_user)):
    _perm(user)
    if not body.sam.strip() or not body.ou_dn.strip():
        raise HTTPException(400, "User name (sAMAccountName) and target OU are required")
    try:
        with _ad(cid) as ad:
            dn = ad.create_user(body.ou_dn, body.sam, body.fields or {}, body.password, body.enabled)
        _audit(user, "user_created", body.sam)
        return {"ok": True, "dn": dn}
    except AdError as e:
        _err(e)


class BulkBody(BaseModel):
    ou_dn: str
    csv: str                    # Kopfzeile: sam,displayName,givenName,sn,mail,password[,ou]
    default_enabled: bool = True


@router.post("/{cid}/users/bulk")
async def bulk_users(cid: str, body: BulkBody, background: bool = False, user: dict = Depends(get_current_user)):
    """Als BACKEND-JOB (app/bgjobs.py): Neuladen/Schliessen bricht nichts ab,
    Fortschritt + Ergebnis in der Aufgaben-Anzeige. ?background=1 -> Job sofort."""
    _perm(user)
    async def _work(job):
        job.update(phase='ad.task', detail="Active Directory: Benutzer per CSV anlegen")
        return await asyncio.to_thread(_bulk_users_impl, cid=cid, body=body, user=user)
    return await bgjobs.run('ad.task', None, title="Active Directory: Benutzer per CSV anlegen", user=user, work=_work,
                            background=background, perm='manage_ad')


def _bulk_users_impl(cid: str, body: BulkBody, user: dict = Depends(get_current_user)):
    _perm(user)
    import csv
    import io
    rows = list(csv.DictReader(io.StringIO(body.csv.strip()), skipinitialspace=True))
    if not rows:
        raise HTTPException(400, "No rows (a header is required: sam,displayName,givenName,sn,mail,password)")
    created, errors = [], []
    try:
        with _ad(cid) as ad:
            for i, row in enumerate(rows, 2):
                row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
                sam = row.get("sam") or row.get("sAMAccountName") or row.get("username")
                if not sam:
                    errors.append(f"Zeile {i}: Spalte 'sam' fehlt")
                    continue
                ou = row.get("ou") or body.ou_dn
                flds = {k: row[k] for k in ("displayName", "givenName", "sn", "mail", "telephoneNumber",
                                            "department", "title", "description") if row.get(k)}
                if not flds.get("displayName"):
                    flds["displayName"] = (flds.get("givenName", "") + " " + flds.get("sn", "")).strip() or sam
                try:
                    ad.create_user(ou, sam, flds, row.get("password", ""), body.default_enabled)
                    created.append(sam)
                except AdError as e:
                    errors.append(f"Zeile {i} ({sam}): {e}")
    except AdError as e:
        _err(e)
    _audit(user, "users_bulk", f"{len(created)} angelegt, {len(errors)} Fehler")
    return {"ok": True, "created": created, "errors": errors}


class AttrsBody(BaseModel):
    dn: str
    attrs: dict


@router.patch("/{cid}/object")
def modify_object(cid: str, body: AttrsBody, user: dict = Depends(get_current_user)):
    _perm(user)
    try:
        with _ad(cid) as ad:
            ad.modify_attrs(body.dn, body.attrs)
        _audit(user, "object_modified", body.dn)
        return {"ok": True}
    except AdError as e:
        _err(e)


class MoveBody(BaseModel):
    dn: str
    new_parent: str = ""
    new_cn: str = ""


@router.post("/{cid}/move")
def move_object(cid: str, body: MoveBody, user: dict = Depends(get_current_user)):
    _perm(user)
    try:
        with _ad(cid) as ad:
            new_dn = ad.rename_move(body.dn, body.new_parent or None, body.new_cn or None)
        _audit(user, "object_moved", f"{body.dn} -> {new_dn}")
        return {"ok": True, "dn": new_dn}
    except AdError as e:
        _err(e)


class DnBody(BaseModel):
    dn: str


class EnableBody(DnBody):
    enabled: bool = True


@router.post("/{cid}/user/enable")
def user_enable(cid: str, body: EnableBody, user: dict = Depends(get_current_user)):
    _perm(user)
    try:
        with _ad(cid) as ad:
            ad.set_enabled(body.dn, body.enabled)
        _audit(user, "user_enabled" if body.enabled else "user_disabled", body.dn)
        return {"ok": True}
    except AdError as e:
        _err(e)


@router.post("/{cid}/user/unlock")
def user_unlock(cid: str, body: DnBody, user: dict = Depends(get_current_user)):
    _perm(user)
    try:
        with _ad(cid) as ad:
            ad.unlock(body.dn)
        _audit(user, "user_unlocked", body.dn)
        return {"ok": True}
    except AdError as e:
        _err(e)


class ExpiryBody(DnBody):
    when: str = "never"       # "never" oder "YYYY-MM-DD"


@router.post("/{cid}/user/expiry")
def user_expiry(cid: str, body: ExpiryBody, user: dict = Depends(get_current_user)):
    _perm(user)
    try:
        with _ad(cid) as ad:
            ad.set_expiry(body.dn, body.when)
        _audit(user, "user_expiry", f"{body.dn}: {body.when}")
        return {"ok": True}
    except AdError as e:
        _err(e)


class PwBody(DnBody):
    password: str
    must_change: bool = False


@router.post("/{cid}/user/password")
def user_password(cid: str, body: PwBody, user: dict = Depends(get_current_user)):
    _perm(user)
    if len(body.password or "") < 1:
        raise HTTPException(400, "Passwort fehlt")
    try:
        with _ad(cid) as ad:
            ad.set_password(body.dn, body.password, body.must_change)
        _audit(user, "user_password", body.dn)      # the password itself is NOT logged
        return {"ok": True}
    except AdError as e:
        _err(e)


# ====================================================== Gruppen
class GroupBody(BaseModel):
    ou_dn: str
    name: str
    scope: str = "global"       # global|domainlocal|universal
    security: bool = True
    description: str = ""


@router.post("/{cid}/groups")
def create_group(cid: str, body: GroupBody, user: dict = Depends(get_current_user)):
    _perm(user)
    if not body.name.strip() or not body.ou_dn.strip():
        raise HTTPException(400, "Group name and target OU are required")
    try:
        with _ad(cid) as ad:
            dn = ad.create_group(body.ou_dn, body.name.strip(), body.scope, body.security, body.description)
        _audit(user, "group_created", body.name)
        return {"ok": True, "dn": dn}
    except AdError as e:
        _err(e)


class MemberBody(BaseModel):
    group_dn: str
    member_dn: str
    add: bool = True


@router.post("/{cid}/group/member")
def group_member(cid: str, body: MemberBody, user: dict = Depends(get_current_user)):
    _perm(user)
    try:
        with _ad(cid) as ad:
            if body.add:
                ad.add_member(body.group_dn, body.member_dn)
            else:
                ad.remove_member(body.group_dn, body.member_dn)
        _audit(user, "group_member_add" if body.add else "group_member_remove",
               f"{body.member_dn} @ {body.group_dn}")
        return {"ok": True}
    except AdError as e:
        _err(e)


# ====================================================== OU
class OuBody(BaseModel):
    parent_dn: str
    name: str
    description: str = ""


@router.post("/{cid}/ous")
def create_ou(cid: str, body: OuBody, user: dict = Depends(get_current_user)):
    _perm(user)
    if not body.name.strip() or not body.parent_dn.strip():
        raise HTTPException(400, "OU name and parent object are required")
    try:
        with _ad(cid) as ad:
            dn = ad.create_ou(body.parent_dn, body.name.strip(), body.description)
        _audit(user, "ou_created", body.name)
        return {"ok": True, "dn": dn}
    except AdError as e:
        _err(e)


@router.delete("/{cid}/object")
def delete_object(cid: str, dn: str, user: dict = Depends(get_current_user)):
    _perm(user)
    try:
        with _ad(cid) as ad:
            ad.delete(dn)
        _audit(user, "object_deleted", dn)
        return {"ok": True}
    except AdError as e:
        _err(e)
