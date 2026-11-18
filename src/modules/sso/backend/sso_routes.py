# sso_routes.py (module "sso")
# ---------------------------
# Manage directory connections (realms, LDAP/Active Directory) - formerly in
# router/admin_routes.py. URLs unchanged. Permission: "manage_sso".
#
#   GET    /api/admin/realms                         list (without bind password)
#   POST   /api/admin/realms                         create
#   PUT    /api/admin/realms/{id}                    modify
#   POST   /api/admin/realms/{id}/test               test the connection
#   DELETE /api/admin/realms/{id}                    delete
#   GET    /api/admin/realms/{id}/ad-groups          list AD groups
#   POST   /api/admin/realms/{id}/ad-groups/import   import AD groups as RMM groups
#   GET    /api/admin/sso/local-admins               local admin accounts (condition for disabling)
#   GET    /api/admin/sso/ad-connections             ADs connected in the "Active Directory" module (for selection)
#   POST   /api/admin/sso/ad-connections/{cid}/import   eine davon als Realm uebernehmen

import asyncio
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import db
from app import bgjobs
from app.auth import get_current_user, require_perm

from . import ldap_auth, guard

router = APIRouter(prefix="/api/admin", tags=["sso"])


class RealmBody(BaseModel):
    name: str
    server: str
    base_dn: str | None = None
    bind_user: str | None = None
    bind_password: str | None = None
    port: int | None = None
    use_ssl: bool = False
    user_filter: str | None = None
    enabled: bool = True


@router.get("/realms")
def list_realms(user: dict = Depends(get_current_user)):
    require_perm(user, "manage_sso")
    realms = db.list_realms()
    # never return the bind password to the frontend
    for r in realms:
        r.pop("bind_password", None)
    return realms


@router.post("/realms")
def create_realm(body: RealmBody, user: dict = Depends(get_current_user)):
    require_perm(user, "manage_sso")
    r = db.create_realm(
        body.name, body.server, body.base_dn, body.bind_user, body.bind_password,
        port=body.port, use_ssl=body.use_ssl, user_filter=body.user_filter,
    )
    r.pop("bind_password", None)
    db.add_audit_entry(user["username"], "realm.created", target=r["id"], details=body.name)
    return r


@router.put("/realms/{realm_id}")
def update_realm(realm_id: str, body: RealmBody, user: dict = Depends(get_current_user)):
    require_perm(user, "manage_sso")
    if not db.get_realm(realm_id):
        raise HTTPException(404, "Realm nicht gefunden")
    # exclude_unset: only change the fields actually sent, so a simple
    # active/inactive toggle does not accidentally overwrite SSL/port/DN.
    r = db.update_realm(realm_id, body.model_dump(exclude_unset=True))
    if r:
        r.pop("bind_password", None)
    db.add_audit_entry(user["username"], "realm.updated", target=realm_id, details=body.name)
    return r


@router.post("/realms/{realm_id}/test")
async def test_realm(realm_id: str, background: bool = False, user: dict = Depends(get_current_user)):
    """Als BACKEND-JOB (app/bgjobs.py): Neuladen/Schliessen bricht nichts ab,
    Fortschritt + Ergebnis in der Aufgaben-Anzeige. ?background=1 -> Job sofort."""
    require_perm(user, "manage_sso")
    async def _work(job):
        job.update(phase='sso.task', detail="SSO: Realm testen")
        return await asyncio.to_thread(_test_realm_impl, realm_id=realm_id, user=user)
    return await bgjobs.run('sso.task', realm_id, title="SSO: Realm testen", user=user, work=_work,
                            background=background, perm='manage_sso')


def _test_realm_impl(realm_id: str, user: dict = Depends(get_current_user)):
    """
    Check the realm configuration: connects with the bind account and searches
    the base DN. Reports success or a meaningful error message.
    """
    require_perm(user, "manage_sso")
    realm = db.get_realm(realm_id)
    if not realm:
        raise HTTPException(404, "Realm nicht gefunden")
    result = ldap_auth.test_realm_connection(realm)
    db.add_audit_entry(user["username"], "realm.tested", target=realm_id)
    return result


@router.delete("/realms/{realm_id}")
def delete_realm(realm_id: str, user: dict = Depends(get_current_user)):
    require_perm(user, "manage_sso")
    db.delete_realm(realm_id)
    db.add_audit_entry(user["username"], "realm.deleted", target=realm_id)
    return {"ok": True}


@router.get("/realms/{realm_id}/ad-groups")
def get_realm_ad_groups(realm_id: str, user: dict = Depends(get_current_user)):
    """
    Listet die Gruppen des AD/LDAP-Verzeichnisses auf und markiert, welche davon
    bereits als RMM-Gruppe importiert sind (damit man ihnen Rechte geben kann).
    """
    require_perm(user, "manage_sso")
    realm = db.get_realm(realm_id)
    if not realm:
        raise HTTPException(404, "Realm nicht gefunden")
    names = ldap_auth.list_realm_groups(realm)
    existing = {g["name"] for g in db.list_groups()}
    return {
        "groups": [{"name": n, "imported": n in existing} for n in names],
        "count": len(names),
    }


