"""
auth.py
-------
Everything around login security:

1. Passwords are NEVER stored in plaintext, but as a "hash" (a one-way
   encryption) via the library "passlib".
2. After login the frontend gets a JWT ("JSON Web Token") - a signed text
   token containing the username + expiry. The frontend sends this token with
   every further request (header "Authorization: Bearer <token>"), so the
   backend knows who is making the request without checking the password again
   on each request.

SSO / directory login (Active Directory via LDAP/LDAPS):
   Besides the local login (authenticate_local) there is the directory login
   (authenticate_realm) in the MODULE "sso". If it is off, AD users cannot sign
   in and their sessions are rejected. Both ultimately deliver the same: a
   finished user dict. From there everything (create JWT, check permissions)
   runs via the same code - whether logged in locally or via AD. AD users get
   their rights via RMM groups named exactly like their AD groups (see
   sync_ad_user_groups in db.py).
"""

import time
import bcrypt
import jwt
from fastapi import Header, HTTPException

from app.config import JWT_SECRET, JWT_EXPIRE_HOURS
from app import db

# Note: we use the "bcrypt" library directly (instead of passlib). bcrypt has
# a technical limit of 72 bytes per password - longer passwords are cleanly
# trimmed to 72 bytes beforehand so there are no crashes (this is common with
# bcrypt and completely harmless, since 72 characters offer more than enough
# security for a password).


def _to_bytes(password: str) -> bytes:
    """Converts the password to bytes and trims to max. 72 bytes (bcrypt limit)."""
    return password.encode("utf-8")[:72]


def hash_password(plain_password: str) -> str:
    """Creates a secure hash from a plaintext password."""
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(_to_bytes(plain_password), salt)
    return hashed.decode("utf-8")  # store as text so it fits into the DB


