# module.py (Modul "usbguard") - USB-Wächter
def on_startup(ctx):
    from rmm_mod_usbguard import guard
    guard.register(ctx)
    ctx.log("USB-Wächter bereit")
