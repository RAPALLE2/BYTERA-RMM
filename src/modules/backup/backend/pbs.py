"""
pbs.py (Modul "backup") - Proxmox Backup Server: Verbindungstest vom Backend.

Gesichert wird direkt vom Client (proxmox-backup-client, Linux) - das Backend
prueft hier nur Erreichbarkeit, Anmeldung und Datastore. Das Zertifikat des
PBS wird gegen den hinterlegten SHA-256-Fingerprint geprueft (PBS nutzt
meist ein selbstsigniertes Zertifikat).
"""

from __future__ import annotations

import hashlib
import json
import socket
import ssl
import urllib.parse
import urllib.request


def _fingerprint(host: str, port: int) -> str:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=15) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
    h = hashlib.sha256(der).hexdigest()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


def test(st: dict) -> dict:
    host, port = st["server"], int(st.get("port") or 8007)
    fp = _fingerprint(host, port)
    want = (st.get("fingerprint") or "").strip().lower()
    if want and want != fp:
        raise RuntimeError(f"Zertifikats-Fingerprint stimmt nicht (Server: {fp})")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE          # Vertrauen ueber den Fingerprint (oben)
    base = f"https://{host}:{port}/api2/json"
    user = st.get("user") or ""
    headers = {}
    if "!" in user:                           # API-Token: user@realm!tokenid
        headers["Authorization"] = f"PBSAPIToken={user}:{st.get('secret') or ''}"
    else:
        body = urllib.parse.urlencode({"username": user, "password": st.get("secret") or ""}).encode()
        r = urllib.request.urlopen(urllib.request.Request(base + "/access/ticket", data=body),
                                   context=ctx, timeout=20)
        ticket = json.loads(r.read())["data"]["ticket"]
        headers["Cookie"] = f"PBSAuthCookie={urllib.parse.quote(ticket)}"
    r = urllib.request.urlopen(urllib.request.Request(base + "/admin/datastore", headers=headers),
                               context=ctx, timeout=20)
    stores = [d.get("store") for d in json.loads(r.read()).get("data") or []]
    if st.get("datastore") not in stores:
        raise RuntimeError(f"Datastore „{st.get('datastore')}“ nicht gefunden (vorhanden: {', '.join(stores) or '-'})")
    return {"ok": True, "fingerprint": fp,
            "hint": "" if want else "Tipp: Fingerprint im Speicherziel eintragen, um das Zertifikat festzunageln."}
