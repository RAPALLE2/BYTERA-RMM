"""
totp.py
-------
Two-factor login with one-time codes (TOTP, RFC 6238) - compatible with all
common apps: Aegis, FreeOTP, Google Authenticator, Microsoft Authenticator,
1Password, Bitwarden …

Why built in-house instead of a library: the procedure is short and clearly
described (HMAC-SHA1 over the time step, then the last digits of it), and for
such a central security building block one fewer dependency is a real gain.
Everything here comes from the standard library.

About the QR code: generating a QR image is far more involved than TOTP itself.
The package `qrcode` is used for it - if it is missing, setup still works: the
app can also take the secret in by hand.

Security notes considered in the implementation:
  * Code comparison is time-constant (`hmac.compare_digest`) - otherwise one
    could guess digit by digit via response times.
  * A code is valid only ONCE: already used time windows are remembered, so an
    intercepted code cannot be reused immediately.
  * One window of tolerance forward and backward (±30 s) against inaccurate
    clocks.
  * Recovery codes are stored only as a hash - like passwords.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import struct
import time
from urllib.parse import quote

DIGITS = 6
PERIOD = 30          # seconds per code
WINDOW = 1           # ± one time step of tolerance
ISSUER = "BYTERA RMM"

# Already used (user, time step) pairs. Small and short-lived, therefore in
# memory - after a restart the protection is briefly gone, which is acceptable
# compared to a database write per login.
_used: dict[str, set[int]] = {}


# ==========================================================================
# Secret
# ==========================================================================

def new_secret(length: int = 20) -> str:
    """
    New secret as Base32 (as the apps expect it).
    20 bytes = 160 bits, which matches the recommendation in RFC 4226.
    """
    return base64.b32encode(os.urandom(length)).decode("ascii").rstrip("=")


def provisioning_uri(secret: str, username: str, issuer: str = ISSUER) -> str:
    """
    The `otpauth://` URL embedded in the QR code.

    The issuer is deliberately in there twice - once in the label, once as a
    parameter. Older apps read only the one, newer ones only the other.
    """
    label = quote(f"{issuer}:{username}", safe="")
    return (f"otpauth://totp/{label}?secret={secret}"
            f"&issuer={quote(issuer, safe='')}"
            f"&algorithm=SHA1&digits={DIGITS}&period={PERIOD}")


# ==========================================================================
# Compute and verify code
# ==========================================================================

def _code_at(secret: str, counter: int) -> str:
    """A one-time code for a specific time step (RFC 4226)."""
    padding = "=" * (-len(secret) % 8)
    key = base64.b32decode(secret.upper() + padding, casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    # "Dynamic truncation": the last 4 bits say where the 4 bytes begin.
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** DIGITS)).zfill(DIGITS)


def current_code(secret: str, at: float | None = None) -> str:
    """The currently valid code - only for tests and the setup preview."""
    return _code_at(secret, int((at if at is not None else time.time()) // PERIOD))


def verify(secret: str, code: str, user_key: str = "", at: float | None = None) -> bool:
    """
    Verifies an entered code.

    user_key: identifier of the user. If given, each code is valid only once -
    without this protection an intercepted code could be used a second time
    within its 30 seconds.
    """
    code = (code or "").strip().replace(" ", "")
    if not secret or not code.isdigit() or len(code) != DIGITS:
        return False

    now = int((at if at is not None else time.time()) // PERIOD)
    for step in range(-WINDOW, WINDOW + 1):
        counter = now + step
        if hmac.compare_digest(_code_at(secret, counter), code):
            if user_key:
                seen = _used.setdefault(user_key, set())
                if counter in seen:
                    return False              # already used
                seen.add(counter)
                # Clean up old entries, otherwise the set grows forever.
                _used[user_key] = {c for c in seen if c >= now - WINDOW - 2}
            return True
    return False


# ==========================================================================
# Recovery codes
# ==========================================================================
# For the "phone gone" case. Without them an administrator would have to step
# in every time - and if it hits the last administrator, nobody would be able
# to act anymore.

def new_backup_codes(count: int = 8) -> list[str]:
    """Easy-to-read one-time codes, e.g. 'k3f9-2mzq'."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"   # without i/l/1/o/0
    out = []
    for _ in range(count):
        raw = "".join(secrets.choice(alphabet) for _ in range(8))
        out.append(f"{raw[:4]}-{raw[4:]}")
    return out


def hash_backup_codes(codes: list[str]) -> str:
    """Store only hashes - the user sees the plaintext codes exactly once."""
    return ",".join(hashlib.sha256(c.encode()).hexdigest() for c in codes)


def use_backup_code(stored: str, code: str) -> tuple[bool, str]:
    """
    Verifies a recovery code and removes it.
    Returns: (matched?, remaining hashes).
    """
    code = (code or "").strip().lower().replace(" ", "")
    if not stored or not code:
        return False, stored or ""
    wanted = hashlib.sha256(code.encode()).hexdigest()
    rest = [h for h in stored.split(",") if h]
    for h in rest:
        if hmac.compare_digest(h, wanted):
            rest.remove(h)
            return True, ",".join(rest)
    return False, stored


# ==========================================================================
# QR-Code
# ==========================================================================

def qr_data_uri(uri: str) -> str | None:
    """
    QR code as a `data:` URL, directly usable in an <img>.
    Returns None if the package `qrcode` is missing - then the UI shows the
    secret to type in.
    """
    try:
        import io
        import qrcode
    except ImportError:
        return None
    try:
        img = qrcode.make(uri)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None
