"""
routers/source_routes.py
------------------------
Admin "Source" tools (super admins only):
  - File explorer over the project (backend / frontend / agent / …) incl.
    reading and saving files.
  - Direct database access: list tables, view contents, run arbitrary SQL
    (incl. the key/value "settings" with JSON arrays).

The associated live shell to the backend host runs via Socket.IO (see
sockets.py, host-term-*), since it is interactive/streaming.

CAUTION: these endpoints give an admin full access to the files and database of
the backend host. They are deliberately released ONLY for super admins.
"""

import io
import shutil
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form
from pydantic import BaseModel

from app import bgjobs, db
from app.auth import (get_current_user, require_admin, require_perm,
                      decode_access_token)

router = APIRouter(prefix="/api/source", tags=["source"])


def _user_from_token_query(request: Request, token: str | None) -> dict:
    """
    Determine the user from the token - from the address line if need be.

    Downloads run via a normal link; the browser cannot send an Authorization
    header along with it. Therefore the token is also accepted as a query
    parameter on such routes (same approach as with the media files).
    """
    raw = token
    if not raw:
        auth = request.headers.get("authorization") or ""
        if auth.startswith("Bearer "):
            raw = auth.removeprefix("Bearer ").strip()
    if not raw:
        raise HTTPException(401, "Not signed in")
    payload = decode_access_token(raw)
    user = db.get_user_by_id(payload["sub"])
    if not user:
        raise HTTPException(401, "User no longer exists")
    return user

# Project root = folder containing backend/ frontend/ agent/.
# source_routes.py is in backend/app/routers/ -> three levels up.
from app.paths import PROJECT_ROOT, DB_FILE as _DB_FILE, DB_BACKUP_FILE as _DB_BACKUP_FILE, DIST_DIR as _DIST_DIR

# Maximum file size loaded in the editor (larger ones only as a hint).
_MAX_READ_BYTES = 2 * 1024 * 1024


def _safe_path(rel: str) -> Path:
    """
    Resolves a (relative) path against the project root and ensures it does NOT
    leave the root (no '..' escape).
    """
    rel = (rel or "").lstrip("/")
    p = (PROJECT_ROOT / rel).resolve()
    if p != PROJECT_ROOT and PROJECT_ROOT not in p.parents:
        raise HTTPException(400, "A path outside the project is not allowed")
    return p


# ------------------------------------------------------------------
# File explorer
# ------------------------------------------------------------------

@router.get("/roots")
def get_roots(user: dict = Depends(get_current_user)):
    """Quick-access folders + project root."""
    require_perm(user, "see_source")
    roots = []
    for name in ["src/backend", "src/frontend", "src/agent"]:
        if (PROJECT_ROOT / name).is_dir():
            roots.append({"name": name, "path": name})
    roots.append({"name": "(project root)", "path": ""})
    return {"project_root": str(PROJECT_ROOT), "roots": roots}


@router.get("/list")
def list_dir(path: str = "", user: dict = Depends(get_current_user)):
    """Lists the contents of a folder (folders first, then files, alphabetically)."""
    require_perm(user, "see_source")
    p = _safe_path(path)
    if not p.exists():
        raise HTTPException(404, "Path not found")
    if not p.is_dir():
        raise HTTPException(400, "Not a folder")
    entries = []
    for child in p.iterdir():
        try:
            st = child.stat()
            entries.append({
                "name": child.name,
                "type": "dir" if child.is_dir() else "file",
                "size": st.st_size,
                "mtime": int(st.st_mtime * 1000),
            })
        except OSError:
            continue
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    rel = str(p.relative_to(PROJECT_ROOT)) if p != PROJECT_ROOT else ""
    parent = "" if rel in ("", ".") else str(Path(rel).parent).replace(".", "")
    return {"path": rel, "parent": parent, "entries": entries}


@router.get("/read")
def read_file(path: str, user: dict = Depends(get_current_user)):
    """Reads a text file (up to 2 MB). Binary files are marked as such."""
    require_perm(user, "see_source")
    p = _safe_path(path)
    if not p.is_file():
        raise HTTPException(404, "File not found")
    size = p.stat().st_size
    if size > _MAX_READ_BYTES:
        return {"path": path, "too_large": True, "size": size, "content": ""}
    raw = p.read_bytes()
    try:
        content = raw.decode("utf-8")
        return {"path": path, "content": content, "size": size, "binary": False}
    except UnicodeDecodeError:
        return {"path": path, "content": "", "size": size, "binary": True}


