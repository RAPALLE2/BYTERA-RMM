#!/usr/bin/env python3
"""
build_installers.py
===================
Builds ready-made installation packages for the RMM agent LOCALLY - for anyone
who cannot or does not want to use the PowerShell/Bash one-liner from the
dashboard (software distribution, GPO, Intune, apt/dnf repos, offline rollout).

Generated packages (depending on the build system, see below):

  Windows   ByteraRmmAgent-Setup.exe   PyInstaller onefile, asks for/knows the
                                        backend URL, installs Python if needed,
                                        creates the SYSTEM autostart task.
                                        Supports "/S" for silent.
            ByteraRmmAgent.msi         Only if WiX (candle/light) is present -
                                        the MSI wraps the Setup.exe (silent),
                                        so it can be distributed via GPO/Intune.
  Linux     bytera-rmm-agent_<v>.deb   Debian/Ubuntu, postinst sets up venv +
                                        systemd service.
            bytera-rmm-agent-<v>.rpm   Fedora/RHEL/openSUSE (needs rpmbuild).
            bytera-rmm-agent.run       Self-extracting shell script - runs
                                        everywhere and can ALWAYS be built
                                        (also on Windows).

All packages set up the same autostart as the web installers:
  Windows -> Task Scheduler, principal SYSTEM, trigger "at system startup"
             (so it runs WITHOUT a login; screen capture starts a helper in the
             active user session when needed)
  Linux   -> systemd unit bytera-agent.service, User=root, multi-user.target

Usage
-----
    python src/backend/app/build_installers.py --backend-url https://rmm.example.com
    python src/backend/app/build_installers.py --targets sh,deb --out dist
    python src/backend/app/build_installers.py --list

Without --backend-url the installer asks for the address at run time (or takes
it from the parameter -BackendUrl / RMM_BACKEND_URL).

The AGENT_TOKEN is - if present - read automatically from src/backend/data/.env,
so the distributed agents can register immediately.
"""

from __future__ import annotations

import argparse
import base64
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

# This file is in src/backend/app/ -> project root = three levels up.
ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src"
AGENT_DIR = SRC / "agent"
BACKEND_ENV = SRC / "backend" / "data" / ".env"
BRANDING = SRC / "frontend" / "image"
DIST_DIR = SRC / "backend" / "storage" / "dist" / "agent"   # = rmm.url/dist/agent

PKG_NAME = "bytera-rmm-agent"
WIN_INSTALL_DIR = r"C:\Program Files\ByteraRmmAgent"
LINUX_INSTALL_DIR = "/opt/bytera-rmm-agent"
TASK_NAME = "ByteraRmmAgent"


# ==========================================================================
# Helper functions
# ==========================================================================

def info(msg: str) -> None:
    print(f"[build] {msg}")


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


# --------------------------------------------------------------------------
# Install build tools automatically when needed
# --------------------------------------------------------------------------
# EXE and MSI need tools that are not available everywhere:
#   PyInstaller  -> Python package, installable via pip
#   WiX v3       -> not a Python package; we download the official binaries
#                   (candle.exe/light.exe) once and put them in the cache
#                   folder. No setup, no admin rights needed.
# Can be turned off with --no-auto-install.

AUTO_INSTALL = True
BUILD_REQUIREMENTS = SRC / "backend" / "data" / "requirements.txt"   # one shared file
CACHE_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / ".rmm-build"
# Official WiX v3 binaries (contains candle.exe + light.exe, ~10 MB).
WIX_URL = "https://github.com/wixtoolset/wix3/releases/download/wix3141rtm/wix314-binaries.zip"


def pip_install(spec: str) -> bool:
    """Installs a Python package into the RUNNING environment."""
    info(f"Installing build dependency: {spec} ...")
    base = [sys.executable, "-m", "pip", "install", "--quiet", spec]
    for extra in ([], ["--break-system-packages"], ["--user"]):
        if subprocess.run(base + extra).returncode == 0:
            return True
    info(f"pip install {spec} failed.")
    return False


def ensure_pyinstaller() -> bool:
    """PyInstaller present? Otherwise pull it via pip."""
    if have("pyinstaller"):
        return True
    try:
        import PyInstaller  # noqa: F401
        return True
    except ImportError:
        pass
    if not AUTO_INSTALL:
        info("PyInstaller missing (auto-installation is turned off). "
             "Install it with: pip install -r src/backend/data/requirements.txt")
        return False
    if not pip_install("pyinstaller>=6.0"):
        return False
    try:
        import PyInstaller  # noqa: F401
        return True
    except ImportError:
        return have("pyinstaller")


def pyinstaller_cmd() -> list[str]:
    """
    Call path for PyInstaller. After "pip install --user" the .exe is not
    necessarily in the PATH - then the module call works anyway.
    """
    if have("pyinstaller"):
        return ["pyinstaller"]
    return [sys.executable, "-m", "PyInstaller"]


def ensure_wix() -> tuple[str, str] | None:
    """
    Returns (candle, light). Order: PATH -> cache -> download.
    Only meaningful on Windows (candle/light are .exe).
    """
    if have("candle") and have("light"):
        return "candle", "light"

    wix_dir = CACHE_DIR / "wix314"
    candle, light = wix_dir / "candle.exe", wix_dir / "light.exe"
    if candle.is_file() and light.is_file():
        return str(candle), str(light)

    if platform.system() != "Windows":
        info("WiX only exists for Windows -> MSI skipped.")
        return None
    if not AUTO_INSTALL:
        info("WiX missing (auto-installation off) -> MSI skipped.")
        return None

    info("Downloading WiX v3 binaries (one-off, ~10 MB) ...")
    import urllib.request
    import zipfile as _zip
    try:
        wix_dir.mkdir(parents=True, exist_ok=True)
        archive = wix_dir / "wix-binaries.zip"
        urllib.request.urlretrieve(WIX_URL, archive)
        with _zip.ZipFile(archive) as zf:
            zf.extractall(wix_dir)
        archive.unlink(missing_ok=True)
    except Exception as e:
        info(f"WiX download failed ({e}) -> MSI skipped.")
        return None

    if candle.is_file() and light.is_file():
        info(f"WiX ready: {wix_dir}")
        return str(candle), str(light)
    info("WiX archive did not contain candle/light -> MSI skipped.")
    return None


def agent_version() -> str:
    """Version from agent/version.txt (only READ, never written)."""
    try:
        v = (AGENT_DIR / "version.txt").read_text(encoding="utf-8").strip()
        return v.lstrip("v") or "0.0.0"
    except OSError:
        return "0.0.0"


