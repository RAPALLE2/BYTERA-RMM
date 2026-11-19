"""
migrate.py
----------
Move a complete RMM instance to another server.

What is taken along:
  * the entire database (data.sqlite) - i.e. clients, users, groups and rights,
    settings, dashboard widgets, tickets, notes, scripts, schedules, audit log
    … simply everything the RMM stores
  * the recordings (recordings/) - the replays of the remote sessions
  * the media library (media_files/) of the audio player
  * the branding (branding/) - custom logos and images
  * the MODULES: which are active + their settings (data/modules.json) and
    their data (data/modules/: spaces directory, client devices, Proxmox
    connections, notes, shares ...)
  * the STORAGE (storage/): spaces files, deployment folder, built client
    packages
  * secrets ONLY on explicit request: .env files (AGENT_TOKEN, JWT_SECRET) and
    the connection of the SQL module (dbconfig.json). Without them agents have
    to re-register, and module data encrypted with JWT_SECRET (e.g. Proxmox
    credentials, API codes) is not readable on the target. With them everything
    stays reachable unchanged.

Why a dedicated file and not simply "copy folders":
The database is written during operation. A simple cp can catch it mid
transaction and yields a corrupted file. Therefore we use SQLite's built-in
backup interface (`Connection.backup`), which pulls a self-consistent state -
without halting operation.

The export is a ZIP with a manifest.json on top. On applying, it is checked
first, so nobody accidentally dumps an arbitrary archive over their
installation.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
import zipfile
from pathlib import Path

from app.paths import (
    BACKEND_DIR, PROJECT_ROOT, DB_FILE, DATA_DIR, ENV_FILE, AGENT_DIR,
    RECORDINGS_DIR, AUDIO_DIR, BRANDING_DIR, BACKEND_VERSION_FILE,
    MODULES_STATE_FILE, MODULES_DATA_DIR, STORAGE_DIR, DBCONFIG_FILE,
)
MANIFEST_NAME = "manifest.json"
ARCHIVE_MARK = "bytera-rmm-migration"
LEGACY_ARCHIVE_MARKS = (ARCHIVE_MARK, "rapalle-rmm-migration")   # archives from the server before the rename

# Folders that can move along. Key = name in the archive.
DATA_DIRS = {
    "recordings": RECORDINGS_DIR,
    "media_files": AUDIO_DIR,
    "branding": BRANDING_DIR,
    "module_data": MODULES_DATA_DIR,    # data of the modules (option "modules")
    "storage": STORAGE_DIR,             # all files (option "storage") - without recordings/audio,
}                                       # those have their own options (see _skip)


def _skip(name: str, f: Path) -> bool:
    """Recordings and audio now live IN storage/, but have their own options -
    therefore skip them for the "storage" folder (otherwise duplicated)."""
    if name != "storage":
        return False
    for sub in (RECORDINGS_DIR, AUDIO_DIR):
        try:
            f.relative_to(sub)
            return True
        except ValueError:
            pass
    return False
# Individual files (option "modules"): enabled modules + module settings
DATA_FILES = {
    "modules_state.json": MODULES_STATE_FILE,
}
# Which option toggles which folder/which file?
OPTION_OF = {"recordings": "recordings", "media_files": "media_files", "branding": "branding",
             "module_data": "modules", "storage": "storage", "modules_state.json": "modules"}

# Secrets - only on request, see module description.
SECRET_FILES = {
    "backend.env": ENV_FILE,
    "agent.env": AGENT_DIR / ".env",
    "dbconfig.json": DBCONFIG_FILE,      # connection of the SQL module (contains credentials)
}


def _modules_state() -> dict:
    try:
        d = json.loads(MODULES_STATE_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _sql_external() -> bool:
    """Does the SQL module use an EXTERNAL database? Then the data is there."""
    try:
        return json.loads(DBCONFIG_FILE.read_text(encoding="utf-8")).get("mode") == "external"
    except Exception:
        return False


# ==========================================================================
# Helper functions
# ==========================================================================

def _dir_size(path: Path) -> tuple[int, int]:
    """(number of files, total size) of a folder - for the preview."""
    count = size = 0
    if not path.is_dir():
        return 0, 0
    for f in path.rglob("*"):
        if f.is_file():
            count += 1
            try:
                size += f.stat().st_size
            except OSError:
                pass
    return count, size


def _version() -> str:
    try:
        return (BACKEND_VERSION_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def _table_counts() -> dict:
    """Row counts of the most important tables - check before and after the move."""
    out: dict[str, int] = {}
    try:
        con = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)
        try:
            names = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'").fetchall()]
            for n in names:
                try:
                    out[n] = con.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0]
                except sqlite3.Error:
                    pass
        finally:
            con.close()
    except sqlite3.Error:
        pass
    return out


def preview() -> dict:
    """What would an export contain? Shown in the dashboard."""
    counts = _table_counts()
    dirs = {}
    total = DB_FILE.stat().st_size if DB_FILE.is_file() else 0
    for name, path in DATA_DIRS.items():
        n, size = _dir_size(path)
        dirs[name] = {"files": n, "size": size}
        total += size
    ms = _modules_state()
    enabled = sorted(k for k, v in (ms.get("enabled") or {}).items() if v)
    return {
        "modules": {"enabled": enabled, "settings": len(ms.get("settings") or {}),
                    "state_file": MODULES_STATE_FILE.is_file()},
        "sql_external": _sql_external(),
        "version": _version(),
        "database": {
            "size": DB_FILE.stat().st_size if DB_FILE.is_file() else 0,
            "tables": len(counts),
            "rows": sum(counts.values()),
            "counts": {k: v for k, v in sorted(
                counts.items(), key=lambda kv: -kv[1])[:12]},
        },
        "dirs": dirs,
        "total_size": total,
        "has_backend_env": SECRET_FILES["backend.env"].is_file(),
    }


def _copy_database(dest: Path) -> None:
    """
    Pull a consistent copy of the database - during operation.

    SQLite's `Connection.backup` copies page by page and takes running
    transactions into account. A simple file copy would be risky here.
    """
    src = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


# ==========================================================================
# Export
# ==========================================================================

def build_export(target: Path, include_recordings: bool = True,
                 include_media: bool = True, include_branding: bool = True,
                 include_secrets: bool = False, include_modules: bool = True,
                 include_storage: bool = True) -> dict:
    """
    Writes the migration archive to `target` and returns the manifest.
    """
    wanted = {
        "recordings": include_recordings,
        "media_files": include_media,
        "branding": include_branding,
        "modules": include_modules,
        "storage": include_storage,
    }

    manifest = {
        "kind": ARCHIVE_MARK,
        "format": 2,                       # 2 = with modules + storage
        "created_at": int(time.time()),
        "created_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
        "version": _version(),
        "hostname": os.getenv("HOSTNAME", ""),
        "includes": {k: bool(v) for k, v in wanted.items()},
        "includes_secrets": bool(include_secrets),
        "counts": _table_counts(),
        "modules_enabled": sorted(k for k, v in (_modules_state().get("enabled") or {}).items() if v)
                           if include_modules else [],
    }

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_db = target.parent / f"data-export-{int(time.time())}.sqlite"
    _copy_database(tmp_db)

    try:
        # ZIP_DEFLATED: database and logs shrink considerably; videos in the
        # recordings are already compressed, which costs hardly any time there.
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED,
                             allowZip64=True) as zf:
            zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2))
            zf.write(tmp_db, "data.sqlite")
            for name, path in DATA_DIRS.items():
                if not wanted.get(OPTION_OF[name]) or not path.is_dir():
                    continue
                for f in sorted(path.rglob("*")):
                    if f.is_file() and not f.name.endswith((".tmp", ".part")) and not _skip(name, f):
                        zf.write(f, f"{name}/{f.relative_to(path)}")
            for arc, path in DATA_FILES.items():
                if wanted.get(OPTION_OF[arc]) and path.is_file():
                    zf.write(path, f"files/{arc}")
            if include_secrets:
                for arc, path in SECRET_FILES.items():
                    if path.is_file():
                        zf.write(path, f"secrets/{arc}")
    finally:
        try:
            tmp_db.unlink()
        except OSError:
            pass

    manifest["archive_size"] = target.stat().st_size
    return manifest


# ==========================================================================
# Import
# ==========================================================================

def read_manifest(archive: Path) -> dict:
    """Read and check the manifest - BEFORE any change to the target system."""
    if not zipfile.is_zipfile(archive):
        raise ValueError("This is not a ZIP archive.")
    with zipfile.ZipFile(archive) as zf:
        if MANIFEST_NAME not in zf.namelist():
            raise ValueError("The archive is missing manifest.json - this is not "
                             "a migration archive of this application.")
        data = json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
        if data.get("kind") not in LEGACY_ARCHIVE_MARKS:
            raise ValueError("The archive does not belong to BYTERA RMM.")
        if "data.sqlite" not in zf.namelist():
            raise ValueError("The archive is missing the database.")
    return data


def apply_import(archive: Path, restore_secrets: bool = False,
                 keep_backup: bool = True) -> dict:
    """
    Applies a migration archive.

    Order chosen deliberately:
      1. Check the manifest (aborts BEFORE anything is touched)
      2. Unpack everything to a side folder - if that fails, the old state is
         still untouched
      3. Set the existing state aside (backup with a timestamp)
      4. Move the new state into place

    The backend must restart afterwards: otherwise the database connection
    still points to the old, now replaced file.
    """
    manifest = read_manifest(archive)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    staging = BACKEND_DIR / f".migrate-{stamp}"
    backup_dir = BACKEND_DIR / f"backup-vor-migration-{stamp}"
    report = {"manifest": manifest, "backup": None, "restored": []}

    # --- 2. Unpack --------------------------------------------------------
    staging.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                # No breaking out of the target folder (zip-slip).
                dest = (staging / info.filename).resolve()
                if staging.resolve() not in dest.parents and dest != staging.resolve():
                    raise ValueError(f"Unsafe path in the archive: {info.filename}")
            zf.extractall(staging)

        new_db = staging / "data.sqlite"
        if not new_db.is_file():
            raise ValueError("Database not found in the archive.")
        # Short function test before the old state is touched.
        con = sqlite3.connect(f"file:{new_db}?mode=ro", uri=True)
        try:
            con.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        finally:
            con.close()

        # --- 3. Backup of the current state -------------------------------
        if keep_backup:
            backup_dir.mkdir(parents=True, exist_ok=True)
            if DB_FILE.is_file():
                shutil.copy2(DB_FILE, backup_dir / "data.sqlite")
            for arc, path in DATA_FILES.items():
                if path.is_file():
                    shutil.copy2(path, backup_dir / arc)
            for name, path in DATA_DIRS.items():
                if path.is_dir() and any(path.iterdir()):
                    shutil.copytree(path, backup_dir / name, dirs_exist_ok=True)
            report["backup"] = str(backup_dir)

        # --- 4. Take over --------------------------------------------------
        shutil.copy2(new_db, DB_FILE)
        # Remove WAL/journal leftovers of the old database, otherwise SQLite
        # mixes the old write buffer into the new file.
        for suffix in ("-wal", "-shm", "-journal"):
            leftover = Path(str(DB_FILE) + suffix)
            if leftover.exists():
                leftover.unlink()
        report["restored"].append("data.sqlite")

        for name, path in DATA_DIRS.items():
            src = staging / name
            if not src.is_dir():
                continue
            # Empty the target folder FIRST. Otherwise the files of the old
            # instance would remain alongside - but the new database no longer
            # knows them. The result would be orphaned recordings and media
            # that only cost space and are never caught during cleanup.
            # Safe because the old state was backed up above (keep_backup);
            # without a backup nothing is deliberately deleted.
            if keep_backup and path.is_dir():
                for old_entry in list(path.iterdir()):
                    try:
                        if old_entry.is_dir() and not old_entry.is_symlink():
                            shutil.rmtree(old_entry)
                        else:
                            old_entry.unlink()
                    except OSError:
                        pass
            path.mkdir(parents=True, exist_ok=True)
            for f in src.rglob("*"):
                rel = f.relative_to(src)
                if f.is_dir():
                    (path / rel).mkdir(parents=True, exist_ok=True)
                else:
                    (path / rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, path / rel)
            report["restored"].append(name)

        for arc, path in DATA_FILES.items():
            src = staging / "files" / arc
            if src.is_file():
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, path)
                report["restored"].append(arc)

        if restore_secrets:
            for arc, path in SECRET_FILES.items():
                src = staging / "secrets" / arc
                if src.is_file():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, path)
                    report["restored"].append(arc)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return report
