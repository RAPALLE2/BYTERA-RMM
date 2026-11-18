# engine.py (module "notify")
# --------------------------
# Rule engine for notifications (formerly core: app/notifier.py).
#
# The admin creates RULES ("When <trigger> on <clients>, then <channel> to <target>").
# The core raises events via app.notifier.fire_event(); they arrive here as a
# module hook (module.py -> on_event -> handle_event).
#
# Channels:
#   email     -> SMTP (settings smtp_host/port/user/password/from/security)
#   webhook   -> stored webhook (Discord or Custom), target = webhook_id
#   dashboard -> live toast to all dashboards (socket event "notify:push")
#
# Dedupe/cooldown: per rule, last_fired (JSON {key: ts}) remembers when an
# event key last fired.

import asyncio
import json
import smtplib
import ssl
import time
from email.mime.text import MIMEText
from email.utils import formatdate

from app import db
from app.notifier import TRIGGERS, CHANNELS   # the catalog stays in the core (events come from there)



# ------------------------------------------------------------------
# Regeln (CRUD-Hilfen auf der notification_rules-Tabelle)
# ------------------------------------------------------------------

def list_rules() -> list[dict]:
    rows = db._conn.execute(
        "SELECT * FROM notification_rules ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


def get_rule(rule_id: str) -> dict | None:
    row = db._conn.execute(
        "SELECT * FROM notification_rules WHERE id = ?", (rule_id,)).fetchone()
    return dict(row) if row else None


def create_rule(fields: dict) -> dict:
    import uuid
    rid = str(uuid.uuid4())
    db._conn.execute(
        "INSERT INTO notification_rules"
        " (id, name, enabled, trigger, client_ids, channel, target, params, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (rid, fields.get("name") or "Regel", int(fields.get("enabled", 1)),
         fields["trigger"], fields.get("client_ids", ""),
         fields["channel"], fields.get("target", ""),
         json.dumps(fields.get("params") or {}), int(time.time() * 1000)))
    db._conn.commit()
    return get_rule(rid)


def update_rule(rule_id: str, fields: dict) -> dict | None:
    allowed = {"name", "enabled", "trigger", "client_ids", "channel", "target", "params"}
    updates = {}
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "params":
            v = json.dumps(v or {})
        if k == "enabled":
            v = int(bool(v))
        updates[k] = v
    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        db._conn.execute(f"UPDATE notification_rules SET {set_clause} WHERE id = ?",
                         (*updates.values(), rule_id))
        db._conn.commit()
    return get_rule(rule_id)


def delete_rule(rule_id: str) -> None:
    db._conn.execute("DELETE FROM notification_rules WHERE id = ?", (rule_id,))
    db._conn.commit()


def _mark_fired(rule_id: str, key: str) -> None:
    row = db._conn.execute(
        "SELECT last_fired FROM notification_rules WHERE id = ?", (rule_id,)).fetchone()
    try:
        data = json.loads(row["last_fired"] or "{}") if row else {}
    except Exception:
        data = {}
    data[key] = int(time.time())
    # Limit old entries so the JSON does not grow unbounded.
    if len(data) > 200:
        for k in sorted(data, key=data.get)[: len(data) - 200]:
            data.pop(k, None)
    db._conn.execute("UPDATE notification_rules SET last_fired = ? WHERE id = ?",
                     (json.dumps(data), rule_id))
    db._conn.commit()


def _cooled_down(rule: dict, key: str, cooldown: int) -> bool:
    if cooldown <= 0:
        return True
    try:
        data = json.loads(rule.get("last_fired") or "{}")
    except Exception:
        data = {}
    return time.time() - float(data.get(key, 0)) >= cooldown


# ------------------------------------------------------------------
# E-Mail (SMTP)
# ------------------------------------------------------------------

def smtp_settings() -> dict:
    return {
        "host": db.get_setting("smtp_host") or "",
        "port": db.get_int_setting("smtp_port") or 587,
        "user": db.get_setting("smtp_user") or "",
        "password": db.get_setting("smtp_password") or "",
        "from": db.get_setting("smtp_from") or (db.get_setting("smtp_user") or ""),
        "security": (db.get_setting("smtp_security") or "starttls").lower(),
    }


