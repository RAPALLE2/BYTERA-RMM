# legacy.py - Umbenennung RAPALLE.net RMM -> BYTERA RMM (nur Standardbibliothek)
# -------------------------------------------------------------------------------
# Eine aeltere App aktualisiert sich mit IHREM (alten) Update-Code. Dabei landet die
# neue Version teils noch am alten Ort:
#   exe   %LOCALAPPDATA%\Programs\RapalleClient\RapalleClient.exe  (alte .exe ersetzt)
#   tar   ~/.local/share/rapalle-client bzw. /opt/rapalle-client     (Dateien ersetzt)
# Beim ersten Start zieht die App deshalb selbst an den neuen Ort um, legt Starter und
# Verknuepfungen neu an, entfernt die alten und startet sich am neuen Ort neu.
# MSI/deb/rpm/pkg erledigt der Paketmanager (gleicher UpgradeCode bzw. Replaces/Obsoletes).
# Datenordner, gespeicherte Anmeldung, Autostart und Link-Schema migrieren store.py,
# secrets_store.py, autostart.py und urlscheme.py.
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

_NOWIN = 0x08000000 if os.name == "nt" else 0


def _ps(script: str) -> None:
    try:
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                       capture_output=True, timeout=30, creationflags=_NOWIN)
    except Exception:
        pass


# ------------------------------------------------------------------ Windows: .exe
def _relocate_exe() -> bool:
    exe = Path(sys.executable).resolve()
    if exe.parent.name.lower() != "rapalleclient":
        return False
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Programs" / "ByteraClient"
    target = base / "ByteraClient.exe"
    try:
        base.mkdir(parents=True, exist_ok=True)
        shutil.copy2(exe, target)
    except OSError:
        return False
    # Verknuepfungen: dort neu anlegen, wo es die alte gab (mindestens Desktop), alte entfernen
    _ps("$sh = New-Object -ComObject WScript.Shell; $made = $false; "
        "foreach ($d in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'))) { "
        "  $old = Join-Path $d 'RAPALLE.net RMM.lnk'; "
        "  if (Test-Path $old) { Remove-Item -Force $old; "
        f"    $l = $sh.CreateShortcut((Join-Path $d 'BYTERA RMM.lnk')); $l.TargetPath = '{target}'; "
        f"    $l.WorkingDirectory = '{base}'; $l.IconLocation = '{target},0'; $l.Save(); $made = $true }} }} "
        "if (-not $made) { "
        f"  $l = $sh.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) 'BYTERA RMM.lnk')); "
        f"  $l.TargetPath = '{target}'; $l.WorkingDirectory = '{base}'; $l.IconLocation = '{target},0'; $l.Save() }}")
    # Nach dem Beenden: alten Ordner loeschen und die App am neuen Ort starten
    import tempfile
    bat = Path(tempfile.mkdtemp(prefix="bytera-move-")) / "move.cmd"
    args = " ".join(f'"{a}"' for a in sys.argv[1:])
    bat.write_text(
        "@echo off\r\n"
        f":warten\r\ntasklist /FI \"PID eq {os.getpid()}\" | find \"{os.getpid()}\" >nul && "
        "(timeout /t 1 /nobreak >nul & goto warten)\r\n"
        f"rmdir /s /q \"{exe.parent}\"\r\nstart \"\" \"{target}\" {args}\r\n(goto) 2>nul & del \"%~f0\"\r\n",
        encoding="mbcs")
    subprocess.Popen(["cmd", "/c", str(bat)], creationflags=0x00000008 | _NOWIN, close_fds=True)
    return True


# ------------------------------------------------------------------ Linux: .tar.gz-Installation
_DESKTOP = ("[Desktop Entry]\nType=Application\nName=BYTERA RMM\nComment=RMM-Dashboard als App\n"
            "Exec={exe} %u\nIcon=bytera-client\nTerminal=false\nCategories=Network;RemoteAccess;\n"
            "MimeType=x-scheme-handler/bytera-rmm;\n")


def _relocate_tar() -> bool:
    from .updater import app_dir
    old = app_dir()
    if old.name != "rapalle-client":
        return False
    new = old.with_name("bytera-client")
    system = str(old).startswith("/opt/")
    home = Path.home()
    bins = [Path("/usr/local/bin")] if system else [home / ".local/bin"]
    apps = Path("/usr/share/applications") if system else home / ".local/share/applications"
    icons = Path("/usr/share/icons/hicolor/256x256/apps") if system else home / ".local/share/icons/hicolor/256x256/apps"
    try:
        shutil.copytree(old, new, dirs_exist_ok=True)
        for junk in ("rapalle_client", "rapalle-client.py", ".rapalle-install"):
            p = new / junk
            shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
        (new / ".bytera-install").write_text("tar\n", encoding="utf-8")
        for b in bins:
            b.mkdir(parents=True, exist_ok=True)
            starter = b / "bytera-client"
            starter.write_text(f'#!/bin/sh\nexec python3 "{new}/bytera-client.py" "$@"\n', encoding="utf-8")
            starter.chmod(0o755)
            (b / "rapalle-client").unlink(missing_ok=True)
        apps.mkdir(parents=True, exist_ok=True)
        (apps / "bytera-client.desktop").write_text(_DESKTOP.format(exe=bins[0] / "bytera-client"), encoding="utf-8")
        (apps / "rapalle-client.desktop").unlink(missing_ok=True)
        res = new / "bytera_client" / "resources" / "rmmclient-256.png"
        if res.is_file():
            icons.mkdir(parents=True, exist_ok=True)
            shutil.copy2(res, icons / "bytera-client.png")
        (icons / "rapalle-client.png").unlink(missing_ok=True)
        shutil.rmtree(old, ignore_errors=True)
    except OSError:
        return False
    os.execv(sys.executable, [sys.executable, str(new / "bytera-client.py"), *sys.argv[1:]])
    return True                                        # (nicht erreicht)


def migrate() -> bool:
    """Beim Start aufrufen. True = diese Instanz soll sich beenden (Neustart am neuen Ort laeuft)."""
    try:
        if os.name == "nt":
            if getattr(sys, "frozen", False) and _relocate_exe():
                return True
            try:
                from . import elevate
                elevate.migrate_legacy_task()
            except Exception:
                pass
            return False
        from .updater import install_kind
        if install_kind() == "tar":
            return _relocate_tar()
    except Exception:
        pass
    return False
