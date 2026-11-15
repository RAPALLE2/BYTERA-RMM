# netsel.py (Modul "browser") - welche Netzwerke kann der Browser benutzen?
# ------------------------------------------------------------------------
#   "direct"        Verbindungen gehen direkt vom RMM-Server aus. Ist das Backend
#                   Teilnehmer oder Host eines WireGuard-Tunnels (Modul "vpn"),
#                   sind dessen Netze damit ebenfalls erreichbar.
#   "agent:<id>"    Agent-Zugang aus dem VPN-Modul ("Backend <-> Agent"): interne
#                   Ziele laufen ueber den Agenten in dessen Netz (Split-Tunnel)
#                   oder - Modus "all" - der gesamte Verkehr.
from __future__ import annotations

from app import db, modules

COLORS = ["#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899", "#14b8a6", "#f97316"]


def _links():
    return modules.use("vpn", "agentlinks")


def list_for(user: dict) -> list[dict]:
    out = [{"id": "direct", "name": "Server (direct)", "kind": "direct", "color": "",
            "detail": "Pages are loaded from the RMM server"}]
    al = _links()
    if al:
        from app.sockets import state
        for i, l in enumerate(sorted(al.for_user(user), key=lambda x: x["name"].lower())):
            c = db.get_client(l["client_id"]) or {}
            nets = ", ".join(l.get("networks") or []) or "–"
            out.append({"id": f"agent:{l['id']}", "name": l["name"], "kind": "agent",
                        "client": c.get("hostname") or l["client_id"][:8],
                        "online": l["client_id"] in state.client_id_to_sid,
                        "mode": l.get("mode"), "networks": l.get("networks") or [],
                        "color": COLORS[i % len(COLORS)],
                        "detail": (f"All traffic via {c.get('hostname') or 'agent'}" if l.get("mode") == "all"
                                   else f"Internal via {c.get('hostname') or 'agent'}: {nets}")})
    return out


def make_router(user: dict, network: str):
    """Router-Funktion fuer socks.SocksServer. Prueft die Berechtigung bei JEDER
    Verbindung neu (Zugang geloescht/abgelaufen -> sofort gesperrt)."""
    if not network or network == "direct":
        async def direct(host, port):
            return "direct", None
        return direct
    lid = network.split(":", 1)[1] if ":" in network else network

    async def via(host, port):
        al = _links()
        if not al:
            return "block", "VPN module is disabled"
        link = al.get(lid)
        if not link or not al.may_use(user, link):
            return "block", "agent link expired or no permission"
        if al.via_agent(link, host):
            return "agent", link["client_id"]
        return "direct", None
    return via


def valid(user: dict, network: str) -> bool:
    return any(n["id"] == network for n in list_for(user))
