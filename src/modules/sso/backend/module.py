# module.py (module "sso")
# -----------------------
# Directory login (LDAP/Active Directory).
#
# Disabling is only allowed when at least one local, active admin exists
# (guard.local_admins). This also applies to the automatic disable after a
# crash (hook "before_disable" = veto).
#
# After disabling, AD users, AD groups and realms remain in the database;
# AD users simply can no longer sign in and running sessions are rejected
# (app/auth.py -> get_current_user).

from . import guard


def can_disable(ctx):
    if not guard.local_admins():
        return ("There is no local, active admin account. Without SSO nobody could sign in "
                "afterwards. Please first create a local user with a password and the role "
                "'admin' or the 'Full Admin' permission.")
    return None


def on_disable(ctx):
    ctx.log("SSO disabled - AD users can no longer sign in (accounts remain).")
