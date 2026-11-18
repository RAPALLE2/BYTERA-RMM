# pm_routes.py (Modul "passwords") - Passwort-Manager (Recht use_passwords)
# -------------------------------------------------------------------------
# ZERO-KNOWLEDGE: Das Backend speichert nur Chiffrate (siehe store.py). Alle
# Ver-/Entschlüsselung passiert im Browser. Ausnahme NUR beim KeePass-Im-/Export
# (kdbx.py), wo Einträge kurzzeitig im Klartext verarbeitet werden.
#
#   GET/POST  /api/pm/account            Konto-Blobs holen/anlegen
#   POST      /api/pm/account/rekey      Master-Passwort geändert -> neue Blobs
#   GET       /api/pm/directory          Benutzer + Public-Keys (zum Teilen)
#   GET/POST  /api/pm/vaults             Tresore / anlegen
#   PUT/DELETE/members …                 Tresor verwalten + teilen
#   GET       /api/pm/vaults/{id}/data   Ordner + Einträge (verschlüsselt)
#   …/groups …/entries                   Ordner & Einträge CRUD
#   GET       /api/pm/lookup?client_id=  Einträge zu einem Client (Autofill)
#   POST      /api/pm/kdbx/parse|build   KeePass import/export (serverseitig)
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from app import db
from app.auth import get_current_user, require_perm

from . import kdbx, store

router = APIRouter(prefix="/api/pm", tags=["passwords"])


def _perm(user):
    require_perm(user, "use_passwords")


def _uid(user) -> str:
    return str(user["id"])


def _require_member(vault_id, user, roles=None) -> dict:
    m = store.member(vault_id, _uid(user))
    if not m:
        raise HTTPException(404, "Tresor nicht gefunden")
    if roles and m["role"] not in roles:
        raise HTTPException(403, "Keine Berechtigung für diese Aktion")
    return m


# ------------------------------------------------ Konto
@router.get("/account")
def get_account(user: dict = Depends(get_current_user)):
    _perm(user)
    a = store.get_account(_uid(user))
    if not a:
        return {"exists": False}
    return {"exists": True, "salt": a["salt"], "iters": a["iters"], "public_key": a["public_key"],
            "enc_private_key": a["enc_private_key"], "verifier": a["verifier"]}


class AccountBody(BaseModel):
    salt: str
    iters: int
    public_key: str
    enc_private_key: str
    verifier: str


@router.post("/account")
def create_account(body: AccountBody, user: dict = Depends(get_current_user)):
    _perm(user)
    if store.get_account(_uid(user)):
        raise HTTPException(409, "Konto besteht bereits")
    store.create_account(_uid(user), body.salt, body.iters, body.public_key, body.enc_private_key, body.verifier)
    db.add_audit_entry(user["username"], "pm.account_created")
    return {"ok": True}


class RekeyBody(BaseModel):
    salt: str
    iters: int
    enc_private_key: str
    verifier: str


@router.post("/account/rekey")
def rekey(body: RekeyBody, user: dict = Depends(get_current_user)):
    _perm(user)
    if not store.get_account(_uid(user)):
        raise HTTPException(404, "Kein Konto")
    store.rekey_account(_uid(user), body.salt, body.iters, body.enc_private_key, body.verifier)
    db.add_audit_entry(user["username"], "pm.master_changed")
    return {"ok": True}


@router.get("/directory")
def directory(user: dict = Depends(get_current_user)):
    _perm(user)
    return {"users": [u for u in store.directory() if u["user_id"] != _uid(user)]}


# ------------------------------------------------ Tresore
@router.get("/vaults")
def list_vaults(user: dict = Depends(get_current_user)):
    _perm(user)
    return {"vaults": [{"id": v["id"], "owner_id": v["owner_id"], "name_enc": v["name_enc"],
                        "role": v["role"], "wrapped_key": v["wrapped_key"],
                        "shared": v["owner_id"] != _uid(user)} for v in store.vaults_for(_uid(user))]}


