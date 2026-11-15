# module.py (module "browser") - life cycle of the server browser.
#  * pass-through mode (proxy.py): the proxy hostnames are hooked into the core
#    host dispatcher (app/hostapps.py) - the setting is read on every request,
#    so changing "proxy_host" needs no restart.
#  * stream mode: on start/enable it is checked whether Chromium starts; if not
#    (or after a container rebuild the system libraries are missing) it is
#    installed automatically as a backend job (live in the dashboard).
import asyncio

from app import hostapps

from . import install, proxy
from . import session as _bs


async def _ensure_chromium():
    ok, why = await install.probe()
    if ok:
        return
    print(f"[browser] Chromium does not start ({why}) - starting the installation")
    try:
        from .browser_routes import _auto_try
        import time
        _auto_try["at"] = time.time()
        install.start_install_job(None)
    except Exception as e:
        print(f"[browser] the installation could not be started: {e}")


async def on_startup(ctx):
    hostapps.register("browser-proxy", proxy.match_host, proxy.app)
    asyncio.get_running_loop().create_task(_ensure_chromium())


async def on_enable(ctx):
    await on_startup(ctx)


def on_disable(ctx):
    hostapps.unregister("browser-proxy")
    try:
        asyncio.get_event_loop().create_task(_bs.MANAGER.shutdown())
    except Exception:
        pass


def on_shutdown(ctx):
    on_disable(ctx)
