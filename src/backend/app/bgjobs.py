"""
bgjobs.py - background jobs of the backend (Core)
=================================================

GROUND RULE: tasks (scans, updates, scripts, installs, builds ...) NEVER run in
the browser session, but as a job in the backend. Reloading the page, a second
user or a closed window change nothing about the job - everyone who looks sees
the same state, and the backend itself holds on to the result.

Convert a route to a job (the normal case, one line):

    @router.post("/{cid}/scan")
    async def scan(cid: str, background: bool = False, user=Depends(get_current_user)):
        require_perm(user, "...")
        async def work(job):
            job.update(phase="scan", pct=10, detail="...")
            return await do_the_thing()                  # -> result
        return await bgjobs.run("patch.scan", key=cid, title="Update search PC-01",
                                user=user, perm="patching", work=work, background=background)

  * background=1 (the dashboard): response IMMEDIATELY with the job; progress live.
  * without background (scripts, REST API /api/v1): the request waits for the
    result as before - but the work still runs in the job and is NOT aborted
    when the caller drops the connection.
  * HTTPException in the runner -> same status code for the waiting caller.
  * key: at most ONE running job per (kind, key); a second start attaches to
    the running one. key=None -> every start is its own job.

Live: socket event "bgjob:update" to /dashboard with ONLY {id, kind, key,
status, rev} (the dashboard namespace is not authenticated). Details via REST
(/api/bgjobs), where the rights are checked (perm + may_see + starter).

History: every job is in the table "bgjobs" (start, end, result, error). This
way the result stays visible even after a restart of the backend; jobs that
were still running at the restart are marked as "interrupted" (exception:
phase "restart" = intended restart, e.g. server update).

Frontend helper: /js/app/bgjobs.js (runJob, watchJobs, cancelJob);
overview of all jobs: task display in the taskbar.
"""

import asyncio
import json
import time
import uuid
from typing import Any, Awaitable, Callable

KEEP_FINISHED_SEC = 6 * 3600     # keep finished jobs in memory this long
KEEP_FINISHED_MAX = 120          # ... but at most this many
KEEP_DB_DAYS = 30                # history in the database
MAX_RESULT_BYTES = 512 * 1024    # larger results are not stored
MAX_LOG_LINES = 400
EMIT_INTERVAL = 0.5              # report progress at most 2x per second
HARD_CANCEL_SEC = 30             # if a job does not react to a cancel this long -> abort the task

_jobs: dict[str, "BgJob"] = {}
_table_ready = False


def _now_ms() -> int:
    return int(time.time() * 1000)


# ======================================================================
# Database history
# ======================================================================
def _db():
    from app import db
    return db


def ensure_table() -> None:
    global _table_ready
    if _table_ready:
        return
    db = _db()
    db._conn.execute("""
        CREATE TABLE IF NOT EXISTS bgjobs (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            key TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            perm TEXT NOT NULL DEFAULT '',
            by_user TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            phase TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '',
            error TEXT,
            started INTEGER NOT NULL,
            finished INTEGER,
            meta TEXT NOT NULL DEFAULT '{}',
            result TEXT,
            logs TEXT
        )""")
    db._conn.execute("CREATE INDEX IF NOT EXISTS bgjobs_kind ON bgjobs(kind, key, started)")
    db._conn.commit()
    _table_ready = True


def _persist(job: "BgJob") -> None:
    try:
        ensure_table()
        res = None
        if job.result is not None:
            try:
                res = json.dumps(job.result, default=str)
                if len(res) > MAX_RESULT_BYTES:
                    res = json.dumps({"_truncated": True, "note": "result too large for the history"})
            except Exception:
                res = None
        db = _db()
        db._conn.execute(
            "INSERT OR REPLACE INTO bgjobs (id, kind, key, title, perm, by_user, status, phase, detail, "
            "error, started, finished, meta, result, logs) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job.id, job.kind, job.key, job.title, job.perm or "", job.by, job.status, job.phase,
             job.detail, job.error, job.started, job.finished,
             json.dumps(job.meta, default=str), res,
             "\n".join(job.logs[-MAX_LOG_LINES:]) if job.logs else None))
        db._conn.commit()
    except Exception as e:                       # history is a convenience - never disturb the job
        print(f"[bgjobs] saving history failed: {e}")


def startup_recover() -> None:
    """At start: finish jobs that were still running at the last stop."""
    try:
        ensure_table()
        db = _db()
        now = _now_ms()
        db._conn.execute(
            "UPDATE bgjobs SET status='done', finished=?, detail='Restart completed' "
            "WHERE status='running' AND phase='restart'", (now,))
        db._conn.execute(
            "UPDATE bgjobs SET status='error', finished=?, error='Interrupted: the backend was restarted' "
            "WHERE status='running'", (now,))
        db._conn.execute("DELETE FROM bgjobs WHERE started < ?", (now - KEEP_DB_DAYS * 86400000,))
        db._conn.commit()
    except Exception as e:
        print(f"[bgjobs] recovery failed: {e}")


