"""
routers/clients_routes.py
----------------------------
Everything about the managed devices (clients) themselves.

Endpoints:
  GET    /api/clients                 -> all clients + online status + live metrics
  GET    /api/clients/{id}            -> a single client
  PUT    /api/clients/{id}            -> edit client (edit dialog:
                                          tenant/location/name/color/active/...)
  DELETE /api/clients/{id}            -> delete client
  POST   /api/clients/{id}/exec       -> run a shell command on the client (terminal)
  POST   /api/clients/bulk-exec       -> shell command on SEVERAL clients at once
  GET    /api/clients/{id}/fs         -> list folder contents on the client (file station)
"""

import asyncio

import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from app import bgjobs, db
from app.auth import (
    get_current_user, require_perm, can_access_client,
    visible_client_ids, user_has_permission,
)
from app.sockets import state, request_exec, request_fs_list, request_proc_list, request_proc_kill, request_fs_read, request_fs_op, send_to_agent, sio


def _require_client_perm(user: dict, client_id: str, perm: str) -> None:
    """Checks a client-related permission; raises 404 instead of 403 if the
    client is not visible to the user at all (hidden clients stay hidden)."""
    if not can_access_client(user, client_id):
        raise HTTPException(404, "Client not found")
    require_perm(user, perm, client_id)

router = APIRouter(prefix="/api/clients", tags=["clients"])


# ==================================================================
# Client websites (quick access + favorites + uptime monitoring)
# ==================================================================

# Allowed notification modes for uptime monitoring:
#   'up'     -> notification when a scan SUCCEEDED (on status change)
#   'down'   -> notification when a scan FAILED (on status change)
#   'always' -> notification after EVERY scan (success and failure)
_NOTIFY_MODES = {"up", "down", "always"}


class WebsiteBody(BaseModel):
    name: str
    url: str
    favorite: bool = False
    monitor_enabled: bool = False
    monitor_notify: str = "down"              # 'up' | 'down' | 'always'
    monitor_interval_seconds: int = 300       # delay between scans
    open_mode: str = "external"               # 'external' tab | 'internal' browser window


class WebsiteUpdateBody(BaseModel):
    name: str | None = None
    url: str | None = None
    favorite: bool | None = None
    monitor_enabled: bool | None = None
    monitor_notify: str | None = None
    monitor_interval_seconds: int | None = None
    open_mode: str | None = None


def _validate_website(url: str | None, notify: str | None, interval: int | None) -> None:
    if url is not None and not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(400, "The URL must start with http:// or https://")
    if notify is not None and notify not in _NOTIFY_MODES:
        raise HTTPException(400, "monitor_notify must be 'up', 'down' or 'always'")
    if interval is not None and interval < 10:
        raise HTTPException(400, "The scan interval must be at least 10 seconds")


def _validate_open_mode(mode: str | None) -> None:
    if mode is not None and mode not in ("external", "internal"):
        raise HTTPException(400, "open_mode must be 'external' or 'internal'")


# Important: define the literal route BEFORE the /{client_id}/... routes.
@router.get("/websites/favorites")
def get_favorite_websites(user: dict = Depends(get_current_user)):
    """All websites pinned as favorites - filtered to clients the user is
    allowed to see at all."""
    favs = db.list_favorite_websites()
    visible = visible_client_ids(user, [w["client_id"] for w in favs])
    return [w for w in favs if w["client_id"] in visible]


@router.get("/{client_id}/websites")
def get_client_websites(client_id: str, user: dict = Depends(get_current_user)):
    if not db.get_client(client_id) or not can_access_client(user, client_id):
        raise HTTPException(404, "Client not found")
    _require_client_perm(user, client_id, "c_websites_view")
    return db.list_client_websites(client_id)


@router.post("/{client_id}/websites")
def create_client_website(client_id: str, body: WebsiteBody,
                                user: dict = Depends(get_current_user)):
    if not db.get_client(client_id):
        raise HTTPException(404, "Client not found")
    _require_client_perm(user, client_id, "c_websites_edit")
    _validate_website(body.url, body.monitor_notify, body.monitor_interval_seconds)
    _validate_open_mode(body.open_mode)

    w = db.create_client_website(
        client_id, body.name.strip(), body.url.strip(),
        favorite=body.favorite, monitor_enabled=body.monitor_enabled,
        monitor_notify=body.monitor_notify,
        monitor_interval_seconds=body.monitor_interval_seconds,
        open_mode=body.open_mode,
    )
    db.add_audit_entry(user["username"], "website.created", target=client_id,
                       details=f"{body.name} ({body.url})")
    return w


@router.put("/{client_id}/websites/{website_id}")
def update_client_website(client_id: str, website_id: str, body: WebsiteUpdateBody,
                                user: dict = Depends(get_current_user)):
    _require_client_perm(user, client_id, "c_websites_edit")
    site = db.get_client_website(website_id)
    if not site or site["client_id"] != client_id:
        raise HTTPException(404, "Website not found")

    fields = body.model_dump(exclude_unset=True)
    _validate_website(fields.get("url"), fields.get("monitor_notify"),
                      fields.get("monitor_interval_seconds"))
    _validate_open_mode(fields.get("open_mode"))
    for key in ("favorite", "monitor_enabled"):
        if key in fields:
            fields[key] = int(fields[key])

    updated = db.update_client_website(website_id, fields)
    db.add_audit_entry(user["username"], "website.updated", target=client_id, details=str(fields))
    return updated


@router.delete("/{client_id}/websites/{website_id}")
def delete_client_website(client_id: str, website_id: str,
                                user: dict = Depends(get_current_user)):
    _require_client_perm(user, client_id, "c_websites_edit")
    site = db.get_client_website(website_id)
    if not site or site["client_id"] != client_id:
        raise HTTPException(404, "Website not found")
    db.delete_client_website(website_id)
    db.add_audit_entry(user["username"], "website.deleted", target=client_id,
                       details=site.get("name"))
    return {"ok": True}


