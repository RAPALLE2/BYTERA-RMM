"""
dbsync.py
---------
External SQL database as persistent storage. Operated via the module "sql"
(src/modules/*/sql). This mechanism stays in the core, so the backend starts
and syncs cleanly even without the module.

Architecture ("working copy" model):
  - The application ALWAYS works with the local SQLite (fast, no dialect
    rewrite of the entire codebase needed).
  - Mode "external": the external SQL DB (MySQL/MariaDB, PostgreSQL or a SQLite
    file, e.g. on a network share) is the persistent storage.
      * On SWITCHING local -> external, EVERYTHING is copied.
      * During operation a background sync mirrors changes (every 60 s, only if
        something changed) and on shutdown.
      * On START in external mode the state is loaded external -> local.
      * On SWITCHING external -> local, a final sync runs and the external
        state is copied to local.

The configuration lives DELIBERATELY outside the database in
backend/dbconfig.json (chicken-and-egg problem):
  { "mode": "local" | "external",
    "type": "mysql" | "postgres" | "sqlite",
    "host": "...", "port": 3306, "user": "...", "password": "...",
    "database": "...",          # for type=sqlite: file path
  }

On dump an additional meta table __rmm_meta is written that contains the
original SQLite CREATE statements of all tables - this lets the local DB be
restored exactly (incl. indexes/defaults) on restore.
"""

import json
import sqlite3
import time
from pathlib import Path

from app import db

from app.paths import DBCONFIG_FILE as _CONFIG_FILE, DB_BACKUP_DIR as _DB_BACKUP_DIR


_META_TABLE = "__rmm_meta"

# Backups of the local database (before every switch).
_BACKUP_DIR = _DB_BACKUP_DIR
# The database can easily be hundreds of MB - therefore deliberately keep few
# backups, otherwise the disk fills up.
_BACKUP_KEEP = 5


# ------------------------------------------------------------------
# Progress / error log of a running switch
# ------------------------------------------------------------------
# The switch copies dozens of tables depending on the data volume. Previously
# one saw nothing of it: either "ok" came at the end or a single error message
# - and at the first error everything aborted. Hence a shared state here that
# the UI can query while the switch runs.
JOB = {
    "running": False,
    "phase": "",            # "backup" | "dump" | "restore" | "fertig" (phase key, mapped to a label by the sql module)
    "table": "",            # table currently being processed
    "done": 0,              # finished tables
    "total": 0,             # tables in total
    "rows": 0,              # rows copied in total
    "errors": [],           # [{table, error}] - the run continues anyway
    "log": [],              # short text lines for the display
    "ok": None,             # True/False once finished
    "detail": "",
    "backup": "",           # path of the backup created beforehand
    "restored": False,      # was it restored after an error?
    "started_at": 0,
    "finished_at": 0,
}


def job_reset(phase: str = "") -> None:
    JOB.update({"running": True, "phase": phase, "table": "", "done": 0,
                "total": 0, "rows": 0, "errors": [], "log": [], "ok": None,
                "detail": "", "backup": "", "restored": False,
                "started_at": int(time.time() * 1000), "finished_at": 0})


def job_log(text: str) -> None:
    JOB["log"].append(text)
    del JOB["log"][:-200]          # do not let it grow unbounded
    print(f"[dbsync] {text}")


def job_error(table: str, err: Exception | str) -> None:
    JOB["errors"].append({"table": table, "error": str(err)})
    job_log(f"ERROR at '{table}': {err}")


def job_finish(ok: bool, detail: str = "") -> None:
    JOB.update({"running": False, "ok": ok, "detail": detail,
                "phase": "fertig", "finished_at": int(time.time() * 1000)})


# ------------------------------------------------------------------
# Backup of the local database
# ------------------------------------------------------------------

def backup_local(tag: str = "switch") -> str:
    """
    Creates a consistent copy of the local SQLite and returns the path.

    Deliberately via the sqlite3 backup API instead of a file copy: the
    connection is open during operation, a simple copy could be made mid
    transaction and would then be unusable - i.e. broken exactly when it is
    needed.
    """
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = _BACKUP_DIR / f"{stamp}-{tag}.sqlite"
    dest = sqlite3.connect(path)
    try:
        db._raw_conn.backup(dest)
        dest.commit()
    finally:
        dest.close()
    _prune_backups()
    return str(path)


def _prune_backups() -> None:
    try:
        files = sorted(_BACKUP_DIR.glob("*.sqlite"), key=lambda p: p.stat().st_mtime)
        for old in files[:-_BACKUP_KEEP]:
            old.unlink(missing_ok=True)
    except OSError:
        pass


