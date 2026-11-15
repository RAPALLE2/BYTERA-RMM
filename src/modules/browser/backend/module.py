# module.py (Modul "browser") - Lebenszyklus des Server-Browsers.
# Beim Start/Einschalten wird geprueft, ob Chromium startet; fehlt es (oder nach
# einem Container-Neubau die Systembibliotheken), wird es automatisch als
# Backend-Job installiert (live im Dashboard sichtbar).
import asyncio

from . import install
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
    asyncio.get_running_loop().create_task(_ensure_chromium())


async def on_enable(ctx):
    await on_startup(ctx)


def on_disable(ctx):
    try:
        asyncio.get_event_loop().create_task(_bs.MANAGER.shutdown())
    except Exception:
        pass


def on_shutdown(ctx):
    on_disable(ctx)
