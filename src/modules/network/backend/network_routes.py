"""
routers/network_routes.py
----------------------------
REST interface for the LAN scan (Network Center / toolbox network scanner).

Endpoints:
  GET /api/network/scan              -> start a new scan (optional ?subnet=192.168.5.)
  GET /api/network/scan/last         -> last scan result
  GET /api/network/portscan?ip=...   -> port scan of a single IP
Everywhere optional ?via=<node ID>: the scan then runs IN THE NODE'S NETWORK (node module
"network", must be loaded on the node). Job IDs of node scans: "n.<node>.<job>".

BACKEND-JOBS (Grundregel: Aufgaben laufen nie in der Browser-Sitzung):
  POST /api/network/scan/job            LAN-Scan als Backend-Job (?target=&speed=&via=)
  POST /api/network/portscan/job        Portscan als Backend-Job (?ip=&mode=&ports=&via=)
  GET  /api/network/scan/last?source=   last finished scan result of the source
Fortschritt, Ergebnis und Abbruch ueber /api/bgjobs (kind "network.scan" bzw.
"network.portscan"), live per Socket "bgjob:update". Neu laden oder ein anderer
Benutzer sehen denselben Job; das Ergebnis haelt das Backend.
Die alten Endpunkte unten bleiben fuer Skripte/REST-API erhalten.
"""

import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException

from app import bgjobs, db
from app.auth import can_access_client, get_current_user, require_perm
from rmm_mod_network.network_scan import (
    scan_local_network, scan_ports, parse_port_spec,
    start_scan_job, get_scan_job, cancel_scan_job, parse_scan_target, SCAN_SPEEDS,
)

router = APIRouter(prefix="/api/network", tags=["network"])


async def _node(user: dict, via: str, op: str, data: dict, timeout: float = 60):
    """Befehl an das Node-Modul "network" der gewaehlten Node."""
    from app import node_manager
    from app.auth import can_access_client
    if not can_access_client(user, via):
        raise HTTPException(404, "Node not found")
    try:
        node_manager.require_module(via, "network")
        return await node_manager.command(via, op, data, timeout=timeout)
    except node_manager.NodeError as e:
        raise HTTPException(409, str(e))


def _split_job(job_id: str) -> tuple[str, str]:
    if job_id.startswith("n.") and job_id.count(".") >= 2:
        _, node, jid = job_id.split(".", 2)
        return node, jid
    return "", job_id

# Simple in-memory cache for the last scan result.
_last_scan: dict = {"at": None, "devices": []}
_last_by_source: dict[str, dict] = {}     # source ("backend"/node ID) -> last finished result
_scan_in_progress = False


@router.get("/scan")
async def run_scan(subnet: str | None = None, user: dict = Depends(get_current_user)):
    """Starts a network scan. Optionally a specific subnet (e.g. '192.168.5.')."""
    require_perm(user, "network_scan")
    global _scan_in_progress, _last_scan

    if _scan_in_progress:
        raise HTTPException(409, "A scan is already running")

    _scan_in_progress = True
    try:
        devices = await scan_local_network(subnet)
        _last_scan = {"at": int(time.time() * 1000), "devices": devices, "subnet": subnet}
        return _last_scan
    finally:
        _scan_in_progress = False


# ------------------------------------------------------------------
# Job-based scan (large networks like 10.10 = /16, with progress)
# ------------------------------------------------------------------

@router.get("/scan/preview")
def scan_preview(target: str | None = None, user: dict = Depends(get_current_user)):
    """Checks the target spec and reports how large the scan would be."""
    require_perm(user, "network_scan")
    try:
        prefixes, label = parse_scan_target(target)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"label": label, "subnets": len(prefixes), "hosts": len(prefixes) * 254,
            "speeds": {k: {"workers": v[0], "per_subnet": v[1]} for k, v in SCAN_SPEEDS.items()}}


@router.post("/scan/start")
async def scan_start(target: str | None = None, speed: str = "normal", via: str = "",
                     user: dict = Depends(get_current_user)):
    """
    Starts a scan in the background and immediately returns the job ID.
      target: "192.168.1.", "10.10" (= /16), "10.10.0.0/16", empty = own network
      speed:  normal | fast | turbo  (Speed-Up distributes the /24 blocks across
              several parallel workers)
    """
    require_perm(user, "network_scan")
    if speed not in SCAN_SPEEDS:
        raise HTTPException(400, f"Unknown speed (allowed: {', '.join(SCAN_SPEEDS)})")
    try:
        parse_scan_target(target)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if via:
        r = await _node(user, via, "net-scan-start", {"target": target, "speed": speed})
        return {"job_id": f"n.{via}.{r['job_id']}", "via": via}
    job_id = start_scan_job(target, speed)
    return {"job_id": job_id}


