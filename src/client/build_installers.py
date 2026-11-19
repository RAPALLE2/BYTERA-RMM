#!/usr/bin/env python3
"""
build_installers.py - builds ALL packages of the BYTERA RMM Client
===================================================================
(Counterpart to src/backend/app/build_installers.py for the agent.)

Output in src/client/release/  (the "client" module offers them under
Profile -> Client app & devices and as a one-liner for download):

  Windows  ByteraClient-<v>-windows.exe   PyInstaller onefile (no dependencies)
           ByteraClient-<v>-windows.msi   installer (Programs + Start menu/Desktop), deployable via GPO/Intune
           ByteraClient-<v>-windows.zip   the .exe to extract (without installation)
  Linux    bytera-client_<v>_all.deb      Debian/Ubuntu/Mint (Python 3 + Tk from the system)
           bytera-client-<v>-1.noarch.rpm Fedora/RHEL/openSUSE
           bytera-client-<v>-1-any.pkg.tar.xz   Arch/Manjaro
           bytera-client-<v>-linux.tar.gz / .tgz / .tar   for all distributions (install.sh included)
  Android  ByteraClient-<v>-android.apk   native app (Android 6+), permanently signed

Tools:
  Easiest: enable the "Client app" module in Docker mode - the module then downloads a
  build image (bytera/rmm-client-builder) with ALL tools and builds inside it (like the agent
  packages at the push of a button, but for all formats).
  By hand (Linux, e.g. Ubuntu 24.04):
  exe   wine64 + Windows Python with PyInstaller  (--winpython C:\\path\\python.exe under Wine)
        or directly on Windows: python build_installers.py --targets exe,zip,msi
        (the script fetches PyInstaller via pip and WiX as a download itself, like for the agent)
  msi   wixl (package "wixl"/msitools), on Windows WiX 3   deb/tar/tgz/pkg  Python only
  rpm   rpmbuild (package "rpm")
  apk   aapt, javac, dalvik-exchange (dx), zipalign, apksigner, android-sdk-platform-23

Usage:
  python3 src/client/build_installers.py                    # everything the tools allow
  python3 src/client/build_installers.py --targets deb,tar,apk
  python3 src/client/build_installers.py --list
  python3 src/client/build_installers.py --tools            # which formats work here? (JSON)
  python3 src/client/build_installers.py --out DIR --set-version 1.2.1   # server button "rebuild"
  python build_installers.py --prepare                      # Windows: fetch PyInstaller + WiX in advance
"""
from __future__ import annotations

import argparse
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DESKTOP = HERE / "desktop"
ANDROID = HERE / "android"
RES = HERE / "resources"
RELEASE = HERE / "release"          # default output (bundled packages); changeable with --out
ALL = ["exe", "msi", "zip", "deb", "rpm", "pkg", "tar", "tgz", "apk"]
# tool store for Windows (PyInstaller/WiX); the module points it to data/modules/client/buildtools
TOOLS_DIR = Path(os.environ.get("RMM_CLIENT_TOOLS") or (HERE / ".buildtools"))
WIX_URL = "https://github.com/wixtoolset/wix3/releases/download/wix3141rtm/wix314-binaries.zip"


def set_version(v: str) -> None:
    """Set __version__ in the desktop client (the Android app picks it up when building)."""
    import re as _re
    if not _re.fullmatch(r"\d+\.\d+\.\d+", v):
        raise SystemExit(f"Invalid version: {v} (format 1.2.3)")
    p = DESKTOP / "bytera_client" / "__init__.py"
    s = p.read_text(encoding="utf-8")
    s = _re.sub(r'__version__\s*=\s*"[^"]*"', f'__version__ = "{v}"', s)
    p.write_text(s, encoding="utf-8")


def tools() -> dict:
    """Which formats can be built on THIS system?"""
    if os.name == "nt":
        # Like for the agent: the script fetches PyInstaller (pip) and WiX (download) itself.
        return {"exe": True, "zip": True, "msi": True, "deb": True, "rpm": bool(shutil.which("rpmbuild")),
                "pkg": True, "tar": True, "tgz": True, "apk": _apk_ok()}
    winpy = os.environ.get("RMM_WINPYTHON") or "/opt/winpy/python/python.exe"
    exe_ok = bool(_wine() and Path(winpy).exists())
    return {"exe": exe_ok, "zip": exe_ok, "msi": exe_ok and bool(shutil.which("wixl")),
            "deb": True, "rpm": bool(shutil.which("rpmbuild")), "pkg": True, "tar": True, "tgz": True,
            "apk": _apk_ok()}


def _wine() -> str | None:
    return shutil.which("wine64") or shutil.which("wine") or \
        ("/usr/lib/wine/wine64" if Path("/usr/lib/wine/wine64").exists() else None)


def _apk_ok() -> bool:
    apk_tools = all(shutil.which(t) for t in ("aapt", "javac", "dalvik-exchange", "zipalign", "apksigner"))
    return apk_tools and Path("/usr/lib/android-sdk/platforms/android-23/android.jar").exists() \
        and (ANDROID / "signing" / "release.jks").exists()


# ------------------------------------------------------------ Windows tools (like for the agent)
def _ensure_crypto(py: str) -> None:
    """'cryptography' for the end-to-end encryption (bundled into the .exe)."""
    subprocess.run([py, "-m", "pip", "install", "--quiet", "cryptography>=41"], capture_output=True, text=True)


