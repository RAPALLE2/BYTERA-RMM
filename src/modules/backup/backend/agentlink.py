"""
agentlink.py (Modul "backup") - Auftraege an Agenten + restic-Bereitstellung.

Backend -> Agent:  "backup-cmd"      {requestId, op, spec, args}
Agent -> Backend:  "backup-progress" {requestId, line}
                   "backup-result"   {requestId, ok, error, ...}
"""

from __future__ import annotations

import asyncio
import bz2
import io
import threading
import urllib.request
import uuid
import zipfile

from app.paths import STORAGE_DIR

RESTIC_VERSION = "0.17.3"
TOOLS_DIR = STORAGE_DIR / "backup-tools"
_pending: dict[str, dict] = {}          # requestId -> {"fut": Future, "progress": callable}
_registered = False
_tool_lock = threading.Lock()


def register(ctx) -> None:
    global _registered
    if _registered:
        return

    async def on_progress(sid, data):
        data = data or {}
        p = _pending.get(str(data.get("requestId") or ""))
        if p and p.get("progress"):
            try:
                p["progress"](str(data.get("line") or "")[:500])
            except Exception:
                pass

    async def on_result(sid, data):
        data = data or {}
        p = _pending.pop(str(data.get("requestId") or ""), None)
        if p and not p["fut"].done():
            p["fut"].set_result(data)

    ctx.sio_on("backup-progress", on_progress, namespace="/agent")
    ctx.sio_on("backup-result", on_result, namespace="/agent")
    _registered = True


async def call(client_id: str, op: str, spec: dict | None = None, args: dict | None = None,
               timeout: float = 600.0, progress=None) -> dict:
    """Auftrag an EINEN Agenten schicken und auf das Ergebnis warten."""
    from app.sockets import send_to_agent
    rid = uuid.uuid4().hex
    fut = asyncio.get_running_loop().create_future()
    _pending[rid] = {"fut": fut, "progress": progress}
    ok = await send_to_agent(client_id, "backup-cmd",
                             {"requestId": rid, "op": op, "spec": spec or {}, "args": args or {}})
    if not ok:
        _pending.pop(rid, None)
        return {"ok": False, "error": "Client ist offline"}
    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        _pending.pop(rid, None)
        return {"ok": False, "error": f"Zeitüberschreitung nach {int(timeout)} s"}


# ------------------------------------------------------------------ restic-Binary

def restic_binary(os_name: str, arch: str) -> bytes:
    """Offizielles restic-Release fuer os/arch (vom Backend einmal geladen und gecacht)."""
    os_name = {"windows": "windows", "linux": "linux", "darwin": "darwin"}.get(os_name.lower())
    arch = {"amd64": "amd64", "x86_64": "amd64", "arm64": "arm64", "aarch64": "arm64",
            "386": "386", "i686": "386", "armv7l": "arm", "arm": "arm"}.get(arch.lower())
    if not os_name or not arch:
        raise ValueError("Nicht unterstützte Plattform")
    name = f"restic_{RESTIC_VERSION}_{os_name}_{arch}" + (".exe" if os_name == "windows" else "")
    target = TOOLS_DIR / name
    with _tool_lock:
        if target.exists():
            return target.read_bytes()
        base = f"https://github.com/restic/restic/releases/download/v{RESTIC_VERSION}/"
        if os_name == "windows":
            raw = urllib.request.urlopen(base + f"restic_{RESTIC_VERSION}_windows_{arch}.zip",
                                         timeout=180).read()
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                exe = next(n for n in zf.namelist() if n.endswith(".exe"))
                data = zf.read(exe)
        else:
            raw = urllib.request.urlopen(base + f"restic_{RESTIC_VERSION}_{os_name}_{arch}.bz2",
                                         timeout=180).read()
            data = bz2.decompress(raw)
        TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(target)
        return data
