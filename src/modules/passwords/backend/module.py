"""Passwort-Manager - Einstiegspunkt des Moduls "passwords"."""
from . import store


async def on_startup(ctx):
    store.ensure_tables()
