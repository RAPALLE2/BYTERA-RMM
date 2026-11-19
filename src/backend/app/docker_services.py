"""
docker_services.py
------------------
Add extra services at the push of a button - but ONLY if the backend itself
runs in a container.

Background: when installed natively, one sets up guacd or a SQL database
normally on the server. In a container that would be cumbersome - one would
have to edit docker-compose.yml on the host. Therefore the backend can start
two neighbor containers itself here:

    guacd   Apache guacd for RDP/VNC/SSH in the browser
    db      MariaDB as an external database

How it works
------------
The container talks via the Docker socket (/var/run/docker.sock) to the host's
Docker service and creates the containers as SIBLINGS - not nested. To make
them reachable, the backend attaches them to its OWN network; they are then
addressed via their container name (e.g. "bytera-rmm-guacd:4822"), entirely
without published ports.

Prerequisite: the socket must be mounted in docker-compose.yml:

    volumes:
      - /var/run/docker.sock:/var/run/docker.sock

Security note: whoever has access to the Docker socket effectively has root on
the host. Therefore all endpoints here are admin-only, and the socket is
deliberately commented out in docker-compose.yml - it is mounted only when this
feature is really wanted.

The database credentials are generated once on creation and stored in the
settings, so the dashboard can pre-fill the database form.
"""

from __future__ import annotations

import json
import secrets
import socket
import string
import http.client
from typing import Any

from app import db
from app.runtime_env import is_docker

DOCKER_SOCKET = "/var/run/docker.sock"

# The two services that can be added.
SERVICES: dict[str, dict[str, Any]] = {
    "guacd": {
        "label": "Guacamole (guacd)",
        "purpose": "Remote desktop in the browser: RDP, VNC, SSH and Telnet.",
        "image": "guacamole/guacd:1.5.5",
        "container": "bytera-rmm-guacd",
        "port": 4822,
    },
    "db": {
        "label": "SQL database (MariaDB)",
        "purpose": "External database instead of the local SQLite file.",
        "image": "mariadb:11",
        "container": "bytera-rmm-db",
        "port": 3306,
        "volume": "bytera-rmm-dbdata",
        "database": "bytera_rmm",
        "user": "rmm",
    },
}

# Names before the rename (RAPALLE.net RMM -> BYTERA RMM). Existing containers
# are renamed (migrate_legacy), the database volume is copied into the new
# volume on the next recreation of the container (the old one stays as a
# backup).
LEGACY: dict[str, dict[str, str]] = {
    "guacd": {"container": "rapalle-rmm-guacd"},
    "db": {"container": "rapalle-rmm-db", "volume": "rapalle-rmm-dbdata"},
}


# ==========================================================================
# Tiny Docker API client (via the Unix socket, without extra packages)
# ==========================================================================

class _UnixHTTPConnection(http.client.HTTPConnection):
    """http.client that talks to a Unix socket instead of TCP."""

    def __init__(self, path: str, timeout: float = 60):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._path)
        self.sock = sock


class DockerError(RuntimeError):
    pass


def _api(method: str, path: str, body: dict | None = None, timeout: float = 60) -> Any:
    """One call against the Docker Engine API. Returns JSON or raw text."""
    conn = _UnixHTTPConnection(DOCKER_SOCKET, timeout=timeout)
    try:
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if payload else {}
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status >= 400:
            try:
                msg = json.loads(raw).get("message", raw.decode(errors="replace"))
            except Exception:
                msg = raw.decode(errors="replace")
            raise DockerError(f"Docker: {msg} (HTTP {resp.status})")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return raw.decode(errors="replace")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def socket_available() -> bool:
    """Is the Docker socket mounted AND reachable?"""
    try:
        _api("GET", "/_ping", timeout=5)
        return True
    except Exception:
        return False


# ==========================================================================
# Own container: determine the network
# ==========================================================================

def _self_container() -> dict | None:
    """
    The description of the OWN container. In Docker the hostname is by default
    the (shortened) container ID.
    """
    import os
    for ident in (os.getenv("HOSTNAME", ""), "/" + os.getenv("HOSTNAME", "")):
        if not ident.strip("/"):
            continue
        try:
            return _api("GET", f"/containers/{ident.lstrip('/')}/json", timeout=10)
        except Exception:
            continue
    return None


def _self_network() -> str | None:
    """Name of the network the backend is attached to (for the neighbors)."""
    info = _self_container()
    if not info:
        return None
    nets = (info.get("NetworkSettings") or {}).get("Networks") or {}
    # "bridge" only as a last resort - name resolution does not work there.
    for name in nets:
        if name != "bridge":
            return name
    return next(iter(nets), None)


