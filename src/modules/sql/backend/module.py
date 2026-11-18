# module.py (module "sql")
# -----------------------
# When the module is disabled, no external database may stay active, because without
# the module there is no UI left to manage it. Therefore the complete external database
# is copied into the local SQLite file, the mode is set to "local" and the backend is
# restarted. If the database ran in a Docker container, that container is stopped
# (container + volume are kept).
#
# If the copy-back fails, external mode stays in place - the core (app/dbsync.py)
# keeps syncing, so no data is lost. The module can then be re-enabled to try again.

import asyncio

from app import dbsync

from . import sqlcore


async def on_disable(ctx):
    cfg = dbsync.load_config()
    if cfg.get("mode", "local") != "external":
        ctx.log("Database is local - nothing to do.")
        return
    if dbsync.JOB.get("running"):
        ctx.log("A database switch is already running - copy-back skipped.")
        raise RuntimeError("A database switch is already running; external DB stays active.")

    ctx.log(f"External database ({cfg.get('type')}) is being copied into the local file …")
    use_docker = sqlcore.docker_db_in_use(cfg)
    dbsync.job_reset("backup")
    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(
        None, lambda: sqlcore.run_switch(dict(cfg), "local", "system:module-sql", restart=False))
    if not ok:
        raise RuntimeError("Copy-back into the local file failed: "
                           + str(dbsync.JOB.get("detail") or "unknown error")
                           + " - external database stays active.")
    ctx.log("Copy-back done: " + str(dbsync.JOB.get("detail") or ""))
    if use_docker:
        try:
            await loop.run_in_executor(None, sqlcore.stop_docker_db)
            ctx.log("Docker database container stopped (data is kept).")
        except Exception as exc:
            ctx.log(f"Docker container could not be stopped: {exc}")
    ctx.log("Backend restarting …")
    sqlcore.restart_soon(5.0)
