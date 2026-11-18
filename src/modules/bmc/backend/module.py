"""iLO / iDRAC (Redfish) - Einstiegspunkt des Moduls "bmc"."""
from app import hostapps

from . import webproxy


async def on_startup(ctx):
    # Native Web-UI / Remote-Konsole per Host-Dispatcher durchreichen (Einstellung
    # "proxy_host" wird bei jeder Anfrage gelesen - Aenderung ohne Neustart).
    hostapps.register("bmc-proxy", webproxy.match_host, webproxy.app)


async def on_shutdown(ctx):
    hostapps.unregister("bmc-proxy")
