"""
runtime_env.py
--------------
Detects HOW the backend runs:

  - "docker"  -> runs in a container (Docker / Podman / LXC image)
  - "native"  -> installed normally via "python run.py" on the host

Why this matters:
In a container some things are NO longer changeable at runtime, because they
are fixed at container start (port mapping, bound address, volumes). If one
changes e.g. PORT in the .env there, the dashboard is no longer reachable
afterwards - the container still maps the old port. Therefore the UI
(Settings -> Source) shows a hint.

The detection uses several signals, so it is correct even if one of them is
missing:
  1. environment variable RMM_INSTALL_KIND (set in the Dockerfile) - strongest signal
  2. marker file /app/.docker-install (created in the Dockerfile)
  3. /.dockerenv (created by Docker itself)
  4. "docker"/"containerd"/"kubepods" in /proc/1/cgroup or /proc/self/mountinfo
"""

import os
import platform
import sys
from pathlib import Path

# Marker file created by the Dockerfile (see Dockerfile in the project root).
_MARKER = Path("/app/.docker-install")


def _cgroup_says_container() -> bool:
    """Searches the kernel files for typical container traces."""
    for path in ("/proc/1/cgroup", "/proc/self/mountinfo"):
        try:
            text = Path(path).read_text(errors="ignore")
        except Exception:
            continue
        low = text.lower()
        if "docker" in low or "containerd" in low or "kubepods" in low or "/lxc/" in low:
            return True
    return False


def is_docker() -> bool:
    """True if the backend runs in a container."""
    kind = (os.getenv("RMM_INSTALL_KIND", "") or "").strip().lower()
    if kind == "docker":
        return True
    if kind == "native":
        return False
    if _MARKER.exists() or Path("/.dockerenv").exists():
        return True
    return _cgroup_says_container()


def install_kind() -> str:
    """'docker' or 'native' - the short key for the UI."""
    return "docker" if is_docker() else "native"


def runtime_info() -> dict:
    """
    Everything the UI needs to know about the installation kind.

    locked_settings = settings that in a container may NO longer be changed via
    .env, because they are tied to the container.
    """
    from app.config import PORT, HOST, GUACD_HOST, GUACD_PORT

    docker = is_docker()
    return {
        "install_kind": "docker" if docker else "native",
        "is_docker": docker,
        "container_name": os.getenv("HOSTNAME", "") if docker else "",
        "compose_project": os.getenv("COMPOSE_PROJECT_NAME", "") if docker else "",
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "port": PORT,
        "host": HOST,
        "guacd": f"{GUACD_HOST}:{GUACD_PORT}",
        # Hard-wired in the container -> changes in the .env have no effect or
        # lock one out. Natively everything is freely changeable.
        "locked_settings": ["PORT", "HOST"] if docker else [],
        "data_paths": {
            "database": "src/backend/data/data.sqlite",
            "files": "src/backend/storage",
            "recordings": "src/backend/storage/recordings",
            "media": "src/backend/storage/audio",
            "branding": "src/frontend/image",
        },
    }
