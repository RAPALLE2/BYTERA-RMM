"""Network tools - entry point."""
from . import portforward


async def on_startup(ctx):
    """Restore open port forwards and start the expiry loop."""
    if ctx.feature("portforward"):
        ctx.spawn("forward-restore", portforward.restore)
        ctx.spawn("forward-expiry", portforward.expiry_loop)