class WriteBody(BaseModel):
    path: str
    content: str


@router.put("/write")
def write_file(body: WriteBody, user: dict = Depends(get_current_user)):
    """Saves text content into a file (overwrites)."""
    require_perm(user, "edit_source")
    p = _safe_path(body.path)
    if p.is_dir():
        raise HTTPException(400, "Is a folder")
    p.write_text(body.content, encoding="utf-8")
    db.add_audit_entry(user["username"], "source.file_saved", target=body.path)
    return {"ok": True, "size": len(body.content.encode("utf-8"))}


# ------------------------------------------------------------------
# Explorer: create folder, create file, delete, rename
# ------------------------------------------------------------------

class PathBody(BaseModel):
    path: str


class RenameBody(BaseModel):
    src: str
    dst: str


@router.post("/mkdir")
def make_dir(body: PathBody, user: dict = Depends(get_current_user)):
    """Creates a (nested) folder."""
    require_perm(user, "edit_source")
    p = _safe_path(body.path)
    if p.exists():
        raise HTTPException(400, "Path already exists")
    p.mkdir(parents=True, exist_ok=False)
    db.add_audit_entry(user["username"], "source.mkdir", target=body.path)
    return {"ok": True}


@router.post("/newfile")
def new_file(body: PathBody, user: dict = Depends(get_current_user)):
    """Creates an empty file."""
    require_perm(user, "edit_source")
    p = _safe_path(body.path)
    if p.exists():
        raise HTTPException(400, "File already exists")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("", encoding="utf-8")
    db.add_audit_entry(user["username"], "source.newfile", target=body.path)
    return {"ok": True}


@router.post("/delete")
def delete_path(body: PathBody, user: dict = Depends(get_current_user)):
    """Deletes a file or a folder (recursively). The project root is off-limits."""
    require_perm(user, "delete_source")
    p = _safe_path(body.path)
    if p == PROJECT_ROOT:
        raise HTTPException(400, "The project root cannot be deleted")
    if not p.exists():
        raise HTTPException(404, "Path not found")
    if p.is_dir():
        shutil.rmtree(p)
    else:
        p.unlink()
    db.add_audit_entry(user["username"], "source.deleted", target=body.path)
    return {"ok": True}


@router.post("/rename")
def rename_path(body: RenameBody, user: dict = Depends(get_current_user)):
    """Renames a file/folder or moves it."""
    require_perm(user, "edit_source")
    src = _safe_path(body.src)
    dst = _safe_path(body.dst)
    if not src.exists():
        raise HTTPException(404, "Source not found")
    if dst.exists():
        raise HTTPException(400, "Target already exists")
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    db.add_audit_entry(user["username"], "source.renamed",
                       target=body.src, details="-> " + body.dst)
    return {"ok": True}


# ------------------------------------------------------------------
# Database
# ------------------------------------------------------------------

def _cursor_columns(cur, rows: list | None = None) -> list[str]:
    """
    Returns the column names of a query result.

    The app uses a thread-safe wrapper around SQLite (db._SafeConn) that
    returns a _CursorResult. Depending on the backend/version the object has
    either .description (like sqlite3), .keys() (like SQLAlchemy) - or nothing
    at all. Then we get the names from the first row (sqlite3.Row.keys()).
    Without this fallback the table view ended in a 500
    ("'_CursorResult' object has no attribute 'description'").
    """
    desc = getattr(cur, "description", None)
    if desc:
        return [d[0] for d in desc]
    keys = getattr(cur, "keys", None)
    if callable(keys):
        try:
            names = list(keys())
            if names:
                return [str(n) for n in names]
        except Exception:
            pass
    for r in (rows or []):
        rk = getattr(r, "keys", None)
        if callable(rk):
            try:
                return [str(n) for n in rk()]
            except Exception:
                pass
        break
    return []


def _has_result_set(cur, rows: list | None = None) -> bool:
    """True if the query returned a result set (SELECT/PRAGMA)."""
    return bool(_cursor_columns(cur, rows)) or bool(rows)


