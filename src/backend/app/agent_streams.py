"""
agent_streams.py (Core)
=======================
TCP connections "through" an agent - as a building block for modules.

On request the agent can open a TCP connection into ITS network and pass the
bytes through over the existing agent connection (events vpn-open / vpn-data /
vpn-close, see agent/main.py "VPN PEER"). Nothing is installed on the agent for
this: no WireGuard, no driver, no admin rights. Host names are resolved by the
AGENT - so internal names (nas.local, intranet) work.

Usage (e.g. server browser over an agent's network):

    s = await agent_streams.open_stream(client_id, "192.168.1.10", 443)
    await s.write(b"...")
    data = await s.read()          # b"" = connection closed
    await s.close()

Port forwarding (module "network") uses the same events with its own
management; sockets.py passes each event to both - each side knows only its own
stream IDs.
"""
from __future__ import annotations

import asyncio
import base64
import uuid

OPEN_TIMEOUT = 15.0
CHUNK = 32768

_streams: dict[str, "AgentStream"] = {}


class AgentStreamError(Exception):
    pass


class AgentStream:
    def __init__(self, client_id: str, host: str, port: int, tag: str):
        self.id = f"as-{uuid.uuid4().hex[:12]}"
        self.client_id = client_id
        self.host = host
        self.port = int(port)
        self.tunnel = f"as:{tag}"
        self._opened: asyncio.Future = asyncio.get_running_loop().create_future()
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.closed = False
        self.bytes_in = 0
        self.bytes_out = 0

    async def write(self, data: bytes) -> None:
        if self.closed or not data:
            return
        from app.sockets import send_to_agent
        self.bytes_out += len(data)
        for i in range(0, len(data), CHUNK):
            ok = await send_to_agent(self.client_id, "vpn-data", {
                "tunnel": self.tunnel, "stream": self.id,
                "data": base64.b64encode(data[i:i + CHUNK]).decode()})
            if not ok:
                self._mark_closed()
                raise AgentStreamError("agent is offline")

    async def read(self) -> bytes:
        """Next chunk of data; b'' = connection ended."""
        if self.closed and self._queue.empty():
            return b""
        return await self._queue.get()

    async def close(self) -> None:
        if self.closed:
            return
        self._mark_closed()
        try:
            from app.sockets import send_to_agent
            await send_to_agent(self.client_id, "vpn-close", {"tunnel": self.tunnel, "stream": self.id})
        except Exception:
            pass

    def _mark_closed(self) -> None:
        if self.closed:
            return
        self.closed = True
        _streams.pop(self.id, None)
        if not self._opened.done():
            self._opened.set_result((False, "closed"))
        self._queue.put_nowait(b"")


async def open_stream(client_id: str, host: str, port: int, tag: str = "", timeout: float = OPEN_TIMEOUT) -> AgentStream:
    """Open a connection to host:port from the agent's network. Raises AgentStreamError."""
    from app.sockets import send_to_agent
    s = AgentStream(client_id, host, port, tag or "x")
    _streams[s.id] = s
    ok = await send_to_agent(client_id, "vpn-open", {
        "tunnel": s.tunnel, "stream": s.id, "host": host, "port": int(port), "proto": "tcp"})
    if not ok:
        _streams.pop(s.id, None)
        raise AgentStreamError("agent is offline")
    try:
        good, err = await asyncio.wait_for(s._opened, timeout=timeout)
    except asyncio.TimeoutError:
        await s.close()
        raise AgentStreamError(f"agent did not connect {host}:{port} in time")
    if not good:
        s._mark_closed()
        raise AgentStreamError(err or f"{host}:{port} unreachable")
    return s


# ------------------------------------------------------------------ events from the agent
def on_open_result(payload: dict) -> None:
    s = _streams.get(payload.get("stream", ""))
    if s and not s._opened.done():
        s._opened.set_result((bool(payload.get("ok")), str(payload.get("error") or "")))


def on_data(payload: dict) -> None:
    s = _streams.get(payload.get("stream", ""))
    if not s or s.closed:
        return
    try:
        raw = base64.b64decode(payload.get("data") or "")
    except Exception:
        return
    if raw:
        s.bytes_in += len(raw)
        s._queue.put_nowait(raw)


def on_close(payload: dict) -> None:
    s = _streams.get(payload.get("stream", ""))
    if s:
        s._mark_closed()


def close_all_for(client_id: str) -> None:
    """Agent disconnected: end all of its streams."""
    for s in [x for x in _streams.values() if x.client_id == client_id]:
        s._mark_closed()


def stats() -> dict:
    return {"open": len(_streams)}