def read_agent_token() -> str:
    """Get the AGENT_TOKEN from src/backend/data/.env (empty if not present)."""
    try:
        for line in BACKEND_ENV.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith("AGENT_TOKEN="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def agent_files() -> list[Path]:
    """All files of the agent (without .env, __pycache__, logs)."""
    out = []
    for p in sorted(AGENT_DIR.rglob("*")):
        if not p.is_file():
            continue
        rel = str(p.relative_to(AGENT_DIR))
        if "__pycache__" in rel or p.name.startswith(".env") or p.suffix == ".log":
            continue
        out.append(p)
    return out


def copy_agent_into(dest: Path, backend_url: str, token: str) -> None:
    """Copy agent files + a prepared .env to dest."""
    dest.mkdir(parents=True, exist_ok=True)
    for p in agent_files():
        rel_target = p.relative_to(AGENT_DIR)
        # Source: src/agent/main.py - on the client the file is still named agent.py
        if str(rel_target) == "main.py":
            rel_target = Path("agent.py")
        target = dest / rel_target
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
    # Take the logo for the consent dialog along (as in the web installer).
    for cand in (BRANDING / "logo_r.png",):
        if cand.is_file():
            shutil.copy2(cand, dest / "logo.png")
            break
    (dest / ".env").write_text(
        f"BACKEND_URL={backend_url}\n"
        f"AGENT_TOKEN={token}\n"
        f"ENROLLMENT_TOKEN=\n"
        f"DEVICE_NAME=\n",
        encoding="utf-8",
    )


# ==========================================================================
# 1) Windows: Setup.exe (PyInstaller)
# ==========================================================================

INSTALLER_STUB = r'''
"""
BYTERA RMM - agent installer (Windows).
Packed into an .exe by src/backend/app/build_installers.py.

Usage:
    ByteraRmmAgent-Setup.exe                      -> interactive
    ByteraRmmAgent-Setup.exe /S                   -> silent (uses the baked-in
                                                      backend URL)
    ByteraRmmAgent-Setup.exe /S /URL=https://...  -> silent, own address
    ByteraRmmAgent-Setup.exe /TOKEN=abc123        -> onboarding token (assigns
                                                      the device directly to the
                                                      tenant/location chosen in
                                                      the dashboard)
    ByteraRmmAgent-Setup.exe /UNINSTALL           -> uninstall
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

INSTALL_DIR = Path(r"__INSTALL_DIR__")
TASK_NAME = "__TASK_NAME__"
BAKED_URL = "__BACKEND_URL__"
# Installation under the old name (RAPALLE.net RMM) - taken over and removed
LEGACY_DIR = Path(r"C:\Program Files\RapalleRmmAgent")
LEGACY_TASKS = ("RapalleRmmAgent", "RapalleNode", "RapalleRmmUpdate", "RapalleRmmUninstall")


def say(msg):
    print(msg)


def run(args, **kw):
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    return subprocess.run(args, **kw)


def powershell(script: str):
    return run(["powershell", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command", script])


def payload_dir() -> Path:
    """The embedded agent/ folder (PyInstaller extracts to _MEIPASS)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / "payload"


def is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def find_python() -> str | None:
    for exe in ("python", "py"):
        p = shutil.which(exe)
        if p:
            return p
    for c in (r"C:\Program Files\Python312\python.exe",
              r"C:\Program Files\Python311\python.exe"):
        if os.path.isfile(c):
            return c
    return None


def install_python() -> str | None:
    """Install Python silently - first winget, then the official installer."""
    say("Python not found - installing it automatically ...")
    if shutil.which("winget"):
        run(["winget", "install", "-e", "--id", "Python.Python.3.12", "--silent",
             "--accept-package-agreements", "--accept-source-agreements"], timeout=1800)
    if not find_python():
        url = "https://www.python.org/ftp/python/3.12.4/python-3.12.4-amd64.exe"
        dst = Path(os.environ["TEMP"]) / "python-setup.exe"
        powershell(f'Invoke-WebRequest -Uri "{url}" -OutFile "{dst}"')
        if dst.is_file():
            run([str(dst), "/quiet", "InstallAllUsers=1", "PrependPath=1"], timeout=1800)
    # Refresh the PATH of the running session
    machine = os.popen('powershell -NoProfile -Command '
                       '"[System.Environment]::GetEnvironmentVariable(\'Path\',\'Machine\')"').read().strip()
    if machine:
        os.environ["PATH"] = machine + ";" + os.environ.get("PATH", "")
    return find_python()


def stop_running_agent():
    say("Stopping any running agent ...")
    run(["schtasks", "/end", "/tn", TASK_NAME])
    for t in LEGACY_TASKS[:2]:                       # agent/node under the old name
        run(["schtasks", "/end", "/tn", t])
        run(["schtasks", "/delete", "/tn", t, "/f"])
    powershell(
        "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' OR Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -like '*agent.py*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    time.sleep(2)


def adopt_legacy():
    """Old installation (RAPALLE.net RMM): take over the assignment (.env) and device ID."""
    if not LEGACY_DIR.is_dir():
        return
    say(f"Old installation found - taking over the assignment from {LEGACY_DIR} ...")
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    for f in (".env", ".device-id"):
        if (LEGACY_DIR / f).is_file() and not (INSTALL_DIR / f).is_file():
            shutil.copy2(LEGACY_DIR / f, INSTALL_DIR / f)


def remove_legacy():
    for t in LEGACY_TASKS:
        run(["schtasks", "/delete", "/tn", t, "/f"])
    powershell("if ([System.Diagnostics.EventLog]::SourceExists('RapalleRMM')) { Remove-EventLog -Source 'RapalleRMM' }")
    shutil.rmtree(LEGACY_DIR, ignore_errors=True)


def register_task(pythonw: Path):
    """
    Autostart as a SERVICE: principal SYSTEM, trigger 'at system startup'.
    This way the agent runs even when NOBODY logs in (server). Screen capture
    starts a helper in the active user session when needed (agent.py
    --screen-helper, see session_bridge.py) - which only works from a SYSTEM
    process.
    """
    say("Setting up autostart as a service (SYSTEM, at system start) ...")
    ps = f"""
$Action = New-ScheduledTaskAction -Execute "{pythonw}" -Argument '"{INSTALL_DIR}\\agent.py"' -WorkingDirectory "{INSTALL_DIR}"
$TrigBoot  = New-ScheduledTaskTrigger -AtStartup
$TrigWatch = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
$Principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$Settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
try {{ Unregister-ScheduledTask -TaskName "{TASK_NAME}" -Confirm:$false -ErrorAction SilentlyContinue }} catch {{}}
Register-ScheduledTask -TaskName "{TASK_NAME}" -Action $Action -Trigger $TrigBoot,$TrigWatch -Principal $Principal -Settings $Settings -Force | Out-Null
Start-ScheduledTask -TaskName "{TASK_NAME}"
"""
    r = powershell(ps)
    if r.returncode != 0:
        say("ERROR while setting up the autostart:")
        say((r.stderr or r.stdout or "").strip())
        return False
    return True


def uninstall():
    say("Uninstalling the BYTERA RMM agent ...")
    stop_running_agent()
    run(["schtasks", "/delete", "/tn", TASK_NAME, "/f"])
    for t in ("ByteraRmmUpdate", "ByteraRmmUninstall"):
        run(["schtasks", "/delete", "/tn", t, "/f"])
    shutil.rmtree(INSTALL_DIR, ignore_errors=True)
    remove_legacy()
    say("Done - the agent has been removed.")
    return 0


def main() -> int:
    argv = [a for a in sys.argv[1:]]
    silent = any(a.upper() == "/S" for a in argv)
    if any(a.upper() in ("/UNINSTALL", "/U") for a in argv):
        return uninstall()

    if not is_admin():
        # PyInstaller is built with --uac-admin (Windows then asks by itself),
        # but in case the manifest is missing or was bypassed: restart here via
        # ShellExecute "runas" and pass all parameters along.
        say("Restarting with administrator rights (UAC prompt) ...")
        try:
            import ctypes
            params = " ".join(f'"{a}"' if " " in a else a for a in argv)
            rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable,
                                                     params, None, 1)
            if rc > 32:
                return 0
        except Exception:
            pass
        say("Elevation denied or failed - "
            "please run as administrator.")
        if not silent:
            input("Press [Enter] to continue ...")
        return 1

    url = BAKED_URL
    enroll = ""
    for a in argv:
        if a.upper().startswith("/URL="):
            url = a.split("=", 1)[1]
        elif a.upper().startswith("/TOKEN="):
            enroll = a.split("=", 1)[1]
    enroll = enroll or os.environ.get("RMM_ENROLLMENT_TOKEN", "")
    url = url or os.environ.get("RMM_BACKEND_URL", "")
    if not url and not silent:
        url = input("Address of the RMM server (e.g. https://rmm.company.com): ").strip()
    if not url:
        say("No backend address given (parameter /URL=... or RMM_BACKEND_URL).")
        return 1
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    url = url.rstrip("/")

    stop_running_agent()
    adopt_legacy()

    say(f"Copying the agent to {INSTALL_DIR} ...")
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    old_env = INSTALL_DIR / ".env"
    keep_env = old_env.read_text(encoding="utf-8", errors="replace") if old_env.is_file() else ""
    for item in payload_dir().iterdir():
        target = INSTALL_DIR / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)

    # Keep the existing .env (device assignment!), only update URL/token.
    base = keep_env or (INSTALL_DIR / ".env").read_text(encoding="utf-8", errors="replace")
    lines = []
    for ln in base.splitlines():
        if ln.startswith("BACKEND_URL="):
            ln = f"BACKEND_URL={url}"
        elif ln.startswith("ENROLLMENT_TOKEN=") and enroll:
            ln = f"ENROLLMENT_TOKEN={enroll}"
        lines.append(ln)
    (INSTALL_DIR / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")

    py = find_python() or install_python()
    if not py:
        say("Python could not be installed - please do it manually.")
        return 1

    say("Installing Python dependencies (may take 1-2 minutes) ...")
    venv = INSTALL_DIR / "venv"
    run([py, "-m", "venv", str(venv)], timeout=900)
    pip = venv / "Scripts" / "pip.exe"
    r = run([str(pip), "install", "-r", str(INSTALL_DIR / "requirements.txt"), "--quiet"],
            timeout=1800)
    if r.returncode != 0:
        say("ERROR during package installation:")
        say((r.stderr or r.stdout or "").strip()[:2000])
        return 1

    if not register_task(venv / "Scripts" / "pythonw.exe"):
        return 1
    remove_legacy()

    say("")
    say("=== DONE ===")
    say("The agent runs as a service under SYSTEM and starts at boot -")
    say("even when nobody logs in.")
    say(f"Log file in case of problems: {INSTALL_DIR}\\agent.log")
    if not silent:
        input("Press [Enter] to continue ...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def build_windows_exe(out: Path, backend_url: str, token: str) -> Path | None:
    if platform.system() != "Windows":
        info("A Windows EXE can only be built ON Windows -> skipped.")
        return None
    if not ensure_pyinstaller():
        return None

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        copy_agent_into(tmp / "payload", backend_url, token)
        stub = tmp / "installer_main.py"
        stub.write_text(
            INSTALLER_STUB
            .replace("__INSTALL_DIR__", WIN_INSTALL_DIR)
            .replace("__TASK_NAME__", TASK_NAME)
            .replace("__BACKEND_URL__", backend_url),
            encoding="utf-8",
        )
        info("Building ByteraRmmAgent-Setup.exe (PyInstaller) ...")
        r = subprocess.run(
            pyinstaller_cmd() + ["--onefile", "--console", "--uac-admin",
             "--name", "ByteraRmmAgent-Setup",
             "--distpath", str(out), "--workpath", str(tmp / "work"),
             "--specpath", str(tmp),
             "--add-data", f"{tmp / 'payload'};payload",
             str(stub)],
            text=True,
        )
        if r.returncode != 0:
            info("PyInstaller failed.")
            return None
    exe = out / "ByteraRmmAgent-Setup.exe"
    return exe if exe.is_file() else None


# --------------------------------------------------------------------------
# Templates for the Windows script packages (.ps1 / .bat)
# --------------------------------------------------------------------------
# Deliberately as .format() templates (no f-strings): PowerShell and Batch are
# full of curly braces - this way nothing has to be doubled.
# Placeholders: {backend_url} {install_dir} {task}

PS_SETUP_TEMPLATE = '''param(
    [string]$BackendUrl = "{backend_url}",
    [string]$Token = "",
    [switch]$Uninstall
)
$ErrorActionPreference = "Stop"
$InstallDir = "{install_dir}"
$TaskName   = "{task}"
$Payload    = $PSScriptRoot

$LegacyDir  = "C:\\Program Files\\RapalleRmmAgent"     # old installation (RAPALLE.net RMM)

function Remove-Legacy {{
    foreach ($t in @("RapalleRmmAgent", "RapalleNode", "RapalleRmmUpdate", "RapalleRmmUninstall")) {{
        schtasks /end /tn $t 2>$null | Out-Null
        schtasks /delete /tn $t /f 2>$null | Out-Null
    }}
    try {{ if ([System.Diagnostics.EventLog]::SourceExists("RapalleRMM")) {{ Remove-EventLog -Source "RapalleRMM" }} }} catch {{}}
    Remove-Item -Recurse -Force $LegacyDir -ErrorAction SilentlyContinue
}}

function Stop-Agent {{
    try {{ schtasks /end /tn $TaskName 2>$null | Out-Null }} catch {{}}
    foreach ($t in @("RapalleRmmAgent", "RapalleNode")) {{ try {{ schtasks /end /tn $t 2>$null | Out-Null }} catch {{}} }}
    try {{
        Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
            Where-Object {{ $_.CommandLine -like "*agent.py*" }} |
            ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}
    }} catch {{}}
    Start-Sleep -Seconds 2
}}