def send_email(to_addrs: str, subject: str, body: str) -> None:
    """Send a text email over the configured SMTP server.
    to_addrs may contain multiple addresses (comma-separated). Blocking -
    from an async context always call via asyncio.to_thread."""
    cfg = smtp_settings()
    if not cfg["host"]:
        raise RuntimeError("No SMTP server configured (Settings -> Notifications).")
    recipients = [a.strip() for a in (to_addrs or "").split(",") if a.strip()]
    if not recipients:
        raise RuntimeError("No recipient address given.")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = cfg["from"] or cfg["user"] or "rmm@localhost"
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)

    if cfg["security"] == "ssl":
        server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=15,
                                  context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=15)
    try:
        server.ehlo()
        if cfg["security"] == "starttls":
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
        if cfg["user"]:
            server.login(cfg["user"], cfg["password"])
        server.sendmail(msg["From"], recipients, msg.as_string())
    finally:
        try:
            server.quit()
        except Exception:
            pass


# ------------------------------------------------------------------
# Dispatch: send a notification over the channel of a rule
# ------------------------------------------------------------------

def _dispatch_sync(rule: dict, notification: dict) -> None:
    """Blocking send (email/webhook) - run in a thread."""
    channel = rule["channel"]
    if channel == "email":
        subject = notification.get("head") or "RMM Benachrichtigung"
        body = (f"{notification.get('body') or notification.get('message') or ''}\n\n"
                f"Client:   {notification.get('client')}\n"
                f"Tenant:   {notification.get('tenant')}\n"
                f"Standort: {notification.get('location')}\n"
                f"Service:  {notification.get('service')}\n"
                f"Level:    {notification.get('level')}\n"
                f"Zeit:     {time.strftime('%d.%m.%Y %H:%M:%S')}\n\n"
                f"— BYTERA RMM")
        send_email(rule.get("target") or "", subject, body)
    elif channel == "webhook":
        from .webhooks import send_webhook
        hook = db.get_webhook(rule.get("target") or "")
        if not hook:
            raise RuntimeError("Webhook der Regel existiert nicht mehr.")
        send_webhook(hook, notification)
    # "dashboard" wird async direkt in _dispatch() erledigt.


async def _dispatch(rule: dict, notification: dict) -> None:
    if rule["channel"] == "dashboard":
        try:
            from app.sockets import sio
            await sio.emit("notify:push", notification, namespace="/dashboard")
        except Exception as e:
            print(f"[notify] Dashboard-Push fehlgeschlagen: {e}")
        return
    try:
        await asyncio.to_thread(_dispatch_sync, rule, notification)
    except Exception as e:
        print(f"[notify] Regel '{rule.get('name')}' ({rule['channel']}) fehlgeschlagen: {e}")


def _rule_matches_client(rule: dict, client_id: str | None) -> bool:
    ids = [c for c in (rule.get("client_ids") or "").split(",") if c]
    if not ids:
        return True          # '' = alle Clients (auch client-lose Events)
    return client_id in ids


async def handle_event(trigger: str, client_id: str | None = None,
                       notification: dict | None = None,
                       dedupe_key: str | None = None) -> None:
    """
    Fuehrt alle passenden Regeln fuer ein Ereignis aus.
      trigger      -> key from TRIGGERS
      client_id    -> betroffener Client (None bei clientlosen Events)
      notification -> Objekt aus build_notification() (head/body/Kontext)
      dedupe_key   -> Cooldown-Schluessel (default: client_id bzw. trigger)
    """
    if trigger not in TRIGGERS:
        return
    key = dedupe_key or client_id or trigger
    cooldown = TRIGGERS[trigger]["cooldown"]
    if notification is None:
        from router.admin_routes import build_notification
        notification = build_notification(trigger, level="info")
    for rule in list_rules():
        if not rule.get("enabled"):
            continue
        if rule["trigger"] != trigger:
            continue
        if not _rule_matches_client(rule, client_id):
            continue
        if not _cooled_down(rule, key, cooldown):
            continue
        _mark_fired(rule["id"], key)
        await _dispatch(rule, notification)


async def send_all_webhooks(notification: dict) -> None:
    """Uptime-Monitor: Meldung an ALLE aktiven Webhooks (altes Verhalten, ohne Regel)."""
    from .webhooks import send_webhook
    for hook in db.list_webhooks():
        if not hook.get("enabled", 1):
            continue
        try:
            await asyncio.to_thread(send_webhook, hook, notification)
        except Exception as e:
            print(f"[notify] Webhook '{hook.get('name')}' fehlgeschlagen: {e}")


# ------------------------------------------------------------------
# Metrik-Schwellwerte (wird pro Heartbeat aus sockets.py aufgerufen)
# ------------------------------------------------------------------