def _row_public(r: dict, with_result: bool = False) -> dict:
    d = {
        "id": r["id"], "kind": r["kind"], "key": r["key"], "title": r["title"],
        "status": r["status"], "phase": r["phase"], "pct": 100.0 if r["status"] == "done" else None,
        "detail": r["detail"], "error": r["error"], "progress": {}, "by": r["by_user"],
        "started": r["started"], "finished": r["finished"], "cancel_requested": False,
        "cancelled_by": None, "meta": json.loads(r["meta"] or "{}"), "rev": 0,
        "running": False, "has_result": r["result"] is not None, "from_history": True,
    }
    if with_result:
        d["result"] = json.loads(r["result"]) if r["result"] else None
        d["logs"] = (r["logs"] or "").split("\n") if r["logs"] else []
    return d


def history(kind: str | None = None, key: str | None = None, limit: int = 50) -> list[dict]:
    """Finished jobs from the database (excluding those still in memory)."""
    try:
        ensure_table()
        q, args = "SELECT * FROM bgjobs WHERE status != 'running'", []
        if kind:
            q += " AND kind = ?"; args.append(kind)
        if key is not None:
            q += " AND key = ?"; args.append(key)
        q += " ORDER BY started DESC LIMIT ?"; args.append(limit)
        return [dict(r) for r in _db()._conn.execute(q, args).fetchall() if r["id"] not in _jobs]
    except Exception:
        return []


def history_get(job_id: str) -> dict | None:
    try:
        ensure_table()
        r = _db()._conn.execute("SELECT * FROM bgjobs WHERE id = ?", (job_id,)).fetchone()
        return dict(r) if r else None
    except Exception:
        return None


# ======================================================================
# Job
# ======================================================================
class BgJob:
    def __init__(self, kind: str, key: str, title: str, perm: str | None,
                 by: str, may_see: Callable[[dict], bool] | None,
                 on_cancel: Callable[["BgJob"], Any] | None,
                 meta: dict | None, private: bool):
        self.id = uuid.uuid4().hex[:16]
        self.kind, self.key, self.title = kind, key, title
        self.perm, self.by = perm, by
        self.may_see = may_see
        self.on_cancel = on_cancel
        self.private = private           # only the starter (+ admins) see the job
        self.meta = dict(meta or {})
        self.status = "running"          # running | done | error | cancelled
        self.phase = ""
        self.pct: float | None = None
        self.detail = ""
        self.error: str | None = None
        self.error_status = 500          # HTTP status for waiting callers
        self.result: Any = None
        self.progress: dict = {}         # free: counters for the display
        self.logs: list[str] = []
        self.started = _now_ms()
        self.finished: int | None = None
        self.cancel_requested = False
        self.cancelled_by: str | None = None
        self.rev = 0
        self.done = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._last_emit = 0.0
        self._emit_pending = False

    # ---------------- used by the runner ----------------
    @property
    def cancelled(self) -> bool:
        return self.cancel_requested

    def update(self, *, phase: str | None = None, pct: float | None = None,
               detail: str | None = None, **progress) -> None:
        """Set progress (reported live to the dashboards, throttled)."""
        if phase is not None:
            self.phase = phase
        if pct is not None:
            self.pct = max(0.0, min(100.0, float(pct)))
        if detail is not None:
            self.detail = str(detail)[:500]
        if progress:
            self.progress.update(progress)
        if phase == "restart":
            _persist(self)               # record before an intended restart
        self._changed()

    def log(self, line: str) -> None:
        """Append a log line (visible in the task display)."""
        ts = time.strftime("%H:%M:%S")
        for ln in str(line).splitlines() or [""]:
            self.logs.append(f"{ts} {ln}"[:1000])
        if len(self.logs) > MAX_LOG_LINES:
            del self.logs[:-MAX_LOG_LINES]
        self._changed()

    def check_cancel(self) -> None:
        """Call in loops: aborts with CancelledError if requested."""
        if self.cancel_requested:
            raise asyncio.CancelledError()

    # ---------------- output ----------------
    def public(self, with_result: bool = False) -> dict:
        d = {
            "id": self.id, "kind": self.kind, "key": self.key, "title": self.title,
            "status": self.status, "phase": self.phase, "pct": self.pct,
            "detail": self.detail, "error": self.error, "progress": self.progress,
            "by": self.by, "started": self.started, "finished": self.finished,
            "cancel_requested": self.cancel_requested, "cancelled_by": self.cancelled_by,
            "meta": self.meta, "rev": self.rev,
            "running": self.status == "running",
            "has_result": self.result is not None,
            "log_lines": len(self.logs),
        }
        if with_result:
            d["result"] = self.result
            d["logs"] = self.logs[-MAX_LOG_LINES:]
        return d

    def visible_to(self, user: dict) -> bool:
        return _visible(user, self.perm, self.by, self.private, self.may_see,
                        self.meta.get("_client"), self.meta.get("_client_perm"))

    # ---------------- internal ----------------
    def _changed(self, force: bool = False) -> None:
        self.rev += 1
        now = time.monotonic()
        if force or now - self._last_emit >= EMIT_INTERVAL:
            self._last_emit = now
            _schedule_emit(self)
        elif not self._emit_pending:
            self._emit_pending = True
            delay = EMIT_INTERVAL - (now - self._last_emit)

            def _later():
                self._emit_pending = False
                self._last_emit = time.monotonic()
                _schedule_emit(self)
            try:
                asyncio.get_running_loop().call_later(delay, _later)
            except RuntimeError:
                self._emit_pending = False


