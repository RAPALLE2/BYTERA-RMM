"""
routers/auth_routes.py
-----------------------
Everything around login and the user's own profile.

Endpoints:
  POST /api/auth/login            -> sign in, returns a JWT token
  POST /api/auth/change-password  -> change your own password (also for the
                                      mandatory change on first login)
  GET  /api/auth/me               -> info about the currently logged-in user
  PUT  /api/auth/profile          -> change your own display name/language/theme
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app import db
from app.auth import (
    authenticate_local,
    authenticate_realm,
    create_access_token,
    get_current_user,
    hash_password,
    verify_password,
    is_super_admin,
    user_has_permission,
    effective_permissions,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str
    password: str
    realm: str = "local"   # "local" or a realm ID (AD)
    code: str = ""         # one-time code of the authenticator app (if enabled)


class ChangePasswordBody(BaseModel):
    current_password: str
    new_password: str


class ProfileBody(BaseModel):
    display_name: str | None = None
    language: str | None = None   # "de" or "en"
    theme: str | None = None      # "dark" or "light"
    accent: str | None = None     # color palette, e.g. "teal", "violet", ...
    email: str | None = None
    design: str | None = None     # style ID ("futuristic" = core, more from module "styles") | "" (= installation default)


def _auth_realm_of(user) -> str | None:
    """Realm ID of the user (AD/LDAP/SSO) or None for a local account.
    Works for dict AND sqlite3.Row."""
    try:
        return user["auth_realm"] if "auth_realm" in user.keys() else None
    except Exception:
        return None


def _public_user(user: dict) -> dict:
    """Returns only the fields the frontend is allowed to see (no password hash!)."""
    return {
        "id": user["id"],
        "username": user["username"],
        "display_name": user["display_name"],
        "role": user["role"],
        "must_change_pw": bool(user["must_change_pw"]),
        "is_admin": is_super_admin(user),
        # Where does full access come from? "role" = admin role (as on creation),
        # "permission" = granted via the 'super_admin' permission, otherwise None.
        "admin_via": ("role" if user["role"] == "admin"
                      else ("permission" if is_super_admin(user) else None)),
        "language": user["language"],
        "theme": user["theme"],
        "accent": user["accent"] if "accent" in user.keys() else "teal",
        # UI design: design = what applies, design_choice = own choice ("" = default)
        "design": db.effective_design(user),
        "design_choice": (user["design"] if "design" in user.keys() else "") or "",
        "default_design": db.get_setting("default_design", "futuristic") or "futuristic",
        "email": (user["email"] if "email" in user.keys() else None) or "",
        # Realm ID (AD/LDAP/SSO) or null for a local account - the frontend
        # uses it to hide the change-password function, among other things.
        "auth_realm": _auth_realm_of(user),
    }


@router.get("/languages")
def languages():
    """All available languages (from all lang folders) - without login, because
    the login window itself should already be translated."""
    from app import i18n_loader
    return {"languages": i18n_loader.available()}


@router.get("/lang/{code}")
def language_strings(code: str):
    """All translations of a language (merged from core + modules)."""
    from app import i18n_loader
    if not i18n_loader.has(code):
        raise HTTPException(404, "Unknown language")
    return {"code": code, "strings": i18n_loader.strings(code)}


@router.get("/realms")
def public_realms():
    """
    Public list of sign-in realms for the dropdown in the login window.
    Contains only id + name (no secrets). "local" is added in the frontend.
    Without the active "sso" module there is no directory login -> empty list.
    """
    from app.auth import sso_enabled
    if not sso_enabled():
        return []
    return db.get_public_realms()


@router.post("/login")
async def login(body: LoginBody, request: Request = None):
    # --- Protection against password guessing (BSI ORP.4.A9) --------------
    # The lock is evaluated BEFORE the password check: otherwise one could keep
    # trying indefinitely despite the lock and draw conclusions from the
    # response times.
    client_ip = ""
    try:
        client_ip = (request.client.host if request and request.client else "") or ""
    except Exception:
        pass

    from app import security_policy
    blocked, remaining = security_policy.lock_status(body.username, client_ip)
    if blocked:
        db.add_audit_entry(body.username, "login.blocked",
                           details=f"locked, {remaining}s left, ip:{client_ip}")
        raise HTTPException(
            429,
            f"Too many failed attempts. Please try again in {max(1, remaining // 60)} "
            f"minutes.")

    # Local sign-in or sign-in against a directory (AD)?
    if body.realm and body.realm != "local":
        user = authenticate_realm(body.username, body.password, body.realm)
    else:
        user = authenticate_local(body.username, body.password)

    if not user:
        locked_now, lock_secs = security_policy.note_failure(body.username, client_ip)
        db.add_audit_entry(
            body.username, "login.failed",
            details=f"realm:{body.realm}, ip:{client_ip}"
                    + (f", ACCOUNT LOCKED for {lock_secs // 60} minutes" if locked_now else ""))
        # Notification rule: failed sign-in
        try:
            from app import notifier
            from router.admin_routes import build_notification
            await notifier.fire_event("login_failed",
                notification=build_notification(
                    f"Failed sign-in for '{body.username}'.",
                    head="Failed sign-in",
                    service="Auth", level="warn"),
                dedupe_key=f"login_failed:{body.username}")
        except Exception:
            pass
        if locked_now:
            raise HTTPException(
                429,
                f"Too many failed attempts - access is locked for "
                f"{lock_secs // 60} minutes.")
        raise HTTPException(401, "Username or password is wrong")

    # --- Second factor (TOTP) --------------------------------------------
    # Only AFTER a correct password: otherwise one could find out which
    # usernames exist via the follow-up prompt.
    if user.get("totp_enabled"):
        from app import totp as _totp
        code = (body.code or "").strip()
        if not code:
            # Not an error but a follow-up prompt: the UI then shows the code
            # field and sends the sign-in again.
            raise HTTPException(401, "2fa_required")

        ok = _totp.verify(user.get("totp_secret") or "", code,
                          user_key=str(user["id"]))
        if not ok:
            # Recovery code? It is consumed in the process.
            used, rest = _totp.use_backup_code(user.get("totp_backup") or "", code)
            if used:
                db.set_user_totp(user["id"], backup=rest)
                db.add_audit_entry(user["username"], "login.2fa_backup_used",
                                   details=f"remaining: {len([x for x in rest.split(',') if x])}")
                ok = True

        if not ok:
            security_policy.note_failure(body.username, client_ip)
            db.add_audit_entry(user["username"], "login.2fa_failed",
                               details=f"ip:{client_ip}")
            raise HTTPException(401, "The one-time code is wrong or has expired")

    # Permission check: anyone without the 'login' permission (and not a super admin)
    # may NOT sign in, even with a correct password.
    if not user_has_permission(user, "login"):
        db.add_audit_entry(user["username"], "login.denied", details="missing 'login' permission")
        raise HTTPException(403, "No permission to sign in to this system")

    security_policy.note_success(body.username, client_ip)
    db.add_audit_entry(user["username"], "login.success",
                       details=f"realm:{body.realm}, ip:{client_ip}")
    # Notification rule: user sign-in
    try:
        from app import notifier
        from router.admin_routes import build_notification
        await notifier.fire_event("user_login",
            notification=build_notification(
                f"{user['username']} signed in to the dashboard.",
                head=f"Sign-in - {user['username']}",
                service="Auth", level="info"),
            dedupe_key=f"user_login:{user['username']}")
    except Exception:
        pass
    # Derive and store the HA1 for the network drive (digest) from the plaintext
    # password, so the user can sign in to the relay with their normal password.
    try:
        db.store_relay_secret(user["id"], body.password)
    except Exception:
        pass
    token = create_access_token(user)
    return {"token": token, "user": _public_user(user)}


@router.post("/change-password")
def change_password(body: ChangePasswordBody, user: dict = Depends(get_current_user)):
    # Directory users (AD/LDAP/SSO, recognizable by auth_realm) have NO local
    # password - the hash is only a placeholder. A "change" would create a
    # local shadow password that works for login bypassing the directory.
    # So block it hard; it is changed in AD/LDAP itself.
    if _auth_realm_of(user):
        raise HTTPException(403, "The password is managed centrally in the directory (AD/LDAP/SSO) and cannot be changed here")
    # API accounts (module "api") have no password, but an API key instead.
    if user.get("role") == "api":
        raise HTTPException(403, "API accounts have no password - use an API key instead (module \"REST API\")")
    # On the very first mandatory password change, current_password="admin" (or similar) must be checked
    if not verify_password(body.current_password, user["password_hash"]):
        raise HTTPException(400, "Current password is wrong")
    # Enforce the password policy on the server side (BSI ORP.4.A8/A22).
    # The fixed limit of 8 characters is not enough for this; the applicable
    # values are in the settings and are documented.
    from app import security_policy
    problems = security_policy.check_password(body.new_password, user["username"])
    if problems:
        raise HTTPException(
            400, "The password does not meet the policy: " + "; ".join(problems))
    if body.new_password == body.current_password:
        raise HTTPException(400, "The new password must differ from the old one")

    db.update_user_password(user["id"], hash_password(body.new_password), must_change_pw=False)
    # Update the HA1 for the network drive with the new password.
    try:
        db.store_relay_secret(user["id"], body.new_password)
    except Exception:
        pass
    db.add_audit_entry(user["username"], "password.changed", target=user["username"])
    return {"ok": True}


@router.get("/me")
def me(user: dict = Depends(get_current_user)):
    return _public_user(user)


@router.get("/effective")
def my_effective_permissions(user: dict = Depends(get_current_user)):
    """
    Effective permissions of the logged-in user for frontend gating:
    { admin, global: {perm: bool}, clients: { client_id: {perm: bool} } }.
    Contains only visible clients.
    """
    all_ids = [c["id"] for c in db.list_clients()]
    return effective_permissions(user, all_ids)


# ------------------------------------------------------------------
# Server-side stored UI settings (layouts, favorites, language, icon mode, ...).
# This way ALL browsers of the same user look identical - instead of every
# browser installation (Firefox/Edge/...) having its own localStorage state.
# ------------------------------------------------------------------

class UiPrefsBody(BaseModel):
    # { "keys": { "<localStorage key>": "<value as string>" } }
    keys: dict[str, str] = {}


@router.get("/ui-prefs")
def get_ui_prefs(user: dict = Depends(get_current_user)):
    # Reads from the user_prefs table (one row per key). Old installations with
    # the former blob are migrated automatically, see db.get_user_prefs().
    return {"keys": db.get_user_prefs(user["id"])}


@router.put("/ui-prefs")
def put_ui_prefs(body: UiPrefsBody, user: dict = Depends(get_current_user)):
    # Legacy route: replaces the whole set. The frontend uses PATCH.
    try:
        old = set(db.get_user_prefs(user["id"]).keys())
        payload = {k: v for k, v in (body.keys or {}).items()}
        for k in old - set(payload.keys()):
            payload[k] = None
        db.merge_user_prefs(user["id"], payload)
    except ValueError as e:
        raise HTTPException(413, str(e))
    return {"ok": True}


class UiPrefsPatchBody(BaseModel):
    # { "keys": { "<key>": "<value>" | null } }  - null deletes the key.
    keys: dict[str, str | None] = {}


@router.patch("/ui-prefs")
def patch_ui_prefs(body: UiPrefsPatchBody, user: dict = Depends(get_current_user)):
    """
    Partial update: only the passed keys are changed, all others stay untouched.
    The frontend uses exclusively this path so that a freshly opened browser
    (empty localStorage) can never overwrite the state stored in the database.
    """
    try:
        db.merge_user_prefs(user["id"], body.keys or {})
    except ValueError as e:
        raise HTTPException(413, str(e))
    return {"ok": True, "bytes": db.user_prefs_size(user["id"])}


# ------------------------------------------------------------------
# Silent mode for the remote screen (one-off): the NEXT session starts
# without a consent dialog on the device, afterwards the mode switches
# off again automatically. Requires the 'screen_silent' permission (global)
# or 'c_screen_silent' on at least one visible client.
# ------------------------------------------------------------------

def _may_use_silent_screen(user: dict) -> bool:
    if is_super_admin(user) or user_has_permission(user, "screen_silent"):
        return True
    from app.auth import visible_client_ids
    all_ids = [c["id"] for c in db.list_clients()]
    for cid in visible_client_ids(user, all_ids):
        if user_has_permission(user, "c_screen_silent", cid):
            return True
    return False


class SilentScreenBody(BaseModel):
    enabled: bool


@router.get("/silent-screen")
def get_silent_screen(user: dict = Depends(get_current_user)):
    return {"enabled": bool(user.get("silent_screen")),
            "allowed": _may_use_silent_screen(user)}


# ------------------------------------------------------------------
# Spotify integration: every logged-in user may read the client ID of the
# Spotify app (configured by the admin in the settings) - it is not a secret
# (it is in every OAuth URL). The login runs entirely in the browser via
# authorization code + PKCE, there is NO client secret.
# ------------------------------------------------------------------

@router.get("/spotify-config")
def spotify_config(user: dict = Depends(get_current_user)):
    # The redirect URI is NOT maintained separately but derived from the
    # "full URL" (Settings -> General -> server_url). This way there is exactly
    # ONE place that holds the public address. If nothing is set there, the
    # frontend uses the address under which the dashboard is currently open.
    # IMPORTANT: Spotify compares CHARACTER-EXACT. Therefore the configured
    # URL is passed on exactly as stored - only multiple trailing slashes are
    # reduced to one. If we forced a trailing slash here, it would no longer
    # match a Spotify entry without a slash (and vice versa) -> "Not matching
    # configuration".
    base = (db.get_setting("server_url", "") or "").strip()
    if base.endswith("/"):
        base = base.rstrip("/") + "/"
    return {
        "client_id": db.get_setting("spotify_client_id", "") or "",
        "redirect_uri": base,
    }


@router.put("/silent-screen")
def put_silent_screen(body: SilentScreenBody, user: dict = Depends(get_current_user)):
    if body.enabled and not _may_use_silent_screen(user):
        raise HTTPException(403, "No permission for the remote screen without a request")
    db.set_user_silent_screen(user["id"], body.enabled)
    db.add_audit_entry(user["username"],
                       "screen.silent_" + ("armed" if body.enabled else "disarmed"))
    return {"ok": True, "enabled": body.enabled}


@router.put("/profile")
def update_profile(body: ProfileBody, user: dict = Depends(get_current_user)):
    # Change the display name only if the 'edit_profile_name' permission is present.
    # Language/theme/accent are personal presentation and always remain allowed.
    new_name = body.display_name
    if (new_name is not None and new_name != user.get("display_name")
            and not (is_super_admin(user) or user_has_permission(user, "edit_profile_name"))):
        new_name = user.get("display_name")
    email = body.email
    if email is not None:
        email = email.strip()
        if email:
            import re as _re
            if not _re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                raise HTTPException(400, "Invalid e-mail address")
            other = db.get_user_by_email(email)
            if other and other["id"] != user["id"]:
                raise HTTPException(400, "This e-mail address is already in use")
    db.update_user_profile(user["id"], new_name, body.language, body.theme, body.accent, email=email,
                           design=body.design)
    return _public_user(db.get_user_by_id(user["id"]))


@router.get("/password-policy")
def password_policy():
    """
    The applicable password policy - retrievable without signing in, because it
    is already needed during the enforced first password change. It reveals
    nothing worth protecting, only the rules that apply when saving anyway.
    """
    from app import security_policy
    return {"policy": security_policy.policy(),
            "text": security_policy.describe_policy()}


# ==================================================================
# TWO-FACTOR SIGN-IN (TOTP)
# ------------------------------------------------------------------
# Every user sets it up themselves in their profile:
#   1. POST /api/auth/2fa/setup    -> secret + QR code (NOT active yet)
#   2. the authenticator app scans the QR code
#   3. POST /api/auth/2fa/activate -> a code from the app confirms the setup
#      and returns the recovery codes once
#   4. POST /api/auth/2fa/disable  -> turn off (password required)
#
# The second step is important: only once a code from the app really matches is
# sign-in switched over to it. Otherwise someone whose app never took the
# secret could lock themselves out.
# ==================================================================

class TotpCodeBody(BaseModel):
    code: str = ""


class TotpDisableBody(BaseModel):
    password: str = ""
    code: str = ""       # alternative: current code from the app


def _confirm_identity(user: dict, password: str = "", code: str = "") -> None:
    """
    Confirms that the account owner is really at the device - not just an open
    browser.

    Important: accounts from a directory (AD/LDAP/SSO) have NO local password at
    all. Previously this stubbornly checked against the local hash, which is
    empty for these accounts - so the answer was always "password wrong", no
    matter what was entered. Now the rule is:

      * local account     -> local password OR current code from the app
      * directory account -> the password is checked against the directory,
                             alternatively the code from the app is enough
    """
    from app import totp as _totp

    # 1) Code from the authenticator app - works for both account types
    #    and is the stronger proof, because it requires the device.
    if code and user.get("totp_secret"):
        if _totp.verify(user["totp_secret"], code, user_key=str(user["id"])):
            return
        used, rest = _totp.use_backup_code(user.get("totp_backup") or "", code)
        if used:
            db.set_user_totp(user["id"], backup=rest)
            # Update the passed object too: it comes from the sign-in and
            # would otherwise still show the old state within the same call -
            # the code could then be used a second time.
            user["totp_backup"] = rest
            return

    realm = user.get("auth_realm")
    if realm:
        # 2a) Directory account: have the password checked there.
        if password:
            try:
                if authenticate_realm(user["username"], password, realm):
                    return
            except Exception:
                pass          # directory not reachable -> error below
        raise HTTPException(
            400,
            "Confirmation failed. This account is managed via a directory "
            "(AD/LDAP/SSO) - please enter the directory password or instead a "
            "current code from the authenticator app.")

    # 2b) Local account
    if password and user.get("password_hash") and verify_password(password, user["password_hash"]):
        return
    raise HTTPException(400, "Password or code is not correct.")


@router.get("/2fa/status")
def totp_status(user: dict = Depends(get_current_user)):
    backup = (user.get("totp_backup") or "")
    return {
        "enabled": bool(user.get("totp_enabled")),
        "backup_left": len([h for h in backup.split(",") if h]),
        # Tells the UI what to ask for: directory accounts have no local
        # password.
        "realm": user.get("auth_realm") or "",
    }


@router.post("/2fa/setup")
def totp_setup(user: dict = Depends(get_current_user)):
    """
    Generate a new secret and return it as a QR code. Not active yet - sign-in
    stays unchanged until the next step is confirmed.
    """
    from app import totp as _totp
    secret = _totp.new_secret()
    db.set_user_totp(user["id"], secret=secret, enabled=False)
    uri = _totp.provisioning_uri(secret, user["username"])
    qr = _totp.qr_data_uri(uri)
    db.add_audit_entry(user["username"], "2fa.setup_started")
    return {
        "secret": secret,
        "uri": uri,
        "qr": qr,          # None -> the UI shows the secret to type in manually
        "digits": _totp.DIGITS,
        "period": _totp.PERIOD,
    }


@router.post("/2fa/activate")
def totp_activate(body: TotpCodeBody, user: dict = Depends(get_current_user)):
    from app import totp as _totp
    secret = user.get("totp_secret")
    if not secret:
        raise HTTPException(400, "Please start the setup first.")
    if not _totp.verify(secret, body.code, user_key=str(user["id"])):
        raise HTTPException(400, "The code is wrong. Check the device time "
                                 "and enter the current code from the app.")
    codes = _totp.new_backup_codes()
    db.set_user_totp(user["id"], enabled=True, backup=_totp.hash_backup_codes(codes))
    db.add_audit_entry(user["username"], "2fa.enabled")
    # The plaintext codes exist EXACTLY ONCE - afterwards only hashes remain.
    return {"ok": True, "backup_codes": codes}


@router.post("/2fa/disable")
def totp_disable(body: TotpDisableBody, user: dict = Depends(get_current_user)):
    """Turn off only with your own password OR code - an open browser is not enough."""
    _confirm_identity(user, body.password, body.code)
    db.set_user_totp(user["id"], secret="", enabled=False, backup="")
    db.add_audit_entry(user["username"], "2fa.disabled")
    return {"ok": True}


@router.post("/2fa/backup-codes")
def totp_new_backup_codes(body: TotpDisableBody,
                                user: dict = Depends(get_current_user)):
    """Generate new recovery codes; the old ones expire in the process."""
    if not user.get("totp_enabled"):
        raise HTTPException(400, "Two-factor sign-in is not active")
    _confirm_identity(user, body.password, body.code)
    from app import totp as _totp
    codes = _totp.new_backup_codes()
    db.set_user_totp(user["id"], backup=_totp.hash_backup_codes(codes))
    db.add_audit_entry(user["username"], "2fa.backup_codes_renewed")
    return {"ok": True, "backup_codes": codes}
