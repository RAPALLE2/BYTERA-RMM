"""
privacy.py
----------
Central place for everything the GDPR requires technically. Deliberately ONE
module so there is exactly one source of truth about where personal data lives
in the system - scattered individual solutions go stale.

Contains:
  * PERSONAL_DATA_MAP  - directory of all tables with a personal reference.
                         Basis for access requests, erasure AND for the report
                         that helps with the record of processing activities
                         (Art. 30).
  * RETENTION          - which retention period applies to which data
                         (Art. 5(1)(e) - storage limitation).
  * export_user_data() - access & data portability (Art. 15 / 20)
  * erase_user()       - erasure (Art. 17), optionally anonymize
  * purge()            - enforce retention periods, runs daily as a job
  * report()           - inventory overview for admins

IMPORTANT scope note: this module handles the TECHNICAL side.
Legal basis, records, DPIA, works agreement and processing contracts are
organizational and live outside the software.
"""

import json
import pathlib
import time
import uuid

from app import db

# ------------------------------------------------------------------
# Directory of personal data
# ------------------------------------------------------------------
# strategy:
#   'delete'    -> the record is removed on erasure
#   'anonymize' -> the record stays (needed for business / evidence duties),
#                  but the personal reference is cut
#
# Commercial or security-related evidence duties can argue against immediate
# erasure (Art. 17(3)(b)/(e)). Therefore the audit log is anonymized instead of
# deleted: the action stays traceable, the person no longer is.

PERSONAL_DATA_MAP = [
    # (table, column with the user ID, label, strategy, name column)
    {"table": "users", "col": "id", "label": "User account",
     "strategy": "delete", "name_col": None},
    {"table": "user_groups", "col": "user_id", "label": "Group membership",
     "strategy": "delete", "name_col": None},
    {"table": "todos", "col": "user_id", "label": "To-dos",
     "strategy": "delete", "name_col": None},
    {"table": "todo_categories", "col": "user_id", "label": "To-do categories",
     "strategy": "delete", "name_col": None},
    {"table": "chat_members", "col": "user_id", "label": "Chat memberships",
     "strategy": "delete", "name_col": None},
    {"table": "chat_messages", "col": "sender_id", "label": "Chat messages",
     "strategy": "delete", "name_col": None},
    {"table": "chat_conversations", "col": "created_by", "label": "Created chats",
     "strategy": "anonymize", "name_col": None},
    {"table": "calendar_events", "col": "created_by", "label": "Created events",
     "strategy": "anonymize", "name_col": "created_by_name"},
    {"table": "calendar_targets", "col": "target_id", "label": "Event assignments",
     "strategy": "delete", "name_col": None, "where": "target_type = 'user'"},
    {"table": "client_notes", "col": "author_id", "label": "Client notes",
     "strategy": "anonymize", "name_col": "author_name"},
    {"table": "note_shares", "col": "user_id", "label": "Note shares",
     "strategy": "delete", "name_col": None},
    {"table": "tickets", "col": "created_by", "label": "Created tickets",
     "strategy": "anonymize", "name_col": None},
    {"table": "ticket_comments", "col": "author_id", "label": "Ticket comments",
     "strategy": "anonymize", "name_col": "author"},
    {"table": "ticket_comment_shares", "col": "user_id", "label": "Comment shares",
     "strategy": "delete", "name_col": None},
    {"table": "ticket_assignees", "col": "subject_id", "label": "Ticket assignments",
     "strategy": "delete", "name_col": None, "where": "subject_type = 'user'"},
    {"table": "media_items", "col": "owner_id", "label": "Media library",
     "strategy": "delete", "name_col": "owner_name"},
    {"table": "ai_connections", "col": "owner_user_id", "label": "AI connections",
     "strategy": "delete", "name_col": None},
    {"table": "ai_connection_shares", "col": "subject_id", "label": "AI shares",
     "strategy": "delete", "name_col": None, "where": "subject_type = 'user'"},
    {"table": "permission_grants", "col": "subject_id", "label": "Permission grants",
     "strategy": "delete", "name_col": None, "where": "subject_type = 'user'"},
    {"table": "org_hierarchy", "col": "child_id", "label": "Org chart placement",
     "strategy": "delete", "name_col": None, "where": "child_type = 'user'"},
    {"table": "activity_log", "col": "actor_id", "label": "Activity log",
     "strategy": "anonymize", "name_col": "actor_name"},
    {"table": "erasure_requests", "col": "user_id", "label": "Erasure requests",
     "strategy": "delete", "name_col": "username"},
]

