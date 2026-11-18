# services.py (module "cloudflare") - which services exist, and where does the tunnel point?
# -------------------------------------------------------------------------------------
# The targets are ALWAYS derived from the ports actually in use (fresh on each sync):
#   dashboard / webdav  RMM port (app.config.PORT), WebDAV under /dav/
#   ftp / sftp          the "relay" module shares the RMM port (switch): mode ftp|sftp
#   console_ssh         "console" module, setting ssh_port (only if ssh_enabled)
#   website             "webserver" module: rmm/<name>/ (files or forward)
#   docker              "docker" module: publication "path" (= website) or "port" (host port)
#   agent               RDP 3389 / SSH 22 / VNC 5900 (or own port) of an agent
#                       endpoint "server": permanent forward in the "network" module (RMM -> agent)
#                       endpoint "node":   cloudflared on a node in the agent network
#   custom              any address (http(s)://, tcp://, ssh://, rdp://)
# cloudflared on the RMM server runs on the same system as the backend (127.0.0.1).

from __future__ import annotations

import socket
import struct

from app import db, modules

PROTO_PORT = {"rdp": 3389, "ssh": 22, "vnc": 5900}
# Access modes per protocol: http = address in the browser; browser = Cloudflare renders SSH/VNC in the
# browser; client = cloudflared access on the PC; warp = private network (WARP client)
MODES = {"http": ["http", "warp"], "ssh": ["browser", "client", "warp"], "vnc": ["browser", "client", "warp"],
         "rdp": ["client", "warp"], "tcp": ["client", "warp"], "ftp": ["warp"]}


def rmm_port() -> int:
    from app.config import PORT
    return int(PORT)


def _on(mid: str) -> bool:
    try:
        return modules.is_enabled(mid)
    except Exception:
        return False


def docker_host() -> str:
    """Docker-Host aus Sicht des Backends (im Container: Standard-Gateway)."""
    try:
        from app.runtime_env import is_docker
        if is_docker():
            with open("/proc/net/route") as f:
                for line in f.readlines()[1:]:
                    p = line.split()
                    if p[1] == "00000000" and int(p[3], 16) & 2:
                        return socket.inet_ntoa(struct.pack("<L", int(p[2], 16)))
    except Exception:
        pass
    return "127.0.0.1"


def own_ip() -> str:
    """IP of the RMM server (for WARP routes to services of the server itself)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def catalog() -> dict:
    """All currently used services - for \"Add route\" (suggestions)."""
    port = rmm_port()
    out = [{"kind": "dashboard", "label": "RMM-Dashboard", "proto": "http", "port": port,
            "hint": "Web interface incl. WebDAV (/dav/) and API"},
           {"kind": "webdav", "label": "WebDAV (Relay)", "proto": "http", "port": port,
            "hint": "own address only for WebDAV drives (/dav/)"}]
    if _on("relay"):
        try:
            mode = modules.use("relay", "ftp_relay").mode()
        except Exception:
            mode = "off"
        if mode == "sftp":
            out.append({"kind": "sftp", "label": "SFTP (Relay)", "proto": "tcp", "port": port})
        elif mode == "ftp":
            out.append({"kind": "ftp", "label": "FTP (Relay)", "proto": "ftp", "port": port,
                        "hint": "FTP only works via WARP (private network) – better use SFTP or WebDAV"})
    if _on("console"):
        st = modules.get_settings("console")
        if str(st.get("ssh_enabled", "no")) == "yes":
            out.append({"kind": "console_ssh", "label": "RMM-Konsole (SSH)", "proto": "ssh",
                        "port": int(st.get("ssh_port") or 2222)})
    sites, pubs = [], []
    if _on("webserver"):
        try:
            for name, s in (modules.use("webserver", "sites").all_sites() or {}).items():
                if (s.get("upstream") or {}).get("provider") == "docker":
                    continue                   # Docker-Veroeffentlichungen stehen unten
                sites.append({"name": name, "title": s.get("title") or name, "public": bool(s.get("public"))})
        except Exception:
            pass
    if _on("docker"):
        try:
            for st in modules.use("docker", "stacks").all_stacks():
                for p in st.get("publish") or []:
                    pubs.append({"stack": st["id"], "stack_name": st["name"], "pub": p.get("id"),
                                 "mode": p.get("mode"), "service": p.get("service"), "port": p.get("port"),
                                 "name": p.get("name"), "public_port": p.get("public_port"),
                                 "target": st.get("target", "local")})
        except Exception:
            pass
    return {"services": out, "websites": sites, "docker": pubs, "rmm_port": port}


