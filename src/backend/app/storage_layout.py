"""
storage_layout.py (Core)
------------------------
ONE location for all files: src/backend/storage (paths.STORAGE_DIR).

At startup (before the modules are loaded) everything still in old locations is
moved to storage/:

    old                                   new
    backend/media/recordings              storage/recordings
    backend/media/tickets                 storage/tickets
    backend/media/audio                   storage/audio
    backend/temp/dist                     storage/dist/agent      (built agent installers)
    backend/data/dist/<folder>            storage/dist/<folder>
    backend/dist/<folder>                 storage/dist/<folder>
    storage/agent                         storage/dist/agent
    storage/client                        storage/dist/client     (uploads + own builds)
    storage/dist/enrollment               storage/dist/enroll
    storage/deployment                    storage/web/deploy
    storage/web/deployment                storage/web/deploy

Rules:
  * Files are MOVED, not copied, and nothing is ever overwritten: if a file of
    the same name already exists at the target, the old one gets the suffix
    " (old N)".
  * Empty old folders are removed. Anything still left in old locations
    afterwards (e.g. unknown files) is NOT touched but listed in
    logs/backend/storage-cleanup.txt.
  * Runs on every startup, but is a no-op after the first time.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from app.paths import (
    BACKEND_DIR, DATA_DIR, SRC_DIR, STORAGE_DIR, DIST_DIR, WEB_DIR,
    RECORDINGS_DIR, AUDIO_DIR, TICKET_FILES_DIR, DEPLOYMENT_DIR,
    DIST_AGENT_DIR, DIST_CLIENT_DIR, DIST_ENROLL_DIR, BACKEND_LOG_DIR,
)

MEDIA = BACKEND_DIR / "media"
TEMP = BACKEND_DIR / "temp"

# (old, new, "dir" = move the folder's contents | "dist" = each subfolder -> dist/<name>)
MOVES: list[tuple[Path, Path, str]] = [
    (MEDIA / "recordings", RECORDINGS_DIR, "dir"),
    (MEDIA / "tickets", TICKET_FILES_DIR, "dir"),
    (MEDIA / "audio", AUDIO_DIR, "dir"),
    (TEMP / "dist", DIST_AGENT_DIR, "dir"),
    (DATA_DIR / "dist", DIST_DIR, "dist"),
    (BACKEND_DIR / "dist", DIST_DIR, "dist"),
    (STORAGE_DIR / "agent", DIST_AGENT_DIR, "dir"),
    (STORAGE_DIR / "client", DIST_CLIENT_DIR, "dir"),
    (DIST_DIR / "enrollment", DIST_ENROLL_DIR, "dir"),
    (STORAGE_DIR / "deployment", DEPLOYMENT_DIR, "dir"),
    (WEB_DIR / "deployment", DEPLOYMENT_DIR, "dir"),
]

# Old locations that should no longer exist after the migration (for the cleanup list).
LEGACY_DIRS: list[Path] = [
    MEDIA, TEMP, DATA_DIR / "dist", BACKEND_DIR / "dist", SRC_DIR / "files",
    STORAGE_DIR / "agent", STORAGE_DIR / "client", STORAGE_DIR / "deployment",
    DIST_DIR / "enrollment", WEB_DIR / "deployment",
]

REPORT = BACKEND_LOG_DIR / "storage-cleanup.txt"


def _free_name(target: Path) -> Path:
    if not target.exists():
        return target
    n = 1
    while True:
        cand = target.with_name(f"{target.stem} (old {n}){target.suffix}")
        if not cand.exists():
            return cand
        n += 1


def _merge(src: Path, dst: Path) -> int:
    """Move contents of src to dst (merge recursively). -> number of entries moved."""
    moved = 0
    dst.mkdir(parents=True, exist_ok=True)
    for item in list(src.iterdir()):
        target = dst / item.name
        if item.is_dir() and target.is_dir():
            moved += _merge(item, target)
            _rm_empty(item)
            continue
        try:
            shutil.move(str(item), str(_free_name(target)))
            moved += 1
        except OSError as e:
            print(f"[storage] could not move {item}: {e}")
    return moved


def _rm_empty(path: Path) -> None:
    """Remove empty folders (including nested empty ones)."""
    if not path.is_dir():
        return
    for sub in list(path.iterdir()):
        if sub.is_dir():
            _rm_empty(sub)
    try:
        if not any(path.iterdir()):
            path.rmdir()
    except OSError:
        pass


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def migrate() -> dict:
    """Move old locations to storage/. Returns {old: count}."""
    done: dict[str, int] = {}
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    for src, dst, mode in MOVES:
        if not src.is_dir() or _same(src, dst):
            continue
        try:
            if mode == "dist":
                n = 0
                for sub in list(src.iterdir()):
                    name = "enroll" if sub.name == "enrollment" else sub.name
                    if sub.is_dir():
                        n += _merge(sub, dst / name)
                    else:                                  # loose file -> dist/agent
                        shutil.move(str(sub), str(_free_name(DIST_AGENT_DIR / sub.name)))
                        n += 1
            else:
                n = _merge(src, dst)
            _rm_empty(src)
            if n:
                done[str(src.relative_to(SRC_DIR.parent))] = n
        except Exception as e:
            print(f"[storage] move {src} -> {dst} failed: {e}")
    for d in LEGACY_DIRS:
        _rm_empty(d)
    if done:
        print("[storage] files moved to the new location (storage/): "
              + ", ".join(f"{k} ({v})" for k, v in done.items()))
    write_report()
    return done


def leftovers() -> list[dict]:
    """Old folders that still exist (not empty after the move)."""
    out = []
    for d in LEGACY_DIRS:
        if d.exists():
            files = sum(1 for f in d.rglob("*") if f.is_file()) if d.is_dir() else 1
            out.append({"path": str(d.relative_to(SRC_DIR.parent)), "files": files})
    return out


def write_report() -> None:
    rest = leftovers()
    try:
        BACKEND_LOG_DIR.mkdir(parents=True, exist_ok=True)
        if not rest:
            if REPORT.exists():
                REPORT.unlink()
            return
        lines = ["Old storage locations that still contain files (were NOT moved automatically).",
                 "Check the contents, move them to src/backend/storage/ by hand if needed, then delete the folder:", ""]
        lines += [f"  {r['path']}   ({r['files']} file(s))" for r in rest]
        REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"[storage] Warning: {len(rest)} old folder(s) with unknown content - see {REPORT}")
    except OSError:
        pass
