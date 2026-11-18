# module.py (module "cloudflare") - Cloudflare Tunnel
# --------------------------------------------------
# startup: download cloudflared (fixed version); if a tunnel already exists, start the connector.
# shutdown/disable: stop the connector. (The tunnels/routes in Cloudflare stay -
# "Remove everything" in the module app clears them.)

import asyncio

from . import connector, remote, store


async def _remote_loop():
    """Close client-app remote accesses again after expiry / end of use."""
    while True:
        await asyncio.sleep(30)
        try:
            await asyncio.to_thread(remote.cleanup)
        except Exception as e:      # noqa: BLE001
            print(f"[cloudflare] cleanup: {e}")


def on_startup(ctx):
    ctx.spawn("remote-cleanup", _remote_loop)
    connector.ensure_binary_async()
    t = store.load()["tunnels"].get("server") or {}
    if t.get("token_enc") and store.load()["routes"]:
        connector.start(store.dec(t["token_enc"]))
        ctx.log("Connecting Cloudflare tunnel")


def on_shutdown(ctx):
    connector.stop()


def on_disable(ctx):
    connector.stop()