def verify_password(plain_password: str, password_hash: str) -> bool:
    """Checks whether a plaintext password matches a stored hash."""
    try:
        return bcrypt.checkpw(_to_bytes(plain_password), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def authenticate_local(username: str, password: str) -> dict | None:
    """
    Checks username/password against the local database.
    Returns the user dict on success, otherwise None.
    """
    user = db.get_user_by_username(username)
    if not user:
        return None
    # API accounts (module "api") authenticate ONLY with an API key at the REST
    # API - never with a password, never in the dashboard. Generic message (no
    # probing).
    if user.get("role") == "api":
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    return user


# Directory login (LDAP/AD): module "sso" (src/modules/backend/sso/ldap_auth.py).

def sso_enabled() -> bool:
    """Is the module "sso" active? (lazy import - modules.py itself imports app.*)"""
    try:
        from app import modules
        return modules.is_enabled("sso")
    except Exception:
        return False


def authenticate_realm(username: str, password: str, realm_id: str) -> dict | None:
    """Login against a directory - only with the active module "sso"."""
    from app import modules
    ldap = modules.use("sso", "ldap_auth") if sso_enabled() else None
    if ldap is None:
        raise HTTPException(403, "Directory login (SSO) is disabled. "
                                 "Please sign in with a local account.")
    return ldap.authenticate_realm(username, password, realm_id)


def create_access_token(user: dict) -> str:
    """Creates a signed JWT for a logged-in user."""
    payload = {
        "sub": user["id"],              # "subject" = user ID
        "username": user["username"],
        "role": user["role"],
        "exp": int(time.time()) + JWT_EXPIRE_HOURS * 3600,  # expiry
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def decode_access_token(token: str) -> dict:
    """Checks a JWT for validity and returns its content."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Session expired, please sign in again")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")
    # Session of a client app (module "client"): the device must still be
    # registered. "Remove" under Profile -> My devices takes effect immediately
    # - for REST AND the live connection (both check here).
    dev = payload.get("dev")
    if dev:
        try:
            from app import modules
            devices = modules.use("client", "devices") if modules.is_enabled("client") else None
        except Exception:
            devices = None
        if devices is None or not devices.is_active(dev, payload.get("sub")):
            raise HTTPException(401, "This device was signed out")
    return payload


def get_current_user(authorization: str | None = Header(default=None)) -> dict:
    """
    FastAPI "dependency": simply add as a parameter to every protected route,
    e.g.:

        @router.get("/something-protected")
        async def route(user: dict = Depends(get_current_user)):
            ...

    FastAPI calls this function automatically, reads the Authorization header,
    checks the token, and passes the matching user to the route. If no valid
    token is present, a 401 error is returned automatically, BEFORE the actual
    route code runs at all.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Not signed in")
    token = authorization.removeprefix("Bearer ").strip()
    payload = decode_access_token(token)
    user = db.get_user_by_id(payload["sub"])
    if not user:
        raise HTTPException(401, "User no longer exists")
    # Directory accounts (AD) need the module "sso". If it is off, already
    # issued sessions no longer count either - the account itself remains.
    try:
        realm = user["auth_realm"] if "auth_realm" in user.keys() else None
    except Exception:
        realm = None
    if realm and not sso_enabled():
        raise HTTPException(401, "Directory login (SSO) is disabled.")
    return user


def require_admin(user: dict) -> None:
    """Small helper: throws an error if the user is not an admin."""
    # Full admin via ANY path (role 'admin', the global right 'admin' - also
    # via a group - or the legacy key 'super_admin'). Previously only the role
    # was checked -> someone who was admin via a group could not reach, e.g.,
    # Source -> Migration, although everything else worked.
    if not is_super_admin(user):
        raise HTTPException(403, "Administrators only")


def require_permission(user: dict, permission: str) -> None:
    """
    Throws a 403 error if the user does not have the given right. Admins always
    have all rights. Otherwise it is checked via the user's groups (see
    db.get_user_permissions).
    """
    perms = db.get_user_permissions(user["id"], user["role"])
    if permission not in perms:
        raise HTTPException(403, f"Missing right: {permission}")


def get_user_with_permissions(user: dict) -> dict:
    """Enriches a user dict with the list of its effective rights (for the frontend)."""
    perms = db.get_user_permissions(user["id"], user["role"])
    return {**user, "permissions": sorted(perms)}


# ==================================================================
# NEW, fine-grained permission system (tri-state grants)
# ==================================================================
# Resolution for a (perm, client_id):
#   1. specific right 'deny' (global OR client)  -> forbidden
#   2. 'admin' wildcard 'deny' (global/client)   -> forbidden
#   3. specific right 'allow'                    -> allowed
#   4. 'admin' wildcard 'allow'                  -> allowed
#   5. otherwise                                 -> forbidden (default-deny)
# The local super admin (role == 'admin') ALWAYS has all rights.


def is_super_admin(user: dict) -> bool:
    """
    Full administrator - may do everything (bypasses the resolver). Paths:
      1. role 'admin' (as before)
      2. the global right 'admin' (wildcard, granted via the rights management
         - also to groups)
      3. the legacy key 'super_admin' (formerly a second, equivalent checkbox;
         both are merged into 'admin', already granted grants still apply)
    An explicit 'deny' removes the right again - but the role 'admin' stays
    unaffected by it.
    """
    try:
        role = user.get("role")
    except AttributeError:
        role = user["role"]
    if role == "admin":
        return True
    # Recursion guard: _has_perm() does NOT ask is_super_admin() for these keys.
    return (_has_perm_raw(user, "admin", None)
            or _has_perm_raw(user, "super_admin", None))


def _aggregate_effect(grants: list[dict], perm: str, scopes: list[str]) -> str | None:
    """
    Aggregates all grants for 'perm' in the given scopes.
    'deny' beats 'allow'; no hit -> None.
    """
    found_allow = False
    for g in grants:
        if g["perm"] == perm and g["scope"] in scopes:
            if g["effect"] == "deny":
                return "deny"
            if g["effect"] == "allow":
                found_allow = True
    return "allow" if found_allow else None


def _pve_connected(client_id: str) -> bool:
    """Does this agent have a Proxmox connection? (module "proxmox"; fast, cached)"""
    try:
        from app import modules
        if not modules.is_enabled("proxmox"):
            return False
        return bool(modules.use("proxmox", "pve_store").is_connected(client_id))
    except Exception:
        return False


def _resolve(grants: list[dict], perm: str, client_id: str | None) -> bool:
    # Scopes of a client check:
    #   global                     -> applies everywhere
    #   DEFAULT_CLIENT_SCOPE ("*") -> "default client": applies to EVERY client
    #   client_id                  -> rights of exactly this client
    # 'deny' beats an 'allow' in ANY of these scopes. So a denial on a concrete
    # client overrides the allow from the default client.
    scopes = ["global"]
    if client_id:
        scopes += [db.DEFAULT_CLIENT_SCOPE]
        # "Default Proxmox" (pve:*) applies to Proxmox rights of an agent WITH a connected Proxmox
        if perm.startswith("pve_") and _pve_connected(client_id):
            scopes.append(db.DEFAULT_PVE_SCOPE)
        scopes.append(client_id)
    # 1./3. specific right. 'deny' on 'perm' itself beats everything.
    specific = _aggregate_effect(grants, perm, scopes)
    if specific == "deny":
        return False
    # Implications: an 'allow' on a stronger/older key satisfies 'perm'
    # automatically (e.g. old use_screen -> c_screen/c_screen_view).
    implied_allow = False
    for src in db.perms_implied_by(perm):
        if _aggregate_effect(grants, src, scopes) == "allow":
            implied_allow = True
            break
    # 2./4. admin wildcard
    admin = _aggregate_effect(grants, "admin", scopes)
    if admin == "deny":
        return False
    if specific == "allow":
        return True
    if implied_allow:
        return True
    if admin == "allow":
        return True
    return False


def _has_perm_raw(user: dict, perm: str, client_id: str | None = None) -> bool:
    """Pure grant resolution WITHOUT the super-admin bypass.
    Only for is_super_admin() itself - otherwise there would be an infinite
    loop (is_super_admin -> user_has_permission -> is_super_admin -> ...)."""
    try:
        return _resolve(db.get_effective_grants(user["id"]), perm, client_id)
    except Exception:
        return False


def user_has_permission(user: dict, perm: str, client_id: str | None = None) -> bool:
    """
    Effective permission check. With client_id, client AND global grants are
    considered. Client actions are additionally gated by 'access_clients'
    (without access to the client -> everything forbidden).
    """
    if is_super_admin(user):
        return True
    grants = db.get_effective_grants(user["id"])
    # Gate: for client-related actions the client must be accessible at all
    # (access_clients on global OR on this client).
    if client_id and perm != "access_clients":
        if not _resolve(grants, "access_clients", client_id):
            return False
    return _resolve(grants, perm, client_id)


def can_access_client(user: dict, client_id: str) -> bool:
    """Is this client visible/accessible to the user?"""
    if is_super_admin(user):
        return True
    grants = db.get_effective_grants(user["id"])
    return _resolve(grants, "access_clients", client_id)


def visible_client_ids(user: dict, all_client_ids: list[str]) -> set[str]:
    """Filters a client-ID list down to those visible to the user."""
    if is_super_admin(user):
        return set(all_client_ids)
    grants = db.get_effective_grants(user["id"])
    return {cid for cid in all_client_ids if _resolve(grants, "access_clients", cid)}


def require_perm(user: dict, perm: str, client_id: str | None = None) -> None:
    """Throws 403 if the right is missing."""
    if not user_has_permission(user, perm, client_id):
        raise HTTPException(403, f"Missing right: {perm}")


def effective_permissions(user: dict, client_ids: list[str]) -> dict:
    """
    Builds the rights overview for the frontend:
      { admin, global: {perm: bool}, clients: { client_id: {perm: bool} } }
    Contains only VISIBLE clients (access_clients).
    """
    if is_super_admin(user):
        g = {p: True for p in db.PERM_KEYS}
        clients = {cid: {p: True for p in db.CLIENT_PERM_KEYS} for cid in client_ids}
        return {"admin": True, "global": g, "clients": clients}

    global_map = {p: user_has_permission(user, p) for p in db.PERM_KEYS}
    clients_map: dict[str, dict] = {}
    for cid in client_ids:
        if not can_access_client(user, cid):
            continue  # do not even include invisible clients
        clients_map[cid] = {p: user_has_permission(user, p, cid) for p in db.CLIENT_PERM_KEYS}
    return {"admin": False, "global": global_map, "clients": clients_map}
