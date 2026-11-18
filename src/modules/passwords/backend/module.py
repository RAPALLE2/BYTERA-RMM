"""Password manager - entry point of the "passwords" module."""
from . import store


async def on_startup(ctx):
    store.ensure_tables()
