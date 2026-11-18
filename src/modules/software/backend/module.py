# module.py (module "software") - software rollout
def on_startup(ctx):
    # Register the response handler for the agents' installation results.
    from rmm_mod_software import rollout
    rollout.register_handlers()
    ctx.log("Software rollout ready")
