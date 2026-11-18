# module.py (module "usbguard") - USB control
import asyncio
import json


def on_startup(ctx):
    from rmm_mod_usbguard import guard
    guard.register(ctx)

    async def settings_watch():
        # No hook for settings changes available: compare every 15 s
        # and on a change distribute the configuration to all connected agents.
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
