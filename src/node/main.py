#!/usr/bin/env python3
"""
BYTERA RMM - NODE
======================

Eine Node ist ein kleiner, EIGENSTAENDIGER Prozess auf einem Client. Sie laeuft
unabhaengig vom Agenten (eigener Dienst/Prozess, eigene Verbindung, eigenes Token).
Der Agent installiert, startet und ueberwacht sie nur.

Was die Node tut
----------------
1. Verbindet sich mit dem Backend (Socket.IO, Namespace /node) und meldet sich an.
2. Prueft DOCKER und meldet den Zustand (oder dass Docker installiert werden muss).
3. Nimmt NODE-MODULE entgegen (Dateien aus src/modules/node/<name>/ des Backends,
   z.B. eine docker-compose.yml) und fuehrt sie aus.
4. Meldet regelmaessig Zustand und Logs.

Sicherheit: Die Node fuehrt NUR die unten definierten Befehle aus (keine freie Shell).
Compose-Dateien und Module kommen ausschliesslich vom Backend.

Konfiguration: node.json neben dieser Datei (schreibt der Agent bei der Installation):
    {"backend_url": "https://rmm.example.com", "client_id": "...", "token": "..."}
Optional per Umgebung: NODE_BACKEND_URL, NODE_CLIENT_ID, NODE_TOKEN.

Node-Module (siehe src/modules/backend/example/GUIDE.md, Abschnitt "Node-Teil")
-----------------------------------------------------------------------------
  kind "compose": docker-compose.yml wird mit `docker compose` verwaltet.
  kind "python":  node_module.py mit start(ctx) / stop(ctx) / action(ctx, name, data)
                  und optional OPS = {"befehl": fn(ctx, data)} - eigene Befehle, die das
                  Backend wie eingebaute aufrufen kann (z.B. "docker-api", "wol").

Seit 0.3.0 startet KEIN Modul von selbst: nach dem Installieren ist es AUS und muss
je Node einzeln aktiviert werden (Aktion "up" -> meta.activated). Module ohne diese
ausdrueckliche Aktivierung (auch alle aus aelteren Versionen) bleiben beim Start aus.

Seit 0.2.0 sind Docker-API, Wake-on-LAN, Seitenabruf (Browser) und Mandanten-Abruf
NICHT mehr fest eingebaut, sondern Node-Module der jeweiligen RMM-Module (docker, wol,
browser, tenants). Sie werden je Node einzeln (Node-App) oder gesammelt (Modul-App ->
Register "Nodes") nachgeladen - so liegen auf einer Node nur die Dateien, die sie braucht.
"""

__version__ = "0.3.0"

import asyncio
import base64
import hashlib
import importlib.util
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import traceback
from pathlib import Path

import socketio

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "node.json"
DATA = HERE / "data"
MODS = DATA / "modules"
LOG_FILE = DATA / "node.log"
PID_FILE = DATA / "node.pid"
STATUS_INTERVAL = 30
MAX_LOG_BYTES = 1_000_000

sio = socketio.AsyncClient(reconnection=False)
_log_buffer: list[str] = []
_python_modules: dict[str, object] = {}      # id -> geladenes node_module.py


# =========================================================================
# END-TO-END ENCRYPTION + TLS PINNING (same protocol as the agent, see backend
# app/security.py). The agent hands over the server key and the TLS
# fingerprint in node.json ("e2e_key", "tls_pin", "tls_port").
# =========================================================================
import contextvars
import struct
import threading

try:
    from cryptography.hazmat.primitives import hashes as _c_hashes, serialization as _c_ser
    from cryptography.hazmat.primitives.asymmetric import ec as _c_ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM as _C_AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF as _C_HKDF
    _E2E_LIB = True
except Exception:                                              # pragma: no cover
    _E2E_LIB = False

_E2E = {"ch": None, "tls_bad_until": 0.0}
_e2e_inner = contextvars.ContextVar("e2e_inner", default=False)


