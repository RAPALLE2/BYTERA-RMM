"""
portal_routes.py (Modul "tickets") - oeffentliches Ticket-Portal
----------------------------------------------------------------
Ein kleines Support-Portal unter rmm.url/web/tickets (die Adresse liegt im
Namensraum des Webserver-Moduls - das Portal erscheint nur, wenn das Modul
"webserver" aktiv ist). Einstellbar in der Ticket-App (nur Admin):

  * Portal an/aus
  * MIT oder OHNE Anmeldung:
      - ohne Anmeldung: jeder kann ueber das Formular ein Ticket erstellen
        (Name/E-Mail als Kontakt, IP-Ratenbegrenzung gegen Spam)
      - mit Anmeldung: Ticket erstellen UND die eigenen/zugewiesenen Tickets
        sehen, kommentieren und (als zugewiesener Bearbeiter) den Status
        aendern - Login mit dem normalen RMM-Konto
  * Titel/Begruessung des Portals

Neue Portal-Tickets werden automatisch der Support-Gruppe zugewiesen (falls
vorhanden), damit sie bei jemandem landen. Private Kommentare bleiben im
Portal IMMER unsichtbar - es zeigt nur Kommentare mit Sichtbarkeit 'all'.
"""

from __future__ import annotations

import json
import threading
import time
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from app import db
from app.auth import decode_access_token, get_current_user, is_super_admin
from app.paths import MODULES_DATA_DIR

from rmm_mod_tickets.tickets_routes import (_assignees_of, _conn, _now,
                                            _save_assignees, AssigneeEntry,
                                            VALID_PRIO)

router = APIRouter(tags=["tickets-portal"])

CFG_DIR = MODULES_DATA_DIR / "tickets"
CFG_FILE = CFG_DIR / "portal.json"
_lock = threading.RLock()

DEFAULT_CFG = {"enabled": False, "require_login": False,
               "title": "Support-Portal", "intro": "Beschreibe dein Anliegen - wir melden uns."}


def _cfg() -> dict:
    with _lock:
        try:
            return {**DEFAULT_CFG, **json.loads(CFG_FILE.read_text(encoding="utf-8"))}
        except (OSError, json.JSONDecodeError):
            return dict(DEFAULT_CFG)


def _save_cfg(cfg: dict) -> None:
    with _lock:
        CFG_DIR.mkdir(parents=True, exist_ok=True)
        CFG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")


