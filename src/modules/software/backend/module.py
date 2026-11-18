# module.py (Modul "software") - Software-Rollout
def on_startup(ctx):
    # Antwort-Handler fuer Installations-Ergebnisse der Agents registrieren.
    from rmm_mod_software import rollout
    rollout.register_handlers()
    ctx.log("Software-Rollout bereit")