@router.get("/db/tables")
def db_tables(user: dict = Depends(get_current_user)):
    """All tables with row count."""
    require_perm(user, "see_source")
    rows = db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    out = []
    for r in rows:
        name = r["name"]
        try:
            cnt = db._conn.execute(f'SELECT COUNT(*) AS c FROM "{name}"').fetchone()["c"]
        except Exception:
            cnt = None
        out.append({"name": name, "count": cnt})
    return {"tables": out}


@router.get("/db/table")
def db_table(name: str, limit: int = 200, offset: int = 0,
                   user: dict = Depends(get_current_user)):
    """Contents of a table (columns + rows, paginated)."""
    require_perm(user, "see_source")
    # Validate the table name (only known tables).
    valid = {r["name"] for r in db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if name not in valid:
        raise HTTPException(404, "Table not found")
    limit = max(1, min(1000, limit))
    # Also deliver the rowid, so cells/rows are uniquely editable/deletable in
    # the frontend. Tables without a rowid (WITHOUT ROWID) fall back to the
    # view-only mode.
    try:
        cur = db._conn.execute(f'SELECT rowid AS __rowid__, * FROM "{name}" LIMIT ? OFFSET ?',
                               (limit, offset))
        raw = cur.fetchall()
        cols = _cursor_columns(cur, raw)[1:]
        rowids = [r[0] for r in raw]
        rows = [list(r)[1:] for r in raw]
        if not cols:
            raise ValueError("no columns determinable")
    except Exception:
        cur = db._conn.execute(f'SELECT * FROM "{name}" LIMIT ? OFFSET ?', (limit, offset))
        raw = cur.fetchall()
        cols = _cursor_columns(cur, raw)
        rows = [list(r) for r in raw]
        rowids = [None] * len(rows)
    if not cols:
        # Last resort: read the columns directly from the table schema.
        cols = [r[1] for r in db._conn.execute(f'PRAGMA table_info("{name}")').fetchall()]
    total = db._conn.execute(f'SELECT COUNT(*) AS c FROM "{name}"').fetchone()["c"]
    return {"name": name, "columns": cols, "rows": rows, "rowids": rowids,
            "total": total, "limit": limit, "offset": offset}


# ------------------------------------------------------------------
# ZIP upload + intelligent extraction
# ------------------------------------------------------------------
PROJECT_MARKERS = {"src"}


def _extract_target_and_root(names: list[str], current_path: str):
    """
    Determines (target folder, content root in the ZIP) from the ZIP content:

    - frontend/backend/agent directly in the ZIP root -> target = PROJECT_ROOT, root = "".
    - ONE wrapper folder (e.g. "BYTERA-RMM/") containing frontend/backend/agent
      -> target = PROJECT_ROOT, root = "<wrapper>/" (the wrapper is stripped).
    - otherwise -> target = currently opened explorer folder, root = "".
    """
    def top(n: str) -> str:
        return n.split("/", 1)[0]

    tops = {top(n) for n in names if n.strip() and top(n)}

    if tops & PROJECT_MARKERS:
        return PROJECT_ROOT, ""

    if len(tops) == 1:
        wrapper = next(iter(tops))
        second = set()
        for n in names:
            parts = n.split("/")
            if len(parts) >= 2 and parts[0] == wrapper and parts[1]:
                second.add(parts[1])
        if second & PROJECT_MARKERS:
            return PROJECT_ROOT, wrapper + "/"

    return _safe_path(current_path), ""


@router.post("/upload-zip")
async def upload_zip(path: str = Form(""), file: UploadFile = File(...),
                     user: dict = Depends(get_current_user)):
    """
    Uploads a ZIP and extracts it (existing files are OVERWRITTEN). Project ZIPs
    (with frontend/backend/agent - also in a wrapper folder) land in the project
    root; other ZIPs in the currently opened explorer folder.
    """
    require_perm(user, "edit_source")
    raw = await file.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise HTTPException(400, "Not a valid ZIP file")

    names = zf.namelist()
    dest, content_root = _extract_target_and_root(names, path)

    extracted, skipped = [], []
    for info in zf.infolist():
        name = info.filename
        if content_root:
            if not name.startswith(content_root):
                continue
            rel = name[len(content_root):]
        else:
            rel = name
        if not rel or rel.endswith("/"):
            continue  # folder entries are created implicitly

        target = (dest / rel).resolve()
        # Zip-slip protection: the target must lie within the project.
        if target != PROJECT_ROOT and PROJECT_ROOT not in target.parents:
            skipped.append(rel)
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as out:  # wb = overwrite
            shutil.copyfileobj(src, out)
        try:
            extracted.append(str(target.relative_to(PROJECT_ROOT)))
        except ValueError:
            extracted.append(str(target))

    code = await _activate_code_changes(extracted)
    db.add_audit_entry(user["username"], "source.zip_extracted",
                       target=("(root)" if dest == PROJECT_ROOT else str(dest.relative_to(PROJECT_ROOT))),
                       details=f"{len(extracted)} files from {file.filename}")
    return {
        "ok": True,
        "dest": "" if dest == PROJECT_ROOT else str(dest.relative_to(PROJECT_ROOT)),
        "extracted": extracted,
        "count": len(extracted),
        "skipped": skipped,
        "project_update": dest == PROJECT_ROOT,
        # Python code is only read at start: changed modules are reloaded right away,
        # changed core files need a backend restart (the dashboard offers it).
        "modules_reloaded": code["reloaded"],
        "module_errors": code["errors"],
        "restart_required": code["restart"],
        "agent_changed": code["agent"],
    }


async def _activate_code_changes(extracted: list[str]) -> dict:
    """Makes freshly extracted server code effective (hotfix ZIP)."""
    from app import modules as _modules
    from app.paths import MODULES_DIR, SRC_DIR
    out = {"reloaded": [], "errors": {}, "restart": False, "agent": False}
    mids = set()
    backend_dir = (SRC_DIR / "backend").resolve()
    mod_dir = MODULES_DIR.resolve()
    agent_dir = (SRC_DIR / "agent").resolve()
    for rel in extracted:
        p = Path(rel)
        p = (p if p.is_absolute() else PROJECT_ROOT / p).resolve()
        if mod_dir in p.parents:
            parts = p.relative_to(mod_dir).parts
            if len(parts) >= 2 and (parts[1] == "backend" or parts[1] == "module.json"):
                mids.add(parts[0])
        elif backend_dir in p.parents and p.suffix == ".py":
            out["restart"] = True
        elif agent_dir in p.parents:
            out["agent"] = True
    if out["restart"]:
        return out                      # the restart loads the modules fresh anyway
    for mid in sorted(mids):
        if mid not in getattr(_modules, "_loaded", {}):
            continue                    # not active -> new code is used when it is enabled
        try:
            await _modules.reload(mid)
            out["reloaded"].append(mid)
        except Exception as exc:        # module crashed on reload -> report, keep the rest running
            out["errors"][mid] = str(exc)
    return out


class SqlBody(BaseModel):
    sql: str


@router.post("/db/query")
def db_query(body: SqlBody, user: dict = Depends(get_current_user)):
    """
    Runs arbitrary SQL. For SELECT, columns + rows are returned, otherwise the
    affected row count. (Super admin only.)
    """
    require_perm(user, "edit_source")
    sql = (body.sql or "").strip()
    if not sql:
        raise HTTPException(400, "Empty SQL")
    try:
        cur = db._conn.execute(sql)
        raw = cur.fetchall()
        if _has_result_set(cur, raw):  # SELECT / PRAGMA with a result set
            cols = _cursor_columns(cur, raw)
            rows = [list(r) for r in raw]
            db._conn.commit()
            return {"kind": "rows", "columns": cols, "rows": rows, "count": len(rows)}
        else:
            db._conn.commit()
            db.add_audit_entry(user["username"], "source.sql_exec",
                               details=sql[:200])
            return {"kind": "exec", "rowcount": cur.rowcount}
    except Exception as e:
        raise HTTPException(400, f"SQL error: {e}")


# ------------------------------------------------------------------
# Database: edit / delete / create / backup (super admin only)
# ------------------------------------------------------------------

def _valid_table(name: str) -> str:
    valid = {r["name"] for r in db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if name not in valid:
        raise HTTPException(404, "Table not found")
    return name


def _valid_column(table: str, col: str) -> str:
    cols = {r["name"] for r in db._conn.execute(f'PRAGMA table_info("{table}")').fetchall()}
    if col not in cols:
        raise HTTPException(404, "Column not found")
    return col


class CellBody(BaseModel):
    table: str
    rowid: int
    column: str
    value: str | None = None   # None = clear the cell (NULL)


@router.put("/db/cell")
def db_update_cell(body: CellBody, user: dict = Depends(get_current_user)):
    """Sets the value of a single cell (value=None -> NULL)."""
    require_perm(user, "edit_source")
    t = _valid_table(body.table)
    c = _valid_column(t, body.column)
    cur = db._conn.execute(f'UPDATE "{t}" SET "{c}" = ? WHERE rowid = ?',
                           (body.value, body.rowid))
    db._conn.commit()
    db.add_audit_entry(user["username"], "source.db_cell_updated",
                       target=f"{t}.{c}", details=f"rowid={body.rowid}")
    return {"ok": True, "rowcount": cur.rowcount}


class RowBody(BaseModel):
    table: str
    rowid: int


@router.post("/db/delete-row")
def db_delete_row(body: RowBody, user: dict = Depends(get_current_user)):
    """Deletes a row by its rowid."""
    require_perm(user, "delete_source")
    t = _valid_table(body.table)
    cur = db._conn.execute(f'DELETE FROM "{t}" WHERE rowid = ?', (body.rowid,))
    db._conn.commit()
    db.add_audit_entry(user["username"], "source.db_row_deleted",
                       target=t, details=f"rowid={body.rowid}")
    return {"ok": True, "rowcount": cur.rowcount}


class InsertRowBody(BaseModel):
    table: str
    values: dict = {}   # {column: value} - missing columns = default/NULL


@router.post("/db/insert-row")
def db_insert_row(body: InsertRowBody, user: dict = Depends(get_current_user)):
    """Inserts a new row (only given columns, rest = default)."""
    require_perm(user, "edit_source")
    t = _valid_table(body.table)
    if body.values:
        cols = [_valid_column(t, c) for c in body.values.keys()]
        placeholders = ", ".join("?" for _ in cols)
        collist = ", ".join(f'"{c}"' for c in cols)
        db._conn.execute(f'INSERT INTO "{t}" ({collist}) VALUES ({placeholders})',
                         tuple(body.values[c] for c in cols))
    else:
        db._conn.execute(f'INSERT INTO "{t}" DEFAULT VALUES')
    db._conn.commit()
    db.add_audit_entry(user["username"], "source.db_row_inserted", target=t)
    return {"ok": True}


class TableBody(BaseModel):
    table: str


@router.post("/db/drop-table")
def db_drop_table(body: TableBody, user: dict = Depends(get_current_user)):
    """Deletes an entire table. (The double confirmation is done by the frontend.)"""
    require_perm(user, "delete_source")
    t = _valid_table(body.table)
    db._conn.execute(f'DROP TABLE "{t}"')
    db._conn.commit()
    db.add_audit_entry(user["username"], "source.db_table_dropped", target=t)
    return {"ok": True}


class CreateTableBody(BaseModel):
    name: str
    columns: str   # e.g. "id INTEGER PRIMARY KEY, name TEXT"


@router.post("/db/create-table")
def db_create_table(body: CreateTableBody, user: dict = Depends(get_current_user)):
    """Creates a new table with the given column definition."""
    require_perm(user, "edit_source")
    name = (body.name or "").strip()
    if not name or not name.replace("_", "").isalnum():
        raise HTTPException(400, "Invalid table name (only letters/numbers/_)")
    cols = (body.columns or "").strip()
    if not cols:
        raise HTTPException(400, "Column definition missing")
    try:
        db._conn.execute(f'CREATE TABLE "{name}" ({cols})')
        db._conn.commit()
    except Exception as e:
        raise HTTPException(400, f"SQL error: {e}")
    db.add_audit_entry(user["username"], "source.db_table_created", target=name)
    return {"ok": True}


@router.post("/db/backup")
async def db_backup(background: bool = False, user: dict = Depends(get_current_user)):
    """Creates a consistent copy of the database as data/backup/data.sqlite
    (as a BACKEND JOB, see app/bgjobs.py)."""
    require_perm(user, "see_source")
    import asyncio

    async def work(job):
        job.update(phase="backup", detail="Database is being backed up")
        return await asyncio.to_thread(_db_backup_sync, user)
    return await bgjobs.run("db.backup", "db", title="Database backup", user=user, work=work,
                            background=background, perm="see_source")


def _db_backup_sync(user: dict) -> dict:
    import sqlite3
    src_path = _DB_FILE
    bak_path = _DB_BACKUP_FILE
    bak_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        dest = sqlite3.connect(str(bak_path))
        with dest:
            db._conn.backup(dest)   # consistent online backup
        dest.close()
    except Exception as e:
        raise HTTPException(500, f"Backup failed: {e}")
    size = bak_path.stat().st_size
    db.add_audit_entry(user["username"], "source.db_backup",
                       target=str(bak_path.name), details=f"{size} bytes")
    return {"ok": True, "path": "src/backend/data/backup/data.sqlite", "size": size}

# ------------------------------------------------------------------
# Installation kind (Docker container or native program)
# ------------------------------------------------------------------

@router.get("/runtime")
def get_runtime(user: dict = Depends(get_current_user)):
    """
    Tells the UI HOW this backend is installed:

      install_kind = "docker"  -> runs as a container. Port/host are tied to
                                  the container and locked at runtime.
      install_kind = "native"  -> installed normally via "python run.py",
                                  everything freely changeable via
                                  src/backend/data/.env.

    Shown as a hint bar in the Source tab.
    """
    require_perm(user, "see_source")
    from app import runtime_env
    return runtime_env.runtime_info()


# ------------------------------------------------------------------
# Clear the dist folder (built agent installation packages)
# ------------------------------------------------------------------

@router.post("/dist/clear")
def clear_dist(user: dict = Depends(get_current_user)):
    """
    Deletes all files in the dist/ folder - that is where the installation
    packages built with app/build_installers.py are (.exe/.msi/.bat/.ps1/.sh/
    .tar.gz/.deb/.rpm/.pkg.tar.xz).

    Useful because every build leaves old versions behind: other versions,
    other backend addresses. The folder itself stays, subfolders are removed
    with it.
    """
    require_perm(user, "edit_source")
    dist = _DIST_DIR
    if not dist.is_dir():
        return {"ok": True, "removed": 0, "freed": 0, "note": "dist folder does not exist"}

    removed, freed, errors = 0, 0, []
    for entry in list(dist.iterdir()):
        try:
            if entry.is_dir() and not entry.is_symlink():
                freed += sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
                shutil.rmtree(entry)
            else:
                freed += entry.stat().st_size
                entry.unlink()
            removed += 1
        except Exception as e:
            errors.append(f"{entry.name}: {e}")

    db.add_audit_entry(user["username"], "source.dist_clear",
                       details=f"{removed} entries, {freed // 1024} KB")
    return {"ok": not errors, "removed": removed, "freed": freed, "errors": errors}


# ==================================================================
# MIGRATION - move the entire instance to another server
# ------------------------------------------------------------------
# Takes the ENTIRE database (clients, users, rights, settings, dashboard
# widgets, tickets, scripts, audit log …) plus recordings, media library and
# branding. See app/migrate.py for the technique.
#
#   GET  /api/source/migrate/info    -> preview: what is in it, how large
#   GET  /api/source/migrate/export  -> download the archive
#   POST /api/source/migrate/import  -> apply the archive (backend restarts)
#
# Administrators only: the archive contains all data of the installation, and
# applying it overwrites the entire content of the target system.
# ==================================================================

@router.get("/migrate/info")
def migrate_info(user: dict = Depends(get_current_user)):
    require_admin(user)
    from app import migrate
    return migrate.preview()


@router.get("/migrate/export")
def migrate_export(request: Request,
                         recordings: bool = True,
                         media: bool = True,
                         branding: bool = True,
                         secrets: bool = False,
                         modules: bool = True,
                         storage: bool = True,
                         token: str | None = None):
    """
    Builds the archive and delivers it.

    The token may also come as a query parameter: the download runs via a
    normal link/`window.open`, which cannot set an Authorization header.
    """
    user = _user_from_token_query(request, token)
    require_admin(user)

    import tempfile
    import time as _time
    from fastapi.responses import FileResponse
    from starlette.background import BackgroundTask
    from app import migrate

    stamp = _time.strftime("%Y%m%d-%H%M%S")
    tmp_dir = Path(tempfile.mkdtemp(prefix="rmm-migrate-"))
    archive = tmp_dir / f"bytera-rmm-migration-{stamp}.zip"
    try:
        manifest = migrate.build_export(
            archive, include_recordings=recordings, include_media=media,
            include_branding=branding, include_secrets=secrets,
            include_modules=modules, include_storage=storage)
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(500, f"Export failed: {e}")

    db.add_audit_entry(user["username"], "source.migrate_export",
                       details=f"{manifest.get('archive_size', 0) // 1024} KB, "
                               f"recordings={recordings}, media={media}, "
                               f"secrets={secrets}")
    # Clean up only AFTER sending - otherwise one pulls the file out from under
    # the running download.
    return FileResponse(
        archive, filename=archive.name, media_type="application/zip",
        background=BackgroundTask(shutil.rmtree, tmp_dir, ignore_errors=True))


@router.post("/migrate/import")
async def migrate_import(request: Request, secrets: bool = False,
                         backup: bool = True, background: bool = False,
                         user: dict = Depends(get_current_user)):
    """
    Applies a migration archive (raw body, as with the ZIP upload).

    The existing state is set aside beforehand
    (backend/backup-vor-migration-<timestamp>/), so a mistake is not final.
    Afterwards the backend restarts - the old database file has, after all,
    been replaced.
    """
    require_admin(user)

    import tempfile
    from app import migrate

    # The upload itself necessarily comes from the browser; the APPLYING then
    # runs as a BACKEND JOB (app/bgjobs.py) and is not aborted.
    data = await request.body()
    if not data:
        raise HTTPException(400, "Empty archive")

    tmp_dir = Path(tempfile.mkdtemp(prefix="rmm-migrate-in-"))
    archive = tmp_dir / "migration.zip"
    archive.write_bytes(data)
    del data
    import asyncio

    async def work(job):
        job.update(phase="import", detail="Archive is being applied (previous state is backed up)")
        try:
            report = await asyncio.to_thread(migrate.apply_import, archive,
                                             restore_secrets=secrets, keep_backup=backup)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(500, f"Applying failed: {e}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        try:
            db.add_audit_entry(user["username"], "source.migrate_import",
                               details=f"source: {report['manifest'].get('created_iso')}, "
                                       f"version {report['manifest'].get('version')}")
        except Exception:
            pass   # The database was just swapped out - no big deal.
        job.update(phase="restart", detail="Applied - backend restarts")
        _schedule_restart_after_migration(delay=4.0)
        report["restart"] = True
        return report

    return await bgjobs.run("migrate.import", "import", title="Apply migration archive", user=user,
                            work=work, background=background, perm="admin")


def _schedule_restart_after_migration(delay: float = 2.0) -> None:
    """
    Restart the backend after applying.

    Needed because the open SQLite connection still points to the replaced
    file. The restart happens delayed, so the response still reaches the
    browser.
    """
    import asyncio
    import os
    import sys

    def _restart():
        print("[migrate] Restarting backend after the migration…")
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os.execv(sys.executable, [sys.executable] + sys.argv)

    try:
        asyncio.get_event_loop().call_later(delay, _restart)
    except Exception:
        _restart()


# ==========================================================================
# Server move: send agents to the NEW backend
# ==========================================================================
# Flow: 1. Export on the old server, apply on the new one (with secrets ->
# same AGENT_TOKEN). 2. On the OLD server, enter the new one's address here and
# "move all agents".
# Technique (without an agent update beforehand): every agent gets
# "update-agent". It fetches its update script from the old server; during the
# move the script writes the NEW address (and optionally the new AGENT_TOKEN)
# into its .env and restarts the agent -> it reports to the new server, with
# the same device ID. Offline agents follow automatically as soon as they
# report (to the old server).
#
#   GET    /api/source/migrate/redirect          state (target, agents online/total)
#   POST   /api/source/migrate/redirect/test     {url} check the new server
#   POST   /api/source/migrate/redirect          {url, agent_token?} start the move
#   DELETE /api/source/migrate/redirect          end the move (agents stay here)

class RedirectBody(BaseModel):
    url: str
    agent_token: str = ""


def _norm_target(url: str) -> str:
    from router.enrollment_routes import _ensure_scheme
    from urllib.parse import urlsplit
    u = _ensure_scheme((url or "").strip()).rstrip("/")
    p = urlsplit(u)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise HTTPException(400, "Please enter the full address of the new server, e.g. https://rmm-new.company.com")
    return u


def _probe(url: str) -> dict:
    """Is this a reachable BYTERA RMM? (seen from the OLD server)"""
    import ssl
    import urllib.request
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE           # only check reachability (self-signed allowed)
    try:
        with urllib.request.urlopen(urllib.request.Request(url + "/agent-dist/update.sh"), timeout=10, context=ctx) as r:
            body = r.read(4000).decode("utf-8", "replace")
        ok = "BYTERA RMM" in body or "RAPALLE.net RMM" in body   # also servers before the rename
        return {"ok": ok, "detail": "New server reachable." if ok else
                "Reachable, but this is apparently not a BYTERA RMM (agent update script missing)."}
    except Exception as e:      # noqa: BLE001
        return {"ok": False, "detail": f"Not reachable: {getattr(e, 'reason', e)}"}


@router.get("/migrate/redirect")
def redirect_state(user: dict = Depends(get_current_user)):
    require_admin(user)
    from app.sockets import state
    clients = db.list_clients()
    online = sum(1 for c in clients if state.is_online(c["id"]))
    return {"target": db.get_setting("migration_target_url") or "",
            "has_token": bool((db.get_setting("migration_agent_token") or "").strip()),
            "active": bool((db.get_setting("migration_target_url") or "").strip()),
            "agents_total": len(clients), "agents_online": online}


@router.post("/migrate/redirect/test")
def redirect_test(body: RedirectBody, user: dict = Depends(get_current_user)):
    require_admin(user)
    return _probe(_norm_target(body.url))


@router.post("/migrate/redirect")
async def redirect_start(body: RedirectBody, background: bool = False, user: dict = Depends(get_current_user)):
    require_admin(user)
    import asyncio
    url = _norm_target(body.url)
    tok = body.agent_token.strip()
    if tok and not all(c.isalnum() or c in "-_.~" for c in tok):
        raise HTTPException(400, "The agent token contains invalid characters")
    probe = await asyncio.get_running_loop().run_in_executor(None, _probe, url)
    if not probe["ok"]:
        raise HTTPException(409, f"Move not started - {probe['detail']}")
    db.set_setting("migration_target_url", url)
    db.set_setting("migration_agent_token", tok)
    db.add_audit_entry(user["username"], "migration.redirect_started", details=f"-> {url}"
                       + (" (with a new agent token)" if tok else ""))

    from app.sockets import _migration_last, send_to_agent, state
    online = [c for c in db.list_clients() if state.is_online(c["id"])]

    async def rollout(job):
        n = len(online)
        for i, c in enumerate(online):
            if job.cancelled:
                job.log("Cancelled - remaining agents are no longer moved")
                break
            _migration_last[c["id"]] = __import__("time").time()
            try:
                await send_to_agent(c["id"], "update-agent", {})
                job.log(f"{c.get('hostname') or c['id']}: move triggered")
            except Exception as e:
                job.log(f"{c.get('hostname') or c['id']}: not reached ({e})")
            job.update(phase="rollout", pct=(i + 1) / max(1, n) * 100, detail=f"{i + 1}/{n} agents triggered")
            if i % 10 == 9:
                await asyncio.sleep(2)          # staggered, so not all download at once
        return {"target": url, "triggered": n}
    out = {"ok": True, "target": url, "triggered": len(online),
           "offline": len(db.list_clients()) - len(online)}
    job, _ = bgjobs.start("migrate.redirect", "redirect", title=f"Move agents to the new server ({len(online)})",
                          user=user, runner=rollout, perm="admin", meta={"target": url})
    out["job_id"] = job.id
    if background:
        return {**job.public(), **out, "job": True}
    return out


@router.delete("/migrate/redirect")
def redirect_stop(user: dict = Depends(get_current_user)):
    require_admin(user)
    db.set_setting("migration_target_url", "")
    db.set_setting("migration_agent_token", "")
    db.add_audit_entry(user["username"], "migration.redirect_stopped")
    return {"ok": True}
