# module.py (Modul "topology") - Einstiegspunkt
# ----------------------------------------------
# Legt beim Start nur den Datenordner an. Die eigentliche Arbeit passiert in
# topology_routes.py (REST) und topo_discovery.py (Erkennung).


async def on_startup(ctx):
    ctx.data_dir.mkdir(parents=True, exist_ok=True)
    ctx.log("Netzwerk-Topologie bereit")