# Tables that reference the person NOT by ID but by username.
# These are handled separately (see _by_username).
USERNAME_TABLES = [
    {"table": "audit_log", "col": "username", "label": "Audit log",
     "strategy": "anonymize"},
    {"table": "screen_recordings", "col": "username", "label": "Screen recordings",
     "strategy": "delete"},
]

ANON_ID = "geloescht"
ANON_NAME = "Deleted user"


# ------------------------------------------------------------------
# Retention periods
# ------------------------------------------------------------------
# key = setting key, unit = 'days' | 'hours', 0 = unlimited.

RETENTION = [
    {"key": "replay_retention_days", "unit": "days", "default": 30,
     "label": "Screen & session recordings",
     "note": "The most sensitive data set: it shows concrete behaviour at the workplace. "
             "Choose a short period."},
    {"key": "retention_audit_days", "unit": "days", "default": 90,
     "label": "Audit log",
     "note": "Anonymized after expiry, not deleted - the action is kept "
             "as a security record, the person is not."},
    {"key": "retention_activity_days", "unit": "days", "default": 180,
     "label": "Activity log (notes, tickets)"},
    {"key": "retention_chat_days", "unit": "days", "default": 365,
     "label": "Chat messages"},
    {"key": "metrics_retention_hours", "unit": "hours", "default": 0,
     "label": "Client metric history",
     "note": "No direct personal reference, but can reveal working hours."},
    {"key": "retention_todo_archive_days", "unit": "days", "default": 365,
     "label": "Archived to-dos"},
    {"key": "retention_enrollment_days", "unit": "days", "default": 30,
     "label": "Used enrollment tokens"},
]


def _conn():
    return db._conn


def _now() -> int:
    return int(time.time() * 1000)


def _table_exists(name: str) -> bool:
    return _conn().execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (name,)).fetchone() is not None


def _cols(table: str) -> list[str]:
    return [r["name"] for r in _conn().execute(f"PRAGMA table_info({table})").fetchall()]


def retention_days(key: str, default: int) -> int:
    try:
        return int(db.get_setting(key, str(default)) or default)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------
# Access / data portability (Art. 15, Art. 20)
# ------------------------------------------------------------------

def export_user_data(user_id: str) -> dict:
    """
    All data stored about a person as a structured dict.
    Deliberately machine-readable (JSON), as required by Art. 20(1).

    Password hashes are NOT included - they are not subject to access
    requests and disclosing them would be a security risk.
    """
    user = db.get_user_by_id(user_id)
    if not user:
        return {}
    username = user.get("username") or ""

    out: dict = {
        "report_created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "notice": (
            "Access under Art. 15 GDPR / data portability under Art. 20 GDPR. "
            "Contains all data stored about this person in the RMM system."
        ),
        "person": {k: v for k, v in user.items() if k != "password_hash"},
        "data": {},
    }

    for entry in PERSONAL_DATA_MAP:
        table = entry["table"]
        if table == "users" or not _table_exists(table):
            continue
        where = f"{entry['col']} = ?"
        if entry.get("where"):
            where += f" AND {entry['where']}"
        try:
            rows = _conn().execute(
                f"SELECT * FROM {table} WHERE {where}", (user_id,)).fetchall()
        except Exception:
            continue
        if rows:
            out["data"][entry["label"]] = [dict(r) for r in rows]

    for entry in USERNAME_TABLES:
        table = entry["table"]
        if not _table_exists(table):
            continue
        try:
            rows = _conn().execute(
                f"SELECT * FROM {table} WHERE {entry['col']} = ?", (username,)).fetchall()
        except Exception:
            continue
        if rows:
            out["data"][entry["label"]] = [dict(r) for r in rows]

    return out