@router.get("/{client_id}/screen-history")
def get_screen_history(client_id: str, user: dict = Depends(get_current_user)):
    """
    Session history of the remote maintenance (like the connection list in
    AnyDesk): assembled from the audit log - start, end, duration, who, and what
    happened in the session (files, permissions, audio). Newest first, max. 50.
    """
    _require_client_perm(user, client_id, "c_screen_view")
    rows = sorted(db.list_audit_for_target(client_id, "screen.", 600), key=lambda r: r["ts"])
    sessions: list[dict] = []
    cur: dict | None = None
    for r in rows:
        act = r["action"]
        if act == "screen.session_started":
            det = r.get("details") or ""
            cur = {"start": r["ts"], "end": None, "duration_s": None, "user": r.get("username"),
                   "ended_by": None, "rec_id": det[4:] if det.startswith("rec:") else None, "events": []}
            sessions.append(cur)
            continue
        if cur is None:
            continue
        if act == "screen.session_ended":
            by, _, secs = (r.get("details") or "").partition(";")
            cur.update(end=r["ts"], ended_by=by or None,
                       duration_s=int(secs) if secs.isdigit() else int((r["ts"] - cur["start"]) / 1000))
            cur = None
            continue
        cur["events"].append({"ts": r["ts"], "action": act, "user": r.get("username"),
                              "details": r.get("details") or ""})
    sessions.reverse()
    return {"sessions": sessions[:50]}


@router.get("/{client_id}/metrics/history")
def get_client_metrics_history(client_id: str, hours: int = 0,
                               user: dict = Depends(get_current_user)):
    """
    Returns the stored metric history of a client. The frontend uses it to
    fill the graphs with history data immediately after a page reload, instead
    of starting from 0.

    Time window: 'hours' from the request, otherwise the configured retention
    period, otherwise the default window of the database layer (24 hours).

    IMPORTANT - this was the crash of 23.08.: previously
    metrics_retention_hours = 0 (the shipping state) meant "return the COMPLETE
    history". With a table that grows with every heartbeat and is never cleaned
    up, after a few weeks this query took 125 seconds and froze the whole
    backend. "Keep indefinitely" is a sensible privacy setting - "READ OUT
    indefinitely" was simply a bug. Both are now separated.
    """
    retention_hours = db.get_int_setting("metrics_retention_hours")
    window = hours if hours and hours > 0 else retention_hours
    since_ts = None
    if window and window > 0:
        since_ts = int(time.time() * 1000) - window * 3600 * 1000
    points = db.get_metrics_history(client_id, since_ts)
    return {"points": points, "count": len(points),
            "window_hours": window or db.METRICS_DEFAULT_WINDOW_H,
            # Honestly report when thinned - otherwise one wonders about gaps
            # in the chart.
            "thinned": len(points) >= db.METRICS_MAX_POINTS}


class ExecBody(BaseModel):
    command: str
    session: str | None = None   # terminal session (for a persistent working directory)
    shell: str = "auto"          # 'cmd' | 'powershell' | 'auto'
    elevated: bool = False       # as administrator (Windows only)


class BulkExecBody(BaseModel):
    client_ids: list[str]
    command: str


class UpdateClientBody(BaseModel):
    hostname: str | None = None
    tenant_id: str | None = None
    location_id: str | None = None
    folder_id: str | None = None
    parent_client_id: str | None = None
    color: str | None = None
    notes: str | None = None
    status_override: str | None = None   # e.g. "maintenance" or null to reset
    active: bool | None = None
    # IMPORTANT: was missing before! Pydantic discards unknown fields - so
    # a VM/LXC classification chosen in the edit dialog was NEVER saved and
    # the dialog showed "Physical device" again after every reload.
    device_type: str | None = None       # physical | vm | lxc
    # Automatic agent update per client: 'global' (follows the settings),
    # 'on' (always) or 'off' (never).
    auto_update: str | None = None
    # Confirmation of the device-type auto-detection (1 = user has decided)
    device_type_ack: int | None = None
    # Icon in sidebar/panel/topology ('' = automatic from the detected kind)
    icon_type: str | None = None
    # Favorite working folder (appears in the web explorer in the root view)
    fav_dir: str | None = None
    # Warranty end (Unix ms). 0/None = no warranty stored.
    warranty_until: int | None = None
    # 1 = essential/critical device: the icon turns RED instead of gray when offline.
    essential: int | None = None
    # Short name for display (empty = hostname) and free tags (comma-separated).
    short_name: str | None = None
    tags: str | None = None


def _with_live_state(c: dict) -> dict:
    """Enriches a client record from the DB with online status + live metrics."""
    return {
        **c,
        # Debounced status: a short reconnect dropout is NOT shown as offline
        # (see ConnectionState.ui_online / disconnect).
        "online": state.ui_online(c["id"]),
        "metrics": state.live_metrics.get(c["id"]),
    }


@router.get("")
def get_clients(user: dict = Depends(get_current_user)):
    clients = db.list_clients()
    visible = visible_client_ids(user, [c["id"] for c in clients])
    # Hostname map over ALL clients (including invisible ones): a VM/LXC whose
    # physical host is not visible to the user is shown in the frontend in a
    # pseudo-folder with the name of the host - for that we need
    # the frontend needs the host name even if the host itself is filtered out.
    name_by_id = {c["id"]: c.get("hostname") for c in clients}
    out = []
    for c in clients:
        if c["id"] not in visible:
            continue
        item = _with_live_state(c)
        if c.get("parent_client_id"):
            item["parent_hostname"] = name_by_id.get(c["parent_client_id"])
        out.append(item)
    return out


@router.get("/{client_id}")
def get_client(client_id: str, user: dict = Depends(get_current_user)):
    c = db.get_client(client_id)
    if not c or not can_access_client(user, client_id):
        raise HTTPException(404, "Client not found")
    return _with_live_state(c)


