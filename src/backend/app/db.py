"""
db.py
-----
Complete database layer for BYTERA RMM.

We use SQLite (built into Python, "import sqlite3" - no installation needed,
no server needed). The database is simply a file "data.sqlite" in the backend
folder.

Table overview (see also the CREATE TABLE blocks further below):
- users:      dashboard login users (admins/technicians)
- tenants:    customers/companies (top level of the hierarchy)
- locations:  locations of a tenant (e.g. "Office Regensburg")
- folders:    freely creatable, nestable folders INSIDE a location
              (e.g. "Rack 1" -> "Server" -> ...). parent_folder_id points
              to another folder, or is NULL when it is a folder directly
              under the location.
- clients:    the actual managed devices (PCs, servers, VMs/CTs).
              parent_client_id is set when a client is a VM/CT of another
              (physical) client - this way the backend does not have to treat
              VMs/CTs as a separate concept, they are simply "clients with a
              parent client".

All functions here are deliberately kept simple (SQL directly, no ORM), so a
Python beginner can see exactly what happens.
"""

import re
import sqlite3
import json
import time
import pathlib
import threading
import uuid

# Path to the database file: in the backend folder, one level above "app/"
from app.paths import DB_FILE as DB_PATH


# ------------------------------------------------------------------
# Thread-safe SQLite connection
# ------------------------------------------------------------------
# There is ONE shared connection for the whole app. But FastAPI/Uvicorn runs
# request handlers in several worker threads, and in addition background tasks
# (automation, uptime, relay auto-close, DB sync, …) access it from the
# event-loop thread. An sqlite3.Connection must NOT be used from several
# threads at once - otherwise sporadic crashes occur like
# "sqlite3.InterfaceError: bad parameter or other API misuse".
#
# Therefore we wrap the real connection in a thin wrapper that serializes ALL
# accesses with ONE (reentrant) lock and materializes query results
# immediately (still under the lock). This way every operation stays atomic
# and the one connection can be used safely from any number of threads.

class _CursorResult:
    """Result of an execute(): rows are already loaded (under the lock) and can
    therefore be used thread-safely (fetchone/fetchall/iteration)."""

    def __init__(self, rows, rowcount, lastrowid, description=None):
        self._rows = rows
        self._i = 0
        self.rowcount = rowcount
        self.lastrowid = lastrowid
        # Column description of the real cursor (7-tuple per column, as in
        # sqlite3). Needed to determine column names - e.g. in the source-DB
        # tool. Without it "cur.description" failed.
        self.description = description

    def keys(self):
        """Column names as a list (like sqlite3.Row / SQLAlchemy result)."""
        return [d[0] for d in (self.description or [])]

    def fetchone(self):
        if self._i < len(self._rows):
            r = self._rows[self._i]
            self._i += 1
            return r
        return None

    def fetchall(self):
        r = self._rows[self._i:]
        self._i = len(self._rows)
        return list(r)

    def fetchmany(self, size=1):
        r = self._rows[self._i:self._i + size]
        self._i += len(r)
        return list(r)

    def __iter__(self):
        while self._i < len(self._rows):
            yield self._rows[self._i]
            self._i += 1


# From this duration on, a query counts as notable. 0 disables the measurement.
import os as _os_slow

try:
    SLOW_QUERY_S = float(_os_slow.getenv("RMM_SLOW_QUERY_S", "0.5") or 0.5)
except ValueError:
    SLOW_QUERY_S = 0.5

from time import perf_counter as _perf

_slow_seen: dict[str, float] = {}


def _report_slow_query(seconds: float, sql: str) -> None:
    """Reports a slow query - at most once per minute per query."""
    if SLOW_QUERY_S <= 0 or seconds < SLOW_QUERY_S:
        return
    short = " ".join(str(sql).split())[:160]
    import time as _time
    now = _time.monotonic()
    if now - _slow_seen.get(short, 0.0) < 60:
        return
    _slow_seen[short] = now
    # The hint text depends on WHERE the query ran. Since the endpoints are in
    # worker threads and the background loops use db.call(), a slow query
    # normally NO longer blocks the event loop - it "only" occupies the
    # database for everyone else. If it runs in the main thread, however, that
    # is the serious case, and the text must say so.
    on_loop = threading.current_thread() is threading.main_thread()
    folge = ("It ran in the MAIN THREAD - the whole server was stalled meanwhile."
             if on_loop else
             "It occupied the database; other accesses had to wait.")
    message = f"SLOW QUERY: {seconds:.2f}s - {short}\n  ({folge})"
    try:
        from app.errors import report, Codes
        report(Codes.DB_SLOW, None, f"{seconds:.2f}s",
               sql=short, on_main_thread=on_loop)
    except Exception:
        print(f"[db] {message}")


# ======================================================================
# Database access OUTSIDE the event loop
# ======================================================================
# The crash on 23.08. (16:23) in a chain:
#
#   1. Someone pulled a Docker image (guacd) in the dashboard. The download
#      saturated the disk.
#   2. A concurrent commit() from a worker thread therefore waited 91 seconds
#      for its fsync - holding the database lock the whole time.
#   3. The relay-expiry loop ran in the MAIN THREAD and called
#      db.list_expired_relay_clients() there. It waited on the same lock -
#      and thus the entire event loop was stalled.
#   4. After 90 seconds the watchdog ended the process.
#
# The HTTP endpoints were already offloaded to worker threads at that time.
# The BACKGROUND LOOPS, however, were not - they are 'async def' and call the
# database directly. Exactly one such loop blocked everything here.
#
# 'call()' moves a database call into a worker thread. This way no background
# loop can stall the event loop anymore, no matter how long the database takes.
#
#     rows = await db.call(db.list_expired_relay_clients, now_ms)

async def call(fn, *args, **kwargs):
    """Runs a database call in a worker thread."""
    import asyncio as _asyncio
    return await _asyncio.to_thread(fn, *args, **kwargs)


# How long to wait for the database lock before giving up. Without a limit a
# caller waits indefinitely - and if it sits on the event loop, the whole
# server is stalled meanwhile. An error after 30 seconds is always better than
# a stalled backend.
try:
    LOCK_TIMEOUT_S = float(_os_slow.getenv("RMM_DB_LOCK_TIMEOUT_S", "30") or 30)
except (ValueError, NameError):
    LOCK_TIMEOUT_S = 30.0


class _LockTimeout(RuntimeError):
    """The database lock was held for too long."""


