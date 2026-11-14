"""
bgjobs.py - Hintergrund-Jobs des Backends (Core)
=================================================

GRUNDREGEL: Aufgaben (Scans, Updates, Skripte, Installationen, Builds ...)
laufen NIE in der Browser-Sitzung, sondern als Job im Backend. Die Seite neu
laden, ein zweiter Benutzer oder ein geschlossenes Fenster aendern nichts am
Job - jeder, der hinschaut, sieht denselben Stand, und das Ergebnis haelt das
Backend selbst fest.

Eine Route auf einen Job umstellen (der Normalfall, eine Zeile):

    @router.post("/{cid}/scan")
    async def scan(cid: str, background: bool = False, user=Depends(get_current_user)):
        require_perm(user, "...")
        async def work(job):
            job.update(phase="scan", pct=10, detail="...")
            return await do_the_thing()                  # -> Ergebnis
        return await bgjobs.run("patch.scan", key=cid, title="Update-Suche PC-01",
                                user=user, perm="patching", work=work, background=background)

  * background=1 (das Dashboard): Antwort SOFORT mit dem Job; Fortschritt live.
  * ohne background (Skripte, REST-API /api/v1): Die Anfrage wartet wie frueher
    auf das Ergebnis - die Arbeit laeuft aber trotzdem im Job und wird NICHT
    abgebrochen, wenn der Aufrufer die Verbindung trennt.
  * HTTPException im Runner -> gleicher Statuscode beim wartenden Aufrufer.
  * key: hoechstens EIN laufender Job je (kind, key); ein zweiter Start haengt
    sich an den laufenden an. key=None -> jeder Start ist ein eigener Job.

Live: Socket-Event "bgjob:update" an /dashboard mit NUR {id, kind, key,
status, rev} (der Dashboard-Namespace ist nicht angemeldet). Details per REST
(/api/bgjobs), dort werden die Rechte geprueft (perm + may_see + Starter).

Verlauf: Jeder Job steht in der Tabelle "bgjobs" (Start, Ende, Ergebnis,
Fehler). So bleibt das Ergebnis auch nach einem Neustart des Backends
sichtbar; Jobs, die beim Neustart noch liefen, werden als "unterbrochen"
markiert (Ausnahme: Phase "restart" = gewollter Neustart, z.B. Server-Update).

Frontend-Helfer: /js/app/bgjobs.js (runJob, watchJobs, cancelJob);
Uebersicht aller Jobs: Aufgaben-Anzeige in der Taskleiste.
"""

import asyncio
import json
import time
import uuid
from typing import Any, Awaitable, Callable

KEEP_FINISHED_SEC = 6 * 3600     # fertige Jobs so lange im Speicher
KEEP_FINISHED_MAX = 120          # ... aber hoechstens so viele
KEEP_DB_DAYS = 30                # Verlauf in der Datenbank
MAX_RESULT_BYTES = 512 * 1024    # groessere Ergebnisse werden nicht gespeichert
MAX_LOG_LINES = 400
EMIT_INTERVAL = 0.5              # Fortschritt hoechstens 2x pro Sekunde melden
HARD_CANCEL_SEC = 30             # reagiert ein Job so lange nicht auf Abbruch -> Task abbrechen

_jobs: dict[str, "BgJob"] = {}
_table_ready = False


def _now_ms() -> int:
    return int(time.time() * 1000)


# ======================================================================
# Datenbank-Verlauf
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
                    res = json.dumps({"_truncated": True, "note": "Ergebnis zu gross für den Verlauf"})
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
    except Exception as e:                       # Verlauf ist Komfort - nie den Job stoeren
        print(f"[bgjobs] Verlauf speichern fehlgeschlagen: {e}")


