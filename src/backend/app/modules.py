"""
modules.py - module manager of the RMM
==================================

Modules are optional features (tickets, Guacamole, patching ...). The core runs
without any module. A module lives in

    src/modules/<id>/module.json  +  subfolders backend/ frontend/ node/ agent/ client/

A module can consist of SEVERAL parts (e.g. backend + frontend). The parts are
combined via the shared "id" in module.json into ONE module - the module app
shows only this one module.

Basic rules
-----------
* All modules are DISABLED by default.
* A module must NEVER take the backend down with it. Every call in module code
  (start, stop, routes, socket events, background tasks, event hooks) is
  guarded. On a crash a crash report is written, the module is disabled
  automatically and the error is shown in the module app.
* A module's Python packages ("pip" in module.json) are installed only on
  activation.

See src/modules/backend/example/GUIDE.md for the guide to building your own.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import re
import subprocess
import sys
import threading
import time
import traceback
import types
from pathlib import Path
from typing import Any, Callable

from app.paths import (
    MODULES_DIR, MODULES_STATE_FILE, MODULES_DATA_DIR, MODULE_CRASH_DIR,
)

AREAS = ("backend", "frontend", "agent", "client", "node")
_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
PKG_PREFIX = "rmm_mod_"                    # Python package name of a loaded backend module
MASK = "••••••••"                          # placeholder for stored passwords

# Events that the core reports to modules (hook names in module.json).
CORE_EVENTS = (
    # Lifecycle of the module itself (hook function gets only ctx)
    "startup", "shutdown",                          # module is loaded / unloaded
    "enable", "disable",                            # user turns the module on / off
    "before_disable",                               # veto: returning text = refuse shutdown
    # Message that ANOTHER module was toggled (data = {"module": id})
    "module.enabled", "module.disabled",
    # The same triggers as webhook/mail notifications (notifier.TRIGGERS).
    # data = {"event", "client_id", "notification"}
    "client_online", "client_offline", "client_new", "agent_update",
    "cpu_high", "ram_high", "disk_high", "temp_high",
    "warranty_expiring", "warranty_expired",
    "website_up", "website_down", "user_login", "login_failed",
    "user_deleted",                                 # user deleted (data: user_id, username)
    # Nodes (data = {"client_id"})
    "node_online", "node_offline", "node_removed",
)

_lock = threading.RLock()
_api = None            # FastAPI app
_sio = None            # Socket.IO server
_loop: asyncio.AbstractEventLoop | None = None
_registry: dict[str, "Module"] = {}
_discover_errors: list[dict] = []
_loaded: dict[str, "Loaded"] = {}
_state: dict = {}
_state_loaded = False


# =========================================================================
# Data model
# =========================================================================

class ModuleError(Exception):
    """Error that may be shown to the user in the module app."""


class Part:
    def __init__(self, area: str, path: Path, manifest: dict):
        self.area, self.path, self.manifest = area, path, manifest


class Module:
    def __init__(self, mid: str):
        self.id = mid
        self.parts: dict[str, Part] = {}

    # --- merged metadata (backend part takes precedence) ---------
    def _ordered(self) -> list[Part]:
        order = sorted(self.parts.values(), key=lambda p: AREAS.index(p.area))
        return order

    def meta(self, key: str, default=None):
        for p in self._ordered():
            if key in p.manifest and p.manifest[key] not in (None, ""):
                return p.manifest[key]
        return default

    def merged_list(self, key: str) -> list:
        out: list = []
        for p in self._ordered():
            for item in p.manifest.get(key, []) or []:
                if item not in out:
                    out.append(item)
        return out

    @property
    def requires(self) -> list[str]:
        return [str(x) for x in self.merged_list("requires")]

    @property
    def pip(self) -> list[str]:
        return [str(x) for x in self.merged_list("pip")]

    @property
    def features(self) -> list[dict]:
        seen, out = set(), []
        for p in self._ordered():
            for f in p.manifest.get("features", []) or []:
                if isinstance(f, dict) and f.get("id") and f["id"] not in seen:
                    seen.add(f["id"]); out.append(f)
        return out

    @property
    def settings_schema(self) -> list[dict]:
        seen, out = set(), []
        for p in self._ordered():
            for f in p.manifest.get("settings", []) or []:
                if isinstance(f, dict) and f.get("key") and f["key"] not in seen:
                    seen.add(f["key"]); out.append(f)
        return out

    @property
    def available(self) -> bool:
        return self.meta("available", True) is not False


class Loaded:
    """Runtime state of an active backend part."""

    def __init__(self, mid: str):
        self.mid = mid
        self.pkg = PKG_PREFIX + mid.replace("-", "_")
        self.routes: list = []
        self.tasks: list[asyncio.Task] = []
        self.sio_handlers: list[tuple[str, str]] = []     # (namespace, event)
        self.hooks: dict[str, str] = {}
        self.entry: types.ModuleType | None = None
        self.ctx: "ModuleContext | None" = None
        self.started = time.time()


# =========================================================================
# State (data/modules.json)
# =========================================================================

def _default_state() -> dict:
    return {"enabled": {}, "settings": {}, "features": {}, "crashes": {},
            "crash_limit": 1, "crash_window_s": 600}


def _load_state() -> None:
    global _state, _state_loaded
    with _lock:
        if _state_loaded:
            return
        st = _default_state()
        try:
            if MODULES_STATE_FILE.is_file():
                raw = json.loads(MODULES_STATE_FILE.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for k in st:
                        if k in raw and isinstance(raw[k], type(st[k])):
                            st[k] = raw[k]
        except Exception as exc:                # a broken file must not block anything
            print(f"[module] modules.json unreadable, starting empty: {exc}")
        _state, _state_loaded = st, True


def _save_state() -> None:
    with _lock:
        try:
            MODULES_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = MODULES_STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(_state, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(MODULES_STATE_FILE)
        except Exception as exc:
            print(f"[module] modules.json could not be saved: {exc}")


# =========================================================================
# Discover
# =========================================================================

def discover() -> dict[str, Module]:
    """Reads all modules under src/modules/<id>/ with ONE module.json and the
    subfolders backend/ frontend/ node/ ... (formerly: modules/<area>/<id>/)."""
    global _registry, _discover_errors
    reg: dict[str, Module] = {}
    errs: list[dict] = []
    if not MODULES_DIR.is_dir():
        with _lock:
            _registry, _discover_errors = reg, errs
        return reg
    for d in sorted(MODULES_DIR.iterdir()):
        mf = d / "module.json"
        if not d.is_dir() or not mf.is_file():
            continue
        try:
            man = json.loads(mf.read_text(encoding="utf-8"))
            if not isinstance(man, dict):
                raise ValueError("module.json must be a JSON object")
            mid = str(man.get("id") or d.name)
            if not _ID_RE.match(mid):
                raise ValueError(f"invalid id '{mid}' (a-z, 0-9, _ and -, 2-32 characters)")
            man["id"] = mid
            mod = reg.setdefault(mid, Module(mid))
            # One part per existing area subfolder - all share the ONE
            # module.json; the loader reads the matching keys per area.
            for area in AREAS:
                sub = d / area
                if sub.is_dir():
                    mod.parts[area] = Part(area, sub, man)
        except Exception as exc:
            errs.append({"path": str(mf.relative_to(MODULES_DIR.parent)), "error": str(exc)})
    with _lock:
        _registry, _discover_errors = reg, errs
    return reg


def get(mid: str) -> Module:
    if mid not in _registry:
        raise ModuleError(f"Unknown module: {mid}")
    return _registry[mid]


# =========================================================================
# Queries (also intended for core code)
# =========================================================================

def is_enabled(mid: str) -> bool:
    """True if the module is active AND loaded."""
    return mid in _loaded


def feature(mid: str, fid: str) -> bool:
    """Is the sub-feature 'fid' of the (active) module 'mid' turned on?"""
    if mid not in _loaded:
        return False
    return _feature_on(mid, fid)


def _feature_on(mid: str, fid: str) -> bool:
    mod = _registry.get(mid)
    if not mod:
        return False
    stored = _state.get("features", {}).get(mid, {})
    if fid in stored:
        return bool(stored[fid])
    for f in mod.features:
        if f["id"] == fid:
            return bool(f.get("default", True))
    return False


def use(mid: str, name: str):
    """
    Returns the Python file <name>.py of an ACTIVE backend module or None.
    This is how core code optionally accesses module functions:

        rec = modules.use("recordings", "recording")
        if rec: rec.record_frame(...)
    """
    ld = _loaded.get(mid)
    if not ld:
        return None
    try:
        return importlib.import_module(f"{ld.pkg}.{name}")
    except Exception as exc:
        report_crash(mid, exc, f"import of {name}.py")
        return None


class _OptionalAPI:
    """
    None-safe access to a file of a module (see optional()).
    If the module is off or crashed, every call is a silent no-op and returns
    None - the calling code simply continues without the function.
    """

    def __init__(self, mid: str, name: str):
        self._mid, self._name = mid, name

    def __getattr__(self, attr):
        mod = use(self._mid, self._name)
        if mod is None:
            return lambda *a, **k: None
        return getattr(mod, attr)

    def __bool__(self):
        return use(self._mid, self._name) is not None


def optional(mid: str, name: str) -> "_OptionalAPI":
    """
    For core code and other modules:

        recording = modules.optional("recordings", "recording")
        recording.record_frame(...)        # does nothing if the module is off
        if recording: ...                  # True if the module is active
    """
    return _OptionalAPI(mid, name)


def setting(mid: str, key: str, default=None):
    mod = _registry.get(mid)
    stored = _state.get("settings", {}).get(mid, {})
    if key in stored:
        return stored[key]
    if mod:
        for f in mod.settings_schema:
            if f["key"] == key and "default" in f:
                return f["default"]
    return default


# =========================================================================
# Crash handling
# =========================================================================

def report_crash(mid: str, exc: BaseException | None, where: str,
                 *, source: str = "backend", extra: str = "", disable: bool = True) -> dict:
    """
    Writes a crash report and disables the module (if the limit is reached).
    NEVER throws - an error here must not take anyone down.
    """
    now = time.time()
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)) if exc else (extra or "")
    summary = {
        "ts": int(now * 1000), "where": where, "source": source, "disabled": bool(disable),
        "error": (f"{type(exc).__name__}: {exc}" if exc else (extra or "Unknown error"))[:500],
        "file": None,
    }
    try:
        d = MODULE_CRASH_DIR / mid
        d.mkdir(parents=True, exist_ok=True)
        fn = d / f"crash-{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}-{int(now * 1000) % 1000:03d}.json"
        fn.write_text(json.dumps({**summary, "module": mid, "traceback": tb,
                                  "python": sys.version.split()[0]}, indent=2, ensure_ascii=False),
                      encoding="utf-8")
        summary["file"] = fn.name
        fn.write_text(json.dumps({**summary, "module": mid, "traceback": tb,
                                  "python": sys.version.split()[0]}, indent=2, ensure_ascii=False),
                      encoding="utf-8")                      # again, now WITH the file name
        for old in sorted(d.glob("crash-*.json"))[:-30]:     # keep only the last 30
            old.unlink(missing_ok=True)
    except Exception:
        pass
    try:
        _load_state()
        with _lock:
            lst = _state.setdefault("crashes", {}).setdefault(mid, [])
            lst.append(summary)
            del lst[:-20]
        _save_state()
        print(f"[module] CRASH in '{mid}' ({where}): {summary['error']}")
        try:
            from app.errors import report as _report
            _report("RMM-MOD-001", exc, f"module '{mid}' ({where})", module=mid)
        except Exception:
            pass
        window = float(_state.get("crash_window_s", 600))
        limit = max(1, int(_state.get("crash_limit", 1)))
        recent = [c for c in _state["crashes"][mid] if now - c["ts"] / 1000 <= window]
        if disable and len(recent) >= limit and mid in _loaded:
            _schedule_deactivate(mid, crash=True)
    except Exception as e2:                                  # niemals werfen
        print(f"[module] crash handling failed: {e2}")
    return summary


def _schedule_deactivate(mid: str, *, crash: bool) -> None:
    loop = _loop
    if loop is None or loop.is_closed():
        return

    def _go():
        loop.create_task(_deactivate_safely(mid, crash=crash))
    try:
        loop.call_soon_threadsafe(_go)
    except Exception:
        pass


async def _disable_veto(mid: str) -> str | None:
    """
    Asks the "before_disable" hook of an active module. If it returns a text (or
    throws), the module must NOT be turned off - the text is the reason. This is
    how e.g. "sso" protects against nobody being able to sign in anymore. An
    error in the hook is deliberately treated as a veto: better a module active
    too long than a locked-out administrator.
    """
    ld = _loaded.get(mid)
    if ld is None or ld.entry is None:
        return None
    name = ld.hooks.get("before_disable")
    fn = getattr(ld.entry, name, None) if name else None
    if not callable(fn):
        return None
    try:
        res = fn(ld.ctx)
        if asyncio.iscoroutine(res):
            res = await res
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        report_crash(mid, exc, "Hook 'before_disable'", disable=False)
        return f"Check before shutdown failed: {exc}"
    return str(res) if res else None


async def _deactivate_safely(mid: str, *, crash: bool) -> None:
    veto = await _disable_veto(mid)
    if veto:
        print(f"[module] '{mid}' stays active despite a crash - shutdown refused: {veto}")
        return
    try:
        await _deactivate(mid, run_hooks=True)
        with _lock:
            _state.setdefault("enabled", {})[mid] = False
        _save_state()
        print(f"[module] '{mid}' was disabled automatically after a crash")
    except Exception as exc:
        print(f"[module] disabling '{mid}' failed: {exc}")


# =========================================================================
# Context for module code
# =========================================================================

class ModuleContext:
    """Passed to the module code (setup/hooks). Everything here is guarded."""

    def __init__(self, mid: str, backend_dir: Path | None):
        self.id = mid
        self.dir = backend_dir
        mod = _registry.get(mid)
        self.name = mod.meta("name", mid) if mod else mid
        self.version = str(mod.meta("version", "0.0.0")) if mod else "0.0.0"

    # --- Infrastruktur ---------------------------------------------------
    @property
    def api(self):
        return _api

    @property
    def sio(self):
        return _sio

    @property
    def data_dir(self) -> Path:
        d = MODULES_DATA_DIR / self.id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def log(self, msg: str) -> None:
        print(f"[{self.id}] {msg}")

    # --- Einstellungen / Teilfunktionen -----------------------------------
    def setting(self, key: str, default=None):
        return setting(self.id, key, default)

    def feature(self, fid: str) -> bool:
        return _feature_on(self.id, fid)

    def is_enabled(self, other_mid: str) -> bool:
        return is_enabled(other_mid)

    def use(self, other_mid: str, name: str):
        return use(other_mid, name)

    # --- Nodes -------------------------------------------------------------------
    @property
    def nodes(self):
        """The nodes from this module's view: list/get/online, deploy/remove/action of its OWN node part."""
        from app.node_manager import NodesAPI
        return NodesAPI(self.id)

    # --- Hintergrundaufgaben ----------------------------------------------
    def spawn(self, name: str, factory: Callable[[], Any]):
        """Starts a continuous loop. If it crashes -> crash report + module off."""
        ld = _loaded.get(self.id)
        if ld is None:
            raise ModuleError("Module is not loaded")
        mid = self.id

        async def runner():
            try:
                await factory()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                report_crash(mid, exc, f"background task '{name}'")

        task = asyncio.get_running_loop().create_task(runner(), name=f"module:{mid}:{name}")
        ld.tasks.append(task)
        return task

    # --- Socket.IO -----------------------------------------------------------
    def sio_on(self, event: str, handler: Callable, namespace: str = "/"):
        """Registers a Socket.IO event; removed again on deactivation."""
        ld = _loaded.get(self.id)
        if ld is None or _sio is None:
            raise ModuleError("Socket.IO not available")
        mid = self.id
        is_async = asyncio.iscoroutinefunction(handler)

        if is_async:
            async def wrapped(*a, **kw):
                try:
                    return await handler(*a, **kw)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    report_crash(mid, exc, f"socket event '{event}'")
        else:
            def wrapped(*a, **kw):
                try:
                    return handler(*a, **kw)
                except Exception as exc:
                    report_crash(mid, exc, f"socket event '{event}'")
        wrapped.__name__ = getattr(handler, "__name__", "handler")
        _sio.on(event, namespace=namespace)(wrapped)
        ld.sio_handlers.append((namespace, event))

    # --- Events ---------------------------------------------------------------
    async def emit(self, event: str, **payload):
        await emit(event, **payload)


