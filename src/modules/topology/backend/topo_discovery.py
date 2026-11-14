# topo_discovery.py (Modul "topology")
# -------------------------------------
# Erkennung der Netzwerk-Topologie - laeuft identisch im Backend UND auf einer
# Node (nur Standardbibliothek, keine App-Importe!).
#
# Was passiert bei einer Erkennung ("Discovery"):
#   1. Eigene IP + Standard-Gateway ermitteln
#   2. Ping-Sweep ueber das Ziel-Netz (Standard: eigenes /24)
#   3. MAC-Adressen aus der ARP-Tabelle, Hostnamen per Reverse-DNS
#   4. Port-Fingerprint je Geraet (wenige gaengige Ports) -> Geraete-Typ raten
#   5. Traceroute Richtung Internet (Ping mit steigender TTL) -> Kette
#      Gateway -> ... -> Internet ("www")
#   6. Daraus ein Baum: www -> (Upstream-Router) -> Gateway -> Geraete
#
# Alles laeuft als Hintergrund-Job mit Fortschritt (wie der Netzwerk-Scan).

import asyncio
import ipaddress
import platform
import re
import socket
import time
import uuid

IS_WINDOWS = platform.system() == "Windows"

# Ports fuer den Fingerprint (bewusst wenige -> schnell)
FP_PORTS = [22, 53, 80, 135, 443, 445, 515, 631, 3389, 5000, 5001, 8006, 8080, 9100]

MAX_TOTAL_PINGS = 384          # harte Obergrenze gleichzeitiger Ping-Prozesse
_ping_gate: asyncio.Semaphore | None = None


def _gate() -> asyncio.Semaphore:
    global _ping_gate
    if _ping_gate is None:
        _ping_gate = asyncio.Semaphore(MAX_TOTAL_PINGS)
    return _ping_gate


# ------------------------------------------------------------------
# Eigene IP / Gateway
# ------------------------------------------------------------------
def get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


