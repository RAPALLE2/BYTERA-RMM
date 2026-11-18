# module.py (Modul "backup") - Backups & Snapshots im Proxmox-Stil
def on_startup(ctx):
    from rmm_mod_backup import agentlink, scheduler, store
    store.storages()                     # Standard-Ziel "Backend" anlegen
    agentlink.register(ctx)
    ctx.spawn("backup-zeitplan", lambda: scheduler.loop(ctx.log))
    ctx.log("Backup-Modul bereit (Zeitplan aktiv)")
