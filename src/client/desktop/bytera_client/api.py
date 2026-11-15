# api.py - Verbindung zum RMM-Backend (nur Standardbibliothek, keine Abhaengigkeiten)
# --------------------------------------------------------------------------------
# Alle Aufrufe sind blockierend; die Oberflaeche ruft sie in einem Hintergrund-
# Thread auf (app.py -> _bg). Fehler kommen als ApiError mit verstaendlicher Meldung.

from __future__ import annotations

import json
import platform
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

from . import __version__
from . import e2e as _e2e

TIMEOUT = 20


class _Fallback(Exception):
    """Backend without the encrypted envelope (old version): use plain requests."""


class ApiError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def normalize_server(url: str) -> str:
    """'rmm.firma.de' -> 'https://rmm.firma.de', Schraegstrich am Ende weg."""
    url = (url or "").strip().rstrip("/")
    if not url:
        raise ApiError("Bitte die Server-Adresse eingeben")
    if "://" not in url:
        url = "https://" + url
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ApiError("Ungültige Server-Adresse")
    return url


class Api:
    def __init__(self, server: str, token: str = "", verify_tls: bool = True, store=None):
        self.server = normalize_server(server)
        self.token = token
        self.verify_tls = verify_tls
        self.store = store                 # remembers the server key / TLS fingerprint (pinning)
        self._sec: dict | None = None

    # ------------------------------------------------------------- encryption (e2e.py)
    def security(self) -> dict:
        """Pinned server key + TLS fingerprint; learned on first contact."""
        if self._sec is not None:
            return self._sec
        cache = dict((self.store.get("security") or {}) if self.store else {})
        sec = cache.get(self.server) or {}
        if not sec.get("key"):
            info = _e2e.fetch_info(self.server, ctx=self._ctx())
            if info and info.get("key"):
                sec = {"key": info["key"], "pin": info.get("tls_pin") or "", "learned": int(__import__("time").time())}
                if self.store:
                    cache[self.server] = sec
                    try:
                        self.store.set(security=cache)
                    except Exception:
                        pass
        self._sec = sec
        return sec

    def encrypted(self) -> bool:
        return bool(_e2e.AVAILABLE and self.security().get("key"))

    def _open(self, req, timeout=TIMEOUT):
        """https with normal checks; the backend's own certificate is accepted by its pinned fingerprint."""
        try:
            return urllib.request.urlopen(req, timeout=timeout, context=self._ctx())
        except (ssl.SSLCertVerificationError, urllib.error.URLError) as e:
            cert_err = isinstance(e, ssl.SSLCertVerificationError) or \
                isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError)
            pin = (self._sec or {}).get("pin")
            if cert_err and pin:
                return _e2e.pinned_opener(pin).open(req, timeout=timeout)
            raise

    def _request_e2e(self, method: str, path: str, headers: dict, data: bytes | None):
        env = _e2e.Envelope(self.security()["key"])
        inner = {k: v for k, v in headers.items() if k.lower() not in ("user-agent",)}
        req = urllib.request.Request(self.server + "/api/e2e", data=env.request_body(method, path, inner, data),
                                     method="POST", headers={"Content-Type": "application/octet-stream",
                                                             "User-Agent": headers.get("User-Agent", "")})
        try:
            with self._open(req) as r:
                out = env.open_response(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 409:
                raise ApiError("The server presents a DIFFERENT encryption key than the one remembered by this app. "
                               "This can be an attack. If the server was reinstalled, remove and add the server "
                               "again in the connection settings.", 409) from None
            if e.code == 404:
                # a server that once offered encryption never drops it - refuse instead of
                # silently falling back to unencrypted requests (downgrade attack)
                raise ApiError("The server does not accept encrypted requests anymore. This can be an attack; "
                               "if the server was downgraded or reinstalled, remove and add it again.", 404) from None
            raise ApiError(f"Encrypted request failed (HTTP {e.code})", e.code) from None
        status = int(out.get("s") or 0)
        raw = out["body"]
        if status >= 400:
            detail = ""
            try:
                detail = (json.loads(raw or b"{}") or {}).get("detail") or ""
            except Exception:
                pass
            raise ApiError(str(detail or f"HTTP {status}"), status)
        return json.loads(raw) if raw else None

    # ------------------------------------------------------------- Basis
    def _ctx(self):
        if self.server.startswith("https://") and not self.verify_tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        return None

    def request(self, method: str, path: str, body: dict | None = None, auth: bool = True):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json", "User-Agent": f"ByteraClient/{__version__}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if auth and self.token:
            headers["Authorization"] = "Bearer " + self.token
        try:
            if self.encrypted():
                return self._request_e2e(method, path, headers, data)
        except _Fallback:
            pass
        except ssl.SSLCertVerificationError:
            raise ApiError("Das Zertifikat des Servers ist nicht vertrauenswürdig "
                           "(selbst signiert?). In den Verbindungs-Einstellungen lässt sich die Prüfung abschalten.") from None
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            if isinstance(e, ApiError):
                raise
            reason = getattr(e, "reason", e)
            raise ApiError(f"Server nicht erreichbar: {reason}") from None
        req = urllib.request.Request(self.server + path, data=data, method=method, headers=headers)
        try:
            with self._open(req) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.reason
            try:
                detail = json.loads(e.read() or b"{}").get("detail") or detail
            except Exception:
                pass
            raise ApiError(str(detail), e.code) from None
        except ssl.SSLCertVerificationError:
            raise ApiError("Das Zertifikat des Servers ist nicht vertrauenswürdig "
                           "(selbst signiert?). In den Verbindungs-Einstellungen lässt sich die Prüfung abschalten.") from None
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            reason = getattr(e, "reason", e)
            raise ApiError(f"Server nicht erreichbar: {reason}") from None

    # ------------------------------------------------------------- Anmeldung
    def realms(self) -> list[dict]:
        try:
            return self.request("GET", "/api/auth/realms", auth=False) or []
        except ApiError:
            return []

    def login(self, username: str, password: str, realm: str = "local", code: str = "") -> dict:
        """-> {token, user}. Bei aktivierter 2FA ohne Code: ApiError("2fa_required", 401)."""
        res = self.request("POST", "/api/auth/login",
                           {"username": username, "password": password, "realm": realm or "local", "code": code},
                           auth=False)
        self.token = res["token"]
        return res

    def register_device(self, name: str) -> dict:
        res = self.request("POST", "/api/client/register", device_info(name))
        self.token = res["access_token"]
        return res

    def session(self, device_token: str) -> dict:
        res = self.request("POST", "/api/client/session",
                           {"device_token": device_token, "app_version": __version__}, auth=False)
        self.token = res["access_token"]
        return res

    # ------------------------------------------------------------- Funktionen
    def config(self) -> dict:
        return self.request("GET", "/api/client/config")

    def clients(self) -> list[dict]:
        return self.request("GET", "/api/clients") or []

    def devices(self) -> list[dict]:
        return (self.request("GET", "/api/client/devices") or {}).get("devices", [])

    def remove_device(self, dev_id: str) -> None:
        self.request("DELETE", f"/api/client/devices/{urllib.parse.quote(dev_id)}")

    def screen(self, client_id: str, protocol: str) -> dict:
        return self.request("POST", "/api/client/screen", {"client_id": client_id, "protocol": protocol})

    def target_info(self, client_id: str, protocol: str) -> dict:
        """Fernbild-Ziel: lokale IP, Voreinstellung, letzte Verbindung (pro Benutzer + Geraet)."""
        return self.request("GET", f"/api/client/targets/{urllib.parse.quote(client_id)}?protocol={protocol}")

    def connect(self, client_id: str, protocol: str, host_mode: str, host: str = "", port_mode: str = "standard",
                port: int | None = None, remember: bool = False, lifetime: str = "session", user: str = "") -> dict:
        """-> {host, port, ...}. Bei "gateway" legt der Server eine Weiterleitung an, bei "tunnel"
        zusaetzlich eine Cloudflare-Adresse (-> hostname, token_id, token_secret, lease)."""
        return self.request("POST", "/api/client/connect", {
            "client_id": client_id, "protocol": protocol, "host_mode": host_mode, "host": host,
            "port_mode": port_mode, "port": port, "remember": remember, "lifetime": str(lifetime or "session"),
            "user": user or ""})

    def redeem(self, ticket: str) -> dict:
        """Startschein aus dem Dashboard (bytera-rmm://launch) einloesen."""
        return self.request("POST", f"/api/client/launch/{urllib.parse.quote(ticket)}")

    def tunnel_ping(self, lease: str) -> dict:
        return self.request("POST", f"/api/client/tunnel/{urllib.parse.quote(lease)}/ping")

    def tunnel_release(self, lease: str) -> None:
        self.request("DELETE", f"/api/client/tunnel/{urllib.parse.quote(lease)}")

    def dashboard_url(self, solo: str | None = None, client_id: str | None = None) -> str:
        """Dashboard-Adresse mit uebergebener Sitzung (#client-token=...)."""
        q = ""
        if solo == "console":                         # RMM-Konsole (Modul "console"), ohne Geraet
            q = "?" + urllib.parse.urlencode({"solo": "console", "via": "client"})
        elif solo:
            q = "?" + urllib.parse.urlencode({"solo": solo, "client": client_id or ""})
        return f"{self.server}/{q}#client-token={self.token}&client-app={__version__}"


def device_info(name: str = "") -> dict:
    sysname = platform.system()
    plat = {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}.get(sysname, sysname.lower())
    osdesc = f"{sysname} {platform.release()}"
    if sysname == "Windows":
        try:
            build = int(platform.version().split(".")[-1])
            osdesc = f"Windows {'11' if build >= 22000 else '10'} (Build {build})"
        except Exception:
            pass
    elif sysname == "Linux":
        try:
            osdesc = platform.freedesktop_os_release().get("PRETTY_NAME", osdesc)
        except Exception:
            pass
    return {"name": name or default_device_name(), "platform": plat, "os": osdesc, "app_version": __version__}


def default_device_name() -> str:
    return socket.gethostname() or "Mein Gerät"


def is_android() -> bool:
    return False
