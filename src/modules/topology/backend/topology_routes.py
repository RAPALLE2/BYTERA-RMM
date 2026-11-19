"""
topology_routes.py (Modul "topology")
-------------------------------------
REST-Schnittstelle fuer die Netzwerk-Topologie-Karte.

  POST /api/topology/discover/start            Erkennung als BACKEND-JOB starten (?target=&via=<Node>)
                                               (laeuft schon eine fuer diese Quelle -> die laufende)
  Fortschritt/Abbruch ueber den Core-Job-Dienst: /api/bgjobs (kind "topology.discover",
  key = Quelle), live per Socket "bgjob:update".
  GET  /api/topology/map?source=               gespeicherte Karte + Bearbeitungen + Agent-Zuordnung
  PUT  /api/topology/map?source=               Bearbeitungen (Positionen, eigene Knoten ...) speichern
  GET  /api/topology/sources                   Quellen, fuer die schon Karten existieren
  GET  /api/topology/agents                    Agents, die man Knoten zuweisen kann
  GET  /api/topology/prefs                     eigene Ansicht (Hintergrund, Raster) - je Benutzer
  PUT  /api/topology/prefs

"Quelle" ist "backend" oder die Client-ID einer Node (Erkennung lief im Netz der Node).
Die Erkennung haengt NICHT an der Browser-Sitzung: Das Backend verfolgt den Job
(auch den auf einer Node), speichert das Ergebnis selbst und meldet Aenderungen
an alle Dashboards ("bgjob:update", "topology:changed").
Agent-Zuordnung: ein erkanntes Geraet gehoert zu einem Agenten, wenn MAC oder IP
zu den Live-Messwerten bzw. der bekannten IP des Clients passt.
"""

import asyncio
import re

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from app import bgjobs, db
from app.auth import can_access_client, get_current_user, require_perm
from app.sockets import state

from rmm_mod_topology import store
from rmm_mod_topology.topo_discovery import (
    cancel_job, get_job, parse_target, start_job,
)

router = APIRouter(prefix="/api/topology", tags=["topology"])


def _norm_mac(mac: str | None) -> str:
    h = re.sub(r"[^0-9a-fA-F]", "", mac or "")
    return ":".join(h[i:i + 2] for i in range(0, 12, 2)).upper() if len(h) == 12 else ""




# ------------------------------------------------------------------
# Agent-Zuordnung: erkannte Geraete <-> installierte Agenten
# ------------------------------------------------------------------
def _agent_index(user: dict) -> tuple[dict, dict]:
    """(MAC -> Client, IP -> Client) fuer alle Clients, die der Benutzer sehen darf."""
    by_mac: dict[str, dict] = {}
    by_ip: dict[str, dict] = {}
    for c in db.list_clients():
        if not can_access_client(user, c["id"]):
            continue
        info = {"id": c["id"], "hostname": c.get("hostname"),
                "color": c.get("color"), "online": state.is_online(c["id"]),
                "platform": c.get("platform"), **_icon_fields(c)}
        if c.get("ip"):
            by_ip.setdefault(str(c["ip"]), info)
        m = state.live_metrics.get(c["id"]) or {}
        mm = _norm_mac(m.get("mac"))
        if mm:
            by_mac.setdefault(mm, info)
        for i in m.get("interfaces") or []:
            if isinstance(i, dict):
                im = _norm_mac(i.get("mac"))
                if im:
                    by_mac.setdefault(im, info)
                if i.get("ipv4"):
                    by_ip.setdefault(str(i["ipv4"]), info)
    return by_mac, by_ip


def _check_source(user: dict, source: str) -> None:
    """A node's map is only visible to whoever may access the node.
    "backend" and user-created maps ("map:<id>") are open to anyone with
    the scan permission."""
    if source == "backend" or store.is_custom_map(source):
        return
    if not can_access_client(user, source):
        raise HTTPException(404, "Quelle nicht gefunden")


def _attach_agents(result: dict, user: dict) -> dict:
    by_mac, by_ip = _agent_index(user)
    out = dict(result)
    nodes = []
    for n in result.get("nodes") or []:
        n = dict(n)
        agent = None
        mm = _norm_mac(n.get("mac"))
        if mm and mm in by_mac:
            agent = by_mac[mm]
        elif n.get("ip") and n["ip"] in by_ip:
            agent = by_ip[n["ip"]]
        if agent:
            n["agent"] = agent
        nodes.append(n)
    out["nodes"] = nodes
    return out


# ------------------------------------------------------------------
# Erkennung (Discovery) als Backend-Job
# ------------------------------------------------------------------
JOB_KIND = "topology.discover"
_PHASES = {"queued": "wartet", "ping": "Geräte suchen (Ping-Sweep)",
           "trace": "Weg ins Internet verfolgen", "fingerprint": "Geräte untersuchen (Ports)"}