@router.put("/{client_id}")
def update_client(client_id: str, body: UpdateClientBody, user: dict = Depends(get_current_user)):
    if not db.get_client(client_id):
        raise HTTPException(404, "Client not found")

    # Apply only the fields actually sent
    fields = body.model_dump(exclude_unset=True)

    # Permissions: the warranty date has its OWN permission ('set_warranty'), so
    # that e.g. purchasing/the warehouse can maintain it without otherwise being
    # able to edit the client. All other fields still require
    # 'manage_clients' (which automatically also covers 'set_warranty').
    if "warranty_until" in fields:
        _require_client_perm(user, client_id, "set_warranty")
    if any(k != "warranty_until" for k in fields):
        _require_client_perm(user, client_id, "manage_clients")
    if "active" in fields:
        fields["active"] = int(fields["active"])  # SQLite has no real bool
    if "device_type" in fields and fields["device_type"] not in ("physical", "vm", "lxc", None):
        raise HTTPException(400, "Invalid device type (physical|vm|lxc)")
    if "icon_type" in fields:
        fields["icon_type"] = fields["icon_type"] or ""
        if fields["icon_type"] and fields["icon_type"] not in db.ICON_TYPES:
            raise HTTPException(400, "Invalid icon type")
    if "auto_update" in fields and fields["auto_update"] not in ("global", "on", "off", None):
        raise HTTPException(400, "Invalid auto-update mode (global|on|off)")
    if fields.get("auto_update") is None and "auto_update" in fields:
        fields["auto_update"] = "global"

    updated = db.update_client(client_id, fields)
    db.add_audit_entry(user["username"], "client.updated", target=client_id, details=str(fields))
    return _with_live_state(updated)


@router.delete("/{client_id}")
def remove_client(client_id: str, user: dict = Depends(get_current_user)):
    _require_client_perm(user, client_id, "c_delete")
    db.delete_client(client_id)
    db.add_audit_entry(user["username"], "client.deleted", target=client_id)
    return {"ok": True}


def _hostname(client_id: str) -> str:
    return (db.get_client(client_id) or {}).get("hostname") or client_id


@router.post("/{client_id}/exec")
async def exec_on_client(client_id: str, body: ExecBody, background: bool = False,
                         user: dict = Depends(get_current_user)):
    """Run a command/script on the client - as a BACKEND JOB (app/bgjobs.py):
    reloading or closing the window does not abort it, the result stays in the
    task view. ?background=1 -> return the job immediately."""
    _require_client_perm(user, client_id, "c_terminal")
    # Every executed command lands in the audit log (as desired: "who did what and when")
    _mode = f"[{body.shell}{'/admin' if body.elevated else ''}] " if body.shell != "auto" or body.elevated else ""
    db.add_audit_entry(user["username"], "terminal.exec", target=client_id, details=_mode + body.command)
    first = (body.command.strip().splitlines() or [""])[0][:60]

    async def work(job):
        job.update(phase="exec", detail=f"running on {_hostname(client_id)}")
        job.log(f"$ {body.command[:2000]}")
        try:
            result = await request_exec(client_id, body.command, session=body.session,
                                        shell=body.shell, elevated=body.elevated)
        except Exception as e:
            raise HTTPException(504, str(e))
        r = result or {}
        if r.get("stdout"):
            job.log(str(r["stdout"])[-20000:])
        if r.get("stderr"):
            job.log("[stderr] " + str(r["stderr"])[-8000:])
        job.update(detail=f"exit code {r.get('code')}" if r.get("code") not in (None, 0) else "done")
        return result

    return await bgjobs.run("client.exec", None, title=f"Command on {_hostname(client_id)}: {first}",
                            user=user, work=work, background=background, client_id=client_id,
                            client_perm="c_terminal", on_cancel=bgjobs.hard_cancel,
                            meta={"client": client_id, "command": body.command[:500]})


@router.post("/bulk-exec")
async def bulk_exec(body: BulkExecBody, background: bool = False, user: dict = Depends(get_current_user)):
    """
    Runs the same command on several clients at once (bulk terminal).
    All requests run in parallel (asyncio.gather); the result is a mapping
    client ID -> result (or error message if one is offline).
    """
    # Global permission required for the bulk shell ...
    require_perm(user, "bulk_shell")
    # ... and per target client the terminal permission (the rest is discarded).
    allowed_ids = [cid for cid in body.client_ids
                   if user_has_permission(user, "c_terminal", cid)]

    db.add_audit_entry(
        user["username"], "terminal.bulk_exec",
        target=",".join(body.client_ids), details=body.command,
    )
    first = (body.command.strip().splitlines() or [""])[0][:50]

    async def work(job):
        total = len(allowed_ids)
        cnt = {"done": 0, "ok": 0, "failed": 0}
        job.update(phase="exec", pct=0, detail=f"0/{total} clients", total=total, **cnt)

        async def run_one(client_id: str) -> tuple[str, dict]:
            try:
                result = await request_exec(client_id, body.command)
                res = (client_id, {"ok": True, **result})
                cnt["ok"] += 1
            except Exception as e:
                res = (client_id, {"ok": False, "error": str(e)})
                cnt["failed"] += 1
            cnt["done"] += 1
            job.log(f"{_hostname(client_id)}: {'OK' if res[1]['ok'] else 'ERROR ' + res[1].get('error', '')}")
            job.update(pct=cnt["done"] / max(1, total) * 100,
                       detail=f"{cnt['done']}/{total} clients - {cnt['ok']} ok - {cnt['failed']} failed", **cnt)
            return res

        return dict(await asyncio.gather(*(run_one(cid) for cid in allowed_ids)))

    return await bgjobs.run("client.bulk_exec", None, title=f"Bulk command on {len(allowed_ids)} clients: {first}",
                            user=user, perm="bulk_shell", work=work, background=background,
                            on_cancel=bgjobs.hard_cancel,
                            meta={"clients": allowed_ids, "command": body.command[:500]})


