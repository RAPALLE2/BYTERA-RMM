"""
agent.py
--------
The program that runs on EVERY machine you want to manage via BYTERA RMM
(your PC, a server, another Windows/Linux machine).

What the agent does:
  1. Connects to the backend via Socket.IO (namespace "/agent")
  2. Registers once ("register") with hostname/OS/IP
  3. Sends a "heartbeat" with current metrics every 5 seconds
     (CPU load, RAM, disk, uptime) - for this we use the
     "psutil" library, which works the same on Windows AND Linux
  4. Waits for commands from the backend:
       - "exec"    -> runs a shell command, sends the result back
       - "fs-list" -> lists a folder, sends the list back

If the agent is started several times in a row, it keeps the same unique ID
via the ".device-id" file - so the backend does not "forget" the client
on every restart.
"""

# IMPORTANT: makes all type annotations "lazy" (stored as strings and not
# evaluated at runtime). This makes the modern syntax "str | None" work on
# Python 3.8/3.9 too (many Windows installations), where it would otherwise
# raise a TypeError. MUST be the very first statement after the docstring.
from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import io
import json
import logging
import os
import platform
import contextvars
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path


# --------------------------------------------------------------
# Self-bootstrap: automatically install missing libraries.
# After installation the agent consists of agent.py + requirements.txt.
# If a lib is missing (e.g. because requirements.txt was extended), we
# install it here before importing it - otherwise the import crashes.
# --------------------------------------------------------------
def _bootstrap_deps() -> None:
    # PyPI name -> import name (only where they differ)
    needed = {
        "python-socketio[asyncio_client]": "socketio",
        "aiohttp": "aiohttp",
        "psutil": "psutil",
        "python-dotenv": "dotenv",
        "mss": "mss",
        "Pillow": "PIL",
        "pynput": "pynput",
        "pyperclip": "pyperclip",
    }
    # Windows: pywinpty for the interactive terminal (ConPTY).
    if os.name == "nt":
        needed["pywinpty"] = "winpty"
    import importlib
    missing_specs = []
    for spec, mod in needed.items():
        try:
            importlib.import_module(mod)
        except Exception:
            missing_specs.append(spec)
    if not missing_specs:
        return
    print(f"[agent-bootstrap] Installing missing packages: {', '.join(missing_specs)}")
    req = Path(__file__).resolve().parent / "requirements.txt"
    # Here too, do not let a console window flash up (Windows). _run is not
    # defined this early, so the flags are inline.
    _boot_kw = {}
    if platform.system() == "Windows":
        _si = subprocess.STARTUPINFO()
        _si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        _si.wShowWindow = 0
        _boot_kw = {"startupinfo": _si, "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    try:
        if req.is_file():
            subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(req)],
                           check=False, timeout=600, **_boot_kw)
        else:
            subprocess.run([sys.executable, "-m", "pip", "install", *missing_specs],
                           check=False, timeout=600, **_boot_kw)
    except Exception as e:
        print(f"[agent-bootstrap] pip failed: {e}")


_bootstrap_deps()

import psutil
import socketio
from dotenv import load_dotenv

# --- Set up logging: writes both to the console and to a file "agent.log"
# next to this script. This way errors can be seen even when the agent runs
# invisibly as a background service / scheduled task. ---
_LOG_FILE = Path(__file__).resolve().parent / "agent.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[
        logging.FileHandler(_LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("agent")


# ------------------------------------------------------------------
# WATCHDOG (self-restart even on HARD crashes)
# ------------------------------------------------------------------
# The existing crash guard below (os.execv after a traceback) only catches
# PYTHON errors. If the process instead dies natively (e.g. a segfault in
# mss/Pillow while grabbing the screen after a session switch / lock screen,
# OOM kill, scheduler kill), NOTHING is logged and nobody restarts it -
# exactly the observed "crashed at the end, no self-restart".
#
# Solution: at startup this process first runs as a tiny WATCHDOG
# (standard library only, cannot crash natively itself) and starts the
# actual agent as a CHILD PROCESS (environment variable RMM_SUPERVISED=1).
# If the child dies - no matter how - the exit code is logged, noted in
# last_crash.txt and restarted after a short wait
# (backoff on crash loops, max. 60s). Exit code 0 = intended shutdown,
# then the watchdog exits too.
# Can be disabled with RMM_NO_WATCHDOG=1 (e.g. for debugging in the console).
def _run_watchdog() -> "int":
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    crash_file = os.path.join(here, "last_crash.txt")
    fast_fails = 0
    log.info("[agent] Watchdog active (PID %s) - supervising the agent process.", os.getpid())
    while True:
        env = dict(os.environ)
        env["RMM_SUPERVISED"] = "1"
        started = time.time()
        try:
            child = subprocess.Popen([sys.executable] + sys.argv, cwd=here, env=env)
            code = child.wait()
        except KeyboardInterrupt:
            return 0
        except Exception as e:
            log.error("[agent] Watchdog: agent start failed: %r", e)
            code = -1
        ran = time.time() - started
        if code == 0:
            log.info("[agent] Watchdog: agent exited normally (exit 0) - watchdog ends.")
            return 0
        log.error("[agent] Watchdog: agent process died (exit code %s) after %.0fs runtime.", code, ran)
        try:
            # Only write if the agent itself left no traceback (hard/native
            # death) - Python crashes write the file below.
            if ran > 2:
                with open(crash_file, "w", encoding="utf-8") as f:
                    f.write(time.strftime("%Y-%m-%d %H:%M:%S")
                            + f"\nHard process death without a Python traceback (exit code {code},"
                              f" runtime {ran:.0f}s).\nTypical causes: native crash in"
                              " mss/Pillow/pynput, OOM kill, external termination.\n")
        except OSError:
            pass
        fast_fails = fast_fails + 1 if ran < 300 else 0
        delay = min(60, 5 * (fast_fails + 1))
        log.info("[agent] Watchdog: restarting in %ss...", delay)
        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            return 0


if (__name__ == "__main__"
        and os.environ.get("RMM_SUPERVISED") != "1"
        and os.environ.get("RMM_NO_WATCHDOG") != "1"
        # Safety net: should agent.py ever be called with --screen-helper
        # (old task, script by hand), it must not spawn a second full agent.
        and "--screen-helper" not in sys.argv):
    sys.exit(_run_watchdog())


def _print(msg):
    """Replaces print() - writes to the console AND the log file."""
    log.info(msg)


# --- Agent console for the dashboard -------------------------------------
# Rolling buffer of ALL log lines of this agent. The dashboard can fetch the
# history via "agent-console-open" and then read along live
# (client terminal -> "Agent console" switch). The handler is attached to the
# root logger, so it captures everything that goes through log/_print.
import collections as _collections

_CONSOLE_BUFFER = _collections.deque(maxlen=2000)
_console_stream = {"on": False}


class _ConsoleBufferHandler(logging.Handler):
    def emit(self, record):
        try:
            line = self.format(record)
        except Exception:
            return
        _CONSOLE_BUFFER.append(line)
        if not _console_stream["on"]:
            return
        # Forward live to the backend (best effort; sio/loop only exist
        # after startup - before that it is only buffered).
        try:
            if "sio" in globals() and sio.connected and "_AGENT_LOOP" in globals():
                asyncio.run_coroutine_threadsafe(
                    sio.emit("agent-console", {"id": DEVICE_ID, "data": line + "\r\n"},
                             namespace="/agent"),
                    _AGENT_LOOP,
                )
        except Exception:
            pass


_console_handler = _ConsoleBufferHandler()
_console_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
logging.getLogger().addHandler(_console_handler)


# Optional libraries for Remote Screen. The agent works without them too
# (then only remote control is disabled, everything else keeps running).
# We deliberately catch EVERY error (not only ImportError), because e.g. pynput
# can fail to import in an environment without a graphical session (Windows
# service as SYSTEM, Linux without X11) with other errors - this must NOT
# cripple the agent completely.
# IMPORTANT: remember the reason for the failure. Previously the exception was
# swallowed silently - the user then only saw "mss/Pillow missing" and had no
# clue WHY. Usually the packages are not missing at all but broken (half
# install, wrong interpreter, missing system library). That is exactly what is
# now in _SCREEN_ERROR.
_SCREEN_ERROR = ""
_INPUT_ERROR = ""

try:
    import mss  # take screenshots (fast, cross-platform)
    from PIL import Image  # to shrink/encode the screenshots
    _SCREEN_AVAILABLE = True
except Exception as _e:
    _SCREEN_AVAILABLE = False
    _SCREEN_ERROR = f"{type(_e).__name__}: {_e}"

try:
    from pynput.mouse import Controller as MouseController, Button as MouseButton
    from pynput.keyboard import Controller as KeyboardController, Key as KeyboardKey
    _INPUT_AVAILABLE = True
except Exception as _e:
    _INPUT_AVAILABLE = False
    _INPUT_ERROR = f"{type(_e).__name__}: {_e}"

# Load the .env file from the same folder as this script
load_dotenv(Path(__file__).resolve().parent / ".env")

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:4000")
AGENT_TOKEN = os.getenv("AGENT_TOKEN", "change-me-super-secret")
ENROLLMENT_TOKEN = os.getenv("ENROLLMENT_TOKEN", "").strip() or None
DEVICE_NAME = os.getenv("DEVICE_NAME") or socket.gethostname()

# ---------------------------------------------------------------------------
# Language of the DEVICE dialogs (consent to the remote screen)
# ---------------------------------------------------------------------------
# These texts are seen by the person sitting IN FRONT OF the device - not the
# user in the dashboard. So the dashboard language cannot apply here: several
# people with different language settings can view the same device, and the
# agent does not learn which one that is when the connection is established.
#
# The language therefore belongs to the device and is set in the .env:
#     AGENT_LANG=de
# The default is "en" (the project standard); set AGENT_LANG=de in the .env to
# keep the device dialogs in German.
AGENT_LANG = (os.getenv("AGENT_LANG", "en") or "en").strip().lower()[:2]
if AGENT_LANG not in ("de", "en"):
    AGENT_LANG = "de"

_AGENT_TEXTS = {
    "de": {
        "consent_window": "BYTERA RMM - Remote-Bildschirm",
        "consent_title": "Remote-Bildschirm zulassen?",
        "consent_requested": "Remote-Bildschirm angefragt",
        "consent_body": "{who} möchte den Bildschirm dieses Computers\nsehen und steuern.",
        "consent_body_1line": "{who} möchte den Bildschirm dieses Computers sehen und steuern.",
        "consent_ask": "Zulassen? (Ohne Antwort wird nach {secs} Sekunden automatisch abgelehnt.)",
        "allow": "Zulassen",
        "allow_check": "✓ Zulassen",
        "deny": "Ablehnen",
        "auto_deny_in": "Automatisch abgelehnt in {secs} s",
        # ---- Log output (agent.log + agent console in the dashboard) ----
        # The logs follow AGENT_LANG too: whoever rolls the agent out in
        # German should not have to read them in English.
        "log_device_name": "Gerätename : {name}",
        "log_device_id": "Geräte-ID  : {id}",
        "log_backend": "Backend    : {url}",
        "log_remote_ctl": "Fernsteuerung: {state}",
        "log_available": "verfügbar",
        "log_disabled": "deaktiviert",
        "log_session": "Sitzung {sid} | aktive Konsolensitzung {csid} | Bildschirm-Helfer: {helper}",
        "log_helper_used": "wird verwendet (Dienst in Sitzung 0)",
        "log_helper_unused": "nicht nötig",
        "log_connected": "Verbunden mit {url} als '{name}' ({id})",
        "log_disconnected": "Verbindung getrennt.",
        "log_conn_failed": "Verbindung zu {url} fehlgeschlagen ({err}), neuer Versuch in 3s...",
        "log_screen_started": "Bildschirm-Streaming gestartet",
        "log_screen_stopped": "Bildschirm-Streaming gestoppt",
        "log_screen_allowed": "Remote-Bildschirm am Gerät ZUGELASSEN",
        "log_screen_denied": "Remote-Bildschirm am Gerät ABGELEHNT (oder keine Antwort)",
        "log_nobody_home": "Remote-Bildschirm: niemand angemeldet - verbinde ohne Abfrage",
        "log_monitor_switched": "Bildschirm gewechselt auf #{n}",
        "log_update_sent": "Update-Bestätigung an das Backend gesendet.",
        "log_metrics_failed": "Fehler beim Senden der Metriken: {err}",
        "log_input_error": "Eingabe-Fehler: {err}",
    },
    "en": {
        "consent_window": "BYTERA RMM - Remote screen",
        "consent_title": "Allow remote screen access?",
        "consent_requested": "Remote screen requested",
        "consent_body": "{who} would like to view and control\nthe screen of this computer.",
        "consent_body_1line": "{who} would like to view and control the screen of this computer.",
        "consent_ask": "Allow? (Without an answer this is denied automatically after {secs} seconds.)",
        "allow": "Allow",
        "allow_check": "✓ Allow",
        "deny": "Deny",
        "auto_deny_in": "Automatically denied in {secs} s",
        # ---- Log output (agent.log + agent console in the dashboard) ----
        "log_device_name": "Device name : {name}",
        "log_device_id": "Device ID   : {id}",
        "log_backend": "Backend     : {url}",
        "log_remote_ctl": "Remote control: {state}",
        "log_available": "available",
        "log_disabled": "disabled",
        "log_session": "Session {sid} | active console session {csid} | screen helper: {helper}",
        "log_helper_used": "in use (service in session 0)",
        "log_helper_unused": "not needed",
        "log_connected": "Connected to {url} as '{name}' ({id})",
        "log_disconnected": "Disconnected.",
        "log_conn_failed": "Connection to {url} failed ({err}), retrying in 3s...",
        "log_screen_started": "Screen streaming started",
        "log_screen_stopped": "Screen streaming stopped",
        "log_screen_allowed": "Remote screen ALLOWED on the device",
        "log_screen_denied": "Remote screen DENIED on the device (or no answer)",
        "log_nobody_home": "Remote screen: nobody logged in - connecting without asking",
        "log_monitor_switched": "Switched to monitor #{n}",
        "log_update_sent": "Update confirmation sent to the backend.",
        "log_metrics_failed": "Failed to send metrics: {err}",
        "log_input_error": "Input error: {err}",
    },
}


def _at(key: str, **kw) -> str:
    """Text for the device dialogs in the language from AGENT_LANG."""
    tbl = _AGENT_TEXTS.get(AGENT_LANG) or _AGENT_TEXTS["de"]
    txt = tbl.get(key) or _AGENT_TEXTS["de"].get(key) or key
    return txt.format(**kw) if kw else txt

IS_WINDOWS = platform.system() == "Windows"

# --------------------------------------------------------------------------
# ALWAYS start subprocesses without a visible console window. On Windows
# subprocess.run() would otherwise briefly open a black CMD/PowerShell window
# on every call - that must never happen during the 5-second heartbeat.
# _run() wraps the necessary flags (CREATE_NO_WINDOW + hidden STARTUPINFO)
# and is used EVERYWHERE from here on instead of subprocess.run().
# --------------------------------------------------------------------------
def _no_window_kwargs() -> dict:
    if not IS_WINDOWS:
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return {"startupinfo": si, "creationflags": flags}


def _run(cmd, **kwargs):
    """
    Replacement for subprocess.run that does not flash a window on Windows.

    IMPORTANT - character set: text=True alone uses the locale code page
    (cp1252 on German systems). winget and PowerShell, however, write UTF-8
    or characters that do not exist in cp1252 at all (0x81, 0x8d, 0x90,
    0x9d ...). The result was a UnicodeDecodeError IN THE MIDDLE of the scan -
    the calling function caught it and dutifully reported "0 updates found".
    That is why UTF-8 with errors='replace' is forced here: better a broken
    character in a name than a silently empty scan result.
    """
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    if kwargs.get("text"):
        kwargs.setdefault("encoding", "utf-8")
        kwargs.setdefault("errors", "replace")
    for k, v in _no_window_kwargs().items():
        kwargs.setdefault(k, v)
    return subprocess.run(cmd, **kwargs)


# TTL cache: do NOT run expensive queries (nvidia-smi, WMI, ping) on every
# 5s heartbeat, but at most every `ttl` seconds. In between the cached value
# is returned.
_ttl_cache: dict = {}


def _ttl(key: str, ttl: float, fn):
    now = time.time()
    ent = _ttl_cache.get(key)
    if ent and (now - ent[0]) < ttl:
        return ent[1]
    val = fn()
    _ttl_cache[key] = (now, val)
    return val


def get_os_release() -> str:
    """
    Determines a meaningful OS name:
    - Linux: reads /etc/os-release and returns e.g. "Ubuntu 22.04" or
      "Debian GNU/Linux 12" or "Proxmox VE" (instead of just the kernel version).
    - Windows: returns e.g. "Windows 11" or "Windows Server 2022".
    - Otherwise: platform.release() as a fallback.
    """
    try:
        if IS_WINDOWS:
            ver = platform.version()      # e.g. "10.0.22621"
            release = platform.release()  # "10" / "11" / "Server..."
            try:
                build = int(ver.split(".")[2])
                if "server" in release.lower():
                    return f"Windows Server ({ver})"
                if build >= 22000:
                    return "Windows 11"
                return "Windows 10"
            except (IndexError, ValueError):
                return f"Windows {release}"
        else:
            info = {}
            try:
                with open("/etc/os-release", "r", encoding="utf-8") as f:
                    for line in f:
                        if "=" in line:
                            k, v = line.rstrip("\n").split("=", 1)
                            info[k] = v.strip().strip('"')
            except FileNotFoundError:
                pass
            if info.get("PRETTY_NAME"):
                return info["PRETTY_NAME"]
            if info.get("NAME"):
                return f"{info['NAME']} {info.get('VERSION_ID', '')}".strip()
    except Exception:
        pass
    return platform.release()


OS_RELEASE = get_os_release()

# File in which we remember our own, persistent device ID
ID_FILE = Path(__file__).resolve().parent / ".device-id"


def get_or_create_device_id() -> str:
    """Reads the stored device ID, or creates a new one on the very first start."""
    if ID_FILE.exists():
        return ID_FILE.read_text().strip()
    new_id = str(uuid.uuid4())
    ID_FILE.write_text(new_id)
    return new_id


DEVICE_ID = get_or_create_device_id()

# After an update the update script creates the marker file ".updated". On the
# next start the agent reads it, reports "updated: true" to the backend (so the
# dashboard can confirm the update as successful) and deletes it.
UPDATED_MARKER = Path(__file__).resolve().parent / ".updated"


def _consume_update_marker() -> bool:
    try:
        if UPDATED_MARKER.exists():
            UPDATED_MARKER.unlink()
            return True
    except Exception:
        pass
    return False


_JUST_UPDATED = _consume_update_marker()

# Own agent version (from version.txt next to this script), so the dashboard
# can detect "outdated" agents.
def _read_agent_version() -> str:
    try:
        return (Path(__file__).resolve().parent / "version.txt").read_text(encoding="utf-8").strip() or "unknown"
    except Exception:
        return "unknown"


AGENT_VERSION = _read_agent_version()


def _read_agent_code_hash() -> str:
    """
    Checksum of our own source code.

    version.txt is regularly forgotten during development. If it still holds
    the same number, the backend considers an ancient agent up to date and
    never sends an update - which is exactly why the patch handlers were
    missing on the clients recently, even though agent.py in the project was
    long newer. The checksum, by contrast, tells the truth about the code that
    is really running.
    """
    try:
        import hashlib
        data = Path(__file__).resolve().read_bytes()
        return hashlib.sha256(data).hexdigest()[:16]
    except Exception:
        return ""


AGENT_CODE_HASH = _read_agent_code_hash()

# The Socket.IO client through which all communication runs.
# Create ONE fixed event loop and set it as current BEFORE the socketio client
# is created. Reason: 'sio' is built at import time; if main() were later
# started via asyncio.run(), everything would run on a DIFFERENT loop
# -> "Future attached to a different loop". We run main() below explicitly on
# exactly this loop (run_until_complete).
try:
    _AGENT_LOOP = asyncio.get_event_loop()
    if _AGENT_LOOP.is_closed():
        raise RuntimeError("closed")
except RuntimeError:
    _AGENT_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_AGENT_LOOP)

# reconnection=False: we control reconnecting OURSELVES (see main()).
# Otherwise the internal auto-reconnection and our loop fight each other
# -> errors "Already connected" and "Future attached to a different loop",
# because two connection life cycles overlap.
sio = socketio.AsyncClient(reconnection=False)


# ==========================================================================
# END-TO-END ENCRYPTION + TLS PINNING  (counterpart: backend app/security.py)
# --------------------------------------------------------------------------
# * With the server's PUBLIC key (E2E_SERVER_KEY in .env - built into new
#   installers, otherwise fetched once and then pinned) every message to and
#   from the backend is encrypted with AES-256-GCM (P-256 ECDH + HKDF). The
#   agent token is sent only encrypted. Works over plain http://, behind
#   reverse proxies and Cloudflare - they only see ciphertext.
# * If the backend offers its own TLS port (TLS_PORT/TLS_PIN), the agent uses it
#   with certificate pinning; if that port is unreachable it falls back to
#   BACKEND_URL (still end-to-end encrypted).
# * Once a key is pinned, the agent NEVER falls back to unencrypted.
# ==========================================================================
try:
    from cryptography.hazmat.primitives import hashes as _c_hashes, serialization as _c_ser
    from cryptography.hazmat.primitives.asymmetric import ec as _c_ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM as _C_AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF as _C_HKDF
    _E2E_LIB = True
except Exception as _e:                                       # pragma: no cover
    _E2E_LIB = False
    _E2E_LIB_ERR = f"{type(_e).__name__}: {_e}"

_ENV_FILE = Path(__file__).resolve().parent / ".env"
E2E_SERVER_KEY = (os.getenv("E2E_SERVER_KEY") or "").strip()
TLS_PIN = (os.getenv("TLS_PIN") or "").strip().lower()
try:
    TLS_PORT = int(os.getenv("TLS_PORT") or 0)
except ValueError:
    TLS_PORT = 0
_E2E = {"ch": None, "tls_bad_until": 0.0, "mode": "plain", "key_refused": 0}
_e2e_inner = contextvars.ContextVar("e2e_inner", default=False)
_E2E_INFO = b"bytera-e2e-v1|"
_E2E_WINDOW = 4096


def _env_store(values: dict) -> None:
    """Set KEY=value lines in the agent .env (atomic)."""
    try:
        lines = _ENV_FILE.read_text(encoding="utf-8").splitlines() if _ENV_FILE.exists() else []
        done = set()
        for i, ln in enumerate(lines):
            k = ln.split("=", 1)[0].strip()
            if k in values:
                lines[i] = f"{k}={values[k]}"
                done.add(k)
        lines += [f"{k}={v}" for k, v in values.items() if k not in done]
        tmp = _ENV_FILE.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, _ENV_FILE)
    except Exception as e:
        _print(f"[e2e] could not write .env: {e}")


def _e2e_kid(key_b64: str) -> str:
    import base64 as _b64
    import hashlib as _hl
    return _hl.sha256(_b64.b64decode(key_b64 + "=" * (-len(key_b64) % 4))).hexdigest()[:16]


class _E2EChan:
    def __init__(self, k_send, k_recv, p_send, p_recv, first_send=0):
        self._s, self._r = _C_AESGCM(k_send), _C_AESGCM(k_recv)
        self._ps, self._pr = p_send, p_recv
        self._cs, self._hi, self._seen = first_send, -1, set()
        self._lock = threading.Lock()

    def seal(self, data: bytes, aad: bytes = b"") -> bytes:
        import struct as _st
        with self._lock:
            n = self._ps + _st.pack(">Q", self._cs)
            self._cs += 1
        return n + self._s.encrypt(n, data, aad or None)

    def open(self, blob: bytes, aad: bytes = b"") -> bytes:
        import struct as _st
        if len(blob) < 28 or blob[:4] != self._pr:
            raise ValueError("bad message")
        ctr = _st.unpack(">Q", blob[4:12])[0]
        with self._lock:
            if ctr in self._seen or ctr <= self._hi - _E2E_WINDOW:
                raise ValueError("replayed message")
            data = self._r.decrypt(blob[:12], blob[12:], aad or None)
            self._seen.add(ctr)
            if ctr > self._hi:
                self._hi = ctr
                if len(self._seen) > 2 * _E2E_WINDOW:
                    self._seen = {c for c in self._seen if c > self._hi - _E2E_WINDOW}
        return data


def _e2e_new_channel() -> "tuple[_E2EChan, bytes, bytes, str]":
    """Fresh ephemeral key -> (channel, own public key, salt, kid)."""
    import base64 as _b64
    srv = _b64.b64decode(E2E_SERVER_KEY + "=" * (-len(E2E_SERVER_KEY) % 4))
    kid = _e2e_kid(E2E_SERVER_KEY)
    eph = _c_ec.generate_private_key(_c_ec.SECP256R1())
    salt = os.urandom(16)
    shared = eph.exchange(_c_ec.ECDH(), _c_ec.EllipticCurvePublicKey.from_encoded_point(_c_ec.SECP256R1(), srv))
    okm = _C_HKDF(algorithm=_c_hashes.SHA256(), length=64, salt=salt, info=_E2E_INFO + kid.encode()).derive(shared)
    ch = _E2EChan(okm[:32], okm[32:], b"c2s\x00", b"s2c\x00")
    epk = eph.public_key().public_bytes(_c_ser.Encoding.X962, _c_ser.PublicFormat.UncompressedPoint)
    return ch, epk, salt, kid


def _e2e_pack(event: str, args: list) -> bytes:
    import json as _js
    import struct as _st
    blobs = []

    def walk(v):
        if isinstance(v, (bytes, bytearray, memoryview)):
            blobs.append(bytes(v))
            return {"\u0000b": len(blobs) - 1}
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [walk(x) for x in v]
        return v
    head = _js.dumps({"e": event, "a": walk(list(args)), "b": [len(b) for b in blobs]},
                     separators=(",", ":"), default=str).encode()
    return _st.pack(">I", len(head)) + head + b"".join(blobs)


def _e2e_unpack(data: bytes):
    import json as _js
    import struct as _st
    hl = _st.unpack(">I", data[:4])[0]
    head = _js.loads(data[4:4 + hl])
    blobs, pos = [], 4 + hl
    for n in head.get("b") or []:
        blobs.append(data[pos:pos + n])
        pos += n

    def walk(v):
        if isinstance(v, dict):
            if len(v) == 1 and "\u0000b" in v:
                return blobs[v["\u0000b"]]
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v
    return str(head["e"]), walk(head.get("a") or [])


def _e2e_auth() -> "tuple[dict, _E2EChan]":
    import base64 as _b64
    import json as _js
    ch, epk, salt, kid = _e2e_new_channel()
    box = ch.seal(_js.dumps({"token": AGENT_TOKEN, "ts": time.time()}).encode(), aad=b"auth")
    return ({"e2e": {"v": 1, "kid": kid, "epk": _b64.b64encode(epk).decode(),
                     "n": _b64.b64encode(salt).decode(), "t": _b64.b64encode(box).decode()}}, ch)


def _e2e_learn(info: dict, how: str) -> bool:
    """Take over the server key (first contact) and TLS data. Returns True if the key is new."""
    global E2E_SERVER_KEY, TLS_PIN, TLS_PORT
    key = str(info.get("key") or "").strip()
    new_key = bool(key) and not E2E_SERVER_KEY
    vals = {}
    if new_key:
        E2E_SERVER_KEY = key
        vals["E2E_SERVER_KEY"] = key
        _print(f"[e2e] server key learned ({how}) - from now on everything is end-to-end encrypted "
               f"(key id {_e2e_kid(key)})")
    pin = str(info.get("tls_pin") or "").strip().lower()
    try:
        port = int(info.get("tls_port") or 0)
    except ValueError:
        port = 0
    if pin and (not TLS_PIN or how == "encrypted channel"):
        if pin != TLS_PIN:
            TLS_PIN = pin
            vals["TLS_PIN"] = pin
    if port != TLS_PORT and (how == "encrypted channel" or not TLS_PORT):
        TLS_PORT = port
        vals["TLS_PORT"] = str(port)
    if vals:
        _env_store(vals)
        _node_cfg_sync()
    return new_key


def _node_cfg_sync() -> None:
    """An installed node gets the same pinned key / TLS data (used from its next start)."""
    try:
        f = NODE_DIR / "node.json"
        if not f.is_file() or not _E2E_LIB:
            return
        import json as _js
        cfg = _js.loads(f.read_text(encoding="utf-8"))
        want = {"e2e_key": E2E_SERVER_KEY, "tls_pin": TLS_PIN, "tls_port": TLS_PORT}
        if any(cfg.get(k) != v for k, v in want.items()):
            cfg.update(want)
            f.write_text(_js.dumps(cfg), encoding="utf-8")
            _print("[e2e] node configuration updated (encryption active from the next node start)")
    except Exception as e:
        _print(f"[e2e] node configuration not updated: {e}")


def _e2e_fetch_info() -> "dict | None":
    """First contact without a built-in key: ask the backend for its PUBLIC key."""
    import json as _js
    import urllib.request
    try:
        with urllib.request.urlopen(BACKEND_URL.rstrip("/") + "/api/security/info", timeout=20) as r:
            return _js.loads(r.read() or b"{}")
    except Exception as e:
        _print(f"[e2e] server key not available yet ({e}) - connecting without encryption for now")
        return None


def _e2e_ensure_lib() -> None:
    """Older installations lack 'cryptography' - install it once in the background."""
    if _E2E_LIB:
        return

    def run():
        try:
            _print(f"[e2e] 'cryptography' missing ({_E2E_LIB_ERR}) - installing it for encryption …")
            r = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "cryptography"],
                               capture_output=True, text=True, timeout=900)
            if r.returncode == 0:
                _print("[e2e] 'cryptography' installed - encryption is active after the next agent restart")
            else:
                _print(f"[e2e] pip failed: {(r.stderr or r.stdout)[-300:]}")
        except Exception as e:
            _print(f"[e2e] pip failed: {e}")
    threading.Thread(target=run, name="e2e-pip", daemon=True).start()


# --- wrap emit / incoming events -----------------------------------------
_raw_emit = sio.emit
_raw_trigger = sio._trigger_event


async def _e2e_emit(event, data=None, namespace=None, callback=None):
    ch = _E2E["ch"]
    if ch is not None and namespace == "/agent" and event != "x":
        args = [] if data is None else (list(data) if isinstance(data, tuple) else [data])
        return await _raw_emit("x", ch.seal(_e2e_pack(event, args)), namespace=namespace, callback=callback)
    return await _raw_emit(event, data, namespace=namespace, callback=callback)


async def _e2e_trigger(event, namespace, *args):
    if (_E2E["ch"] is not None and namespace == "/agent" and not _e2e_inner.get()
            and event not in ("connect", "disconnect", "connect_error", "x", "__disconnect_final")):
        _print(f"[e2e] unencrypted event '{event}' dropped (injection?)")
        return None
    return await _raw_trigger(event, namespace, *args)


sio.emit = _e2e_emit
sio._trigger_event = _e2e_trigger


@sio.on("x", namespace="/agent")
async def _e2e_in(data):
    ch = _E2E["ch"]
    if ch is None or not isinstance(data, (bytes, bytearray)):
        return None
    try:
        event, args = _e2e_unpack(ch.open(bytes(data)))
    except Exception as e:
        _print(f"[e2e] message rejected: {e}")
        return None
    tok = _e2e_inner.set(True)
    try:
        return await _raw_trigger(event, "/agent", *args)
    finally:
        _e2e_inner.reset(tok)


@sio.on("e2e-offer", namespace="/agent")
async def _on_e2e_offer(info):
    """Plain connection + the server offers its key -> learn it and reconnect encrypted."""
    if _E2E["ch"] is not None or not _E2E_LIB or E2E_SERVER_KEY:
        return
    if _e2e_learn(info or {}, "first contact"):
        _print("[e2e] reconnecting encrypted …")
        asyncio.get_running_loop().call_later(2, lambda: asyncio.ensure_future(sio.disconnect()))


@sio.on("security", namespace="/agent")
async def _on_security(info):
    """Encrypted: TLS port + certificate fingerprint of the backend."""
    old = (TLS_PORT, TLS_PIN)
    _e2e_learn(info or {}, "encrypted channel")
    if (TLS_PORT, TLS_PIN) != old and TLS_PORT and _E2E["mode"] == "e2e":
        _print(f"[e2e] backend offers TLS on port {TLS_PORT} - used from the next connection")


def _connect_plan() -> "list[tuple[str, str]]":
    """[(url, 'tls'|'plain'), ...] - own TLS port first (pinned), then BACKEND_URL."""
    from urllib.parse import urlparse
    plan = []
    if E2E_SERVER_KEY and _E2E_LIB and TLS_PORT and TLS_PIN and time.time() > _E2E["tls_bad_until"]:
        u = urlparse(BACKEND_URL)
        if u.scheme == "http" and u.hostname:
            host = f"[{u.hostname}]" if ":" in u.hostname else u.hostname
            plan.append((f"https://{host}:{TLS_PORT}", "tls"))
    plan.append((BACKEND_URL, "plain"))
    return plan


async def _set_transport(kind: str) -> None:
    """Pinned TLS session for the own TLS port, normal session otherwise."""
    import aiohttp
    eio = sio.eio
    if eio.http is not None and not eio.http.closed:
        try:
            await eio.http.close()
        except Exception:
            pass
    eio.http = None
    eio.external_http = False
    if kind == "tls":
        eio.http = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=aiohttp.Fingerprint(bytes.fromhex(TLS_PIN))))


async def _sio_connect_secure() -> None:
    """Connect: encrypted when possible, pinned TLS when possible, never silently weaker."""
    if not E2E_SERVER_KEY and _E2E_LIB:
        info = await asyncio.get_running_loop().run_in_executor(None, _e2e_fetch_info)
        if info and info.get("key"):
            _e2e_learn(info, "first contact")
    last_err = None
    for url, kind in _connect_plan():
        if E2E_SERVER_KEY and _E2E_LIB:
            auth, ch = _e2e_auth()
            _E2E["ch"] = ch
        else:
            auth, ch = {"token": AGENT_TOKEN}, None
            _E2E["ch"] = None
        try:
            await _set_transport(kind)
            _print(f"[agent] connecting to {url} (namespace /agent, "
                   f"{'end-to-end encrypted' if ch else 'NOT encrypted'}{', pinned TLS' if kind == 'tls' else ''})")
            await sio.connect(url, auth=auth, namespaces=["/agent"], wait_timeout=30,
                              transports=["websocket"] if kind == "tls" else None)
            _E2E["mode"] = "e2e" if ch else "plain"
            return
        except Exception as e:
            last_err = e
            _E2E["ch"] = None
            if "unknown server key" in repr(e):
                _E2E["key_refused"] += 1
                _print("[e2e] the backend has a DIFFERENT key than the one pinned here. For safety the agent "
                       "does not accept it automatically. If the server was reinstalled, remove E2E_SERVER_KEY/"
                       "TLS_PIN from the agent .env (or reinstall the agent).")
            if kind == "tls":
                _E2E["tls_bad_until"] = time.time() + 3600
                _print(f"[e2e] TLS port not usable ({type(e).__name__}: unreachable or certificate fingerprint "
                       f"does not match) - using {BACKEND_URL}, still end-to-end encrypted")
                try:
                    await sio.disconnect()
                except Exception:
                    pass
                continue
            raise
    if last_err:
        raise last_err


def _backend_fetch(path: str, timeout: int = 120, limit: int = 64 * 1024 * 1024) -> bytes:
    """GET a backend file with the agent token - end-to-end encrypted when possible."""
    import base64 as _b64
    import json as _js
    import urllib.request
    if E2E_SERVER_KEY and _E2E_LIB:
        ch, epk, salt, kid = _e2e_new_channel()
        req = {"m": "GET", "p": path, "h": {"X-Agent-Token": AGENT_TOKEN}, "ts": time.time()}
        body = b"BXE1" + bytes.fromhex(kid) + epk + salt + ch.seal(_js.dumps(req).encode(), aad=b"req")
        r = urllib.request.Request(BACKEND_URL.rstrip("/") + "/api/e2e", data=body, method="POST",
                                   headers={"Content-Type": "application/octet-stream"})
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            out = _js.loads(ch.open(resp.read(), aad=b"resp"))
        if int(out.get("s") or 0) != 200:
            raise RuntimeError(f"HTTP {out.get('s')}")
        data = _b64.b64decode(out.get("b") or "")
    else:
        r = urllib.request.Request(BACKEND_URL.rstrip("/") + path, headers={"X-Agent-Token": AGENT_TOKEN})
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            data = resp.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError("download unexpectedly large")
    return data

# Set as soon as the backend has CONFIRMED our registration
# ('register-ack'). As long as it is not set, the agent stays silent -
# so it does not occupy server capacity that another agent currently
# needs for its own registration.
_REGISTERED = asyncio.Event()

# So that not all agents knock at the same moment (typical after a server
# restart), each agent delays its FIRST attempt by a fixed span derived from
# its own device ID. Fixed instead of random, so the order stays stable across
# restarts and the agents do not keep getting in each other's way.
CONNECT_SPREAD_S = max(0, int(os.getenv("AGENT_CONNECT_SPREAD_S", "30") or 30))


def get_local_ip() -> str | None:
    """Determines our own local IP address (see the explanation in network_scan.py in the backend)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


# --------------------------------------------------------------
# Connection setup & registration
# --------------------------------------------------------------

def _detect_device_type():
    """
    Best-effort detection of whether this machine is a VM, an LXC container or
    physical hardware. Result: "vm" | "lxc" | "physical" | None
    (None = unknown, then NOTHING is changed on the backend).
    This way the edit dialog automatically preselects the correct device type
    instead of wrongly defaulting to "Physical device".
    """
    try:
        sysname = platform.system().lower()
        if sysname == "linux":
            # 1) systemd-detect-virt is the most reliable source.
            try:
                out = subprocess.run(["systemd-detect-virt"], capture_output=True,
                                     text=True, timeout=5).stdout.strip().lower()
                if out and out != "none":
                    if out in ("lxc", "lxc-libvirt", "openvz", "systemd-nspawn", "docker", "podman"):
                        return "lxc"
                    return "vm"
                if out == "none":
                    return "physical"
            except Exception:
                pass
            # 2) container markers (PID 1 environment / /run files)
            try:
                env = Path("/proc/1/environ").read_bytes()
                if b"container=lxc" in env or Path("/run/systemd/container").exists():
                    return "lxc"
            except Exception:
                pass
            # 3) CPU hypervisor flag + DMI product name
            try:
                if "hypervisor" in Path("/proc/cpuinfo").read_text(errors="ignore"):
                    return "vm"
            except Exception:
                pass
            try:
                prod = Path("/sys/class/dmi/id/product_name").read_text(errors="ignore").lower()
                if any(k in prod for k in ("kvm", "qemu", "vmware", "virtualbox", "virtual machine", "hyper-v")):
                    return "vm"
                if prod.strip():
                    return "physical"
            except Exception:
                pass
            return None
        if sysname == "windows":
            # Query manufacturer/model - match typical VM signatures.
            try:
                out = subprocess.run(
                    ["wmic", "computersystem", "get", "manufacturer,model"],
                    capture_output=True, text=True, timeout=10).stdout.lower()
                if any(k in out for k in ("vmware", "virtualbox", "qemu", "kvm", "virtual machine", "xen", "parallels")):
                    return "vm"
                if out.strip():
                    return "physical"
            except Exception:
                pass
            return None
    except Exception:
        pass
    return None


DETECTED_DEVICE_TYPE = _detect_device_type()


# SMBIOS chassis types (DMI / Win32_SystemEnclosure)
_CHASSIS_LAPTOP = {8, 9, 10, 11, 12, 14, 18, 21, 30, 31, 32}
_CHASSIS_DESKTOP = {3, 4, 5, 6, 7, 13, 15, 16, 24, 34, 35, 36}
_CHASSIS_SERVER = {17, 22, 23, 25, 28, 29}


def _detect_device_kind():
    """
    Kind of device for the icon in the dashboard (same icons as the topology map):
    "laptop" | "pc" | "server" | "hypervisor" | "vm" | "lxc" | "docker" | None.
    Best effort - the user can override the icon in the edit dialog.
    """
    try:
        sysname = platform.system().lower()
        if sysname == "linux":
            virt = ""
            try:
                virt = subprocess.run(["systemd-detect-virt"], capture_output=True,
                                      text=True, timeout=5).stdout.strip().lower()
            except Exception:
                pass
            if virt in ("docker", "podman") or Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
                return "docker"
            if Path("/usr/bin/pveversion").exists() or Path("/etc/pve").is_dir():
                return "hypervisor"                       # Proxmox VE host
            if DETECTED_DEVICE_TYPE in ("vm", "lxc"):
                return DETECTED_DEVICE_TYPE
        elif DETECTED_DEVICE_TYPE == "vm":
            return "vm"
        try:
            if psutil.sensors_battery() is not None:
                return "laptop"
        except Exception:
            pass
        chassis = set()
        if sysname == "linux":
            try:
                chassis = {int(Path("/sys/class/dmi/id/chassis_type").read_text().strip())}
            except Exception:
                pass
        elif sysname == "windows":
            try:
                out = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     "(Get-CimInstance Win32_SystemEnclosure).ChassisTypes -join ','"],
                    capture_output=True, text=True, timeout=20,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
                chassis = {int(x) for x in out.replace(",", " ").split() if x.strip().isdigit()}
            except Exception:
                pass
        if chassis & _CHASSIS_LAPTOP:
            return "laptop"
        if chassis & _CHASSIS_SERVER:
            return "server"
        if sysname == "windows":
            try:
                if "server" in (platform.win32_edition() or "").lower() or "server" in platform.release().lower():
                    return "server"
            except Exception:
                pass
            return "pc"
        if sysname == "darwin":
            return "pc"
        if chassis & _CHASSIS_DESKTOP:
            # a desktop chassis without a graphical session manager is used as a server
            gui = any(Path(p).exists() for p in ("/usr/bin/gnome-shell", "/usr/bin/plasmashell",
                      "/usr/bin/xfce4-session", "/usr/sbin/lightdm", "/usr/sbin/gdm3", "/usr/sbin/sddm", "/usr/bin/cinnamon"))
            return "pc" if gui else "server"
        return "server"
    except Exception:
        return None


DETECTED_DEVICE_KIND = _detect_device_kind()


def _read_and_clear_last_crash():
    """
    Reads last_crash.txt (written by the crash guard in __main__), deletes the
    file and returns its (truncated) content - reported to the backend during
    registration and shown to the user there.
    """
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_crash.txt")
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read().strip()
        try:
            os.remove(path)
        except OSError:
            pass
        return content[-4000:] if content else None
    except Exception:
        return None


@sio.event(namespace="/agent")
async def connect():
    """Called automatically as soon as the connection to the backend is up."""
    global _JUST_UPDATED
    _print(f"[agent] {_at('log_connected', url=BACKEND_URL, name=DEVICE_NAME, id=DEVICE_ID)}")
    await sio.emit(
        "register",
        {
            "id": DEVICE_ID,
            "hostname": DEVICE_NAME,
            "platform": platform.system(),       # "Windows" or "Linux"
            "arch": platform.machine(),           # e.g. "AMD64", "x86_64"
            "release": OS_RELEASE,                # e.g. "Ubuntu 22.04" / "Windows 11"
            "ip": get_local_ip(),
            # The networks this device is attached to - for site-to-site.
            # Without this the backend would have to guess the network (so far:
            # a blanket /24). With a /16 or /22, parts of the network were then
            # missing from the routes, and the user could not reach some of the
            # devices - with no discernible reason.
            "subnets": get_local_subnets(),
            "enrollment_token": ENROLLMENT_TOKEN,  # only relevant the first time
            "updated": _JUST_UPDATED,              # true = just came out of an update
            "agent_version": AGENT_VERSION,        # own version (for the "outdated" hint)
            "agent_code_hash": AGENT_CODE_HASH,     # checksum of the running code
            # Trait of this build: does the agent know the screen helper
            # for Windows session 0? This shows in the dashboard immediately
            # whether the client is really running the new file - a version
            # number alone did not reliably show that recently.
            "screen_helper": True,
            # Is a remote session currently running? After a reconnect this lets
            # the backend know whether the stream continues or must be restarted.
            "screen_active": bool(_screen_stream.get("active")),
            "screen_flow": 1,                      # kennt ACK-Flusskontrolle
            "vdisplay": _vdisplay_status() if IS_WINDOWS else {"supported": False},
            "multiuser_select": IS_WINDOWS,
            "registry": IS_WINDOWS,
            "wu_auto": wu_auto_status() if IS_WINDOWS else {"supported": False},
            # Announce patch capability unprompted. This way the backend does
            # not have to send a ping first to know whether a patch job on this
            # client makes sense at all.
            "patch_protocol": PATCH_PROTOCOL,
            "patch_sources": _patch_sources(),
            "device_type": DETECTED_DEVICE_TYPE,   # "vm"/"lxc"/"physical"/None (auto-detection)
            "device_kind": DETECTED_DEVICE_KIND,   # icon: laptop/pc/server/hypervisor/vm/lxc/docker
            "last_crash": _read_and_clear_last_crash(),  # traceback of the last crash (if any)
        },
        namespace="/agent",
    )
    if _JUST_UPDATED:
        _print(f"[agent] {_at('log_update_sent')}")
        _JUST_UPDATED = False  # report only once, not on every reconnect


@sio.on("register-ack", namespace="/agent")
async def on_register_ack(data=None):
    """
    The backend has processed our registration - successfully OR NOT.

    Only after a SUCCESSFUL registration do we send heartbeats. Before that,
    every heartbeat would be nothing but extra load on a server that is
    currently taking on all the other agents in turn.

    CHANGED (2026-09-20): Previously this only did '_REGISTERED.set()' - the
    acknowledgement was believed unchecked. But the backend also sent ok=True
    when the registration had in fact failed. Result: the agent happily kept
    running and reported heartbeats while there was no client record in the
    dashboard at all. Exactly the picture of "installs cleanly, never shows up".

    Now the content is evaluated. On a failure the agent says clearly WHY and
    registers again after a short pause - the most common cause (server under
    load right now) resolves itself.
    """
    data = data or {}
    if data.get("ok", True):
        _REGISTERED.set()
        _print("[agent] Registration confirmed by the server - starting heartbeats")
        # Marker for the update script: "the new version has arrived at the
        # server" (health check, otherwise rollback to the old version).
        try:
            (Path(__file__).resolve().parent / ".connected").write_text(str(int(time.time())), encoding="utf-8")
        except Exception:
            pass
        await _report_update_failure()
        # USB guard: fetch the configuration (only answers if the "usbguard"
        # module is active on the backend - otherwise the monitor stays off).
        try:
            await sio.emit("usb-config-get", {"id": DEVICE_ID}, namespace="/agent")
        except Exception:
            pass
        return

    _REGISTERED.clear()
    reason = data.get("reason") or "no reason given"
    _print(f"[agent] REGISTRATION REJECTED: {reason}. "
           f"This device therefore does NOT appear in the dashboard. "
           f"Retrying in 15 seconds.")
    await asyncio.sleep(15)
    try:
        await connect()          # sends the same 'register' again
    except Exception as e:
        _print(f"[agent] Re-registration failed: {e!r}")


@sio.event(namespace="/agent")
async def disconnect():
    """Connection gone -> pause heartbeats until we are registered again."""
    _REGISTERED.clear()


@sio.event(namespace="/agent")
async def connect_error(data):
    _print(f"[agent] Connection error to {BACKEND_URL}: {data!r} "
           f"(check: reachable? TLS certificate valid? does the proxy forward /socket.io?)")


# --------------------------------------------------------------
# Regular metrics (heartbeat)
# --------------------------------------------------------------

# Marker for the network throughput calculation (difference between two measurements)
_last_net = {"ts": None, "bytes_sent": 0, "bytes_recv": 0}


# ------------------------------------------------------------------
# Extended hardware / telemetry collection
# ------------------------------------------------------------------
# All extra values are BEST-EFFORT: if a sensor/module is missing on a
# platform, the value is simply omitted instead of raising an error.
# Static info (CPU/RAM/GPU model) is determined once and cached, dynamic
# values (temp, clock, IO, ping) each cycle.

_static_hw_cache = None            # info determined once, unchanging
_last_diskio = {"ts": None, "read": 0, "write": 0}
_ping_targets = {"google": "8.8.8.8", "cloudflare": "1.1.1.1"}


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _cpu_model() -> str:
    # Determine the CPU name across platforms.
    if IS_WINDOWS:
        # platform.processor() only returns the identifier ("AMD64 Family 25 ...").
        # The real marketing name is in the registry - without a subprocess/window.
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                               r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            name = winreg.QueryValueEx(k, "ProcessorNameString")[0]
            winreg.CloseKey(k)
            if name and name.strip():
                return name.strip()
        except Exception:
            pass
        # Fallback: WMI name (hidden window via _run).
        txt = _safe(lambda: _run(["powershell", "-NoProfile", "-Command",
                                  "(Get-CimInstance Win32_Processor).Name"], timeout=6).stdout) or ""
        for line in txt.splitlines():
            if line.strip():
                return line.strip()
        return _safe(lambda: __import__("platform").processor()) or "unknown"
    else:
        info = _safe(lambda: open("/proc/cpuinfo").read()) or ""
        for line in info.splitlines():
            if "model name" in line:
                return line.split(":", 1)[1].strip()
    return _safe(lambda: __import__("platform").processor()) or "unknown"


def _gpu_models() -> list:
    # GPU names best effort. On Linux via lspci, on Windows via WMI/cim.
    out = []
    if IS_WINDOWS:
        txt = _safe(lambda: _run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name"],
            capture_output=True, text=True, timeout=6).stdout) or ""
        out = [l.strip() for l in txt.splitlines() if l.strip()]
    else:
        txt = _safe(lambda: _run(["lspci"], capture_output=True, text=True, timeout=6).stdout) or ""
        for line in txt.splitlines():
            low = line.lower()
            if "vga compatible" in low or "3d controller" in low or " display " in low:
                out.append(line.split(":", 2)[-1].strip())
    return out[:4]


def _ram_modules() -> list:
    # RAM sticks (vendor/size/clock) best effort.
    mods = []
    if IS_WINDOWS:
        txt = _safe(lambda: _run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_PhysicalMemory | ForEach-Object { \"$($_.Manufacturer)|$($_.Capacity)|$($_.Speed)|$($_.PartNumber)\" }"],
            capture_output=True, text=True, timeout=6).stdout) or ""
        for line in txt.splitlines():
            p = line.strip().split("|")
            if len(p) >= 3 and p[1].isdigit():
                vendor = (p[0] or "").strip()
                part = (p[3].strip() if len(p) > 3 else "")
                # Manufacturer is "Unknown"/empty on many sticks -> then use the
                # part number as a more meaningful name.
                if not vendor or vendor.lower() in ("unknown", "n/a", "0"):
                    vendor = part or vendor or "?"
                mods.append({"vendor": vendor, "size": int(p[1]),
                             "speed": _safe(lambda: int(p[2])) or 0,
                             "part": part})
    else:
        # dmidecode usually needs root; only use it if available.
        txt = _safe(lambda: _run(["dmidecode", "-t", "memory"],
                    capture_output=True, text=True, timeout=6).stdout) or ""
        cur = {}
        for line in txt.splitlines():
            s = line.strip()
            if s.startswith("Size:") and "No Module" not in s:
                cur = {"raw_size": s.split(":", 1)[1].strip()}
            elif s.startswith("Manufacturer:") and cur:
                cur["vendor"] = s.split(":", 1)[1].strip()
            elif s.startswith("Speed:") and cur:
                cur["speed_str"] = s.split(":", 1)[1].strip()
                mods.append({"vendor": cur.get("vendor", "?"),
                             "size_str": cur.get("raw_size", "?"),
                             "speed_str": cur.get("speed_str", "?")})
                cur = {}
    return mods[:8]


def _mac_and_interfaces() -> dict:
    """
    Determines the primary MAC address and a list of the network interfaces
    (name, MAC, IPv4). Purely informational for the client panels.
    """
    result = {"mac": None, "interfaces": []}
    try:
        import uuid as _uuid
        node = _uuid.getnode()
        # getnode() sets a bit on a random fallback -> then unclear.
        mac = ":".join(f"{(node >> ele) & 0xff:02x}" for ele in range(40, -8, -8))
        result["mac"] = mac
    except Exception:
        pass
    try:
        addrs = psutil.net_if_addrs()
        for name, entries in addrs.items():
            nic = {"name": name, "mac": None, "ipv4": None}
            for e in entries:
                fam = getattr(e, "family", None)
                famname = getattr(fam, "name", str(fam))
                if famname in ("AF_LINK", "AF_PACKET") or (hasattr(__import__("socket"), "AF_LINK") and fam == __import__("socket").AF_LINK):
                    nic["mac"] = e.address
                elif fam == __import__("socket").AF_INET:
                    nic["ipv4"] = e.address
            if nic["ipv4"] or nic["mac"]:
                result["interfaces"].append(nic)
        # Prefer taking the primary MAC from the interface with the default IP.
        primary_ip = get_local_ip()
        for nic in result["interfaces"]:
            if nic["ipv4"] == primary_ip and nic["mac"]:
                result["mac"] = nic["mac"]
                break
    except Exception:
        pass
    return result


def _static_hardware() -> dict:
    global _static_hw_cache
    if _static_hw_cache is not None:
        return _static_hw_cache
    plat = __import__("platform")
    net = _mac_and_interfaces()
    info = {
        "cpuModel": _cpu_model(),
        "arch": _safe(lambda: plat.machine()) or "",
        "gpuModels": _gpu_models(),
        "ramModules": _ram_modules(),
        "cpuMaxFreq": _safe(lambda: round(psutil.cpu_freq().max)) if _safe(lambda: psutil.cpu_freq()) else None,
        "mac": net.get("mac"),
        "interfaces": net.get("interfaces", []),
        "hostname": DEVICE_NAME,
    }
    _static_hw_cache = info
    return info


def _cpu_temp() -> float | None:
    temps = _safe(lambda: psutil.sensors_temperatures())
    if temps:
        # Prefer CPU-related sensors, otherwise the first available one.
        for key in ("coretemp", "k10temp", "cpu_thermal", "acpitz", "zenpower"):
            if key in temps and temps[key]:
                return round(temps[key][0].current, 1)
        for arr in temps.values():
            if arr:
                return round(arr[0].current, 1)
    if IS_WINDOWS:
        # Windows: psutil cannot read temperatures -> WMI thermal zone (tenths
        # of a Kelvin). Expensive -> only every 30 s (TTL cache). For a real CPU
        # temperature on AMD/Intel desktops also LibreHardwareMonitor (see _win_hw_sensors).
        return _ttl("cputemp_win", 30, _cpu_temp_windows)
    # Linux fallback: hwmon directly (k10temp/coretemp provide Tctl/Package here).
    hw_temps, _ = _read_hwmon()
    for pref in ("tctl", "tdie", "package", "cpu"):
        for name, v in hw_temps.items():
            if pref in name.lower():
                return v
    return next(iter(hw_temps.values()), None)


def _cpu_temp_windows() -> float | None:
    txt = _safe(lambda: _run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature "
         "-ErrorAction SilentlyContinue | Select-Object -ExpandProperty CurrentTemperature "
         "| Measure-Object -Maximum).Maximum"],
        capture_output=True, text=True, timeout=6).stdout) or ""
    v = _safe(lambda: int(txt.strip()))
    if v and v > 0:
        return round(v / 10.0 - 273.15, 1)   # tenths of a Kelvin -> °C
    return None


def _read_hwmon():
    """Linux fallback: read temperatures & fans directly from /sys/class/hwmon
    (if psutil.sensors_* returns nothing, e.g. without an lm-sensors config).
    Returns (temps{name:°C}, fans{name:rpm})."""
    import glob as _glob
    temps, fans = {}, {}
    for hw in _glob.glob("/sys/class/hwmon/hwmon*"):
        name = _safe(lambda hw=hw: open(hw + "/name").read().strip()) or os.path.basename(hw)
        for tf in _glob.glob(hw + "/temp*_input"):
            v = _safe(lambda tf=tf: int(open(tf).read().strip()))
            if v is None:
                continue
            lp = tf.replace("_input", "_label")
            lbl = _safe(lambda lp=lp: open(lp).read().strip()) or os.path.basename(tf).replace("_input", "")
            temps[f"{name}: {lbl}"] = round(v / 1000.0, 1)
        for ff in _glob.glob(hw + "/fan*_input"):
            v = _safe(lambda ff=ff: int(open(ff).read().strip()))
            if v is None:
                continue
            fans[f"{name}: {os.path.basename(ff).replace('_input', '')}"] = int(v)
    return temps, fans


def _all_temps() -> dict:
    """All temperature sensors as {name: °C}. psutil first, then hwmon fallback."""
    out = {}
    temps = _safe(lambda: psutil.sensors_temperatures()) or {}
    for chip, arr in temps.items():
        for i, s in enumerate(arr):
            label = (s.label or f"{chip} {i}").strip()
            name = f"{chip}: {label}" if label and label.lower() not in chip.lower() else chip
            if s.current is not None:
                out[name] = round(s.current, 1)
    if not out and not IS_WINDOWS:
        hw_temps, _ = _read_hwmon()
        out.update(hw_temps)
    return out


def _all_fans() -> dict:
    """All fans as {name: rpm}. psutil first, then hwmon fallback."""
    out = {}
    fans = _safe(lambda: psutil.sensors_fans()) or {}
    for chip, arr in fans.items():
        for i, f in enumerate(arr):
            label = (f.label or f"{chip} {i}").strip()
            if f.current is not None:
                out[label] = int(f.current)
    if not out and not IS_WINDOWS:
        _, hw_fans = _read_hwmon()
        out.update(hw_fans)
    return out


def _fan_speed() -> int | None:
    fans = _safe(lambda: psutil.sensors_fans())
    if not fans:
        # Linux fallback from hwmon
        if not IS_WINDOWS:
            _, hw_fans = _read_hwmon()
            if hw_fans:
                return int(next(iter(hw_fans.values())))
        return None
    for arr in fans.values():
        if arr:
            return int(arr[0].current)
    return None


def _gpus() -> list:
    """GPU telemetry via nvidia-smi (if present): name, utilization %,
    VRAM used/total, temperature, power draw. Works across platforms when
    NVIDIA drivers are installed."""
    txt = _safe(lambda: _run(
        ["nvidia-smi",
         "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=4).stdout)
    if not txt:
        return []
    gpus = []
    for line in txt.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6:
            continue
        def _f(x):
            try: return float(x)
            except Exception: return None
        gpus.append({
            "name": parts[0],
            "load": _f(parts[1]),                                  # %
            "memUsed": int(_f(parts[2]) * 1024 * 1024) if _f(parts[2]) is not None else None,  # MiB->Bytes
            "memTotal": int(_f(parts[3]) * 1024 * 1024) if _f(parts[3]) is not None else None,
            "temp": _f(parts[4]),                                  # °C
            "power": _f(parts[5]),                                 # W
        })
    return gpus[:4]


def _amd_gpus_sysfs() -> list:
    """AMD GPU telemetry via sysfs (Linux, amdgpu driver): utilization,
    temperature, power draw, VRAM. No external tools needed."""
    if IS_WINDOWS:
        return []
    import glob as _glob
    gpus = []
    for card in sorted(_glob.glob("/sys/class/drm/card[0-9]")):
        dev = card + "/device"
        busy = _safe(lambda dev=dev: int(open(dev + "/gpu_busy_percent").read().strip()))
        if busy is None:
            continue   # no amdgpu sensor -> skip
        temp = power = None
        for hw in _glob.glob(dev + "/hwmon/hwmon*"):
            t = _safe(lambda hw=hw: int(open(hw + "/temp1_input").read().strip()))
            if t is not None:
                temp = round(t / 1000.0, 1)
            p = _safe(lambda hw=hw: int(open(hw + "/power1_average").read().strip()))
            if p is not None:
                power = round(p / 1_000_000.0, 1)
        mem_total = _safe(lambda dev=dev: int(open(dev + "/mem_info_vram_total").read().strip()))
        mem_used = _safe(lambda dev=dev: int(open(dev + "/mem_info_vram_used").read().strip()))
        name = _safe(lambda dev=dev: open(dev + "/product_name").read().strip()) or "AMD GPU"
        gpus.append({"name": name, "load": float(busy), "temp": temp, "power": power,
                     "memUsed": mem_used, "memTotal": mem_total})
    return gpus[:4]


def _win_hw_sensors() -> dict:
    """Windows: read sensors (CPU temp, fans, package power, GPU from AMD/NVIDIA/
    Intel) via LibreHardwareMonitor or OpenHardwareMonitor - the most reliable
    source for these values on Windows. Requirement: one of the tools is
    running. Without it, Windows technically cannot provide CPU temperature,
    fans and power draw (psutil does not support Windows sensors). Returns
    (only keys that exist): cpuTemp, temps{}, fans{}, powerWatts, gpus[]."""
    if not IS_WINDOWS:
        return {}
    data = None
    for ns in ("root/LibreHardwareMonitor", "root/OpenHardwareMonitor"):
        txt = _safe(lambda ns=ns: _run(
            ["powershell", "-NoProfile", "-Command",
             f"Get-CimInstance -Namespace {ns} -ClassName Sensor -ErrorAction SilentlyContinue | "
             "Select-Object Name,SensorType,Value,Identifier,Parent | ConvertTo-Json -Compress"],
            timeout=8).stdout)
        if txt and txt.strip():
            try:
                parsed = json.loads(txt)
                data = parsed if isinstance(parsed, list) else [parsed]
                if data:
                    break
            except Exception:
                data = None
    if not data:
        return {}

    temps, fans = {}, {}
    gpu_acc = {}
    cpu_temp_candidates = {}
    pkg_power = None

    for s in data:
        st = (s.get("SensorType") or "").lower()
        nm = (s.get("Name") or "").strip()
        val = s.get("Value")
        ident = (s.get("Identifier") or "").lower()
        parent = (s.get("Parent") or ident).lower()
        if val is None:
            continue
        try: val = round(float(val), 1)
        except Exception: continue

        is_gpu = "gpu" in ident or "gpu" in parent
        if st == "temperature":
            temps[nm] = val
            if is_gpu:
                gpu_acc.setdefault(parent, {})["temp"] = val
            elif "cpu" in ident or "core" in nm.lower() or "tctl" in nm.lower() or "package" in nm.lower():
                cpu_temp_candidates[nm] = val
        elif st == "fan":
            fans[nm] = int(val)
        elif st == "power":
            if is_gpu:
                gpu_acc.setdefault(parent, {})["power"] = val
            elif "package" in nm.lower() or "cpu" in nm.lower() or "total" in nm.lower():
                pkg_power = max(pkg_power or 0, val)
        elif st == "load":
            if is_gpu and ("core" in nm.lower() or "gpu" in nm.lower()):
                gpu_acc.setdefault(parent, {}).setdefault("load", val)
        elif st in ("smalldata", "data"):
            low = nm.lower()
            if is_gpu and "memory" in low:
                g = gpu_acc.setdefault(parent, {})
                if "used" in low: g["memUsed"] = int(val * 1024 * 1024)
                elif "total" in low: g["memTotal"] = int(val * 1024 * 1024)

    out = {}
    if temps: out["temps"] = temps
    if fans: out["fans"] = fans
    if pkg_power is not None: out["powerWatts"] = pkg_power
    if cpu_temp_candidates:
        pick = None
        for key in ("tctl", "tdie", "cpu package", "core (tctl", "cpu total"):
            for nm, v in cpu_temp_candidates.items():
                if key in nm.lower(): pick = v; break
            if pick is not None: break
        if pick is None:
            pick = round(sum(cpu_temp_candidates.values()) / len(cpu_temp_candidates), 1)
        out["cpuTemp"] = pick
    gpus = []
    for parent, g in gpu_acc.items():
        name = parent.split("/")[-2] if "/" in parent else "GPU"
        gpus.append({"name": name.upper(), "load": g.get("load"), "temp": g.get("temp"),
                     "power": g.get("power"), "memUsed": g.get("memUsed"), "memTotal": g.get("memTotal")})
    if gpus: out["gpus"] = gpus
    return out


def _battery() -> dict | None:
    b = _safe(lambda: psutil.sensors_battery())
    if not b:
        return None
    return {"percent": round(b.percent, 1),
            "plugged": bool(b.power_plugged),
            "secsleft": (b.secsleft if b.secsleft not in (None, psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN) else None)}


def _power_watts() -> float | None:
    # Instantaneous power draw (W), best effort. Two sources (Linux):
    #  1) RAPL energy counters of ALL packages (intel-rapl:0, :1, ...) -> dJ/dt.
    #  2) hwmon power*_input (microwatts, instantaneous) as a fallback.
    if IS_WINDOWS:
        return None
    import glob as _glob
    # (1) RAPL: only the package zones (intel-rapl:N), not the sub-domains :N:M.
    pkgs = sorted(p for p in _glob.glob("/sys/class/powercap/intel-rapl:*/energy_uj")
                  if p.count(":") == 1)
    total_uj = 0
    found = False
    for path in pkgs:
        uj = _safe(lambda p=path: int(open(p).read().strip()))
        if uj is not None:
            total_uj += uj
            found = True
    if found:
        now = time.time()
        prev = getattr(_power_watts, "_prev", None)
        _power_watts._prev = (now, total_uj)
        if prev:
            dt = now - prev[0]
            dj = (total_uj - prev[1]) / 1_000_000.0
            if dt > 0 and dj >= 0:
                return round(dj / dt, 1)
        # no dt yet on the first call -> fall back to hwmon
    # (2) hwmon instantaneous power (microwatts).
    watts = 0.0
    any_hw = False
    for path in _glob.glob("/sys/class/hwmon/hwmon*/power*_input"):
        uw = _safe(lambda p=path: int(open(p).read().strip()))
        if uw:
            watts += uw / 1_000_000.0
            any_hw = True
    if any_hw:
        return round(watts, 1)
    return None


def _disk_io_speed() -> dict:
    # Derive read/write rate (bytes/s) from the total counters.
    io = _safe(lambda: psutil.disk_io_counters())
    if not io:
        return {}
    now = time.time()
    res = {}
    if _last_diskio["ts"] is not None:
        dt = now - _last_diskio["ts"]
        if dt > 0:
            res["diskRead"] = round((io.read_bytes - _last_diskio["read"]) / dt)
            res["diskWrite"] = round((io.write_bytes - _last_diskio["write"]) / dt)
    _last_diskio["ts"] = now
    _last_diskio["read"] = io.read_bytes
    _last_diskio["write"] = io.write_bytes
    return res


def _ping_ms(host: str) -> float | None:
    # A single ping via the system tool. Multilingual parsing
    # (time=/Zeit=/tiempo=/temps=) with average and number fallbacks.
    param = "-n" if IS_WINDOWS else "-c"
    timeout_param = "-w" if IS_WINDOWS else "-W"
    tval = "1000" if IS_WINDOWS else "1"
    try:
        r = _run(["ping", param, "1", timeout_param, tval, host], timeout=4, errors="ignore")
        out = (r.stdout or "").lower()
        import re as _re
        m = _re.search(r"(?:time|zeit|tiempo|temps|tempo|czas)[=<]\s*([\d.,]+)\s*ms", out)
        if not m:
            m = _re.search(r"(?:average|mittelwert|moyenne|promedio|media)\s*[=:]\s*([\d.,]+)\s*ms", out)
        if not m:
            m = _re.search(r"[=<]\s*([\d.,]+)\s*ms", out)   # last resort
        if m:
            return round(float(m.group(1).replace(",", ".")), 1)
    except Exception:
        return None
    return None


def collect_extended_metrics() -> dict:
    """Extra telemetry (hardware, temperatures, IO, ping). All best effort;
    missing sensors simply do not appear in the result."""
    ext = {}
    ext.update(_static_hardware())          # cpuModel, gpuModels, ramModules, ...

    freq = _safe(lambda: psutil.cpu_freq())
    if freq:
        ext["cpuFreq"] = round(freq.current)
    # Clock per core. Windows usually returns only one value -> mirror it onto
    # all cores so the panel shows data (approximation).
    per = _safe(lambda: psutil.cpu_freq(percpu=True))
    if per and len(per) > 1:
        ext["cpuFreqPerCore"] = [round(f.current) for f in per]
    elif freq:
        n = _safe(lambda: psutil.cpu_count(logical=True)) or 0
        if n:
            ext["cpuFreqPerCore"] = [round(freq.current)] * n

    # Load average: native on Linux; on Windows our own EWMA estimator
    # (psutil.getloadavg is only emulated there and 0 at first). Set in
    # collect_metrics; here only for non-Windows from psutil.
    if not IS_WINDOWS:
        la = _safe(lambda: psutil.getloadavg())
        if la:
            ext["load1"], ext["load5"], ext["load15"] = [round(x, 2) for x in la]

    ext["procCount"] = _safe(lambda: len(psutil.pids())) or 0

    for name, fn in (("cpuTemp", _cpu_temp), ("fanSpeed", _fan_speed)):
        v = fn()
        if v is not None:
            ext[name] = v
    # All temperature/fan sensors (Linux via psutil).
    temps = _all_temps()
    if temps:
        ext["temps"] = temps
    fans = _all_fans()
    if fans:
        ext["fans"] = fans
    # GPU telemetry: nvidia-smi (expensive, every 15 s) or AMD sysfs (Linux, cheap).
    gpus = _ttl("gpus", 15, _gpus) or _amd_gpus_sysfs()
    if gpus:
        ext["gpus"] = gpus

    # Windows sensors via LibreHardwareMonitor/OpenHardwareMonitor (CPU temp,
    # fans, package power, AMD/NVIDIA GPU). Every 10 s (WMI call). Supplements
    # or overrides the fields above if nothing came from there.
    if IS_WINDOWS:
        hw = _ttl("winhw", 10, _win_hw_sensors)
        if hw:
            if hw.get("temps"): ext["temps"] = {**ext.get("temps", {}), **hw["temps"]}
            if hw.get("fans"): ext["fans"] = {**ext.get("fans", {}), **hw["fans"]}
            if "cpuTemp" not in ext and hw.get("cpuTemp") is not None: ext["cpuTemp"] = hw["cpuTemp"]
            if "fanSpeed" not in ext and hw.get("fans"):
                ext["fanSpeed"] = next(iter(hw["fans"].values()))
            if "powerWatts" not in ext and hw.get("powerWatts") is not None: ext["powerWatts"] = hw["powerWatts"]
            if not ext.get("gpus") and hw.get("gpus"): ext["gpus"] = hw["gpus"]

    bat = _battery()
    if bat:
        ext["battery"] = bat
    pw = _power_watts()
    if pw is not None:
        ext["powerWatts"] = pw

    ext.update(_disk_io_speed())            # diskRead, diskWrite (bytes/s)

    # Ping targets (network quality) - starts one ping process per target, so
    # only measure every 15 s (reuse the last value in between).
    def _measure_pings():
        out = {}
        for label, host in _ping_targets.items():
            v = _ping_ms(host)
            if v is not None:
                out[label] = v
        return out
    pings = _ttl("pings", 15, _measure_pings)
    if pings:
        ext["ping"] = pings

    return ext


def get_local_subnets() -> list:
    """
    Determines the IPv4 networks this device is attached to.

    Example: address 192.168.178.79 with mask 255.255.255.0 gives
    "192.168.178.0/24".

    Loopback (127.x), automatic addresses (169.254.x) and the tunnel networks
    themselves are omitted - the latter would create a route to themselves.
    """
    out = []
    try:
        import ipaddress as _ip
        for name, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                if getattr(a, "family", None) != socket.AF_INET:
                    continue
                ip, mask = a.address, getattr(a, "netmask", None)
                if not ip or not mask:
                    continue
                if ip.startswith("127.") or ip.startswith("169.254."):
                    continue
                try:
                    net = _ip.ip_network(f"{ip}/{mask}", strict=False)
                except ValueError:
                    continue
                # A /32 is not a network but a single address.
                if net.prefixlen >= 31:
                    continue
                text = str(net)
                if text not in out:
                    out.append(text)
    except Exception as e:
        _print(f"[agent] Could not determine networks: {e}")
    return out[:8]


def collect_metrics() -> dict:
    """
    Collects the current system metrics. Runs in a background thread
    (see heartbeat_loop) because psutil.cpu_percent() briefly "blocks"
    (it measures the load over a small time window).
    """
    # Measure total CPU load AND per core in one go (percpu splits the 0.5s
    # window across all cores; the total is their mean).
    per_core = psutil.cpu_percent(interval=0.5, percpu=True) or []
    cpu_percent = round(sum(per_core) / len(per_core)) if per_core else psutil.cpu_percent()
    memory = psutil.virtual_memory()
    # Swap / paging memory (on Linux the "swap partition/file", on Windows the
    # page file). swap_memory() exists on both systems.
    try:
        swap = psutil.swap_memory()
        swap_used = int(swap.used)
        swap_total = int(swap.total)
    except Exception:
        swap_used = 0
        swap_total = 0
    disk_path = "C:\\" if IS_WINDOWS else "/"
    disk = psutil.disk_usage(disk_path)

    # --- Netzwerk-Durchsatz berechnen ---
    # psutil returns total counters (bytes since system start). The throughput
    # is the difference from the last measurement divided by the elapsed time.
    net = psutil.net_io_counters()
    now = time.time()
    net_in_per_s = 0.0
    net_out_per_s = 0.0
    if _last_net["ts"] is not None:
        elapsed = now - _last_net["ts"]
        if elapsed > 0:
            net_in_per_s = (net.bytes_recv - _last_net["bytes_recv"]) / elapsed
            net_out_per_s = (net.bytes_sent - _last_net["bytes_sent"]) / elapsed
    _last_net["ts"] = now
    _last_net["bytes_recv"] = net.bytes_recv
    _last_net["bytes_sent"] = net.bytes_sent

    # --- All disks/partitions individually (for the disk tab in the dashboard) ---
    disks = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
            disks.append({
                "device": part.device,
                "mountpoint": part.mountpoint,
                "used": usage.used,
                "total": usage.total,
            })
        except (PermissionError, OSError):
            continue  # e.g. skip CD drives that are not ready

    base = {
        "cpuLoad": round(cpu_percent),
        "cpuPerCore": [round(x) for x in per_core],           # load per core (%)
        "cpuCores": psutil.cpu_count(logical=False) or 0,   # physical cores
        "cpuThreads": psutil.cpu_count(logical=True) or 0,  # logical cores (threads)
        "memUsed": memory.total - memory.available,
        "memTotal": memory.total,
        "memAvailable": memory.available,   # free/available RAM
        "memCached": int(getattr(memory, "cached", 0) or 0),     # cache (Linux)
        "memBuffers": int(getattr(memory, "buffers", 0) or 0),   # buffers (Linux)
        "swapUsed": swap_used,               # used swap/page memory (bytes)
        "swapTotal": swap_total,             # total swap/page memory (bytes)
        "diskUsed": disk.used,
        "diskTotal": disk.total,
        "disks": disks,
        "netIn": round(net_in_per_s),      # bytes per second received
        "netOut": round(net_out_per_s),    # bytes per second sent
        "uptime": int(time.time() - psutil.boot_time()),
    }
    # Estimate load average on Windows ourselves (EWMA from CPU load * core
    # count), since there is no real load average there.
    if IS_WINDOWS:
        try:
            ncpu = psutil.cpu_count(logical=True) or 1
            inst = (cpu_percent / 100.0) * ncpu
            g = collect_metrics
            prev = getattr(g, "_load", None) or {"1": inst, "5": inst, "15": inst}
            import math as _math
            a1, a5, a15 = _math.exp(-5/60), _math.exp(-5/300), _math.exp(-5/900)
            nl = {"1": prev["1"]*a1 + inst*(1-a1),
                  "5": prev["5"]*a5 + inst*(1-a5),
                  "15": prev["15"]*a15 + inst*(1-a15)}
            g._load = nl
            base["load1"] = round(nl["1"], 2)
            base["load5"] = round(nl["5"], 2)
            base["load15"] = round(nl["15"], 2)
        except Exception:
            pass
    # Append extended telemetry (best effort, must never disturb the heartbeat).
    try:
        base.update(collect_extended_metrics())
    except Exception:
        pass
    return base


async def heartbeat_loop():
    """Runs permanently in the background and sends metrics every 5 seconds."""
    loop = asyncio.get_event_loop()
    while True:
        # Only send if the connection is up AND the registration is confirmed.
        # Both together prevent a freshly connected agent from interfering with
        # the server while it is onboarding other agents.
        if sio.connected and _REGISTERED.is_set():
            try:
                # collect_metrics() briefly blocks -> run it in a thread so the
                # rest of the program (e.g. incoming commands) does not wait
                metrics = await loop.run_in_executor(None, collect_metrics)
                await sio.emit("heartbeat", {"id": DEVICE_ID, "metrics": metrics}, namespace="/agent")
            except Exception as e:
                _print(f"[agent] {_at('log_metrics_failed', err=e)}")
        # Small random jitter in the cadence: without it the heartbeats of all
        # agents keep arriving on the same second and create regular load spikes
        # instead of a steady baseline load.
        await asyncio.sleep(5 + random.uniform(0, 1.5))


# --------------------------------------------------------------
# Remote commands: run shell commands (terminal app in the dashboard)
# --------------------------------------------------------------

# Per terminal session (= one terminal window in the dashboard) we remember the
# current working directory, so that "cd" & co. persist across several commands
# - exactly like in a real shell.
_terminal_sessions: dict[str, dict] = {}
_terminal_sessions_lock = threading.Lock()


def _session_cwd(session_id: str | None) -> str | None:
    """Current working directory of a terminal session (creates it if needed)."""
    if not session_id:
        return None
    with _terminal_sessions_lock:
        sess = _terminal_sessions.setdefault(session_id, {"cwd": os.getcwd()})
        cwd = sess.get("cwd")
    if not cwd or not os.path.isdir(cwd):
        cwd = os.getcwd()
    return cwd


def _update_session_cwd(session_id: str | None, new_cwd: str) -> None:
    """Stores the new working directory after a command (if it changed)."""
    if not session_id or not new_cwd:
        return
    with _terminal_sessions_lock:
        _terminal_sessions.setdefault(session_id, {})["cwd"] = new_cwd


def _run_shell_command(command: str, session_id: str | None = None,
                       shell: str = "auto", elevated: bool = False) -> tuple[str, str, int]:
    """
    Runs a shell command and returns (stdout, stderr, exit_code).

    shell:    'cmd' | 'powershell' | 'auto' (auto = cmd on Windows, sh on POSIX)
    elevated: True -> run as Administrator on Windows (UAC elevation).

    If a session_id is set, the command runs in the remembered working
    directory of that terminal session, and a directory possibly changed via
    "cd" is carried over to the next command.
    """
    start_cwd = _session_cwd(session_id)
    cwd_file = None
    script_file = None
    is_win = os.name == "nt"
    use_ps = (shell == "powershell")
    try:
        fd, cwd_file = tempfile.mkstemp(prefix="rmm_cwd_")
        os.close(fd)

        # Windows + Administrator: dedicated elevation path (UAC).
        if is_win and elevated:
            return _run_elevated_windows(command, start_cwd, use_ps)

        if is_win and use_ps:
            # Run PowerShell robustly: -EncodedCommand (Base64/UTF-16LE) bypasses
            # ExecutionPolicy, quoting AND encoding problems that, with
            # "-File script.ps1" on locked-down/localized machines, cause
            # commands like 'irm' to appear "not found". We wrap the user command,
            # append exit-code and CWD output, and set the console output
            # explicitly to UTF-8.
            ps_script = (
                "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\r\n"
                + (f'Set-Location -LiteralPath "{start_cwd}"\r\n' if start_cwd else "")
                + command + "\r\n"
                + "$__rmm_rc = $LASTEXITCODE; if ($null -eq $__rmm_rc) { $__rmm_rc = 0 }\r\n"
                + f'(Get-Location).Path | Out-File -Encoding utf8 -FilePath "{cwd_file}"\r\n'
                + "exit $__rmm_rc\r\n"
            )
            encoded = base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
            argv = ["powershell", "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded]
        elif is_win:
            # cmd: temporary .bat.
            fd, script_file = tempfile.mkstemp(prefix="rmm_cmd_", suffix=".bat")
            os.close(fd)
            with open(script_file, "w", encoding="utf-8") as f:
                f.write("@echo off\r\n")
                f.write(command + "\r\n")
                f.write('set "__rmm_rc=%ERRORLEVEL%"\r\n')
                f.write(f'cd > "{cwd_file}"\r\n')
                f.write("exit /b %__rmm_rc%\r\n")
            argv = ["cmd", "/c", script_file]
        else:
            # POSIX: sh -c with a wrapper that writes out the PWD after the command.
            wrapper = (
                f"{command}\n"
                f"__rmm_rc=$?\n"
                f"printf '%s' \"$PWD\" > '{cwd_file}' 2>/dev/null\n"
                f"exit $__rmm_rc\n"
            )
            argv = ["/bin/sh", "-c", wrapper]

        result = _run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            cwd=start_cwd if start_cwd else None,
        )

        # Adopt the new working directory (if anything changed).
        try:
            with open(cwd_file, "r", encoding="utf-8", errors="replace") as f:
                new_cwd = f.read().strip().strip('"')
            if new_cwd and os.path.isdir(new_cwd):
                _update_session_cwd(session_id, new_cwd)
        except OSError:
            pass

        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        return "", "Timeout: command took too long (>15s)", 1
    except Exception as e:
        return "", str(e), 1
    finally:
        for path in (cwd_file, script_file):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass


def _ps_single_quote(s: str) -> str:
    """Escapes a string for PowerShell single quotes (double up '')."""
    return "'" + s.replace("'", "''") + "'"


def _run_elevated_windows(command: str, start_cwd: str | None, use_ps: bool) -> tuple[str, str, int]:
    """
    Runs a command on Windows AS ADMINISTRATOR (UAC elevation via PowerShell
    Start-Process -Verb RunAs). Since the elevated process is a separate one,
    stdout/stderr/exit code are collected via temp files.

    If the agent already runs as SYSTEM/Administrator (autostart as a service),
    NO UAC dialog appears - the elevation is then transparent.
    """
    out_file = tempfile.mkstemp(prefix="rmm_out_", suffix=".txt")[1]
    err_file = tempfile.mkstemp(prefix="rmm_err_", suffix=".txt")[1]
    rc_file = tempfile.mkstemp(prefix="rmm_rc_", suffix=".txt")[1]
    inner = tempfile.mkstemp(prefix="rmm_inner_", suffix=(".ps1" if use_ps else ".bat"))[1]
    try:
        if use_ps:
            with open(inner, "w", encoding="utf-8") as f:
                f.write("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\r\n")
                if start_cwd:
                    f.write(f'Set-Location -LiteralPath "{start_cwd}"\r\n')
                f.write(f'& {{ {command} }} > "{out_file}" 2> "{err_file}"\r\n')
                f.write(f'$c = $LASTEXITCODE; if ($null -eq $c) {{ $c = 0 }}; $c | Out-File -Encoding ascii "{rc_file}"\r\n')
            inner_exec = f'powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{inner}"'
        else:
            with open(inner, "w", encoding="utf-8") as f:
                f.write("@echo off\r\n")
                if start_cwd:
                    f.write(f'cd /d "{start_cwd}"\r\n')
                f.write(f'call {command} > "{out_file}" 2> "{err_file}"\r\n')
                f.write(f'echo %ERRORLEVEL% > "{rc_file}"\r\n')
            inner_exec = f'cmd /c "{inner}"'

        # Start-Process -Verb RunAs raises the inner command to admin rights.
        # The launcher itself is passed as EncodedCommand so that the nested
        # quotes stay clean.
        launch_script = (
            f"$p = Start-Process -FilePath cmd.exe -ArgumentList '/c',{_ps_single_quote(inner_exec)} "
            f"-Verb RunAs -WindowStyle Hidden -PassThru -Wait; exit $p.ExitCode"
        )
        encoded = base64.b64encode(launch_script.encode("utf-16-le")).decode("ascii")
        _run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-EncodedCommand", encoded],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )

        def _read(p):
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    return f.read()
            except OSError:
                return ""
        stdout = _read(out_file)
        stderr = _read(err_file)
        rc_raw = _read(rc_file).strip()
        try:
            code = int(rc_raw)
        except ValueError:
            code = 0
        if not stdout and not stderr and code != 0:
            stderr = "(Elevated command failed or UAC denied.)"
        return stdout, stderr, code
    except subprocess.TimeoutExpired:
        return "", "Timeout on the elevated command (>120s)", 1
    except Exception as e:
        return "", f"Elevation failed: {e}", 1
    finally:
        for path in (out_file, err_file, rc_file, inner):
            try:
                os.unlink(path)
            except OSError:
                pass


@sio.on("exec", namespace="/agent")
async def on_exec(data):
    """Called when the backend wants to send a terminal command."""
    request_id = data.get("requestId")
    command = data.get("command", "")
    session_id = data.get("session")
    shell = data.get("shell", "auto")        # 'cmd' | 'powershell' | 'auto'
    elevated = bool(data.get("elevated", False))

    loop = asyncio.get_event_loop()
    stdout, stderr, exit_code = await loop.run_in_executor(
        None, _run_shell_command, command, session_id, shell, elevated
    )

    await sio.emit(
        "exec-result",
        {"requestId": request_id, "stdout": stdout, "stderr": stderr, "code": exit_code},
        namespace="/agent",
    )


# ======================================================================
# VPN PEER  (WireGuard-compatible tunnel, client side)
# ----------------------------------------------------------------------
# Important to understand: no WireGuard runs on THIS device. Nothing is
# installed, no driver, no TUN device, no administrator rights.
#
# The actual WireGuard tunnel terminates in the backend (there in pure
# Python, see backend/app/wireguard.py). What arrives here is already
# unpacked: simple instructions like "open a TCP connection to 192.168.1.5:445
# and shuffle these bytes back and forth". The agent does this with the
# standard module 'socket' - nothing more is needed.
#
# From the point of view of the target machine on the network, the connection
# thus comes from this device. That is exactly intended: the client is the
# bridgehead into the network.
#
# Events from the backend:
#   vpn-open   {tunnel, stream, host, port}  -> open a connection
#   vpn-data   {stream, data(base64)}        -> send bytes to the target
#   vpn-close  {stream}                      -> close the connection
#   vpn-udp    {tunnel, host, port, sport, src, data}
#   vpn-ping   {tunnel, host, src, ident, seq}
#
# Responses back: vpn-open-result, vpn-data, vpn-close, vpn-udp,
# vpn-ping-result.
# ======================================================================

# How many connections may a tunnel keep open at once? The limit protects
# the device against a port scan through the tunnel tearing open thousands
# of sockets.
VPN_MAX_STREAMS = int(os.getenv("AGENT_VPN_MAX_STREAMS", "128") or 128)
# Time limit for establishing the connection to the target.
VPN_CONNECT_TIMEOUT = 8.0
# After this much inactivity a UDP mapping is cleaned up again.
VPN_UDP_IDLE = 60.0

_vpn_streams: dict[str, dict] = {}          # stream-ID -> {sock, thread, tunnel}
_vpn_streams_lock = threading.Lock()
_vpn_udp: dict[tuple, dict] = {}            # (tunnel, host, port, sport) -> mapping
_vpn_udp_lock = threading.Lock()


def _vpn_emit(event: str, payload: dict) -> None:
    """Send an event to the backend - also from a background thread."""
    try:
        asyncio.run_coroutine_threadsafe(
            sio.emit(event, payload, namespace="/agent"), _AGENT_LOOP)
    except Exception:
        pass


# ----------------------------------------------------------------------
# TCP
# ----------------------------------------------------------------------

def _vpn_reader(stream_id: str, sock: socket.socket, tunnel: str) -> None:
    """
    Reads everything that comes back from the target and sends it to the backend.

    Runs in its own thread. That is the more honest way here than asyncio:
    the sockets are ordinary, blocking sockets, and one thread per connection
    is unproblematic at the manageable counts of a tunnel - unlike hundreds of
    permanent connections.
    """
    try:
        while True:
            try:
                chunk = sock.recv(32768)
            except (OSError, socket.timeout):
                break
            if not chunk:
                break
            _vpn_emit("vpn-data", {
                "tunnel": tunnel, "stream": stream_id,
                "data": base64.b64encode(chunk).decode(),
            })
    finally:
        _vpn_drop_stream(stream_id, notify=True)


def _vpn_drop_stream(stream_id: str, notify: bool) -> None:
    with _vpn_streams_lock:
        entry = _vpn_streams.pop(stream_id, None)
    if not entry:
        return
    try:
        entry["sock"].close()
    except Exception:
        pass
    if notify:
        _vpn_emit("vpn-close", {"tunnel": entry.get("tunnel", ""),
                                "stream": stream_id})


# ---------------------------------------------------------------------------
# WireGuard VPN (module "vpn"): the backend generates the complete configuration
# (keys, address, host) and sends it here; the agent only applies it.
#   wg-join  {tunnel, peer, name, conf}   ->  wg-result {tunnel, peer, ok, error}
#   wg-leave {tunnel, name}
# Linux: wireguard-tools (wg-quick) needed. Windows: WireGuard for Windows (service).
# Same logic as modules/node/vpn/wgsys.py (compact here, the agent is a single file).
# ---------------------------------------------------------------------------
_WG_NAME = re.compile(r"^[A-Za-z0-9_=+.-]{1,15}$")


def _wg_conf_dir():
    if IS_WINDOWS:
        d = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "Bytera", "wireguard")
    else:
        d = "/etc/wireguard"
    os.makedirs(d, exist_ok=True)
    return d


def _wg_exe():
    if IS_WINDOWS:
        p = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "WireGuard", "wireguard.exe")
        return p if os.path.exists(p) else shutil.which("wireguard.exe")
    return shutil.which("wg-quick")


def _child_env():
    """Environment for external programs (wireguard.exe, netsh, ...). An .exe built with
    PyInstaller sets its own DLL/path variables; if child processes inherit them, WireGuard
    reports "DLL not found". Hence without _MEI*/_PYI* variables and with the original PATH."""
    env = dict(os.environ)
    if getattr(sys, "frozen", False):
        for k in list(env):
            if k.startswith(("_MEI", "_PYI")):
                env.pop(k, None)
        mei = getattr(sys, "_MEIPASS", "")
        if mei and env.get("PATH"):
            env["PATH"] = os.pathsep.join(p for p in env["PATH"].split(os.pathsep) if not p.startswith(mei))
    return env


def _wg_run(cmd, timeout=90):
    k32 = None
    if IS_WINDOWS and getattr(sys, "frozen", False):
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.SetDllDirectoryW(None)            # do not pass the .exe DLL search path to child processes
        except Exception:
            k32 = None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=_child_env(),
                           creationflags=0x08000000 if IS_WINDOWS else 0)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    finally:
        if k32 is not None:
            try:
                k32.SetDllDirectoryW(getattr(sys, "_MEIPASS", None))
            except Exception:
                pass


def _wg_gui_dir():
    """Configuration store of the WireGuard app: files here are shown by the app in its tunnel list
    (the WireGuard manager encrypts them itself into <name>.conf.dpapi)."""
    return os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "WireGuard", "Data", "Configurations")


def _wg_svc_exists(name):
    """Does the WireGuard tunnel service already exist? (otherwise wireguard.exe shows a window
    "The specified service is not an installed service" on /uninstalltunnelservice and waits for OK.)"""
    code, _ = _wg_run(["sc", "query", "WireGuardTunnel$" + name], 20)
    return code == 0


def _wg_dpapi_encrypt(data, name):
    """Encrypt the configuration the same way the WireGuard app does (DPAPI, description =
    tunnel name). The agent runs as SYSTEM - like the WireGuard manager - so the app can
    read the file and shows the tunnel in its list."""
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = BLOB()
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(blob_in), ctypes.c_wchar_p(name), None, None, None,
                                                  CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out)):
        raise OSError(ctypes.GetLastError(), "CryptProtectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _wg_win_install(exe, name, conf):
    """Windows: place the tunnel in the WireGuard app's store (encrypted, so the app shows it
    in its list) and start it as a service -> visible AND active. Without an error window."""
    if _wg_svc_exists(name):
        _wg_run([exe, "/uninstalltunnelservice", name], 60)
        for _ in range(20):
            if not _wg_svc_exists(name):
                break
            time.sleep(0.5)
    d = _wg_gui_dir()
    os.makedirs(d, exist_ok=True)
    for old in (name + ".conf", name + ".conf.dpapi"):
        try:
            os.remove(os.path.join(d, old))
        except OSError:
            pass
    target = os.path.join(d, name + ".conf.dpapi")
    try:
        with open(target, "wb") as f:
            f.write(_wg_dpapi_encrypt(conf.encode("utf-8"), name))
    except Exception as e:                                       # fallback: plaintext (works, app may not show it)
        _print(f"[wg] Could not encrypt for the WireGuard app ({e}) - using plaintext")
        target = os.path.join(_wg_conf_dir(), name + ".conf")
        with open(target, "w", encoding="utf-8") as f:
            f.write(conf)
    code, out = _wg_run([exe, "/installtunnelservice", target])
    if code == 0:
        for _ in range(20):                                      # is the service really there?
            if _wg_svc_exists(name):
                break
            time.sleep(0.5)
        else:
            return 1, "The WireGuard tunnel service was not created"
    return code, out


def _wg_install():
    """Install WireGuard on the first tunnel (agent runs as root/SYSTEM)."""
    try:
        if IS_WINDOWS:
            if shutil.which("winget"):
                _wg_run(["winget", "install", "-e", "--id", "WireGuard.WireGuard", "--silent",
                         "--accept-package-agreements", "--accept-source-agreements"], 600)
        elif shutil.which("apt-get"):
            _wg_run(["apt-get", "install", "-y", "-q", "wireguard-tools"], 600)
        elif shutil.which("dnf"):
            _wg_run(["dnf", "install", "-y", "wireguard-tools"], 600)
        elif shutil.which("pacman"):
            _wg_run(["pacman", "-S", "--noconfirm", "wireguard-tools"], 600)
    except Exception:
        pass


def _wg_up(name, conf):
    exe = _wg_exe()
    if not exe:
        _wg_install()
        exe = _wg_exe()
    if not exe:
        raise RuntimeError("WireGuard missing - " + ("install WireGuard for Windows" if IS_WINDOWS
                                                  else "install the wireguard-tools package"))
    if name.startswith("rmt"):
        # Endpoint test: clean up leftovers of earlier tests (rmt*) first
        for old in os.listdir(_wg_conf_dir()):
            if old.startswith("rmt") and old.endswith(".conf") and old[:-5] != name:
                try:
                    _wg_down(old[:-5])
                except Exception:
                    pass
    path = os.path.join(_wg_conf_dir(), name + ".conf")
    with open(path, "w", encoding="utf-8") as f:
        f.write(conf)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    if IS_WINDOWS:
        code, out = _wg_win_install(exe, name, conf)
    else:
        if os.path.exists("/sys/class/net/" + name):
            _wg_run(["wg-quick", "down", name], 60)
        code, out = _wg_run(["wg-quick", "up", name])
    if code != 0:
        raise RuntimeError(out.strip()[-400:] or "WireGuard error %d" % code)


def _wg_firewall(name, allow_from, add=True):
    """Allow inbound traffic from the VPN network (ping, shares, remote services of the peers).
    Windows: a firewall rule per tunnel. Linux: ufw or firewalld, if active (otherwise usually nothing to do)."""
    rule = "RMM-VPN " + name
    try:
        if IS_WINDOWS:
            _wg_run(["netsh", "advfirewall", "firewall", "delete", "rule", "name=" + rule], 30)
            if add and allow_from:
                remote = ",".join(x.strip() for x in allow_from.split(",") if x.strip())
                _wg_run(["netsh", "advfirewall", "firewall", "add", "rule", "name=" + rule, "dir=in", "action=allow",
                         "protocol=any", "remoteip=" + remote, "profile=any", "enable=yes"], 30)
            return
        if shutil.which("ufw"):
            code, out = _wg_run(["ufw", "status"], 20)
            if code == 0 and "Status: active" in out:
                _wg_run(["ufw", "--force", "delete", "allow", "in", "on", name], 20)
                if add:
                    _wg_run(["ufw", "allow", "in", "on", name], 20)
        if shutil.which("firewall-cmd"):
            code, _ = _wg_run(["firewall-cmd", "--state"], 20)
            if code == 0:
                _wg_run(["firewall-cmd", "--zone=trusted", ("--add-interface=" if add else "--remove-interface=") + name], 20)
    except Exception as e:
        _print(f"[wg] firewall {name}: {e}")


def _wg_down(name):
    _wg_firewall(name, "", add=False)
    exe = _wg_exe()
    if exe:
        if IS_WINDOWS:
            if _wg_svc_exists(name):                               # otherwise an error window from wireguard.exe
                _wg_run([exe, "/uninstalltunnelservice", name], 60)
            for old in (name + ".conf", name + ".conf.dpapi"):     # also remove it from the WireGuard app
                try:
                    os.remove(os.path.join(_wg_gui_dir(), old))
                except OSError:
                    pass
        elif os.path.exists("/sys/class/net/" + name):
            _wg_run(["wg-quick", "down", name], 60)
    try:
        os.remove(os.path.join(_wg_conf_dir(), name + ".conf"))
    except OSError:
        pass
    if IS_WINDOWS:                                                 # leftover from before the rename
        legacy = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "Rapalle", "wireguard")
        try:
            os.remove(os.path.join(legacy, name + ".conf"))
        except OSError:
            pass
        for d in (legacy, os.path.dirname(legacy)):
            try:
                os.rmdir(d)                                        # only if empty
            except OSError:
                pass


@sio.on("wg-join", namespace="/agent")
async def on_wg_join(payload):
    name = str(payload.get("name") or "")
    res = {"tunnel": payload.get("tunnel"), "peer": payload.get("peer"), "ok": False, "error": ""}
    try:
        if not _WG_NAME.match(name):
            raise RuntimeError("invalid tunnel name")
        await asyncio.to_thread(_wg_up, name, str(payload.get("conf") or ""))
        if payload.get("allow_from"):
            await asyncio.to_thread(_wg_firewall, name, str(payload.get("allow_from")), True)
        res["ok"] = True
        _print(f"[wg] tunnel {name} connected")
    except Exception as e:
        res["error"] = str(e)
        _print(f"[wg] Tunnel {name}: {e}")
    await sio.emit("wg-result", res, namespace="/agent")


@sio.on("wg-leave", namespace="/agent")
async def on_wg_leave(payload):
    name = str(payload.get("name") or "")
    if _WG_NAME.match(name):
        try:
            await asyncio.to_thread(_wg_down, name)
            _print(f"[wg] tunnel {name} removed")
        except Exception as e:
            _print(f"[wg] removing tunnel {name}: {e}")


@sio.on("vpn-open", namespace="/agent")
async def on_vpn_open(payload):
    """Open a new connection through the tunnel."""
    stream_id = payload.get("stream") or ""
    tunnel = payload.get("tunnel") or ""
    host = payload.get("host") or ""
    port = int(payload.get("port") or 0)
    if not stream_id or not host or not port:
        return

    with _vpn_streams_lock:
        too_many = len(_vpn_streams) >= VPN_MAX_STREAMS
    if too_many:
        _print(f"[vpn] limit of {VPN_MAX_STREAMS} connections reached - "
               f"{host}:{port} rejected")
        await sio.emit("vpn-open-result",
                       {"tunnel": tunnel, "stream": stream_id, "ok": False,
                        "error": "too many open connections"},
                       namespace="/agent")
        return

    def _connect():
        try:
            sock = socket.create_connection((host, port), VPN_CONNECT_TIMEOUT)
        except Exception as e:
            _vpn_emit("vpn-open-result", {"tunnel": tunnel, "stream": stream_id,
                                          "ok": False, "error": str(e)})
            return
        sock.settimeout(None)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
        reader = threading.Thread(target=_vpn_reader,
                                  args=(stream_id, sock, tunnel), daemon=True)
        with _vpn_streams_lock:
            _vpn_streams[stream_id] = {"sock": sock, "thread": reader,
                                       "tunnel": tunnel}
        _vpn_emit("vpn-open-result", {"tunnel": tunnel, "stream": stream_id,
                                      "ok": True})
        reader.start()

    # Establishing the connection blocks for up to VPN_CONNECT_TIMEOUT seconds -
    # that belongs in a thread, otherwise the whole agent stalls.
    threading.Thread(target=_connect, daemon=True).start()


@sio.on("vpn-data", namespace="/agent")
async def on_vpn_data(payload):
    """Forward bytes from the tunnel to the target."""
    stream_id = payload.get("stream") or ""
    with _vpn_streams_lock:
        entry = _vpn_streams.get(stream_id)
    if not entry:
        return
    try:
        data = base64.b64decode(payload.get("data") or "")
    except Exception:
        return
    try:
        entry["sock"].sendall(data)
    except Exception:
        _vpn_drop_stream(stream_id, notify=True)


@sio.on("vpn-close", namespace="/agent")
async def on_vpn_close(payload):
    """Close the connection (the peer in the tunnel is done)."""
    _vpn_drop_stream(payload.get("stream") or "", notify=False)


# ----------------------------------------------------------------------
# UDP
# ----------------------------------------------------------------------

def _vpn_udp_reader(key: tuple, sock: socket.socket, tunnel: str,
                    src: str, sport: int, host: str, port: int) -> None:
    """Waits for response datagrams and sends them back through the tunnel."""
    try:
        while True:
            sock.settimeout(VPN_UDP_IDLE)
            try:
                data, _addr = sock.recvfrom(65535)
            except (OSError, socket.timeout):
                break
            if not data:
                break
            _vpn_emit("vpn-udp", {
                "tunnel": tunnel,
                # 'host/port' here is the sender on the client's network,
                # 'dst/dport' the user in the tunnel.
                "host": host, "port": port,
                "dst": src, "dport": sport,
                "data": base64.b64encode(data).decode(),
            })
    finally:
        with _vpn_udp_lock:
            entry = _vpn_udp.pop(key, None)
        if entry:
            try:
                entry["sock"].close()
            except Exception:
                pass


@sio.on("vpn-udp", namespace="/agent")
async def on_vpn_udp(payload):
    """Send a UDP datagram from the tunnel (e.g. DNS)."""
    tunnel = payload.get("tunnel") or ""
    host = payload.get("host") or ""
    port = int(payload.get("port") or 0)
    src = payload.get("src") or ""
    sport = int(payload.get("sport") or 0)
    if not host or not port:
        return
    try:
        data = base64.b64decode(payload.get("data") or "")
    except Exception:
        return

    key = (tunnel, host, port, sport)
    with _vpn_udp_lock:
        entry = _vpn_udp.get(key)
        if entry is None:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            except Exception as e:
                _print(f"[vpn] UDP socket failed: {e}")
                return
            entry = {"sock": sock}
            _vpn_udp[key] = entry
            threading.Thread(target=_vpn_udp_reader,
                             args=(key, sock, tunnel, src, sport, host, port),
                             daemon=True).start()
    try:
        entry["sock"].sendto(data, (host, port))
    except Exception as e:
        _print(f"[vpn] UDP to {host}:{port} failed: {e}")


# ----------------------------------------------------------------------
# ICMP (ping)
# ----------------------------------------------------------------------

@sio.on("vpn-ping", namespace="/agent")
async def on_vpn_ping(payload):
    """
    Reachability check through the tunnel.

    Building real ICMP packets requires a raw socket and thus root or
    administrator rights - which the agent deliberately does not have
    everywhere. Therefore the system tool 'ping' is used; the result
    (reachable yes/no) is enough for the response the backend generates in
    the tunnel.
    """
    tunnel = payload.get("tunnel") or ""
    host = payload.get("host") or ""
    if not host:
        return

    def _run():
        try:
            if IS_WINDOWS:
                cmd = ["ping", "-n", "1", "-w", "1500", host]
            else:
                cmd = ["ping", "-c", "1", "-W", "2", host]
            res = subprocess.run(cmd, capture_output=True, timeout=6,
                                 **_no_window_kwargs())
            ok = res.returncode == 0
        except Exception:
            ok = False
        _vpn_emit("vpn-ping-result", {
            "tunnel": tunnel, "host": host, "src": payload.get("src") or "",
            "ident": payload.get("ident") or 0, "seq": payload.get("seq") or 0,
            "payload_len": payload.get("payload_len") or 32, "ok": ok,
        })

    threading.Thread(target=_run, daemon=True).start()


# ======================================================================
# NODE (installation & supervision)
# ----------------------------------------------------------------------
# A NODE is a separate process (src/node/main.py) that runs INDEPENDENTLY of
# the agent - with its own connection to the backend and its own token. The
# agent only takes care of the surroundings:
#
#   node-install  fetch the package (ZIP) from the backend, extract to ./node/,
#                 write node.json, set it up as a service and start it
#   node-ensure   make sure the node is running and up to date
#   node-remove   stop it, remove the service, delete ./node/
#
# Service (depending on the system): systemd (Linux, root) / scheduled task
# (Windows) / launchd (macOS, root) - otherwise a detached process that the
# watchdog (node_watchdog_loop) restarts when needed.
# Without 'node-install' there is not a single line of node code on the client.
# ======================================================================

NODE_DIR = Path(os.path.dirname(os.path.abspath(__file__))) / "node"
NODE_SERVICE = "bytera-node"
NODE_TASK = "ByteraNode"
NODE_PLIST = "/Library/LaunchDaemons/net.bytera.node.plist"


def _node_installed() -> bool:
    return (NODE_DIR / "main.py").is_file() and (NODE_DIR / "node.json").is_file()


def _node_pid() -> int | None:
    try:
        pid = int((NODE_DIR / "data" / "node.pid").read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        if psutil.pid_exists(pid) and "main.py" in " ".join(psutil.Process(pid).cmdline()):
            return pid
    except Exception:
        pass
    return None


def _node_hash() -> str:
    try:
        return (NODE_DIR / ".hash").read_text().strip()
    except OSError:
        return ""


def _node_mode() -> str:
    try:
        return (NODE_DIR / ".mode").read_text().strip()
    except OSError:
        return ""


def _node_download(path: str) -> bytes:
    return _backend_fetch(path, timeout=120, limit=25 * 1024 * 1024)


def _node_extract(data: bytes) -> None:
    import zipfile
    NODE_DIR.mkdir(parents=True, exist_ok=True)
    base = NODE_DIR.resolve()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            if name.endswith("/") or name == "node.json" or name.split("/")[0] in ("data", "__pycache__"):
                continue
            target = (NODE_DIR / name).resolve()
            target.relative_to(base)                    # prevents ../ in the ZIP
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(name))


def _node_run(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return 1, str(e)


def _node_is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _node_start_process() -> None:
    """Detached process (survives the agent, restarted by the watchdog)."""
    kw: dict = {"cwd": str(NODE_DIR), "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200       # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen([sys.executable, str(NODE_DIR / "main.py")], **kw)


def _node_service_install() -> str:
    """Sets up the node as a service and starts it. Returns the operating mode."""
    py, main = sys.executable, str(NODE_DIR / "main.py")
    if sys.platform.startswith("linux") and _node_is_root() and shutil.which("systemctl") and os.path.isdir("/run/systemd/system"):
        unit = (f"[Unit]\nDescription=BYTERA RMM Node\nAfter=network-online.target docker.service\nWants=network-online.target\n\n"
                f"[Service]\nWorkingDirectory={NODE_DIR}\nExecStart={py} {main}\nRestart=always\nRestartSec=5\n\n"
                f"[Install]\nWantedBy=multi-user.target\n")
        Path(f"/etc/systemd/system/{NODE_SERVICE}.service").write_text(unit, encoding="utf-8")
        _node_run(["systemctl", "daemon-reload"])
        code, out = _node_run(["systemctl", "enable", "--now", NODE_SERVICE])
        if code == 0:
            return "systemd"
        _print(f"[node] systemd service not possible: {out.strip()[:200]}")
    elif os.name == "nt":
        code, out = _node_run(["schtasks", "/create", "/tn", NODE_TASK, "/tr", f'"{py}" "{main}"', "/sc", "onstart",
                               "/ru", "SYSTEM", "/rl", "HIGHEST", "/f"])
        if code == 0:
            _node_run(["schtasks", "/run", "/tn", NODE_TASK])
            return "task"
        _print(f"[node] scheduled task not possible: {out.strip()[:200]}")
    elif sys.platform == "darwin" and _node_is_root():
        plist = ('<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>'
                 '<key>Label</key><string>net.bytera.node</string>'
                 f'<key>ProgramArguments</key><array><string>{py}</string><string>{main}</string></array>'
                 f'<key>WorkingDirectory</key><string>{NODE_DIR}</string>'
                 '<key>RunAtLoad</key><true/><key>KeepAlive</key><true/></dict></plist>')
        Path(NODE_PLIST).write_text(plist, encoding="utf-8")
        code, _ = _node_run(["launchctl", "load", "-w", NODE_PLIST])
        if code == 0:
            return "launchd"
    _node_start_process()
    return "process"


def _node_service_remove() -> None:
    mode = _node_mode()
    if mode == "systemd":
        _node_run(["systemctl", "disable", "--now", NODE_SERVICE])
        try:
            Path(f"/etc/systemd/system/{NODE_SERVICE}.service").unlink()
        except OSError:
            pass
        _node_run(["systemctl", "daemon-reload"])
    elif mode == "task":
        _node_run(["schtasks", "/end", "/tn", NODE_TASK])
        _node_run(["schtasks", "/delete", "/tn", NODE_TASK, "/f"])
    elif mode == "launchd":
        _node_run(["launchctl", "unload", "-w", NODE_PLIST])
        try:
            Path(NODE_PLIST).unlink()
        except OSError:
            pass
    pid = _node_pid()
    if pid:
        try:
            p = psutil.Process(pid)
            p.terminate()
            p.wait(5)
        except Exception:
            try:
                psutil.Process(pid).kill()
            except Exception:
                pass


def _node_do_install(payload: dict) -> str:
    """Blocking (run in a thread): fetch package, extract, configure, start."""
    data = _node_download(str(payload.get("path") or "/api/nodes/package"))
    if NODE_DIR.exists():
        _node_service_remove()                         # cleanly stop the old version
    _node_extract(data)
    cfg = {"backend_url": BACKEND_URL, "client_id": str(payload.get("client_id") or DEVICE_ID),
           "token": str(payload.get("token") or ""),
           # encryption: the node uses the same pinned server key / TLS fingerprint as the agent
           "e2e_key": E2E_SERVER_KEY if _E2E_LIB else "", "tls_pin": TLS_PIN, "tls_port": TLS_PORT}
    cfg_file = NODE_DIR / "node.json"
    cfg_file.write_text(json.dumps(cfg), encoding="utf-8")
    try:
        os.chmod(cfg_file, 0o600)
    except OSError:
        pass
    (NODE_DIR / ".hash").write_text(str(payload.get("hash") or ""), encoding="utf-8")
    mode = _node_service_install()
    (NODE_DIR / ".mode").write_text(mode, encoding="utf-8")
    return mode


def _node_do_update(payload: dict) -> None:
    """Apply a new package (node.json and data/ stay), restart the node."""
    data = _node_download(str(payload.get("path") or "/api/nodes/package"))
    _node_service_remove()                              # reads .mode to find the old service
    _node_extract(data)
    (NODE_DIR / ".hash").write_text(str(payload.get("hash") or ""), encoding="utf-8")
    (NODE_DIR / ".mode").write_text(_node_service_install(), encoding="utf-8")


async def _node_report(op: str, ok: bool, error: str = "", **extra) -> None:
    try:
        await sio.emit("node-agent-result", {"op": op, "ok": ok, "error": error, **extra}, namespace="/agent")
    except Exception:
        pass


@sio.on("node-install", namespace="/agent")
async def on_node_install(payload):
    _print("[node] installation requested")
    try:
        mode = await asyncio.get_running_loop().run_in_executor(None, _node_do_install, payload or {})
        _print(f"[node] installed (operating mode: {mode})")
        await _node_report("install", True, mode=mode)
    except Exception as e:
        _print(f"[node] installation failed: {e}")
        await _node_report("install", False, str(e))


@sio.on("node-ensure", namespace="/agent")
async def on_node_ensure(payload):
    payload = payload or {}
    try:
        if not _node_installed():
            await _node_report("ensure", False, "Node files are missing on the client - please re-activate the node.")
            return
        loop = asyncio.get_running_loop()
        if payload.get("hash") and payload["hash"] != _node_hash():
            _print("[node] updating ...")
            await loop.run_in_executor(None, _node_do_update, payload)
        elif not _node_pid():
            await loop.run_in_executor(None, _node_start_process if _node_mode() in ("", "process") else _node_service_install)
        await _node_report("ensure", True)
    except Exception as e:
        _print(f"[node] ensure failed: {e}")
        await _node_report("ensure", False, str(e))


@sio.on("node-remove", namespace="/agent")
async def on_node_remove(payload):
    _print("[node] removal requested")
    try:
        def _remove():
            _node_service_remove()
            shutil.rmtree(NODE_DIR, ignore_errors=True)
        await asyncio.get_running_loop().run_in_executor(None, _remove)
        await _node_report("remove", True)
    except Exception as e:
        _print(f"[node] removal failed: {e}")
        await _node_report("remove", False, str(e))


async def node_watchdog_loop():
    """Restarts the node if it (without a service) is no longer running."""
    while True:
        await asyncio.sleep(60)
        try:
            if _node_installed() and _node_mode() in ("", "process") and not _node_pid():
                _print("[node] process was no longer running - restarting")
                await asyncio.get_running_loop().run_in_executor(None, _node_start_process)
        except Exception as e:
            _print(f"[node] watchdog: {e}")


# ======================================================================
# MAINTENANCE MODE / DIAGNOSTICS (agent side)
# ----------------------------------------------------------------------
# When the backend turns on maintenance mode, the agent records everything
# and uploads it regularly. This puts backend and agent output on ONE
# timeline - on a crash shortly after the connection is established that is
# exactly the decisive question: what happened on both sides in the same
# second?
#
# Recorded is everything that otherwise disappears silently:
#   * every line via log/_print (already attached to the root logger)
#   * exceptions in threads (threading.excepthook)
#   * exceptions in asyncio tasks (loop exception handler)
#   * regular measurements (memory, threads, file descriptors)
#
# The buffer ALWAYS runs, even without maintenance mode. Only the upload
# depends on the switch. This way the last minutes are included immediately
# when it is turned on, instead of only from the moment of activation.
# ======================================================================

_DIAG = {
    "enabled": False,
    "queue": _collections.deque(maxlen=4000),   # not yet uploaded
    "lock": threading.Lock(),
}


def _diag_add(line: str, level: str = "INFO") -> None:
    """Add one line to the upload queue. Must never raise."""
    try:
        stamp = time.strftime("%H:%M:%S")
        with _DIAG["lock"]:
            _DIAG["queue"].append(f"{stamp} [{level}] {line}")
    except Exception:
        pass


class _DiagHandler(logging.Handler):
    """Attached to the root logger, thereby capturing everything _print writes."""

    def emit(self, record):
        try:
            _diag_add(record.getMessage(), record.levelname)
            if record.exc_info:
                import traceback as _tb
                _diag_add("".join(_tb.format_exception(*record.exc_info)), "ERROR")
        except Exception:
            pass


def _diag_thread_hook(args):
    import traceback as _tb
    text = "".join(_tb.format_exception(args.exc_type, args.exc_value,
                                        args.exc_traceback))
    name = args.thread.name if args.thread else "?"
    _print(f"[diag] Unhandled exception in thread {name}:\n{text}")


def _diag_loop_handler(loop, context):
    """
    Exceptions from asyncio tasks.

    Without this hook, Python only reports "Task exception was never
    retrieved" at some later point - at a time that no longer has anything
    to do with the cause.
    """
    exc = context.get("exception")
    msg = context.get("message") or "asyncio error"
    if exc:
        import traceback as _tb
        detail = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))
    else:
        detail = str(context)
    _print(f"[diag] {msg}\n{detail}")


def _diag_install() -> None:
    """Once at startup. Runs independently of the switch."""
    try:
        logging.getLogger().addHandler(_DiagHandler())
        threading.excepthook = _diag_thread_hook
        _AGENT_LOOP.set_exception_handler(_diag_loop_handler)
    except Exception as e:
        print(f"[diag] hook installation failed: {e}")
    _start_loop_watchdog()


# ----------------------------------------------------------------------
# Watchdog for the agent's event loop
# ----------------------------------------------------------------------
# The same principle as in the backend, for the same reason: an agent whose
# loop hangs has not crashed - it is alive but no longer reports anything
# and responds to no command. In the dashboard it shows as "online" and
# still does nothing. That is the most annoying of all faults, because it
# looks like a server error.
#
# A thread measures from the outside. If the loop stalls for too long, the
# agent exits - the service or the restart logic in main() catches it, and
# within seconds it is back.

_AGENT_BEAT = {"t": 0.0, "worst": 0.0}
# From here on the agent is considered hung and restarts.
AGENT_LOOP_KILL_S = float(os.getenv("AGENT_LOOP_KILL_S", "120") or 120)


async def loop_heartbeat_agent():
    """Sets a timestamp twice per second."""
    while True:
        _AGENT_BEAT["t"] = time.monotonic()
        await asyncio.sleep(0.5)


def _agent_watchdog_thread() -> None:
    warned = 0.0
    while True:
        time.sleep(1.0)
        last = _AGENT_BEAT["t"]
        if not last:
            continue
        lag = time.monotonic() - last
        if lag > _AGENT_BEAT["worst"]:
            _AGENT_BEAT["worst"] = lag
        if lag < 5.0:
            continue
        if time.monotonic() - warned > 30:
            warned = time.monotonic()
            _print(f"[diag] event loop blocked for {lag:.0f}s")
            try:
                import traceback as _tb
                for tid, frame in sys._current_frames().items():
                    _print(f"[diag] Thread {tid}:\n"
                           + "".join(_tb.format_stack(frame))[:2000])
            except Exception:
                pass
        if AGENT_LOOP_KILL_S > 0 and lag >= AGENT_LOOP_KILL_S:
            _print(f"[diag] EMERGENCY STOP: loop stalled for {lag:.0f}s - "
                   f"the agent is restarting.")
            try:
                path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "last_crash.txt")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(time.strftime("%Y-%m-%d %H:%M:%S")
                            + f"\nEmergency stop: event loop stalled for {lag:.0f}s\n")
            except OSError:
                pass
            # Restart instead of just exiting: a stopped agent would not come
            # back on its own without service management.
            try:
                os.execv(sys.executable, [sys.executable] + sys.argv)
            except Exception:
                os._exit(75)


def _start_loop_watchdog() -> None:
    threading.Thread(target=_agent_watchdog_thread, name="loop-watchdog",
                     daemon=True).start()


def _diag_sample() -> str:
    """One measurement point - cheap enough for every 30 seconds."""
    parts = []
    try:
        p = psutil.Process()
        with p.oneshot():
            parts.append(f"rss={p.memory_info().rss // 1048576}MB")
            parts.append(f"threads={p.num_threads()}")
            try:
                parts.append(f"fds={p.num_fds()}")
            except Exception:
                parts.append(f"handles={getattr(p, 'num_handles', lambda: 0)()}")
            parts.append(f"conns={len(p.net_connections(kind='inet'))}")
    except Exception:
        pass
    try:
        parts.append(f"tasks={len(asyncio.all_tasks(_AGENT_LOOP))}")
    except Exception:
        pass
    parts.append(f"connected={sio.connected}")
    parts.append(f"registered={_REGISTERED.is_set()}")
    return "METRIC " + " ".join(parts)


def _geo_wifi():
    """Nearby Wi-Fi access points (BSSID + signal in %). Quiet on errors."""
    aps = []
    try:
        if IS_WINDOWS:
            out = subprocess.run(["netsh", "wlan", "show", "networks", "mode=bssid"],
                                 capture_output=True, text=True, timeout=12,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            cur_sig = None
            for line in out.splitlines():
                s = line.strip()
                m = re.search(r"BSSID\s*\d+\s*:\s*([0-9A-Fa-f:]{17})", s)
                if m:
                    aps.append({"mac": m.group(1).lower(), "signal": -65})
                    cur_sig = len(aps) - 1
                elif cur_sig is not None:
                    ms = re.search(r"Signal\s*:\s*(\d+)%", s)
                    if ms:
                        aps[cur_sig]["signal"] = int(int(ms.group(1)) / 2) - 100
        elif sys.platform == "darwin":
            ap = "/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport"
            out = subprocess.run([ap, "-s"], capture_output=True, text=True, timeout=12).stdout
            for line in out.splitlines()[1:]:
                m = re.search(r"([0-9a-fA-F:]{17})\s+(-?\d+)", line)
                if m:
                    aps.append({"mac": m.group(1).lower(), "signal": int(m.group(2))})
        else:
            out = subprocess.run(["nmcli", "-t", "-f", "BSSID,SIGNAL", "dev", "wifi"],
                                 capture_output=True, text=True, timeout=12).stdout
            for line in out.splitlines():
                parts = line.replace("\\:", "§").split(":")
                if not parts or len(parts[0].replace("§", ":")) < 17:
                    continue
                mac = parts[0].replace("§", ":").lower()
                try:
                    pct = int(parts[-1])
                except ValueError:
                    pct = 50
                aps.append({"mac": mac, "signal": int(pct / 2) - 100})
    except Exception:
        pass
    # Merge duplicates
    seen = {}
    for a in aps:
        if a["mac"] not in seen:
            seen[a["mac"]] = a
    return list(seen.values())[:30]


def _geo_os():
    """Operating-system location (only if allowed by the user). Windows: System.Device."""
    try:
        if IS_WINDOWS:
            ps = ("$ErrorActionPreference='Stop';Add-Type -AssemblyName System.Device;"
                  "$w=New-Object System.Device.Location.GeoCoordinateWatcher('High');"
                  "$null=$w.TryStart($true,[TimeSpan]::FromSeconds(8));"
                  "$c=$w.Position.Location;"
                  "if($c.IsUnknown){'none'}else{\"$($c.Latitude),$($c.Longitude),$($c.HorizontalAccuracy)\"}")
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                                 timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
            if out and out != "none" and "," in out:
                lat, lon, acc = (out.split(",") + ["0"])[:3]
                return {"lat": float(lat), "lon": float(lon), "accuracy": float(acc or 0)}
    except Exception:
        pass
    return None


def _geo_public_ip():
    try:
        import urllib.request
        for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
            try:
                with urllib.request.urlopen(url, timeout=8) as r:
                    ip = r.read().decode().strip()
                    if ip and len(ip) <= 45:
                        return ip
            except Exception:
                continue
    except Exception:
        pass
    return None


@sio.on("geo-locate", namespace="/agent")
async def on_geo_locate(payload):
    """The backend requests location data (Wi-Fi APs, OS location, public IP)."""
    rid = (payload or {}).get("requestId")
    try:
        wifi, os_loc, ip = await asyncio.gather(
            asyncio.to_thread(_geo_wifi), asyncio.to_thread(_geo_os), asyncio.to_thread(_geo_public_ip))
        result = {"requestId": rid, "wifi": wifi, "os": os_loc, "public_ip": ip}
    except Exception as exc:
        result = {"requestId": rid, "error": str(exc)}
    try:
        await sio.emit("geo-result", result, namespace="/agent")
    except Exception:
        pass


@sio.on("diag-mode", namespace="/agent")
async def on_diag_mode(payload):
    """The backend turns maintenance mode on or off."""
    enabled = bool((payload or {}).get("enabled"))
    was = _DIAG["enabled"]
    _DIAG["enabled"] = enabled
    if enabled and not was:
        _print(f"[diag] maintenance mode ON - agent {AGENT_VERSION}, "
               f"PID {os.getpid()}, Python {sys.version.split()[0]}, "
               f"{platform.platform()}")
        _diag_report_last_crash()
    elif not enabled and was:
        _print("[diag] maintenance mode OFF")


def _diag_report_last_crash() -> None:
    """
    Reports a previous crash after the fact.

    On a crash the agent writes last_crash.txt and restarts itself. This
    file is often the only thing left of a crash - it belongs in the
    diagnostics package.
    """
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "last_crash.txt")
        if not os.path.isfile(path):
            return
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            _print("[diag] Last crash of this agent:\n" + f.read()[:8000])
    except Exception as e:
        _print(f"[diag] last_crash.txt not readable: {e}")


async def diag_upload_loop():
    """
    Uploads the collected lines.

    In small chunks and with spacing: an agent that sends logs every second
    would itself be a load - and maintenance mode is meant to find the
    problem, not enlarge it.
    """
    while True:
        await asyncio.sleep(20)
        try:
            if not _DIAG["enabled"] or not sio.connected or not _REGISTERED.is_set():
                # Not connected: lines stay in the queue and go out next
                # time. The lines from a connection fault are exactly the
                # interesting ones.
                continue
            _diag_add(_diag_sample(), "DEBUG")
            with _DIAG["lock"]:
                lines = list(_DIAG["queue"])
                _DIAG["queue"].clear()
            if not lines:
                continue
            # Send in blocks so that no oversized message is created.
            for i in range(0, len(lines), 200):
                await sio.emit("diag-log", {"id": DEVICE_ID,
                                            "lines": lines[i:i + 200]},
                               namespace="/agent")
        except Exception as e:
            print(f"[diag] upload failed: {e}")


# ======================================================================
# SELF-HEALING: background loops that never quietly disappear
# ----------------------------------------------------------------------
# A loop started with create_task() is GONE after an exception - silently.
# The agent then seemingly keeps running but no longer reports metrics or
# responds to commands. From the outside this looks like "the agent has
# crashed", even though the process is still alive.
#
# This wrapper catches the exception, writes it to the log and restarts the
# loop - with growing spacing, so that a permanently broken loop does not
# max out the CPU.
# ======================================================================

def _agent_supervise(name: str, factory):
    async def runner():
        delay = 2.0
        while True:
            started = time.monotonic()
            try:
                await factory()
                _print(f"[supervisor] '{name}' ended normally")
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                import traceback as _tb
                _print(f"[supervisor] '{name}' crashed:\n{_tb.format_exc()}")
                # If the loop ran for a long time it was probably a one-off.
                # If it dies again immediately, the spacing grows.
                delay = 2.0 if time.monotonic() - started > 120 else min(delay * 2, 300.0)
                _print(f"[supervisor] '{name}' restarting in {delay:.0f}s")
                await asyncio.sleep(delay)

    return asyncio.create_task(runner(), name=f"supervised:{name}")


# ==============================================================
# INTERACTIVE TERMINAL (real PTY session)
# ==============================================================
# Instead of running individual commands, the agent keeps a REAL shell open
# per terminal window (bash/sh on Linux, cmd/powershell on Windows). This way
# everything works like in a local terminal: 'cd' is persistent, editors like
# nano/vim run, plus history, colors, interactive programs.
#
# Linux/macOS: Python standard library 'pty' (no extra package needed).
# Windows:     'pywinpty' (ConPTY). Installed automatically via bootstrap;
#              if it is missing, a clear message is printed.
#
# Data flow:
#   Dashboard --term-open {session, shell, cols, rows}--> agent starts a shell
#   Dashboard --term-input {session, data}-------------> write into the shell
#   Dashboard --term-resize {session, cols, rows}------> adjust the PTY size
#   Dashboard --term-close {session}-------------------> end the shell
#   Agent     --term-output {session, data}------------> output to the dashboard
#   Agent     --term-exit {session}--------------------> the shell has ended
# --------------------------------------------------------------

_terminals: dict = {}   # session_id -> handle (platform-dependent)


def _resolve_shell_cmd(shell: str, elevated: bool, mode: str = "admin") -> list | None:
    """Determines the shell start command depending on request, mode and platform.

    mode "admin": direct shell (root/system - as before).
    mode "user":  POSIX starts /bin/login in the PTY -> real username/password
                  prompt via PAM, exactly as on an SSH/TTY console.
                  Returns None = no login program found.
    """
    if IS_WINDOWS:
        # Windows only knows the system shell here (the frontend does not even
        # offer the user login there).
        if shell == "powershell":
            return ["powershell.exe", "-NoLogo", "-NoProfile"]
        return ["cmd.exe"]
    if mode == "user":
        for candidate in ("/bin/login", "/usr/bin/login", "/sbin/login", "/usr/sbin/login"):
            if os.path.exists(candidate):
                # -p: pass the environment (TERM!) through to the user shell.
                return [candidate, "-p"]
        return None
    # POSIX: prefer bash, otherwise sh. 'shell' is irrelevant here.
    for candidate in ("/bin/bash", "/bin/sh"):
        if os.path.exists(candidate):
            return [candidate, "-i"] if candidate.endswith("bash") else [candidate]
    return ["/bin/sh"]


class _PosixTerminal:
    """Interactive shell via a real PTY (Linux/macOS)."""

    def __init__(self, session_id, shell, cols, rows, loop, mode="admin"):
        import pty, fcntl, termios, struct
        self.session_id = session_id
        self.loop = loop
        self.alive = True
        argv = _resolve_shell_cmd(shell, False, mode)
        if argv is None:
            # User login requested, but no login program available.
            raise RuntimeError("No /bin/login found on this system - "
                              "user login not possible")
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            # Child process: set the environment and start the shell.
            os.environ["TERM"] = "xterm-256color"
            try:
                os.execvp(argv[0], argv)
            except Exception:
                os._exit(1)
        # Parent process: set the PTY size.
        self._set_size(cols, rows)
        # Reader thread: reads PTY output and sends it to the dashboard.
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _set_size(self, cols, rows):
        try:
            import fcntl, termios, struct
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, winsize)
        except Exception:
            pass

    def _read_loop(self):
        while self.alive:
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                break
            if not data:
                break
            text = data.decode("utf-8", errors="replace")
            asyncio.run_coroutine_threadsafe(
                sio.emit("term-output", {"id": DEVICE_ID, "session": self.session_id,
                                         "data": text}, namespace="/agent"),
                self.loop,
            )
        self.alive = False
        asyncio.run_coroutine_threadsafe(
            sio.emit("term-exit", {"id": DEVICE_ID, "session": self.session_id},
                     namespace="/agent"),
            self.loop,
        )

    def write(self, data: str):
        try:
            os.write(self.fd, data.encode("utf-8"))
        except OSError:
            pass

    def resize(self, cols, rows):
        self._set_size(cols, rows)

    def close(self):
        self.alive = False
        try:
            os.close(self.fd)
        except OSError:
            pass
        try:
            os.kill(self.pid, 9)
        except OSError:
            pass


class _WinTerminal:
    """Interactive shell via ConPTY (Windows, requires pywinpty)."""

    def __init__(self, session_id, shell, cols, rows, loop):
        from winpty import PtyProcess
        self.session_id = session_id
        self.loop = loop
        self.alive = True
        # winpty.PtyProcess.spawn expects a COMMAND STRING (not a list!).
        # A list leads to a silent failure/exception.
        if shell == "powershell":
            cmd = "powershell.exe -NoLogo -NoProfile"
        else:
            cmd = "cmd.exe"
        self.proc = PtyProcess.spawn(cmd, dimensions=(rows, cols))
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        while self.alive:
            try:
                data = self.proc.read(65536)
            except EOFError:
                break
            except Exception:
                break
            if not data:
                if not self.proc.isalive():
                    break
                continue
            asyncio.run_coroutine_threadsafe(
                sio.emit("term-output", {"id": DEVICE_ID, "session": self.session_id,
                                         "data": data}, namespace="/agent"),
                self.loop,
            )
        self.alive = False
        asyncio.run_coroutine_threadsafe(
            sio.emit("term-exit", {"id": DEVICE_ID, "session": self.session_id},
                     namespace="/agent"),
            self.loop,
        )

    def write(self, data: str):
        try:
            self.proc.write(data)
        except Exception:
            pass

    def resize(self, cols, rows):
        try:
            self.proc.setwinsize(rows, cols)
        except Exception:
            pass

    def close(self):
        self.alive = False
        try:
            self.proc.terminate(force=True)
        except Exception:
            pass


def _pip(*args):
    """Call pip in the SAME interpreter, without a console window."""
    kw = {}
    if platform.system() == "Windows":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        kw = {"startupinfo": si,
              "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    try:
        return subprocess.run([sys.executable, "-m", "pip", *args],
                              capture_output=True, text=True, timeout=600, **kw)
    except Exception as e:
        _print(f"[repair] pip {' '.join(args)} failed: {e}")
        return None


_screen_repair_tried = False


def _repair_screen() -> bool:
    """
    One-time attempt to make mss/Pillow (and pynput) runnable again.

    The most common case is not "package missing" but "package broken":
    an aborted download, a half install, or a leftover that pip considers
    present. A plain 'pip install' then reports "already satisfied" and
    changes nothing - hence --force-reinstall.
    """
    global _screen_repair_tried, _SCREEN_AVAILABLE, _INPUT_AVAILABLE
    global _SCREEN_ERROR, _INPUT_ERROR, mss, Image
    global MouseController, MouseButton, KeyboardController, KeyboardKey
    if _screen_repair_tried:
        return _SCREEN_AVAILABLE
    _screen_repair_tried = True

    _print(f"[repair] reinstalling screen-capture packages (reason: {_SCREEN_ERROR})")
    _pip("install", "--no-cache-dir", "--force-reinstall", "mss>=9.0.0", "Pillow>=10.0.0")
    if not _INPUT_AVAILABLE:
        _pip("install", "--no-cache-dir", "--force-reinstall", "pynput>=1.7.0")

    import importlib
    importlib.invalidate_caches()
    for name in [m for m in list(sys.modules)
                 if m in ("mss", "PIL", "pynput") or m.startswith(("mss.", "PIL.", "pynput."))]:
        sys.modules.pop(name, None)
    try:
        import mss as _mss
        from PIL import Image as _Image
        mss, Image = _mss, _Image
        _SCREEN_AVAILABLE = True
        _SCREEN_ERROR = ""
        _print("[repair] mss/Pillow reinstalled successfully.")
    except Exception as e:
        _SCREEN_AVAILABLE = False
        _SCREEN_ERROR = f"{type(e).__name__}: {e}"
        _print(f"[repair] mss/Pillow still not loadable: {e}")
    try:
        from pynput.mouse import Controller as _MC, Button as _MB
        from pynput.keyboard import Controller as _KC, Key as _KK
        MouseController, MouseButton, KeyboardController, KeyboardKey = _MC, _MB, _KC, _KK
        _INPUT_AVAILABLE = True
        _INPUT_ERROR = ""
    except Exception as e:
        _INPUT_AVAILABLE = False
        _INPUT_ERROR = f"{type(e).__name__}: {e}"
    return _SCREEN_AVAILABLE


_winpty_repair_tried = False


def _winpty_diagnose() -> str:
    """
    Collects what is needed to make sense of a broken pywinpty.

    The typical error 'No module named winpty._winpty' almost always means:
    there is a package named 'winpty' on the search path whose compiled part
    (_winpty.pyd) is missing - either a leftover of an old pywinpty 1.x version
    or the old, different PyPI package 'winpty'. A 'pip install pywinpty'
    changes nothing, because pip considers the existing directory installed
    or the leftover still shadows it.

    Second common case: 'pip install' ran in a DIFFERENT Python than the one
    the agent runs with. That is why the interpreter path is included in the
    output - it shows the difference at a glance.
    """
    lines = [f"Python: {sys.executable}"]
    try:
        import importlib.util
        spec = importlib.util.find_spec("winpty")
        lines.append(f"winpty found at: {getattr(spec, 'origin', None)}")
    except Exception as e:
        lines.append(f"winpty not found: {e}")
    try:
        from importlib.metadata import version, distributions
        try:
            lines.append(f"pywinpty version: {version('pywinpty')}")
        except Exception:
            lines.append("pywinpty: not registered as a package")
        # The OLD, same-named package 'winpty' is the most common cause.
        for d in distributions():
            if (d.metadata.get("Name") or "").lower() == "winpty":
                lines.append(f"WARNING: old package 'winpty' {d.version} installed "
                             f"- this collides with pywinpty")
    except Exception:
        pass
    return "\n".join(lines)


def _winpty_repair() -> bool:
    """
    One-time repair attempt: remove the colliding 'winpty', delete leftovers
    and cleanly reinstall pywinpty. Returns True if the import works
    afterwards.
    """
    global _winpty_repair_tried
    if _winpty_repair_tried:
        return False
    _winpty_repair_tried = True

    pip = _pip
    _print("[term] repairing pywinpty…")
    # 1) Remove both packages cleanly (the old 'winpty' is the troublemaker).
    pip("uninstall", "-y", "winpty")
    pip("uninstall", "-y", "pywinpty")
    # 2) Delete the leftover directory - without this Python still finds a
    #    'winpty' without the compiled part.
    try:
        import site
        import shutil
        for base in set(site.getsitepackages() + [site.getusersitepackages()]):
            leftover = Path(base) / "winpty"
            if leftover.is_dir():
                shutil.rmtree(leftover, ignore_errors=True)
                _print(f"[term] leftover removed: {leftover}")
    except Exception as e:
        _print(f"[term] cleanup skipped: {e}")
    # 3) Reinstall.
    pip("install", "--no-cache-dir", "--force-reinstall", "pywinpty>=2.0.0")

    try:
        import importlib
        importlib.invalidate_caches()
        for name in [m for m in list(sys.modules) if m == "winpty" or m.startswith("winpty.")]:
            sys.modules.pop(name, None)
        importlib.import_module("winpty")
        _print("[term] pywinpty repaired successfully.")
        return True
    except Exception as e:
        _print(f"[term] repair failed: {e}")
        return False


class _WinPipeTerminal:
    """
    Fallback terminal for Windows WITHOUT pywinpty.

    Instead of a real ConPTY, the shell runs on normal pipes. This is a
    deliberate compromise, but a usable one: commands, output and input work.
    What does NOT work are full-screen applications (nano-like editors,
    progress bars with cursor control) and resizing - those need a real PTY.

    The point: better a limited terminal than none at all, if pywinpty simply
    will not run on the machine.
    """

    LIMITED_NOTE = ("\x1b[33m[Limited mode: without pywinpty the shell runs "
                    "on plain pipes. Full-screen programs and resizing "
                    "do not work here.]\x1b[0m\r\n")

    def __init__(self, session_id, shell, cols, rows, loop):
        self.session_id = session_id
        self.loop = loop
        self.alive = True
        if shell == "powershell":
            cmd = ["powershell.exe", "-NoLogo", "-NoProfile", "-NoExit", "-Command", "-"]
        else:
            cmd = ["cmd.exe", "/Q"]
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0, startupinfo=si,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
        self._emit(self.LIMITED_NOTE)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _emit(self, text):
        asyncio.run_coroutine_threadsafe(
            sio.emit("term-output", {"id": DEVICE_ID, "session": self.session_id,
                                     "data": text}, namespace="/agent"),
            self.loop,
        )

    def _read_loop(self):
        while self.alive:
            try:
                chunk = self.proc.stdout.read(1)
                if not chunk:
                    break
                # Read everything already available, so the output does not
                # trickle through the network character by character.
                try:
                    import msvcrt  # noqa: F401  (Windows only)
                except Exception:
                    pass
                extra = self.proc.stdout.read1(65536) if hasattr(self.proc.stdout, "read1") else b""
                data = (chunk + (extra or b"")).decode("utf-8", errors="replace")
            except Exception:
                break
            self._emit(data.replace("\n", "\r\n") if "\r\n" not in data else data)
        self.alive = False
        asyncio.run_coroutine_threadsafe(
            sio.emit("term-exit", {"id": DEVICE_ID, "session": self.session_id},
                     namespace="/agent"),
            self.loop,
        )

    def write(self, data: str):
        try:
            self.proc.stdin.write(data.replace("\r", "\r\n").encode("utf-8", "replace"))
            self.proc.stdin.flush()
        except Exception:
            pass

    def resize(self, cols, rows):
        pass          # not possible without a PTY

    def close(self):
        self.alive = False
        try:
            self.proc.kill()
        except Exception:
            pass


# ------------------------------------------------------------------
# BACKUP & SNAPSHOTS (module "backup", Proxmox style)
#   Engine restic (folder OR image; incremental, deduplicated, end-to-end
#   encrypted) - repo depends on the path:
#     rest_path -> restic REST on the backend (local or via backend to a NAS)
#     smb       -> Windows: UNC path, Linux: short-lived CIFS mount
#     sftp      -> restic SFTP with key login
#   Engine pbs: Proxmox Backup Server via proxmox-backup-client (Linux).
#   Image: Linux reads the block device (default: system disk), Windows a
#   VSS shadow copy of C: (consistent volume state).
#   OS snapshots: Windows VSS, Linux btrfs / ZFS / LVM.
# ------------------------------------------------------------------
_BK_TOOLS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools")
_NOWIN = 0x08000000 if IS_WINDOWS else 0


class _BkError(Exception):
    pass


def _bk_run(cmd, timeout=600, input_text=None, env=None):
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout,
                       input=input_text, env=env, creationflags=_NOWIN)
    return r.returncode, r.stdout or "", r.stderr or ""


def _bk_arch() -> str:
    m = platform.machine().lower()
    return {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64",
            "i686": "386", "x86": "386", "armv7l": "arm"}.get(m, m)


def _restic_bin() -> str:
    found = shutil.which("restic")
    if found:
        return found
    name = "restic.exe" if IS_WINDOWS else "restic"
    p = os.path.join(_BK_TOOLS, name)
    if os.path.isfile(p) and os.path.getsize(p) > 1_000_000:
        return p
    os.makedirs(_BK_TOOLS, exist_ok=True)
    osn = "windows" if IS_WINDOWS else platform.system().lower()
    data = _backend_fetch(f"/api/backup/tools/restic?os={osn}&arch={_bk_arch()}",
                          timeout=900, limit=200 * 1024 * 1024)
    tmp = p + ".part"
    with open(tmp, "wb") as fh:
        fh.write(data)
    if not IS_WINDOWS:
        os.chmod(tmp, 0o755)
    os.replace(tmp, p)
    return p


def _iso_ms(s: str) -> int:
    from datetime import datetime
    s = str(s or "")
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)?", s)
    if not m:
        return 0
    frac = (m.group(2) or ".0")[:7]
    tz = m.group(3) or "+00:00"
    tz = "+00:00" if tz == "Z" else tz
    try:
        return int(datetime.fromisoformat(m.group(1) + frac + tz).timestamp() * 1000)
    except ValueError:
        return 0


class _Repo:
    """Prepares access to a restic repo (mount, keys ...)."""

    def __init__(self, spec: dict):
        self.spec = spec or {}
        self.env = dict(os.environ)
        self.extra: list = []
        self.repo = ""
        self._cleanup: list = []
        self.insecure = False

    def __enter__(self):
        s = self.spec
        self.env["RESTIC_PASSWORD"] = s.get("password") or ""
        self.env["RESTIC_CACHE_DIR"] = os.path.join(_BK_TOOLS, "cache")
        sub = (s.get("subdir") or "").strip("/")
        if s.get("rest_path"):
            self.repo = "rest:" + BACKEND_URL.rstrip("/") + s["rest_path"]
        elif s.get("smb"):
            c = s["smb"]
            share = "\\\\" + c["server"] + "\\" + c["share"].strip("\\/")
            path = "\\".join(x for x in [c.get("path", "").strip("\\/").replace("/", "\\"),
                                          sub.replace("/", "\\")] if x)
            if IS_WINDOWS:
                user = c.get("user") or ""
                if user and c.get("domain") and "\\" not in user:
                    user = c["domain"] + "\\" + user
                cmd = ["net", "use", share]
                if user:
                    cmd += [c.get("password") or "", f"/user:{user}"]
                _bk_run(cmd + ["/persistent:no"], timeout=60)
                self._cleanup.append(lambda: _bk_run(["net", "use", share, "/delete", "/y"], timeout=30))
                self.repo = share + "\\" + path
                os.makedirs(self.repo, exist_ok=True)
            else:
                mnt = tempfile.mkdtemp(prefix="bytera-smb-")
                cred = os.path.join(tempfile.mkdtemp(prefix="bytera-cred-"), "c")
                with open(os.open(cred, os.O_WRONLY | os.O_CREAT, 0o600), "w") as fh:
                    fh.write(f"username={c.get('user') or 'guest'}\npassword={c.get('password') or ''}\n")
                    if c.get("domain"):
                        fh.write(f"domain={c['domain']}\n")
                rc, _o, err = _bk_run(["mount", "-t", "cifs", "//" + c["server"] + "/" + c["share"].strip("\\/"),
                                       mnt, "-o", f"credentials={cred},uid=0,gid=0"], timeout=60)
                shutil.rmtree(os.path.dirname(cred), ignore_errors=True)
                if rc != 0:
                    os.rmdir(mnt)
                    raise _BkError(f"SMB share could not be mounted (cifs-utils installed?): {err.strip()[:300]}")
                self._cleanup.append(lambda: (_bk_run(["umount", mnt], timeout=60), os.rmdir(mnt)))
                self.repo = os.path.join(mnt, path.replace("\\", "/"))
                os.makedirs(self.repo, exist_ok=True)
        elif s.get("sftp"):
            c = s["sftp"]
            if not c.get("private_key"):
                raise _BkError("Direct SFTP needs an SSH key (create it in the storage target)")
            kd = tempfile.mkdtemp(prefix="bytera-ssh-")
            key = os.path.join(kd, "id")
            with open(os.open(key, os.O_WRONLY | os.O_CREAT, 0o600), "w") as fh:
                fh.write(c["private_key"].strip() + "\n")
            self._cleanup.append(lambda: shutil.rmtree(kd, ignore_errors=True))
            kh = os.path.join(_BK_TOOLS, "known_hosts")
            os.makedirs(_BK_TOOLS, exist_ok=True)
            base = (c.get("path") or "").rstrip("/")
            self.repo = f"sftp:{c['user']}@{c['host']}:{base}/{sub}"
            self.extra = ["-o", f"sftp.command=ssh {c['user']}@{c['host']} -p {int(c.get('port') or 22)} "
                                f"-i {key} -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile={kh} -s sftp"]
        else:
            raise _BkError("Unknown repo")
        self.env["RESTIC_REPOSITORY"] = self.repo
        return self

    def __exit__(self, *exc):
        for fn in reversed(self._cleanup):
            try:
                fn()
            except Exception:
                pass


def _restic(rp: _Repo, args: list, stdin=None, progress=None, timeout=None):
    """Run restic; JSON status lines become progress messages."""
    def once(insecure):
        cmd = [_restic_bin(), *rp.extra, *(["--insecure-tls"] if insecure else []), *args]
        p = subprocess.Popen(cmd, stdin=stdin if stdin is not None else subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=rp.env,
                             text=True, errors="replace", creationflags=_NOWIN)
        out, last = [], 0.0
        for line in p.stdout:
            line = line.rstrip("\n")
            if line.startswith('{"message_type":"status"'):
                if progress and time.time() - last > 5:
                    last = time.time()
                    try:
                        st = json.loads(line)
                        done = st.get("bytes_done") or 0
                        total = st.get("total_bytes") or 0
                        progress(f"{(st.get('percent_done') or 0) * 100:.1f} % - "
                                 f"{done / 1048576:.0f} / {total / 1048576:.0f} MB")
                    except ValueError:
                        pass
                continue
            out.append(line)
        err = p.stderr.read()
        rc = p.wait(timeout=timeout)
        return rc, "\n".join(out), err
    rc, out, err = once(rp.insecure)
    if rc != 0 and not rp.insecure and stdin is None and rp.repo.startswith("rest:https") \
            and ("x509" in err or "certificate" in err):
        rp.insecure = True                    # self-signed backend certificate
        rc, out, err = once(True)
    return rc, out, err


def _restic_init(rp: _Repo) -> None:
    rc, _o, err = _restic(rp, ["cat", "config"], timeout=300)
    if rc == 0:
        return
    rc, _o, err2 = _restic(rp, ["init", "--repository-version", "2"], timeout=300)
    if rc != 0:
        raise _BkError(f"Repo could not be created: {(err2 or err).strip()[-400:]}")


def _restic_snapshots(rp: _Repo) -> list:
    rc, out, err = _restic(rp, ["snapshots", "--json", "--no-lock"], timeout=600)
    if rc != 0:
        if "does not exist" in err or "unable to open config" in err:
            return []
        raise _BkError(err.strip()[-400:])
    items = []
    for s in json.loads(out or "[]") or []:
        tags = s.get("tags") or []
        summ = s.get("summary") or {}
        items.append({"id": s.get("id"), "short": s.get("short_id"), "time": _iso_ms(s.get("time")),
                      "paths": s.get("paths") or [], "host": s.get("hostname"),
                      "mode": "image" if "bytera-image" in tags else "files",
                      "protected": "protected" in tags,
                      "size": summ.get("total_bytes_processed"), "added": summ.get("data_added")})
    return sorted(items, key=lambda x: x["time"], reverse=True)


def _root_disk() -> str:
    rc, src, _e = _bk_run(["findmnt", "-no", "SOURCE", "/"], timeout=20)
    rc, out, _e = _bk_run(["lsblk", "-nspo", "NAME,TYPE", src.strip()], timeout=20)
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == "disk":
            return parts[0].lstrip("`-|└─├ ")
    raise _BkError("System disk could not be determined - please specify the device in the job")


def _ps(script: str, timeout=120):
    return _bk_run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout=timeout)


def _vss_create(volume="C:\\") -> tuple:
    rc, out, err = _ps(
        f"$r=(Get-WmiObject -List Win32_ShadowCopy).Create('{volume}','ClientAccessible');"
        "if($r.ReturnValue -ne 0){throw \"VSS error $($r.ReturnValue)\"};"
        "$c=Get-WmiObject Win32_ShadowCopy | Where-Object {$_.ID -eq $r.ShadowID};"
        "\"$($c.ID)|$($c.DeviceObject)\"", timeout=300)
    if rc != 0 or "|" not in out:
        raise _BkError(f"VSS shadow copy failed: {(err or out).strip()[-300:]}")
    sid, dev = out.strip().splitlines()[-1].split("|", 1)
    return sid.strip(), dev.strip()


def _vss_delete(sid: str) -> None:
    _bk_run(["vssadmin", "delete", "shadows", f"/shadow={sid}", "/quiet"], timeout=120)


def _bk_backup_restic(spec, args, progress) -> dict:
    with _Repo(spec) as rp:
        _restic_init(rp)
        tags = ["--tag", "bytera"]
        if args.get("job"):
            tags += ["--tag", f"job:{args['job']}"]
        comp = ["--compression", args.get("compression") or "auto"]
        summary = None
        if args.get("mode") == "image":
            tags += ["--tag", "bytera-image"]
            if IS_WINDOWS:
                sid, dev = _vss_create("C:\\")
                progress(f"VSS shadow copy created ({sid}) - reading volume C: …")
                try:
                    p = subprocess.Popen([_restic_bin(), *rp.extra,
                                          *(["--insecure-tls"] if rp.insecure else []),
                                          "backup", "--json", *tags, *comp,
                                          "--stdin", "--stdin-filename", "volume-C.img"],
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, env=rp.env, creationflags=_NOWIN)
                    err_box = []

                    def feeder():
                        try:
                            with open(dev, "rb", buffering=0) as src:
                                while True:
                                    try:
                                        chunk = src.read(4 * 1024 * 1024)
                                    except OSError:
                                        break             # end of the volume
                                    if not chunk:
                                        break
                                    p.stdin.write(chunk)
                        except Exception as e:
                            err_box.append(str(e))
                        finally:
                            try:
                                p.stdin.close()
                            except Exception:
                                pass
                    th = threading.Thread(target=feeder, daemon=True)
                    th.start()
                    out_lines = [ln.decode("utf-8", "replace") for ln in p.stdout]
                    th.join()
                    err = p.stderr.read().decode("utf-8", "replace")
                    if p.wait() != 0 or err_box:
                        raise _BkError(f"Image backup failed: {(err_box[0] if err_box else err)[-400:]}")
                    out = "".join(out_lines)
                finally:
                    _vss_delete(sid)
            else:
                dev = args.get("image_device") or _root_disk()
                progress(f"Reading disk {dev} (crash-consistent) …")
                dd = subprocess.Popen(["dd", f"if={dev}", "bs=4M", "status=none"], stdout=subprocess.PIPE)
                rc, out, err = _restic(rp, ["backup", "--json", *tags, *comp, "--stdin",
                                            "--stdin-filename", f"disk-{os.path.basename(dev)}.img"],
                                       stdin=dd.stdout, progress=progress)
                dd.wait()
                if rc != 0:
                    raise _BkError(f"Image backup failed: {err.strip()[-400:]}")
        else:
            paths = [p for p in (args.get("paths") or []) if p.strip()]
            if not paths:
                paths = (["C:\\Users"] if IS_WINDOWS else ["/etc", "/home", "/root"])
                paths = [p for p in paths if os.path.exists(p)]
            ex = []
            for e in args.get("excludes") or []:
                ex += ["--exclude", e]
            vss = ["--use-fs-snapshot"] if IS_WINDOWS else []
            progress(f"Backing up {', '.join(paths)} …")
            rc, out, err = _restic(rp, ["backup", "--json", *tags, *comp, *vss, *ex, *paths],
                                   progress=progress)
            if rc not in (0, 3):                  # 3 = some individual files not readable
                raise _BkError(f"Backup failed: {err.strip()[-400:]}")
            if rc == 3:
                progress("Note: some files were not readable and are missing from the backup")
        for line in (out or "").splitlines():
            if '"message_type":"summary"' in line:
                try:
                    summary = json.loads(line)
                except ValueError:
                    pass
        for sid in args.get("protected") or []:
            _restic(rp, ["tag", "--add", "protected", sid], timeout=300)
        keep = args.get("keep") or {}
        if keep:
            flags = []
            for k, v in keep.items():
                flags += [f"--{k.replace('_', '-')}", str(int(v))]
            progress(f"Applying retention ({', '.join(f'{k}={v}' for k, v in keep.items())}) …")
            rc, _o, err = _restic(rp, ["forget", "--tag", "bytera", "--keep-tag", "protected",
                                       *flags, "--prune"], timeout=6 * 3600)
            if rc != 0:
                progress(f"Retention failed: {err.strip()[-200:]}")
        snaps = _restic_snapshots(rp)
        return {"snapshot_id": (summary or {}).get("snapshot_id"),
                "summary": {k: (summary or {}).get(k) for k in
                            ("files_new", "files_changed", "data_added", "total_bytes_processed",
                             "total_duration")},
                "snapshots": snaps}


def _bk_restore_restic(spec, args, progress) -> dict:
    sid = args.get("id") or ""
    with _Repo(spec) as rp:
        snaps = {s["id"]: s for s in _restic_snapshots(rp)}
        snap = snaps.get(sid)
        if not snap:
            raise _BkError("Backup not found")
        if snap["mode"] == "image":
            dest = args.get("image_dest") or ""
            if not dest:
                raise _BkError("For an image, specify a target file (or a device on Linux)")
            name = (snap.get("paths") or ["/"])[0]
            if args.get("image_to_device"):
                if IS_WINDOWS:
                    raise _BkError("On Windows an image can only be restored as a file")
                with open("/proc/mounts") as fh:
                    if any(line.split()[0].startswith(dest) for line in fh):
                        raise _BkError(f"{dest} is mounted - only write to a device that is NOT in use")
            progress(f"Writing image to {dest} …")
            with open(dest, "wb") as fh:
                p = subprocess.Popen([_restic_bin(), *rp.extra,
                                      *(["--insecure-tls"] if rp.insecure else []),
                                      "dump", sid, name], stdout=fh,
                                     stderr=subprocess.PIPE, env=rp.env, creationflags=_NOWIN)
                err = p.stderr.read().decode("utf-8", "replace")
                if p.wait() != 0:
                    raise _BkError(err[-400:])
            return {"restored": dest}
        inc = []
        for i in args.get("include") or []:
            inc += ["--include", i]
        if args.get("target") == "path":
            target = args.get("path") or ""
            if not target:
                raise _BkError("Target folder missing")
            os.makedirs(target, exist_ok=True)
            rc, _o, err = _restic(rp, ["restore", sid, "--target", target, *inc], progress=progress)
        elif IS_WINDOWS:
            # restic stores C:\x as /C/x -> restore each drive into its own root
            drives = sorted({p.strip("/").split("/")[0] for p in snap.get("paths") or [] if p.strip("/")})
            rc, err = 0, ""
            for d in drives:
                dinc = []
                for i in args.get("include") or []:
                    if i.startswith(f"/{d}/") or i == f"/{d}":
                        dinc += ["--include", i[len(d) + 1:] or "/"]
                if args.get("include") and not dinc:
                    continue
                rc, _o, err = _restic(rp, ["restore", f"{sid}:/{d}", "--target", f"{d}:\\", *dinc],
                                      progress=progress)
                if rc != 0:
                    break
        else:
            rc, _o, err = _restic(rp, ["restore", sid, "--target", "/", *inc], progress=progress)
        if rc != 0:
            raise _BkError(f"Restore failed: {err.strip()[-400:]}")
        return {"restored": args.get("path") or "original location"}


# ---------------- Proxmox Backup Server (Linux) ----------------
def _pbs_bin() -> str:
    if IS_WINDOWS:
        raise _BkError("There is no Proxmox backup client for Windows - please use SMB/SFTP/backend "
                       "or back up the client (if VM/LXC) via the Proxmox module")
    found = shutil.which("proxmox-backup-client")
    if found:
        return found
    rc, _o, err = _bk_run(["apt-get", "install", "-y", "proxmox-backup-client"], timeout=900,
                          env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"})
    found = shutil.which("proxmox-backup-client")
    if not found:
        raise _BkError("proxmox-backup-client is missing and could not be installed "
                       f"(Proxmox package source set up?): {err.strip()[-200:]}")
    return found


def _pbs_env(spec) -> tuple:
    c = spec["pbs"]
    env = {**os.environ, "PBS_PASSWORD": c.get("secret") or "",
           "PBS_REPOSITORY": f"{c['user']}@{c['server']}:{int(c.get('port') or 8007)}:{c['datastore']}"}
    if c.get("fingerprint"):
        env["PBS_FINGERPRINT"] = c["fingerprint"]
    ns = ["--ns", c["namespace"]] if c.get("namespace") else []
    return env, ns


def _pbs_snapshots(spec) -> list:
    env, ns = _pbs_env(spec)
    group = f"host/{spec['backup_id']}"
    rc, out, err = _bk_run([_pbs_bin(), "snapshot", "list", group, "--output-format", "json", *ns],
                           timeout=300, env=env)
    if rc != 0:
        if "does not exist" in err or "not found" in err.lower():
            return []
        raise _BkError(err.strip()[-300:])
    items = []
    for s in json.loads(out or "[]"):
        t = int(s.get("backup-time") or 0)
        files = [f.get("filename") for f in s.get("files") or []]
        items.append({"id": f"host/{spec['backup_id']}/{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t))}",
                      "time": t * 1000, "size": s.get("size"), "notes": s.get("comment") or "",
                      "protected": bool(s.get("protected")),
                      "mode": "image" if any(str(f).startswith("disk") for f in files) else "files",
                      "paths": [_pbs_decode(f) for f in files if str(f).endswith(".pxar.didx")]})
    return sorted(items, key=lambda x: x["time"], reverse=True)


def _pbs_name(path: str) -> str:
    return "p" + path.encode("utf-8").hex()


def _pbs_decode(fname: str) -> str:
    base = str(fname).split(".")[0]
    try:
        return bytes.fromhex(base[1:]).decode("utf-8") if base.startswith("p") else base
    except ValueError:
        return base


def _bk_backup_pbs(spec, args, progress) -> dict:
    env, ns = _pbs_env(spec)
    if args.get("mode") == "image":
        dev = args.get("image_device") or _root_disk()
        archives = [f"disk.img:{dev}"]
    else:
        paths = [p for p in (args.get("paths") or []) if p.strip()] or ["/etc", "/home", "/root"]
        archives = [f"{_pbs_name(p)}.pxar:{p}" for p in paths if os.path.exists(p)]
    progress(f"Proxmox Backup Server: backing up {len(archives)} archive(s) …")
    rc, out, err = _bk_run([_pbs_bin(), "backup", *archives, "--backup-type", "host",
                            "--backup-id", spec["backup_id"], *ns], timeout=24 * 3600, env=env)
    if rc != 0:
        raise _BkError(f"PBS backup failed: {err.strip()[-400:]}")
    snaps = _pbs_snapshots(spec)
    newest = snaps[0]["id"] if snaps else None
    if newest and args.get("name"):
        _bk_run([_pbs_bin(), "snapshot", "notes", "update", newest, args["name"], *ns], timeout=120, env=env)
    keep = args.get("keep") or {}
    if keep:
        flags = []
        for k, v in keep.items():
            flags += [f"--{k.replace('_', '-')}", str(int(v))]
        _bk_run([_pbs_bin(), "prune", f"host/{spec['backup_id']}", *flags, *ns], timeout=3600, env=env)
        snaps = _pbs_snapshots(spec)
    return {"snapshot_id": newest, "snapshots": snaps}


def _bk_restore_pbs(spec, args, progress) -> dict:
    env, ns = _pbs_env(spec)
    snap = next((s for s in _pbs_snapshots(spec) if s["id"] == args.get("id")), None)
    if not snap:
        raise _BkError("Backup not found")
    if snap["mode"] == "image":
        dest = args.get("image_dest") or ""
        if not dest:
            raise _BkError("Specify a target file or device")
        rc, _o, err = _bk_run([_pbs_bin(), "restore", snap["id"], "disk.img", dest, *ns],
                              timeout=24 * 3600, env=env)
    else:
        rc, err = 0, ""
        for p in snap["paths"]:
            target = os.path.join(args["path"], p.strip("/").replace("/", "_")) \
                if args.get("target") == "path" else p
            progress(f"Restoring {p} to {target} …")
            rc, _o, err = _bk_run([_pbs_bin(), "restore", snap["id"], f"{_pbs_name(p)}.pxar", target,
                                   "--overwrite", "true", *ns], timeout=24 * 3600, env=env)
            if rc != 0:
                break
    if rc != 0:
        raise _BkError(f"PBS restore failed: {err.strip()[-400:]}")
    return {"restored": args.get("path") or args.get("image_dest") or "original location"}


# ---------------- Operating-system snapshots ----------------
def _fs_root_info():
    rc, out, _e = _bk_run(["findmnt", "-no", "FSTYPE,SOURCE,TARGET", "/"], timeout=20)
    parts = out.split()
    if len(parts) < 3:
        raise _BkError("File system could not be determined")
    return parts[0], parts[1].split("[")[0], parts[2]


def _lvm_names(source: str):
    rc, out, _e = _bk_run(["lvs", "--noheadings", "--separator", "|", "-o", "vg_name,lv_name", source], timeout=30)
    if rc != 0 or "|" not in out:
        return None
    vg, lv = out.strip().split("|", 1)
    return vg.strip(), lv.strip()


def _fs_snap(op: str, args: dict) -> dict:
    name = re.sub(r"[^A-Za-z0-9_-]", "_", str(args.get("name") or "snap"))[:40]
    if IS_WINDOWS:
        if op == "list":
            rc, out, err = _ps(
                "$v=@{};Get-CimInstance Win32_Volume|%{$v[$_.DeviceID]=$_.DriveLetter};"
                "Get-CimInstance Win32_ShadowCopy|%{[pscustomobject]@{id=$_.ID;"
                "time=[int64](([datetime]$_.InstallDate).ToUniversalTime()-[datetime]'1970-01-01').TotalMilliseconds;"
                "volume=$v[$_.VolumeName];device=$_.DeviceObject}}|ConvertTo-Json -Compress")
            data = json.loads(out or "[]") if out.strip() else []
            data = [data] if isinstance(data, dict) else data
            return {"kind": "vss", "snapshots": [{"id": d["id"], "time": d["time"],
                                                  "volume": d.get("volume") or "", "device": d.get("device")}
                                                 for d in data],
                    "info": "Windows shadow copies (VSS). Individual files/folders can be restored; "
                            "the whole system drive cannot be rolled back while running."}
        if op == "create":
            sid, _dev = _vss_create((args.get("volume") or "C:").rstrip("\\") + "\\")
            return {"id": sid}
        if op == "delete":
            _vss_delete(args["id"])
            return {}
        if op == "restore":
            snaps = {s["id"]: s for s in _fs_snap("list", {})["snapshots"]}
            s = snaps.get(args["id"])
            path = str(args.get("path") or "")
            if not s or len(path) < 3 or path[1:3] != ":\\":
                raise _BkError("Snapshot or path invalid (e.g. C:\\Users\\john\\Documents)")
            link = os.path.join(os.environ.get("ProgramData", "C:\\ProgramData"), f"bytera-shadow-{uuid.uuid4().hex[:8]}")
            _bk_run(["cmd", "/c", "mklink", "/d", link, s["device"] + "\\"], timeout=60)
            try:
                src = os.path.join(link, path[3:])
                dest = args.get("dest") or path
                if os.path.isdir(src):
                    rc, out, err = _bk_run(["robocopy", src, dest, "/E", "/COPY:DAT", "/R:1", "/W:1",
                                            "/NFL", "/NDL"], timeout=6 * 3600)
                    if rc >= 8:
                        raise _BkError(f"robocopy error {rc}: {(out + err)[-300:]}")
                else:
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    shutil.copy2(src, dest)
            finally:
                _bk_run(["cmd", "/c", "rmdir", link], timeout=60)
            return {"note": f"{path} restored to {args.get('dest') or path}"}
        if op == "rollback":
            raise _BkError("Windows shadow copies cannot be fully rolled back - "
                           "please restore individual files/folders")
    fstype, source, mnt = _fs_root_info()
    if fstype == "btrfs":
        sdir = os.path.join(mnt, ".snapshots")
        if op == "list":
            items = []
            if os.path.isdir(sdir):
                for n in sorted(os.listdir(sdir)):
                    if n.startswith("bytera-"):
                        m = re.match(r"bytera-(\d+)-(.*)", n)
                        items.append({"id": n, "time": int(m.group(1)) * 1000 if m else 0,
                                      "name": m.group(2) if m else n})
            return {"kind": "btrfs", "snapshots": items,
                    "info": "btrfs snapshots (read-only) under /.snapshots. Files can be restored."}
        if op == "create":
            os.makedirs(sdir, exist_ok=True)
            sid = f"bytera-{int(time.time())}-{name}"
            rc, _o, err = _bk_run(["btrfs", "subvolume", "snapshot", "-r", mnt, os.path.join(sdir, sid)])
            if rc != 0:
                raise _BkError(err.strip()[-300:])
            return {"id": sid}
        if op == "delete":
            rc, _o, err = _bk_run(["btrfs", "subvolume", "delete", os.path.join(sdir, os.path.basename(args["id"]))])
            if rc != 0:
                raise _BkError(err.strip()[-300:])
            return {}
        snap_root = os.path.join(sdir, os.path.basename(args.get("id") or ""))
    elif fstype == "zfs":
        if op == "list":
            rc, out, err = _bk_run(["zfs", "list", "-H", "-p", "-t", "snapshot", "-o", "name,creation", "-d", "1", source])
            items = []
            for line in out.splitlines():
                n, c = line.split("\t")
                items.append({"id": n, "time": int(c) * 1000, "name": n.split("@", 1)[1]})
            return {"kind": "zfs", "snapshots": items, "info": f"ZFS snapshots of {source}."}
        if op == "create":
            sid = f"{source}@bytera-{name}"
            rc, _o, err = _bk_run(["zfs", "snapshot", sid])
            if rc != 0:
                raise _BkError(err.strip()[-300:])
            return {"id": sid}
        if op == "delete":
            if not str(args["id"]).startswith(source + "@"):
                raise _BkError("Invalid snapshot")
            rc, _o, err = _bk_run(["zfs", "destroy", args["id"]])
            if rc != 0:
                raise _BkError(err.strip()[-300:])
            return {}
        if op == "rollback":
            if mnt == "/":
                raise _BkError("The running root file system cannot be rolled back live - "
                               "please restore individual files")
            rc, _o, err = _bk_run(["zfs", "rollback", "-r", args["id"]])
            if rc != 0:
                raise _BkError(err.strip()[-300:])
            return {"note": "rolled back"}
        snap_root = os.path.join(mnt, ".zfs", "snapshot", str(args.get("id", "")).split("@", 1)[-1])
    else:
        names = _lvm_names(source)
        if not names:
            raise _BkError(f"File system {fstype} on {source} does not support snapshots "
                           "(btrfs, ZFS or LVM needed)")
        vg, lv = names
        if op == "list":
            rc, out, _e = _bk_run(["lvs", "--noheadings", "--separator", "|", "-o",
                                   "lv_name,lv_time,origin", "--config", "report/time_format=%s", vg])
            items = []
            for line in out.splitlines():
                p = [x.strip() for x in line.split("|")]
                if len(p) == 3 and p[2] == lv and p[0].startswith("bytera_"):
                    items.append({"id": f"{vg}/{p[0]}", "time": int(p[1]) * 1000 if p[1].isdigit() else 0,
                                  "name": p[0][7:]})
            return {"kind": "lvm", "snapshots": items,
                    "info": "LVM snapshots. Rollback is applied on the next reboot."}
        if op == "create":
            sid = f"{vg}/bytera_{name}"
            rc, _o, err = _bk_run(["lvcreate", "-s", "-n", f"bytera_{name}", "-l", "20%ORIGIN", f"{vg}/{lv}"])
            if rc != 0:
                raise _BkError(f"LVM snapshot failed (free space in the VG?): {err.strip()[-300:]}")
            return {"id": sid}
        if op == "delete":
            rc, _o, err = _bk_run(["lvremove", "-y", args["id"]])
            if rc != 0:
                raise _BkError(err.strip()[-300:])
            return {}
        if op == "rollback":
            rc, _o, err = _bk_run(["lvconvert", "--merge", args["id"]])
            if rc != 0:
                raise _BkError(err.strip()[-300:])
            return {"note": "Rollback scheduled - will be applied on the next reboot"}
        tmp = tempfile.mkdtemp(prefix="bytera-lvsnap-")
        rc, _o, err = _bk_run(["mount", "-o", "ro,nouuid" if fstype == "xfs" else "ro", f"/dev/{args['id']}", tmp])
        if rc != 0:
            os.rmdir(tmp)
            raise _BkError(err.strip()[-300:])
        try:
            return _fs_copy_back(tmp, mnt, args)
        finally:
            _bk_run(["umount", tmp])
            os.rmdir(tmp)
    if op == "rollback":
        raise _BkError("For this snapshot type, please restore individual files/folders")
    return _fs_copy_back(snap_root, mnt, args)


def _fs_copy_back(snap_root: str, mnt: str, args: dict) -> dict:
    path = os.path.abspath(str(args.get("path") or ""))
    rel = os.path.relpath(path, mnt)
    if rel.startswith(".."):
        raise _BkError("Path is not on this file system")
    src = os.path.join(snap_root, rel)
    if not os.path.exists(src):
        raise _BkError("Path does not exist in the snapshot")
    dest = args.get("dest") or path
    if os.path.isdir(src):
        os.makedirs(dest, exist_ok=True)
        rc, _o, err = _bk_run(["cp", "-a", src + "/.", dest + "/"], timeout=6 * 3600)
    else:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        rc, _o, err = _bk_run(["cp", "-a", src, dest], timeout=6 * 3600)
    if rc != 0:
        raise _BkError(err.strip()[-300:])
    return {"note": f"{path} restored to {dest}"}


def _bk_dispatch(op: str, spec: dict, args: dict, progress) -> dict:
    pbs = spec.get("kind") == "pbs"
    if op == "backup":
        return _bk_backup_pbs(spec, args, progress) if pbs else _bk_backup_restic(spec, args, progress)
    if op == "snapshots":
        if pbs:
            return {"snapshots": _pbs_snapshots(spec)}
        with _Repo(spec) as rp:
            return {"snapshots": _restic_snapshots(rp)}
    if op == "restore":
        return _bk_restore_pbs(spec, args, progress) if pbs else _bk_restore_restic(spec, args, progress)
    if op == "forget":
        if pbs:
            env, ns = _pbs_env(spec)
            for sid in args.get("ids") or []:
                rc, _o, err = _bk_run([_pbs_bin(), "snapshot", "forget", sid, *ns], timeout=600, env=env)
                if rc != 0:
                    raise _BkError(err.strip()[-300:])
            return {"snapshots": _pbs_snapshots(spec)}
        with _Repo(spec) as rp:
            rc, _o, err = _restic(rp, ["forget", *(args.get("ids") or []), "--prune"], timeout=6 * 3600)
            if rc != 0:
                raise _BkError(err.strip()[-300:])
            return {"snapshots": _restic_snapshots(rp)}
    if op == "meta":
        if pbs:
            env, ns = _pbs_env(spec)
            if args.get("notes") is not None:
                _bk_run([_pbs_bin(), "snapshot", "notes", "update", args["id"], args["notes"] or "", *ns],
                        timeout=120, env=env)
            if args.get("protected") is not None:
                _bk_run([_pbs_bin(), "snapshot", "protected", "update", args["id"],
                         "true" if args["protected"] else "false", *ns], timeout=120, env=env)
            return {}
        if args.get("protected") is not None:
            with _Repo(spec) as rp:
                rc, _o, err = _restic(rp, ["tag", "--add" if args["protected"] else "--remove",
                                           "protected", args["id"]], timeout=600)
                if rc != 0:
                    raise _BkError(err.strip()[-300:])
        return {}
    if op == "ls":
        if pbs:
            raise _BkError("File view for PBS backups is in the PBS web interface - "
                           "here whole archives can be restored")
        with _Repo(spec) as rp:
            rc, out, err = _restic(rp, ["ls", "--json", args["id"], args.get("path") or "/"], timeout=600)
            if rc != 0:
                raise _BkError(err.strip()[-300:])
            base = (args.get("path") or "/").rstrip("/")
            entries = []
            for line in out.splitlines():
                try:
                    n = json.loads(line)
                except ValueError:
                    continue
                if n.get("struct_type") == "node" and n.get("path", "").rstrip("/") != base:
                    entries.append({k: n.get(k) for k in ("name", "type", "path", "size", "mtime")})
            return {"entries": entries}
    if op.startswith("fs-snap-"):
        return _fs_snap(op[8:], args)
    raise _BkError(f"Unknown action {op}")


@sio.on("backup-cmd", namespace="/agent")
async def on_backup_cmd(data):
    data = data or {}
    rid = data.get("requestId")
    loop = asyncio.get_event_loop()

    def progress(line):
        if sio.connected:
            asyncio.run_coroutine_threadsafe(
                sio.emit("backup-progress", {"id": DEVICE_ID, "requestId": rid, "line": str(line)[:500]},
                         namespace="/agent"), loop)

    def work():
        try:
            return {"ok": True, **(_bk_dispatch(str(data.get("op") or ""), data.get("spec") or {},
                                                data.get("args") or {}, progress) or {})}
        except _BkError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    res = await loop.run_in_executor(None, work)
    await sio.emit("backup-result", {"id": DEVICE_ID, "requestId": rid, **res}, namespace="/agent")


# ------------------------------------------------------------------
# MAINTENANCE MODE (core): suspend protection mechanisms. The backend sends
# {"active", "until"}; when the time runs out, the agent also ends it itself.
# ------------------------------------------------------------------
_maint = {"active": False, "until": 0}


def _maintenance_active() -> bool:
    if not _maint["active"]:
        return False
    until = int(_maint.get("until") or 0)
    if until and until <= time.time() * 1000:
        _maint["active"] = False
        _print("[maintenance] maintenance mode expired")
        _usb_on_maintenance_change()
        return False
    return True


@sio.on("maintenance-mode", namespace="/agent")
async def on_maintenance_mode(data):
    data = data or {}
    was = _maint["active"]
    _maint["active"] = bool(data.get("active"))
    _maint["until"] = int(data.get("until") or 0)
    if was != _maint["active"]:
        _print(f"[maintenance] maintenance mode {'ACTIVE - protections suspended' if _maint['active'] else 'ended'}")
        await asyncio.get_event_loop().run_in_executor(None, _usb_on_maintenance_change)


# ------------------------------------------------------------------
# USB CONTROL (module "usbguard"): report plug/unplug and - in block mode -
# only pass new devices to the system after approval.
#   Linux:   /sys/bus/usb/devices/usb*/authorized_default = 0 -> new devices
#            stay unauthorized by the kernel (no drivers, no data) until
#            "authorized" = 1 is set. Hubs are released automatically
#            (otherwise devices behind them would be invisible).
#   Windows: new USB devices are disabled via pnputil right after detection
#            and re-enabled on approval (a short, technically unavoidable
#            detection gap). Hubs/host controllers stay active.
# Trusted devices are allowed automatically:
#   * by device ID (VID:PID, optionally serial number) - e.g. YubiKeys
#   * storage sticks via a SIGNED key file ".bytera-usbkey" in the root
#     directory. The file is signed with the backend key (Ed25519) and bound
#     to the stick's serial number - copying it to another stick is useless.
#     To check it, the stick is briefly mounted READ-ONLY (noexec); if the
#     file is invalid, it is locked again immediately.
# In maintenance mode nothing is blocked.
# ------------------------------------------------------------------
_usb = {"enabled": False, "block": False, "thread": None, "pending": set(),
        "trust": [], "pubkey": "", "revoked": set(), "loop": None}
_USB_SYS = "/sys/bus/usb/devices"
USB_KEYFILE = ".bytera-usbkey"


def _usb_read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except Exception:
        return ""


def _usb_write(path: str, value: str) -> bool:
    try:
        with open(path, "w") as fh:
            fh.write(value)
        return True
    except Exception:
        return False


def _usb_list_linux() -> dict:
    out = {}
    try:
        names = os.listdir(_USB_SYS)
    except Exception:
        return out
    for n in names:
        if ":" in n:                      # skip interfaces
            continue
        base = os.path.join(_USB_SYS, n)
        vid = _usb_read(os.path.join(base, "idVendor"))
        if not vid:
            continue
        pid = _usb_read(os.path.join(base, "idProduct"))
        label = " ".join(x for x in (_usb_read(os.path.join(base, "manufacturer")),
                                     _usb_read(os.path.join(base, "product"))) if x)
        storage = False
        try:
            for sub in os.listdir(base):
                if sub.startswith(n + ":") and _usb_read(os.path.join(base, sub, "bInterfaceClass")) == "08":
                    storage = True
        except Exception:
            pass
        out[n] = {"name": f"{label or 'USB device'} ({vid}:{pid})",
                  "vid": vid.lower(), "pid": pid.lower(),
                  "serial": _usb_read(os.path.join(base, "serial")),
                  "hub": _usb_read(os.path.join(base, "bDeviceClass")) == "09",
                  "storage": storage,
                  "root": n.startswith("usb"),
                  "authorized": _usb_read(os.path.join(base, "authorized")) != "0"}
    return out


def _usb_list_windows() -> dict:
    import subprocess as _sp
    ps = ("Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like 'USB\\*' } | "
          "Select-Object InstanceId,FriendlyName,Class,Status,Service | ConvertTo-Json -Compress")
    try:
        r = _sp.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                    capture_output=True, text=True, errors="replace", timeout=30,
                    creationflags=0x08000000)
        data = json.loads(r.stdout or "[]")
    except Exception:
        return {}
    if isinstance(data, dict):
        data = [data]
    out = {}
    for d in data or []:
        iid = str(d.get("InstanceId") or "")
        if not iid:
            continue
        name = str(d.get("FriendlyName") or "USB device")
        up = iid.upper()
        m = re.search(r"VID_([0-9A-F]{4})&PID_([0-9A-F]{4})", up)
        seg = iid.split("\\")[-1]
        # A segment with '&' is a Windows-generated identifier, not a real
        # serial number - then no binding to the stick is possible.
        serial = "" if "&" in seg else seg
        out[iid] = {"name": name,
                    "vid": (m.group(1).lower() if m else ""), "pid": (m.group(2).lower() if m else ""),
                    "serial": serial,
                    # Only let through real hubs/controllers. Note: the
                    # "USB mass storage device" also has the class "USB"!
                    "hub": "ROOT_HUB" in up or "HUB" in name.upper()
                           or "HOST CONTROLLER" in name.upper(),
                    "storage": str(d.get("Service") or "").upper() == "USBSTOR",
                    "root": False,
                    "authorized": str(d.get("Status") or "").upper() != "ERROR"}
    return out


def _usb_set_block_default(block: bool) -> None:
    if IS_WINDOWS:
        return
    try:
        for n in os.listdir(_USB_SYS):
            if n.startswith("usb"):
                _usb_write(os.path.join(_USB_SYS, n, "authorized_default"), "0" if block else "1")
    except Exception:
        pass


def _usb_apply(dev: str, allow: bool) -> None:
    import subprocess as _sp
    if IS_WINDOWS:
        verb = "/enable-device" if allow else "/disable-device"
        try:
            _sp.run(["pnputil", verb, dev], capture_output=True, timeout=60,
                    creationflags=0x08000000)
        except Exception:
            pass
    else:
        _usb_write(os.path.join(_USB_SYS, dev, "authorized"), "1" if allow else "0")


def _usb_emit(payload: dict) -> None:
    loop = _usb.get("loop")
    if sio.connected and loop:
        payload["id"] = DEVICE_ID
        asyncio.run_coroutine_threadsafe(sio.emit("usb-event", payload, namespace="/agent"), loop)


def _usb_blocking() -> bool:
    return _usb["enabled"] and _usb["block"] and not _maintenance_active()


def _usb_on_maintenance_change() -> None:
    """Maintenance on: release everything. Maintenance off: arm the lock again."""
    if not _usb["enabled"]:
        return
    _usb_set_block_default(_usb_blocking())
    if not _usb_blocking():
        for dev in list(_usb["pending"]):
            _usb_apply(dev, True)
            _usb["pending"].discard(dev)


def _usb_id_trusted(info: dict) -> bool:
    for t in _usb["trust"]:
        if str(t.get("vid") or "").lower() != info.get("vid"):
            continue
        if t.get("pid") and str(t["pid"]).lower() != info.get("pid"):
            continue
        if t.get("serial") and str(t["serial"]) != info.get("serial"):
            continue
        return True
    return False


def _usb_verify_keyfile(raw: bytes, info: dict) -> str | None:
    """Checks a key file. Returns: label (valid) or None."""
    try:
        import base64 as _b64
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        doc = json.loads(raw.decode("utf-8"))
        if doc.get("v") != 1 or not _usb["pubkey"]:
            return None
        kid = str(doc.get("kid") or "")
        if not kid or kid in _usb["revoked"]:
            return None
        vid, pid, serial = (str(doc.get(k) or "") for k in ("vid", "pid", "serial"))
        # Binding to THIS stick: serial number (and ID) must match.
        if not serial or serial != info.get("serial"):
            return None
        if vid.lower() != info.get("vid") or pid.lower() != info.get("pid"):
            return None
        msg = f"bytera-usbkey|v1|{kid}|{vid.lower()}|{pid.lower()}|{serial}|{doc.get('label','')}|{doc.get('iat',0)}"
        Ed25519PublicKey.from_public_bytes(_b64.b64decode(_usb["pubkey"])).verify(
            _b64.b64decode(doc.get("sig") or ""), msg.encode("utf-8"))
        return str(doc.get("label") or "key file")
    except Exception:
        return None


def _usb_read_keyfile_linux(dev: str) -> bytes | None:
    """Briefly authorize the stick, mount it read-only, read the key file."""
    import subprocess as _sp
    import tempfile as _tf
    _usb_write(os.path.join(_USB_SYS, dev, "authorized"), "1")
    devpath = os.path.realpath(os.path.join(_USB_SYS, dev))
    parts = []
    for _ in range(20):                       # wait up to 10 s for the block device
        time.sleep(0.5)
        try:
            for blk in os.listdir("/sys/block"):
                if not os.path.realpath(os.path.join("/sys/block", blk)).startswith(devpath):
                    continue
                subs = [p for p in os.listdir(os.path.join("/sys/block", blk)) if p.startswith(blk)]
                parts = ["/dev/" + p for p in subs] or ["/dev/" + blk]
        except Exception:
            pass
        if parts:
            break
    for part in parts:
        mnt = _tf.mkdtemp(prefix="bytera-usbchk-")
        try:
            r = _sp.run(["mount", "-o", "ro,noexec,nosuid,nodev", part, mnt],
                        capture_output=True, timeout=20)
            if r.returncode != 0:
                continue
            try:
                p = os.path.join(mnt, USB_KEYFILE)
                if os.path.isfile(p) and os.path.getsize(p) < 16384:
                    with open(p, "rb") as fh:
                        return fh.read()
            finally:
                _sp.run(["umount", mnt], capture_output=True, timeout=20)
        except Exception:
            pass
        finally:
            try:
                os.rmdir(mnt)
            except OSError:
                pass
    return None


def _win_removable_letters() -> set:
    try:
        import ctypes
        import string
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        out = set()
        for i, letter in enumerate(string.ascii_uppercase):
            if mask & (1 << i) and ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\") == 2:
                out.add(letter)
        return out
    except Exception:
        return set()


def _usb_read_keyfile_windows(before: set) -> bytes | None:
    for _ in range(20):                       # wait up to 10 s for the drive letter
        time.sleep(0.5)
        new = _win_removable_letters() - before
        for letter in sorted(new):
            p = f"{letter}:\\{USB_KEYFILE}"
            try:
                if os.path.isfile(p) and os.path.getsize(p) < 16384:
                    with open(p, "rb") as fh:
                        return fh.read()
            except Exception:
                pass
        if new:
            return None
    return None


def _usb_handle_new(dev: str, info: dict, letters_before: set) -> None:
    base = {"dev": dev, "name": info["name"], "vid": info.get("vid", ""),
            "pid": info.get("pid", ""), "serial": info.get("serial", ""),
            "storage": bool(info.get("storage"))}
    if not _usb_blocking():
        if _usb["enabled"] and _usb["block"]:
            _usb_apply(dev, True)              # maintenance mode: always allow
        _usb_emit({**base, "action": "add", "blocked": False,
                   "trusted": "maintenance" if _maintenance_active() else ""})
        return
    if info.get("hub"):
        _usb_apply(dev, True)                  # always let hubs through
        return
    if _usb_id_trusted(info):
        _usb_apply(dev, True)
        _usb_emit({**base, "action": "add", "blocked": False, "trusted": "id"})
        return
    if info.get("storage"):
        raw = (_usb_read_keyfile_windows(letters_before) if IS_WINDOWS
               else _usb_read_keyfile_linux(dev))
        label = _usb_verify_keyfile(raw, info) if raw else None
        if label:
            _usb_apply(dev, True)
            _usb_emit({**base, "action": "add", "blocked": False, "trusted": f"key:{label}"})
            return
    _usb_apply(dev, False)                     # block (Linux: revoke again)
    _usb["pending"].add(dev)
    _usb_emit({**base, "action": "add", "blocked": True, "trusted": ""})


def _usb_loop():
    lister = _usb_list_windows if IS_WINDOWS else _usb_list_linux
    known = lister()                           # do not report existing devices at startup
    letters = _win_removable_letters() if IS_WINDOWS else set()
    while _usb["enabled"]:
        time.sleep(3 if IS_WINDOWS else 1.5)
        try:
            _maintenance_active()              # detect expiry locally too
            cur = lister()
            for dev, info in cur.items():
                if dev in known or info.get("root"):
                    continue
                _usb_handle_new(dev, info, letters)
            for dev, info in known.items():
                if dev not in cur and not info.get("root"):
                    _usb["pending"].discard(dev)
                    _usb_emit({"action": "remove", "dev": dev, "name": info["name"],
                               "blocked": False})
            known = cur
            if IS_WINDOWS:
                letters = _win_removable_letters()
        except Exception:
            pass
    _usb["thread"] = None


@sio.on("usb-config", namespace="/agent")
async def on_usb_config(data):
    data = data or {}
    _usb["loop"] = asyncio.get_event_loop()
    _usb["enabled"] = bool(data.get("enabled"))
    _usb["block"] = bool(data.get("enabled")) and bool(data.get("block_new"))
    _usb["trust"] = list(data.get("trust") or [])
    _usb["pubkey"] = str(data.get("pubkey") or "")
    _usb["revoked"] = set(data.get("revoked") or [])
    _usb_set_block_default(_usb_blocking())
    t = _usb.get("thread")
    if _usb["enabled"] and (t is None or not t.is_alive()):
        t = threading.Thread(target=_usb_loop, daemon=True)
        _usb["thread"] = t
        t.start()
    _print(f"[usb] control {'active' if _usb['enabled'] else 'off'}"
           + (" (block mode)" if _usb["block"] else "")
           + (f", {len(_usb['trust'])} trusted device IDs" if _usb["trust"] else ""))


@sio.on("usb-decide", namespace="/agent")
async def on_usb_decide(data):
    data = data or {}
    dev = str(data.get("dev") or "")
    if not dev or dev not in _usb["pending"]:
        return
    allow = bool(data.get("allow"))
    await asyncio.get_event_loop().run_in_executor(None, _usb_apply, dev, allow)
    if allow:
        _usb["pending"].discard(dev)
    _print(f"[usb] {dev}: {'allowed' if allow else 'stays blocked'}")


# ------------------------------------------------------------------
# TEMPORARY USERS: create a local account with a random one-time password
# (Linux: useradd/chpasswd, Windows: net user). The agent monitors the expiry
# itself (persisted in tempusers.json, so it survives restarts):
#   '1h'/'2h'/'4h'/'24h' -> delete after expiry
#   'logoff'             -> delete once the user has no session after having
#                           logged in once ("at end of use")
#   'never'              -> never delete automatically
# ------------------------------------------------------------------
_TEMPUSER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tempusers.json")
_tempuser_lock = threading.Lock()
_tempuser_thread = {"t": None}


def _tempusers_load() -> dict:
    try:
        with open(_TEMPUSER_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _tempusers_save(data: dict) -> None:
    try:
        with open(_TEMPUSER_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except Exception:
        pass


def _tempuser_sessions(username: str) -> bool:
    """Does the user currently have a session? (who/quser, best effort)"""
    import subprocess as _sp
    try:
        if IS_WINDOWS:
            out = _sp.run("quser", shell=True, capture_output=True, text=True,
                          errors="replace", timeout=20).stdout or ""
        else:
            out = _sp.run(["who"], capture_output=True, text=True,
                          errors="replace", timeout=20).stdout or ""
        return any(line.split() and username.lower() in line.split()[0].lower()
                   for line in out.splitlines())
    except Exception:
        return False


def _tempuser_delete(username: str) -> bool:
    import subprocess as _sp
    try:
        if IS_WINDOWS:
            _sp.run(f'net user "{username}" /delete', shell=True,
                    capture_output=True, timeout=60)
        else:
            _sp.run(["pkill", "-KILL", "-u", username], capture_output=True, timeout=30)
            _sp.run(["userdel", "-r", username], capture_output=True, timeout=60)
        _print(f"[tempuser] user {username} deleted")
        return True
    except Exception as exc:
        _print(f"[tempuser] deleting {username} failed: {exc}")
        return False


def _tempuser_watch_loop():
    while True:
        time.sleep(60)
        try:
            with _tempuser_lock:
                data = _tempusers_load()
            changed = False
            now = time.time()
            for name, info in list(data.items()):
                mode = info.get("mode")
                drop = False
                if mode == "until" and now >= float(info.get("until") or 0):
                    drop = True
                elif mode == "logoff":
                    if _tempuser_sessions(name):
                        if not info.get("seen"):
                            info["seen"] = True
                            changed = True
                    elif info.get("seen"):
                        drop = True        # was logged in, now no longer
                if drop:
                    _tempuser_delete(name)
                    data.pop(name, None)
                    changed = True
            if changed:
                with _tempuser_lock:
                    _tempusers_save(data)
        except Exception:
            pass


def _tempuser_create(expiry: str) -> dict:
    import secrets as _sec
    import subprocess as _sp
    username = "temp_" + "".join(_sec.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(6))
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789!#+-"
    password = "".join(_sec.choice(alphabet) for _ in range(16))
    if IS_WINDOWS:
        r = _sp.run(f'net user "{username}" "{password}" /add /comment:"BYTERA temp user"',
                    shell=True, capture_output=True, text=True, errors="replace", timeout=60)
        if r.returncode != 0:
            return {"ok": False, "error": (r.stderr or r.stdout or "net user failed").strip()[:300]}
        _sp.run(f'net localgroup Users "{username}" /add', shell=True, capture_output=True, timeout=30)
        note = "Local Windows account (group: Users)."
    else:
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            return {"ok": False, "error": "agent is not running as root"}
        r = _sp.run(["useradd", "-m", "-s", "/bin/bash", "-c", "BYTERA temp user", username],
                    capture_output=True, text=True, errors="replace", timeout=60)
        if r.returncode != 0:
            return {"ok": False, "error": (r.stderr or "useradd failed").strip()[:300]}
        r = _sp.run(["chpasswd"], input=f"{username}:{password}", text=True,
                    capture_output=True, timeout=30)
        if r.returncode != 0:
            _sp.run(["userdel", "-r", username], capture_output=True, timeout=30)
            return {"ok": False, "error": "setting the password failed"}
        note = "Local Linux account with a home directory."
    hours = {"1h": 1, "2h": 2, "4h": 4, "24h": 24}.get(expiry)
    with _tempuser_lock:
        data = _tempusers_load()
        if hours:
            data[username] = {"mode": "until", "until": time.time() + hours * 3600}
            note += f" Will be deleted automatically in {hours} h."
        elif expiry == "logoff":
            data[username] = {"mode": "logoff", "seen": False}
            note += " Will be deleted after logoff (end of use)."
        else:
            note += " No automatic expiry."
        _tempusers_save(data)
    t = _tempuser_thread.get("t")
    if t is None or not t.is_alive():
        t = threading.Thread(target=_tempuser_watch_loop, daemon=True)
        _tempuser_thread["t"] = t
        t.start()
    _print(f"[tempuser] user {username} created (expiry: {expiry})")
    return {"ok": True, "username": username, "password": password, "note": note}


@sio.on("tempuser-create", namespace="/agent")
async def on_tempuser_create(data):
    data = data or {}
    loop = asyncio.get_event_loop()
    try:
        res = await loop.run_in_executor(None, _tempuser_create,
                                         str(data.get("expiry") or "1h"))
    except Exception as exc:
        res = {"ok": False, "error": str(exc)[:300]}
    res["requestId"] = data.get("requestId")
    res["id"] = DEVICE_ID
    await sio.emit("tempuser-result", res, namespace="/agent")


# ------------------------------------------------------------------
# BLACKBOX "last state": every X minutes (configured by the backend) report a
# small state snapshot: desktop screenshot (downscaled), metrics and the last
# log lines. If the device goes offline, the dashboard shows the last reported
# state. The backend stores metrics as DELTAS and discards the old screenshot
# as soon as a new one arrives.
# ------------------------------------------------------------------
_blackbox = {"enabled": False, "interval": 5, "thread": None, "last": 0.0}


def _blackbox_shot() -> str | None:
    """Small JPEG screenshot (base64) - None if no display is reachable."""
    try:
        import mss
        from PIL import Image
        import io as _io
        import base64 as _b64
        with mss.mss() as sct:
            mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            raw = sct.grab(mon)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        img.thumbnail((800, 800))
        buf = _io.BytesIO()
        img.save(buf, "JPEG", quality=40)
        return _b64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


def _blackbox_loop(loop):
    while True:
        time.sleep(10)
        try:
            if not _blackbox["enabled"] or not sio.connected:
                continue
            if time.time() - _blackbox["last"] < _blackbox["interval"] * 60:
                continue
            _blackbox["last"] = time.time()
            payload = {
                "id": DEVICE_ID,
                "metrics": collect_metrics(),
                "log": "\n".join(str(x) for x in list(_CONSOLE_BUFFER)[-120:]),
            }
            shot = _blackbox_shot()
            if shot:
                payload["shot"] = shot
            asyncio.run_coroutine_threadsafe(
                sio.emit("blackbox-snap", payload, namespace="/agent"), loop)
        except Exception:
            pass


@sio.on("blackbox-config", namespace="/agent")
async def on_blackbox_config(data):
    data = data or {}
    _blackbox["enabled"] = bool(data.get("enabled"))
    try:
        _blackbox["interval"] = max(1, min(int(data.get("interval_min") or 5), 240))
    except (TypeError, ValueError):
        _blackbox["interval"] = 5
    _blackbox["last"] = 0.0          # report a state promptly on a config change
    t = _blackbox.get("thread")
    if _blackbox["enabled"] and (t is None or not t.is_alive()):
        t = threading.Thread(target=_blackbox_loop,
                             args=(asyncio.get_event_loop(),), daemon=True)
        _blackbox["thread"] = t
        t.start()


@sio.on("software-install", namespace="/agent")
async def on_software_install(data):
    """Software rollout (module "software"): download the installation files
    from the backend and run the install command. Placeholders in the command:
    {dir} = download folder, {file} = first file (full path)."""
    data = data or {}
    request_id = data.get("requestId")
    loop = asyncio.get_event_loop()

    def _work():
        import shutil as _sh
        import subprocess as _sp
        import tempfile as _tf
        out, code = "", -1
        tdir = _tf.mkdtemp(prefix="bytera-sw-")
        try:
            first = ""
            for f in data.get("files") or []:
                name = os.path.basename(str(f.get("name") or "file"))
                blob = _backend_fetch(str(f.get("path") or ""), timeout=600,
                                      limit=2 * 1024 * 1024 * 1024)
                p = os.path.join(tdir, name)
                with open(p, "wb") as fh:
                    fh.write(blob)
                if not first:
                    first = p
            cmd = str(data.get("command") or "").replace("{dir}", tdir).replace("{file}", first)
            if not cmd and first:
                # No command: silently install known formats directly.
                low = first.lower()
                if low.endswith(".msi"):
                    cmd = f'msiexec /i "{first}" /qn /norestart'
                elif low.endswith(".exe"):
                    cmd = f'"{first}" /S'
                elif low.endswith(".deb"):
                    cmd = f'apt-get install -y "{first}" || dpkg -i "{first}"'
                elif low.endswith(".rpm"):
                    cmd = f'rpm -i "{first}"'
            if not cmd:
                return "No install command specified", 2
            res = _sp.run(cmd, shell=True, cwd=tdir, capture_output=True,
                          text=True, errors="replace", timeout=1800)
            out = ((res.stdout or "") + (res.stderr or ""))[-6000:]
            code = res.returncode
            return out, code
        except Exception as exc:
            return f"{out}\n{exc}", code if code >= 0 else 1
        finally:
            _sh.rmtree(tdir, ignore_errors=True)

    try:
        out, code = await loop.run_in_executor(None, _work)
    except Exception as exc:
        out, code = str(exc), 1
    await sio.emit("software-install-result", {
        "id": DEVICE_ID, "requestId": request_id, "code": code, "output": out,
    }, namespace="/agent")


@sio.on("migrate-server", namespace="/agent")
async def on_migrate_server(data):
    """Backend migration: the (old) backend announces the NEW server address.
    The agent writes it to its .env and restarts - from then on it connects to
    the new backend. Since the backend's data/.env is migrated along, the agent
    token and E2E/TLS pinning stay valid."""
    from urllib.parse import urlparse
    url = str((data or {}).get("url") or "").strip().rstrip("/")
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        _print(f"[migrate] received an invalid new server address: {url!r}")
        return
    if url == BACKEND_URL.rstrip("/"):
        return  # already set correctly
    _print(f"[migrate] new backend address: {url} - applying and restarting")
    try:
        _env_store({"BACKEND_URL": url})
    except Exception as e:
        _print(f"[migrate] could not write .env: {e}")
        return

    async def _restart_soon():
        await asyncio.sleep(1.5)   # let the ack/logs flush out first
        try:
            await sio.disconnect()
        except Exception:
            pass
        os.execv(sys.executable, [sys.executable] + sys.argv)

    asyncio.get_event_loop().create_task(_restart_soon())


@sio.on("term-open", namespace="/agent")
async def on_term_open(data):
    """Starts a real interactive shell session."""
    session_id = data.get("session")
    shell = data.get("shell", "auto")
    mode = data.get("mode") if data.get("mode") in ("admin", "user") else "admin"
    cols = int(data.get("cols", 80))
    rows = int(data.get("rows", 24))
    if not session_id:
        return

    if IS_WINDOWS and mode == "user":
        # Without our own SSH server there is no clean console login on
        # Windows (runas & co. open their own windows on the desktop).
        await sio.emit("term-output", {
            "id": DEVICE_ID, "session": session_id,
            "data": "\x1b[33m[User login is not available on Windows - "
                    "starting a normal shell.]\x1b[0m\r\n",
        }, namespace="/agent")
        mode = "admin"

    # IMMEDIATE marker: proves that THIS (new) agent code is running. If the
    # user does not see this line, the client is still running the OLD agent.py
    # (process not restarted after the update).
    what = "login (user sign-in)" if mode == "user" else f"{shell} shell"
    await sio.emit("term-output", {
        "id": DEVICE_ID, "session": session_id,
        "data": f"\x1b[90m[Agent starting {what}…]\x1b[0m\r\n",
    }, namespace="/agent")

    if mode == "user" and not IS_WINDOWS:
        try:
            if os.geteuid() != 0:
                await sio.emit("term-output", {
                    "id": DEVICE_ID, "session": session_id,
                    "data": "\x1b[33m[Note: the agent is not running as root - "
                            "the sign-in may fail.]\x1b[0m\r\n",
                }, namespace="/agent")
        except Exception:
            pass

    # Already open? Close it first (restart).
    old = _terminals.pop(session_id, None)
    if old:
        try:
            old.close()
        except Exception:
            pass

    loop = asyncio.get_event_loop()
    try:
        if IS_WINDOWS:
            term = _WinTerminal(session_id, shell, cols, rows, loop)
        else:
            term = _PosixTerminal(session_id, shell, cols, rows, loop, mode)
        _terminals[session_id] = term
    except ImportError as e:
        # pywinpty is missing or broken. First try to repair it ourselves -
        # that covers the most common case (leftovers of an old version or the
        # colliding package 'winpty'). If that fails, the shell keeps running
        # in the limited pipe mode instead of not at all.
        diag = _winpty_diagnose()
        _print(f"[term] pywinpty import failed: {e}\n{diag}")
        term = None
        if _winpty_repair():
            try:
                term = _WinTerminal(session_id, shell, cols, rows, loop)
                await sio.emit("term-output", {
                    "id": DEVICE_ID, "session": session_id,
                    "data": "\x1b[32m[pywinpty was repaired.]\x1b[0m\r\n",
                }, namespace="/agent")
            except Exception as e2:
                _print(f"[term] still no ConPTY after repair: {e2}")
                term = None
        if term is None:
            await sio.emit("term-output", {
                "id": DEVICE_ID, "session": session_id,
                "data": "\r\n\x1b[33mNo ConPTY available "
                        f"({e}).\x1b[0m\r\n"
                        + "".join(f"\x1b[90m{l}\x1b[0m\r\n" for l in diag.split("\n"))
                        + "\x1b[90mNote: a 'pip install pywinpty' usually "
                          "is not enough - a leftover is on the search path. Manually: "
                          "pip uninstall -y winpty pywinpty && pip install pywinpty"
                          "\x1b[0m\r\n",
            }, namespace="/agent")
            try:
                term = _WinPipeTerminal(session_id, shell, cols, rows, loop)
            except Exception as e3:
                await sio.emit("term-output", {
                    "id": DEVICE_ID, "session": session_id,
                    "data": f"\r\n\x1b[31mThe fallback mode also failed: {e3}\x1b[0m\r\n",
                }, namespace="/agent")
                await sio.emit("term-exit", {"id": DEVICE_ID, "session": session_id},
                               namespace="/agent")
                return
        _terminals[session_id] = term
    except Exception as e:
        import traceback
        _print(f"[term] shell start failed: {e}\n{traceback.format_exc()}")
        await sio.emit("term-output", {
            "id": DEVICE_ID, "session": session_id,
            "data": f"\r\n\x1b[31mTerminal error: {e}\x1b[0m\r\n",
        }, namespace="/agent")
        await sio.emit("term-exit", {"id": DEVICE_ID, "session": session_id}, namespace="/agent")


@sio.on("term-input", namespace="/agent")
async def on_term_input(data):
    term = _terminals.get(data.get("session"))
    if term:
        term.write(data.get("data", ""))


@sio.on("term-resize", namespace="/agent")
async def on_term_resize(data):
    term = _terminals.get(data.get("session"))
    if term:
        term.resize(int(data.get("cols", 80)), int(data.get("rows", 24)))


@sio.on("term-close", namespace="/agent")
async def on_term_close(data):
    term = _terminals.pop(data.get("session"), None)
    if term:
        term.close()


# --------------------------------------------------------------
# Agent console: the dashboard reads along this agent's log
# (history from the rolling buffer + live lines).
# --------------------------------------------------------------

@sio.on("agent-console-open", namespace="/agent")
async def on_agent_console_open(data=None):
    _console_stream["on"] = True
    history = "\r\n".join(_CONSOLE_BUFFER)
    if history:
        history += "\r\n"
    await sio.emit("agent-console-history",
                   {"id": DEVICE_ID, "data": history}, namespace="/agent")


@sio.on("agent-console-close", namespace="/agent")
async def on_agent_console_close(data=None):
    _console_stream["on"] = False


# --------------------------------------------------------------
# Remote commands: list the file system (File Station in the dashboard)
# --------------------------------------------------------------

def _list_windows_drives() -> list[dict]:
    """
    Determines ALL drives on Windows (local disks, USB, CD, and mapped network
    drives) via psutil - more reliable than the outdated 'wmic', which often no
    longer exists on newer Windows versions.
    """
    drives = []
    try:
        # all=True also includes network drives and removable media
        for part in psutil.disk_partitions(all=True):
            mount = part.mountpoint
            if not mount:
                continue
            # type marker for a nice label (local vs. network drive)
            is_network = "remote" in (part.opts or "").lower() or part.fstype == ""
            try:
                usage = psutil.disk_usage(mount)
                size = usage.total
            except Exception:
                size = 0
            label = mount.rstrip("\\")
            if is_network:
                label += "  (network drive)"
            drives.append({
                "name": label,
                "path": mount if mount.endswith("\\") else mount + "\\",
                "isDir": True,
                "size": size,
                "mtime": 0,
            })
    except Exception:
        pass

    return drives or [{"name": "C:", "path": "C:\\", "isDir": True, "size": 0, "mtime": 0}]


def _perm_string(mode: int, is_dir: bool, is_link: bool = False) -> str:
    """Builds an 'ls -al'-style permission string, e.g. 'drwxr-xr-x'."""
    import stat as _stat
    if is_link:
        type_char = "l"
    elif is_dir:
        type_char = "d"
    elif _stat.S_ISCHR(mode):
        type_char = "c"
    elif _stat.S_ISBLK(mode):
        type_char = "b"
    elif _stat.S_ISFIFO(mode):
        type_char = "p"
    elif _stat.S_ISSOCK(mode):
        type_char = "s"
    else:
        type_char = "-"
    perms = ""
    for who, r, w, x in (
        ("USR", _stat.S_IRUSR, _stat.S_IWUSR, _stat.S_IXUSR),
        ("GRP", _stat.S_IRGRP, _stat.S_IWGRP, _stat.S_IXGRP),
        ("OTH", _stat.S_IROTH, _stat.S_IWOTH, _stat.S_IXOTH),
    ):
        perms += "r" if mode & r else "-"
        perms += "w" if mode & w else "-"
        perms += "x" if mode & x else "-"
    return type_char + perms


def _owner_group(stat_res) -> tuple[str, str]:
    """Determines owner and group names (Linux/Mac). On Windows or with
    missing modules, the numeric UID/GID is returned."""
    uid = getattr(stat_res, "st_uid", 0)
    gid = getattr(stat_res, "st_gid", 0)
    owner, group = str(uid), str(gid)
    if not IS_WINDOWS:
        try:
            import pwd
            owner = pwd.getpwuid(uid).pw_name
        except Exception:
            pass
        try:
            import grp
            group = grp.getgrgid(gid).gr_name
        except Exception:
            pass
    return owner, group


def _entry_meta(full_path: str, is_dir: bool) -> dict:
    """Collects size, mtime, permissions, owner, group and octal mode of a
    file/folder - as 'ls -al' would show it."""
    import stat as _stat
    meta = {"size": 0, "mtime": 0, "perms": "", "owner": "", "group": "",
            "mode": "", "is_link": False}
    try:
        is_link = os.path.islink(full_path)
        st = os.lstat(full_path) if is_link else os.stat(full_path)
        meta["size"] = st.st_size
        meta["mtime"] = int(st.st_mtime * 1000)
        meta["is_link"] = is_link
        meta["perms"] = _perm_string(st.st_mode, is_dir, is_link)
        meta["mode"] = oct(_stat.S_IMODE(st.st_mode))[-3:]
        owner, group = _owner_group(st)
        meta["owner"], meta["group"] = owner, group
    except Exception:
        pass
    return meta


def _list_directory(path: str) -> list[dict]:
    """Lists the contents of a folder. For an empty path: show drives or root/home."""
    if not path:
        if IS_WINDOWS:
            return _list_windows_drives()
        # Linux/Mac: show root, home and all mounted partitions
        home = str(Path.home())
        entries = [
            {"name": "/ (root)", "path": "/", "isDir": True, "size": 0, "mtime": 0},
            {"name": f"Home ({home})", "path": home, "isDir": True, "size": 0, "mtime": 0},
        ]
        seen = {"/", home}
        try:
            for part in psutil.disk_partitions(all=False):
                mp = part.mountpoint
                if mp and mp not in seen:
                    seen.add(mp)
                    try:
                        size = psutil.disk_usage(mp).total
                    except Exception:
                        size = 0
                    entries.append({
                        "name": f"{mp} ({part.device})",
                        "path": mp, "isDir": True, "size": size, "mtime": 0,
                    })
        except Exception:
            pass
        return entries

    entries = []
    with os.scandir(path) as it:
        for entry in it:
            try:
                is_dir = entry.is_dir()
            except Exception:
                is_dir = False
            meta = _entry_meta(entry.path, is_dir)
            entries.append({
                "name": entry.name,
                "path": os.path.join(path, entry.name),
                "isDir": is_dir,
                **meta,
            })
    entries.sort(key=lambda e: (not e["isDir"], e["name"].lower()))
    return entries


@sio.on("fs-list", namespace="/agent")
async def on_fs_list(data):
    """Called when the backend wants to see the contents of a folder."""
    request_id = data.get("requestId")
    req_path = data.get("path", "")

    loop = asyncio.get_event_loop()
    try:
        entries = await loop.run_in_executor(None, _listdir_admin, req_path)
        await sio.emit("fs-result", {"requestId": request_id, "entries": entries}, namespace="/agent")
    except Exception as e:
        await sio.emit("fs-result", {"requestId": request_id, "error": str(e)}, namespace="/agent")


def _read_file_b64(path: str, max_bytes: int = 25 * 1024 * 1024) -> dict:
    """Reads a file and returns it base64-encoded (for the download)."""
    size = os.path.getsize(path)
    if size > max_bytes:
        raise ValueError(f"File too large ({size} bytes, max. {max_bytes})")
    with open(path, "rb") as f:
        content = f.read()
    return {
        "name": os.path.basename(path),
        "data": base64.b64encode(content).decode("ascii"),
    }


@sio.on("fs-read", namespace="/agent")
async def on_fs_read(data):
    """Reads a single file for the download in the dashboard."""
    request_id = data.get("requestId")
    path = data.get("path", "")
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, _read_file_admin, path)
        await sio.emit("fs-read-result", {"requestId": request_id, **result}, namespace="/agent")
    except Exception as e:
        await sio.emit("fs-read-result", {"requestId": request_id, "error": str(e)}, namespace="/agent")


# --------------------------------------------------------------
# Writing file operations (upload, folder, delete, rename, edit)
# --------------------------------------------------------------

def _write_file_b64(path: str, data_b64: str) -> dict:
    """Writes an (uploaded or edited) file to the target path."""
    content = base64.b64decode(data_b64)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)
    return {"ok": True, "path": path, "size": len(content)}


def _make_dir(path: str) -> dict:
    os.makedirs(path, exist_ok=False)
    return {"ok": True, "path": path}


def _delete_path(path: str) -> dict:
    """Deletes a file or a (possibly non-empty) folder."""
    import shutil
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    else:
        os.remove(path)
    return {"ok": True, "path": path}


def _rename_path(src: str, dst: str) -> dict:
    os.rename(src, dst)
    return {"ok": True, "src": src, "dst": dst}


# --------------------------------------------------------------
# File operations with ADMIN RIGHTS (web explorer + explorer relay)
# --------------------------------------------------------------
# If the agent does NOT already run as SYSTEM/root, file access to protected
# paths fails with "access denied" (and the relay client shows errors or drops
# the connection). So for EVERYTHING that goes through the web explorer or the
# explorer relay: first the normal (fast) attempt - if it fails with
# access-denied, the operation is automatically retried ELEVATED:
#   Windows: Start-Process -Verb RunAs (if the agent runs as service/SYSTEM,
#            this is transparent without a UAC dialog)
#   Linux:   sudo -n (requires a NOPASSWD rule for the agent user; if the agent
#            runs as root, the direct attempt already works)
# This way the operations are effectively ALWAYS run with admin rights and
# "editing a file without permission" no longer crashes the relay.

def _is_access_denied(exc: Exception) -> bool:
    """Detects 'access denied' errors across platforms."""
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError):
        if getattr(exc, "winerror", None) == 5:          # ERROR_ACCESS_DENIED
            return True
        import errno as _errno
        return exc.errno in (_errno.EACCES, _errno.EPERM)
    return False


# Standalone mini script that runs the file operation in the ELEVATED
# process. Exchange via two JSON files (request/response) so that no
# command-line quoting problems arise.
_FS_ELEV_SOURCE = r'''
import base64, json, os, shutil, sys
with open(sys.argv[1], "r", encoding="utf-8") as f:
    req = json.load(f)
op = req.get("op"); res = {}
try:
    if op == "list":
        out = []
        base = req["path"]
        for name in sorted(os.listdir(base)):
            full = os.path.join(base, name)
            try:
                st = os.stat(full, follow_symlinks=False)
                is_dir = os.path.isdir(full)
                out.append({"name": name, "path": full, "isDir": is_dir,
                            "size": 0 if is_dir else int(st.st_size),
                            "mtime": int(st.st_mtime * 1000)})
            except Exception:
                out.append({"name": name, "path": full, "isDir": False,
                            "size": 0, "mtime": 0})
        res = {"entries": out}
    elif op == "read":
        with open(req["path"], "rb") as f:
            data = f.read()
        res = {"name": os.path.basename(req["path"]),
               "data": base64.b64encode(data).decode("ascii")}
    elif op == "write":
        content = base64.b64decode(req.get("data", ""))
        os.makedirs(os.path.dirname(req["path"]) or ".", exist_ok=True)
        with open(req["path"], "wb") as f:
            f.write(content)
        res = {"ok": True, "path": req["path"], "size": len(content)}
    elif op == "mkdir":
        os.makedirs(req["path"], exist_ok=False)
        res = {"ok": True, "path": req["path"]}
    elif op == "delete":
        p = req["path"]
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
        else:
            os.remove(p)
        res = {"ok": True, "path": p}
    elif op == "rename":
        os.rename(req["src"], req["dst"])
        res = {"ok": True, "src": req["src"], "dst": req["dst"]}
    else:
        res = {"error": "Unknown operation: %s" % op}
except Exception as e:
    res = {"error": str(e)}
with open(sys.argv[2], "w", encoding="utf-8") as f:
    json.dump(res, f)
'''


def _fs_elevated(op: dict) -> dict:
    """Runs a file operation ELEVATED (admin/root). Blocking - called in the
    executor like the direct variants."""
    import json as _json_mod
    import shutil as _shutil
    tmpdir = tempfile.mkdtemp(prefix="rmm-fsadmin-")
    script = os.path.join(tmpdir, "fsop.py")
    req_f = os.path.join(tmpdir, "req.json")
    res_f = os.path.join(tmpdir, "res.json")
    try:
        with open(script, "w", encoding="utf-8") as f:
            f.write(_FS_ELEV_SOURCE)
        with open(req_f, "w", encoding="utf-8") as f:
            _json_mod.dump(op, f)

        if IS_WINDOWS:
            # Same pattern as _run_elevated_windows: Start-Process -Verb RunAs.
            inner_exec = f'"{sys.executable}" "{script}" "{req_f}" "{res_f}"'
            launch_script = (
                f"$p = Start-Process -FilePath cmd.exe -ArgumentList '/c',{_ps_single_quote(inner_exec)} "
                f"-Verb RunAs -WindowStyle Hidden -PassThru -Wait; exit $p.ExitCode"
            )
            encoded = base64.b64encode(launch_script.encode("utf-16-le")).decode("ascii")
            _run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                  "-EncodedCommand", encoded],
                 capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        else:
            # Linux/macOS: sudo without a password prompt (-n). Without a matching
            # sudoers rule this fails in a controlled way (no hang).
            _run(["sudo", "-n", sys.executable, script, req_f, res_f],
                 capture_output=True, timeout=120)

        if not os.path.exists(res_f):
            raise PermissionError(
                "Access denied - running with admin rights is not possible "
                "(UAC declined or sudo without a NOPASSWD rule).")
        with open(res_f, "r", encoding="utf-8") as f:
            res = _json_mod.load(f)
        if res.get("error"):
            raise PermissionError(res["error"])
        return res
    finally:
        _shutil.rmtree(tmpdir, ignore_errors=True)


# ------------------------------------------------------------------
# Elevation throttle: explorer-relay browsing can trigger DOZENS of
# "access denied" cases per folder (folder.jpg, Thumbs.db, system
# junctions, ...). Without a throttle, a RunAs PowerShell process would be
# started for each one (up to 120s timeout) - which destabilized the agent
# in the past (hard process death 0x80000003).
#   - _ELEV_LOCK:     never more than ONE elevation at a time
#   - _elev_failed:   10min block per path after a failed elevation
#   - _elev_last_fail: 60s global pause after ANY failure
#   - system junctions (Documents and Settings, $Recycle.Bin, ...) are not
#     elevated at all on "list" - elevation does not help there anyway.
# ------------------------------------------------------------------
_ELEV_LOCK = threading.Lock()
_elev_failed: dict[str, float] = {}
_elev_last_fail = 0.0
_ELEV_PATH_COOLDOWN = 600.0   # seconds per path
_ELEV_GLOBAL_COOLDOWN = 60.0  # seconds after any failure
_WIN_SYSTEM_JUNCTIONS = {
    "documents and settings", "$recycle.bin", "system volume information",
    "recovery", "config.msi", "dfsrprivate", "perflogs",
}


def _elev_key(op: dict) -> str:
    return f"{op.get('op')}::{op.get('path') or op.get('src') or ''}".lower()


def _fs_with_admin(direct, op: dict):
    """Direct attempt; on 'access denied' automatically retried elevated.
    With a throttle, so explorer-relay browsing does not cause an elevation storm."""
    global _elev_last_fail
    try:
        return direct()
    except Exception as e:
        if not _is_access_denied(e):
            raise
        # Known system junctions: elevation does nothing when listing.
        if op.get("op") == "list":
            base = os.path.basename((op.get("path") or "").rstrip("\\/")).lower()
            if base in _WIN_SYSTEM_JUNCTIONS:
                raise
        now = time.time()
        key = _elev_key(op)
        ts = _elev_failed.get(key)
        if ts and now - ts < _ELEV_PATH_COOLDOWN:
            raise  # same path failed only recently
        if now - _elev_last_fail < _ELEV_GLOBAL_COOLDOWN:
            raise  # global grace period after the last failure
        _print(f"[agent] file operation '{op.get('op')}' without permission "
               f"({e}) -> retrying with admin rights")
        with _ELEV_LOCK:
            try:
                res = _fs_elevated(op)
                _elev_failed.pop(key, None)
                return res
            except Exception:
                _elev_failed[key] = time.time()
                _elev_last_fail = time.time()
                raise


def _listdir_admin(path: str):
    # Empty path = drive/root overview, never needs elevation.
    if not path:
        return _list_directory(path)
    res = _fs_with_admin(lambda: _list_directory(path), {"op": "list", "path": path})
    return res["entries"] if isinstance(res, dict) and "entries" in res else res


def _read_file_admin(path: str) -> dict:
    return _fs_with_admin(lambda: _read_file_b64(path), {"op": "read", "path": path})


def _write_file_admin(path: str, data_b64: str) -> dict:
    return _fs_with_admin(lambda: _write_file_b64(path, data_b64),
                          {"op": "write", "path": path, "data": data_b64})


def _mkdir_admin(path: str) -> dict:
    return _fs_with_admin(lambda: _make_dir(path), {"op": "mkdir", "path": path})


def _delete_admin(path: str) -> dict:
    return _fs_with_admin(lambda: _delete_path(path), {"op": "delete", "path": path})


def _rename_admin(src: str, dst: str) -> dict:
    return _fs_with_admin(lambda: _rename_path(src, dst),
                          {"op": "rename", "src": src, "dst": dst})


@sio.on("fs-write", namespace="/agent")
async def on_fs_write(data):
    """Upload a file or write back an edited file."""
    request_id = data.get("requestId")
    path = data.get("path", "")
    payload = data.get("data", "")
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, _write_file_admin, path, payload)
        await sio.emit("fs-op-result", {"requestId": request_id, **result}, namespace="/agent")
    except Exception as e:
        await sio.emit("fs-op-result", {"requestId": request_id, "error": str(e)}, namespace="/agent")


@sio.on("fs-mkdir", namespace="/agent")
async def on_fs_mkdir(data):
    request_id = data.get("requestId")
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, _mkdir_admin, data.get("path", ""))
        await sio.emit("fs-op-result", {"requestId": request_id, **result}, namespace="/agent")
    except Exception as e:
        await sio.emit("fs-op-result", {"requestId": request_id, "error": str(e)}, namespace="/agent")


@sio.on("fs-delete", namespace="/agent")
async def on_fs_delete(data):
    request_id = data.get("requestId")
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, _delete_admin, data.get("path", ""))
        await sio.emit("fs-op-result", {"requestId": request_id, **result}, namespace="/agent")
    except Exception as e:
        await sio.emit("fs-op-result", {"requestId": request_id, "error": str(e)}, namespace="/agent")


@sio.on("fs-rename", namespace="/agent")
async def on_fs_rename(data):
    request_id = data.get("requestId")
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, _rename_admin,
                                             data.get("src", ""), data.get("dst", ""))
        await sio.emit("fs-op-result", {"requestId": request_id, **result}, namespace="/agent")
    except Exception as e:
        await sio.emit("fs-op-result", {"requestId": request_id, "error": str(e)}, namespace="/agent")


# --------------------------------------------------------------
# Task manager: structured process list + terminate a process
# --------------------------------------------------------------

def _collect_processes() -> list[dict]:
    """Collects the running processes with CPU/RAM usage via psutil."""
    procs = []
    for p in psutil.process_iter(["pid", "name", "username", "memory_percent"]):
        try:
            info = p.info
            procs.append({
                "pid": info["pid"],
                "name": info["name"] or "?",
                "username": info.get("username") or "",
                "cpu": p.cpu_percent(interval=0),   # since the last call
                "mem": round(info.get("memory_percent") or 0, 1),
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    # Sort by memory usage, largest first
    procs.sort(key=lambda x: x["mem"], reverse=True)
    return procs[:60]  # limit to the top 60


@sio.on("proc-list", namespace="/agent")
async def on_proc_list(data):
    """Returns the process list to the task manager in the dashboard."""
    request_id = data.get("requestId")
    loop = asyncio.get_event_loop()
    try:
        processes = await loop.run_in_executor(None, _collect_processes)
        await sio.emit("proc-result", {"requestId": request_id, "processes": processes}, namespace="/agent")
    except Exception as e:
        await sio.emit("proc-result", {"requestId": request_id, "error": str(e)}, namespace="/agent")


@sio.on("proc-kill", namespace="/agent")
async def on_proc_kill(data):
    """Terminates a process by its PID."""
    request_id = data.get("requestId")
    pid = data.get("pid")
    try:
        psutil.Process(int(pid)).terminate()
        await sio.emit("proc-kill-result", {"requestId": request_id, "ok": True}, namespace="/agent")
    except Exception as e:
        await sio.emit("proc-kill-result", {"requestId": request_id, "ok": False, "error": str(e)}, namespace="/agent")


# --------------------------------------------------------------
# Remote Screen: Bildschirm streamen + Maus/Tastatur fernsteuern
# --------------------------------------------------------------
# Ablauf:
#   - Dashboard schickt "screen-start" -> wir starten einen Hintergrund-Thread,
#     der laufend Screenshots macht, verkleinert, als JPEG/Base64 kodiert und
#     einzeln als "screen-frame" ans Backend schickt (das leitet sie ans
#     Dashboard weiter).
#   - Dashboard schickt "screen-stop" -> Thread beenden.
#   - Dashboard schickt "screen-input" (Maus-Bewegung/Klick/Tastatur) -> wir
#     simulieren die Eingabe lokal über pynput.
#
# Hinweis zur Latenz: Wir nutzen hier bewusst simples JPEG-Streaming über
# Socket.IO (leicht verständlich, überall lauffähig). Für noch flüssigeres Bild
# könnte man später auf WebRTC umstellen - die Dashboard-Seite müsste dann nur
# die Frames anders empfangen, der Rest bliebe gleich.

_screen_stream = {"active": False, "thread": None, "sid_loop": None,
                  "quality": 55, "fps": 10,
                  "monitor": 1,          # gewählter Bildschirm (1 = primär)
                  "mon_left": 0, "mon_top": 0,   # Offset des gewählten Bildschirms
                  # Verbindung zum Aufnahme-Helfer in der Benutzersitzung
                  # (nur Windows/Sitzung 0, sonst immer None).
                  "helper": None,
                  # Zaehler laufender Sitzungswuensche. Trifft waehrend einer
                  # offenen Zustimmungsabfrage ein "screen-stop" ein (Dashboard-
                  # Fenster geschlossen), wird der Zaehler erhoeht - die
                  # Abfrage weiss dann, dass niemand mehr zusieht, und startet
                  # KEINEN Stream mehr. Vorher lief der Stream sonst blind los.
                  "epoch": 0,
                  # Erzwingt das naechste Bild, auch wenn sich nichts geaendert
                  # hat (Dashboard verbindet neu / Wachhund im Fenster).
                  "force": False}


# --------------------------------------------------------------------------
# FLUSSKONTROLLE FUER DIE BILDER (Ursache "Remote Screen bricht ab")
# --------------------------------------------------------------------------
# Frueher wurde jedes Bild ohne Rueckmeldung losgeschickt. War der Upload des
# Geraets langsamer als die erzeugten Bilder, wuchs die Sendewarteschlange von
# Socket.IO immer weiter - die Ping/Pong-Pakete standen HINTER den Bildern,
# und nach ~60 s warf der Server den Agenten raus. Ein Keep-alive aendert
# daran nichts, die Leitung war ja nicht still, sondern verstopft.
#
# Jetzt: Jedes Bild wird mit Rueckmeldung (Socket.IO-ACK) gesendet, hoechstens
# WINDOW Bilder sind gleichzeitig unterwegs. Ist die Leitung voll, wird nicht
# gestaut, sondern nur das NEUESTE Bild gemerkt ("latest frame wins") und
# gesendet, sobald Platz ist. Die gemessene Laufzeit steuert zusaetzlich eine
# Qualitaetsstufe (0 = wie eingestellt, 3 = stark reduziert).
class _FrameFlow:
    WINDOW = 2
    ACK_TIMEOUT = 5.0          # ohne ACK so lange -> Slots freigeben (ACK verloren)

    def __init__(self):
        self.lock = threading.Lock()
        self.sent_ts = _collections.deque()   # Sendezeiten der offenen Bilder
        self.pending = None
        self.rtt = 0.3
        self.level = 0
        self._lvl_changed = 0.0
        self.frames = 0

    def reset(self):
        with self.lock:
            self.sent_ts.clear()
            self.pending = None
            self.rtt = 0.3
            self.level = 0
            self.frames = 0

    def congested(self) -> bool:
        with self.lock:
            # WICHTIG: Auch HIER verlorene Rueckmeldungen aufraeumen - nicht nur
            # in offer(). Vorher galt: Gingen beide ACKs verloren (typisch beim
            # kurzen Reconnect - Socket.IO wirft offene Callbacks der alten
            # Verbindung weg), blieb sent_ts fuer immer voll, die Capture-
            # Schleife drehte nur noch in congested()->sleep und offer() wurde
            # NIE mehr aufgerufen. Folge: Der Remote Screen fror nach einiger
            # Zeit dauerhaft ein ("geht nach ~50 Frames aus").
            now = time.time()
            if self.sent_ts and now - self.sent_ts[0] > self.ACK_TIMEOUT:
                self.sent_ts.clear()
                self._adapt(now, slow=True)
            return len(self.sent_ts) >= self.WINDOW

    def _adapt(self, now: float, slow: bool = False):
        # Hoechstens alle 3 s eine Stufe nach unten (schlechter), alle 6 s
        # eine Stufe nach oben - sonst pendelt die Qualitaet.
        if (slow or self.rtt > 1.2) and self.level < 3 and now - self._lvl_changed > 3.0:
            self.level += 1
            self._lvl_changed = now
            _print(f"[screen] Leitung langsam (Laufzeit {self.rtt:.2f}s) -> Qualitaetsstufe {self.level}")
        elif self.rtt < 0.35 and self.level > 0 and now - self._lvl_changed > 6.0:
            self.level -= 1
            self._lvl_changed = now

    def quality(self, configured: int) -> int:
        return max(20, int(configured) - 15 * self.level)

    def max_width(self, configured_w: int) -> int:
        caps = {0: 0, 1: 0, 2: 1280, 3: 1024}
        cap = caps.get(self.level, 0)
        if not cap:
            return configured_w
        return min(configured_w, cap) if configured_w else cap

    def offer(self, payload: dict) -> bool:
        """Bild abgeben. True = sofort gesendet, False = als 'neuestes' gemerkt."""
        with self.lock:
            now = time.time()
            if self.sent_ts and now - self.sent_ts[0] > self.ACK_TIMEOUT:
                self.sent_ts.clear()             # Rueckmeldungen verloren -> freigeben
                self._adapt(now, slow=True)
            if len(self.sent_ts) >= self.WINDOW:
                self.pending = payload
                return False
            self._emit_locked(payload, now)
            return True

    def _emit_locked(self, payload, now):
        if not sio.connected:
            self.pending = None
            return
        self.sent_ts.append(now)
        self.frames += 1

        def _ack(*_a, _ts=now):
            self._on_ack(_ts)

        async def _go():
            try:
                await sio.emit("screen-frame", payload, namespace="/agent", callback=_ack)
            except Exception:
                self._on_ack(None)

        try:
            asyncio.run_coroutine_threadsafe(_go(), _AGENT_LOOP)
        except Exception:
            self.sent_ts.pop()

    def _on_ack(self, ts):
        with self.lock:
            now = time.time()
            if self.sent_ts:
                self.sent_ts.popleft()
            if ts:
                self.rtt = self.rtt * 0.8 + (now - ts) * 0.2
                self._adapt(now)
            if self.pending is not None and len(self.sent_ts) < self.WINDOW:
                p, self.pending = self.pending, None
                self._emit_locked(p, now)


_FLOW = _FrameFlow()


def _screen_status(loop, state: str, code: str = "", detail: str = "") -> None:
    """Zwischenstand der Aufnahme ans Dashboard (z.B. 'paused' bei
    Sperrbildschirm/UAC, 'live' wenn es wieder Bilder gibt)."""
    try:
        asyncio.run_coroutine_threadsafe(
            sio.emit("screen-status", {"id": DEVICE_ID, "state": state, "code": code,
                                       "detail": detail[:300]}, namespace="/agent"), loop)
    except Exception:
        pass


def _detect_from_xorg_process():
    """
    Sucht per psutil den laufenden X-Server und liest DISPLAY + Xauthority-Datei
    aus dessen Kommandozeile. So kann der Agent auch als Dienst (ohne eigenes
    DISPLAY) den angemeldeten Desktop erfassen. Rückgabe: (display, authfile).
    """
    try:
        for proc in psutil.process_iter(["name", "cmdline"]):
            name = (proc.info.get("name") or "").lower()
            if name not in ("xorg", "x", "xwayland") and "xorg" not in name:
                continue
            cmd = proc.info.get("cmdline") or []
            display = None
            auth = None
            for i, tok in enumerate(cmd):
                # Display sieht aus wie ":0" oder ":0.0"
                if tok.startswith(":") and tok[1:].split(".")[0].isdigit():
                    display = ":" + tok[1:].split(".")[0]
                if tok == "-auth" and i + 1 < len(cmd):
                    auth = cmd[i + 1]
            if display:
                return display, auth
    except Exception:
        pass
    return None, None


def _detect_graphical_display():
    """
    Prüft, ob ein erfassbarer grafischer Bildschirm verfügbar ist, und setzt auf
    Linux bei Bedarf DISPLAY/XAUTHORITY.

    Rueckgabe: (verfuegbar: bool, hinweis: str, code: str|None, params: dict)
    Der Hinweis ist die ENGLISCHE Rueckfallebene fuer Protokoll und aeltere
    Dashboards; `code`/`params` werden im Browser uebersetzt (siehe
    _notify_screen_error).
    """
    system = platform.system()
    # Windows/macOS haben praktisch immer einen erfassbaren Desktop.
    if system in ("Windows", "Darwin"):
        return True, "", None, {}

    # Linux: DISPLAY schon gesetzt? Dann direkt versuchen.
    if os.environ.get("DISPLAY"):
        return True, "", None, {}

    # 1) X-Server-Prozess finden (liefert DISPLAY + Xauthority)
    display, auth = _detect_from_xorg_process()
    if display:
        os.environ["DISPLAY"] = display
        if auth and os.path.isfile(auth):
            os.environ["XAUTHORITY"] = auth
        else:
            _find_xauthority_fallback()
        return True, f"Display {display} detected", "agent_display_found", {"display": display}

    # 2) Ersatz: aktive X-Sockets in /tmp/.X11-unix suchen
    try:
        socket_dir = "/tmp/.X11-unix"
        nums = []
        if os.path.isdir(socket_dir):
            for entry in os.listdir(socket_dir):
                if entry.startswith("X") and entry[1:].isdigit():
                    nums.append(int(entry[1:]))
        if nums:
            os.environ["DISPLAY"] = f":{sorted(nums)[0]}"
            _find_xauthority_fallback()
            return True, f"Display :{sorted(nums)[0]} detected", \
                   "agent_display_found", {"display": f":{sorted(nums)[0]}"}
    except Exception:
        pass

    # 3) Wayland-Sitzung? Dann klar sagen, WARUM keine Aufnahme möglich ist
    #    (mss kann Wayland nicht erfassen - X11-Sitzung nötig).
    try:
        run_dir = "/run/user"
        if os.path.isdir(run_dir):
            for uid in os.listdir(run_dir):
                udir = os.path.join(run_dir, uid)
                if any(e.startswith("wayland-") for e in os.listdir(udir)):
                    return (False,
                            "Wayland session detected - screen capture needs X11. "
                            "On Ubuntu: pick 'Ubuntu on Xorg' at the login screen (gear icon). "
                            "A shell will be opened instead.",
                            "agent_wayland", {})
    except Exception:
        pass

    # Kein grafischer Bildschirm -> Shell-only.
    return (False,
            "No graphical screen found (headless VM/server). "
            "A shell will be opened instead.",
            "agent_no_display", {})


def _find_xauthority_fallback():
    """
    Setzt XAUTHORITY, wenn der X-Server keinen '-auth'-Parameter verraten hat.
    Ohne das richtige Cookie verweigert der X-Server (z.B. Ubuntu Desktop mit
    GDM) dem als root laufenden Agenten sonst Bildaufnahme UND Tkinter-Dialog.
    Kandidaten: GDM-Xauthority unter /run/user/<uid>/, ~/.Xauthority der
    angemeldeten Benutzer, Mutter-XWayland-Auth.
    """
    if os.environ.get("XAUTHORITY") and os.path.isfile(os.environ["XAUTHORITY"]):
        return
    candidates = []
    try:
        run_dir = "/run/user"
        if os.path.isdir(run_dir):
            for uid in os.listdir(run_dir):
                udir = os.path.join(run_dir, uid)
                candidates.append(os.path.join(udir, "gdm", "Xauthority"))
                try:
                    for e in os.listdir(udir):
                        if e.startswith(".mutter-Xwaylandauth"):
                            candidates.append(os.path.join(udir, e))
                except OSError:
                    pass
    except Exception:
        pass
    try:
        import pwd
        for u in pwd.getpwall():
            if u.pw_uid >= 1000 and os.path.isdir(u.pw_dir):
                candidates.append(os.path.join(u.pw_dir, ".Xauthority"))
    except Exception:
        pass
    for c in candidates:
        try:
            if os.path.isfile(c):
                os.environ["XAUTHORITY"] = c
                _print(f"[agent] XAUTHORITY gesetzt: {c}")
                return
        except OSError:
            continue

# Maus-/Tastatur-Controller erst hier erstellen. Auch das kann fehlschlagen
# (z.B. keine grafische Sitzung) - dann deaktivieren wir die Fernsteuerung,
# statt den ganzen Agenten abstürzen zu lassen.
_mouse = None
_keyboard = None
if _INPUT_AVAILABLE:
    try:
        _mouse = MouseController()
        _keyboard = KeyboardController()
    except Exception as e:
        _INPUT_AVAILABLE = False
        _print(f"[agent] {_at('log_remote_ctl', state=_at('log_disabled'))} (controller init failed: {e})")


# ======================================================================
# Bildschirmaufnahme aus Sitzung 0 (Windows-Dienst) - Helfer-Prozess
# ======================================================================
# Ausgangslage: Der Windows-Agent wird als geplante Aufgabe unter dem Konto
# SYSTEM eingerichtet und startet beim Hochfahren. Solche Prozesse laufen in
# "Sitzung 0" - einer eigenen, unsichtbaren Sitzung, die seit Windows Vista
# strikt vom Desktop des angemeldeten Benutzers getrennt ist. Eine Aufnahme
# aus Sitzung 0 sieht den Benutzer-Desktop nicht; je nach Bibliothek kommt ein
# schwarzes Bild oder ein Fehler ("BitBlt failed", "access denied").
#
# Das laesst sich nicht wegkonfigurieren - man braucht einen zweiten Prozess
# IN der Sitzung des Benutzers. Genau das passiert hier:
#
#   1. Der Agent (Sitzung 0) oeffnet einen Server auf 127.0.0.1 mit zufaelligem
#      Port und zufaelligem Schluessel.
#   2. Er startet sich selbst noch einmal - diesmal mit dem Token des
#      angemeldeten Benutzers, also in dessen Sitzung - mit den Argumenten
#      --screen-helper <port> <schluessel>.
#   3. Der Helfer verbindet sich zurueck, weist sich mit dem Schluessel aus und
#      schickt von da an JPEG-Bilder. Eingaben (Maus/Tastatur) gehen den
#      umgekehrten Weg, denn auch die funktionieren aus Sitzung 0 nicht.
#
# Der Server lauscht ausschliesslich auf 127.0.0.1 und akzeptiert genau eine
# Verbindung, die den Schluessel kennt - von aussen ist da nichts erreichbar.


# ----------------------------------------------------------------------
# Der Helfer als EIGENSTAENDIGES Miniprogramm
# ----------------------------------------------------------------------
# Erster Versuch war, agent.py einfach noch einmal mit --screen-helper zu
# starten. Das schlug fehl: Der Helfer laeuft als normaler BENUTZER, agent.py
# schreibt beim Import aber unter anderem seine Logdatei nach
# "C:\Program Files\ByteraRmmAgent\agent.log" - dort hat ein Benutzer kein
# Schreibrecht. Der Prozess startete (die PID stand im Log), starb dann aber
# beim Import, noch bevor er sich verbinden konnte. Genau das sah man als
# "Helfer hat sich nicht gemeldet: timed out".
#
# Deshalb wird der Helfer jetzt als kleines, unabhaengiges Skript neben
# agent.py geschrieben. Es importiert nur, was es wirklich braucht, schreibt
# sein eigenes Protokoll ins TEMP-Verzeichnis des Benutzers und kennt weder
# Backend noch Watchdog.
_HELPER_FILENAME = "_screen_helper.py"

_HELPER_SOURCE = r"""# Automatisch erzeugt von agent.py - nicht von Hand bearbeiten.
# Laeuft in der Sitzung des angemeldeten Benutzers und liefert dem Agenten
# (Sitzung 0) Bildschirminhalte, Eingaben und die Zustimmungsabfrage.
import io, json, os, socket, sys, tempfile, threading, time, traceback

LOG = os.path.join(tempfile.gettempdir(), "rmm-screen-helper.log")

# Harte Abstuerze (z.B. in Tk/Tcl) beenden den Prozess, ohne dass Python noch
# einen Traceback schreibt. faulthandler schreibt dann wenigstens den Stack ins
# Protokoll - sonst war im Dashboard nur "Bild steht" zu sehen.
try:
    import faulthandler
    _FAULT_FILE = open(LOG, "a", encoding="utf-8")
    faulthandler.enable(_FAULT_FILE)
except Exception:
    _FAULT_FILE = None

def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg) + "\n")
    except Exception:
        pass

# Auch hier senden ZWEI Threads auf denselben Socket: der Steuer-Thread
# (Antwort der Zustimmungsabfrage) und die Aufnahmeschleife (Bilder). Ohne
# Schloss ueberlappen die sendall() und zerstoeren die Laengen-Praefixe.
_SEND_LOCK = threading.Lock()

def send(sock, payload):
    with _SEND_LOCK:
        sock.sendall(len(payload).to_bytes(4, "big") + payload)

def recv(sock):
    def exact(n):
        buf = b""
        while len(buf) < n:
            c = sock.recv(n - len(buf))
            if not c:
                return None
            buf += c
        return buf
    head = exact(4)
    return exact(int.from_bytes(head, "big")) if head else None

LANG = "de"

# Nur die Texte, die die Person VOR dem Geraet sieht. ACHTUNG: In
# _HELPER_SOURCE darf kein Docstring mit dreifachen Anfuehrungszeichen stehen -
# das wuerde den r-String beenden und agent.py zerlegen.
TEXTS = {
    "de": {
        "window": "BYTERA RMM - Remote-Bildschirm",
        "title": "Remote-Bildschirm zulassen?",
        "body": "{who} moechte den Bildschirm dieses Computers\nsehen und steuern.",
        "allow": "\u2713 Zulassen",
        "deny": "Ablehnen",
        "auto": "Automatisch abgelehnt in {secs} s",
        "somebody": "Ein Administrator",
        "sess_title": "Fernwartung aktiv",
        "sess_sub": "{who} ist mit diesem Computer verbunden.",
        "sess_chat_ph": "Nachricht an den Administrator ...",
        "sess_send": "Senden",
        "sess_grant_on": "Administrator-Rechte erlauben",
        "sess_grant_off": "Administrator-Rechte entziehen",
        "sess_granted": "Administrator-Rechte sind erteilt.",
        "sess_disconnect": "Verbindung trennen",
        "sess_you": "Du",
        "sess_admin": "Admin",
        "sess_connected": "Verbunden",
        "sess_ended": "Die Fernwartung wurde beendet.",
        "sess_window": "BYTERA RMM - Fernwartung",
        "sess_end": "Sitzung beenden",
        "sess_perms": "BERECHTIGUNGEN DES ADMINISTRATORS",
        "perm_input": "Steuerung",
        "perm_clipboard": "Zwischenablage",
        "perm_admin": "Admin-Rechte",
        "perm_on_input": "Maus und Tastatur duerfen gesteuert werden.",
        "perm_off_input": "Steuerung gesperrt - der Administrator kann nur zusehen.",
        "perm_on_clipboard": "Zwischenablage freigegeben.",
        "perm_off_clipboard": "Zwischenablage gesperrt.",
        "perm_on_admin": "Administrator-Rechte erteilt.",
        "perm_off_admin": "Administrator-Rechte entzogen.",
        "hint_input_off": "Nur Zusehen.",
        "hint_clip_off": "Zwischenablage gesperrt.",
        "sess_req": "Der Administrator bittet um Administrator-Rechte.",
        "sess_req_allow": "Erlauben",
        "sess_req_deny": "Ablehnen",
        "sess_req_denied": "Anfrage nach Administrator-Rechten abgelehnt.",
        "perm_files": "Dateien",
        "perm_on_files": "Datei-Uebertragung erlaubt.",
        "perm_off_files": "Datei-Uebertragung gesperrt.",
        "hint_files_off": "Dateien gesperrt.",
        "file_pick": "Datei an den Administrator senden",
        "file_too_big": "Die Datei ist zu gross (hoechstens 1 GB).",
        "file_sending": "Sende {name} ...",
        "file_sent": "{name} wurde gesendet.",
        "file_failed": "{name} konnte nicht gesendet werden.",
        "file_received": "Datei empfangen: {name}",
        "file_open": "Ordner oeffnen",
        "audio_on": "Der Administrator hoert jetzt den Ton dieses Computers mit.",
        "audio_off": "Ton-Uebertragung beendet.",
    },
    "en": {
        "window": "BYTERA RMM - Remote screen",
        "title": "Allow remote screen access?",
        "body": "{who} would like to view and control\nthe screen of this computer.",
        "allow": "\u2713 Allow",
        "deny": "Deny",
        "auto": "Automatically denied in {secs} s",
        "somebody": "An administrator",
        "sess_title": "Remote support active",
        "sess_sub": "{who} is connected to this computer.",
        "sess_chat_ph": "Message to the administrator ...",
        "sess_send": "Send",
        "sess_grant_on": "Allow administrator rights",
        "sess_grant_off": "Revoke administrator rights",
        "sess_granted": "Administrator rights are granted.",
        "sess_disconnect": "Disconnect",
        "sess_you": "You",
        "sess_admin": "Admin",
        "sess_connected": "Connected",
        "sess_ended": "The remote session has ended.",
        "sess_window": "BYTERA RMM - Remote support",
        "sess_end": "End session",
        "sess_perms": "ADMINISTRATOR PERMISSIONS",
        "perm_input": "Control",
        "perm_clipboard": "Clipboard",
        "perm_admin": "Admin rights",
        "perm_on_input": "Mouse and keyboard may be controlled.",
        "perm_off_input": "Control blocked - the administrator can only watch.",
        "perm_on_clipboard": "Clipboard allowed.",
        "perm_off_clipboard": "Clipboard blocked.",
        "perm_on_admin": "Administrator rights granted.",
        "perm_off_admin": "Administrator rights revoked.",
        "hint_input_off": "View only.",
        "hint_clip_off": "Clipboard blocked.",
        "sess_req": "The administrator is asking for administrator rights.",
        "sess_req_allow": "Allow",
        "sess_req_deny": "Deny",
        "sess_req_denied": "Request for administrator rights denied.",
        "perm_files": "Files",
        "perm_on_files": "File transfer allowed.",
        "perm_off_files": "File transfer blocked.",
        "hint_files_off": "Files blocked.",
        "file_pick": "Send a file to the administrator",
        "file_too_big": "The file is too large (max. 1 GB).",
        "file_sending": "Sending {name} ...",
        "file_sent": "{name} has been sent.",
        "file_failed": "{name} could not be sent.",
        "file_received": "File received: {name}",
        "file_open": "Open folder",
        "audio_on": "The administrator is now listening to this computer's sound.",
        "audio_off": "Sound transfer stopped.",
    },
}


def ht(key, **kw):
    tbl = TEXTS.get(LANG) or TEXTS["de"]
    txt = tbl.get(key) or TEXTS["de"].get(key) or key
    return txt.format(**kw) if kw else txt


# --- EIN Oberflaechen-Thread fuer alles, was Tk braucht ------------------------
# Frueher erzeugte jeder Teil sein EIGENES Tk in seinem EIGENEN Thread: die
# Zustimmungsabfrage im Steuer-Thread, das Sitzungsfenster in einem weiteren
# Thread, die Zwischenablage mal hier, mal dort. Tk ist dafuer nicht gebaut -
# unter Windows fuehrte das zu (a) Fenstern, die keine Tastatur-Eingaben bekamen,
# und (b) harten Abstuerzen des ganzen Helfers ("Tcl_AsyncDelete: async handler
# deleted by the wrong thread"), sobald die Speicherbereinigung ein altes
# Tk-Objekt in einem fremden Thread aufraeumte - typischerweise wenige Sekunden
# nach Sitzungsbeginn, also nach ein paar Dutzend Bildern. Jetzt gibt es genau
# EIN Tk (verborgenes Hauptfenster) im HAUPT-Thread des Helfers; alle anderen
# Threads geben ihre Tk-Arbeit ueber ui_call() dorthin ab.
import queue as _queue
_UI = {"root": None, "q": _queue.Queue(), "ok": False, "ready": threading.Event()}


def ui_call(fn, wait=True, timeout=None):
    # Der Oberflaechen-Thread startet parallel zum Steuer-Thread - ein frueher
    # Auftrag (Zustimmung, Sitzungsfenster) wartet kurz, bis er bereit ist.
    _UI["ready"].wait(10)
    if not _UI["ok"]:
        raise RuntimeError("keine Oberflaeche (Tk) verfuegbar")
    box = {"done": threading.Event(), "res": None, "err": None}
    _UI["q"].put((fn, box))
    if not wait:
        return None
    if not box["done"].wait(timeout):
        raise RuntimeError("Oberflaeche antwortet nicht")
    if box["err"] is not None:
        raise box["err"]
    return box["res"]


def ui_loop(state):
    # Laeuft im Haupt-Thread. False = kein Tk verfuegbar (dann ohne Fenster weiter).
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
    except Exception as e:
        log("Oberflaeche (Tk) nicht verfuegbar: %s" % e)
        _UI["ready"].set()
        return False
    _UI["root"] = root
    _UI["ok"] = True
    _UI["ready"].set()

    def pump():
        # ZUERST neu einplanen: ein Auftrag kann eine eigene Warteschleife
        # starten (Zustimmungsabfrage) - die Warteschlange muss weiterlaufen.
        if state["stop"]:
            try: root.quit()
            except Exception: pass
            return
        root.after(40, pump)
        while True:
            try:
                fn, box = _UI["q"].get_nowait()
            except _queue.Empty:
                break
            try:
                box["res"] = fn()
            except Exception as e:
                box["err"] = e
                log("Oberflaeche: %s" % e)
            finally:
                box["done"].set()

    root.after(40, pump)
    try:
        root.mainloop()
    except Exception as e:
        log("Oberflaeche beendet: %s" % e)
    _UI["ok"] = False
    try: root.destroy()
    except Exception: pass
    return True


def _logo_images(win):
    # Logo (liegt neben dem Helfer) als Fenstersymbol. Bilder am Fenster
    # festhalten, sonst raeumt Python sie weg.
    try:
        import tkinter as tk
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.png")
        if not os.path.isfile(p):
            return None
        big = tk.PhotoImage(master=win, file=p)
        icons = [big.subsample(max(1, big.width() // t), max(1, big.width() // t)) for t in (64, 32, 16)]
        win._icon_refs = [big] + icons
        try: win.iconphoto(False, *icons)
        except Exception: pass
        return big
    except Exception as e:
        log("logo not loadable: %s" % e)
        return None


# --- Datei-Uebertragung waehrend der Sitzung (wie AnyDesk) -------------------
# Admin -> Benutzer: Stuecke kommen als "file-chunk" an und landen im Ordner
# "Downloads" des angemeldeten Benutzers (der Helfer laeuft als dieser Benutzer,
# die Datei gehoert also ihm). Jedes Stueck wird bestaetigt ("file-ack") - erst
# dann schickt das Dashboard das naechste (Flusskontrolle).
# Benutzer -> Admin: "Datei senden" im Sitzungsfenster liest die Datei in
# Stuecken und schickt sie als "file-out" ans Dashboard.
FILE_CHUNK = 256 * 1024
FILE_MAX = 1024 * 1024 * 1024          # 1 GB je Datei


def downloads_dir():
    home = os.path.expanduser("~")
    for cand in (os.path.join(home, "Downloads"), home, tempfile.gettempdir()):
        if os.path.isdir(cand):
            return cand
    return tempfile.gettempdir()


def safe_name(name):
    name = os.path.basename(str(name or "").replace("\\", "/")).strip() or "datei"
    bad = '<>:"/\\|?*'
    name = "".join("_" if (ch in bad or ord(ch) < 32) else ch for ch in name)
    return name[:180] or "datei"


def unique_path(folder, name):
    base, ext = os.path.splitext(name)
    path, n = os.path.join(folder, name), 1
    while os.path.exists(path):
        path = os.path.join(folder, "%s (%d)%s" % (base, n, ext))
        n += 1
    return path


def open_folder(path):
    try:
        if sys.platform == "win32":
            import subprocess
            subprocess.Popen(["explorer", "/select,", path])
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", "-R", path])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
    except Exception as e:
        log("Ordner oeffnen: %s" % e)


# --- Sitzungsfenster beim Benutzer (nach dem Vorbild von AnyDesk) ---------------
# Zeigt, WER verbunden ist und seit wann, einen Chat mit dem Administrator, die
# Berechtigungen als Schalter (Steuerung, Zwischenablage, Administrator-Rechte)
# und "Sitzung beenden". Bei Silent-Modus wird es NICHT geoeffnet.
# Wichtig zum Aufbau: Eingabezeile, Berechtigungen und "Sitzung beenden" werden
# ZUERST von unten angeordnet, der Chat-Verlauf fuellt nur den Rest. Vorher kam
# der Verlauf zuerst, nahm seine volle Wunschhoehe und schob Eingabefeld und
# Knoepfe aus dem Fenster - der Benutzer konnte nur lesen.
# Alles hier laeuft im Oberflaechen-Thread (ui_call).
class SessionWindow:
    BG = "#131c2b"; PANEL = "#0d1420"; TEXT = "#e6ecf5"; SUB = "#8ea0b8"
    ACC = "#4f8cff"; ON = "#2e7d57"; OFF = "#24304a"; DANGER = "#b4455a"; WARN = "#c98a1b"

    def __init__(self, root, who, reply, perms):
        import tkinter as tk
        self.tk = tk
        self.reply = reply
        self.closed = False
        self.started = time.time()
        self.perms = {"input": True, "clipboard": True, "files": True, "admin": False}
        self.perms.update(perms or {})
        BG, PANEL, TEXT, SUB = self.BG, self.PANEL, self.TEXT, self.SUB
        w = self.win = tk.Toplevel(root)
        w.title(ht("sess_window"))
        w.configure(bg=BG)
        w.attributes("-topmost", True)
        w.minsize(340, 470)
        try:
            W, H = 370, 560
            x = w.winfo_screenwidth() - W - 16
            y = max(10, w.winfo_screenheight() - H - 70)
            w.geometry("%dx%d+%d+%d" % (W, H, x, y))
        except Exception:
            pass
        _logo_images(w)

        # Kopf: Avatar mit Initiale, Name, Verbindungsdauer
        head = tk.Frame(w, bg=BG, padx=14, pady=12); head.pack(side="top", fill="x")
        initial = (str(who).strip()[:1] or "A").upper()
        tk.Label(head, text=initial, width=3, height=1, bg=self.ACC, fg="#ffffff",
                 font=("Segoe UI", 16, "bold")).pack(side="left", padx=(0, 10))
        info = tk.Frame(head, bg=BG); info.pack(side="left", fill="x", expand=True)
        tk.Label(info, text=str(who), font=("Segoe UI", 12, "bold"), bg=BG, fg=TEXT, anchor="w").pack(fill="x")
        self.timer = tk.Label(info, text=ht("sess_connected"), font=("Segoe UI", 9), bg=BG, fg=SUB, anchor="w")
        self.timer.pack(fill="x")

        # --- von UNTEN: Sitzung beenden, Berechtigungen, Eingabezeile ---
        tk.Button(w, text="\u23F9  " + ht("sess_end"), command=self._end, bg=self.DANGER, fg="#ffffff",
                  activebackground="#9a3a4d", activeforeground="#ffffff", bd=0,
                  font=("Segoe UI", 10, "bold"), pady=9, cursor="hand2").pack(side="bottom", fill="x", padx=10, pady=(4, 10))

        perm = tk.Frame(w, bg=BG, padx=10); perm.pack(side="bottom", fill="x", pady=(6, 2))
        tk.Label(perm, text=ht("sess_perms"), font=("Segoe UI", 8, "bold"), bg=BG, fg=SUB, anchor="w").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        self.perm_btns = {}
        for col, (key, icon) in enumerate((("input", "\U0001F5B1"), ("clipboard", "\U0001F4CB"),
                                           ("files", "\U0001F4C1"), ("admin", "\U0001F6E1"))):
            # 2 x 2 Schalter (vier nebeneinander wurden bei langen Texten abgeschnitten)
            b = tk.Button(perm, text=icon + "  " + ht("perm_" + key), command=lambda k=key: self._toggle(k),
                          bd=0, font=("Segoe UI", 9, "bold"), fg="#ffffff", pady=7, cursor="hand2")
            b.grid(row=1 + col // 2, column=col % 2, sticky="nsew", padx=3, pady=2)
            perm.grid_columnconfigure(col % 2, weight=1, uniform="p")
            self.perm_btns[key] = b
        self.perm_hint = tk.Label(perm, text="", font=("Segoe UI", 8), bg=BG, fg=SUB, anchor="w", justify="left", wraplength=330)
        self.perm_hint.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # Anfrage des Administrators (nur sichtbar, wenn er Rechte anfordert)
        self.req = tk.Frame(w, bg=self.WARN, padx=10, pady=8)
        tk.Label(self.req, text=ht("sess_req"), bg=self.WARN, fg="#ffffff", font=("Segoe UI", 9, "bold"),
                 anchor="w", justify="left", wraplength=320).pack(fill="x")
        rb = tk.Frame(self.req, bg=self.WARN); rb.pack(fill="x", pady=(6, 0))
        tk.Button(rb, text=ht("sess_req_allow"), command=lambda: self._answer_request(True), bg=self.ON, fg="#ffffff",
                  bd=0, padx=12, pady=4, cursor="hand2").pack(side="left")
        tk.Button(rb, text=ht("sess_req_deny"), command=lambda: self._answer_request(False), bg=self.OFF, fg="#ffffff",
                  bd=0, padx=12, pady=4, cursor="hand2").pack(side="left", padx=6)

        row = tk.Frame(w, bg=BG, padx=10); row.pack(side="bottom", fill="x", pady=(4, 0))
        self.row = row
        self.entry = tk.Entry(row, bg=PANEL, fg=TEXT, insertbackground=TEXT, bd=0, font=("Segoe UI", 10),
                              relief="flat", highlightthickness=1, highlightbackground="#24304a", highlightcolor=self.ACC)
        self.entry.pack(side="left", fill="x", expand=True, ipady=7, padx=(0, 6))
        self.entry.bind("<Return>", lambda e: self._send())
        tk.Button(row, text=ht("sess_send"), command=self._send, bg=self.ACC, fg="#ffffff", bd=0,
                  font=("Segoe UI", 9, "bold"), padx=14, pady=6, cursor="hand2").pack(side="right")
        # Datei an den Administrator schicken (wie "Datei senden" bei AnyDesk)
        tk.Button(row, text="\U0001F4CE", command=self._pick_file, bg=self.OFF, fg="#ffffff", bd=0,
                  font=("Segoe UI", 10), padx=8, pady=4, cursor="hand2").pack(side="right", padx=(0, 6))

        # --- Rest: Chat-Verlauf (klein anfangen, waechst mit dem Fenster) ---
        chat = tk.Frame(w, bg=PANEL); chat.pack(side="top", fill="both", expand=True, padx=10, pady=(0, 4))
        self.log = tk.Text(chat, height=6, bg=PANEL, fg=TEXT, bd=0, font=("Segoe UI", 10), wrap="word",
                           state="disabled", padx=8, pady=8)
        sb = tk.Scrollbar(chat, command=self.log.yview); sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        self.log.config(yscrollcommand=sb.set)
        self.log.tag_config("you", foreground="#7fd6a0", font=("Segoe UI", 9, "bold"))
        self.log.tag_config("admin", foreground="#7fb4ff", font=("Segoe UI", 9, "bold"))
        self.log.tag_config("sys", foreground=SUB, font=("Segoe UI", 8, "italic"))
        self.log.tag_config("file", foreground="#f0c674", font=("Segoe UI", 9, "bold"))
        self._link_n = 0
        # Klick irgendwo in den Verlauf -> Schreibfeld bekommt den Fokus
        self.log.bind("<Button-1>", lambda e: self.entry.focus_set())

        w.protocol("WM_DELETE_WINDOW", self._minimize)       # X minimiert (wie AnyDesk), trennt nicht
        self._paint_perms()
        self._sys(ht("sess_sub", who=who))
        self._tick()
        try:
            w.lift()
            self.entry.focus_set()
        except Exception:
            pass

    # --- Darstellung ---
    def _paint_perms(self):
        for key, b in self.perm_btns.items():
            on = bool(self.perms.get(key))
            b.config(bg=self.ON if on else self.OFF, activebackground=self.ON if on else self.OFF)
        hints = []
        if not self.perms.get("input"):
            hints.append(ht("hint_input_off"))
        if not self.perms.get("clipboard"):
            hints.append(ht("hint_clip_off"))
        if not self.perms.get("files"):
            hints.append(ht("hint_files_off"))
        if self.perms.get("admin"):
            hints.append(ht("sess_granted"))
        self.perm_hint.config(text="  ".join(hints))

    def _tick(self):
        if self.closed:
            return
        s = int(time.time() - self.started)
        self.timer.config(text="%s  %02d:%02d:%02d" % (ht("sess_connected"), s // 3600, s % 3600 // 60, s % 60))
        self.win.after(1000, self._tick)

    def _append(self, who_tag, who_text, text):
        self.log.config(state="normal")
        self.log.insert("end", time.strftime("%H:%M ") + who_text + ": ", who_tag)
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _sys(self, text):
        self.log.config(state="normal"); self.log.insert("end", text + "\n", "sys")
        self.log.see("end"); self.log.config(state="disabled")

    def _attention(self):
        try:
            self.win.deiconify(); self.win.lift(); self.win.bell()
        except Exception:
            pass

    # --- Aktionen des Benutzers ---
    def _send(self):
        txt = self.entry.get().strip()
        if not txt:
            return
        self.entry.delete(0, "end")
        self._append("you", ht("sess_you"), txt)
        try: self.reply({"session": "chat", "text": txt})
        except Exception: pass

    def _toggle(self, key, value=None):
        v = (not self.perms.get(key)) if value is None else bool(value)
        self.perms[key] = v
        self._paint_perms()
        self._sys(ht(("perm_on_" if v else "perm_off_") + key))
        try:
            if key == "admin":
                self.reply({"session": "grant", "granted": v})
            else:
                self.reply({"session": "perm", "perm": key, "value": v})
        except Exception:
            pass

    def _answer_request(self, ok):
        self.req.pack_forget()
        if ok:
            if not self.perms.get("admin"):
                self._toggle("admin", True)
        else:
            self._sys(ht("sess_req_denied"))
            try: self.reply({"session": "grant-denied"})
            except Exception: pass

    def _end(self):
        try: self.reply({"session": "disconnect"})
        except Exception: pass
        self._sys(ht("sess_ended"))
        self.close()

    def _minimize(self):
        try: self.win.iconify()
        except Exception: pass

    # --- Dateien ---
    def _pick_file(self):
        try:
            from tkinter import filedialog
            path = filedialog.askopenfilename(parent=self.win, title=ht("file_pick"))
        except Exception as e:
            log("Dateiauswahl: %s" % e)
            return
        if not path:
            return
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        if size > FILE_MAX:
            self._sys(ht("file_too_big"))
            return
        self._sys(ht("file_sending", name=os.path.basename(path)))
        threading.Thread(target=self._send_file, args=(path, size), daemon=True).start()

    def _send_file(self, path, size):
        tid = "u%d%04d" % (int(time.time() * 1000), os.getpid() % 10000)
        name = os.path.basename(path)
        import base64
        try:
            with open(path, "rb") as f:
                seq = 0
                while True:
                    data = f.read(FILE_CHUNK)
                    last = f.tell() >= size or not data
                    self.reply({"session": "file-out", "tid": tid, "name": name, "size": size, "seq": seq,
                                "data": base64.b64encode(data).decode("ascii"), "last": bool(last)})
                    seq += 1
                    if last:
                        break
                    time.sleep(0.01)          # Bilder nicht ausbremsen
            msg = ht("file_sent", name=name)
        except Exception as e:
            log("Datei senden: %s" % e)
            msg = ht("file_failed", name=name)
        try:
            ui_call(lambda: self._sys(msg), wait=False)
        except Exception:
            pass

    def on_file_received(self, name, path):
        # Zeile mit anklickbarem "Ordner oeffnen"
        self._link_n += 1
        tag = "lnk%d" % self._link_n
        self.log.config(state="normal")
        self.log.insert("end", time.strftime("%H:%M ") + "\U0001F4E5 " + ht("file_received", name=name) + "  ", "file")
        self.log.insert("end", ht("file_open"), (tag,))
        self.log.insert("end", "\n")
        self.log.tag_config(tag, foreground="#7fb4ff", underline=True)
        self.log.tag_bind(tag, "<Button-1>", lambda e, p=path: open_folder(p))
        self.log.tag_bind(tag, "<Enter>", lambda e: self.log.config(cursor="hand2"))
        self.log.tag_bind(tag, "<Leave>", lambda e: self.log.config(cursor=""))
        self.log.see("end")
        self.log.config(state="disabled")
        self._attention()

    def on_audio(self, on):
        # Datenschutz: sichtbar machen, dass der Ton dieses PCs uebertragen wird
        self._sys("\U0001F50A " + ht("audio_on") if on else "\U0001F507 " + ht("audio_off"))
        self.timer.config(fg="#f0c674" if on else self.SUB)

    # --- vom Agenten (ueber ui_call im Oberflaechen-Thread) ---
    def on_admin_chat(self, text):
        self._append("admin", ht("sess_admin"), text)
        self._attention()

    def on_admin_request(self):
        if self.perms.get("admin"):
            return
        self.req.pack(side="bottom", fill="x", padx=10, pady=(4, 0), before=self.row)
        self._sys(ht("sess_req"))
        self._attention()

    def close(self):
        if self.closed:
            return
        self.closed = True
        try: self.win.destroy()
        except Exception: pass


def consent_dialog(who, timeout_s):
    # Laeuft im Oberflaechen-Thread (ui_call). Eigenes Toplevel am gemeinsamen
    # Tk; gewartet wird mit wait_window - die Warteschlange laeuft dabei weiter.
    import tkinter as tk
    root = _UI["root"]
    result = {"ok": False}
    top = tk.Toplevel(root)
    top.title(ht("window"))
    top.attributes("-topmost", True)
    top.configure(bg="#131c2b")
    top.resizable(False, False)
    big = _logo_images(top)
    small = None
    if big is not None:
        try:
            small = big.subsample(max(1, big.width() // 64), max(1, big.width() // 64))
            top._icon_refs.append(small)
        except Exception:
            small = None
    if small is not None:
        tk.Label(top, image=small, bg="#131c2b").pack(pady=(20, 4))
    else:
        tk.Label(top, text="\U0001F5A5", font=("Segoe UI Emoji", 30),
                 bg="#131c2b", fg="#e8eef7").pack(pady=(20, 4))
    tk.Label(top, text=ht("title"), bg="#131c2b", fg="#e8eef7",
             font=("Segoe UI", 14, "bold")).pack(pady=(4, 8))
    tk.Label(top, text=ht("body", who=str(who)), bg="#131c2b", fg="#8fa3bd",
             font=("Segoe UI", 10), justify="center").pack(padx=28)
    countdown = tk.Label(top, text="", bg="#131c2b", fg="#8fa3bd", font=("Segoe UI", 9))
    countdown.pack(pady=(10, 0))
    row = tk.Frame(top, bg="#131c2b"); row.pack(pady=18)
    def yes():
        result["ok"] = True; top.destroy()
    def no():
        top.destroy()
    tk.Button(row, text=ht("deny"), command=no, width=14,
              bg="#243043", fg="#e8eef7", relief="flat").pack(side="left", padx=6)
    tk.Button(row, text=ht("allow"), command=yes, width=14,
              bg="#2f6fed", fg="#ffffff", relief="flat").pack(side="left", padx=6)
    left = {"n": int(timeout_s)}
    def tick():
        if not top.winfo_exists():
            return
        if left["n"] <= 0:
            try: top.destroy()
            except Exception: pass
            return
        countdown.config(text=ht("auto", secs=left["n"]))
        left["n"] -= 1
        top.after(1000, tick)
    tick()
    top.protocol("WM_DELETE_WINDOW", no)
    try:
        top.update_idletasks()
        w, h = top.winfo_width(), top.winfo_height()
        x = (top.winfo_screenwidth() - w) // 2
        y = (top.winfo_screenheight() - h) // 3
        top.geometry("+%d+%d" % (x, y))
        top.lift()
        top.focus_force()
    except Exception:
        pass
    root.wait_window(top)
    return result["ok"]

def main():
    port, key = int(sys.argv[1]), sys.argv[2]
    # Drittes Argument ist die Sprache (siehe AGENT_LANG im Agenten).
    # Faellt es weg (alter Agent startet neuen Helfer), bleibt Deutsch.
    global LANG
    LANG = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] in ("de", "en") else "de"
    log("Start (PID %s, Python %s)" % (os.getpid(), sys.executable))
    sock = socket.create_connection(("127.0.0.1", port), timeout=15)
    # Zeitlimit NUR fuer den Verbindungsaufbau. Blieb es stehen, beendete jede
    # Pause ohne Steuerbefehl von mehr als 15 s (Admin schaut nur zu, bewegt die
    # Maus nicht) den Steuerkanal - und damit den ganzen Helfer: das Bild blieb
    # nach einigen Dutzend Bildern stehen.
    sock.settimeout(None)
    send(sock, json.dumps({"key": key, "pid": os.getpid()}).encode())
    log("connected to the agent on port %s" % port)

    state = {"quality": 60, "fps": 10, "monitor": 1, "audio": False, "width": 0,
             "left": 0, "top": 0, "stream": False, "stop": False, "sesswin": None}

    state["files"] = {}

    def receive_chunk(o):
        import base64
        tid = str(o.get("tid") or "")
        st = state["files"].get(tid)
        if st is None:
            if int(o.get("seq") or 0) != 0:
                raise RuntimeError("unbekannte Uebertragung")
            if int(o.get("size") or 0) > FILE_MAX:
                raise RuntimeError("Datei zu gross")
            path = unique_path(downloads_dir(), safe_name(o.get("name")))
            st = state["files"][tid] = {"f": open(path, "wb"), "path": path, "n": 0}
        data = base64.b64decode(o.get("data") or "")
        st["n"] += len(data)
        if st["n"] > FILE_MAX:
            raise RuntimeError("Datei zu gross")
        st["f"].write(data)
        if o.get("last"):
            st["f"].close()
            state["files"].pop(tid, None)
            log("Datei empfangen: %s (%d Bytes)" % (st["path"], st["n"]))
            sess_do("on_file_received", os.path.basename(st["path"]), st["path"])
            return {"done": True, "path": st["path"]}
        return {}

    def sess_do(method, *args):
        # Methode des Sitzungsfensters im Oberflaechen-Thread aufrufen (ohne zu warten).
        w = state.get("sesswin")
        if w is None:
            return
        try:
            ui_call(lambda: getattr(w, method)(*args), wait=False)
        except Exception as e:
            log("Sitzungsfenster (%s): %s" % (method, e))

    mouse = keyboard = None
    try:
        from pynput.mouse import Controller as MC, Button as MB
        from pynput.keyboard import Controller as KC, Key as KK
        mouse, keyboard = MC(), KC()
    except Exception as e:
        log("input not available: %s" % e)
        MB = KK = None

    # --- Benutzer hat Vorrang: bewegt die Person vor dem PC die Maus selbst,
    # werden die fern gesteuerten Maus-Befehle kurz ignoriert. Wir unterscheiden
    # unsere EIGENE Fernbewegung (mouse.position = ...) von einer echten Bewegung,
    # indem wir uns die zuletzt gesetzte Position merken.
    user_mouse = {"last_local": 0.0, "set_pos": None, "set_at": 0.0}
    PRIORITY_WINDOW = 1.2          # s, so lange gilt der Benutzer als "aktiv"

    def _on_local_move(x, y):
        now = time.time()
        sp = user_mouse["set_pos"]
        # Kam diese Bewegung von UNS (letzte ferngesetzte Position, frisch)? -> ignorieren.
        if sp is not None and abs(x - sp[0]) <= 2 and abs(y - sp[1]) <= 2 and (now - user_mouse["set_at"]) < 0.5:
            return
        user_mouse["last_local"] = now

    try:
        from pynput.mouse import Listener as _MouseListener
        _ml = _MouseListener(on_move=_on_local_move)
        _ml.daemon = True
        _ml.start()
    except Exception as e:
        log("Maus-Beobachter nicht verfuegbar: %s" % e)

    def user_active():
        return (time.time() - user_mouse["last_local"]) < PRIORITY_WINDOW

    # Tastennamen des Browsers -> pynput. MUSS zur Tabelle _SPECIAL_KEYS im
    # Agenten passen. Vorher stand hier nur getattr(Key, name): Das Dashboard
    # schickt aber Browser-Namen ("Enter", "Escape", "Control", "ArrowUp"),
    # pynput heisst die Dinger anders ("enter", "esc", "ctrl", "up"). Jeder
    # Treffer war None, danach versuchte pynput die ZEICHENKETTE zu druecken
    # und warf - die Tastatur-Fernsteuerung tat deshalb gar nichts.
    SPECIAL = {}
    if KK is not None:
        SPECIAL = {
            "Enter": KK.enter, "Backspace": KK.backspace, "Tab": KK.tab,
            "Escape": KK.esc, " ": KK.space,
            "ArrowUp": KK.up, "ArrowDown": KK.down,
            "ArrowLeft": KK.left, "ArrowRight": KK.right,
            "Delete": KK.delete, "Home": KK.home, "End": KK.end,
            "PageUp": KK.page_up, "PageDown": KK.page_down,
            "Control": KK.ctrl, "Alt": KK.alt, "Shift": KK.shift,
            "Meta": KK.cmd,
        }

    def to_key(name):
        # Browser-Name -> pynput-Taste. Einzelne Zeichen bleiben Zeichen.
        # ACHTUNG: Dieser Code steht in _HELPER_SOURCE, einem r-String mit
        # dreifachen Anfuehrungszeichen. Ein Docstring darin wuerde den String
        # beenden und agent.py zerlegen - deshalb nur Kommentare, keine
        # Docstrings.
        if not name:
            return None
        if name in SPECIAL:
            return SPECIAL[name]
        if len(name) == 1:
            return name
        # F1..F12 und alles andere, was pynput unter kleinem Namen kennt.
        return getattr(KK, name.lower(), None)

    def set_clipboard(text):
        # Zwischenablage der Benutzersitzung setzen - ueber das EINE Tk im
        # Oberflaechen-Thread (kein eigenes Tk pro Aufruf mehr, siehe ui_call).
        def _do():
            r = _UI["root"]
            r.clipboard_clear(); r.clipboard_append(text)
            r.update()      # erst dadurch uebernimmt Windows den Inhalt
        ui_call(_do, timeout=5)

    def get_clipboard():
        return ui_call(lambda: _UI["root"].clipboard_get(), timeout=5)

    def do_input(d):
        k = d.get("type")
        # Tastatur/Zwischenablage brauchen keine Maus - deshalb VOR der
        # Maus-Pruefung behandeln.
        if k == "clipboard-set":
            try: set_clipboard(d.get("text", ""))
            except Exception as e: log("Zwischenablage setzen: %s" % e)
            return
        if k in ("key", "combo", "text") and keyboard is None:
            return
        if k == "text":
            keyboard.type(d.get("text", ""))
            return
        if k in ("key", "combo"):
            names = d.get("keys") or [d.get("key")]
            keys = [to_key(n) for n in names]
            keys = [x for x in keys if x is not None]
            for x in keys:
                keyboard.press(x)
            for x in reversed(keys):
                keyboard.release(x)
            return

        if mouse is None:
            return
        # Benutzer-Vorrang: bewegt die Person gerade selbst die Maus, lassen wir
        # ihre Bewegung in Ruhe und verwerfen die ferngesteuerte Maus-Aktion.
        if k in ("move", "click", "down", "up", "double", "dblclick", "scroll") and user_active():
            return
        ox, oy = state["left"], state["top"]
        def pos(x):
            return (int(x["x"]) + ox, int(x["y"]) + oy)
        btn = {"left": "left", "right": "right", "middle": "middle"}.get(
            d.get("button", "left"), "left")
        b = getattr(MB, btn)
        def _set(p):
            mouse.position = p
            user_mouse["set_pos"] = p
            user_mouse["set_at"] = time.time()
        if k == "move":
            _set(pos(d))
        elif k == "click":
            _set(pos(d)); mouse.click(b, 1)
        elif k == "down":
            _set(pos(d)); mouse.press(b)
        elif k == "up":
            _set(pos(d)); mouse.release(b)
        elif k in ("double", "dblclick"):
            # Das Dashboard schickt "double" - der Helfer kannte bisher nur
            # "dblclick", ein Doppelklick kam also nie an.
            _set(pos(d)); mouse.click(MB.left, 2)
        elif k == "scroll":
            mouse.scroll(0, int(d.get("dy", 0)))

    def control():
        while not state["stop"]:
            try:
                msg = recv(sock)
            except Exception as e:
                log("Steuerkanal beendet: %s" % e); break
            if msg is None or msg[:1] != b"C":
                if msg is None:
                    break
                continue
            try:
                o = json.loads(msg[1:].decode())
            except Exception:
                continue
            c = o.get("cmd")
            if c == "config":
                state["quality"] = int(o.get("quality", state["quality"]))
                state["fps"] = int(o.get("fps", state["fps"]))
                state["monitor"] = int(o.get("monitor", state["monitor"]))
                state["width"] = int(o.get("width", state.get("width", 0)) or 0)
                state["stream"] = bool(o.get("stream", state["stream"]))
            elif c == "refresh":
                state["force"] = True          # naechstes Bild auch ohne Aenderung
            elif c == "input":
                try: do_input(o.get("data") or {})
                except Exception as e: log("Eingabe: %s" % e)
            elif c == "consent":
                ok = False
                try:
                    secs = int(o.get("timeout", 30))
                    ok = ui_call(lambda: consent_dialog(o.get("who") or ht("somebody"), secs),
                                 timeout=secs + 30)
                except Exception as e:
                    log("Zustimmungsdialog fehlgeschlagen: %s" % e)
                try:
                    send(sock, b"R" + json.dumps({"consent": bool(ok)}).encode())
                except Exception:
                    pass
            elif c == "session-open":
                # Sitzungsfenster im Oberflaechen-Thread aufbauen.
                old = state.get("sesswin")
                if old is not None and old.closed:
                    state["sesswin"] = old = None
                if old is None:
                    def _reply(obj):
                        send(sock, b"R" + json.dumps(obj).encode())
                    try:
                        who, perms = o.get("who") or ht("somebody"), o.get("perms") or {}
                        state["sesswin"] = ui_call(
                            lambda: SessionWindow(_UI["root"], who, _reply, perms), timeout=15)
                    except Exception as e:
                        log("Sitzungsfenster-Start: %s" % e)
            elif c == "session-chat":
                sess_do("on_admin_chat", o.get("text") or "")
            elif c == "session-request-grant":
                sess_do("on_admin_request")
            elif c == "audio":
                state["audio"] = bool(o.get("enabled"))
                sess_do("on_audio", state["audio"])
            elif c == "file-chunk":
                ack = {"session": "file-ack", "tid": o.get("tid"), "seq": o.get("seq")}
                try:
                    ack.update(receive_chunk(o))
                except Exception as e:
                    log("Datei empfangen: %s" % e)
                    ack["error"] = str(e)
                    st = state["files"].pop(o.get("tid"), None)
                    if st:
                        try: st["f"].close(); os.remove(st["path"])
                        except Exception: pass
                try:
                    send(sock, b"R" + json.dumps(ack).encode())
                except Exception:
                    pass
            elif c == "session-close":
                sess_do("close")
                state["sesswin"] = None
            elif c == "stop":
                sess_do("close")
                state["stop"] = True
                break
        state["stop"] = True

    threading.Thread(target=control, daemon=True).start()
    cap = threading.Thread(target=capture, args=(sock, state), daemon=True)
    cap.start()
    threading.Thread(target=audio_loop, args=(sock, state), daemon=True).start()
    # Haupt-Thread = Oberflaeche (Tk mag den Haupt-Thread am liebsten). Ohne Tk
    # laeuft der Helfer einfach ohne Fenster weiter, bis der Agent ihn beendet.
    if not ui_loop(state):
        while not state["stop"]:
            time.sleep(0.3)
    state["stop"] = True
    cap.join(timeout=3)
    log("Ende")


def audio_loop(sock, state):
    # Ton des PCs mitschicken (wie AnyDesk "Ton uebertragen"): Aufnahme dessen,
    # was aus den Lautsprechern kommt (Loopback), 16 kHz mono, als 8-Bit-mu-law
    # (~16 KB/s). Laeuft nur, solange der Admin den Ton eingeschaltet hat.
    # Braucht das Paket "soundcard" (+ numpy); fehlt es, meldet der Helfer das
    # einmal und der Admin sieht "Ton nicht verfuegbar".
    RATE, BLOCK = 16000, 1600            # 100 ms je Stueck
    def status(**kw):
        try:
            send(sock, b"R" + json.dumps(dict({"session": "audio"}, **kw)).encode())
        except Exception:
            pass
    while not state["stop"]:
        if not state.get("audio"):
            time.sleep(0.2)
            continue
        try:
            if sys.platform == "win32":
                import ctypes
                ctypes.windll.ole32.CoInitializeEx(None, 0)   # COM fuer WASAPI in DIESEM Thread
            import numpy as np
            import soundcard as sc
            spk = sc.default_speaker()
            mic = sc.get_microphone(id=str(spk.name), include_loopback=True)
        except Exception as e:
            log("Ton nicht verfuegbar: %s" % e)
            status(error=str(e), code="agent_audio_unavailable")
            state["audio"] = False
            continue
        log("Ton an (%s)" % getattr(spk, "name", "?"))
        status(state="on")
        mu = 255.0
        try:
            with mic.recorder(samplerate=RATE, channels=1, blocksize=BLOCK) as rec:
                while state.get("audio") and not state["stop"]:
                    data = rec.record(numframes=BLOCK)
                    x = np.clip(np.asarray(data, dtype=np.float32).reshape(-1), -1.0, 1.0)
                    y = np.sign(x) * np.log1p(mu * np.abs(x)) / np.log1p(mu)
                    send(sock, b"A" + ((y + 1.0) * 127.5 + 0.5).astype(np.uint8).tobytes())
        except Exception as e:
            log("Tonaufnahme: %s" % e)
            status(error=str(e), code="agent_audio_failed")
            state["audio"] = False
            time.sleep(1.0)
            continue
        log("Ton aus")
        status(state="off")


def capture(sock, state):
    # Bildschirmaufnahme in einem eigenen Thread (mss-Objekt wird hier erzeugt
    # und nur hier benutzt - mss haelt Windows-Handles pro Thread).
    try:
        import mss
        from PIL import Image
    except Exception as e:
        log("mss/Pillow fehlen: %s" % e)
        try:
            send(sock, b"F" + json.dumps({"error": "mss/Pillow fehlen: %s" % e}).encode() + b"\0")
        except Exception:
            pass
        return

    # Robust gegen kurze Aussetzer: Sperrbildschirm, UAC-Abfrage (sicherer
    # Desktop), Aufloesungs- oder Monitorwechsel lassen einzelne Aufnahmen
    # scheitern. Frueher beendete schon EIN Fehler die ganze Uebertragung - das
    # Bild blieb nach wenigen Dutzend Bildern einfach stehen. Jetzt wird die
    # Aufnahme neu aufgesetzt und erst nach anhaltenden Fehlern (~15 s) aufgegeben.
    sct = None
    errors = 0
    frames_ok = 0            # >0 = Sitzung lief schon -> Fehler sind voruebergehend
    paused = False
    last_crc = None
    last_frame = 0.0
    last_send = time.time()
    import zlib
    def _keepalive():
        nonlocal last_send
        if time.time() - last_send >= 4.0:
            try:
                send(sock, b"K")          # Lebenszeichen - haelt die Leitung wach
                last_send = time.time()
            except Exception:
                state["stop"] = True
    def _status(st, code="", detail=""):
        try:
            send(sock, b"S" + json.dumps({"state": st, "code": code, "detail": str(detail)[:300]}).encode())
        except Exception:
            state["stop"] = True
    # --- Auto-Sync der Zwischenablage (Benutzersitzung -> Dashboard) ---
    # Direkt per ctypes gelesen (CF_UNICODETEXT), damit kein Umweg ueber den
    # Tk-Oberflaechen-Thread noetig ist. Ob der Text ein Echo eines vom
    # Dashboard gesetzten Inhalts ist, filtert der AGENT (suppress-Abgleich).
    clip = {"last": None, "ts": 0.0, "first": True}
    def _clip_read():
        import ctypes
        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        if not u32.OpenClipboard(0):
            return None
        try:
            h = u32.GetClipboardData(13)       # CF_UNICODETEXT
            if not h:
                return ""
            p = k32.GlobalLock(h)
            if not p:
                return ""
            try:
                return ctypes.c_wchar_p(p).value or ""
            finally:
                k32.GlobalUnlock(h)
        finally:
            u32.CloseClipboard()
    def _clip_poll():
        now = time.time()
        if now - clip["ts"] < 1.5:
            return
        clip["ts"] = now
        try:
            text = _clip_read()
        except Exception:
            return
        if text is None:
            return
        if clip["first"]:
            clip["last"] = text                # Bestand beim Start nicht pushen
            clip["first"] = False
            return
        if text and text != clip["last"]:
            clip["last"] = text
            if len(text) <= 512 * 1024:
                try:
                    send(sock, b"R" + json.dumps({"session": "clip", "text": text}).encode())
                except Exception:
                    pass

    while not state["stop"]:
        if not state["stream"]:
            _keepalive()
            time.sleep(0.15)
            continue
        _clip_poll()
        try:
            if sct is None:
                sct = mss.mss()
            mons = sct.monitors
            count = max(1, len(mons) - 1)
            idx = state["monitor"] if 0 <= state["monitor"] <= count else 1
            mon = mons[idx]
            state["left"], state["top"] = mon.get("left", 0), mon.get("top", 0)
            shot = sct.grab(mon)
            errors = 0
            if paused:
                paused = False
                _status("live")
            # Unveraendertes Bild nicht erneut schicken (alle 3 s trotzdem eins).
            crc = zlib.crc32(shot.bgra)
            now = time.time()
            if crc == last_crc and not state.get("force") and now - last_frame < 3.0:
                _keepalive()
                time.sleep(1.0 / max(1, min(30, state["fps"])))
                continue
            state["force"] = False
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            # Gleiche Regel wie im direkten Aufnahmeweg des Agenten:
            # hohe Qualitaet -> bis 1920 px, sonst 1280 px.
            q = max(10, min(95, state["quality"]))
            _fw = int(state.get("width", 0) or 0)
            max_width = _fw if _fw > 0 else (1920 if q >= 70 else 1280)
            if img.width > max_width:
                ratio = max_width / img.width
                img = img.resize((max_width, int(img.height * ratio)))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=q)
            # Gemeldet wird die ECHTE Bildschirmgroesse (nicht die des
            # verkleinerten JPEG): Das Dashboard rechnet Mausklicks damit zurueck.
            meta = {"width": shot.width, "height": shot.height, "index": idx,
                    "count": count, "left": state["left"], "top": state["top"]}
            payload = b"F" + json.dumps(meta).encode() + b"\0" + buf.getvalue()
            last_crc = crc
            last_frame = now
        except Exception as e:
            errors += 1
            if errors in (1, 10) or errors % 50 == 0:
                log("Aufnahme (Versuch %d): %s" % (errors, e))
            try:
                if sct is not None:
                    sct.close()
            except Exception:
                pass
            sct = None
            last_crc = None
            if frames_ok > 0:
                # Sitzung lief schon: Sperrbildschirm, UAC, Benutzerwechsel ...
                # NICHT aufgeben, sondern pausieren und weiter versuchen.
                if not paused:
                    paused = True
                    _status("paused", "agent_screen_paused", e)
                _keepalive()
                time.sleep(min(2.0, 0.3 * errors))
                continue
            if errors >= 50:
                try:
                    send(sock, b"F" + json.dumps({"error": str(e)}).encode() + b"\0")
                except Exception:
                    pass
                break
            _keepalive()           # trotz Aufnahmefehler am Leben bleiben
            time.sleep(0.3)
            continue
        try:
            send(sock, payload)
            last_send = time.time()
            frames_ok += 1
        except Exception as e:
            log("Verbindung zum Agenten weg: %s" % e)
            state["stop"] = True
            break
        time.sleep(1.0 / max(1, min(30, state["fps"])))
    try:
        if sct is not None:
            sct.close()
    except Exception:
        pass
    log("Aufnahme beendet")

try:
    main()
except Exception:
    log(traceback.format_exc())
"""


def _write_helper_script() -> str:
    """Schreibt das Helfer-Skript neben agent.py und gibt den Pfad zurueck.
    Der Agent laeuft als SYSTEM und darf dort schreiben; der Benutzer darf
    lesen und ausfuehren - genau das wird gebraucht."""
    path = Path(__file__).resolve().parent / _HELPER_FILENAME
    try:
        if not path.is_file() or path.read_text(encoding="utf-8") != _HELPER_SOURCE:
            path.write_text(_HELPER_SOURCE, encoding="utf-8")
    except OSError as e:
        _print(f"[screen-helper] script not writable ({e}) - using TEMP")
        import tempfile
        path = Path(tempfile.gettempdir()) / _HELPER_FILENAME
        path.write_text(_HELPER_SOURCE, encoding="utf-8")
    return str(path)



def _win_session_id() -> int:
    """Sitzungs-ID dieses Prozesses (-1, wenn nicht ermittelbar)."""
    if platform.system() != "Windows":
        return -1
    try:
        import ctypes
        from ctypes import wintypes
        sid = wintypes.DWORD()
        ok = ctypes.windll.kernel32.ProcessIdToSessionId(
            ctypes.windll.kernel32.GetCurrentProcessId(), ctypes.byref(sid))
        return sid.value if ok else -1
    except Exception:
        return -1


def _win_console_session_id() -> int:
    """
    ID der aktiven Konsolensitzung (die Sitzung mit Bildschirm/Tastatur).
    Nur fuer die Diagnose: Weicht sie von der eigenen Sitzung ab, sitzt der
    Agent in einer Sitzung OHNE sichtbaren Desktop - genau die Lage, in der
    BitBlt scheitert. -1, wenn nicht ermittelbar.
    """
    if platform.system() != "Windows":
        return -1
    try:
        import ctypes
        sid = ctypes.windll.kernel32.WTSGetActiveConsoleSessionId()
        return -1 if sid == 0xFFFFFFFF else int(sid)
    except Exception:
        return -1


# Sitzung, in der zuletzt ein Helfer gestartet wurde. Der Start laeuft ueber
# mehrere Funktionen (Prozessstart, dann Socket-Annahme), deshalb hier abgelegt
# statt durchgereicht.
_LAST_HELPER_SESSION = [None]

# Vom Dashboard gewaehlte Windows-Sitzung (Mehrbenutzer: "erst einen Benutzer
# waehlen"). None = automatisch die am besten geeignete Sitzung. Gilt fuer
# Zustimmungsabfrage UND Aufnahme/Steuerung, damit beides denselben Benutzer
# trifft.
_PREFERRED_SESSION = [None]


def _set_preferred_session(sid):
    try:
        _PREFERRED_SESSION[0] = int(sid) if sid is not None and str(sid) != "" else None
    except (TypeError, ValueError):
        _PREFERRED_SESSION[0] = None


def _win_session_user(session_id: int) -> str:
    """Anmeldename (DOMAENE\\Benutzer) einer Windows-Sitzung - '' wenn unbekannt."""
    if platform.system() != "Windows":
        return ""
    try:
        import ctypes
        from ctypes import wintypes
        wts = ctypes.windll.wtsapi32

        def q(info_class):
            buf = ctypes.c_void_p()
            n = wintypes.DWORD(0)
            if not wts.WTSQuerySessionInformationW(0, wintypes.DWORD(session_id), info_class,
                                                   ctypes.byref(buf), ctypes.byref(n)):
                return ""
            try:
                return ctypes.wstring_at(buf) if buf.value else ""
            finally:
                wts.WTSFreeMemory(buf)
        user = q(5)        # WTSUserName
        if not user:
            return ""
        domain = q(7)      # WTSDomainName
        return f"{domain}\\{user}" if domain else user
    except Exception:
        return ""


def _win_logged_in_users() -> list:
    """Alle Windows-Sitzungen mit angemeldetem Benutzer - fuer die Auswahl im
    Dashboard. Jeweils {session, user, state, console, active}. Reihenfolge wie
    _win_user_sessions() (am besten geeignete zuerst)."""
    out = []
    try:
        console = _win_console_session_id()
        active = set(_win_user_sessions_by_state(0))
        for sid in _win_user_sessions():
            user = _win_session_user(sid)
            if not user:
                continue
            out.append({"session": sid, "user": user,
                        "state": "active" if sid in active else "disconnected",
                        "console": sid == console, "active": sid in active})
    except Exception as e:
        _print(f"[screen] Benutzerliste nicht lesbar: {e}")
    return out


def _win_user_sessions_by_state(want_state: int) -> list:
    """Sitzungs-IDs mit genau diesem WTS-State (0=aktiv, 4=getrennt)."""
    if platform.system() != "Windows":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        class _SI(ctypes.Structure):
            _fields_ = [("SessionId", wintypes.DWORD), ("pWinStationName", wintypes.LPWSTR),
                        ("State", ctypes.c_int)]
        wts = ctypes.windll.wtsapi32
        count = wintypes.DWORD(0)
        arr = ctypes.POINTER(_SI)()
        if not wts.WTSEnumerateSessionsW(0, 0, 1, ctypes.byref(arr), ctypes.byref(count)):
            return []
        try:
            return [int(arr[i].SessionId) for i in range(count.value)
                    if int(arr[i].SessionId) != 0 and int(arr[i].State) == want_state]
        finally:
            try:
                wts.WTSFreeMemory(arr)
            except Exception:
                pass
    except Exception:
        return []


def _win_user_sessions() -> list[int]:
    """
    Alle Windows-Sitzungen, in denen ein Benutzer angemeldet ist - sortiert
    nach Eignung fuer eine Bildschirmaufnahme.

    Warum nicht einfach WTSGetActiveConsoleSessionId()? Weil das NUR die
    physische Konsole liefert (Monitor/Tastatur am Geraet). Bei einer
    RDP-Verbindung - auch ueber das Guacamole-Gateway des RMM - hat der
    Benutzer eine eigene Sitzung, und die Konsole ist leer. Der Helfer wurde
    dann nie gestartet, obwohl jemand angemeldet war.

    Reihenfolge:
      1. WTSActive  (angemeldet UND verbunden - hier ist wirklich ein Desktop)
      2. WTSDisconnected (angemeldet, aber getrennt - Desktop existiert weiter,
         die Aufnahme liefert das zuletzt gezeigte Bild)
      3. Konsolensitzung zuerst innerhalb derselben Stufe, weil sie am
         ehesten einen echten Monitor hat.
    Sitzung 0 bleibt immer aussen vor - dort laufen die Dienste, kein Desktop.
    """
    if platform.system() != "Windows":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        class _WTS_SESSION_INFOW(ctypes.Structure):
            _fields_ = [("SessionId", wintypes.DWORD),
                        ("pWinStationName", wintypes.LPWSTR),
                        ("State", ctypes.c_int)]

        wts = ctypes.windll.wtsapi32
        count = wintypes.DWORD(0)
        arr = ctypes.POINTER(_WTS_SESSION_INFOW)()
        # 0 = WTS_CURRENT_SERVER_HANDLE
        if not wts.WTSEnumerateSessionsW(0, 0, 1, ctypes.byref(arr), ctypes.byref(count)):
            return []
        try:
            console = _win_console_session_id()
            active, disconnected = [], []
            for i in range(count.value):
                info = arr[i]
                sid, state = int(info.SessionId), int(info.State)
                if sid == 0:
                    continue                    # Dienst-Sitzung, kein Desktop
                if state == 0:                  # WTSActive
                    active.append(sid)
                elif state == 4:                # WTSDisconnected
                    disconnected.append(sid)
            # Konsolensitzung innerhalb ihrer Stufe nach vorne holen.
            key = lambda x: (x != console, x)   # noqa: E731
            ordered = sorted(active, key=key) + sorted(disconnected, key=key)
            # Hat das Dashboard einen Benutzer gewaehlt (Mehrbenutzer), kommt
            # dessen Sitzung nach vorne - sofern sie noch existiert.
            pref = _PREFERRED_SESSION[0]
            if pref is not None and pref in ordered:
                ordered = [pref] + [x for x in ordered if x != pref]
            return ordered
        finally:
            try:
                wts.WTSFreeMemory(arr)
            except Exception:
                pass
    except Exception as e:
        _print(f"[screen-helper] Sitzungsliste nicht lesbar: {e}")
        # Rueckfallebene: wenigstens die Konsolensitzung versuchen.
        cid = _win_console_session_id()
        return [cid] if cid > 0 else []


def _needs_session_helper() -> bool:
    """Muessen wir ueber einen Helfer gehen? Nur Windows, nur aus Sitzung 0."""
    if platform.system() != "Windows":
        return False
    if os.environ.get("RMM_SCREEN_HELPER") == "1":
        return False          # wir SIND der Helfer
    return _win_session_id() == 0


def _win_start_in_user_session(args: list) -> bool:
    """
    Startet ein Programm in der Sitzung des angemeldeten Benutzers.

    Der Weg ist der uebliche fuer Dienste: Token der aktiven Konsolensitzung
    holen (WTSQueryUserToken), duplizieren, Umgebungsblock erzeugen und den
    Prozess auf dem sichtbaren Desktop "winsta0\\default" starten.
    Voraussetzung ist, dass wir als SYSTEM laufen - sonst fehlt das Recht.
    """
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.windll.kernel32
        advapi = ctypes.windll.advapi32
        wtsapi = ctypes.windll.wtsapi32
        userenv = ctypes.windll.userenv

        # Sitzung suchen, in der wirklich jemand angemeldet ist.
        #
        # WICHTIG: Frueher stand hier nur WTSGetActiveConsoleSessionId(). Das
        # liefert ausschliesslich die PHYSISCHE Konsolensitzung (Monitor +
        # Tastatur am Geraet). Wer sich per RDP - und damit auch ueber das
        # Guacamole-Gateway des RMM - verbindet, sitzt aber in einer EIGENEN
        # Sitzung; die Konsole ist dann leer und liefert 0 oder die ID einer
        # Sitzung ohne Benutzer. Ergebnis: "Keine aktive Benutzersitzung
        # gefunden", obwohl jemand angemeldet ist. Genau dieser Fall.
        #
        # Deshalb werden jetzt ALLE Sitzungen durchgegangen und die erste
        # genommen, fuer die sich ein Benutzertoken holen laesst.
        candidates = _win_user_sessions()
        if not candidates:
            _print("[screen-helper] Keine Sitzung mit angemeldetem Benutzer gefunden "
                   "(niemand angemeldet?)")
            return False

        session = None
        token = wintypes.HANDLE()
        last_err = 0
        for cand in candidates:
            if wtsapi.WTSQueryUserToken(wintypes.DWORD(cand), ctypes.byref(token)):
                session = cand
                break
            last_err = ctypes.GetLastError()
            _print(f"[screen-helper] Sitzung {cand}: WTSQueryUserToken "
                   f"fehlgeschlagen (error {last_err})")
        if session is None:
            _print(f"[screen-helper] Kein Benutzertoken erhalten "
                   f"(zuletzt error {last_err}) - laeuft der Agent als SYSTEM?")
            return False
        _print(f"[screen-helper] Nutze Sitzung {session} von {candidates}")
        _LAST_HELPER_SESSION[0] = session

        dup = wintypes.HANDLE()
        # 2 = SecurityImpersonation, 1 = TokenPrimary
        if not advapi.DuplicateTokenEx(token, 0x02000000, None, 2, 1, ctypes.byref(dup)):
            _print(f"[screen-helper] DuplicateTokenEx fehlgeschlagen "
                   f"(error {ctypes.GetLastError()})")
            k32.CloseHandle(token)
            return False
        k32.CloseHandle(token)

        env = ctypes.c_void_p()
        userenv.CreateEnvironmentBlock(ctypes.byref(env), dup, False)

        class STARTUPINFOW(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                        ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                        ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                        ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                        ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                        ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
                        ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
                        ("hStdError", wintypes.HANDLE)]

        class PROCESS_INFORMATION(ctypes.Structure):
            _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                        ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

        si = STARTUPINFOW()
        si.cb = ctypes.sizeof(si)
        si.lpDesktop = "winsta0\\default"     # der sichtbare Desktop
        si.dwFlags = 0x00000001               # STARTF_USESHOWWINDOW
        si.wShowWindow = 0                    # SW_HIDE
        pi = PROCESS_INFORMATION()

        cmdline = " ".join(f'"{a}"' for a in args)
        CREATE_UNICODE_ENVIRONMENT = 0x00000400
        CREATE_NO_WINDOW = 0x08000000
        ok = advapi.CreateProcessAsUserW(
            dup, None, ctypes.create_unicode_buffer(cmdline), None, None, False,
            CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW, env,
            str(Path(args[1]).resolve().parent) if len(args) > 1 else None,
            ctypes.byref(si), ctypes.byref(pi))
        if not ok:
            _print(f"[screen-helper] CreateProcessAsUser fehlgeschlagen "
                   f"(error {ctypes.GetLastError()})")
            return False
        _print(f"[screen-helper] helper started in session {session} "
               f"(PID {pi.dwProcessId})")
        k32.CloseHandle(pi.hProcess)
        k32.CloseHandle(pi.hThread)
        return True
    except Exception as e:
        _print(f"[screen-helper] Start fehlgeschlagen: {e}")
        return False


def _sock_send(sock, payload: bytes) -> None:
    sock.sendall(len(payload).to_bytes(4, "big") + payload)


def _sock_recv(sock) -> bytes | None:
    def _exact(n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf
    head = _exact(4)
    if not head:
        return None
    return _exact(int.from_bytes(head, "big"))


class _ScreenHelper:
    """Verbindung des Agenten (Sitzung 0) zu seinem Helfer in der Benutzersitzung."""

    def __init__(self, server, conn):
        self.server = server
        self.conn = conn
        self.alive = True
        # In welcher Windows-Sitzung laeuft dieser Helfer? _ensure_helper()
        # vergleicht das mit der aktuellen Benutzersitzung.
        self.session = None
        # Der Socket wird von ZWEI Threads benutzt: der Event-Loop schickt
        # Eingaben (jede Mausbewegung!), der Aufnahme-Thread liest Bilder und
        # schickt Konfigurationen. Ohne Schloss ueberlappen zwei sendall() und
        # zerstoeren die Laengen-Praefixe -> der Helfer liest Muell, der
        # Steuerkanal stirbt lautlos. Genau so fiel die Fernsteuerung aus.
        self._send_lock = threading.Lock()

    def _readable(self, timeout: float) -> bool:
        """Wartet mit select() darauf, dass Daten anliegen.
        Wichtig: NICHT socket.settimeout() benutzen. Das gilt fuer den ganzen
        Socket - also auch fuer sendall() im anderen Thread, das dann mitten
        in einer Nachricht abbrechen kann."""
        import select as _select
        try:
            r, _w, _e = _select.select([self.conn], [], [], max(0.0, timeout))
            return bool(r)
        except Exception:
            return False

    @classmethod
    def start(cls, loop, timeout: float = 25.0):
        import secrets
        import socket as _socket

        server = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        server.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))       # nur lokal, freier Port
        server.listen(1)
        server.settimeout(timeout)
        port = server.getsockname()[1]
        key = secrets.token_hex(16)

        # NICHT agent.py erneut starten - siehe Kommentar bei _HELPER_SOURCE.
        script = _write_helper_script()
        exe = sys.executable
        # Die Sprache wird als drittes Argument mitgegeben - der Helfer laeuft in
        # einem eigenen Prozess und liest die .env des Agenten nicht.
        cmd = [exe, script, str(port), key, AGENT_LANG]
        if IS_WINDOWS:
            # Dienst in Sitzung 0 -> Helfer in die Benutzersitzung bringen.
            if not _win_start_in_user_session(cmd):
                server.close()
                return None
        else:
            # Linux/macOS: Der Agent laeuft bereits in der grafischen Sitzung des
            # Benutzers (DISPLAY/XAUTHORITY gesetzt). Der Helfer erbt die Umgebung
            # und kann so das Chat-/Rechte-Fenster anzeigen.
            if not os.environ.get("DISPLAY") and sys.platform != "darwin":
                _print("[screen-helper] kein DISPLAY - Chat-/Rechte-Fenster nicht möglich")
                server.close()
                return None
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
            except Exception as e:
                _print(f"[screen-helper] Helfer-Start (Linux) fehlgeschlagen: {e}")
                server.close()
                return None

        try:
            conn, _addr = server.accept()
        except Exception as e:
            _print(f"[screen-helper] helper did not report in: {e}")
            server.close()
            return None
        conn.settimeout(20.0)
        try:
            hello = _sock_recv(conn)
            if not hello or json.loads(hello.decode()).get("key") != key:
                _print("[screen-helper] wrong key - connection discarded")
                conn.close()
                server.close()
                return None
        except Exception as e:
            _print(f"[screen-helper] Anmeldung fehlgeschlagen: {e}")
            conn.close()
            server.close()
            return None
        _print("[screen-helper] Helfer verbunden.")
        # Ab jetzt blockierend: Wartezeiten regelt select() pro Leserichtung,
        # damit ein Timeout nie ein laufendes sendall() zerreisst.
        try:
            conn.settimeout(None)
        except Exception:
            pass
        obj = cls(server, conn)
        # Sitzung merken: _ensure_helper() vergleicht sie spaeter mit der
        # aktuellen Benutzersitzung und startet den Helfer neu, wenn der
        # Benutzer inzwischen woanders sitzt (Konsole <-> RDP).
        obj.session = _LAST_HELPER_SESSION[0]
        return obj

    def send(self, obj) -> bool:
        try:
            with self._send_lock:
                _sock_send(self.conn, b"C" + json.dumps(obj).encode())
            return True
        except Exception:
            self.alive = False
            return False

    def recv(self, timeout: float | None = None):
        """
        Naechste Nachricht vom Helfer.
        Rueckgabe: ("frame", meta, jpeg) | ("reply", obj, None) | None
        """
        try:
            if timeout is not None and not self._readable(timeout):
                return None
            msg = _sock_recv(self.conn)
        except Exception:
            return None
        if not msg:
            # Gelesen werden konnte, aber nichts kam: Gegenseite hat die
            # Verbindung geschlossen. Frueher wie "Zeitlimit" behandelt ->
            # ping() klappte auf dem halb offenen Socket und die Schleife
            # drehte ohne Bilder endlos weiter.
            return ("eof", None, None) if msg is None else None
        if msg[:1] == b"S":
            try:
                return "status", json.loads(msg[1:].decode()), None
            except Exception:
                return "unknown", None, None
        if msg[:1] == b"F":
            sep = msg.index(b"\0", 1)
            return "frame", json.loads(msg[1:sep].decode()), msg[sep + 1:]
        if msg[:1] == b"R":
            return "reply", json.loads(msg[1:].decode()), None
        if msg[:1] == b"A":
            return "audio", None, msg[1:]          # Ton: mu-law, mono, 16 kHz
        if msg[:1] == b"K":
            return "keepalive", None, None         # Lebenszeichen des Helfers
        return "unknown", None, None               # nicht als "Verbindung weg" werten

    def ping(self) -> bool:
        """Aktiver Verbindungstest: ein Ping an den Helfer. True = Leitung ok.
        Der Helfer ignoriert unbekannte Steuerbefehle, es reicht, dass das
        Senden nicht scheitert (sonst ist der Socket wirklich tot)."""
        try:
            self.send({"cmd": "ping"})
            return True
        except Exception:
            return False

    def drain(self, seconds: float = 1.5) -> None:
        """
        Restnachrichten der VORHERIGEN Sitzung wegwerfen.
        Nach einem Stop liegen oft noch Bilder im Socket-Puffer. Wurden die
        nicht abgeraeumt, verbrauchte die naechste Zustimmungsabfrage ihre
        Wartezeit mit dem Lesen alter Frames - im Dashboard stand dann
        'abgelehnt', obwohl niemand etwas angeklickt hatte.
        """
        end = time.time() + seconds
        while time.time() < end:
            if not self._readable(0.2):
                return          # nichts mehr da - fertig
            try:
                if _sock_recv(self.conn) is None:
                    self.alive = False
                    return
            except Exception:
                self.alive = False
                return

    def ask_consent(self, who: str, timeout_s: int):
        """Zustimmungsdialog IN der Benutzersitzung anzeigen lassen.
        Aus Sitzung 0 ist ein Fenster fuer den Benutzer unsichtbar - genau
        deshalb kam bisher 'Warte auf Bestaetigung', ohne dass am Bildschirm
        etwas erschien.

        Rueckgabe: True = zugelassen, False = abgelehnt/Zeit abgelaufen,
        None = Verbindung zum Helfer gestoert. Nur bei None darf der Aufrufer
        den Helfer neu starten und es erneut versuchen - eine echte Ablehnung
        wird NICHT wiederholt.
        """
        # Stream sicher anhalten und Reste abraeumen, damit die Antwort des
        # Dialogs nicht hinter alten Bildern haengt.
        self.send({"cmd": "config", "stream": False})
        self.drain()
        if not self.alive:
            return None
        if not self.send({"cmd": "consent", "who": who, "timeout": int(timeout_s)}):
            return None
        deadline = time.time() + timeout_s + 15
        while time.time() < deadline:
            got = self.recv(timeout=max(1.0, deadline - time.time()))
            if got is None:
                self.alive = False
                return None      # Leitung tot -> KEINE stille Ablehnung
            kind, obj, _ = got
            if kind == "eof":
                self.alive = False
                return None      # Helfer weg -> Aufrufer darf neu starten
            if kind == "reply" and "consent" in obj:
                return bool(obj["consent"])
        return False

    def close(self):
        self.alive = False
        for s in (self.conn, self.server):
            try:
                s.close()
            except Exception:
                pass


def _screen_helper_loop(loop, helper) -> bool:
    """Nimmt die Bilder vom Helfer entgegen und schickt sie ans Dashboard.
    Rueckgabe True = der Helfer ist WAEHREND der Sitzung weggebrochen (der
    Aufrufer startet ihn neu), False = regulaer beendet oder Fehler gemeldet.

    Die Bilder laufen ueber _FLOW (Flusskontrolle): Ist die Leitung voll, wird
    der Helfer NICHT blockiert (Chat/Rechte-Antworten laufen ueber denselben
    Socket), sondern nur das neueste Bild gemerkt. Qualitaet/Breite des Helfers
    folgen der Qualitaetsstufe der Flusskontrolle."""
    def _cfg_tuple():
        return (_FLOW.quality(int(_screen_stream.get("quality", 60))),
                int(_screen_stream.get("fps", 10)),
                int(_screen_stream.get("monitor", 1) or 1),
                _FLOW.max_width(int(_screen_stream.get("width", 0) or 0)))

    def _hcfg(cfg):
        q, fps, mon, w = cfg
        return {"cmd": "config", "stream": True, "quality": q, "fps": fps,
                "monitor": mon, "width": w}

    _FLOW.reset()
    last_cfg = _cfg_tuple()
    helper.send(_hcfg(last_cfg))
    helper.send({"cmd": "refresh"})
    while _screen_stream["active"] and helper.alive:
        cfg = _cfg_tuple()
        if cfg != last_cfg:
            helper.send(_hcfg(cfg))
            last_cfg = cfg
        if _screen_stream.get("force"):
            _screen_stream["force"] = False
            helper.send({"cmd": "refresh"})
        got = helper.recv(timeout=15.0)
        if got is None:
            # 15 s GANZ still (der Helfer sendet sonst alle ~4 s ein Lebens-
            # zeichen): Verbindung aktiv pruefen statt sofort aufzugeben.
            if helper.ping():
                continue
            helper.alive = False
            break
        kind, meta, jpeg = got
        if kind == "eof":
            helper.alive = False
            break
        if kind == "keepalive":
            continue
        if kind == "status":
            st = (meta or {}).get("state") or ""
            _screen_status(loop, st, (meta or {}).get("code") or "", (meta or {}).get("detail") or "")
            continue
        if kind == "reply" and isinstance(meta, dict) and meta.get("session"):
            _handle_session_reply(loop, meta)
            continue
        if kind == "audio":
            _session_emit(loop, "screen-audio", {"data": base64.b64encode(jpeg).decode(),
                                                 "rate": 16000, "codec": "mulaw8"})
            continue
        if kind != "frame":
            continue
        if meta.get("error"):
            _notify_screen_error(loop, f"Helper reports: {meta['error']}",
                                 code="agent_err_helper", params={"err": meta["error"]})
            _screen_stream["active"] = False
            return False
        _screen_stream["mon_left"] = meta.get("left", 0)
        _screen_stream["mon_top"] = meta.get("top", 0)
        if not sio.connected:
            _FLOW.reset()                # ACKs der alten Verbindung kommen nie mehr
            continue                     # Backend gerade weg - Bild verwerfen
        _FLOW.offer({
            "id": DEVICE_ID, "image": base64.b64encode(jpeg).decode(),
            "width": meta.get("width"), "height": meta.get("height"),
            "monitor_index": meta.get("index", 0),
            "monitor_count": meta.get("count", 1),
        })
    died = bool(_screen_stream["active"] and not helper.alive)
    if not died:
        _screen_stream["active"] = False
    _print("[screen-helper] transfer finished." + (" (helper lost)" if died else ""))
    return died


def _ensure_helper(loop):
    """
    Verbindung zum Helfer in der Benutzersitzung holen (oder aufbauen).
    Wird sowohl fuer die Zustimmungsabfrage als auch fuer die Aufnahme
    gebraucht - deshalb an einer Stelle gebuendelt.
    """
    helper = _screen_stream.get("helper")
    if helper is not None and helper.alive:
        # Sitzt der laufende Helfer noch in der RICHTIGEN Sitzung?
        # Wechselt der Benutzer zwischen Konsole und RDP (oder meldet sich neu
        # an), bekommt er eine ANDERE Sitzungs-ID. Der alte Helfer laeuft dann
        # zwar noch, zeichnet aber einen Desktop auf, den niemand mehr sieht -
        # und sein Zustimmungsdialog erscheint auf einem unsichtbaren
        # Bildschirm. Deshalb hier vergleichen und ihn notfalls ersetzen.
        if not IS_WINDOWS:
            return helper                      # Linux/macOS: ein Helfer, keine Sitzungs-IDs
        want = _win_user_sessions()
        have = getattr(helper, "session", None)
        if not want or have is None or have == want[0]:
            return helper
        _print(f"[screen-helper] Benutzer ist jetzt in Sitzung {want[0]}, "
               f"der Helfer laeuft in {have} - starte ihn neu.")
        try:
            helper.close()
        except Exception:
            pass
        _screen_stream["helper"] = None
    helper = _ScreenHelper.start(loop)
    _screen_stream["helper"] = helper
    return helper


def _screen_capture_loop(loop):
    """Läuft in einem eigenen Thread und schickt fortlaufend Bildschirm-Frames."""
    is_linux = platform.system() == "Linux"

    # Windows-Sonderfall: Der Agent läuft als geplante Aufgabe unter SYSTEM und
    # damit in Sitzung 0. Von dort ist der Desktop des angemeldeten Benutzers
    # grundsätzlich nicht erreichbar ("Session-0-Isolation"). Deshalb starten
    # wir einen Helfer IN der Benutzersitzung und lassen uns die Bilder von
    # dort schicken. Details siehe _ScreenHelper.
    if _needs_session_helper():
        helper = _ensure_helper(loop)
        if helper is None:
            _notify_screen_error(
                loop,
                "The agent runs as a service in session 0 and cannot capture the "
                "desktop from there. The helper for the user session did not "
                "report in. Check whether anyone is signed in on the device. The "
                "helper log is in the signed-in user's TEMP folder as "
                "rmm-screen-helper.log (type %TEMP% in Explorer). "
                f"[Sessions with a signed-in user: {_win_user_sessions() or 'none'}]",
                code="agent_err_no_helper",
                # Die gefundenen Sitzungen mitschicken: Ohne diese Angabe war
                # nicht zu unterscheiden, ob NIEMAND angemeldet ist oder ob der
                # Start des Helfers in einer gefundenen Sitzung scheiterte -
                # zwei voellig verschiedene Ursachen mit demselben Text.
                params={"sessions": ", ".join(str(x) for x in _win_user_sessions()) or "-"})
            _screen_stream["active"] = False
            return
        # Bricht der Helfer mitten in der Sitzung weg (Absturz, Abmeldung,
        # Bildschirmsperre mit Sitzungswechsel), wird er bis zu dreimal neu
        # gestartet und die Sitzung laeuft weiter - ohne neue Zustimmung, sie
        # wurde fuer DIESE Sitzung ja schon erteilt. Frueher blieb das Bild in
        # dem Fall einfach stehen, ohne jede Meldung.
        restarts = 0
        while True:
            try:
                died = _screen_helper_loop(loop, helper)
            finally:
                # Verbindung offen lassen: Die naechste Sitzung braucht sie fuer
                # die Zustimmungsabfrage sofort wieder. Nur die Uebertragung anhalten.
                if helper.alive:
                    helper.send({"cmd": "config", "stream": False})
            if not died or not _screen_stream["active"]:
                return
            restarts += 1
            if restarts > 3:
                _notify_screen_error(loop, "The screen helper in the user session stopped repeatedly.",
                                     code="agent_err_helper_lost", params={})
                _screen_stream["active"] = False
                return
            _print(f"[screen-helper] Helfer weggebrochen - Neustart {restarts}/3")
            try:
                helper.close()
            except Exception:
                pass
            _screen_stream["helper"] = None
            time.sleep(1.0)
            helper = _ensure_helper(loop)
            if helper is None:
                _notify_screen_error(loop, "The screen helper in the user session could not be restarted.",
                                     code="agent_err_helper_lost", params={})
                _screen_stream["active"] = False
                return
            if _session.get("active") and _session.get("window"):
                _open_session_window(_session.get("by") or "")

    try:
        sct = mss.mss()
    except Exception as e:
        # Kein Display erfassbar. Auf Linux/headless direkt auf Shell umschalten,
        # sonst normale Fehlermeldung (Windows -> RDP-Angebot im Dashboard).
        if is_linux:
            _notify_screen_mode(loop, "shell", f"Screen not capturable: {e}",
                                code="agent_mode_no_screen", params={"err": str(e)})
        else:
            _notify_screen_error(loop, f"Screen capture not possible: {e}",
                                 code="agent_err_capture", params={"err": str(e)})
        _screen_stream["active"] = False
        return

    # Verfügbare Einzel-Bildschirme: sct.monitors[0] ist die Gesamtfläche,
    # ab Index 1 die einzelnen Monitore. Anzahl = len - 1.
    mon_count = max(1, len(sct.monitors) - 1)
    cur_idx = -1
    monitor = None
    screen_w = screen_h = 0

    def _select_monitor(idx):
        nonlocal monitor, screen_w, screen_h, cur_idx
        # Index 0 = Gesamtfläche ALLER Monitore (mss.monitors[0]),
        # 1..mon_count = einzelne Bildschirme.
        idx = max(0, min(int(idx), mon_count))
        monitor = sct.monitors[idx]
        screen_w, screen_h = monitor["width"], monitor["height"]
        _screen_stream["mon_left"] = monitor.get("left", 0)
        _screen_stream["mon_top"] = monitor.get("top", 0)
        cur_idx = idx
        return idx

    _select_monitor(_screen_stream.get("monitor", 1))
    consecutive_errors = 0
    helper_retry_done = False   # Fallback auf die Benutzersitzung nur einmal
    frames_ok = 0               # >0 = die Sitzung lief schon -> Fehler sind voruebergehend
    paused = False
    last_crc = None
    last_sent = 0.0
    _FLOW.reset()
    import zlib as _zlib

    while _screen_stream["active"]:
        try:
            # Backend gerade nicht erreichbar: nichts aufnehmen, nur warten.
            # Die Sitzung bleibt bestehen und laeuft nach dem Reconnect weiter.
            if not sio.connected:
                # Offene Sende-Slots verwerfen: Die ACKs der alten Verbindung
                # kommen nie mehr an - ohne Reset waere die "Leitung" nach dem
                # Reconnect dauerhaft als voll markiert (eingefrorenes Bild).
                _FLOW.reset()
                time.sleep(0.5)
                continue
            # Leitung voll? Dann nicht sinnlos weiter kodieren - kurz warten.
            if _FLOW.congested():
                time.sleep(0.05)
                continue

            # Live-Wechsel: hat das Dashboard einen anderen Monitor gewählt?
            want = max(0, min(int(_screen_stream.get("monitor", 1)), mon_count))
            if want != cur_idx:
                _select_monitor(want)
                last_crc = None

            if sct is None:
                sct = mss.mss()
                mon_count = max(1, len(sct.monitors) - 1)
                _select_monitor(min(cur_idx if cur_idx >= 0 else 1, mon_count))
            raw = sct.grab(monitor)
            consecutive_errors = 0  # erfolgreich -> Fehlerzähler zurücksetzen
            if paused:
                paused = False
                _screen_status(loop, "live")

            # Unveraendertes Bild nicht erneut schicken (spart auf ruhigen
            # Bildschirmen fast die ganze Bandbreite). Alle 3 s trotzdem eins,
            # damit das Dashboard sieht, dass die Sitzung lebt.
            crc = _zlib.crc32(raw.bgra)
            now = time.time()
            force = bool(_screen_stream.get("force"))
            if crc == last_crc and not force and now - last_sent < 3.0:
                time.sleep(1.0 / max(1, min(int(_screen_stream.get("fps", 10)), 30)))
                continue
            _screen_stream["force"] = False

            img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
            _q_conf = max(10, min(int(_screen_stream.get("quality", 55)), 95))
            # Zielbreite: fest gewaehlt (bytera://.../resolution=WxH) hat Vorrang,
            # sonst automatisch nach Qualitaet (1920/1280). Die Flusskontrolle
            # senkt Qualitaet/Breite bei langsamer Leitung zusaetzlich.
            _fixed_w = int(_screen_stream.get("width", 0) or 0)
            max_width = _fixed_w if _fixed_w > 0 else (1920 if _q_conf >= 70 else 1280)
            max_width = _FLOW.max_width(max_width)
            if img.width > max_width:
                ratio = max_width / img.width
                img = img.resize((max_width, int(img.height * ratio)))

            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=_FLOW.quality(_q_conf))
            b64 = base64.b64encode(buffer.getvalue()).decode("ascii")

            _FLOW.offer({
                "id": DEVICE_ID,
                "image": b64,
                "width": screen_w,
                "height": screen_h,
                "monitor_index": cur_idx,      # aktuell gestreamter Monitor
                "monitor_count": mon_count,    # Anzahl verfügbarer Monitore
            })
            last_crc = crc
            last_sent = now
            frames_ok += 1
            _fps = int(_screen_stream.get("fps", 10))
            _fps = max(1, min(_fps, 30))
            time.sleep(1.0 / _fps)
        except Exception as e:
            consecutive_errors += 1
            msg = str(e)
            # Lief die Sitzung schon (es gab Bilder), ist ein Aufnahmefehler
            # fast immer voruebergehend: Sperrbildschirm (Win+L), UAC-Abfrage,
            # Aufloesungs-/Monitorwechsel, Benutzerwechsel. Frueher endete die
            # Sitzung nach 5 Fehlern - jetzt wird pausiert und weiter versucht,
            # bis jemand die Sitzung beendet.
            if frames_ok > 0:
                if not paused:
                    paused = True
                    _print(f"[agent] Aufnahme pausiert ({msg}) - versuche weiter")
                    _screen_status(loop, "paused", "agent_screen_paused", msg)
                try:
                    sct.close()
                except Exception:
                    pass
                sct = None
                time.sleep(min(2.0, 0.3 * consecutive_errors))
                continue
            # Linux/headless: kein Sinn weiterzuprobieren -> Shell anbieten.
            if is_linux:
                _notify_screen_mode(loop, "shell", f"Screen capture aborted: {msg}",
                                    code="agent_mode_aborted", params={"err": msg})
                _screen_stream["active"] = False
                break
            # Windows: typischer Fall headless/kein Desktop -> RDP-Angebot.
            if "denied" in msg.lower() or "bitblt" in msg.lower():
                # ZWEITER VERSUCH ueber die Benutzersitzung (getrennte RDP-
                # Sitzung, Task-Start "unabhaengig von der Anmeldung" usw.).
                # Nur EINMAL versuchen, sonst Endlosschleife.
                if IS_WINDOWS and not helper_retry_done and os.environ.get("RMM_SCREEN_HELPER") != "1":
                    helper_retry_done = True
                    _print(f"[agent] Direkte Aufnahme scheitert ({msg}) - "
                           f"versuche Helfer in der aktiven Benutzersitzung.")
                    helper = _ensure_helper(loop)
                    if helper is not None:
                        try:
                            _screen_helper_loop(loop, helper)
                        finally:
                            helper.send({"cmd": "config", "stream": False})
                        break
                    _print("[agent] helper unreachable - giving up.")
                tech = (f"{msg} | session {_win_session_id()} | "
                        f"active console session {_win_console_session_id()} | "
                        f"helper path {'yes' if _needs_session_helper() else 'no'}")
                _notify_screen_error(
                    loop,
                    "The screen cannot be captured. Common cause: the PC is a "
                    "headless VM/server WITHOUT an active graphical session (no "
                    "monitor, nobody signed in, or reachable only via RDP). Without "
                    "a real screen session there is nothing to transmit. Fix: stay "
                    "signed in on the device, provide a (virtual) monitor, or "
                    f"install a virtual display driver. [Technical: {tech}]",
                    code="agent_err_no_display", params={"tech": tech}
                )
                _screen_stream["active"] = False
                break
            # Andere Fehler beim Start: ein paar Mal tolerieren, dann aufgeben
            if consecutive_errors >= 5:
                _notify_screen_error(loop, f"Screen capture aborted: {msg}",
                                     code="agent_err_aborted", params={"err": msg})
                _screen_stream["active"] = False
                break
            time.sleep(1)

    try:
        if sct is not None:
            sct.close()
    except Exception:
        pass


def _notify_screen_error(loop, message, code=None, params=None):
    """
    Schickt eine Fehlermeldung ans Dashboard (einmalig).

    WARUM code/params zusaetzlich zum Text?
    Der Agent kennt die Sprache des Dashboard-Benutzers nicht - mehrere Personen
    mit verschiedenen Spracheinstellungen koennen dasselbe Geraet ansehen. Statt
    eines fertigen Satzes schickt der Agent daher einen Uebersetzungs-SCHLUESSEL
    (`code`) und die Einsetzwerte (`params`); uebersetzt wird erst im Browser.

    `message` bleibt als Rueckfallebene erhalten und ist bewusst ENGLISCH:
      - Ein aelteres Dashboard, das `code` nicht kennt, zeigt weiterhin Text.
      - Die Agent-Konsole und das Protokoll bleiben lesbar.
    Das Backend reicht die Nutzlast unveraendert weiter, es braucht also KEINE
    Anpassung fuer die neuen Felder.
    """
    _print(f"[agent] Screen error: {message}")
    payload = {"id": DEVICE_ID, "error": message}
    if code:
        payload["code"] = code
        payload["params"] = params or {}
    try:
        asyncio.run_coroutine_threadsafe(
            sio.emit("screen-error", payload, namespace="/agent"),
            loop,
        )
    except Exception:
        pass


def _notify_screen_mode(loop, mode, reason="", code=None, params=None):
    """
    Teilt dem Dashboard mit, WIE Remote-Zugriff möglich ist. Wichtigster Fall:
    mode='shell' -> das Dashboard öffnet direkt eine Shell (headless/Shell-only).

    `reason` ist die englische Rueckfallebene, `code`/`params` werden im
    Dashboard uebersetzt (siehe _notify_screen_error).
    """
    _print(f"[agent] Screen mode: {mode} ({reason})")
    payload = {"id": DEVICE_ID, "mode": mode, "reason": reason}
    if code:
        payload["code"] = code
        payload["params"] = params or {}
    try:
        asyncio.run_coroutine_threadsafe(
            sio.emit("screen-mode", payload, namespace="/agent"),
            loop,
        )
    except Exception:
        pass


# ====================================================================
# Virtueller Bildschirmtreiber (headless) - nur Windows
# --------------------------------------------------------------------
# Ein "Indirect Display Driver" (IDD) stellt dem System einen Bildschirm
# bereit, obwohl kein Monitor angeschlossen ist. Erst damit gibt es auf
# einem headless-Server/VM ueberhaupt einen Desktop, den der Remote-
# Bildschirm aufnehmen kann. Treiber + INF kommen als signiertes ZIP vom
# RMM-Server (das Modul "headless-screen" laedt sie beim Aktivieren dorthin);
# der Agent installiert sie als SYSTEM mit pnputil. Ohne URL wird nichts
# installiert - es werden keine Binaerdateien mit dem Agenten ausgeliefert.
# ====================================================================
_VDISPLAY_DIR = Path(os.environ.get("ProgramData") or r"C:\ProgramData") / "ByteraRMM" / "vdisplay" \
    if platform.system() == "Windows" else Path("/tmp/rmm-vdisplay")
_VDISPLAY_HWID = "Root\\ByteraIddSampleDriver"   # HardwareID der mitgelieferten INF


def _vdisplay_status() -> dict:
    """-> {supported, installed, present, active, driver, error}."""
    if not IS_WINDOWS:
        return {"supported": False, "installed": False, "present": False, "active": False,
                "error": "Nur unter Windows verfuegbar"}
    out = {"supported": True, "installed": False, "present": False, "active": False, "error": ""}
    try:
        r = _run(["pnputil", "/enum-devices", "/connected"])
        txt = (r.stdout or "") + (r.stdout is None and "" or "")
        if any(k in txt for k in ("ByteraIdd", "Bytera Virtual Display", "RapalleIdd", "Rapalle Virtual Display")):
            out["present"] = True
    except Exception:
        pass
    # Treiber im Treiberspeicher? (auch wenn gerade kein Geraet aktiv ist)
    try:
        r = _run(["pnputil", "/enum-drivers"])
        low = (r.stdout or "").lower()
        if any(k in low for k in ("byteraidd", "bytera virtual display", "rapalleidd", "rapalle virtual display")):
            out["installed"] = True
    except Exception:
        pass
    # Aktiver (zusaetzlicher) Bildschirm vorhanden?
    try:
        import ctypes
        n = ctypes.windll.user32.GetSystemMetrics(80)   # SM_CMONITORS
        out["monitors"] = int(n)
        out["active"] = out["present"] and int(n) >= 1
    except Exception:
        pass
    return out


def _vdisplay_install(url: str, sha256: str = "") -> dict:
    """Treiber-ZIP vom Server holen, pruefen, entpacken und als Geraet anlegen."""
    if not IS_WINDOWS:
        return {"ok": False, "error": "Der virtuelle Bildschirm wird nur unter Windows unterstuetzt."}
    if _win_session_id() != 0 and os.environ.get("RMM_SCREEN_HELPER") == "1":
        return {"ok": False, "error": "Nur der Agent-Dienst (SYSTEM) kann den Treiber installieren."}
    if not url:
        return {"ok": False, "error": "Kein Treiberpaket auf dem Server hinterlegt "
                "(Modul „Headless-Bildschirm“ zuerst aktivieren)."}
    try:
        import hashlib
        import urllib.request
        import zipfile
        _VDISPLAY_DIR.mkdir(parents=True, exist_ok=True)
        zpath = _VDISPLAY_DIR / "driver.zip"
        # Relative Server-Adresse gegen die eigene Server-URL aufloesen und den
        # Download mit dem Agent-Token absichern (die Datei liegt auf dem RMM).
        full = url if "://" in url else (BACKEND_URL.rstrip("/") + "/" + url.lstrip("/"))
        _print(f"[vdisplay] lade Treiberpaket: {full}")
        if full.startswith(BACKEND_URL.rstrip("/") + "/"):   # own server: token, encrypted
            blob = _backend_fetch("/" + full[len(BACKEND_URL.rstrip("/")):].lstrip("/"), timeout=180)
        else:
            with urllib.request.urlopen(urllib.request.Request(full), timeout=120) as resp:
                blob = resp.read()
        if sha256 and hashlib.sha256(blob).hexdigest().lower() != sha256.lower():
            return {"ok": False, "error": "Pruefsumme des Treiberpakets stimmt nicht - Abbruch."}
        zpath.write_bytes(blob)
        with zipfile.ZipFile(zpath) as zf:
            # Zip-Slip vermeiden
            for name in zf.namelist():
                if name.startswith(("/", "\\")) or ".." in Path(name).parts:
                    return {"ok": False, "error": f"Unsicherer Pfad im Paket: {name}"}
            zf.extractall(_VDISPLAY_DIR)
        inf = next(iter(_VDISPLAY_DIR.rglob("*.inf")), None)
        if not inf:
            return {"ok": False, "error": "Im Treiberpaket ist keine INF-Datei enthalten."}
        # 1) Treiber in den Treiberspeicher (signiert) aufnehmen
        r = _run(["pnputil", "/add-driver", str(inf), "/install"])
        log = (r.stdout or "") + (r.stderr or "")
        _print(f"[vdisplay] add-driver rc={r.returncode}: {log.strip()[:300]}")
        # 2) Geraet anlegen (falls noch keins aktiv). Bevorzugt mit der
        #    mitgelieferten Installationshilfe, sonst ueber die INF.
        helper_exe = next(iter(_VDISPLAY_DIR.rglob("*nstall*.exe")), None)
        if not _vdisplay_status().get("present"):
            if helper_exe:
                _run([str(helper_exe), "install"])
            else:
                _run(["pnputil", "/add-device", "/hardwareid", _VDISPLAY_HWID])
        st = _vdisplay_status()
        if not (st.get("installed") or st.get("present")):
            return {"ok": False, "error": "Installation lief durch, aber der Treiber ist nicht sichtbar. "
                    "Moeglicher Grund: nicht signierter Treiber oder fehlende Rechte. "
                    f"Details: {log.strip()[:300]}"}
        return {"ok": True, "detail": "Virtueller Bildschirm installiert.", **st}
    except Exception as e:
        return {"ok": False, "error": f"Installation fehlgeschlagen: {e}"}


def _vdisplay_remove() -> dict:
    if not IS_WINDOWS:
        return {"ok": False, "error": "Nur unter Windows."}
    try:
        helper_exe = next(iter(_VDISPLAY_DIR.rglob("*nstall*.exe")), None)
        if helper_exe:
            _run([str(helper_exe), "remove"])
        else:
            _run(["pnputil", "/remove-device", "/hardwareid", _VDISPLAY_HWID])
        # Treiber aus dem Speicher nehmen
        r = _run(["pnputil", "/enum-drivers"])
        pub = None
        cur = {}
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if line.lower().startswith("published name"):
                cur["pub"] = line.split(":", 1)[1].strip()
            elif ("bytera" in line.lower() or "rapalle" in line.lower()) and cur.get("pub"):
                pub = cur["pub"]
        if pub:
            _run(["pnputil", "/delete-driver", pub, "/uninstall", "/force"])
        return {"ok": True, "detail": "Virtueller Bildschirm entfernt."}
    except Exception as e:
        return {"ok": False, "error": f"Entfernen fehlgeschlagen: {e}"}


# ====================================================================
# Registry (nur Windows) - lesen/schreiben/auflisten/loeschen
# ====================================================================
_REG_HIVES = {}
if IS_WINDOWS:
    import winreg as _wr
    _REG_HIVES = {
        "HKLM": _wr.HKEY_LOCAL_MACHINE, "HKEY_LOCAL_MACHINE": _wr.HKEY_LOCAL_MACHINE,
        "HKCU": _wr.HKEY_CURRENT_USER, "HKEY_CURRENT_USER": _wr.HKEY_CURRENT_USER,
        "HKCR": _wr.HKEY_CLASSES_ROOT, "HKEY_CLASSES_ROOT": _wr.HKEY_CLASSES_ROOT,
        "HKU": _wr.HKEY_USERS, "HKEY_USERS": _wr.HKEY_USERS,
        "HKCC": _wr.HKEY_CURRENT_CONFIG, "HKEY_CURRENT_CONFIG": _wr.HKEY_CURRENT_CONFIG,
    }
_REG_TYPES = {}
if IS_WINDOWS:
    import winreg as _wr
    _REG_TYPES = {"REG_SZ": _wr.REG_SZ, "REG_EXPAND_SZ": _wr.REG_EXPAND_SZ,
                  "REG_DWORD": _wr.REG_DWORD, "REG_QWORD": getattr(_wr, "REG_QWORD", 11),
                  "REG_MULTI_SZ": _wr.REG_MULTI_SZ, "REG_BINARY": _wr.REG_BINARY}
    _REG_TYPE_NAME = {v: k for k, v in _REG_TYPES.items()}


def _reg_split(path: str):
    """'HKLM\\Software\\Foo' -> (hive_const, 'Software\\Foo'). Akzeptiert / und \\."""
    import winreg as _wr
    path = (path or "").replace("/", "\\").strip("\\ ")
    if "\\" in path:
        root, sub = path.split("\\", 1)
    else:
        root, sub = path, ""
    hive = _REG_HIVES.get(root.upper())
    if hive is None:
        raise ValueError(f"Unbekannter Registry-Hive: {root}")
    return hive, sub


def _reg_list(path: str) -> dict:
    """Werte + Unterschluessel eines Schluessels auflisten."""
    import winreg as _wr
    hive, sub = _reg_split(path)
    key = _wr.OpenKey(hive, sub, 0, _wr.KEY_READ)
    try:
        values, subkeys = [], []
        i = 0
        while True:
            try:
                name, data, typ = _wr.EnumValue(key, i)
            except OSError:
                break
            values.append({"name": name or "(Standard)", "type": _REG_TYPE_NAME.get(typ, str(typ)),
                           "data": _reg_jsonable(data)})
            i += 1
        i = 0
        while True:
            try:
                subkeys.append(_wr.EnumKey(key, i))
            except OSError:
                break
            i += 1
        return {"path": path, "values": values, "subkeys": sorted(subkeys)}
    finally:
        _wr.CloseKey(key)


def _reg_jsonable(data):
    if isinstance(data, bytes):
        return data.hex()
    if isinstance(data, (list, tuple)):
        return list(data)
    return data


def _reg_get(path: str, name: str) -> dict:
    import winreg as _wr
    hive, sub = _reg_split(path)
    key = _wr.OpenKey(hive, sub, 0, _wr.KEY_READ)
    try:
        data, typ = _wr.QueryValueEx(key, name)
        return {"name": name, "type": _REG_TYPE_NAME.get(typ, str(typ)), "data": _reg_jsonable(data)}
    finally:
        _wr.CloseKey(key)


def _reg_set(path: str, name: str, type_name: str, data) -> dict:
    import winreg as _wr
    hive, sub = _reg_split(path)
    typ = _REG_TYPES.get((type_name or "REG_SZ").upper(), _wr.REG_SZ)
    if typ in (_wr.REG_DWORD, getattr(_wr, "REG_QWORD", 11)):
        data = int(data)
    elif typ == _wr.REG_MULTI_SZ:
        data = list(data) if isinstance(data, (list, tuple)) else str(data).split("\n")
    elif typ == _wr.REG_BINARY:
        data = bytes.fromhex(data) if isinstance(data, str) else bytes(data)
    else:
        data = str(data)
    key = _wr.CreateKeyEx(hive, sub, 0, _wr.KEY_SET_VALUE | _wr.KEY_CREATE_SUB_KEY)
    try:
        _wr.SetValueEx(key, name, 0, typ, data)
    finally:
        _wr.CloseKey(key)
    return {"ok": True}


def _reg_delete(path: str, name: str | None) -> dict:
    import winreg as _wr
    hive, sub = _reg_split(path)
    if name:
        key = _wr.OpenKey(hive, sub, 0, _wr.KEY_SET_VALUE)
        try:
            _wr.DeleteValue(key, name)
        finally:
            _wr.CloseKey(key)
    else:
        _wr.DeleteKey(hive, sub)
    return {"ok": True}


def registry_op(op: str, data: dict) -> dict:
    if not IS_WINDOWS:
        return {"ok": False, "error": "Registry gibt es nur unter Windows."}
    try:
        path = data.get("path") or ""
        if op == "list":
            return {"ok": True, **_reg_list(path)}
        if op == "get":
            return {"ok": True, **_reg_get(path, data.get("name") or "")}
        if op == "set":
            return _reg_set(path, data.get("name") or "", data.get("type") or "REG_SZ", data.get("data"))
        if op == "delete":
            return _reg_delete(path, data.get("name"))
        return {"ok": False, "error": f"Unbekannte Registry-Aktion: {op}"}
    except FileNotFoundError:
        return {"ok": False, "error": "Schlüssel/Wert nicht gefunden"}
    except PermissionError:
        return {"ok": False, "error": "Keine Berechtigung (als Administrator nötig)"}
    except Exception as e:      # noqa: BLE001
        return {"ok": False, "error": str(e)}


# ====================================================================
# Windows-Update: automatische Updates aus-/einschalten (Richtlinie)
# ====================================================================
# Setzt HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU
#   NoAutoUpdate = 1 (aus) / 0 (an).  Installation laeuft weiter ueber die
#   Patch-Funktion (patch-scan/patch-apply), auch wenn die Automatik aus ist.
_WU_AU_KEY = r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU"


def wu_auto_status() -> dict:
    if not IS_WINDOWS:
        return {"supported": False}
    try:
        import winreg as _wr
        try:
            key = _wr.OpenKey(_wr.HKEY_LOCAL_MACHINE, _WU_AU_KEY, 0, _wr.KEY_READ)
            try:
                val = _wr.QueryValueEx(key, "NoAutoUpdate")[0]
            finally:
                _wr.CloseKey(key)
            disabled = bool(int(val))
        except FileNotFoundError:
            disabled = False
        return {"supported": True, "auto_disabled": disabled}
    except Exception as e:      # noqa: BLE001
        return {"supported": True, "error": str(e)}


def wu_set_auto(disabled: bool) -> dict:
    if not IS_WINDOWS:
        return {"ok": False, "error": "Nur unter Windows."}
    try:
        import winreg as _wr
        key = _wr.CreateKeyEx(_wr.HKEY_LOCAL_MACHINE, _WU_AU_KEY, 0, _wr.KEY_SET_VALUE)
        try:
            _wr.SetValueEx(key, "NoAutoUpdate", 0, _wr.REG_DWORD, 1 if disabled else 0)
            # 2 = "benachrichtigen, nicht automatisch laden/installieren"
            _wr.SetValueEx(key, "AUOptions", 0, _wr.REG_DWORD, 2 if disabled else 4)
        finally:
            _wr.CloseKey(key)
        # Dienst benachrichtigen (best effort)
        try:
            subprocess.run(["gpupdate", "/target:computer", "/force"], timeout=60,
                           capture_output=True)
        except Exception:
            pass
        return {"ok": True, **wu_auto_status()}
    except PermissionError:
        return {"ok": False, "error": "Keine Berechtigung (als Administrator/SYSTEM nötig)"}
    except Exception as e:      # noqa: BLE001
        return {"ok": False, "error": str(e)}


def _someone_logged_in() -> bool:
    """
    Prüft, ob am Gerät gerade jemand angemeldet ist. Nur dann wird um
    Zustimmung gebeten - an unbeaufsichtigten Geräten (niemand angemeldet)
    verbindet der Remote-Bildschirm direkt. Bei unklarer Lage wird zur
    Sicherheit "angemeldet" angenommen (dann wird gefragt).
    """
    try:
        if IS_WINDOWS:
            r = _run(["quser"])           # Exit-Code 1 + leere Liste, wenn niemand angemeldet
            out = (r.stdout or "").strip()
            return r.returncode == 0 and len(out.splitlines()) > 1
        # Linux: 'who' erfasst klassische Logins; 'loginctl' zusätzlich
        # xrdp-/Wayland-/Seat-Sitzungen, die 'who' teils nicht listet.
        r = _run(["who"])
        if (r.stdout or "").strip():
            return True
        r = _run(["loginctl", "list-sessions", "--no-legend"])
        return r.returncode == 0 and bool((r.stdout or "").strip())
    except Exception:
        return True


def _tk_consent_dialog(who: str, timeout_s: int) -> bool:
    """
    Schön gestaltetes Tkinter-Zustimmungsfenster (dunkles Design, Countdown,
    Zulassen/Ablehnen). Läuft blockierend im Executor-Thread. Wirft bei
    Problemen (kein Tkinter/kein Display) - der Aufrufer fällt dann auf die
    nativen Dialoge (MessageBox/zenity/...) zurück.
    """
    import tkinter as tk

    BG = "#0f1520"; PANEL = "#161e2c"; TEXT = "#e6ecf5"; SUB = "#8ea0b8"
    ACCENT = "#4f8cff"; DANGER = "#3a4356"

    result = {"allow": False}
    # Windows-Taskleiste: Ohne eigene AppUserModelID gruppiert Windows das
    # Fenster unter pythonw.exe und zeigt in der TASKLEISTE das Python-Icon,
    # egal was iconphoto setzt. Mit eigener ID zieht die Taskleiste das
    # Fenster-Icon (unser Logo).
    if IS_WINDOWS:
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ByteraRMM.Agent.Consent")
        except Exception:
            pass
    root = tk.Tk()
    root.withdraw()
    root.title("BYTERA RMM")
    # Logo: logo.png (PNG!) liegt seit Install/Update neben agent.py.
    # iconphoto kann PNG auf Windows UND Linux - iconbitmap dagegen nur echte
    # .ico-Dateien (das alte favicon.ico war ein PNG -> blankes Icon).
    # Mehrere Größen mitgeben, damit Titelleiste (klein) UND Taskleiste
    # (mittel) jeweils eine scharfe Variante bekommen.
    _logo_img = None
    try:
        _logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.png")
        if os.path.isfile(_logo_path):
            _logo_img = tk.PhotoImage(file=_logo_path)
            _sizes = []
            for target in (64, 32, 16):
                f = max(1, _logo_img.width() // target)
                _sizes.append(_logo_img.subsample(f, f))
            root._icon_refs = [_logo_img, *_sizes]   # GC-Schutz
            root.iconphoto(True, *_sizes, _logo_img)
    except Exception:
        _logo_img = None
    root.configure(bg=BG)
    root.resizable(False, False)
    root.attributes("-topmost", True)
    try:
        root.overrideredirect(False)
    except Exception:
        pass

    frame = tk.Frame(root, bg=BG, padx=28, pady=22)
    frame.pack()
    if _logo_img is not None:
        try:
            # 1024er-Logo auf ~64 px fürs Fenster herunterrechnen.
            _factor = max(1, _logo_img.width() // 64)
            _logo_small = _logo_img.subsample(_factor, _factor)
            root._logo_refs = (_logo_img, _logo_small)   # GC-Schutz
            tk.Label(frame, image=_logo_small, bg=BG).pack()
        except Exception:
            tk.Label(frame, text="🖥️", font=("Segoe UI Emoji", 30), bg=BG, fg=TEXT).pack()
    else:
        tk.Label(frame, text="🖥️", font=("Segoe UI Emoji", 30), bg=BG, fg=TEXT).pack()
    tk.Label(frame, text=_at("consent_requested"), font=("Segoe UI", 14, "bold"),
             bg=BG, fg=TEXT, pady=6).pack()
    tk.Label(frame,
             text=_at("consent_body", who=who),
             font=("Segoe UI", 10), bg=BG, fg=SUB, justify="center").pack()
    countdown = tk.Label(frame, text="", font=("Segoe UI", 9), bg=BG, fg=SUB, pady=8)
    countdown.pack()

    btns = tk.Frame(frame, bg=BG)
    btns.pack(pady=(4, 0))

    def _finish(allow):
        result["allow"] = allow
        try:
            root.destroy()
        except Exception:
            pass

    def _mkbtn(parent, text, bg, fg, cmd):
        b = tk.Label(parent, text=text, font=("Segoe UI", 10, "bold"),
                     bg=bg, fg=fg, padx=22, pady=8, cursor="hand2")
        b.bind("<Button-1>", lambda e: cmd())
        return b

    deny_btn = _mkbtn(btns, _at("deny"), DANGER, TEXT, lambda: _finish(False))
    allow_btn = _mkbtn(btns, _at("allow_check"), ACCENT, "#ffffff", lambda: _finish(True))
    deny_btn.pack(side="left", padx=6)
    allow_btn.pack(side="left", padx=6)

    remaining = {"s": timeout_s}
    def _tick():
        remaining["s"] -= 1
        if remaining["s"] <= 0:
            _finish(False)   # keine Antwort -> ablehnen
            return
        countdown.config(text=_at("auto_deny_in", secs=remaining["s"]))
        root.after(1000, _tick)
    countdown.config(text=_at("auto_deny_in", secs=timeout_s))
    root.after(1000, _tick)

    # Zentrieren + in den Vordergrund
    root.update_idletasks()
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    x = (root.winfo_screenwidth() - w) // 2
    y = (root.winfo_screenheight() - h) // 3
    root.geometry(f"+{x}+{y}")
    root.deiconify()
    try:
        root.lift(); root.focus_force()
    except Exception:
        pass
    root.protocol("WM_DELETE_WINDOW", lambda: _finish(False))   # X = ablehnen
    root.mainloop()
    return result["allow"]


def _ask_screen_consent(requested_by: str, timeout_s: int = 30) -> bool:
    """
    Zeigt am Gerät eine Abfrage "Remote-Bildschirm zulassen?" und gibt True
    zurück, wenn der Benutzer zustimmt. Keine Antwort innerhalb des Timeouts,
    Ablehnung oder ein Fehler (kein Dialog möglich) bedeuten NEIN - verbunden
    wird nur bei ausdrücklicher Bestätigung.
    Läuft blockierend und wird deshalb im Executor ausgeführt.
    """
    who = requested_by or "Ein Administrator"

    # Sitzung 0: Ein Fenster, das WIR hier oeffnen, sieht der Benutzer nie -
    # der Desktop gehoert einer anderen Sitzung. Deshalb laesst der Helfer den
    # Dialog dort anzeigen. Genau das war der Grund, warum im Dashboard
    # "Warte auf Bestaetigung" stand, am Bildschirm aber nichts erschien.
    if _needs_session_helper():
        # Zwei Anlaeufe: Beim ersten kann die Leitung zum Helfer noch von der
        # vorherigen Sitzung her tot sein (Helfer beendet, Abmeldung,
        # Benutzerwechsel). Frueher wurde das als "abgelehnt" gewertet -
        # deshalb stand nach Stop/Neustart einer Sitzung IMMER "abgelehnt" da.
        for attempt in (1, 2):
            try:
                helper = _ensure_helper(_AGENT_LOOP)
                if helper is None:
                    _print("[agent] no helper in the user session - "
                           "cannot ask for consent (nobody logged in?)")
                    return False
                answer = helper.ask_consent(who, timeout_s)
                if answer is not None:
                    return bool(answer)
                _print(f"[agent] helper not responding (attempt {attempt}) - "
                       f"reconnecting.")
            except Exception as e:
                _print(f"[agent] consent via the helper failed "
                       f"(Versuch {attempt}): {e}")
            # Tote Verbindung wegwerfen, damit _ensure_helper einen neuen
            # Helfer in der aktuellen Benutzersitzung startet.
            old = _screen_stream.get("helper")
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass
            _screen_stream["helper"] = None
        _print("[agent] consent could not be obtained on the second attempt "
               "- session is denied.")
        return False

    title = _at("consent_window")
    text = (_at("consent_body_1line", who=who) + "\n\n"
            + _at("consent_ask", secs=timeout_s))
    # 1) Bevorzugt: das schön gestaltete Tkinter-Fenster (Windows + Linux).
    try:
        return _tk_consent_dialog(who, timeout_s)
    except Exception as e:
        _print(f"[agent] tkinter dialog not possible ({e}) - using native dialog")
    # 2) Fallbacks: native Dialoge des jeweiligen Systems.
    try:
        if IS_WINDOWS:
            import ctypes
            MB_YESNO = 0x4; MB_ICONQUESTION = 0x20
            MB_SYSTEMMODAL = 0x1000; MB_SETFOREGROUND = 0x10000; MB_TOPMOST = 0x40000
            flags = MB_YESNO | MB_ICONQUESTION | MB_SYSTEMMODAL | MB_SETFOREGROUND | MB_TOPMOST
            user32 = ctypes.windll.user32
            try:
                # MessageBoxTimeoutW: wie MessageBox, aber mit Timeout (ms).
                # Rückgabe 6 = Ja, 7 = Nein, 32000 = Timeout.
                res = user32.MessageBoxTimeoutW(0, text, title, flags, 0, timeout_s * 1000)
            except AttributeError:
                res = user32.MessageBoxW(0, text, title, flags)
            return res == 6
        # Linux (Desktop): zenity -> kdialog -> xmessage, was verfügbar ist.
        import shutil as _shutil
        if _shutil.which("zenity"):
            r = _run(["zenity", "--question", f"--title={title}", f"--text={text}",
                      f"--timeout={timeout_s}", f"--ok-label={_at('allow')}", f"--cancel-label={_at('deny')}"],
                     capture_output=True)
            return r.returncode == 0   # 0=Ja, 1=Nein, 5=Timeout
        if _shutil.which("kdialog"):
            try:
                r = _run(["kdialog", "--title", title, "--yesno", text],
                         capture_output=True, timeout=timeout_s + 5)
                return r.returncode == 0
            except Exception:
                return False           # Timeout/Fehler -> abgelehnt
        if _shutil.which("xmessage"):
            # Exit-Code = Button-Wert; Timeout liefert 0 -> deshalb 101/102.
            r = _run(["xmessage", "-center", "-timeout", str(timeout_s),
                      "-buttons", f"{_at('allow')}:101,{_at('deny')}:102", text],
                     capture_output=True)
            return r.returncode == 101
        _print("[agent] no dialog tool (zenity/kdialog/xmessage) found - remote screen denied")
        return False
    except Exception as e:
        _print(f"[agent] consent dialog failed ({e}) - remote screen denied")
        return False


# --- Chat-/Steuerfenster der laufenden Fernwartung (Agent-Seite) --------------
# Oeffnet im Helfer (Benutzersitzung) das Fenster, in dem die Person vor dem PC
# chatten, Admin-Rechte erlauben und die Verbindung trennen kann. Bei Silent-
# Modus (require_consent == False) wird es NICHT geoeffnet.
_session = {"active": False, "by": "", "granted": False, "window": False,
            # Berechtigungen, die der Benutzer im Sitzungsfenster schaltet (wie AnyDesk)
            "perms": {"input": True, "clipboard": True, "files": True}}


def _session_perm(name: str) -> bool:
    """Darf der Administrator gerade <name> (input|clipboard)? Ohne aktive Sitzung: ja."""
    if not _session.get("active"):
        return True
    return bool((_session.get("perms") or {}).get(name, True))


def _session_perm_payload() -> dict:
    p = dict(_session.get("perms") or {})
    p["admin"] = bool(_session.get("granted"))
    return {"perms": p}


def _open_session_window(requested_by: str) -> None:
    helper = _screen_stream.get("helper")
    if helper is None or not helper.alive:
        # Direkter Aufnahmeweg (Agent sitzt selbst in der Benutzersitzung): es gibt
        # noch keinen Helfer. Das Chat-/Rechte-Fenster lebt aber IM Helfer - also
        # hier einen fuer die Oberflaeche starten (Windows: in die Benutzersitzung,
        # Linux/macOS: als Prozess mit geerbtem DISPLAY). Fruehere Ursache fuer
        # "Chat geht nicht" / "kein Rechte-Fenster": ohne Helfer kam nie ein Fenster.
        try:
            helper = _ensure_helper(_AGENT_LOOP)
        except Exception as e:
            _print(f"[agent] Sitzungsfenster: Helfer-Start fehlgeschlagen: {e}")
            helper = None
    if helper is None or not helper.alive:
        _print("[agent] Hinweis: Chat-/Rechte-Fenster konnte nicht geöffnet werden "
               "(kein Helfer/Desktop).")
        return
    perms = dict(_session.get("perms") or {})
    perms["admin"] = bool(_session.get("granted"))
    _session["window"] = helper.send({"cmd": "session-open", "who": requested_by or _at("consent_requested"),
                                      "perms": perms})


def _close_session_window() -> None:
    helper = _screen_stream.get("helper")
    if helper is not None and helper.alive and _session.get("window"):
        helper.send({"cmd": "session-close"})
    _session["window"] = False


def _handle_session_reply(loop, obj: dict) -> None:
    """Ereignis aus dem Fenster (Benutzersitzung) -> ans Dashboard melden."""
    kind = obj.get("session")
    if kind == "chat":
        text = (obj.get("text") or "")[:2000]
        _session_emit(loop, "screen-session-chat", {"from": "user", "text": text})
    elif kind == "grant":
        _session["granted"] = bool(obj.get("granted"))
        _session_emit(loop, "screen-session-grant", {"granted": _session["granted"]})
        _session_emit(loop, "screen-session-perms", _session_perm_payload())
        _print(f"[agent] Admin-Rechte vom Benutzer {'erteilt' if _session['granted'] else 'entzogen'}")
    elif kind == "grant-denied":
        _session_emit(loop, "screen-session-grant", {"granted": False, "denied": True})
        _print("[agent] Benutzer hat die Anfrage nach Admin-Rechten abgelehnt.")
    elif kind == "perm":
        name = obj.get("perm")
        if name in ("input", "clipboard", "files"):
            _session.setdefault("perms", {})[name] = bool(obj.get("value"))
            _session_emit(loop, "screen-session-perms", _session_perm_payload())
            _print(f"[agent] Berechtigung '{name}' vom Benutzer {'erlaubt' if obj.get('value') else 'gesperrt'}")
    elif kind == "clip":
        # Zwischenablage der Benutzersitzung hat sich geaendert (Helfer-Watcher).
        # Echo-Filter: Vom Dashboard gesetzte Inhalte nicht zurueckschicken.
        text = str(obj.get("text") or "")
        if (_session_perm("clipboard") and text
                and text != _clip_watch.get("suppress") and len(text) <= _CLIP_MAX):
            _session_emit(loop, "screen-clipboard", {"text": text, "push": True})
    elif kind == "audio":
        _session_emit(loop, "screen-audio", {k: obj.get(k) for k in ("state", "error", "code")})
    elif kind == "file-ack":
        _session_emit(loop, "screen-file-ack", {k: obj.get(k) for k in ("tid", "seq", "done", "error")})
    elif kind == "file-out":
        if not _session_perm("files"):
            return
        _session_emit(loop, "screen-file-out", {k: obj.get(k) for k in
                                                ("tid", "name", "size", "seq", "data", "last")})
    elif kind == "disconnect":
        _print("[agent] Benutzer hat die Fernwartung getrennt.")
        _session_emit(loop, "screen-session-disconnect", {})
        _screen_stream["active"] = False
        _screen_stream["epoch"] += 1
        _session["active"] = False


def _session_emit(loop, event: str, payload: dict) -> None:
    try:
        asyncio.run_coroutine_threadsafe(
            sio.emit(event, {"id": DEVICE_ID, **payload}, namespace="/agent"), loop)
    except Exception as e:      # noqa: BLE001
        _print(f"[agent] Session-Event {event}: {e}")


def session_grant_active() -> bool:
    """Darf der Agent waehrend dieser Sitzung mit erhoehten Rechten arbeiten?
    (Vom Dashboard genutzte Befehle duerfen das pruefen.)"""
    return bool(_session.get("active") and _session.get("granted"))


# --- Datei-Uebertragung in der Sitzung (Admin -> Benutzer) --------------------
# Ohne Helfer (Linux, Agent laeuft direkt in der Benutzersitzung) schreibt der
# Agent selbst in den Downloads-Ordner seines Benutzers.
_direct_files: dict = {}


def _direct_receive(d: dict) -> dict:
    tid = str(d.get("tid") or "")
    st = _direct_files.get(tid)
    if st is None:
        if int(d.get("seq") or 0) != 0:
            raise RuntimeError("unknown transfer")
        home = os.path.expanduser("~")
        folder = os.path.join(home, "Downloads") if os.path.isdir(os.path.join(home, "Downloads")) else home
        name = os.path.basename(str(d.get("name") or "file").replace("\\", "/")) or "file"
        name = "".join("_" if ch in '<>:"/\\|?*' or ord(ch) < 32 else ch for ch in name)[:180]
        base, ext = os.path.splitext(name)
        path, n = os.path.join(folder, name), 1
        while os.path.exists(path):
            path, n = os.path.join(folder, f"{base} ({n}){ext}"), n + 1
        st = _direct_files[tid] = {"f": open(path, "wb"), "path": path}
    st["f"].write(base64.b64decode(d.get("data") or ""))
    if d.get("last"):
        st["f"].close()
        _direct_files.pop(tid, None)
        return {"done": True}
    return {}


@sio.on("screen-file-chunk", namespace="/agent")
async def on_screen_file_chunk(data):
    d = data or {}
    ack = {"id": DEVICE_ID, "tid": d.get("tid"), "seq": d.get("seq")}
    if not _session_perm("files"):
        ack.update(error="blocked", code="agent_files_blocked")
        await sio.emit("screen-file-ack", ack, namespace="/agent")
        return
    helper = _screen_stream.get("helper")
    if helper is not None and helper.alive:
        # Bestaetigung kommt vom Helfer (file-ack -> _handle_session_reply)
        helper.send({"cmd": "file-chunk", **{k: d.get(k) for k in ("tid", "seq", "name", "size", "data", "last")}})
        return
    try:
        ack.update(await asyncio.get_event_loop().run_in_executor(None, _direct_receive, d))
    except Exception as e:      # noqa: BLE001
        ack["error"] = str(e)
    await sio.emit("screen-file-ack", ack, namespace="/agent")


@sio.on("screen-audio", namespace="/agent")
async def on_screen_audio_toggle(data):
    """Ton des PCs mitschicken ein/aus (Aufnahme im Helfer, d.h. in der Benutzersitzung)."""
    on = bool((data or {}).get("enabled"))
    _screen_stream["audio"] = on
    helper = _screen_stream.get("helper")
    if helper is not None and helper.alive:
        helper.send({"cmd": "audio", "enabled": on})
    elif on:
        await sio.emit("screen-audio", {"id": DEVICE_ID, "error": "no helper", "code": "agent_audio_unavailable"},
                       namespace="/agent")


@sio.on("screen-session-request-grant", namespace="/agent")
async def on_screen_session_request_grant(data):
    """Administrator bittet um Administrator-Rechte -> Hinweis im Sitzungsfenster."""
    if not _session.get("active"):
        return
    if not _session.get("window"):
        _open_session_window(_session.get("by") or "")   # Fenster (und Helfer) notfalls oeffnen
    helper = _screen_stream.get("helper")
    if _session.get("window") and helper is not None and helper.alive:
        helper.send({"cmd": "session-request-grant"})


@sio.on("screen-session-chat", namespace="/agent")
async def on_screen_session_chat(data):
    """Chat-Nachricht vom Administrator -> im Fenster der Benutzersitzung zeigen."""
    text = (data or {}).get("text") or ""
    helper = _screen_stream.get("helper")
    if (helper is None or not helper.alive) and _session.get("active"):
        _open_session_window(_session.get("by") or "")   # Fenster (und Helfer) notfalls oeffnen
        helper = _screen_stream.get("helper")
    if helper is not None and helper.alive:
        if not _session.get("window"):
            _open_session_window(_session.get("by") or "")
        helper.send({"cmd": "session-chat", "text": str(text)[:2000]})


@sio.on("screen-start", namespace="/agent")
async def on_screen_start(data):
    """Startet das Bildschirm-Streaming - oder signalisiert Shell-Modus bei headless."""
    loop = asyncio.get_event_loop()

    # Aufnahme-Qualität/FPS vom Backend übernehmen (aus den Einstellungen).
    if isinstance(data, dict):
        if data.get("quality"):
            _screen_stream["quality"] = int(data["quality"])
        if data.get("fps"):
            _screen_stream["fps"] = int(data["fps"])

    # Ohne Bildaufnahme-Pakete gibt es nichts zu streamen. Vorher aber EINMAL
    # selbst reparieren - meist sind die Pakete nicht weg, sondern kaputt.
    if not _SCREEN_AVAILABLE:
        await loop.run_in_executor(None, _repair_screen)
    if not _SCREEN_AVAILABLE:
        _notify_screen_mode(loop, "shell",
                            "Screen streaming not possible: mss/Pillow cannot be "
                            f"loaded ({_SCREEN_ERROR}). Python: {sys.executable}. "
                            "A shell will be opened instead.",
                            code="agent_mode_no_packages",
                            params={"err": str(_SCREEN_ERROR), "python": sys.executable})
        return

    # Ist überhaupt ein grafischer Bildschirm da? (setzt auf Linux ggf. DISPLAY)
    available, reason, dcode, dparams = _detect_graphical_display()
    if not available:
        _notify_screen_mode(loop, "shell", reason, code=dcode, params=dparams)
        return

    # Läuft bereits ein Stream? Früher wurde hier einfach zurückgekehrt -
    # dadurch bekam eine NEU gestartete Sitzung KEINE erneute Zustimmungs-
    # abfrage und hängte sich still an den laufenden Stream. Jetzt beenden wir
    # den alten Stream und lassen den normalen Ablauf (inkl. Consent) neu
    # durchlaufen, sodass jede neue Sitzung sauber bestätigt werden muss.
    if _screen_stream["active"]:
        _print("[agent] screen-start bei bereits aktivem Stream -> alten Stream beenden, Consent neu einholen")
        _screen_stream["active"] = False
        old = _screen_stream.get("thread")
        if old and old.is_alive():
            try:
                old.join(timeout=2.0)
            except Exception:
                pass

    # Zustimmung am Gerät einholen (nur physische Geräte; das Backend schickt
    # require_consent entsprechend - und als doppelte Absicherung fragen VMs
    # und LXCs auch bei gesetztem Flag nie).
    # Mehrbenutzer: Hat das Dashboard einen Benutzer (Windows-Sitzung) gewaehlt,
    # trifft die Auswahl sowohl die Zustimmungsabfrage als auch die Aufnahme.
    _set_preferred_session((data or {}).get("session") if isinstance(data, dict) else None)
    require_consent = bool(isinstance(data, dict) and data.get("require_consent"))
    _print(f"[agent] screen-start empfangen (require_consent={require_consent}, "
           f"device_type={DETECTED_DEVICE_TYPE or 'physical'})")
    # Nur LXC-Container fragen nie (dort meldet sich niemand grafisch an).
    # VMs fragen sehr wohl - z.B. wenn jemand per RDP in der VM arbeitet.
    if DETECTED_DEVICE_TYPE == "lxc":
        require_consent = False
    # Niemand angemeldet? Dann gibt es niemanden, der zustimmen könnte ->
    # direkt verbinden (typisch: unbeaufsichtigter physischer Rechner).
    if require_consent and not await loop.run_in_executor(None, _someone_logged_in):
        _print(f"[agent] {_at('log_nobody_home')}")
        require_consent = False
    # Marke fuer DIESE Anfrage. Kommt waehrend der Abfrage ein "screen-stop"
    # (Dashboard-Fenster zu), erhoeht on_screen_stop den Zaehler und wir
    # brechen danach ab, statt ins Leere zu streamen.
    _screen_stream["epoch"] += 1
    my_epoch = _screen_stream["epoch"]

    if require_consent:
        requested_by = (data or {}).get("requested_by") or ""
        _notify_screen_mode(loop, "consent", "Waiting for confirmation on the device…",
                            code="agent_mode_consent")
        allowed = await loop.run_in_executor(None, _ask_screen_consent, requested_by)
        if _screen_stream["epoch"] != my_epoch:
            _print("[agent] consent arrived too late - the session has meanwhile "
                   "ended or been requested again. No stream is started.")
            return
        if _screen_stream["active"]:
            return  # in der Zwischenzeit anderweitig gestartet
        if not allowed:
            _print(f"[agent] {_at('log_screen_denied')}")
            try:
                await sio.emit("screen-error", {
                    "id": DEVICE_ID, "consent_denied": True,
                    "error": "The user at the device did not confirm remote screen access.",
        "code": "agent_err_consent_denied", "params": {},
                }, namespace="/agent")
            except Exception:
                pass
            return
        _print(f"[agent] {_at('log_screen_allowed')}")

    _screen_stream["active"] = True
    _screen_stream["thread"] = threading.Thread(target=_screen_capture_loop, args=(loop,), daemon=True)
    _screen_stream["thread"].start()
    # Zwischenablage-Sync (Geraet -> Dashboard) fuer die Dauer der Sitzung.
    _start_clipboard_watch(loop)
    _print(f"[agent] {_at('log_screen_started')}")

    # Chat-/Steuerfenster in der Benutzersitzung - NUR wenn nicht Silent-Modus
    # (bei Silent kommt require_consent=False vom Backend). Kurz warten, bis der
    # Helfer verbunden ist, dann oeffnen.
    # Silent-Modus (require_consent == False): kein Fenster beim Benutzer UND der
    # Admin hat sofort erhoehte Rechte, ohne dass jemand am Geraet zustimmt.
    # Mit Benutzerfreigabe (require_consent == True): Rechte erst nach Klick im Fenster.
    _session.update(active=True, by=(data or {}).get("requested_by") or "",
                    granted=not require_consent, perms={"input": True, "clipboard": True, "files": True})
    _session_emit(loop, "screen-session-perms", _session_perm_payload())
    if require_consent:
        def _later():
            for _ in range(20):
                if _screen_stream.get("helper") and _screen_stream["helper"].alive:
                    break
                time.sleep(0.25)
            _open_session_window(_session["by"])
        threading.Thread(target=_later, daemon=True).start()
    else:
        # Dashboard informieren, dass im Silent-Modus bereits Admin-Rechte gelten.
        _session_emit(loop, "screen-session-grant", {"granted": True, "silent": True})
        _print("[agent] Silent-Modus: Admin-Rechte ohne Benutzerfreigabe aktiv.")


# ============================================================================
# Digitaler USB-Stick (temp-usb): Ordner-Sync zwischen /storage/temp-usb/<id>
# und einem lokalen Ordner (Standard C:\bytera bzw. /mnt/bytera). Der Benutzer
# kann Dateien dort ablegen/ausfuehren; was er ablegt, kommt in den Bereich
# zurueck und ist im Relay sichtbar. "usb"-Modus (virtueller Stick per Treiber)
# faellt auf Ordner-Sync zurueck, wenn kein Treiber hinterlegt/installiert ist.
# ============================================================================
_tempusb = {}          # id -> {"path": Ordner, "mode", "known": {relpfad: mtime}, "stop": Event}
_tempusb_lock = threading.Lock()
_TEMPUSB_PUT = {}      # (id,name) -> bytes (laufender Empfang vom Server)


def _tempusb_default_dir() -> str:
    return r"C:\bytera" if IS_WINDOWS else "/mnt/bytera"


def _tempusb_emit(event: str, data: dict) -> None:
    try:
        asyncio.run_coroutine_threadsafe(
            sio.emit(event, {"id": DEVICE_ID, **data}, namespace="/agent"), _AGENT_LOOP)
    except Exception as e:
        _print(f"[temp-usb] emit {event}: {e}")


def _tempusb_upload_file(area_id: str, root: str, rel: str) -> None:
    import base64 as _b64
    full = os.path.join(root, rel.replace("/", os.sep))
    try:
        data = open(full, "rb").read()
    except OSError:
        return
    chunk = 256 * 1024
    n = max(1, (len(data) + chunk - 1) // chunk)
    for seq in range(n):
        _tempusb_emit("temp-usb-upload", {"id": area_id, "name": rel.replace(os.sep, "/"),
                                          "seq": seq, "last": seq == n - 1,
                                          "data": _b64.b64encode(data[seq * chunk:(seq + 1) * chunk]).decode()})


def _tempusb_watch(area_id: str) -> None:
    # Pollt den Ordner; neue/geaenderte Dateien (die NICHT gerade vom Server
    # kamen) werden hochgeladen. Kein Zusatzpaket noetig.
    import time as _t
    while True:
        with _tempusb_lock:
            st = _tempusb.get(area_id)
        if not st or st["stop"].is_set():
            return
        root = st["path"]
        try:
            for dp, _dn, fn in os.walk(root):
                for f in fn:
                    full = os.path.join(dp, f)
                    rel = os.path.relpath(full, root).replace(os.sep, "/")
                    try:
                        m = os.path.getmtime(full)
                    except OSError:
                        continue
                    if st["known"].get(rel) != m:
                        st["known"][rel] = m
                        _tempusb_upload_file(area_id, root, rel)
        except Exception as e:
            _print(f"[temp-usb] watch {area_id}: {e}")
        _t.sleep(3.0)


@sio.on("temp-usb-attach", namespace="/agent")
async def on_tempusb_attach(data):
    d = data or {}
    area_id = str(d.get("id") or "")
    if not area_id:
        return
    path = (d.get("path") or "").strip() or _tempusb_default_dir()
    mode = d.get("mode") or "folder"
    note = ""
    if mode == "usb" and not (d.get("driver") or "").strip():
        mode = "folder"                       # kein Treiber hinterlegt -> Ordner-Sync
        note = "kein USB-Treiber hinterlegt - nutze Ordner-Sync"
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        _tempusb_emit("temp-usb-status", {"id": area_id, "error": str(e), "code": "agent_tempusb_mkdir"})
        return
    with _tempusb_lock:
        old = _tempusb.get(area_id)
        if old:
            old["stop"].set()
        # "known" wird durch temp-usb-put (Server-Dateien) gefuellt; alles andere
        # im Ordner gilt als lokal und wird hochgeladen.
        _tempusb[area_id] = {"path": path, "mode": mode, "known": {}, "stop": threading.Event()}
    threading.Thread(target=_tempusb_watch, args=(area_id,), name=f"tempusb-{area_id}", daemon=True).start()
    _print(f"[agent] temp-usb angehängt: {area_id} -> {path} ({mode})")
    _tempusb_emit("temp-usb-status", {"id": area_id, "state": "attached", "path": path, "mode": mode, "note": note})


@sio.on("temp-usb-put", namespace="/agent")
async def on_tempusb_put(data):
    d = data or {}
    area_id, name = str(d.get("id") or ""), str(d.get("name") or "")
    with _tempusb_lock:
        st = _tempusb.get(area_id)
    if not st or not name:
        return
    key = (area_id, name)
    try:
        _TEMPUSB_PUT[key] = _TEMPUSB_PUT.get(key, b"") + base64.b64decode(d.get("data") or "")
    except Exception:
        _TEMPUSB_PUT.pop(key, None)
        return
    if not d.get("last"):
        return
    buf = _TEMPUSB_PUT.pop(key, b"")
    full = os.path.join(st["path"], name.replace("/", os.sep))
    try:
        os.makedirs(os.path.dirname(full) or st["path"], exist_ok=True)
        with open(full, "wb") as f:
            f.write(buf)
        with _tempusb_lock:
            st["known"][name] = os.path.getmtime(full)      # nicht zurueckspiegeln
    except OSError as e:
        _print(f"[temp-usb] put {name}: {e}")


@sio.on("temp-usb-detach", namespace="/agent")
async def on_tempusb_detach(data):
    area_id = str((data or {}).get("id") or "")
    with _tempusb_lock:
        st = _tempusb.pop(area_id, None)
    if st:
        st["stop"].set()
        _print(f"[agent] temp-usb gelöst: {area_id}")
    _tempusb_emit("temp-usb-status", {"id": area_id, "state": "detached"})


@sio.on("screen-stop", namespace="/agent")
async def on_screen_stop(data):
    """Stoppt das Bildschirm-Streaming."""
    _screen_stream["audio"] = False                       # Ton immer mit ausschalten
    _h = _screen_stream.get("helper")
    if _h is not None and _h.alive:
        _h.send({"cmd": "audio", "enabled": False})
    _screen_stream["active"] = False
    # Auch eine noch offene Zustimmungsabfrage entwerten (siehe "epoch"):
    # sonst startete eine verspaetete Zustimmung einen Stream, den niemand
    # mehr sieht - und blockierte die naechste Sitzung.
    _screen_stream["epoch"] += 1
    _close_session_window()
    _session.update(active=False, granted=False)
    _print(f"[agent] {_at('log_screen_stopped')}")


@sio.on("screen-config", namespace="/agent")
async def on_screen_config(data):
    """Qualitaet/FPS waehrend der Sitzung aendern. Der Capture-Loop (bzw. der
    Helfer ueber _screen_helper_loop) uebernimmt die Werte automatisch."""
    if not isinstance(data, dict):
        return
    try:
        if data.get("fps") is not None:
            _screen_stream["fps"] = max(1, min(30, int(data["fps"])))
        if data.get("quality") is not None:
            _screen_stream["quality"] = max(10, min(95, int(data["quality"])))
        if data.get("width") is not None:
            w = int(data["width"])
            _screen_stream["width"] = 0 if w <= 0 else max(320, min(3840, w))   # 0 = automatisch
    except Exception:
        pass


@sio.on("screen-refresh", namespace="/agent")
async def on_screen_refresh(data=None):
    """Dashboard (neu verbunden / Wachhund) will sofort ein aktuelles Bild -
    auch wenn sich am Bildschirm nichts geaendert hat."""
    _screen_stream["force"] = True
    _FLOW.reset()                     # evtl. verlorene Rueckmeldungen verwerfen
    h = _screen_stream.get("helper")
    if h is not None and h.alive and _screen_stream.get("active"):
        h.send({"cmd": "refresh"})


@sio.on("screen-set-monitor", namespace="/agent")
async def on_screen_set_monitor(data):
    """Wechselt den gestreamten Bildschirm (Multi-Monitor). Der Capture-Loop
    übernimmt die Auswahl beim nächsten Frame automatisch."""
    if isinstance(data, dict) and data.get("monitor") is not None:
        try:
            # 0 = "Alle Bildschirme" (mss.monitors[0] = Gesamtfläche aller Monitore)
            _screen_stream["monitor"] = max(0, int(data["monitor"]))
            _print(f"[agent] {_at('log_monitor_switched', n=_screen_stream['monitor'])}")
        except Exception:
            pass


@sio.on("screen-users", namespace="/agent")
async def on_screen_users(data):
    """Dashboard fragt: welche Benutzer sind angemeldet? (Mehrbenutzer-Auswahl,
    nur Windows). Antwort ueber 'screen-users' zurueck."""
    loop = asyncio.get_event_loop()
    users = []
    multi = False
    if IS_WINDOWS:
        users = await loop.run_in_executor(None, _win_logged_in_users)
        multi = len(users) > 1
    try:
        await sio.emit("screen-users", {
            "id": DEVICE_ID, "users": users, "multi": multi,
            "platform": platform.system(),
        }, namespace="/agent")
    except Exception:
        pass


@sio.on("vdisplay-status", namespace="/agent")
async def on_vdisplay_status(data):
    """Zustand des virtuellen Bildschirmtreibers (headless) melden."""
    loop = asyncio.get_event_loop()
    st = await loop.run_in_executor(None, _vdisplay_status)
    try:
        await sio.emit("vdisplay-status", {"id": DEVICE_ID, **st}, namespace="/agent")
    except Exception:
        pass


@sio.on("vdisplay-install", namespace="/agent")
async def on_vdisplay_install(data):
    """Virtuellen Bildschirmtreiber installieren (nur Windows, als SYSTEM)."""
    loop = asyncio.get_event_loop()
    url = (data or {}).get("url") or ""
    sha = (data or {}).get("sha256") or ""
    res = await loop.run_in_executor(None, _vdisplay_install, url, sha)
    try:
        await sio.emit("vdisplay-result", {"id": DEVICE_ID, "action": "install", **res},
                       namespace="/agent")
    except Exception:
        pass
    st = await loop.run_in_executor(None, _vdisplay_status)
    await sio.emit("vdisplay-status", {"id": DEVICE_ID, **st}, namespace="/agent")


@sio.on("registry", namespace="/agent")
async def on_registry(data):
    """Registry-Aktion (list/get/set/delete). Antwort ueber 'registry-result'."""
    loop = asyncio.get_event_loop()
    op = (data or {}).get("op") or "list"
    rid = (data or {}).get("rid") or ""
    res = await loop.run_in_executor(None, registry_op, op, data or {})
    try:
        await sio.emit("registry-result", {"id": DEVICE_ID, "rid": rid, "op": op, **res}, namespace="/agent")
    except Exception:
        pass


@sio.on("wu-auto", namespace="/agent")
async def on_wu_auto(data):
    """Automatische Windows-Updates aus/ein (oder nur Status, wenn 'set' fehlt)."""
    loop = asyncio.get_event_loop()
    if isinstance(data, dict) and "disabled" in data:
        res = await loop.run_in_executor(None, wu_set_auto, bool(data.get("disabled")))
    else:
        res = await loop.run_in_executor(None, wu_auto_status)
    try:
        await sio.emit("wu-auto", {"id": DEVICE_ID, **res}, namespace="/agent")
    except Exception:
        pass


@sio.on("vdisplay-remove", namespace="/agent")
async def on_vdisplay_remove(data):
    """Virtuellen Bildschirmtreiber wieder entfernen."""
    loop = asyncio.get_event_loop()
    res = await loop.run_in_executor(None, _vdisplay_remove)
    try:
        await sio.emit("vdisplay-result", {"id": DEVICE_ID, "action": "remove", **res},
                       namespace="/agent")
    except Exception:
        pass
    st = await loop.run_in_executor(None, _vdisplay_status)
    await sio.emit("vdisplay-status", {"id": DEVICE_ID, **st}, namespace="/agent")


# Umrechnungstabelle für Sondertasten vom Browser zu pynput
_SPECIAL_KEYS = {}
if _INPUT_AVAILABLE:
    _SPECIAL_KEYS = {
        "Enter": KeyboardKey.enter, "Backspace": KeyboardKey.backspace,
        "Tab": KeyboardKey.tab, "Escape": KeyboardKey.esc, " ": KeyboardKey.space,
        "ArrowUp": KeyboardKey.up, "ArrowDown": KeyboardKey.down,
        "ArrowLeft": KeyboardKey.left, "ArrowRight": KeyboardKey.right,
        "Delete": KeyboardKey.delete, "Home": KeyboardKey.home, "End": KeyboardKey.end,
        "PageUp": KeyboardKey.page_up, "PageDown": KeyboardKey.page_down,
        "Control": KeyboardKey.ctrl, "Alt": KeyboardKey.alt, "Shift": KeyboardKey.shift,
        "Meta": KeyboardKey.cmd,
    }


@sio.on("update-agent", namespace="/agent")
async def on_update_agent(data):
    """
    Aktualisiert den Agenten, indem der Client-Install-/Update-Befehl in einer
    eigenen Shell-Session ausgeführt wird (genau wie bei der Erstinstallation).
    Das Update-Skript stoppt den laufenden Agenten, ersetzt agent.py und startet
    den Dienst neu - deshalb läuft der Befehl LOSGELÖST vom Agent-Prozess.
    """
    _print("[agent] Update angefordert - starte Update-Befehl in eigener Shell...")
    await _emit_action_log("update", "received", "Update-Befehl empfangen")
    loop = asyncio.get_event_loop()
    detail = await loop.run_in_executor(None, _run_dist_command, "update")
    await _emit_action_log("update", detail.get("stage", "launched"), detail.get("detail", ""))
    try:
        asyncio.ensure_future(_watch_update_result())
    except Exception:
        pass


@sio.on("uninstall-agent", namespace="/agent")
async def on_uninstall_agent(data):
    """
    Deinstalliert den Agenten: führt das Uninstall-Skript in einer eigenen Shell
    aus (Dienst/Task entfernen, Programmordner löschen). Läuft losgelöst, da es
    den eigenen Prozess beendet.
    """
    try:                       # Eine Node ohne Agent waere verwaist
        _node_service_remove(); shutil.rmtree(NODE_DIR, ignore_errors=True)
    except Exception:
        pass
    _print("[agent] Deinstallation angefordert - starte Uninstall-Befehl in eigener Shell...")
    await _emit_action_log("uninstall", "received", "Uninstall-Befehl empfangen")
    loop = asyncio.get_event_loop()
    detail = await loop.run_in_executor(None, _run_dist_command, "uninstall")
    await _emit_action_log("uninstall", detail.get("stage", "launched"), detail.get("detail", ""))


async def _report_update_failure() -> bool:
    """Hat das Update-Skript einen Fehlschlag hinterlegt (.update_failed)?
    Dann ans Dashboard melden: Das Update wurde abgebrochen bzw. zurueckgerollt,
    der Agent laeuft mit der bisherigen Version weiter."""
    f = Path(__file__).resolve().parent / ".update_failed"
    try:
        if not f.exists():
            return False
        reason = f.read_text(encoding="utf-8", errors="replace").strip()[:500]
        f.unlink()
    except Exception:
        return False
    _print(f"[agent] UPDATE FEHLGESCHLAGEN - laufe mit bisheriger Version weiter: {reason}")
    await _emit_action_log("update", "error",
                           f"Update fehlgeschlagen, Agent laeuft mit Version {AGENT_VERSION} weiter: {reason}")
    return True


async def _watch_update_result(minutes: int = 20):
    """Nach dem Start eines Updates: Bricht das Skript VOR dem Tausch ab, wird
    dieser Agent nie neu gestartet - dann hier den Grund abholen und melden."""
    for _ in range(minutes * 6):
        await asyncio.sleep(10)
        try:
            if await _report_update_failure():
                return
        except Exception:
            pass


async def _emit_action_log(kind: str, stage: str, detail: str = ""):
    """Meldet dem Backend (und damit dem Dashboard), was bei Update/Uninstall passiert."""
    try:
        await sio.emit("agent-action-log", {
            "id": DEVICE_ID, "kind": kind, "stage": stage, "detail": detail,
            "agent_version": AGENT_VERSION,
        }, namespace="/agent")
    except Exception:
        pass


def _run_dist_command(kind: str):
    """
    Startet den Update- bzw. Uninstall-Befehl als EIGENSTÄNDIGEN Prozess, der den
    Tod des Agent-Prozesses überlebt (das Skript stoppt den Agenten selbst).

    Linux: Wir starten den Befehl als TRANSIENTEN systemd-Dienst
    (`systemd-run --collect`). Ein solcher Dienst läuft in einem EIGENEN cgroup,
    unabhängig vom Dienst des Agenten. Dadurch überlebt er zuverlässig das
    `systemctl stop bytera-agent` (dessen KillMode=control-group sonst alle
    Kindprozesse mitreißt - das war der Grund, warum der Uninstall zuvor
    scheinbar nichts tat).

    kind: 'update' | 'uninstall'

    Der exakte Befehl wird ins Agent-Log geschrieben, damit man ihn zum Testen
    auch von Hand auf dem Client ausführen kann.
    """
    import shutil

    try:
        if IS_WINDOWS:
            import base64
            fname = "update.ps1" if kind == "update" else "uninstall.ps1"
            url = f"{BACKEND_URL}/agent-dist/{fname}"
            inner_ps = f"iwr '{url}' -UseBasicParsing | iex"
            enc = base64.b64encode(inner_ps.encode("utf-16-le")).decode()
            taskname = "ByteraRmmUpdate" if kind == "update" else "ByteraRmmUninstall"
            event_id = "812" if kind == "update" else "811"
            tr = f"powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand {enc}"

            _print(f"[agent] {kind}: exakter Befehl (Windows, ELEVATED PowerShell):")
            _print(f'         powershell -NoProfile -ExecutionPolicy Bypass -Command "{inner_ps}"')

            # (A) Bevorzugt: vorautorisierten SYSTEM-Task per EVENT auslösen. Das
            #     braucht KEINE Admin-Rechte im Agenten - die Aufgabenplanung führt
            #     das Skript als SYSTEM aus. Voraussetzung: elevate.ps1 wurde einmal
            #     ausgeführt (bzw. der Installer hat die Tasks eingerichtet).
            try:
                q = _run(["schtasks", "/query", "/tn", taskname],
                                   capture_output=True, text=True)
                task_exists = (q.returncode == 0)
            except Exception:
                task_exists = False
            if task_exists:
                triggered = False
                trig_err = ""
                # Primär: Write-EventLog -> Provider ist garantiert 'ByteraRMM'
                # (matcht die Event-Subscription des SYSTEM-Tasks sicher).
                try:
                    w = _run(
                        ["powershell", "-NoProfile", "-Command",
                         f"Write-EventLog -LogName Application -Source 'ByteraRMM' "
                         f"-EventId {event_id} -EntryType Information -Message 'rmm {kind}'"],
                        capture_output=True, text=True,
                    )
                    triggered = (w.returncode == 0)
                    if not triggered:
                        trig_err = (w.stderr or w.stdout or "").strip()
                except Exception as e:
                    trig_err = str(e)
                # Fallback: eventcreate (falls Write-EventLog nicht verfügbar/fehlerhaft)
                if not triggered:
                    try:
                        ev = _run(
                            ["eventcreate", "/L", "Application", "/SO", "ByteraRMM",
                             "/T", "INFORMATION", "/ID", event_id, "/D", f"rmm {kind}"],
                            capture_output=True, text=True,
                        )
                        triggered = (ev.returncode == 0)
                        if not triggered:
                            trig_err = (ev.stderr or ev.stdout or "").strip()
                    except Exception as e:
                        trig_err = str(e)
                if triggered:
                    _print(f"[agent] {kind}: SYSTEM-Task '{taskname}' per Event {event_id} ausgelöst (elevated).")
                    return {"stage": "launched",
                            "detail": f"SYSTEM task '{taskname}' triggered via event (elevated)."}
                else:
                    _print(f"[agent] event trigger failed: {trig_err}")

            # (B) Sonst: SYSTEM-Task direkt anlegen (klappt nur, wenn Agent elevated).
            created = False
            schtasks_err = ""
            try:
                r = _run(
                    ["schtasks", "/create", "/tn", taskname, "/ru", "SYSTEM",
                     "/rl", "HIGHEST", "/sc", "ONCE", "/st", "00:00", "/f", "/tr", tr],
                    capture_output=True, text=True,
                )
                if r.returncode == 0:
                    _run(["schtasks", "/run", "/tn", taskname],
                                   capture_output=True, text=True)
                    created = True
                    _print(f"[agent] {kind}-Befehl als SYSTEM-Task '{taskname}' gestartet.")
                else:
                    schtasks_err = (r.stderr or r.stdout or "").strip()
                    _print(f"[agent] schtasks (SYSTEM) fehlgeschlagen: {schtasks_err} - Fallback.")
            except Exception as e:
                schtasks_err = str(e)
                _print(f"[agent] schtasks-Aufruf fehlgeschlagen ({e}) - Fallback.")
            if created:
                return {"stage": "launched", "detail": f"SYSTEM-Task '{taskname}' gestartet."}

            # (C) Fallback: losgelöste PowerShell mit Agent-Rechten (reicht nur, wenn
            #     der Agent schon elevated läuft).
            flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
            subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-Command", inner_ps],
                creationflags=flags, close_fds=True,
            )
            _print(f"[agent] {kind}-Befehl (Windows Fallback, losgelöst) gestartet.")
            return {"stage": "launched-fallback",
                    "detail": ("No elevated path available (agent not running as admin and "
                               "no maintenance tasks installed). Run ONCE as administrator "
                               f"ausführen: powershell -NoProfile -ExecutionPolicy Bypass -Command "
                               f"\"iwr '{BACKEND_URL}/agent-dist/elevate.ps1' -UseBasicParsing | iex\"")}
        else:
            fname = "update.sh" if kind == "update" else "uninstall.sh"
            url = f"{BACKEND_URL}/agent-dist/{fname}"
            inner = f"curl -fsSL '{url}' | bash"
            logf = f"/tmp/bytera-agent-{kind}.log"
            _print(f"[agent] {kind}: exakter Befehl (Linux, als root):")
            _print(f"         {inner}   (Log: {logf})")

            systemd_run = shutil.which("systemd-run") or (
                "/usr/bin/systemd-run" if os.path.exists("/usr/bin/systemd-run") else None
            )
            launched = False
            if systemd_run:
                try:
                    # Transienter, unabhängiger Dienst (eigener cgroup) -> überlebt
                    # das Stoppen des Agent-Dienstes. --collect räumt ihn nach Ende auf.
                    subprocess.Popen(
                        [systemd_run, "--collect", "--quiet",
                         "bash", "-c", f"{inner} >{logf} 2>&1"],
                        start_new_session=True, close_fds=True,
                    )
                    launched = True
                    _print(f"[agent] {kind}-Befehl über systemd-run (transienter Dienst) gestartet.")
                except Exception as e:
                    _print(f"[agent] systemd-run fehlgeschlagen ({e}) - nutze Fallback.")
            if not launched:
                # Fallback ohne systemd: vollständig losgelöste Shell (setsid+nohup+&).
                subprocess.Popen(
                    ["setsid", "bash", "-c", f"nohup {inner} >{logf} 2>&1 &"],
                    start_new_session=True, close_fds=True,
                )
                _print(f"[agent] {kind}-Befehl über setsid/nohup (Fallback) gestartet.")
                return {"stage": "launched-fallback",
                        "detail": f"started without systemd (log: {logf})"}
            return {"stage": "launched", "detail": f"systemd-run gestartet (Log: {logf})"}
    except Exception as e:
        _print(f"[agent] {kind}-Befehl fehlgeschlagen: {e}")
        return {"stage": "error", "detail": str(e)}
    return {"stage": "launched", "detail": ""}


# --------------------------------------------------------------
# Zwischenablage-Synchronisierung (Remote Screen)
# --------------------------------------------------------------
# Das Dashboard kann die lokale Zwischenablage an diesen PC senden
# (screen-input type="clipboard-set") oder die hiesige Zwischenablage
# abfragen (screen-clipboard-get -> Antwort-Event "screen-clipboard").
# Genutzt wird pyperclip (wird per Bootstrap automatisch nachinstalliert);
# schlaegt das fehl, wird ein klarer Fehler zurueckgemeldet.

def _clipboard_get() -> str:
    import pyperclip
    return pyperclip.paste() or ""


def _clipboard_set(text: str) -> None:
    import pyperclip
    pyperclip.copy(text or "")


# --- Automatischer Zwischenablage-Sync (wie AnyDesk) -------------------------
# Waehrend einer aktiven Screen-Sitzung beobachtet ein Thread die hiesige
# Zwischenablage und PUSHT Aenderungen von selbst ans Dashboard (Event
# "screen-clipboard" mit push=True). Die Gegenrichtung (Dashboard -> Geraet)
# laeuft weiter ueber screen-input type="clipboard-set".
# "suppress" merkt sich den zuletzt VOM DASHBOARD gesetzten Text, damit der
# Watcher ihn nicht als "neue Aenderung" wieder zurueckschickt (Ping-Pong).
_clip_watch = {"thread": None, "suppress": ""}
_CLIP_MAX = 512 * 1024          # sehr grosse Inhalte nicht automatisch uebertragen


def _clipboard_watch_loop(loop):
    last = None
    first = True
    while _screen_stream["active"]:
        try:
            if sio.connected and _session_perm("clipboard"):
                text = _clipboard_get()
                if first:
                    last = text          # Bestand beim Start nicht pushen
                    first = False
                elif text and text != last:
                    last = text
                    if text != _clip_watch.get("suppress") and len(text) <= _CLIP_MAX:
                        _session_emit(loop, "screen-clipboard",
                                      {"text": text, "push": True})
        except Exception:
            pass                        # kein Clipboard-Zugriff: still weiterprobieren
        time.sleep(1.2)
    _clip_watch["thread"] = None


def _start_clipboard_watch(loop) -> None:
    # Windows-Dienst (Sitzung 0): Die Zwischenablage des Benutzers ist von hier
    # aus nicht sichtbar - dort beobachtet sie der HELFER in der Benutzersitzung
    # (Reply "clip" -> _handle_session_reply).
    if _needs_session_helper():
        return
    t = _clip_watch.get("thread")
    if t is not None and t.is_alive():
        return
    t = threading.Thread(target=_clipboard_watch_loop, args=(loop,), daemon=True)
    _clip_watch["thread"] = t
    t.start()


@sio.on("screen-clipboard-get", namespace="/agent")
async def on_screen_clipboard_get(data):
    """Liest die lokale Zwischenablage und schickt sie ans Dashboard zurueck."""
    if not _session_perm("clipboard"):
        await sio.emit("screen-clipboard", {"id": DEVICE_ID, "error": "Clipboard blocked by the user.",
                                            "code": "agent_clip_blocked"}, namespace="/agent")
        return
    loop = asyncio.get_event_loop()
    try:
        text = await loop.run_in_executor(None, _clipboard_get)
        await sio.emit("screen-clipboard", {"id": DEVICE_ID, "text": text},
                       namespace="/agent")
    except Exception as e:
        await sio.emit("screen-clipboard", {"id": DEVICE_ID, "error": str(e)},
                       namespace="/agent")


@sio.on("screen-input", namespace="/agent")
async def on_screen_input(data):
    """
    Simuliert eine Maus-/Tastatureingabe, die vom Dashboard kommt.
    data.type ist einer von: "move", "click", "scroll", "key", "text", "combo"
    """
    # Berechtigungen aus dem Sitzungsfenster (vom Benutzer geschaltet).
    kind = (data or {}).get("type")
    if kind == "clipboard-set":
        if not _session_perm("clipboard"):
            return
        # Vom Dashboard gesetzten Text merken, damit der Clipboard-Watcher ihn
        # nicht gleich wieder als "Aenderung am Geraet" zurueckschickt.
        _clip_watch["suppress"] = str((data or {}).get("text") or "")
    elif not _session_perm("input"):
        return
    # Laeuft ein Helfer in der Benutzersitzung? Dann MUSS die Eingabe dort
    # ausgefuehrt werden - aus Sitzung 0 erreicht kein Klick den Desktop.
    helper = _screen_stream.get("helper")
    if helper is not None and helper.alive:
        helper.send({"cmd": "input", "data": data})
        return

    if not _INPUT_AVAILABLE:
        return

    loop = asyncio.get_event_loop()
    # Input-Simulation ist blockierend -> in einen Thread auslagern
    await loop.run_in_executor(None, _apply_input, data)


def _apply_input(data):
    """Führt die eigentliche Eingabe-Simulation aus (läuft im Thread-Pool)."""
    try:
        kind = data.get("type")
        # Der gestreamte Monitor kann einen Offset zur Gesamtfläche haben
        # (z.B. zweiter Monitor rechts). Browser-Koordinaten sind relativ zum
        # gestreamten Bild -> Offset addieren, damit der Klick auf dem richtigen
        # Monitor landet.
        ox = int(_screen_stream.get("mon_left", 0) or 0)
        oy = int(_screen_stream.get("mon_top", 0) or 0)

        def _pos(d):
            return (int(d["x"]) + ox, int(d["y"]) + oy)

        if kind == "move":
            _mouse.position = _pos(data)

        elif kind == "click":
            _mouse.position = _pos(data)
            button = MouseButton.right if data.get("button") == "right" else MouseButton.left
            _mouse.click(button, 1)

        elif kind == "down":
            # Maustaste DRÜCKEN und gedrückt halten (Beginn eines Drag)
            _mouse.position = _pos(data)
            button = MouseButton.right if data.get("button") == "right" else MouseButton.left
            _mouse.press(button)

        elif kind == "up":
            # Maustaste LOSLASSEN (Ende eines Drag)
            _mouse.position = _pos(data)
            button = MouseButton.right if data.get("button") == "right" else MouseButton.left
            _mouse.release(button)

        elif kind == "double":
            # Doppelklick
            _mouse.position = _pos(data)
            _mouse.click(MouseButton.left, 2)

        elif kind == "scroll":
            _mouse.scroll(0, int(data.get("dy", 0)))

        elif kind == "text":
            # einzelnes Zeichen/kurzer Text tippen
            _keyboard.type(data.get("text", ""))

        elif kind == "key":
            # eine Sondertaste (Enter, Backspace, Pfeile, ...)
            key = _SPECIAL_KEYS.get(data.get("key"))
            if key:
                _keyboard.press(key)
                _keyboard.release(key)

        elif kind == "clipboard-set":
            # Zwischenablage des Dashboards auf diesem PC setzen (Clipboard-Sync)
            _clipboard_set(data.get("text", ""))

        elif kind == "combo":
            # Tastenkombination, z.B. Strg+C: data.keys = ["Control", "c"]
            keys = data.get("keys", [])
            resolved = [_SPECIAL_KEYS.get(k, k) for k in keys]
            for k in resolved:
                _keyboard.press(k)
            for k in reversed(resolved):
                _keyboard.release(k)
    except Exception as e:
        _print(f"[agent] {_at('log_input_error', err=e)}")


# --------------------------------------------------------------
# Programmstart
# --------------------------------------------------------------

def _install_exception_logging():
    """Sorgt dafuer, dass NICHTS mehr stumm stirbt: unbehandelte Fehler aus
    Threads und asyncio-Tasks landen im agent.log statt im Nirwana."""
    def _thread_hook(args):
        log.error("[agent] Unbehandelter Fehler in Thread %s: %r",
                  getattr(args.thread, "name", "?"), args.exc_value,
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    try:
        threading.excepthook = _thread_hook
    except Exception:
        pass
    def _loop_handler(loop, context):
        exc = context.get("exception")
        log.error("[agent] Unbehandelter Fehler im Event-Loop: %s%s",
                  context.get("message", ""), f" ({exc!r})" if exc else "")
    try:
        _AGENT_LOOP.set_exception_handler(_loop_handler)
    except Exception:
        pass


# ====================================================================
# Umbenennung RAPALLE.net RMM -> BYTERA RMM: Reste der Altinstallation
# --------------------------------------------------------------------
# Das Update-Skript zieht den Agenten bereits in den neuen Ordner um
# (/opt/bytera-rmm-agent bzw. C:\Program Files\ByteraRmmAgent) und
# entfernt die alten Dienste. Laeuft dabei etwas nicht durch (z.B. weil das
# alte Wartungs-Task das Skript gestartet hat und Windows es beim Loeschen
# beendet), raeumt der NEUE Agent beim Start selbst auf. Alles ist
# idempotent und fasst nur die alten Namen an - nie den eigenen Ordner.
# ====================================================================
_LEGACY_LINUX_DIR = "/opt/rapalle-rmm-agent"
_LEGACY_WIN_DIR = r"C:\Program Files\RapalleRmmAgent"
_LEGACY_UNIT = "rapalle-agent"
_LEGACY_NODE_UNIT = "rapalle-node"
_LEGACY_WIN_TASKS = ("RapalleRmmAgent", "RapalleRmmUpdate", "RapalleRmmUninstall", "RapalleNode")
_LEGACY_NODE_PLIST = "/Library/LaunchDaemons/net.rapalle.node.plist"


def _legacy_rebrand_cleanup() -> None:
    if os.environ.get("RMM_SCREEN_HELPER") == "1":
        return
    me = os.path.normcase(os.path.dirname(os.path.abspath(__file__)))
    try:
        if IS_WINDOWS:
            legacy = os.path.normcase(_LEGACY_WIN_DIR)
            if me == legacy:
                return                       # wir SIND noch die Altinstallation -> nichts anfassen
            for t in _LEGACY_WIN_TASKS:
                if _run(["schtasks", "/query", "/tn", t], capture_output=True, text=True).returncode == 0:
                    _run(["schtasks", "/end", "/tn", t], capture_output=True, text=True)
                    _run(["schtasks", "/delete", "/tn", t, "/f"], capture_output=True, text=True)
                    _print(f"[agent] Umbenennung: alte Aufgabe '{t}' entfernt")
            try:
                _run(["powershell", "-NoProfile", "-Command",
                      "if ([System.Diagnostics.EventLog]::SourceExists('RapalleRMM')) "
                      "{ Remove-EventLog -Source 'RapalleRMM' }"], capture_output=True, text=True)
            except Exception:
                pass
            pd = os.environ.get("ProgramData") or r"C:\ProgramData"
            # Treiber-Cache des virtuellen Bildschirms in den neuen Ordner holen
            # (Die alten WireGuard-Dateien unter ProgramData\\Rapalle bleiben liegen, solange ein
            #  Tunneldienst sie noch benutzen koennte - _wg_down raeumt sie beim Abbau mit weg.)
            for old, new in ((os.path.join(pd, "RapalleRMM", "vdisplay"), os.path.join(pd, "ByteraRMM", "vdisplay")),):
                if os.path.isdir(old):
                    os.makedirs(new, exist_ok=True)
                    for n in os.listdir(old):
                        if not os.path.exists(os.path.join(new, n)):
                            try:
                                shutil.move(os.path.join(old, n), os.path.join(new, n))
                            except Exception:
                                pass
                    shutil.rmtree(old, ignore_errors=True)
                    try:
                        os.rmdir(os.path.dirname(old))
                    except OSError:
                        pass
            if os.path.isdir(_LEGACY_WIN_DIR):
                shutil.rmtree(_LEGACY_WIN_DIR, ignore_errors=True)
                _print("[agent] Umbenennung: alter Programmordner entfernt")
        else:
            if me == os.path.normcase(_LEGACY_LINUX_DIR):
                return
            if hasattr(os, "geteuid") and os.geteuid() != 0:
                return
            for unit in (_LEGACY_UNIT, _LEGACY_NODE_UNIT):
                f = f"/etc/systemd/system/{unit}.service"
                if os.path.exists(f):
                    _run(["systemctl", "disable", "--now", unit], capture_output=True, text=True)
                    try:
                        os.unlink(f)
                    except OSError:
                        pass
                    _run(["systemctl", "daemon-reload"], capture_output=True, text=True)
                    _run(["systemctl", "reset-failed", unit], capture_output=True, text=True)
                    _print(f"[agent] Umbenennung: alter Dienst '{unit}' entfernt")
            if sys.platform == "darwin" and os.path.exists(_LEGACY_NODE_PLIST):
                _run(["launchctl", "unload", "-w", _LEGACY_NODE_PLIST], capture_output=True, text=True)
                try:
                    os.unlink(_LEGACY_NODE_PLIST)
                except OSError:
                    pass
            if os.path.isdir(_LEGACY_LINUX_DIR):
                shutil.rmtree(_LEGACY_LINUX_DIR, ignore_errors=True)
                _print("[agent] Umbenennung: alter Programmordner entfernt")
    except Exception as e:
        _print(f"[agent] Umbenennung: Aufraeumen der Altinstallation fehlgeschlagen: {e}")


async def main():
    _install_exception_logging()
    _print(f"[agent] {_at('log_device_name', name=DEVICE_NAME)}")
    _print(f"[agent] {_at('log_device_id', id=DEVICE_ID)}")
    _print(f"[agent] {_at('log_backend', url=BACKEND_URL)}")
    _print(f"[agent] Remote Screen: "
           + ("verfügbar" if _SCREEN_AVAILABLE
              else f"deaktiviert - mss/Pillow nicht ladbar ({_SCREEN_ERROR})"))
    if not _INPUT_AVAILABLE:
        _print(f"[agent] {_at('log_remote_ctl', state=_at('log_disabled'))} - pynput ({_INPUT_ERROR})")
    _print(f"[agent] {_at('log_remote_ctl', state=_at('log_available') if _INPUT_AVAILABLE else _at('log_disabled'))}")
    # Beweis-Zeile: Steht sie NICHT im Log, laeuft auf dem Client eine aeltere
    # agent.py - egal was die Versionsnummer behauptet.
    if IS_WINDOWS:
        _print("[agent] " + _at("log_session",
               sid=_win_session_id(), csid=_win_console_session_id(),
               helper=_at("log_helper_used") if _needs_session_helper()
                      else _at("log_helper_unused")))

    # Heartbeat läuft als eigene Hintergrund-Aufgabe, unabhängig von der Verbindung
    # Diagnose einhaengen, BEVOR irgendetwas anderes startet - alles was
    # danach schiefgeht, soll bereits im Puffer landen.
    _diag_install()
    _e2e_ensure_lib()                       # encryption library (older installations)
    _node_cfg_sync()                        # installed node: same pinned key
    # Temp-Benutzer-Verfall weiter ueberwachen (uebersteht Agent-Neustarts).
    if _tempusers_load():
        t = threading.Thread(target=_tempuser_watch_loop, daemon=True)
        _tempuser_thread["t"] = t
        t.start()
    # Reste der Installation unter altem Namen (RAPALLE.net RMM) wegraeumen
    threading.Thread(target=_legacy_rebrand_cleanup, name="legacy-cleanup", daemon=True).start()

    # Alle Dauerschleifen laufen unter dem Aufseher. Stirbt eine an einer
    # Ausnahme, wird sie neu gestartet statt lautlos zu verschwinden.
    _agent_supervise("loop-herzschlag", loop_heartbeat_agent)
    _agent_supervise("heartbeat", heartbeat_loop)
    _agent_supervise("diag-upload", diag_upload_loop)
    _agent_supervise("node-watchdog", node_watchdog_loop)

    # Verbindungsschleife: WIR steuern das Wiederverbinden selbst. Vor jedem
    # Versuch sauber trennen, damit die interne Client-Zustand nicht auf
    # "Already connected" hängen bleibt.
    if any(x in BACKEND_URL for x in ("localhost", "127.0.0.1")):
        _print(f"[agent] WARNUNG: BACKEND_URL zeigt auf {BACKEND_URL} - ein entfernter/"
               f"public client can NOT reach that. Set the public "
               f"Adresse eintragen (z.B. https://domain) bzw. im Dashboard unter "
               f"Einstellungen -> Allgemein -> 'Server-URL' setzen.")
    # --- Gestaffelter Erststart -------------------------------------
    # Aus der Geräte-ID wird eine feste Wartezeit innerhalb von
    # CONNECT_SPREAD_S abgeleitet. Damit klopfen nach einem Server-Neustart
    # nicht alle Agenten in derselben Sekunde an, sondern verteilt über das
    # Fenster - und der Server nimmt sie der Reihe nach auf.
    if CONNECT_SPREAD_S > 0:
        import hashlib as _hl
        _fingerprint = int(_hl.sha256(str(DEVICE_ID).encode("utf-8")).hexdigest()[:8], 16)
        offset = (_fingerprint % (CONNECT_SPREAD_S * 1000)) / 1000.0
        _print(f"[agent] Gestaffelter Start: warte {offset:.1f}s vor dem ersten Verbindungsversuch")
        await asyncio.sleep(offset)

    backoff = 3.0            # aktuelle Wartezeit zwischen zwei Versuchen
    attempts = 0             # Fehlversuche in Folge
    # Obergrenze der Wartezeit. Nach einigen Fehlversuchen geht der Agent in
    # einen ruhigen 10-Minuten-Takt ueber. Er gibt dabei NIE auf: Ist das
    # Backend tagelang weg, klopft er weiterhin alle zehn Minuten an und ist
    # binnen zehn Minuten wieder da, sobald es zurueckkommt. Ein Agent, der
    # aufgibt, muesste von Hand angefasst werden - genau das soll ein RMM
    # ersparen.
    BACKOFF_MAX = 600.0

    while True:
        _REGISTERED.clear()
        refused_busy = False
        try:
            if sio.connected:
                try:
                    await sio.disconnect()
                except Exception:
                    pass
            await _sio_connect_secure()
            backoff = 3.0    # Verbindung stand -> Backoff zurücksetzen
            attempts = 0
            # Blockiert, solange die Verbindung steht - mit Wachhund: Steht die
            # Leitung zwar, aber der Server bestaetigt die Anmeldung 5 Minuten
            # lang nicht (haengende/halb offene Verbindung, Server-Stoerung),
            # blieb der Agent frueher "verbunden" und im Dashboard offline.
            # Jetzt wird neu verbunden.
            _wait_task = asyncio.ensure_future(sio.wait())
            _t_conn = time.time()
            while not _wait_task.done():
                await asyncio.wait({_wait_task}, timeout=20)
                if (not _wait_task.done() and not _REGISTERED.is_set()
                        and time.time() - _t_conn > 300):
                    _print("[agent] Verbunden, aber seit 5 Minuten keine Anmelde-"
                           "Bestaetigung vom Server - verbinde neu")
                    break
            if not _wait_task.done():
                _wait_task.cancel()
            _print(f"[agent] {_at('log_disconnected')}")
        except Exception as e:
            # Der Server weist uns ab, weil er gerade andere Agenten aufnimmt.
            # Das ist KEIN Fehler, sondern die Warteschlange bei der Arbeit -
            # entsprechend ruhig protokollieren und in Ruhe erneut versuchen.
            refused_busy = "busy" in repr(e).lower()
            if refused_busy:
                _print("[agent] Server nimmt gerade andere Agenten auf - warte und versuche es erneut")
            else:
                _print(f"[agent] {_at('log_conn_failed', url=BACKEND_URL, err=repr(e))}")
        finally:
            _REGISTERED.clear()
            _E2E["ch"] = None

        # Immer sauber trennen, bevor der nächste Versuch startet.
        try:
            await sio.disconnect()
        except Exception:
            pass

        # Wartezeit vor dem nächsten Versuch: schrittweise länger, plus ein
        # Zufallsanteil. Ohne den Zufallsanteil laufen abgewiesene Agenten
        # synchron und stürmen den Server im Gleichtakt erneut ("Thundering
        # Herd") - genau das Verhalten, das den Container umgebracht hat.
        attempts += 1
        wait_s = min(BACKOFF_MAX, backoff) + random.uniform(0, min(30.0, backoff))
        if attempts in (5, 20) or attempts % 50 == 0:
            # Nicht jede Runde protokollieren - sonst laeuft bei einem
            # laengeren Ausfall das Log voll und verdeckt das Wesentliche.
            _print(f"[agent] {attempts} Verbindungsversuche erfolglos, "
                   f"naechster in {wait_s / 60:.1f} min - der Agent gibt nicht auf")
        await asyncio.sleep(wait_s)
        backoff = min(BACKOFF_MAX, backoff * 2 if refused_busy else backoff * 1.5)


# ==========================================================================
# SOFTWARE-PATCHING  (Protokoll 2)
# --------------------------------------------------------------------------
# Leitgedanke dieser Fassung: Der Agent sagt von sich aus, was er kann und
# was er tut. Die vorige Fassung schwieg im Fehlerfall - jede Quelle, die
# nicht ansprang, lieferte einfach eine leere Liste zurück. Ob "keine
# Updates vorhanden" oder "winget gar nicht erst gestartet" gemeint war,
# liess sich von aussen nicht unterscheiden. Deshalb jetzt:
#
#   * Jede Quelle liefert ein Ergebnis MIT Zustand (ok / Fehlertext), nie
#     nur eine leere Liste.
#   * Der Agent meldet seine Fähigkeiten schon beim Anmelden mit
#     (PATCH_PROTOCOL im 'register'), nicht erst auf Nachfrage. Das Backend
#     weiss dadurch VOR jedem Auftrag, ob der Client überhaupt patchen kann.
#   * Jeder Handler antwortet IMMER - auch wenn er selbst scheitert.
#     Ein stiller Handler ist von aussen nicht von einem fehlenden zu
#     unterscheiden, und genau diese Verwechslung hat zuletzt viel Zeit
#     gekostet.
#   * Während eines Scans laufend Fortschrittsmeldungen, damit niemand
#     minutenlang auf einen stehenden Balken schaut.
#
# Quellen: Windows Update (COM-API), winget, apt, dnf.
# Stufen:  security | critical | important | moderate | low | feature | other
# ==========================================================================

# Fähigkeitsstufe dieses Agenten. Wird beim Anmelden mitgeschickt; das
# Backend entscheidet daran, ob es einen Patch-Auftrag überhaupt schickt.
#   1 = erste Fassung (patch-scan/patch-apply, kein patch-ping)
#   2 = diese Fassung (Selbstauskunft, Fortschritt, Fehler je Quelle)
PATCH_PROTOCOL = 2

PATCH_EVENTS = ("patch-ping", "patch-scan", "patch-apply", "patch-selftest")

_PATCH_LEVELS = ("security", "critical", "important", "moderate",
                 "low", "feature", "other")


def _patch_log(msg: str) -> None:
    _print(f"[patch] {msg}")


# --------------------------------------------------------------------------
# Werkzeuge
# --------------------------------------------------------------------------

def _winget_exe() -> "str | None":
    """
    Pfad zu winget.exe.

    Der Agent läuft als Dienst unter SYSTEM. Dort liegt winget NICHT im PATH:
    die App-Execution-Aliase unter %LOCALAPPDATA%\\Microsoft\\WindowsApps gibt
    es nur für angemeldete Benutzer. Ein blosses "winget" endete deshalb im
    FileNotFoundError - der früher stillschweigend zu "keine Updates" wurde.
    """
    if not IS_WINDOWS:
        return None
    found = shutil.which("winget")
    if found:
        return found
    base = Path(os.environ.get("ProgramW6432") or r"C:\Program Files") / "WindowsApps"
    try:
        for pattern in ("Microsoft.DesktopAppInstaller_*_x64__*/winget.exe",
                        "Microsoft.DesktopAppInstaller_*/winget.exe"):
            for cand in sorted(base.glob(pattern), reverse=True):
                if cand.exists():
                    return str(cand)
    except Exception:
        pass
    return None


def _source(name: str, ok: bool, patches=None, error: str = "",
            note: str = "") -> dict:
    """
    Einheitliches Ergebnis EINER Quelle.

    'ok=False' heisst: die Quelle konnte nicht befragt werden. Das ist
    ausdrücklich etwas anderes als 'ok=True' mit leerer Liste ("befragt,
    nichts gefunden"). Genau diese Unterscheidung fehlte vorher.
    """
    return {"source": name, "ok": bool(ok), "patches": patches or [],
            "count": len(patches or []), "error": error[:500], "note": note[:300]}


def _is_admin() -> bool:
    """Läuft der Agent mit den Rechten, die zum Installieren nötig sind?"""
    try:
        if IS_WINDOWS:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        return os.geteuid() == 0
    except Exception:
        return False


# --------------------------------------------------------------------------
# Windows Update (COM)
# --------------------------------------------------------------------------
# Über die COM-API statt über das Modul PSWindowsUpdate: das ist auf den
# meisten Systemen nicht installiert, und eine Ferninstallation richtet mehr
# Schaden an als sie hilft.

_WU_SEARCH_PS = r"""
$ErrorActionPreference = 'Stop'
try {
  $session  = New-Object -ComObject Microsoft.Update.Session
  $searcher = $session.CreateUpdateSearcher()
  $result   = $searcher.Search("IsInstalled=0 and IsHidden=0")
  $out = @()
  foreach ($u in $result.Updates) {
    $cats = @()
    foreach ($c in $u.Categories) { $cats += $c.Name }
    $kb = @()
    foreach ($k in $u.KBArticleIDs) { $kb += "KB$k" }
    $out += [PSCustomObject]@{
      id        = $u.Identity.UpdateID
      name      = $u.Title
      version   = ($kb -join ',')
      severity  = [string]$u.MsrcSeverity
      categories= ($cats -join ',')
      size      = [int64]$u.MaxDownloadSize
      reboot    = [int]$u.InstallationBehavior.RebootBehavior
    }
  }
  # Immer ein Array ausgeben: ohne den Komma-Operator macht PowerShell aus
  # einem einzelnen Element ein Objekt und aus null Elementen gar nichts.
  ConvertTo-Json -InputObject @($out) -Compress -Depth 3
} catch {
  Write-Output ('{"__error__":"' + ($_.Exception.Message -replace '"','') + '"}')
}
"""

_WU_INSTALL_PS = r"""
$ErrorActionPreference = 'Stop'
$wanted = '__IDS__'.Split(',') | Where-Object { $_ -ne '' }
try {
  $session  = New-Object -ComObject Microsoft.Update.Session
  $searcher = $session.CreateUpdateSearcher()
  $result   = $searcher.Search("IsInstalled=0 and IsHidden=0")
  $coll = New-Object -ComObject Microsoft.Update.UpdateColl
  foreach ($u in $result.Updates) {
    if ($wanted -contains $u.Identity.UpdateID) {
      if (-not $u.EulaAccepted) { $u.AcceptEula() | Out-Null }
      $coll.Add($u) | Out-Null
    }
  }
  if ($coll.Count -eq 0) {
    Write-Output '{"installed":0,"reboot":false,"code":0,"note":"nichts passendes gefunden"}'
    exit
  }
  $dl = $session.CreateUpdateDownloader(); $dl.Updates = $coll; $dl.Download() | Out-Null
  $inst = $session.CreateUpdateInstaller(); $inst.Updates = $coll
  $r = $inst.Install()
  $reboot = [bool]$r.RebootRequired
  Write-Output ('{"installed":' + $coll.Count + ',"reboot":' + $reboot.ToString().ToLower() + ',"code":' + $r.ResultCode + '}')
} catch {
  Write-Output ('{"__error__":"' + ($_.Exception.Message -replace '"','') + '"}')
}
"""


def _ps(script: str, timeout: int = 600):
    """
    PowerShell ausführen.

    Die Ausgabekodierung wird ausdrücklich auf UTF-8 gestellt. Ohne das
    liefert PowerShell die Konsolen-Codepage (deutsch: cp850) - Update-Titel
    mit Umlauten kamen als Buchstabensalat an oder brachten das Einlesen
    ganz zum Scheitern.
    """
    prelude = "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
    return _run(["powershell", "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass", "-Command", prelude + script],
                timeout=timeout)


def _level_from_windows(severity: str, categories: str) -> str:
    """
    Schweregrad einer Windows-Aktualisierung bestimmen.

    Die Kategorie hat Vorrang vor MsrcSeverity: Microsoft lässt den
    Schweregrad bei vielen Sicherheitsupdates leer. Wer nur nach
    MsrcSeverity filtert, verpasst genau die Updates, die er wollte.
    """
    cats = (categories or "").lower()
    sev = (severity or "").strip().lower()
    if "security" in cats:
        return "security"
    if "critical" in cats:
        return "critical"
    if sev in ("critical", "important", "moderate", "low"):
        return sev
    if "definition" in cats:
        return "security"          # Virendefinitionen: sicherheitsrelevant
    if "driver" in cats:
        return "other"
    if "feature pack" in cats or "upgrade" in cats:
        return "feature"
    return "other"


def _scan_windows_os() -> dict:
    if not IS_WINDOWS:
        return _source("windows-update", True, note="kein Windows")
    try:
        res = _ps(_WU_SEARCH_PS, timeout=420)
    except subprocess.TimeoutExpired:
        return _source("windows-update", False,
                       error="Zeitüberschreitung bei der Windows-Update-Abfrage (>7 min)")
    except Exception as e:
        return _source("windows-update", False, error=f"{e.__class__.__name__}: {e}")

    raw = (res.stdout or "").strip()
    if not raw:
        err = (res.stderr or "").strip()[:400]
        if err:
            return _source("windows-update", False,
                           error=f"PowerShell meldete: {err}")
        return _source("windows-update", True, note="keine Ausgabe - nichts gefunden")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return _source("windows-update", False,
                       error=f"Antwort war kein JSON: {raw[:200]}")

    if isinstance(data, dict):
        if data.get("__error__"):
            return _source("windows-update", False, error=str(data["__error__"]))
        data = [data]

    out = []
    for u in data or []:
        uid = str(u.get("id") or "").strip()
        if not uid:
            continue
        out.append({
            "uid": uid,
            "name": u.get("name") or "Unbenannte Aktualisierung",
            "current_version": "",
            "available_version": u.get("version") or "",
            "source": "windows-update",
            "level": _level_from_windows(u.get("severity"), u.get("categories")),
            "size": int(u.get("size") or 0),
            # RebootBehavior: 0 = nie, 1 = immer, 2 = kann sein
            "needs_reboot": 1 if int(u.get("reboot") or 0) else 0,
        })
    return _source("windows-update", True, out)


# --------------------------------------------------------------------------
# winget
# --------------------------------------------------------------------------

def _winget_columns(header: str) -> "dict | None":
    """
    Spaltenanfänge aus der Kopfzeile ableiten - ohne die Spalten beim Namen
    zu nennen.

    Der frühere Weg suchte die Wörter "Id" und "Available". Auf einem
    deutschen Windows heissen die Spalten "Kennung" und "Verfügbar", auf
    einem französischen wieder anders: die Kopfzeile wurde nie erkannt und
    der Scan meldete grundsätzlich null Anwendungsupdates.

    Womit man rechnen kann, ist die Formatierung: winget trennt seine
    Spalten immer durch MINDESTENS zwei Leerzeichen und richtet die Werte
    darunter linksbündig an derselben Position aus. Wir lesen also nur die
    Startspalten ab und ignorieren, was in der Überschrift steht.

    Spaltenfolge bei winget stabil: Name, Kennung, Version, Verfügbar, Quelle.
    """
    starts = [m.start() for m in re.finditer(r"(?<=\s)\S", "  " + header)]
    # "  " vorangestellt, damit auch die erste Spalte erkannt wird; die
    # Verschiebung um zwei Zeichen wieder herausrechnen.
    starts = sorted({max(0, s - 2) for s in starts})
    # Nur echte Spaltenanfänge behalten: solche, vor denen zwei Leerzeichen
    # stehen (innerhalb einer Überschrift wie "Verfügbare Version" steht nur
    # eines).
    cols = [0] + [s for s in starts if s >= 2 and header[s - 2:s] == "  "]
    cols = sorted(set(cols))
    if len(cols) < 4:
        return None
    return {"name": cols[0], "id": cols[1], "cur": cols[2],
            "avail": cols[3], "end": cols[4] if len(cols) > 4 else None}


def _scan_winget() -> dict:
    if not IS_WINDOWS:
        return _source("winget", True, note="kein Windows")
    exe = _winget_exe()
    if not exe:
        return _source("winget", False, error=(
            "winget was not found. Under the SYSTEM service account it is not "
            "on the PATH; the 'App Installer' package may also be missing."))
    try:
        res = _run([exe, "upgrade", "--include-unknown",
                    "--accept-source-agreements", "--disable-interactivity"],
                   timeout=240)
    except subprocess.TimeoutExpired:
        return _source("winget", False, error="Zeitüberschreitung bei winget (>4 min)")
    except Exception as e:
        return _source("winget", False, error=f"{e.__class__.__name__}: {e}")

    lines = [l.rstrip() for l in (res.stdout or "").splitlines()]
    # Die Trennzeile dient nur dazu, die Kopfzeile zu FINDEN: sie besteht in
    # jeder Sprache aus Bindestrichen. Die Spaltenpositionen kommen dann aus
    # der Zeile darüber.
    sep_idx = next((i for i, l in enumerate(lines)
                    if l.strip() and set(l.strip()) == {"-"}
                    and len(l.strip()) > 20), None)
    if sep_idx is None or sep_idx == 0:
        # Kein Tabellenkopf: entweder wirklich nichts zu tun, oder winget hat
        # sich beschwert. Beides unterscheidbar machen statt zu schweigen.
        text = " ".join(l.strip() for l in lines if l.strip())
        if res.returncode != 0:
            return _source("winget", False,
                           error=f"winget endete mit Code {res.returncode}: {text[:300]}")
        return _source("winget", True,
                       note="keine Tabelle in der Ausgabe - nichts zu aktualisieren")

    cols = _winget_columns(lines[sep_idx - 1])
    if not cols:
        return _source("winget", False, error=(
            "Could not detect the column layout of the winget table. Header: "
            f"{lines[sep_idx - 1][:160]}"))

    out = []
    for line in lines[sep_idx + 1:]:
        if not line.strip() or set(line.strip()) <= {"-", " "}:
            continue
        name = line[cols["name"]:cols["id"]].strip()
        pkg = line[cols["id"]:cols["cur"]].strip()
        cur = line[cols["cur"]:cols["avail"]].strip()
        avail_field = line[cols["avail"]:cols["end"]] if cols["end"] else line[cols["avail"]:]
        avail = avail_field.strip().split(" ")[0] if avail_field.strip() else ""
        # Fusszeilen ("2 Upgrades verfügbar.") laufen quer über die Spalten:
        # in der Kennung-Spalte stünde dann Text mit Leerzeichen.
        if not pkg or not name or " " in pkg:
            continue
        if not avail or avail.lower() in ("unknown", "unbekannt"):
            continue
        out.append({
            "uid": pkg, "name": name,
            "current_version": cur, "available_version": avail,
            "source": "winget",
            # winget kennt keine Schweregrade. Als 'security' zu raten wäre
            # gefährlich - dann würden Sicherheitsregeln beliebige Apps
            # mitaktualisieren.
            "level": "other",
            "size": 0, "needs_reboot": 0,
        })
    return _source("winget", True, out)


# --------------------------------------------------------------------------
# apt / dnf
# --------------------------------------------------------------------------

def _has(binary: str) -> bool:
    return bool(shutil.which(binary))


def _scan_apt() -> dict:
    """
    LC_ALL=C erzwingt englische Ausgabe - sonst heisst es auf deutschen
    Systemen "aktualisierbar von:" statt "upgradable from:". Beide
    Schreibweisen werden trotzdem erkannt, falls die Locale nicht greift.
    """
    env = {**os.environ, "LC_ALL": "C", "LANG": "C",
           "DEBIAN_FRONTEND": "noninteractive"}
    note = ""
    try:
        upd = _run(["apt-get", "update", "-qq"], timeout=240, env=env)
        if upd.returncode != 0:
            # Kein Abbruch: die Paketliste kann veraltet, aber brauchbar sein.
            note = f"apt-get update meldete Code {upd.returncode}"
    except Exception as e:
        note = f"apt-get update übersprungen ({e})"
    try:
        res = _run(["apt", "list", "--upgradable"], timeout=180, env=env)
    except Exception as e:
        return _source("apt", False, error=f"{e.__class__.__name__}: {e}")

    pattern = re.compile(
        r"^(?P<pkg>[^/\s]+)/(?P<repo>\S+)\s+(?P<avail>\S+)\s+\S+"
        r"\s+\[(?:upgradable from|aktualisierbar von):\s*(?P<cur>[^\]]+)\]")
    out = []
    for line in (res.stdout or "").splitlines():
        m = pattern.match(line.strip())
        if not m:
            continue
        out.append({
            "uid": m.group("pkg"), "name": m.group("pkg"),
            "current_version": m.group("cur").strip(),
            "available_version": m.group("avail"),
            "source": "apt",
            # Debian/Ubuntu kennzeichnen Sicherheitsaktualisierungen über die
            # Quelle ("...-security"). Das ist die einzige verlässliche Angabe.
            "level": "security" if "security" in m.group("repo").lower() else "other",
            "size": 0, "needs_reboot": 0,
        })
    return _source("apt", True, out, note=note)


def _scan_dnf() -> dict:
    try:
        # check-update endet planmässig mit Code 100, wenn es Updates gibt.
        res = _run(["dnf", "-q", "check-update"], timeout=240)
    except Exception as e:
        return _source("dnf", False, error=f"{e.__class__.__name__}: {e}")
    if res.returncode not in (0, 100):
        return _source("dnf", False, error=(
            f"dnf check-update endete mit Code {res.returncode}: "
            f"{(res.stderr or '').strip()[:300]}"))

    sec = set()
    try:
        s = _run(["dnf", "-q", "updateinfo", "list", "security"], timeout=240)
        for line in (s.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 3:
                sec.add(parts[-1].rsplit("-", 2)[0])
    except Exception:
        pass

    out = []
    for line in (res.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 3 or line.startswith(("Last metadata", "Obsoleting", "Security")):
            continue
        pkg = parts[0].rsplit(".", 1)[0]
        out.append({
            "uid": pkg, "name": pkg, "current_version": "",
            "available_version": parts[1], "source": "dnf",
            "level": "security" if pkg in sec else "other",
            "size": 0, "needs_reboot": 0,
        })
    return _source("dnf", True, out)


# --------------------------------------------------------------------------
# Selbstauskunft
# --------------------------------------------------------------------------

def _patch_sources() -> list:
    """Welche Quellen kommen auf DIESEM System überhaupt in Frage?"""
    if IS_WINDOWS:
        src = ["windows-update"]
        if _winget_exe():
            src.append("winget")
        return src
    src = []
    if _has("apt"):
        src.append("apt")
    elif _has("dnf"):
        src.append("dnf")
    return src


def _patch_capabilities() -> dict:
    """
    Wozu ist dieser Agent in Sachen Patching in der Lage? Wird beim Anmelden
    und bei jedem 'patch-ping' gemeldet - das Backend muss dadurch nicht mehr
    raten, ob ein Auftrag überhaupt Sinn ergibt.
    """
    return {
        "protocol": PATCH_PROTOCOL,
        "agent_version": AGENT_VERSION,
        "agent_code_hash": AGENT_CODE_HASH,
        "platform": platform.system(),
        "release": OS_RELEASE,
        "sources": _patch_sources(),
        "winget": _winget_exe() or "",
        "elevated": _is_admin(),
        "events": list(PATCH_EVENTS),
    }


# --------------------------------------------------------------------------
# Sammeln und Installieren
# --------------------------------------------------------------------------

def _collect_patches(progress=None) -> dict:
    """
    Alle Quellen befragen (blockierend, läuft im Thread).

    'progress' ist ein Rückruf(stage, detail, done, total) für die
    Fortschrittsmeldungen. Er darf niemals eine Ausnahme nach aussen tragen -
    ein kaputter Fortschrittsbalken darf keinen Scan abbrechen.
    """
    def tell(stage, detail, done, total):
        if not progress:
            return
        try:
            progress(stage, detail, done, total)
        except Exception:
            pass

    scanners = []
    if IS_WINDOWS:
        scanners.append(("windows-update", _scan_windows_os))
        scanners.append(("winget", _scan_winget))
    elif _has("apt"):
        scanners.append(("apt", _scan_apt))
    elif _has("dnf"):
        scanners.append(("dnf", _scan_dnf))

    total = len(scanners)
    sources, patches = [], []
    for idx, (name, fn) in enumerate(scanners):
        tell("scanning", name, idx, total)
        try:
            result = fn()
        except Exception as e:
            result = _source(name, False, error=f"{e.__class__.__name__}: {e}")
        sources.append({k: v for k, v in result.items() if k != "patches"})
        patches += result["patches"]
        state = f"{result['count']}" if result["ok"] else f"FEHLER: {result['error'][:120]}"
        _patch_log(f"{name}: {state}")
        tell("scanned", name, idx + 1, total)

    # Doppelte (gleiche Quelle + gleiche Kennung) entfernen.
    seen, unique = set(), []
    for p in patches:
        key = (p["source"], p["uid"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(p)

    return {
        "patches": unique,
        "sources": sources,
        "capabilities": _patch_capabilities(),
        "scanned_at": int(time.time() * 1000),
    }


def _apply_patches(items: list, progress=None) -> dict:
    """
    Ausgewählte Aktualisierungen installieren.

    'items' sind Einträge aus dem letzten Scan ({uid, source, name}). Es wird
    NIE etwas installiert, das nicht ausdrücklich benannt wurde - kein
    "alles aktualisieren" per Fernsteuerung.
    """
    def tell(stage, detail, done, total):
        if not progress:
            return
        try:
            progress(stage, detail, done, total)
        except Exception:
            pass

    installed, failed, reboot = [], [], False
    total = len(items)
    done = 0

    # --- Windows Update: alle in EINEM Durchgang, das ist deutlich schneller
    wu = [i for i in items if i.get("source") == "windows-update"]
    if wu and IS_WINDOWS:
        names = ", ".join(str(i.get("name") or i.get("uid"))[:40] for i in wu[:3])
        tell("installing", f"Windows Update ({len(wu)}): {names}", done, total)
        try:
            ids = ",".join(str(i["uid"]) for i in wu)
            res = _ps(_WU_INSTALL_PS.replace("__IDS__", ids), timeout=5400)
            lines = [l for l in (res.stdout or "").strip().splitlines() if l.strip()]
            data = json.loads(lines[-1]) if lines else {}
            if data.get("__error__"):
                for i in wu:
                    failed.append({"name": i.get("name") or i.get("uid"),
                                   "error": str(data["__error__"])})
            else:
                installed += [i.get("name") or i.get("uid") for i in wu]
                reboot = reboot or bool(data.get("reboot"))
        except Exception as e:
            for i in wu:
                failed.append({"name": i.get("name") or i.get("uid"),
                               "error": f"{e.__class__.__name__}: {e}"})
        done += len(wu)
        tell("installed", f"Windows Update: {len(wu)}", done, total)

    # --- alles Übrige einzeln
    for item in items:
        src = item.get("source")
        uid = item.get("uid")
        name = item.get("name") or uid or "?"
        if src == "windows-update":
            continue
        tell("installing", str(name), done, total)
        try:
            if src == "winget":
                exe = _winget_exe()
                if not exe:
                    raise RuntimeError("winget ist auf diesem Client nicht verfügbar")
                r = _run([exe, "upgrade", "--id", uid, "--exact", "--silent",
                          "--accept-package-agreements", "--accept-source-agreements",
                          "--disable-interactivity"], timeout=3600)
            elif src == "apt":
                env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive",
                       "LC_ALL": "C", "LANG": "C"}
                r = _run(["apt-get", "install", "-y", "--only-upgrade", uid],
                         timeout=3600, env=env)
            elif src == "dnf":
                r = _run(["dnf", "-y", "upgrade", uid], timeout=3600)
            else:
                raise RuntimeError(f"Unbekannte Quelle: {src}")

            if r.returncode == 0:
                installed.append(name)
            else:
                err = (r.stderr or r.stdout or "").strip()[-400:]
                failed.append({"name": name, "error": err or f"Code {r.returncode}"})
        except Exception as e:
            failed.append({"name": name, "error": f"{e.__class__.__name__}: {e}"})
        done += 1
        tell("installed", str(name), done, total)

    if IS_WINDOWS:
        # Ein ausstehender Neustart lässt sich zuverlässig an diesem
        # Registrierungsschlüssel ablesen.
        try:
            chk = _ps("if (Test-Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\"
                      "CurrentVersion\\WindowsUpdate\\Auto Update\\RebootRequired')"
                      " { 'yes' } else { 'no' }", timeout=60)
            if "yes" in (chk.stdout or ""):
                reboot = True
        except Exception:
            pass
    elif Path("/var/run/reboot-required").exists():
        reboot = True

    return {"installed": installed, "failed": failed, "needs_reboot": reboot}


# --------------------------------------------------------------------------
# Ereignis-Handler
# --------------------------------------------------------------------------
# Grundregel: JEDER Handler antwortet, auch wenn er selbst scheitert. Ein
# stiller Handler ist von aussen nicht von einem fehlenden zu unterscheiden -
# das Backend meldet dann fälschlich "Agent zu alt", obwohl in Wahrheit eine
# Ausnahme geflogen ist.

async def _patch_reply(event: str, payload: dict) -> None:
    try:
        await sio.emit(event, payload, namespace="/agent")
    except Exception as e:
        _patch_log(f"could not send reply '{event}': {e}")


def _patch_progress_sender(request_id: str, kind: str):
    """
    Baut einen Rückruf, der aus dem Arbeits-Thread heraus Fortschritt meldet.
    Die Emission muss zurück auf den Event-Loop - deshalb
    run_coroutine_threadsafe.
    """
    loop = _AGENT_LOOP

    def send(stage: str, detail: str, done: int, total: int) -> None:
        payload = {"id": DEVICE_ID, "requestId": request_id, "kind": kind,
                   "stage": stage, "detail": detail, "done": done, "total": total}
        try:
            asyncio.run_coroutine_threadsafe(
                sio.emit("patch-progress", payload, namespace="/agent"), loop)
        except Exception:
            pass
    return send


@sio.on("patch-ping", namespace="/agent")
async def on_patch_ping(data):
    """Selbstauskunft. Antwortet sofort, ohne irgendetwas zu starten."""
    request_id = (data or {}).get("requestId")
    _patch_log("capability query received")
    try:
        caps = _patch_capabilities()
    except Exception as e:
        caps = {"protocol": PATCH_PROTOCOL, "error": f"{e.__class__.__name__}: {e}"}
    await _patch_reply("patch-ping-result",
                       {"requestId": request_id, "ok": True, **caps})


@sio.on("patch-selftest", namespace="/agent")
async def on_patch_selftest(data):
    """
    Kurzer Funktionstest OHNE vollständigen Scan: prüft, ob die Quellen
    ansprechbar sind. Dauert Sekunden statt Minuten und beantwortet die
    Frage "liegt es am Agenten oder am System?".
    """
    request_id = (data or {}).get("requestId")
    _patch_log("Selbsttest angefordert")
    checks = []

    def check(name, fn):
        try:
            checks.append({"name": name, **fn()})
        except Exception as e:
            checks.append({"name": name, "ok": False,
                           "detail": f"{e.__class__.__name__}: {e}"})

    if IS_WINDOWS:
        def wu():
            r = _ps("try { $s = New-Object -ComObject Microsoft.Update.Session; "
                    "if ($s) { 'ok' } } catch { 'ERR ' + $_.Exception.Message }",
                    timeout=90)
            txt = (r.stdout or "").strip()
            return {"ok": txt.startswith("ok"), "detail": txt[:300] or "keine Ausgabe"}
        check("Windows-Update-COM", wu)

        def wg():
            exe = _winget_exe()
            if not exe:
                return {"ok": False, "detail": "winget.exe nicht gefunden"}
            r = _run([exe, "--version"], timeout=60)
            return {"ok": r.returncode == 0,
                    "detail": (r.stdout or r.stderr or "").strip()[:200] or exe}
        check("winget", wg)
    else:
        for tool in ("apt", "dnf"):
            if _has(tool):
                check(tool, lambda t=tool: {
                    "ok": True, "detail": shutil.which(t) or t})

    check("Rechte", lambda: {"ok": _is_admin(),
                             "detail": "elevated" if _is_admin() else
                                       "NOT elevated - installing will fail"})

    await _patch_reply("patch-selftest-result", {
        "requestId": request_id, "ok": True,
        "capabilities": _patch_capabilities(), "checks": checks})


@sio.on("patch-scan", namespace="/agent")
async def on_patch_scan(data):
    """Nach verfügbaren Aktualisierungen suchen und melden."""
    request_id = (data or {}).get("requestId")
    _patch_log("Suche nach Aktualisierungen gestartet")
    progress = _patch_progress_sender(request_id, "scan")
    try:
        result = await _AGENT_LOOP.run_in_executor(
            None, functools.partial(_collect_patches, progress))
        _patch_log(f"Suche beendet: {len(result['patches'])} Aktualisierung(en)")
        await _patch_reply("patch-scan-result", {"requestId": request_id, **result})
    except Exception as e:
        _patch_log(f"Suche fehlgeschlagen: {e.__class__.__name__}: {e}")
        await _patch_reply("patch-scan-result", {
            "requestId": request_id,
            "error": f"{e.__class__.__name__}: {e}",
            "capabilities": _patch_capabilities()})


@sio.on("patch-apply", namespace="/agent")
async def on_patch_apply(data):
    """Benannte Aktualisierungen installieren."""
    request_id = (data or {}).get("requestId")
    items = (data or {}).get("items") or []
    if not items:
        await _patch_reply("patch-apply-result", {
            "requestId": request_id, "error": "Keine Aktualisierungen angegeben"})
        return

    _patch_log(f"Installiere {len(items)} Aktualisierung(en)")
    await _emit_action_log("patch", "started", f"{len(items)} Aktualisierung(en)")
    progress = _patch_progress_sender(request_id, "apply")
    try:
        result = await _AGENT_LOOP.run_in_executor(
            None, functools.partial(_apply_patches, items, progress))
        note = (f"{len(result['installed'])} installiert, "
                f"{len(result['failed'])} fehlgeschlagen")
        _patch_log(note)
        await _emit_action_log("patch", "finished", note)
        await _patch_reply("patch-apply-result", {"requestId": request_id, **result})
    except Exception as e:
        msg = f"{e.__class__.__name__}: {e}"
        _patch_log(f"Installation fehlgeschlagen: {msg}")
        await _emit_action_log("patch", "failed", msg)
        await _patch_reply("patch-apply-result", {"requestId": request_id, "error": msg})


# Beim Laden einmal ausgeben, WELCHE Patch-Ereignisse dieser Agent kennt.
# Damit steht in der Agent-Konsole schwarz auf weiss, ob die neue Fassung
# wirklich läuft - genau die Frage, die sich aus der Ferne sonst nicht
# beantworten lässt.
_patch_log(f"Modul geladen - Protokoll {PATCH_PROTOCOL}, "
           f"Ereignisse: {', '.join(PATCH_EVENTS)}")


if __name__ == "__main__":
    # Helfer-Modus: Der Agent hat sich selbst in der Benutzersitzung gestartet,
    # um dort den Bildschirm aufzunehmen (siehe _ScreenHelper). In diesem Modus
    # wird KEINE Verbindung zum Backend aufgebaut - der Helfer redet nur mit
    # dem Haupt-Agenten auf 127.0.0.1.
    # WICHTIG: auf dem OBEN gesetzten Loop laufen (nicht asyncio.run(), das einen
    # neuen Loop anlegen würde) -> derselbe Loop, auf dem 'sio' erstellt wurde.
    asyncio.set_event_loop(_AGENT_LOOP)
    try:
        _AGENT_LOOP.run_until_complete(main())
    except KeyboardInterrupt:
        pass
    except SystemExit:
        raise
    except BaseException:
        # CRASH-SCHUTZ: Traceback in last_crash.txt sichern (wird beim nächsten
        # erfolgreichen Registrieren ans Backend gemeldet und dem Nutzer
        # angezeigt), kurz warten und den Agenten NEU STARTEN.
        import traceback
        tb = traceback.format_exc()
        try:
            _crash_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_crash.txt")
            with open(_crash_file, "w", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n" + tb)
        except OSError:
            pass
        # NEUSTART-SCHUTZ: Stuerzt der Agent immer wieder SOFORT ab, wuerde
        # ein 5-Sekunden-Takt die Maschine dauerhaft belasten und das Log
        # zumuellen. Deshalb wird mitgezaehlt, wie oft kurz hintereinander
        # neu gestartet wurde, und die Wartezeit waechst bis auf 10 Minuten.
        # Aufgegeben wird trotzdem NIE - der Agent kommt immer wieder.
        _restart_state = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "restart_count.txt")
        _count, _last = 0, 0.0
        try:
            with open(_restart_state, "r", encoding="utf-8") as f:
                _parts = f.read().split(",")
                _count, _last = int(_parts[0]), float(_parts[1])
        except (OSError, ValueError, IndexError):
            pass
        # Laeuft der Agent laenger als 10 Minuten stabil, zaehlt der naechste
        # Absturz wieder als Einzelfall.
        _count = _count + 1 if (time.time() - _last) < 600 else 1
        try:
            with open(_restart_state, "w", encoding="utf-8") as f:
                f.write(f"{_count},{time.time()}")
        except OSError:
            pass
        _wait = min(5 * (2 ** min(_count - 1, 7)), 600)
        _print(f"[agent] ABSTURZ Nr. {_count} - Neustart in {_wait} Sekunden:\n" + tb)
        try:
            time.sleep(_wait)
        except Exception:
            pass
        os.execv(sys.executable, [sys.executable] + sys.argv)
    finally:
        try:
            _AGENT_LOOP.close()
        except Exception:
            pass

