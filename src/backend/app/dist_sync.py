# dist_sync.py (Core) - frueher Modul "dist" - vorhandene Agent-/Client-Pakete nach /dist spiegeln
# --------------------------------------------------------------------------
# Mitgelieferte Client-Pakete (src/client/release, Teil des Codes) werden nach
# storage/dist/client gespiegelt (nur wenn neuer/fehlend), damit /dist/client
# sie zeigt. Eigene Builds und Uploads landen ohnehin direkt dort.
from __future__ import annotations

import shutil
from pathlib import Path

from app.paths import BACKEND_DIR

from app import dist_store as store

CLIENT_SOURCES = [BACKEND_DIR.parent / "client" / "release"]
CLIENT_EXT = (".exe", ".msi", ".zip", ".deb", ".rpm", ".pkg.tar.xz", ".tar.gz", ".tgz", ".tar", ".apk")


def _copy_new(src: Path, dst: Path) -> int:
    n = 0
    if not src.is_dir():
        return 0
    dst.mkdir(parents=True, exist_ok=True)
    for f in src.iterdir():
        if not f.is_file() or not f.name.lower().endswith(CLIENT_EXT):
            continue
        target = dst / f.name
        if not target.exists() or target.stat().st_mtime < f.stat().st_mtime or target.stat().st_size != f.stat().st_size:
            try:
                shutil.copy2(f, target)
                n += 1
            except OSError:
                pass
    return n


def sync_packages() -> int:
    store.ensure_dirs()
    n = 0
    client_dst = store.DIST_DIR / "client"
    for src in CLIENT_SOURCES:
        n += _copy_new(src, client_dst)
    return n
