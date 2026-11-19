"""
notifier.py (Core)
------------------
Event trigger for notifications. The actual work (rules, SMTP, webhooks,
dashboard toasts, warranty check) is done by the MODULE "notify"
(src/modules/backend/notify). If it is off, all calls here run silently into
the void.

The core reports events at the known places:
  sockets.py   client_online/offline/new, agent_update, metric thresholds
  main.py      website_up/down (uptime monitor)
  auth_routes  user_login, login_failed

fire_event() forwards the event to ALL active modules that have a hook for it
(module.json -> "hooks") - "notify" is one of them.
"""

import asyncio

# Trigger catalog: key -> (label, default cooldown seconds, params hint).
# Stays in the core, because the events come from the core; the module delivers
# it to its rule editor.
TRIGGERS = {
    "client_offline":    {"label": "Client goes offline",              "cooldown": 60,    "params": []},
    "client_online":     {"label": "Client comes online",              "cooldown": 60,    "params": []},
    "client_new":        {"label": "New client registered",            "cooldown": 0,     "params": []},
    "warranty_expiring": {"label": "Warranty expiring soon",           "cooldown": 86400, "params": ["days_before"]},
    "warranty_expired":  {"label": "Warranty expired",                 "cooldown": 86400, "params": []},
    "cpu_high":          {"label": "CPU load above threshold",         "cooldown": 1800,  "params": ["threshold"]},
    "ram_high":          {"label": "RAM usage above threshold",        "cooldown": 1800,  "params": ["threshold"]},
    "disk_high":         {"label": "Disk usage above threshold",       "cooldown": 21600, "params": ["threshold"]},
    "temp_high":         {"label": "CPU temperature above threshold",  "cooldown": 1800,  "params": ["threshold"]},
    "website_down":      {"label": "Monitored website down",           "cooldown": 300,   "params": []},
    "website_up":        {"label": "Monitored website up again",       "cooldown": 300,   "params": []},
    "agent_update":      {"label": "Agent was updated",                "cooldown": 0,     "params": []},
    "user_login":        {"label": "User login to the dashboard",      "cooldown": 0,     "params": []},
    "login_failed":      {"label": "Failed login",                     "cooldown": 60,    "params": []},
}

CHANNELS = ("email", "webhook", "dashboard")


def _engine():
    """Rule engine of the module "notify" - None if it is off."""
    try:
        from app import modules
        return modules.use("notify", "engine") if modules.is_enabled("notify") else None
    except Exception:
        return None


async def fire_event(trigger: str, client_id: str | None = None,
                     notification: dict | None = None,
                     dedupe_key: str | None = None) -> None:
    """
    Reports an event to the modules.
      trigger      -> key from TRIGGERS
      client_id    -> affected client (None for clientless events)
      notification -> object from build_notification() (head/body/context)
      dedupe_key   -> cooldown key (default: client_id or trigger)
    """
    if trigger not in TRIGGERS:
        return
    # Maintenance mode (Core): no alarms for devices under maintenance.
    if client_id:
        try:
            from app import db as _db
            if _db.in_maintenance(client_id):
                return
        except Exception:
            pass
    if notification is None:
        from router.admin_routes import build_notification
        notification = build_notification(trigger, level="info")
    try:
        from app import modules as _modules
        await _modules.emit(trigger, event=trigger, client_id=client_id,
                            notification=notification, dedupe_key=dedupe_key)
    except Exception as exc:     # modules must never disturb the core - but stay visible
        print(f"[notify] event '{trigger}' could not be distributed: {exc}")


def fire_event_threadsafe(loop, trigger: str, **kwargs) -> None:
    """For synchronous callers (e.g. auth routes without await)."""
    try:
        asyncio.run_coroutine_threadsafe(fire_event(trigger, **kwargs), loop)
    except Exception:
        pass


async def check_metric_thresholds(client: dict, metrics: dict) -> None:
    """Per heartbeat from sockets.py: cpu/ram/disk/temp rules (module "notify")."""
    eng = _engine()
    if eng is not None:
        await eng.check_metric_thresholds(client, metrics)


async def send_all_webhooks(notification: dict) -> None:
    """Uptime monitor: notify all webhooks (module "notify")."""
    eng = _engine()
    if eng is not None:
        await eng.send_all_webhooks(notification)