def ensure_pyinstaller() -> None:
    """PyInstaller present? Otherwise install via pip (Windows only)."""
    try:
        import PyInstaller  # noqa: F401
        return
    except ImportError:
        pass
    print("  PyInstaller missing - installing via pip ...")
    for extra in ([], ["--user"]):
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", *extra, "pyinstaller>=6.0,<7",
                            "cryptography>=41"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return
    raise RuntimeError("PyInstaller could not be installed via pip:\n" + (r.stdout + r.stderr)[-1500:])


def ensure_wix() -> tuple[Path, Path]:
    """WiX 3 (candle/light) in the tool folder - download if needed (Windows only)."""
    for d in [TOOLS_DIR / "wix314"] + [Path(p) for p in (os.environ.get("WIX", ""),) if p]:
        for base in (d, d / "bin"):
            if (base / "candle.exe").exists() and (base / "light.exe").exists():
                return base / "candle.exe", base / "light.exe"
    wix = TOOLS_DIR / "wix314"
    print(f"  Downloading WiX 3.14 to {wix} ...")
    wix.mkdir(parents=True, exist_ok=True)
    archive = wix / "wix-binaries.zip"
    urllib.request.urlretrieve(WIX_URL, archive)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(wix)
    archive.unlink()
    return wix / "candle.exe", wix / "light.exe"


def prepare() -> None:
    """Fetch tools in advance (the module calls this on enable)."""
    if os.name == "nt":
        ensure_pyinstaller()
        print("PyInstaller ready")
        print(f"WiX ready: {ensure_wix()[0].parent}")
    print("Formats: " + ", ".join(k for k, v in tools().items() if v))


def version() -> str:
    """Only the number from __version__ = "x.y.z" - robust against comments after the line.
    (Previously everything after "=" was taken: with a comment, quotes and "/" ended up
    in the package names -> the build failed, "set version number" reported an error.)"""
    import re as _re
    s = (DESKTOP / "bytera_client" / "__init__.py").read_text(encoding="utf-8")
    m = _re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', s, _re.M)
    return m.group(1).strip() if m else "1.0.0"


def run(cmd, **kw):
    print("  $", " ".join(str(c) for c in cmd))
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed:\n{(r.stdout + r.stderr)[-3000:]}")
    return r