# ------------------------------------------------------------------
# Erasure (Art. 17)
# ------------------------------------------------------------------

def erase_user(user_id: str, mode: str = "anonymize") -> dict:
    """
    Remove the personal reference of a person.

    mode='anonymize' (recommended): records with an evidence function stay,
        the reference to the person is cut. A clean implementation of Art. 17
        while respecting paragraph 3.
    mode='hard': everything is deleted, including this person's audit log.
        Only use when no evidence duty stands against it.

    Returns a report (table -> number of affected rows) that helps prove the
    erasure.
    """
    user = db.get_user_by_id(user_id)
    if not user:
        return {"error": "User not found"}
    username = user.get("username") or ""
    report: dict[str, int] = {}
    c = _conn()

    for entry in PERSONAL_DATA_MAP:
        table, col = entry["table"], entry["col"]
        if table == "users" or not _table_exists(table):
            continue
        where = f"{col} = ?"
        if entry.get("where"):
            where += f" AND {entry['where']}"
        strategy = entry["strategy"]
        if mode == "hard":
            strategy = "delete"
        try:
            if strategy == "delete":
                cur = c.execute(f"DELETE FROM {table} WHERE {where}", (user_id,))
            else:
                sets = [f"{col} = ?"]
                params: list = [ANON_ID]
                if entry.get("name_col") and entry["name_col"] in _cols(table):
                    sets.append(f"{entry['name_col']} = ?")
                    params.append(ANON_NAME)
                params.append(user_id)
                cur = c.execute(
                    f"UPDATE {table} SET {', '.join(sets)} WHERE {where}", params)
            if cur.rowcount:
                report[entry["label"]] = cur.rowcount
        except Exception as e:
            report[f"{entry['label']} (error)"] = str(e)

    # Tables referenced by username
    for entry in USERNAME_TABLES:
        table, col = entry["table"], entry["col"]
        if not _table_exists(table):
            continue
        strategy = "delete" if mode == "hard" else entry["strategy"]
        try:
            if entry["table"] == "screen_recordings":
                # First the files from disk, then the database rows.
                rows = c.execute(
                    "SELECT file_path FROM screen_recordings WHERE username = ?",
                    (username,)).fetchall()
                for r in rows:
                    _delete_recording_file(r["file_path"])
            if strategy == "delete":
                cur = c.execute(f"DELETE FROM {table} WHERE {col} = ?", (username,))
            else:
                cur = c.execute(
                    f"UPDATE {table} SET {col} = ? WHERE {col} = ?", (ANON_NAME, username))
            if cur.rowcount:
                report[entry["label"]] = cur.rowcount
        except Exception as e:
            report[f"{entry['label']} (error)"] = str(e)

    c.execute("DELETE FROM users WHERE id = ?", (user_id,))
    c.commit()
    report["User account"] = 1

    # The erasure itself is logged - without the name of the deleted
    # person, otherwise it would reappear via the log.
    db.add_audit_entry("system", "privacy.user_erased",
                       details=f"Mode: {mode}, affected areas: {len(report)}")
    return report


# ------------------------------------------------------------------
# Enforce retention periods (Art. 5(1)(e))
# ------------------------------------------------------------------