@router.get("/{client_id}/fs")
async def list_client_fs(client_id: str, path: str = "", user: dict = Depends(get_current_user)):
    _require_client_perm(user, client_id, "c_explorer_view")
    try:
        entries = await request_fs_list(client_id, path)
    except Exception as e:
        raise HTTPException(504, str(e))
    return {"path": path, "entries": entries}


@router.get("/{client_id}/processes")
async def list_client_processes(client_id: str, user: dict = Depends(get_current_user)):
    """Returns the running process list of a client (task manager)."""
    _require_client_perm(user, client_id, "c_taskmanager_view")
    try:
        processes = await request_proc_list(client_id)
    except Exception as e:
        raise HTTPException(504, str(e))
    return {"processes": processes}


class KillBody(BaseModel):
    pid: int


@router.post("/{client_id}/processes/kill")
async def kill_client_process(client_id: str, body: KillBody, user: dict = Depends(get_current_user)):
    """Kills a process on a client."""
    _require_client_perm(user, client_id, "c_taskmanager_kill")
    try:
        result = await request_proc_kill(client_id, body.pid)
    except Exception as e:
        raise HTTPException(504, str(e))
    db.add_audit_entry(user["username"], "process.kill", target=client_id, details=f"PID {body.pid}")
    return result


@router.get("/{client_id}/fs/read")
async def read_client_file(client_id: str, path: str, user: dict = Depends(get_current_user)):
    """
    Reads a file from the client and returns it base64-encoded.
    The frontend turns it into a download. (Intended for smaller files;
    the agent limits the size to 25 MB.)
    """
    _require_client_perm(user, client_id, "c_explorer_view")
    try:
        result = await request_fs_read(client_id, path)
    except Exception as e:
        raise HTTPException(504, str(e))
    db.add_audit_entry(user["username"], "file.download", target=client_id, details=path)
    return result


# ------------------------------------------------------------------
# Writing file operations on a client (upload/folder/delete/...)
# All of these require the "use_explorer" permission.
# ------------------------------------------------------------------

class FsWriteBody(BaseModel):
    path: str          # target path incl. file name
    data: str          # base64-encoded content


class FsPathBody(BaseModel):
    path: str


class FsRenameBody(BaseModel):
    src: str
    dst: str


@router.post("/{client_id}/fs/write")
async def write_client_file(client_id: str, body: FsWriteBody, user: dict = Depends(get_current_user)):
    """Uploads a file or writes back an edited file."""
    _require_client_perm(user, client_id, "c_explorer_edit")
    try:
        result = await request_fs_op(client_id, "fs-write", {"path": body.path, "data": body.data})
    except Exception as e:
        raise HTTPException(504, str(e))
    db.add_audit_entry(user["username"], "file.upload", target=client_id, details=body.path)
    return result


@router.post("/{client_id}/fs/mkdir")
async def mkdir_client(client_id: str, body: FsPathBody, user: dict = Depends(get_current_user)):
    _require_client_perm(user, client_id, "c_explorer_edit")
    try:
        result = await request_fs_op(client_id, "fs-mkdir", {"path": body.path})
    except Exception as e:
        raise HTTPException(504, str(e))
    db.add_audit_entry(user["username"], "file.mkdir", target=client_id, details=body.path)
    return result


@router.post("/{client_id}/fs/delete")
async def delete_client_path(client_id: str, body: FsPathBody, user: dict = Depends(get_current_user)):
    _require_client_perm(user, client_id, "c_explorer_edit")
    try:
        result = await request_fs_op(client_id, "fs-delete", {"path": body.path})
    except Exception as e:
        raise HTTPException(504, str(e))
    db.add_audit_entry(user["username"], "file.delete", target=client_id, details=body.path)
    return result


@router.post("/{client_id}/fs/rename")
async def rename_client_path(client_id: str, body: FsRenameBody, user: dict = Depends(get_current_user)):
    _require_client_perm(user, client_id, "c_explorer_edit")
    try:
        result = await request_fs_op(client_id, "fs-rename", {"src": body.src, "dst": body.dst})
    except Exception as e:
        raise HTTPException(504, str(e))
    db.add_audit_entry(user["username"], "file.rename", target=client_id,
                       details=f"{body.src} -> {body.dst}")
    return result


@router.post("/{client_id}/update-agent")
async def update_client_agent(client_id: str, wait: bool = False, background: bool = False,
                              user: dict = Depends(get_current_user)):
    """
    Tells the agent to update itself (downloads the current agent package,
    replaces agent.py, restarts the service). After the restart the agent
    reports "updated" back.

    IMPORTANT: the request returns IMMEDIATELY (order accepted). Previously it
    kept open for up to 55 s waiting for the confirmation - reverse proxies
    (nginx/NPM, Traefik, ...) abort such requests before that and the dashboard
    showed "Update failed: error 504", even though the update may have been
    running. The dashboard polls the progress via GET .../update-agent/status
    (in addition the socket events client:action-log / client:updated arrive).

    ?wait=true restores the old, blocking behavior for scripts/REST API.
    """
    _require_client_perm(user, client_id, "manage_agent")
    if not state.is_online(client_id):
        raise HTTPException(409, "The agent is not connected.")
    running = bgjobs.active("agent.update", client_id)
    if running:                                  # already running -> attach
        return await bgjobs.respond(running, False, background or not wait)

    client = db.get_client(client_id)
    state.update_confirmed.pop(client_id, None)
    state.update_jobs[client_id] = {
        "started": time.time(), "mono": time.monotonic(), "stage": "sent", "detail": "",
        "updated": False, "version_before": (client or {}).get("agent_version") or "",
        "version": "", "by": user["username"],
    }

    ok = await send_to_agent(client_id, "update-agent", {})
    if not ok:
        state.update_jobs.pop(client_id, None)
        raise HTTPException(503, "Client is offline")
    db.add_audit_entry(user["username"], "agent.update_triggered", target=client_id,
                       details=(client or {}).get("hostname"))

    # The backend tracks the update as a job (reload/other users see it)
    job, new = bgjobs.start("agent.update", client_id,
                            title=f"Agent update {(client or {}).get('hostname') or client_id}",
                            user=user, runner=_agent_update_runner(client_id), client_id=client_id,
                            client_perm="manage_agent",
                            meta={"client": client_id})
    if background:
        return await bgjobs.respond(job, new, True)
    if not wait:
        return {"ok": True, "pending": True, "job_id": job.id}
    return await bgjobs.respond(job, new, False)


