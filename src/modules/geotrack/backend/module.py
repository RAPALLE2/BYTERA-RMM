"""Location tracker - entry point of the "geotrack" module."""
import asyncio
import time

from app import db

from . import geo, store

_ctx = None


def _geo_result(sid, payload):
    """Agent reply to "geo-locate" -> resolve the waiting future."""
    from app.sockets import state
    fut = state.pending_requests.get((payload or {}).get("requestId"))
    if fut and not fut.done():
        fut.set_result(payload)


async def _locate_client(client_id: str, client=None) -> dict:
    """Locate one client and store the result. Raises on failure."""
    client = client or db.get_client(client_id) or {}
    try:
        data = await geo.request_agent(client_id)
        loc = await geo.resolve(client_id, data, public_ip_hint=client.get("public_ip") or client.get("ip") or "")
    except Exception as exc:
        store.mark(client_id, str(exc))
        raise
    saved = store.add_location(client_id, loc["lat"], loc["lon"], loc.get("accuracy", 0), loc["source"],
                               loc.get("city", ""), loc.get("country", ""))
    store.mark(client_id, "")
    return saved


async def _loop(ctx):
    """Background scheduler: re-locate tracked, online clients at the configured interval."""
    from app.sockets import state
    while True:
        try:
            interval = int(ctx.setting("interval_min", 30) or 30)
            retention = int(ctx.setting("retention_days", 90) or 90)
            store.prune(retention)
            online = set(getattr(state, "client_id_to_sid", {}).keys())
            for row in store.tracked_clients():
                cid = row["client_id"]
                if cid not in online:
                    continue
                last = store.last_location(cid)
                if last and (time.time() * 1000 - last["ts"]) < interval * 60 * 1000 * 0.9:
                    continue
                try:
                    await _locate_client(cid)
                except Exception:
                    pass
                await asyncio.sleep(2)            # don't hit all agents at once
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(60)


async def on_startup(ctx):
    global _ctx
    _ctx = ctx
    store.ensure_tables()
    ctx.sio_on("geo-result", _geo_result, namespace="/agent")
    ctx.spawn("geo-loop", lambda: _loop(ctx))


def locate_now(client_id, client=None):
    return _locate_client(client_id, client)
