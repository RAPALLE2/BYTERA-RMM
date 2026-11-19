"""
routers/store_routes.py
-----------------------
Activation + module store (both under Settings).

ACTIVATION (/api/activation):
  Input field for an activation code. The code has NO function yet - it is only
  stored in the database with its redemption date. Later the RMM uses it to
  query the license server and may unlock premium features or load add-on
  modules.

MODULE STORE (/api/modstore/...):
  Custom package sources like the community store of Synology DSM: the admin
  configures a server address (HTTP directory listing OR JSON manifest). All
  .zip files found there are shown and can be installed directly as a module
  (download -> safe extraction -> modules/<id>/ -> rescan). The module is then
  enabled as usual in the module management.

  Supported source formats:
    * HTML directory listing (nginx/Apache/`python -m http.server`): all
      href="...zip" links are collected.
    * JSON: either ["a.zip", "b.zip"], or [{"name": "...", "url": "...",
      "size": 123, "description": "..."}], or {"packages": [...]} - so you can
      later run a "real" store server with metadata.
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

# Download upper limit for module ZIPs (protection against memory/disk hogs).
MAX_ZIP_BYTES = 300 * 1024 * 1024
HTTP_TIMEOUT = 20


def _admin(user: dict) -> None:
    # Same permission as the module management.
    require_perm(user, "manage_modules")


# ==================================================================
# ACTIVATION
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
        raise HTTPException(400, "Activation code is too short")
    if len(key) > 200:
        raise HTTPException(400, "Activation code is too long")
    row = db.redeem_activation_key(key, user.get("username"))
    db.add_audit_entry(user.get("username"), "activation.redeem", None,
                       f"activation code redeemed ({key[:4]}...)")
    return {"key": row["key"], "redeemed_at": row["redeemed_at"],
            "redeemed_by": row.get("redeemed_by")}


# ==================================================================
# MODULE STORE: package sources
# ==================================================================

class SourceBody(BaseModel):
    name: str = ""
    url: str


def _check_source_url(url: str) -> str:
    url = (url or "").strip()
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise HTTPException(400, "Address must start with http:// or https://")
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
# MODULE STORE: browse source + install
# ==================================================================

def _http_get(url: str, limit: int) -> bytes:
    """GET with timeout and size limit (reads at most limit+1 bytes)."""
    req = urllib.request.Request(url, headers={"User-Agent": "BYTERA-RMM ModStore"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(400, "File is too large")
    return data


def _parse_listing(base_url: str, raw: bytes) -> list[dict]:
    """Extracts all .zip entries from a directory listing or JSON manifest."""
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
        # JSON manifest
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
            pass  # then treat it as HTML/text

    # HTML listing: all links to .zip
    for m in re.finditer(r'href=["\']([^"\']+?\.zip)["\']', text, re.I):
        u = m.group(1)
        add(u.rsplit("/", 1)[-1], u)
    # Plain-text listing: one file per line
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
        raise HTTPException(404, "Package source not found")
    try:
        raw = _http_get(src["url"], 5 * 1024 * 1024)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Source not reachable: {exc}")
    pkgs = _parse_listing(src["url"], raw)
    pkgs.sort(key=lambda p: p["name"].lower())
    # Which modules are already installed? (matched by module IDs)
    installed = {m.get("id"): m.get("version") for m in modules.list_all()}
    return {"source": src, "packages": pkgs, "installed": installed}


class InstallBody(BaseModel):
    source_id: str
    url: str


def _same_origin(a: str, b: str) -> bool:
    pa, pb = urllib.parse.urlparse(a), urllib.parse.urlparse(b)
    return (pa.scheme, pa.netloc) == (pb.scheme, pb.netloc)


def _find_module_root(tmp: Path) -> Path | None:
    """module.json directly in the ZIP root folder or one level deeper."""
    if (tmp / "module.json").exists():
        return tmp
    candidates = [d for d in tmp.iterdir() if d.is_dir() and (d / "module.json").exists()]
    return candidates[0] if len(candidates) == 1 else None


@router.post("/api/modstore/install")
def install_package(body: InstallBody, user: dict = Depends(get_current_user)):
    _admin(user)
    src = db.get_module_source(body.source_id)
    if not src:
        raise HTTPException(404, "Package source not found")
    if not _same_origin(src["url"], body.url):
        raise HTTPException(400, "Download address does not belong to this package source")

    data = None
    try:
        data = _http_get(body.url, MAX_ZIP_BYTES)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Download failed: {exc}")

    with tempfile.TemporaryDirectory(prefix="modstore-") as tdir:
        tmp = Path(tdir)
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise HTTPException(400, "The file is not a valid ZIP archive")
        # Safe extraction (no zip-slip: no paths outside the target folder).
        for info in zf.infolist():
            dest = (tmp / info.filename).resolve()
            if not str(dest).startswith(str(tmp.resolve())):
                raise HTTPException(400, "ZIP contains disallowed paths")
        zf.extractall(tmp)

        root = _find_module_root(tmp)
        if root is None:
            raise HTTPException(400, "No module.json found in the ZIP - "
                                     "this is not a BYTERA module package")
        try:
            meta = json.loads((root / "module.json").read_text(encoding="utf-8"))
        except Exception:
            raise HTTPException(400, "module.json in the package is not readable")
        mid = str(meta.get("id") or "").strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}", mid):
            raise HTTPException(400, f"Invalid module ID in module.json: {mid!r}")

        target = Path(MODULES_DIR) / mid
        # Update allowed: replace the existing folder (like "install" in the
        # DSM store). Disabling/enabling is handled by the module management.
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(root, target)

    modules.discover()
    db.add_audit_entry(user.get("username"), "modstore.install", mid,
                       f"{meta.get('name') or mid} v{meta.get('version') or '?'} from {body.url}")
    return {"installed": mid, "name": meta.get("name") or mid,
            "version": meta.get("version") or "?",
            "hint": "Module installed - enable it in the module management to use it."}