_STAGE_TEXT = {"sent": "command sent", "received": "agent received the command",
               "launched": "update script running, agent restarting",
               "launched-fallback": "update script running (fallback path)",
               "updated": "updated", "error": "error"}


def _agent_update_runner(client_id: str):
    async def runner(job):
        # Update script: download + packages + up to 180 s health check
        # (rollback afterwards if needed) -> wait generously.
        deadline = time.monotonic() + 420.0
        last = None
        while time.monotonic() < deadline:
            uj = state.update_jobs.get(client_id) or {}
            if uj.get("updated"):
                v = uj.get("version") or (db.get_client(client_id) or {}).get("agent_version") or ""
                job.log(f"agent reports back{(' with version ' + v) if v else ''}")
                return {"ok": True, "updated": True, "version": v,
                        "version_before": uj.get("version_before", "")}
            stage = uj.get("stage") or "sent"
            if (stage, uj.get("detail")) != last:
                last = (stage, uj.get("detail"))
                job.log(f"{_STAGE_TEXT.get(stage, stage)}{(': ' + uj['detail']) if uj.get('detail') else ''}")
            job.update(phase=stage, detail=_STAGE_TEXT.get(stage, stage)
                       + ("" if state.is_online(client_id) else " - agent disconnected (restart)"))
            if stage == "error":
                raise HTTPException(502, uj.get("detail") or "Agent reports an error during the update")
            await asyncio.sleep(1.0)
        raise HTTPException(504, "The agent did not confirm a successful update within 7 minutes "
                                 "- log on the client: /var/log/bytera-agent-update.log "
                                 "(Linux) resp. C:\\ProgramData\\ByteraRmmAgent-update.log (Windows).")
    return runner


@router.get("/{client_id}/update-agent/status")
def update_client_agent_status(client_id: str, user: dict = Depends(get_current_user)):
    """Progress of the most recently triggered agent update of this client."""
    _require_client_perm(user, client_id, "manage_agent")
    job = state.update_jobs.get(client_id)
    client = db.get_client(client_id) or {}
    if not job:
        return {"job": False, "online": state.is_online(client_id),
                "agent_version": client.get("agent_version") or ""}
    elapsed = time.monotonic() - job["mono"]
    out = {k: v for k, v in job.items() if k != "mono"}
    out.update({"job": True, "elapsed": int(elapsed), "online": state.is_online(client_id),
                "agent_version": client.get("agent_version") or ""})
    if job["updated"]:
        if not job.get("audited"):
            job["audited"] = True
            db.add_audit_entry(job.get("by") or user["username"], "agent.updated",
                               target=client_id, details=client.get("hostname"))
    elif job["stage"] != "error" and elapsed > 420:
        out["stage"] = "timeout"
    return out


class UpdateAllBody(BaseModel):
    # Earmark offline clients for the update: they are updated as soon as
    # they connect to the backend the next time.
    include_offline: bool = False


@router.post("/update-all-agents")
async def update_all_agents(body: UpdateAllBody | None = None, background: bool = False,
                            user: dict = Depends(get_current_user)):
    """
    Triggers an agent update for ALL currently connected clients the user is
    allowed to manage. Deliberately "fire-and-forget": it does not wait for the
    individual confirmations (that would take too long with many clients) -
    each agent updates itself and reconnects.

    With include_offline=true, offline clients are additionally EARMARKED
    (pending_agent_update): they get the update automatically as soon as they
    are online again (see sockets.on_register).

    As soon as ALL triggered clients are reconnected, the dashboards
    automatically get a notification (background task).
    """
    include_offline = bool(body and body.include_offline)
    all_clients = db.list_clients()
    visible = visible_client_ids(user, [c["id"] for c in all_clients])

    # First only COLLECT who is affected - do not trigger anything yet.
    targets: list[str] = []
    offline, no_perm, queued = 0, 0, 0
    for c in all_clients:
        cid = c["id"]
        if cid not in visible:
            continue
        # Touch only clients with management permission (skip silently).
        if not user_has_permission(user, "manage_agent", cid):
            no_perm += 1
            continue
        if not state.is_online(cid):
            offline += 1
            if include_offline:
                db.set_pending_agent_update(cid, True)
                queued += 1
            continue
        targets.append(cid)

    db.add_audit_entry(user["username"], "agent.update_all_triggered",
                       details=f"scheduled={len(targets)}, offline={offline}, "
                               f"queued={queued}")

    # Roll out staggered, NOT all at once.
    #
    # Why: an "update-agent" triggers far more on the agent than a short
    # signal. It downloads update.sh or update.ps1 and then the new
    # agent.py from exactly this backend, exits and reconnects.
    # If this happens on all devices in the same second, the server gets
    # simultaneously: N downloads, N disconnects and shortly after N sign-ins
    # - while someone reloads the dashboard in the browser. Exactly this
    # combination was in the log: the server was busy for a few seconds, the
    # health check ran into its 5-second limit and Cloudflare into its timeout.
    #
    # The admission gate in sockets.py only eases the return. The sending-out
    # must be throttled here - otherwise the wave forms.
    out = {"ok": True, "triggered": len(targets), "offline": offline,
           "queued_offline": queued, "skipped_no_permission": no_perm}
    if targets:
        # Roll out + wait for the return as a BACKEND JOB (task view)
        async def work(job):
            job.update(phase="rollout", pct=0, detail=f"0/{len(targets)} triggered", total=len(targets))
            return await _rollout_agent_updates(targets, user["username"], job)
        job, _ = bgjobs.start("agent.update_all", "all", title=f"Agent update for {len(targets)} clients",
                              user=user, runner=work, meta={"clients": targets, "summary": out})
        out["job_id"] = job.id
        if background:
            return {**job.public(), **out, "job": True}
    return out


