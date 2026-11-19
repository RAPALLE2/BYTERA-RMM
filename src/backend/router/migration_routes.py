"""
routers/migration_routes.py
---------------------------
AUTO-MIGRATION between two BYTERA backends (Settings -> Migration).

Flow: on the NEW backend you log in through the migration window with an admin
account of the OLD RMM. The backends connect, the new backend downloads
EVERYTHING and adopts it on the next start:

  * Database             (consistent SQLite snapshot: clients, users, settings,
                          module settings, activation codes, tickets, audit,
                          ... simply everything)
  * data/                (.env with agent/E2E/TLS keys -> agents stay
                          authenticated; modules.json, module data folders)
  * storage/             (complete /storage: dist, web, spaces, docker,
                          recordings, tickets, audio, temp-usb, ...)
  * branding images      (frontend/image)
  * custom modules       (modules/<id> that do not exist on the new backend)

Address of the agents (selectable in the window):
  * "same": the new backend will be reachable at the PREVIOUS address (IP/DNS is
            moved) -> nothing to do on the agents.
  * "new":  the address changes -> the OLD backend sends the new address to all
            online-connected agents via "migrate-server"; agents that are
            currently offline get it on their next connect (setting
            migration_handoff_url, see sockets.py).

Both backends must run on a code revision that includes this router.
The actual rebuild happens on restart in db._apply_pending_migration().
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import ssl
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app import bgjobs, db
from app.auth import get_current_user
from app.paths import (BACKEND_DIR, BRANDING_DIR, DATA_DIR,
                       MODULES_DIR, STORAGE_DIR)

router = APIRouter(tags=["migration"])

STAGING_DIR = BACKEND_DIR / "migration_incoming"
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024 * 1024    # 20 GB safety upper bound
HTTP_TIMEOUT = 30


def _admin(user: dict) -> None:
    if (user or {}).get("role") != "admin":
        raise HTTPException(403, "Migration requires an admin account")


def _dir_size(p: Path) -> int:
    total = 0
    if p.is_dir():
        for f in p.rglob("*"):
            try:
                if f.is_file() and not f.is_symlink():
                    total += f.stat().st_size
            except OSError:
                pass
    return total


def _local_info() -> dict:
    version = "?"
    try:
        version = (DATA_DIR / "version.txt").read_text(encoding="utf-8").strip()
    except OSError:
        pass
    counts = {}
    try:
        for name, sql in (("clients", "SELECT COUNT(*) FROM clients"),
                          ("users", "SELECT COUNT(*) FROM users")):
            counts[name] = _sql_count(sql)
    except Exception:
        pass
    return {
        "migration": True,                       # "this backend supports migration"
        "version": version,
        "counts": counts,
        "sizes": {
            "data": _dir_size(DATA_DIR),
            "storage": _dir_size(STORAGE_DIR),
            "modules": _dir_size(Path(MODULES_DIR)),
        },
    }


def _sql_count(sql: str) -> int:
    row = db._conn.execute(sql).fetchone()
    return int(row[0]) if row else 0


# ==================================================================
# EXPORT SIDE (runs on the OLD backend)
# ==================================================================

@router.get("/api/migration/info")
def migration_info(user: dict = Depends(get_current_user)):
    _admin(user)
    return _local_info()


def _build_export_zip(dest: Path) -> None:
    """Packs data/ (with DB snapshot), storage/, branding and all modules."""
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        # --- Consistent DB snapshot (online backup API) ---
        with tempfile.TemporaryDirectory(prefix="mig-db-") as td:
            snap = Path(td) / "data.sqlite"
            target = sqlite3.connect(snap)
            try:
                db._conn.backup(target)
            finally:
                target.close()
            zf.write(snap, "data/data.sqlite")

        def add_tree(src: Path, arc_prefix: str, skip_names: set[str] = frozenset()):
            if not src.is_dir():
                return
            for f in sorted(src.rglob("*")):
                if not f.is_file() or f.is_symlink():
                    continue
                rel = f.relative_to(src)
                if rel.parts and rel.parts[0] in skip_names:
                    continue
                try:
                    zf.write(f, f"{arc_prefix}/{rel.as_posix()}")
                except OSError:
                    pass  # file being written/locked right now: skip

        # data/ without the live DB (snapshot is already included), without
        # backups and without version.txt/requirements.txt (part of the code).
        add_tree(DATA_DIR, "data",
                 skip_names={"data.sqlite", "data.sqlite-wal", "data.sqlite-shm",
                             "backup", "version.txt", "requirements.txt"})
        add_tree(STORAGE_DIR, "storage")
        add_tree(BRANDING_DIR, "branding")
        add_tree(Path(MODULES_DIR), "modules")

        zf.writestr("manifest.json", json.dumps({
            "kind": "bytera-migration",
            "created": int(time.time() * 1000),
            "info": _local_info(),
        }, indent=2))


@router.get("/api/migration/archive")
def migration_archive(user: dict = Depends(get_current_user)):
    _admin(user)
    tmp = Path(tempfile.mkdtemp(prefix="mig-export-")) / "migration.zip"
    try:
        _build_export_zip(tmp)
    except Exception as exc:
        shutil.rmtree(tmp.parent, ignore_errors=True)
        raise HTTPException(500, f"Export failed: {exc}")
    db.add_audit_entry(user.get("username"), "migration.export", None,
                       f"migration archive exported ({tmp.stat().st_size} bytes)")

    # Clean up the temp folder after streaming.
    class _CleanupFileResponse(FileResponse):
        async def __call__(self, scope, receive, send):
            try:
                await super().__call__(scope, receive, send)
            finally:
                shutil.rmtree(tmp.parent, ignore_errors=True)

    return _CleanupFileResponse(tmp, media_type="application/zip",
                                filename="bytera-migration.zip")


class HandoffBody(BaseModel):
    url: str


@router.post("/api/migration/handoff")
async def migration_handoff(body: HandoffBody, user: dict = Depends(get_current_user)):
    """Move all agents to the NEW backend address (now + on the next connect
    if an agent is currently offline)."""
    _admin(user)
    url = (body.url or "").strip().rstrip("/")
    from urllib.parse import urlparse
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise HTTPException(400, "New address must start with http:// or https://")
    await db.call(db.set_setting, "migration_handoff_url", url)

    from app.sockets import send_to_agent
    notified = 0
    for c in await db.call(db.list_clients):
        try:
            if await send_to_agent(c["id"], "migrate-server", {"url": url}):
                notified += 1
        except Exception:
            pass
    db.add_audit_entry(user.get("username"), "migration.handoff", None,
                       f"agents moved to {url} ({notified} reached online)")
    return {"ok": True, "notified": notified}


# ==================================================================
# IMPORT SIDE (runs on the NEW backend)
# ==================================================================

class RemoteBody(BaseModel):
    url: str
    username: str
    password: str
    code: str = ""          # 2FA one-time code, if the account has one


class ImportBody(RemoteBody):
    mode: str = "same"      # "same" = address stays | "new" = new address
    new_url: str = ""       # for mode="new": address of THIS new backend


def _remote_request(base: str, path: str, token: str | None = None,
                    payload: dict | None = None, out_file: Path | None = None,
                    progress=None) -> dict | None:
    """HTTP(S) call to the old backend (synchronous; use via asyncio.to_thread).
    For self-signed certificates it retries once unverified."""
    url = base.rstrip("/") + path
    headers = {"User-Agent": "BYTERA-RMM Migration"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)

    def _do(ctx):
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=ctx) as r:
            if out_file is not None:
                done = 0
                with open(out_file, "wb") as f:
                    while True:
                        chunk = r.read(1024 * 1024)
                        if not chunk:
                            break
                        done += len(chunk)
                        if done > MAX_ARCHIVE_BYTES:
                            raise RuntimeError("Archive exceeds the 20 GB limit")
                        f.write(chunk)
                        if progress:
                            progress(done)
                return None
            body = r.read(50 * 1024 * 1024)
            return json.loads(body.decode("utf-8", "replace") or "null")

    try:
        return _do(None)
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8", "replace")).get("detail")
        except Exception:
            detail = None
        raise RuntimeError(detail or f"{e.code} {e.reason}")
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return _do(ctx)
        raise RuntimeError(f"{getattr(e, 'reason', e)}")


def _remote_login(base: str, username: str, password: str, code: str) -> str:
    res = _remote_request(base, "/api/auth/login", payload={
        "username": username, "password": password, "code": code or "",
    })
    token = (res or {}).get("token")
    if not token:
        raise RuntimeError("Login to the old RMM failed (no token)")
    return token


@router.post("/api/migration/check")
async def migration_check(body: RemoteBody, user: dict = Depends(get_current_user)):
    """Test the connection to the old RMM: login + fetch info (no transfer yet)."""
    _admin(user)
    base = (body.url or "").strip()
    if not base.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "Address must start with http:// or https://")
    try:
        token = await asyncio.to_thread(_remote_login, base, body.username,
                                        body.password, body.code)
        info = await asyncio.to_thread(_remote_request, base,
                                       "/api/migration/info", token)
    except RuntimeError as exc:
        raise HTTPException(502, str(exc))
    if not (info or {}).get("migration"):
        raise HTTPException(502, "The old backend does not know migration yet - "
                                 "please update it to the current revision first")
    return {"remote": info, "local": _local_info()}


def _schedule_restart(delay: float = 2.0) -> None:
    def _restart():
        time.sleep(delay)
        print("[migration] restart - migrated data will be adopted on start ...")
        os.execv(sys.executable, [sys.executable] + sys.argv)
    threading.Thread(target=_restart, daemon=True).start()


@router.post("/api/migration/import")
async def migration_import(body: ImportBody, background: bool = False,
                           user: dict = Depends(get_current_user)):
    """The actual migration (background job with live log)."""
    _admin(user)
    base = (body.url or "").strip()
    if not base.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "Address must start with http:// or https://")
    mode = "new" if body.mode == "new" else "same"
    new_url = (body.new_url or "").strip().rstrip("/")
    if mode == "new" and not new_url.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "Please provide the new backend address for the agents")

    async def work(job):
        def step(txt):
            job.log(txt)
            job.detail = txt
        step(f"Connecting to {base} ...")
        token = await asyncio.to_thread(_remote_login, base, body.username,
                                        body.password, body.code)
        info = await asyncio.to_thread(_remote_request, base,
                                       "/api/migration/info", token)
        if not (info or {}).get("migration"):
            raise RuntimeError("The old backend does not know migration yet - "
                               "please update it there first")
        total = sum((info.get("sizes") or {}).values())
        step(f"Old RMM: version {info.get('version')}, "
                f"{(info.get('counts') or {}).get('clients', '?')} clients, "
                f"~{total // 1048576} MB data")

        shutil.rmtree(STAGING_DIR, ignore_errors=True)
        STAGING_DIR.mkdir(parents=True, exist_ok=True)
        archive = STAGING_DIR / "migration.zip"

        step("Downloading migration archive (the old backend is packing it) ...")
        last = {"mb": -50}

        def prog(done):
            mb = done // 1048576
            if mb >= last["mb"] + 50:
                last["mb"] = mb
                step(f"  ... {mb} MB transferred")

        await asyncio.to_thread(_remote_request, base, "/api/migration/archive",
                                token, None, archive, prog)
        step(f"Download complete ({archive.stat().st_size // 1048576} MB) - extracting ...")

        with zipfile.ZipFile(archive) as zf:
            for i in zf.infolist():                      # Zip-Slip-Schutz
                dest = (STAGING_DIR / i.filename).resolve()
                if not str(dest).startswith(str(STAGING_DIR.resolve())):
                    raise RuntimeError("Archive contains disallowed paths")
            zf.extractall(STAGING_DIR)
        archive.unlink(missing_ok=True)
        if not (STAGING_DIR / "data" / "data.sqlite").exists():
            raise RuntimeError("Archive incomplete (no database included)")

        if mode == "new":
            step(f"Moving agents to the new address: {new_url} ...")
            res = await asyncio.to_thread(_remote_request, base,
                                          "/api/migration/handoff", token,
                                          {"url": new_url})
            job.log(f"  ... {(res or {}).get('notified', 0)} agents reached directly; "
                    "offline agents follow on their next connect to the old backend")
        else:
            job.log("Address stays the same - nothing to do on the agents "
                    "(move IP/DNS to this server)")

        (STAGING_DIR / "apply.json").write_text(json.dumps({
            "from": base, "mode": mode, "new_url": new_url,
            "at": int(time.time() * 1000), "by": user.get("username"),
        }), encoding="utf-8")
        db.add_audit_entry(user.get("username"), "migration.import", None,
                           f"Migration von {base} ({mode})")
        step("Everything transferred. The backend restarts and adopts the data - "
                "then please log in again (users come from the old RMM).")
        _schedule_restart()
        return {"ok": True, "restarting": True}

    return await bgjobs.run("migration", "import",
                            title=f"Migration von {base}", user=user,
                            work=work, background=background)
