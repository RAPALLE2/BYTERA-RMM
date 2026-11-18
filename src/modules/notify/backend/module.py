# module.py (module "notify")
# --------------------------
# Notifications: rules (trigger -> channel), SMTP, webhooks, dashboard toasts.
#
# The core raises events via app.notifier.fire_event(); each of them arrives
# here as a hook (module.json -> "hooks") and goes to the rule engine.
# Metric thresholds and the uptime monitor call engine.py directly (via
# app.notifier). The warranty check runs as a background task.

from . import engine


def on_startup(ctx):
    ctx.spawn("garantie", engine.warranty_loop)


async def on_event(ctx, data: dict):
    await engine.handle_event(
        data.get("event") or "",
        client_id=data.get("client_id"),
        notification=data.get("notification"),
        dedupe_key=data.get("dedupe_key"),
    )
