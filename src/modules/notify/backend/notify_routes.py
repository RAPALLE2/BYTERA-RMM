"""
notify_routes.py (module "notify", formerly router/notify_routes.py)
------------------------------------------------------------------
Notifications rework: rule management + SMTP settings.

  GET  /api/notify/catalog        -> trigger/channel catalog (for the editor)
  GET  /api/notify/rules          -> all rules
  POST /api/notify/rules          -> create a rule
  PATCH /api/notify/rules/{id}    -> modify a rule
  DELETE /api/notify/rules/{id}   -> delete a rule
  POST /api/notify/rules/{id}/test-> test notification over the rule's channel
  GET  /api/notify/smtp           -> SMTP settings (password masked)
  POST /api/notify/smtp           -> save SMTP settings
  POST /api/notify/smtp/test      -> send a test mail

Permission: admin OR 'manage_settings' (same boundary as the other
notification settings in the settings window).
"""

import asyncio
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import db
from app import bgjobs

from . import engine as notifier   # Regel-Motor des Moduls (gleiche Funktionsnamen wie frueher)
from app.auth import get_current_user, is_super_admin, user_has_permission
from router.admin_routes import build_notification

router = APIRouter(prefix="/api/notify", tags=["notify"])


def _require_manage(user: dict) -> None:
    if is_super_admin(user) or user_has_permission(user, "manage_settings"):
        return
    raise HTTPException(403, "Fehlendes Recht: manage_settings")


@router.get("/catalog")
async def catalog(user: dict = Depends(get_current_user)):
    _require_manage(user)
    return {
        "triggers": [{"key": k, "label": v["label"], "params": v["params"],
                      "cooldown": v["cooldown"]}
                     for k, v in notifier.TRIGGERS.items()],
        "channels": list(notifier.CHANNELS),
    }


# ------------------------------------------------------------------
# Regeln
# ------------------------------------------------------------------

