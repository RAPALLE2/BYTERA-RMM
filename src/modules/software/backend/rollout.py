"""
rollout.py (module "software") - the actual installation engine.

install(client_id, package_id) sends the package to the agent (file paths to
download + command) and waits for the result. Used both by the deploy endpoint
(online clients) and by the core's offline queue (app/sockets.py, kind "software").
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from pathlib import Path

from app.paths import MODULES_DATA_DIR, STORAGE_DIR

DIR = MODULES_DATA_DIR / "software"
PKG_FILE = DIR / "packages.json"
FILES_DIR = STORAGE_DIR / "software"   # not public - agents download with an agent token
_lock = threading.RLock()

_pending: dict[str, asyncio.Future] = {}
_registered = False


def load_packages() -> dict:
    with _lock:
        try:
            return json.loads(PKG_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"packages": {}}


def save_packages(data: dict) -> None:
    with _lock:
        DIR.mkdir(parents=True, exist_ok=True)
        tmp = PKG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(PKG_FILE)


def register_handlers() -> None:
    """Register the agents' result event once on the socket server."""
    global _registered
    if _registered:
        return
    from app.sockets import sio

    async def _on_result(sid, data):
        data = data or {}
        fut = _pending.pop(str(data.get("requestId") or ""), None)
        if fut is not None and not fut.done():
            fut.set_result(data)

    sio.on("software-install-result", _on_result, namespace="/agent")
    _registered = True


def command_for(pkg: dict, platform: str) -> str:
    """Installationskommando je Plattform (win/linux), leer = Automatik."""
    plat = "win" if "win" in (platform or "").lower() else "linux"
    return str(pkg.get("commands", {}).get(plat) or pkg.get("commands", {}).get("all") or "")


async def install(client_id: str, package_id: str, timeout: float = 1800.0) -> dict:
    """Install the package on ONE (online) client and wait for the result."""
    from app import db
    from app.sockets import send_to_agent

    pkg = load_packages()["packages"].get(package_id or "")
    if not pkg:
        return {"ok": False, "error": f"Paket {package_id!r} nicht gefunden"}
    client = await db.call(db.get_client, client_id)
    if not client:
        return {"ok": False, "error": "Client nicht gefunden"}

    from urllib.parse import quote
    files = [{"name": f["name"],
              "path": f"/api/software/agent-file/{package_id}/{quote(f['name'])}"}
             for f in pkg.get("files", [])]
    request_id = uuid.uuid4().hex
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _pending[request_id] = fut
    ok = await send_to_agent(client_id, "software-install", {
        "requestId": request_id,
        "files": files,
        "command": command_for(pkg, client.get("platform") or ""),
        "name": pkg.get("name") or package_id,
    })
    if not ok:
        _pending.pop(request_id, None)
        return {"ok": False, "error": "Client ist offline"}
    try:
        res = await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        _pending.pop(request_id, None)
        return {"ok": False, "error": f"Timeout after {int(timeout)} s"}
    code = int(res.get("code") if res.get("code") is not None else 1)
    return {"ok": code == 0, "code": code, "output": res.get("output") or "",
            "error": None if code == 0 else f"Exit-Code {code}"}