def _optional_user(authorization: str | None = Header(default=None)) -> dict | None:
    """Wie get_current_user, aber ohne Zwang - fuer Routen, die je nach
    Portal-Einstellung mit ODER ohne Anmeldung arbeiten."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    try:
        payload = decode_access_token(authorization.removeprefix("Bearer ").strip())
        return db.get_user_by_id(payload["sub"])
    except Exception:
        return None


# IP -> Zeitstempel der letzten anonymen Ticket-Erstellungen (Spam-Bremse)
_rate: dict[str, list[float]] = {}
RATE_MAX, RATE_WINDOW = 10, 3600.0


def _rate_ok(ip: str) -> bool:
    now = time.time()
    lst = [t for t in _rate.get(ip, []) if now - t < RATE_WINDOW]
    if len(lst) >= RATE_MAX:
        _rate[ip] = lst
        return False
    lst.append(now)
    _rate[ip] = lst
    return True


def _portal_may(user: dict, t: dict) -> bool:
    """Portal-Sicht: Ersteller oder (direkt/per Gruppe) zugewiesen."""
    if t["created_by"] == user["username"]:
        return True
    gids = set(db.get_user_group_ids(user["id"]))
    for a in _assignees_of(t["id"]):
        if a["subject_type"] == "user" and a["subject_id"] == user["id"]:
            return True
        if a["subject_type"] == "group" and a["subject_id"] in gids:
            return True
    return False


def _is_assigned(user: dict, ticket_id: str) -> bool:
    gids = set(db.get_user_group_ids(user["id"]))
    for a in _assignees_of(ticket_id):
        if a["subject_type"] == "user" and a["subject_id"] == user["id"]:
            return True
        if a["subject_type"] == "group" and a["subject_id"] in gids:
            return True
    return False


def _assign_support(ticket_id: str) -> None:
    grp = db.get_group_by_name(db.DEFAULT_GROUP_SUPPORT)
    if grp:
        _save_assignees(ticket_id, [AssigneeEntry(subject_type="group", subject_id=grp["id"])])


# ------------------------------------------------------------------
# Konfiguration (Admin, aus der Ticket-App)
# ------------------------------------------------------------------

class PortalCfgBody(BaseModel):
    enabled: bool
    require_login: bool
    title: str = "Support-Portal"
    intro: str = ""


@router.get("/api/tickets/portal/admin-config")
def get_admin_config(user: dict = Depends(get_current_user)):
    if not is_super_admin(user):
        raise HTTPException(403, "Nur Admins")
    from app import modules
    return {**_cfg(), "webserver_on": modules.is_enabled("webserver")}


@router.post("/api/tickets/portal/admin-config")
def set_admin_config(body: PortalCfgBody, user: dict = Depends(get_current_user)):
    if not is_super_admin(user):
        raise HTTPException(403, "Nur Admins")
    cfg = {"enabled": bool(body.enabled), "require_login": bool(body.require_login),
           "title": (body.title or "Support-Portal").strip()[:80],
           "intro": (body.intro or "").strip()[:500]}
    _save_cfg(cfg)
    db.add_audit_entry(user["username"], "tickets.portal.config", None,
                       f"enabled={cfg['enabled']} login={cfg['require_login']}")
    return cfg


# ------------------------------------------------------------------
# Oeffentliche Portal-API
# ------------------------------------------------------------------

@router.get("/api/tickets/portal/config")
def portal_config():
    cfg = _cfg()
    return {"enabled": cfg["enabled"], "require_login": cfg["require_login"],
            "title": cfg["title"], "intro": cfg["intro"]}


class PortalCreate(BaseModel):
    title: str
    text: str = ""
    priority: str = "normal"
    name: str = ""      # nur ohne Anmeldung: Kontaktname
    email: str = ""     # nur ohne Anmeldung: Kontakt


@router.post("/api/tickets/portal/create")
def portal_create(body: PortalCreate, request: Request,
                  user: dict | None = Depends(_optional_user)):
    cfg = _cfg()
    if not cfg["enabled"]:
        raise HTTPException(404, "Das Ticket-Portal ist nicht aktiviert")
    if cfg["require_login"] and user is None:
        raise HTTPException(401, "Bitte zuerst anmelden")
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(400, "Bitte einen Betreff angeben")
    prio = body.priority if body.priority in VALID_PRIO else "normal"
    desc = (body.text or "").strip()[:20000]

    if user is None:
        ip = (request.client.host if request and request.client else "") or "?"
        if not _rate_ok(ip):
            raise HTTPException(429, "Zu viele Anfragen - bitte später erneut versuchen")
        name = (body.name or "").strip()[:80] or "Portal-Gast"
        email = (body.email or "").strip()[:160]
        created_by = f"portal:{name}"
        desc = (f"[Über das Ticket-Portal erstellt]\n"
                f"Name: {name}\nE-Mail: {email or '-'}\nIP: {ip}\n\n{desc}")
    else:
        created_by = user["username"]
        desc = "[Über das Ticket-Portal erstellt]\n\n" + desc

    tid = uuid.uuid4().hex
    now = _now()
    _conn().execute(
        "INSERT INTO tickets (id, title, description, status, priority, created_by, "
        "created_at, updated_at) VALUES (?, ?, ?, 'open', ?, ?, ?, ?)",
        (tid, title[:200], desc, prio, created_by, now, now))
    _conn().commit()
    _assign_support(tid)
    db.add_audit_entry(created_by, "ticket.portal.created", target=tid, details=title[:200])
    return {"ok": True, "id": tid,
            "message": "Ticket erstellt. " + ("Du findest es unter „Meine Tickets“." if user
                                              else "Wir melden uns über die angegebene Adresse.")}


def _portal_ticket_out(t: dict, user: dict) -> dict:
    return {"id": t["id"], "title": t["title"], "status": t["status"],
            "priority": t["priority"], "created_at": t["created_at"],
            "updated_at": t["updated_at"], "created_by": t["created_by"],
            "assigned_to_me": _is_assigned(user, t["id"])}


@router.get("/api/tickets/portal/mine")
def portal_mine(user: dict = Depends(get_current_user)):
    cfg = _cfg()
    if not cfg["enabled"]:
        raise HTTPException(404, "Das Ticket-Portal ist nicht aktiviert")
    rows = [dict(r) for r in _conn().execute(
        "SELECT * FROM tickets ORDER BY updated_at DESC").fetchall()]
    return [_portal_ticket_out(t, user) for t in rows if _portal_may(user, t)]


def _portal_get(user: dict, ticket_id: str) -> dict:
    row = _conn().execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Ticket nicht gefunden")
    t = dict(row)
    if not (_portal_may(user, t) or is_super_admin(user)):
        raise HTTPException(403, "Kein Zugriff auf dieses Ticket")
    return t


@router.get("/api/tickets/portal/ticket/{ticket_id}")
def portal_ticket(ticket_id: str, user: dict = Depends(get_current_user)):
    t = _portal_get(user, ticket_id)
    # Nur OEFFENTLICHE Kommentare (visibility 'all') - interne bleiben intern.
    comments = [dict(r) for r in _conn().execute(
        "SELECT id, author, text, created_at FROM ticket_comments "
        "WHERE ticket_id = ? AND visibility = 'all' ORDER BY created_at",
        (ticket_id,)).fetchall()]
    out = _portal_ticket_out(t, user)
    out["description"] = t["description"]
    out["comments"] = comments
    return out


class PortalComment(BaseModel):
    text: str


@router.post("/api/tickets/portal/ticket/{ticket_id}/comment")
def portal_comment(ticket_id: str, body: PortalComment,
                   user: dict = Depends(get_current_user)):
    t = _portal_get(user, ticket_id)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "Leerer Kommentar")
    cid = uuid.uuid4().hex
    _conn().execute(
        "INSERT INTO ticket_comments (id, ticket_id, author, author_id, text, created_at, visibility) "
        "VALUES (?, ?, ?, ?, ?, ?, 'all')",
        (cid, ticket_id, user["username"], user["id"], text[:10000], _now()))
    _conn().execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (_now(), ticket_id))
    _conn().commit()
    db.add_audit_entry(user["username"], "ticket.portal.comment", target=ticket_id, details=text[:120])
    return {"ok": True, "id": cid}


class PortalStatus(BaseModel):
    status: str


@router.post("/api/tickets/portal/ticket/{ticket_id}/status")
def portal_status(ticket_id: str, body: PortalStatus,
                  user: dict = Depends(get_current_user)):
    t = _portal_get(user, ticket_id)
    # Status aendern darf im Portal nur, wem das Ticket ZUGEWIESEN ist.
    if not (_is_assigned(user, ticket_id) or is_super_admin(user)):
        raise HTTPException(403, "Status ändern dürfen nur zugewiesene Bearbeiter")
    if body.status not in ("open", "in_progress", "resolved"):
        raise HTTPException(400, "Ungültiger Status")
    now = _now()
    if body.status == "resolved":
        _conn().execute("UPDATE tickets SET status = ?, resolved_at = ?, resolved_by = ?, "
                        "updated_at = ? WHERE id = ?",
                        (body.status, now, user["username"], now, ticket_id))
    else:
        _conn().execute("UPDATE tickets SET status = ?, resolved_at = NULL, resolved_by = NULL, "
                        "updated_at = ? WHERE id = ?", (body.status, now, ticket_id))
    _conn().commit()
    db.add_audit_entry(user["username"], "ticket.portal.status", target=ticket_id, details=body.status)
    return {"ok": True}


# ------------------------------------------------------------------
# Die Portal-SEITE: rmm.url/web/tickets (braucht das Webserver-Modul)
# ------------------------------------------------------------------

@router.get("/web/tickets", include_in_schema=False)
def portal_redirect():
    return RedirectResponse("/web/tickets/", status_code=308)


@router.get("/web/tickets/", include_in_schema=False)
def portal_page():
    from app import modules
    if not modules.is_enabled("webserver"):
        return HTMLResponse("<h3>Das Ticket-Portal benötigt das aktivierte Modul „Webserver“.</h3>",
                            status_code=404)
    cfg = _cfg()
    if not cfg["enabled"]:
        return HTMLResponse("<h3>Das Ticket-Portal ist derzeit nicht aktiviert.</h3>", status_code=404)
    return HTMLResponse(_PORTAL_HTML)


# Eine einzige, in sich geschlossene Seite (kein Dashboard-Code, kein CDN).
_PORTAL_HTML = r"""<!doctype html>
<html lang="de"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Support-Portal</title>
<style>
:root{--bg:#0f1318;--panel:#161c26;--panel2:#1d2635;--border:#2a3650;--text:#e6e9f0;--sub:#8b96ab;--accent:#4e8cff;--ok:#34d399;--warn:#fbbf24;--danger:#f87171}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,sans-serif;background:var(--bg);color:var(--text)}
.wrap{max-width:760px;margin:0 auto;padding:24px 16px}
h1{font-size:22px;margin:6px 0} .sub{color:var(--sub);font-size:14px;margin-bottom:18px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:16px;margin-bottom:14px}
input,textarea,select{width:100%;background:var(--panel2);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:9px 10px;font-size:14px;margin:4px 0 10px}
textarea{min-height:110px;resize:vertical}
button{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:10px 16px;font-size:14px;cursor:pointer}
button.ghost{background:transparent;border:1px solid var(--border);color:var(--text)}
.err{color:var(--danger);font-size:13px;margin:6px 0} .okmsg{color:var(--ok);font-size:14px;margin:6px 0}
.ticket{cursor:pointer;display:flex;gap:10px;align-items:center;padding:10px 4px;border-bottom:1px solid var(--border)}
.badge{font-size:11px;border-radius:6px;padding:2px 8px;background:var(--panel2);border:1px solid var(--border)}
.s-open{color:var(--warn)} .s-in_progress{color:var(--accent)} .s-resolved{color:var(--ok)} .s-closed{color:var(--sub)}
.comment{background:var(--panel2);border-radius:8px;padding:8px 10px;margin:6px 0;font-size:14px}
.meta{font-size:11px;color:var(--sub)} a{color:var(--accent)}
.row{display:flex;gap:8px;flex-wrap:wrap} .row>*{flex:1}
</style></head><body><div class="wrap" id="app">Lade …</div>
<script>
"use strict";
const $=s=>document.querySelector(s);
let CFG=null, TOKEN=sessionStorage.getItem("portal-token")||null, USER=sessionStorage.getItem("portal-user")||null;
const esc=t=>String(t??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const STATUS={open:"Offen",in_progress:"In Arbeit",resolved:"Gelöst",closed:"Geschlossen"};
async function api(path,opts={}){
  const h={"Content-Type":"application/json",...(opts.headers||{})};
  if(TOKEN)h.Authorization="Bearer "+TOKEN;
  const r=await fetch(path,{...opts,headers:h});
  let d=null;try{d=await r.json()}catch{}
  if(!r.ok)throw new Error((d&&d.detail)||("HTTP "+r.status));
  return d;
}
const fmt=ms=>ms?new Date(ms).toLocaleString("de-DE"):"";
function header(){
  return `<h1>🎫 ${esc(CFG.title)}</h1><div class="sub">${esc(CFG.intro||"")}</div>`+
    (USER?`<div class="sub">Angemeldet als <b>${esc(USER)}</b> ·
      <a href="#" onclick="logout();return false">Abmelden</a></div>`:"");
}
window.logout=()=>{TOKEN=USER=null;sessionStorage.clear();render();};
function createForm(){
  return `<div class="card"><b>Neues Ticket</b>
    ${USER?"":`<div class="row"><input id="p-name" placeholder="Dein Name"><input id="p-email" placeholder="E-Mail für Rückfragen"></div>`}
    <input id="p-title" placeholder="Betreff">
    <textarea id="p-text" placeholder="Beschreibung des Anliegens …"></textarea>
    <select id="p-prio"><option value="low">Niedrig</option><option value="normal" selected>Normal</option>
      <option value="high">Hoch</option><option value="critical">Kritisch</option></select>
    <button onclick="createTicket()">Ticket absenden</button>
    <div id="p-msg"></div></div>`;
}
window.createTicket=async()=>{
  const m=$("#p-msg");m.innerHTML="";
  try{
    const res=await api("/api/tickets/portal/create",{method:"POST",body:JSON.stringify({
      title:$("#p-title").value,text:$("#p-text").value,priority:$("#p-prio").value,
      name:$("#p-name")?.value||"",email:$("#p-email")?.value||""})});
    m.innerHTML=`<div class="okmsg">✅ ${esc(res.message)}</div>`;
    $("#p-title").value="";$("#p-text").value="";
    if(USER)loadMine();
  }catch(e){m.innerHTML=`<div class="err">${esc(e.message)}</div>`;}
};
function loginForm(){
  return `<div class="card"><b>Anmelden</b>
    <input id="l-user" placeholder="Benutzername">
    <input id="l-pass" type="password" placeholder="Passwort">
    <input id="l-code" placeholder="2FA-Code (falls aktiv)">
    <button onclick="doLogin()">Anmelden</button><div id="l-msg"></div></div>`;
}
window.doLogin=async()=>{
  const m=$("#l-msg");m.innerHTML="";
  try{
    const res=await api("/api/auth/login",{method:"POST",body:JSON.stringify({
      username:$("#l-user").value,password:$("#l-pass").value,code:$("#l-code").value||""})});
    TOKEN=res.token;USER=res.user?.username||$("#l-user").value;
    sessionStorage.setItem("portal-token",TOKEN);sessionStorage.setItem("portal-user",USER);
    render();
  }catch(e){m.innerHTML=`<div class="err">${esc(e.message)}</div>`;}
};
async function loadMine(){
  const box=$("#mine");if(!box)return;
  try{
    const list=await api("/api/tickets/portal/mine");
    box.innerHTML=`<b>Meine Tickets</b>`+(list.length?list.map(t=>`
      <div class="ticket" onclick="openTicket('${t.id}')">
        <span class="badge s-${t.status}">${STATUS[t.status]||t.status}</span>
        <span style="flex:1">${esc(t.title)}${t.assigned_to_me?' <span class="badge">dir zugewiesen</span>':""}</span>
        <span class="meta">${fmt(t.updated_at)}</span></div>`).join("")
      :`<div class="meta" style="padding:8px 0">Noch keine Tickets.</div>`);
  }catch(e){box.innerHTML=`<div class="err">${esc(e.message)}</div>`;}
}
window.openTicket=async(id)=>{
  try{
    const t=await api("/api/tickets/portal/ticket/"+id);
    $("#app").innerHTML=header()+`
      <div class="card">
        <a href="#" onclick="render();return false">← Zurück</a>
        <h1 style="font-size:18px">${esc(t.title)}</h1>
        <div class="meta">Status: <span class="s-${t.status}">${STATUS[t.status]||t.status}</span>
          · Priorität: ${esc(t.priority)} · erstellt ${fmt(t.created_at)} von ${esc(t.created_by)}</div>
        ${t.assigned_to_me?`<div style="margin:10px 0" class="row">
          <button class="ghost" onclick="setStatus('${t.id}','in_progress')">▶ In Arbeit</button>
          <button class="ghost" onclick="setStatus('${t.id}','resolved')">✅ Gelöst</button>
          <button class="ghost" onclick="setStatus('${t.id}','open')">↩ Wieder öffnen</button></div>`:""}
        <pre style="white-space:pre-wrap;font-family:inherit;font-size:14px">${esc(t.description)}</pre>
        <b>Kommentare</b>
        ${t.comments.map(c=>`<div class="comment"><div class="meta">${esc(c.author)} · ${fmt(c.created_at)}</div>${esc(c.text)}</div>`).join("")||'<div class="meta">Noch keine.</div>'}
        <textarea id="c-text" placeholder="Antwort schreiben …"></textarea>
        <button onclick="addComment('${t.id}')">Antworten</button><div id="c-msg"></div>
      </div>`;
  }catch(e){alert(e.message);}
};
window.setStatus=async(id,st)=>{try{await api("/api/tickets/portal/ticket/"+id+"/status",{method:"POST",body:JSON.stringify({status:st})});openTicket(id);}catch(e){alert(e.message);}};
window.addComment=async(id)=>{
  const m=$("#c-msg");m.innerHTML="";
  try{await api("/api/tickets/portal/ticket/"+id+"/comment",{method:"POST",body:JSON.stringify({text:$("#c-text").value})});openTicket(id);}
  catch(e){m.innerHTML=`<div class="err">${esc(e.message)}</div>`;}
};
async function render(){
  if(CFG.require_login&&!TOKEN){$("#app").innerHTML=header()+loginForm();return;}
  $("#app").innerHTML=header()+createForm()+(TOKEN?`<div class="card" id="mine">Lade …</div>`:"");
  if(TOKEN)loadMine();
}
(async()=>{
  try{CFG=await api("/api/tickets/portal/config");}
  catch(e){$("#app").innerHTML=`<div class="err">${esc(e.message)}</div>`;return;}
  if(!CFG.enabled){$("#app").innerHTML="<h3>Das Portal ist derzeit nicht aktiviert.</h3>";return;}
  document.title=CFG.title;
  render();
})();
</script></body></html>
"""
