# module.py (module "addir") - manage Active Directory (LDAP write access)
# No background task needed; connections are opened per request and closed again.
def on_startup(ctx):
    ctx.log("Active Directory management ready")