def desktop_files() -> list[tuple[Path, str]]:
    """(source, relative target) of all desktop client files."""
    out = []
    for p in sorted(DESKTOP.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            out.append((p, p.relative_to(DESKTOP).as_posix()))
    return out


# ============================================================== Windows
def build_exe(v: str, winpython: str | None) -> Path:
    out = RELEASE / f"ByteraClient-{v}-windows.exe"
    args = ["-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed", "--name", "ByteraClient",
            "--icon", "bytera_client/resources/rmmclient.ico",
            "--add-data", "bytera_client/resources;bytera_client/resources", "bytera-client.py"]
    with tempfile.TemporaryDirectory() as td:
        work = Path(td) / "src"
        shutil.copytree(DESKTOP, work, ignore=shutil.ignore_patterns("__pycache__"))
        if os.name == "nt":
            ensure_pyinstaller()
            _ensure_crypto(sys.executable)
            run([sys.executable, *args], cwd=work)
        else:
            wine = _wine() or "/usr/lib/wine/wine64"
            py = winpython or os.environ.get("RMM_WINPYTHON") or "/opt/winpy/python/python.exe"
            if not Path(wine).exists() or not Path(py).exists():
                raise RuntimeError("For the .exe on Linux, wine64 and a Windows Python with PyInstaller are "
                                   "needed (--winpython). Alternatively build on Windows.")
            env = dict(os.environ, WINEDEBUG="-all")
            # end-to-end encryption needs 'cryptography' inside the .exe
            subprocess.run([wine, py, "-m", "pip", "install", "--quiet", "cryptography>=41"], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, timeout=900)
            # Wine needs a pipe as output (file redirection -> "Invalid handle")
            p = subprocess.Popen([wine, py, *args], cwd=work, env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True)
            log = p.communicate(timeout=1800)[0]
            if p.returncode != 0 or not (work / "dist" / "ByteraClient.exe").exists():
                raise RuntimeError("PyInstaller (Wine) failed:\n" + log[-3000:])
        shutil.copy2(work / "dist" / "ByteraClient.exe", out)
    return out


def build_zip(v: str, exe: Path) -> Path:
    out = RELEASE / f"ByteraClient-{v}-windows.zip"
    readme = ("BYTERA RMM Client\r\n======================\r\n\r\n"
              "Run ByteraClient.exe - no installation needed (Windows 10/11).\r\n"
              "For a Start menu/desktop shortcut, prefer the .msi.\r\n")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(exe, "ByteraClient/ByteraClient.exe")
        z.writestr("ByteraClient/README.txt", readme)
    return out


def build_msi(v: str, exe: Path) -> Path:
    wixl = shutil.which("wixl")
    if not wixl and os.name != "nt":
        raise RuntimeError("wixl missing (apt install wixl)")
    wix = ensure_wix() if os.name == "nt" and not wixl else None
    out = RELEASE / f"ByteraClient-{v}-windows.msi"
    ver4 = ".".join((v.split(".") + ["0", "0", "0"])[:3])
    wxs = f"""<?xml version="1.0" encoding="utf-8"?>
<Wix xmlns="http://schemas.microsoft.com/wix/2006/wi">
  <Product Id="*" Name="BYTERA RMM Client" Language="1031" Codepage="1252" Version="{ver4}"
           Manufacturer="BYTERA" UpgradeCode="6B3F0E5A-2C4D-4E8B-9A71-5D2C8F1E0A34">
    <Package InstallerVersion="200" Compressed="yes" InstallScope="perMachine"
             Description="BYTERA RMM Client" Manufacturer="BYTERA" />
    <MajorUpgrade DowngradeErrorMessage="A newer version is already installed." />
    <Media Id="1" Cabinet="client.cab" EmbedCab="yes" />
    <Icon Id="AppIcon" SourceFile="rmmclient.ico" />
    <Property Id="ARPPRODUCTICON" Value="AppIcon" />
    <!-- Choice in the install wizard: 1 = with administrator rights (VPN without prompt), 0 = without.
         Unattended: msiexec /i ByteraClient.msi /qn ADMINMODE=0 -->
    <Property Id="ADMINMODE" Value="1" />
    <Directory Id="TARGETDIR" Name="SourceDir">
      <Directory Id="ProgramFiles64Folder">
        <Directory Id="INSTALLDIR" Name="BYTERA RMM Client">
          <Component Id="MainExe" Guid="7D1A2B3C-4E5F-4A6B-8C7D-9E0F1A2B3C4D" Win64="yes">
            <File Id="ByteraClientExe" Source="ByteraClient.exe" KeyPath="yes" />
          </Component>
          <Component Id="ModeReg" Guid="5A4B3C2D-1E0F-4A9B-8C7D-6E5F4A3B2C1D" Win64="yes">
            <RegistryValue Root="HKLM" Key="Software\\BYTERA\\RMMClient" Name="AdminMode" Type="string"
                           Value="[ADMINMODE]" KeyPath="yes" />
          </Component>
        </Directory>
      </Directory>
      <Directory Id="ProgramMenuFolder">
        <Component Id="StartShortcut" Guid="8E2B3C4D-5F6A-4B7C-9D8E-0F1A2B3C4D5E">
          <Shortcut Id="StartMenuShortcut" Name="BYTERA RMM" Target="[INSTALLDIR]ByteraClient.exe"
                    WorkingDirectory="INSTALLDIR" Icon="AppIcon" />
          <RegistryValue Root="HKCU" Key="Software\\BYTERA\\RMMClient" Name="startmenu" Type="integer" Value="1" KeyPath="yes" />
        </Component>
      </Directory>
      <Directory Id="DesktopFolder">
        <Component Id="DesktopShortcut" Guid="9F3C4D5E-6A7B-4C8D-0E9F-1A2B3C4D5E6F">
          <Shortcut Id="DesktopShortcutLnk" Name="BYTERA RMM" Target="[INSTALLDIR]ByteraClient.exe"
                    WorkingDirectory="INSTALLDIR" Icon="AppIcon" />
          <RegistryValue Root="HKCU" Key="Software\\BYTERA\\RMMClient" Name="desktop" Type="integer" Value="1" KeyPath="yes" />
        </Component>
      </Directory>
    </Directory>
    <Feature Id="Main" Level="1" Title="BYTERA RMM Client">
      <ComponentRef Id="MainExe" />
      <ComponentRef Id="ModeReg" />
      <ComponentRef Id="StartShortcut" />
      <ComponentRef Id="DesktopShortcut" />
    </Feature>
  </Product>
</Wix>
"""
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        shutil.copy2(exe, t / "ByteraClient.exe")
        shutil.copy2(RES / "rmmclient.ico", t / "rmmclient.ico")
        (t / "client.wxs").write_text(wxs, encoding="utf-8")
        if wix:     # Windows: WiX 3 (candle + light)
            run([str(wix[0]), "-nologo", "-arch", "x64", "-out", "client.wixobj", "client.wxs"], cwd=t)
            run([str(wix[1]), "-nologo", "-sval", "-out", str(out), "client.wixobj"], cwd=t)
        else:
            run([wixl or "wixl", "-a", "x64", "-o", str(out), "client.wxs"], cwd=t)
            _msi_add_wizard(out, t, v)
    return out


def _msi_add_wizard(msi: Path, t: Path, v: str) -> None:
    """Add the install wizard to the MSI afterwards (wixl cannot do dialogs): choice
    "with administrator rights" / "without" -> property ADMINMODE -> HKLM\\Software\\BYTERA\\RMMClient\\AdminMode.
    The app evaluates it on first start (scheduled task or never ask)."""
    msibuild = shutil.which("msibuild")
    if not msibuild and shutil.which("apt-get") and hasattr(os, "geteuid") and os.geteuid() == 0:
        subprocess.run(["apt-get", "install", "-y", "-q", "msitools"], capture_output=True)   # older build image
        msibuild = shutil.which("msibuild")
    if not msibuild:
        print("  (msibuild missing - MSI without wizard; apt install msitools)")
        return

    def idt(name, cols, types, key, rows):
        lines = ["\t".join(cols), "\t".join(types), "\t".join([name, *key])]
        lines += ["\t".join("" if c is None else str(c) for c in r) for r in rows]
        # libmsi expects UTF-8 and stores in the MSI codepage (1252)
        (t / f"{name}.idt").write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))
        return f"{name}.idt"

    W, H = 370, 270
    files = [
        idt("TextStyle", ["TextStyle", "FaceName", "Size", "Color", "StyleBits"], ["s72", "s32", "i2", "I4", "I2"],
            ["TextStyle"], [["DlgFont8", "Segoe UI", 9, None, None], ["DlgTitle", "Segoe UI", 12, None, 1]]),
        idt("Dialog", ["Dialog", "HCentering", "VCentering", "Width", "Height", "Attributes", "Title", "Control_First",
                       "Control_Default", "Control_Cancel"],
            ["s72", "i2", "i2", "i2", "i2", "I4", "L128", "s50", "S50", "S50"], ["Dialog"],
            [["ModeDlg", 50, 50, W, H, 3, "BYTERA RMM Client " + v, "Mode", "Install", "Cancel"],
             ["FinishDlg", 50, 50, W, 160, 3, "BYTERA RMM Client " + v, "Done", "Done", "Done"]]),
        idt("Control", ["Dialog_", "Control", "Type", "X", "Y", "Width", "Height", "Attributes", "Property", "Text",
                        "Control_Next", "Help"],
            ["s72", "s50", "s20", "i2", "i2", "i2", "i2", "I4", "S72", "L0", "S50", "L50"], ["Dialog_", "Control"],
            [["ModeDlg", "Title", "Text", 20, 14, 330, 20, 3, None, "{\\DlgTitle}Install BYTERA RMM Client", None, None],
             ["ModeDlg", "Intro", "Text", 20, 40, 330, 34, 3, None,
              "How should the app run? VPN tunnels (WireGuard) require administrator rights on Windows.", None, None],
             ["ModeDlg", "Mode", "RadioButtonGroup", 20, 80, 330, 110, 3, "ADMINMODE", None, "Install", None],
             ["ModeDlg", "Install", "PushButton", 230, 238, 60, 18, 3, None, "Install", "Cancel", None],
             ["ModeDlg", "Cancel", "PushButton", 296, 238, 56, 18, 3, None, "Cancel", "Mode", None],
             ["FinishDlg", "Text", "Text", 20, 20, 330, 80, 3, None,
              "The BYTERA RMM Client is installed. The shortcut is on the desktop and in the Start menu.", None, None],
             ["FinishDlg", "Done", "PushButton", 296, 132, 56, 18, 3, None, "Done", None, None]]),
        idt("RadioButton", ["Property", "Order", "Value", "X", "Y", "Width", "Height", "Text", "Help"],
            ["s72", "i2", "s64", "i2", "i2", "i2", "i4", "L0", "L50"], ["Property", "Order"],
            [["ADMINMODE", 1, "1", 0, 0, 330, 46,
              "With administrator rights (recommended): always starts as administrator, VPN without prompts", None],
             ["ADMINMODE", 2, "0", 0, 56, 330, 46,
              "Without administrator rights: no prompts at start, VPN is then not available", None]]),
        idt("ControlEvent", ["Dialog_", "Control_", "Event", "Argument", "Condition", "Ordering"],
            ["s72", "s50", "s50", "s255", "S255", "I2"], ["Dialog_", "Control_", "Event", "Argument", "Condition"],
            [["ModeDlg", "Install", "EndDialog", "Return", "1", 1],
             ["ModeDlg", "Cancel", "EndDialog", "Exit", "1", 1],
             ["FinishDlg", "Done", "EndDialog", "Return", "1", 1]]),
    ]
    for f in files:
        run([msibuild, str(msi), "-i", f], cwd=t)
    # dialogs into the UI sequence: wizard before the installation, "Done" after (-1)
    seq = subprocess.run(["msiinfo", "export", str(msi), "InstallUISequence"], capture_output=True, text=True).stdout
    rows = [ln.split("\t") for ln in seq.replace("\r", "").split("\n")[3:] if ln.strip()]
    rows = [r for r in rows if r[0] not in ("ModeDlg", "FinishDlg")]
    rows += [["ModeDlg", "NOT Installed", "1100"], ["FinishDlg", "", "-1"]]
    files2 = [idt("InstallUISequence", ["Action", "Condition", "Sequence"], ["s72", "S255", "I2"], ["Action"], rows)]
    prop = subprocess.run(["msiinfo", "export", str(msi), "Property"], capture_output=True, text=True).stdout
    prows = [ln.split("\t") for ln in prop.replace("\r", "").split("\n")[3:] if ln.strip()]
    prows = [r for r in prows if r[0] != "DefaultUIFont"] + [["DefaultUIFont", "DlgFont8"]]
    for r in prows:                      # pass ADMINMODE on to the (elevated) installation
        if r[0] == "SecureCustomProperties" and "ADMINMODE" not in r[1]:
            r[1] = r[1] + ";ADMINMODE" if r[1] else "ADMINMODE"
    if not any(r[0] == "SecureCustomProperties" for r in prows):
        prows.append(["SecureCustomProperties", "ADMINMODE"])
    files2.append(idt("Property", ["Property", "Value"], ["s72", "l0"], ["Property"], prows))
    for f in files2:
        run([msibuild, str(msi), "-i", f], cwd=t)


