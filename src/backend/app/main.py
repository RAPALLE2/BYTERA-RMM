"""
main.py
-------
Der zentrale Einstiegspunkt des Backends. Verbindet drei Dinge zu EINER
Anwendung, die mit einem einzigen Befehl gestartet wird:

  1. FastAPI          -> die REST-API (/api/...)
  2. Socket.IO         -> die Echtzeit-Verbindungen (/socket.io/...)
  3. Statische Dateien -> das komplette Frontend (HTML/CSS/JS) aus dem
                          "frontend"-Ordner, unter der Wurzel "/"

Das bedeutet: du brauchst NUR diesen einen Python-Prozess zu starten
(see run.py) - no separate web server, no Node/npm needed to view the
dashboard in the browser. Just open http://localhost:4000
"""

from pathlib import Path

import socketio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import db
from app import loghub as _loghub
_loghub.install()  # idempotent: captures backend output (if not started via run.py)
from app.config import CORS_ORIGIN
from app.sockets import sio
# Der Import selbst ist die erste Gefahrenstelle: Eine einzige Datei mit
# einem Syntaxfehler laesst hier ALLES scheitern, noch bevor _mount()
# ueberhaupt zum Zuge kaeme. Deshalb zuerst der gewoehnliche Sammelimport -
# und wenn der stolpert, werden die Module einzeln nachgeholt.
from router import (
    modules_routes,
    nodes_routes,
    docker_routes,
    auth_routes,
    users_routes,
    hierarchy_routes,
    clients_routes,
    files_routes,
    enrollment_routes,
    audit_routes,
    admin_routes,
    agent_update_routes,
    update_routes,
    source_routes,
    notes_routes,
    diag_routes,
    dist_routes,       # frueher Modul "dist" - jetzt Core (Verteilung unter /dist)
)  # noqa: E501 - Sammelimport, Absicherung siehe _import_router() unten


def _import_router(name):
    """
    Holt einen Router einzeln nach.

    Wird nur gebraucht, wenn der Sammelimport oben scheitert - dann steht in
    der Container-Ausgabe genau, WELCHE Datei das Problem hat, statt eines
    Stacktraces, in dem der Name untergeht.
    """
    import importlib
    try:
        return importlib.import_module(f"router.{name}")
    except Exception as exc:
        from app.errors import report, Codes
        report(Codes.BOOT_ROUTER, exc, f"Router-Datei '{name}.py' ist defekt",
               folge="Dieser Bereich fehlt, das Backend startet trotzdem")
        return None

# 1) Initialize the database (creates tables, creates admin/admin if needed)
# Vorher: Ist der externe Datenbank-Modus aktiv, den Stand von extern laden
# (afterwards init_db migrations run normally on the loaded state).
from app import rebrand as _rebrand
_rebrand.before_db()        # Umbenennung RAPALLE.net -> BYTERA: Container/dbconfig nachziehen
from app import dbsync as _dbsync
_dbsync.startup_restore_if_external()
db.init_db()
_rebrand.after_db()
# EIN Ablageort fuer alle Dateien (src/backend/storage): alte Orte (media/,
# temp/dist, storage/deployment, dist/enrollment, ...) einmalig dorthin schieben.
try:
    from app import storage_layout as _storage_layout
    _storage_layout.migrate()
except Exception as _e:
    print(f"[storage] Umzug uebersprungen: {_e}")
# Hintergrund-Jobs: Verlaufstabelle + beim letzten Stopp laufende Jobs abschliessen
try:
    from app import bgjobs as _bgjobs_boot
    _bgjobs_boot.startup_recover()
except Exception as _e:
    print(f"[bgjobs] Start: {_e}")         # Einstellungen, gespeicherte Skripte, repo.txt, Standard-Logos

# (Aufbewahrungsfristen: siehe Modul 'privacy')

# 2) Create the FastAPI app and mount all route modules
api = FastAPI(title="BYTERA RMM Backend")

# Modul-Manager: findet module.json, laedt aktivierte Module im Startup-Event.
# WICHTIG: direkt nach dem Erzeugen der App, noch VOR allen anderen Middlewares -
# seine GuardMiddleware muss die innerste sein, damit sie Abstuerze aus
# Modul-Routen sieht, bevor _RequestWatch & Co. daraus einen 500er machen.
from app import modules as _modules
_modules.init(api, sio)
from app import node_manager as _node_manager   # registriert den Socket.IO-Namespace /node

