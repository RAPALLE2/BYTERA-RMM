"""
scheduler.py (Modul "backup") - Zeitplan wie bei Proxmox-Backup-Jobs.

Zeitplan eines Jobs:
  {"type": "daily",  "time": "02:00"}
  {"type": "weekly", "time": "21:00", "days": [1, 3, 5]}     (1 = Montag)
  {"type": "hourly", "every": 4}                             (alle 4 Stunden)
  {"type": "manual"}                                         (nur per Hand)

Je Client merkt sich der Job den zuletzt bedienten Termin ("runs"). Ist ein
Client beim Termin offline, wird das Backup - wie "Verpasste wiederholen"
bei Proxmox - nachgeholt, sobald er online kommt (repeat_missed). Ohne
diese Option wird der Termin nach 15 Minuten als "verpasst" vermerkt.
"""

from __future__ import annotations

import asyncio
import time

from rmm_mod_backup import engine, store


def last_slot(schedule: dict, now: float | None = None) -> float | None:
    """Zeitpunkt (Unix-s) des zuletzt faelligen Termins - None = nie faellig."""
    now = now or time.time()
    typ = (schedule or {}).get("type", "manual")
    if typ == "hourly":
        every = max(1, int(schedule.get("every") or 1)) * 3600
        lt = time.localtime(now)
        midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        return midnight + ((now - midnight) // every) * every
    if typ not in ("daily", "weekly"):
        return None
    try:
        hh, mm = (int(x) for x in str(schedule.get("time") or "02:00").split(":"))
    except ValueError:
        hh, mm = 2, 0
    days = {int(d) for d in (schedule.get("days") or [])} if typ == "weekly" else set(range(1, 8))
    if not days:
        return None
    for back in range(0, 8):
        t = now - back * 86400
        lt = time.localtime(t)
        slot = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))
        if slot <= now and (lt.tm_wday + 1) in days:
            return slot
    return None


_running: set = set()


async def tick(log) -> None:
    from app.sockets import state
    now = time.time()
    for jid, job in store.jobs().items():
        if not job.get("enabled", True):
            continue
        slot = last_slot(job.get("schedule") or {}, now)
        if slot is None or slot < (job.get("created") or 0) / 1000:
            continue
        runs = dict(job.get("runs") or {})
        changed = False
        for cid in engine.job_targets({**job, "id": jid}):
            if runs.get(cid, 0) >= slot or (jid, cid) in _running:
                continue
            if not state.is_online(cid) and job.get("engine", "agent") == "agent":
                if not job.get("repeat_missed", True) and now - slot > 900:
                    runs[cid] = slot
                    changed = True
                    log(f"Job „{job.get('name')}“: {cid} offline - Termin verpasst")
                continue
            runs[cid] = slot
            changed = True
            _running.add((jid, cid))
            asyncio.get_running_loop().create_task(_run(jid, {**job, "id": jid}, cid, log))
        if changed:
            store.update_job_state(jid, runs=runs)


async def _run(jid: str, job: dict, cid: str, log) -> None:
    """Startet den Lauf als Hintergrund-Job (sichtbar in der Aufgaben-Anzeige).
    Status und Sperre werden IM Job gepflegt - background=True kehrt sofort zurueck."""
    from app import bgjobs, db
    client = db.get_client(cid) or {}
    host = client.get("short_name") or client.get("hostname") or cid

    async def work(bj):
        def plog(line):
            bj.log(line)
            bj.detail = line
        try:
            res = await engine.run_job(job, cid, log=plog)
            store.update_job_state(jid, last_run=int(time.time() * 1000), last_status="ok")
            return res
        except Exception as exc:
            store.update_job_state(jid, last_run=int(time.time() * 1000), last_status=f"Fehler: {exc}")
            raise
        finally:
            _running.discard((jid, cid))

    try:
        await bgjobs.run("backup", f"{jid}:{cid}", title=f"Backup „{job.get('name')}“: {host}",
                         user={"username": "Zeitplan", "role": "admin"}, work=work,
                         background=True, client_id=cid)
    except Exception as exc:
        _running.discard((jid, cid))
        log(f"Job {jid} / {cid}: {exc}")


async def loop(log) -> None:
    while True:
        await asyncio.sleep(60)
        try:
            await tick(log)
        except Exception as exc:
            log(f"Zeitplan-Fehler: {exc}")