def startup_recover() -> None:
    """Beim Start: Jobs, die beim letzten Stopp noch liefen, abschliessen."""
    try:
        ensure_table()
        db = _db()
        now = _now_ms()
        db._conn.execute(
            "UPDATE bgjobs SET status='done', finished=?, detail='Neustart abgeschlossen' "
            "WHERE status='running' AND phase='restart'", (now,))
        db._conn.execute(
            "UPDATE bgjobs SET status='error', finished=?, error='Unterbrochen: Das Backend wurde neu gestartet' "
            "WHERE status='running'", (now,))
        db._conn.execute("DELETE FROM bgjobs WHERE started < ?", (now - KEEP_DB_DAYS * 86400000,))
        db._conn.commit()
    except Exception as e:
        print(f"[bgjobs] Wiederherstellung fehlgeschlagen: {e}")


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
    """Beendete Jobs aus der Datenbank (ohne die, die noch im Speicher sind)."""
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
        self.private = private           # nur Starter (+ Admins) sehen den Job
        self.meta = dict(meta or {})
        self.status = "running"          # running | done | error | cancelled
        self.phase = ""
        self.pct: float | None = None
        self.detail = ""
        self.error: str | None = None
        self.error_status = 500          # HTTP-Status fuer wartende Aufrufer
        self.result: Any = None
        self.progress: dict = {}         # frei: Zaehler fuer die Anzeige
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

    # ---------------- vom Runner benutzt ----------------
    @property
    def cancelled(self) -> bool:
        return self.cancel_requested

    def update(self, *, phase: str | None = None, pct: float | None = None,
               detail: str | None = None, **progress) -> None:
        """Fortschritt setzen (wird gedrosselt live an die Dashboards gemeldet)."""
        if phase is not None:
            self.phase = phase
        if pct is not None:
            self.pct = max(0.0, min(100.0, float(pct)))
        if detail is not None:
            self.detail = str(detail)[:500]
        if progress:
            self.progress.update(progress)
        if phase == "restart":
            _persist(self)               # vor einem gewollten Neustart festhalten
        self._changed()

    def log(self, line: str) -> None:
        """Eine Log-Zeile anhaengen (sichtbar in der Aufgaben-Anzeige)."""
        ts = time.strftime("%H:%M:%S")
        for ln in str(line).splitlines() or [""]:
            self.logs.append(f"{ts} {ln}"[:1000])
        if len(self.logs) > MAX_LOG_LINES:
            del self.logs[:-MAX_LOG_LINES]
        self._changed()

    def check_cancel(self) -> None:
        """In Schleifen aufrufen: bricht mit CancelledError ab, wenn angefordert."""
        if self.cancel_requested:
            raise asyncio.CancelledError()

    # ---------------- Ausgabe ----------------
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

    # ---------------- intern ----------------
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
    """Wer darf einen Job sehen? Admins immer, der Starter immer. Sonst:
    privat -> niemand; Recht/Client-Zugriff/may_see muessen passen. Ein Job ganz
    OHNE solche Angaben ist nur fuer Starter + Admins sichtbar (sicherer Standard)."""
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
    """Sichtbarkeit eines Verlaufs-Eintrags. Die may_see-Funktion des Jobs gibt es
    nach dem Neustart nicht mehr; deshalb gelten Recht, Client(-Recht) und Starter -
    und Jobs, die nur ueber may_see eingeschraenkt waren, sieht dann nur der Starter."""
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
        # Gewollter Neustart (z.B. Server-Update) beendet die Task - das ist Erfolg.
        job.status = "done" if job.phase == "restart" and not job.cancel_requested else "cancelled"
    except HTTPException as e:
        job.status, job.error, job.error_status = "error", str(e.detail), e.status_code
    except Exception as e:                      # Fehler gehoert in den Job, nicht in den Log-Spam
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
                    job.status, job.error = "error", f"Ergebnis speichern fehlgeschlagen: {e}"
        if job.error:
            job.log(f"FEHLER: {job.error}")
        _persist(job)
        job.done.set()
        job._changed(force=True)
        _prune()


# ======================================================================
# Oeffentliche Funktionen
# ======================================================================
def start(kind: str, key: str | None = None, *, title: str, user: dict | None,
          runner: Callable[[BgJob], Awaitable[Any]], perm: str | None = None,
          may_see: Callable[[dict], bool] | None = None,
          on_cancel: Callable[[BgJob], Any] | None = None,
          on_done: Callable[[BgJob], Any] | None = None,
          meta: dict | None = None, client_id: str | None = None,
          client_perm: str | None = None, private: bool = False) -> tuple[BgJob, bool]:
    """Startet einen Job - oder liefert den schon laufenden fuer (kind, key)
    (gleicher Titel = gleicher Vorgang -> anhaengen; anderer Titel -> 409).
    client_id: Job betrifft diesen Client -> nur sichtbar fuer Benutzer mit Zugriff
               (client_perm: zusaetzlich dieses Client-Recht, z.B. "c_terminal").
    private: nur der Starter (und Admins) sehen den Job.
    Rueckgabe: (job, neu_gestartet)."""
    if key is not None and private:
        key = f"{(user or {}).get('username', 'system')}:{key}"   # private Jobs je Benutzer
    if key is not None:
        running = active(kind, key)
        if running:
            if running.title != title:
                # Ein ANDERER Vorgang am selben Objekt laeuft -> nicht still anhaengen
                from fastapi import HTTPException
                raise HTTPException(409, f"Es läuft bereits: {running.title}"
                                         f"{' (gestartet von ' + running.by + ')' if running.by else ''}")
            return running, False
    meta = dict(meta or {})
    if client_id:
        meta["_client"] = client_id
        if client_perm:
            meta["_client_perm"] = client_perm
    if may_see:
        meta["_may_see"] = True          # nach Neustart nicht pruefbar -> Verlauf nur fuer Starter
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
    """HTTP-Antwort: background -> Job sofort; sonst auf das Ergebnis warten
    (der Job laeuft weiter, auch wenn der Aufrufer die Verbindung trennt)."""
    if background:
        return {**job.public(), "attached": not new, "job": True}
    from fastapi import HTTPException
    await asyncio.shield(job.done.wait())
    if job.status == "error":
        raise HTTPException(job.error_status or 500, job.error or "Fehler")
    if job.status == "cancelled":
        raise HTTPException(409, f"Abgebrochen{' von ' + job.cancelled_by if job.cancelled_by else ''}")
    return job.result


async def run(kind: str, key: str | None = None, *, title: str, user: dict | None,
              work: Callable[[BgJob], Awaitable[Any]], background: bool = False, **kw) -> Any:
    """start() + respond() in einem Aufruf - der Normalfall in Routen."""
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
    """Laufender Job, sonst der zuletzt beendete fuer (kind, key) (nur Speicher)."""
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
    """Abbruch anfordern. Der Runner prueft job.cancelled; reagiert er nicht,
    wird die Task nach HARD_CANCEL_SEC hart abgebrochen."""
    if job.status != "running":
        return False
    job.cancel_requested = True
    job.cancelled_by = (user or {}).get("username")
    job.log(f"Abbruch angefordert{' von ' + job.cancelled_by if job.cancelled_by else ''}")
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
    """on_cancel fuer Jobs ohne Zwischenstaende: Task sofort beenden."""
    if job._task and job.status == "running":
        job._task.cancel()


def shutdown() -> None:
    """Beim Beenden des Backends laufende Jobs abbrechen."""
    for j in list(_jobs.values()):
        if j.status == "running" and j._task:
            j._task.cancel()
