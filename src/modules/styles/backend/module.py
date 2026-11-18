# module.py (module "styles") - entry point
# -------------------------------------------
# Styles are pure frontend files under frontend/styles/<folder>/. The backend
# only reads their style.json (registry.py) so that profile/settings can offer
# the choice and stored values can be validated.

from rmm_mod_styles import registry


async def on_startup(ctx):
    ids = [s["id"] for s in registry.styles()]
    ctx.log(f"{len(ids)} Style(s): {', '.join(ids) or '-'}")
