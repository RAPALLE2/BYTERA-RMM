# webhooks.py (Modul "notify")
# ----------------------------
# Versand an Webhooks (frueher router/admin_routes.py).
#   Discord -> fertiges Embed (head=Titel, body=Beschreibung, Kontextfelder, Farbe je Level)
#   Custom  -> JSON mit head/body/Kontext ODER eigenes Body-Template mit Platzhaltern

import json
import urllib.request
from datetime import datetime, timezone

from router.admin_routes import LEVEL_COLORS, build_notification


def _discord_payload(n: dict) -> dict:
    """
    Fertiges Discord-Template: ein Embed mit head als Titel, body als
    Beschreibung, den Kontextfeldern (Tenant / Location / Client / Service),
    einem ISO-Timestamp und der Farbe passend zum Level.
    """
    ts_iso = datetime.fromtimestamp(
        n["timestamp"] / 1000, tz=timezone.utc
    ).isoformat()

    embed = {
        "title": n["head"],
        "description": n["body"],
        "color": LEVEL_COLORS.get(n["level"], LEVEL_COLORS["info"]),
        "timestamp": ts_iso,
        "fields": [
            {"name": "Tenant",   "value": str(n["tenant"]),   "inline": True},
            {"name": "Location", "value": str(n["location"]), "inline": True},
            {"name": "Client",   "value": str(n["client"]),   "inline": True},
            {"name": "Service",  "value": str(n["service"]),  "inline": True},
        ],
        "footer": {"text": "BYTERA RMM"},
    }
    return {"embeds": [embed]}


def send_webhook(webhook: dict, notification: dict | str) -> None:
    """
    Send a notification to a webhook.

    'notification' is normally an object from build_notification() with
    head/body/context. For convenience a simple string is also accepted
    (then automatically wrapped into a notification).

    - Discord: ready-made embed template (head=title, body=description,
      context fields, timestamp, color depending on level).
    - Custom : generic JSON that ALWAYS contains 'head' and 'body', plus
      the full context.

    Runs synchronously in a short HTTP request (best effort).
    """
    if isinstance(notification, str):
        notification = build_notification(notification)

    # Eigene HTTP-Header des Webhooks (JSON-Objekt als Text in der DB).
    extra_headers = {}
    try:
        parsed = json.loads(webhook.get("headers") or "{}")
        if isinstance(parsed, dict):
            extra_headers = {str(k): str(v) for k, v in parsed.items()}
    except Exception:
        pass

    # Eigenes Body-Template: Platzhalter {head} {body} {message} {client}
    # {tenant} {location} {service} {level} {timestamp} werden ersetzt.
    template = (webhook.get("body_template") or "").strip()
    if webhook["type"] != "discord" and template:
        out = template
        for key in ("head", "body", "message", "client", "tenant",
                    "location", "service", "level", "timestamp"):
            val = str(notification.get(key, ""))
            # In JSON templates special characters must be escaped - we
            # escape like a JSON string WITHOUT the surrounding quotes,
            # so that "text": "{body}" stays valid.
            out = out.replace("{" + key + "}", json.dumps(val)[1:-1])
        data = out.encode("utf-8")
        content_type = extra_headers.pop("Content-Type", None) \
            or extra_headers.pop("content-type", None) or "application/json"
        req = urllib.request.Request(
            webhook["url"], data=data,
            headers={"Content-Type": content_type, "User-Agent": "Mozilla/5.0",
                     "Accept": "application/json", **extra_headers},
            method="POST")
        urllib.request.urlopen(req, timeout=10)
        return

    if webhook["type"] == "discord":
        payload = _discord_payload(notification)
    else:
        # Custom-Ziel: head + body sind garantiert vorhanden, dazu der Kontext.
        payload = {
            "head": notification["head"],
            "body": notification["body"],
            "tenant": notification["tenant"],
            "location": notification["location"],
            "client": notification["client"],
            "message": notification["message"],
            "service": notification["service"],
            "level": notification["level"],
            "timestamp": notification["timestamp"],
        }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook["url"],
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
            **extra_headers,
        },
        method="POST",
    )
    urllib.request.urlopen(req, timeout=10)