@router.get("/scan/job/{job_id}")
async def scan_job_status(job_id: str, user: dict = Depends(get_current_user)):
    """Progress + (on completion) found devices."""
    require_perm(user, "network_scan")
    node, jid = _split_job(job_id)
    if node:
        return {**(await _node(user, node, "net-scan-status", {"job_id": jid})), "id": job_id, "via": node}
    job = get_scan_job(job_id)
    if not job:
        raise HTTPException(404, "Scan job not found (may have expired)")
    return {k: v for k, v in job.items() if k != "cancel"}


@router.post("/scan/job/{job_id}/cancel")
async def scan_job_cancel(job_id: str, user: dict = Depends(get_current_user)):
    require_perm(user, "network_scan")
    node, jid = _split_job(job_id)
    if node:
        return await _node(user, node, "net-scan-cancel", {"job_id": jid})
    if not cancel_scan_job(job_id):
        raise HTTPException(404, "Kein laufender Scan mit dieser ID")
    return {"ok": True}


@router.get("/scan/last")
def get_last_scan(source: str = "", user: dict = Depends(get_current_user)):
    require_perm(user, "network_scan")
    if source:
        if source != "backend" and not can_access_client(user, source):
            raise HTTPException(404, "Source not found")
        return _last_by_source.get(source) or {"at": None, "devices": []}
    return _last_scan


# ------------------------------------------------------------------
# LAN scan + port scan as a BACKEND JOB (app/bgjobs.py)
# ------------------------------------------------------------------
def _check_node(user: dict, via: str) -> None:
    from app import node_manager
    if not can_access_client(user, via):
        raise HTTPException(404, "Node not found")
    try:
        node_manager.require_module(via, "network")
    except node_manager.NodeError as e:
        raise HTTPException(409, str(e))


def _scan_runner(source: str, target: str | None, speed: str, via: str):
    async def runner(job: bgjobs.BgJob):
        from app import node_manager
        if via:
            r = await node_manager.command(via, "net-scan-start", {"target": target, "speed": speed}, timeout=60)
            jid = r["job_id"]

            async def status():
                return await node_manager.command(via, "net-scan-status", {"job_id": jid}, timeout=60)

            async def cancel():
                await node_manager.command(via, "net-scan-cancel", {"job_id": jid}, timeout=30)
        else:
            jid = start_scan_job(target, speed)

            async def status():
                j = get_scan_job(jid)
                if not j:
                    raise RuntimeError("Scan verschwunden")
                return {k: v for k, v in j.items() if k != "cancel"}

            async def cancel():
                cancel_scan_job(jid)

        sent_cancel, fails = False, 0
        while True:
            if job.cancelled and not sent_cancel:
                sent_cancel = True
                try:
                    await cancel()
                except Exception:
                    pass
            try:
                j = await status()
                fails = 0
            except Exception as e:            # node briefly gone -> do not give up immediately
                fails += 1
                if fails >= 15:
                    raise RuntimeError(f"Node no longer responds: {e}")
                await asyncio.sleep(2)
                continue
            th, dh = j.get("total_hosts") or 0, j.get("done_hosts") or 0
            job.update(phase="scan", pct=(dh / th * 100) if th else 0,
                       detail=f"{j.get('label', '')} · {dh:,}/{th:,} addresses · {j.get('found', 0)} found".replace(",", "."),
                       label=j.get("label", ""), done_hosts=dh, total_hosts=th,
                       done_subnets=j.get("done_subnets", 0), total_subnets=j.get("total_subnets", 0),
                       found=j.get("found", 0), max_parallel=j.get("max_parallel"))
            if j.get("status") in ("done", "error", "cancelled"):
                break
            await asyncio.sleep(0.7)

        if j["status"] == "error":
            raise RuntimeError(j.get("error") or "Scan failed")
        res = {"at": int(time.time() * 1000), "devices": j.get("devices") or [],
               "label": j.get("label", ""), "source": source, "subnet": target,
               "complete": j["status"] == "done"}
        if j["status"] == "done":                 # the BACKEND holds the result
            _last_by_source[source] = res
            if source == "backend":
                global _last_scan
                _last_scan = res
        return res
    return runner


@router.post("/scan/job")
async def scan_job_start(target: str | None = None, speed: str = "fast", via: str = "",
                         user: dict = Depends(get_current_user)):
    require_perm(user, "network_scan")
    if speed not in SCAN_SPEEDS:
        raise HTTPException(400, f"Unknown speed (allowed: {', '.join(SCAN_SPEEDS)})")
    try:
        _, label = parse_scan_target(target)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if via:
        _check_node(user, via)
    source = via or "backend"
    job, new = bgjobs.start(
        "network.scan", source, title=f"LAN-Scan {label}", user=user, perm="network_scan",
        may_see=(lambda u: can_access_client(u, via)) if via else None,
        runner=_scan_runner(source, target, speed, via),
        meta={"target": target or "", "speed": speed, "via": via, "label": label})
    if new:
        db.add_audit_entry(user["username"], "network.scan", target=f"{source}:{label}")
    return {**job.public(), "attached": not new}


