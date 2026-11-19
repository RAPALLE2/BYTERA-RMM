# features.py - platform functions: relay as a drive, remote screen via RDP/VNC
# ----------------------------------------------------------------------------
# The commands are built by PURE functions (build_*) so they can be tested without
# Windows/Android (tests/test_logic.py). They are executed with
# subprocess WITHOUT a shell (no problems with spaces/special characters).
#
# Relay as a drive (WebDAV, sign-in with username + DEVICE TOKEN):
#   Windows  net use <X>: \\host@SSL@port\dav  -> appears in Explorer
#   Linux    gio mount davs://user@host:port/dav/ -> appears in the file manager (GVFS)
# Remote screen:
#   Windows  mstsc (.rdp file) / VNC viewer (TightVNC, RealVNC, UltraVNC, TigerVNC)
#   Linux    xfreerdp3/xfreerdp/remmina  or vncviewer/remmina
#   (Android: its own app under src/client/android)

from __future__ import annotations

import os
import platform
import shutil
import string
import subprocess
import tempfile
import urllib.parse
from pathlib import Path



def system() -> str:
    return {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}.get(platform.system(), "other")


class FeatureError(Exception):
    pass


# ============================================================== Relay drive
def server_parts(server: str) -> tuple[str, str, int]:
    p = urllib.parse.urlparse(server)
    scheme = p.scheme or "https"
    port = p.port or (443 if scheme == "https" else 80)
    return scheme, p.hostname or "", port


def build_windows_unc(server: str) -> str:
    """https://rmm.company.com:8443 -> \\\\rmm.company.com@SSL@8443\\dav"""
    scheme, host, port = server_parts(server)
    ssl = scheme == "https"
    default = 443 if ssl else 80
    unc = "\\\\" + host + ("@SSL" if ssl else "") + (f"@{port}" if port != default else "") + "\\dav"
    return unc


def build_windows_mount(server: str, letter: str, username: str, token: str) -> list[str]:
    return ["net", "use", f"{letter}:", build_windows_unc(server), token, f"/user:{username}", "/persistent:yes"]


def build_windows_unmount(letter: str) -> list[str]:
    return ["net", "use", f"{letter}:", "/delete", "/y"]


def build_linux_uri(server: str, username: str) -> str:
    scheme, host, port = server_parts(server)
    dav = "davs" if scheme == "https" else "dav"
    default = 443 if scheme == "https" else 80
    user = urllib.parse.quote(username, safe="")
    return f"{dav}://{user}@{host}" + (f":{port}" if port != default else "") + "/dav/"


def free_drive_letter(used: set[str] | None = None) -> str:
    """Find a free drive letter from Z downward."""
    if used is None:
        used = {c for c in string.ascii_uppercase if os.path.exists(f"{c}:\\")}
    for c in reversed(string.ascii_uppercase[3:]):   # Z..D
        if c not in used:
            return c
    raise FeatureError("No free drive letter")


_current: subprocess.Popen | None = None      # running command (for the stop button)


def cancel_current() -> None:
    """Cancel the running command (stop button in the control window)."""
    p = _current
    if p is not None and p.poll() is None:
        try:
            p.kill()
        except Exception:
            pass


def _run(cmd: list[str], input_text: str | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
    global _current
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000   # CREATE_NO_WINDOW
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE if input_text is not None else None, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, **kw)
    _current = p
    try:
        out, err = p.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        out, err = p.communicate()
    finally:
        _current = None
    if p.returncode is not None and p.returncode < 0:
        raise FeatureError("Cancelled")
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


def build_windows_url(server: str) -> str:
    """Second spelling for net use (some Windows 11 builds handle it better)."""
    scheme, host, port = server_parts(server)
    default = 443 if scheme == "https" else 80
    return f"{scheme}://{host}" + (f":{port}" if port != default else "") + "/dav/"