def _host_path_for(container_path: str) -> str | None:
    """
    Find the matching host path for a path IN the container.
    Needed so guacd puts its recordings in the same folder the backend reads
    later.
    """
    info = _self_container()
    if not info:
        return None
    best = None
    for m in info.get("Mounts") or []:
        dest = (m.get("Destination") or "").rstrip("/")
        if dest and container_path.rstrip("/").startswith(dest):
            rest = container_path.rstrip("/")[len(dest):]
            src = m.get("Source") or ""
            if src:
                best = src + rest
    return best


# ==========================================================================
# Status / switching on and off
# ==========================================================================

def _find_container(name: str) -> dict | None:
    try:
        found = _api("GET",
                     "/containers/json?all=1&filters="
                     + json.dumps({"name": [name]}).replace(" ", ""))
    except Exception:
        return None
    for c in found or []:
        if any(n.lstrip("/") == name for n in c.get("Names", [])):
            return c
    return None


_migrated = False


def migrate_legacy() -> list[str]:
    """
    Rename containers with the old name (rapalle-rmm-*) to the new name and
    update the settings that contain the container name as the host. Renaming
    works during operation - the Docker DNS then knows the new name. Idempotent;
    runs at most once at start and before each action.
    """
    global _migrated
    done: list[str] = []
    if _migrated:
        return done
    if not (is_docker() and socket_available()):
        return done
    _migrated = True
    for key, old in LEGACY.items():
        new = SERVICES[key]["container"]
        try:
            if _find_container(old["container"]) and not _find_container(new):
                _api("POST", f"/containers/{old['container']}/rename?name={new}", timeout=30)
                done.append(f"{old['container']} -> {new}")
        except DockerError as e:
            done.append(f"{old['container']}: {e}")
            continue
        setting = "guacd_host" if key == "guacd" else "docker_db_host"
        try:
            if (db.get_setting(setting) or "") == old["container"]:
                db.set_setting(setting, new)
        except Exception:
            pass
    return done


def _volume_exists(name: str) -> bool:
    try:
        _api("GET", f"/volumes/{name}", timeout=15)
        return True
    except DockerError:
        return False


def _copy_volume(src: str, dst: str, image: str) -> None:
    """Copy the content of a volume into another (helper container, the service is stopped meanwhile)."""
    _create_volume(dst)
    cfg = {"Image": image, "Entrypoint": ["sh", "-c"], "Cmd": ["cp -a /from/. /to/"],
           "HostConfig": {"Binds": [f"{src}:/from:ro", f"{dst}:/to"]}}
    c = _api("POST", "/containers/create", cfg, timeout=60)
    cid = c["Id"]
    try:
        _api("POST", f"/containers/{cid}/start", timeout=60)
        res = _api("POST", f"/containers/{cid}/wait", timeout=1800) or {}
        if int(res.get("StatusCode", 1)) != 0:
            raise DockerError(f"copying volume {src} to {dst} failed")
    finally:
        try:
            _api("DELETE", f"/containers/{cid}?force=1", timeout=60)
        except DockerError:
            pass


def status() -> dict:
    """State of all extra services - basis for the display in the dashboard."""
    migrate_legacy()
    available = is_docker() and socket_available()
    out = {
        "is_docker": is_docker(),
        "socket": available,
        "network": _self_network() if available else None,
        "services": [],
    }
    for key, spec in SERVICES.items():
        entry = {
            "key": key,
            "label": spec["label"],
            "purpose": spec["purpose"],
            "image": spec["image"],
            "container": spec["container"],
            "state": "unavailable",
            "running": False,
        }
        if available:
            c = _find_container(spec["container"])
            if c:
                entry["state"] = c.get("State", "unknown")
                entry["running"] = c.get("State") == "running"
            else:
                entry["state"] = "absent"
        out["services"].append(entry)
    return out


def _pull(image: str) -> None:
    """Fetch the image if it is missing locally (may take a moment)."""
    name, _, tag = image.partition(":")
    _api("POST", f"/images/create?fromImage={name}&tag={tag or 'latest'}", timeout=900)


