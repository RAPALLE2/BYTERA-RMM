"""
routers/agent_update_routes.py
-------------------------------
Provides the latest version of the agent code so agents can update themselves.
On an "update-agent" command the agent downloads the file from
/api/agent/latest, replaces its own agent.py and restarts.

No auth needed for the download itself (the code is on every client anyway),
but the version is viewable via /api/agent/version.
"""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

router = APIRouter(prefix="/api/agent", tags=["agent"])

# Path to agent.py in the project (src/agent/main.py)
from app.paths import AGENT_DIR as _AGENT_DIR, AGENT_MAIN_FILE as _AGENT_PY, AGENT_VERSION_FILE as _AGENT_VERSION_FILE


def _agent_version() -> str:
    """
    Version of the shipped agent - the single source of truth is
    agent/version.txt (increment it there on agent changes). Read fresh on
    every call, so an update of the file takes effect without a backend restart
    (important for the later auto-update).
    """
    try:
        v = _AGENT_VERSION_FILE.read_text(encoding="utf-8").strip()
        return v or "0.0.0"
    except OSError:
        return "0.0.0"


@router.get("/version")
def agent_version():
    return {"version": _agent_version()}


@router.get("/latest", response_class=PlainTextResponse)
def agent_latest():
    """Returns the current agent source code as text."""
    try:
        code = _AGENT_PY.read_text(encoding="utf-8")
    except FileNotFoundError:
        return PlainTextResponse("# agent/main.py not found", status_code=404)
    return PlainTextResponse(code, media_type="text/x-python")