def _win_service_running(name: str) -> bool | None:
    try:
        r = _run(["sc", "query", name], timeout=15)
    except Exception:
        return None
    if r.returncode != 0:
        return None                                   # the service does not exist
    return "RUNNING" in (r.stdout or "")


def _win_prepare_webdav(http: bool) -> str:
    """Windows 11: the "WebClient" service (WebDAV mini redirector) is often stopped or set to
    "Manual" and does not start on its own -> net use fails with system error 67.
    Once, with administrator rights (UAC): set the service to Automatic + start it, remove the
    file-size limit (default 50 MB), allow Basic sign-in for HTTP."""
    state = _win_service_running("WebClient")
    if state is None:
        return ("The Windows service \"WebClient\" is missing. On Windows Server: install the \"WebDAV Redirector\" feature "
                "(Server Manager -> Features -> WebDAV Redirector).")
    if state and not http:
        return ""
    from .vpn import _elevated
    lines = ["Set-Service -Name WebClient -StartupType Automatic",
             "Start-Service -Name WebClient",
             "New-ItemProperty -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\WebClient\\Parameters' "
             "-Name FileSizeLimitInBytes -PropertyType DWord -Value 4294967295 -Force | Out-Null"]
    if http:
        lines.append("New-ItemProperty -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\WebClient\\Parameters' "
                     "-Name BasicAuthLevel -PropertyType DWord -Value 2 -Force | Out-Null")
        lines.append("Restart-Service -Name WebClient")
    _elevated(lines)
    import time as _t
    for _ in range(10):
        if _win_service_running("WebClient"):
            return ""
        _t.sleep(1)
    return "The \"WebClient\" service could not be started (administrator rights declined?)."


_WIN_ERRORS = {
    "67": "Network name not found - the WebClient service is not running or the server is not reachable via HTTPS.",
    "53": "Network path not found - check the server address/port.",
    "5": "Access denied - sign-in rejected (device removed in the dashboard? sign in again).",
    "86": "Sign-in rejected - sign the device in again.",
    "1244": "Sign-in rejected - sign the device in again.",
    "1326": "Sign-in rejected - sign the device in again.",
    "85": "The drive letter is already in use.",
    "1219": "There is already a connection to this server with different credentials (net use * /delete helps).",
    "2148073478": "The server certificate is not trusted - WebDAV on Windows needs a valid certificate.",
}


def _win_error_hint(text: str) -> str:
    import re as _re
    m = _re.search(r"(?:Systemfehler|System error)\s+(\d+)", text or "")
    return _WIN_ERRORS.get(m.group(1), "") if m else ""


