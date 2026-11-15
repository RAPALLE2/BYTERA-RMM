# e2e.py - encrypted connection to the RMM backend (counterpart: backend app/security.py)
# --------------------------------------------------------------------------------------
# 1. End-to-end: every API call travels inside an encrypted envelope (POST /api/e2e,
#    P-256 ECDH + HKDF-SHA256 + AES-256-GCM). Reverse proxies / Cloudflare only see
#    ciphertext - also the login password and the session token. Needs the
#    'cryptography' package (part of the Windows build; Linux: python3-cryptography).
# 2. TLS pinning: the backend's own self-signed certificate is accepted when its
#    fingerprint matches the pinned one (instead of "switch certificate checks off").
# The server's PUBLIC key + fingerprint are learned on first contact and pinned
# afterwards; a different key later is refused (possible attack).
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import socket
import ssl
import struct
import time
import urllib.request

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    AVAILABLE = True
except Exception:                                   # pragma: no cover
    AVAILABLE = False

INFO = b"bytera-e2e-v1|"


def kid_of(key_b64: str) -> str:
    return hashlib.sha256(base64.b64decode(key_b64 + "=" * (-len(key_b64) % 4))).hexdigest()[:16]


class Envelope:
    """One request: fresh ephemeral key -> seal request, open response."""

    def __init__(self, server_key_b64: str):
        srv = base64.b64decode(server_key_b64 + "=" * (-len(server_key_b64) % 4))
        self.kid = kid_of(server_key_b64)
        eph = ec.generate_private_key(ec.SECP256R1())
        self.salt = os.urandom(16)
        shared = eph.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), srv))
        okm = HKDF(algorithm=hashes.SHA256(), length=64, salt=self.salt, info=INFO + self.kid.encode()).derive(shared)
        self._c2s, self._s2c = AESGCM(okm[:32]), AESGCM(okm[32:])
        self.epk = eph.public_key().public_bytes(serialization.Encoding.X962,
                                                 serialization.PublicFormat.UncompressedPoint)

    def request_body(self, method: str, path: str, headers: dict, body: bytes | None) -> bytes:
        req = {"m": method, "p": path, "h": headers, "ts": time.time()}
        if body:
            req["b"] = base64.b64encode(body).decode()
        nonce = b"c2s\x00" + struct.pack(">Q", 0)
        sealed = nonce + self._c2s.encrypt(nonce, json.dumps(req).encode(), b"req")
        return b"BXE1" + bytes.fromhex(self.kid) + self.epk + self.salt + sealed

    def open_response(self, blob: bytes) -> dict:
        if len(blob) < 28 or blob[:4] != b"s2c\x00":
            raise ValueError("bad response")
        out = json.loads(self._s2c.decrypt(blob[:12], blob[12:], b"resp"))
        out["body"] = base64.b64decode(out.get("b") or "")
        return out


# ------------------------------------------------------------------ pinned TLS for urllib
class _PinnedConnection(http.client.HTTPSConnection):
    pin = ""

    def connect(self):
        super().connect()
        der = self.sock.getpeercert(binary_form=True) or b""
        if hashlib.sha256(der).hexdigest() != self.pin:
            self.sock.close()
            raise ssl.SSLError("certificate fingerprint does not match the pinned one (possible attack)")


class _PinnedHandler(urllib.request.HTTPSHandler):
    def __init__(self, pin: str):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE                  # replaced by the fingerprint check
        super().__init__(context=ctx)
        self._pin = pin

    def https_open(self, req):
        pin = self._pin

        class Conn(_PinnedConnection):
            pass
        Conn.pin = pin
        return self.do_open(lambda host, **kw: Conn(host, **kw), req)


def pinned_opener(pin: str) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_PinnedHandler(pin))


def fetch_info(server: str, ctx=None, timeout: float = 10) -> dict | None:
    try:
        with urllib.request.urlopen(server.rstrip("/") + "/api/security/info", timeout=timeout, context=ctx) as r:
            return json.loads(r.read() or b"{}")
    except Exception:
        return None


def port_open(host: str, port: int, timeout: float = 3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
