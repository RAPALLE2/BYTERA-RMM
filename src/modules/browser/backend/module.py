# module.py (Modul "browser") - Lebenszyklus des Server-Browsers.
from . import session as _bs


def on_disable(ctx):
    # Chromium beim Abschalten des Moduls sauber beenden.
    try:
        import asyncio
        asyncio.get_event_loop().create_task(_bs.MANAGER.shutdown())
    except Exception:
        pass


def on_shutdown(ctx):
    on_disable(ctx)