def list_backups() -> list[dict]:
    out = []
    try:
        for p in sorted(_BACKUP_DIR.glob("*.sqlite"),
                        key=lambda p: p.stat().st_mtime, reverse=True):
            st = p.stat()
            out.append({"name": p.name, "path": str(p),
                        "size": st.st_size, "at": int(st.st_mtime * 1000)})
    except OSError:
        pass
    return out


def restore_local_backup(path: str) -> None:
    """
    Restores a backup into the RUNNING local database.

    Here too the backup API instead of swapping the file: the application keeps
    the connection open, a file swap would pull the ground out from under it.
    This way the content is replaced in place.
    """
    src = sqlite3.connect(path)
    try:
        src.backup(db._raw_conn)
        db._raw_conn.commit()
    finally:
        src.close()

# For the change check of the periodic sync
_last_total_changes: int | None = None


# ------------------------------------------------------------------
# Konfiguration
# ------------------------------------------------------------------

def load_config() -> dict:
    try:
        cfg = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            raise ValueError("not an object")
    except (OSError, ValueError):
        cfg = {}
    cfg.setdefault("mode", "local")
    cfg.setdefault("type", "mysql")
    cfg.setdefault("host", "127.0.0.1")
    cfg.setdefault("port", 3306)
    cfg.setdefault("user", "")
    cfg.setdefault("password", "")
    cfg.setdefault("database", "")
    return cfg


def save_config(cfg: dict) -> None:
    _CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8")


def public_config() -> dict:
    """Configuration without the password (for the API/UI)."""
    cfg = dict(load_config())
    cfg["password"] = "•••" if cfg.get("password") else ""
    return cfg


# ------------------------------------------------------------------
# Externe Verbindung (kleine Dialekt-Schicht)
# ------------------------------------------------------------------


def _ensure_driver(module: str, pypi: str):
    """
    Import the driver - and if it is missing, install it ourselves.

    Both drivers are fixed in requirements.txt, but existing installations
    often do not have them yet (the image was built before the change). A
    single pip call is not always enough for that: depending on the
    environment it fails on an "externally managed environment" (PEP 668) or on
    missing write permissions in the system directory. Therefore several ways
    are tried in turn - and on failure the REAL pip output is in the message,
    instead of just "driver missing".
    """
    import importlib
    try:
        return importlib.import_module(module)
    except ImportError:
        pass

    import subprocess
    import sys

    attempts = [
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", pypi],
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
         "--break-system-packages", pypi],
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
         "--user", pypi],
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
         "--user", "--break-system-packages", pypi],
    ]
    errors = []
    for cmd in attempts:
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if res.returncode == 0:
                importlib.invalidate_caches()
                # A package freshly installed to --user may lie in a path that
                # is not yet in sys.path.
                try:
                    import site
                    for p in (site.getusersitepackages(),):
                        if p and p not in sys.path:
                            sys.path.insert(0, p)
                except Exception:
                    pass
                try:
                    return importlib.import_module(module)
                except ImportError as e:
                    errors.append(f"installed, but not importable: {e}")
                    continue
            tail = (res.stderr or res.stdout or "").strip().splitlines()
            errors.append(tail[-1] if tail else f"exit code {res.returncode}")
        except Exception as e:
            errors.append(str(e))

    raise RuntimeError(
        f"The database driver '{pypi}' is missing and could not be installed "
        f"automatically. Last error: {errors[-1] if errors else 'unknown'}. "
        f"Remedy: rebuild the container image (docker compose build --no-cache) "
        f"or run in the container: pip install {pypi}") from None