async def notify_changed(source: str, origin: str = "", what: str = "map") -> None:
    """Alle Dashboards: Karte/Bearbeitungen dieser Quelle haben sich geaendert."""
    try:
        from app.sockets import sio
        await sio.emit("topology:changed", {"source": source, "origin": origin, "what": what},
                       namespace="/dashboard")
    except Exception:
        pass


def _mirror(job: bgjobs.BgJob, j: dict) -> None:
    """Stand der Erkennung (topo_discovery-Job) in den Backend-Job uebernehmen."""
    phase = j.get("phase") or ""
    pct, detail = 0.0, _PHASES.get(phase, phase)
    if phase == "ping" and j.get("total_hosts"):
        pct = j["done_hosts"] / j["total_hosts"] * 80
        detail += f" · {j['done_hosts']}/{j['total_hosts']} Adressen · {j.get('found', 0)} gefunden"
    elif phase == "trace":
        pct = 84
    elif phase == "fingerprint":
        pct = 86 + (j["fp_done"] / j["fp_total"] * 14 if j.get("fp_total") else 0)
        detail += f" · {j.get('fp_done', 0)}/{j.get('fp_total', 0)}"
    job.update(phase=phase, pct=pct, detail=detail, label=j.get("label") or "",
               found=j.get("found", 0))


def _make_runner(source: str, target: str | None, via: str):
    async def runner(job: bgjobs.BgJob):
        from app import node_manager
        if via:
            node_manager.require_module(via, "topology")
            r = await node_manager.command(via, "topo-start", {"target": target}, timeout=60)
            jid = r["job_id"]

            async def status():
                return await node_manager.command(via, "topo-status", {"job_id": jid}, timeout=60)

            async def cancel():
                await node_manager.command(via, "topo-cancel", {"job_id": jid}, timeout=30)
        else:
            jid = start_job(target)

            async def status():
                j = get_job(jid)
                if not j:
                    raise RuntimeError("Erkennung verschwunden")
                return {k: v for k, v in j.items() if k != "cancel"}

            async def cancel():
                cancel_job(jid)

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
            except Exception as e:            # Node kurz weg -> nicht sofort aufgeben
                fails += 1
                if fails >= 15:
                    raise RuntimeError(f"Node antwortet nicht mehr: {e}")
                await asyncio.sleep(2)
                continue
            _mirror(job, j)
            if j.get("status") in ("done", "error", "cancelled"):
                break
            await asyncio.sleep(0.8)

        if j["status"] == "error":
            raise RuntimeError(j.get("error") or "Erkennung fehlgeschlagen")
        result = j.get("result")
        if j["status"] == "done" and result:
            await db.call(store.set_map, source, result)   # Ergebnis speichert das BACKEND
            await notify_changed(source, what="map")
        devices = sum(1 for n in (result or {}).get("nodes", []) if str(n.get("id", "")).startswith("dev:"))
        return {"devices": devices, "label": (result or {}).get("label", ""), "source": source}
    return runner


@router.post("/discover/start")
async def discover_start(target: str | None = None, via: str = "",
                         map_id: str = Query("", alias="map"),
                         user: dict = Depends(get_current_user)):
    require_perm(user, "network_scan")
    try:
        parse_target(target)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # "map" chooses WHERE the result is stored (a user-created canvas);
    # "via" chooses WHERE the scan runs (backend or a node). Without a map
    # the two are the same, as before.
    if map_id:
        if not store.is_custom_map(map_id):
            raise HTTPException(400, "Ungültige Karte")
        source = map_id
    else:
        source = via or "backend"
    if via:
        from app import node_manager
        if not can_access_client(user, via):
            raise HTTPException(404, "Node nicht gefunden")
        try:
            node_manager.require_module(via, "topology")
        except node_manager.NodeError as e:
            raise HTTPException(409, str(e))
    label = (target or "").strip() or "eigenes Netz"
    job, new = bgjobs.start(
        JOB_KIND, source, title=f"Topologie-Erkennung ({label})", user=user,
        perm="network_scan",
        may_see=(lambda u: can_access_client(u, via)) if via else None,
        runner=_make_runner(source, target, via),
        meta={"target": target or "", "via": via})
    if new:
        db.add_audit_entry(user["username"], "topology.discover", target=f"{source}:{label}")
    return {**job.public(), "attached": not new}


# ------------------------------------------------------------------
# Karte + Bearbeitungen
# ------------------------------------------------------------------
def _icon_fields(c: dict) -> dict:
    """What the frontend needs to pick the agent's icon (js/app/deviceicons.js clientIconType)."""
    return {"icon_type": c.get("icon_type") or "", "detected_kind": c.get("detected_kind") or "",
            "device_type": c.get("device_type") or "", "release": c.get("release") or ""}


def _agent_info(user: dict, cid: str) -> dict | None:
    if not cid or not can_access_client(user, cid):
        return None
    c = db.get_client(cid) if hasattr(db, "get_client") else next((x for x in db.list_clients() if x["id"] == cid), None)
    if not c:
        return None
    return {"id": c["id"], "hostname": c.get("hostname"), "color": c.get("color"),
            "online": state.is_online(c["id"]), "platform": c.get("platform"), "ip": c.get("ip"), **_icon_fields(c)}


