# node_module.py (node part of module "addir") - DNS & Group Policy from the DC
# -------------------------------------------------------------------------------
# Intended for a BYTERA node ON the domain controller (or in the same LAN):
#   * LDAP goes to the given server (default: localhost)
#   * SYSVOL is - when available - used directly as a local folder
#     (C:\Windows\SYSVOL\sysvol\<domain>), otherwise via SMB
#   * ADMX templates: central store in SYSVOL, otherwise C:\Windows\PolicyDefinitions
# Same logic as in the backend (adcore.py sits in this folder).
#
#   "ad-call" {conn: {...}, op, args} -> result of the core operation
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

_core = None


def _load_core():
    global _core
    if _core is None:
        spec = importlib.util.spec_from_file_location("rmm_node_adcore", Path(__file__).with_name("adcore.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _core = mod
    return _core


def _ensure_libs(need_smb: bool):
    missing = []
    try:
        import ldap3  # noqa: F401
    except ImportError:
        missing.append("ldap3")
    if need_smb:
        try:
            import smbclient  # noqa: F401
        except ImportError:
            missing.append("smbprotocol")
    if missing:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", *missing],
                           capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            raise RuntimeError(f"Could not install Python packages {', '.join(missing)}: {r.stderr[-300:]}")


def _local_sysvol(domain: str):
    root = os.environ.get("SystemRoot", "C:\\Windows")
    for p in (os.path.join(root, "SYSVOL", "sysvol", domain), os.path.join(root, "SYSVOL", "domain")):
        if os.path.isdir(os.path.join(p, "Policies")):
            return p
    return None


def ad_call(ctx, data: dict):
    conn = data.get("conn") or {}
    base_dn = (conn.get("base_dn") or "").strip()
    domain = ".".join(p.split("=", 1)[1] for p in base_dn.split(",") if p.strip().upper().startswith("DC="))
    server = (conn.get("server") or "localhost").replace("ldaps://", "").replace("ldap://", "").split(":")[0]
    sysvol = _local_sysvol(domain)
    _ensure_libs(need_smb=sysvol is None)
    core = _load_core()
    import ldap3
    tls = None
    use_ssl = bool(conn.get("use_ssl"))
    if use_ssl or conn.get("start_tls"):
        import ssl
        tls = ldap3.Tls(validate=ssl.CERT_REQUIRED if conn.get("verify_cert") else ssl.CERT_NONE)
    srv = ldap3.Server(server, port=int(conn["port"]) if conn.get("port") else None, use_ssl=use_ssl,
                       tls=tls, get_info=ldap3.ALL, connect_timeout=8)
    user, pw = conn.get("bind_user") or "", conn.get("bind_password") or ""
    c = ldap3.Connection(srv, user=user or None, password=pw or None,
                         authentication=ldap3.SIMPLE if user else ldap3.ANONYMOUS, raise_exceptions=False)
    if conn.get("start_tls") and not use_ssl:
        c.open()
        c.start_tls()
    if not c.bind():
        raise RuntimeError("AD login failed (check the bind user/password)")
    try:
        if sysvol:
            fs = core.LocalFs(sysvol)
        else:
            fs = core.SmbFs(f"\\\\{server}\\SYSVOL\\{domain}", server, conn.get("smb_user") or user, pw)
        local_pd = os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "PolicyDefinitions")
        admx_fb = core.LocalFs(local_pd) if os.path.isdir(local_pd) else None
        cr = core.Core(c, base_dn, fs, admx_fb)
        try:
            return {"result": cr.dispatch(str(data.get("op") or ""), data.get("args") or {})}
        except core.CoreError as exc:
            return {"error": str(exc)}
    finally:
        try:
            c.unbind()
        except Exception:
            pass


OPS = {"ad-call": ad_call}


def start(ctx):
    return {"ready": True}


def stop(ctx):
    return {"stopped": True}
