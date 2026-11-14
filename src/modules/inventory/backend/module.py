# module.py (Modul "inventory") - Einstiegspunkt
# -----------------------------------------------
# Legt beim Start die Tabellen an (CREATE TABLE IF NOT EXISTS).
# Die Daten bleiben beim Deaktivieren des Moduls erhalten.

from rmm_mod_inventory.inventory_routes import ensure_tables


async def on_startup(ctx):
    ensure_tables()
    ctx.log("Inventar bereit")
