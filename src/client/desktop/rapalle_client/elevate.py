"""
elevate.py - Administratorrechte fuer die Client-App (Windows)
==============================================================

VPN braucht unter Windows Administratorrechte (WireGuard-Tunnel sind Dienste). Statt bei
jeder VPN-Aktion zu fragen, fragt die App EINMAL beim Start:
  * erlaubt   -> die App laeuft als Administrator und richtet VPN-Tunnel ohne weitere Fragen ein
  * verweigert -> die App laeuft normal weiter, VPN ist dann aus (keine Rueckfragen)

Option „Immer als Administrator starten (ohne Rückfrage)“: legt einmalig eine geplante Aufgabe
mit hoechsten Rechten an (RapalleRMM\\ClientAdmin). Jeder normale Start (Verknuepfung, Autostart)
startet dann diese Aufgabe -> Administratorrechte ohne UAC-Abfrage.

Programme fuer den Benutzer (Dashboard-Fenster im Browser) werden aus einer Administrator-App
NICHT mit Adminrechten gestartet, sondern ueber die Explorer-Shell als normaler Benutzer.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

IS_WINDOWS = os.name == "nt"
TASK = r"RapalleRMM\ClientAdmin"
_NOWIN = 0x08000000


def is_admin() -> bool:
    if not IS_WINDOWS:
        return hasattr(os, "geteuid") and os.geteuid() == 0
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _self_cmd(extra: list[str]) -> tuple[str, str]:
    """(Programm, Parameter) fuer einen Neustart dieser App."""
    if getattr(sys, "frozen", False):
        return sys.executable, subprocess.list2cmdline(extra)
    script = os.path.abspath(sys.argv[0])
    return sys.executable, subprocess.list2cmdline([script, *extra])


def relaunch_elevated(extra: list[str]) -> bool:
    """UAC-Abfrage; True = Administrator-Instanz gestartet (diese beendet sich dann)."""
    if not IS_WINDOWS:
        return False
    import ctypes
    exe, params = _self_cmd(extra + ["--elevated"])
    r = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
    return int(r) > 32                      # <= 32: abgelehnt (5) oder Fehler


# ------------------------------------------------------------------ geplante Aufgabe (ohne Rueckfrage)
def task_exists() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        return subprocess.run(["schtasks", "/query", "/tn", TASK], capture_output=True,
                              timeout=15, creationflags=_NOWIN).returncode == 0
    except Exception:
        return False


def run_task() -> bool:
    try:
        return subprocess.run(["schtasks", "/run", "/tn", TASK], capture_output=True,
                              timeout=15, creationflags=_NOWIN).returncode == 0
    except Exception:
        return False


def create_task() -> tuple[bool, str]:
    """Nur als Administrator moeglich. Startet die App mit hoechsten Rechten im Benutzerkontext."""
    exe, params = _self_cmd(["--elevated", "--from-task"])
    user = os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "")
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>RAPALLE.net RMM Client als Administrator (VPN ohne Rückfrage)</Description></RegistrationInfo>
  <Principals><Principal id="Author"><UserId>{user}</UserId><LogonType>InteractiveToken</LogonType>
    <RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>Parallel</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <AllowStartOnDemand>true</AllowStartOnDemand><Priority>5</Priority></Settings>
  <Actions Context="Author"><Exec><Command>{exe}</Command><Arguments>{params.replace('&', '&amp;').replace('<', '&lt;')}</Arguments></Exec></Actions>
</Task>"""
    fd, path = tempfile.mkstemp(suffix=".xml")
    with os.fdopen(fd, "w", encoding="utf-16") as f:
        f.write(xml)
    try:
        r = subprocess.run(["schtasks", "/create", "/tn", TASK, "/xml", path, "/f"], capture_output=True,
                           text=True, timeout=30, creationflags=_NOWIN)
        return r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def delete_task() -> tuple[bool, str]:
    r = subprocess.run(["schtasks", "/delete", "/tn", TASK, "/f"], capture_output=True, text=True,
                       timeout=30, creationflags=_NOWIN)
    return r.returncode == 0, (r.stdout or "") + (r.stderr or "")