class ImportAdGroupsBody(BaseModel):
    names: list[str] = []          # leer = alle gefundenen importieren


@router.post("/realms/{realm_id}/ad-groups/import")
async def import_realm_ad_groups(realm_id: str, body: ImportAdGroupsBody,
                                 background: bool = False, user: dict = Depends(get_current_user)):
    """Als BACKEND-JOB (app/bgjobs.py): Neuladen/Schliessen bricht nichts ab,
    Fortschritt + Ergebnis in der Aufgaben-Anzeige. ?background=1 -> Job sofort."""
    require_perm(user, "manage_sso")
    async def _work(job):
        job.update(phase='sso.task', detail="SSO: AD-Gruppen importieren")
        return await asyncio.to_thread(_import_realm_ad_groups_impl, realm_id=realm_id, body=body, user=user)
    return await bgjobs.run('sso.task', realm_id, title="SSO: AD-Gruppen importieren", user=user, work=_work,
                            background=background, perm='manage_sso')


def _import_realm_ad_groups_impl(realm_id: str, body: ImportAdGroupsBody,
                                 user: dict = Depends(get_current_user)):
    """
    Import AD groups as RMM groups (is_ad_group=1). Afterwards, permissions can
    be assigned to these groups in the permissions app. Members of the AD group
    inherit these permissions automatically on login (matched by name).
    """
    require_perm(user, "manage_sso")
    realm = db.get_realm(realm_id)
    if not realm:
        raise HTTPException(404, "Realm nicht gefunden")

    names = body.names
    if not names:
        names = ldap_auth.list_realm_groups(realm)   # nichts angegeben -> alle importieren

    imported = []
    for name in names:
        g = db.upsert_ad_group(name)
        imported.append(g["name"])
    db.add_audit_entry(user["username"], "ad_groups.imported", target=realm_id,
                       details=f"{len(imported)} Gruppen")
    return {"ok": True, "imported": imported, "count": len(imported)}


@router.get("/sso/ad-connections")
def ad_connections(user: dict = Depends(get_current_user)):
    """Im Modul "Active Directory" verbundene ADs - direkt als Anmeldequelle waehlbar."""
    require_perm(user, "manage_sso")
    try:
        from app import modules
        if not modules.is_enabled("addir"):
            return {"available": False, "connections": []}
        conns = modules.use("addir", "store").list_conns()
    except Exception:
        return {"available": False, "connections": []}
    taken = {(r.get("server"), r.get("base_dn")) for r in db.list_realms_full()}
    return {"available": True, "connections": [
        {"id": c["id"], "name": c["name"], "server": c.get("server"), "base_dn": c.get("base_dn"),
         "bind_user": c.get("bind_user"), "port": c.get("port"), "use_ssl": bool(c.get("use_ssl")),
         "already": (c.get("server"), c.get("base_dn")) in taken}
        for c in conns]}


@router.post("/sso/ad-connections/{cid}/import")
async def import_ad_connection(cid: str, background: bool = False, user: dict = Depends(get_current_user)):
    """Als BACKEND-JOB (app/bgjobs.py): Neuladen/Schliessen bricht nichts ab,
    Fortschritt + Ergebnis in der Aufgaben-Anzeige. ?background=1 -> Job sofort."""
    require_perm(user, "manage_sso")
    async def _work(job):
        job.update(phase='sso.task', detail="SSO: adopt AD connection as a realm")
        return await asyncio.to_thread(_import_ad_connection_impl, cid=cid, user=user)
    return await bgjobs.run('sso.task', cid, title="SSO: adopt AD connection as a realm", user=user, work=_work,
                            background=background, perm='manage_sso')


def _import_ad_connection_impl(cid: str, user: dict = Depends(get_current_user)):
    """Eine AD-Verbindung des Moduls "Active Directory" als SSO-Realm anlegen
    (Bind-Benutzer/Passwort werden uebernommen)."""
    require_perm(user, "manage_sso")
    from app import modules
    if not modules.is_enabled("addir"):
        raise HTTPException(409, "The \"Active Directory\" module is not active")
    adstore = modules.use("addir", "store")
    c = adstore.get(cid)
    if not c:
        raise HTTPException(404, "AD-Verbindung nicht gefunden")
    r = db.create_realm(c["name"], c.get("server") or "", c.get("base_dn") or "",
                        c.get("bind_user") or "", adstore.dec(c.get("bind_pw_enc", "")),
                        port=c.get("port"), use_ssl=bool(c.get("use_ssl")))
    r.pop("bind_password", None)
    db.add_audit_entry(user["username"], "realm.imported_from_ad", target=r["id"], details=c["name"])
    return {"ok": True, "realm": r}


@router.get("/sso/local-admins")
def local_admins(user: dict = Depends(get_current_user)):
    """Lokale, aktive Admin-Konten. Mindestens eins ist Pflicht, um SSO abzuschalten."""
    require_perm(user, "manage_sso")
    admins = guard.local_admins()
    return {"count": len(admins),
            "admins": [{"username": a["username"], "display_name": a.get("display_name") or ""} for a in admins]}