# ---------------------------------------------------------------------------
# Beobachtung jeder einzelnen Anfrage
# ---------------------------------------------------------------------------
# Zweck: Nach einem Absturz soll im Protokoll stehen, WELCHE Anfrage zuletzt
# lief. Genau diese Angabe fehlte bisher - im Container-Log standen nur
# Zugriffszeilen von uvicorn, die erst NACH der Antwort geschrieben werden.
# Stirbt der Prozess mitten in einer Anfrage, taucht sie dort also nie auf.
#
# Diese Middleware schreibt deshalb:
#   * every request that takes unusually long (with duration and path),
#   * every exception with a full stack trace,
#   * and it records which requests are running RIGHT NOW. The loop guard
#     nimmt diese Liste in seinen Stack-Abzug auf.
import time as _t
import traceback as _traceback
from starlette.middleware.base import BaseHTTPMiddleware as _BaseMW
from starlette.responses import JSONResponse as _JSONResp

# From this duration on, a request counts as suspiciously slow.
import os as _os_env

try:
    SLOW_REQUEST_S = float(_os_env.getenv("RMM_SLOW_REQUEST_S", "2.0") or 2.0)
except ValueError:
    SLOW_REQUEST_S = 2.0

# Was gerade in Bearbeitung ist: Kennung -> (Pfad, Startzeit)
INFLIGHT: dict[int, tuple[str, float]] = {}
_inflight_seq = 0


class _RequestWatch(_BaseMW):
    async def dispatch(self, request, call_next):
        global _inflight_seq
        _inflight_seq += 1
        key = _inflight_seq
        path = f"{request.method} {request.url.path}"
        started = _t.monotonic()
        INFLIGHT[key] = (path, started)
        try:
            response = await call_next(request)
        except Exception as exc:
            # Eine Ausnahme darf NIE die Verbindung einfach abreissen lassen -
            # der Browser wartet sonst bis in sein eigenes Zeitlimit. Lieber
            # eine ehrliche 500 MIT KENNCODE: Damit kann der Benutzer die
            # Meldung weitergeben und man findet die Stelle im Protokoll
            # sofort wieder, ohne nach Uhrzeiten zu suchen.
            duration = _t.monotonic() - started
            from app.errors import report as _report, Codes as _Codes
            code = _report(_Codes.API_UNHANDLED, exc, path,
                           dauer=f"{duration:.1f}s",
                           client=getattr(request.client, "host", "?"))
            return _JSONResp(
                status_code=500,
                content={"detail": f"Interner Fehler ({code}) in "
                                   f"{request.url.path}. Der Kenncode steht im "
                                   f"Server-Protokoll – dort ist auch die "
                                   f"Ursache vermerkt.",
                         "error_code": code})
        finally:
            duration = _t.monotonic() - started
            INFLIGHT.pop(key, None)
            if duration >= SLOW_REQUEST_S:
                try:
                    from app.errors import report as _report, Codes as _Codes
                    _report(_Codes.API_SLOW, None, path,
                            dauer=f"{duration:.1f}s")
                except Exception:
                    pass
        return response


api.add_middleware(_RequestWatch)

# Host lock: access only via the addresses stored in the settings.
# Placed right at the front so that disallowed hosts do not even get
# to see CORS headers. Off by default.
from app.hostlock import HostLockMiddleware as _HostLock
api.add_middleware(_HostLock)

# --- Fehlertexte in der Sprache des Benutzers ------------------------------
# Das Backend wirft seine HTTPExceptions weiterhin mit deutschem Text. Dieser
# Handler uebersetzt den Text kurz vor dem Senden - EINE Stelle statt ueber 200
# verstreuter Aufrufe. Die Sprache kommt aus dem Profil des angemeldeten
# Benutzers (users.language); ist niemand angemeldet, gilt die Server-Sprache.
# Unbekannte Texte bleiben unveraendert, es kann also nichts verlorengehen.
from fastapi import HTTPException as _HTTPExc
from fastapi.responses import JSONResponse as _JSONResponse
from app.i18n import translate_detail as _tr_detail, server_lang as _srv_lang


def _lang_of_request(request) -> str:
    """Sprache des anfragenden Benutzers, sonst Server-Sprache."""
    try:
        from app.auth import get_current_user
        user = get_current_user(request.headers.get("authorization"))
        if user and user.get("language"):
            return str(user["language"])
    except Exception:
        # Kein/ungueltiges Token: Das ist normal (Login, oeffentliche Seiten).
        pass
    return _srv_lang()


@api.exception_handler(_HTTPExc)
async def _translated_http_exception(request, exc: _HTTPExc):
    return _JSONResponse(
        status_code=exc.status_code,
        content={"detail": _tr_detail(exc.detail, _lang_of_request(request))},
        headers=getattr(exc, "headers", None),
    )


