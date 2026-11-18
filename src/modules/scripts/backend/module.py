"""Scripts & automation - entry point."""
from . import automation_engine


async def on_startup(ctx):
    """The automation engine checks every minute which scheduled commands are due."""
    if ctx.feature("automation"):
        ctx.spawn("automation", automation_engine.run)
