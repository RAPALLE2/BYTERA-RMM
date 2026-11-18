"""
trust.py (Modul "usbguard") - vertrauenswuerdige USB-Geraete.

1) Geraete-IDs (VID:PID, optional Seriennummer) - z.B. YubiKeys oder andere
   Schluessel-Geraete ohne Dateisystem. Werden sofort zugelassen.
2) Signierte Schluesseldateien ".bytera-usbkey" fuer Speichersticks. Das
   Backend signiert mit einem eigenen Ed25519-Schluessel (privater Teil
   bleibt hier, Agents bekommen nur den oeffentlichen). Jede Datei ist an
   VID, PID und Seriennummer EINES Sticks gebunden und einzeln widerrufbar.

Ablage: data/modules/usbguard/{trust.json, signing_key.pem}
"""

from __future__ import annotations

import base64
import json
import secrets
import threading
import time

from app.paths import MODULES_DATA_DIR

DIR = MODULES_DATA_DIR / "usbguard"
TRUST_FILE = DIR / "trust.json"
KEY_FILE = DIR / "signing_key.pem"
_lock = threading.RLock()


def _load() -> dict:
    with _lock:
        try:
            d = json.loads(TRUST_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            d = {}
    d.setdefault("ids", [])
    d.setdefault("keys", [])
    return d


def _save(d: dict) -> None:
    with _lock:
        DIR.mkdir(parents=True, exist_ok=True)
        tmp = TRUST_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(TRUST_FILE)


def _private_key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    with _lock:
        if KEY_FILE.exists():
            return serialization.load_pem_private_key(KEY_FILE.read_bytes(), password=None)
        DIR.mkdir(parents=True, exist_ok=True)
        key = Ed25519PrivateKey.generate()
        KEY_FILE.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
        try:
            KEY_FILE.chmod(0o600)
        except OSError:
            pass
        return key


def public_key_b64() -> str:
    from cryptography.hazmat.primitives import serialization
    raw = _private_key().public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def agent_payload() -> dict:
    """Was die Agents fuer die Pruefung brauchen."""
    d = _load()
    return {"trust": [{"vid": i["vid"], "pid": i.get("pid", ""), "serial": i.get("serial", "")}
                      for i in d["ids"]],
            "pubkey": public_key_b64(),
            "revoked": [k["kid"] for k in d["keys"] if k.get("revoked")]}


def list_all() -> dict:
    return _load()


def _hex4(v: str) -> str:
    v = (v or "").strip().lower()
    if len(v) != 4 or any(c not in "0123456789abcdef" for c in v):
        raise ValueError("VID/PID müssen 4-stellige Hex-Werte sein (z.B. 1050)")
    return v


def add_id(label: str, vid: str, pid: str = "", serial: str = "", by: str = "") -> dict:
    entry = {"id": secrets.token_hex(6), "label": (label or "").strip()[:80] or "Gerät",
             "vid": _hex4(vid), "pid": _hex4(pid) if pid else "",
             "serial": (serial or "").strip()[:128], "by": by,
             "created": int(time.time() * 1000)}
    d = _load()
    d["ids"].append(entry)
    _save(d)
    return entry


def remove_id(entry_id: str) -> None:
    d = _load()
    d["ids"] = [i for i in d["ids"] if i["id"] != entry_id]
    _save(d)


def issue_key(label: str, vid: str, pid: str, serial: str, by: str = "") -> tuple[dict, bytes]:
    """Erzeugt eine signierte Schluesseldatei (Inhalt als Bytes) fuer EINEN Stick."""
    serial = (serial or "").strip()
    if not serial:
        raise ValueError("Der Stick braucht eine eindeutige Seriennummer - ohne sie "
                         "liesse sich die Datei auf jeden Stick kopieren")
    vid, pid = _hex4(vid), _hex4(pid)
    label = (label or "").strip()[:80] or "USB-Stick"
    kid = secrets.token_hex(8)
    iat = int(time.time())
    msg = f"bytera-usbkey|v1|{kid}|{vid}|{pid}|{serial}|{label}|{iat}"
    sig = _private_key().sign(msg.encode("utf-8"))
    doc = {"v": 1, "kid": kid, "label": label, "vid": vid, "pid": pid, "serial": serial,
           "iat": iat, "sig": base64.b64encode(sig).decode(),
           "hint": "BYTERA USB-Schluesseldatei - im Stammverzeichnis des Sticks ablegen."}
    meta = {"kid": kid, "label": label, "vid": vid, "pid": pid, "serial": serial,
            "by": by, "created": iat * 1000, "revoked": False}
    d = _load()
    d["keys"].append(meta)
    _save(d)
    return meta, json.dumps(doc, ensure_ascii=False, indent=1).encode("utf-8")


def revoke_key(kid: str) -> None:
    d = _load()
    for k in d["keys"]:
        if k["kid"] == kid:
            k["revoked"] = True
    _save(d)
