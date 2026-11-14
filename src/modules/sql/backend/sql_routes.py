# sql_routes.py (Modul "sql")
# ---------------------------
# Datenbank verwalten (frueher Core: router/database_routes.py). Die URLs sind
# unveraendert geblieben, damit api.js und bestehende Skripte weiter passen.
#
# Recht: "admin_settings" (Admin-Einstellungen: IP/Port, Server-Update, DB …).
#
#   GET  /api/admin/database/info            Modus + Konfiguration (ohne Passwort)
#   POST /api/admin/database/test            Verbindung testen
#   POST /api/admin/database/switch          Modus wechseln (Daten kopieren, Neustart)
#   GET  /api/admin/database/progress        Fortschritt des Wechsels
#   GET  /api/admin/database/backups         Sicherungen in backend/data/backup
#   POST /api/admin/database/backup          Sicherung jetzt erstellen
#   POST /api/admin/database/restore-backup  Sicherung zurueckspielen (Neustart)
#   GET  /api/admin/database/docker          Docker-Datenbank: Zustand + Zugangsdaten
#                                            (nur sinnvoll, wenn das Backend im Container laeuft)

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
    """Uebergebene Felder mit der gespeicherten Config zusammenfuehren
    (leeres Passwort = altes Passwort behalten)."""
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


# Alle Datenbank-Aufgaben laufen als BACKEND-JOB (app/bgjobs.py): Neuladen
# bricht nichts ab, Fortschritt/Log stehen in der Aufgaben-Anzeige.
_PERM = "admin_settings"


@router.post("/test")
async def database_test(body: DbConfigBody, background: bool = False, user: dict = Depends(get_current_user)):
    _perm(user)
    cfg = _merge_config(body)

    async def work(job):
        job.update(phase="test", detail=f"Verbindung zu {cfg.get('host') or cfg.get('type')} wird geprüft")
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
        raise HTTPException(400, "Ungültiger Modus (local|external)")
    if dbsync.JOB.get("running"):
        raise HTTPException(409, "Es läuft bereits ein Datenbank-Wechsel")
    cfg = _merge_config(body)
    # Vorab pruefen, solange noch nichts angefasst wurde.
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
                       detail=f"{J.get('phase', '')} · {J.get('table', '')} · {J.get('done', 0)}/{tot} Tabellen · {J.get('rows', 0)} Zeilen")
            if not J.get("running"):
                break
            await asyncio.sleep(1.0)
        if J.get("ok") is False:
            raise RuntimeError(J.get("detail") or "Datenbank-Wechsel fehlgeschlagen")
        job.update(phase="restart", detail=J.get("detail") or "fertig")
        return {"ok": True, "detail": J.get("detail"), "errors": J.get("errors")}
    job, _ = bgjobs.start("sql.switch", "db", title=f"Datenbank wechseln ({body.mode})", user=user,
                          runner=mirror, perm=_PERM)
    out = {"ok": True, "started": True, "job_id": job.id,
           "detail": "Wechsel läuft - Fortschritt siehe /progress"}
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
    """Sicherung der kompletten Datenbank nach backend/data/backup.

    Die lokale SQLite ist immer die Arbeitskopie - auch im externen Modus -,
    deshalb enthaelt die Sicherung in jedem Modus den vollstaendigen Stand.
    """
    _perm(user)

    async def work(job):
        job.update(phase="backup", detail="Datenbank wird gesichert")
        try:
            path = await asyncio.to_thread(dbsync.backup_local, "manuell")
        except Exception as e:
            raise HTTPException(500, f"Sicherung fehlgeschlagen: {e}")
        db.add_audit_entry(user["username"], "database.backup", details=path)
        return {"ok": True, "path": path}
    return await bgjobs.run("sql.backup", "db", title="Datenbank sichern", user=user, work=work,
                            background=background, perm=_PERM)


@router.post("/restore-backup")
async def database_restore_backup(body: BackupBody, background: bool = False, user: dict = Depends(get_current_user)):
    """Eine Sicherung zurueckspielen. Das Backend startet danach neu."""
    _perm(user)
    known = {b["path"] for b in dbsync.list_backups()}
    if body.path not in known:
        raise HTTPException(400, "Unbekannte Sicherung")
    async def work(job):
        job.update(phase="restore", detail="Sicherung wird zurückgespielt")
        try:
            await asyncio.to_thread(dbsync.restore_local_backup, body.path)
        except Exception as e:
            raise HTTPException(500, f"Wiederherstellung fehlgeschlagen: {e}")
        db.add_audit_entry(user["username"], "database.restored", details=body.path)
        job.update(phase="restart", detail="Zurückgespielt - Backend startet neu")
        sqlcore.restart_soon()
        return {"ok": True, "restart": "Backend startet in wenigen Sekunden neu"}
    return await bgjobs.run("sql.restore", "db", title="Datenbank-Sicherung zurückspielen", user=user, work=work,
                            background=background, perm=_PERM)


@router.get("/docker")
def database_docker(user: dict = Depends(get_current_user)):
    """Zustand des Docker-Datenbank-Containers. is_docker=false -> Bereich ausblenden."""
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