# ============================================================== Linux
LINUX_LAUNCHER = """#!/bin/sh
# Starts the BYTERA RMM Client (Python 3 + Tk from the system)
exec python3 /opt/bytera-client/bytera-client.py "$@"
"""
DESKTOP_ENTRY = """[Desktop Entry]
Type=Application
Name=BYTERA RMM
Comment=RMM dashboard, relay drive, remote shell and remote screen
Exec=bytera-client %u
Icon=bytera-client
Terminal=false
Categories=Network;RemoteAccess;
MimeType=x-scheme-handler/bytera-rmm;
StartupWMClass=Tk
"""
# Transition starter (rename RAPALLE.net RMM -> BYTERA RMM): an old .tar installation
# updates itself with its old update code - which looks for the folder "rapalle-client-*/app"
# (a reference to the new folder in the package) and then starts "rapalle-client.py". This
# starter calls the new app, which then moves itself to the new location (legacy.py).
LEGACY_SHIM = """#!/usr/bin/env python3
# Transition starter: starts the BYTERA RMM Client (formerly RAPALLE.net RMM).
import os
import sys
here = os.path.dirname(os.path.abspath(__file__))
os.execv(sys.executable, [sys.executable, os.path.join(here, "bytera-client.py"), *sys.argv[1:]])
"""
LEGACY_LAUNCHER = """#!/bin/sh
# Transition starter (formerly RAPALLE.net RMM) - starts the BYTERA RMM Client
exec /usr/bin/bytera-client "$@"
"""
TAR_INSTALL = """#!/bin/sh
# Installs the BYTERA RMM Client for the current user (without root):
#   ~/.local/share/bytera-client, ~/.local/bin/bytera-client, Start menu entry.
# System-wide (to /opt):  sudo ./install.sh --system
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ "$1" = "--system" ]; then
  DEST=/opt/bytera-client; BIN=/usr/local/bin; APPS=/usr/share/applications; ICONS=/usr/share/icons/hicolor/256x256/apps
else
  DEST="$HOME/.local/share/bytera-client"; BIN="$HOME/.local/bin"; APPS="$HOME/.local/share/applications"
  ICONS="$HOME/.local/share/icons/hicolor/256x256/apps"
fi
command -v python3 >/dev/null || { echo "python3 missing (e.g. sudo apt install python3 python3-tk)"; exit 1; }
python3 -c "import tkinter" 2>/dev/null || echo "Note: Tk missing - please install python3-tk (Debian/Ubuntu) or python3-tkinter (Fedora)."
mkdir -p "$DEST" "$BIN" "$APPS" "$ICONS"
cp -r "$HERE/app/." "$DEST/"
rm -f "$DEST/rapalle-client.py"          # transition starter only for auto-updates of old installations
# rename: remove installation under the old name (RAPALLE.net RMM)
OLD="$(dirname "$DEST")/rapalle-client"
if [ -d "$OLD" ]; then rm -rf "$OLD"; fi
rm -f "$BIN/rapalle-client" "$APPS/rapalle-client.desktop" "$ICONS/rapalle-client.png"
printf '#!/bin/sh\\nexec python3 "%s/bytera-client.py" "$@"\\n' "$DEST" > "$BIN/bytera-client"
chmod +x "$BIN/bytera-client"
cp "$HERE/bytera-client.png" "$ICONS/bytera-client.png"
sed "s|^Exec=.*|Exec=$BIN/bytera-client %u|" "$HERE/bytera-client.desktop" > "$APPS/bytera-client.desktop"
echo "Installed. Start via the application menu or: $BIN/bytera-client"
"""


