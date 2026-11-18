# module.py (Modul "usbguard") - USB-Steuerung
import asyncio
import json


def on_startup(ctx):
    from rmm_mod_usbguard import guard
    guard.register(ctx)

    async def settings_watch():
        # Kein Hook fuer Einstellungsaenderungen vorhanden: alle 15 s vergleichen
        # und bei Aenderung die Konfiguration an alle verbundenen Agents verteilen.
        last = None
        while True:
            await asyncio.sleep(15)
            cur = json.dumps(guard._cfg(), sort_keys=True)
            if last is not None and cur != last:
                await guard.broadcast_config()
                ctx.log("Einstellungen geaendert - an alle Agents verteilt")
            last = cur

    ctx.spawn("usbguard-settings", settings_watch)
    ctx.log("USB-Steuerung bereit")