class _E2EChan:
    def __init__(self, k_send, k_recv, p_send, p_recv):
        self._s, self._r = _C_AESGCM(k_send), _C_AESGCM(k_recv)
        self._ps, self._pr = p_send, p_recv
        self._cs, self._hi, self._seen = 0, -1, set()
        self._lock = threading.Lock()

    def seal(self, data: bytes, aad: bytes = b"") -> bytes:
        with self._lock:
            n = self._ps + struct.pack(">Q", self._cs)
            self._cs += 1
        return n + self._s.encrypt(n, data, aad or None)

    def open(self, blob: bytes) -> bytes:
        if len(blob) < 28 or blob[:4] != self._pr:
            raise ValueError("bad message")
        ctr = struct.unpack(">Q", blob[4:12])[0]
        with self._lock:
            if ctr in self._seen or ctr <= self._hi - 4096:
                raise ValueError("replayed message")
            data = self._r.decrypt(blob[:12], blob[12:], None)
            self._seen.add(ctr)
            if ctr > self._hi:
                self._hi = ctr
                if len(self._seen) > 8192:
                    self._seen = {c for c in self._seen if c > self._hi - 4096}
        return data


def _e2e_pack(event: str, args: list) -> bytes:
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
    head = json.dumps({"e": event, "a": walk(list(args)), "b": [len(b) for b in blobs]},
                      separators=(",", ":"), default=str).encode()
    return struct.pack(">I", len(head)) + head + b"".join(blobs)


def _e2e_unpack(data: bytes):
    hl = struct.unpack(">I", data[:4])[0]
    head = json.loads(data[4:4 + hl])
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


def _e2e_auth(cfg: dict) -> "tuple[dict, _E2EChan]":
    key = base64.b64decode(cfg["e2e_key"] + "=" * (-len(cfg["e2e_key"]) % 4))
    kid = hashlib.sha256(key).hexdigest()[:16]
    eph = _c_ec.generate_private_key(_c_ec.SECP256R1())
    salt = os.urandom(16)
    shared = eph.exchange(_c_ec.ECDH(), _c_ec.EllipticCurvePublicKey.from_encoded_point(_c_ec.SECP256R1(), key))
    okm = _C_HKDF(algorithm=_c_hashes.SHA256(), length=64, salt=salt,
                  info=b"bytera-e2e-v1|" + kid.encode()).derive(shared)
    ch = _E2EChan(okm[:32], okm[32:], b"c2s\x00", b"s2c\x00")
    box = ch.seal(json.dumps({"client_id": cfg["client_id"], "token": cfg["token"], "ts": time.time()}).encode(),
                  aad=b"auth")
    epk = eph.public_key().public_bytes(_c_ser.Encoding.X962, _c_ser.PublicFormat.UncompressedPoint)
    return ({"e2e": {"v": 1, "kid": kid, "epk": base64.b64encode(epk).decode(),
                     "n": base64.b64encode(salt).decode(), "t": base64.b64encode(box).decode()}}, ch)


_raw_emit = sio.emit
_raw_trigger = sio._trigger_event


async def _e2e_emit(event, data=None, namespace=None, callback=None):
    ch = _E2E["ch"]
    if ch is not None and namespace == "/node" and event != "x":
        args = [] if data is None else (list(data) if isinstance(data, tuple) else [data])
        return await _raw_emit("x", ch.seal(_e2e_pack(event, args)), namespace=namespace, callback=callback)
    return await _raw_emit(event, data, namespace=namespace, callback=callback)


async def _e2e_trigger(event, namespace, *args):
    if (_E2E["ch"] is not None and namespace == "/node" and not _e2e_inner.get()
            and event not in ("connect", "disconnect", "connect_error", "x", "__disconnect_final")):
        return None                                   # plain event on an encrypted connection: injected
    return await _raw_trigger(event, namespace, *args)


sio.emit = _e2e_emit
sio._trigger_event = _e2e_trigger


@sio.on("x", namespace="/node")
async def _e2e_in(data):
    ch = _E2E["ch"]
    if ch is None or not isinstance(data, (bytes, bytearray)):
        return None
    try:
        event, args = _e2e_unpack(ch.open(bytes(data)))
    except Exception as exc:
        log(f"encrypted message rejected: {exc}")
        return None
    tok = _e2e_inner.set(True)
    try:
        return await _raw_trigger(event, "/node", *args)
    finally:
        _e2e_inner.reset(tok)