api.add_middleware(
    CORSMiddleware,
    allow_origins=[CORS_ORIGIN] if CORS_ORIGIN != "*" else ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Own hostnames of modules (e.g. the browser proxy "bx-<id>.example.com").
# Added last = outermost: those hosts never reach the dashboard app.
from app.hostapps import HostDispatch as _HostDispatch
api.add_middleware(_HostDispatch)

def _mount(name, module) -> None:
    """Bindet einen Router ein und ueberlebt es, wenn er kaputt ist."""
    try:
        api.include_router(module.router)
    except Exception as exc:
        from app.errors import report, Codes
        report(Codes.BOOT_ROUTER, exc,
               f"Router '{name}' konnte nicht eingebunden werden",
               folge=f"Die Endpunkte von {name} fehlen, der Rest laeuft")


# Jeder Router einzeln eingebunden.
#
# Frueher standen hier 34 nackte include_router()-Zeilen. Scheitert eine
# davon - eine kaputte Datei nach einem Update, ein fehlendes Modul -, wirft
# der Import beim Start, und das GESAMTE Backend startet nicht. Der Container
# landet dann in der Notfall-Fehlerseite, und alle 34 Bereiche sind weg, weil
# einer defekt war.
#
# _mount() bindet jeden Router einzeln ein und meldet einen Fehlschlag mit
# Kenncode. Ein defekter Bereich fehlt dann - alle anderen laufen.
# Modul-Verwaltung (Core): Modul-App-API + Frontend-Dateien aktiver Module
_mount("modules_routes", modules_routes)
_mount("nodes_routes", nodes_routes)      # Nodes (eigener Prozess auf dem Client)
_mount("docker_routes", docker_routes)
_mount("auth_routes", auth_routes)
_mount("users_routes", users_routes)
_mount("hierarchy_routes", hierarchy_routes)
_mount("clients_routes", clients_routes)
_mount("files_routes", files_routes)
_mount("enrollment_routes", enrollment_routes)
_mount("audit_routes", audit_routes)
_mount("admin_routes", admin_routes)
_mount("agent_update_routes", agent_update_routes)
_mount("update_routes", update_routes)
# Datenbank-Verwaltung (lokal/extern/Docker-DB): Modul "sql" (src/modules/backend/sql)
_mount("source_routes", source_routes)
# Benachrichtigungen (Regeln, SMTP, Webhooks): Modul "notify" (src/modules/backend/notify)
_mount("notes_routes", notes_routes)       # Client-Notizen (Sichtbarkeit + Protokoll)
# VPN AUSGEBAUT (2026-09-20): Der WireGuard-Teil ist vollstaendig entfernt.
# Grund: Sein Datenweg hing im Event-Loop (siehe unten) und hat das Backend
# reihenweise in den Loop-Waechter laufen lassen. Port-Weiterleitung und
# Relay-Explorer bleiben - die brauchen kein VPN.
_mount("diag_routes", diag_routes)
_mount("dist_routes", dist_routes)         # Verteilung /dist (Core)
# Hintergrund-Jobs (Scans, Erkennungen ...): laufen im Backend, nicht in der
# Browser-Sitzung - Neuladen/andere Benutzer sehen denselben Stand (app/bgjobs.py)
# Encryption towards agents/clients (info, status, end-to-end REST envelope)
security_routes = _import_router("security_routes")
if security_routes is not None:
    _mount("security_routes", security_routes)
bgjobs_routes = _import_router("bgjobs_routes")
if bgjobs_routes is not None:
    _mount("bgjobs_routes", bgjobs_routes)
# Aktivierung + Modul-Store (Settings -> Aktivierung / Modul-Store)
store_routes = _import_router("store_routes")
if store_routes is not None:
    _mount("store_routes", store_routes)
# Auto-Migration von einem anderen BYTERA-Backend (Settings -> Migration)
migration_routes = _import_router("migration_routes")
if migration_routes is not None:
    _mount("migration_routes", migration_routes)

# (Guacamole-WebSocket-Tunnel registriert das Modul 'guacamole' selbst.)


# ------------------------------------------------------------------
# Versions: the single source of truth are the version.txt files
# (backend/version.txt and agent/version.txt). Basis for the
# later auto-update: agents/frontends compare their version
# against /api/version and update themselves on a mismatch.
# ------------------------------------------------------------------
from app.paths import (
    PROJECT_ROOT as _PROJECT_ROOT, FRONTEND_DIR, BACKEND_VERSION_FILE as _BACKEND_VERSION_FILE,
    AGENT_VERSION_FILE as _AGENT_VERSION_FILE_MAIN,
)

# Unique ID of THIS backend process. Changes on every (re)start.
# The frontend polls it and reloads as soon as it changes -> after
# a backend restart the web console refreshes on its own.
import uuid as _uuid
BOOT_ID = _uuid.uuid4().hex


def _read_version(path: Path, fallback: str = "0.0.0") -> str:
    try:
        v = path.read_text(encoding="utf-8").strip()
        return v or fallback
    except OSError:
        return fallback


def get_backend_version() -> str:
    return _read_version(_BACKEND_VERSION_FILE)


def get_agent_version() -> str:
    return _read_version(_AGENT_VERSION_FILE_MAIN)


@api.get("/api/health")
async def health_check():
    """
    Health check - deliberately the cheapest thing possible.

    NOTHING is touched here: no database, no file, no network. The
    endpoint answers exactly one question - "is the event loop still
    running?" - and it should be able to answer that even when the
    server is currently under full load.

    An earlier version read version.txt from disk on every call. That
    is harmless while nothing is going on, and exactly the straw too
    much when Docker knocks every five seconds during a mass update.
    The version is still available under /api/version.
    """
    return {"ok": True, "name": "BYTERA RMM"}


@api.get("/api/version")
async def versions():
    """
    Central version info (no auth - contains no secrets):
    backend and agent version from the respective version.txt files.
    'boot_id' identifiziert den laufenden Prozess (Auto-Reload nach Restart).
    """
    return {"backend": get_backend_version(), "agent": get_agent_version(), "boot_id": BOOT_ID}


@api.get("/api/boot-id")
async def boot_id():
    """Leichter Endpunkt, den das Frontend pollt, um Backend-Neustarts zu erkennen."""
    return {"boot_id": BOOT_ID}


from fastapi import Request as _Request


@api.get("/api/server-address")
async def server_address(request: _Request):
    """
    Returns the "best" publicly reachable base URL of the backend - for
    things like the Explorer relay (network-drive URL) and install commands.

    Priority:
      1. server_url  (full, manually set URL, e.g. https://rmm.company.com)
      2. server_domain or server_host + server_backend_port
      3. Fallback: host from the incoming request, but does NOT replace
         'localhost'/'127.0.0.1' by itself - instead the Host header is
         used so the address is correct for REMOTE machines.

    The frontend should show this URL instead of 'localhost', because a
    network drive on ANOTHER PC can never reach 'localhost'.
    """
    # 1) Full URL set?
    server_url = (db.get_setting("server_url") or "").strip()
    if server_url:
        # Make sure a scheme is present - otherwise the address is
        # interpreted as a RELATIVE path in the browser (led to /<ip>/dav 404).
        if not server_url.lower().startswith(("http://", "https://")):
            server_url = f"{request.url.scheme}://{server_url}"
        return {"base_url": server_url.rstrip("/"), "source": "server_url",
                "backend_port": (db.get_setting("server_backend_port") or "4000").strip()}

    # 2) Domain/host + port from the settings
    domain = (db.get_setting("server_domain") or "").strip()
    host = (db.get_setting("server_host") or "").strip()
    backend_port = (db.get_setting("server_backend_port") or "4000").strip()

    chosen = domain or host
    # If accidentally entered with a scheme (e.g. "http://10.0.0.1"),
    # strip the scheme - we set it ourselves.
    chosen = chosen.replace("https://", "").replace("http://", "").strip("/")
    scheme = request.url.scheme or "http"
    if chosen:
        # Do not append a port for standard ports
        if (scheme == "http" and backend_port == "80") or (scheme == "https" and backend_port == "443"):
            base = f"{scheme}://{chosen}"
        else:
            base = f"{scheme}://{chosen}:{backend_port}"
        return {"base_url": base, "source": "settings", "backend_port": backend_port}

    # 3) Fallback: Host header of the request (works for the calling
    #    browser). If that is 'localhost'/'127.0.0.1' (dashboard opened
    #    locally), the automatically detected LAN IP of the backend is
    #    used instead (config.HOST) - so the address is ALWAYS filled
    #    dynamically from a variable and valid for REMOTE machines, never
    #    a hard-coded 'localhost'.
    host_header = request.headers.get("host") or request.url.netloc
    host_only = host_header.split(":")[0]
    is_localhost = host_only in ("localhost", "127.0.0.1", "::1")
    if is_localhost:
        from app.config import HOST as _detected_host
        if _detected_host and _detected_host != "0.0.0.0":
            port = host_header.split(":")[1] if ":" in host_header else backend_port
            host_header = f"{_detected_host}:{port}"
            is_localhost = False
    base = f"{scheme}://{host_header}"
    return {"base_url": base.rstrip("/"), "source": "request",
            "backend_port": backend_port,
            "is_localhost": is_localhost}


# ------------------------------------------------------------------
# Automation engine: checks every minute whether scheduled automations are
# due and runs their command on the target clients. Runs as a
# background task in the same process.
# ------------------------------------------------------------------
import asyncio as _asyncio
import time as _time




# ------------------------------------------------------------------
# Uptime monitor: checks the websites bound to clients (client_websites
# with monitor_enabled=1) at the configured interval via HTTP request.
# Depending on monitor_notify it notifies:
#   'up'     -> only when a scan succeeded  (on status change down->up
#               or on the very first successful scan)
#   'down'   -> only when a scan failed (on status change up->down
#               or on the very first failed scan)
#   'always' -> after EVERY scan (success and failure)
# Notification goes through ALL configured webhooks AND as a normal
# in-app notification (socket event "notify" to all dashboards).
# ------------------------------------------------------------------
import urllib.request as _urlreq
import urllib.error as _urlerr

from router.admin_routes import build_notification as _build_notification


def _check_website(url: str) -> tuple[bool, str | None]:
    """Calls the URL (synchronous, runs via to_thread). 2xx/3xx = up.
    Returns: (ok, error_message)."""
    req = _urlreq.Request(url, headers={"User-Agent": "BYTERA-RMM-UptimeMonitor/1.0"})
    try:
        with _urlreq.urlopen(req, timeout=15) as resp:
            code = getattr(resp, "status", 200)
            if 200 <= code < 400:
                return True, None
            return False, f"HTTP {code}"
    except _urlerr.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, str(e)


async def _notify_uptime(site: dict, ok: bool, error: str | None) -> None:
    """Baut die Notification und verschickt sie an Webhooks + Dashboards."""
    client = db.get_client(site["client_id"]) or {}
    tenant_name = location_name = None
    try:
        if client.get("tenant_id"):
            tenant_name = next((t["name"] for t in db.list_tenants()
                                if t["id"] == client["tenant_id"]), None)
        if client.get("location_id"):
            location_name = next((l["name"] for l in db.list_locations()
                                  if l["id"] == client["location_id"]), None)
    except Exception:
        pass

    if ok:
        head = f"Website reachable - {site['name']}"
        body_txt = f"{site['url']} responded to the uptime scan (UP)."
        level = "success"
    else:
        head = f"Website not reachable - {site['name']}"
        body_txt = f"{site['url']} did NOT respond to the uptime scan (DOWN)." \
                   + (f" Error: {error}" if error else "")
        level = "error"

    notification = _build_notification(
        body_txt, head=head, body=body_txt,
        tenant=tenant_name, location=location_name,
        client=client.get("hostname"), service="Uptime-Monitor", level=level,
    )

    # 1) Alle Webhooks (Modul "notify"; ist es aus, passiert nichts)
    try:
        from app import notifier as _notifier
        await _notifier.send_all_webhooks(notification)
    except Exception as e:
        print(f"[uptime] Webhooks fehlgeschlagen: {e}")

    # 2) Normale In-App-Notification an alle verbundenen Dashboards
    try:
        await sio.emit("notify", {
            "message": f"{head}\n{body_txt}",
            "level": level,
        }, namespace="/dashboard")
    except Exception as e:
        print(f"[uptime] Dashboard-Notify fehlgeschlagen: {e}")

    db.add_audit_entry("system", "website.uptime", target=site["client_id"],
                       details=f"{site['name']} -> {'UP' if ok else 'DOWN'}")

    # 3) Benachrichtigungs-Regeln (website_up / website_down)
    try:
        from app import notifier as _notifier
        await _notifier.fire_event("website_up" if ok else "website_down",
                                   client_id=site["client_id"],
                                   notification=notification,
                                   dedupe_key=f"{site['id']}:{'up' if ok else 'down'}")
    except Exception as e:
        print(f"[uptime] Regel-Dispatch fehlgeschlagen: {e}")


async def _uptime_monitor_engine():
    while True:
        try:
            import time as _time
            now_ms = int(_time.time() * 1000)
            for site in await db.call(db.list_monitored_websites):
                interval_ms = max(10, int(site["monitor_interval_seconds"] or 300)) * 1000
                last = site["last_checked"] or 0
                if now_ms - last < interval_ms:
                    continue  # not due yet

                ok, error = await _asyncio.to_thread(_check_website, site["url"])
                new_status = "up" if ok else "down"
                prev_status = site["last_status"]   # None = never checked
                await db.call(db.set_website_check_result, site["id"],
                              new_status, error)

                mode = site["monitor_notify"] or "down"
                changed = prev_status != new_status  # incl. very first scan
                should_notify = (
                    mode == "always"
                    or (mode == "up" and ok and changed)
                    or (mode == "down" and not ok and changed)
                )
                if should_notify:
                    await _notify_uptime(site, ok, error)

                # Inform dashboards about the new status (panel updates the indicator)
                try:
                    await sio.emit("website:status", {
                        "id": site["id"], "client_id": site["client_id"],
                        "status": new_status,
                    }, namespace="/dashboard")
                except Exception:
                    pass
        except Exception as e:
            print(f"[uptime] Engine error: {e}")
        await _asyncio.sleep(5)  # check every 5 seconds which scans are due


@api.on_event("startup")
async def _start_background_tasks():
    # Encryption: server key + own TLS port (app/security.py, app/tls_listener.py)
    try:
        from app import security as _sec, tls_listener as _tls
        _sec.server_key()
        _asyncio.get_running_loop().create_task(_tls.run(socket_app))
    except Exception as _e:
        print(f"[security] start: {_e}")
    try:                                   # Arbeitsspeicher klein halten (app/memtrim.py)
        from app import memtrim as _memtrim
        _memtrim.start(_asyncio.get_running_loop())
    except Exception as _e:
        print(f"[startup] memtrim: {_e}")
    # Verteilung /dist (Core, frueher Modul "dist"): Kern-Ordner anlegen und
    # vorhandene Agent-/Client-Pakete nach /dist spiegeln.
    try:
        from app import dist_store as _dist_store, dist_sync as _dist_sync
        _dist_store.ensure_dirs()
        _n = _dist_sync.sync_packages()
        if _n:
            print(f"[dist] {_n} Paket(e) nach /dist gespiegelt")
    except Exception as _e:
        print(f"[dist] Start uebersprungen: {_e}")
    # Aktivierte Module laden. Ein Fehler betrifft NUR das jeweilige Modul.
    try:
        await _modules.load_enabled()
    except Exception as _e:
        print(f"[module] Laden uebersprungen: {_e}")
    # If the backend process was previously ended by a crash and automatically
    # restarted by run.py, this is recorded here - after a successful startup -
    # recorded in the audit log (counterpart to 'backend.crash').
    try:
        if db.get_setting("backend_crash_pending", "0") == "1":
            db.add_audit_entry("system", "backend.restarted",
                               details="Automatic restart after a crash")
            db.set_setting("backend_crash_pending", "0")
    except Exception as e:
        print(f"[startup] Crash recovery audit failed: {e}")
    # Hook in diagnostics and resume maintenance mode if it was active
    # before a restart. This is deliberately placed RIGHT AT THE FRONT:
    # everything that goes wrong afterwards should already land in the log.
    from app import diagnostics as _diag
    _diag.install()
    _diag.report_previous_shutdown()
    # Signaturen der Socket-Handler pruefen, bevor irgendjemand verbindet.
    try:
        from app import sockets as _sockets
        _sockets.verify_handlers()
    except Exception as _e:
        print(f"[sockets] Signaturpruefung uebersprungen: {_e}")
    _diag.restore_from_settings()
    # Der Loop-Waechter laeuft in einem EIGENEN Thread. Das ist Absicht:
    # Steht die Ereignisschleife still, kaeme eine async-Ueberwachung selbst
    # nicht mehr zum Zug und wuerde ausgerechnet den Fall verschweigen, den
    # sie melden soll. Ein Thread misst von aussen.
    _supervise("loop-herzschlag", _diag.loop_heartbeat)
    _diag.start_watchdog()
    _supervise("diagnose-messwerte", _diag.sampler_loop)
    _supervise("tabellen-groesse", _diag.table_size_watch)

    _supervise("uptime-monitor", _uptime_monitor_engine)
    # Warranty check loop: now runs in the "notify" module (ctx.spawn)
    _supervise("server-auto-update", _server_auto_update_engine)
    _supervise("db-sync", _db_sync_engine)
    # Wartungsmodus: abgelaufene Zeitfenster beenden und an Agents melden.
    from app.sockets import maintenance_expiry_loop as _maint_loop
    _supervise("wartungsmodus-ablauf", _maint_loop)


    # Frueher wurde hier vpn.start() angeworfen (wireguard-go + TUN-Geraet).
    # Der TUN-Leser hing per add_reader DIREKT im Event-Loop und erzeugte pro
    # IP-Paket einen asyncio-Task - bei echtem Tunnel-Verkehr tausende pro
    # Sekunde in genau der Schleife, die auch alle Agenten, die Shell und den
    # Remote-Screen bedient. Das war die Hauptquelle der Loop-Blockaden.
    # Der VPN-Start ist ersatzlos entfallen.


# ----------------------------------------------------------------------
# Supervisor for background tasks
# ----------------------------------------------------------------------
# Until now every loop was started with create_task(). If such a loop
# throws an exception, it is GONE - silently. The backend then seems to
# keep running, but automation, uptime checks or patching no longer
# happen. From the outside that looks exactly like "the backend is acting
# up" or "it crashes".
#
# The supervisor catches the exception, writes it to the log and restarts
# the loop - with a growing delay, so that a permanently broken loop does
# not max out the machine in an endless loop.

_TASKS: dict = {}


def _supervise(name: str, factory) -> None:
    """Starts a background loop that recovers after an error."""

    async def runner():
        delay = 2.0
        while True:
            started = _time.time()
            try:
                await factory()
                # Ended normally: the loop was meant to be finite.
                print(f"[supervisor] '{name}' ended normally")
                return
            except _asyncio.CancelledError:
                raise
            except Exception as exc:
                from app.errors import report as _report, Codes as _Codes
                _report(_Codes.TASK_LOOP, exc, f"background loop '{name}'",
                        ran=f"{_time.time() - started:.0f}s")
                # If the task ran for a long time it was probably a one-off -
                # then restart immediately with a short delay. If it keeps
                # dying right away, the delay grows.
                delay = 2.0 if _time.time() - started > 120 else min(delay * 2, 300.0)
                print(f"[supervisor] '{name}' restarts in {delay:.0f}s")
                await _asyncio.sleep(delay)

    _TASKS[name] = _asyncio.create_task(runner(), name=f"supervised:{name}")




async def _db_sync_engine():
    """Mirrors the local DB to the external DB every 60 s (only on changes)."""
    while True:
        await _asyncio.sleep(60)
        try:
            loop = _asyncio.get_event_loop()
            await loop.run_in_executor(None, _dbsync.periodic_sync)
        except Exception as e:
            print(f"[dbsync] Engine error: {e}")


@api.on_event("shutdown")
async def _final_db_sync():
    """On shutdown, write the last state to the external DB."""
    try:
        from app import bgjobs as _bgjobs
        _bgjobs.shutdown()
    except Exception as e:
        print(f"[bgjobs] Shutdown failed: {e}")
    try:
        await _modules.shutdown_all()
    except Exception as e:
        print(f"[module] Shutdown failed: {e}")
    try:
        _dbsync.periodic_sync()
    except Exception as e:
        print(f"[dbsync] Final sync failed: {e}")


async def _server_auto_update_engine():
    """Checks periodically (every 30 min) whether a server auto-update is due."""
    await _asyncio.sleep(60)   # give the backend time to start up first
    while True:
        try:
            await update_routes.auto_update_tick()
        except Exception as e:
            print(f"[auto-update] Engine-Fehler: {e}")
        await _asyncio.sleep(1800)


# 3) Mount the frontend folder as static files.
#    Important: this must happen AFTER the API routes, otherwise the
#    static files would "snatch" all requests before the API sees them.
# FRONTEND_DIR comes from app/paths.py (src/frontend)

# Serve images (incl. uploaded branding) BEFORE the catch-all mount:
# first from the writable upload store, otherwise from the bundled
# frontend/image. This way replaced logos are visible immediately, even
# if the frontend folder is read-only.
from fastapi.responses import FileResponse as _FileResponse
from fastapi import HTTPException as _HTTPException
from fastapi import Depends as _Depends
from app.auth import get_current_user as _current_user
from router.admin_routes import branding_path as _branding_path


@api.get("/image/{name}")
async def _serve_image(name: str):
    p = _branding_path(name)
    if p is None:
        raise _HTTPException(status_code=404, detail="Image not found")
    # no-cache: the browser MUST revalidate (Last-Modified/ETag come from
    # FileResponse). Without this header browsers cache images heuristically
    # for hours - replaced branding logos then did not appear ("image does
    # not load"), although the upload had long been saved.
    return _FileResponse(str(p), headers={"Cache-Control": "no-cache"})


@api.get("/api/frontend-version")
async def _frontend_version(user: dict = _Depends(_current_user)):
    """
    What is REALLY in the frontend folder of this server?

    Separates two cases that look identical in the browser and have already
    cost a whole debugging session once:

      * The file on disk is still the old one -> the deployment did not
        arrive. The checksum reported here then does not change, no matter
        how often you reload.
      * The file is new but the browser does not get it -> something caches
        in between (the browser itself, nginx, Cloudflare).

    Comparison: call this endpoint and compare the reported checksum with
    the one the browser actually loaded (developer tools -> Network -> look
    at the .js file). If they do not match, something caches in between.
    """
    import hashlib
    watched = ["js/app.js", "js/api.js", "js/i18n.js", "js/apps/webbrowser.js"]
    out = {}
    for rel in watched:
        p = FRONTEND_DIR / rel
        try:
            data = p.read_bytes()
            out[rel] = {
                "sha256": hashlib.sha256(data).hexdigest()[:16],
                "bytes": len(data),
                "mtime": int(p.stat().st_mtime),
                # A concrete marker instead of a mere checksum: does the
                # file still contain code of the removed page proxy?
                "has_webproxy": b"webproxy" in data,
            }
        except OSError as e:
            out[rel] = {"error": str(e)}
    return {"frontend_dir": str(FRONTEND_DIR), "files": out}


class _RevalidatingStatic(StaticFiles):
    """
    StaticFiles mit passenden Cache-Headern fuer die Frontend-Dateien.

    Ausgangslage: StaticFiles schickt von Haus aus NUR Last-Modified und ETag,
    aber kein Cache-Control. Ohne Cache-Control cachen Browser heuristisch -
    je nach Alter der Datei durchaus stundenlang, ohne auch nur nachzufragen.
    Bei ES-Modulen ist das tueckisch: Nach einem Update lief im Browser weiter
    der alte Code und rief Endpunkte auf, die es serverseitig nicht mehr gibt.

    Deshalb stand hier ueberall 'no-cache' ("vor Benutzung rueckfragen").
    Das hat aber einen Preis, der erst im Betrieb hinter einem Proxy auffiel:
    Das Dashboard besteht aus mehreren Dutzend ES-Modulen. Bei JEDEM Laden
    ergab das mehrere Dutzend Rueckfragen innerhalb weniger Sekunden - ein
    Muster, das Rate-Limiter (Cloudflare & Co.) als Burst einstufen und mit
    429 abweisen. Dann fehlen einzelne Module, und der Browser arbeitet mit
    alten Resten weiter ("does not provide an export named ...").

    Kompromiss:
      *.html                 -> no-cache. Das ist EINE Datei; sie muss immer
                               frisch sein, denn sie zieht alles andere nach.
      *.js/.mjs/.css/.json   -> kurzes max-age. Innerhalb dieser Zeitspanne
                               kommt ein Reload ganz ohne Anfragen aus, was
                               den Burst beseitigt. Danach wird wieder
                               revalidiert, Updates kommen also weiter an.
      alles andere           -> unveraendert (Bilder, Schriften).

    Die Dauer laesst sich ueber die Einstellung 'static_cache_seconds' aendern;
    0 stellt das alte Verhalten (immer rueckfragen) wieder her.
    """

    REVALIDATE = (".js", ".mjs", ".css", ".json", ".map")
    ALWAYS_FRESH = (".html",)
    DEFAULT_MAX_AGE = 60

    def _max_age(self) -> int:
        try:
            raw = db.get_setting("static_cache_seconds")
            if raw is None or str(raw).strip() == "":
                return self.DEFAULT_MAX_AGE
            return max(0, min(86400, int(str(raw).strip())))
        except Exception:
            return self.DEFAULT_MAX_AGE

    async def get_response(self, path: str, scope):
        low = path.lower()
        # Schutz gegen fehlende Exporte (app/frontend_guard.py): Passt eine
        # Datei nicht zur Version, werden Platzhalter angehaengt, damit nicht
        # der GESAMTE Modulbaum (inkl. Anmeldung) ausfaellt.
        if low.endswith(".js"):
            try:
                from app import frontend_guard
                extra = frontend_guard.patch_for("/" + path.lstrip("/").replace("\\", "/"))
            except Exception:
                extra = ""
            if extra:
                patched = await self._patched_response(path, scope, extra)
                if patched is not None:
                    return patched
        response = await super().get_response(path, scope)
        if low.endswith(self.ALWAYS_FRESH):
            response.headers["Cache-Control"] = "no-cache"
        elif low.endswith(self.REVALIDATE):
            secs = self._max_age()
            response.headers["Cache-Control"] = (
                f"private, max-age={secs}, must-revalidate" if secs else "no-cache")
        return response


    async def _patched_response(self, path: str, scope, extra: str):
        """Datei + Platzhalter ausliefern (mit eigener ETag, damit der Browser
        keine alte, ungepatchte Fassung aus dem Cache nimmt)."""
        import hashlib
        from starlette.responses import Response as _Resp
        try:
            full = (FRONTEND_DIR / path.lstrip("/")).resolve()
            if FRONTEND_DIR.resolve() not in full.parents or not full.is_file():
                return None
            body = full.read_bytes() + extra.encode("utf-8")
        except OSError:
            return None
        etag = '"g-' + hashlib.sha1(body).hexdigest()[:20] + '"'
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        inm = dict(scope.get("headers") or []).get(b"if-none-match", b"").decode("latin-1")
        if inm and etag in inm:
            return _Resp(status_code=304, headers=headers)
        return _Resp(content=body, media_type="text/javascript; charset=utf-8", headers=headers)


@api.get("/api/frontend/health")
async def _frontend_health():
    """Ohne Anmeldung abrufbar (nur Dateinamen): Welche Frontend-Dateien
    importieren Funktionen, die es in der Zieldatei nicht gibt? Wird vom
    sicheren Lader (js/boot.js) im Notfall-Modus angezeigt."""
    from app import frontend_guard
    return frontend_guard.health()


try:
    from app import frontend_guard as _fg
    _fg.check_on_startup()
except Exception as _e:
    print(f"[frontend-guard] {_e}")

api.mount("/", _RevalidatingStatic(directory=FRONTEND_DIR, html=True), name="frontend")


# 4) Combine Socket.IO and FastAPI into a single ASGI application.
#    "socket_app" is what uvicorn actually starts in the end (see run.py).
socket_app = socketio.ASGIApp(sio, other_asgi_app=api, socketio_path="socket.io")


