"""
portforward.py
==============
Port forwarding: make a service on a client reachable, without VPN, without a
driver, without open ports at the customer.

Why this is the better way
-----------------------------
The actual wish is almost always: "I want to reach the VNC server on grandpa's
machine." Building a whole VPN for that is a big detour - and it fails on
prerequisites that a NAS does not always have (kernel module, TUN device,
elevated permissions).

Instead, something much simpler happens here: the backend opens a listening
port, and everything that arrives there is passed to the target machine over
the ALREADY EXISTING agent connection.

    VNC viewer  ->  NAS:55900  ->  Socket.IO  ->  agent  ->  127.0.0.1:5900

Prerequisites: none. No WireGuard, no driver, no open port at the customer -
the agent built the connection on its own anyway. Nothing changes on the
target device.

It uses exactly the events the agent already knows for the VPN
(vpn-open / vpn-data / vpn-close). The agent does NOT need to be touched.

Limits, honestly named
------------------------
  * TCP, not UDP. For VNC, RDP, SSH, SMB and web interfaces that is enough;
    for voice transmission or games it is not.
  * The traffic runs through the backend. For large file transfers that is
    slower than a direct connection.
  * A listening port is reachable by ANYONE who can reach the backend.
    Therefore the origin can be restricted (see 'allow_from') and every
    forward expires after a configured time.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import time
import uuid

from app import db
from app.errors import Codes, report

# Range from which free listening ports are assigned.
PORT_RANGE_START = 55000
PORT_RANGE_END = 55999
# How many connections one forward may carry at once.
MAX_CONNECTIONS = 64
# Time limit for establishing the connection to the target.
OPEN_TIMEOUT = 15.0


class _Conn:
    """A single forwarded TCP connection."""

    def __init__(self, stream_id: str, reader, writer, forward: "Forward"):
        self.id = stream_id
        self.reader = reader
        self.writer = writer
        self.forward = forward
        self.opened = asyncio.get_event_loop().create_future()
        self.closed = False
        self.bytes_in = 0
        self.bytes_out = 0


class Forward:
    """A running forward: one listening port -> one target on a client."""

    def __init__(self, rec: dict):
        self.id = rec["id"]
        self.client_id = rec["client_id"]
        self.host = rec.get("target_host") or "127.0.0.1"
        self.port = int(rec["target_port"])
        self.listen_port = int(rec["listen_port"])
        self.username = rec.get("username") or ""
        self.expires_at = int(rec.get("expires_at") or 0)
        self.allow_from = (rec.get("allow_from") or "").strip()
        self.server: asyncio.AbstractServer | None = None
        self.conns: dict[str, _Conn] = {}
        self.total = 0

    # -- Lauschen ------------------------------------------------------

    async def start(self) -> None:
        self.server = await asyncio.start_server(
            self._on_client, "0.0.0.0", self.listen_port)
        print(f"[weiterleitung] Port {self.listen_port} -> {self.client_id} "
              f"{self.host}:{self.port}")

    async def stop(self) -> None:
        for conn in list(self.conns.values()):
            self._close(conn)
        if self.server:
            self.server.close()
            try:
                await self.server.wait_closed()
            except Exception:
                pass
            self.server = None

    def _allowed(self, peer_ip: str) -> bool:
        """May this origin use the forward?"""
        if not self.allow_from:
            return True
        for entry in self.allow_from.replace(";", ",").split(","):
            entry = entry.strip()
            if not entry:
                continue
            try:
                if ipaddress.ip_address(peer_ip) in ipaddress.ip_network(
                        entry, strict=False):
                    return True
            except ValueError:
                continue
        return False

    # -- An incoming connection ----------------------------------------

    async def _on_client(self, reader, writer) -> None:
        peer = writer.get_extra_info("peername")
        peer_ip = peer[0] if peer else "?"

        if not self._allowed(peer_ip):
            print(f"[weiterleitung] {peer_ip} abgewiesen (Herkunft nicht erlaubt)")
            writer.close()
            return
        if len(self.conns) >= MAX_CONNECTIONS:
            print(f"[weiterleitung] Port {self.listen_port}: zu viele "
                  f"Verbindungen ({MAX_CONNECTIONS})")
            writer.close()
            return

        stream_id = f"pf-{self.id[:8]}-{uuid.uuid4().hex[:8]}"
        conn = _Conn(stream_id, reader, writer, self)
        self.conns[stream_id] = conn
        _streams[stream_id] = conn
        self.total += 1

        from app.sockets import send_to_agent
        try:
            await send_to_agent(self.client_id, "vpn-open", {
                "tunnel": f"pf:{self.id}", "stream": stream_id,
                "host": self.host, "port": self.port, "proto": "tcp",
            })
            ok = await asyncio.wait_for(conn.opened, timeout=OPEN_TIMEOUT)
        except asyncio.TimeoutError:
            report(Codes.SOCK_TIMEOUT, None,
                   "agent did not confirm the forward",
                   client=self.client_id, target=f"{self.host}:{self.port}")
            ok = False
        except Exception as e:
            report(Codes.SOCK_EMIT, e, "establish forward",
                   client=self.client_id)
            ok = False

        if not ok:
            self._close(conn)
            return

        # Ab hier nur noch durchreichen.
        try:
            while True:
                chunk = await reader.read(32768)
                if not chunk:
                    break
                conn.bytes_out += len(chunk)
                await send_to_agent(self.client_id, "vpn-data", {
                    "tunnel": f"pf:{self.id}", "stream": stream_id,
                    "data": base64.b64encode(chunk).decode(),
                })
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        except Exception as e:
            report(Codes.VPN_PACKET, e, "read forward")
        finally:
            try:
                await send_to_agent(self.client_id, "vpn-close",
                                    {"tunnel": f"pf:{self.id}",
                                     "stream": stream_id})
            except Exception:
                pass
            self._close(conn)

    def _close(self, conn: _Conn) -> None:
        if conn.closed:
            return
        conn.closed = True
        self.conns.pop(conn.id, None)
        _streams.pop(conn.id, None)
        if not conn.opened.done():
            conn.opened.set_result(False)
        try:
            conn.writer.close()
        except Exception:
            pass

    def stats(self) -> dict:
        return {
            "id": self.id, "client_id": self.client_id,
            "listen_port": self.listen_port,
            "target": f"{self.host}:{self.port}",
            "username": self.username, "expires_at": self.expires_at,
            "allow_from": self.allow_from,
            "active": len(self.conns), "total": self.total,
            "bytes_in": sum(c.bytes_in for c in self.conns.values()),
            "bytes_out": sum(c.bytes_out for c in self.conns.values()),
            "running": self.server is not None,
        }


# ----------------------------------------------------------------------
# Verwaltung
# ----------------------------------------------------------------------

forwards: dict[str, Forward] = {}
_streams: dict[str, _Conn] = {}


def owns_stream(stream_id: str) -> bool:
    """Does this data stream belong to a forward (and not to the VPN)?"""
    return stream_id in _streams


def on_open_result(payload: dict) -> None:
    conn = _streams.get(payload.get("stream", ""))
    if conn and not conn.opened.done():
        conn.opened.set_result(bool(payload.get("ok")))
        if not payload.get("ok"):
            print(f"[weiterleitung] Ziel nicht erreichbar: "
                  f"{payload.get('error') or 'keine Angabe'}")


def on_data(payload: dict) -> None:
    conn = _streams.get(payload.get("stream", ""))
    if not conn or conn.closed:
        return
    try:
        raw = base64.b64decode(payload.get("data") or "")
    except Exception:
        return
    conn.bytes_in += len(raw)
    try:
        conn.writer.write(raw)
    except Exception:
        conn.forward._close(conn)


def on_close(payload: dict) -> None:
    conn = _streams.get(payload.get("stream", ""))
    if conn:
        conn.forward._close(conn)


def free_port() -> int:
    """A free listening port from the designated range."""
    import socket as _s
    used = {f.listen_port for f in forwards.values()}
    for port in range(PORT_RANGE_START, PORT_RANGE_END + 1):
        if port in used:
            continue
        with _s.socket(_s.AF_INET, _s.SOCK_STREAM) as probe:
            probe.setsockopt(_s.SOL_SOCKET, _s.SO_REUSEADDR, 1)
            try:
                probe.bind(("0.0.0.0", port))
            except OSError:
                continue
        return port
    raise RuntimeError("Kein freier Port mehr im Bereich "
                       f"{PORT_RANGE_START}-{PORT_RANGE_END}")


async def create(client_id: str, target_port: int, username: str,
                 minutes: int = 240, target_host: str = "127.0.0.1",
                 listen_port: int = 0, allow_from: str = "",
                 label: str = "") -> dict:
    """Creates a forward and starts it immediately."""
    client = db.get_client(client_id)
    if not client:
        raise ValueError("Client unknown")

    rec = {
        "id": uuid.uuid4().hex,
        "client_id": client_id,
        "label": label or f"Port {target_port}",
        "target_host": target_host or "127.0.0.1",
        "target_port": int(target_port),
        "listen_port": int(listen_port) or free_port(),
        "username": username,
        "allow_from": allow_from,
        "created_at": int(time.time() * 1000),
        "expires_at": (int(time.time() * 1000) + minutes * 60_000
                       if minutes and minutes > 0 else 0),
    }
    db.create_port_forward(rec)
    forward = Forward(rec)
    try:
        await forward.start()
    except OSError as e:
        db.close_port_forward(rec["id"])
        raise RuntimeError(f"Port {rec['listen_port']} not available: {e}")
    forwards[rec["id"]] = forward
    return rec


async def stop(forward_id: str) -> bool:
    forward = forwards.pop(forward_id, None)
    if forward:
        await forward.stop()
    db.close_port_forward(forward_id)
    return bool(forward)


async def restore() -> None:
    """After a restart, reopen the forwards that are still valid."""
    for rec in db.list_port_forwards(active_only=True):
        if rec.get("expires_at") and rec["expires_at"] < time.time() * 1000:
            db.close_port_forward(rec["id"])
            continue
        forward = Forward(rec)
        try:
            await forward.start()
            forwards[rec["id"]] = forward
        except OSError as e:
            report(Codes.EXTERNAL, e, "forward not restored",
                   port=rec["listen_port"])
            db.close_port_forward(rec["id"])


async def expiry_loop() -> None:
    """Schliesst abgelaufene Weiterleitungen."""
    while True:
        await asyncio.sleep(60)
        now = time.time() * 1000
        for fid, forward in list(forwards.items()):
            if forward.expires_at and forward.expires_at < now:
                print(f"[weiterleitung] Port {forward.listen_port} abgelaufen")
                await stop(fid)


def overview() -> list[dict]:
    out = []
    for rec in db.list_port_forwards(active_only=True):
        forward = forwards.get(rec["id"])
        item = dict(rec)
        item.update(forward.stats() if forward
                    else {"running": False, "active": 0, "total": 0})
        client = db.get_client(rec["client_id"]) or {}
        item["hostname"] = client.get("hostname") or rec["client_id"]
        item["client_online"] = bool(client.get("online"))
        out.append(item)
    return out