class ExternalDB:
    """
    Minimal abstraction over MySQL/PostgreSQL/SQLite for exactly the operations
    the sync needs: create/drop tables, bulk insert, read everything.
    """

    def __init__(self, cfg: dict):
        self.type = (cfg.get("type") or "mysql").lower()
        self.cfg = cfg
        if self.type == "mysql":
            pymysql = _ensure_driver("pymysql", "PyMySQL")
            self.conn = pymysql.connect(
                host=cfg.get("host") or "127.0.0.1",
                port=int(cfg.get("port") or 3306),
                user=cfg.get("user") or "",
                password=cfg.get("password") or "",
                database=cfg.get("database") or "",
                charset="utf8mb4", autocommit=False,
            )
            self.ph = "%s"
        elif self.type == "postgres":
            psycopg2 = _ensure_driver("psycopg2", "psycopg2-binary")
            self.conn = psycopg2.connect(
                host=cfg.get("host") or "127.0.0.1",
                port=int(cfg.get("port") or 5432),
                user=cfg.get("user") or "",
                password=cfg.get("password") or "",
                dbname=cfg.get("database") or "",
            )
            self.ph = "%s"
        elif self.type == "sqlite":
            path = cfg.get("database") or ""
            if not path:
                raise RuntimeError("For type=sqlite, 'database' must be the file path")
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(path)
            self.ph = "?"
        else:
            raise RuntimeError(f"Unknown database type: {self.type}")

    # --- Identifiers/types per dialect ---

    def q(self, ident: str) -> str:
        return f"`{ident}`" if self.type == "mysql" else f'"{ident}"'

    def coltype(self, sqlite_decl: str) -> str:
        d = (sqlite_decl or "").upper()
        if "INT" in d:
            return "BIGINT"
        if "REAL" in d or "FLOA" in d or "DOUB" in d:
            return "DOUBLE" if self.type == "mysql" else ("DOUBLE PRECISION" if self.type == "postgres" else "REAL")
        if "BLOB" in d:
            return "LONGBLOB" if self.type == "mysql" else ("BYTEA" if self.type == "postgres" else "BLOB")
        return "LONGTEXT" if self.type == "mysql" else "TEXT"

    # --- Basic operations ---

    def execute(self, sql: str, params: tuple = ()):  # noqa: ANN001
        cur = self.conn.cursor()
        cur.execute(sql, params)
        return cur

    def executemany(self, sql: str, rows: list):
        if not rows:
            return
        cur = self.conn.cursor()
        cur.executemany(sql, rows)

    def commit(self):
        self.conn.commit()

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def recreate_table(self, name: str, columns: list[tuple[str, str]]) -> None:
        """columns: list of (name, sqlite_type_declaration)."""
        self.execute(f"DROP TABLE IF EXISTS {self.q(name)}")
        cols = ", ".join(f"{self.q(c)} {self.coltype(t)}" for c, t in columns)
        self.execute(f"CREATE TABLE {self.q(name)} ({cols})")

    def insert_rows(self, name: str, colnames: list[str], rows: list) -> None:
        cols = ", ".join(self.q(c) for c in colnames)
        ph = ", ".join([self.ph] * len(colnames))
        self.executemany(f"INSERT INTO {self.q(name)} ({cols}) VALUES ({ph})", rows)

    def read_all(self, name: str, colnames: list[str]) -> list:
        cols = ", ".join(self.q(c) for c in colnames)
        cur = self.execute(f"SELECT {cols} FROM {self.q(name)}")
        return cur.fetchall()

    def table_exists(self, name: str) -> bool:
        try:
            self.execute(f"SELECT 1 FROM {self.q(name)} LIMIT 1" if self.type != "postgres"
                         else f'SELECT 1 FROM {self.q(name)} LIMIT 1')
            return True
        except Exception:
            try:
                self.conn.rollback()   # PostgreSQL: a failed query ends the TX
            except Exception:
                pass
            return False


def test_connection(cfg: dict) -> dict:
    """Test the connection; throws an understandable error on problems."""
    ext = ExternalDB(cfg)
    try:
        ext.execute("SELECT 1")
        return {"ok": True}
    finally:
        ext.close()


# ------------------------------------------------------------------
# Dump / restore
# ------------------------------------------------------------------

def _local_tables() -> list[dict]:
    """All local tables with CREATE SQL and columns (name + type)."""
    tables = []
    rows = db._conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    for r in rows:
        cols = db._conn.execute(f'PRAGMA table_info("{r["name"]}")').fetchall()
        tables.append({
            "name": r["name"],
            "sql": r["sql"],
            "columns": [(c["name"], c["type"] or "TEXT") for c in cols],
        })
    return tables