# ------------------------------------------------------------------ als normaler Benutzer starten
def run_unelevated(cmd: list[str]) -> bool:
    """Aus einer Administrator-App ein Programm als normaler Benutzer starten (ueber die Explorer-Shell)."""
    if not (IS_WINDOWS and is_admin()):
        return False
    fd, path = tempfile.mkstemp(suffix=".cmd", prefix="rmm-start-")
    with os.fdopen(fd, "w", encoding="mbcs" if IS_WINDOWS else "utf-8") as f:
        f.write("@echo off\r\nstart \"\" " + subprocess.list2cmdline(cmd) + "\r\n(goto) 2>nul & del \"%~f0\"\r\n")
    subprocess.Popen(["explorer.exe", path], creationflags=_NOWIN)
    return True


def install_mode() -> str | None:
    """Auswahl aus dem MSI-Installationsassistenten: '1' = mit Administratorrechten, '0' = ohne, None = unbekannt."""
    if not IS_WINDOWS:
        return None
    try:
        import winreg
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"Software\RAPALLE.net\RMMClient", 0,
                                    winreg.KEY_READ | view) as k:
                    v, _ = winreg.QueryValueEx(k, "AdminMode")
                    v = str(v).strip()
                    if v in ("0", "1"):
                        return v
            except OSError:
                continue
    except Exception:
        pass
    return None


def create_desktop_shortcut() -> None:
    """Desktop-Verknuepfung "RAPALLE.net RMM" auf diese .exe (wie bei der MSI-Installation)."""
    if not (IS_WINDOWS and getattr(sys, "frozen", False)):
        return
    exe = sys.executable
    ps = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut("
          "(Join-Path ([Environment]::GetFolderPath('Desktop')) 'RAPALLE.net RMM.lnk')); "
          f"$s.TargetPath = '{exe}'; $s.WorkingDirectory = '{os.path.dirname(exe)}'; "
          f"$s.IconLocation = '{exe},0'; $s.Save()")
    try:
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                       capture_output=True, timeout=30, creationflags=_NOWIN)
    except Exception:
        pass


def installed_by_msi() -> bool:
    exe = sys.executable.lower()
    return any(p and exe.startswith(p.lower()) for p in
               (os.environ.get(k, "") for k in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")))


def first_run_setup(store) -> None:
    """Die .exe ohne Installation verhaelt sich wie die MSI: beim ersten Start derselbe Assistent
    (mit/ohne Administratorrechte) und eine Desktop-Verknuepfung."""
    if not (IS_WINDOWS and getattr(sys, "frozen", False)) or store.get("setup_done") or installed_by_msi():
        return
    choice = {"mode": "1"}
    try:
        import tkinter as tk
        from tkinter import ttk
        root = tk.Tk()
        root.title("RAPALLE.net RMM Client – Einrichtung")
        root.resizable(False, False)
        f = ttk.Frame(root, padding=18)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="RAPALLE.net RMM Client einrichten", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        ttk.Label(f, text="Wie soll die App laufen? VPN-Tunnel (WireGuard) brauchen unter Windows "
                          "Administratorrechte.", wraplength=420, justify="left").pack(anchor="w", pady=(6, 10))
        v = tk.StringVar(value="1")
        ttk.Radiobutton(f, variable=v, value="1", text="Mit Administratorrechten (empfohlen): startet immer als "
                        "Administrator, VPN ohne Rückfragen").pack(anchor="w", pady=2)
        ttk.Radiobutton(f, variable=v, value="0", text="Ohne Administratorrechte: keine Rückfragen beim Start, "
                        "VPN ist dann nicht verfügbar").pack(anchor="w", pady=2)
        lnk = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, variable=lnk, text="Verknüpfung auf dem Desktop anlegen").pack(anchor="w", pady=(10, 0))
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(14, 0))

        def ok():
            choice["mode"] = v.get()
            choice["lnk"] = lnk.get()
            root.destroy()
        ttk.Button(row, text="Weiter", command=ok).pack(side="right")
        root.protocol("WM_DELETE_WINDOW", ok)
        root.mainloop()
    except Exception:
        choice["lnk"] = True
    if choice.get("lnk", True):
        create_desktop_shortcut()
    store.set(setup_done=True, install_mode=choice["mode"])


