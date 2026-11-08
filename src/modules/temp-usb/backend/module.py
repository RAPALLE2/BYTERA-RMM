# module.py (Modul "temp-usb") - legt die Kern-Ordner an.
from . import store


def on_startup(ctx):
    store.ensure_dirs()
    ctx.log("temp-usb bereit (/storage/temp-usb)")


def on_enable(ctx):
    store.ensure_dirs()
