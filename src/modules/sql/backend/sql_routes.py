# sql_routes.py (module "sql")
# ---------------------------
# Manage the database (formerly core: router/database_routes.py). The URLs are
# unchanged so api.js and existing scripts keep working.
#
# Permission: "admin_settings" (admin settings: IP/port, server update, DB …).
#
#   GET  /api/admin/database/info            mode + configuration (without password)
#   POST /api/admin/database/test            test the connection
#   POST /api/admin/database/switch          switch mode (copy data, restart)
#   GET  /api/admin/database/progress        progress of the switch
#   GET  /api/admin/database/backups         backups in backend/data/backup
#   POST /api/admin/database/backup          create a backup now
#   POST /api/admin/database/restore-backup  restore a backup (restart)
#   GET  /api/admin/database/docker          Docker database: state + credentials
#                                            (only useful when the backend runs in a container)

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import bgjobs, db, dbsync
from app.auth import get_current_user, require_perm

from . import sqlcore

router = APIRouter(prefix="/api/admin/database", tags=["database"])


def _perm(user: dict) -> None:
    require_perm(user, "admin_settings")


class DbConfigBody(BaseModel):
    type: str = "mysql"            # mysql | postgres | sqlite
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = ""
    password: str = ""             # leer = gespeichertes Passwort behalten
    database: str = ""


class SwitchBody(DbConfigBody):
    mode: str                      # "local" | "external"


class BackupBody(BaseModel):
    path: str


def _merge_config(body: DbConfigBody) -> dict:
    """Merge the supplied fields with the stored config
    (empty password = keep the old password)."""
    cfg = dbsync.load_config()
    cfg.update({
        "type": body.type, "host": body.host, "port": body.port,
        "user": body.user, "database": body.database,
    })
    if body.password:
        cfg["password"] = body.password
    return cfg


@router.get("/info")
def database_info(user: dict = Depends(get_current_user)):
    _perm(user)
    return {"config": dbsync.public_config()}


# All database tasks run as a BACKEND JOB (app/bgjobs.py): a reload
# does not abort anything, progress/log are shown in the task view.
_PERM = "admin_settings"


@router.post("/test")
async def database_test(body: DbConfigBody, background: bool = False, user: dict = Depends(get_current_user)):
    _perm(user)
    cfg = _merge_config(body)

    async def work(job):
        job.update(phase="test", detail=f"Testing connection to {cfg.get('host') or cfg.get('type')}")
        try:
            await asyncio.to_thread(dbsync.test_connection, cfg)
        except Exception as e:
            raise HTTPException(400, f"Verbindung fehlgeschlagen: {e}")
        return {"ok": True}
    return await bgjobs.run("sql.test", None, title="Datenbank: Verbindung testen", user=user, work=work,
                            background=background, private=True)


@router.post("/switch")
async def database_switch(body: SwitchBody, background: bool = False, user: dict = Depends(get_current_user)):
    _perm(user)
    if body.mode not in ("local", "external"):
        raise HTTPException(400, "Invalid mode (local|external)")
    if dbsync.JOB.get("running"):
        raise HTTPException(409, "A database switch is already running")
    cfg = _merge_config(body)
    # Check beforehand, while nothing has been touched yet.
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, dbsync.test_connection, cfg)
    except Exception as e:
        raise HTTPException(400, f"Verbindung fehlgeschlagen: {e}")
    dbsync.job_reset("backup")
    username = user["username"]
    loop.run_in_executor(None, lambda: sqlcore.run_switch(cfg, body.mode, username))

    # Den laufenden Wechsel (dbsync.JOB) live in den Job-Dienst spiegeln
    async def mirror(job):
        J, last = dbsync.JOB, None
        while True:
            lines = list(J.get("log") or [])
            start = 0
            if last is not None:
                start = max((i for i, ln in enumerate(lines) if ln == last), default=-1) + 1
            for ln in lines[start:]:
                job.log(ln)
            if lines:
                last = lines[-1]
            tot = J.get("total") or 0
            job.update(phase=J.get("phase") or "", pct=(J.get("done", 0) / tot * 100) if tot else None,
                       detail=f"{J.get('phase', '')} · {J.get('table', '')} · {J.get('done', 0)}/{tot} tables · {J.get('rows', 0)} rows")
            if not J.get("running"):
                break
            await asyncio.sleep(1.0)
        if J.get("ok") is False:
            raise RuntimeError(J.get("detail") or "Database switch failed")
        job.update(phase="restart", detail=J.get("detail") or "fertig")
        return {"ok": True, "detail": J.get("detail"), "errors": J.get("errors")}
    job, _ = bgjobs.start("sql.switch", "db", title=f"Switch database ({body.mode})", user=user,
                          runner=mirror, perm=_PERM)
    out = {"ok": True, "started": True, "job_id": job.id,
           "detail": "Switch running - see /progress for progress"}
    return {**job.public(), **out, "job": True} if background else out


@router.get("/progress")
def database_progress(user: dict = Depends(get_current_user)):
    _perm(user)
    return dict(dbsync.JOB)


@router.get("/backups")
def database_backups(user: dict = Depends(get_current_user)):
    _perm(user)
    return {"backups": dbsync.list_backups(), "dir": str(dbsync._BACKUP_DIR)}


@router.post("/backup")
async def database_backup(background: bool = False, user: dict = Depends(get_current_user)):
    """Back up the complete database to backend/data/backup.

    The local SQLite is always the working copy - even in external mode -,
    so the backup contains the full state in every mode.
    """
    _perm(user)

    async def work(job):
        job.update(phase="backup", detail="Backing up the database")
        try:
            path = await asyncio.to_thread(dbsync.backup_local, "manual")
        except Exception as e:
            raise HTTPException(500, f"Backup failed: {e}")
        db.add_audit_entry(user["username"], "database.backup", details=path)
        return {"ok": True, "path": path}
    return await bgjobs.run("sql.backup", "db", title="Back up database", user=user, work=work,
                            background=background, perm=_PERM)


@router.post("/restore-backup")
async def database_restore_backup(body: BackupBody, background: bool = False, user: dict = Depends(get_current_user)):
    """Restore a backup. The backend restarts afterwards."""
    _perm(user)
    known = {b["path"] for b in dbsync.list_backups()}
    if body.path not in known:
        raise HTTPException(400, "Unknown backup")
    async def work(job):
        job.update(phase="restore", detail="Restoring the backup")
        try:
            await asyncio.to_thread(dbsync.restore_local_backup, body.path)
        except Exception as e:
            raise HTTPException(500, f"Restore failed: {e}")
        db.add_audit_entry(user["username"], "database.restored", details=body.path)
        job.update(phase="restart", detail="Restored - backend restarting")
        sqlcore.restart_soon()
        return {"ok": True, "restart": "Backend restarts in a few seconds"}
    return await bgjobs.run("sql.restore", "db", title="Restore database backup", user=user, work=work,
                            background=background, perm=_PERM)


@router.get("/docker")
def database_docker(user: dict = Depends(get_current_user)):
    """State of the Docker database container. is_docker=false -> hide the section."""
    _perm(user)
    from app import docker_services
    st = docker_services.status()
    out = {"is_docker": bool(st.get("is_docker")), "socket": bool(st.get("socket")),
           "service": None, "credentials": None}
    if out["is_docker"] and out["socket"]:
        out["service"] = next((s for s in st.get("services", []) if s.get("key") == "db"), None)
        try:
            out["credentials"] = docker_services.db_credentials()
        except Exception:
            out["credentials"] = None
    return out
