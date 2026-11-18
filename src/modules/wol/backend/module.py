# module.py (module "wol") - Wake-on-LAN
# No background task needed. MACs are read from the live metrics.
def on_startup(ctx):
    ctx.log("Wake-on-LAN ready")
