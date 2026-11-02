"""
rebrand.py
----------
Umbenennung RAPALLE.net RMM -> BYTERA RMM: zieht bestehende Installationen beim
Backend-Start automatisch nach. Alles ist idempotent (mehrfaches Ausfuehren
schadet nicht) und faengt Fehler selbst ab - der Start darf daran nie scheitern.

Zwei Phasen (aufgerufen in app/main.py):

  before_db()  VOR dem Laden der Datenbank
               - Docker-Nachbarcontainer rapalle-rmm-* -> bytera-rmm-* umbenennen
               - dbconfig.json: Host "rapalle-rmm-db" -> "bytera-rmm-db"
  after_db()   NACH db.init_db()
               - Einstellungen guacd_host / docker_db_host nachziehen
               - gespeicherte Skripte (z.B. "Agent Update") auf die neuen Pfade/Namen
               - data/repo.txt auf den neuen Repo-Namen (Rueckfall: update_routes)
               - Logos: unveraenderte ALTE Standard-Logos durch die neuen ersetzen
                 (frontend/image ist beim Selbst-Update geschuetzt, deshalb liegen die
                 neuen Standards in app/assets/branding). Eigene Uploads bleiben.

Agents und Client-App migrieren sich selbst (Update-Skripte bzw. App-Start).
"""
from __future__ import annotations

import hashlib
import json
import shutil

from app.paths import BRANDING_DIR, BRANDING_DEFAULTS_DIR, DBCONFIG_FILE, REPO_FILE

# Ersetzungen fuer gespeicherte Skripte (Reihenfolge: lange Namen zuerst)
_SCRIPT_MAP = (
    ("/opt/rapalle-rmm-agent", "/opt/bytera-rmm-agent"),
    ("Program Files\\RapalleRmmAgent", "Program Files\\ByteraRmmAgent"),
    ("Program Files\\\\RapalleRmmAgent", "Program Files\\\\ByteraRmmAgent"),
    ("rapalle-agent-update.log", "bytera-agent-update.log"),
    ("RapalleRmmUpdate", "ByteraRmmUpdate"),
    ("RapalleRmmUninstall", "ByteraRmmUninstall"),
    ("RapalleRmmAgent", "ByteraRmmAgent"),
    ("'RapalleRMM'", "'ByteraRMM'"),
    ("\"RapalleRMM\"", "\"ByteraRMM\""),
    ("rapalle-agent", "bytera-agent"),
    ("RAPALLE.net RMM", "BYTERA RMM"),
)

_LEGACY_HOSTS = {"rapalle-rmm-db": "bytera-rmm-db", "rapalle-rmm-guacd": "bytera-rmm-guacd"}

# sha256 der alten Standard-Logos (RAPALLE.net). Nur GENAU diese Dateien werden ersetzt.
_LEGACY_LOGO_HASHES = {
    "favicon.ico": {"dbc1e8d2c95b15992e017f16459df5755d6e0b67e453ab7affe5c6c5ac35d2f7"},
    "logo.png": {"dbc1e8d2c95b15992e017f16459df5755d6e0b67e453ab7affe5c6c5ac35d2f7"},
    "logo_r.png": {"9b8dbf680d03de91f7766dcbb5e0bbb58f236c9dead50b278b64b4e44437adbc"},
    "logo_big.png": {"fe23ccee2c3c4ba8399f322312b052444d634a7fcd1ca53b0420dc65d69ba337"},
    "login-bg.jpg": {"d977a78a364c8698d1986cfbf7c5593b111b7642889a0d591e0f4198261d5224"},
}


def _log(msg: str) -> None:
    print(f"[rebrand] {msg}")


def _safe(fn):
    try:
        fn()
    except Exception as e:      # noqa: BLE001 - der Start darf hieran nie scheitern
        _log(f"{fn.__name__} uebersprungen: {e}")


# --------------------------------------------------------------------------
# Phase 1: vor der Datenbank
# --------------------------------------------------------------------------

def _docker_containers() -> None:
    from app import docker_services
    for line in docker_services.migrate_legacy():
        _log(f"Container umbenannt: {line}")


def _dbconfig() -> None:
    if not DBCONFIG_FILE.is_file():
        return
    cfg = json.loads(DBCONFIG_FILE.read_text(encoding="utf-8"))
    host = str(cfg.get("host") or "")
    if host in _LEGACY_HOSTS:
        cfg["host"] = _LEGACY_HOSTS[host]
        DBCONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        _log(f"dbconfig.json: Host {host} -> {cfg['host']}")


def before_db() -> None:
    _safe(_docker_containers)
    _safe(_dbconfig)


# --------------------------------------------------------------------------
# Phase 2: nach der Datenbank
# --------------------------------------------------------------------------

def _settings() -> None:
    from app import db
    for key in ("guacd_host", "docker_db_host"):
        v = db.get_setting(key) or ""
        if v in _LEGACY_HOSTS:
            db.set_setting(key, _LEGACY_HOSTS[v])
            _log(f"Einstellung {key}: {v} -> {_LEGACY_HOSTS[v]}")


def _scripts() -> None:
    from app import db
    rows = db._conn.execute(
        "SELECT id, command FROM scripts WHERE lower(command) LIKE '%rapalle%'").fetchall()
    n = 0
    for r in rows:
        cmd = r["command"] or ""
        new = cmd
        for a, b in _SCRIPT_MAP:
            new = new.replace(a, b)
        if new != cmd:
            db._conn.execute("UPDATE scripts SET command = ? WHERE id = ?", (new, r["id"]))
            n += 1
    if n:
        db._conn.commit()
        _log(f"{n} gespeicherte(s) Skript(e) auf die neuen Namen umgestellt")


def _repo() -> None:
    if not REPO_FILE.is_file():
        return
    url = REPO_FILE.read_text(encoding="utf-8").strip()
    if "/Rapalle.net-RMM" in url:
        REPO_FILE.write_text(url.replace("/Rapalle.net-RMM", "/BYTERA-RMM") + "\n", encoding="utf-8")
        _log("repo.txt auf BYTERA-RMM umgestellt")


def _sha(p) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _logos() -> None:
    if not BRANDING_DEFAULTS_DIR.is_dir():
        return
    BRANDING_DIR.mkdir(parents=True, exist_ok=True)
    for name, old_hashes in _LEGACY_LOGO_HASHES.items():
        src = BRANDING_DEFAULTS_DIR / name
        dst = BRANDING_DIR / name
        if not src.is_file():
            continue
        if dst.is_file() and _sha(dst) not in old_hashes:
            continue                       # eigenes Logo oder schon das neue -> nicht anfassen
        tmp = dst.with_name(dst.name + ".tmp-rebrand")
        shutil.copyfile(src, tmp)
        tmp.replace(dst)
        _log(f"Logo ersetzt: {name}")


def after_db() -> None:
    _safe(_settings)
    _safe(_scripts)
    _safe(_repo)
    _safe(_logos)
