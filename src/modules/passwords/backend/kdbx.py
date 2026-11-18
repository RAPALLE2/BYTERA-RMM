"""
kdbx.py (module "passwords") - read and write KeePass files (.kdbx).

A deliberate exception to zero-knowledge ONLY for import/export: entries are
briefly present in plaintext in the backend here (the .kdbx + its KeePass
password come from the browser). The VAULT itself stays end-to-end encrypted:
after parsing, the browser re-encrypts every entry before it is stored; on
export the browser decrypts first and only then sends plaintext to build the
file. Nothing is ever persisted in plaintext.

Uses pykeepass (installed via pip when the module is enabled).
"""

from __future__ import annotations

import io


class KdbxError(RuntimeError):
    pass


def _load(data: bytes, password: str):
    try:
        from pykeepass import PyKeePass
    except ImportError:
        raise KdbxError("Python-Paket 'pykeepass' fehlt (Modul neu aktivieren installiert es)")
    try:
        return PyKeePass(io.BytesIO(data), password=password or None)
    except Exception as exc:
        raise KdbxError(f"Could not open the KeePass file (wrong password or not a KDBX?): {exc}")


def _group_path(group) -> list[str]:
    parts = []
    g = group
    while g is not None and g.name is not None:
        if g.parentgroup is None and g.name == "Root":
            break
        parts.insert(0, g.name)
        g = g.parentgroup
    return parts


def parse(data: bytes, password: str) -> dict:
    """.kdbx -> {groups:[[pfad...]], entries:[{path, uuid, title, username, password, url, notes, fields, otp}]}"""
    kp = _load(data, password)
    groups = []
    for g in kp.groups:
        p = _group_path(g)
        if p:
            groups.append(p)
    entries = []
    for e in kp.entries:
        fields = {}
        try:
            for k in (e.custom_properties or {}):
                fields[k] = e.custom_properties[k]
        except Exception:
            pass
        entries.append({
            "path": _group_path(e.group),
            "uuid": e.uuid.hex if e.uuid else "",
            "title": e.title or "",
            "username": e.username or "",
            "password": e.password or "",
            "url": e.url or "",
            "notes": e.notes or "",
            "otp": getattr(e, "otp", "") or "",
            "fields": fields,
        })
    return {"groups": groups, "entries": entries}


def build(password: str, tree: dict) -> bytes:
    """tree = {entries:[{path:[...], title, username, password, url, notes, fields}]} -> .kdbx-Bytes (KDBX4)."""
    try:
        from pykeepass import create_database
    except ImportError:
        raise KdbxError("Python-Paket 'pykeepass' fehlt (Modul neu aktivieren installiert es)")
    if not password:
        raise KdbxError("A password must be set for the KeePass file")
    import tempfile
    import os
    tmp = tempfile.NamedTemporaryFile(suffix=".kdbx", delete=False)
    tmp.close()
    try:
        kp = create_database(tmp.name, password=password)
        group_cache = {(): kp.root_group}

        def ensure_group(path):
            path = tuple(path or [])
            if path in group_cache:
                return group_cache[path]
            parent = ensure_group(path[:-1])
            g = kp.add_group(parent, path[-1])
            group_cache[path] = g
            return g

        for e in tree.get("entries", []):
            grp = ensure_group(tuple(e.get("path") or []))
            entry = kp.add_entry(grp, e.get("title") or "(ohne Titel)", e.get("username") or "",
                                 e.get("password") or "", url=e.get("url") or "", notes=e.get("notes") or "")
            for k, v in (e.get("fields") or {}).items():
                if k and k not in ("Title", "UserName", "Password", "URL", "Notes"):
                    try:
                        entry.set_custom_property(str(k), str(v))
                    except Exception:
                        pass
        kp.save()
        with open(tmp.name, "rb") as fh:
            return fh.read()
    except KdbxError:
        raise
    except Exception as exc:
        raise KdbxError(f"Could not create the KeePass file: {exc}")
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
