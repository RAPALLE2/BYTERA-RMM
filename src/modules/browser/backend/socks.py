# socks.py (Modul "browser") - lokaler SOCKS5-Zugang je Browser-Netzwerk
# ---------------------------------------------------------------------
# Jeder Browser-Kontext (= ein "Netzwerk": direkt oder ueber einen Agenten)
# bekommt einen eigenen SOCKS5-Server auf 127.0.0.1:<zufall>. Chromium schickt
# ALLE Verbindungen dorthin (auch localhost - siehe --proxy-bypass-list=<-loopback>).
# Hier wird je Verbindung entschieden:
#
#   direkt         asyncio.open_connection() vom Server aus
#   ueber Agent    app/agent_streams.open_stream() -> der Agent verbindet in SEINEM
#                  Netz (Hostnamen loest der Agent auf: nas.local, intranet ...)
#
# Schutz: Direktverbindungen zu Loopback (127.0.0.0/8, ::1), Link-Local und
# Cloud-Metadaten (169.254.0.0/16) sind gesperrt - sonst koennte jeder
# Browser-Benutzer interne Dienste des RMM-Servers selbst aufrufen.
# Chromium sendet bei SOCKS5 den HOSTNAMEN (DNS-Aufloesung beim Proxy).
from __future__ import annotations

import asyncio
import ipaddress
import socket
import struct
from typing import Awaitable, Callable

# route(host, port) -> ("direct", None) | ("agent", client_id) | ("block", grund)
Router = Callable[[str, int], Awaitable[tuple[str, str | None]]]

_BLOCK_NETS = [ipaddress.ip_network(n) for n in
               ("127.0.0.0/8", "::1/128", "169.254.0.0/16", "fe80::/10", "0.0.0.0/8")]


def _blocked_ip(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(a in n for n in _BLOCK_NETS)


async def _resolve_direct(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [i[4][0] for i in infos]


class SocksServer:
    def __init__(self, router: Router, label: str = ""):
        self.router = router
        self.label = label
        self.server: asyncio.AbstractServer | None = None
        self.port = 0
        self.active = 0
        self.errors: list[str] = []

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            try:
                await self.server.wait_closed()
            except Exception:
                pass
            self.server = None

    def _note(self, msg: str) -> None:
        self.errors = (self.errors + [msg])[-20:]

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.active += 1
        try:
            await self._handle(reader, writer)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except Exception as e:
            self._note(str(e))
        finally:
            self.active -= 1
            try:
                writer.close()
            except Exception:
                pass

    async def _handle(self, reader, writer) -> None:
        # --- Begruessung: nur "keine Authentifizierung" (Server lauscht nur lokal)
        ver, n = await reader.readexactly(2)
        if ver != 5:
            return
        await reader.readexactly(n)
        writer.write(b"\x05\x00")
        await writer.drain()
        # --- Anfrage
        ver, cmd, _rsv, atyp = await reader.readexactly(4)
        if atyp == 1:
            host = socket.inet_ntop(socket.AF_INET, await reader.readexactly(4))
        elif atyp == 3:
            ln = (await reader.readexactly(1))[0]
            host = (await reader.readexactly(ln)).decode("ascii", errors="replace")
        elif atyp == 4:
            host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
        else:
            return await self._reply(writer, 8)
        port = struct.unpack("!H", await reader.readexactly(2))[0]
        if cmd != 1:                                       # nur CONNECT
            return await self._reply(writer, 7)

        kind, ref = await self.router(host, port)
        if kind == "block":
            self._note(f"{host}:{port} blocked ({ref})")
            return await self._reply(writer, 2)
        if kind == "agent":
            return await self._via_agent(ref, host, port, reader, writer)
        return await self._direct(host, port, reader, writer)

    async def _reply(self, writer, code: int) -> None:
        writer.write(b"\x05" + bytes([code]) + b"\x00\x01" + b"\x00" * 6)
        try:
            await writer.drain()
        except Exception:
            pass

    # ------------------------------------------------------------ direkt
    async def _direct(self, host, port, reader, writer) -> None:
        try:
            ips = await _resolve_direct(host, port)
        except Exception as e:
            self._note(f"{host}: {e}")
            return await self._reply(writer, 4)
        ips = [ip for ip in ips if not _blocked_ip(ip)]
        if not ips:
            self._note(f"{host}:{port} blocked (address of the server itself)")
            return await self._reply(writer, 2)
        up_r = up_w = None
        for ip in ips:
            try:
                up_r, up_w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=15)
                break
            except Exception as e:
                last = e
        if not up_w:
            self._note(f"{host}:{port}: {last}")
            return await self._reply(writer, 5)
        await self._reply(writer, 0)

        async def pump(src, dst):
            try:
                while True:
                    data = await src.read(65536)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            except Exception:
                pass
            finally:
                try:
                    dst.close()
                except Exception:
                    pass
        await asyncio.gather(pump(reader, up_w), pump(up_r, writer))

    # ------------------------------------------------------------ ueber Agent
    async def _via_agent(self, client_id, host, port, reader, writer) -> None:
        from app import agent_streams
        try:
            s = await agent_streams.open_stream(client_id, host, port, tag="browser")
        except Exception as e:
            self._note(f"{host}:{port} via agent: {e}")
            return await self._reply(writer, 5)
        await self._reply(writer, 0)

        async def up():
            try:
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    await s.write(data)
            except Exception:
                pass
            finally:
                await s.close()

        async def down():
            try:
                while True:
                    data = await s.read()
                    if not data:
                        break
                    writer.write(data)
                    await writer.drain()
            except Exception:
                pass
            finally:
                try:
                    writer.close()
                except Exception:
                    pass
        await asyncio.gather(up(), down())