def purge() -> dict:
    """
    Apply all configured retention periods. Runs daily as a background job and
    can be triggered manually by admins.
    Returns what was actually removed.
    """
    c = _conn()
    now = _now()
    result: dict[str, int] = {}

    def cutoff(key: str, default: int, unit: str = "days") -> int | None:
        v = retention_days(key, default)
        if v <= 0:
            return None                       # 0 = keep indefinitely
        factor = 3600_000 if unit == "hours" else 86400_000
        return now - v * factor

    # --- Recordings (file + DB row) ---
    cut = cutoff("replay_retention_days", 30)
    if cut is not None and _table_exists("screen_recordings"):
        rows = c.execute(
            "SELECT id, file_path FROM screen_recordings WHERE started_at < ?",
            (cut,)).fetchall()
        for r in rows:
            _delete_recording_file(r["file_path"])
        c.execute("DELETE FROM screen_recordings WHERE started_at < ?", (cut,))
        result["Recordings"] = len(rows)

    # --- Audit log: anonymize instead of delete ---
    cut = cutoff("retention_audit_days", 90)
    if cut is not None and _table_exists("audit_log"):
        cur = c.execute(
            "UPDATE audit_log SET username = ? WHERE ts < ? AND username != ?",
            (ANON_NAME, cut, ANON_NAME))
        result["Audit log anonymized"] = cur.rowcount

    # --- Activity log ---
    cut = cutoff("retention_activity_days", 180)
    if cut is not None and _table_exists("activity_log"):
        cur = c.execute("DELETE FROM activity_log WHERE created_at < ?", (cut,))
        result["Activity log"] = cur.rowcount

    # --- Chat messages ---
    cut = cutoff("retention_chat_days", 365)
    if cut is not None and _table_exists("chat_messages"):
        cur = c.execute("DELETE FROM chat_messages WHERE created_at < ?", (cut,))
        result["Chat messages"] = cur.rowcount

    # --- Metric history ---
    cut = cutoff("metrics_retention_hours", 0, unit="hours")
    if cut is not None and _table_exists("metrics_history"):
        cur = c.execute("DELETE FROM metrics_history WHERE ts < ?", (cut,))
        result["Metric history"] = cur.rowcount

    # --- Archived to-dos ---
    cut = cutoff("retention_todo_archive_days", 365)
    if cut is not None and _table_exists("todos"):
        cur = c.execute(
            "DELETE FROM todos WHERE updated_at < ? AND category_id IN"
            " (SELECT id FROM todo_categories WHERE builtin = 'archive')", (cut,))
        result["Archived to-dos"] = cur.rowcount

    # --- Used enrollment tokens ---
    cut = cutoff("retention_enrollment_days", 30)
    if cut is not None and _table_exists("enrollment_tokens"):
        cur = c.execute("DELETE FROM enrollment_tokens WHERE created_at < ?", (cut,))
        result["Enrollment tokens"] = cur.rowcount

    c.commit()

    # --- Orphan recording files without a DB entry ---
    result["Orphan files"] = _purge_orphan_files()

    total = sum(v for v in result.values() if isinstance(v, int))
    if total:
        db.add_audit_entry("system", "privacy.purge",
                           details=f"{total} records removed after retention expiry")
    db.set_setting("privacy_last_purge", str(now))
    return result


def _recordings_dir() -> pathlib.Path:
    from app.paths import RECORDINGS_DIR
    return RECORDINGS_DIR


def _local_file(stored_path: str) -> pathlib.Path | None:
    """
    Find the ACTUAL local file for a path stored in the DB.

    The DB stores absolute paths of the machine that recorded (e.g. Windows
    paths). If the installation is moved, migrated or the database is loaded
    elsewhere via dbsync, those point nowhere. The file name, on the other
    hand, is stable (UUID + timestamp), so it is resolved primarily via that.
    """
    if not stored_path:
        return None
    name = stored_path.replace("\\", "/").rsplit("/", 1)[-1]
    if not name:
        return None
    local = _recordings_dir() / name
    if local.is_file():
        return local
    direct = pathlib.Path(stored_path)
    return direct if direct.is_file() else None


def _delete_recording_file(stored_path: str) -> bool:
    f = _local_file(stored_path)
    if not f:
        return False
    try:
        f.unlink()
        return True
    except Exception:
        return False


