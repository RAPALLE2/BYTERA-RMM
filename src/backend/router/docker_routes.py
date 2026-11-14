"""
routers/docker_routes.py
------------------------
Zusatzdienste im Container-Betrieb dazuschalten. Die Oberflaeche dazu steckt in
den Modulen: "db" -> Modul "sql", "guacd" -> Modul "guacamole".
Siehe app/docker_services.py für die Technik dahinter.

Endpunkte (nur Admin):
  GET  /api/admin/docker/services              -> Zustand aller Dienste
  POST /api/admin/docker/services/{key}/enable -> anlegen + starten
  POST /api/admin/docker/services/{key}/disable-> stoppen + entfernen
  GET  /api/admin/docker/db-credentials        -> Zugangsdaten der SQL-Datenbank
                                                  (füllt das DB-Formular vor)

Läuft das Backend NICHT im Container, liefert der Status einfach
"is_docker: false" - die Oberfläche blendet den Bereich dann aus.
"""

import asyncio
from fastapi import APIRouter, Depends, HTTPException

from app import bgjobs, db
from app import docker_services
from app import modules
from app.auth import get_current_user
from router.admin_routes import require_admin

router = APIRouter(prefix="/api/admin/docker", tags=["docker"])

# Welcher Dienst gehoert zu welchem Modul? Ist das Modul aus, laesst sich der
# Dienst nicht starten/neu starten (Stoppen bleibt erlaubt - zum Aufraeumen).
_SERVICE_MODULE = {"db": "sql", "guacd": "guacamole"}


def _require_module(key: str) -> None:
    mid = _SERVICE_MODULE.get(key)
    if mid and not modules.is_enabled(mid):
        raise HTTPException(400, f"Dieser Dienst gehoert zum Modul '{mid}' - bitte erst das Modul aktivieren.")


@router.get("/services")
def list_services(user: dict = Depends(get_current_user)):
    require_admin(user)
    return docker_services.status()


async def _svc_job(key: str, verb: str, user: dict, background: bool, fn):
    """Docker-Dienst-Aktion als BACKEND-JOB (Image laden/Container starten dauert)."""
    async def work(job):
        job.update(phase=verb, detail=f"Dienst „{key}“: {verb} (Image laden/Container starten kann dauern)")
        return await asyncio.to_thread(fn)
    return await bgjobs.run("docker.service", key, title=f"Docker-Dienst „{key}“ {verb}", user=user,
                            work=work, background=background, perm="admin", meta={"service": key})


@router.post("/services/{key}/enable")
async def enable_service(key: str, background: bool = False, user: dict = Depends(get_current_user)):
    require_admin(user)
    _require_module(key)
    db.add_audit_entry(user["username"], "docker.enable", target=key)
    return await _svc_job(key, "starten", user, background, lambda: _enable_service(key))


def _enable_service(key: str) -> dict:
    try:
        result = docker_services.enable(key)
    except docker_services.DockerError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Dienst konnte nicht gestartet werden: {e}")
    # Die frisch gesetzten Werte gleich mitliefern, damit das Dashboard die
    # Formulare ohne zweiten Aufruf vorausfüllen kann.
    result["status"] = docker_services.status()
    result["db"] = docker_services.db_credentials()
    result["guacd"] = {
        "host": db.get_setting("guacd_host") or "",
        "port": int(db.get_setting("guacd_port") or 4822),
    }
    return result


@router.post("/services/{key}/disable")
async def disable_service(key: str, background: bool = False, user: dict = Depends(get_current_user)):
    require_admin(user)
    db.add_audit_entry(user["username"], "docker.disable", target=key)

    def fn():
        try:
            result = docker_services.disable(key)
        except docker_services.DockerError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(500, f"Dienst konnte nicht gestoppt werden: {e}")
        result["status"] = docker_services.status()
        return result
    return await _svc_job(key, "stoppen", user, background, fn)


@router.post("/services/{key}/restart")
async def restart_service(key: str, background: bool = False, user: dict = Depends(get_current_user)):
    require_admin(user)
    _require_module(key)
    db.add_audit_entry(user["username"], "docker.restart", target=key)

    def fn():
        try:
            return docker_services.restart(key)
        except docker_services.DockerError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(500, f"Dienst konnte nicht neu gestartet werden: {e}")
    return await _svc_job(key, "neu starten", user, background, fn)


@router.get("/services/{key}/details")
def service_details(key: str, user: dict = Depends(get_current_user)):
    require_admin(user)
    try:
        return docker_services.details(key)
    except docker_services.DockerError as e:
        raise HTTPException(400, str(e))


@router.get("/db-credentials")
def db_credentials(user: dict = Depends(get_current_user)):
    require_admin(user)
    return docker_services.db_credentials()