# ===================================================================== Ziel einer Route
def proto_of(r: dict) -> str:
    k = r["kind"]
    if k in ("dashboard", "webdav", "website"):
        return "http"
    if k == "docker":
        return "http"
    if k == "sftp":
        return "tcp"
    if k == "ftp":
        return "ftp"
    if k == "console_ssh":
        return "ssh"
    if k == "agent":
        return r["ref"].get("proto", "rdp")
    if k == "custom":
        u = (r["ref"].get("url") or "").lower()
        for p in ("ssh", "rdp", "tcp"):
            if u.startswith(p + "://"):
                return p
        return "http"
    return "http"


def tunnel_key(r: dict) -> str:
    """\"server\" or \"node:<client_id>\" - which tunnel/connector serves the route?"""
    if r["kind"] == "agent" and r.get("endpoint") == "node" and r.get("node_id"):
        return f"node:{r['node_id']}"
    return "server"


def origin(r: dict, forward_port: int | None = None) -> dict:
    """-> {service, path?, host, port} - cloudflared-Ziel aus Sicht des zustaendigen Connectors."""
    k, ref, port = r["kind"], r.get("ref") or {}, rmm_port()
    lo = "127.0.0.1"
    if k in ("dashboard", "webdav"):
        return {"service": f"http://{lo}:{port}", "host": own_ip(), "port": port}
    if k == "sftp":
        return {"service": f"tcp://{lo}:{port}", "host": own_ip(), "port": port}
    if k == "ftp":
        return {"service": f"tcp://{lo}:{port}", "host": own_ip(), "port": port}
    if k == "console_ssh":
        p = int(modules.get_settings("console").get("ssh_port") or 2222)
        return {"service": f"ssh://{lo}:{p}", "host": own_ip(), "port": p}
    if k == "website":
        name = ref.get("site", "")
        return {"service": f"http://{lo}:{port}", "path": f"/{name}/", "host": own_ip(), "port": port}
    if k == "docker":
        pubs = {(p["stack"], p["pub"]): p for p in catalog()["docker"]}
        p = pubs.get((ref.get("stack"), ref.get("pub")))
        if not p:
            raise ValueError("Docker publication no longer exists")
        if p["mode"] == "path":
            return {"service": f"http://{lo}:{port}", "path": f"/{p['name']}/", "host": own_ip(), "port": port}
        if p.get("target", "local") != "local":
            raise ValueError("Tunnel port publications on nodes via a node route (own address)")
        return {"service": f"http://{docker_host()}:{p['public_port']}", "host": docker_host(),
                "port": int(p["public_port"])}
    if k == "agent":
        proto = ref.get("proto", "rdp")
        tport = int(ref.get("port") or PROTO_PORT.get(proto, 3389))
        scheme = {"rdp": "rdp", "ssh": "ssh", "vnc": "tcp"}[proto]
        if tunnel_key(r) == "server":
            if not forward_port:
                raise ValueError("Forward to the agent is missing (Network module)")
            return {"service": f"{scheme}://{lo}:{forward_port}", "host": own_ip(), "port": forward_port}
        host = agent_lan_host(r)
        return {"service": f"{scheme}://{host}:{tport}", "host": host, "port": tport}
    if k == "custom":
        url = (ref.get("url") or "").strip()
        if not url or "://" not in url:
            raise ValueError("Give a custom address in the format http://host:port, tcp://host:port, ssh://…")
        hp = url.split("://", 1)[1].split("/", 1)[0]
        h, _, p = hp.rpartition(":")
        return {"service": url, "host": h or hp, "port": int(p) if p.isdigit() else 0}
    raise ValueError(f"Unbekannte Art: {k}")


def agent_lan_host(r: dict) -> str:
    ref = r.get("ref") or {}
    if ref.get("host_mode") == "custom" and ref.get("host"):
        return ref["host"]
    c = db.get_client(r["ref"].get("client_id", "")) or {}
    ip = (c.get("ip") or "").strip()
    if not ip:
        raise ValueError("No local IP known for the agent – give an own address")
    return ip


def agent_target(r: dict) -> tuple[str, int]:
    """Target of the forward from the agent's point of view (endpoint server)."""
    ref = r.get("ref") or {}
    proto = ref.get("proto", "rdp")
    port = int(ref.get("port") or PROTO_PORT.get(proto, 3389))
    host = ref.get("host") if ref.get("host_mode") == "custom" and ref.get("host") else "127.0.0.1"
    return host, port