def _tar_dirs(tf: tarfile.TarFile, name: str) -> None:
    """Create missing directory entries (dpkg needs them)."""
    seen = tf.__dict__.setdefault("_rmm_dirs", set())
    parts = name.split("/")[:-1]
    for i in range(1, len(parts) + 1):
        d = "/".join(parts[:i])
        if d in ("", ".") or d in seen:
            continue
        seen.add(d)
        ti = tarfile.TarInfo(d)
        ti.type, ti.mode, ti.mtime, ti.uname, ti.gname = tarfile.DIRTYPE, 0o755, int(time.time()), "root", "root"
        tf.addfile(ti)


def _tar_add(tf: tarfile.TarFile, name: str, data: bytes, mode: int = 0o644):
    _tar_dirs(tf, name)
    ti = tarfile.TarInfo(name)
    ti.size, ti.mode, ti.mtime, ti.uname, ti.gname = len(data), mode, int(time.time()), "root", "root"
    tf.addfile(ti, io.BytesIO(data))


def _tar_legacy(tf: tarfile.TarFile, base: str, v: str) -> None:
    """Transition for old .tar installations (see LEGACY_SHIM)."""
    _tar_add(tf, f"{base}/app/rapalle-client.py", LEGACY_SHIM.encode(), 0o755)
    ti = tarfile.TarInfo(f"rapalle-client-{v}")
    ti.type, ti.linkname, ti.mode, ti.mtime, ti.uname, ti.gname = tarfile.SYMTYPE, base, 0o777, int(time.time()), "root", "root"
    tf.addfile(ti)