# How many agents are updated at once and how long is waited afterwards.
# Adjustable via environment variables, so one can turn it up on powerful
# hardware without changing code.
def _rollout_settings() -> tuple[int, float]:
    import os
    try:
        size = max(1, int(os.getenv("AGENT_UPDATE_BATCH", "2") or 2))
    except ValueError:
        size = 2
    try:
        pause = max(0.0, float(os.getenv("AGENT_UPDATE_PAUSE_S", "20") or 20))
    except ValueError:
        pause = 20.0
    return size, pause


async def _rollout_agent_updates(client_ids: list[str], username: str, job=None) -> dict:
    """
    Sends the update out in small waves and waits in between.

    The call returns immediately; the rollout runs in the background.
    That is why the UI reports "scheduled" and not "done" - with 50 devices
    the run takes several minutes, and that is intentional.
    """
    batch_size, pause = _rollout_settings()
    triggered: list[str] = []
    print(f"[update] Rollout for {len(client_ids)} agents "
          f"({batch_size} at once, {pause:.0f}s pause)")

    total = len(client_ids)
    for start in range(0, len(client_ids), batch_size):
        if job and job.cancelled:
            job.log("Cancelled - remaining clients are no longer triggered")
            break
        chunk = client_ids[start:start + batch_size]
        for cid in chunk:
            if not state.is_online(cid):
                if job:
                    job.log(f"{_hostname(cid)}: offline in the meantime - skipped")
                continue   # gone in the meantime - the rest continues
            # Discard the old confirmation, so only the NEW reconnect counts.
            state.update_confirmed.pop(cid, None)
            try:
                if await send_to_agent(cid, "update-agent", {}):
                    triggered.append(cid)
                    if job:
                        job.log(f"{_hostname(cid)}: update triggered")
            except Exception as e:
                print(f"[update] {cid} not reached: {e}")
                if job:
                    job.log(f"{_hostname(cid)}: not reached ({e})")
        if job:
            n = min(start + batch_size, total)
            job.update(pct=n / total * 50, detail=f"{n}/{total} triggered ({batch_size} each, {pause:.0f}s pause)")
        if start + batch_size < len(client_ids):
            await asyncio.sleep(pause)

    if triggered:
        return await _await_agents_reconnect(triggered, username, job=job)
    return {"triggered": 0, "reconnected": 0, "missing": []}


async def _await_agents_reconnect(client_ids: list[str], username: str,
                                  timeout_seconds: float = 300.0, job=None) -> dict:
    """Waits until all agents triggered by the mass update have gone through
    their restart, and then notifies all dashboards.

    IMPORTANT: at trigger time the clients are still connected. A client only
    counts as "reconnected" once it has FIRST gone offline (update restart)
    and THEN come online again. The most reliable signal for this is
    `state.update_confirmed` (set only on the reconnect AFTER an update).
    Additionally we watch the offline->online transition directly, in case an
    agent reports no `updated` flag."""
    # Phase 1: wait for going offline. Phase 2: wait for being online again.
    waiting_offline = set(client_ids)   # must leave first (update restart)
    waiting_online = set()              # was offline, must come back
    total = len(client_ids)
    deadline = time.monotonic() + timeout_seconds

    while (waiting_offline or waiting_online) and time.monotonic() < deadline:
        await asyncio.sleep(1.0)

        # Phase 1: clients that (a) went offline -> phase 2, or
        # (b) have already reported back via update reconnect -> done.
        for cid in list(waiting_offline):
            if cid in state.update_confirmed:
                # Has already completed the full cycle (offline+online).
                state.update_confirmed.pop(cid, None)
                waiting_offline.discard(cid)
            elif not state.is_online(cid):
                # Now offline (update restart) -> wait for the return.
                waiting_offline.discard(cid)
                waiting_online.add(cid)

        # Phase 2: wait for the return (update reconnect OR online again).
        for cid in list(waiting_online):
            if cid in state.update_confirmed or state.is_online(cid):
                state.update_confirmed.pop(cid, None)
                waiting_online.discard(cid)
                if job:
                    job.log(f"{_hostname(cid)}: reconnected")
        if job:
            back = total - len(waiting_offline) - len(waiting_online)
            job.update(phase="reconnect", pct=50 + back / total * 50,
                       detail=f"{back}/{total} reconnected")

    missing = waiting_offline | waiting_online
    reconnected = total - len(missing)
    if not missing:
        message = (f"Agent update completed: all {total} clients are "
                   f"reconnected.")
        level = "success"
        db.add_audit_entry(username, "agent.update_all_completed",
                           details=f"all {total} clients reconnected")
    else:
        # Names of the still-missing clients (a few at most) for the message.
        missing_names = []
        for cid in list(missing)[:5]:
            c = db.get_client(cid)
            missing_names.append((c or {}).get("hostname") or cid)
        extra = "" if len(missing) <= 5 else f" (+{len(missing) - 5} more)"
        message = (f"Agent update: {reconnected} of {total} clients "
                   f"reconnected. Did not report back: {', '.join(missing_names)}{extra}.")
        level = "warning"
        db.add_audit_entry(username, "agent.update_all_completed",
                           details=f"{reconnected}/{total} reconnected")

    try:
        await sio.emit("notify", {"message": message, "level": level},
                       namespace="/dashboard")
    except Exception as e:
        print(f"[update-all] dashboard notify failed: {e}")
    if job:
        job.update(detail=message)
    return {"triggered": total, "reconnected": reconnected,
            "missing": [_hostname(c) for c in missing], "message": message}