if ($Uninstall) {{
    Stop-Agent
    schtasks /delete /tn $TaskName /f 2>$null | Out-Null
    foreach ($t in @("ByteraRmmUpdate", "ByteraRmmUninstall")) {{
        schtasks /delete /tn $t /f 2>$null | Out-Null
    }}
    Remove-Item -Recurse -Force $InstallDir -ErrorAction SilentlyContinue
    Remove-Legacy
    Write-Host "Agent removed."
    exit 0
}}

# --- Admin rights: restart OURSELVES if necessary -------------------------
# Without administrator rights one can neither write to "Program Files" nor
# create a SYSTEM task. Instead of aborting, we simply restart ourselves via
# UAC and pass all parameters along.
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {{
    Write-Host "Restarting with administrator rights (UAC prompt) ..." -ForegroundColor Yellow
    $relaunch = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"")
    if ($BackendUrl) {{ $relaunch += @("-BackendUrl", "`"$BackendUrl`"") }}
    if ($Token)      {{ $relaunch += @("-Token", "`"$Token`"") }}
    if ($Uninstall)  {{ $relaunch += "-Uninstall" }}
    try {{
        $p = Start-Process -FilePath "powershell.exe" -ArgumentList $relaunch -Verb RunAs -PassThru -Wait
        exit $p.ExitCode
    }} catch {{
        Write-Host "Elevation denied or failed." -ForegroundColor Red
        exit 1
    }}
}}

if (-not $BackendUrl) {{ $BackendUrl = $env:RMM_BACKEND_URL }}
if (-not $BackendUrl) {{ $BackendUrl = Read-Host "Address of the RMM server (e.g. https://rmm.company.com)" }}
if (-not $BackendUrl) {{ Write-Host "No backend address given." -ForegroundColor Red; exit 1 }}
if ($BackendUrl -notmatch "^https?://") {{ $BackendUrl = "https://$BackendUrl" }}
$BackendUrl = $BackendUrl.TrimEnd("/")
if (-not $Token) {{ $Token = $env:RMM_ENROLLMENT_TOKEN }}

# --- 1. Python sicherstellen ---------------------------------------------
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) {{
    Write-Host "Python not found - installing it automatically ..." -ForegroundColor Yellow
    if (Get-Command winget -ErrorAction SilentlyContinue) {{
        winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
    }} else {{
        $pyUrl = "https://www.python.org/ftp/python/3.12.4/python-3.12.4-amd64.exe"
        Invoke-WebRequest -Uri $pyUrl -OutFile "$env:TEMP\\python-setup.exe"
        Start-Process -Wait -FilePath "$env:TEMP\\python-setup.exe" -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1"
    }}
    $env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
}}
if (-not $python) {{ Write-Host "Python is still missing - please install it manually." -ForegroundColor Red; exit 1 }}

# --- 2. Copy files (keep an existing .env!) -------------------------------
Stop-Agent
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$keepEnv = ""
if (Test-Path "$InstallDir\\.env") {{ $keepEnv = Get-Content "$InstallDir\\.env" -Raw }}
elseif (Test-Path "$LegacyDir\\.env") {{ $keepEnv = Get-Content "$LegacyDir\\.env" -Raw }}    # take over the old installation
if ((Test-Path "$LegacyDir\\.device-id") -and -not (Test-Path "$InstallDir\\.device-id")) {{
    Copy-Item "$LegacyDir\\.device-id" "$InstallDir\\.device-id" -Force
}}
Get-ChildItem $Payload -Exclude "_setup.ps1" | Copy-Item -Destination $InstallDir -Recurse -Force
if ($keepEnv) {{ Set-Content "$InstallDir\\.env" $keepEnv -NoNewline }}

$envLines = @()
foreach ($line in (Get-Content "$InstallDir\\.env")) {{
    if ($line -like "BACKEND_URL=*") {{ $line = "BACKEND_URL=$BackendUrl" }}
    elseif ($Token -and $line -like "ENROLLMENT_TOKEN=*") {{ $line = "ENROLLMENT_TOKEN=$Token" }}
    $envLines += $line
}}
Set-Content "$InstallDir\\.env" ($envLines -join "`n")

# --- 3. Dependencies ------------------------------------------------------
Write-Host "Installing Python dependencies (may take 1-2 minutes) ..."
& $python -m venv "$InstallDir\\venv"
& "$InstallDir\\venv\\Scripts\\python.exe" -m pip install --upgrade pip --quiet
& "$InstallDir\\venv\\Scripts\\pip.exe" install -r "$InstallDir\\requirements.txt" --quiet
if ($LASTEXITCODE -ne 0) {{ Write-Host "ERROR during package installation." -ForegroundColor Red; exit 1 }}

# --- 4. Autostart as a service (SYSTEM, at system startup) ----------------
# This way the agent runs even without a login (server). Screen capture starts
# a helper in the active user session when needed.
Write-Host "Setting up autostart as a service (SYSTEM, at system start) ..."
$Action    = New-ScheduledTaskAction -Execute "$InstallDir\\venv\\Scripts\\pythonw.exe" -Argument "`"$InstallDir\\agent.py`"" -WorkingDirectory $InstallDir
$TrigBoot  = New-ScheduledTaskTrigger -AtStartup
$TrigWatch = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
$Principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$Settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
try {{ Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue }} catch {{}}
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $TrigBoot,$TrigWatch -Principal $Principal -Settings $Settings -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Remove-Legacy

Write-Host ""
Write-Host "=== DONE ===" -ForegroundColor Green
Write-Host "The agent runs as a service under SYSTEM and starts at boot."
Write-Host "Log file for troubleshooting: $InstallDir\\agent.log"
'''


PS_WRAPPER_TEMPLATE = '''<#
BYTERA RMM - agent installation (Windows, standalone)
The complete agent is embedded as a base64 ZIP at the end of this file - so
neither the internet nor access to the backend is needed.

Usage (PowerShell as administrator):
    .\\install-agent.ps1
    .\\install-agent.ps1 -BackendUrl https://rmm.company.com -Token <onboarding-token>
    .\\install-agent.ps1 -Uninstall

If execution is blocked:
    powershell -NoProfile -ExecutionPolicy Bypass -File .\\install-agent.ps1
#>
param(
    [string]$BackendUrl = "{backend_url}",
    [string]$Token = "",
    [switch]$Uninstall
)
$ErrorActionPreference = "Stop"

# --- Admin rights: restart OURSELVES via UAC if necessary -----------------
# This way a right-click "Run with PowerShell" or a normal call is enough - the
# rights prompt comes automatically.
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {{
    Write-Host "Restarting with administrator rights (UAC prompt) ..." -ForegroundColor Yellow
    $relaunch = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"")
    if ($BackendUrl) {{ $relaunch += @("-BackendUrl", "`"$BackendUrl`"") }}
    if ($Token)      {{ $relaunch += @("-Token", "`"$Token`"") }}
    if ($Uninstall)  {{ $relaunch += "-Uninstall" }}
    try {{
        $p = Start-Process -FilePath "powershell.exe" -ArgumentList $relaunch -Verb RunAs -PassThru -Wait
        exit $p.ExitCode
    }} catch {{
        Write-Host "Elevation denied or failed." -ForegroundColor Red
        exit 1
    }}
}}

$work = Join-Path $env:TEMP ("rmm-agent-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $work | Out-Null
try {{
    # --- Extract the embedded ZIP and unpack it ---------------------------
    $self = Get-Content -LiteralPath $PSCommandPath -Raw
    $mark = "#__PAYLOAD__#"
    $b64  = $self.Substring($self.LastIndexOf($mark) + $mark.Length)
    $zip  = Join-Path $work "payload.zip"
    [IO.File]::WriteAllBytes($zip, [Convert]::FromBase64String(($b64 -replace "\\s", "")))
    Expand-Archive -LiteralPath $zip -DestinationPath (Join-Path $work "agent") -Force

    # --- Run setup --------------------------------------------------------
    $setup = Join-Path $work "agent\\_setup.ps1"
    $argv = @("-BackendUrl", $BackendUrl)
    if ($Token)     {{ $argv += @("-Token", $Token) }}
    if ($Uninstall) {{ $argv += "-Uninstall" }}
    & $setup @argv
    exit $LASTEXITCODE
}} finally {{
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}}
#__PAYLOAD__#
'''


BAT_TEMPLATE = '''@echo off
REM ========================================================================
REM BYTERA RMM - agent installation (Windows, standalone)
REM The complete agent is embedded as a base64 ZIP at the end of this file.
REM
REM Usage (command prompt as administrator):
REM     install-agent.bat
REM     install-agent.bat https://rmm.company.com <onboarding-token>
REM     install-agent.bat /uninstall
REM ========================================================================
setlocal EnableDelayedExpansion

REM --- Admin rights: restart OURSELVES via UAC if necessary --------------
REM This way a double-click is enough - Windows then asks for the rights.
net session >nul 2>&1
if errorlevel 1 (
    echo Restarting with administrator rights ^(UAC prompt^) ...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs"
    exit /b
)

set "UNINST="
set "BACKEND=%~1"
set "TOKEN=%~2"
if /i "%~1"=="/uninstall" set "UNINST=1" & set "BACKEND="
if "%BACKEND%"=="" set "BACKEND={backend_url}"

set "WORK=%TEMP%\\rmm-agent-%RANDOM%%RANDOM%"
mkdir "%WORK%" >nul 2>&1

REM --- Find the line number of the payload marker, cut out everything after -
for /f "delims=:" %%a in ('findstr /n /b /c:":PAYLOAD_BELOW" "%~f0"') do set "LN=%%a"
more +%LN% "%~f0" > "%WORK%\\payload.b64"
certutil -f -decode "%WORK%\\payload.b64" "%WORK%\\payload.zip" >nul
if errorlevel 1 (
    echo ERROR while unpacking the embedded package.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath '%WORK%\\payload.zip' -DestinationPath '%WORK%\\agent' -Force"

if "%UNINST%"=="1" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%WORK%\\agent\\_setup.ps1" -Uninstall
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%WORK%\\agent\\_setup.ps1" -BackendUrl "%BACKEND%" -Token "%TOKEN%"
)
set "RC=%ERRORLEVEL%"

rd /s /q "%WORK%" >nul 2>&1
if "%RC%"=="0" (echo. & echo Installation complete.) else (echo. & echo Installation failed [code %RC%].)
if not "%UNINST%"=="1" pause
exit /b %RC%

:PAYLOAD_BELOW
'''


# ==========================================================================
# 1b) Windows: setup logic as PowerShell (for .ps1 and .bat)
# ==========================================================================

def windows_setup_ps1(backend_url: str) -> str:
    """
    The actual installer as a PowerShell script. It lives IN the package
    (_setup.ps1) and is called by install-agent.ps1 / install-agent.bat after
    unpacking. Same steps as Setup.exe and the web installer: check/install
    Python, venv + dependencies, autostart task under SYSTEM with the trigger
    "at system startup".
    """
    return PS_SETUP_TEMPLATE.format(backend_url=backend_url,
                                    install_dir=WIN_INSTALL_DIR,
                                    task=TASK_NAME)


def _windows_payload_b64(backend_url: str, token: str) -> str:
    """Agent + _setup.ps1 as a ZIP, base64-encoded (for .ps1/.bat)."""
    import zipfile as _zip
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "agent"
        copy_agent_into(src, backend_url, token)
        (src / "_setup.ps1").write_text(windows_setup_ps1(backend_url), encoding="utf-8")
        zpath = tmp / "payload.zip"
        with _zip.ZipFile(zpath, "w", _zip.ZIP_DEFLATED) as zf:
            for f in sorted(src.rglob("*")):
                if f.is_file():
                    zf.write(f, arcname=str(f.relative_to(src)))
        return base64.b64encode(zpath.read_bytes()).decode("ascii")


def _wrap_b64(b64: str, width: int = 76) -> str:
    return "\n".join(b64[i:i + width] for i in range(0, len(b64), width))


def build_windows_ps1(out: Path, backend_url: str, token: str) -> Path:
    """
    Standalone PowerShell installation script with an embedded agent.
    Needs neither the internet nor access to the backend.
    """
    b64 = _wrap_b64(_windows_payload_b64(backend_url, token))
    script = PS_WRAPPER_TEMPLATE.format(backend_url=backend_url) + b64 + "\n"
    dest = out / "install-agent.ps1"
    info(f"Building {dest.name} ...")
    dest.write_text(script, encoding="utf-8")
    return dest


def build_windows_bat(out: Path, backend_url: str, token: str) -> Path:
    """
    Standalone .bat - for environments where .ps1 is blocked or where
    installation should be done by double-click. The base64 ZIP is behind the
    marker :PAYLOAD_BELOW; "more +N" cuts it out, "certutil -decode" turns it
    back into a ZIP. Then _setup.ps1 takes over.
    """
    b64 = _wrap_b64(_windows_payload_b64(backend_url, token), 76)
    head = BAT_TEMPLATE.format(backend_url=backend_url)
    dest = out / "install-agent.bat"
    info(f"Building {dest.name} ...")
    # CRLF: cmd.exe processes pure-LF scripts unreliably.
    data = (head.replace("\n", "\r\n") + b64.replace("\n", "\r\n") + "\r\n")
    dest.write_bytes(data.encode("utf-8"))
    return dest



# ==========================================================================
# 2) Windows: MSI (only with WiX v3 - candle/light)
# ==========================================================================

WXS = r'''<?xml version="1.0" encoding="utf-8"?>
<Wix xmlns="http://schemas.microsoft.com/wix/2006/wi">
  <Product Id="*" Name="BYTERA RMM Agent" Language="1033"
           Version="__VERSION__" Manufacturer="BYTERA"
           UpgradeCode="7B2F1C64-9A3D-4F51-8E2A-0C7A1D4E9B31">
    <Package InstallerVersion="300" Compressed="yes" InstallScope="perMachine" />
    <MajorUpgrade DowngradeErrorMessage="A newer version is already installed." />
    <MediaTemplate EmbedCab="yes" />

    <Directory Id="TARGETDIR" Name="SourceDir">
      <Directory Id="ProgramFiles64Folder">
        <Directory Id="INSTALLFOLDER" Name="ByteraRmmAgent" />
      </Directory>
    </Directory>

    <ComponentGroup Id="Payload" Directory="INSTALLFOLDER">
      <Component Id="SetupExe" Guid="*">
        <File Id="SetupExeFile" Source="__SETUP_EXE__" KeyPath="yes" />
      </Component>
    </ComponentGroup>

    <Feature Id="Main" Title="RMM Agent" Level="1">
      <ComponentGroupRef Id="Payload" />
    </Feature>

    <!-- After copying: run setup silently (sets up venv + SYSTEM task). -->
    <CustomAction Id="RunSetup" FileKey="SetupExeFile" ExeCommand="/S"
                  Execute="deferred" Impersonate="no" Return="check" />
    <!-- On uninstall, remove the agent cleanly. -->
    <CustomAction Id="RunUninstall" FileKey="SetupExeFile" ExeCommand="/UNINSTALL"
                  Execute="deferred" Impersonate="no" Return="ignore" />

    <InstallExecuteSequence>
      <Custom Action="RunSetup" After="InstallFiles">NOT Installed</Custom>
      <Custom Action="RunUninstall" Before="RemoveFiles">REMOVE="ALL"</Custom>
    </InstallExecuteSequence>
  </Product>
</Wix>
'''


def build_windows_msi(out: Path, setup_exe: Path | None) -> Path | None:
    if setup_exe is None or not setup_exe.is_file():
        info("MSI skipped (Setup.exe missing).")
        return None
    wix = ensure_wix()
    if not wix:
        return None
    candle, light = wix
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        wxs = tmp / "agent.wxs"
        wxs.write_text(
            WXS.replace("__VERSION__", agent_version())
               .replace("__SETUP_EXE__", str(setup_exe)),
            encoding="utf-8",
        )
        info("Building ByteraRmmAgent.msi (WiX) ...")
        if subprocess.run([candle, "-nologo", "-out", str(tmp / "agent.wixobj"), str(wxs)]).returncode:
            return None
        msi = out / "ByteraRmmAgent.msi"
        if subprocess.run([light, "-nologo", "-out", str(msi), str(tmp / "agent.wixobj")]).returncode:
            return None
        return msi if msi.is_file() else None


# ==========================================================================
# 3) Linux: shared installation logic (postinst / .run)
# ==========================================================================

def linux_setup_script(backend_url: str) -> str:
    """
    Run after unpacking (deb postinst, rpm post, .run).
    Sets up venv + dependencies + systemd service.

    The service runs as root under multi-user.target - so it starts at boot,
    without anyone having to log in.
    """
    return f'''#!/bin/bash
set -e
INSTALL_DIR="{LINUX_INSTALL_DIR}"
DEFAULT_URL="{backend_url}"

# Backend address: package default, environment variable or prompt.
URL="${{RMM_BACKEND_URL:-$DEFAULT_URL}}"
if [ -z "$URL" ] && [ -t 0 ]; then
  read -r -p "Address of the RMM server (e.g. https://rmm.company.com): " URL
fi
if [ -z "$URL" ]; then
  echo "No backend address set. Add it later in $INSTALL_DIR/.env"
  echo "and then run: systemctl restart bytera-agent"
else
  sed -i "s|^BACKEND_URL=.*|BACKEND_URL=${{URL%/}}|" "$INSTALL_DIR/.env" 2>/dev/null || true
fi

# Onboarding token (assigns the device directly to the chosen tenant/location).
if [ -n "${{RMM_ENROLLMENT_TOKEN:-}}" ]; then
  sed -i "s|^ENROLLMENT_TOKEN=.*|ENROLLMENT_TOKEN=$RMM_ENROLLMENT_TOKEN|" "$INSTALL_DIR/.env" 2>/dev/null || true
fi

# --- System prerequisites (python3, venv, pip) ---------------------------
PYV="$(python3 -c 'import sys;print(f"{{sys.version_info.major}}.{{sys.version_info.minor}}")' 2>/dev/null || echo 3)"
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y -qq >/dev/null 2>&1 || true
  apt-get install -y -qq "python${{PYV}}-venv" >/dev/null 2>&1 \\
    || apt-get install -y -qq python3-venv >/dev/null 2>&1 || true
  apt-get install -y -qq python3-pip >/dev/null 2>&1 || true
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y -q python3 python3-pip >/dev/null 2>&1 || true
elif command -v zypper >/dev/null 2>&1; then
  zypper --non-interactive install -y python3 python3-pip >/dev/null 2>&1 || true
elif command -v pacman >/dev/null 2>&1; then
  pacman -Sy --noconfirm python python-pip >/dev/null 2>&1 || true
fi

# --- Rename: take over the old installation (RAPALLE.net RMM) -------------
LEGACY_DIR="/opt/rapalle-rmm-agent"
if [ -d "$LEGACY_DIR" ] || [ -f /etc/systemd/system/rapalle-agent.service ]; then
  echo "Legacy install (RAPALLE.net RMM) found - taking over its identity and removing it ..."
  systemctl disable --now rapalle-agent 2>/dev/null || true
  systemctl disable --now rapalle-node 2>/dev/null || true
  pkill -f "$LEGACY_DIR/" 2>/dev/null || true
  if [ -f "$LEGACY_DIR/.device-id" ] && [ ! -f "$INSTALL_DIR/.device-id" ]; then
    cp "$LEGACY_DIR/.device-id" "$INSTALL_DIR/.device-id"
  fi
  if [ -f "$LEGACY_DIR/.env" ]; then
    # keep the self-assigned device name
    v="$(grep -m1 "^DEVICE_NAME=" "$LEGACY_DIR/.env" | cut -d= -f2- || true)"
    if [ -n "$v" ]; then sed -i "s|^DEVICE_NAME=.*|DEVICE_NAME=$v|" "$INSTALL_DIR/.env" 2>/dev/null || true; fi
  fi
  rm -f /etc/systemd/system/rapalle-agent.service /etc/systemd/system/rapalle-node.service
  systemctl daemon-reload 2>/dev/null || true
  rm -rf "$LEGACY_DIR"
fi

# --- Dependencies in a venv (fallback: system-wide) -----------------------
cd "$INSTALL_DIR"
if python3 -m venv venv >/dev/null 2>&1; then
  ./venv/bin/pip install --quiet --upgrade pip || true
  ./venv/bin/pip install --quiet -r requirements.txt
  PYEXEC="$INSTALL_DIR/venv/bin/python"
else
  python3 -m pip install --quiet --break-system-packages -r requirements.txt \\
    || python3 -m pip install --quiet -r requirements.txt
  PYEXEC="$(command -v python3)"
fi

# --- systemd service ------------------------------------------------------
cat > /etc/systemd/system/bytera-agent.service <<UNIT
[Unit]
Description=BYTERA RMM Agent
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
WorkingDirectory=$INSTALL_DIR
ExecStart=$PYEXEC $INSTALL_DIR/agent.py
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now bytera-agent
echo "Done - status: systemctl status bytera-agent"
'''


LINUX_REMOVE = f'''#!/bin/bash
systemctl stop bytera-agent 2>/dev/null || true
systemctl disable bytera-agent 2>/dev/null || true
rm -f /etc/systemd/system/bytera-agent.service
systemctl daemon-reload 2>/dev/null || true
rm -rf {LINUX_INSTALL_DIR}/venv
exit 0
'''


# ==========================================================================
# 4) Linux: .deb
# ==========================================================================

def build_deb(out: Path, backend_url: str, token: str) -> Path | None:
    if not have("dpkg-deb"):
        info("dpkg-deb missing -> .deb skipped.")
        return None
    version = agent_version()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "pkg"
        copy_agent_into(root / LINUX_INSTALL_DIR.lstrip("/"), backend_url, token)
        debian = root / "DEBIAN"
        debian.mkdir(parents=True)
        (debian / "control").write_text(
            f"Package: {PKG_NAME}\n"
            f"Version: {version}\n"
            "Section: admin\n"
            "Priority: optional\n"
            "Architecture: all\n"
            "Depends: python3 (>= 3.9), python3-venv | python3-virtualenv, systemd\n"
            # Rename: replaces the package under the old name (RAPALLE.net RMM)
            "Replaces: rapalle-rmm-agent\n"
            "Conflicts: rapalle-rmm-agent\n"
            "Provides: rapalle-rmm-agent\n"
            "Maintainer: BYTERA <noreply@rapalle.net>\n"
            "Description: BYTERA RMM Agent\n"
            " Connects this device to the BYTERA RMM dashboard.\n"
            " Runs as a systemd service (root) and starts at boot.\n",
            encoding="utf-8",
        )
        (debian / "postinst").write_text(linux_setup_script(backend_url), encoding="utf-8")
        (debian / "prerm").write_text(LINUX_REMOVE, encoding="utf-8")
        for f in ("postinst", "prerm"):
            os.chmod(debian / f, 0o755)
        # The .env must not be overwritten on update.
        (debian / "conffiles").write_text(f"{LINUX_INSTALL_DIR}/.env\n", encoding="utf-8")

        deb = out / f"{PKG_NAME}_{version}_all.deb"
        info(f"Building {deb.name} ...")
        if subprocess.run(["dpkg-deb", "--build", "--root-owner-group",
                           str(root), str(deb)]).returncode:
            return None
        return deb if deb.is_file() else None


# ==========================================================================
# 5) Linux: .rpm
# ==========================================================================

def build_rpm(out: Path, backend_url: str, token: str) -> Path | None:
    if not have("rpmbuild"):
        info("rpmbuild missing -> .rpm skipped (package: rpm / rpm-build).")
        return None
    version = agent_version()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        top = tmp / "rpmbuild"
        for d in ("SPECS", "SOURCES", "BUILD", "RPMS", "SRPMS", "BUILDROOT"):
            (top / d).mkdir(parents=True)

        src = tmp / f"{PKG_NAME}-{version}"
        copy_agent_into(src, backend_url, token)
        tar = top / "SOURCES" / f"{PKG_NAME}-{version}.tar.gz"
        with tarfile.open(tar, "w:gz") as tf:
            tf.add(src, arcname=f"{PKG_NAME}-{version}")

        post = linux_setup_script(backend_url).replace("#!/bin/bash\nset -e\n", "")
        spec = f"""Name:           {PKG_NAME}
Version:        {version}
Release:        1
Summary:        BYTERA RMM Agent
License:        MIT
BuildArch:      noarch
Source0:        {PKG_NAME}-{version}.tar.gz
Requires:       python3, systemd
Obsoletes:      rapalle-rmm-agent
Provides:       rapalle-rmm-agent

%description
Connects this device to the BYTERA RMM dashboard.
Runs as a systemd service (root) and starts at boot.

%prep
%setup -q

%install
mkdir -p %{{buildroot}}{LINUX_INSTALL_DIR}
cp -a . %{{buildroot}}{LINUX_INSTALL_DIR}/

%files
%config(noreplace) {LINUX_INSTALL_DIR}/.env
{LINUX_INSTALL_DIR}

%post
{post}

%preun
if [ $1 -eq 0 ]; then
{LINUX_REMOVE.replace("#!/bin/bash", "").replace("exit 0", "")}
fi
"""
        specfile = top / "SPECS" / f"{PKG_NAME}.spec"
        specfile.write_text(spec, encoding="utf-8")
        info(f"Building {PKG_NAME}-{version}.rpm ...")
        r = subprocess.run(["rpmbuild", "-bb", "--define", f"_topdir {top}", str(specfile)])
        if r.returncode:
            return None
        for f in (top / "RPMS").rglob("*.rpm"):
            dest = out / f.name
            shutil.copy2(f, dest)
            return dest
    return None


# ==========================================================================
# 6) Everywhere: self-extracting .run script
# ==========================================================================

def build_run(out: Path, backend_url: str, token: str) -> Path:
    """
    Self-extracting installer: shell header + base64-encoded tar.gz.
    Can be built on ANY system (also Windows) and runs on any Linux with
    bash + tar - entirely without a package manager.
    """
    version = agent_version()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "agent"
        copy_agent_into(src, backend_url, token)
        tgz = tmp / "payload.tar.gz"
        with tarfile.open(tgz, "w:gz") as tf:
            tf.add(src, arcname=".")
        b64 = base64.b64encode(tgz.read_bytes()).decode("ascii")

    setup = linux_setup_script(backend_url).replace("#!/bin/bash\nset -e\n", "")
    header = f'''#!/bin/bash
# BYTERA RMM Agent {version} - self-extracting installer
# Usage:    sudo ./install-agent.sh          (asks for the address if needed)
#           sudo RMM_BACKEND_URL=https://rmm.company.com ./install-agent.sh
#           sudo RMM_ENROLLMENT_TOKEN=<token> ./install-agent.sh
#           sudo ./install-agent.sh --uninstall
set -e
INSTALL_DIR="{LINUX_INSTALL_DIR}"

# --- Root rights: restart OURSELVES via sudo if necessary -----------------
# "-E" keeps environment variables like RMM_BACKEND_URL / RMM_ENROLLMENT_TOKEN
# alive, otherwise they would be gone after the switch to sudo.
if [ "$(id -u)" != "0" ]; then
  if command -v sudo >/dev/null 2>&1; then
    echo "Restarting with root privileges (sudo) ..."
    exec sudo -E "$0" "$@"
  fi
  echo "Please run as root (sudo is not installed)."; exit 1
fi

if [ "$1" = "--uninstall" ]; then
{LINUX_REMOVE.replace("#!/bin/bash", "").rstrip()}
  rm -rf "$INSTALL_DIR"
  echo "Agent removed."
  exit 0
fi

echo "Unpacking agent to $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"
# Save the existing .env (device assignment!).
KEEP=""
[ -f "$INSTALL_DIR/.env" ] && KEEP="$(cat "$INSTALL_DIR/.env")"
ARCHIVE_LINE=$(awk '/^__PAYLOAD_BELOW__/ {{print NR + 1; exit 0}}' "$0")
tail -n +"$ARCHIVE_LINE" "$0" | base64 -d | tar xzf - -C "$INSTALL_DIR"
[ -n "$KEEP" ] && printf '%s\\n' "$KEEP" > "$INSTALL_DIR/.env"

{setup}
exit 0
__PAYLOAD_BELOW__
'''
    dest = out / "install-agent.sh"
    info(f"Building {dest.name} ...")
    dest.write_text(header + b64 + "\n", encoding="utf-8", newline="\n")
    os.chmod(dest, 0o755)
    # Also store it under the classic .run name (identical content) - some
    # distribution tools expect this extension.
    run_copy = out / f"{PKG_NAME}.run"
    shutil.copy2(dest, run_copy)
    os.chmod(run_copy, 0o755)
    return dest


# ==========================================================================
# 6b) Linux: plain tarball package (.tar.gz)
# ==========================================================================

def build_tgz(out: Path, backend_url: str, token: str) -> Path:
    """
    Classic archive: agent files + install.sh + uninstall.sh.
    For anyone who would rather see for themselves what gets installed - or
    roll the package out via Ansible/Salt:

        tar xzf bytera-rmm-agent-<v>.tar.gz
        cd bytera-rmm-agent-<v> && sudo ./install.sh
    """
    version = agent_version()
    name = f"{PKG_NAME}-{version}"
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / name
        copy_agent_into(src, backend_url, token)

        install = src / "install.sh"
        install.write_text(
            "#!/bin/bash\n"
            "# Installs the RMM agent from THIS folder to "
            f"{LINUX_INSTALL_DIR}\n"
            "# Usage:   sudo ./install.sh   or\n"
            "#          sudo RMM_BACKEND_URL=https://rmm.company.com "
            "RMM_ENROLLMENT_TOKEN=<token> ./install.sh\n"
            "set -e\n"
            # Root rights: restart via sudo if necessary (-E keeps RMM_* variables).
            'if [ "$(id -u)" != "0" ]; then\n'
            '  if command -v sudo >/dev/null 2>&1; then exec sudo -E "$0" "$@"; fi\n'
            '  echo "Please run as root (sudo is missing)."; exit 1\n'
            'fi\n'
            'HERE="$(cd "$(dirname "$0")" && pwd)"\n'
            f'INSTALL_DIR="{LINUX_INSTALL_DIR}"\n'
            'mkdir -p "$INSTALL_DIR"\n'
            '# Keep the existing .env (device assignment!).\n'
            'KEEP=""; [ -f "$INSTALL_DIR/.env" ] && KEEP="$(cat "$INSTALL_DIR/.env")"\n'
            'cp -a "$HERE/." "$INSTALL_DIR/"\n'
            'rm -f "$INSTALL_DIR/install.sh" "$INSTALL_DIR/uninstall.sh"\n'
            '[ -n "$KEEP" ] && printf \'%s\\n\' "$KEEP" > "$INSTALL_DIR/.env"\n'
            + linux_setup_script(backend_url).replace("#!/bin/bash\nset -e\n", ""),
            encoding="utf-8",
        )
        uninstall = src / "uninstall.sh"
        uninstall.write_text(
            LINUX_REMOVE.replace(
                "#!/bin/bash\n",
                '#!/bin/bash\n'
                '# Root rights: restart via sudo if necessary.\n'
                'if [ "$(id -u)" != "0" ]; then\n'
                '  if command -v sudo >/dev/null 2>&1; then exec sudo -E "$0" "$@"; fi\n'
                '  echo "Please run as root (sudo is missing)."; exit 1\n'
                'fi\n',
            ).replace("exit 0", f'rm -rf {LINUX_INSTALL_DIR}\necho "Agent removed."\nexit 0'),
            encoding="utf-8",
        )
        os.chmod(install, 0o755)
        os.chmod(uninstall, 0o755)

        dest = out / f"{name}.tar.gz"
        info(f"Building {dest.name} ...")
        with tarfile.open(dest, "w:gz") as tf:
            tf.add(src, arcname=name)
    return dest


# ==========================================================================
# 6c) Arch Linux: .pkg.tar.xz
# ==========================================================================

def build_pkg(out: Path, backend_url: str, token: str) -> Path:
    """
    Arch/Manjaro package. Built by hand (tar.xz with .PKGINFO + .INSTALL), so
    NO makepkg is needed - the package can therefore also be created on a
    Debian or Windows server.

        sudo pacman -U bytera-rmm-agent-<v>-any.pkg.tar.xz

    Note: without makepkg the (optional) .MTREE file is missing. pacman prints a
    warning about it and installs normally - only the file checksums are left
    out.
    """
    version = agent_version().replace("-", "_")   # pacman does not like "-" in the version
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        root = tmp / "pkgroot"
        copy_agent_into(root / LINUX_INSTALL_DIR.lstrip("/"), backend_url, token)

        size = sum(f.stat().st_size for f in root.rglob("*") if f.is_file())
        (root / ".PKGINFO").write_text(
            f"pkgname = {PKG_NAME}\n"
            f"pkgver = {version}-1\n"
            "pkgdesc = BYTERA RMM Agent\n"
            "url = https://github.com/juliusbrussee\n"
            "builddate = 0\n"
            "packager = BYTERA\n"
            f"size = {size}\n"
            "arch = any\n"
            "license = MIT\n"
            "replaces = rapalle-rmm-agent\n"          # rename (RAPALLE.net RMM)
            "conflict = rapalle-rmm-agent\n"
            "depend = python\n"
            "depend = systemd\n"
            f"backup = {LINUX_INSTALL_DIR.lstrip('/')}/.env\n",
            encoding="utf-8",
        )
        # pacman hooks: post_install/post_upgrade set up the service.
        setup = linux_setup_script(backend_url).replace("#!/bin/bash\nset -e\n", "")
        remove = LINUX_REMOVE.replace("#!/bin/bash\n", "").replace("exit 0", "")
        (root / ".INSTALL").write_text(
            "post_install() {\n" + setup + "\n}\n\n"
            "post_upgrade() {\n  post_install\n}\n\n"
            "pre_remove() {\n" + remove + "\n}\n",
            encoding="utf-8",
        )

        dest = out / f"{PKG_NAME}-{version}-1-any.pkg.tar.xz"
        info(f"Building {dest.name} ...")
        with tarfile.open(dest, "w:xz") as tf:
            # .PKGINFO must be the FIRST entry in the archive.
            tf.add(root / ".PKGINFO", arcname=".PKGINFO")
            tf.add(root / ".INSTALL", arcname=".INSTALL")
            for f in sorted(root.rglob("*")):
                if f.name in (".PKGINFO", ".INSTALL") and f.parent == root:
                    continue
                tf.add(f, arcname=str(f.relative_to(root)), recursive=False)
    return dest



# ==========================================================================
# CLI
# ==========================================================================

ALL_TARGETS = ["exe", "msi", "bat", "ps1", "sh", "tgz", "deb", "rpm", "pkg"]


def main() -> int:
    ap = argparse.ArgumentParser(description="Builds local agent installation packages.")
    ap.add_argument("--backend-url", default="",
                    help="Address of the RMM server baked into the package "
                         "(empty = the installer asks at run time)")
    ap.add_argument("--token", default="",
                    help="AGENT_TOKEN (default: from src/backend/data/.env)")
    ap.add_argument("--targets", default="auto",
                    help=f"Comma list of {','.join(ALL_TARGETS)} or 'auto' "
                         "(everything the system can build)")
    ap.add_argument("--out", default=str(DIST_DIR), help="Output folder")
    ap.add_argument("--no-auto-install", action="store_true",
                    help="Do NOT auto-install missing build tools (PyInstaller, WiX)")
    ap.add_argument("--list", action="store_true",
                    help="Only show which packages can be built here")
    args = ap.parse_args()

    global AUTO_INSTALL
    AUTO_INSTALL = not args.no_auto_install

    if not AGENT_DIR.is_dir():
        info(f"Agent folder not found: {AGENT_DIR}")
        return 1

    # exe/msi: tools are pulled in automatically when needed (PyInstaller via
    # pip, WiX as a download) - so "runs on Windows" is enough here.
    is_win = platform.system() == "Windows"
    can = {
        "exe": is_win,
        "msi": is_win,
        # Script and archive packages need NO external tool - they can be
        # built on any system, also for the respective other platform (e.g. the
        # Windows .bat on a Linux server).
        "bat": True,
        "ps1": True,
        "sh": True,
        "tgz": True,
        "pkg": True,
        "deb": have("dpkg-deb"),
        "rpm": have("rpmbuild"),
    }
    if args.list:
        print("Buildable on this system:")
        for t in ALL_TARGETS:
            print(f"  {t:4} {'yes' if can[t] else 'no'}")
        return 0

    targets = ([t for t in ALL_TARGETS if can[t]] if args.targets == "auto"
               else [t.strip() for t in args.targets.split(",") if t.strip()])
    unknown = [t for t in targets if t not in ALL_TARGETS]
    if unknown:
        info(f"Unknown targets: {', '.join(unknown)}")
        return 1

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    url = args.backend_url.rstrip("/")
    token = args.token or read_agent_token()
    if not token:
        info("WARNING: no AGENT_TOKEN found (src/backend/data/.env) - the agents "
             "cannot register with it. Provide it with --token.")

    info(f"Agent version {agent_version()} | targets: {', '.join(targets)} | output: {out}")
    built: list[Path] = []
    setup_exe = None
    if "exe" in targets:
        setup_exe = build_windows_exe(out, url, token)
        if setup_exe:
            built.append(setup_exe)
    if "msi" in targets:
        # The MSI wraps the Setup.exe - if it is missing, it is built first.
        if setup_exe is None:
            existing = out / "ByteraRmmAgent-Setup.exe"
            setup_exe = existing if existing.is_file() else build_windows_exe(out, url, token)
        msi = build_windows_msi(out, setup_exe)
        if msi:
            built.append(msi)
    if "bat" in targets:
        built.append(build_windows_bat(out, url, token))
    if "ps1" in targets:
        built.append(build_windows_ps1(out, url, token))
    if "tgz" in targets:
        built.append(build_tgz(out, url, token))
    if "pkg" in targets:
        built.append(build_pkg(out, url, token))
    if "deb" in targets:
        d = build_deb(out, url, token)
        if d:
            built.append(d)
    if "rpm" in targets:
        r = build_rpm(out, url, token)
        if r:
            built.append(r)
    if "sh" in targets:
        built.append(build_run(out, url, token))

    print()
    if not built:
        info("Nothing was built - see the notes above.")
        return 1
    info("Finished packages:")
    for b in built:
        info(f"  {b}  ({b.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