async def _connect_secure(cfg: dict) -> None:
    from urllib.parse import urlparse
    import aiohttp
    plan = []
    if cfg.get("e2e_key") and _E2E_LIB and cfg.get("tls_port") and cfg.get("tls_pin") \
            and time.time() > _E2E["tls_bad_until"]:
        u = urlparse(cfg["backend_url"])
        if u.scheme == "http" and u.hostname:
            plan.append((f"https://{u.hostname}:{int(cfg['tls_port'])}", "tls"))
    plan.append((cfg["backend_url"], "plain"))
    for url, kind in plan:
        if cfg.get("e2e_key") and _E2E_LIB:
            auth, ch = _e2e_auth(cfg)
        else:
            auth, ch = {"client_id": cfg["client_id"], "token": cfg["token"]}, None
        _E2E["ch"] = ch
        eio = sio.eio
        if eio.http is not None and not eio.http.closed:
            await eio.http.close()
        eio.http, eio.external_http = None, False
        if kind == "tls":
            eio.http = aiohttp.ClientSession(connector=aiohttp.TCPConnector(
                ssl=aiohttp.Fingerprint(bytes.fromhex(cfg["tls_pin"]))))
        log(f"Verbinde zu {url} (Namespace /node, {'end-to-end encrypted' if ch else 'NOT encrypted'}"
            f"{', pinned TLS' if kind == 'tls' else ''}) ...")
        try:
            await sio.connect(url, namespaces=["/node"], wait_timeout=30, auth=auth,
                              transports=["websocket"] if kind == "tls" else None)
            return
        except Exception as exc:
            _E2E["ch"] = None
            if kind == "tls":
                _E2E["tls_bad_until"] = time.time() + 3600
                log(f"TLS port not usable ({exc}) - using {cfg['backend_url']}")
                try:
                    await sio.disconnect()
                except Exception:
                    pass
                continue
            raise


# =========================================================================
# Hilfen
# =========================================================================

def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    _log_buffer.append(line)
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > MAX_LOG_BYTES:
            LOG_FILE.replace(LOG_FILE.with_suffix(".log.1"))
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_config() -> dict:
    cfg: dict = {}
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    cfg["backend_url"] = os.getenv("NODE_BACKEND_URL") or cfg.get("backend_url") or ""
    cfg["client_id"] = os.getenv("NODE_CLIENT_ID") or cfg.get("client_id") or ""
    cfg["token"] = os.getenv("NODE_TOKEN") or cfg.get("token") or ""
    cfg["e2e_key"] = os.getenv("NODE_E2E_KEY") or cfg.get("e2e_key") or ""
    return cfg