# =========================================================================
# Mount / unmount routes (always BEFORE the static frontend mount)
# =========================================================================

_PASS_EXC: tuple = ()
_guard_paths: dict[str, list] = {}      # mid -> [(regex, methods|None)]


def _pass_exceptions() -> tuple:
    global _PASS_EXC
    if not _PASS_EXC:
        from starlette.exceptions import HTTPException as _H
        from starlette.websockets import WebSocketDisconnect as _W
        from starlette.requests import ClientDisconnect as _C
        from fastapi.exceptions import RequestValidationError as _V
        _PASS_EXC = (_H, _W, _C, _V, asyncio.CancelledError)
    return _PASS_EXC


def _owner_of(scope) -> str | None:
    path = scope.get("path") or ""
    method = (scope.get("method") or "").upper()
    for mid, items in _guard_paths.items():
        for rx, methods in items:
            if rx.match(path) and (methods is None or not method or method in methods):
                return mid
    return None


class GuardMiddleware:
    """
    Catches unhandled exceptions from module endpoints (HTTP and WebSocket).
    It sits INSIDE Starlette's ServerErrorMiddleware: HTTPException & co. are
    handled normally before and never reach it - only real crashes land here.
    The path -> module mapping is in _guard_paths. Independent of FastAPI
    internals, works with any FastAPI version.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") not in ("http", "websocket") or not _guard_paths:
            return await self.app(scope, receive, send)
        mid = _owner_of(scope)
        if mid is None:
            return await self.app(scope, receive, send)
        started = {"v": False}

        async def send2(message):
            if message.get("type") in ("http.response.start", "websocket.accept"):
                started["v"] = True
            await send(message)
        try:
            await self.app(scope, receive, send2)
        except _pass_exceptions():
            raise
        except Exception as exc:
            report_crash(mid, exc, f"Route {scope.get('path')}")
            try:
                if scope["type"] == "http" and not started["v"]:
                    body = json.dumps({"detail": f"Module '{mid}' crashed and was "
                                                 f"disabled. Details in the module app."}).encode()
                    await send({"type": "http.response.start", "status": 500,
                                "headers": [(b"content-type", b"application/json"),
                                            (b"content-length", str(len(body)).encode())]})
                    await send({"type": "http.response.body", "body": body})
                elif scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1011})
            except Exception:
                pass


def _router_patterns(router) -> list:
    out = []
    for r in getattr(router, "routes", []) or []:
        rx = getattr(r, "path_regex", None)
        if rx is not None:
            methods = getattr(r, "methods", None)
            out.append((rx, {m.upper() for m in methods} if methods else None))
    return out


def _routes_changed() -> None:
    """Newer FastAPI versions cache the route resolution - invalidate the cache."""
    mark = getattr(_api.router, "_mark_routes_changed", None)
    if callable(mark):
        mark()


def _place_before_static(new_routes: list) -> None:
    """New routes must come BEFORE the frontend's '/' mount, otherwise that one wins."""
    from starlette.routing import Mount
    routes = _api.router.routes
    ids = {id(r) for r in new_routes}
    rest = [r for r in routes if id(r) not in ids]
    idx = len(rest)
    for i, r in enumerate(rest):
        if isinstance(r, Mount) and getattr(r, "path", None) in ("", "/"):
            idx = i
            break
    routes[:] = rest[:idx] + list(new_routes) + rest[idx:]
    _routes_changed()


