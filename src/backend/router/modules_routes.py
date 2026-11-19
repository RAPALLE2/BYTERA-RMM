# router/modules_routes.py
# ------------------------
# API of the module app (Core). Managed with the global permission "manage_modules".
#
#   GET    /api/modules                       list of all modules (status, features, settings)
#   GET    /api/modules/frontend              active modules with a frontend part (all logged-in users)
#   POST   /api/modules/{id}/enable           enable (?with_deps=1 also enables dependencies)
#   POST   /api/modules/{id}/disable          disable (?cascade=1 also disables dependent modules)
#   GET    /api/modules/{id}/settings         settings
#   PUT    /api/modules/{id}/settings         save settings
#   PUT    /api/modules/{id}/features         toggle sub-features
#   GET    /api/modules/{id}/crashes          crash reports
#   DELETE /api/modules/{id}/crashes          delete crash reports
#   POST   /api/modules/{id}/report-crash     report an error from the frontend
#   PUT    /api/modules/crash-policy          how many crashes lead to shutdown
#   POST   /api/modules/rescan                re-read module.json
#   GET    /modules/frontend/{id}/{path}      frontend files of an ACTIVE module

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app import db, modules
from app.auth import get_current_user, require_perm

router = APIRouter()


def _admin(user: dict) -> None:
    # Global permission "manage_modules" (permission management -> general).
    # Administrators have it automatically.
    require_perm(user, "manage_modules")


def _wrap(exc: modules.ModuleError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/api/modules")
def list_modules(user: dict = Depends(get_current_user)):
    _admin(user)
    return modules.list_all()


@router.get("/api/modules/frontend")
def frontend_modules(user: dict = Depends(get_current_user)):
    return {"modules": modules.frontend_manifest()}


@router.post("/api/modules/rescan")
def rescan(user: dict = Depends(get_current_user)):
    _admin(user)
    modules.discover()
    return modules.list_all()


class CrashPolicy(BaseModel):
    crash_limit: int = Field(ge=1, le=50)


@router.put("/api/modules/crash-policy")
def crash_policy(body: CrashPolicy, user: dict = Depends(get_current_user)):
    _admin(user)
    return modules.set_crash_policy(body.crash_limit)


def _mod_name(mid: str) -> str:
    try:
        return modules.describe(mid).get("name") or mid
    except Exception:
        return mid


@router.post("/api/modules/{mid}/enable")
async def enable_module(mid: str, with_deps: bool = False, background: bool = False,
                        user: dict = Depends(get_current_user)):
    """Enable a module as a BACKEND JOB (app/bgjobs.py): loading
    dependencies/tools can take a while - reloading does not abort it."""
    _admin(user)
    from app import bgjobs

    async def work(job):
        job.update(phase="enable", detail="Dependencies are being checked/loaded, module is starting")
        try:
            res = await modules.enable(mid, with_deps=with_deps)
        except modules.ModuleError as exc:
            raise _wrap(exc)
        db.add_audit_entry(user["username"], "module.enable", target=mid, details=",".join(res["enabled"]))
        return {**res, "module": modules.describe(mid)}
    return await bgjobs.run("module.toggle", mid, title=f"Enable module '{_mod_name(mid)}'", user=user,
                            work=work, background=background, perm="manage_modules", meta={"module": mid})


@router.post("/api/modules/{mid}/disable")
async def disable_module(mid: str, cascade: bool = False, background: bool = False,
                         user: dict = Depends(get_current_user)):
    _admin(user)
    from app import bgjobs

    async def work(job):
        job.update(phase="disable", detail="Module is shutting down")
        try:
            res = await modules.disable(mid, cascade=cascade)
        except modules.ModuleError as exc:
            raise _wrap(exc)
        db.add_audit_entry(user["username"], "module.disable", target=mid, details=",".join(res["disabled"]))
        return {**res, "module": modules.describe(mid)}
    return await bgjobs.run("module.toggle", mid, title=f"Disable module '{_mod_name(mid)}'", user=user,
                            work=work, background=background, perm="manage_modules", meta={"module": mid})


@router.get("/api/modules/{mid}/settings")
def get_settings(mid: str, user: dict = Depends(get_current_user)):
    _admin(user)
    try:
        return {"settings": modules.get_settings(mid)}
    except modules.ModuleError as exc:
        raise _wrap(exc)


@router.put("/api/modules/{mid}/settings")
def put_settings(mid: str, body: dict, user: dict = Depends(get_current_user)):
    _admin(user)
    try:
        res = modules.set_settings(mid, body.get("settings") or {})
    except modules.ModuleError as exc:
        raise _wrap(exc)
    db.add_audit_entry(user["username"], "module.settings", target=mid)
    return {"settings": res}


@router.put("/api/modules/{mid}/features")
async def put_features(mid: str, body: dict, user: dict = Depends(get_current_user)):
    _admin(user)
    try:
        res = await modules.set_features(mid, body.get("features") or {})
    except modules.ModuleError as exc:
        raise _wrap(exc)
    db.add_audit_entry(user["username"], "module.features", target=mid, details=str(res))
    return {"features": res, "module": modules.describe(mid)}


@router.get("/api/modules/{mid}/crashes")
def get_crashes(mid: str, user: dict = Depends(get_current_user)):
    _admin(user)
    try:
        return {"crashes": modules.read_crashes(mid)}
    except modules.ModuleError as exc:
        raise _wrap(exc)


@router.delete("/api/modules/{mid}/crashes")
def delete_crashes(mid: str, user: dict = Depends(get_current_user)):
    _admin(user)
    try:
        modules.clear_crashes(mid)
    except modules.ModuleError as exc:
        raise _wrap(exc)
    return {"ok": True}


class FrontendCrash(BaseModel):
    where: str = Field(default="frontend", max_length=200)
    error: str = Field(default="", max_length=1000)
    stack: str = Field(default="", max_length=6000)
    fatal: bool = False        # True: module could not be loaded -> shut it down


@router.post("/api/modules/{mid}/report-crash")
def report_frontend_crash(mid: str, body: FrontendCrash, user: dict = Depends(get_current_user)):
    try:
        modules.get(mid)
    except modules.ModuleError as exc:
        raise _wrap(exc)
    if not modules.is_enabled(mid):
        return {"ok": True, "ignored": True}
    # Fatal (module could not be loaded/rendered): counts against the crash limit
    # and shuts the module down. Non-fatal runtime errors of individual browsers
    # are only stored as a report (visible in the module app) but shut nothing down.
    modules.report_crash(mid, None, body.where, source="frontend",
                         extra=f"{body.error}\n{body.stack}\nuser={user.get('username')}".strip(),
                         disable=bool(body.fatal))
    return {"ok": True}


@router.get("/modules/frontend/{mid}/{rel:path}")
def module_frontend_file(mid: str, rel: str):
    f = modules.frontend_file(mid, rel)
    if f is None:
        raise HTTPException(status_code=404, detail="Not found")
    # Fill missing exports with placeholders (app/frontend_guard.py) - otherwise
    # a whole module tree fails just because one file does not match the revision
    # of the others.
    if str(rel).lower().endswith(".js"):
        try:
            from app import frontend_guard
            extra = frontend_guard.patch_for(f"/modules/frontend/{mid}/{rel}")
            if extra:
                from fastapi.responses import Response as _Resp
                return _Resp(content=open(f, "rb").read() + extra.encode("utf-8"),
                             media_type="text/javascript; charset=utf-8",
                             headers={"Cache-Control": "no-cache"})
        except Exception:
            pass
    return FileResponse(f, headers={"Cache-Control": "no-cache"})
