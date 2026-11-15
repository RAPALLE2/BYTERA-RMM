"""
storage_layout.py (Core)
------------------------
EIN Ablageort fuer alle Dateien: src/backend/storage (paths.STORAGE_DIR).

Beim Start (vor dem Laden der Module) wird alles, was noch an alten Orten
liegt, nach storage/ verschoben:

    alt                                   neu
    backend/media/recordings              storage/recordings
    backend/media/tickets                 storage/tickets
    backend/media/audio                   storage/audio
    backend/temp/dist                     storage/dist/agent      (gebaute Agent-Installer)
    backend/data/dist/<ordner>            storage/dist/<ordner>
    backend/dist/<ordner>                 storage/dist/<ordner>
    storage/agent                         storage/dist/agent
    storage/client                        storage/dist/client     (Uploads + eigene Builds)
    storage/dist/enrollment               storage/dist/enroll
    storage/deployment                    storage/web/deploy
    storage/web/deployment                storage/web/deploy

Regeln:
  * Es wird VERSCHOBEN, nicht kopiert, und nie etwas ueberschrieben: Liegt am
    Ziel schon eine gleichnamige Datei, bekommt die alte den Zusatz " (alt N)".
  * Leere alte Ordner werden entfernt. Was danach noch an alten Orten liegt
    (z.B. unbekannte Dateien), wird NICHT angefasst, sondern in
    logs/backend/storage-cleanup.txt aufgelistet.
  * Laeuft bei jedem Start, ist aber nach dem ersten Mal ein No-op.
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

# (alt, neu, "dir" = Inhalt des Ordners verschieben | "dist" = jeder Unterordner -> dist/<name>)
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

# Alte Orte, die es nach dem Umbau nicht mehr geben soll (fuer die Aufraeum-Liste).
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
        cand = target.with_name(f"{target.stem} (alt {n}){target.suffix}")
        if not cand.exists():
            return cand
        n += 1


def _merge(src: Path, dst: Path) -> int:
    """Inhalt von src nach dst verschieben (rekursiv zusammenfuehren). -> Anzahl bewegter Eintraege."""
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
            print(f"[storage] konnte {item} nicht verschieben: {e}")
    return moved


def _rm_empty(path: Path) -> None:
    """Leere Ordner (auch verschachtelt leere) entfernen."""
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
    """Alte Orte nach storage/ verschieben. Gibt {alt: anzahl} zurueck."""
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
                    else:                                  # lose Datei -> dist/agent
                        shutil.move(str(sub), str(_free_name(DIST_AGENT_DIR / sub.name)))
                        n += 1
            else:
                n = _merge(src, dst)
            _rm_empty(src)
            if n:
                done[str(src.relative_to(SRC_DIR.parent))] = n
        except Exception as e:
            print(f"[storage] Umzug {src} -> {dst} fehlgeschlagen: {e}")
    for d in LEGACY_DIRS:
        _rm_empty(d)
    if done:
        print("[storage] Dateien an den neuen Ort (storage/) verschoben: "
              + ", ".join(f"{k} ({v})" for k, v in done.items()))
    write_report()
    return done


def leftovers() -> list[dict]:
    """Alte Ordner, die noch existieren (nach dem Umzug nicht leer)."""
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
        lines = ["Alte Ablageorte, die noch Dateien enthalten (wurden NICHT automatisch verschoben).",
                 "Inhalt pruefen, ggf. von Hand nach src/backend/storage/ legen, dann den Ordner loeschen:", ""]
        lines += [f"  {r['path']}   ({r['files']} Datei(en))" for r in rest]
        REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"[storage] Achtung: {len(rest)} alte(r) Ordner mit unbekanntem Inhalt - siehe {REPORT}")
    except OSError:
        pass
