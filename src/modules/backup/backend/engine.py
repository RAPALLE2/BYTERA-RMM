"""
engine.py (Modul "backup") - fuehrt Backups aus (Proxmox-Stil).

Zwei Engines je Client:
  * "agent": der BYTERA-Agent sichert selbst
       - restic (Ordner ODER Image), Ziel je nach Weg:
           direct      -> SMB-Freigabe oder SFTP direkt vom Client
           backend     -> restic-REST am Backend, Daten lokal am Backend
           via_backend -> restic-REST am Backend, Backend legt auf NAS ab (SMB/SFTP/FTP)
       - Proxmox Backup Server (direct, Linux-Clients) per proxmox-backup-client
  * "pve": Client ist eine Proxmox-VM/-LXC -> vzdump ueber das Proxmox-Modul
           (Ziel = Proxmox-Storage, auch ein dort eingebundener PBS)

Aufbewahrung wie bei Proxmox: keep-last/-hourly/-daily/-weekly/-monthly/-yearly.
Komprimierung wie bei Proxmox: none | fast | good | zstd (schnell & gut).
"""

from __future__ import annotations

import asyncio
import time

from app import db

from rmm_mod_backup import agentlink, store

KEEP_KEYS = ("keep_last", "keep_hourly", "keep_daily", "keep_weekly", "keep_monthly", "keep_yearly")
COMPRESSION = {"none": "off", "fast": "fastest", "good": "max", "zstd": "auto"}
PVE_COMPRESS = {"none": "0", "fast": "lzo", "good": "gzip", "zstd": "zstd"}
ROUTES = {
    # Weg -> erlaubte Speicherziel-Typen
    "direct": ("smb", "sftp", "pbs"),
    "backend": ("backend",),
    "via_backend": ("smb", "sftp", "ftp"),
}


class EngineError(Exception):
    pass


def check_route(storage: dict, route: str) -> None:
    allowed = ROUTES.get(route)
    if not allowed or storage.get("type") not in allowed:
        names = {"direct": "direkt", "backend": "aufs Backend", "via_backend": "über das Backend"}
        raise EngineError(f"Speicherziel-Typ „{storage.get('type')}“ ist für den Weg "
                          f"„{names.get(route, route)}“ nicht möglich")


def retention(job: dict) -> dict:
    r = job.get("retention") or {}
    return {k: int(r[k]) for k in KEEP_KEYS if str(r.get(k) or "").strip() not in ("", "0")}


def render_name(template: str, client: dict, job: dict | None = None) -> str:
    t = template or "{{client}} {{date}} {{time}}"
    host = client.get("short_name") or client.get("hostname") or client.get("id")
    now = time.localtime()
    return (t.replace("{{client}}", host).replace("{{hostname}}", client.get("hostname") or "")
             .replace("{{date}}", time.strftime("%Y-%m-%d", now))
             .replace("{{time}}", time.strftime("%H:%M", now))
             .replace("{{job}}", (job or {}).get("name") or "manuell"))[:120]


def agent_spec(client_id: str, storage: dict, route: str) -> dict:
    """Alles, was der Agent zum Erreichen des Repos braucht."""
    check_route(storage, route)
    sec = store.repo_secret(client_id, storage["id"])
    sub = f"bytera-backup/{client_id}"
    t = storage["type"]
    if t == "pbs":
        return {"kind": "pbs", "backup_id": client_id,
                "pbs": {k: storage.get(k, "") for k in
                        ("server", "port", "datastore", "user", "secret", "fingerprint", "namespace")}}
    if route in ("backend", "via_backend"):
        return {"kind": "restic", "password": sec["password"],
                "rest_path": f"/api/backup/rest/{sec['token']}/"}
    if t == "smb":
        return {"kind": "restic", "password": sec["password"], "subdir": sub,
                "smb": {k: storage.get(k, "") for k in ("server", "share", "path", "user", "password", "domain")}}
    if t == "sftp":
        return {"kind": "restic", "password": sec["password"], "subdir": sub,
                "sftp": {k: storage.get(k, "") for k in ("host", "port", "user", "path", "private_key")}}
    raise EngineError("Unbekannte Kombination aus Ziel und Weg")


def default_route(storage: dict) -> str:
    t = storage.get("type")
    return {"backend": "backend", "ftp": "via_backend"}.get(t, "direct")


def pve_target(client_id: str) -> dict | None:
    """Proxmox-Zuordnung eines Clients (VM/LXC) - None, wenn keine."""
    try:
        from rmm_mod_proxmox import match
        m = match.mapping(client_id)
        return dict(m) if m and m.get("vmid") else None
    except Exception:
        return None


def _pve_client(host_cid: str):
    try:
        from rmm_mod_proxmox import pve_client
    except ImportError:
        raise EngineError("Das Proxmox-Modul ist nicht aktiv")
    return pve_client.client_for(host_cid)


