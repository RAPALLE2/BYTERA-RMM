"""
security_policy.py
------------------
Two protection mechanisms that the BSI IT-Grundschutz explicitly requires and
that were missing so far:

  1. Password policy  (ORP.4.A8 "regulation of password use",
     ORP.4.A22 "regulation of password quality")
     Minimum length and character classes are checked SERVER-SIDE. A check in
     the browser alone would be ineffective, because the API can be addressed
     directly too.

  2. Protection against password guessing (ORP.4.A9 "identification and
     authentication", elementary threat G 0.34)
     After several failed attempts the account is locked for a while. It is
     counted per username AND per source IP, so neither a single account can be
     brute-forced nor can attacks be spread widely from one place.

Both are adjustable via the settings - the Grundschutz does not prescribe fixed
numbers, but a deliberate, documented decision. The default values follow the
current BSI recommendations: rather long than complicated, no enforced regular
change.

IMPORTANT: this file does not make the software "BSI-compliant" - compliant is
always an organization with its information network, not a program. It ensures
that the technical requirements can be met.
See docs/BSI-IT-Grundschutz.md.
"""

from __future__ import annotations

import re
import time
import unicodedata

from app import db

# --- Default values -------------------------------------------------------
# Deliberately documented so the security concept records what applies.
DEFAULTS = {
    "pw_min_length": 12,        # BSI: length matters more than special characters
    "pw_require_classes": 3,    # of: lowercase, uppercase, digits, special characters
    "pw_block_username": 1,     # username must not be contained in the password
    "login_max_attempts": 5,    # lock after this
    "login_lock_minutes": 15,   # duration of the lock
    "login_window_minutes": 15, # time window in which failed attempts count
}

# Very common passwords that are no good despite meeting the rules.
# Deliberately kept short: a real blocklist belongs in the operations docs.
_TRIVIAL = {
    "passwort", "password", "administrator", "admin", "willkommen",
    "welcome", "qwertz", "qwerty", "123456", "12345678", "letmein",
    "changeme", "geheim", "bytera", "rmm",
}


def setting(key: str) -> int:
    """Value from the settings, otherwise the documented default."""
    try:
        raw = db.get_setting(key)
        if raw is not None and str(raw).strip() != "":
            return int(raw)
    except Exception:
        pass
    return int(DEFAULTS[key])


def policy() -> dict:
    """The applicable policy - also for display in the dashboard."""
    return {k: setting(k) for k in DEFAULTS}


# ==========================================================================
# 1. Password policy
# ==========================================================================

def check_password(password: str, username: str = "") -> list[str]:
    """
    Checks a password. Returns: list of violations (empty = OK).

    Deliberately ALL violations are collected instead of stopping at the first -
    otherwise one works through the rules in several attempts.
    """
    problems: list[str] = []
    pw = password or ""

    min_len = setting("pw_min_length")
    if len(pw) < min_len:
        problems.append(f"at least {min_len} characters (currently {len(pw)})")

    classes = 0
    if re.search(r"[a-zäöüß]", pw):
        classes += 1
    if re.search(r"[A-ZÄÖÜ]", pw):
        classes += 1
    if re.search(r"\d", pw):
        classes += 1
    # Anything that is neither a letter nor a digit counts as a special character.
    if re.search(r"[^\wÄÖÜäöüß]|_", pw):
        classes += 1

    need = setting("pw_require_classes")
    if classes < need:
        problems.append(
            f"at least {need} different character types "
            f"(lower/uppercase letters, digits, special characters) - currently {classes}")

    # Compare without accents/case, so "Pa$$word" is also caught.
    flat = unicodedata.normalize("NFKD", pw).lower()
    flat_simple = (flat.replace("$", "s").replace("0", "o").replace("1", "i")
                       .replace("3", "e").replace("@", "a").replace("!", "i"))
    for bad in _TRIVIAL:
        if bad in flat_simple:
            problems.append(f"contains a very common word ({bad})")
            break

    if setting("pw_block_username") and username:
        u = username.strip().lower()
        if len(u) >= 3 and u in flat:
            problems.append("contains the username")

    if pw and len(set(pw)) <= 3:
        problems.append("consists of too few different characters")

    return problems


def describe_policy() -> str:
    """One-liner for error messages and input fields."""
    return (f"At least {setting('pw_min_length')} characters and "
            f"{setting('pw_require_classes')} different character types "
            f"(lower/uppercase letters, digits, special characters).")


# ==========================================================================
# 2. Protection against password guessing
# ==========================================================================
# The failed attempts live in memory: they are short-lived, and a backend
# restart should not lock anyone out permanently. Whoever needs the lock across
# restarts moves it into the database - but then with regular cleanup.
_failed: dict[str, list[float]] = {}
_locked: dict[str, float] = {}


def _key(kind: str, value: str) -> str:
    return f"{kind}:{(value or '').strip().lower()}"


def _prune(key: str, now: float) -> list[float]:
    window = setting("login_window_minutes") * 60
    hits = [t for t in _failed.get(key, []) if now - t < window]
    if hits:
        _failed[key] = hits
    else:
        _failed.pop(key, None)
    return hits


def lock_status(username: str, ip: str = "") -> tuple[bool, int]:
    """
    (locked?, remaining seconds). Called BEFORE the password check - otherwise
    the lock could be bypassed by quickly continuing to try.
    """
    now = time.time()
    for key in (_key("user", username), _key("ip", ip) if ip else None):
        if not key:
            continue
        until = _locked.get(key)
        if until and until > now:
            return True, int(until - now)
        if until:
            _locked.pop(key, None)
    return False, 0


def note_failure(username: str, ip: str = "") -> tuple[bool, int]:
    """
    Record a failed attempt. Returns: (now locked?, remaining seconds).
    The caller additionally writes an audit entry - unauthorized login attempts
    must be traceable per OPS.1.1.5.
    """
    now = time.time()
    max_tries = setting("login_max_attempts")
    lock_for = setting("login_lock_minutes") * 60
    locked = False

    for key in (_key("user", username), _key("ip", ip) if ip else None):
        if not key:
            continue
        hits = _prune(key, now)
        hits.append(now)
        _failed[key] = hits
        if len(hits) >= max_tries:
            _locked[key] = now + lock_for
            _failed.pop(key, None)
            locked = True

    return locked, int(lock_for) if locked else 0


def note_success(username: str, ip: str = "") -> None:
    """After a successful login the count is void."""
    for key in (_key("user", username), _key("ip", ip) if ip else None):
        if key:
            _failed.pop(key, None)
            _locked.pop(key, None)


def unlock(username: str) -> None:
    """Manual unlock by an administrator."""
    _locked.pop(_key("user", username), None)
    _failed.pop(_key("user", username), None)


def locked_accounts() -> list[dict]:
    """Currently locked accounts/addresses - for display in the dashboard."""
    now = time.time()
    out = []
    for key, until in list(_locked.items()):
        if until <= now:
            _locked.pop(key, None)
            continue
        kind, _, value = key.partition(":")
        out.append({"kind": kind, "value": value, "seconds_left": int(until - now)})
    return out