@router.post("/{client_id}/uninstall-agent")
async def uninstall_client_agent(client_id: str, background: bool = False,
                                 user: dict = Depends(get_current_user)):
    """
    Uninstalls the agent completely and removes the client from the
    dashboard - but ONLY if it really disappears. Flow:

      1. Send the uninstall command to the agent (stops the service and deletes
         ALL agent data on the client, see agent.py / uninstall.sh|ps1).
      2. Wait up to 60 seconds until the client really goes offline
         (= the agent process has ended, the socket connection is cut).
      3a. If it goes offline in that time -> remove the client from the database
          (disappears from the dashboard) and report success.
      3b. If it is STILL online after 60 s -> error message; the client is NOT
          removed (the uninstall apparently failed).
    """
    _require_client_perm(user, client_id, "manage_agent")

    client = db.get_client(client_id)
    if not client:
        raise HTTPException(404, "Client not found")

    # The agent must be connected so it can receive and execute the uninstall
    # command. An already offline/dead client is removed via the normal
    # "Delete" button in the edit dialog.
    if not state.is_online(client_id):
        raise HTTPException(
            409,
            "The agent is not connected. A remote uninstall is only possible "
            "with a connected agent. An already offline client can be removed via "
            "'Edit -> Delete' from the dashboard.",
        )

    async def work(job):
        return await _uninstall_work(job, client_id, client, user)

    return await bgjobs.run("agent.uninstall", client_id, title=f"Uninstall agent: {client.get('hostname') or client_id}",
                            user=user, work=work, background=background, client_id=client_id,
                            client_perm="manage_agent",
                            meta={"client": client_id})


async def _uninstall_work(job, client_id: str, client: dict, user: dict):
    # 1. Trigger the uninstall
    job.update(phase="send", detail="sending the uninstall order")
    ok = await send_to_agent(client_id, "uninstall-agent", {})
    if not ok:
        raise HTTPException(503, "Client is offline")
    db.add_audit_entry(user["username"], "agent.uninstall_triggered", target=client_id,
                       details=client.get("hostname"))

    # 2. Wait for "offline" (max. 60 s, check every 2 s).
    job.update(phase="wait", detail="waiting for the agent to go offline (max. 60 s)")
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        await asyncio.sleep(2.0)
        if not state.is_online(client_id):
            # 3a. Really gone -> remove from the dashboard.
            db.delete_client(client_id)
            db.add_audit_entry(user["username"], "agent.uninstalled", target=client_id,
                               details=client.get("hostname"))
            # Inform all dashboards about the changed client list.
            from app.sockets import sio
            await sio.emit("clients:changed", namespace="/dashboard")
            return {"ok": True, "removed": True, "hostname": client.get("hostname")}

    # 3b. Timeout: agent did not go offline -> error, client stays.
    db.add_audit_entry(user["username"], "agent.uninstall_timeout", target=client_id,
                       details=client.get("hostname"))
    raise HTTPException(
        504,
        "The agent did not go offline within 60 seconds. "
        "The uninstall may have failed - the client was NOT removed from the "
        "dashboard. Please check the client and try again if necessary.",
    )


