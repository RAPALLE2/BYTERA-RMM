# node_module.py (Node-Teil von Modul "topology")
# ------------------------------------------------
# Topologie-Erkennung IM NETZ DIESER NODE (statt vom RMM-Server aus).
# topo_discovery.py ist dieselbe Datei wie im Backend-Teil (nur Standardbibliothek).
#   "topo-start"   {target}    -> {job_id}
#   "topo-status"  {job_id}    -> Job (Fortschritt, bei Abschluss "result" = Graph)
#   "topo-cancel"  {job_id}    -> {ok}
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "rmm_node_topo_discovery", Path(__file__).with_name("topo_discovery.py"))
td = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(td)


async def topo_start(ctx, data: dict) -> dict:
    return {"job_id": td.start_job(data.get("target") or None)}


def topo_status(ctx, data: dict) -> dict:
    job = td.get_job(str(data.get("job_id") or ""))
    if not job:
        raise RuntimeError("Erkennung nicht gefunden (evtl. abgelaufen)")
    return {k: v for k, v in job.items() if k != "cancel"}


def topo_cancel(ctx, data: dict) -> dict:
    return {"ok": td.cancel_job(str(data.get("job_id") or ""))}


OPS = {"topo-start": topo_start, "topo-status": topo_status, "topo-cancel": topo_cancel}


def start(ctx):
    return {"ready": True}


def stop(ctx):
    return {"stopped": True}
