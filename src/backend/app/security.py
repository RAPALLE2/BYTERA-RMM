"""
security.py (core) - encrypted connections to agents and client apps
===================================================================

Two layers, both automatic:

1. END-TO-END encryption ("BXE1") of everything between backend and agent /
   client app - independent of the transport. Even with plain http://, a
   reverse proxy or Cloudflare in between, nobody can read or change the
   traffic (shell, screen, files, the agent token itself).
     * The backend has a long-term P-256 key (data/security/e2e_key.pem).
       Agents/clients know its PUBLIC key (built into the installer, otherwise
       learned once and then pinned - "trust on first use").
     * Each connection: ephemeral P-256 key of the device -> ECDH with the
       server key -> HKDF-SHA256 -> two AES-256-GCM keys (one per direction).
     * Every message carries a counter in its nonce; old or repeated messages
       are rejected (no replay).
     P-256 instead of X25519 so that every Android version can do it.

2. TLS on its own port (RMM_TLS_PORT, default 4443) with a self-signed
   certificate the backend creates itself. Devices pin its fingerprint, so a
   self-signed certificate is as safe as a bought one. If the port is not
   reachable (old Docker setup, firewall) devices fall back to the normal
   address - still end-to-end encrypted.

Wire formats (the agent, the desktop client and the Android app implement the
same - see agent/main.py "E2E", client .../e2e.py, android .../E2E.java):

  socket handshake  auth = {"e2e": {"v":1, "kid":hex8, "epk":b64(65 bytes),
                                    "n":b64(16 bytes), "t":b64(seal(c2s, token json))}}
  socket message    event "x", one binary argument = seal(pack(event, args))
  REST envelope     POST /api/e2e  body = b"BXE1" kid(8) epk(65) n(16) seal(c2s, request json)
                    answer          = seal(s2c, response json)
  seal              nonce(12) = prefix(4) + counter(8, big endian) ; nonce + AES-GCM(ciphertext+tag)
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import ipaddress
import json
import os
import socket
import struct
import threading
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.x509.oid import NameOID

from app.paths import DATA_DIR

SEC_DIR = DATA_DIR / "security"
KEY_FILE = SEC_DIR / "e2e_key.pem"
TLS_CERT = SEC_DIR / "tls_cert.pem"
TLS_KEY = SEC_DIR / "tls_key.pem"
VERSION = 1
INFO = b"bytera-e2e-v1|"
C2S = b"c2s\x00"
S2C = b"s2c\x00"
MAX_SKEW = 15 * 60
WINDOW = 4096

_lock = threading.Lock()
_key: ec.EllipticCurvePrivateKey | None = None


def b64e(b: bytes) -> str:
    return base64.b64encode(b).decode()


def b64d(s: str) -> bytes:
    return base64.b64decode(s + "=" * (-len(s) % 4))


# ------------------------------------------------------------------ server key
def _ensure_dir() -> None:
    SEC_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(SEC_DIR, 0o700)
    except OSError:
        pass


def server_key() -> ec.EllipticCurvePrivateKey:
    global _key
    with _lock:
        if _key is None:
            _ensure_dir()
            if KEY_FILE.exists():
                _key = serialization.load_pem_private_key(KEY_FILE.read_bytes(), password=None)
            else:
                _key = ec.generate_private_key(ec.SECP256R1())
                KEY_FILE.write_bytes(_key.private_bytes(serialization.Encoding.PEM,
                                                        serialization.PrivateFormat.PKCS8,
                                                        serialization.NoEncryption()))
                try:
                    os.chmod(KEY_FILE, 0o600)
                except OSError:
                    pass
        return _key


def pub_raw(key=None) -> bytes:
    k = key or server_key()
    return k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def server_pub_b64() -> str:
    return b64e(pub_raw())


def key_id(raw: bytes | None = None) -> str:
    return hashlib.sha256(raw or pub_raw()).hexdigest()[:16]


# ------------------------------------------------------------------ crypto primitives
def derive(priv: ec.EllipticCurvePrivateKey, peer_raw: bytes, salt: bytes, kid: str) -> tuple[bytes, bytes]:
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), peer_raw)
    shared = priv.exchange(ec.ECDH(), peer)
    okm = HKDF(algorithm=hashes.SHA256(), length=64, salt=salt, info=INFO + kid.encode()).derive(shared)
    return okm[:32], okm[32:]                      # (client->server, server->client)


class Channel:
    """One direction pair. seal() for sending, open() for receiving."""

    def __init__(self, k_send: bytes, k_recv: bytes, p_send: bytes, p_recv: bytes, first_send: int = 0):
        self._send = AESGCM(k_send)
        self._recv = AESGCM(k_recv)
        self._ps, self._pr = p_send, p_recv
        self._cs = first_send
        self._hi = -1                              # highest counter received
        self._seen: set[int] = set()               # counters received within the window
        self._lock = threading.Lock()

    def seal(self, data: bytes, aad: bytes = b"") -> bytes:
        with self._lock:
            n = self._ps + struct.pack(">Q", self._cs)
            self._cs += 1
        return n + self._send.encrypt(n, data, aad or None)

    def open(self, blob: bytes, aad: bytes = b"") -> bytes:
        if len(blob) < 28 or blob[:4] != self._pr:
            raise ValueError("bad message")
        ctr = struct.unpack(">Q", blob[4:12])[0]
        with self._lock:
            # messages sent at the same time may arrive slightly out of order: accept
            # every counter once inside a window of WINDOW, never twice, never older
            if ctr in self._seen or ctr <= self._hi - WINDOW:
                raise ValueError("replayed message")
            data = self._recv.decrypt(blob[:12], blob[12:], aad or None)
            self._seen.add(ctr)
            if ctr > self._hi:
                self._hi = ctr
                if len(self._seen) > 2 * WINDOW:
                    low = self._hi - WINDOW
                    self._seen = {c for c in self._seen if c > low}
        return data


def server_channel(epk: bytes, salt: bytes, kid: str) -> Channel:
    k_c2s, k_s2c = derive(server_key(), epk, salt, kid)
    return Channel(k_s2c, k_c2s, S2C, C2S)


def client_channel(server_pub: bytes, kid: str) -> tuple[Channel, bytes, bytes]:
    """For tests / Python clients: -> (channel, epk, salt)."""
    eph = ec.generate_private_key(ec.SECP256R1())
    salt = os.urandom(16)
    k_c2s, k_s2c = derive(eph, server_pub, salt, kid)
    return Channel(k_c2s, k_s2c, C2S, S2C), pub_raw(eph), salt


# ------------------------------------------------------------------ message packing (json + binary parts)
_BKEY = "\u0000b"


def pack(event: str, args: list) -> bytes:
    blobs: list[bytes] = []

    def walk(v):
        if isinstance(v, (bytes, bytearray, memoryview)):
            blobs.append(bytes(v))
            return {_BKEY: len(blobs) - 1}
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [walk(x) for x in v]
        return v

    head = json.dumps({"e": event, "a": walk(list(args)), "b": [len(b) for b in blobs]},
                      separators=(",", ":"), default=str).encode()
    return struct.pack(">I", len(head)) + head + b"".join(blobs)


def unpack(data: bytes) -> tuple[str, list]:
    hl = struct.unpack(">I", data[:4])[0]
    head = json.loads(data[4:4 + hl])
    blobs, pos = [], 4 + hl
    for n in head.get("b") or []:
        blobs.append(data[pos:pos + n])
        pos += n

    def walk(v):
        if isinstance(v, dict):
            if len(v) == 1 and _BKEY in v:
                return blobs[v[_BKEY]]
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v

    return str(head["e"]), walk(head.get("a") or [])


# ------------------------------------------------------------------ TLS certificate (own port)
def tls_port() -> int:
    try:
        return int(os.getenv("RMM_TLS_PORT", "4443") or 0)
    except ValueError:
        return 0


def _local_ips() -> set[str]:
    ips = {"127.0.0.1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ips.add(info[4][0])
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    host = (os.getenv("HOST") or "").strip()
    if host and host not in ("0.0.0.0", "::"):
        ips.add(host)
    return ips


def ensure_tls_cert() -> tuple[Path, Path]:
    """Self-signed certificate (ECDSA P-256, 20 years). Devices pin its fingerprint,
    so names/IPs in it do not matter for security - they only help browsers."""
    with _lock:
        if TLS_CERT.exists() and TLS_KEY.exists():
            return TLS_CERT, TLS_KEY
        _ensure_dir()
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "BYTERA RMM"),
                          x509.NameAttribute(NameOID.ORGANIZATION_NAME, "BYTERA RMM (self-signed)")])
        alt: list = [x509.DNSName("localhost")]
        try:
            alt.append(x509.DNSName(socket.gethostname()))
        except Exception:
            pass
        for ip in _local_ips():
            try:
                alt.append(x509.IPAddress(ipaddress.ip_address(ip)))
            except ValueError:
                pass
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=365 * 20))
                .add_extension(x509.SubjectAlternativeName(alt), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .sign(key, hashes.SHA256()))
        TLS_KEY.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
        try:
            os.chmod(TLS_KEY, 0o600)
        except OSError:
            pass
        TLS_CERT.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        return TLS_CERT, TLS_KEY


def tls_pin() -> str:
    """sha256 of the certificate (DER), hex - what devices pin."""
    try:
        ensure_tls_cert()
        cert = x509.load_pem_x509_certificate(TLS_CERT.read_bytes())
        return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    except Exception:
        return ""


TLS_STATUS = {"active": False, "error": ""}


# ------------------------------------------------------------------ settings / public info
def require_e2e() -> bool:
    try:
        from app import db
        return (db.get_setting("security_require_e2e", "0") or "0") == "1"
    except Exception:
        return False


def public_info() -> dict:
    """Public: the PUBLIC key and the certificate fingerprint (nothing secret)."""
    return {"v": VERSION, "key": server_pub_b64(), "kid": key_id(),
            "tls_port": tls_port() if TLS_STATUS["active"] else 0, "tls_pin": tls_pin(),
            "require": require_e2e()}


def env_lines() -> str:
    """Lines for the agent .env of new installations (key pinned from the start)."""
    return (f"E2E_SERVER_KEY={server_pub_b64()}\n"
            f"E2E_KEY_ID={key_id()}\n"
            f"TLS_PIN={tls_pin()}\n"
            f"TLS_PORT={tls_port()}\n")