def build_tar(v: str) -> list[Path]:
    base = f"bytera-client-{v}"
    outs = []
    for suffix, mode in ((".tar.gz", "w:gz"), (".tar", "w")):
        out = RELEASE / f"bytera-client-{v}-linux{suffix}"
        with tarfile.open(out, mode) as tf:
            for src, rel in desktop_files():
                _tar_add(tf, f"{base}/app/{rel}", src.read_bytes(), 0o755 if rel.endswith("bytera-client.py") else 0o644)
            _tar_add(tf, f"{base}/app/.bytera-install", b"tar\n")              # auto-update: installation type
            _tar_add(tf, f"{base}/install.sh", TAR_INSTALL.encode(), 0o755)
            _tar_add(tf, f"{base}/bytera-client.desktop", DESKTOP_ENTRY.encode())
            _tar_add(tf, f"{base}/bytera-client.png", (RES / "rmmclient-256.png").read_bytes())
            _tar_legacy(tf, base, v)
        outs.append(out)
    return outs


def build_tgz(v: str) -> Path:
    """Like .tar.gz, just with the .tgz extension (some tools expect it)."""
    base = f"bytera-client-{v}"
    out = RELEASE / f"bytera-client-{v}-linux.tgz"
    with tarfile.open(out, "w:gz") as tf:
        for src, rel in desktop_files():
            _tar_add(tf, f"{base}/app/{rel}", src.read_bytes(), 0o755 if rel.endswith("bytera-client.py") else 0o644)
        _tar_add(tf, f"{base}/app/.bytera-install", b"tar\n")
        _tar_add(tf, f"{base}/install.sh", TAR_INSTALL.encode(), 0o755)
        _tar_add(tf, f"{base}/bytera-client.desktop", DESKTOP_ENTRY.encode())
        _tar_add(tf, f"{base}/bytera-client.png", (RES / "rmmclient-256.png").read_bytes())
        _tar_legacy(tf, base, v)
    return out


def _system_tree(kind: str) -> list[tuple[str, bytes, int]]:
    """Files of a system-wide installation (/opt + /usr) - the same for .deb, .rpm and .pkg."""
    files = []
    for src, rel in desktop_files():
        files.append((f"opt/bytera-client/{rel}", src.read_bytes(), 0o755 if rel.endswith("bytera-client.py") else 0o644))
    files.append(("opt/bytera-client/.bytera-install", f"{kind}\n".encode(), 0o644))   # auto-update: installation type
    files.append(("usr/bin/bytera-client", LINUX_LAUNCHER.encode(), 0o755))
    files.append(("usr/bin/rapalle-client", LEGACY_LAUNCHER.encode(), 0o755))   # transition: old restart command
    files.append(("usr/share/applications/bytera-client.desktop", DESKTOP_ENTRY.encode(), 0o644))
    files.append(("usr/share/icons/hicolor/256x256/apps/bytera-client.png", (RES / "rmmclient-256.png").read_bytes(), 0o644))
    return files


def build_rpm(v: str) -> Path:
    """.rpm (noarch) via rpmbuild - Fedora, RHEL/Alma/Rocky, openSUSE."""
    if not shutil.which("rpmbuild"):
        raise RuntimeError("rpmbuild missing (apt install rpm / dnf install rpm-build)")
    out_name = f"bytera-client-{v}-1.noarch.rpm"
    with tempfile.TemporaryDirectory() as td:
        top = Path(td) / "rpmbuild"
        for d in ("SPECS", "SOURCES", "BUILD", "RPMS", "SRPMS", "BUILDROOT"):
            (top / d).mkdir(parents=True)
        root = Path(td) / "root"
        for rel, data, mode in _system_tree("rpm"):
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            p.chmod(mode)
        spec = f"""Name:           bytera-client
Version:        {v}
Release:        1
Summary:        BYTERA RMM Client
License:        MIT
BuildArch:      noarch
Requires:       python3
Requires:       (python3-tkinter or python3-tk)
Recommends:     (chromium or google-chrome-stable or microsoft-edge-stable or firefox)
Recommends:     libsecret
Recommends:     python3-cryptography
AutoReqProv:    no
Obsoletes:      rapalle-client
Provides:       rapalle-client

%description
The RMM dashboard as an app: stored sign-in, relay as a drive in the
file manager, remote shell in its own window and remote screen via RDP/VNC.

%install
mkdir -p %{{buildroot}}
cp -a {root}/. %{{buildroot}}/

%post
command -v update-desktop-database >/dev/null && update-desktop-database -q /usr/share/applications || true

%files
%defattr(-,root,root,-)
/opt/bytera-client
/usr/bin/bytera-client
/usr/bin/rapalle-client
/usr/share/applications/bytera-client.desktop
/usr/share/icons/hicolor/256x256/apps/bytera-client.png
"""
        specfile = top / "SPECS" / "bytera-client.spec"
        specfile.write_text(spec, encoding="utf-8")
        run(["rpmbuild", "-bb", "--define", f"_topdir {top}", "--define", "__os_install_post %{nil}",
             "--define", "_build_id_links none", str(specfile)])
        built = next((top / "RPMS").rglob("*.rpm"), None)
        if not built:
            raise RuntimeError("rpmbuild produced no package")
        out = RELEASE / out_name
        shutil.copy2(built, out)
    return out


