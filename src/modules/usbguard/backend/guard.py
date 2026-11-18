"""
guard.py (Modul "usbguard") - Vermittlung zwischen Agent und Dashboard.

Agent -> Backend:  "usb-config-get"  (Konfiguration abholen, beim Verbinden)
                   "usb-event"       {action: add|remove, dev, name, blocked}
Backend -> Agent:  "usb-config"      {enabled, block_new}
                   "usb-decide"      {dev, allow}
Backend -> Dash:   "usb:event"       (Meldung) / "usb:ask" (Erlauben/Blockieren)

Timeout: Entscheidet nach timeout_s automatisch (timeout_action allow|deny)
und informiert die Dashboards, dass die Frage erledigt ist.
"""

from __future__ import annotations

import asyncio
import time

_pending: dict[str, dict] = {}
_broadcast_sio = None


async def broadcast_config() -> int:
    """Aktuelle Konfiguration (inkl. Vertrauensliste) an alle Agents schicken."""
    if _broadcast_sio is None:
        return 0
    await _broadcast_sio.emit("usb-config", _cfg(), namespace="/agent")
    return 1     # f"{client_id}|{dev}" -> {"task": ..., "ts": ...}


def _cfg():
    from app import modules
    from rmm_mod_usbguard import trust
    return {**trust.agent_payload(),
        "enabled": bool(modules.setting("usbguard", "enabled", False)),
        "block_new": bool(modules.setting("usbguard", "block_new", False)),
        "timeout_s": max(5, min(int(modules.setting("usbguard", "timeout_s", 60) or 60), 3600)),
        "timeout_action": "allow" if str(modules.setting("usbguard", "timeout_action", "deny")).strip().lower() == "allow" else "deny",
    }


async def decide(client_id: str, dev: str, allow: bool, by: str) -> bool:
    """Entscheidung (Dashboard oder Timeout) an den Agenten geben."""
    from app import db
    from app.sockets import send_to_agent, sio
    key = f"{client_id}|{dev}"
    p = _pending.pop(key, None)
    if p:
        task = p.get("task")
        if task and not task.done():
            task.cancel()
    ok = await send_to_agent(client_id, "usb-decide", {"dev": dev, "allow": bool(allow)})
    db.add_audit_entry(by, "usbguard.decide", client_id,
                       f"{dev}: {'erlaubt' if allow else 'blockiert'}")
    await sio.emit("usb:decided", {"client_id": client_id, "dev": dev,
                                   "allow": bool(allow), "by": by}, namespace="/dashboard")
    return ok


def register(ctx) -> None:
    from app.sockets import sio, state

    async def on_config_get(sid, data=None):
        await sio.emit("usb-config", _cfg(), to=sid, namespace="/agent")

    # Konfiguration nach Aenderung (Einstellungen, Vertrauensliste) an alle
    # verbundenen Agents neu verteilen - siehe broadcast_config().
    global _broadcast_sio
    _broadcast_sio = sio

    async def on_event(sid, data):
        data = data or {}
        client_id = state.sid_to_client_id.get(sid)
        if not client_id:
            return
        cfg = _cfg()
        if not cfg["enabled"]:
            return
        from app import db
        client = await db.call(db.get_client, client_id)
        host = (client or {}).get("short_name") or (client or {}).get("hostname") or client_id
        trusted = str(data.get("trusted") or "")
        ev = {
            "client_id": client_id, "host": host, "trusted": trusted,
            "vid": str(data.get("vid") or ""), "pid": str(data.get("pid") or ""),
            "serial": str(data.get("serial") or "")[:128], "storage": bool(data.get("storage")),
            "action": "add" if data.get("action") == "add" else "remove",
            "dev": str(data.get("dev") or "?")[:200],
            "name": str(data.get("name") or "USB-Gerät")[:200],
            "blocked": bool(data.get("blocked")),
            "ts": int(time.time() * 1000),
            "timeout_s": cfg["timeout_s"], "timeout_action": cfg["timeout_action"],
        }
        why = {"id": " (vertrauenswürdige Geräte-ID)", "maintenance": " (Wartungsmodus)"}.get(
            trusted, f" (Schlüsseldatei: {trusted[4:]})" if trusted.startswith("key:") else "")
        db.add_audit_entry("system", "usbguard.event", client_id,
                           f"{ev['name']} {'eingesteckt' if ev['action'] == 'add' else 'ausgesteckt'}"
                           + (" (blockiert, wartet auf Freigabe)" if ev["blocked"] else why))
        if db.in_maintenance(client_id) and not ev["blocked"]:
            return                         # Wartung: protokollieren, aber kein Alarm
        if ev["blocked"]:
            await sio.emit("usb:ask", ev, namespace="/dashboard")
            key = f"{client_id}|{ev['dev']}"

            async def _timeout():
                try:
                    await asyncio.sleep(cfg["timeout_s"])
                except asyncio.CancelledError:
                    return
                if key in _pending:
                    await decide(client_id, ev["dev"],
                                 cfg["timeout_action"] == "allow",
                                 f"Timeout ({cfg['timeout_action']})")

            _pending[key] = {"ts": time.time(),
                             "task": asyncio.get_running_loop().create_task(_timeout())}
        else:
            await sio.emit("usb:event", ev, namespace="/dashboard")

    sio.on("usb-config-get", on_config_get, namespace="/agent")
    sio.on("usb-event", on_event, namespace="/agent")
