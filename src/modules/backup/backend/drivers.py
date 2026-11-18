"""
drivers.py (Modul "backup") - Dateizugriff des BACKENDS auf Speicherziele.

Gebraucht fuer die Wege "Backend" und "ueber Backend": Der Client spricht
das restic-REST-Protokoll mit dem Backend (rest_routes.py), und das Backend
legt die (bereits verschluesselten!) Daten hier ab - lokal oder auf einem
NAS per SMB, SFTP oder FTP(S).

Alle Methoden sind synchron (Aufruf ueber asyncio.to_thread).
Pfade sind immer relativ ("<repo>/data/ab/abcd..."), mit "/" getrennt.
"""

from __future__ import annotations

import ftplib
import io
import os
import posixpath
import threading
from pathlib import Path

from rmm_mod_backup.store import LOCAL_ROOT


class DriverError(Exception):
    pass


def _clean(rel: str) -> str:
    parts = [p for p in rel.replace("\\", "/").split("/") if p and p not in (".", "..")]
    return "/".join(parts)


# ------------------------------------------------------------------ lokal
class Local:
    def __init__(self, cfg: dict):
        self.root = Path(cfg.get("path") or LOCAL_ROOT)
        self.root.mkdir(parents=True, exist_ok=True)

    def _p(self, rel: str) -> Path:
        p = (self.root / _clean(rel)).resolve()
        if not str(p).startswith(str(self.root.resolve())):
            raise DriverError("Pfad ausserhalb des Speicherziels")
        return p

    def stat(self, rel):
        p = self._p(rel)
        return p.stat().st_size if p.is_file() else None

    def read(self, rel, offset=0, length=None) -> bytes:
        with open(self._p(rel), "rb") as fh:
            fh.seek(offset)
            return fh.read() if length is None else fh.read(length)

    def write(self, rel, fileobj) -> None:
        p = self._p(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".part")
        with open(tmp, "wb") as fh:
            while True:
                chunk = fileobj.read(1024 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
        tmp.replace(p)

    def delete(self, rel) -> None:
        self._p(rel).unlink(missing_ok=True)

    def list(self, rel) -> list[tuple[str, int]]:
        p = self._p(rel)
        out = []
        if p.is_dir():
            for root, _dirs, files in os.walk(p):
                for f in files:
                    if f.endswith(".part"):
                        continue
                    fp = Path(root) / f
                    out.append((f, fp.stat().st_size))
        return out

    def mkdirs(self, rel) -> None:
        self._p(rel).mkdir(parents=True, exist_ok=True)

    def usage(self) -> dict:
        import shutil
        u = shutil.disk_usage(self.root)
        return {"total": u.total, "used": u.used, "free": u.free}


# ------------------------------------------------------------------ SMB / CIFS
class Smb:
    def __init__(self, cfg: dict):
        try:
            import smbclient
        except ImportError:
            raise DriverError("Python-Paket 'smbprotocol' fehlt (Modul neu aktivieren installiert es)")
        self.sc = smbclient
        self.server = cfg["server"]
        self.base = "\\\\" + cfg["server"] + "\\" + cfg["share"].strip("\\/")
        sub = _clean(cfg.get("path") or "")
        if sub:
            self.base += "\\" + sub.replace("/", "\\")
        user = cfg.get("user") or ""
        if cfg.get("domain") and user and "\\" not in user:
            user = cfg["domain"] + "\\" + user
        smbclient.register_session(self.server, username=user or None,
                                   password=cfg.get("password") or None)

    def _p(self, rel):
        r = _clean(rel)
        return self.base + ("\\" + r.replace("/", "\\") if r else "")

    def stat(self, rel):
        try:
            return self.sc.stat(self._p(rel)).st_size
        except OSError:
            return None

    def read(self, rel, offset=0, length=None):
        with self.sc.open_file(self._p(rel), mode="rb") as fh:
            fh.seek(offset)
            return fh.read() if length is None else fh.read(length)

    def write(self, rel, fileobj):
        self.mkdirs(posixpath.dirname(_clean(rel)))
        with self.sc.open_file(self._p(rel), mode="wb") as fh:
            while True:
                chunk = fileobj.read(1024 * 1024)
                if not chunk:
                    break
                fh.write(chunk)

    def delete(self, rel):
        try:
            self.sc.remove(self._p(rel))
        except OSError:
            pass

    def list(self, rel):
        out = []
        try:
            for root, _dirs, files in self.sc.walk(self._p(rel)):
                for f in files:
                    out.append((f, self.sc.stat(root + "\\" + f).st_size))
        except OSError:
            pass
        return out

    def mkdirs(self, rel):
        if _clean(rel):
            self.sc.makedirs(self._p(rel), exist_ok=True)

    def usage(self):
        return {}


# ------------------------------------------------------------------ SFTP
class Sftp:
    _cache: dict = {}
    _clock = threading.Lock()

    def __init__(self, cfg: dict):
        try:
            import paramiko  # noqa: F401
        except ImportError:
            raise DriverError("Python-Paket 'paramiko' fehlt (Modul neu aktivieren installiert es)")
        self.cfg = cfg
        self.base = (cfg.get("path") or "").rstrip("/")

    def _client(self):
        import paramiko
        key = (self.cfg["host"], int(self.cfg.get("port") or 22), self.cfg.get("user"))
        with self._clock:
            c = self._cache.get(key)
            if c is not None:
                try:
                    c.listdir(".")
                    return c
                except Exception:
                    self._cache.pop(key, None)
            t = paramiko.Transport((key[0], key[1]))
            pkey = None
            if self.cfg.get("private_key"):
                pkey = paramiko.Ed25519Key.from_private_key(io.StringIO(self.cfg["private_key"]))
            t.connect(username=key[2], password=self.cfg.get("password") or None, pkey=pkey)
            c = paramiko.SFTPClient.from_transport(t)
            self._cache[key] = c
            return c

    def _p(self, rel):
        r = _clean(rel)
        return f"{self.base}/{r}" if r else (self.base or ".")

    def stat(self, rel):
        try:
            return self._client().stat(self._p(rel)).st_size
        except IOError:
            return None

    def read(self, rel, offset=0, length=None):
        with self._client().open(self._p(rel), "rb") as fh:
            fh.seek(offset)
            return fh.read() if length is None else fh.read(length)

    def write(self, rel, fileobj):
        self.mkdirs(posixpath.dirname(_clean(rel)))
        c = self._client()
        tmp = self._p(rel) + ".part"
        with c.open(tmp, "wb") as fh:
            fh.set_pipelined(True)
            while True:
                chunk = fileobj.read(1024 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
        try:
            c.posix_rename(tmp, self._p(rel))
        except IOError:
            c.rename(tmp, self._p(rel))

    def delete(self, rel):
        try:
            self._client().remove(self._p(rel))
        except IOError:
            pass

    def list(self, rel):
        import stat as _st
        c, out = self._client(), []

        def walk(p):
            try:
                for a in c.listdir_attr(p):
                    if _st.S_ISDIR(a.st_mode):
                        walk(f"{p}/{a.filename}")
                    elif not a.filename.endswith(".part"):
                        out.append((a.filename, a.st_size))
            except IOError:
                pass
        walk(self._p(rel))
        return out

    def mkdirs(self, rel):
        c, cur = self._client(), self.base
        for part in _clean(rel).split("/"):
            if not part:
                continue
            cur = f"{cur}/{part}" if cur else part
            try:
                c.stat(cur)
            except IOError:
                c.mkdir(cur)

    def usage(self):
        return {}


# ------------------------------------------------------------------ FTP / FTPS
class Ftp:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.base = "/" + _clean(cfg.get("path") or "")
        self._lock = threading.Lock()
        self._ftp = None

    def _conn(self):
        if self._ftp is not None:
            try:
                self._ftp.voidcmd("NOOP")
                return self._ftp
            except Exception:
                self._ftp = None
        cls = ftplib.FTP_TLS if self.cfg.get("tls") else ftplib.FTP
        f = cls()
        f.connect(self.cfg["host"], int(self.cfg.get("port") or 21), timeout=60)
        f.login(self.cfg.get("user") or "anonymous", self.cfg.get("password") or "")
        if isinstance(f, ftplib.FTP_TLS):
            f.prot_p()
        f.voidcmd("TYPE I")
        self._ftp = f
        return f

    def _p(self, rel):
        r = _clean(rel)
        return posixpath.join(self.base, r) if r else self.base

    def stat(self, rel):
        with self._lock:
            try:
                return self._conn().size(self._p(rel))
            except ftplib.all_errors:
                return None

    def read(self, rel, offset=0, length=None):
        buf = io.BytesIO()

        class _Done(Exception):
            pass

        def cb(data):
            buf.write(data)
            if length is not None and buf.tell() >= length:
                raise _Done()
        with self._lock:
            f = self._conn()
            try:
                f.retrbinary(f"RETR {self._p(rel)}", cb, rest=offset or None)
            except _Done:
                try:
                    f.abort()
                except ftplib.all_errors:
                    pass
                self._ftp = None          # nach ABORT Verbindung frisch aufbauen
        data = buf.getvalue()
        return data if length is None else data[:length]

    def write(self, rel, fileobj):
        with self._lock:
            f = self._conn()
            self._mk(f, posixpath.dirname(self._p(rel)))
            tmp = self._p(rel) + ".part"
            f.storbinary(f"STOR {tmp}", fileobj, blocksize=1024 * 1024)
            f.rename(tmp, self._p(rel))

    def delete(self, rel):
        with self._lock:
            try:
                self._conn().delete(self._p(rel))
            except ftplib.all_errors:
                pass

    def list(self, rel):
        out = []
        with self._lock:
            f = self._conn()

            def walk(p):
                try:
                    entries = list(f.mlsd(p, facts=["type", "size"]))
                except ftplib.all_errors:
                    return
                for name, facts in entries:
                    if name in (".", ".."):
                        continue
                    if facts.get("type") == "dir":
                        walk(posixpath.join(p, name))
                    elif not name.endswith(".part"):
                        out.append((name, int(facts.get("size") or 0)))
            walk(self._p(rel))
        return out

    def _mk(self, f, path):
        cur = ""
        for part in path.strip("/").split("/"):
            if not part:
                continue
            cur += "/" + part
            try:
                f.mkd(cur)
            except ftplib.error_perm:
                pass

    def mkdirs(self, rel):
        with self._lock:
            self._mk(self._conn(), self._p(rel))

    def usage(self):
        return {}


_DRIVERS = {"backend": Local, "smb": Smb, "sftp": Sftp, "ftp": Ftp}
_inst: dict = {}
_ilock = threading.Lock()


def driver_for(storage: dict):
    """Treiber-Instanz je Speicherziel (wird bei Konfig-Aenderung neu gebaut)."""
    cls = _DRIVERS.get(storage.get("type"))
    if cls is None:
        raise DriverError(f"Speicherziel-Typ {storage.get('type')!r} wird vom Backend nicht beschrieben")
    key = (storage["id"], repr(sorted((k, str(v)) for k, v in storage.items())))
    with _ilock:
        d = _inst.get(key)
        if d is None:
            d = _inst[key] = cls(storage)
        return d


def test(storage: dict) -> dict:
    """Verbindungstest: Ordner anlegen, Datei schreiben, lesen, loeschen."""
    d = driver_for(storage)
    probe = ".bytera-test"
    d.write(probe, io.BytesIO(b"bytera"))
    ok = d.read(probe) == b"bytera"
    d.delete(probe)
    if not ok:
        raise DriverError("Testdatei konnte nicht korrekt zurückgelesen werden")
    return {"ok": True, **(d.usage() or {})}