def build_pkg(v: str) -> Path:
    """Arch/Manjaro package (.pkg.tar.xz) - built ourselves, without makepkg (like for the agent)."""
    pv = v.replace("-", "_")
    out = RELEASE / f"bytera-client-{pv}-1-any.pkg.tar.xz"
    tree = _system_tree("pkg")
    size = sum(len(d) for _, d, _ in tree)
    pkginfo = (f"pkgname = bytera-client\npkgver = {pv}-1\npkgdesc = BYTERA RMM Client\n"
               f"url = https://rapalle.net\nbuilddate = {int(time.time())}\npackager = BYTERA\n"
               f"size = {size}\narch = any\nlicense = MIT\nreplaces = rapalle-client\nconflict = rapalle-client\n"
               f"depend = python\ndepend = tk\n"
               f"optdepend = chromium: dashboard as an app window\noptdepend = libsecret: sign-in in the keyring\n"
               f"optdepend = freerdp: remote screen via RDP\noptdepend = tigervnc: remote screen via VNC\n")
    install = ("post_install() {\n  command -v update-desktop-database >/dev/null && "
               "update-desktop-database -q /usr/share/applications || true\n}\n\n"
               "post_upgrade() {\n  post_install\n}\n")
    with tarfile.open(out, "w:xz") as tf:
        _tar_add(tf, ".PKGINFO", pkginfo.encode())          # must be the FIRST entry
        _tar_add(tf, ".INSTALL", install.encode())
        for rel, data, mode in tree:
            _tar_add(tf, rel, data, mode)
    return out


def build_deb(v: str) -> Path:
    """.deb without dpkg-deb (ar + tar), so it runs on any build system."""
    out = RELEASE / f"bytera-client_{v}_all.deb"
    data = io.BytesIO()
    size = 0
    with tarfile.open(fileobj=data, mode="w:gz") as tf:
        for rel, b, mode in _system_tree("deb"):
            size += len(b)
            _tar_add(tf, f"./{rel}", b, mode)
    control = (f"Package: bytera-client\nVersion: {v}\nSection: net\nPriority: optional\nArchitecture: all\n"
               f"Depends: python3 (>= 3.8), python3-tk\n"
               f"Replaces: rapalle-client\nConflicts: rapalle-client\nProvides: rapalle-client\n"
               f"Recommends: chromium | chromium-browser | google-chrome-stable | microsoft-edge-stable | firefox | firefox-esr, "
               f"libglib2.0-bin, libsecret-tools, python3-cryptography\n"
               f"Suggests: freerdp3-x11 | freerdp2-x11 | remmina, tigervnc-viewer\n"
               f"Installed-Size: {max(1, size // 1024)}\nMaintainer: BYTERA <support@rapalle.net>\n"
               f"Description: BYTERA RMM Client\n"
               f" The RMM dashboard as an app: stored sign-in, relay as a drive in the\n"
               f" file manager, remote shell in its own window and remote screen via RDP/VNC.\n")
    ctl = io.BytesIO()
    with tarfile.open(fileobj=ctl, mode="w:gz") as tf:
        _tar_add(tf, "./control", control.encode())
        _tar_add(tf, "./postinst", b"#!/bin/sh\nset -e\ncommand -v update-desktop-database >/dev/null && "
                                   b"update-desktop-database -q /usr/share/applications || true\n", 0o755)

    def ar_member(name: str, payload: bytes) -> bytes:
        hdr = f"{name:<16}{int(time.time()):<12}{0:<6}{0:<6}{100644:<8}{len(payload):<10}`\n".encode()
        return hdr + payload + (b"\n" if len(payload) % 2 else b"")

    with open(out, "wb") as f:
        f.write(b"!<arch>\n")
        f.write(ar_member("debian-binary", b"2.0\n"))
        f.write(ar_member("control.tar.gz", ctl.getvalue()))
        f.write(ar_member("data.tar.gz", data.getvalue()))
    return out


# ============================================================== Android
def build_apk(v: str) -> Path:
    jar = Path("/usr/lib/android-sdk/platforms/android-23/android.jar")
    tools = {t: shutil.which(t) for t in ("aapt", "javac", "dalvik-exchange", "zipalign", "apksigner")}
    missing = [t for t, p in tools.items() if not p] + ([] if jar.exists() else ["android-sdk-platform-23"])
    if missing:
        raise RuntimeError("Missing tools: " + ", ".join(missing) +
                           " (apt install aapt openjdk-17-jdk-headless dalvik-exchange zipalign apksigner android-sdk-platform-23)")
    ks, kspass = ANDROID / "signing" / "release.jks", ANDROID / "signing" / "keystore.pass"
    if not ks.exists():
        raise RuntimeError("Signing key missing: src/client/android/signing/release.jks")
    out = RELEASE / f"ByteraClient-{v}-android.apk"
    code = int("".join(f"{int(x):02d}" for x in (v.split(".") + ["0", "0"])[:3]))   # 1.0.0 -> 10000
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        (t / "gen").mkdir()
        (t / "classes").mkdir()
        man = (ANDROID / "AndroidManifest.xml").read_text(encoding="utf-8")
        man = man.replace('android:versionCode="1"', f'android:versionCode="{code}"').replace(
            'android:versionName="1.0.0"', f'android:versionName="{v}"')
        (t / "AndroidManifest.xml").write_text(man, encoding="utf-8")
        java = (ANDROID / "src/net/bytera/rmmclient/MainActivity.java").read_text(encoding="utf-8")
        java = java.replace('static final String VERSION = "1.0.0";', f'static final String VERSION = "{v}";')
        jdir = t / "java/net/bytera/rmmclient"
        jdir.mkdir(parents=True)
        (jdir / "MainActivity.java").write_text(java, encoding="utf-8")
        run(["aapt", "package", "-f", "-m", "-M", "AndroidManifest.xml", "-S", str(ANDROID / "res"),
             "-A", str(ANDROID / "assets"), "-I", str(jar), "-J", "gen", "-F", "unsigned.apk"], cwd=t)
        run(["javac", "-source", "8", "-target", "8", "-encoding", "UTF-8", "-Xlint:-options", "-bootclasspath", str(jar),
             "-d", "classes", "gen/net/bytera/rmmclient/R.java", "java/net/bytera/rmmclient/MainActivity.java"], cwd=t)
        run(["dalvik-exchange", "--dex", "--min-sdk-version=23", "--output=classes.dex", "classes"], cwd=t)
        run(["aapt", "add", "unsigned.apk", "classes.dex"], cwd=t)
        run(["zipalign", "-f", "4", "unsigned.apk", "aligned.apk"], cwd=t)
        run(["apksigner", "sign", "--ks", str(ks), "--ks-key-alias", "rmmclient", "--ks-pass", f"file:{kspass}",
             "--out", str(out), "aligned.apk"], cwd=t)
        run(["apksigner", "verify", str(out)], cwd=t)
    for extra in RELEASE.glob("*.idsig"):
        extra.unlink()
    return out