@router.get("/map")
def get_map(source: str = "backend", user: dict = Depends(get_current_user)):
    require_perm(user, "network_scan")
    _check_source(user, source)
    result = store.get_map(source)
    ovr = store.get_overrides(source)
    # agents assigned by hand (own nodes and discovered ones)
    ids = {n.get("agent_id") for n in ovr["custom_nodes"]} | {m.get("agent_id") for m in ovr["meta"].values()}
    agents = {cid: a for cid in ids if cid for a in [_agent_info(user, cid)] if a}
    return {
        "source": source,
        "map": _attach_agents(result, user) if result else None,
        "overrides": ovr,
        "agents": agents,
    }


@router.get("/agents")
def list_agents(user: dict = Depends(get_current_user)):
    require_perm(user, "network_scan")
    out = []
    for c in db.list_clients():
        if can_access_client(user, c["id"]):
            out.append({"id": c["id"], "hostname": c.get("hostname") or c["id"][:8], "ip": c.get("ip") or "",
                        "platform": c.get("platform") or "", "online": state.is_online(c["id"]), **_icon_fields(c)})
    out.sort(key=lambda a: a["hostname"].lower())
    return {"agents": out}


@router.get("/prefs")
def get_prefs(user: dict = Depends(get_current_user)):
    require_perm(user, "network_scan")
    return store.get_prefs(user["id"])


@router.put("/prefs")
def put_prefs(prefs: dict = Body(...), user: dict = Depends(get_current_user)):
    require_perm(user, "network_scan")
    return store.set_prefs(user["id"], prefs if isinstance(prefs, dict) else {})


@router.put("/map")
async def put_map(source: str = "backend", origin: str = "", overrides: dict = Body(...),
                  user: dict = Depends(get_current_user)):
    require_perm(user, "network_scan")
    _check_source(user, source)
    if not isinstance(overrides, dict):
        raise HTTPException(400, "Bearbeitungen fehlen")
    saved = await db.call(store.set_overrides, source, overrides)
    db.add_audit_entry(user["username"], "topology.edit", target=source)
    await notify_changed(source, origin=origin[:40], what="overrides")   # andere Fenster/Benutzer live
    return {"ok": True, "overrides": saved}


@router.get("/sources")
def get_sources(user: dict = Depends(get_current_user)):
    require_perm(user, "network_scan")
    sources = [x for x in store.list_sources()
               if x == "backend"
               or (not store.is_custom_map(x) and can_access_client(user, x))]
    return {"sources": sources, "maps": store.list_custom_maps()}


# ------------------------------------------------------------------
# User-created maps (independent canvases, e.g. one per subnet)
# ------------------------------------------------------------------
@router.post("/maps")
async def create_map(body: dict = Body(default={}), user: dict = Depends(get_current_user)):
    require_perm(user, "network_scan")
    label = (body or {}).get("label") if isinstance(body, dict) else ""
    r = await db.call(store.create_map, label or "")
    db.add_audit_entry(user["username"], "topology.map.create", target=r["source"])
    await notify_changed(r["source"], what="maps")
    return r


@router.patch("/maps")
async def rename_map(body: dict = Body(...), user: dict = Depends(get_current_user)):
    require_perm(user, "network_scan")
    source = (body or {}).get("source") or ""
    if not store.is_custom_map(source):
        raise HTTPException(400, "Nur eigene Karten lassen sich umbenennen")
    r = await db.call(store.rename_map, source, (body or {}).get("label") or "")
    if not r.get("ok"):
        raise HTTPException(400, "Umbenennen fehlgeschlagen")
    db.add_audit_entry(user["username"], "topology.map.rename", target=source)
    await notify_changed(source, what="maps")
    return r


@router.delete("/maps")
async def delete_map(source: str = "", user: dict = Depends(get_current_user)):
    require_perm(user, "network_scan")
    if not store.is_custom_map(source):
        raise HTTPException(400, "Nur eigene Karten lassen sich löschen")
    r = await db.call(store.delete_source, source)
    db.add_audit_entry(user["username"], "topology.map.delete", target=source)
    await notify_changed(source, what="maps")
    return r


@router.post("/map/delete-node")
async def delete_node(source: str = "backend", origin: str = "",
                      body: dict = Body(...), user: dict = Depends(get_current_user)):
    """Really delete discovered devices from the saved map (no soft-hide).
    A later scan or a manual add brings them back."""
    require_perm(user, "network_scan")
    _check_source(user, source)
    ids = (body or {}).get("ids") if isinstance(body, dict) else None
    if not isinstance(ids, list):
        raise HTTPException(400, "ids fehlen")
    r = await db.call(store.delete_nodes, source, ids)
    db.add_audit_entry(user["username"], "topology.delete-node", target=source)
    await notify_changed(source, origin=origin[:40], what="map")
    return {"ok": True, **r}
