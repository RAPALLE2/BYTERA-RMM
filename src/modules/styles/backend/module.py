# module.py (Modul "styles") - Einstiegspunkt
# -------------------------------------------
# Styles sind reine Frontend-Dateien unter frontend/styles/<ordner>/. Das
# Backend liest nur ihre style.json (registry.py), damit Profil/Einstellungen
# die Auswahl anbieten und gespeicherte Werte geprueft werden koennen.

from rmm_mod_styles import registry


async def on_startup(ctx):
    ids = [s["id"] for s in registry.styles()]
    ctx.log(f"{len(ids)} Style(s): {', '.join(ids) or '-'}")