def _mount_router(ld: Loaded, router) -> None:
    before = {id(r) for r in _api.router.routes}
    _api.include_router(router)
    new = [r for r in _api.router.routes if id(r) not in before]
    _place_before_static(new)
    ld.routes.extend(new)
    _guard_paths.setdefault(ld.mid, []).extend(_router_patterns(router))


def _mount_websocket(ld: Loaded, path: str, endpoint) -> None:
    before = {id(r) for r in _api.router.routes}
    _api.add_api_websocket_route(path, endpoint)
    new = [r for r in _api.router.routes if id(r) not in before]
    _place_before_static(new)
    ld.routes.extend(new)
    _guard_paths.setdefault(ld.mid, []).extend(
        [(rx, None) for rx in (getattr(r, "path_regex", None) for r in new) if rx is not None])


# =========================================================================
# Enable / disable
# =========================================================================

def _pip_missing(reqs: list[str]) -> list[str]:
    from importlib import metadata
    missing = []
    for r in reqs:
        name = re.split(r"[<>=!~;\[ ]", r.strip(), maxsplit=1)[0]
        if not name:
            continue
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            missing.append(r)
            continue
        try:
            from packaging.requirements import Requirement
            req = Requirement(r)
            if req.specifier and not req.specifier.contains(installed, prereleases=True):
                missing.append(r)
        except Exception:
            pass
    return missing


