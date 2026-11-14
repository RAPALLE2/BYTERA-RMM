"""
topology_routes.py (Modul "topology")
-------------------------------------
REST-Schnittstelle fuer die Netzwerk-Topologie-Karte.

  POST /api/topology/discover/start            Erkennung starten (?target=&via=<Node>)
  GET  /api/topology/discover/job/{id}         Fortschritt (+ Ergebnis bei Abschluss)
  POST /api/topology/discover/job/{id}/cancel  Erkennung abbrechen
  GET  /api/topology/map?source=               gespeicherte Karte + Bearbeitungen + Agent-Zuordnung
  PUT  /api/topology/map?source=               Bearbeitungen (Positionen, eigene Knoten ...) speichern
  GET  /api/topology/sources                   Quellen, fuer die schon Karten existieren

"Quelle" ist "backend" oder die Client-ID einer Node (Erkennung lief im Netz der Node).
Job-IDs von Node-Erkennungen: "n.<node>.<job>" (wie beim Netzwerk-Scan).
Agent-Zuordnung: ein erkanntes Geraet gehoert zu einem Agenten, wenn MAC oder IP
zu den Live-Messwerten bzw. der bekannten IP des Clients passt.
"""

import re

from fastapi import APIRouter, Body, Depends, HTTPException

from app import db
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


async def _node(user: dict, via: str, op: str, data: dict, timeout: float = 120):
    """Befehl an das Node-Modul "topology" der gewaehlten Node."""
    from app import node_manager
    if not can_access_client(user, via):
        raise HTTPException(404, "Node nicht gefunden")
    try:
        node_manager.require_module(via, "topology")
        return await node_manager.command(via, op, data, timeout=timeout)
    except node_manager.NodeError as e:
        raise HTTPException(409, str(e))


def _split_job(job_id: str) -> tuple[str, str]:
    if job_id.startswith("n.") and job_id.count(".") >= 2:
        _, node, jid = job_id.split(".", 2)
        return node, jid
    return "", job_id


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
                "platform": c.get("platform")}
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
# Erkennung (Discovery)
# ------------------------------------------------------------------
@router.post("/discover/start")
async def discover_start(target: str | None = None, via: str = "",
                         user: dict = Depends(get_current_user)):
    require_perm(user, "network_scan")
    try:
        parse_target(target)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if via:
        r = await _node(user, via, "topo-start", {"target": target})
        return {"job_id": f"n.{via}.{r['job_id']}", "via": via}
    return {"job_id": start_job(target)}


@router.get("/discover/job/{job_id}")
async def discover_job(job_id: str, user: dict = Depends(get_current_user)):
    require_perm(user, "network_scan")
    node, jid = _split_job(job_id)
    if node:
        job = {**(await _node(user, node, "topo-status", {"job_id": jid})),
               "id": job_id, "via": node}
        source = node
    else:
        job = get_job(jid)
        if not job:
            raise HTTPException(404, "Erkennung nicht gefunden (evtl. abgelaufen)")
        job = {k: v for k, v in job.items() if k != "cancel"}
        source = "backend"
    # Fertige Erkennung: Ergebnis als Karte der Quelle speichern + Agenten zuordnen
    if job.get("status") == "done" and job.get("result"):
        store.set_map(source, job["result"])
        job["result"] = _attach_agents(job["result"], user)
        job["source"] = source
    return job


@router.post("/discover/job/{job_id}/cancel")
async def discover_cancel(job_id: str, user: dict = Depends(get_current_user)):
    require_perm(user, "network_scan")
    node, jid = _split_job(job_id)
    if node:
        return await _node(user, node, "topo-cancel", {"job_id": jid})
    if not cancel_job(jid):
        raise HTTPException(404, "Keine laufende Erkennung mit dieser ID")
    return {"ok": True}


# ------------------------------------------------------------------
# Karte + Bearbeitungen
# ------------------------------------------------------------------
@router.get("/map")
def get_map(source: str = "backend", user: dict = Depends(get_current_user)):
    require_perm(user, "network_scan")
    result = store.get_map(source)
    return {
        "source": source,
        "map": _attach_agents(result, user) if result else None,
        "overrides": store.get_overrides(source),
    }


@router.put("/map")
def put_map(source: str = "backend", overrides: dict = Body(...),
            user: dict = Depends(get_current_user)):
    require_perm(user, "network_scan")
    if not isinstance(overrides, dict):
        raise HTTPException(400, "Bearbeitungen fehlen")
    saved = store.set_overrides(source, overrides)
    db.add_audit_entry(user["username"], "topology.edit", target=source)
    return {"ok": True, "overrides": saved}


@router.get("/sources")
def get_sources(user: dict = Depends(get_current_user)):
    require_perm(user, "network_scan")
    return {"sources": store.list_sources()}