async def pve_wait(host_cid: str, node: str, upid: str, log=None, timeout: float = 6 * 3600) -> None:
    cl = _pve_client(host_cid)
    t0, seen = time.time(), 0
    while time.time() - t0 < timeout:
        await asyncio.sleep(3)
        st = await asyncio.to_thread(cl.get, f"/nodes/{node}/tasks/{upid}/status")
        try:
            lines = await asyncio.to_thread(cl.get, f"/nodes/{node}/tasks/{upid}/log",
                                            start=seen, limit=200) or []
            for ln in lines:
                seen = max(seen, int(ln.get("n", 0)))
                if log:
                    log(str(ln.get("t", ""))[:300])
        except Exception:
            pass
        if (st or {}).get("status") == "stopped":
            if (st or {}).get("exitstatus") != "OK":
                raise EngineError(f"Proxmox-Aufgabe fehlgeschlagen: {st.get('exitstatus')}")
            return
    raise EngineError("Proxmox-Aufgabe dauert zu lange")


def _prune_str(keep: dict) -> str:
    return ",".join(f"{k.replace('_', '-')}={v}" for k, v in keep.items())


# ------------------------------------------------------------------ Ausfuehren

_client_locks: dict[str, asyncio.Lock] = {}


async def backup_client(client_id: str, *, storage_id: str = "", route: str = "backend",
                        mode: str = "files", paths: list | None = None, excludes: list | None = None,
                        image_device: str = "", compression: str = "zstd", keep: dict | None = None,
                        name: str = "", engine: str = "agent", pve_storage: str = "",
                        pve_mode: str = "snapshot", job: dict | None = None, log=None) -> dict:
    """Ein Backup fuer EINEN Client (Aufruf aus Job oder "Jetzt sichern")."""
    client = await db.call(db.get_client, client_id)
    if not client:
        raise EngineError("Client nicht gefunden")
    lock = _client_locks.setdefault(client_id, asyncio.Lock())
    if lock.locked():
        raise EngineError("Für diesen Client läuft bereits ein Backup")
    async with lock:
        keep = keep or {}
        title = name or render_name((job or {}).get("name_template", ""), client, job)

        if engine == "pve":
            tgt = pve_target(client_id)
            if not tgt:
                raise EngineError("Client ist keiner Proxmox-VM/-LXC zugeordnet")
            cl = _pve_client(tgt["host"])
            params = {"vmid": int(tgt["vmid"]), "storage": pve_storage, "mode": pve_mode,
                      "compress": PVE_COMPRESS.get(compression, "zstd"),
                      "notes-template": title.replace("\n", " ")}
            if keep:
                params["prune-backups"] = _prune_str(keep)
            if log:
                log(f"Proxmox vzdump {tgt['type']}/{tgt['vmid']} -> {pve_storage} ({pve_mode})")
            upid = await asyncio.to_thread(cl.call, "POST", f"/nodes/{tgt['node']}/vzdump", params)
            await pve_wait(tgt["host"], tgt["node"], upid, log)
            return {"ok": True, "engine": "pve"}

        st = store.storage(storage_id)
        if not st:
            raise EngineError("Speicherziel nicht gefunden")
        spec = agent_spec(client_id, st, route)
        args = {"mode": mode, "paths": paths or [], "excludes": excludes or [],
                "image_device": image_device, "compression": COMPRESSION.get(compression, "auto"),
                "keep": keep, "name": title, "job": (job or {}).get("id", ""),
                "protected": store.protected_ids(client_id, storage_id)}
        res = await agentlink.call(client_id, "backup", spec, args, timeout=24 * 3600, progress=log)
        if not res.get("ok"):
            raise EngineError(res.get("error") or "Backup fehlgeschlagen")
        if res.get("snapshots") is not None:
            store.set_snapshots(client_id, storage_id, spec["kind"], res["snapshots"], route)
        if res.get("snapshot_id"):
            store.update_snapshot_meta(client_id, storage_id, res["snapshot_id"], name=title)
        return {"ok": True, "snapshot_id": res.get("snapshot_id"), "summary": res.get("summary")}


async def run_job(job: dict, client_id: str, log=None) -> dict:
    return await backup_client(
        client_id, storage_id=job.get("storage", ""), route=job.get("route", "backend"),
        mode=job.get("mode", "files"), paths=job.get("paths") or [], excludes=job.get("excludes") or [],
        image_device=job.get("image_device", ""), compression=job.get("compression", "zstd"),
        keep=retention(job), engine=job.get("engine", "agent"),
        pve_storage=job.get("pve_storage", ""), pve_mode=job.get("pve_mode", "snapshot"),
        job=job, log=log)


def job_targets(job: dict) -> list[str]:
    """Clients eines Jobs: feste Auswahl + alle mit einem der Job-Tags."""
    ids = list(job.get("clients") or [])
    tags = {t.strip().lower() for t in (job.get("tags") or []) if t.strip()}
    if tags:
        for c in db.list_clients():
            ct = {t.strip().lower() for t in str(c.get("tags") or "").split(",") if t.strip()}
            if ct & tags and c["id"] not in ids:
                ids.append(c["id"])
    return ids