def _pip_install_blocking(reqs: list[str]) -> None:
    base = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--quiet"]
    for extra in ([], ["--break-system-packages"]):
        res = subprocess.run(base + extra + reqs, capture_output=True, text=True, timeout=900)
        if res.returncode == 0:
            importlib.invalidate_caches()
            return
        err = (res.stderr or res.stdout or "").strip()
        if "externally-managed" not in err.lower() or extra:
            raise ModuleError("pip installation failed: " + err[-600:])


async def _ensure_pip(mod: Module) -> list[str]:
    missing = _pip_missing(mod.pip)
    if missing:
        print(f"[module] '{mod.id}': installing {', '.join(missing)}")
        await asyncio.get_running_loop().run_in_executor(None, _pip_install_blocking, missing)
        still = _pip_missing(mod.pip)
        if still:
            raise ModuleError("Packages still not satisfied after installation: " + ", ".join(still))
    return missing


def _dependents(mid: str, only_enabled: bool = True) -> list[str]:
    out = []
    for other in _registry.values():
        if mid in other.requires and other.id != mid:
            if not only_enabled or other.id in _loaded:
                out.append(other.id)
    return out


def _dep_order(mid: str, seen: list[str] | None = None) -> list[str]:
    """Dependencies first, then the module itself."""
    seen = seen if seen is not None else []
    mod = get(mid)
    for dep in mod.requires:
        if dep not in _registry:
            raise ModuleError(f"'{mid}' needs the unknown module '{dep}'")
        if dep in seen:
            raise ModuleError(f"Circular dependency at '{dep}'")
        _dep_order(dep, seen)
    if mid not in seen:
        seen.append(mid)
    return seen