def dump_local_to_external(cfg: dict | None = None, track: bool = False) -> dict:
    """
    Copies the ENTIRE local database into the external one (replaces everything
    there).

    'track=True' records the progress into JOB and lets the run CONTINUE on a
    table error, instead of aborting immediately. This way one sees all
    problems at once at the end instead of only ever the first. The caller then
    decides, based on JOB["errors"], whether the switch counts.
    """
    cfg = cfg or load_config()
    ext = ExternalDB(cfg)
    try:
        tables = _local_tables()
        counts = {}
        if track:
            JOB["phase"] = "dump"
            JOB["total"] = len(tables)
            job_log(f"Copying {len(tables)} tables to {cfg.get('type')}…")
        for t in tables:
            colnames = [c[0] for c in t["columns"]]
            if track:
                JOB["table"] = t["name"]
            try:
                data = db._conn.execute(
                    f'SELECT {", ".join(chr(34)+c+chr(34) for c in colnames)} FROM "{t["name"]}"'
                ).fetchall()
                rows = [tuple(r[c] for c in colnames) for r in data]
                ext.recreate_table(t["name"], t["columns"])
                ext.insert_rows(t["name"], colnames, rows)
                counts[t["name"]] = len(rows)
                if track:
                    JOB["rows"] += len(rows)
            except Exception as e:
                if not track:
                    raise
                job_error(t["name"], e)
                counts[t["name"]] = 0
            finally:
                if track:
                    JOB["done"] += 1
        # Meta: original SQLite schemas + column lists + timestamp
        ext.recreate_table(_META_TABLE, [("k", "TEXT"), ("v", "TEXT")])
        meta = {
            "schema": {t["name"]: t["sql"] for t in tables},
            "columns": {t["name"]: [c[0] for c in t["columns"]] for t in tables},
            "synced_at": int(time.time() * 1000),
        }
        ext.insert_rows(_META_TABLE, ["k", "v"], [("state", json.dumps(meta))])
        ext.commit()
        return {"ok": True, "tables": counts, "synced_at": meta["synced_at"]}
    finally:
        ext.close()


def _read_meta(ext: ExternalDB) -> dict | None:
    if not ext.table_exists(_META_TABLE):
        return None
    rows = ext.read_all(_META_TABLE, ["k", "v"])
    for k, v in rows:
        if k == "state":
            return json.loads(v)
    return None


def restore_external_to_local(cfg: dict | None = None, track: bool = False) -> dict:
    """
    Replaces the local database with the state from the external DB.
    The tables are recreated with the ORIGINAL SQLite CREATE statements (from
    __rmm_meta), then all rows are loaded.
    """
    cfg = cfg or load_config()
    ext = ExternalDB(cfg)
    try:
        meta = _read_meta(ext)
        if not meta:
            raise RuntimeError("The external database contains no RMM state (__rmm_meta missing)")
        counts = {}
        cur = db._conn
        cur.execute("PRAGMA foreign_keys = OFF")
        if track:
            JOB["phase"] = "restore"
            JOB["total"] = len(meta["schema"])
            job_log(f"Loading {len(meta['schema'])} tables from {cfg.get('type')}…")
        for name, create_sql in meta["schema"].items():
            colnames = meta["columns"][name]
            if track:
                JOB["table"] = name
            try:
                rows = ext.read_all(name, colnames) if ext.table_exists(name) else []
                cur.execute(f'DROP TABLE IF EXISTS "{name}"')
                cur.execute(create_sql)
                if rows:
                    ph = ", ".join(["?"] * len(colnames))
                    cols = ", ".join(f'"{c}"' for c in colnames)
                    cur.executemany(
                        f'INSERT INTO "{name}" ({cols}) VALUES ({ph})',
                        [tuple(r) for r in rows],
                    )
                counts[name] = len(rows)
                if track:
                    JOB["rows"] += len(rows)
            except Exception as e:
                if not track:
                    raise
                job_error(name, e)
                counts[name] = 0
            finally:
                if track:
                    JOB["done"] += 1
        cur.commit()
        return {"ok": True, "tables": counts, "synced_at": meta.get("synced_at")}
    finally:
        ext.close()


# ------------------------------------------------------------------
# Running operation (startup restore, periodic sync)
# ------------------------------------------------------------------

def startup_restore_if_external() -> None:
    """
    Called at backend start BEFORE init_db(): if the external mode is active,
    the state from the external DB is loaded into the local working copy. If
    that fails, the backend continues with the local state (better than not
    starting at all) and reports the error in the log.
    """
    cfg = load_config()
    if cfg.get("mode") != "external":
        return
    try:
        res = restore_external_to_local(cfg)
        total = sum(res["tables"].values())
        print(f"[dbsync] External mode: loaded {total} rows from {cfg.get('type')}")
    except Exception as e:
        print(f"[dbsync] WARNING: external DB not loadable ({e}) - continuing with local state")
    global _last_total_changes
    _last_total_changes = db._conn.total_changes


def periodic_sync() -> None:
    """
    Mirrors the local working copy into the external DB if something changed
    since the last sync. Called by main.py periodically and on shutdown.
    (Synchronous; run by main in the executor.)
    """
    global _last_total_changes
    cfg = load_config()
    if cfg.get("mode") != "external":
        return
    changes = db._conn.total_changes
    if _last_total_changes is not None and changes == _last_total_changes:
        return   # nothing changed -> nothing to do
    try:
        dump_local_to_external(cfg)
        _last_total_changes = changes
    except Exception as e:
        print(f"[dbsync] sync into external DB failed: {e}")