class RuleBody(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    trigger: str | None = None
    client_ids: str | None = None      # kommagetrennt, '' = alle
    channel: str | None = None
    target: str | None = None
    params: dict | None = None


def _validate(fields: dict) -> None:
    if "trigger" in fields and fields["trigger"] not in notifier.TRIGGERS:
        raise HTTPException(400, "Unbekannter Trigger")
    if "channel" in fields and fields["channel"] not in notifier.CHANNELS:
        raise HTTPException(400, "Unbekannter Kanal (email|webhook|dashboard)")


@router.get("/rules")
async def list_rules(user: dict = Depends(get_current_user)):
    _require_manage(user)
    return notifier.list_rules()


@router.post("/rules")
async def create_rule(body: RuleBody, user: dict = Depends(get_current_user)):
    _require_manage(user)
    fields = body.model_dump(exclude_unset=True)
    if not fields.get("trigger") or not fields.get("channel"):
        raise HTTPException(400, "trigger und channel sind erforderlich")
    _validate(fields)
    if fields["channel"] in ("email", "webhook") and not (fields.get("target") or "").strip():
        raise HTTPException(400, "Ziel (E-Mail-Adresse bzw. Webhook) erforderlich")
    rule = notifier.create_rule(fields)
    db.add_audit_entry(user["username"], "notify.rule_created", target=rule["id"],
                       details=fields.get("name") or fields["trigger"])
    return rule


@router.patch("/rules/{rule_id}")
async def update_rule(rule_id: str, body: RuleBody,
                      user: dict = Depends(get_current_user)):
    _require_manage(user)
    if not notifier.get_rule(rule_id):
        raise HTTPException(404, "Rule not found")
    fields = body.model_dump(exclude_unset=True)
    _validate(fields)
    return notifier.update_rule(rule_id, fields)


@router.delete("/rules/{rule_id}")
async def delete_rule(rule_id: str, user: dict = Depends(get_current_user)):
    _require_manage(user)
    notifier.delete_rule(rule_id)
    db.add_audit_entry(user["username"], "notify.rule_deleted", target=rule_id)
    return {"ok": True}


@router.post("/rules/{rule_id}/test")
async def test_rule(rule_id: str, background: bool = False, user: dict = Depends(get_current_user)):
    """Als BACKEND-JOB (app/bgjobs.py): Neuladen/Schliessen bricht nichts ab,
    Fortschritt + Ergebnis in der Aufgaben-Anzeige. ?background=1 -> Job sofort."""
    _require_manage(user)
    async def _work(job):
        job.update(phase='notify.test', detail="Benachrichtigungsregel testen")
        return await _test_rule_impl(rule_id=rule_id, user=user)
    return await bgjobs.run('notify.test', None, title="Benachrichtigungsregel testen", user=user, work=_work,
                            background=background, private=True)


async def _test_rule_impl(rule_id: str, user: dict = Depends(get_current_user)):
    _require_manage(user)
    rule = notifier.get_rule(rule_id)
    if not rule:
        raise HTTPException(404, "Rule not found")
    n = build_notification(
        f"Test of rule \"{rule['name']}\" (trigger: "
        f"{notifier.TRIGGERS.get(rule['trigger'], {}).get('label', rule['trigger'])}).",
        head="Test notification", client="Test client",
        service="Rule test", level="info")
    try:
        await notifier._dispatch(rule, n)   # deliberately without cooldown
    except Exception as e:
        raise HTTPException(502, f"Versand fehlgeschlagen: {e}")
    return {"ok": True}


# ------------------------------------------------------------------
# SMTP
# ------------------------------------------------------------------

class SmtpBody(BaseModel):
    host: str
    port: int = 587
    user: str = ""
    password: str | None = None    # None = leave unchanged
    from_addr: str = ""
    security: str = "starttls"     # starttls | ssl | none


@router.get("/smtp")
async def get_smtp(user: dict = Depends(get_current_user)):
    _require_manage(user)
    cfg = notifier.smtp_settings()
    cfg["password"] = "•••" if cfg["password"] else ""
    cfg["from_addr"] = cfg.pop("from")
    return cfg


@router.post("/smtp")
def set_smtp(body: SmtpBody, user: dict = Depends(get_current_user)):
    _require_manage(user)
    if body.security not in ("starttls", "ssl", "none"):
        raise HTTPException(400, "Invalid security (starttls|ssl|none)")
    db.set_setting("smtp_host", body.host.strip())
    db.set_setting("smtp_port", str(int(body.port)))
    db.set_setting("smtp_user", body.user.strip())
    if body.password is not None:      # empty string = delete the password
        db.set_setting("smtp_password", body.password)
    db.set_setting("smtp_from", body.from_addr.strip())
    db.set_setting("smtp_security", body.security)
    db.add_audit_entry(user["username"], "notify.smtp_updated", details=body.host)
    return {"ok": True}


class SmtpTestBody(BaseModel):
    to: str


@router.post("/smtp/test")
async def test_smtp(body: SmtpTestBody, background: bool = False, user: dict = Depends(get_current_user)):
    """Als BACKEND-JOB (app/bgjobs.py): Neuladen/Schliessen bricht nichts ab,
    Fortschritt + Ergebnis in der Aufgaben-Anzeige. ?background=1 -> Job sofort."""
    _require_manage(user)
    async def _work(job):
        job.update(phase='notify.test', detail=f"Test-Mail an {body.to}")
        return await _test_smtp_impl(body=body, user=user)
    return await bgjobs.run('notify.test', None, title=f"Test-Mail an {body.to}", user=user, work=_work,
                            background=background, private=True)


async def _test_smtp_impl(body: SmtpTestBody, user: dict = Depends(get_current_user)):
    _require_manage(user)
    import asyncio
    try:
        await asyncio.to_thread(
            notifier.send_email, body.to,
            "RMM test mail",
            "This test mail confirms that the SMTP connection works.\n\n"
            "— BYTERA RMM")
    except Exception as e:
        raise HTTPException(502, f"Test mail failed: {e}")
    return {"ok": True}