async def _call_hook(ld: Loaded, event: str, payload: dict | None = None, *, lifecycle: bool = False) -> None:
    name = ld.hooks.get(event)
    if not name or ld.entry is None:
        return
    fn = getattr(ld.entry, name, None)
    if not callable(fn):
        report_crash(ld.mid, None, f"hook '{event}'", extra=f"function '{name}' missing in {ld.entry.__name__}")
        return
    try:
        res = fn(ld.ctx) if lifecycle else fn(ld.ctx, payload or {})
        if asyncio.iscoroutine(res):
            await res
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        report_crash(ld.mid, exc, f"Hook '{event}'")
        if lifecycle and event in ("startup", "enable"):
            raise ModuleError(f"Hook '{event}' failed: {exc}") from exc


def _backend_part(mod: Module) -> Part | None:
    return mod.parts.get("backend")


async def _activate(mid: str, *, user_enable: bool) -> None:
    """Loads the backend part. Throws on errors - the caller cleans up."""
    mod = get(mid)
    if mid in _loaded:
        return
    ld = Loaded(mid)
    bp = _backend_part(mod)
    ld.ctx = ModuleContext(mid, bp.path if bp else None)
    _loaded[mid] = ld
    try:
        if bp:
            man = bp.manifest
            pkg = types.ModuleType(ld.pkg)
            pkg.__path__ = [str(bp.path)]                    # subfiles: from rmm_mod_x import y
            pkg.__package__ = ld.pkg
            sys.modules[ld.pkg] = pkg
            importlib.invalidate_caches()
            ld.hooks = {k: v for k, v in (man.get("hooks") or {}).items() if isinstance(v, str)}
            entry = man.get("entry")
            if entry is None and (bp.path / "module.py").is_file():
                entry = "module"
            if entry:
                ld.entry = importlib.import_module(f"{ld.pkg}.{str(entry).removesuffix('.py')}")
            # Routers (optional per sub-feature)
            for spec in man.get("routers", []) or []:
                rname = spec if isinstance(spec, str) else spec.get("module")
                feat = None if isinstance(spec, str) else spec.get("feature")
                if feat and not _feature_on(mid, feat):
                    continue
                rmod = importlib.import_module(f"{ld.pkg}.{rname}")
                _mount_router(ld, rmod.router)
            for spec in man.get("websockets", []) or []:
                feat = spec.get("feature")
                if feat and not _feature_on(mid, feat):
                    continue
                wmod = importlib.import_module(f"{ld.pkg}.{spec['module']}")
                _mount_websocket(ld, spec["path"], getattr(wmod, spec["endpoint"]))
            # Hooks
            await _call_hook(ld, "startup", lifecycle=True)
            if user_enable:
                await _call_hook(ld, "enable", lifecycle=True)
        if _api is not None:
            _api.openapi_schema = None
        print(f"[module] '{mid}' active (v{mod.meta('version', '?')})")
    except BaseException:
        await _deactivate(mid, run_hooks=False)
        raise
    await emit("module.enabled", module=mid)


async def _deactivate(mid: str, *, run_hooks: bool, user_disable: bool = False) -> None:
    ld = _loaded.get(mid)
    if ld is None:
        return
    if run_hooks:
        try:
            await _call_hook(ld, "shutdown", lifecycle=True)
            if user_disable:
                await _call_hook(ld, "disable", lifecycle=True)
        except Exception as exc:
            print(f"[module] '{mid}': shutdown hook failed: {exc}")
    _loaded.pop(mid, None)                                   # counts as inactive from now on
    for t in ld.tasks:
        t.cancel()
    for ns, ev in ld.sio_handlers:
        try:
            _sio.handlers.get(ns, {}).pop(ev, None)
        except Exception:
            pass
    if ld.routes and _api is not None:
        ids = {id(r) for r in ld.routes}
        _api.router.routes[:] = [r for r in _api.router.routes if id(r) not in ids]
        _routes_changed()
        _api.openapi_schema = None
    _guard_paths.pop(mid, None)
    for name in [n for n in sys.modules if n == ld.pkg or n.startswith(ld.pkg + ".")]:
        sys.modules.pop(name, None)
    importlib.invalidate_caches()
    try:
        await emit("module.disabled", module=mid)
    except Exception:
        pass
    if _registry.get(mid) and "node" in _registry[mid].parts:
        try:                                               # remove the node part from the nodes
            from app import node_manager
            await node_manager.module_disabled(mid)
        except Exception as exc:
            print(f"[module] '{mid}': node part could not be removed: {exc}")