def _visible(user: dict, perm: str | None, by: str, private: bool,
             may_see: Callable[[dict], bool] | None, client_id: str | None = None,
             client_perm: str | None = None) -> bool:
    """Who may see a job? Admins always, the starter always. Otherwise:
    private -> nobody; right/client access/may_see must match. A job entirely
    WITHOUT such info is visible only to starter + admins (safe default)."""
    from app.auth import can_access_client, is_super_admin, user_has_permission
    try:
        if is_super_admin(user):
            return True
    except Exception:
        pass
    if by and user.get("username") == by:
        return True
    if private:
        return False
    if not (perm or may_see or client_id):
        return False
    try:
        if perm and not user_has_permission(user, perm):
            return False
        if client_id and not can_access_client(user, client_id):
            return False
        if client_id and client_perm and not user_has_permission(user, client_perm, client_id):
            return False
        if may_see:
            return bool(may_see(user))
    except Exception:
        return False
    return True


def row_visible_to(r: dict, user: dict) -> bool:
    """Visibility of a history entry. The job's may_see function no longer
    exists after a restart; therefore right, client(-right) and starter apply -
    and jobs that were restricted only via may_see are then seen only by the
    starter."""
    meta = json.loads(r.get("meta") or "{}")
    perm = r.get("perm") or None
    client = meta.get("_client")
    if not (perm or client) or meta.get("_may_see"):
        return _visible(user, None, r.get("by_user") or "", True, None)
    return _visible(user, perm, r.get("by_user") or "", bool(meta.get("_private")), None,
                    client, meta.get("_client_perm"))


def _schedule_emit(job: BgJob) -> None:
    payload = {"id": job.id, "kind": job.kind, "key": job.key,
               "status": job.status, "rev": job.rev}

    async def _send():
        try:
            from app.sockets import sio
            await sio.emit("bgjob:update", payload, namespace="/dashboard")
        except Exception:
            pass
    try:
        asyncio.get_running_loop().create_task(_send())
    except RuntimeError:
        pass


def _prune() -> None:
    now = _now_ms()
    done = sorted((j.finished or 0, jid) for jid, j in _jobs.items() if j.status != "running")
    for fin, jid in done:
        if now - fin > KEEP_FINISHED_SEC * 1000:
            _jobs.pop(jid, None)
    done = sorted((j.finished or 0, jid) for jid, j in _jobs.items() if j.status != "running")
    for _, jid in done[:-KEEP_FINISHED_MAX] if len(done) > KEEP_FINISHED_MAX else []:
        _jobs.pop(jid, None)


async def _run(job: BgJob, runner: Callable[[BgJob], Awaitable[Any]],
               on_done: Callable[[BgJob], Any] | None) -> None:
    from fastapi import HTTPException
    try:
        job.result = await runner(job)
        job.status = "cancelled" if job.cancel_requested else "done"
    except asyncio.CancelledError:
        # An intended restart (e.g. server update) ends the task - that is a success.
        job.status = "done" if job.phase == "restart" and not job.cancel_requested else "cancelled"
    except HTTPException as e:
        job.status, job.error, job.error_status = "error", str(e.detail), e.status_code
    except Exception as e:                      # the error belongs in the job, not in the log spam
        job.status = "error"
        job.error = str(e) or e.__class__.__name__
    finally:
        job.finished = _now_ms()
        if job.status == "done":
            job.pct = 100.0
        if on_done:
            try:
                r = on_done(job)
                if asyncio.iscoroutine(r):
                    await r
            except Exception as e:
                if job.status == "done":
                    job.status, job.error = "error", f"saving result failed: {e}"
        if job.error:
            job.log(f"ERROR: {job.error}")
        _persist(job)
        job.done.set()
        job._changed(force=True)
        _prune()


