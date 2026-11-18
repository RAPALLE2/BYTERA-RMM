# module.py (module "webserver")
# -----------------------------
# Mounts the public addresses of all websites (rmm.url.de/<name>) and unmounts
# them again when disabled. The hosting core (webhost.py) is also used by the
# "deployment" module (requires: [webserver]).

from . import sites


def on_startup(ctx):
    sites.ROOT.mkdir(parents=True, exist_ok=True)
    sites.sync_routes()
    n = len(sites.all_sites())
    if n:
        ctx.log(f"{n} website(s) mounted")


def on_enable(ctx):
    sites.sync_routes()


def on_disable(ctx):
    sites.unmount_routes()


def on_shutdown(ctx):
    sites.unmount_routes()