async def check_metric_thresholds(client: dict, metrics: dict) -> None:
    """Check cpu_high / ram_high / disk_high / temp_high for a client.
    The per-rule cooldown prevents alarm storms on every heartbeat."""
    from router.admin_routes import build_notification
    cid = client["id"]
    hostname = client.get("hostname") or cid

    def _n(head, msg, level="warn"):
        return build_notification(msg, head=head, client=hostname,
                                  service="Monitoring", level=level)

    checks = []
    cpu = metrics.get("cpuLoad")
    if cpu is not None:
        checks.append(("cpu_high", float(cpu), "%",
                       f"CPU usage at {round(float(cpu))}%"))
    mem_t, mem_u = metrics.get("memTotal"), metrics.get("memUsed")
    if mem_t:
        ram = (mem_u or 0) / mem_t * 100
        checks.append(("ram_high", ram, "%", f"RAM usage at {round(ram)}%"))
    disk_t, disk_u = metrics.get("diskTotal"), metrics.get("diskUsed")
    if disk_t:
        dsk = (disk_u or 0) / disk_t * 100
        checks.append(("disk_high", dsk, "%", f"Disk usage at {round(dsk)}%"))
    temp = metrics.get("cpuTemp")
    if temp is not None:
        checks.append(("temp_high", float(temp), "°C",
                       f"CPU temperature at {round(float(temp))}°C"))

    for trigger, value, unit, msg in checks:
        # Only continue if there is a matching active rule at all
        # (saves the build_notification work in the normal case).
        rules = [r for r in list_rules()
                 if r.get("enabled") and r["trigger"] == trigger
                 and _rule_matches_client(r, cid)]
        if not rules:
            continue
        for rule in rules:
            try:
                params = json.loads(rule.get("params") or "{}")
            except Exception:
                params = {}
            threshold = float(params.get("threshold") or (90 if unit == "%" else 85))
            if value < threshold:
                continue
            if not _cooled_down(rule, cid, TRIGGERS[trigger]["cooldown"]):
                continue
            _mark_fired(rule["id"], cid)
            head = f"{TRIGGERS[trigger]['label']} – {hostname}"
            await _dispatch(rule, _n(head, f"{msg} (threshold: {round(threshold)}{unit})."))


# ------------------------------------------------------------------
# Warranty check loop (runs periodically in the background)
# ------------------------------------------------------------------

async def warranty_loop() -> None:
    """Check hourly all clients that have a warranty date set:
      warranty_expiring -> within params.days_before (default 30 days)
      warranty_expired  -> date passed
    The 24h cooldown per rule+client ensures at most 1 mail/day."""
    from router.admin_routes import build_notification
    await asyncio.sleep(20)   # let the backend start up calmly first
    while True:
        try:
            now = int(time.time() * 1000)
            rows = db._conn.execute(
                "SELECT id, hostname, warranty_until FROM clients"
                " WHERE warranty_until IS NOT NULL AND warranty_until > 0").fetchall()
            for row in rows:
                cid, hostname, until = row["id"], row["hostname"] or row["id"], row["warranty_until"]
                days_left = (until - now) / 86400000
                until_txt = time.strftime("%d.%m.%Y", time.localtime(until / 1000))
                if days_left < 0:
                    n = build_notification(
                        f"The warranty of {hostname} expired on {until_txt} "
                        f"({abs(int(days_left))} days ago).",
                        head=f"Warranty expired – {hostname}",
                        client=hostname, service="Warranty", level="error")
                    await handle_event("warranty_expired", client_id=cid, notification=n)
                else:
                    # expiring: check each rule's own lead time
                    for rule in list_rules():
                        if not rule.get("enabled") or rule["trigger"] != "warranty_expiring":
                            continue
                        if not _rule_matches_client(rule, cid):
                            continue
                        try:
                            params = json.loads(rule.get("params") or "{}")
                        except Exception:
                            params = {}
                        days_before = float(params.get("days_before") or 30)
                        if days_left > days_before:
                            continue
                        if not _cooled_down(rule, cid, TRIGGERS["warranty_expiring"]["cooldown"]):
                            continue
                        _mark_fired(rule["id"], cid)
                        n = build_notification(
                            f"The warranty of {hostname} expires on {until_txt} "
                            f"({int(days_left)} days left).",
                            head=f"Warranty expiring soon – {hostname}",
                            client=hostname, service="Warranty", level="warn")
                        await _dispatch(rule, n)
        except Exception as e:
            print(f"[notify] Warranty check failed: {e}")
        await asyncio.sleep(3600)
