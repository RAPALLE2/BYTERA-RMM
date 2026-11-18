"""
routers/store_routes.py
-----------------------
Aktivierung + Modul-Store (beides unter Einstellungen).

AKTIVIERUNG (/api/activation):
  Eingabefeld für einen Aktivierungscode. Der Code hat noch KEINE Funktion -
  er wird nur mit Einlösedatum in der Datenbank gespeichert. Später fragt das
  RMM damit den Lizenzserver ab und schaltet ggf. Premium-Features frei oder
  lädt Zusatz-Module.

MODUL-STORE (/api/modstore/...):
  Eigene Paketquellen wie beim Community-Store von Synology DSM: Der Admin
  hinterlegt eine Server-Adresse (HTTP-Verzeichnis-Listing ODER JSON-Manifest).
  Alle dort auffindbaren .zip-Dateien werden angezeigt und können direkt als
  Modul installiert werden (Download -> sicheres Entpacken -> modules/<id>/ ->
  Rescan). Aktiviert wird das Modul danach wie gewohnt in der Modul-Verwaltung.

  Unterstützte Quellen-Formate:
    * HTML-Verzeichnis-Listing (nginx/Apache/`python -m http.server`):
      alle href="...zip"-Links werden eingesammelt.
    * JSON: entweder ["a.zip", "b.zip"], oder [{"name": "...", "url": "...",
      "size": 123, "description": "..."}], oder {"packages": [...]} - so kann
      man später einen "richtigen" Store-Server mit Metadaten betreiben.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import db
from app import modules
from app.auth import get_current_user, require_perm
from app.paths import MODULES_DIR

router = APIRouter(tags=["store"])

# Download-Obergrenze für Modul-ZIPs (Schutz vor Speicher-/Plattenfressern).
MAX_ZIP_BYTES = 300 * 1024 * 1024
HTTP_TIMEOUT = 20


def _admin(user: dict) -> None:
    # Gleiche Berechtigung wie die Modul-Verwaltung.
    require_perm(user, "manage_modules")


# ==================================================================
# AKTIVIERUNG
# ==================================================================

class ActivationBody(BaseModel):
    key: str


@router.get("/api/activation")
def get_activation(user: dict = Depends(get_current_user)):
    _admin(user)
    row = db.get_activation()
    if not row:
        return {"key": None, "redeemed_at": None, "redeemed_by": None}
    return {"key": row["key"], "redeemed_at": row["redeemed_at"],
            "redeemed_by": row.get("redeemed_by")}


@router.post("/api/activation")
def redeem_activation(body: ActivationBody, user: dict = Depends(get_current_user)):
    _admin(user)
    key = (body.key or "").strip()
    if len(key) < 4:
        raise HTTPException(400, "Aktivierungscode ist zu kurz")
    if len(key) > 200:
        raise HTTPException(400, "Aktivierungscode ist zu lang")
    row = db.redeem_activation_key(key, user.get("username"))
    db.add_audit_entry(user.get("username"), "activation.redeem", None,
                       f"Aktivierungscode eingelöst ({key[:4]}…)")
    return {"key": row["key"], "redeemed_at": row["redeemed_at"],
            "redeemed_by": row.get("redeemed_by")}


# ==================================================================
# MODUL-STORE: Paketquellen
# ==================================================================

class SourceBody(BaseModel):
    name: str = ""
    url: str


def _check_source_url(url: str) -> str:
    url = (url or "").strip()
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise HTTPException(400, "Adresse muss mit http:// oder https:// beginnen")
    return url


@router.get("/api/modstore/sources")
def list_sources(user: dict = Depends(get_current_user)):
    _admin(user)
    return db.list_module_sources()


@router.post("/api/modstore/sources")
def add_source(body: SourceBody, user: dict = Depends(get_current_user)):
    _admin(user)
    url = _check_source_url(body.url)
    name = (body.name or "").strip() or urllib.parse.urlparse(url).netloc
    row = db.add_module_source(name, url)
    db.add_audit_entry(user.get("username"), "modstore.source.add", None, url)
    return row


@router.delete("/api/modstore/sources/{source_id}")
def delete_source(source_id: str, user: dict = Depends(get_current_user)):
    _admin(user)
    db.delete_module_source(source_id)
    return {"ok": True}


# ==================================================================
# MODUL-STORE: Quelle durchsuchen + installieren
# ==================================================================

def _http_get(url: str, limit: int) -> bytes:
    """GET mit Timeout und Größenlimit (liest höchstens limit+1 Bytes)."""
    req = urllib.request.Request(url, headers={"User-Agent": "BYTERA-RMM ModStore"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(400, "Datei ist zu groß")
    return data


def _parse_listing(base_url: str, raw: bytes) -> list[dict]:
    """Zieht alle .zip-Einträge aus einem Verzeichnis-Listing oder JSON-Manifest."""
    text = raw.decode("utf-8", "replace")
    entries: dict[str, dict] = {}

    def add(name: str, url: str, size=None, description=""):
        name = name.strip()
        if not name.lower().endswith(".zip"):
            return
        full = urllib.parse.urljoin(base_url if base_url.endswith("/") else base_url + "/", url)
        entries[full] = {"name": urllib.parse.unquote(name), "url": full,
                         "size": size, "description": description or ""}

    stripped = text.lstrip()
    if stripped.startswith("[") or stripped.startswith("{"):
        # JSON-Manifest
        try:
            data = json.loads(stripped)
            if isinstance(data, dict):
                data = data.get("packages") or data.get("modules") or []
            for item in data if isinstance(data, list) else []:
                if isinstance(item, str):
                    add(item.rsplit("/", 1)[-1], item)
                elif isinstance(item, dict):
                    u = str(item.get("url") or item.get("file") or item.get("name") or "")
                    n = str(item.get("name") or u.rsplit("/", 1)[-1])
                    if not n.lower().endswith(".zip"):
                        n += ".zip" if u.lower().endswith(".zip") else ""
                    add(n or u, u, item.get("size"), str(item.get("description") or ""))
            return list(entries.values())
        except (json.JSONDecodeError, TypeError):
            pass  # dann wie HTML/Text behandeln

    # HTML-Listing: alle Links auf .zip
    for m in re.finditer(r'href=["\']([^"\']+?\.zip)["\']', text, re.I):
        u = m.group(1)
        add(u.rsplit("/", 1)[-1], u)
    # Plain-Text-Listing: eine Datei pro Zeile
    if not entries:
        for line in text.splitlines():
            line = line.strip()
            if line.lower().endswith(".zip") and "<" not in line:
                add(line.rsplit("/", 1)[-1], line)
    return list(entries.values())


@router.get("/api/modstore/browse/{source_id}")
def browse_source(source_id: str, user: dict = Depends(get_current_user)):
    _admin(user)
    src = db.get_module_source(source_id)
    if not src:
        raise HTTPException(404, "Paketquelle nicht gefunden")
    try:
        raw = _http_get(src["url"], 5 * 1024 * 1024)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Quelle nicht erreichbar: {exc}")
    pkgs = _parse_listing(src["url"], raw)
    pkgs.sort(key=lambda p: p["name"].lower())
    # Welche Module sind schon installiert? (Abgleich über die Modul-IDs)
    installed = {m.get("id"): m.get("version") for m in modules.list_all()}
    return {"source": src, "packages": pkgs, "installed": installed}


class InstallBody(BaseModel):
    source_id: str
    url: str


def _same_origin(a: str, b: str) -> bool:
    pa, pb = urllib.parse.urlparse(a), urllib.parse.urlparse(b)
    return (pa.scheme, pa.netloc) == (pb.scheme, pb.netloc)


def _find_module_root(tmp: Path) -> Path | None:
    """module.json direkt im ZIP-Wurzelordner oder eine Ebene tiefer."""
    if (tmp / "module.json").exists():
        return tmp
    candidates = [d for d in tmp.iterdir() if d.is_dir() and (d / "module.json").exists()]
    return candidates[0] if len(candidates) == 1 else None


@router.post("/api/modstore/install")
def install_package(body: InstallBody, user: dict = Depends(get_current_user)):
    _admin(user)
    src = db.get_module_source(body.source_id)
    if not src:
        raise HTTPException(404, "Paketquelle nicht gefunden")
    if not _same_origin(src["url"], body.url):
        raise HTTPException(400, "Download-Adresse gehört nicht zu dieser Paketquelle")

    data = None
    try:
        data = _http_get(body.url, MAX_ZIP_BYTES)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Download fehlgeschlagen: {exc}")

    with tempfile.TemporaryDirectory(prefix="modstore-") as tdir:
        tmp = Path(tdir)
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise HTTPException(400, "Die Datei ist kein gültiges ZIP-Archiv")
        # Sicheres Entpacken (kein Zip-Slip: keine Pfade außerhalb des Zielordners).
        for info in zf.infolist():
            dest = (tmp / info.filename).resolve()
            if not str(dest).startswith(str(tmp.resolve())):
                raise HTTPException(400, "ZIP enthält unzulässige Pfade")
        zf.extractall(tmp)

        root = _find_module_root(tmp)
        if root is None:
            raise HTTPException(400, "Im ZIP wurde keine module.json gefunden - "
                                     "das ist kein BYTERA-Modul-Paket")
        try:
            meta = json.loads((root / "module.json").read_text(encoding="utf-8"))
        except Exception:
            raise HTTPException(400, "module.json im Paket ist nicht lesbar")
        mid = str(meta.get("id") or "").strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}", mid):
            raise HTTPException(400, f"Ungültige Modul-ID in module.json: {mid!r}")

        target = Path(MODULES_DIR) / mid
        # Update erlaubt: vorhandenen Ordner ersetzen (wie "Installieren" im
        # DSM-Store). Deaktivieren/Aktivieren übernimmt die Modul-Verwaltung.
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(root, target)

    modules.discover()
    db.add_audit_entry(user.get("username"), "modstore.install", mid,
                       f"{meta.get('name') or mid} v{meta.get('version') or '?'} von {body.url}")
    return {"installed": mid, "name": meta.get("name") or mid,
            "version": meta.get("version") or "?",
            "hint": "Modul installiert - zum Verwenden in der Modul-Verwaltung aktivieren."}