def mount_relay(server: str, username: str, token: str, store) -> str:
    """Connects the relay as a drive. -> message for the user."""
    sysname = system()
    if sysname == "windows":
        letter = store.get("drive_letter") or free_drive_letter()
        scheme, _, _ = server_parts(server)
        problem = _win_prepare_webdav(scheme == "http")
        if problem:
            raise FeatureError(f"Could not connect the drive:\n{problem}")
        _run(build_windows_unmount(letter))                    # remove an old/half connection on the letter
        errors = []
        for target in (build_windows_unc(server), build_windows_url(server)):
            r = _run(["net", "use", f"{letter}:", target, token, f"/user:{username}", "/persistent:yes"], timeout=90)
            if r.returncode == 0:
                store.set(drive_letter=letter)
                try:
                    os.startfile(f"{letter}:\\")   # type: ignore[attr-defined]
                except Exception:
                    pass
                return f"The relay is now drive {letter}: in Explorer."
            errors.append(((r.stderr or r.stdout) or "").strip())
        msg = errors[-1] or errors[0]
        hint = _win_error_hint(" ".join(errors))
        if scheme == "http" and not hint:
            hint = "Windows only allows WebDAV without HTTPS with BasicAuthLevel = 2 - better make the server reachable via HTTPS."
        if store.get("insecure", False):
            hint += "\nNote: the app is set to \"do not verify certificate\" - Windows WebDAV does not accept invalid certificates."
        raise FeatureError(f"Could not connect the drive:\n{msg}" + (f"\n\n{hint}" if hint else ""))
    if sysname == "linux":
        if not shutil.which("gio"):
            raise FeatureError("\"gio\" is missing (package glib2/libglib2.0-bin). It is used to mount the drive in the file manager.")
        uri = build_linux_uri(server, username)
        # depending on the version, gio asks only for the password or for user + password.
        for answer in (f"{token}\n", f"{username}\n{token}\n", f"{username}\n\n{token}\n"):
            r = _run(["gio", "mount", uri], input_text=answer)
            out = (r.stderr or "") + (r.stdout or "")
            if r.returncode == 0 or "already mounted" in out.lower() or "bereits eingeh" in out.lower():
                store.set(linux_mount=uri)
                if shutil.which("xdg-open"):
                    subprocess.Popen(["xdg-open", uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return "The relay is mounted in the file manager (sidebar / \"Other locations\")."
        raise FeatureError(f"Mounting failed:\n{out.strip()}")
    raise FeatureError("The drive is not supported on this system.")


def unmount_relay(store) -> str:
    sysname = system()
    if sysname == "windows":
        letter = store.get("drive_letter")
        if not letter:
            return "No drive is connected."
        _run(build_windows_unmount(letter))
        store.set(drive_letter="")
        return f"Drive {letter}: disconnected."
    if sysname == "linux":
        uri = store.get("linux_mount")
        if not uri:
            return "Nothing is mounted."
        _run(["gio", "mount", "-u", uri])
        store.set(linux_mount="")
        return "Relay unmounted."
    return "Nothing to disconnect."


# ============================================================== Remote screen
WIN_VNC_VIEWERS = [
    r"C:\Program Files\TightVNC\tvnviewer.exe",
    r"C:\Program Files\RealVNC\VNC Viewer\vncviewer.exe",
    r"C:\Program Files\uvnc bvba\UltraVNC\vncviewer.exe",
    r"C:\Program Files\TigerVNC\vncviewer.exe",
    r"C:\Program Files (x86)\TightVNC\tvnviewer.exe",
    r"C:\Program Files (x86)\uvnc bvba\UltraVNC\vncviewer.exe",
]


def build_rdp_file(host: str, port: int, username: str = "") -> str:
    lines = [f"full address:s:{host}:{port}", "prompt for credentials:i:1", "screen mode id:i:2",
             "authentication level:i:2", "redirectclipboard:i:1"]
    if username:
        lines.append(f"username:s:{username}")
    return "\r\n".join(lines) + "\r\n"


def linux_screen_candidates(protocol: str, host: str, port: int) -> list[list[str]]:
    if protocol == "rdp":
        return [["xfreerdp3", f"/v:{host}:{port}", "/dynamic-resolution"],
                ["xfreerdp", f"/v:{host}:{port}", "/dynamic-resolution"],
                ["remmina", "-c", f"rdp://{host}:{port}"]]
    return [["vncviewer", f"{host}::{port}"],
            ["remmina", "-c", f"vnc://{host}:{port}"],
            ["xdg-open", f"vnc://{host}:{port}"]]


def android_screen_uri(protocol: str, host: str, port: int) -> str:
    if protocol == "rdp":
        # Microsoft Remote Desktop / "Windows App" understands this URI scheme
        return "rdp://" + urllib.parse.quote(f"full address=s:{host}:{port}", safe="=:")
    return f"vnc://{host}:{port}"


def launch_screen(protocol: str, host: str, port: int) -> str:
    sysname = system()
    if sysname == "windows":
        if protocol == "rdp":
            f = Path(tempfile.gettempdir()) / f"bytera-{host}-{port}.rdp"
            f.write_text(build_rdp_file(host, port), encoding="utf-8")
            os.startfile(str(f))   # type: ignore[attr-defined]
            return "Opening Remote Desktop …"
        for exe in WIN_VNC_VIEWERS:
            if os.path.exists(exe):
                subprocess.Popen([exe, f"{host}::{port}"])
                return f"Opening {Path(exe).stem} …"
        try:
            os.startfile(f"vnc://{host}:{port}")   # type: ignore[attr-defined]
            return "Opening VNC viewer …"
        except OSError:
            raise FeatureError(f"No VNC viewer found. Please install e.g. TightVNC Viewer.\nAddress: {host}::{port}")
    if sysname == "linux":
        for cmd in linux_screen_candidates(protocol, host, port):
            if shutil.which(cmd[0]):
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return f"Opening {cmd[0]} …"
        need = "FreeRDP or Remmina" if protocol == "rdp" else "a VNC viewer (TigerVNC) or Remmina"
        raise FeatureError(f"Please install {need}.\nAddress: {host}:{port}")
    raise FeatureError(f"Please connect manually: {host}:{port}")


# ------------------------------------------------------------------ open externally (from the dashboard)
LINUX_TERMINALS = [("x-terminal-emulator", ["-e"]), ("gnome-terminal", ["--"]), ("konsole", ["-e"]),
                   ("xfce4-terminal", ["-x"]), ("mate-terminal", ["-x"]), ("tilix", ["-e"]), ("kitty", []),
                   ("alacritty", ["-e"]), ("xterm", ["-e"])]


def build_ssh_command(host: str, port: int, user: str = "", alias: str = "") -> list[str]:
    """ssh command. alias = own known_hosts name (tunnel: 127.0.0.1 with changing port)."""
    cmd = ["ssh", "-p", str(port)]
    if alias:
        cmd += ["-o", f"HostKeyAlias={alias}"]
    return cmd + [f"{user}@{host}" if user else host]


def start_program(protocol: str, host: str, port: int, user: str = "", alias: str = ""):
    """Starts RDP/VNC/SSH. -> (message, process or None). The tunnel needs the process
    ("until end of use": program closed -> tunnel closed)."""
    sysname = system()
    if protocol == "ssh":
        ssh = build_ssh_command(host, port, user, alias)
        if sysname == "windows":
            if not shutil.which("ssh"):
                raise FeatureError("No SSH client found (Windows: Settings -> Optional Features -> OpenSSH Client).")
            p = subprocess.Popen(ssh, creationflags=0x00000010)          # CREATE_NEW_CONSOLE
            return "Opening SSH …", p
        if sysname == "linux":
            if not shutil.which("ssh"):
                raise FeatureError("No SSH client found (package openssh-client).")
            for term, flag in LINUX_TERMINALS:
                if shutil.which(term):
                    p = subprocess.Popen([term, *flag, *ssh], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return f"SSH in {term} …", p
            raise FeatureError("No terminal program found. Command: " + " ".join(ssh))
        raise FeatureError("Please connect manually: " + " ".join(ssh))
    if sysname == "windows" and protocol == "rdp":
        f = Path(tempfile.gettempdir()) / f"bytera-{host}-{port}.rdp"
        f.write_text(build_rdp_file(host, port, user), encoding="utf-8")
        return "Opening Remote Desktop …", subprocess.Popen(["mstsc", str(f)])
    if sysname == "windows":
        for exe in WIN_VNC_VIEWERS:
            if os.path.exists(exe):
                return f"Opening {Path(exe).stem} …", subprocess.Popen([exe, f"{host}::{port}"])
        return launch_screen(protocol, host, port), None
    if sysname == "linux":
        for cmd in linux_screen_candidates(protocol, host, port):
            if shutil.which(cmd[0]) and cmd[0] != "xdg-open":
                return f"Opening {cmd[0]} …", subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return launch_screen(protocol, host, port), None
    return launch_screen(protocol, host, port), None