class _TimedLock:
    """
    Like an RLock, but with a time limit and a report.

    If the limit is exceeded, that is valuable information: it means another
    operation is holding the database unusually long. Previously this was only
    noticed indirectly - from a query that supposedly took 109 seconds, when in
    truth it had waited 109 seconds.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.holder = ""
        self.since = 0.0

    def __enter__(self):
        waited_from = _perf()
        if not self._lock.acquire(timeout=LOCK_TIMEOUT_S if LOCK_TIMEOUT_S > 0 else -1):
            held = _perf() - self.since if self.since else 0.0
            try:
                from app.errors import report, Codes
                report(Codes.DB_LOCKED, None,
                       f"lock not obtained after {_perf() - waited_from:.0f}s",
                       held_by=self.holder or "unknown",
                       held_since=f"{held:.0f}s")
            except Exception:
                pass
            raise _LockTimeout(
                f"database held for {held:.0f}s by '{self.holder}' - "
                f"call aborted so the server keeps running")
        waited = _perf() - waited_from
        self.since = _perf()
        if waited > 5.0:
            try:
                from app.errors import report, Codes
                report(Codes.DB_LOCKED, None,
                       f"waited {waited:.0f}s for the database",
                       previously_held_by=self.holder or "unknown")
            except Exception:
                pass
        self.holder = threading.current_thread().name
        return self

    def __exit__(self, *exc):
        self.holder = ""
        self.since = 0.0
        self._lock.release()
        return False


class _SafeConn:
    """Thin, thread-safe wrapper around the shared SQLite connection."""

    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)
        # _TimedLock instead of RLock: with a time limit and a report of who holds it.
        object.__setattr__(self, "_lock", _TimedLock())

    @property
    def lock(self):
        return object.__getattribute__(self, "_lock")

    def _run(self, method, sql, arg):
        # TIMING: all database accesses run SYNCHRONOUSLY in the same event
        # loop that also serves the HTTP requests. As long as a query takes
        # milliseconds, nobody notices. If it takes a second, the whole server
        # is stalled during that second - including the health check and all
        # agent connections.
        #
        # The HTTP endpoints are now in worker threads, the background loops
        # use db.call(). This measurement stays anyway: it names the queries
        # that take too long, and thanks to _TimedLock now distinguishes
        # between "computes long" and "waits for the lock" - previously both
        # looked the same.
        _t0 = _perf()
        with self.lock:
            real = object.__getattribute__(self, "_conn")
            # The emergency-stop callback needs this info to know WHAT is
            # running right now and since when.
            _current["sql"] = str(sql)
            _current["started"] = _t0
            _current["warned"] = False
            try:
                cur = method(real, sql, arg) if arg is not None else method(real, sql)
            except sqlite3.OperationalError as exc:
                _current["started"] = 0.0
                waited = _perf() - _t0
                text = str(exc).lower()
                if "interrupt" in text:
                    # Aborted by our own emergency stop.
                    from app.errors import report, Codes
                    report(Codes.DB_ABORT, exc,
                           f"query aborted after {waited:.0f}s "
                           f"(limit {QUERY_DEADLINE_S:.0f}s)",
                           sql=str(sql)[:200])
                elif "locked" in text or "busy" in text:
                    from app.errors import report, Codes
                    report(Codes.DB_LOCKED, exc, f"after {waited:.1f}s",
                           sql=str(sql)[:200])
                else:
                    from app.errors import report, Codes
                    report(Codes.DB_QUERY, exc, "query failed",
                           sql=str(sql)[:200])
                raise
            finally:
                _current["started"] = 0.0
            try:
                rows = cur.fetchall()
            except Exception:
                rows = []
            # description MUST be read here (still under the lock) - after the
            # next execute() on the same connection it is gone.
            try:
                desc = cur.description
            except Exception:
                desc = None
            result = _CursorResult(rows, cur.rowcount, cur.lastrowid, desc)
        _report_slow_query(_perf() - _t0, sql)
        return result

    def execute(self, sql, params=None):
        import sqlite3 as _sq
        return self._run(_sq.Connection.execute, sql, params if params is not None else ())

    def executemany(self, sql, seq):
        import sqlite3 as _sq
        return self._run(_sq.Connection.executemany, sql, list(seq))

    def executescript(self, sql):
        with self.lock:
            return object.__getattribute__(self, "_conn").executescript(sql)

    def commit(self):
        with self.lock:
            return object.__getattribute__(self, "_conn").commit()

    def rollback(self):
        with self.lock:
            return object.__getattribute__(self, "_conn").rollback()

    def backup(self, target, *a, **k):
        with self.lock:
            return object.__getattribute__(self, "_conn").backup(target, *a, **k)

    def cursor(self):
        # A direct cursor bypasses the serialization; only use it under .lock.
        with self.lock:
            return object.__getattribute__(self, "_conn").cursor()

    @property
    def total_changes(self):
        with self.lock:
            return object.__getattribute__(self, "_conn").total_changes

    # Pass other attributes (e.g. row_factory) through to the real connection.
    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_conn"), name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_conn"), name, value)


def _apply_pending_migration() -> None:
    """Backend migration (Settings -> Migration): applies a transfer staged by
    the migration import BEFORE the database is opened.

    router/migration_routes.py downloads an archive from the old backend and
    extracts it to <backend>/migration_incoming/ (+ apply.json as a marker);
    then the backend restarts. Here the parts are moved into place:
      data/     -> DATA_DIR   (DB, .env, module settings; version.txt and
                               requirements.txt of the NEW backend stay)
      storage/  -> STORAGE_DIR (files, websites, recordings, dist, /storage)
      branding/ -> frontend/image (logos/images)
      modules/  -> MODULES_DIR (only modules that do not yet exist here)
    The existing DB/.env is backed up as premigration-*. Errors do NOT abort the
    start - the rest is applied and logged."""
    import shutil
    from app.paths import (BACKEND_DIR, DATA_DIR, STORAGE_DIR, MODULES_DIR,
                           BRANDING_DIR)
    staging = BACKEND_DIR / "migration_incoming"
    marker = staging / "apply.json"
    if not marker.exists():
        return
    print("[migration] Applying the migrated data of the old backend …")

    def _replace(src: pathlib.Path, dst: pathlib.Path) -> None:
        if dst.is_dir() and not dst.is_symlink():
            shutil.rmtree(dst, ignore_errors=True)
        elif dst.exists():
            dst.unlink(missing_ok=True)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))

    # ---- data/ (DB, .env, modules.json, module data) ----
    src_data = staging / "data"
    if src_data.is_dir():
        for keepname, backup in (("data.sqlite", "premigration-data.sqlite"),
                                 (".env", ".env.premigration")):
            cur = DATA_DIR / keepname
            if cur.exists() and (src_data / keepname).exists():
                try:
                    shutil.copy2(cur, DATA_DIR / backup)
                except OSError as e:
                    print(f"[migration] backup {backup} failed: {e}")
        for item in src_data.iterdir():
            if item.name in ("version.txt", "requirements.txt", "backup"):
                continue  # belong to the NEW backend version
            try:
                _replace(item, DATA_DIR / item.name)
            except OSError as e:
                print(f"[migration] data/{item.name}: {e}")

    # ---- storage/ ----
    src_storage = staging / "storage"
    if src_storage.is_dir():
        for item in src_storage.iterdir():
            try:
                _replace(item, STORAGE_DIR / item.name)
            except OSError as e:
                print(f"[migration] storage/{item.name}: {e}")

    # ---- branding/ (frontend/image) ----
    src_brand = staging / "branding"
    if src_brand.is_dir():
        for item in src_brand.iterdir():
            try:
                _replace(item, BRANDING_DIR / item.name)
            except OSError as e:
                print(f"[migration] branding/{item.name}: {e}")

    # ---- modules/ (only ones unknown here, e.g. custom modules) ----
    src_mods = staging / "modules"
    if src_mods.is_dir():
        for item in src_mods.iterdir():
            if not item.is_dir():
                continue
            target = pathlib.Path(MODULES_DIR) / item.name
            if target.exists():
                continue  # Standard-/vorhandenes Modul: Code des neuen Stands behalten
            try:
                shutil.move(str(item), str(target))
                print(f"[migration] Custom module taken over: {item.name}")
            except OSError as e:
                print(f"[migration] modules/{item.name}: {e}")

    shutil.rmtree(staging, ignore_errors=True)
    print("[migration] Transfer complete - old data is kept as "
          "premigration-* in data/.")


try:
    _apply_pending_migration()
except Exception as _mig_exc:   # migration must never prevent the start
    print(f"[migration] ERROR while applying: {_mig_exc}")

# A single, shared connection for the whole app.
# check_same_thread=False, because FastAPI/Uvicorn can use several threads;
# _SafeConn handles the serialization (see above).
_raw_conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)


# ----------------------------------------------------------------------
# EMERGENCY STOP FOR QUERIES
# ----------------------------------------------------------------------
# The incident on 23.08. in one sentence: ONE query ran for 125 seconds
# (a client's complete metric history, unbounded). Because all queries go
# through the same lock, the ENTIRE backend was stalled - login, settings,
# agents, health check. After 90 seconds the watchdog ended the process.
#
# The query in question is fixed further below. But relying on there being no
# second one of this kind would be naive - there are over 300 query sites.
# Hence a limit here that applies to ALL:
#
# SQLite regularly calls a callback during a running query. If it returns
# anything other than 0, the query is ABORTED. This way no single query can
# stall the server anymore, no matter how it looks or who writes it.
#
# The caller then gets an ordinary exception and can react to it. An aborted
# query is always better than a stalled server: on an abort one display is
# missing, on a stall everything is missing.

import os as _os_db

try:
    QUERY_DEADLINE_S = float(_os_db.getenv("RMM_QUERY_DEADLINE_S", "20") or 20)
except ValueError:
    QUERY_DEADLINE_S = 20.0

# How often SQLite checks. 20000 instructions are a few milliseconds - often
# enough for a precise limit, rare enough not to slow things down.
_PROGRESS_STEPS = 20000

# When the running query started (only one value, since the lock allows only
# one query at a time anyway).
_current = {"sql": "", "started": 0.0, "warned": False}


def _progress_handler() -> int:
    """Runs DURING a query. Aborts it after the time limit."""
    started = _current["started"]
    if not started:
        return 0
    elapsed = _perf() - started
    # Early warning still WHILE the query runs. Previously the message only
    # appeared afterwards - at 125 seconds far too late to understand what is
    # stalling the backend right now.
    if not _current["warned"] and elapsed > 3.0:
        _current["warned"] = True
        try:
            from app.errors import report, Codes
            report(Codes.DB_SLOW, None,
                   f"query has been running for {elapsed:.0f}s AND IS BLOCKING RIGHT NOW",
                   sql=_current["sql"][:200])
        except Exception:
            pass
    if QUERY_DEADLINE_S > 0 and elapsed > QUERY_DEADLINE_S:
        return 1      # != 0 -> SQLite aborts
    return 0


try:
    _raw_conn.set_progress_handler(_progress_handler, _PROGRESS_STEPS)
except Exception as _e:
    print(f"[db] query time limit could not be enabled: {_e}")


# ----------------------------------------------------------------------
# Tune SQLite for slow file systems
# ----------------------------------------------------------------------
# Why this matters: the database lives in backend/ and thus in the bind mount.
# On a NAS that is a network or Btrfs file system, and every commit() triggers
# an fsync there by default. With 81 commit sites in the code and seven agents
# writing a heartbeat every five seconds, that is constant disk syncs - and
# they run in the event-loop thread.
#
# If a single fsync takes several seconds once (NAS in spindown, a snapshot, a
# running scrub), the ENTIRE backend is stalled that long: no HTTP response,
# no Socket.IO pings. The Docker health check times out after 5 s, Cloudflare
# reports 524, and all agents lose the connection at once - exactly the pattern
# from the log on 22.08. at 23:38:29 (agents 7 -> 0, process stayed alive).
#
# The following settings change nothing about data safety in operation, but
# make commit() cheaper by orders of magnitude.
def _tune_sqlite(conn) -> None:
    """Sets the PRAGMAs. Errors here must never prevent the start."""
    settings = [
        # WAL: writes land in a journal file instead of rewriting the main
        # file. Readers no longer block writers.
        ("journal_mode", "WAL"),
        # NORMAL instead of FULL: fsync only on checkpoint, not on every
        # commit. On a failure the very last transaction may be lost - for a
        # monitoring tool that is clearly the better choice over a frozen
        # backend.
        ("synchronous", "NORMAL"),
        # Instead of throwing "database is locked" immediately, wait up to 15 s.
        ("busy_timeout", "15000"),
        # Temporary tables in memory, not on the NAS.
        ("temp_store", "MEMORY"),
        # ~64 MB cache: significantly fewer reads on the file system.
        ("cache_size", "-64000"),
        # Merge the WAL file at ~4 MB so it does not grow unbounded.
        ("wal_autocheckpoint", "1000"),
    ]
    for key, value in settings:
        try:
            conn.execute(f"PRAGMA {key}={value}")
        except sqlite3.Error as e:
            print(f"[db] PRAGMA {key}={value} not set: {e}")
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        print(f"[db] SQLite ready: journal_mode={mode}, synchronous=NORMAL, "
              f"busy_timeout=15s")
    except sqlite3.Error:
        pass


_tune_sqlite(_raw_conn)

# row_factory lets us access rows like dictionaries (e.g. row["hostname"]
# instead of row[3]), which is much more readable.
_raw_conn.row_factory = sqlite3.Row

_conn = _SafeConn(_raw_conn)


def _new_id() -> str:
    """Creates a new, unique ID (UUID) for new records."""
    return str(uuid.uuid4())


def _now_ms() -> int:
    """Current time in milliseconds since 1970 (handy for timestamps)."""
    return int(time.time() * 1000)


def _migrate_add_column(table: str, column: str, definition: str) -> None:
    """
    Adds a column if it does not yet exist (simple migration). This way
    existing databases from older versions can be upgraded without having to
    delete data.sqlite.
    """
    existing = [row["name"] for row in _conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in existing:
        _conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        _conn.commit()


def init_db() -> None:
    """
    Creates all tables if they do not yet exist.
    Called once at backend startup (see main.py).
    """
    _conn.executescript(
        """
        -- dashboard login users
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'admin',   -- 'admin' or 'viewer' (preparation for finer rights later)
            must_change_pw INTEGER NOT NULL DEFAULT 0,  -- 1 = must set a new password at next login
            language TEXT NOT NULL DEFAULT 'en',   -- language preference: 'en' (default) or 'de'
            theme TEXT NOT NULL DEFAULT 'dark',    -- 'dark' or 'light'
            created_at INTEGER NOT NULL
        );

        -- customers/companies (top hierarchy level)
        CREATE TABLE IF NOT EXISTS tenants (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            color TEXT NOT NULL DEFAULT '#2dd4bf'
        );

        -- locations of a tenant
        CREATE TABLE IF NOT EXISTS locations (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            name TEXT NOT NULL
        );

        -- freely creatable, nestable folders inside a location
        CREATE TABLE IF NOT EXISTS folders (
            id TEXT PRIMARY KEY,
            location_id TEXT NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
            parent_folder_id TEXT REFERENCES folders(id) ON DELETE CASCADE,
            name TEXT NOT NULL
        );

        -- the actual managed devices
        CREATE TABLE IF NOT EXISTS clients (
            id TEXT PRIMARY KEY,
            hostname TEXT NOT NULL,
            tenant_id TEXT REFERENCES tenants(id) ON DELETE SET NULL,
            location_id TEXT REFERENCES locations(id) ON DELETE SET NULL,
            folder_id TEXT REFERENCES folders(id) ON DELETE SET NULL,
            parent_client_id TEXT REFERENCES clients(id) ON DELETE CASCADE, -- set for VM/CT
            platform TEXT,
            arch TEXT,
            release TEXT,
            ip TEXT,
            color TEXT NOT NULL DEFAULT '#38bdf8',
            notes TEXT NOT NULL DEFAULT '',
            status_override TEXT,        -- e.g. 'maintenance', set manually by the admin
            active INTEGER NOT NULL DEFAULT 1,  -- 0 = disabled (but stays visible/historical)
            first_seen INTEGER NOT NULL,
            last_seen INTEGER NOT NULL
        );

        -- audit log: records who did what and when (basic, to be extended)
        CREATE TABLE IF NOT EXISTS audit_log (
            id TEXT PRIMARY KEY,
            ts INTEGER NOT NULL,
            username TEXT,
            action TEXT NOT NULL,     -- e.g. "login", "exec", "client.update"
            target TEXT,              -- e.g. client ID or username of an affected record
            details TEXT              -- free text/JSON with further info
        );

        -- one-time tokens for client onboarding ("Add client" in the sidebar).
        -- A token is created on clicking "Add client" and already contains
        -- tenant/location, so the new agent is sorted in correctly right away.
        CREATE TABLE IF NOT EXISTS enrollment_tokens (
            token TEXT PRIMARY KEY,
            tenant_id TEXT REFERENCES tenants(id) ON DELETE SET NULL,
            location_id TEXT REFERENCES locations(id) ON DELETE SET NULL,
            created_at INTEGER NOT NULL,
            used_at INTEGER
        );

        -- offline queue: actions run as soon as the agent is (back) online -
        -- e.g. "install software XY" or a shell command. Processed when the
        -- agent registers (sockets.py).
        CREATE TABLE IF NOT EXISTS client_queue (
            id TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            kind TEXT NOT NULL,               -- 'shell' | 'software' | ...
            label TEXT NOT NULL DEFAULT '',   -- display ("Installing 7-Zip")
            payload TEXT NOT NULL DEFAULT '{}', -- JSON depending on kind
            created_by TEXT,
            created_at INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', -- pending|running|done|failed
            result TEXT,
            finished_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_queue_client ON client_queue (client_id, status);

        -- activation codes (Settings -> Activation). No function yet: the
        -- code is only stored with its redemption date; later the RMM uses it
        -- to query the license server (premium features / add-on modules).
        -- Each redemption is its own row - the newest is the active one.
        CREATE TABLE IF NOT EXISTS activation_keys (
            id TEXT PRIMARY KEY,
            key TEXT NOT NULL,
            redeemed_at INTEGER NOT NULL,     -- Unix ms of the redemption
            redeemed_by TEXT                  -- username that redeemed it
        );

        -- package sources of the module store (like community sources in
        -- Synology DSM): a server address holding module ZIPs (directory
        -- listing or JSON manifest). Managed in Settings -> Module Store.
        CREATE TABLE IF NOT EXISTS module_sources (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL,
            added_at INTEGER NOT NULL
        );

        -- recordings of remote-screen sessions.
        -- The actual frames are stored as a file on disk (path in file_path),
        -- here only the metadata. Auto-deletion after X days (see cleanup).
        CREATE TABLE IF NOT EXISTS screen_recordings (
            id TEXT PRIMARY KEY,
            client_id TEXT,
            client_hostname TEXT,
            username TEXT,             -- who started the session
            started_at INTEGER NOT NULL,
            ended_at INTEGER,
            frame_count INTEGER NOT NULL DEFAULT 0,
            file_path TEXT NOT NULL
        );

        -- saved scripts (Scripts app): named commands with a platform tag.
        CREATE TABLE IF NOT EXISTS scripts (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            command TEXT NOT NULL,
            os TEXT NOT NULL DEFAULT 'any',   -- 'windows', 'linux', 'any'
            created_at INTEGER NOT NULL
        );

        -- groups/roles with rights. permissions is a comma-separated list of
        -- permission keys (e.g. "login,screen,terminal"). is_ad_group=1 marks
        -- groups that originate from a directory (AD).
        CREATE TABLE IF NOT EXISTS groups (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            permissions TEXT NOT NULL DEFAULT '',
            is_ad_group INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL
        );

        -- mapping user <-> group (a user can be in several groups).
        CREATE TABLE IF NOT EXISTS user_groups (
            user_id TEXT NOT NULL,
            group_id TEXT NOT NULL,
            PRIMARY KEY (user_id, group_id)
        );

        -- fine-grained permission grants (tri-state: allow/deny, else = unset).
        -- subject_type: 'user' or 'group'; scope: 'global' or a client_id.
        -- Deny always wins, 'admin' is a wildcard right (see the auth.py resolver).
        CREATE TABLE IF NOT EXISTS permission_grants (
            id TEXT PRIMARY KEY,
            subject_type TEXT NOT NULL,   -- 'user' | 'group'
            subject_id TEXT NOT NULL,
            scope TEXT NOT NULL,          -- 'global' | <client_id>
            perm TEXT NOT NULL,
            effect TEXT NOT NULL,         -- 'allow' | 'deny'
            UNIQUE(subject_type, subject_id, scope, perm)
        );
        CREATE INDEX IF NOT EXISTS idx_perm_grants_subject
            ON permission_grants (subject_type, subject_id);

        -- directory connections (realms), e.g. Active Directory via LDAP/LDAPS.
        -- Fully functional: users can sign in against these realms.
        CREATE TABLE IF NOT EXISTS realms (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            server TEXT NOT NULL,
            base_dn TEXT,
            bind_user TEXT,
            bind_password TEXT,
            port INTEGER,                         -- NULL = default (389, or 636 with SSL)
            use_ssl INTEGER NOT NULL DEFAULT 0,   -- 1 = LDAPS
            user_filter TEXT,                     -- optional additional LDAP filter
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL
        );

        -- websites "bound" to a client (quick access).
        -- Optionally with uptime monitoring: the backend calls the URL at the
        -- chosen interval and notifies depending on the notify mode.
        CREATE TABLE IF NOT EXISTS client_websites (
            id TEXT PRIMARY KEY,
            client_id TEXT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            favorite INTEGER NOT NULL DEFAULT 0,          -- 1 = pinned as a favorite
            monitor_enabled INTEGER NOT NULL DEFAULT 0,   -- 1 = uptime monitoring active
            monitor_notify TEXT NOT NULL DEFAULT 'down',  -- 'up' | 'down' | 'always'
            monitor_interval_seconds INTEGER NOT NULL DEFAULT 300, -- delay between scans
            last_status TEXT,                             -- 'up' | 'down' | NULL (never checked)
            last_checked INTEGER,                         -- timestamp (ms) of the last scan
            last_status_change INTEGER,                   -- when the status last changed (ms)
            last_error TEXT,                              -- last error message (when down)
            created_at INTEGER NOT NULL
        );

        -- webhook targets for notifications (Discord or custom).
        CREATE TABLE IF NOT EXISTS webhooks (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'custom',   -- 'discord' | 'custom'
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL
        );

        -- automations: command/script that runs on clients at fixed intervals.
        CREATE TABLE IF NOT EXISTS automations (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            command TEXT NOT NULL,
            client_ids TEXT NOT NULL DEFAULT '',   -- comma-separated client IDs
            interval_seconds INTEGER NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            last_run INTEGER,
            created_at INTEGER NOT NULL
        );

        -- automation runs: ONE entry per client per run. This lets the
        -- dashboard show a result list per run (output, error, exit code,
        -- time). run_id groups all clients of ONE run.
        CREATE TABLE IF NOT EXISTS automation_runs (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,               -- groups one run
            automation_id TEXT NOT NULL,
            client_id TEXT NOT NULL,
            client_hostname TEXT,
            started_at INTEGER NOT NULL,
            stdout TEXT,
            stderr TEXT,
            exit_code INTEGER,
            ok INTEGER NOT NULL DEFAULT 0       -- 1 = ran successfully
        );

        -- Global settings as a simple key/value store.
        -- Used among other things for the install server address and the
        -- metric/replay retention (see DEFAULT_SETTINGS further below).
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        -- WireGuard-compatible VPN tunnels. One record = one issued tunnel
        -- file for exactly one user on exactly one client.
        -- The PRIVATE key is deliberately NOT stored here: it exists only
        -- once, in the downloaded .conf file.
        -- Members of the virtual network. A member is either a CLIENT (gets a
        -- fixed address, notices nothing of it itself) or a USER tunnel. The
        -- address stays the same so one can remember and write it down.
        CREATE TABLE IF NOT EXISTS vpn_members (
            kind TEXT NOT NULL,          -- 'client' or 'user'
            ref TEXT NOT NULL,           -- client_id or tunnel_id
            address TEXT NOT NULL UNIQUE,
            label TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            PRIMARY KEY (kind, ref)
        );

        -- port forwards: a listening port on the backend points to a service
        -- of a client. Needs neither a VPN nor open ports at the customer -
        -- it runs over the existing agent connection.
        CREATE TABLE IF NOT EXISTS port_forwards (
            id TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            target_host TEXT NOT NULL DEFAULT '127.0.0.1',
            target_port INTEGER NOT NULL,
            listen_port INTEGER NOT NULL,
            username TEXT NOT NULL DEFAULT '',
            allow_from TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL DEFAULT 0,
            closed_at INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS vpn_tunnels (
            id TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            username TEXT NOT NULL DEFAULT '',
            address TEXT NOT NULL,            -- address in the tunnel network
            public_key TEXT NOT NULL,         -- public key of the user
            preshared_key TEXT NOT NULL DEFAULT '',
            allowed_ips TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL DEFAULT 0,   -- 0 = unlimited
            closed_at INTEGER NOT NULL DEFAULT 0,    -- 0 = still open
            last_handshake INTEGER NOT NULL DEFAULT 0,
            -- 'client' = only the device itself reachable
            -- 'site'   = whole network behind the node reachable
            mode TEXT NOT NULL DEFAULT 'client',
            -- 'direct' = tunnel ends on the node, 'relay' = in the backend
            transport TEXT NOT NULL DEFAULT 'relay',
            -- 1 = real LAN address via the L2 bridge of the node
            l2 INTEGER NOT NULL DEFAULT 0
        );

        -- long-term history of client metrics (persisted so the graphs do
        -- not start at 0 after a page reload).
        CREATE TABLE IF NOT EXISTS metrics_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id TEXT NOT NULL,
            ts INTEGER NOT NULL,            -- timestamp in ms
            cpu REAL NOT NULL DEFAULT 0,    -- percent
            ram REAL NOT NULL DEFAULT 0,    -- percent
            net_in REAL NOT NULL DEFAULT 0, -- bytes/s
            net_out REAL NOT NULL DEFAULT 0,-- bytes/s
            extra TEXT                      -- JSON: complete metric snapshot
                                            -- (so that Ping/Temp/Power/... ALSO
                                            --  have a history, not only cpu/ram/net)
        );
        CREATE INDEX IF NOT EXISTS idx_metrics_history_client_ts
            ON metrics_history (client_id, ts);

        -- ----------------------------------------------------------
        -- AI chat: stored API connections (URL/key/model).
        -- visibility: 'private' (creator only), 'all' (all users),
        -- 'shared' (only the users/groups listed in ai_connection_shares).
        -- The API key is NEVER delivered to non-owners - the chat always runs
        -- as a proxy via the backend.
        -- ----------------------------------------------------------
        CREATE TABLE IF NOT EXISTS ai_connections (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            api_url TEXT NOT NULL,
            api_key TEXT NOT NULL,
            model TEXT NOT NULL,
            visibility TEXT NOT NULL DEFAULT 'private',  -- 'private' | 'all' | 'shared'
            owner_user_id TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ai_connection_shares (
            connection_id TEXT NOT NULL,
            subject_type TEXT NOT NULL,   -- 'user' | 'group'
            subject_id TEXT NOT NULL,
            PRIMARY KEY (connection_id, subject_type, subject_id)
        );

        -- ----------------------------------------------------------
        -- Ticket system. Tickets are visible only to admins, the creator,
        -- directly assigned users and members of assigned groups.
        -- ----------------------------------------------------------
        CREATE TABLE IF NOT EXISTS tickets (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open',      -- 'open' | 'in_progress' | 'resolved' | 'closed'
            priority TEXT NOT NULL DEFAULT 'normal',  -- 'low' | 'normal' | 'high' | 'critical'
            created_by TEXT NOT NULL,                 -- username
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            due_date INTEGER,                         -- due date (ms) or NULL
            resolved_at INTEGER,
            resolved_by TEXT
        );
        CREATE TABLE IF NOT EXISTS ticket_assignees (
            ticket_id TEXT NOT NULL,
            subject_type TEXT NOT NULL,   -- 'user' | 'group'
            subject_id TEXT NOT NULL,
            PRIMARY KEY (ticket_id, subject_type, subject_id)
        );
        CREATE TABLE IF NOT EXISTS ticket_clients (
            ticket_id TEXT NOT NULL,
            client_id TEXT NOT NULL,
            PRIMARY KEY (ticket_id, client_id)
        );
        CREATE TABLE IF NOT EXISTS ticket_comments (
            id TEXT PRIMARY KEY,
            ticket_id TEXT NOT NULL,
            author TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ticket_files (
            id TEXT PRIMARY KEY,
            ticket_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            size INTEGER NOT NULL DEFAULT 0,
            mime TEXT,
            uploaded_by TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        """
    )
    _conn.commit()

    # Migration: add columns for VM/LXC marking if the clients table still
    # comes from an older version.
    _migrate_add_column("clients", "device_type", "TEXT DEFAULT 'physical'")  # 'physical' | 'vm' | 'lxc'
    # "Update all agents" with an offline option: the flag remembers that the
    # client should receive an agent update as soon as it is back online.
    _migrate_add_column("clients", "pending_agent_update", "INTEGER DEFAULT 0")
    # Silent mode for the remote screen (one-off, see sockets.py) and
    # server-side stored UI settings (layouts, favorites, ...), so they are the
    # same in EVERY browser (not per browser/localStorage).
    _migrate_add_column("users", "silent_screen", "INTEGER DEFAULT 0")
    _migrate_add_column("users", "ui_prefs", "TEXT")
    _migrate_add_column("users", "email", "TEXT")
    # UI design per user: '' = installation default (setting "default_design"),
    # 'futuristic' = desktop with windows, 'standard' = classic (fixed
    # navigation on the left, content as tabs).
    _migrate_add_column("users", "design", "TEXT DEFAULT ''")
    # Client websites: open in the internal browser window or an external tab.
    _migrate_add_column("client_websites", "open_mode", "TEXT DEFAULT 'external'")
    _migrate_add_column("clients", "agent_version", "TEXT")  # reported agent version (for the "outdated" hint)
    # Automatic agent update per client: 'global' = follows the setting in
    # Settings, 'on' = always, 'off' = never.
    _migrate_add_column("clients", "auto_update", "TEXT NOT NULL DEFAULT 'global'")
    # Auto-detection of the device type: the agent reports vm/lxc - but this
    # is applied only after CONFIRMATION by the user in the client panel.
    _migrate_add_column("clients", "detected_device_type", "TEXT")
    _migrate_add_column("clients", "device_type_ack", "INTEGER NOT NULL DEFAULT 0")
    # Device icon: the agent reports its kind (laptop/pc/server/hypervisor/vm/lxc/docker),
    # icon_type is the manual choice in the edit dialog ('' = automatic). Same icons as
    # the topology map (frontend js/app/deviceicons.js).
    _migrate_add_column("clients", "detected_kind", "TEXT")
    _migrate_add_column("clients", "icon_type", "TEXT NOT NULL DEFAULT ''")
    # --- NODE TIER --------------------------------------------------
    # A normal client reports metrics and runs commands. A NODE can
    # additionally serve as a bridgehead into the network: its own VPN
    # endpoint, reverse proxy, optionally an L2 bridge. The add-on modules
    # needed for that are loaded EXCLUSIVELY on nodes - an ordinary client
    # sees nothing of it.
    # The IPv4 networks the client is attached to (JSON list, reported by the
    # agent). Basis for site-to-site routes - previously /24 was assumed
    # across the board, which produced wrong routes with /16 or /22.
    _migrate_add_column("clients", "lan_subnets", "TEXT")
    _migrate_add_column("clients", "is_node", "INTEGER NOT NULL DEFAULT 0")
    # Self-report of the node: which modules run, which UDP port, whether the
    # L2 bridge (driver) is available. JSON, informational only.
    _migrate_add_column("clients", "node_caps", "TEXT")
    # Publicly reachable UDP address of the node ("host:port"), determined by
    # the backend from the node's probe packet. Empty = reachable only via the
    # backend (relay operation).
    _migrate_add_column("clients", "node_endpoint", "TEXT")
    # Time of the last successful reachability check (ms).
    _migrate_add_column("clients", "node_endpoint_checked", "INTEGER NOT NULL DEFAULT 0")
    # VPN tunnels: add operating mode, transport path and L2 bridge.
    _migrate_add_column("vpn_tunnels", "mode", "TEXT NOT NULL DEFAULT 'client'")
    _migrate_add_column("vpn_tunnels", "transport", "TEXT NOT NULL DEFAULT 'relay'")
    _migrate_add_column("vpn_tunnels", "l2", "INTEGER NOT NULL DEFAULT 0")

    # Nodes (new): a separate process on the client, independent of the agent.
    # See app/node_manager.py. The old columns clients.is_node/node_* are
    # legacy of the earlier node concept and are no longer used.
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS nodes (
            client_id   TEXT PRIMARY KEY,
            token_hash  TEXT NOT NULL,
            state       TEXT NOT NULL DEFAULT 'installing',  -- installing|installed|removing|error
            error       TEXT,
            version     TEXT,
            info        TEXT,       -- JSON: operating system, Docker state ...
            modules     TEXT,       -- JSON: node modules installed on the node
            created_at  INTEGER NOT NULL DEFAULT 0,
            last_seen   INTEGER NOT NULL DEFAULT 0
        )""")
    _conn.commit()
    _migrate_add_column("enrollment_tokens", "client_name", "TEXT")  # optional desired name during onboarding
    _migrate_add_column("users", "accent", "TEXT DEFAULT 'teal'")  # personal color palette
    _migrate_add_column("users", "auth_realm", "TEXT")  # NULL = local user, else realm ID (AD)
    _migrate_add_column("users", "relay_password", "TEXT")  # (old, unused) - former one-time password
    _migrate_add_column("users", "relay_ha1", "TEXT")  # digest HA1 = MD5(user:realm:account-password), set at login
    _migrate_add_column("clients", "relay_enabled", "INTEGER NOT NULL DEFAULT 0")  # 1 = released in the explorer relay
    # Automatic closing of the relay: Unix time in ms at which the release is
    # lifted automatically. 0 = never (stays permanently open).
    _migrate_add_column("clients", "relay_expires_at", "INTEGER NOT NULL DEFAULT 0")
    # Favorite working folder per client (appears in the web explorer in the
    # drive/root view, e.g. /var/www on a web host). '' = none.
    _migrate_add_column("clients", "fav_dir", "TEXT NOT NULL DEFAULT ''")
    # AD groups that have nothing to do with the RMM can be marked as
    # "unmanaged": they land in a folder that is collapsed by default in the
    # rights/group selection and are not assigned to AD users.
    _migrate_add_column("groups", "unmanaged", "INTEGER NOT NULL DEFAULT 0")
    # Default groups: '' = normal group, 'user' = every new user is added
    # automatically, 'admin' = every new administrator.
    _migrate_add_column("groups", "auto_assign", "TEXT NOT NULL DEFAULT ''")
    _migrate_add_column("screen_recordings", "format", "TEXT NOT NULL DEFAULT 'frames'")  # 'frames' (old) | 'video' (client recording)
    _migrate_add_column("scripts", "folder", "TEXT NOT NULL DEFAULT ''")  # folder name for the Scripts app ('' = no folder)
    _migrate_add_column("metrics_history", "extra", "TEXT")  # JSON snapshot of all metrics (history for Ping/Temp/Power/...)
    # Warranty end per client (Unix ms, NULL = no warranty stored).
    _migrate_add_column("clients", "warranty_until", "INTEGER")
    # "Essential" device: 1 = critical - if it goes offline, the sidebar shows
    # the device icon RED (instead of gray). Set in the client edit dialog.
    _migrate_add_column("clients", "essential", "INTEGER NOT NULL DEFAULT 0")
    # Short name (shown in sidebar/topology/inventory instead of the hostname)
    # and free tags (comma-separated, e.g. "production,hall2,printer").
    _migrate_add_column("clients", "short_name", "TEXT NOT NULL DEFAULT ''")
    _migrate_add_column("clients", "tags", "TEXT NOT NULL DEFAULT ''")
    # Maintenance mode (Core): end as Unix ms, 0 = "until I turn it off
    # myself". It is active as long as status_override = 'maintenance' (see
    # client_maintenance()). During maintenance alarms stay silent, and
    # protection mechanisms (USB lock, patch windows ...) are suspended.
    _migrate_add_column("clients", "maintenance_until", "INTEGER NOT NULL DEFAULT 0")
    # Ticket comments: visibility like the notes ('all' | 'private' | 'custom')
    # and the user ID of the author (previously only the display name).
    _migrate_add_column("ticket_comments", "visibility", "TEXT DEFAULT 'all'")
    _migrate_add_column("ticket_comments", "author_id", "TEXT")

    # Custom webhooks: custom HTTP headers (JSON object as text) and a freely
    # definable body template with placeholders ({head}, {body}, ...).
    _migrate_add_column("webhooks", "headers", "TEXT")
    _migrate_add_column("webhooks", "body_template", "TEXT")

    # ------------------------------------------------------------------
    # Chat (WhatsApp-like): direct messages + groups with group admins.
    # Notification rules: which trigger fires which channel.
    # ------------------------------------------------------------------
    _conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS chat_conversations (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL DEFAULT 'dm',        -- 'dm' | 'group'
            name TEXT,                              -- group name (NULL for dm)
            created_by TEXT,                        -- user_id of the creator
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chat_members (
            conversation_id TEXT NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,    -- group admin (irrelevant for dm)
            joined_at INTEGER NOT NULL,
            last_read_at INTEGER NOT NULL DEFAULT 0,-- for the unread counter
            PRIMARY KEY (conversation_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS chat_messages (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
            sender_id TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_chat_messages_conv
            ON chat_messages(conversation_id, created_at);

        -- ----------------------------------------------------------
        -- Client notes as INDIVIDUAL entries with visibility:
        --   'all'     -> visible to everyone allowed to see the client
        --   'private' -> only for the author
        --   'custom'  -> author + selected users (note_shares)
        -- The old free-text field clients.notes stays and is taken over once
        -- as an entry with visibility 'all'.
        -- ----------------------------------------------------------
        CREATE TABLE IF NOT EXISTS client_notes (
            id TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            author_id TEXT,
            author_name TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL,
            visibility TEXT NOT NULL DEFAULT 'all',
            pinned INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_client_notes_client
            ON client_notes(client_id, created_at);
        CREATE TABLE IF NOT EXISTS note_shares (
            note_id TEXT NOT NULL REFERENCES client_notes(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL,
            PRIMARY KEY (note_id, user_id)
        );

        -- Same visibility logic for ticket comments.
        CREATE TABLE IF NOT EXISTS ticket_comment_shares (
            comment_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            PRIMARY KEY (comment_id, user_id)
        );

        -- ----------------------------------------------------------
        -- Activity log for notes and tickets.
        -- entity_type: 'client_notes' | 'ticket'
        -- entity_id:   client_id      | ticket_id
        -- action:      e.g. 'note.created', 'ticket.status', 'comment.created'
        -- ----------------------------------------------------------
        CREATE TABLE IF NOT EXISTS activity_log (
            id TEXT PRIMARY KEY,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            actor_id TEXT,
            actor_name TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_activity_entity
            ON activity_log(entity_type, entity_id, created_at);

        -- ----------------------------------------------------------
        -- Media library of the audio player (media hub):
        --   kind 'local'   -> uploaded MP3/MP4 (file under media_files/)
        --   kind 'youtube' -> saved YouTube link (video or playlist)
        --   kind 'spotify' -> saved Spotify link
        --   kind 'radio'   -> custom stream/station
        -- shared = 1: visible to all users, otherwise only to the owner.
        -- ----------------------------------------------------------
        CREATE TABLE IF NOT EXISTS media_items (
            id TEXT PRIMARY KEY,
            owner_id TEXT,
            owner_name TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            subtitle TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL DEFAULT '',
            filename TEXT,
            size INTEGER NOT NULL DEFAULT 0,
            mime TEXT,
            shared INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_media_owner
            ON media_items(owner_id, kind, created_at);

        -- playlists: bundle arbitrary library entries - radio stations,
        -- uploaded files, YouTube and Spotify links.
        CREATE TABLE IF NOT EXISTS media_playlists (
            id TEXT PRIMARY KEY,
            owner_id TEXT,
            owner_name TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            shared INTEGER NOT NULL DEFAULT 0,
            favorite INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL
        );
        -- mapping list -> entry. "position" keeps the order.
        CREATE TABLE IF NOT EXISTS media_playlist_items (
            playlist_id TEXT NOT NULL,
            item_id TEXT NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (playlist_id, item_id)
        );
        CREATE INDEX IF NOT EXISTS idx_media_pl_items
            ON media_playlist_items(playlist_id, position);

        -- ----------------------------------------------------------
        -- ORGANIZATION HIERARCHY
        -- Each node (user OR group) can have EXACTLY ONE superior - also a user
        -- or a group. This yields a tree: "who reports to whom?".
        -- Superiors may see the calendars of their subordinates and enter
        -- appointments there (see calendar_routes).
        -- ----------------------------------------------------------
        CREATE TABLE IF NOT EXISTS org_hierarchy (
            child_type TEXT NOT NULL,      -- 'user' | 'group'
            child_id TEXT NOT NULL,
            parent_type TEXT,              -- NULL = top level
            parent_id TEXT,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (child_type, child_id)
        );
        CREATE INDEX IF NOT EXISTS idx_org_parent
            ON org_hierarchy(parent_type, parent_id);

        -- ----------------------------------------------------------
        -- CALENDAR
        -- An appointment can target users, groups AND clients
        -- (calendar_targets). importance: low | normal | high | critical
        -- ----------------------------------------------------------
        CREATE TABLE IF NOT EXISTS calendar_events (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            start_at INTEGER NOT NULL,     -- Unix ms
            end_at INTEGER NOT NULL,       -- Unix ms
            all_day INTEGER NOT NULL DEFAULT 0,
            importance TEXT NOT NULL DEFAULT 'normal',
            created_by TEXT,
            created_by_name TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_cal_start ON calendar_events(start_at);
        CREATE TABLE IF NOT EXISTS calendar_targets (
            event_id TEXT NOT NULL REFERENCES calendar_events(id) ON DELETE CASCADE,
            target_type TEXT NOT NULL,     -- 'user' | 'group' | 'client'
            target_id TEXT NOT NULL,
            PRIMARY KEY (event_id, target_type, target_id)
        );
        CREATE INDEX IF NOT EXISTS idx_cal_target
            ON calendar_targets(target_type, target_id);

        -- notification rules: "If <trigger> (on clients X/Y/Z), then
        -- <channel> to <target>". params = JSON (e.g. days_before,
        -- threshold). last_fired = JSON {key: ts} for cooldown/dedupe.
        CREATE TABLE IF NOT EXISTS notification_rules (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            trigger TEXT NOT NULL,
            client_ids TEXT NOT NULL DEFAULT '',    -- comma-separated, '' = all clients
            channel TEXT NOT NULL,                  -- 'email' | 'webhook' | 'dashboard'
            target TEXT NOT NULL DEFAULT '',        -- email address(es) or webhook_id
            params TEXT NOT NULL DEFAULT '{}',
            last_fired TEXT NOT NULL DEFAULT '{}',
            created_at INTEGER NOT NULL
        );

        -- ----------------------------------------------------------
        -- TODOS (personal task list, "mini Notion")
        -- Strictly private: every record hangs on user_id, there are no
        -- shares. Categories are the importance columns; four of them are
        -- created automatically per user (builtin: high | mid | low |
        -- archive), everyone can create more themselves.
        -- rank = manual ordering, smaller = further up/front.
        -- ----------------------------------------------------------
        CREATE TABLE IF NOT EXISTS todo_categories (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            color TEXT NOT NULL DEFAULT '#38bdf8',
            rank INTEGER NOT NULL DEFAULT 0,
            builtin TEXT NOT NULL DEFAULT '',   -- '' = self-created
            created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_todo_cat_user
            ON todo_categories(user_id, rank);

        CREATE TABLE IF NOT EXISTS todos (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            category_id TEXT NOT NULL,
            prev_category_id TEXT NOT NULL DEFAULT '',  -- category before archiving
            title TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            done INTEGER NOT NULL DEFAULT 0,
            done_at INTEGER,
            recurring INTEGER NOT NULL DEFAULT 0,       -- 1 = re-check daily
            last_reset TEXT NOT NULL DEFAULT '',        -- 'YYYY-MM-DD' of the last daily reset
            last_done_day TEXT NOT NULL DEFAULT '',     -- 'YYYY-MM-DD' of the last check-off
            streak INTEGER NOT NULL DEFAULT 0,          -- streak of completed days
            due_at INTEGER,
            rank INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_todos_user
            ON todos(user_id, category_id, done, rank);

        -- ----------------------------------------------------------
        -- GDPR: erasure requests of data subjects (Art. 17).
        -- Not executed immediately - Art. 17 (3) has exceptions, so an admin
        -- reviews it. status: open | done | rejected
        -- ----------------------------------------------------------
        CREATE TABLE IF NOT EXISTS erasure_requests (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            username TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'account',   -- 'account' | 'content'
            reason TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open',
            note TEXT NOT NULL DEFAULT '',          -- reason for the decision
            created_at INTEGER NOT NULL,
            handled_at INTEGER,
            handled_by TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_erasure_status
            ON erasure_requests(status, created_at);

        -- ----------------------------------------------------------
        -- SOFTWARE PATCHING
        -- patches = inventory per client from the last scan. Replaced for the
        -- client on every scan, not appended - otherwise long-installed
        -- updates stay in the list forever.
        -- level: security | critical | important | moderate | low | feature | other
        -- source: windows-update | winget | apt | dnf
        -- ----------------------------------------------------------
        CREATE TABLE IF NOT EXISTS patches (
            id TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            uid TEXT NOT NULL,              -- identifier in the package manager
            name TEXT NOT NULL,
            current_version TEXT NOT NULL DEFAULT '',
            available_version TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            level TEXT NOT NULL DEFAULT 'other',
            size INTEGER NOT NULL DEFAULT 0,
            needs_reboot INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',  -- pending | installing | installed | failed | excluded
            error TEXT NOT NULL DEFAULT '',
            found_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_patches_client
            ON patches(client_id, status, level);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_patches_unique
            ON patches(client_id, source, uid);

        -- auto-patch rules. scope='global' applies to all clients set to
        -- 'global'; scope='client' with client_id overrides the global rule.
        -- Same logic as the agent auto-update.
        CREATE TABLE IF NOT EXISTS patch_rules (
            id TEXT PRIMARY KEY,
            scope TEXT NOT NULL DEFAULT 'global',   -- 'global' | 'client'
            client_id TEXT,
            enabled INTEGER NOT NULL DEFAULT 0,
            levels TEXT NOT NULL DEFAULT 'security,critical',  -- comma-separated
            sources TEXT NOT NULL DEFAULT '',       -- empty = all sources
            window_start TEXT NOT NULL DEFAULT '02:00',
            window_end TEXT NOT NULL DEFAULT '05:00',
            weekdays TEXT NOT NULL DEFAULT '1,2,3,4,5,6,7',    -- 1=Mon
            auto_reboot INTEGER NOT NULL DEFAULT 0,
            exclusions TEXT NOT NULL DEFAULT '',    -- comma-separated uids
            scan_interval_hours INTEGER NOT NULL DEFAULT 24,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_patch_rules_client
            ON patch_rules(scope, COALESCE(client_id, ''));

        -- history: what was installed when, by whom (or automatically).
        CREATE TABLE IF NOT EXISTS patch_runs (
            id TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            trigger TEXT NOT NULL DEFAULT 'manual',  -- 'manual' | 'auto'
            actor TEXT NOT NULL DEFAULT '',
            requested INTEGER NOT NULL DEFAULT 0,
            installed INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0,
            needs_reboot INTEGER NOT NULL DEFAULT 0,
            detail TEXT NOT NULL DEFAULT '',
            started_at INTEGER NOT NULL,
            finished_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_patch_runs_client
            ON patch_runs(client_id, started_at);

        -- hosts whose certificate a user explicitly accepted in the
        -- internal browser. Deliberately PER USER: an exception one user sets
        -- must not silently apply to everyone else. 'reason' records WHAT was
        -- wrong with the certificate, so it stays traceable later what the
        -- exception was once meant for.
        CREATE TABLE IF NOT EXISTS webproxy_trusted_hosts (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            host TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_webproxy_trust
            ON webproxy_trusted_hosts(username, host);
        """
    )
    _conn.commit()

    # Shares ("for specific") also apply to GROUPS. The user_id column still
    # carries the ID - subject_type says whether user or group.
    # MUST come AFTER the CREATE TABLE script - further above the tables
    # note_shares/ticket_comment_shares do not yet exist.
    # Workspace / department of a user (free text).
    _migrate_add_column("users", "workspace", "TEXT")
    _migrate_add_column("erasure_requests", "note", "TEXT NOT NULL DEFAULT ''")
    # Patch summary directly on the client: list_clients() does SELECT *, so
    # the values are available in every dashboard widget without an extra query.
    _migrate_add_column("clients", "patch_policy", "TEXT NOT NULL DEFAULT 'global'")
    _migrate_add_column("clients", "patch_count", "INTEGER NOT NULL DEFAULT 0")
    _migrate_add_column("clients", "patch_security", "INTEGER NOT NULL DEFAULT 0")
    _migrate_add_column("clients", "patch_critical", "INTEGER NOT NULL DEFAULT 0")
    _migrate_add_column("clients", "patch_reboot", "INTEGER NOT NULL DEFAULT 0")
    _migrate_add_column("clients", "patch_last_scan", "INTEGER")
    _migrate_add_column("note_shares", "subject_type", "TEXT NOT NULL DEFAULT 'user'")
    _migrate_add_column("ticket_comment_shares", "subject_type", "TEXT NOT NULL DEFAULT 'user'")
    _conn.commit()

    # Migration: extend realms with port, SSL (LDAPS) and an optional
    # additional user filter (for production AD connections).
    _migrate_add_column("realms", "port", "INTEGER")                 # NULL = default (389 / 636 with SSL)
    _migrate_add_column("realms", "use_ssl", "INTEGER NOT NULL DEFAULT 0")  # 1 = LDAPS
    _migrate_add_column("realms", "user_filter", "TEXT")            # optional additional LDAP filter

    # Audio player: favorite marking on library entries.
    _migrate_add_column("media_items", "favorite", "INTEGER NOT NULL DEFAULT 0")

    # Two-factor login (TOTP). The secret is generated during setup and armed
    # only after a successful check (totp_enabled=1) - otherwise one could lock
    # oneself out if the app does not deliver the code.
    _migrate_add_column("users", "totp_secret", "TEXT")
    _migrate_add_column("users", "totp_enabled", "INTEGER NOT NULL DEFAULT 0")
    _migrate_add_column("users", "totp_backup", "TEXT")   # hashes only

    # Migration of metric settings: whoever stored the OLD defaults (60 s
    # interval / 1 h retention) unchanged is lifted to the new values (10 s /
    # unlimited). Deliberately differing values are kept.
    _conn.execute("DELETE FROM settings WHERE key = 'metrics_interval_seconds' AND value = '60'")
    _conn.execute("DELETE FROM settings WHERE key = 'metrics_retention_hours' AND value = '1'")

    # If no user exists yet: create the default login admin/admin, with
    # must_change_pw=1, so a new password MUST be set at the first login.
    if _conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 0:
        from app.auth import hash_password  # imported locally to avoid a circular import

        _conn.execute(
            """
            INSERT INTO users (id, username, password_hash, display_name, role, must_change_pw, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (_new_id(), "admin", hash_password("admin"), "Administrator", "admin", 1, _now_ms()),
        )
        _conn.commit()
        print("[db] default login created: username 'admin', password 'admin' (change required at first login)")

    # Create the default groups (All users / All administrators) once and
    # backfill existing accounts.
    _seed_default_groups()

    # Seed the default scripts (agent update, app updates) once.
    _seed_default_scripts()

    # One-off: English becomes the default language (server texts, scripts,
    # UI). Existing accounts are switched over; whoever wants German selects it
    # afterwards in the profile (or server_language for server texts).
    if not get_setting("lang_default_en_done"):
        set_setting("server_language", "en")
        _conn.execute("UPDATE users SET language = 'en' WHERE language = 'de' OR language = '' OR language IS NULL")
        set_setting("lang_default_en_done", "1")
        _conn.commit()
        print("[db] default language switched to English (one-off).")


# ------------------------------------------------------------------
# DEFAULT GROUPS
# ------------------------------------------------------------------
# Two groups into which new accounts are added automatically:
#   "Alle Benutzer"          -> every account
#   "Alle Administratoren"   -> every account with the role 'admin'
#
# They are perfectly normal groups: rights can be adjusted freely in the
# rights management, and they may be deleted - the seed runs only ONCE
# (settings flag 'default_groups_seeded'), so they do not reappear.
#
# The starting rights are deliberately conservative: login, own dashboard,
# profile name, personal tools. NOTHING that reveals other people's devices
# or data ('access_clients' is intentionally missing).
DEFAULT_GROUP_USER = "Alle Benutzer"
DEFAULT_GROUP_ADMIN = "Alle Administratoren"
DEFAULT_GROUP_SUPPORT = "Alle Support"

_DEFAULT_USER_PERMS = [
    "login", "see_dashboard", "restore_session", "customize_dashboard",
    "edit_profile_name", "use_todos", "use_calendar",
    # Tickets: EVERYONE must be able to open and read a ticket. Otherwise the
    # "rights missing -> create a ticket" path leads nowhere: exactly the
    # people who lack a right could then not even report it.
    "ticket_read", "ticket_create", "ticket_comment",
    # module "spaces" (since seed 3): own space + public spaces
    "get_user_space", "use_public_space",
]
# The wildcard right suffices for the admin group: it covers all others.
_DEFAULT_ADMIN_PERMS = ["admin"]

# Support: works on the tickets that arise from missing rights. Deliberately
# WITHOUT 'access_clients' - support should be able to work on tickets, not
# automatically see all devices. That is granted additionally if needed.
_DEFAULT_SUPPORT_PERMS = _DEFAULT_USER_PERMS + [
    "ticket_edit", "ticket_assign", "ticket_resolve", "see_org",
]

# Seed version: on an increase, missing groups are created and EXACTLY the
# rights newly added in this version are backfilled. Deliberately not the
# complete list - otherwise an administrator would get back rights they
# deliberately removed from the group.
_DEFAULT_GROUPS_SEED = "3"
_SEED_ADD_V2 = {
    DEFAULT_GROUP_USER: ["ticket_read", "ticket_create", "ticket_comment"],
    DEFAULT_GROUP_ADMIN: [],
    # The support group is NEW in this version - it is created completely.
    DEFAULT_GROUP_SUPPORT: _DEFAULT_SUPPORT_PERMS,
}
# Seed 3: module "spaces" - normal users (and support) may use their own
# space and the public spaces.
_SEED_ADD_V3 = {
    DEFAULT_GROUP_USER: ["get_user_space", "use_public_space"],
    DEFAULT_GROUP_ADMIN: [],
    DEFAULT_GROUP_SUPPORT: ["get_user_space", "use_public_space"],
}
# Version -> rights to backfill. On an update all versions AFTER the stored
# one are applied.
_SEED_ADDS = {"2": _SEED_ADD_V2, "3": _SEED_ADD_V3}


def _seed_default_groups() -> None:
    # Self-healing instead of "once and never again": if the seed was already
    # set earlier but the groups are not (or no longer) there - e.g. because an
    # older version never created them, or they were lost on a database switch
    # - they are created again. Only if both groups exist does everything stay
    # untouched (then they may also be deleted without coming back... see below).
    version = get_setting("default_groups_seeded") or ""
    wanted = (
        (DEFAULT_GROUP_USER, _DEFAULT_USER_PERMS, "user"),
        (DEFAULT_GROUP_ADMIN, _DEFAULT_ADMIN_PERMS, "admin"),
        (DEFAULT_GROUP_SUPPORT, _DEFAULT_SUPPORT_PERMS, "support"),
    )
    if version == _DEFAULT_GROUPS_SEED:
        _backfill_default_groups()
        return
    if version:
        # Older version: only EXTEND. Existing groups keep their rights; only
        # missing permissions are added and missing groups are recreated.
        # Whoever deliberately deleted a group does not get it back - except
        # the support group, which did not exist before at all.
        for name, perms, kind in wanted:
            grp = get_group_by_name(name)
            fresh = False
            if not grp:
                if name != DEFAULT_GROUP_SUPPORT:
                    continue
                grp = create_group(name, [p for p in perms if p in ALL_PERMISSIONS])
                fresh = True
            _conn.execute("UPDATE groups SET auto_assign = ? WHERE id = ?", (kind, grp["id"]))
            wish = perms if fresh else [p for v, adds in _SEED_ADDS.items() if v > version
                                        for p in adds.get(name, [])]
            have = {g["perm"] for g in get_grants("group", grp["id"])
                    if g.get("scope") == "global"}
            add = [{"scope": "global", "perm": p, "effect": "allow"}
                   for p in wish if p not in have]
            if add:
                set_grants("group", grp["id"],
                           get_grants("group", grp["id"]) + add)
        _conn.commit()
        set_setting("default_groups_seeded", _DEFAULT_GROUPS_SEED)
        print("[db] default groups extended (incl. '%s')" % DEFAULT_GROUP_SUPPORT)
        _backfill_default_groups()
        return
    for name, perms, kind in wanted:
        grp = get_group_by_name(name)
        # Also fill the old rights column: some checks still go through
        # get_user_permissions() instead of the grants.
        legacy = [p for p in perms if p in ALL_PERMISSIONS]
        if not grp:
            grp = create_group(name, legacy)
        else:
            update_group(grp["id"], name, legacy)
        _conn.execute("UPDATE groups SET auto_assign = ? WHERE id = ?", (kind, grp["id"]))
        set_grants("group", grp["id"],
                   [{"scope": "global", "perm": p, "effect": "allow"} for p in perms])
    _conn.commit()
    set_setting("default_groups_seeded", _DEFAULT_GROUPS_SEED)
    print(f"[db] default groups created: '{DEFAULT_GROUP_USER}', "
          f"'{DEFAULT_GROUP_ADMIN}', '{DEFAULT_GROUP_SUPPORT}'")
    _backfill_default_groups()


def auto_assign_groups(user_id: str, role: str) -> None:
    """Adds a user to all matching default groups."""
    try:
        rows = _conn.execute(
            "SELECT id, auto_assign FROM groups WHERE auto_assign IS NOT NULL "
            "AND auto_assign != ''").fetchall()
    except Exception:
        return
    for r in rows:
        kind = (r["auto_assign"] or "").strip()
        # "user" applies to ALL roles, "admin"/"support"/"api" only to the
        # respective one ("api" = template "API user", the module "api" creates
        # the group).
        if kind == "user" or (kind == role and role in ("admin", "support", "api")):
            _conn.execute(
                "INSERT OR IGNORE INTO user_groups (user_id, group_id) VALUES (?, ?)",
                (user_id, r["id"]))
    _conn.commit()


def _backfill_default_groups() -> None:
    """Backfill existing accounts - idempotent (INSERT OR IGNORE)."""
    try:
        users = _conn.execute("SELECT id, role FROM users").fetchall()
    except Exception:
        return
    for u in users:
        auto_assign_groups(u["id"], u["role"])


def _seed_default_scripts() -> None:
    """
    Creates the bundled default scripts ONCE (settings flag
    'default_scripts_seeded'). This keeps them deletable/editable without
    reappearing on every backend start.

    Contains:
      - Agent Update (Linux)   -> official /agent-dist/update.sh, detached
      - Agent Update (Windows) -> SYSTEM maintenance task (event 812), fallback update.ps1
      - Update Windows apps (winget upgrade --all)
      - Update Linux packages (apt/dnf/yum/pacman/zypper)
    """
    # Versioned seed: "2" = current state (with folders + ping script).
    # Existing installations with value "1" get the folders backfilled and
    # missing scripts added; deleted scripts stay deleted.
    row = _conn.execute(
        "SELECT value FROM settings WHERE key = 'default_scripts_seeded'"
    ).fetchone()
    seed_version = row["value"] if row else None
    if seed_version == "2":
        return

    # (name, os, folder, command)
    defaults = [
        (
            "Agent Update (Linux)",
            "linux",
            "Linux",
            "# Starts the official agent update. IMPORTANT: detached from the\n"
            "# agent process (systemd-run), because the update stops the agent\n"
            "# service - otherwise the update would kill itself along with it.\n"
            "BACKEND_URL=$(sed -n 's/^BACKEND_URL=//p' /opt/bytera-rmm-agent/.env | tr -d '\\r')\n"
            "if [ -z \"$BACKEND_URL\" ]; then echo 'BACKEND_URL not found (/opt/bytera-rmm-agent/.env)'; exit 1; fi\n"
            "if command -v systemd-run >/dev/null 2>&1; then\n"
            "  systemd-run --collect --quiet bash -c \"curl -fsSL \\\"$BACKEND_URL/agent-dist/update.sh\\\" | bash >/tmp/bytera-agent-update.log 2>&1\"\n"
            "else\n"
            "  nohup bash -c \"curl -fsSL \\\"$BACKEND_URL/agent-dist/update.sh\\\" | bash >/tmp/bytera-agent-update.log 2>&1\" >/dev/null 2>&1 &\n"
            "fi\n"
            "echo \"Agent update started (log: /tmp/bytera-agent-update.log)\"",
        ),
        (
            "Agent Update (Windows)",
            "windows",
            "Windows",
            "REM Triggers the official agent update. Preferably via the pre-\n"
            "REM installed SYSTEM maintenance task (event 812, elevated). Fallback:\n"
            "REM start update.ps1 detached (log: %TEMP%\\bytera-agent-update.log).\n"
            "powershell -NoProfile -ExecutionPolicy Bypass -Command \"$ok=$false; try { Write-EventLog -LogName Application -Source 'ByteraRMM' -EventId 812 -EntryType Information -Message 'rmm update'; $ok=$true; Write-Host 'Agent update triggered via SYSTEM task (event 812).' } catch {}; if (-not $ok) { $b=(Select-String -Path 'C:\\Program Files\\ByteraRmmAgent\\.env' -Pattern '^BACKEND_URL=(.+)$').Matches[0].Groups[1].Value.Trim(); $cmd='iwr ' + $b + '/agent-dist/update.ps1 -UseBasicParsing | iex > $env:TEMP\\bytera-agent-update.log 2>&1'; Start-Process powershell -WindowStyle Hidden -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-Command',$cmd); Write-Host 'Agent update started (update.ps1).' }\"",
        ),
        (
            "Update Windows apps (winget)",
            "windows",
            "Windows",
            "REM Updates ALL programs managed by winget unattended.\n"
            "winget upgrade --all --silent --disable-interactivity --accept-source-agreements --accept-package-agreements --include-unknown",
        ),
        (
            "Update Linux packages",
            "linux",
            "Linux",
            "# Updates all system packages - detects the package manager automatically.\n"
            "export DEBIAN_FRONTEND=noninteractive\n"
            "if command -v apt-get >/dev/null 2>&1; then\n"
            "  apt-get update && apt-get upgrade -y\n"
            "elif command -v dnf >/dev/null 2>&1; then\n"
            "  dnf upgrade -y\n"
            "elif command -v yum >/dev/null 2>&1; then\n"
            "  yum update -y\n"
            "elif command -v pacman >/dev/null 2>&1; then\n"
            "  pacman -Syu --noconfirm\n"
            "elif command -v zypper >/dev/null 2>&1; then\n"
            "  zypper --non-interactive update\n"
            "else\n"
            "  echo 'No known package manager found'; exit 1\n"
            "fi",
        ),
    ]

    defaults.append((
        "Ping test (1.1.1.1)",
        "any",
        "Test",
        # Runs on BOTH platforms: Linux takes -c 4; on Windows -c fails and the
        # second call with -n 4 takes effect.
        "ping -c 4 1.1.1.1 || ping -n 4 1.1.1.1",
    ))

    existing = {r["name"]: r for r in _conn.execute("SELECT * FROM scripts").fetchall()}
    created = updated = 0
    for name, os_target, folder, command in defaults:
        if name in existing:
            # Upgrade from seed version 1: add the folder if the user has not
            # yet sorted the script themselves.
            if seed_version == "1" and not (existing[name]["folder"] or "").strip():
                _conn.execute("UPDATE scripts SET folder = ? WHERE id = ?",
                              (folder, existing[name]["id"]))
                updated += 1
            continue
        if seed_version == "1" and name != "Ping test (1.1.1.1)":
            continue   # on upgrade only add the NEW script (deleted ones stay gone)
        _conn.execute(
            "INSERT INTO scripts (id, name, command, os, folder, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (_new_id(), name, command, os_target, folder, _now_ms()),
        )
        created += 1
    _conn.execute(
        "INSERT INTO settings (key, value) VALUES ('default_scripts_seeded', '2') "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
    )
    _conn.commit()
    if created or updated:
        print(f"[db] default scripts: {created} created, {updated} sorted into folders")


# ------------------------------------------------------------------
# USERS
# ------------------------------------------------------------------

def get_user_by_username(username: str) -> dict | None:
    row = _conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: str) -> dict | None:
    row = _conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def list_users() -> list[dict]:
    rows = _conn.execute("SELECT * FROM users ORDER BY username").fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------
# Explorer relay: login + release per client
# ------------------------------------------------------------------
# For digest authentication (so Windows can sign in over HTTP WITHOUT a
# registry change) the server needs the HA1 = MD5(user:realm:password). Since
# we store passwords only as bcrypt, we compute the HA1 ONCE during the normal
# dashboard login (where the plaintext password is available) and store it.
# This way the user can sign in to the network drive with their PERFECTLY
# NORMAL account password.
RELAY_REALM = "BYTERA RMM Relay"


def _relay_fernet():
    """Fernet instance whose key is derived from the JWT_SECRET. Used to
    store the account password for the network-drive login (digest) encrypted
    - needed because digest requires the plaintext password to compute HA1 for
    the username sent by the client."""
    import base64, hashlib
    from cryptography.fernet import Fernet
    from app.config import JWT_SECRET
    key = base64.urlsafe_b64encode(hashlib.sha256(JWT_SECRET.encode()).digest())
    return Fernet(key)


def store_relay_secret(user_id: str, plain_password: str) -> None:
    """Store the encrypted account password for the relay (in the field relay_password)."""
    try:
        token = _relay_fernet().encrypt(plain_password.encode()).decode("ascii")
        _conn.execute("UPDATE users SET relay_password = ? WHERE id = ?", (token, user_id))
        _conn.commit()
    except Exception:
        pass


def get_relay_secret(user_id: str) -> str | None:
    """Decrypted account password for the digest computation (or None)."""
    row = _conn.execute("SELECT relay_password FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row or not row["relay_password"]:
        return None
    try:
        return _relay_fernet().decrypt(row["relay_password"].encode()).decode()
    except Exception:
        return None


# Backward-compatible names (in case referenced elsewhere)
def store_relay_ha1(user_id: str, username: str, plain_password: str) -> None:
    store_relay_secret(user_id, plain_password)


def get_relay_ha1(user_id: str) -> str | None:
    return None


def get_user_by_username_any(username: str) -> dict | None:
    """Look up a user only by username (without a realm suffix)."""
    return get_user_by_username(username)


def set_client_relay_enabled(client_id: str, enabled: bool, expires_at: int = 0) -> None:
    """Set the relay release. expires_at = Unix ms for automatic closing
    (0 = never). When locking, the expiry time is always reset."""
    if enabled:
        _conn.execute(
            "UPDATE clients SET relay_enabled = 1, relay_expires_at = ? WHERE id = ?",
            (int(expires_at or 0), client_id))
    else:
        _conn.execute(
            "UPDATE clients SET relay_enabled = 0, relay_expires_at = 0 WHERE id = ?",
            (client_id,))
    _conn.commit()


def set_client_relay_expiry(client_id: str, expires_at: int) -> None:
    """Set only the auto-close time of an (already released) client."""
    _conn.execute("UPDATE clients SET relay_expires_at = ? WHERE id = ?",
                  (int(expires_at or 0), client_id))
    _conn.commit()


def is_client_relay_enabled(client_id: str) -> bool:
    row = _conn.execute("SELECT relay_enabled FROM clients WHERE id = ?", (client_id,)).fetchone()
    return bool(row and row["relay_enabled"])


def list_relay_enabled_clients() -> list[dict]:
    rows = _conn.execute(
        "SELECT * FROM clients WHERE relay_enabled = 1 ORDER BY hostname").fetchall()
    return [dict(r) for r in rows]


def list_expired_relay_clients(now_ms: int) -> list[dict]:
    """Released clients whose auto-close time has been reached (>0)."""
    rows = _conn.execute(
        "SELECT * FROM clients WHERE relay_enabled = 1 AND relay_expires_at > 0 "
        "AND relay_expires_at <= ?", (int(now_ms),)).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------
# VPN tunnels (WireGuard-compatible)
# ------------------------------------------------------------------

# ------------------------------------------------------------------
# Virtual network: members
# ------------------------------------------------------------------

def set_vpn_member(kind: str, ref: str, address: str, label: str = "") -> None:
    _conn.execute(
        "INSERT OR REPLACE INTO vpn_members (kind, ref, address, label, created_at) "
        "VALUES (?, ?, ?, ?, ?)", (kind, ref, address, label, _now_ms()))
    _conn.commit()


def get_vpn_member_address(kind: str, ref: str) -> str:
    row = _conn.execute(
        "SELECT address FROM vpn_members WHERE kind = ? AND ref = ?",
        (kind, ref)).fetchone()
    return row["address"] if row else ""


def get_vpn_member_ref(kind: str, address: str) -> str | None:
    row = _conn.execute(
        "SELECT ref FROM vpn_members WHERE kind = ? AND address = ?",
        (kind, address)).fetchone()
    return row["ref"] if row else None


def list_vpn_members() -> list[dict]:
    return [dict(r) for r in _conn.execute(
        "SELECT * FROM vpn_members ORDER BY address").fetchall()]


def list_vpn_member_addresses() -> list[str]:
    return [r["address"] for r in _conn.execute(
        "SELECT address FROM vpn_members").fetchall()]


def remove_vpn_member(kind: str, ref: str) -> None:
    _conn.execute("DELETE FROM vpn_members WHERE kind = ? AND ref = ?",
                  (kind, ref))
    _conn.commit()


# ------------------------------------------------------------------
# Port forwards
# ------------------------------------------------------------------

def create_port_forward(rec: dict) -> None:
    _conn.execute(
        """INSERT INTO port_forwards (id, client_id, label, target_host,
               target_port, listen_port, username, allow_from, created_at,
               expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (rec["id"], rec["client_id"], rec.get("label", ""),
         rec.get("target_host", "127.0.0.1"), int(rec["target_port"]),
         int(rec["listen_port"]), rec.get("username", ""),
         rec.get("allow_from", ""), int(rec["created_at"]),
         int(rec.get("expires_at") or 0)))
    _conn.commit()


def list_port_forwards(active_only: bool = True,
                       client_id: str | None = None) -> list[dict]:
    sql = "SELECT * FROM port_forwards"
    where, params = [], []
    if active_only:
        where.append("closed_at = 0")
    if client_id:
        where.append("client_id = ?")
        params.append(client_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    return [dict(r) for r in _conn.execute(sql + " ORDER BY created_at DESC",
                                           tuple(params)).fetchall()]


def get_port_forward(forward_id: str) -> dict | None:
    row = _conn.execute("SELECT * FROM port_forwards WHERE id = ?",
                        (forward_id,)).fetchone()
    return dict(row) if row else None


def close_port_forward(forward_id: str) -> None:
    _conn.execute("UPDATE port_forwards SET closed_at = ? "
                  "WHERE id = ? AND closed_at = 0", (_now_ms(), forward_id))
    _conn.commit()


def create_vpn_tunnel(rec: dict) -> None:
    """Creates a new tunnel (legacy of the earlier VPN)."""
    _conn.execute(
        """
        INSERT INTO vpn_tunnels
            (id, client_id, name, username, address, public_key,
             preshared_key, allowed_ips, created_at, expires_at,
             mode, transport, l2)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (rec["id"], rec["client_id"], rec.get("name", ""), rec.get("username", ""),
         rec["address"], rec["public_key"], rec.get("preshared_key", ""),
         rec.get("allowed_ips", ""), int(rec["created_at"]),
         int(rec.get("expires_at") or 0),
         rec.get("mode", "client"), rec.get("transport", "relay"),
         int(rec.get("l2") or 0)),
    )
    _conn.commit()


def get_vpn_tunnel(tunnel_id: str) -> dict | None:
    row = _conn.execute("SELECT * FROM vpn_tunnels WHERE id = ?",
                        (tunnel_id,)).fetchone()
    return dict(row) if row else None


def list_vpn_tunnels(active_only: bool = True,
                     client_id: str | None = None) -> list[dict]:
    """Tunnel list. 'active_only' hides closed tunnels."""
    sql = "SELECT * FROM vpn_tunnels"
    where, params = [], []
    if active_only:
        where.append("closed_at = 0")
    if client_id:
        where.append("client_id = ?")
        params.append(client_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC"
    return [dict(r) for r in _conn.execute(sql, tuple(params)).fetchall()]


def list_expired_vpn_tunnels(now_ms: int) -> list[dict]:
    """Open tunnels whose expiry time has been reached (expires_at > 0)."""
    rows = _conn.execute(
        "SELECT * FROM vpn_tunnels WHERE closed_at = 0 AND expires_at > 0 "
        "AND expires_at <= ?", (int(now_ms),)).fetchall()
    return [dict(r) for r in rows]


def close_vpn_tunnel(tunnel_id: str) -> None:
    _conn.execute("UPDATE vpn_tunnels SET closed_at = ? WHERE id = ? AND closed_at = 0",
                  (_now_ms(), tunnel_id))
    _conn.commit()


def touch_vpn_tunnel(tunnel_id: str, ts_ms: int) -> None:
    """Remembers the last successful handshake (for the overview)."""
    _conn.execute("UPDATE vpn_tunnels SET last_handshake = ? WHERE id = ?",
                  (int(ts_ms), tunnel_id))
    _conn.commit()


# ------------------------------------------------------------------
# Node tier
# ------------------------------------------------------------------

def set_client_subnets(client_id: str, subnets) -> None:
    """Remembers the networks the agent reported."""
    import json as _json
    value = _json.dumps([str(x) for x in (subnets or [])][:8])
    _conn.execute("UPDATE clients SET lan_subnets = ? WHERE id = ?",
                  (value, client_id))
    _conn.commit()


def get_client_subnets(client_id: str) -> list:
    row = _conn.execute("SELECT lan_subnets FROM clients WHERE id = ?",
                        (client_id,)).fetchone()
    if not row or not row["lan_subnets"]:
        return []
    import json as _json
    try:
        return list(_json.loads(row["lan_subnets"]) or [])
    except (ValueError, TypeError):
        return []


# ----------------------------------------------------------------------
# Nodes (see app/node_manager.py)
# ----------------------------------------------------------------------
_NODE_JSON_FIELDS = ("info", "modules")


def _node_row(row) -> dict | None:
    if not row:
        return None
    d = dict(row)
    for k in _NODE_JSON_FIELDS:
        try:
            d[k] = json.loads(d[k]) if d.get(k) else ({} if k != "info" else {})
        except (ValueError, TypeError):
            d[k] = {}
    return d


def node_get(client_id: str) -> dict | None:
    return _node_row(_conn.execute("SELECT * FROM nodes WHERE client_id = ?", (client_id,)).fetchone())


def node_list() -> list[dict]:
    rows = _conn.execute("SELECT * FROM nodes ORDER BY created_at").fetchall()
    return [_node_row(r) for r in rows]


def node_create(client_id: str, token_hash: str) -> None:
    """Creates the node (or resets it with a new token)."""
    _conn.execute(
        "INSERT OR REPLACE INTO nodes (client_id, token_hash, state, error, version, info, modules, created_at, last_seen) "
        "VALUES (?, ?, 'installing', NULL, NULL, '{}', '{}', ?, 0)",
        (client_id, token_hash, _now_ms()))
    _conn.commit()


def node_update(client_id: str, **fields) -> None:
    allowed = {"token_hash", "state", "error", "version", "info", "modules", "last_seen"}
    cols, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k in _NODE_JSON_FIELDS and not isinstance(v, str):
            v = json.dumps(v)
        cols.append(f"{k} = ?"); vals.append(v)
    if not cols:
        return
    vals.append(client_id)
    _conn.execute(f"UPDATE nodes SET {', '.join(cols)} WHERE client_id = ?", vals)
    _conn.commit()


def node_delete(client_id: str) -> None:
    _conn.execute("DELETE FROM nodes WHERE client_id = ?", (client_id,))
    _conn.commit()


def _default_user_language() -> str:
    """Language for new accounts = server language (default: English)."""
    try:
        from .i18n import server_lang
        return server_lang()
    except Exception:
        return "en"


def create_user(username: str, password_hash: str, display_name: str, role: str, must_change_pw: bool, email: str | None = None) -> dict:
    user_id = _new_id()
    _conn.execute(
        """
        INSERT INTO users (id, username, password_hash, display_name, role, must_change_pw, created_at, email, language)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, username, password_hash, display_name, role, int(must_change_pw), _now_ms(),
         (email or "").strip() or None, _default_user_language()),
    )
    _conn.commit()
    # Assign the default groups immediately, so a fresh account is not left
    # without any right (not even 'login').
    auto_assign_groups(user_id, role)
    return get_user_by_id(user_id)


def update_user_password(user_id: str, password_hash: str, must_change_pw: bool = False) -> None:
    _conn.execute(
        "UPDATE users SET password_hash = ?, must_change_pw = ? WHERE id = ?",
        (password_hash, int(must_change_pw), user_id),
    )
    _conn.commit()


def set_user_totp(user_id: str, secret: str | None = None,
                  enabled: bool | None = None, backup: str | None = None) -> None:
    """
    Set a user's two-factor fields. Only the provided values are changed - so a
    used recovery code can be removed, for example, without touching the secret.
    """
    sets, args = [], []
    if secret is not None:
        sets.append("totp_secret = ?"); args.append(secret or None)
    if enabled is not None:
        sets.append("totp_enabled = ?"); args.append(int(bool(enabled)))
    if backup is not None:
        sets.append("totp_backup = ?"); args.append(backup or None)
    if not sets:
        return
    args.append(user_id)
    _conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", tuple(args))
    _conn.commit()


# UI styles: the core knows only "futuristic" (desktop with windows).
# Further styles (Classic = "standard", Classic Hard = "classic_hard", custom)
# come from the module "styles" (src/modules/styles/frontend/styles/*/style.json).
CORE_DESIGN = "futuristic"
_DESIGN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")


def available_designs() -> list[str]:
    """Currently selectable styles: core default + those of the module "styles" (if active)."""
    out = [CORE_DESIGN]
    try:
        from app import modules as _modules
        reg = _modules.use("styles", "registry")
        if reg:
            out += [i for i in reg.ids() if i != CORE_DESIGN]
    except Exception:
        pass
    return out


def valid_design_id(design: str | None) -> bool:
    """Any validly written ID can be stored - even if the style is currently
    unavailable (module off): the choice is kept and takes effect again as soon
    as it is back."""
    return bool(design) and bool(_DESIGN_ID_RE.match(design))


def effective_design(user) -> str:
    """The style that applies for the user: own choice, otherwise the
    installation default - each only if available; otherwise futuristic."""
    own = ""
    try:
        own = (user["design"] if "design" in user.keys() else "") or ""
    except Exception:
        own = ""
    avail = available_designs()
    if own in avail:
        return own
    d = get_setting("default_design", CORE_DESIGN) or CORE_DESIGN
    return d if d in avail else CORE_DESIGN


def update_user_profile(user_id: str, display_name: str | None = None, language: str | None = None, theme: str | None = None, accent: str | None = None, email: str | None = None, design: str | None = None) -> None:
    """Updates only the provided fields (the others stay unchanged)."""
    user = get_user_by_id(user_id)
    if not user:
        return
    cur_email = user["email"] if "email" in user.keys() else None
    _conn.execute(
        "UPDATE users SET display_name = ?, language = ?, theme = ?, accent = ?, email = ? WHERE id = ?",
        (
            display_name if display_name is not None else user["display_name"],
            language if language is not None else user["language"],
            theme if theme is not None else user["theme"],
            accent if accent is not None else (user["accent"] if "accent" in user.keys() else "teal"),
            (email.strip() or None) if email is not None else cur_email,
            user_id,
        ),
    )
    if design is not None:
        _conn.execute("UPDATE users SET design = ? WHERE id = ?",
                      (design if valid_design_id(design) else "", user_id))
    _conn.commit()


def set_user_email(user_id: str, email: str | None) -> None:
    _conn.execute("UPDATE users SET email = ? WHERE id = ?", ((email or "").strip() or None, user_id))
    _conn.commit()


def get_user_by_email(email: str) -> dict | None:
    if not email:
        return None
    row = _conn.execute("SELECT * FROM users WHERE email = ? COLLATE NOCASE", (email.strip(),)).fetchone()
    return dict(row) if row else None


def delete_user(user_id: str) -> None:
    _conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    _conn.commit()


# ------------------------------------------------------------------
# TENANTS / LOCATIONS / FOLDERS  (Hierarchie)
# ------------------------------------------------------------------

def list_tenants() -> list[dict]:
    return [dict(r) for r in _conn.execute("SELECT * FROM tenants ORDER BY name").fetchall()]


def create_tenant(name: str, color: str = "#2dd4bf") -> dict:
    tid = _new_id()
    _conn.execute("INSERT INTO tenants (id, name, color) VALUES (?, ?, ?)", (tid, name, color))
    _conn.commit()
    return dict(_conn.execute("SELECT * FROM tenants WHERE id = ?", (tid,)).fetchone())


def ensure_uncategorized() -> tuple[str, str]:
    """
    Ensures there is a tenant "Uncategorized" with a location "Default", and
    returns (tenant_id, location_id).

    Used when a client is added WITHOUT a tenant/location - it then lands here
    automatically instead of being "homeless".
    """
    # Find or create the tenant "Uncategorized"
    row = _conn.execute("SELECT id FROM tenants WHERE name = ?", ("Uncategorized",)).fetchone()
    if row:
        tenant_id = row["id"]
    else:
        tenant_id = _new_id()
        _conn.execute(
            "INSERT INTO tenants (id, name, color) VALUES (?, ?, ?)",
            (tenant_id, "Uncategorized", "#7f93ad"),
        )

    # Find or create the location "Default" inside this tenant
    row = _conn.execute(
        "SELECT id FROM locations WHERE tenant_id = ? AND name = ?", (tenant_id, "Default")
    ).fetchone()
    if row:
        location_id = row["id"]
    else:
        location_id = _new_id()
        _conn.execute(
            "INSERT INTO locations (id, tenant_id, name) VALUES (?, ?, ?)",
            (location_id, tenant_id, "Default"),
        )

    _conn.commit()
    return tenant_id, location_id


def list_locations(tenant_id: str | None = None) -> list[dict]:
    if tenant_id:
        rows = _conn.execute("SELECT * FROM locations WHERE tenant_id = ? ORDER BY name", (tenant_id,)).fetchall()
    else:
        rows = _conn.execute("SELECT * FROM locations ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def create_location(tenant_id: str, name: str) -> dict:
    lid = _new_id()
    _conn.execute("INSERT INTO locations (id, tenant_id, name) VALUES (?, ?, ?)", (lid, tenant_id, name))
    _conn.commit()
    return dict(_conn.execute("SELECT * FROM locations WHERE id = ?", (lid,)).fetchone())


def _move_clients_to_uncategorized(where_sql: str, params: tuple) -> int:
    """
    Moves all clients matched by where_sql to Uncategorized/Default (folder_id
    is cleared, since folders belong to the old location). Returns the number
    of moved clients.
    """
    unc_tenant, unc_location = ensure_uncategorized()
    cur = _conn.execute(
        f"UPDATE clients SET tenant_id = ?, location_id = ?, folder_id = NULL WHERE {where_sql}",
        (unc_tenant, unc_location, *params),
    )
    return cur.rowcount


def delete_location(location_id: str) -> dict:
    """
    Deletes a location. All clients in it move to Uncategorized/Default.
    The "Default" location of the Uncategorized tenant is protected (catch-all).
    Folders of the location are deleted too (clients were moved beforehand).
    """
    unc_tenant, unc_location = ensure_uncategorized()
    if location_id == unc_location:
        raise ValueError("The default location 'Uncategorized/Default' cannot be deleted")
    if not _conn.execute("SELECT 1 FROM locations WHERE id = ?", (location_id,)).fetchone():
        raise KeyError("location not found")

    moved = _move_clients_to_uncategorized("location_id = ?", (location_id,))
    # Delete folders explicitly (do not rely on FK CASCADE - PRAGMA
    # foreign_keys is off by default in SQLite).
    _conn.execute("DELETE FROM folders WHERE location_id = ?", (location_id,))
    _conn.execute("DELETE FROM locations WHERE id = ?", (location_id,))
    _conn.commit()
    return {"moved_clients": moved}


def delete_tenant(tenant_id: str) -> dict:
    """
    Deletes a tenant including all its locations and folders. All clients of
    the tenant (including those without a location) move to Uncategorized/Default.
    The Uncategorized tenant itself is protected (catch-all).
    """
    unc_tenant, _ = ensure_uncategorized()
    if tenant_id == unc_tenant:
        raise ValueError("The tenant 'Uncategorized' cannot be deleted")
    if not _conn.execute("SELECT 1 FROM tenants WHERE id = ?", (tenant_id,)).fetchone():
        raise KeyError("tenant not found")

    # Catch clients via tenant_id OR via a location of the tenant (also covers
    # inconsistent records where only one is set).
    moved = _move_clients_to_uncategorized(
        "tenant_id = ? OR location_id IN (SELECT id FROM locations WHERE tenant_id = ?)",
        (tenant_id, tenant_id),
    )
    _conn.execute(
        "DELETE FROM folders WHERE location_id IN (SELECT id FROM locations WHERE tenant_id = ?)",
        (tenant_id,),
    )
    _conn.execute("DELETE FROM locations WHERE tenant_id = ?", (tenant_id,))
    _conn.execute("DELETE FROM tenants WHERE id = ?", (tenant_id,))
    _conn.commit()
    return {"moved_clients": moved}


def list_folders(location_id: str | None = None) -> list[dict]:
    if location_id:
        rows = _conn.execute("SELECT * FROM folders WHERE location_id = ?", (location_id,)).fetchall()
    else:
        rows = _conn.execute("SELECT * FROM folders").fetchall()
    return [dict(r) for r in rows]


def create_folder(location_id: str, name: str, parent_folder_id: str | None = None) -> dict:
    fid = _new_id()
    _conn.execute(
        "INSERT INTO folders (id, location_id, parent_folder_id, name) VALUES (?, ?, ?, ?)",
        (fid, location_id, parent_folder_id, name),
    )
    _conn.commit()
    return dict(_conn.execute("SELECT * FROM folders WHERE id = ?", (fid,)).fetchone())


def delete_folder(folder_id: str) -> int:
    """
    Deletes a folder including all subfolders (recursively). Clients that were
    in one of these folders only lose their folder assignment (folder_id = NULL)
    - they stay in their location/tenant. Returns the number of deleted folders.
    """
    # Collect all affected folder IDs (the folder + all descendants).
    to_delete = [folder_id]
    frontier = [folder_id]
    while frontier:
        children = _conn.execute(
            "SELECT id FROM folders WHERE parent_folder_id = ?", (frontier.pop(),)
        ).fetchall()
        for c in children:
            to_delete.append(c["id"])
            frontier.append(c["id"])
    placeholders = ",".join("?" for _ in to_delete)
    # Detach clients from these folders (they stay in location/tenant).
    _conn.execute(
        f"UPDATE clients SET folder_id = NULL WHERE folder_id IN ({placeholders})",
        to_delete,
    )
    _conn.execute(f"DELETE FROM folders WHERE id IN ({placeholders})", to_delete)
    _conn.commit()
    return len(to_delete)


# ------------------------------------------------------------------
# CLIENTS
# ------------------------------------------------------------------

def upsert_client(
    client_id: str,
    hostname: str,
    platform: str | None,
    arch: str | None,
    release: str | None,
    ip: str | None,
) -> None:
    """
    Called when an agent registers.
    Creates the client (it then lands first WITHOUT a tenant/location,
    "unassigned" - the admin assigns it to a tenant/location in the dashboard
    via the edit dialog) or updates the basic info if it already exists.
    """
    now = _now_ms()
    existing = _conn.execute("SELECT id FROM clients WHERE id = ?", (client_id,)).fetchone()
    if existing:
        _conn.execute(
            """
            UPDATE clients
            SET hostname=?, platform=?, arch=?, release=?, ip=?, last_seen=?
            WHERE id=?
            """,
            (hostname, platform, arch, release, ip, now, client_id),
        )
    else:
        _conn.execute(
            """
            INSERT INTO clients (id, hostname, platform, arch, release, ip, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (client_id, hostname, platform, arch, release, ip, now, now),
        )
    _conn.commit()


def touch_client(client_id: str) -> None:
    """Updates only the 'last seen' timestamp (on every heartbeat)."""
    _conn.execute("UPDATE clients SET last_seen = ? WHERE id = ?", (_now_ms(), client_id))
    _conn.commit()


def apply_detected_device_type(client_id: str, detected: str | None) -> bool:
    """
    Remembers the device type auto-detected by the agent ("vm"/"lxc") in
    detected_device_type. The actual device_type is NO longer changed
    automatically - instead the client panel asks the user once whether the
    client should be treated as VM/LXC (with host selection) or as physical
    (confirmation -> device_type_ack = 1).
    Returns True if a (new) detection was stored.
    """
    if detected not in ("vm", "lxc"):
        return False
    row = _conn.execute("SELECT detected_device_type FROM clients WHERE id = ?", (client_id,)).fetchone()
    if not row or row["detected_device_type"] == detected:
        return False
    _conn.execute("UPDATE clients SET detected_device_type = ? WHERE id = ?", (detected, client_id))
    _conn.commit()
    return True


DEVICE_KINDS = ("laptop", "pc", "server", "hypervisor", "vm", "lxc", "docker")
ICON_TYPES = ("www", "isp", "router", "switch", "firewall", "ap", "controller", "management", "subnet",
              "server", "hypervisor", "vm", "lxc", "docker", "nas", "storage", "pc", "laptop", "printer",
              "phone", "tv", "camera", "iot", "device")


def apply_detected_kind(client_id: str, kind: str | None) -> bool:
    """Stores the device kind the agent detected (for the icon). True = changed."""
    if kind not in DEVICE_KINDS:
        return False
    row = _conn.execute("SELECT detected_kind FROM clients WHERE id = ?", (client_id,)).fetchone()
    if not row or row["detected_kind"] == kind:
        return False
    _conn.execute("UPDATE clients SET detected_kind = ? WHERE id = ?", (kind, client_id))
    _conn.commit()
    return True


def list_clients() -> list[dict]:
    return [dict(r) for r in _conn.execute("SELECT * FROM clients ORDER BY hostname").fetchall()]


def get_client(client_id: str) -> dict | None:
    row = _conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    return dict(row) if row else None


def set_agent_version(client_id: str, version: str | None) -> None:
    """Stores the version reported by the agent (for the 'outdated' hint)."""
    if not version:
        return
    _conn.execute("UPDATE clients SET agent_version = ? WHERE id = ?", (version, client_id))
    _conn.commit()


def set_pending_agent_update(client_id: str, on: bool) -> None:
    """
    Pre-marks (or clears) that the client should receive an agent update as
    soon as it connects next time (offline option of "Update all agents now").
    """
    _conn.execute("UPDATE clients SET pending_agent_update = ? WHERE id = ?",
                  (1 if on else 0, client_id))
    _conn.commit()


def set_user_silent_screen(user_id: int, on: bool) -> None:
    """One-off silent mode: this user's NEXT remote-screen session starts
    without a consent dialog on the device; afterwards the flag is cleared
    automatically (see sockets.dashboard_screen_start)."""
    _conn.execute("UPDATE users SET silent_screen = ? WHERE id = ?",
                  (1 if on else 0, user_id))
    _conn.commit()


_UI_PREFS_MAX_BYTES = 512 * 1024   # legacy: ONE blob in users.ui_prefs

# New storage: one row PER KEY instead of a single large blob.
#
# Why the rework: previously all UI settings lay together in users.ui_prefs and
# were checked against a 512 KB limit. As soon as a single chunk (fleet
# history, notifications, large layouts) exceeded the limit, the ENTIRE save
# failed - and with it the start-menu layout with its folders. From the
# outside this looked as if the browser cache was "forgotten": in another
# browser/under another URL there was simply nothing, because nothing ever
# arrived.
#
# Now in the worst case ONE key fails, never all of them again.
_UI_PREF_KEY_MAX_BYTES = 2 * 1024 * 1024      # 2 MB per key
_UI_PREF_TOTAL_MAX_BYTES = 32 * 1024 * 1024   # 32 MB per user in total


def _ensure_user_prefs_table() -> None:
    _conn.executescript("""
        CREATE TABLE IF NOT EXISTS user_prefs (
            user_id TEXT NOT NULL,
            key     TEXT NOT NULL,
            value   TEXT NOT NULL,
            updated_at INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, key)
        );
    """)
    _conn.commit()


def get_user_prefs(user_id) -> dict:
    """All UI keys of a user as {key: value}."""
    _ensure_user_prefs_table()
    out: dict[str, str] = {}
    try:
        rows = _conn.execute(
            "SELECT key, value FROM user_prefs WHERE user_id = ?",
            (str(user_id),)).fetchall()
        for r in rows:
            out[r["key"]] = r["value"]
    except Exception:
        pass
    # One-off import from the old blob column, so existing installations keep
    # their settings.
    if not out:
        raw = get_user_ui_prefs(user_id)
        if raw:
            import json as _json
            try:
                data = _json.loads(raw)
                keys = data.get("keys") if isinstance(data, dict) else None
                if isinstance(keys, dict) and keys:
                    merge_user_prefs(user_id, keys)
                    out = {str(k): str(v) for k, v in keys.items()}
            except Exception:
                pass
    return out


def merge_user_prefs(user_id, keys: dict) -> None:
    """
    Write/delete individual keys (value None deletes).
    Oversized single values are skipped - the rest is stored anyway, so one
    outlier does not block everything.
    """
    _ensure_user_prefs_table()
    uid = str(user_id)
    skipped: list[str] = []
    for k, v in (keys or {}).items():
        key = str(k)
        if v is None:
            _conn.execute("DELETE FROM user_prefs WHERE user_id = ? AND key = ?", (uid, key))
            continue
        val = str(v)
        if len(val.encode("utf-8")) > _UI_PREF_KEY_MAX_BYTES:
            skipped.append(key)
            continue
        _conn.execute(
            "INSERT INTO user_prefs (user_id, key, value, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (uid, key, val, _now_ms()))
    _conn.commit()
    if skipped:
        print(f"[db] UI settings too large, skipped: {', '.join(skipped)}")


def user_prefs_size(user_id) -> int:
    _ensure_user_prefs_table()
    try:
        row = _conn.execute(
            "SELECT COALESCE(SUM(LENGTH(value)), 0) AS n FROM user_prefs WHERE user_id = ?",
            (str(user_id),)).fetchone()
        return int(row["n"] or 0)
    except Exception:
        return 0


def get_user_ui_prefs(user_id: int) -> str | None:
    """Server-side stored UI settings (JSON string) of the user."""
    row = _conn.execute("SELECT ui_prefs FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["ui_prefs"] if row and row["ui_prefs"] else None


def set_user_ui_prefs(user_id: int, prefs_json: str) -> None:
    """Stores the UI settings (JSON string, size-limited)."""
    if prefs_json and len(prefs_json.encode("utf-8")) > _UI_PREFS_MAX_BYTES:
        raise ValueError("UI settings too large (max. 512 KB)")
    _conn.execute("UPDATE users SET ui_prefs = ? WHERE id = ?", (prefs_json, user_id))
    _conn.commit()


def merge_user_ui_prefs(user_id: int, keys: dict) -> dict:
    """
    Merges individual UI keys into the stored state WITHOUT losing the other
    keys.

    Why this matters: previously the frontend always wrote the COMPLETE set via
    PUT. If one opened the RMM via a different URL/browser (empty localStorage)
    and a save ran before the server state was loaded, server-side stored
    things (open folders in the start menu, favorites, layouts) were
    overwritten or deleted. With the merge a client can only ever change what
    it actually knows.

    Value None deletes the key specifically.
    """
    import json as _json
    raw = get_user_ui_prefs(user_id)
    cur = {}
    if raw:
        try:
            data = _json.loads(raw)
            if isinstance(data, dict) and isinstance(data.get("keys"), dict):
                cur = data["keys"]
        except Exception:
            cur = {}
    for k, v in (keys or {}).items():
        if v is None:
            cur.pop(k, None)
        else:
            cur[str(k)] = str(v)
    set_user_ui_prefs(user_id, _json.dumps({"keys": cur}))
    return cur


def update_client(client_id: str, fields: dict) -> dict | None:
    """
    Generic update for the edit dialog in the frontend.
    'fields' is a dict with the columns to be changed,
    e.g. {"hostname": "New name", "tenant_id": "...", "color": "#ff0000"}.
    """
    allowed = {
        "hostname", "tenant_id", "location_id", "folder_id", "parent_client_id",
        "color", "notes", "status_override", "active", "device_type", "auto_update",
        "device_type_ack", "fav_dir", "warranty_until", "patch_policy", "icon_type",
        "essential", "short_name", "tags", "maintenance_until",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_client(client_id)

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [client_id]
    _conn.execute(f"UPDATE clients SET {set_clause} WHERE id = ?", values)
    _conn.commit()
    return get_client(client_id)


def delete_client(client_id: str) -> None:
    _conn.execute("DELETE FROM clients WHERE id = ?", (client_id,))
    # Also clean up client-scoped permission grants (avoid orphaned entries).
    _conn.execute("DELETE FROM permission_grants WHERE scope = ?", (client_id,))
    _conn.commit()


# ------------------------------------------------------------------
# MAINTENANCE MODE (Core) - alarms off, protection mechanisms suspended
# ------------------------------------------------------------------

def client_maintenance(client: dict | None) -> dict:
    """{"active": bool, "until": ms (0 = until ended manually)} for a client record."""
    if not client or client.get("status_override") != "maintenance":
        return {"active": False, "until": 0}
    until = int(client.get("maintenance_until") or 0)
    if until and until <= _now_ms():
        return {"active": False, "until": until}       # expired
    return {"active": True, "until": until}


def in_maintenance(client_id: str | None) -> bool:
    """Convenient test for core and modules (e.g. notifier, patching, USB)."""
    if not client_id:
        return False
    try:
        return client_maintenance(get_client(client_id))["active"]
    except Exception:
        return False


def set_maintenance(client_id: str, minutes: int | None) -> dict:
    """minutes > 0: for X minutes; 0: until ended manually; None: end."""
    if minutes is None:
        _conn.execute("UPDATE clients SET status_override = NULL, maintenance_until = 0 "
                      "WHERE id = ? AND status_override = 'maintenance'", (client_id,))
    else:
        until = _now_ms() + int(minutes) * 60_000 if minutes > 0 else 0
        _conn.execute("UPDATE clients SET status_override = 'maintenance', maintenance_until = ? "
                      "WHERE id = ?", (until, client_id))
    _conn.commit()
    return client_maintenance(get_client(client_id))


def expired_maintenance_ids() -> list[str]:
    rows = _conn.execute(
        "SELECT id FROM clients WHERE status_override = 'maintenance' "
        "AND maintenance_until > 0 AND maintenance_until <= ?", (_now_ms(),)).fetchall()
    return [r["id"] for r in rows]


# ------------------------------------------------------------------
# OFFLINE QUEUE (actions "as soon as the agent is online")
# ------------------------------------------------------------------

def queue_add(client_id: str, kind: str, label: str, payload: dict,
              created_by: str | None = None) -> dict:
    row = {"id": _new_id(), "client_id": client_id, "kind": kind,
           "label": (label or "")[:200], "payload": json.dumps(payload or {}),
           "created_by": created_by, "created_at": _now_ms(), "status": "pending"}
    _conn.execute(
        "INSERT INTO client_queue (id, client_id, kind, label, payload, created_by, "
        "created_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
        (row["id"], client_id, kind, row["label"], row["payload"],
         created_by, row["created_at"]))
    _conn.commit()
    return row


def queue_list(client_id: str, include_done: bool = True) -> list[dict]:
    sql = "SELECT * FROM client_queue WHERE client_id = ?"
    if not include_done:
        sql += " AND status IN ('pending','running')"
    rows = _conn.execute(sql + " ORDER BY created_at", (client_id,)).fetchall()
    return [dict(r) for r in rows]


def queue_pending(client_id: str) -> list[dict]:
    rows = _conn.execute(
        "SELECT * FROM client_queue WHERE client_id = ? AND status = 'pending' "
        "ORDER BY created_at", (client_id,)).fetchall()
    return [dict(r) for r in rows]


def queue_set_status(qid: str, status: str, result: str | None = None) -> None:
    _conn.execute(
        "UPDATE client_queue SET status = ?, result = ?, finished_at = ? WHERE id = ?",
        (status, (result or "")[:4000],
         _now_ms() if status in ("done", "failed") else None, qid))
    _conn.commit()


def queue_delete(qid: str) -> None:
    _conn.execute("DELETE FROM client_queue WHERE id = ?", (qid,))
    _conn.commit()


# ------------------------------------------------------------------
# ACTIVATION + MODULE STORE (Settings -> Activation / Module Store)
# ------------------------------------------------------------------

def redeem_activation_key(key: str, username: str | None = None) -> dict:
    """Stores an activation code with its redemption date (newest row = active)."""
    row = {"id": _new_id(), "key": key, "redeemed_at": _now_ms(), "redeemed_by": username}
    _conn.execute(
        "INSERT INTO activation_keys (id, key, redeemed_at, redeemed_by) VALUES (?, ?, ?, ?)",
        (row["id"], row["key"], row["redeemed_at"], row["redeemed_by"]),
    )
    _conn.commit()
    return row


def get_activation() -> dict | None:
    """The most recently redeemed activation code (or None)."""
    row = _conn.execute(
        "SELECT * FROM activation_keys ORDER BY redeemed_at DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def list_module_sources() -> list[dict]:
    rows = _conn.execute("SELECT * FROM module_sources ORDER BY added_at").fetchall()
    return [dict(r) for r in rows]


def get_module_source(source_id: str) -> dict | None:
    row = _conn.execute("SELECT * FROM module_sources WHERE id = ?", (source_id,)).fetchone()
    return dict(row) if row else None


def add_module_source(name: str, url: str) -> dict:
    row = {"id": _new_id(), "name": name, "url": url, "added_at": _now_ms()}
    _conn.execute(
        "INSERT INTO module_sources (id, name, url, added_at) VALUES (?, ?, ?, ?)",
        (row["id"], row["name"], row["url"], row["added_at"]),
    )
    _conn.commit()
    return row


def delete_module_source(source_id: str) -> None:
    _conn.execute("DELETE FROM module_sources WHERE id = ?", (source_id,))
    _conn.commit()


# ------------------------------------------------------------------
# AUDIT LOG
# ------------------------------------------------------------------

def add_audit_entry(username: str | None, action: str, target: str | None = None, details: str | None = None) -> None:
    _conn.execute(
        "INSERT INTO audit_log (id, ts, username, action, target, details) VALUES (?, ?, ?, ?, ?, ?)",
        (_new_id(), _now_ms(), username, action, target, details),
    )
    _conn.commit()


def list_audit_for_target(target: str, action_prefix: str, limit: int = 500) -> list[dict]:
    """Audit entries of a target (e.g. client) with an action prefix, newest first."""
    rows = _conn.execute(
        "SELECT * FROM audit_log WHERE target = ? AND action LIKE ? ORDER BY ts DESC LIMIT ?",
        (target, action_prefix + "%", limit),
    ).fetchall()
    return [dict(r) for r in rows]


def list_audit_log(limit: int = 200) -> list[dict]:
    rows = _conn.execute(
        "SELECT * FROM audit_log ORDER BY ts DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def cleanup_old_audit_entries(max_age_days: int = 30) -> None:
    """Deletes audit entries older than max_age_days (retention period)."""
    cutoff = _now_ms() - max_age_days * 86400 * 1000
    _conn.execute("DELETE FROM audit_log WHERE ts < ?", (cutoff,))
    _conn.commit()


# ------------------------------------------------------------------
# ENROLLMENT TOKENS (client onboarding)
# ------------------------------------------------------------------

def create_enrollment_token(tenant_id: str | None, location_id: str | None, client_name: str | None = None) -> str:
    """Creates a new, random onboarding token."""
    token = uuid.uuid4().hex  # e.g. "3f9a2b1c..." - long enough to be unguessable
    _conn.execute(
        "INSERT INTO enrollment_tokens (token, tenant_id, location_id, client_name, created_at) VALUES (?, ?, ?, ?, ?)",
        (token, tenant_id, location_id, client_name, _now_ms()),
    )
    _conn.commit()
    return token


def get_enrollment_token(token: str) -> dict | None:
    row = _conn.execute("SELECT * FROM enrollment_tokens WHERE token = ?", (token,)).fetchone()
    return dict(row) if row else None


def mark_enrollment_token_used(token: str) -> None:
    _conn.execute("UPDATE enrollment_tokens SET used_at = ? WHERE token = ?", (_now_ms(), token))
    _conn.commit()


# ------------------------------------------------------------------
# SCREEN RECORDINGS (recording of remote sessions)
# ------------------------------------------------------------------

def create_recording(client_id: str, client_hostname: str, username: str, file_path: str,
                     fmt: str = "frames") -> str:
    """Creates a new recording entry and returns its ID.
    fmt: 'frames' (JPEG frames, default) or 'term' (terminal session as a
    text replay - see recording.start_term_recording)."""
    rec_id = _new_id()
    _conn.execute(
        """INSERT INTO screen_recordings
           (id, client_id, client_hostname, username, started_at, file_path, format)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (rec_id, client_id, client_hostname, username, _now_ms(), file_path, fmt),
    )
    _conn.commit()
    return rec_id


def create_video_recording(client_id: str, client_hostname: str, username: str,
                           file_path: str, started_at: int, ended_at: int) -> str:
    """Creates an entry for a 1:1 video recording captured in the browser."""
    rec_id = _new_id()
    _conn.execute(
        """INSERT INTO screen_recordings
           (id, client_id, client_hostname, username, started_at, ended_at, frame_count, file_path, format)
           VALUES (?, ?, ?, ?, ?, ?, 0, ?, 'video')""",
        (rec_id, client_id, client_hostname, username, started_at, ended_at, file_path),
    )
    _conn.commit()
    return rec_id


def finish_recording(rec_id: str, frame_count: int) -> None:
    """Marks a recording as finished and stores the frame count."""
    _conn.execute(
        "UPDATE screen_recordings SET ended_at = ?, frame_count = ? WHERE id = ?",
        (_now_ms(), frame_count, rec_id),
    )
    _conn.commit()


def list_recordings(limit: int = 200) -> list[dict]:
    rows = _conn.execute(
        "SELECT * FROM screen_recordings ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_recording(rec_id: str) -> dict | None:
    row = _conn.execute("SELECT * FROM screen_recordings WHERE id = ?", (rec_id,)).fetchone()
    return dict(row) if row else None


def delete_recording(rec_id: str) -> dict | None:
    """Removes a recording DB entry and returns it (for file deletion)."""
    rec = get_recording(rec_id)
    if rec:
        _conn.execute("DELETE FROM screen_recordings WHERE id = ?", (rec_id,))
        _conn.commit()
    return rec


def prune_missing_recordings() -> int:
    """
    Removes replay entries whose file no longer exists on disk (e.g. deleted
    manually or volume switched). IMPORTANT: only the screen_recordings entry
    is removed - the audit log is kept completely. Returns the number of
    removed entries.
    """
    from pathlib import Path as _P
    removed = 0
    rows = _conn.execute("SELECT id, file_path FROM screen_recordings").fetchall()
    for r in rows:
        fp = r["file_path"]
        if not fp:
            continue
        try:
            if not _P(fp).exists():
                _conn.execute("DELETE FROM screen_recordings WHERE id = ?", (r["id"],))
                removed += 1
        except OSError:
            # Path not checkable (e.g. network drive offline) -> keep entry.
            continue
    if removed:
        _conn.commit()
    return removed


def list_old_recordings(max_age_days: int) -> list[dict]:
    """Returns recordings older than max_age_days (for auto-deletion)."""
    cutoff = _now_ms() - max_age_days * 86400 * 1000
    rows = _conn.execute(
        "SELECT * FROM screen_recordings WHERE started_at < ?", (cutoff,)
    ).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------
# SCRIPTS (saved commands)
# ------------------------------------------------------------------

def list_scripts() -> list[dict]:
    return [dict(r) for r in _conn.execute("SELECT * FROM scripts ORDER BY name").fetchall()]


def get_script(script_id: str) -> dict | None:
    row = _conn.execute("SELECT * FROM scripts WHERE id = ?", (script_id,)).fetchone()
    return dict(row) if row else None


def create_script(name: str, command: str, os_target: str, folder: str = "") -> dict:
    sid = _new_id()
    _conn.execute(
        "INSERT INTO scripts (id, name, command, os, folder, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (sid, name, command, os_target, (folder or "").strip(), _now_ms()),
    )
    _conn.commit()
    return get_script(sid)


def update_script(script_id: str, name: str, command: str, os_target: str, folder: str = "") -> dict | None:
    _conn.execute(
        "UPDATE scripts SET name = ?, command = ?, os = ?, folder = ? WHERE id = ?",
        (name, command, os_target, (folder or "").strip(), script_id),
    )
    _conn.commit()
    return get_script(script_id)


def delete_script(script_id: str) -> None:
    _conn.execute("DELETE FROM scripts WHERE id = ?", (script_id,))
    _conn.commit()


# ------------------------------------------------------------------
# GROUPS / ROLES & RIGHTS
# ------------------------------------------------------------------
# Available permission keys (legacy group checkboxes, global allow):
ALL_PERMISSIONS = [
    "login", "screen", "terminal", "explorer", "quick_actions",
    "audit", "manage_users", "manage_clients", "automation",
]

# --- New, fine-grained permission vocabulary (tri-state grants) ------------
# 'admin' is a wildcard (covers all other rights in the same scope).
# 'access_clients' controls visibility + basic access to clients.
# Besides the NEW keys we keep the OLD keys valid, so already stored grants
# still resolve (no DB migration!). The old->new mapping happens at runtime
# via _PERM_IMPLIES (see below).

# Global (universal) rights:
GLOBAL_PERM_KEYS = [
    "admin",
    "login",
    "see_dashboard",
    "customize_dashboard",
    "restore_session",
    "edit_profile_name",
    "access_clients",
    "see_replay", "delete_replay",
    "see_audit",
    "see_source", "edit_source", "delete_source",
    "manage_branding",
    "manage_sso",
    # Settings: view / change default settings / admin settings
    # (IP/port, server update from GitHub, database, restart/stop, guacd).
    "see_settings", "manage_settings", "admin_settings",
    "see_permissions", "manage_permissions",
    # Manage modules: module app (enable/disable, settings, sub-features,
    # crash reports). Previously only for administrators.
    "manage_modules",
    # module "spaces": own space, public spaces, see/edit all spaces
    "get_user_space", "use_public_space", "see_all_spaces", "edit_all_spaces",
    # module "deployment": deployment server (page + files, relay area "Deployment")
    "manage_deployment",
    # module "api": use the REST API (/api/v1) - additionally the same right
    # applies per call as in the dashboard (e.g. delete a client -> c_delete).
    "use_api",
    # module "webserver": create/delete websites and edit ALL websites
    "manage_websites",
    # module "docker": create stacks, control ALL containers/images/volumes/networks
    "manage_docker",
    "manage_cloudflare",
    "manage_headless_screen",
    "manage_canary",
    "manage_ad",
    "use_bmc",         # module "bmc": view and control iLO/iDRAC
    "manage_bmc",      # module "bmc": create/change BMC connections
    "use_passwords",   # module "passwords": use the password manager
    "use_geotrack",    # module "geotrack": view locations
    "manage_geotrack", # module "geotrack": tracking on/off, clear history
    "manage_tenants",
    "use_tenants",
    "manage_dist",
    "manage_temp_usb",
    "use_browser",
    # module "console": use the RMM console (SSH, dashboard, client app) - per command additionally the matching right
    "use_console",
    "create_users",
    "network_scan", "port_scan",
    "bulk_shell",
    "use_scripts", "create_scripts",
    "automation",
    "manage_hierarchy",
    "play_games",
    # audio player / media hub (own library, uploads)
    "use_media",
    # Provide media for EVERYONE: uploads, links and playlists that every user
    # sees. Deliberately a separate right - whoever may use the player
    # ("use_media") should not automatically be able to change the shared
    # library.
    "share_media",
    "manage_favorites",
    "use_relay", "relay_unlimited",
    # chat app (direct messages & groups)
    "use_chat",
    # personal todo list (strictly private, no shares)
    "use_todos",
    # software patching: view inventory and trigger updates or set rules.
    "patching", "manage_patching",
    # Privacy: set deadlines, information about third parties, execute erasure.
    # Deliberately NOT included in 'admin_settings' - whoever may change server
    # settings need not automatically see other people's personal data.
    "manage_privacy",
    # (Previously there was also "super_admin" - "full administrator" - here.
    #  That was the same as the 'admin' wildcard and merely confusing: two
    #  checkboxes with identical effect. Both are now merged into 'admin';
    #  'super_admin' stays valid as a legacy key so already granted rights
    #  keep working.)
    # Enroll new clients (create enrollment token, fetch agent installer)
    "add_client",
    # org chart (super-/subordination of users and groups)
    "see_org", "manage_org",
    # Calendar
    "use_calendar",      # see own calendar + own appointments
    "manage_calendar",   # create/change appointments for others (beyond one's
                         # own superior role)
    # Remote screen without a request: allows the one-off "silent mode" in the
    # profile (next remote session without a consent dialog on the device).
    "screen_silent",
    # Ticket system (visibility of individual tickets additionally limited to
    # admins/creator/assignees, see tickets_routes.py).
    # VPN: use the app or issue tunnels without an expiry.
    "use_vpn", "vpn_unlimited",
    "ticket_read", "ticket_create", "ticket_edit", "ticket_comment",
    "ticket_assign", "ticket_resolve", "ticket_delete",
    # module "inventory": view inventory or manage devices/stock
    "see_inventory", "manage_inventory",
]

# Per-client rights:
CLIENT_ONLY_PERM_KEYS = [
    "access_clients",
    "manage_clients",       # edit
    "manage_agent",         # update (agent update)
    "c_delete",             # delete
    "c_screen", "c_screen_view",
    "c_terminal", "c_terminal_console",
    "c_guacamole",
    "c_power",              # shut down/restart
    "c_explorer_view", "c_explorer_edit",
    "c_taskmanager_view", "c_taskmanager_kill",
    "c_relay", "c_relay_unlimited",
    "c_vpn", "c_vpn_unlimited",     # issue VPN tunnels to this client
    # Manage this client's node: install/remove/update, install Docker,
    # provide/remove/control node modules. Whoever has it on at least one
    # client sees the node overview (only with those clients) and the catalog
    # of node modules.
    "manage_nodes",
    # module "proxmox" (per agent with a connected Proxmox instance). Additional
    # area "Default Proxmox" (pve:*) = applies to ALL agents with Proxmox.
    "pve_access",      # see the Proxmox dashboard
    "pve_vms_all",     # see all VMs
    "pve_lxc_all",     # see all LXC containers
    "pve_power",       # start/stop/restart guests
    "pve_console",     # console (noVNC / web interface)
    "pve_snapshot",    # create/revert/delete snapshots
    "pve_backup",      # create/view backups (vzdump)
    "pve_config",      # change guest configuration
    "pve_storage",     # see storage/contents
    "pve_users",       # Proxmox users/groups/ACL
    "pve_nodes",       # nodes, tasks, services
    "pve_api",         # arbitrary Proxmox API calls (API explorer) - powerful!
    "pve_manage",      # set up the Proxmox connection + manage guest shares
    "c_portforward",                # port forward to this client
    "c_notes_view", "c_notes_edit",
    "c_patch_view", "c_patch_apply",   # see / install this client\047s patches
    "c_registry",                      # read/write registry (Windows only)
    "c_win_update",                    # Windows automatic updates off/on (Windows only)
    "c_websites_view", "c_websites_edit",
    "c_screen_silent",      # remote screen without a request (per client)
    "set_warranty",         # set/change this client\047s warranty date
]

# Legacy keys (only for backward resolution of stored grants).
LEGACY_PERM_KEYS = [
    "use_guacamole", "use_terminal", "use_screen", "use_explorer",
    "use_taskmanager", "manage_users",
    # Merged with 'admin' (see above) - no longer offered, but still resolves.
    "super_admin",
    # Old node-proxy right (node system rebuilt) - resolution only.
    "c_nodeproxy",
]

# Master list of valid keys (for set_grants validation + resolver).
PERM_KEYS = GLOBAL_PERM_KEYS + [
    p for p in CLIENT_ONLY_PERM_KEYS if p not in GLOBAL_PERM_KEYS
] + LEGACY_PERM_KEYS

# ------------------------------------------------------------------
# DEFAULT CLIENT ("default client")
# A pseudo client scope whose client rights apply to EVERY client. This lets
# rights be granted once centrally, instead of setting them per client. 
# Resolution (see auth._resolve): 'deny' always beats 'allow' - so a denial on
# a CONCRETE client overrides the allow from the default client.
# ------------------------------------------------------------------
DEFAULT_CLIENT_SCOPE = "*"
# "Default Proxmox": Proxmox rights (pve_*) for ALL agents with a connected Proxmox
DEFAULT_PVE_SCOPE = "pve:*"

# Which rights are offered in the general tab (global) or the client tab.
GENERAL_PERM_KEYS = list(GLOBAL_PERM_KEYS)
CLIENT_PERM_KEYS = list(CLIENT_ONLY_PERM_KEYS)

# ------------------------------------------------------------------
# Permission implications (at runtime, without changing the DB):
# An 'allow' on the source key automatically applies to all target keys too.
# Covers (a) old keys -> new keys and (b) "stronger right covers weaker"
# (e.g. full access implies view-only).
# ------------------------------------------------------------------
_PERM_IMPLIES = {
    # --- Old -> New (stored grants stay untouched) ---
    "use_screen": ["c_screen", "c_screen_view"],
    "use_terminal": ["c_terminal", "c_terminal_console"],
    "use_explorer": ["c_explorer_view", "c_explorer_edit"],
    "use_taskmanager": ["c_taskmanager_view", "c_taskmanager_kill"],
    "use_guacamole": ["c_guacamole"],
    "manage_users": ["create_users", "see_permissions", "manage_permissions"],
    "manage_bmc": ["use_bmc"],
    "manage_geotrack": ["use_geotrack"],
    # old manage_clients also covered notes/websites/delete/power
    "manage_clients": ["c_delete", "c_power", "c_notes_view", "c_notes_edit",
                       "c_websites_view", "c_websites_edit", "set_warranty"],
    # --- Stronger right covers weaker ---
    "c_screen": ["c_screen_view"],
    "c_terminal": ["c_terminal_console"],
    "c_explorer_edit": ["c_explorer_view"],
    "c_taskmanager_kill": ["c_taskmanager_view"],
    "c_notes_edit": ["c_notes_view"],
    "c_websites_edit": ["c_websites_view"],
    "c_relay_unlimited": ["c_relay"],
    "c_vpn_unlimited": ["c_vpn"],
    "manage_permissions": ["see_permissions"],
    "manage_org": ["see_org"],
    "manage_calendar": ["use_calendar"],
    "manage_patching": ["patching"],
    # Whoever may provide media for everyone may use the player themselves.
    "share_media": ["use_media"],
    "c_patch_apply": ["c_patch_view"],
    "edit_source": ["see_source"],
    "delete_source": ["see_source", "edit_source"],
    "relay_unlimited": ["use_relay"],
    "vpn_unlimited": ["use_vpn"],
    "create_scripts": ["use_scripts"],
    # Settings: editing requires viewing, admin settings also cover the
    # default settings.
    "manage_settings": ["see_settings"],
    "admin_settings": ["manage_settings", "see_settings"],
    # Customizing the dashboard requires seeing the dashboard.
    "customize_dashboard": ["see_dashboard"],
    # Deleting recordings requires viewing.
    "delete_replay": ["see_replay"],
    # Screen without a request requires the screen right.
    "c_screen_silent": ["c_screen"],
    # Ticket rights require the read right.
    "ticket_create": ["ticket_read"],
    "ticket_edit": ["ticket_read"],
    "ticket_comment": ["ticket_read"],
    "ticket_assign": ["ticket_read"],
    "ticket_resolve": ["ticket_read"],
    "ticket_delete": ["ticket_read"],
    # Managing inventory requires viewing.
    "manage_inventory": ["see_inventory"],
}


def perm_implies_map() -> dict:
    """Implication map (source -> covered rights) for the frontend."""
    return {k: list(v) for k, v in _PERM_IMPLIES.items()}


def perms_implied_by(perm: str) -> list[str]:
    """Source keys whose 'allow' automatically satisfies the requested right 'perm'."""
    return [src for src, dsts in _PERM_IMPLIES.items() if perm in dsts]


# Legacy group right -> new perm key (for backward compatibility).
_LEGACY_PERM_MAP = {
    "login": "login", "screen": "use_screen", "terminal": "use_terminal",
    "explorer": "use_explorer", "audit": "see_audit",
    "manage_users": "manage_users", "manage_clients": "manage_clients",
    "automation": "automation",
}


# ------------------------------------------------------------------
# PERMISSION GRANTS (tri-state, user/group, global/client)
# ------------------------------------------------------------------

def get_grants(subject_type: str, subject_id: str) -> list[dict]:
    """All grants of ONE subject (user or group)."""
    rows = _conn.execute(
        "SELECT scope, perm, effect FROM permission_grants "
        "WHERE subject_type = ? AND subject_id = ?",
        (subject_type, subject_id),
    ).fetchall()
    return [dict(r) for r in rows]


def set_grants(subject_type: str, subject_id: str, grants: list[dict]) -> None:
    """
    Replaces ALL grants of a subject. 'grants' is a list of
    {scope, perm, effect}. effect must be 'allow' or 'deny'; anything else (or
    omitted combinations) means 'unset' and is not stored.
    """
    _conn.execute(
        "DELETE FROM permission_grants WHERE subject_type = ? AND subject_id = ?",
        (subject_type, subject_id),
    )
    for g in grants:
        scope = g.get("scope") or "global"
        perm = g.get("perm")
        effect = g.get("effect")
        if perm not in PERM_KEYS or effect not in ("allow", "deny"):
            continue
        _conn.execute(
            "INSERT OR REPLACE INTO permission_grants "
            "(id, subject_type, subject_id, scope, perm, effect) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_new_id(), subject_type, subject_id, scope, perm, effect),
        )
    _conn.commit()


def delete_grants_for_client(client_id: str) -> None:
    """Cleans up client-scoped grants when a client is deleted.
    The default client scope ('*') is always kept."""
    if not client_id or client_id == DEFAULT_CLIENT_SCOPE:
        return
    _conn.execute("DELETE FROM permission_grants WHERE scope = ?", (client_id,))
    _conn.commit()


def get_effective_grants(user_id: str) -> list[dict]:
    """
    Collects ALL relevant grants of a user: their own (subject 'user') plus
    those of all groups they are a member of ('group'), plus the legacy group
    rights (groups.permissions) as global allow grants.
    Returns: list of {scope, perm, effect}.
    """
    grants: list[dict] = []
    grants.extend(get_grants("user", user_id))

    group_ids = get_user_group_ids(user_id)
    for gid in group_ids:
        grants.extend(get_grants("group", gid))
        # Legacy: the group\047s old checkbox rights as global allow grants.
        g = get_group(gid)
        if g:
            for p in (g["permissions"] or "").split(","):
                key = _LEGACY_PERM_MAP.get(p.strip())
                if key:
                    grants.append({"scope": "global", "perm": key, "effect": "allow"})
    return grants


def list_groups() -> list[dict]:
    rows = _conn.execute("SELECT * FROM groups ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def get_group(group_id: str) -> dict | None:
    row = _conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
    return dict(row) if row else None


def get_group_by_name(name: str) -> dict | None:
    row = _conn.execute("SELECT * FROM groups WHERE name = ?", (name,)).fetchone()
    return dict(row) if row else None


def upsert_ad_group(name: str) -> dict:
    """
    Creates an AD group (is_ad_group=1) if it does not yet exist and returns it.
    If a group with the same name already exists (whether local or AD), it is
    returned unchanged (names are the matching key between AD groups and RMM
    groups).
    """
    existing = get_group_by_name(name)
    if existing:
        return existing
    return create_group(name, [], is_ad_group=True)


def create_group(name: str, permissions: list[str], is_ad_group: bool = False) -> dict:
    gid = _new_id()
    _conn.execute(
        "INSERT INTO groups (id, name, permissions, is_ad_group, created_at) VALUES (?, ?, ?, ?, ?)",
        (gid, name, ",".join(permissions), 1 if is_ad_group else 0, _now_ms()),
    )
    _conn.commit()
    return get_group(gid)


def set_group_unmanaged(group_id: str, unmanaged: bool) -> dict | None:
    """Marks an (AD) group as unmanaged (1) or managed (0). Unmanaged groups
    appear in a collapsed folder and are NOT assigned automatically on AD
    login."""
    _conn.execute("UPDATE groups SET unmanaged = ? WHERE id = ?",
                  (1 if unmanaged else 0, group_id))
    _conn.commit()
    return get_group(group_id)


def update_group(group_id: str, name: str, permissions: list[str]) -> dict | None:
    _conn.execute(
        "UPDATE groups SET name = ?, permissions = ? WHERE id = ?",
        (name, ",".join(permissions), group_id),
    )
    _conn.commit()
    return get_group(group_id)


def delete_group(group_id: str) -> None:
    _conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))
    _conn.execute("DELETE FROM user_groups WHERE group_id = ?", (group_id,))
    _conn.commit()


def set_user_groups(user_id: str, group_ids: list[str]) -> None:
    _conn.execute("DELETE FROM user_groups WHERE user_id = ?", (user_id,))
    for gid in group_ids:
        _conn.execute("INSERT OR IGNORE INTO user_groups (user_id, group_id) VALUES (?, ?)", (user_id, gid))
    _conn.commit()


def get_user_group_ids(user_id: str) -> list[str]:
    rows = _conn.execute("SELECT group_id FROM user_groups WHERE user_id = ?", (user_id,)).fetchall()
    return [r["group_id"] for r in rows]


def get_user_permissions(user_id: str, role: str) -> set[str]:
    """
    Determines a user's effective rights: admins have ALL rights, otherwise the
    union of the rights of all the user's groups.
    """
    if role == "admin":
        return set(ALL_PERMISSIONS)
    perms: set[str] = set()
    rows = _conn.execute(
        """SELECT g.permissions FROM groups g
           JOIN user_groups ug ON ug.group_id = g.id
           WHERE ug.user_id = ?""",
        (user_id,),
    ).fetchall()
    for r in rows:
        perms.update([p for p in (r["permissions"] or "").split(",") if p])
    return perms


# ------------------------------------------------------------------
# REALMS (directory connection, e.g. Active Directory)
# ------------------------------------------------------------------

def list_realms() -> list[dict]:
    return [dict(r) for r in _conn.execute("SELECT * FROM realms ORDER BY name").fetchall()]


def list_realms_full() -> list[dict]:
    """Like list_realms(), but INCLUDING bind_password - for internal auth use only!"""
    return [dict(r) for r in _conn.execute("SELECT * FROM realms ORDER BY name").fetchall()]


def get_public_realms() -> list[dict]:
    """Only the fields needed for the login dropdown (id + name), without secrets."""
    rows = _conn.execute("SELECT id, name FROM realms WHERE enabled = 1 ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def upsert_ad_user(username: str, display_name: str, realm_id: str) -> dict:
    """
    Creates an AD user locally (if new) or updates their display name. AD users
    have no usable password hash (they always sign in via the directory) and
    the role 'viewer' - their actual rights come via the assigned groups.
    """
    existing = get_user_by_username(username)
    if existing:
        _conn.execute(
            "UPDATE users SET display_name = ?, auth_realm = ? WHERE id = ?",
            (display_name, realm_id, existing["id"]),
        )
        _conn.commit()
        return get_user_by_id(existing["id"])

    user_id = _new_id()
    _conn.execute(
        """INSERT INTO users (id, username, password_hash, display_name, role, must_change_pw, auth_realm, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, username, "!ad-login-no-password!", display_name, "viewer", 0, realm_id, _now_ms()),
    )
    _conn.commit()
    return get_user_by_id(user_id)


def sync_ad_user_groups(user_id: str, ad_group_names: list[str]) -> None:
    """
    Automatically assigns an AD user their AD groups:
      - For each AD group an RMM group is created if needed (is_ad_group=1).
      - Groups marked as "unmanaged" are skipped (they should have nothing to
        do with the RMM).
      - Manually assigned NON-AD groups (local groups) are kept.
    """
    # Keep existing local (non-AD) memberships.
    keep = []
    for gid in get_user_group_ids(user_id):
        g = get_group(gid)
        if g and not g.get("is_ad_group"):
            keep.append(gid)

    ad_ids = []
    for name in ad_group_names:
        g = upsert_ad_group(name)          # creates the AD group if new
        if g.get("unmanaged"):
            continue                        # do not assign unmanaged AD groups
        ad_ids.append(g["id"])

    # Clean up order/duplicates and save.
    set_user_groups(user_id, list(dict.fromkeys(keep + ad_ids)))


def get_realm(realm_id: str) -> dict | None:
    row = _conn.execute("SELECT * FROM realms WHERE id = ?", (realm_id,)).fetchone()
    return dict(row) if row else None


def create_realm(name, server, base_dn, bind_user, bind_password,
                 port=None, use_ssl=False, user_filter=None) -> dict:
    rid = _new_id()
    _conn.execute(
        """INSERT INTO realms
           (id, name, server, base_dn, bind_user, bind_password, port, use_ssl, user_filter, enabled, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
        (rid, name, server, base_dn, bind_user, bind_password,
         port, 1 if use_ssl else 0, user_filter, _now_ms()),
    )
    _conn.commit()
    return dict(_conn.execute("SELECT * FROM realms WHERE id = ?", (rid,)).fetchone())


def update_realm(realm_id: str, fields: dict) -> dict | None:
    """
    Updates a realm. Only known fields are applied. An empty bind_password is
    ignored (so editing does not accidentally delete the stored password when
    the field is left empty).
    """
    allowed = {
        "name", "server", "base_dn", "bind_user", "bind_password",
        "port", "use_ssl", "user_filter", "enabled",
    }
    updates = {}
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "bind_password" and (v is None or v == ""):
            continue  # empty password = leave unchanged
        if k in ("use_ssl", "enabled"):
            v = 1 if v else 0
        updates[k] = v
    if not updates:
        return get_realm(realm_id)
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [realm_id]
    _conn.execute(f"UPDATE realms SET {set_clause} WHERE id = ?", values)
    _conn.commit()
    return get_realm(realm_id)


def delete_realm(realm_id: str) -> None:
    _conn.execute("DELETE FROM realms WHERE id = ?", (realm_id,))
    _conn.commit()


# ------------------------------------------------------------------
# SETTINGS (global key/value store)
# ------------------------------------------------------------------

# Default values for all known settings. get_setting() falls back to this when
# nothing is stored in the DB yet.
DEFAULT_SETTINGS = {
    # Empty = derive automatically from the requested URL (request.base_url).
    # If one sets e.g. "https://rmm.company.com" here, the install commands show
    # this address instead of "localhost". A full URL overrides host/domain/port
    # below.
    "server_url": "",
    # Server IP/host or optional domain and ports (for the install commands).
    "server_host": "",              # IP or hostname
    "server_domain": "",            # optional domain (preferred over the IP)
    "server_backend_port": "4000",  # port of the backend/API
    "server_frontend_port": "4000", # port of the dashboard (frontend)
    # --- Maintenance mode / diagnostics ---
    # Logs EVERYTHING (backend + agents) - for tracking down crashes. Costs
    # space, therefore off by default.
    "maintenance_mode": "0",
    "maintenance_until": "0",
    "maintenance_agents": "0",
    # --- VPN (WireGuard-compatible) ---
    "vpn_enabled": "1",
    # UDP port of the tunnel endpoint. Empty = environment variable VPN_PORT or
    # the WireGuard default 51820.
    "vpn_port": "",
    # Address range from which the tunnel endpoints get their address.
    "vpn_subnet": "10.77.0.0/16",
    # Set ONLY if the VPN endpoint is reachable at a DIFFERENT address than the
    # dashboard. That is the normal case behind a reverse proxy: the proxy does
    # not pass UDP through, so port 51820 must point directly at the server. If
    # the field stays empty, the host is derived from server_url or
    # server_domain/server_host.
    "vpn_endpoint_host": "",
    # Optional DNS server to be used in the tunnel.
    "vpn_dns": "",
    # MTU of the tunnel. 1380 is still safe even behind DSL/PPPoE.
    "vpn_mtu": "1380",
    # Name zone of the virtual network: <hostname>.rmm
    "vpn_zone": "rmm",
    # UDP port of the relay (reachability behind NAT).
    "vpn_relay_port": "51821",
    # Port on which nodes open their own WireGuard.
    # Point DNS in the tunnel at the backend (name resolution in the virtual
    # network). 0 = the client keeps its own DNS.
    "vpn_use_internal_dns": "1",
    # The interval at which a metric point is stored (seconds).
    "metrics_interval_seconds": "10",
    # How long the metric history is kept (hours).
    # 0 = UNLIMITED: nothing is ever deleted automatically - after a browser
    # reload the graphs always show the complete history so far.
    "metrics_retention_hours": "0",
    # --- Host lock: access only via the stored addresses ---
    # "0" = off (default). Deliberately off, because a wrongly set lock costs
    # access; see hostlock.py.
    "host_lock_enabled": "0",
    # 'ui'  = lock only the UI/API, agents still get through
    # 'all' = also check agent connections (Socket.IO)
    "host_lock_scope": "ui",
    # Additionally allowed addresses, comma-separated (e.g. internal DNS name).
    "host_lock_extra": "",
    # Trust X-Forwarded-Host? Only enable if a reverse proxy really sits in
    # front - otherwise anyone can bypass the lock themselves.
    "host_lock_trust_proxy": "0",
    # --- Retention periods (GDPR Art. 5 (1) (e)) ---
    # Everywhere: 0 = unlimited. That is a deliberate decision and should be
    # justified - "forever by accident" is no legal basis. The periods are
    # enforced by privacy.purge() (daily job).
    # How long screen replays are kept (days). The most sensitive data of the
    # system, hence a conservative default.
    "replay_retention_days": "30",
    "retention_audit_days": "90",        # anonymized afterwards, not deleted
    "retention_activity_days": "180",
    "retention_chat_days": "365",
    "retention_todo_archive_days": "365",
    "retention_enrollment_days": "30",
    # Time of the last run (display only).
    "privacy_last_purge": "0",
    # --- Recording settings (screen agent AND Guacamole sessions) ---
    # Master switch: "1" = sessions are recorded as a replay, "0" = off.
    "recording_enabled": "1",
    # Screen-agent replay: JPEG quality (1-100) and max. frames/second.
    "screen_record_quality": "40",
    "screen_record_fps": "5",
    # Guacamole replay (RDP/VNC/SSH/Telnet): quality, FPS, resolution scaling.
    # scale 1.0 = full resolution; 0.5 = half (smaller files).
    "guac_record_quality": "50",
    "guac_record_fps": "8",
    "guac_record_scale": "0.75",
    # Externally hosted Apache guacd (for remote desktop in the browser).
    "guacd_host": "127.0.0.1",
    "guacd_port": "4822",
    # Server self-update from GitHub: "1" = automatic, channel see below.
    "server_auto_update": "0",
    # "commit" = latest commit, "full" = latest full release,
    # "any" = latest release (alpha and full)
    "server_auto_update_channel": "full",
    # Automatic agent update: "1" = outdated agents update themselves on
    # connecting (clients with auto_update='global' follow this setting;
    # 'on'/'off' per client takes precedence). Default off.
    "agent_auto_update": "0",
    # --- Software patching ---
    # How often the agents scan for updates (hours).
    "patch_scan_interval_hours": "24",
    # Master switch for automatic installation. Clients with
    # patch_policy='global' follow it; 'on'/'off' per client overrides.
    "patch_auto_enabled": "0",
    # --- SMTP (email notifications) ---
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_user": "",
    "smtp_password": "",
    "smtp_from": "",
    "smtp_security": "starttls",   # 'starttls' | 'ssl' | 'none'
}


def get_setting(key: str, default: str | None = None) -> str | None:
    row = _conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is not None:
        return row["value"]
    if key in DEFAULT_SETTINGS:
        return DEFAULT_SETTINGS[key]
    return default


def get_int_setting(key: str) -> int:
    """Read a setting as an integer; falls back to the default on nonsense."""
    try:
        return int(float(get_setting(key)))
    except (TypeError, ValueError):
        try:
            return int(float(DEFAULT_SETTINGS.get(key, 0)))
        except (TypeError, ValueError):
            return 0


def get_float_setting(key: str) -> float:
    """Read a setting as a float; falls back to the default on nonsense."""
    try:
        return float(get_setting(key))
    except (TypeError, ValueError):
        try:
            return float(DEFAULT_SETTINGS.get(key, 0))
        except (TypeError, ValueError):
            return 0.0


def get_all_settings() -> dict:
    """All settings (defaults + stored overrides) as a dict."""
    result = dict(DEFAULT_SETTINGS)
    for r in _conn.execute("SELECT key, value FROM settings").fetchall():
        result[r["key"]] = r["value"]
    return result


def set_setting(key: str, value) -> None:
    _conn.execute(
        """INSERT INTO settings (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (key, str(value)),
    )
    _conn.commit()


# ------------------------------------------------------------------
# METRICS HISTORY (persisted graph data)
# ------------------------------------------------------------------

def record_metric_point(
    client_id: str, cpu: float, ram: float, net_in: float, net_out: float,
    ts: int | None = None, extra: str | None = None,
) -> None:
    """Stores a single metric data point for a client.
    extra = JSON string of the complete metric snapshot (optional), so that
    metrics without their own column (ping, temperature, power, ...) also get a
    history."""
    ts = ts if ts is not None else _now_ms()
    _conn.execute(
        """INSERT INTO metrics_history (client_id, ts, cpu, ram, net_in, net_out, extra)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (client_id, ts, cpu, ram, net_in, net_out, extra),
    )
    _conn.commit()


# Upper bounds for the metric history.
#
# THIS IS WHERE THE CRASH of 23.08. HAPPENED: this query ran WITHOUT any limit.
# The setting 'metrics_retention_hours' is 0 in the delivery state, which means
# "keep unlimited" - and on loading the dashboard read a client's COMPLETE
# history. Every heartbeat writes a row with a JSON field every five seconds;
# with seven devices that is around 120,000 rows a day. After a few weeks the
# query took 125 seconds. Because all queries go through the same lock, the
# entire backend was stalled that long - until the watchdog ended it.
#
# Two limits, both needed:
#   * a time window when the caller names none
#   * an upper bound on points, with thinning instead of truncation
#
# Thinning instead of truncation matters: a truncated history shows only the
# beginning and looks as if nothing happened afterwards. A thinned one shows
# the whole period, just coarser - and that is exactly what a chart only a few
# hundred pixels wide needs.
METRICS_DEFAULT_WINDOW_H = 24
METRICS_MAX_POINTS = 3000


def get_metrics_history(client_id: str, since_ts: int | None = None,
                        max_points: int = METRICS_MAX_POINTS) -> list[dict]:
    """
    Data points of a client, ascending by time - always bounded.

    'since_ts' = None no longer means "everything", but the default window
    (24 hours). Whoever really wants more passes a timestamp - and still gets
    at most 'max_points' points.
    """
    if since_ts is None:
        since_ts = int(time.time() * 1000) - METRICS_DEFAULT_WINDOW_H * 3600_000

    # Count first: cheap thanks to the index on (client_id, ts), and the count
    # decides whether thinning is needed.
    row = _conn.execute(
        "SELECT COUNT(*) AS n FROM metrics_history WHERE client_id = ? AND ts >= ?",
        (client_id, since_ts)).fetchone()
    total = int(row["n"] if row else 0)

    if total > max_points:
        # Take every n-th row. ROW_NUMBER() exists since SQLite 3.25, which is
        # included in every supported Python.
        step = (total // max_points) + 1
        rows = _conn.execute(
            """SELECT ts, cpu, ram, net_in, net_out, extra FROM (
                   SELECT ts, cpu, ram, net_in, net_out, extra,
                          ROW_NUMBER() OVER (ORDER BY ts ASC) AS rn
                   FROM metrics_history WHERE client_id = ? AND ts >= ?
               ) WHERE rn % ? = 0 ORDER BY ts ASC LIMIT ?""",
            (client_id, since_ts, step, max_points)).fetchall()
    else:
        rows = _conn.execute(
            """SELECT ts, cpu, ram, net_in, net_out, extra FROM metrics_history
               WHERE client_id = ? AND ts >= ? ORDER BY ts ASC LIMIT ?""",
            (client_id, since_ts, max_points)).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        # Merge extra (JSON snapshot) directly into the point, so the frontend
        # can read every metric (Ping/Temp/Power/...) as a history.
        raw = d.pop("extra", None)
        if raw:
            try:
                snap = json.loads(raw)
                if isinstance(snap, dict):
                    d["m"] = snap
            except (ValueError, TypeError):
                pass
        out.append(d)
    return out


def prune_metrics_history(older_than_ts: int) -> None:
    """Deletes data points older than the timestamp."""
    _conn.execute("DELETE FROM metrics_history WHERE ts < ?", (older_than_ts,))
    _conn.commit()


# ------------------------------------------------------------------
# WEBHOOKS (notifications)
# ------------------------------------------------------------------

# ------------------------------------------------------------------
# Client websites (quick access + uptime monitoring)
# ------------------------------------------------------------------

def list_client_websites(client_id: str) -> list[dict]:
    """All websites bound to a client (for the quick access)."""
    rows = _conn.execute(
        "SELECT * FROM client_websites WHERE client_id = ? ORDER BY favorite DESC, name",
        (client_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_favorite_websites() -> list[dict]:
    """All websites pinned as favorites (across all clients), enriched with the
    hostname of the associated client."""
    rows = _conn.execute(
        """SELECT w.*, c.hostname AS client_hostname
           FROM client_websites w
           JOIN clients c ON c.id = w.client_id
           WHERE w.favorite = 1
           ORDER BY w.name""",
    ).fetchall()
    return [dict(r) for r in rows]


def list_monitored_websites() -> list[dict]:
    """All websites with active uptime monitoring (for the monitor engine)."""
    rows = _conn.execute(
        """SELECT w.*, c.hostname AS client_hostname, c.tenant_id, c.location_id
           FROM client_websites w
           JOIN clients c ON c.id = w.client_id
           WHERE w.monitor_enabled = 1""",
    ).fetchall()
    return [dict(r) for r in rows]


def get_client_website(website_id: str) -> dict | None:
    row = _conn.execute("SELECT * FROM client_websites WHERE id = ?", (website_id,)).fetchone()
    return dict(row) if row else None


def create_client_website(client_id: str, name: str, url: str, favorite: bool = False,
                          monitor_enabled: bool = False, monitor_notify: str = "down",
                          monitor_interval_seconds: int = 300,
                          open_mode: str = "external") -> dict:
    wid = _new_id()
    _conn.execute(
        """INSERT INTO client_websites
           (id, client_id, name, url, favorite, monitor_enabled, monitor_notify,
            monitor_interval_seconds, open_mode, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (wid, client_id, name, url, int(favorite), int(monitor_enabled),
         monitor_notify, int(monitor_interval_seconds), open_mode, _now_ms()),
    )
    _conn.commit()
    return get_client_website(wid)


def update_client_website(website_id: str, fields: dict) -> dict | None:
    """Updates only the provided fields (analogous to update_client)."""
    allowed = {"name", "url", "favorite", "monitor_enabled", "monitor_notify",
               "monitor_interval_seconds", "open_mode"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if fields:
        sets = ", ".join(f"{k} = ?" for k in fields)
        _conn.execute(f"UPDATE client_websites SET {sets} WHERE id = ?",
                      (*fields.values(), website_id))
        _conn.commit()
    return get_client_website(website_id)


def delete_client_website(website_id: str) -> None:
    _conn.execute("DELETE FROM client_websites WHERE id = ?", (website_id,))
    _conn.commit()


def set_website_check_result(website_id: str, status: str, error: str | None = None) -> None:
    """Store the result of an uptime scan ('up'/'down'). Always updates
    last_checked, last_status_change only on an actual change."""
    now = _now_ms()
    prev = _conn.execute("SELECT last_status FROM client_websites WHERE id = ?",
                         (website_id,)).fetchone()
    changed = (prev is None) or (prev["last_status"] != status)
    if changed:
        _conn.execute(
            """UPDATE client_websites SET last_status = ?, last_checked = ?,
               last_status_change = ?, last_error = ? WHERE id = ?""",
            (status, now, now, error, website_id))
    else:
        _conn.execute(
            "UPDATE client_websites SET last_checked = ?, last_error = ? WHERE id = ?",
            (now, error, website_id))
    _conn.commit()


def list_webhooks() -> list[dict]:
    return [dict(r) for r in _conn.execute("SELECT * FROM webhooks ORDER BY name").fetchall()]


def get_webhook(webhook_id: str) -> dict | None:
    row = _conn.execute("SELECT * FROM webhooks WHERE id = ?", (webhook_id,)).fetchone()
    return dict(row) if row else None


def update_webhook(webhook_id: str, fields: dict) -> dict | None:
    """Updates only the provided fields of a webhook."""
    allowed = {"name", "url", "type", "enabled", "headers", "body_template"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        _conn.execute(f"UPDATE webhooks SET {set_clause} WHERE id = ?",
                      (*updates.values(), webhook_id))
        _conn.commit()
    return get_webhook(webhook_id)


def create_webhook(name, url, wtype, headers: str | None = None,
                   body_template: str | None = None) -> dict:
    wid = _new_id()
    _conn.execute(
        "INSERT INTO webhooks (id, name, url, type, headers, body_template, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (wid, name, url, wtype, headers, body_template, _now_ms()),
    )
    _conn.commit()
    return get_webhook(wid)


def delete_webhook(webhook_id: str) -> None:
    _conn.execute("DELETE FROM webhooks WHERE id = ?", (webhook_id,))
    _conn.commit()


# ------------------------------------------------------------------
# AUTOMATIONS (scheduled commands/scripts)
# ------------------------------------------------------------------

def list_automations() -> list[dict]:
    return [dict(r) for r in _conn.execute("SELECT * FROM automations ORDER BY name").fetchall()]


def get_automation(auto_id: str) -> dict | None:
    row = _conn.execute("SELECT * FROM automations WHERE id = ?", (auto_id,)).fetchone()
    return dict(row) if row else None


def create_automation(name, command, client_ids, interval_seconds) -> dict:
    aid = _new_id()
    _conn.execute(
        """INSERT INTO automations (id, name, command, client_ids, interval_seconds, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (aid, name, command, ",".join(client_ids), interval_seconds, _now_ms()),
    )
    _conn.commit()
    return get_automation(aid)


def set_automation_enabled(auto_id: str, enabled: bool) -> None:
    _conn.execute("UPDATE automations SET enabled = ? WHERE id = ?", (1 if enabled else 0, auto_id))
    _conn.commit()


def mark_automation_run(auto_id: str) -> None:
    _conn.execute("UPDATE automations SET last_run = ? WHERE id = ?", (_now_ms(), auto_id))
    _conn.commit()


def record_automation_result(run_id: str, automation_id: str, client_id: str,
                             client_hostname: str | None, stdout: str, stderr: str,
                             exit_code: int | None, ok: bool) -> None:
    """Stores the result of ONE client within an automation run."""
    _conn.execute(
        """INSERT INTO automation_runs
           (id, run_id, automation_id, client_id, client_hostname, started_at,
            stdout, stderr, exit_code, ok)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (_new_id(), run_id, automation_id, client_id, client_hostname, _now_ms(),
         (stdout or "")[:20000], (stderr or "")[:20000], exit_code, 1 if ok else 0),
    )
    _conn.commit()


def list_automation_runs(automation_id: str, limit_runs: int = 20) -> list[dict]:
    """
    Returns the latest runs of an automation, grouped by run_id. Each run
    contains the result list of all clients.
    """
    rows = [dict(r) for r in _conn.execute(
        """SELECT * FROM automation_runs WHERE automation_id = ?
           ORDER BY started_at DESC""", (automation_id,)).fetchall()]
    runs: dict[str, dict] = {}
    order: list[str] = []
    for r in rows:
        rid = r["run_id"]
        if rid not in runs:
            runs[rid] = {"run_id": rid, "started_at": r["started_at"], "results": []}
            order.append(rid)
        runs[rid]["results"].append({
            "client_id": r["client_id"],
            "client_hostname": r["client_hostname"],
            "stdout": r["stdout"],
            "stderr": r["stderr"],
            "exit_code": r["exit_code"],
            "ok": bool(r["ok"]),
            "started_at": r["started_at"],
        })
        runs[rid]["started_at"] = max(runs[rid]["started_at"], r["started_at"])
    return [runs[rid] for rid in order[:limit_runs]]


def prune_automation_runs(older_than_ts: int) -> None:
    _conn.execute("DELETE FROM automation_runs WHERE started_at < ?", (older_than_ts,))
    _conn.commit()


def delete_automation(auto_id: str) -> None:
    _conn.execute("DELETE FROM automations WHERE id = ?", (auto_id,))
    _conn.commit()


def get_due_automations() -> list[dict]:
    """Returns enabled automations whose interval has elapsed since last_run."""
    now = _now_ms()
    due = []
    for a in list_automations():
        if not a["enabled"]:
            continue
        last = a["last_run"] or 0
        if now - last >= a["interval_seconds"] * 1000:
            due.append(a)
    return due
