"""
store.py (module "passwords") - storage for the password manager.

ZERO-KNOWLEDGE: the backend stores ciphertext only. It never sees the master
password, the derived key, the private key, vault keys or plaintext. All crypto
happens in the browser (frontend/crypto.js).

Tabellen (SQLite, db._conn):
  pm_accounts  per user: salt/iters, public_key (plaintext), enc_private_key
               (encrypted with the master password = the "encrypted master
               Key"), verifier.
  pm_vaults    vaults (name_enc encrypted with the vault key).
  pm_members   membership: wrapped_key = vault key, wrapped with the member's RSA
               Key des Mitglieds verpackt. So bleibt Teilen Ende-zu-Ende.
  pm_groups    folders (KeePass-like), name_enc.
  pm_entries   entries: blob = fully encrypted entry; only the
               client_id/service link is kept as plaintext metadata
               (for per-client suggestion/autofill), never credentials.
"""

from __future__ import annotations

import time
import uuid

from app import db


def _c():
    return db._conn


def _now() -> int:
    return int(time.time() * 1000)


def _id() -> str:
    return uuid.uuid4().hex[:16]


def ensure_tables() -> None:
    c = _c()
    c.execute("""CREATE TABLE IF NOT EXISTS pm_accounts (
        user_id TEXT PRIMARY KEY, salt TEXT NOT NULL, iters INTEGER NOT NULL,
        public_key TEXT NOT NULL, enc_private_key TEXT NOT NULL, verifier TEXT NOT NULL,
        created_at INTEGER NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS pm_vaults (
        id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, name_enc TEXT NOT NULL, created_at INTEGER NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS pm_members (
        vault_id TEXT NOT NULL, user_id TEXT NOT NULL, wrapped_key TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'member', added_at INTEGER NOT NULL,
        PRIMARY KEY (vault_id, user_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS pm_groups (
        id TEXT PRIMARY KEY, vault_id TEXT NOT NULL, parent_id TEXT NOT NULL DEFAULT '',
        name_enc TEXT NOT NULL, sort INTEGER NOT NULL DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS pm_entries (
        id TEXT PRIMARY KEY, vault_id TEXT NOT NULL, group_id TEXT NOT NULL DEFAULT '',
        client_id TEXT NOT NULL DEFAULT '', service TEXT NOT NULL DEFAULT '',
        blob TEXT NOT NULL, uuid TEXT NOT NULL DEFAULT '', updated_at INTEGER NOT NULL)""")
    c.execute("CREATE INDEX IF NOT EXISTS pm_members_user ON pm_members(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS pm_entries_vault ON pm_entries(vault_id)")
    c.execute("CREATE INDEX IF NOT EXISTS pm_entries_client ON pm_entries(client_id)")
    c.commit()


# ------------------------------------------------ Account
def get_account(user_id: str) -> dict | None:
    r = _c().execute("SELECT * FROM pm_accounts WHERE user_id=?", (user_id,)).fetchone()
    return dict(r) if r else None


def create_account(user_id, salt, iters, public_key, enc_private_key, verifier):
    _c().execute("INSERT INTO pm_accounts(user_id,salt,iters,public_key,enc_private_key,verifier,created_at) "
                 "VALUES(?,?,?,?,?,?,?)", (user_id, salt, int(iters), public_key, enc_private_key, verifier, _now()))
    _c().commit()


def rekey_account(user_id, salt, iters, enc_private_key, verifier):
    _c().execute("UPDATE pm_accounts SET salt=?,iters=?,enc_private_key=?,verifier=? WHERE user_id=?",
                 (salt, int(iters), enc_private_key, verifier, user_id))
    _c().commit()


def directory() -> list[dict]:
    """Users who have set up the password manager (for sharing)."""
    rows = _c().execute("SELECT user_id, public_key FROM pm_accounts").fetchall()
    keys = {str(r["user_id"]): r["public_key"] for r in rows}
    out = []
    for u in db.list_users():
        uid = str(u["id"])
        if uid in keys:
            out.append({"user_id": uid, "username": u["username"], "public_key": keys[uid]})
    return out


# ------------------------------------------------ Tresore / Mitgliedschaft
def member(vault_id, user_id) -> dict | None:
    r = _c().execute("SELECT * FROM pm_members WHERE vault_id=? AND user_id=?", (vault_id, user_id)).fetchone()
    return dict(r) if r else None


def vaults_for(user_id: str) -> list[dict]:
    rows = _c().execute("""SELECT v.id, v.owner_id, v.name_enc, v.created_at, m.wrapped_key, m.role
        FROM pm_vaults v JOIN pm_members m ON m.vault_id=v.id WHERE m.user_id=? ORDER BY v.created_at""",
        (user_id,)).fetchall()
    return [dict(r) for r in rows]


def get_vault(vault_id) -> dict | None:
    r = _c().execute("SELECT * FROM pm_vaults WHERE id=?", (vault_id,)).fetchone()
    return dict(r) if r else None