# =========================================================================
# Public control (used by the module app)
# =========================================================================

async def enable(mid: str, *, with_deps: bool = False) -> dict:
    _load_state()
    mod = get(mid)
    if not mod.available:
        raise ModuleError(f"'{mid}' is marked as unavailable")
    order = _dep_order(mid)
    missing = [d for d in order[:-1] if d not in _loaded]
    if missing and not with_deps:
        names = ", ".join(get(d).meta("name", d) for d in missing)
        raise ModuleError(f"Requires first: {names}")
    done: list[str] = []
    for m in order:
        if m in _loaded:
            continue
        mm = get(m)
        try:
            installed = await _ensure_pip(mm)
            await _activate(m, user_enable=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            rep = report_crash(m, exc, "activation")
            with _lock:
                _state.setdefault("enabled", {})[m] = False
            _save_state()
            raise ModuleError(f"Activation of '{m}' failed: {rep['error']}") from exc
        with _lock:
            _state.setdefault("enabled", {})[m] = True
        done.append(m)
    _save_state()
    return {"enabled": done}


async def disable(mid: str, *, cascade: bool = False) -> dict:
    _load_state()
    get(mid)
    deps = _dependents(mid)
    if deps and not cascade:
        names = ", ".join(get(d).meta("name", d) for d in deps)
        raise ModuleError(f"Still required by: {names}")
    for m in deps + [mid]:                                   # check the veto BEFORE any change
        veto = await _disable_veto(m)
        if veto:
            raise ModuleError(f"'{get(m).meta('name', m)}' cannot be disabled: {veto}")
    done: list[str] = []
    for d in deps:
        await _deactivate(d, run_hooks=True, user_disable=True)
        _state.setdefault("enabled", {})[d] = False
        done.append(d)
    await _deactivate(mid, run_hooks=True, user_disable=True)
    _state.setdefault("enabled", {})[mid] = False
    done.append(mid)
    _save_state()
    return {"disabled": done}


async def reload(mid: str) -> None:
    """Reloads an active module (e.g. after toggling a sub-feature)."""
    if mid not in _loaded:
        return
    await _deactivate(mid, run_hooks=True)
    try:
        await _activate(mid, user_enable=False)
    except Exception as exc:
        rep = report_crash(mid, exc, "reload")
        _state.setdefault("enabled", {})[mid] = False
        _save_state()
        raise ModuleError(f"Reload failed: {rep['error']}") from exc


# =========================================================================
# Backend start / end
# =========================================================================

def init(api, sio) -> None:
    """Called once by main.py before the server accepts requests."""
    global _api, _sio
    _api, _sio = api, sio
    api.add_middleware(GuardMiddleware)        # crash protection for module routes
    _load_state()
    discover()
    MODULES_DATA_DIR.mkdir(parents=True, exist_ok=True)


def _styles_used() -> bool:
    from app import db
    if (db.get_setting("default_design", "futuristic") or "futuristic") != "futuristic":
        return True
    row = db._conn.execute(
        "SELECT 1 FROM users WHERE design IS NOT NULL AND design != '' AND design != 'futuristic' LIMIT 1").fetchone()
    return bool(row)


def _adopt_former_core_features() -> None:
    """
    Features that used to be built into the core (or another module) and are now
    their own modules: if they were already in use, the module is turned on
    automatically on the FIRST start after the update. Otherwise e.g. AD users
    would suddenly be locked out. If the administrator has already toggled a
    module themselves (entry present in modules.json), nothing is changed.
    """
    en = _state.setdefault("enabled", {})
    def _db():
        return __import__("app.db", fromlist=["db"])

    def _notify_used():
        d = _db()
        rules = d._conn.execute("SELECT COUNT(*) FROM notification_rules").fetchone()[0]
        return bool(rules) or bool(d.list_webhooks())

    def _deployment_used():
        if not en.get("relay"):
            return False
        from app.paths import STORAGE_DIR, DEPLOYMENT_DIR
        d = DEPLOYMENT_DIR if DEPLOYMENT_DIR.is_dir() else STORAGE_DIR / "deployment"
        public = str(_db().get_setting("deployment_public") or "0").lower() in ("1", "true", "yes", "on")
        return public or (d.is_dir() and any(d.iterdir()))

    def _tickets_used():
        # Tickets used to be in the module "collab" (sub-feature "tickets").
        return bool(en.get("collab")) and _state.get("features", {}).get("collab", {}).get("tickets", True) is not False

    checks = {
        "sso": lambda: bool(_db().list_realms()),
        "sql": lambda: __import__("app.dbsync", fromlist=["dbsync"]).load_config().get("mode") == "external",
        "notify": _notify_used,
        "tickets": _tickets_used,
        # "collab" (notebook) needs the module "spaces" since version 1.2.
        "spaces": lambda: bool(en.get("collab")),
        # Deployment used to be in the relay module: enable when the relay is
        # on and the page was published or something is in the folder.
        "deployment": _deployment_used,
        # Classic/Classic Hard used to be in the core: enable when a user or the
        # installation default uses one of them.
        "styles": _styles_used,
    }
    for mid, used in checks.items():
        if mid in en or mid not in _registry:
            continue
        try:
            if used():
                en[mid] = True
                print(f"[module] '{mid}' was in use so far - enabled automatically (update adoption)")
        except Exception as exc:
            print(f"[module] update adoption for '{mid}' failed: {exc}")


async def load_enabled() -> None:
    """Loads all enabled modules. An error affects ONLY the respective module."""
    global _loop
    _loop = asyncio.get_running_loop()
    _load_state()
    _adopt_former_core_features()
    wanted = [m for m, on in _state.get("enabled", {}).items() if on and m in _registry]
    ordered: list[str] = []
    for m in wanted:
        try:
            for x in _dep_order(m):
                if x not in ordered:
                    ordered.append(x)
        except Exception as exc:
            report_crash(m, exc, "dependencies")
            _state["enabled"][m] = False
    for m in ordered:
        if not _state["enabled"].get(m) and m not in wanted:
            # Dependency of an active module but itself off (e.g. after an update
            # that introduces a new dependency): turn it on automatically -
            # otherwise the dependent module would run without its basis.
            users = [w for w in wanted if m in _dep_order(w)]
            if not users:
                continue
            _state["enabled"][m] = True
            print(f"[module] '{m}' enabled automatically (needed by: {', '.join(users)})")
        try:
            await _ensure_pip(get(m))
            await _activate(m, user_enable=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            report_crash(m, exc, "start")
            _state["enabled"][m] = False
    _save_state()


async def shutdown_all() -> None:
    for mid in list(_loaded):
        try:
            await _deactivate(mid, run_hooks=True)
        except Exception as exc:
            print(f"[module] shutdown '{mid}': {exc}")


async def emit(event: str, /, **payload) -> None:
    """Reports an event to all active modules that have the hook for it.

    "event" is positional (/) so callers can also pass a field named event in
    the payload (notifier.fire_event does that). Previously this gave
    "multiple values for argument 'event'" - and no hook ran.
    """
    for mid, ld in list(_loaded.items()):
        if event in ld.hooks and event not in ("startup", "shutdown", "enable", "disable"):
            await _call_hook(ld, event, dict(payload))


def emit_nowait(event: str, /, **payload) -> None:
    """For callers without a running event loop (threads)."""
    loop = _loop
    if loop is None or loop.is_closed() or not _loaded:
        return
    try:
        loop.call_soon_threadsafe(lambda: loop.create_task(emit(event, **payload)))
    except Exception:
        pass


# =========================================================================
# Settings & sub-features (module app)
# =========================================================================

def get_settings(mid: str) -> dict:
    mod = get(mid)
    out = {}
    for f in mod.settings_schema:
        val = setting(mid, f["key"], f.get("default"))
        if f.get("type") == "password" and val:
            val = MASK
        out[f["key"]] = val
    return out


def set_settings(mid: str, values: dict) -> dict:
    _load_state()
    mod = get(mid)
    schema = {f["key"]: f for f in mod.settings_schema}
    store = _state.setdefault("settings", {}).setdefault(mid, {})
    for k, v in (values or {}).items():
        f = schema.get(k)
        if not f:
            continue
        typ = f.get("type", "string")
        if typ == "password" and v == MASK:
            continue
        if typ == "bool":
            v = bool(v)
        elif typ == "number":
            try:
                v = float(v) if "." in str(v) else int(v)
            except (TypeError, ValueError):
                raise ModuleError(f"'{f.get('label', k)}' must be a number")
            if "min" in f and v < f["min"]:
                raise ModuleError(f"'{f.get('label', k)}' is smaller than {f['min']}")
            if "max" in f and v > f["max"]:
                raise ModuleError(f"'{f.get('label', k)}' is greater than {f['max']}")
        elif typ == "select":
            opts = [o["value"] if isinstance(o, dict) else o for o in f.get("options", [])]
            if v not in opts:
                raise ModuleError(f"Invalid choice for '{f.get('label', k)}'")
        else:
            v = "" if v is None else str(v)
        store[k] = v
    _save_state()
    return get_settings(mid)


async def set_features(mid: str, values: dict) -> dict:
    _load_state()
    mod = get(mid)
    valid = {f["id"] for f in mod.features}
    store = _state.setdefault("features", {}).setdefault(mid, {})
    changed = False
    for k, v in (values or {}).items():
        if k in valid and store.get(k) != bool(v):
            store[k] = bool(v)
            changed = True
    _save_state()
    if changed and mid in _loaded:
        await reload(mid)
    return {f["id"]: _feature_on(mid, f["id"]) for f in mod.features}


def set_crash_policy(limit: int | None = None) -> dict:
    _load_state()
    if limit is not None:
        _state["crash_limit"] = max(1, min(int(limit), 50))
        _save_state()
    return {"crash_limit": _state["crash_limit"], "crash_window_s": _state["crash_window_s"]}


# =========================================================================
# Presentation for the API
# =========================================================================

def _status(mid: str) -> str:
    if mid in _loaded:
        return "enabled"
    if _state.get("crashes", {}).get(mid) and not _state.get("enabled", {}).get(mid):
        return "crashed"
    return "disabled"


def describe(mid: str) -> dict:
    mod = get(mid)
    status = _status(mid)
    crashes = _state.get("crashes", {}).get(mid, [])
    feats = [{"id": f["id"], "label": f.get("label", f["id"]),
              "description": f.get("description", ""), "enabled": _feature_on(mid, f["id"])}
             for f in mod.features]
    missing_pip = []
    try:
        missing_pip = _pip_missing(mod.pip)
    except Exception:
        pass
    fm = mod.parts.get("frontend")
    return {
        "id": mid,
        "name": mod.meta("name", mid),
        "version": str(mod.meta("version", "0.0.0")),
        "description": mod.meta("description", ""),
        "author": mod.meta("author", ""),
        "category": mod.meta("category", "Other"),
        "icon": mod.meta("icon", "🧩"),
        "status": status,
        "enabled": status == "enabled",
        "available": mod.available,
        "unavailable_reason": mod.meta("unavailable_reason", "") if not mod.available else "",
        "parts": sorted(mod.parts, key=AREAS.index),
        "requires": mod.requires,
        "required_by": _dependents(mid, only_enabled=False),
        "pip": mod.pip,
        "pip_missing": missing_pip,
        "features": feats,
        "settings_schema": mod.settings_schema,
        "settings": get_settings(mid),
        "custom_settings_ui": bool(fm and (fm.manifest.get("settings_ui") or {}).get("entry")),
        "last_crash": crashes[-1] if crashes else None,
        "crash_count": len(crashes),
        "started_at": int(_loaded[mid].started * 1000) if mid in _loaded else None,
    }


def list_all() -> dict:
    _load_state()
    mods = [describe(m) for m in sorted(_registry, key=lambda x: (_registry[x].meta("category", ""), x))]
    return {"modules": mods, "errors": list(_discover_errors),
            "crash_limit": _state.get("crash_limit", 1)}


def read_crashes(mid: str) -> list[dict]:
    get(mid)
    out = []
    d = MODULE_CRASH_DIR / mid
    if d.is_dir():
        for f in sorted(d.glob("crash-*.json"), reverse=True)[:30]:
            try:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                out.append({"file": f.name, "error": "report unreadable"})
    return out


def clear_crashes(mid: str) -> None:
    _load_state()
    get(mid)
    _state.get("crashes", {}).pop(mid, None)
    d = MODULE_CRASH_DIR / mid
    if d.is_dir():
        for f in d.glob("crash-*.json"):
            f.unlink(missing_ok=True)
    _save_state()


def node_bundle(mid: str) -> dict | None:
    """
    Packs the NODE part of an ACTIVE module (src/modules/node/<id>/) for sending to
    a node: all files (base64) + manifest. None if there is no node part or the
    module is not active.
    """
    import base64
    import hashlib
    mod = _registry.get(mid)
    part = mod.parts.get("node") if mod else None
    if part is None or mid not in _loaded:
        return None
    files: dict[str, str] = {}
    total = 0
    h = hashlib.sha256()
    for p in sorted(part.path.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts:
            continue
        data = p.read_bytes()
        total += len(data)
        if total > 4 * 1024 * 1024:
            raise ModuleError(f"Node part of '{mid}' is larger than 4 MB")
        rel = p.relative_to(part.path).as_posix()
        files[rel] = base64.b64encode(data).decode()
        h.update(rel.encode()); h.update(data)
    return {"id": mid, "version": str(mod.meta("version", "0.0.0")), "manifest": part.manifest,
            "files": files, "hash": h.hexdigest()[:16]}


def node_modules() -> list[dict]:
    """All modules with a node part (for the node UI), incl. whether they are active."""
    out = []
    for mid in sorted(_registry):
        mod = _registry[mid]
        part = mod.parts.get("node")
        if not part:
            continue
        node = part.manifest.get("node") or {}
        out.append({"id": mid, "name": mod.meta("name", mid), "version": str(mod.meta("version", "0.0.0")),
                    "icon": mod.meta("icon", "🧩"), "description": mod.meta("description", ""),
                    "active": mid in _loaded, "kind": node.get("kind", "compose"),
                    "requires_docker": bool(node.get("requires_docker", node.get("kind", "compose") == "compose")),
                    "actions": node.get("actions") or ["up", "down", "restart", "status", "logs"],
                    "params": node.get("params") or []})
    return out


def frontend_manifest() -> list[dict]:
    """Active modules with a frontend part - loaded by the frontend after login."""
    out = []
    for mid in sorted(_loaded):
        mod = _registry.get(mid)
        fp = mod.parts.get("frontend") if mod else None
        if not fp:
            continue
        m = fp.manifest
        apps = [a for a in (m.get("apps") or [])
                if not a.get("feature") or _feature_on(mid, a["feature"])]
        out.append({
            "id": mid, "name": mod.meta("name", mid), "version": str(mod.meta("version", "0.0.0")),
            "icon": mod.meta("icon", "🧩"),
            "base": f"/modules/frontend/{mid}/",
            "features": {f["id"]: _feature_on(mid, f["id"]) for f in mod.features},
            "apps": apps,
            "init": m.get("init"),
            "settings_ui": m.get("settings_ui"),
        })
    return out


def frontend_file(mid: str, rel: str) -> Path | None:
    """Safely resolved path of a frontend file of an ACTIVE module."""
    if mid not in _loaded:
        return None
    mod = _registry.get(mid)
    fp = mod.parts.get("frontend") if mod else None
    if not fp:
        return None
    base = fp.path.resolve()
    try:
        target = (base / rel).resolve()
        target.relative_to(base)
    except Exception:
        return None
    return target if target.is_file() else None
