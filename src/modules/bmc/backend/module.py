"""iLO / iDRAC (Redfish) - entry point of the "bmc" module."""
from app import hostapps

from . import webproxy


async def on_startup(ctx):
    # Proxy the native web UI / remote console via the host dispatcher (the
    # "proxy_host" setting is read on every request - changes need no restart).
    hostapps.register("bmc-proxy", webproxy.match_host, webproxy.app)


async def on_shutdown(ctx):
    hostapps.unregister("bmc-proxy")