class VaultBody(BaseModel):
    name_enc: str
    wrapped_key: str


@router.post("/vaults")
def create_vault(body: VaultBody, user: dict = Depends(get_current_user)):
    _perm(user)
    if not store.get_account(_uid(user)):
        raise HTTPException(400, "Bitte zuerst den Passwort-Manager einrichten")
    vid = store.create_vault(_uid(user), body.name_enc, body.wrapped_key)
    return {"id": vid}


class RenameBody(BaseModel):
    name_enc: str


@router.put("/vaults/{vault_id}")
def rename_vault(vault_id: str, body: RenameBody, user: dict = Depends(get_current_user)):
    _perm(user)
    _require_member(vault_id, user, roles=("owner", "admin"))
    store.rename_vault(vault_id, body.name_enc)
    return {"ok": True}


@router.delete("/vaults/{vault_id}")
def delete_vault(vault_id: str, user: dict = Depends(get_current_user)):
    _perm(user)
    _require_member(vault_id, user, roles=("owner",))
    store.delete_vault(vault_id)
    db.add_audit_entry(user["username"], "pm.vault_deleted", target=vault_id)
    return {"ok": True}


@router.get("/vaults/{vault_id}/members")
def list_members(vault_id: str, user: dict = Depends(get_current_user)):
    _perm(user)
    _require_member(vault_id, user)
    return {"members": store.members(vault_id)}


class MemberBody(BaseModel):
    user_id: str
    wrapped_key: str
    role: str = "member"


@router.post("/vaults/{vault_id}/members")
def add_member(vault_id: str, body: MemberBody, user: dict = Depends(get_current_user)):
    _perm(user)
    _require_member(vault_id, user, roles=("owner", "admin"))
    if not store.get_account(body.user_id):
        raise HTTPException(400, "Zielbenutzer hat den Passwort-Manager nicht eingerichtet")
    role = body.role if body.role in ("member", "admin") else "member"
    store.add_member(vault_id, body.user_id, body.wrapped_key, role)
    db.add_audit_entry(user["username"], "pm.vault_shared", target=vault_id, details=f"-> {body.user_id}")
    return {"ok": True}


@router.delete("/vaults/{vault_id}/members/{member_id}")
def remove_member(vault_id: str, member_id: str, user: dict = Depends(get_current_user)):
    _perm(user)
    v = store.get_vault(vault_id)
    if not v:
        raise HTTPException(404, "Tresor nicht gefunden")
    # Besitzer/Admin dürfen andere entfernen; jeder darf sich selbst entfernen
    if member_id != _uid(user):
        _require_member(vault_id, user, roles=("owner", "admin"))
    if member_id == v["owner_id"]:
        raise HTTPException(400, "Der Besitzer kann nicht entfernt werden (Tresor löschen stattdessen)")
    store.remove_member(vault_id, member_id)
    return {"ok": True}


# ------------------------------------------------ Inhalt (Ordner + Einträge)
@router.get("/vaults/{vault_id}/data")
def vault_data(vault_id: str, user: dict = Depends(get_current_user)):
    _perm(user)
    _require_member(vault_id, user)
    return {"groups": [{"id": g["id"], "parent_id": g["parent_id"], "name_enc": g["name_enc"], "sort": g["sort"]}
                       for g in store.groups(vault_id)],
            "entries": [{"id": e["id"], "group_id": e["group_id"], "client_id": e["client_id"],
                         "service": e["service"], "blob": e["blob"], "uuid": e["uuid"], "updated_at": e["updated_at"]}
                        for e in store.entries(vault_id)]}


class GroupBody(BaseModel):
    parent_id: str = ""
    name_enc: str
    sort: int = 0


@router.post("/vaults/{vault_id}/groups")
def create_group(vault_id: str, body: GroupBody, user: dict = Depends(get_current_user)):
    _perm(user)
    _require_member(vault_id, user, roles=("owner", "admin", "member"))
    return {"id": store.create_group(vault_id, body.parent_id, body.name_enc, body.sort)}