@router.get("/{client_id}/rdp-file")
def get_rdp_file(client_id: str, user: dict = Depends(get_current_user)):
    """
    Creates a .rdp file with the IP address of the client. The user opens it,
    whereupon the native Windows RDP client (mstsc) connects.

    This is the reliable way for headless VMs: RDP creates its own graphics
    session that can then be remote-controlled - unlike our screen streaming,
    which requires an already existing session.
    """
    client = db.get_client(client_id)
    if not client or not can_access_client(user, client_id):
        raise HTTPException(404, "Client not found")
    require_perm(user, "c_screen", client_id)

    host = client.get("ip")
    if not host:
        raise HTTPException(400, "No IP address known for this client")

    # Minimal, common .rdp configuration. The user enters credentials when
    # connecting (we store no passwords in the file).
    rdp_content = "\r\n".join([
        "screen mode id:i:2",              # fullscreen
        "use multimon:i:0",
        "desktopwidth:i:1280",
        "desktopheight:i:720",
        "session bpp:i:32",
        f"full address:s:{host}",
        "audiomode:i:0",
        "redirectclipboard:i:1",           # share clipboard
        "autoreconnection enabled:i:1",
        "authentication level:i:0",        # no strict server auth (lab-friendly)
        "prompt for credentials:i:1",      # ask for credentials
        "negotiate security layer:i:1",
        "",
    ])

    db.add_audit_entry(user["username"], "rdp.file_generated", target=client_id, details=host)
    filename = f"{client.get('hostname', 'client')}.rdp"
    return PlainTextResponse(
        rdp_content,
        media_type="application/x-rdp",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ------------------------------------------------------------------
# OFFLINE QUEUE: actions "as soon as the agent is online" (e.g. shell command
# or software installation). If the agent is currently online, the caller can
# set run_now=1 - then the action runs immediately.
# ------------------------------------------------------------------

class QueueBody(BaseModel):
    kind: str = "shell"            # 'shell' | 'software'
    label: str = ""
    command: str = ""              # for kind='shell'
    elevated: bool = False
    timeout: int = 300
    package_id: str = ""           # for kind='software'


@router.get("/api/clients/{client_id}/queue")
async def client_queue_list(client_id: str, user: dict = Depends(get_current_user)):
    _perm(user, "use_terminal", client_id)
    return await db.call(db.queue_list, client_id)


@router.post("/api/clients/{client_id}/queue")
async def client_queue_add(client_id: str, body: QueueBody,
                           user: dict = Depends(get_current_user)):
    _perm(user, "use_terminal", client_id)
    if body.kind == "shell":
        cmd = (body.command or "").strip()
        if not cmd:
            raise HTTPException(400, "Command missing")
        payload = {"command": cmd, "elevated": bool(body.elevated),
                   "timeout": max(10, min(int(body.timeout or 300), 3600))}
        label = body.label or (cmd[:60] + ("..." if len(cmd) > 60 else ""))
    elif body.kind == "software":
        if not body.package_id:
            raise HTTPException(400, "package_id missing")
        payload = {"package_id": body.package_id}
        label = body.label or f"Install software ({body.package_id})"
    else:
        raise HTTPException(400, "Unknown action kind")
    row = await db.call(db.queue_add, client_id, body.kind, label, payload,
                        user.get("username"))
    db.add_audit_entry(user.get("username"), "queue.add", client_id, label)
    # Agent currently online? Then kick the queue immediately by using the
    # normal worker (same behavior as on registration).
    if state.is_online(client_id):
        from app.sockets import kick_offline_queue
        await kick_offline_queue(client_id)
    return row


@router.delete("/api/clients/{client_id}/queue/{qid}")
async def client_queue_delete(client_id: str, qid: str,
                              user: dict = Depends(get_current_user)):
    _perm(user, "use_terminal", client_id)
    await db.call(db.queue_delete, qid)
    return {"ok": True}


# ------------------------------------------------------------------
# BLACKBOX "last state" (screenshot + metric deltas + agent logs, stored by
# sockets.on_blackbox_snap). Intended for diagnostics AFTER a client has gone
# offline.
# ------------------------------------------------------------------

@router.get("/api/clients/{client_id}/blackbox")
async def client_blackbox(client_id: str, user: dict = Depends(get_current_user)):
    _perm(user, "access_clients", client_id)
    import json as _j
    from app.paths import STORAGE_DIR
    d = STORAGE_DIR / "blackbox" / client_id
    try:
        st = _j.loads((d / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ts": None}
    try:
        metrics_tail = (d / "metrics.jsonl").read_text(encoding="utf-8").splitlines()[-120:]
    except OSError:
        metrics_tail = []
    try:
        log_tail = (d / "log.txt").read_text(encoding="utf-8")[-12000:]
    except OSError:
        log_tail = ""
    return {"ts": st.get("ts"), "metrics": st.get("metrics") or {},
            "has_shot": (d / "screenshot.jpg").exists(),
            "deltas": metrics_tail, "log": log_tail}


@router.get("/api/clients/{client_id}/blackbox/screenshot")
async def client_blackbox_shot(client_id: str, user: dict = Depends(get_current_user)):
    _perm(user, "access_clients", client_id)
    from fastapi.responses import FileResponse as _FR
    from app.paths import STORAGE_DIR
    p = STORAGE_DIR / "blackbox" / client_id / "screenshot.jpg"
    if not p.exists():
        raise HTTPException(404, "No screenshot available")
    return _FR(p, media_type="image/jpeg",
               headers={"Cache-Control": "no-cache"})


# ------------------------------------------------------------------
# TEMPORARY USER with a one-time password (Linux + Windows).
# The password is generated by the AGENT and only passed through here -
# neither stored nor logged (audit only username + expiry).
# ------------------------------------------------------------------

class TempUserBody(BaseModel):
    expiry: str = "1h"       # 1h | 2h | 4h | 24h | logoff | never


@router.post("/api/clients/{client_id}/tempuser")
async def client_tempuser(client_id: str, body: TempUserBody,
                          user: dict = Depends(get_current_user)):
    _perm(user, "use_terminal", client_id)
    if body.expiry not in ("1h", "2h", "4h", "24h", "logoff", "never"):
        raise HTTPException(400, "Invalid expiry")
    from app.sockets import request_tempuser
    try:
        res = await request_tempuser(client_id, body.expiry)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    except (TimeoutError, asyncio.TimeoutError):
        raise HTTPException(504, "Agent did not answer in time")
    if not res.get("ok"):
        raise HTTPException(500, res.get("error") or "Creation failed")
    db.add_audit_entry(user.get("username"), "client.tempuser", client_id,
                       f"{res.get('username')} (expiry: {body.expiry})")
    return {"username": res.get("username"), "password": res.get("password"),
            "note": res.get("note") or ""}


# ------------------------------------------------------------------
# MAINTENANCE MODE (Core): alarms off, protection mechanisms suspended
# (USB lock, patch window ...). duration: "30m","1h","2h","4h",
# "8h","24h", "manual" (until ended manually) or "off" (end).
# ------------------------------------------------------------------

_MAINT_MIN = {"30m": 30, "1h": 60, "2h": 120, "4h": 240, "8h": 480, "24h": 1440, "manual": 0}


class MaintenanceBody(BaseModel):
    duration: str = "1h"
    client_ids: list[str] = []      # only for the bulk route


async def _set_maint(client_id: str, duration: str, user: dict) -> dict:
    _perm(user, "edit_clients", client_id)
    if duration != "off" and duration not in _MAINT_MIN:
        raise HTTPException(400, "Invalid duration")
    await db.call(db.set_maintenance, client_id,
                  None if duration == "off" else _MAINT_MIN[duration])
    db.add_audit_entry(user.get("username"), "client.maintenance", client_id,
                       "ended" if duration == "off" else f"on ({duration})")
    from app.sockets import push_maintenance
    return await push_maintenance(client_id)


@router.post("/api/clients/{client_id}/maintenance")
async def client_maintenance_set(client_id: str, body: MaintenanceBody,
                                 user: dict = Depends(get_current_user)):
    return await _set_maint(client_id, body.duration, user)


@router.post("/api/clients-maintenance")
async def clients_maintenance_bulk(body: MaintenanceBody,
                                   user: dict = Depends(get_current_user)):
    out = {}
    for cid in dict.fromkeys(body.client_ids or []):
        try:
            out[cid] = await _set_maint(cid, body.duration, user)
        except HTTPException as exc:
            out[cid] = {"error": exc.detail}
    return out
