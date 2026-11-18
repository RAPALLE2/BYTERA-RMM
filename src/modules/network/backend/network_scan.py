"""
network_scan.py
----------------
Scans the local network (the /24 subnet the backend machine itself is in) for
reachable devices - the counterpart to "Network Center" in Synology DSM.

Approach:
1. Find the own local IP -> derive the subnet from it (e.g. 192.168.1.x)
2. Ping all 254 possible addresses in parallel ("ping sweep")
3. For the replies, read the MAC address from the system ARP table
4. For the replies, try to resolve the hostname via reverse DNS

Everything runs asynchronously (asyncio) so that 254 pings do not cost time
254 times in a row, but run largely in parallel.
"""

import asyncio
import platform
import re
import socket
import time
import uuid

IS_WINDOWS = platform.system() == "Windows"


def get_local_subnet() -> tuple[str, str]:
    """
    Determines the own local IP address and the /24 subnet prefix from it.
    Trick: we "connect" a UDP socket to a public address as a test (without
    actually sending data!) - the operating system then automatically picks the
    "right" local network interface.
    Returns: (subnet prefix e.g. "192.168.1.", own IP e.g. "192.168.1.42")
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        own_ip = s.getsockname()[0]
    except Exception:
        own_ip = "127.0.0.1"
    finally:
        s.close()

    parts = own_ip.split(".")
    prefix = ".".join(parts[:3]) + "."
    return prefix, own_ip


async def _ping_host(ip: str) -> bool:
    """Pings a single IP address (platform-dependent command)."""
    if IS_WINDOWS:
        cmd = ["ping", "-n", "1", "-w", "300", ip]
    else:
        cmd = ["ping", "-c", "1", "-W", "1", ip]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        out, _ = await proc.communicate()
        text = out.decode(errors="ignore")
        if IS_WINDOWS:
            return "TTL=" in text or "ttl=" in text
        return "bytes from" in text or "1 packets received" in text or "1 received" in text
    except Exception:
        return False


async def _read_arp_table() -> dict[str, str]:
    """Reads the system ARP table to find MAC addresses matching the pings."""
    mapping: dict[str, str] = {}
    try:
        proc = await asyncio.create_subprocess_exec(
            "arp", "-a", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        out, _ = await proc.communicate()
        text = out.decode(errors="ignore")

        if IS_WINDOWS:
            pattern = re.compile(r"(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F-]{17})")
        else:
            pattern = re.compile(r"\((\d+\.\d+\.\d+\.\d+)\)\s+at\s+([0-9a-fA-F:]{17})")

        for match in pattern.finditer(text):
            mapping[match.group(1)] = match.group(2).upper()
    except Exception:
        pass  # arp not available -> simply show no MAC addresses
    return mapping


async def _reverse_dns(ip: str) -> str | None:
    """Tries to find a hostname for an IP address (may fail, that is ok)."""
    loop = asyncio.get_event_loop()
    try:
        # gethostbyaddr is a blocking function -> run it in a thread so it does
        # not hold up the rest of the asyncio code.
        hostname, _, _ = await loop.run_in_executor(None, socket.gethostbyaddr, ip)
        return hostname
    except Exception:
        return None


# ------------------------------------------------------------------
# Parse the target spec: /24, /16 (e.g. "10.10") or CIDR
# ------------------------------------------------------------------
def parse_scan_target(spec: str | None) -> tuple[list[str], str]:
    """
    Converts a target spec into a list of /24 prefixes.
    Supports:
      "192.168.1." / "192.168.1"   -> one /24           (254 addresses)
      "10.10." / "10.10"           -> 256 /24 networks   (65,024 addresses)
      "10."  / "10"                -> 65,536 /24 networks (deliberately allowed,
                                      takes correspondingly long)
      "10.10.0.0/16", "192.168.1.0/24", "172.16.0.0/22"  -> CIDR
      empty                         -> own /24
    Returns: (list of /24 prefixes like "10.10.7.", label)
    """
    spec = (spec or "").strip()
    if not spec:
        prefix, _ = get_local_subnet()
        return [prefix], prefix + "x"

    # --- CIDR notation ---
    if "/" in spec:
        import ipaddress
        try:
            net = ipaddress.ip_network(spec, strict=False)
        except ValueError as e:
            raise ValueError(f"Invalid CIDR spec: {e}")
        if net.version != 4:
            raise ValueError("Only IPv4 is supported")
        if net.prefixlen < 8:
            raise ValueError("Network too large (at least /8)")
        # Collect all contained /24 blocks
        prefixes = []
        if net.prefixlen >= 24:
            first = net.network_address
            prefixes.append(".".join(str(first).split(".")[:3]) + ".")
        else:
            for sub in net.subnets(new_prefix=24):
                prefixes.append(".".join(str(sub.network_address).split(".")[:3]) + ".")
        return prefixes, str(net)

    # --- Dotted notation: the number of octets decides ---
    parts = [p for p in spec.split(".") if p != ""]
    for p in parts:
        if not p.isdigit() or not (0 <= int(p) <= 255):
            raise ValueError(f"Invalid octet: {p}")
    if len(parts) == 3:
        pref = ".".join(parts) + "."
        return [pref], pref + "x"
    if len(parts) == 4:
        pref = ".".join(parts[:3]) + "."
        return [pref], pref + "x"
    if len(parts) == 2:                       # e.g. "10.10" -> 10.10.0.x … 10.10.255.x
        return [f"{parts[0]}.{parts[1]}.{i}." for i in range(256)], f"{parts[0]}.{parts[1]}.x.x"
    if len(parts) == 1:                       # e.g. "10" -> /8
        return [f"{parts[0]}.{i}.{j}." for i in range(256) for j in range(256)], f"{parts[0]}.x.x.x"
    raise ValueError("Please give the target as 10.10, 192.168.1. or 10.10.0.0/16")


async def scan_local_network(subnet_prefix: str | None = None) -> list[dict]:
    """
    Runs the complete scan and returns a list of devices:
    [{"ip": ..., "mac": ..., "hostname": ..., "alive": True}, ...]

    subnet_prefix: optional subnet like "192.168.5." - if not given, the own
    subnet is determined automatically.
    """
    if subnet_prefix:
        prefix = subnet_prefix if subnet_prefix.endswith(".") else subnet_prefix + "."
        own_ip = None
    else:
        prefix, own_ip = get_local_subnet()

    candidate_ips = [f"{prefix}{i}" for i in range(1, 255)]

    # At most 32 pings at once, so as not to overload the network
    semaphore = asyncio.Semaphore(32)
    alive_ips: set[str] = set()

    async def check_one(ip: str) -> None:
        async with semaphore:
            if await _ping_host(ip):
                alive_ips.add(ip)

    await asyncio.gather(*(check_one(ip) for ip in candidate_ips))
    if own_ip:
        alive_ips.add(own_ip)  # the own machine is "in the network" too

    arp_table = await _read_arp_table()

    async def build_device(ip: str) -> dict:
        hostname = await _reverse_dns(ip)
        return {"ip": ip, "mac": arp_table.get(ip), "hostname": hostname, "alive": True}

    devices = await asyncio.gather(*(build_device(ip) for ip in alive_ips))

    # Sort by IP address (numerically, not alphabetically, otherwise .10 comes before .2)
    devices.sort(key=lambda d: tuple(int(part) for part in d["ip"].split(".")))
    return devices


# ==================================================================
# JOB-BASED SCAN (for large networks like /16)
# Scanning one /24 in one go takes seconds - a /16 is 256 such blocks. That is
# why a large scan runs as a background job: the frontend starts it, polls the
# progress and sees found devices already DURING the scan.
#
# "Speed-Up": the /24 blocks are distributed across several workers that run in
# parallel - and each worker in turn pings several addresses at once.
# Effective concurrency = workers * per_subnet.
# ==================================================================
SCAN_SPEEDS = {
    # name: (parallel /24 blocks, concurrent pings per block)
    "normal": (1, 64),      # gentle on network and CPU
    "fast":   (8, 96),      # Speed-Up on
    "turbo":  (24, 128),    # very aggressive, only useful in your own LAN
}

# HARD upper bound for concurrent pings across ALL workers.
# Each ping is its own system process; without this brake Turbo (24 x 128)
# would start over 3000 processes at once and, depending on the limit
# (ulimit -u), fail at the operating system. 512 is fast and stays safe.
MAX_TOTAL_PINGS = 512
_ping_gate: asyncio.Semaphore | None = None


def _gate() -> asyncio.Semaphore:
    """The semaphore must be created in the running event loop."""
    global _ping_gate
    if _ping_gate is None:
        _ping_gate = asyncio.Semaphore(MAX_TOTAL_PINGS)
    return _ping_gate

_scan_jobs: dict[str, dict] = {}


def get_scan_job(job_id: str) -> dict | None:
    return _scan_jobs.get(job_id)


def cancel_scan_job(job_id: str) -> bool:
    job = _scan_jobs.get(job_id)
    if not job or job["status"] not in ("running", "queued"):
        return False
    job["cancel"] = True
    return True


def _prune_scan_jobs(keep: int = 8) -> None:
    """Clean up old, finished jobs so memory does not grow."""
    done = [(j["started"], jid) for jid, j in _scan_jobs.items()
            if j["status"] in ("done", "error", "cancelled")]
    for _, jid in sorted(done)[:-keep] if len(done) > keep else []:
        _scan_jobs.pop(jid, None)


async def _scan_one_subnet(prefix: str, per_subnet: int, job: dict) -> list[str]:
    """Pings through a /24 and returns the reachable IPs."""
    sem = asyncio.Semaphore(per_subnet)
    alive: list[str] = []

    async def one(ip: str) -> None:
        if job.get("cancel"):
            return
        # Two brakes: per subnet (sem) and global across all workers (_gate),
        # so that never more than MAX_TOTAL_PINGS processes run at once.
        async with sem:
            if job.get("cancel"):
                return
            async with _gate():
                if job.get("cancel"):
                    return
                if await _ping_host(ip):
                    alive.append(ip)
                    job["found"] += 1
        job["done_hosts"] += 1

    await asyncio.gather(*(one(f"{prefix}{i}") for i in range(1, 255)))
    return alive


async def run_scan_job(job_id: str, target: str | None, speed: str = "normal") -> None:
    """Background task: scans all /24 blocks of the target and fills the job."""
    job = _scan_jobs[job_id]
    try:
        prefixes, label = parse_scan_target(target)
        workers, per_subnet = SCAN_SPEEDS.get(speed, SCAN_SPEEDS["normal"])
        job.update({
            "status": "running", "label": label,
            "total_subnets": len(prefixes), "done_subnets": 0,
            "total_hosts": len(prefixes) * 254, "done_hosts": 0,
            "workers": workers, "per_subnet": per_subnet,
            "max_parallel": min(workers * per_subnet, MAX_TOTAL_PINGS),
        })

        # Distribute the /24 blocks evenly across the workers (Speed-Up).
        queue: asyncio.Queue = asyncio.Queue()
        for pref in prefixes:
            queue.put_nowait(pref)
        alive_ips: list[str] = []

        async def worker() -> None:
            while not queue.empty() and not job.get("cancel"):
                pref = await queue.get()
                try:
                    alive_ips.extend(await _scan_one_subnet(pref, per_subnet, job))
                finally:
                    job["done_subnets"] += 1
                    queue.task_done()

        await asyncio.gather(*(worker() for _ in range(min(workers, len(prefixes)))))

        # Add the own IP if the own network was scanned
        _, own_ip = get_local_subnet()
        if own_ip and any(own_ip.startswith(p) for p in prefixes) and own_ip not in alive_ips:
            alive_ips.append(own_ip)

        arp_table = await _read_arp_table()

        async def build(ip: str) -> dict:
            return {"ip": ip, "mac": arp_table.get(ip),
                    "hostname": await _reverse_dns(ip), "alive": True}

        devices = list(await asyncio.gather(*(build(ip) for ip in alive_ips)))
        devices.sort(key=lambda d: tuple(int(x) for x in d["ip"].split(".")))
        job["devices"] = devices
        job["status"] = "cancelled" if job.get("cancel") else "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
    finally:
        job["finished"] = int(time.time() * 1000)
        _prune_scan_jobs()


def start_scan_job(target: str | None, speed: str = "normal") -> str:
    """Creates a job and starts it in the background. Returns the job ID."""
    # Check the target up front so bad input is noticed immediately.
    prefixes, label = parse_scan_target(target)
    job_id = uuid.uuid4().hex[:12]
    _scan_jobs[job_id] = {
        "id": job_id, "status": "queued", "target": target or "", "label": label,
        "speed": speed, "total_subnets": len(prefixes), "done_subnets": 0,
        "total_hosts": len(prefixes) * 254, "done_hosts": 0, "found": 0,
        "devices": [], "error": None, "cancel": False,
        "started": int(time.time() * 1000), "finished": None,
    }
    asyncio.get_event_loop().create_task(run_scan_job(job_id, target, speed))
    return job_id


# Common ports with plain-text names (for the port-scan output)
COMMON_PORTS = {
    21: "FTP", 22: "SSH", 23: "Telnet", 25: "SMTP", 53: "DNS", 80: "HTTP",
    110: "POP3", 135: "RPC", 139: "NetBIOS", 143: "IMAP", 443: "HTTPS",
    445: "SMB", 993: "IMAPS", 995: "POP3S", 1433: "MSSQL", 3306: "MySQL",
    3389: "RDP", 5432: "PostgreSQL", 5900: "VNC", 8080: "HTTP-Alt", 8443: "HTTPS-Alt",
}


def parse_port_spec(spec: str) -> list[int]:
    """
    Converts a port spec like "22,80,8000-8100" into a sorted, unique list of
    valid ports (1-65535). Invalid parts are ignored.
    """
    ports: set[int] = set()
    for part in (spec or "").replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                start, end = int(a), int(b)
            except ValueError:
                continue
            if start > end:
                start, end = end, start
            for p in range(max(1, start), min(65535, end) + 1):
                ports.add(p)
        else:
            try:
                p = int(part)
            except ValueError:
                continue
            if 1 <= p <= 65535:
                ports.add(p)
    return sorted(ports)


async def scan_ports(
    ip: str,
    ports: list[int] | None = None,
    concurrency: int = 100,
    timeout: float = 1.0,
) -> list[dict]:
    """
    Checks which of the given ports are open for an IP.
    Without a port list the common ports (COMMON_PORTS) are checked.
    Returns: [{"port": 22, "service": "SSH", "open": True}, ...] (open ones only).

    For very large port lists (e.g. "all ports"), higher concurrency and a
    shorter timeout keep the scan finishing in a reasonable time.
    """
    if ports is None:
        ports = sorted(COMMON_PORTS.keys())

    semaphore = asyncio.Semaphore(concurrency)
    open_ports = []

    async def check_port(port: int):
        async with semaphore:
            try:
                fut = asyncio.open_connection(ip, port)
                reader, writer = await asyncio.wait_for(fut, timeout=timeout)
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
                open_ports.append({"port": port, "service": COMMON_PORTS.get(port, "?"), "open": True})
            except Exception:
                pass  # port closed/filtered

    await asyncio.gather(*(check_port(p) for p in ports))
    open_ports.sort(key=lambda x: x["port"])
    return open_ports
