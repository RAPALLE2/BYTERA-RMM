"""
calls_routes.py (Modul "calls") - Signalisierung für Audio-/Video-Anrufe
------------------------------------------------------------------------
Die MEDIEN (Ton, Kamera, Bildschirm) laufen per WebRTC DIREKT zwischen den
Teilnehmern und sind Ende-zu-Ende verschlüsselt (DTLS-SRTP) - das Backend
sieht sie nie. Hier läuft nur die SIGNALISIERUNG (wer ruft wen, SDP-Angebote,
ICE-Kandidaten), und zwar bewusst über REST + Dashboard-Socket:

  * Senden IMMER per REST (harte Anmeldeprüfung über den JWT) - der Absender
    einer Nachricht ist damit vom Backend VERIFIZIERT, nie selbst behauptet.
  * Zustellen über den Dashboard-Socket, aber NUR an die Socket-Sitzungen des
    Ziel-Benutzers (die Zuordnung Benutzer -> Socket wird per REST registriert,
    ebenfalls mit Anmeldeprüfung).
  * JEDE zugestellte Nachricht trägt eine HMAC-SHA256-SIGNATUR des Backends
    über (Ereignis|Raum|von|an|Zeit|Inhalts-Hash). Den Prüf-Schlüssel bekommen
    nur angemeldete Benutzer (GET /api/calls/verify-key). Der Empfänger prüft
    die Signatur, bevor er ein SDP/ICE übernimmt - gefälschte oder von Dritten
    eingeschleuste Signalisierung wird verworfen.

Gruppenanrufe laufen als Mesh (jeder mit jedem) - gedacht für kleine Runden.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import db
from app.auth import get_current_user

router = APIRouter(prefix="/api/calls", tags=["calls"])

# Pro Backend-Start ein frischer Signatur-Schlüssel (Signalisierung ist
# flüchtig - nach einem Neustart gibt es ohnehin keine laufenden Anrufe mehr).
_SIGN_KEY = secrets.token_bytes(32)

# username -> {sid: letzter Registrierungszeitpunkt}
_user_sids: dict[str, dict[str, float]] = {}
# room_id -> {"members": {username}, "video": bool, "by": str, "ts": float}
_rooms: dict[str, dict] = {}
_SID_TTL = 6 * 3600


def _sign(event: str, room: str, frm: str, to: str, ts: int, payload) -> str:
    # ensure_ascii=False, damit die Kanonisierung EXAKT der des Frontends
    # entspricht (JS laesst Umlaute usw. unverändert).
    blob = json.dumps(payload or {}, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode()
    msg = f"{event}|{room}|{frm}|{to}|{ts}|{hashlib.sha256(blob).hexdigest()}".encode()
    return hmac.new(_SIGN_KEY, msg, hashlib.sha256).hexdigest()


def _sids_of(username: str) -> list[str]:
    now = time.time()
    entry = _user_sids.get(username) or {}
    live = {sid: ts for sid, ts in entry.items() if now - ts < _SID_TTL}
    _user_sids[username] = live
    return list(live)


async def _deliver(event: str, room: str, frm: str, to: str, payload: dict) -> int:
    """Signierte Nachricht an alle Dashboard-Sockets des Ziel-Benutzers."""
    from app.sockets import sio
    ts = int(time.time() * 1000)
    envelope = {"event": event, "room": room, "from": frm, "to": to, "ts": ts,
                "payload": payload or {},
                "sig": _sign(event, room, frm, to, ts, payload or {})}
    n = 0
    for sid in _sids_of(to):
        try:
            await sio.emit("call-msg", envelope, to=sid, namespace="/dashboard")
            n += 1
        except Exception:
            pass
    return n


def _room(room_id: str) -> dict:
    r = _rooms.get(room_id)
    if not r:
        raise HTTPException(404, "Dieser Anruf existiert nicht (mehr)")
    return r


def _member(room_id: str, user: dict) -> dict:
    r = _room(room_id)
    if user["username"] not in r["members"]:
        raise HTTPException(403, "Du bist kein Teilnehmer dieses Anrufs")
    return r


# ------------------------------------------------------------------
# Registrierung + Schlüssel
# ------------------------------------------------------------------

class RegisterBody(BaseModel):
    sid: str


@router.post("/register")
def register(body: RegisterBody, user: dict = Depends(get_current_user)):
    """Dashboard-Socket dieses (angemeldeten!) Benutzers registrieren - nur so
    erreichen ihn Klingeln und Signalisierung. Die Zuordnung kommt per REST,
    damit niemand fremde Benutzernamen am Socket behaupten kann."""
    sid = (body.sid or "").strip()
    if not sid or len(sid) > 64:
        raise HTTPException(400, "Ungültige Socket-ID")
    _user_sids.setdefault(user["username"], {})[sid] = time.time()
    return {"ok": True}


@router.get("/verify-key")
def verify_key(user: dict = Depends(get_current_user)):
    """Prüf-Schlüssel für die Signaturen (nur angemeldet abrufbar)."""
    return {"key": _SIGN_KEY.hex(), "algo": "HMAC-SHA256",
            "format": "event|room|from|to|ts|sha256(payload-json sortiert)"}


@router.get("/users")
def call_users(user: dict = Depends(get_current_user)):
    """Anrufbare Benutzer (alle aktiven, außer man selbst)."""
    out = []
    for u in db.list_users():
        if u.get("username") == user["username"]:
            continue
        if u.get("active") in (0, False):
            continue
        out.append({"username": u["username"],
                    "display_name": u.get("display_name") or u["username"],
                    "online": bool(_sids_of(u["username"]))})
    out.sort(key=lambda x: (not x["online"], x["display_name"].lower()))
    return out


# ------------------------------------------------------------------
# Anruf-Ablauf
# ------------------------------------------------------------------

class InviteBody(BaseModel):
    to: list[str]
    video: bool = False
    room: str = ""      # gesetzt: weitere Person in laufenden Anruf einladen


@router.post("/invite")
async def invite(body: InviteBody, user: dict = Depends(get_current_user)):
    targets = [t for t in dict.fromkeys(body.to or []) if t and t != user["username"]]
    if not targets:
        raise HTTPException(400, "Bitte mindestens einen Benutzer auswählen")
    known = {u["username"] for u in db.list_users()}
    for t in targets:
        if t not in known:
            raise HTTPException(404, f"Benutzer {t} nicht gefunden")
    if body.room:
        r = _member(body.room, user)
        room_id = body.room
    else:
        room_id = uuid.uuid4().hex[:12]
        r = _rooms[room_id] = {"members": {user["username"]}, "video": bool(body.video),
                               "by": user["username"], "ts": time.time()}
    rang = []
    for t in targets:
        n = await _deliver("ring", room_id, user["username"], t,
                           {"video": r["video"], "group": len(targets) > 1 or len(r["members"]) > 1,
                            "members": sorted(r["members"])})
        rang.append({"user": t, "online": n > 0})
    db.add_audit_entry(user["username"], "calls.invite", None,
                       f"{', '.join(targets)} ({'Video' if r['video'] else 'Audio'})")
    return {"room": room_id, "video": r["video"], "rang": rang}


class RoomBody(BaseModel):
    room: str


@router.post("/join")
async def join(body: RoomBody, user: dict = Depends(get_current_user)):
    r = _room(body.room)
    others = sorted(r["members"] - {user["username"]})
    r["members"].add(user["username"])
    # Bestehende Teilnehmer bauen die Verbindung ZUM Neuen auf (sie senden das
    # Angebot) - der Neue wartet auf Offers. So gibt es keine Glare-Konflikte.
    for o in others:
        await _deliver("peer-join", body.room, user["username"], o, {})
    return {"room": body.room, "video": r["video"], "members": sorted(r["members"])}


@router.post("/decline")
async def decline(body: RoomBody, user: dict = Depends(get_current_user)):
    r = _rooms.get(body.room)
    if r:
        for m in sorted(r["members"]):
            await _deliver("declined", body.room, user["username"], m, {})
    return {"ok": True}


@router.post("/leave")
async def leave(body: RoomBody, user: dict = Depends(get_current_user)):
    r = _rooms.get(body.room)
    if not r:
        return {"ok": True}
    r["members"].discard(user["username"])
    for m in sorted(r["members"]):
        await _deliver("peer-leave", body.room, user["username"], m, {})
    if not r["members"]:
        _rooms.pop(body.room, None)
    return {"ok": True}


class SignalBody(BaseModel):
    room: str
    to: str
    data: dict          # {type: offer|answer|ice, ...} - Inhalt ist Sache der Clients


@router.post("/signal")
async def signal(body: SignalBody, user: dict = Depends(get_current_user)):
    """SDP/ICE an EINEN Teilnehmer desselben Raums weiterreichen. Absender ist
    der angemeldete Benutzer (nie aus dem Body), Zustellung signiert."""
    r = _member(body.room, user)
    if body.to not in r["members"]:
        raise HTTPException(403, "Ziel ist kein Teilnehmer dieses Anrufs")
    if len(json.dumps(body.data)) > 256 * 1024:
        raise HTTPException(413, "Signalisierungs-Nachricht zu groß")
    n = await _deliver("signal", body.room, user["username"], body.to, body.data)
    return {"delivered": n > 0}