def ensure_desktop_shortcut(store) -> None:      # (aeltere Aufrufe)
    first_run_setup(store)


def _mode(store) -> str | None:
    return install_mode() or store.get("install_mode")


def _apply_install_mode(argv: list[str], store, extra: list[str]) -> bool:
    """Auswahl (MSI-Assistent bzw. Einrichtung der .exe) einmalig umsetzen. True = Admin-Instanz gestartet."""
    mode = _mode(store)
    if mode is None or store.get("install_mode_applied") == mode:
        return False
    if mode == "0":                                    # „Ohne Administratorrechte“: nie nachfragen
        store.set(admin_prompt=False, install_mode_applied="0")
        return False
    if task_exists():
        store.set(install_mode_applied="1")
        return False
    if is_admin():                                     # „Mit Administratorrechten“: Aufgabe anlegen
        ok, _ = create_task()
        if ok:
            store.set(install_mode_applied="1", admin_prompt=True)
        return False
    if "--elevated" in argv:
        return False
    return relaunch_elevated(extra)                    # einmalige Abfrage zum Einrichten


# ------------------------------------------------------------------ Startmarke (Start wirklich erfolgt?)
def _marker(data_dir: Path) -> Path:
    return Path(data_dir) / "started.marker"


def mark_started(data_dir: Path) -> None:
    """Ganz am Anfang jeder Instanz: zeigt der startenden Instanz, dass die Admin-Instanz wirklich laeuft."""
    try:
        _marker(data_dir).write_text(f"{os.getpid()} {int(is_admin())} {time.time()}")
    except OSError:
        pass


def _wait_started(data_dir: Path, since: float, timeout: float = 10) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if _marker(data_dir).stat().st_mtime >= since - 0.5:
                return True
        except OSError:
            pass
        time.sleep(0.25)
    return False


def startup(argv: list[str], store, data_dir: Path | None = None) -> bool:
    """Beim Start aufrufen. True = diese Instanz beenden (eine Admin-Instanz laeuft NACHWEISLICH).
    Startet die Admin-Instanz nicht (Aufgabe haengt, Abfrage abgelehnt …), laeuft diese normal weiter -
    die App startet also IMMER."""
    if not IS_WINDOWS:
        return False
    extra = [a for a in argv if a not in ("--elevated", "--from-task", "--setup-admin", "--remove-admin")]
    try:
        first_run_setup(store)
    except Exception:
        pass
    if is_admin():
        os.environ["RMM_ADMIN"] = "1"
        try:
            _apply_install_mode(argv, store, extra)    # als Admin: Aufgabe ggf. anlegen
        except Exception:
            pass
        return False
    os.environ["RMM_ADMIN"] = "0"
    if "--elevated" in argv:                           # Admin-Start versucht, aber nicht admin -> nicht nochmal
        return False
    t0 = time.time()
    try:
        recent_fail = time.time() - float(store.get("task_start_failed", 0) or 0) < 86400
        if not recent_fail and task_exists() and run_task():   # „Immer als Administrator“: ohne Rueckfrage
            if data_dir is None or _wait_started(data_dir, t0):
                return True
            # Aufgabe gestartet, aber keine Admin-Instanz -> normal weiter (statt "nichts passiert")
            store.set(task_start_failed=time.time())
            return False
        mode = _mode(store)
        want = store.get("admin_prompt", True) and mode != "0"
        if want and store.get("install_mode_applied") != mode and mode == "1":
            t1 = time.time()
            if _apply_install_mode(argv, store, extra):
                return data_dir is None or _wait_started(data_dir, t1, 15)
            return False
        if want:
            t1 = time.time()
            if relaunch_elevated(extra):
                if data_dir is None or _wait_started(data_dir, t1, 15):
                    return True
                return False                           # Admin-Instanz kam nicht hoch -> normal weiter
            store.set(admin_denied_ts=time.time())
    except Exception:
        pass
    return False
