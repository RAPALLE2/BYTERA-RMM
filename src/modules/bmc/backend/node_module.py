# node_module.py (Node-Teil von Modul "bmc") - Redfish vom Management-Netz aus
# ----------------------------------------------------------------------------
# Laeuft auf einer BYTERA-Node, die die iLO/iDRAC-Schnittstellen erreicht
# (Management-LAN ist oft vom Backend isoliert). Dieselbe Logik wie im Backend
# (redfish.py liegt daneben).
#
#   "bmc-call" {conn: {host,port,username,password,verify_tls,fingerprint,insecure_http}, op, args}
#       -> {result: ...} | {error: "..."}
import importlib.util
import subprocess
import sys
from pathlib import Path

_rf = None


def _load():
    global _rf
    if _rf is None:
        spec = importlib.util.spec_from_file_location("rmm_node_redfish", Path(__file__).with_name("redfish.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _rf = mod
    return _rf


def _ensure_httpx():
    try:
        import httpx  # noqa: F401
    except ImportError:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "httpx"],
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"httpx ließ sich nicht installieren: {r.stderr[-300:]}")


def bmc_call(ctx, data: dict):
    _ensure_httpx()
    rf = _load()
    conn = data.get("conn") or {}
    try:
        client = rf.RedfishClient(conn).connect()
    except rf.RedfishError as exc:
        return {"error": str(exc)}
    try:
        return {"result": client.dispatch(str(data.get("op") or ""), data.get("args") or {})}
    except rf.RedfishError as exc:
        return {"error": str(exc)}
    finally:
        client.close()


OPS = {"bmc-call": bmc_call}


def start(ctx):
    return {"ready": True}


def stop(ctx):
    return {"stopped": True}
