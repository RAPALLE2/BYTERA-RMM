"""Automation engine: runs due scheduled commands on the target clients.

Each run of an automation runs as a BACKEND JOB (app/bgjobs.py, kind
"automation.run") - it appears live in the task view, with per-client progress
and a log; the result/history are additionally kept in the DB as before.
"""
import asyncio as _asyncio
import uuid as _uuid

from app import bgjobs, db
from app.sockets import request_exec as _request_exec


async def _run_automation(job, auto: dict) -> dict:
    client_ids = [c for c in (auto["client_ids"] or "").split(",") if c]
    run_id = _uuid.uuid4().hex   # groups this run
    total, ok_n, fail_n = len(client_ids), 0, 0
    job.update(phase="exec", pct=0, detail=f"0/{total} clients", total=total)
    for i, cid in enumerate(client_ids):
        if job.cancelled:
            job.log("Cancelled - remaining clients are skipped")
            break
        hostname = None
        try:
            c = await db.call(db.get_client, cid)
            hostname = c["hostname"] if c else None
        except Exception:
            pass
        try:
            res = await _request_exec(cid, auto["command"], timeout_seconds=60)
            await db.call(db.record_automation_result,
                          run_id, auto["id"], cid, hostname,
                          res.get("stdout", ""), res.get("stderr", ""),
                          res.get("code"), ok=True)
            ok_n += 1
            job.log(f"{hostname or cid}: Exit {res.get('code')}")
        except Exception as e:
            print(f"[automation] '{auto['name']}' auf {cid} fehlgeschlagen: {e}")
            await db.call(db.record_automation_result,
                          run_id, auto["id"], cid, hostname,
                          "", str(e), None, ok=False)
            fail_n += 1
            job.log(f"{hostname or cid}: FEHLER {e}")
        job.update(pct=(i + 1) / max(1, total) * 100,
                   detail=f"{i + 1}/{total} clients · {ok_n} ok · {fail_n} failed", done=i + 1)
    await db.call(db.mark_automation_run, auto["id"])
    await db.call(db.add_audit_entry, "system", "automation.executed", target=auto["id"], details=auto["name"])
    return {"run_id": run_id, "ok": ok_n, "failed": fail_n, "total": total}


async def run():
    while True:
        try:
            for auto in await db.call(db.get_due_automations, ):
                job, new = bgjobs.start(
                    "automation.run", auto["id"], title=f"Automation „{auto['name']}“",
                    user=None, perm="automation",
                    runner=lambda j, auto=auto: _run_automation(j, auto),
                    meta={"automation": auto["id"]})
                if new:
                    await job.done.wait()          # nacheinander, wie bisher
        except Exception as e:
            print(f"[automation] engine error: {e}")
        await _asyncio.sleep(30)  # check every 30 seconds
