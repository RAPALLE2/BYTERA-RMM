"""
routers/audit_routes.py
--------------------------
Shows the audit log (who did what and when - logins, terminal commands, client
changes, user changes). Retention: 30 days, see db.cleanup_old_audit_entries(),
which runs at start and periodically afterwards.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app import db
from app.auth import get_current_user, require_perm

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("")
def get_audit_log(limit: int = 200, user: dict = Depends(get_current_user)):
    require_perm(user, "see_audit")
    return db.list_audit_log(limit)


class ClientErrorBody(BaseModel):
    message: str
    level: str = "error"     # "error" | "warn" | "info"
    context: str | None = None   # e.g. "vnc", "terminal", "explorer"
    action: str | None = None    # optional, allowed action (e.g. crash report)


# Only these actions reported by the frontend are allowed - this lets the
# frontend write crash reports (analogous to the agents) with a meaningful
# action into the audit log, without allowing arbitrary action names.
_ALLOWED_CLIENT_ACTIONS = {"frontend.crash_recovered", "frontend.crash"}


@router.post("/log-error")
def log_client_error(body: ClientErrorBody, user: dict = Depends(get_current_user)):
    """
    Lets the frontend also record important errors (shown to the user as a
    notification) permanently in the audit log.
    """
    if body.action in _ALLOWED_CLIENT_ACTIONS:
        action = body.action
    else:
        action = "error.warn" if body.level == "warn" else "error.reported"
    details = f"[{body.context}] {body.message}" if body.context else body.message
    db.add_audit_entry(user["username"], action, details=details[:500])
    return {"ok": True}