# ============================================================== main
def main():
    ap = argparse.ArgumentParser(description="Builds the packages of the BYTERA RMM Client")
    ap.add_argument("--targets", default=",".join(ALL), help="Comma list of: " + ",".join(ALL))
    ap.add_argument("--winpython", help="Windows python.exe (under Wine) with PyInstaller")
    ap.add_argument("--list", action="store_true", help="show existing packages")
    ap.add_argument("--keep-old", action="store_true", help="do not delete older versions in release/")
    ap.add_argument("--out", help="output folder instead of src/client/release")
    ap.add_argument("--set-version", help="set the version first (e.g. 1.2.1)")
    ap.add_argument("--tools", action="store_true", help="print available formats as JSON")
    ap.add_argument("--available", action="store_true", help="build only formats for which tools are available")
    ap.add_argument("--prepare", action="store_true", help="fetch tools in advance (Windows: PyInstaller + WiX)")
    a = ap.parse_args()
    global RELEASE
    if a.out:                               # ALWAYS create the output folder first (the build container collects /out,
        RELEASE = Path(a.out).resolve()     # even if the build fails -> otherwise "/out not found")
        RELEASE.mkdir(parents=True, exist_ok=True)
    if a.set_version:                       # before --tools: "--set-version X --tools" sets and reports
        set_version(a.set_version)
    if a.prepare:
        prepare()
        return
    if a.tools:
        import json as _json
        print(_json.dumps({"tools": tools(), "version": version()}))
        return
    if a.out:
        RELEASE = Path(a.out).resolve()
    RELEASE.mkdir(parents=True, exist_ok=True)
    if a.list:
        for p in sorted(RELEASE.iterdir()):
            print(f"{p.stat().st_size:>12,}  {p.name}")
        return
    v = version()
    targets = [t.strip() for t in a.targets.split(",") if t.strip()]
    if a.available:
        av = tools()
        skipped = [t for t in targets if not av.get(t)]
        targets = [t for t in targets if av.get(t)]
        if skipped:
            print(f"Skipped (tools missing here): {', '.join(skipped)}")
    if not a.keep_old:
        # remove old versions only for the formats being rebuilt now
        exts = {"exe": "-windows.exe", "zip": "-windows.zip", "msi": "-windows.msi", "deb": "_all.deb",
                "rpm": ".noarch.rpm", "pkg": "-any.pkg.tar.xz", "tar": "-linux.tar", "tgz": "-linux.tgz",
                "apk": "-android.apk"}
        for p in RELEASE.iterdir():
            if not p.is_file() or v in p.name or not p.name.lower().startswith(("bytera", "rapalle")):
                continue
            if any(t in targets and (p.name.endswith(sfx) or (t == "tar" and p.name.endswith("-linux.tar.gz")))
                   for t, sfx in exts.items()):
                p.unlink()
    print(f"BYTERA RMM Client {v} -> {RELEASE}")
    ok, failed, exe = [], [], RELEASE / f"ByteraClient-{v}-windows.exe"
    steps = [("exe", lambda: build_exe(v, a.winpython)), ("zip", lambda: build_zip(v, exe)),
             ("msi", lambda: build_msi(v, exe)), ("deb", lambda: build_deb(v)), ("rpm", lambda: build_rpm(v)),
             ("pkg", lambda: build_pkg(v)), ("tar", lambda: build_tar(v)), ("tgz", lambda: build_tgz(v)),
             ("apk", lambda: build_apk(v))]
    for name, fn in steps:
        if name not in targets:
            continue
        if name in ("zip", "msi") and not exe.exists():
            failed.append((name, "needs the .exe first"))
            continue
        print(f"\n== {name}")
        try:
            res = fn()
            for p in (res if isinstance(res, list) else [res]):
                print(f"  -> {p.name} ({p.stat().st_size:,} Bytes)")
                ok.append(p.name)
        except Exception as e:      # noqa: BLE001 - continue with the next package
            print(f"  !! {e}")
            failed.append((name, str(e).splitlines()[0]))
    print(f"\nDone: {len(ok)} package(s)" + (f", failed: {', '.join(n for n, _ in failed)}" if failed else ""))
    sys.exit(1 if failed and not ok else 0)


if __name__ == "__main__":
    main()