def create_vault(owner_id, name_enc, wrapped_key) -> str:
    vid = _id()
    _c().execute("INSERT INTO pm_vaults(id,owner_id,name_enc,created_at) VALUES(?,?,?,?)",
                 (vid, owner_id, name_enc, _now()))
    _c().execute("INSERT INTO pm_members(vault_id,user_id,wrapped_key,role,added_at) VALUES(?,?,?,?,?)",
                 (vid, owner_id, wrapped_key, "owner", _now()))
    _c().commit()
    return vid


def rename_vault(vault_id, name_enc):
    _c().execute("UPDATE pm_vaults SET name_enc=? WHERE id=?", (name_enc, vault_id))
    _c().commit()


def delete_vault(vault_id):
    for t in ("pm_entries", "pm_groups", "pm_members"):
        _c().execute(f"DELETE FROM {t} WHERE vault_id=?", (vault_id,))
    _c().execute("DELETE FROM pm_vaults WHERE id=?", (vault_id,))
    _c().commit()


def members(vault_id) -> list[dict]:
    rows = _c().execute("SELECT vault_id,user_id,role,added_at FROM pm_members WHERE vault_id=?", (vault_id,)).fetchall()
    names = {str(u["id"]): u["username"] for u in db.list_users()}
    out = [dict(r) for r in rows]
    for m in out:
        m["username"] = names.get(str(m["user_id"]), m["user_id"])
    return out


def add_member(vault_id, user_id, wrapped_key, role="member"):
    _c().execute("INSERT OR REPLACE INTO pm_members(vault_id,user_id,wrapped_key,role,added_at) VALUES(?,?,?,?,?)",
                 (vault_id, user_id, wrapped_key, role, _now()))
    _c().commit()


def remove_member(vault_id, user_id):
    _c().execute("DELETE FROM pm_members WHERE vault_id=? AND user_id=?", (vault_id, user_id))
    _c().commit()


# ------------------------------------------------ Folders
def groups(vault_id) -> list[dict]:
    rows = _c().execute("SELECT * FROM pm_groups WHERE vault_id=? ORDER BY sort,id", (vault_id,)).fetchall()
    return [dict(r) for r in rows]


def create_group(vault_id, parent_id, name_enc, sort=0) -> str:
    gid = _id()
    _c().execute("INSERT INTO pm_groups(id,vault_id,parent_id,name_enc,sort) VALUES(?,?,?,?,?)",
                 (gid, vault_id, parent_id or "", name_enc, int(sort)))
    _c().commit()
    return gid


def update_group(gid, name_enc=None, parent_id=None, sort=None):
    sets, vals = [], []
    if name_enc is not None:
        sets.append("name_enc=?"); vals.append(name_enc)
    if parent_id is not None:
        sets.append("parent_id=?"); vals.append(parent_id)
    if sort is not None:
        sets.append("sort=?"); vals.append(int(sort))
    if sets:
        vals.append(gid)
        _c().execute(f"UPDATE pm_groups SET {','.join(sets)} WHERE id=?", vals)
        _c().commit()


def delete_group(gid, vault_id):
    # Move subgroups and entries of this group into the vault root
    _c().execute("UPDATE pm_groups SET parent_id='' WHERE parent_id=? AND vault_id=?", (gid, vault_id))
    _c().execute("UPDATE pm_entries SET group_id='' WHERE group_id=? AND vault_id=?", (gid, vault_id))
    _c().execute("DELETE FROM pm_groups WHERE id=? AND vault_id=?", (gid, vault_id))
    _c().commit()


# ------------------------------------------------ Entries
def entries(vault_id) -> list[dict]:
    rows = _c().execute("SELECT * FROM pm_entries WHERE vault_id=? ORDER BY updated_at DESC", (vault_id,)).fetchall()
    return [dict(r) for r in rows]


def get_entry(eid) -> dict | None:
    r = _c().execute("SELECT * FROM pm_entries WHERE id=?", (eid,)).fetchone()
    return dict(r) if r else None


def create_entry(vault_id, group_id, client_id, service, blob, euuid="") -> str:
    eid = _id()
    _c().execute("INSERT INTO pm_entries(id,vault_id,group_id,client_id,service,blob,uuid,updated_at) "
                 "VALUES(?,?,?,?,?,?,?,?)", (eid, vault_id, group_id or "", client_id or "", service or "",
                                             blob, euuid or eid, _now()))
    _c().commit()
    return eid


def update_entry(eid, group_id, client_id, service, blob):
    _c().execute("UPDATE pm_entries SET group_id=?,client_id=?,service=?,blob=?,updated_at=? WHERE id=?",
                 (group_id or "", client_id or "", service or "", blob, _now(), eid))
    _c().commit()


def delete_entry(eid):
    _c().execute("DELETE FROM pm_entries WHERE id=?", (eid,))
    _c().commit()


def entries_for_client(user_id, client_id) -> list[dict]:
    """Entries linked to a client, across all of the user's vaults."""
    rows = _c().execute("""SELECT e.*, m.wrapped_key, v.name_enc FROM pm_entries e
        JOIN pm_members m ON m.vault_id=e.vault_id AND m.user_id=?
        JOIN pm_vaults v ON v.id=e.vault_id WHERE e.client_id=?""", (user_id, client_id)).fetchall()
    return [dict(r) for r in rows]
