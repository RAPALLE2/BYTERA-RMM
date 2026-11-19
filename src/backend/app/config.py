"""
config.py
---------
Reads all settings from the .env file (or environment variables) and provides
them as simple Python constants for the rest of the app.

This way no other part of the code needs to know HOW the configuration is
loaded - it simply imports e.g. "from app.config import AGENT_TOKEN".
"""

import os
from dotenv import load_dotenv

# Loads the file ".env" from backend/data/ (if present)
from app.paths import ENV_FILE
load_dotenv(ENV_FILE)

# Port the server runs on
PORT: int = int(os.getenv("PORT", "4000"))


def _detect_local_ip() -> str:
    """
    Determines the local LAN IP of this machine (the IP via which other devices
    reach the backend). No packet is actually sent - the UDP connect only
    queries the routing. Fallbacks: hostname resolution, lastly 0.0.0.0 (all
    interfaces) if nothing works.
    """
    import socket as _socket
    try:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    try:
        ip = _socket.gethostbyname(_socket.gethostname())
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return "0.0.0.0"


# Address the backend is bound to. On a fresh install (no HOST variable in the
# .env) the machine's local IP is used automatically - instead of 0.0.0.0 /
# localhost / 127.0.0.1. In the .env, HOST can be set fixed at any time (e.g.
# HOST=0.0.0.0 for all interfaces).
HOST: str = (os.getenv("HOST", "") or "").strip() or _detect_local_ip()

# Token that every agent must send when connecting
AGENT_TOKEN: str = os.getenv("AGENT_TOKEN", "change-me-super-secret")

# Secret for signing the login tokens (JWT)
JWT_SECRET: str = os.getenv("JWT_SECRET", "change-me-jwt-secret")

# How long a login token is valid (in hours)
JWT_EXPIRE_HOURS: int = 12

# Allowed origin for browser requests (CORS)
CORS_ORIGIN: str = os.getenv("CORS_ORIGIN", "*")

# Apache Guacamole: address of the guacd daemon (proxy that speaks RDP/VNC/SSH).
# The default suits a locally running guacd (e.g. via Docker on port 4822).
GUACD_HOST: str = os.getenv("GUACD_HOST", "127.0.0.1")
GUACD_PORT: int = int(os.getenv("GUACD_PORT", "4822"))