def _purge_orphan_files() -> int:
    """
    Files in the recordings folder that no longer have a DB entry.
    Without this, videos stay on disk after erasures - and they remain
    personal data.

    SAFETY BRAKE: comparison is only by file name. If NO single file matches a
    DB entry although both sides are filled, this is most likely a path/mount
    problem and not a folder full of orphans - then nothing is touched. Better
    a few left over than an accidentally deleted set.
    """
    folder = _recordings_dir()
    if not folder.is_dir():
        return 0
    files = [f for f in folder.iterdir() if f.is_file()]
    if not files:
        return 0

    known = set()
    if _table_exists("screen_recordings"):
        for r in _conn().execute("SELECT file_path FROM screen_recordings").fetchall():
            p = (r["file_path"] or "").replace("\\", "/").rsplit("/", 1)[-1]
            if p:
                known.add(p)

    if known and not any(f.name in known for f in files):
        print("[privacy] Orphan cleanup skipped: no file name matches a"
              " database entry (probably a moved path).")
        return 0

    removed = 0
    for f in files:
        if f.name in known:
            continue
        try:
            f.unlink()
            removed += 1
        except Exception:
            pass
    return removed


# ------------------------------------------------------------------
# Inventory overview (helps with the record under Art. 30)
# ------------------------------------------------------------------

def report() -> dict:
    """Which data sets exist, how large, how old the oldest entry is."""
    c = _conn()
    items = []
    ts_cols = {
        "audit_log": "ts", "activity_log": "created_at", "chat_messages": "created_at",
        "screen_recordings": "started_at", "metrics_history": "ts",
        "todos": "created_at", "enrollment_tokens": "created_at",
        "calendar_events": "created_at", "client_notes": "created_at",
    }
    for entry in PERSONAL_DATA_MAP + USERNAME_TABLES:
        table = entry["table"]
        if not _table_exists(table):
            continue
        try:
            count = c.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        except Exception:
            continue
        oldest = None
        col = ts_cols.get(table)
        if col and col in _cols(table) and count:
            row = c.execute(f"SELECT MIN({col}) AS m FROM {table}").fetchone()
            oldest = row["m"]
        items.append({
            "table": table, "label": entry["label"], "count": count,
            "oldest": oldest, "strategy": entry["strategy"],
        })

    folder = _recordings_dir()
    files, size = 0, 0
    if folder.is_dir():
        for f in folder.iterdir():
            if f.is_file():
                files += 1
                try:
                    size += f.stat().st_size
                except Exception:
                    pass

    last = db.get_setting("privacy_last_purge", "0")
    return {
        "items": sorted(items, key=lambda x: -x["count"]),
        "recording_files": files,
        "recording_bytes": size,
        "last_purge": int(last or 0),
        "retention": [
            {**r, "value": retention_days(r["key"], r["default"])} for r in RETENTION
        ],
    }


# ------------------------------------------------------------------
# Erasure requests (self-service for data subjects)
# ------------------------------------------------------------------

def create_erasure_request(user: dict, kind: str, reason: str) -> str:
    """
    Data subjects can request erasure themselves. The request is not executed
    immediately - an admin must review it, because erasing an active account
    has operational consequences and Art. 17(3) has exceptions. Art. 12(3)
    allows one month for this.
    """
    rid = str(uuid.uuid4())
    _conn().execute(
        "INSERT INTO erasure_requests (id, user_id, username, kind, reason,"
        " status, created_at, handled_at, handled_by)"
        " VALUES (?, ?, ?, ?, ?, 'open', ?, NULL, '')",
        (rid, user["id"], user.get("username", ""), kind, reason[:2000], _now()))
    _conn().commit()
    db.add_audit_entry(user.get("username"), "privacy.erasure_requested",
                       details=f"Kind: {kind}")
    return rid


def list_erasure_requests(status: str | None = None) -> list[dict]:
    sql = "SELECT * FROM erasure_requests"
    params: tuple = ()
    if status:
        sql += " WHERE status = ?"
        params = (status,)
    sql += " ORDER BY created_at DESC"
    return [dict(r) for r in _conn().execute(sql, params).fetchall()]