# ======================================================================
# Public functions
# ======================================================================
def start(kind: str, key: str | None = None, *, title: str, user: dict | None,
          runner: Callable[[BgJob], Awaitable[Any]], perm: str | None = None,
          may_see: Callable[[dict], bool] | None = None,
          on_cancel: Callable[[BgJob], Any] | None = None,
          on_done: Callable[[BgJob], Any] | None = None,
          meta: dict | None = None, client_id: str | None = None,
          client_perm: str | None = None, private: bool = False) -> tuple[BgJob, bool]:
    """Starts a job - or returns the one already running for (kind, key)
    (same title = same operation -> attach; different title -> 409).
    client_id: job concerns this client -> visible only to users with access
               (client_perm: additionally this client right, e.g. "c_terminal").
    private: only the starter (and admins) see the job.
    Returns: (job, newly_started)."""
    if key is not None and private:
        key = f"{(user or {}).get('username', 'system')}:{key}"   # private jobs per user
    if key is not None:
        running = active(kind, key)
        if running:
            if running.title != title:
                # A DIFFERENT operation on the same object is running -> do not attach silently
                from fastapi import HTTPException
                raise HTTPException(409, f"Already running: {running.title}"
                                         f"{' (started by ' + running.by + ')' if running.by else ''}")
            return running, False
    meta = dict(meta or {})
    if client_id:
        meta["_client"] = client_id
        if client_perm:
            meta["_client_perm"] = client_perm
    if may_see:
        meta["_may_see"] = True          # not checkable after a restart -> history only for the starter
    if private:
        meta["_private"] = True
    job = BgJob(kind, key if key is not None else uuid.uuid4().hex[:10], title, perm,
                (user or {}).get("username", "system"), may_see, on_cancel, meta, private)
    _jobs[job.id] = job
    _persist(job)
    job._task = asyncio.get_running_loop().create_task(_run(job, runner, on_done))
    job._changed(force=True)
    return job, True


async def respond(job: BgJob, new: bool, background: bool) -> Any:
    """HTTP response: background -> job immediately; otherwise wait for the
    result (the job keeps running even if the caller drops the connection)."""
    if background:
        return {**job.public(), "attached": not new, "job": True}
    from fastapi import HTTPException
    await asyncio.shield(job.done.wait())
    if job.status == "error":
        raise HTTPException(job.error_status or 500, job.error or "Error")
    if job.status == "cancelled":
        raise HTTPException(409, f"Cancelled{' by ' + job.cancelled_by if job.cancelled_by else ''}")
    return job.result


async def run(kind: str, key: str | None = None, *, title: str, user: dict | None,
              work: Callable[[BgJob], Awaitable[Any]], background: bool = False, **kw) -> Any:
    """start() + respond() in one call - the normal case in routes."""
    job, new = start(kind, key, title=title, user=user, runner=work, **kw)
    return await respond(job, new, background)


def get(job_id: str) -> BgJob | None:
    return _jobs.get(job_id)


def active(kind: str, key: str | None = None) -> BgJob | None:
    for j in _jobs.values():
        if j.kind == kind and (key is None or j.key == key) and j.status == "running":
            return j
    return None


def latest(kind: str, key: str) -> BgJob | None:
    """Running job, otherwise the most recently finished for (kind, key) (memory only)."""
    run_ = active(kind, key)
    if run_:
        return run_
    cand = [j for j in _jobs.values() if j.kind == kind and j.key == key]
    return max(cand, key=lambda j: j.started) if cand else None


def list_jobs(kind: str | None = None, key: str | None = None) -> list[BgJob]:
    out = [j for j in _jobs.values()
           if (kind is None or j.kind == kind) and (key is None or j.key == key)]
    return sorted(out, key=lambda j: j.started, reverse=True)


async def cancel(job: BgJob, user: dict | None = None) -> bool:
    """Request a cancel. The runner checks job.cancelled; if it does not react,
    the task is hard-aborted after HARD_CANCEL_SEC."""
    if job.status != "running":
        return False
    job.cancel_requested = True
    job.cancelled_by = (user or {}).get("username")
    job.log(f"Cancel requested{' by ' + job.cancelled_by if job.cancelled_by else ''}")
    job._changed(force=True)
    if job.on_cancel:
        try:
            r = job.on_cancel(job)
            if asyncio.iscoroutine(r):
                await r
        except Exception:
            pass

    async def _hard():
        await asyncio.sleep(HARD_CANCEL_SEC)
        if job.status == "running" and job._task:
            job._task.cancel()
    asyncio.get_running_loop().create_task(_hard())
    return True


def hard_cancel(job: BgJob) -> None:
    """on_cancel for jobs without intermediate states: end the task immediately."""
    if job._task and job.status == "running":
        job._task.cancel()


def shutdown() -> None:
    """Abort running jobs when the backend shuts down."""
    for j in list(_jobs.values()):
        if j.status == "running" and j._task:
            j._task.cancel()
