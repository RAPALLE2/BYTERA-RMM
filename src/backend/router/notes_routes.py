"""
routers/notes_routes.py
-----------------------
Client notes as individual entries with visibility:

  'all'     -> visible to everyone who may see the client
  'private' -> only for the author ("only for me")
  'custom'  -> author + selected users

Plus an activity log per client (who created/changed/deleted which note and
when). Permissions:
  c_notes_view -> see notes (list + log)
  c_notes_edit -> create, change, delete, pin

On the first call, any existing legacy text from clients.notes is imported once
as a note with visibility 'all' so that nothing is lost.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import db, visibility as vis
from app.auth import get_current_user, is_super_admin, require_perm

router = APIRouter(prefix="/api/clients", tags=["notes"])

ENTITY = "client_notes"
MAX_TEXT = 20000


def _conn():
    return db._conn


def _client_or_404(client_id: str) -> dict:
    c = db.get_client(client_id)
    if not c:
        raise HTTPException(404, "Client not found")
    return c


def _migrate_legacy_note(client: dict, user: dict) -> None:
    """Import legacy free text from clients.notes once as a note."""
    text = (client.get("notes") or "").strip()
    if not text:
        return
    exists = _conn().execute(
        "SELECT 1 FROM client_notes WHERE client_id = ? AND author_id IS NULL"
        " AND text = ? LIMIT 1", (client["id"], text)).fetchone()
    if exists:
        return
    now = vis.now_ms()
    _conn().execute(
        "INSERT INTO client_notes (id, client_id, author_id, author_name, text,"
        " visibility, pinned, created_at, updated_at)"
        " VALUES (?, ?, NULL, 'imported', ?, 'all', 0, ?, ?)",
        (vis.new_id(), client["id"], text, now, now))
    _conn().commit()
    vis.log(ENTITY, client["id"], None, "note.created", "legacy note imported")


def _serialize(rows: list[dict], user: dict, shares: dict) -> list[dict]:
    out = []
    for r in rows:
        entry = dict(r)
        entry["shared_with"] = shares.get(r["id"], [])
        entry["can_edit"] = vis.may_modify(user, entry)
        if vis.may_see(user, entry, entry["shared_with"]):
            out.append(entry)
        elif is_super_admin(user):
            # Admin sees THAT a private note exists (deletable) but not its
            # content.
            out.append(vis.redact(entry))
    return out


# ------------------------------------------------------------------
# Reading
# ------------------------------------------------------------------

@router.get("/{client_id}/notes")
def list_notes(client_id: str, user: dict = Depends(get_current_user)):
    client = _client_or_404(client_id)
    require_perm(user, "c_notes_view", client_id)
    _migrate_legacy_note(client, user)
    rows = [dict(r) for r in _conn().execute(
        "SELECT * FROM client_notes WHERE client_id = ?"
        " ORDER BY pinned DESC, created_at DESC", (client_id,)).fetchall()]
    shares = vis.shares_map("note_shares", "note_id", [r["id"] for r in rows])
    return _serialize(rows, user, shares)


@router.get("/{client_id}/notes/activity")
def notes_activity(client_id: str, user: dict = Depends(get_current_user)):
    _client_or_404(client_id)
    require_perm(user, "c_notes_view", client_id)
    entries = vis.get_log(ENTITY, client_id)
    for e in entries:
        e["label"] = vis.ACTION_LABELS.get(e["action"], e["action"])
    return entries


@router.get("/{client_id}/notes/users")
def notes_users(client_id: str, user: dict = Depends(get_current_user)):
    """Selection list for 'for specific': users AND groups.
    Unmanaged AD groups are flagged so the frontend can sort them into a
    separate folder."""
    _client_or_404(client_id)
    require_perm(user, "c_notes_view", client_id)
    return {
        "users": [{"id": u["id"], "username": u["username"]}
                  for u in db.list_users() if u["id"] != user["id"]],
        "groups": [{"id": g["id"], "name": g["name"],
                    "is_ad_group": bool(g.get("is_ad_group")),
                    "unmanaged": bool(g.get("unmanaged"))}
                   for g in db.list_groups()],
    }


# ------------------------------------------------------------------
# Writing
# ------------------------------------------------------------------

class NoteBody(BaseModel):
    text: str
    visibility: str = "all"          # all | private | custom
    # Entries: {"type": "user"|"group", "id": "..."} - plain IDs count as users.
    shared_with: list = []
    pinned: bool = False


def _validate(body_text: str) -> str:
    text = (body_text or "").strip()
    if not text:
        raise HTTPException(400, "Empty note")
    if len(text) > MAX_TEXT:
        raise HTTPException(400, f"Note too long (max. {MAX_TEXT} characters)")
    return text


def _vis_label(v: str) -> str:
    return {"all": "for everyone", "private": "only for me",
            "custom": "for specific users"}.get(v, v)


@router.post("/{client_id}/notes")
def create_note(client_id: str, body: NoteBody,
                      user: dict = Depends(get_current_user)):
    _client_or_404(client_id)
    require_perm(user, "c_notes_edit", client_id)
    text = _validate(body.text)
    v = vis.normalize_visibility(body.visibility)
    note_id, now = vis.new_id(), vis.now_ms()
    _conn().execute(
        "INSERT INTO client_notes (id, client_id, author_id, author_name, text,"
        " visibility, pinned, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (note_id, client_id, user["id"], user["username"], text, v,
         int(bool(body.pinned)), now, now))
    _conn().commit()
    if v == "custom":
        vis.set_shares("note_shares", "note_id", note_id, body.shared_with)
    vis.log(ENTITY, client_id, user, "note.created",
            f"{_vis_label(v)}: {text[:60]}")
    return {"id": note_id}


@router.put("/{client_id}/notes/{note_id}")
def update_note(client_id: str, note_id: str, body: NoteBody,
                      user: dict = Depends(get_current_user)):
    _client_or_404(client_id)
    require_perm(user, "c_notes_edit", client_id)
    row = _conn().execute(
        "SELECT * FROM client_notes WHERE id = ? AND client_id = ?",
        (note_id, client_id)).fetchone()
    if not row:
        raise HTTPException(404, "Note not found")
    note = dict(row)
    if not vis.may_modify(user, note):
        raise HTTPException(403, "Only the author may change this note")
    text = _validate(body.text)
    v = vis.normalize_visibility(body.visibility)
    _conn().execute(
        "UPDATE client_notes SET text = ?, visibility = ?, pinned = ?, updated_at = ?"
        " WHERE id = ?", (text, v, int(bool(body.pinned)), vis.now_ms(), note_id))
    _conn().commit()
    vis.set_shares("note_shares", "note_id", note_id,
                   body.shared_with if v == "custom" else [])
    if v != note["visibility"]:
        vis.log(ENTITY, client_id, user, "note.visibility",
                f"{_vis_label(note['visibility'])} -> {_vis_label(v)}")
    else:
        vis.log(ENTITY, client_id, user, "note.updated", text[:60])
    return {"ok": True}


@router.delete("/{client_id}/notes/{note_id}")
def delete_note(client_id: str, note_id: str,
                      user: dict = Depends(get_current_user)):
    _client_or_404(client_id)
    require_perm(user, "c_notes_edit", client_id)
    row = _conn().execute(
        "SELECT * FROM client_notes WHERE id = ? AND client_id = ?",
        (note_id, client_id)).fetchone()
    if not row:
        raise HTTPException(404, "Note not found")
    note = dict(row)
    if not vis.may_modify(user, note):
        raise HTTPException(403, "Only the author may delete this note")
    _conn().execute("DELETE FROM note_shares WHERE note_id = ?", (note_id,))
    _conn().execute("DELETE FROM client_notes WHERE id = ?", (note_id,))
    _conn().commit()
    # For others' (private) notes, do not write content into the log.
    detail = note["text"][:60] if vis.may_see(user, note) else "(private entry)"
    vis.log(ENTITY, client_id, user, "note.deleted", detail)
    return {"ok": True}