def _portscan_plan(mode: str, ports: str | None) -> tuple[list[int] | None, int, float]:
    if mode == "all":
        return list(range(1, 65536)), 500, 0.5
    if mode == "custom":
        plist = parse_port_spec(ports or "")
        if not plist:
            raise HTTPException(400, "No valid ports given.")
        return plist, (500 if len(plist) > 2000 else 200), 0.6
    return None, 100, 1.0                      # gaengige Ports


@router.post("/portscan/job")
async def portscan_job_start(ip: str, mode: str = "standard", ports: str | None = None, via: str = "",
                             user: dict = Depends(get_current_user)):
    require_perm(user, "port_scan")
    ip = (ip or "").strip()
    if not ip:
        raise HTTPException(400, "IP fehlt")
    mode = (mode or "standard").lower()
    plist, conc, tmo = _portscan_plan(mode, ports)
    if via:
        _check_node(user, via)
    source = via or "backend"

    async def runner(job: bgjobs.BgJob):
        from app import node_manager
        n = len(plist) if plist else None
        job.update(phase="scan", detail=f"{ip}: checking {n:,} ports".replace(",", ".")
                   if n else f"{ip}: checking common ports")
        if via:
            r = await node_manager.command(via, "net-portscan",
                                           {"ip": ip, "ports": plist or [], "concurrency": conc, "timeout": tmo},
                                           timeout=900 if mode == "all" else 180)
            found = r.get("open") or []
        else:
            found = await scan_ports(ip, plist, concurrency=conc, timeout=tmo)
        return {"ip": ip, "mode": mode, "scanned": n, "ports": found, "via": via}

    def hard_cancel(job: bgjobs.BgJob):
        if job._task:                          # Portscan hat keine Zwischenstaende -> sofort beenden
            job._task.cancel()

    job, new = bgjobs.start(
        "network.portscan", f"{source}:{ip}", title=f"Portscan {ip}", user=user, perm="port_scan",
        may_see=(lambda u: can_access_client(u, via)) if via else None,
        runner=runner, on_cancel=hard_cancel,
        meta={"ip": ip, "mode": mode, "ports": ports or "", "via": via})
    if new:
        db.add_audit_entry(user["username"], "network.portscan", target=f"{source}:{ip}")
    return {**job.public(), "attached": not new}


@router.get("/portscan")
async def run_portscan(
    ip: str,
    mode: str = "standard",       # "standard" | "all" | "custom"
    ports: str | None = None,      # bei mode=custom: z.B. "22,80,8000-8100"
    via: str = "",                 # node ID: port scan from the node's network
    user: dict = Depends(get_current_user),
):
    """
    Portscan einer IP.
      - standard: common ports (fast)
      - all:      all ports 1-65535 (takes longer, higher concurrency)
      - custom:   freely given ports/ranges (parameter 'ports')
    """
    require_perm(user, "port_scan")
    mode = (mode or "standard").lower()
    if via:
        if mode == "all":
            plist, conc, tmo = list(range(1, 65536)), 500, 0.5
        elif mode == "custom":
            plist = parse_port_spec(ports or "")
            if not plist:
                raise HTTPException(400, "No valid ports given.")
            conc, tmo = (500 if len(plist) > 2000 else 200), 0.6
        else:
            plist, conc, tmo = [], 100, 1.0
        r = await _node(user, via, "net-portscan", {"ip": ip, "ports": plist, "concurrency": conc, "timeout": tmo},
                        timeout=900 if mode == "all" else 180)
        return {"ip": ip, "mode": mode, "scanned": len(plist) if mode != "standard" else None,
                "ports": r.get("open") or [], "via": via}
    if mode == "all":
        port_list = list(range(1, 65536))
        open_ports = await scan_ports(ip, port_list, concurrency=500, timeout=0.5)
    elif mode == "custom":
        port_list = parse_port_spec(ports or "")
        if not port_list:
            raise HTTPException(400, "No valid ports given.")
        # Scale more conservatively, depending on the number of ports.
        conc = 500 if len(port_list) > 2000 else 200
        open_ports = await scan_ports(ip, port_list, concurrency=conc, timeout=0.6)
    else:
        open_ports = await scan_ports(ip)  # common ports
    return {"ip": ip, "mode": mode, "scanned": len(port_list) if mode != "standard" else None, "ports": open_ports}