async def get_default_gateway() -> str | None:
    """Standard-Gateway ermitteln (Linux: `ip route`, Windows: `route print`)."""
    try:
        if IS_WINDOWS:
            proc = await asyncio.create_subprocess_exec(
                "route", "print", "0.0.0.0",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await proc.communicate()
            m = re.search(r"0\.0\.0\.0\s+0\.0\.0\.0\s+(\d+\.\d+\.\d+\.\d+)",
                          out.decode(errors="ignore"))
            return m.group(1) if m else None
        proc = await asyncio.create_subprocess_exec(
            "ip", "route", "show", "default",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
        m = re.search(r"default\s+via\s+(\d+\.\d+\.\d+\.\d+)", out.decode(errors="ignore"))
        return m.group(1) if m else None
    except Exception:
        return None


# ------------------------------------------------------------------
# Ping / ARP / DNS (wie im Netzwerk-Scan)
# ------------------------------------------------------------------
async def _ping(ip: str, ttl: int | None = None) -> str:
    """Pingt eine IP. Rueckgabe: kompletter Ausgabetext (fuer TTL-Auswertung)."""
    if IS_WINDOWS:
        cmd = ["ping", "-n", "1", "-w", "900"]
        if ttl:
            cmd += ["-i", str(ttl)]
    else:
        cmd = ["ping", "-c", "1", "-W", "1"]
        if ttl:
            cmd += ["-t", str(ttl)]
    cmd.append(ip)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
        return out.decode(errors="ignore")
    except Exception:
        return ""


async def _ping_alive(ip: str) -> bool:
    text = await _ping(ip)
    if IS_WINDOWS:
        return "TTL=" in text or "ttl=" in text
    return "bytes from" in text or "1 received" in text or "1 packets received" in text


async def read_arp_table() -> dict[str, str]:
    mapping: dict[str, str] = {}
    try:
        proc = await asyncio.create_subprocess_exec(
            "arp", "-a", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
        text = out.decode(errors="ignore")
        if IS_WINDOWS:
            pattern = re.compile(r"(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F-]{17})")
        else:
            pattern = re.compile(r"\((\d+\.\d+\.\d+\.\d+)\)\s+at\s+([0-9a-fA-F:]{17})")
        for m in pattern.finditer(text):
            mapping[m.group(1)] = m.group(2).upper().replace("-", ":")
    except Exception:
        pass
    return mapping


async def _reverse_dns(ip: str) -> str | None:
    loop = asyncio.get_event_loop()
    try:
        host, _, _ = await asyncio.wait_for(
            loop.run_in_executor(None, socket.gethostbyaddr, ip), timeout=2.0)
        return host
    except Exception:
        return None


# ------------------------------------------------------------------
# Traceroute Richtung Internet - per Ping mit steigender TTL (portabel,
# braucht weder traceroute noch Raw-Sockets).
# ------------------------------------------------------------------
async def trace_to_internet(target: str = "8.8.8.8", max_hops: int = 8) -> list[str]:
    """Liste der Hop-IPs vom Gateway Richtung Internet (bis zum ersten
    oeffentlichen Hop einschliesslich, danach ist "www")."""
    hops: list[str] = []
    for ttl in range(1, max_hops + 1):
        text = await _ping(target, ttl=ttl)
        ip = None
        if IS_WINDOWS:
            m = (re.search(r"Antwort von (\d+\.\d+\.\d+\.\d+):", text)
                 or re.search(r"Reply from (\d+\.\d+\.\d+\.\d+):", text))
            if m:
                ip = m.group(1)
        else:
            m = re.search(r"[Ff]rom (\d+\.\d+\.\d+\.\d+)", text)
            if m:
                ip = m.group(1)
        if not ip:
            break                      # Hop antwortet nicht -> hier aufhoeren
        if ip == target:
            break                      # Ziel erreicht
        if ip in hops:
            break                      # Schleife
        hops.append(ip)
        try:
            if not ipaddress.ip_address(ip).is_private:
                break                  # erster oeffentlicher Hop = Provider-Seite
        except ValueError:
            break
    return hops


# ------------------------------------------------------------------
# Port-Fingerprint + Geraete-Typ raten
# ------------------------------------------------------------------
async def _check_port(ip: str, port: int, timeout: float = 0.6) -> bool:
    try:
        fut = asyncio.open_connection(ip, port)
        _, writer = await asyncio.wait_for(fut, timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


async def fingerprint_ports(ip: str, sem: asyncio.Semaphore) -> list[int]:
    open_ports: list[int] = []

    async def one(p: int) -> None:
        async with sem:
            if await _check_port(ip, p):
                open_ports.append(p)

    await asyncio.gather(*(one(p) for p in FP_PORTS))
    return sorted(open_ports)


_HOST_HINTS = [
    (("fritz", "router", "gateway", "gw", "edge", "mikrotik", "opnsense",
      "pfsense", "firewall", "fw"), "router"),
    (("switch", "sw-", "-sw", "cisco", "procurve", "netgear-sw"), "switch"),
    (("unifi", "-ap", "ap-", "accesspoint", "wlan", "wifi"), "ap"),
    (("proxmox", "pve", "esx", "hyperv", "xcp"), "hypervisor"),
    (("nas", "synology", "diskstation", "qnap", "truenas", "san"), "nas"),
    (("printer", "print", "drucker", "brother", "epson", "hp-", "kyocera", "lexmark"), "printer"),
    (("cam", "camera", "ipc", "dvr", "nvr"), "camera"),
    (("tv", "chromecast", "firetv", "shield", "roku", "appletv"), "tv"),
    (("iphone", "android", "phone", "handy", "pixel", "galaxy", "oneplus", "xiaomi", "redmi"), "phone"),
    (("laptop", "notebook", "macbook", "thinkpad"), "laptop"),
    (("esp", "tasmota", "shelly", "sonoff", "hue", "homeassistant", "iot"), "iot"),
    (("srv", "server", "-dc", "dc-", "dc0", "dc1", "host"), "server"),
]


def classify_device(ip: str, hostname: str | None, ports: list[int],
                    gateway_ip: str | None) -> str:
    """Geraete-Typ raten: erst Gateway, dann Hostname-Hinweise, dann Ports."""
    if gateway_ip and ip == gateway_ip:
        return "router"
    h = (hostname or "").lower()
    for hints, kind in _HOST_HINTS:
        if any(x in h for x in hints):
            return kind
    ps = set(ports)
    if ps & {515, 631, 9100}:
        return "printer"
    if 8006 in ps:
        return "hypervisor"
    if ps & {5000, 5001} and 445 in ps:
        return "nas"
    if 53 in ps and (80 in ps or 443 in ps) and not (ps & {3389, 445}):
        return "router"
    if 3389 in ps or (445 in ps and 135 in ps):
        return "server" if 22 in ps or 8080 in ps else "pc"
    if 22 in ps and not (ps & {80, 443}):
        return "server"
    if 22 in ps:
        return "server"
    if ps & {80, 443, 8080}:
        return "iot"
    return "device"


# ------------------------------------------------------------------
# Ziel-Angabe -> /24-Praefixe (vereinfachte Variante des Netzwerk-Scans)
# ------------------------------------------------------------------
def parse_target(spec: str | None) -> tuple[list[str], str]:
    spec = (spec or "").strip()
    if not spec:
        own = get_local_ip()
        pref = ".".join(own.split(".")[:3]) + "."
        return [pref], pref + "x"
    if "/" in spec:
        net = ipaddress.ip_network(spec, strict=False)
        if net.version != 4:
            raise ValueError("Nur IPv4 wird unterstuetzt")
        if net.prefixlen < 16:
            raise ValueError("Netz zu gross (mindestens /16)")
        if net.prefixlen >= 24:
            return [".".join(str(net.network_address).split(".")[:3]) + "."], str(net)
        return [".".join(str(s.network_address).split(".")[:3]) + "."
                for s in net.subnets(new_prefix=24)], str(net)
    parts = [p for p in spec.split(".") if p != ""]
    for p in parts:
        if not p.isdigit() or not (0 <= int(p) <= 255):
            raise ValueError(f"Ungueltiges Oktett: {p}")
    if len(parts) in (3, 4):
        pref = ".".join(parts[:3]) + "."
        return [pref], pref + "x"
    if len(parts) == 2:
        return [f"{parts[0]}.{parts[1]}.{i}." for i in range(256)], f"{parts[0]}.{parts[1]}.x.x"
    raise ValueError("Ziel bitte als 192.168.1. / 10.10 / 10.10.0.0/16 angeben")


# ==================================================================
# Discovery-Job
# ==================================================================
_jobs: dict[str, dict] = {}


def get_job(job_id: str) -> dict | None:
    return _jobs.get(job_id)


def cancel_job(job_id: str) -> bool:
    job = _jobs.get(job_id)
    if not job or job["status"] not in ("running", "queued"):
        return False
    job["cancel"] = True
    return True


def _prune(keep: int = 6) -> None:
    done = sorted((j["started"], jid) for jid, j in _jobs.items()
                  if j["status"] in ("done", "error", "cancelled"))
    for _, jid in done[:-keep] if len(done) > keep else []:
        _jobs.pop(jid, None)


async def _run_job(job_id: str, target: str | None) -> None:
    job = _jobs[job_id]
    try:
        prefixes, label = parse_target(target)
        own_ip = get_local_ip()
        gateway = await get_default_gateway()

        job.update({"status": "running", "phase": "ping", "label": label,
                    "total_hosts": len(prefixes) * 254, "done_hosts": 0, "found": 0})

        # ---- 1. Ping-Sweep ----
        alive: set[str] = set()
        sem = asyncio.Semaphore(96)

        async def check(ip: str) -> None:
            if job.get("cancel"):
                return
            async with sem:
                if job.get("cancel"):
                    return
                async with _gate():
                    if await _ping_alive(ip):
                        alive.add(ip)
                        job["found"] += 1
            job["done_hosts"] += 1

        for pref in prefixes:
            if job.get("cancel"):
                break
            await asyncio.gather(*(check(f"{pref}{i}") for i in range(1, 255)))

        if any(own_ip.startswith(p) for p in prefixes):
            alive.add(own_ip)
        if gateway and any(gateway.startswith(p) for p in prefixes):
            alive.add(gateway)

        # ---- 2. Traceroute Richtung Internet ----
        job["phase"] = "trace"
        hops = [] if job.get("cancel") else await trace_to_internet()

        # ---- 3. ARP / DNS / Port-Fingerprint ----
        job["phase"] = "fingerprint"
        job["fp_total"] = len(alive)
        job["fp_done"] = 0
        arp = await read_arp_table()
        port_sem = asyncio.Semaphore(160)

        async def build(ip: str) -> dict:
            hostname = await _reverse_dns(ip)
            ports = [] if job.get("cancel") else await fingerprint_ports(ip, port_sem)
            job["fp_done"] += 1
            return {
                "ip": ip,
                "mac": arp.get(ip),
                "hostname": hostname,
                "ports": ports,
                "type": classify_device(ip, hostname, ports, gateway),
                "self": ip == own_ip,
                "gateway": ip == gateway,
            }

        devices = list(await asyncio.gather(*(build(ip) for ip in sorted(alive))))
        devices.sort(key=lambda d: tuple(int(x) for x in d["ip"].split(".")))

        # ---- 4. Graph bauen: www -> Hops -> Gateway -> Geraete ----
        nodes: list[dict] = [{"id": "www", "type": "www", "label": "Internet", "level": 0}]
        edges: list[list[str]] = []
        known = {d["ip"] for d in devices}

        chain: list[dict] = []          # vom Internet abwaerts
        for ip in reversed(hops):       # hops[0] = Gateway, hops[-1] = Richtung Internet
            if ip == gateway or ip in known:
                continue                # Gateway/bekannte Geraete kommen gleich als Geraet
            try:
                pub = not ipaddress.ip_address(ip).is_private
            except ValueError:
                pub = False
            chain.append({"id": f"hop:{ip}", "type": "isp" if pub else "router",
                          "label": ip, "ip": ip, "level": len(chain) + 1, "hop": True})

        prev = "www"
        for c in chain:
            nodes.append(c)
            edges.append([prev, c["id"]])
            prev = c["id"]

        gw_level = len(chain) + 1
        gw_id = None
        for d in devices:
            d["id"] = f"dev:{d['mac'] or d['ip']}"
            if d["gateway"]:
                gw_id = d["id"]
                d["level"] = gw_level
        if gw_id is None and gateway:   # Gateway hat nicht auf Ping geantwortet
            arp_gw = arp.get(gateway)
            d = {"ip": gateway, "mac": arp_gw, "hostname": None, "ports": [],
                 "type": "router", "self": False, "gateway": True,
                 "id": f"dev:{arp_gw or gateway}", "level": gw_level}
            devices.insert(0, d)
            gw_id = d["id"]

        for d in devices:
            d.setdefault("level", gw_level + 1)
            nodes.append(d)
            if d["id"] == gw_id:
                edges.append([prev, d["id"]])
            elif gw_id:
                edges.append([gw_id, d["id"]])
            else:
                edges.append([prev, d["id"]])

        job["result"] = {
            "at": int(time.time() * 1000),
            "label": label,
            "own_ip": own_ip,
            "gateway": gateway,
            "nodes": nodes,
            "edges": edges,
        }
        job["status"] = "cancelled" if job.get("cancel") else "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
    finally:
        job["finished"] = int(time.time() * 1000)
        _prune()


def start_job(target: str | None) -> str:
    parse_target(target)                # Fehleingaben sofort melden
    job_id = uuid.uuid4().hex[:12]
    _jobs[job_id] = {
        "id": job_id, "status": "queued", "phase": "queued", "target": target or "",
        "label": "", "total_hosts": 0, "done_hosts": 0, "found": 0,
        "fp_total": 0, "fp_done": 0, "result": None, "error": None, "cancel": False,
        "started": int(time.time() * 1000), "finished": None,
    }
    asyncio.get_event_loop().create_task(_run_job(job_id, target))
    return job_id
