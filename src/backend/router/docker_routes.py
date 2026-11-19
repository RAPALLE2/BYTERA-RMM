"""
routers/docker_routes.py
------------------------
Add optional services in container operation. The UI for this lives in the
modules: "db" -> module "sql", "guacd" -> module "guacamole".
See app/docker_services.py for the technology behind it.

Endpoints (admin only):
  GET  /api/admin/docker/services              -> state of all services
  POST /api/admin/docker/services/{key}/enable -> create + start
  POST /api/admin/docker/services/{key}/disable-> stop + remove
  GET  /api/admin/docker/db-credentials        -> credentials of the SQL database
                                                  (pre-fills the DB form)

If the backend does NOT run in a container, the status simply returns
"is_docker: false" - the UI then hides the section.
"""

import asyncio
from fastapi import APIRouter, Depends, HTTPException

from app import bgjobs, db
from app import docker_services
from app import modules
from app.auth import get_current_user
from router.admin_routes import require_admin

router = APIRouter(prefix="/api/admin/docker", tags=["docker"])

# Which service belongs to which module? If the module is off, the service
# cannot be started/restarted (stopping stays allowed - for cleanup).
_SERVICE_MODULE = {"db": "sql", "guacd": "guacamole"}


def _require_module(key: str) -> None:
    mid = _SERVICE_MODULE.get(key)
    if mid and not modules.is_enabled(mid):
        raise HTTPException(400, f"This service belongs to module '{mid}' - please enable the module first.")


@router.get("/services")
def list_services(user: dict = Depends(get_current_user)):
    require_admin(user)
    return docker_services.status()


async def _svc_job(key: str, verb: str, user: dict, background: bool, fn):
    """Docker service action as a BACKEND JOB (pulling the image/starting the container takes time)."""
    async def work(job):
        job.update(phase=verb, detail=f"Service '{key}': {verb} (pulling the image/starting the container can take a while)")
        return await asyncio.to_thread(fn)
    return await bgjobs.run("docker.service", key, title=f"Docker service '{key}' {verb}", user=user,
                            work=work, background=background, perm="admin", meta={"service": key})


@router.post("/services/{key}/enable")
async def enable_service(key: str, background: bool = False, user: dict = Depends(get_current_user)):
    require_admin(user)
    _require_module(key)
    db.add_audit_entry(user["username"], "docker.enable", target=key)
    return await _svc_job(key, "starting", user, background, lambda: _enable_service(key))


def _enable_service(key: str) -> dict:
    try:
        result = docker_services.enable(key)
    except docker_services.DockerError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Service could not be started: {e}")
    # Return the freshly set values right away so the dashboard can pre-fill the
    # forms without a second call.
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
            raise HTTPException(500, f"Service could not be stopped: {e}")
        result["status"] = docker_services.status()
        return result
    return await _svc_job(key, "stopping", user, background, fn)


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
            raise HTTPException(500, f"Service could not be restarted: {e}")
    return await _svc_job(key, "restarting", user, background, fn)


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
