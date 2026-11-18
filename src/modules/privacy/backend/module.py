"""Privacy - entry point: applies the retention periods at startup and daily thereafter."""
import asyncio

from app import db
from . import privacy


async def on_startup(ctx):
    await _purge_once(ctx, "startup")
    ctx.spawn("privacy-purge", lambda: _daily(ctx))


async def _purge_once(ctx, label):
    report = await db.call(privacy.purge)
    removed = sum(v for v in report.values() if isinstance(v, int))
    if removed:
        ctx.log(f"Retention periods applied ({label}): {report}")


async def _daily(ctx):
    await asyncio.sleep(120)           # wait out the startup rush
    while True:
        await _purge_once(ctx, "daily")
        await asyncio.sleep(86400)
