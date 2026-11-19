"""
routers/users_routes.py
-------------------------
User management for the settings app in the dashboard. Only admins may do this.

Endpoints:
  GET    /api/users        -> list all users
  POST   /api/users        -> create a new user
                               (with a choice of "one-time password" vs.
                               "set a final password directly")
  DELETE /api/users/{id}   -> delete a user
"""

import secrets
import string

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import db
from app.auth import get_current_user, hash_password, require_admin, require_perm, is_super_admin

router = APIRouter(prefix="/api/users", tags=["users"])


class CreateUserBody(BaseModel):
    username: str
    display_name: str
    # "admin" (full access), "support" (support default group) or "viewer".
    # The role only controls which default groups are assigned automatically
    # (see db.auto_assign_groups) - the rights themselves hang on the groups or
    # the grants.
    role: str = "admin"
    email: str | None = None
    password: str | None = None    # if None -> automatically generated one-time password
    one_time_password: bool = True  # True = user MUST set their own password at first login


def _generate_one_time_password(length: int = 12) -> str:
    """Creates a random but easy-to-read one-time password."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


@router.get("")
def get_users(user: dict = Depends(get_current_user)):
    require_perm(user, "see_permissions")
    users = db.list_users()
    out = []
    for u in users:
        row = {k: v for k, v in u.items() if k != "password_hash"}
        # EFFECTIVE admin status: role 'admin' OR the right 'super_admin'
        # (also inherited via a group). The frontend then shows the ADMIN tag -
        # super_admin is thus equivalent to the role.
        row["is_admin"] = is_super_admin(u)
        row["admin_via"] = ("role" if u.get("role") == "admin"
                            else ("permission" if row["is_admin"] else None))
        out.append(row)
    return out


@router.post("")
def create_user(body: CreateUserBody, user: dict = Depends(get_current_user)):
    require_perm(user, "create_users")

    if db.get_user_by_username(body.username):
        raise HTTPException(400, "Username already exists")

    if body.email:
        import re as _re
        if not _re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", body.email.strip()):
            raise HTTPException(400, "Invalid email address")
        existing = db.get_user_by_email(body.email.strip())
        if existing:
            raise HTTPException(400, "This email address is already in use")

    # "api" = template "API user" (module "api" creates the matching group)
    if body.role not in ("admin", "support", "viewer", "api"):
        raise HTTPException(400, "Unknown role (allowed: admin, support, viewer)")

    # API account (template "API user", module "api"): NO password, NO first
    # login - instead a JWT-signed API key that is shown exactly once. A
    # password exists only internally (random, unknown); a password login is
    # blocked for API accounts anyway (auth.authenticate_local).
    if body.role == "api":
        from app import modules
        apikeys = modules.use("api", "apikeys") if modules.is_enabled("api") else None
        if apikeys is None:
            raise HTTPException(409, "API accounts need the \"REST API\" module")
        import secrets as _secrets
        new_user = db.create_user(username=body.username, password_hash=hash_password(_secrets.token_urlsafe(48)),
                                  display_name=body.display_name, role="api", must_change_pw=False,
                                  email=body.email)
        key = apikeys.generate(new_user)
        db.add_audit_entry(user["username"], "user.created", target=body.username, details="API account")
        return {"id": new_user["id"], "username": new_user["username"], "generated_password": None, "api_key": key}

    # Either use the password provided by the admin, or generate one
    plain_password = body.password or _generate_one_time_password()
    # Self-set passwords must meet the policy (ORP.4.A8). Automatically
    # generated one-time passwords are exempt: they are replaced immediately at
    # the first login anyway.
    if body.password:
        from app import security_policy
        problems = security_policy.check_password(body.password, body.username)
        if problems:
            raise HTTPException(
                400, "The password does not meet the policy: " + "; ".join(problems))

    new_user = db.create_user(
        username=body.username,
        password_hash=hash_password(plain_password),
        display_name=body.display_name,
        role=body.role,
        must_change_pw=body.one_time_password,
        email=body.email,
    )
    db.add_audit_entry(user["username"], "user.created", target=body.username)

    # We return the plaintext password EXACTLY ONCE (right after creation), so
    # the admin can tell it to the new user. It is not stored or shown again
    # anywhere else.
    return {
        "id": new_user["id"],
        "username": new_user["username"],
        "generated_password": plain_password if body.one_time_password or not body.password else None,
    }


@router.delete("/{user_id}")
def remove_user(user_id: str, user: dict = Depends(get_current_user)):
    require_perm(user, "create_users")
    if user_id == user["id"]:
        raise HTTPException(400, "You cannot delete yourself")
    gone = db.get_user_by_id(user_id) or {}
    db.delete_user(user_id)
    db.add_audit_entry(user["username"], "user.deleted", target=user_id)
    # Let modules clean up (e.g. "client": sign out devices)
    try:
        from app import modules
        modules.emit_nowait("user_deleted", user_id=user_id, username=gone.get("username", ""))
    except Exception:
        pass
    return {"ok": True}