def _gen_password(length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _create_volume(name: str) -> None:
    try:
        _api("POST", "/volumes/create", {"Name": name})
    except DockerError:
        pass   # already exists


def enable(key: str) -> dict:
    """
    Create and start the service. If it is already running, it is only started.
    Afterwards the matching values are available in the settings.
    """
    spec = SERVICES.get(key)
    if not spec:
        raise DockerError(f"Unknown service: {key}")
    if not is_docker():
        raise DockerError("This feature only exists in container operation.")
    if not socket_available():
        raise DockerError(
            "The Docker socket is not reachable. In docker-compose.yml, enable "
            "the line '/var/run/docker.sock:/var/run/docker.sock' under 'volumes' "
            "and run 'docker compose up -d'."
        )

    migrate_legacy()
    network = _self_network()
    name = spec["container"]
    existing = _find_container(name)

    if not existing:
        _pull(spec["image"])
        cfg: dict[str, Any] = {
            "Image": spec["image"],
            "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
        }
        if network:
            cfg["HostConfig"]["NetworkMode"] = network

        if key == "guacd":
            # Recordings should land in the same folder as the backend\047s.
            host_rec = _host_path_for("/app/src/backend/storage/recordings")
            if host_rec:
                cfg["HostConfig"]["Binds"] = [f"{host_rec}:/recordings"]
        elif key == "db":
            password = db.get_setting("docker_db_password") or _gen_password()
            root_pw = db.get_setting("docker_db_root_password") or _gen_password()
            old_vol = LEGACY["db"]["volume"]
            if not _volume_exists(spec["volume"]) and _volume_exists(old_vol):
                # Rename: take over the data from the old volume (the old one stays as a backup)
                _copy_volume(old_vol, spec["volume"], spec["image"])
            _create_volume(spec["volume"])
            cfg["Env"] = [
                f"MARIADB_ROOT_PASSWORD={root_pw}",
                f"MARIADB_DATABASE={db.get_setting('docker_db_name') or spec['database']}",
                f"MARIADB_USER={spec['user']}",
                f"MARIADB_PASSWORD={password}",
            ]
            cfg["HostConfig"]["Binds"] = [f"{spec['volume']}:/var/lib/mysql"]
            db.set_setting("docker_db_password", password)
            db.set_setting("docker_db_root_password", root_pw)

        _api("POST", f"/containers/create?name={name}", cfg, timeout=120)

    _api("POST", f"/containers/{name}/start", timeout=120)

    # --- Pre-fill the settings ---------------------------------------------
    if key == "guacd":
        # Addressable via the container name in the shared network.
        db.set_setting("guacd_host", name)
        db.set_setting("guacd_port", str(spec["port"]))
    elif key == "db":
        db.set_setting("docker_db_host", name)
        db.set_setting("docker_db_port", str(spec["port"]))
        db.set_setting("docker_db_user", spec["user"])
        # Keep an existing name: a taken-over database (e.g. from before the
        # rename: rapalle_rmm) keeps that name in the server.
        db.set_setting("docker_db_name", db.get_setting("docker_db_name") or spec["database"])

    return {"ok": True, "service": key, "container": name}


def disable(key: str, remove: bool = True) -> dict:
    """
    Stop the service (and remove the container). Data is kept: the database
    volume is deliberately NOT deleted, so an accidental shutdown destroys
    nothing.
    """
    spec = SERVICES.get(key)
    if not spec:
        raise DockerError(f"Unknown service: {key}")
    if not socket_available():
        raise DockerError("Docker socket not reachable.")

    name = spec["container"]
    if not _find_container(name):
        return {"ok": True, "service": key, "note": "was not present"}
    try:
        _api("POST", f"/containers/{name}/stop?t=10", timeout=60)
    except DockerError:
        pass
    if remove:
        _api("DELETE", f"/containers/{name}", timeout=60)
    return {"ok": True, "service": key}


def restart(key: str) -> dict:
    """Restart the service\047s container (without removing it)."""
    spec = SERVICES.get(key)
    if not spec:
        raise DockerError(f"Unknown service: {key}")
    if not socket_available():
        raise DockerError("Docker socket not reachable.")
    name = spec["container"]
    if not _find_container(name):
        raise DockerError("The container does not exist - start it first.")
    _api("POST", f"/containers/{name}/restart?t=10", timeout=90)
    return {"ok": True, "service": key}


def details(key: str) -> dict:
    """Monitoring data of a service container (state, runtime, restarts, health)."""
    spec = SERVICES.get(key)
    if not spec:
        raise DockerError(f"Unknown service: {key}")
    out = {"key": key, "label": spec["label"], "image": spec["image"], "container": spec["container"],
           "available": socket_available(), "exists": False, "running": False, "state": "unavailable"}
    if not out["available"]:
        return out
    try:
        info = _api("GET", f"/containers/{spec['container']}/json", timeout=15)
    except DockerError:
        out["state"] = "absent"
        return out
    st = info.get("State", {}) or {}
    out.update({
        "exists": True, "running": bool(st.get("Running")), "state": st.get("Status", "unknown"),
        "started_at": st.get("StartedAt"), "finished_at": st.get("FinishedAt"),
        "restart_count": info.get("RestartCount", 0), "exit_code": st.get("ExitCode"),
        "oom_killed": bool(st.get("OOMKilled")), "health": (st.get("Health") or {}).get("Status"),
        "error": st.get("Error") or "",
    })
    return out


def db_credentials() -> dict:
    """
    Credentials of the added database - pre-fills the form under
    Settings -> Database.
    """
    if not db.get_setting("docker_db_host"):
        return {}
    return {
        "type": "mysql",
        "host": db.get_setting("docker_db_host"),
        "port": int(db.get_setting("docker_db_port") or 3306),
        "user": db.get_setting("docker_db_user") or "rmm",
        "password": db.get_setting("docker_db_password") or "",
        "database": db.get_setting("docker_db_name") or "bytera_rmm",
    }