async def run(cmd: list[str], timeout: float = 120, cwd: Path | None = None, env: dict | None = None) -> tuple[int, str]:
    """Fuehrt einen Befehl aus (ohne Shell). Gibt (Exitcode, Ausgabe) zurueck."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(cwd) if cwd else None, env={**os.environ, **(env or {})},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except FileNotFoundError:
        return 127, f"Programm nicht gefunden: {cmd[0]}"
    except OSError as exc:
        return 126, str(exc)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return 124, f"Zeitueberschreitung nach {timeout:.0f}s"
    return proc.returncode or 0, (out or b"").decode(errors="replace")


def safe_join(base: Path, rel: str) -> Path:
    """Verhindert Pfad-Tricks (../) in mitgeschickten Dateinamen."""
    target = (base / rel).resolve()
    target.relative_to(base.resolve())
    return target


# =========================================================================
# Docker
# =========================================================================

def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


async def docker_status() -> dict:
    exe = shutil.which("docker")
    st = {"installed": bool(exe), "daemon": False, "compose": False, "version": None,
          "compose_version": None, "can_install": False, "hint": ""}
    system = platform.system()
    if exe:
        code, out = await run([exe, "version", "--format", "{{.Server.Version}}"], timeout=15)
        if code == 0 and out.strip():
            st["daemon"], st["version"] = True, out.strip().splitlines()[0]
        code, out = await run([exe, "compose", "version", "--short"], timeout=15)
        if code == 0 and out.strip():
            st["compose"], st["compose_version"] = True, out.strip().splitlines()[0]
        if not st["daemon"]:
            st["hint"] = "Docker ist installiert, aber der Dienst laeuft nicht (z.B. 'systemctl start docker' bzw. Docker Desktop starten)."
        elif not st["compose"]:
            st["hint"] = "Das Docker-Compose-Plugin fehlt (Paket 'docker-compose-plugin')."
    else:
        if system == "Linux":
            st["can_install"] = _is_root() and bool(shutil.which("curl") or shutil.which("wget"))
            st["hint"] = ("Docker ist nicht installiert. Es kann automatisch installiert werden."
                          if st["can_install"] else
                          "Docker ist nicht installiert. Die Node laeuft ohne Root-Rechte bzw. ohne curl/wget - bitte Docker manuell installieren (https://docs.docker.com/engine/install/).")
        elif system == "Windows":
            st["hint"] = "Docker ist nicht installiert. Bitte Docker Desktop installieren: https://docs.docker.com/desktop/setup/install/windows-install/"
        elif system == "Darwin":
            st["hint"] = "Docker ist nicht installiert. Bitte Docker Desktop installieren: https://docs.docker.com/desktop/setup/install/mac-install/"
        else:
            st["hint"] = "Docker ist nicht installiert."
    return st


async def docker_install() -> dict:
    """Installiert Docker (nur Linux, nur als Root). Windows/macOS: Hinweis."""
    if platform.system() != "Linux":
        raise RuntimeError("Automatische Installation gibt es nur unter Linux. Bitte Docker Desktop manuell installieren.")
    if not _is_root():
        raise RuntimeError("Die Node laeuft nicht als Root - Docker bitte manuell installieren.")
    if shutil.which("docker"):
        return {"already": True, "docker": await docker_status()}
    fetch = ("curl -fsSL https://get.docker.com -o /tmp/rmm-get-docker.sh" if shutil.which("curl")
             else "wget -qO /tmp/rmm-get-docker.sh https://get.docker.com")
    code, out = await run(["sh", "-c", fetch], timeout=120)
    if code != 0:
        raise RuntimeError("Installationsskript konnte nicht geladen werden: " + out[-300:])
    log("Docker wird installiert (get.docker.com) ...")
    code, out = await run(["sh", "/tmp/rmm-get-docker.sh"], timeout=900)
    if code != 0:
        raise RuntimeError("Docker-Installation fehlgeschlagen: " + out[-500:])
    if shutil.which("systemctl"):
        await run(["systemctl", "enable", "--now", "docker"], timeout=60)
    return {"installed": True, "docker": await docker_status()}


# =========================================================================
# Node-Module
# =========================================================================

def _meta_path(mid: str) -> Path:
    return MODS / mid / ".rmm-module.json"


def _read_meta(mid: str) -> dict:
    try:
        return json.loads(_meta_path(mid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_meta(mid: str, meta: dict) -> None:
    _meta_path(mid).write_text(json.dumps(meta, indent=2), encoding="utf-8")


def _valid_id(mid: str) -> str:
    mid = str(mid or "")
    if not mid or not all(c.isalnum() or c in "-_" for c in mid) or len(mid) > 32:
        raise ValueError("ungueltige Modul-ID")
    return mid


def _node_section(mid: str) -> dict:
    return _read_meta(mid).get("manifest", {}).get("node", {}) or {}


async def _compose(mid: str, *args: str, timeout: float = 300) -> tuple[int, str]:
    d = MODS / mid
    sec = _node_section(mid)
    cfile = sec.get("compose") or "docker-compose.yml"
    st = await docker_status()
    if not st["installed"] or not st["daemon"] or not st["compose"]:
        raise RuntimeError("Docker ist nicht einsatzbereit: " + (st["hint"] or "unbekannter Grund"))
    return await run(["docker", "compose", "-p", f"rmm-{mid}", "-f", cfile, *args], cwd=d, timeout=timeout)


class ModuleContext:
    """Wird python-Node-Modulen uebergeben."""

    def __init__(self, mid: str):
        self.id = mid
        self.dir = MODS / mid
        self.data_dir = MODS / mid / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.params = _read_meta(mid).get("params", {})
        # Adresse des RMM-Backends (z.B. fuer Weiterleitungen wie das Agent-Gateway)
        self.backend_url = load_config().get("backend_url", "")

    def log(self, msg: str) -> None:
        log(f"[{self.id}] {msg}")

    async def run(self, cmd: list[str], timeout: float = 120):
        return await run(cmd, timeout=timeout, cwd=self.dir)


async def _py_call(mid: str, fn: str, *args):
    mod = _python_modules.get(mid)
    f = getattr(mod, fn, None) if mod else None
    if not callable(f):
        return None
    res = f(ModuleContext(mid), *args)
    if asyncio.iscoroutine(res):
        res = await res
    return res


def _py_load(mid: str) -> None:
    path = MODS / mid / "node_module.py"
    if not path.is_file():
        raise RuntimeError("node_module.py fehlt")
    spec = importlib.util.spec_from_file_location(f"rmm_node_{mid}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _python_modules[mid] = mod


async def module_install(data: dict) -> dict:
    bundle = data.get("bundle") or {}
    mid = _valid_id(bundle.get("id"))
    params = data.get("params") or {}
    d = MODS / mid
    was_active = bool(_read_meta(mid).get("activated")) if d.exists() else False
    if d.exists():                                   # Neu einspielen: alten Stand sauber stoppen
        try:
            await module_action({"id": mid, "action": "down"})
        except Exception:
            pass
        for item in d.iterdir():
            if item.name == "data":
                continue
            shutil.rmtree(item) if item.is_dir() else item.unlink()
    d.mkdir(parents=True, exist_ok=True)
    for rel, b64 in (bundle.get("files") or {}).items():
        target = safe_join(d, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(b64))
    # Parameter als .env fuer docker compose (${NAME} in der compose-Datei)
    env_lines = [f"{str(k).upper()}={str(v).replace(chr(10), ' ')}" for k, v in params.items()
                 if str(k).replace("_", "").isalnum()]
    (d / ".env").write_text("\n".join(env_lines) + ("\n" if env_lines else ""), encoding="utf-8")
    _write_meta(mid, {"id": mid, "version": bundle.get("version"), "hash": bundle.get("hash"),
                      "manifest": bundle.get("manifest") or {}, "params": params,
                      "desired": "up" if was_active else "down", "activated": was_active,
                      "installed_at": int(time.time())})
    log(f"Modul '{mid}' v{bundle.get('version')} installiert" + ("" if was_active else " (aus - bitte aktivieren)"))
    # Kein Autostart mehr: jedes Modul wird je Node einzeln aktiviert. Nur ein beim
    # Neu-Einspielen bereits aktiviertes Modul laeuft gleich wieder.
    if was_active:
        try:
            return {"installed": True, "start": await module_action({"id": mid, "action": "up"})}
        except Exception as exc:
            return {"installed": True, "start_error": str(exc)}
    return {"installed": True, "active": False}
    return {"installed": True}


async def module_action(data: dict) -> dict:
    mid = _valid_id(data.get("id"))
    action = str(data.get("action") or "")
    if not (MODS / mid).is_dir():
        raise RuntimeError(f"Modul '{mid}' ist nicht installiert")
    sec = _node_section(mid)
    kind = sec.get("kind", "compose")
    allowed = sec.get("actions") or ["up", "down", "restart", "status", "logs"]
    if action not in allowed:
        raise RuntimeError(f"Aktion '{action}' ist fuer dieses Modul nicht erlaubt")
    meta = _read_meta(mid)
    if kind == "compose":
        if action == "up":
            code, out = await _compose(mid, "up", "-d", "--remove-orphans")
            meta["desired"] = "up"; meta["activated"] = True
        elif action == "down":
            code, out = await _compose(mid, "down")
            meta["desired"] = "down"; meta["activated"] = False
        elif action == "restart":
            code, out = await _compose(mid, "restart")
        elif action == "status":
            code, out = await _compose(mid, "ps", "--format", "json", timeout=60)
            return {"exit": code, "containers": _parse_ps(out)}
        elif action == "logs":
            code, out = await _compose(mid, "logs", "--tail", str(int(data.get("data", {}).get("lines", 100))), "--no-color", timeout=60)
            return {"exit": code, "log": out[-20000:]}
        else:
            raise RuntimeError(f"Unbekannte Aktion '{action}'")
        _write_meta(mid, meta)
        if code != 0:
            raise RuntimeError(out[-600:] or f"docker compose endete mit {code}")
        return {"exit": code, "output": out[-600:]}
    if kind == "python":
        # Geladen == aktiv: nur "up"/"restart" laden das Modul (sonst waeren seine
        # Befehle/OPS verfuegbar, obwohl es nicht aktiviert ist).
        if action == "up":
            if mid not in _python_modules:
                _py_load(mid)
            meta["desired"] = "up"; meta["activated"] = True; _write_meta(mid, meta)
            return {"result": await _py_call(mid, "start")}
        if action == "down":
            meta["desired"] = "down"; meta["activated"] = False; _write_meta(mid, meta)
            res = await _py_call(mid, "stop") if mid in _python_modules else {"stopped": True}
            _python_modules.pop(mid, None)           # gestoppt -> seine Befehle (OPS) sind weg
            return {"result": res}
        if action == "restart":
            if mid not in _python_modules:
                raise RuntimeError(f"Modul '{mid}' ist nicht aktiviert")
            await _py_call(mid, "stop")
            _py_load(mid)
            return {"result": await _py_call(mid, "start")}
        if action == "status":
            return {"result": {"running": mid in _python_modules,
                               "ops": sorted(getattr(_python_modules.get(mid), "OPS", {}) or {})}}
        return {"result": await _py_call(mid, "action", action, data.get("data") or {})}
    raise RuntimeError(f"Unbekannte Modulart '{kind}'")


def _parse_ps(out: str) -> list[dict]:
    items = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                c = json.loads(line)
                items.append({"name": c.get("Name"), "service": c.get("Service"), "state": c.get("State"),
                              "status": c.get("Status"), "ports": c.get("Publishers") or c.get("Ports")})
            except ValueError:
                pass
        elif line.startswith("["):
            try:
                for c in json.loads(line):
                    items.append({"name": c.get("Name"), "service": c.get("Service"), "state": c.get("State"),
                                  "status": c.get("Status"), "ports": c.get("Publishers") or c.get("Ports")})
            except ValueError:
                pass
    return items


async def module_remove(data: dict) -> dict:
    mid = _valid_id(data.get("id"))
    d = MODS / mid
    if not d.is_dir():
        return {"removed": False}
    try:
        await module_action({"id": mid, "action": "down"})
    except Exception as exc:
        log(f"Modul '{mid}': Stoppen beim Entfernen: {exc}")
    if mid in _python_modules:
        _python_modules.pop(mid, None)
    shutil.rmtree(d, ignore_errors=True)
    log(f"Modul '{mid}' entfernt")
    return {"removed": True}


async def module_list() -> dict:
    """Installierte Node-Module mit Zustand - Teil jedes Statusberichts."""
    out: dict[str, dict] = {}
    if not MODS.is_dir():
        return out
    for d in sorted(MODS.iterdir()):
        meta = _read_meta(d.name)
        if not meta:
            continue
        sec = meta.get("manifest", {}).get("node", {}) or {}
        entry = {"version": meta.get("version"), "kind": sec.get("kind", "compose"),
                 "desired": meta.get("desired"), "activated": bool(meta.get("activated")), "state": "unknown"}
        if entry["kind"] == "compose":
            try:
                st = await docker_status()
                if st["daemon"] and st["compose"]:
                    code, o = await _compose(d.name, "ps", "--format", "json", timeout=30)
                    cs = _parse_ps(o)
                    entry["containers"] = cs
                    entry["state"] = ("running" if cs and all(c["state"] == "running" for c in cs)
                                      else "stopped" if not cs else "partial")
                else:
                    entry["state"] = "no-docker"
            except Exception as exc:
                entry["state"], entry["error"] = "error", str(exc)[:200]
        else:
            entry["state"] = "running" if d.name in _python_modules else "stopped"
        out[d.name] = entry
    return out


async def reconcile() -> None:
    """Beim Start: Module, die laufen sollen, wieder hochfahren (idempotent)."""
    if not MODS.is_dir():
        return
    for d in sorted(MODS.iterdir()):
        meta = _read_meta(d.name)
        if meta.get("desired") == "up" and not meta.get("activated"):
            # aus aelterer Node-Version (Autostart) -> jetzt AUS, muss einzeln aktiviert werden
            try:
                await module_action({"id": d.name, "action": "down"})   # z.B. Compose-Container wirklich stoppen
            except Exception:
                meta["desired"] = "down"; meta["activated"] = False
                _write_meta(d.name, meta)
            log(f"Modul '{d.name}' ist aus - bitte in der Node-App/Modul-App aktivieren")
            continue
        if meta.get("desired") == "up":
            try:
                await module_action({"id": d.name, "action": "up"})
                log(f"Modul '{d.name}' gestartet")
            except Exception as exc:
                log(f"Modul '{d.name}' konnte nicht gestartet werden: {exc}")


# =========================================================================
# Befehle vom Backend
# =========================================================================

async def _full_report() -> dict:
    return {
        "version": __version__, "os": f"{platform.system()} {platform.release()}",
        "hostname": socket.gethostname(), "python": sys.version.split()[0],
        "docker": await docker_status(), "modules": await module_list(),
        "ops": sorted(_module_ops()),            # Befehle aus Node-Modulen (seit 0.2.0)
    }


def _module_ops() -> dict:
    """Befehle, die laufende python-Node-Module ueber OPS bereitstellen -> {op: (modul, fn)}"""
    out = {}
    for mid, mod in _python_modules.items():
        for op, fn in (getattr(mod, "OPS", None) or {}).items():
            if callable(fn):
                out[str(op)] = (mid, fn)
    return out


async def _run_module_op(op: str, data: dict):
    entry = _module_ops().get(op)
    if not entry:
        raise RuntimeError(f"Befehl '{op}' ist auf dieser Node nicht verfuegbar - das zugehoerige "
                           f"Node-Modul ist nicht installiert oder gestoppt (Node-App bzw. Modul-App -> Nodes).")
    mid, fn = entry
    if asyncio.iscoroutinefunction(fn):
        return await fn(ModuleContext(mid), data)
    return await asyncio.get_running_loop().run_in_executor(None, fn, ModuleContext(mid), data)


async def handle(op: str, data: dict):
    if op == "ping":
        return {"pong": True, "time": int(time.time())}
    if op == "status":
        return await _full_report()
    if op == "docker-status":
        return await docker_status()
    if op == "docker-install":
        return await docker_install()
    if op == "module-install":
        return await module_install(data)
    if op == "module-remove":
        return await module_remove(data)
    if op == "module-action":
        return await module_action(data)
    if op == "module-list":
        return await module_list()
    if op == "logs":
        try:
            return {"log": LOG_FILE.read_text(encoding="utf-8")[-20000:]}
        except OSError:
            return {"log": ""}
    return await _run_module_op(op, data)


@sio.on("node-cmd", namespace="/node")
async def on_cmd(payload):
    rid = (payload or {}).get("requestId")
    op = str((payload or {}).get("op") or "")
    try:
        result = await handle(op, (payload or {}).get("data") or {})
        await sio.emit("node-result", {"requestId": rid, "ok": True, "data": result}, namespace="/node")
    except Exception as exc:
        log(f"Befehl '{op}' fehlgeschlagen: {exc}")
        await sio.emit("node-result", {"requestId": rid, "ok": False, "error": str(exc)[:800]}, namespace="/node")
    await _flush_logs()


async def _flush_logs() -> None:
    if _log_buffer and sio.connected:
        text, _log_buffer[:] = "\n".join(_log_buffer[-50:]), []
        try:
            await sio.emit("node-log", {"text": text}, namespace="/node")
        except Exception:
            pass


# =========================================================================
# Hauptschleife
# =========================================================================

async def status_loop() -> None:
    while True:
        await asyncio.sleep(STATUS_INTERVAL)
        if sio.connected:
            try:
                await sio.emit("node-status", await _full_report(), namespace="/node")
                await _flush_logs()
            except Exception as exc:
                log(f"Status senden: {exc}")


async def connection_loop(cfg: dict) -> None:
    backoff = 3.0
    while True:
        try:
            await _connect_secure(cfg)
            backoff = 3.0
            await sio.emit("node-hello", await _full_report(), namespace="/node")
            log("verbunden")
            await sio.wait()
            log("Verbindung getrennt")
        except Exception as exc:
            log(f"Verbindung fehlgeschlagen: {exc}")
            if "unauthorized" in str(exc).lower():
                log("Das Backend lehnt dieses Node-Token ab (Node wurde entfernt?). Neuer Versuch in 60 s.")
                backoff = 60.0
        _E2E["ch"] = None
        try:
            await sio.disconnect()
        except Exception:
            pass
        await asyncio.sleep(backoff)
        backoff = min(backoff * 1.7, 60.0)


async def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    MODS.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    if not (cfg["backend_url"] and cfg["client_id"] and cfg["token"]):
        print("node.json unvollstaendig (backend_url, client_id, token)", file=sys.stderr)
        sys.exit(2)
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    log(f"Node {__version__} startet (Client {cfg['client_id']})")
    asyncio.create_task(status_loop())
    asyncio.create_task(reconcile())
    await connection_loop(cfg)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception:
        print(traceback.format_exc(), file=sys.stderr)
        sys.exit(1)
    finally:
        try:
            PID_FILE.unlink()
        except OSError:
            pass
