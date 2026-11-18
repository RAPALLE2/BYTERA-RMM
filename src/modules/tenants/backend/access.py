# access.py (module "tenants") - who may see/open which tenant?
# ---------------------------------------------------------------------
# The FINE-grained permissions live on the tenant backend itself. Here only
# who may see and open a tenant in the main RMM is controlled:
# empty allow list = visible to everyone; otherwise only the named users/groups.
# Super admins always see everything.
from __future__ import annotations

from app import db
from app.auth import is_super_admin


def may_open(user: dict, tenant: dict) -> bool:
    if is_super_admin(user):
        return True
    allow = tenant.get("allow") or []
    if not allow:
        return True
    gids = set(db.get_user_group_ids(user["id"]))
    for a in allow:
        if a.get("type") == "user" and a.get("id") == user["id"]:
            return True
        if a.get("type") == "group" and a.get("id") in gids:
            return True
    return False


def visible_tenants(user: dict, tenants: list[dict]) -> list[dict]:
    return [t for t in tenants if may_open(user, t)]