class GroupUpdate(BaseModel):
    name_enc: str | None = None
    parent_id: str | None = None
    sort: int | None = None


@router.put("/vaults/{vault_id}/groups/{gid}")
def update_group(vault_id: str, gid: str, body: GroupUpdate, user: dict = Depends(get_current_user)):
    _perm(user)
    _require_member(vault_id, user)
    store.update_group(gid, body.name_enc, body.parent_id, body.sort)
    return {"ok": True}


@router.delete("/vaults/{vault_id}/groups/{gid}")
def delete_group(vault_id: str, gid: str, user: dict = Depends(get_current_user)):
    _perm(user)
    _require_member(vault_id, user)
    store.delete_group(gid, vault_id)
    return {"ok": True}


class EntryBody(BaseModel):
    group_id: str = ""
    client_id: str = ""
    service: str = ""
    blob: str
    uuid: str = ""


@router.post("/vaults/{vault_id}/entries")
def create_entry(vault_id: str, body: EntryBody, user: dict = Depends(get_current_user)):
    _perm(user)
    _require_member(vault_id, user)
    return {"id": store.create_entry(vault_id, body.group_id, body.client_id, body.service, body.blob, body.uuid)}


@router.put("/vaults/{vault_id}/entries/{eid}")
def update_entry(vault_id: str, eid: str, body: EntryBody, user: dict = Depends(get_current_user)):
    _perm(user)
    _require_member(vault_id, user)
    e = store.get_entry(eid)
    if not e or e["vault_id"] != vault_id:
        raise HTTPException(404, "Eintrag nicht gefunden")
    store.update_entry(eid, body.group_id, body.client_id, body.service, body.blob)
    return {"ok": True}


@router.delete("/vaults/{vault_id}/entries/{eid}")
def delete_entry(vault_id: str, eid: str, user: dict = Depends(get_current_user)):
    _perm(user)
    _require_member(vault_id, user)
    e = store.get_entry(eid)
    if not e or e["vault_id"] != vault_id:
        raise HTTPException(404, "Eintrag nicht gefunden")
    store.delete_entry(eid)
    return {"ok": True}


@router.get("/lookup")
def lookup(client_id: str, user: dict = Depends(get_current_user)):
    _perm(user)
    rows = store.entries_for_client(_uid(user), client_id)
    return {"entries": [{"id": e["id"], "vault_id": e["vault_id"], "wrapped_key": e["wrapped_key"],
                         "service": e["service"], "blob": e["blob"]} for e in rows]}


# ------------------------------------------------ KeePass (serverseitig, nur Im-/Export)
@router.post("/kdbx/parse")
async def kdbx_parse(file: UploadFile = File(...), password: str = Form(""), user: dict = Depends(get_current_user)):
    _perm(user)
    data = await file.read()
    if len(data) > 60 * 1024 * 1024:
        raise HTTPException(413, "Datei zu groß")
    try:
        res = kdbx.parse(data, password)
    except kdbx.KdbxError as exc:
        raise HTTPException(400, str(exc))
    db.add_audit_entry(user["username"], "pm.kdbx_import", details=f"{len(res['entries'])} Einträge")
    return res


class BuildBody(BaseModel):
    password: str
    tree: dict


@router.post("/kdbx/build")
def kdbx_build(body: BuildBody, user: dict = Depends(get_current_user)):
    _perm(user)
    try:
        data = kdbx.build(body.password, body.tree or {})
    except kdbx.KdbxError as exc:
        raise HTTPException(400, str(exc))
    db.add_audit_entry(user["username"], "pm.kdbx_export", details=f"{len(body.tree.get('entries', []))} Einträge")
    return Response(data, media_type="application/octet-stream",
                    headers={"Content-Disposition": 'attachment; filename="bytera-export.kdbx"'})
