"""
vpn.py - fully automatic management of the RMM WireGuard tunnels (Windows/Linux)
==========================================================================

Polls GET /api/vpn/my every minute (all active tunnels the signed-in user is a
participant of, with a ready WireGuard file) and reconciles:
  * new tunnel             -> write the file, create the tunnel and connect
  * file changed           -> set up again (e.g. new endpoint)
  * tunnel gone/expired    -> disconnect and delete the tunnel
Only tunnels this app created itself are ever touched (list in the app store).
Signing out removes all of them.

Windows: WireGuard for Windows must be installed; setting it up as a service
         requires administrator rights (one UAC prompt per change).
Linux:   wireguard-tools; set up via pkexec (password prompt) or directly as root.
Android: not possible automatically - import the file/QR code from the dashboard (app
         "VPN") into the WireGuard app.
"""
from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import threading
from pathlib import Path

IS_WINDOWS = platform.system() == "Windows"


def wg_exe() -> str | None:
    if IS_WINDOWS:
        p = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "WireGuard", "wireguard.exe")
        return p if os.path.exists(p) else shutil.which("wireguard.exe")
    return shutil.which("wg-quick")


def supported() -> tuple[bool, str]:
    if platform.system() not in ("Windows", "Linux"):
        return False, "Automation only on Windows and Linux"
    if not wg_exe():
        return False, ("Install WireGuard for Windows (wireguard.com/install)" if IS_WINDOWS
                       else "Install the \"wireguard-tools\" package")
    return True, ""


def _conf_dir(data_dir: Path) -> Path:
    d = Path(data_dir) / "wireguard"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _elevated(script_lines: list[str]) -> tuple[int, str]:
    """Run commands with admin/root rights (UAC or pkexec).
    Windows: the commands go into a temporary .ps1 file that runs via a UAC prompt with
    administrator rights (no problems with quotes/spaces in paths)."""
    if IS_WINDOWS:
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".ps1", prefix="rmm-")
        logp = path + ".log"
        with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
            f.write("$ErrorActionPreference = 'Continue'\r\n")
            # A process started via UAC cannot return its output to us -> into a file.
            # Write-Output is redirected; log external programs with "| Write-Output".
            f.write(f"$RmmLog = '{logp}'\r\n")
            f.write("function Write-Output { param([Parameter(ValueFromPipeline=$true)]$InputObject) "
                    "process { Add-Content -LiteralPath $RmmLog -Value ([string]$InputObject) -Encoding UTF8 } }\r\n")
            f.write("\r\n".join(script_lines) + "\r\n")
            f.write("exit $LASTEXITCODE\r\n")
        ps = ("$p = Start-Process -FilePath powershell -Verb RunAs -Wait -PassThru -WindowStyle Hidden "
              f"-ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File \"{path}\"'); exit $p.ExitCode")
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                               creationflags=0x08000000)       # CREATE_NO_WINDOW
            out = (p.stdout or "") + (p.stderr or "")
            try:
                out = Path(logp).read_text(encoding="utf-8-sig", errors="replace") + out
            except OSError:
                pass
            if "abgebrochen" in out.lower() or "canceled by the user" in out.lower():
                return 1, "Administrator rights were declined."
            return p.returncode, out
        except Exception as e:  # noqa: BLE001
            return 1, str(e)
        finally:
            for x in (path, logp):
                try:
                    os.unlink(x)
                except OSError:
                    pass
    sh = " ; ".join(script_lines)
    cmd = ["sh", "-c", sh] if hasattr(os, "geteuid") and os.geteuid() == 0 else ["pkexec", "sh", "-c", sh]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:  # noqa: BLE001
        return 1, str(e)


def _dpapi_machine(data: bytes, name: str) -> bytes:
    """Encrypt the configuration in the WireGuard app format (<name>.conf.dpapi): DPAPI with
    description = tunnel name. CRYPTPROTECT_LOCAL_MACHINE so the WireGuard manager (SYSTEM)
    can read the file - this way the tunnel appears in the app immediately, without a manager restart."""
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = BLOB()
    flags = 0x1 | 0x4                                   # UI_FORBIDDEN | LOCAL_MACHINE
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(blob_in), ctypes.c_wchar_p(name), None, None, None,
                                                  flags, ctypes.byref(blob_out)):
        raise OSError(ctypes.GetLastError(), "CryptProtectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


class _Lock:
    """Only ONE setup at a time (background sync and "Activate in PC app" run in
    different processes - two simultaneous installs produced "tunnel already exists")."""

    def __init__(self, data_dir: Path):
        self.path = _conf_dir(data_dir) / ".install.lock"
        self.fd = None

    def __enter__(self):
        import time as _t
        for _ in range(240):                                    # wait up to 2 min
            try:
                self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                try:
                    if _t.time() - self.path.stat().st_mtime > 300:   # stale lock left behind
                        self.path.unlink()
                        continue
                except OSError:
                    pass
                _t.sleep(0.5)
        raise RuntimeError("Another VPN setup is still running - please try again shortly.")

    def __exit__(self, *exc):
        try:
            os.close(self.fd)
        finally:
            try:
                self.path.unlink()
            except OSError:
                pass


def siblings(iface: str) -> list[str]:
    """Other WireGuard tunnels of the SAME RMM tunnel on this PC (name "r" + tunnel ID + participant ID).
    Typical: the agent also runs on this PC and is already a participant. Both tunnels would create the
    same route (e.g. 10.77.0.0/24) - Windows then reports "The object already exists"."""
    if len(iface) < 9 or not iface.startswith("r"):
        return []
    prefix = iface[:9]                                   # "r" + 8 chars of the tunnel ID
    out = []
    try:
        if IS_WINDOWS:
            r = subprocess.run(["sc", "query", "type=", "service", "state=", "all"], capture_output=True, text=True,
                               timeout=20, creationflags=0x08000000)
            for line in (r.stdout or "").splitlines():
                line = line.strip()
                if line.upper().startswith(("SERVICE_NAME:", "DIENSTNAME:")) and "WireGuardTunnel$" in line:
                    name = line.split("WireGuardTunnel$", 1)[1].strip()
                    if name.startswith(prefix) and name != iface:
                        out.append(name)
        else:
            out = [p.name for p in Path("/sys/class/net").glob(prefix + "*") if p.name != iface]
    except Exception:
        pass
    return out


def service_running(iface: str) -> bool:
    try:
        if IS_WINDOWS:
            r = subprocess.run(["sc", "query", "WireGuardTunnel$" + iface], capture_output=True, text=True,
                               timeout=10, creationflags=0x08000000)
            return r.returncode == 0 and "RUNNING" in (r.stdout or "")
        return Path(f"/sys/class/net/{iface}").exists()
    except Exception:
        return False


class SameDeviceError(RuntimeError):
    pass


def install_locked(data_dir: Path, store, iface: str, conf: str, force: bool = False) -> bool:
    """Set up with a lock; afterwards checks again whether another process already did it.
    Returns: True = set up, False = was already up to date."""
    h = hashlib.sha256(conf.encode()).hexdigest()[:16]
    with _Lock(data_dir):
        try:
            store.reload()
        except Exception:
            pass
        managed = dict(store.get("vpn_managed", {}) or {})
        if managed.get(iface) == h and (not force or service_running(iface)):
            # same config and the tunnel is running -> do NOT set up again (a second setup
            # removed the running tunnel and then failed on its still-present adapter)
            return False
        sib = siblings(iface)
        if sib:
            if iface in managed:                         # remove remnants of an earlier attempt
                try:
                    uninstall(data_dir, iface)
                except Exception:
                    pass
                managed.pop(iface, None)
                store.set(vpn_managed=managed)
            raise SameDeviceError(
                "This PC is already a participant of this tunnel (the agent on this PC, tunnel " + sib[0] + "). "
                "You are therefore already in the VPN here and can reach the other participants - a second access on "
                "the same PC is not possible (same network). Use a different PC for your own access.")
        install(data_dir, iface, conf)
        try:
            store.reload()
        except Exception:
            pass
        managed = dict(store.get("vpn_managed", {}) or {})
        managed[iface] = h
        store.set(vpn_managed=managed)
        return True


def uninstall_locked(data_dir: Path, store, iface: str) -> None:
    with _Lock(data_dir):
        uninstall(data_dir, iface)
        try:
            store.reload()
        except Exception:
            pass
        managed = dict(store.get("vpn_managed", {}) or {})
        managed.pop(iface, None)
        store.set(vpn_managed=managed)


def install(data_dir: Path, iface: str, conf: str) -> None:
    path = _conf_dir(data_dir) / f"{iface}.conf"
    path.write_text(conf, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    exe = wg_exe()
    if IS_WINDOWS:
        # Encrypted for the WireGuard app (immediately in its list), then with admin rights:
        # remove the old service only if it exists, wait until it is REALLY gone (otherwise
        # "object already exists"), place the file in the app store and start the tunnel.
        import re as _re
        _m = _re.search(r"^Address\s*=\s*([0-9.]+)", conf, _re.M)
        ip = _m.group(1) if _m else ""
        _m = _re.search(r"^AllowedIPs\s*=\s*([^,\s]+)", conf, _re.M)
        route = _m.group(1) if _m else ""
        dp = _conf_dir(data_dir) / f"{iface}.conf.dpapi"
        dp.write_bytes(_dpapi_machine(conf.encode("utf-8"), iface))
        code, out = _elevated([
            f"$exe = '{exe}'",
            f"$n = '{iface}'",
            f"$src = '{dp}'",
            "$d = Join-Path $env:ProgramFiles 'WireGuard\\Data\\Configurations'",
            "New-Item -ItemType Directory -Force -Path $d | Out-Null",
            "function Svc { Get-Service -Name ('WireGuardTunnel$' + $n) -ErrorAction SilentlyContinue }",
            "if (Svc) {",
            "  & $exe /uninstalltunnelservice $n | Out-Null",
            "  for ($i = 0; $i -lt 20 -and (Svc); $i++) { Start-Sleep -Milliseconds 500 }",
            "  if (Svc) {",
            "    # the service is 'marked for deletion' as long as the WireGuard manager keeps it open",
            "    Restart-Service -Name 'WireGuardManager' -Force -ErrorAction SilentlyContinue",
            "    for ($i = 0; $i -lt 20 -and (Svc); $i++) { Start-Sleep -Milliseconds 500 }",
            "  }",
            "}",
            "if (Svc) { Write-Output 'The old WireGuard tunnel service cannot be removed - please restart the computer.'; exit 4 }",
            "# orphaned network adapter of the same name (crashed tunnel) -> otherwise 'The object already exists'",
            "for ($i = 0; $i -lt 10; $i++) {",
            "  $a = Get-NetAdapter -Name $n -IncludeHidden -ErrorAction SilentlyContinue",
            "  if (-not $a) { break }",
            "  if ($i -ge 4 -and $a.PnPDeviceID) { pnputil /remove-device \"$($a.PnPDeviceID)\" | Out-Null }",
            "  Start-Sleep -Milliseconds 700",
            "}",
            "# Old RMM tunnels occupying the SAME address (e.g. from an earlier, long-deleted",
            "# RMM tunnel; older app versions created them without an entry in the WireGuard list) -> remove.",
            "# Otherwise: 'unable to set ips: The object already exists'.",
            f"$ip = '{ip}'",
            "if ($ip) {",
            "  foreach ($x in @(Get-NetIPAddress -IPAddress $ip -ErrorAction SilentlyContinue | Where-Object { $_.InterfaceAlias -ne $n })) {",
            "    $al = $x.InterfaceAlias",
            "    if ($al -like 'rmv*' -or $al -match '^r[0-9a-f]{14}$' -or $al -like 'rmt*') {",
            "      Write-Output ('Removing old RMM tunnel ' + $al + ' (occupies ' + $ip + ')')",
            "      if (Get-Service -Name ('WireGuardTunnel$' + $al) -ErrorAction SilentlyContinue) { & $exe /uninstalltunnelservice $al | Out-Null }",
            "      for ($i = 0; $i -lt 20 -and (Get-Service -Name ('WireGuardTunnel$' + $al) -ErrorAction SilentlyContinue); $i++) { Start-Sleep -Milliseconds 500 }",
            "      Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $d ($al + '.conf')), (Join-Path $d ($al + '.conf.dpapi')), (Join-Path (Join-Path $env:ProgramData 'Bytera') ($al + '.conf')), (Join-Path (Join-Path $env:ProgramData 'Rapalle') ($al + '.conf'))",
            "      for ($i = 0; $i -lt 10; $i++) { $a = Get-NetAdapter -Name $al -IncludeHidden -ErrorAction SilentlyContinue; if (-not $a) { break }; if ($i -ge 4 -and $a.PnPDeviceID) { pnputil /remove-device \"$($a.PnPDeviceID)\" | Out-Null }; Start-Sleep -Milliseconds 700 }",
            "    } else {",
            "      Write-Output ('The address ' + $ip + ' is already used on this PC by \"' + $al + '\" (not an RMM tunnel) - please disconnect it there.')",
            "      exit 6",
            "    }",
            "  }",
            "}",
            "Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $d ($n + '.conf')), (Join-Path $d ($n + '.conf.dpapi'))",
            "$dst = Join-Path $d ($n + '.conf.dpapi')",
            "Copy-Item -Force -LiteralPath $src -Destination $dst",
            "# The WireGuard manager (running when the WireGuard app is open) notices the new file and",
            "# restarts a RUNNING tunnel for it ('activated - deactivated - already exists').",
            "# So first wait until it has processed the file, then start.",
            "if (Get-Process -Name 'wireguard' -ErrorAction SilentlyContinue) { Start-Sleep -Seconds 4 } else { Start-Sleep -Milliseconds 500 }",
            "& $exe /installtunnelservice $dst | Out-Null",
            "for ($i = 0; $i -lt 20 -and -not (Svc); $i++) { Start-Sleep -Milliseconds 500 }",
            "if (-not (Svc)) { Write-Output 'The WireGuard tunnel service was not created.'; exit 3 }",
            "# Wait up to 25 s for 'Running' (StartPending is normal, e.g. while the endpoint name is resolved);",
            "# only an error once the service is stopped/gone or the time has run out.",
            "$s = $null",
            "for ($i = 0; $i -lt 50; $i++) {",
            "  $s = Svc",
            "  if ($s -and $s.Status -eq 'Running') { break }",
            "  if ($i -ge 6 -and (-not $s -or $s.Status -eq 'Stopped')) { break }",
            "  Start-Sleep -Milliseconds 500",
            "}",
            "if ($s -and $s.Status -eq 'Running') { Write-Output ('Tunnel running (' + $n + ')') }",
            "if (-not $s -or $s.Status -ne 'Running') {",
            "  Write-Output ('Tunnel start from the WireGuard store failed (status: ' + $(if ($s) { $s.Status } else { 'no service' }) + ').')",
            "  Write-Output '--- WireGuard log (end) ---'",
            "  & $exe /dumplog 2>&1 | Select-Object -Last 25 | ForEach-Object { Write-Output $_ }",
            "  Write-Output '--- Network ---'",
            "  Get-NetAdapter -IncludeHidden -ErrorAction SilentlyContinue | Where-Object { $_.InterfaceDescription -like '*WireGuard*' -or $_.InterfaceDescription -like '*Wintun*' } | ForEach-Object { Write-Output ('Adapter: ' + $_.Name + ' / ' + $_.Status + ' / ' + $_.InterfaceDescription) }",
            f"  Get-NetIPAddress -IPAddress '{ip}' -ErrorAction SilentlyContinue | ForEach-Object {{ Write-Output ('IP {ip} already assigned on: ' + $_.InterfaceAlias) }}",
            f"  Get-NetRoute -DestinationPrefix '{route}' -ErrorAction SilentlyContinue | ForEach-Object {{ Write-Output ('Route {route} already present on: ' + $_.InterfaceAlias) }}",
            "  # fallback: plaintext config outside the WireGuard store (like a manual import)",
            "  if (Svc) { & $exe /uninstalltunnelservice $n | Out-Null; for ($i = 0; $i -lt 20 -and (Svc); $i++) { Start-Sleep -Milliseconds 500 } }",
            "  $alt = Join-Path $env:ProgramData 'Bytera'",
            "  New-Item -ItemType Directory -Force -Path $alt | Out-Null",
            "  $alt = Join-Path $alt ($n + '.conf')",
            f"  Copy-Item -Force -LiteralPath '{path}' -Destination $alt",
            "  icacls $alt /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null",
            "  & $exe /installtunnelservice $alt | Out-Null",
            "  for ($i = 0; $i -lt 50; $i++) { $s = Svc; if ($s -and $s.Status -eq 'Running') { break }; if ($i -ge 6 -and (-not $s -or $s.Status -eq 'Stopped')) { break }; Start-Sleep -Milliseconds 500 }",
            "  if ($s -and $s.Status -eq 'Running') { Write-Output 'FALLBACK-OK'; exit 0 }",
            "  Write-Output 'The fallback (plaintext file) does not start either.'",
            "  & $exe /dumplog 2>&1 | Select-Object -Last 12 | ForEach-Object { Write-Output $_ }",
            "  exit 5",
            "}",
            "exit 0",
        ])
        try:
            dp.unlink()
        except OSError:
            pass
    else:
        code, out = _elevated([f"install -m 600 '{path}' /etc/wireguard/{iface}.conf",
                               f"wg-quick down {iface} 2>/dev/null", f"wg-quick up {iface}"])
    try:                                                  # log for troubleshooting (shown in the VPN area)
        import time as _t
        with open(_conf_dir(data_dir) / "vpn.log", "a", encoding="utf-8") as f:
            f.write(f"\n=== {_t.strftime('%Y-%m-%d %H:%M:%S')} install {iface} -> {code}\n{out.strip()}\n")
    except OSError:
        pass
    if code != 0:
        raise RuntimeError((out.strip()[-2500:] or f"Error {code}") + f" (code {code})")
    if "FALLBACK-OK" in out:
        LAST_NOTE[iface] = "running (fallback: not in the WireGuard list - diagnostics in vpn.log)"


LAST_NOTE: dict[str, str] = {}


def uninstall(data_dir: Path, iface: str) -> None:
    exe = wg_exe()
    if exe:
        if IS_WINDOWS:
            _elevated([f"$exe = '{exe}'", f"$n = '{iface}'",
                       "if (Get-Service -Name ('WireGuardTunnel$' + $n) -ErrorAction SilentlyContinue) { & $exe /uninstalltunnelservice $n | Out-Null }",
                       "$d = Join-Path $env:ProgramFiles 'WireGuard\\Data\\Configurations'",
                       f"Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $d '{iface}.conf'), (Join-Path $d '{iface}.conf.dpapi'), (Join-Path (Join-Path $env:ProgramData 'Bytera') '{iface}.conf'), (Join-Path (Join-Path $env:ProgramData 'Rapalle') '{iface}.conf')"])
        else:
            _elevated([f"wg-quick down {iface} 2>/dev/null", f"rm -f /etc/wireguard/{iface}.conf"])
    try:
        (_conf_dir(data_dir) / f"{iface}.conf").unlink()
    except OSError:
        pass


import socket as _socket
import urllib.parse as _uparse


def vpn_my_path(store) -> str:
    """GET path for /api/vpn/my with a device identifier, so EACH device gets its
    own VPN address (several devices on the same account = one IP each)."""
    try:
        dev = (store.get("device_id") or "").strip()
    except Exception:
        dev = ""
    if not dev:
        return "/api/vpn/my"
    try:
        name = (store.get("device_name") or _socket.gethostname() or "").strip()
    except Exception:
        name = ""
    return "/api/vpn/my?" + _uparse.urlencode({"device_id": dev, "device_name": name})


class VpnSync:
    """Background sync. get_api() returns the current Api object (or None)."""

    def __init__(self, store, data_dir: Path, get_api, on_status=None, interval: int = 60, refresh=None):
        self.store, self.data_dir, self.get_api = store, Path(data_dir), get_api
        self.refresh = refresh            # renew the session (device token) when the old one has expired
        self.on_status = on_status or (lambda text: None)
        self.interval = interval
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._failed: dict[str, float] = {}     # "<iface>:<hash>" -> time; do not retry failed ones every minute

    def managed(self) -> dict:
        return dict(self.store.get("vpn_managed", {}) or {})          # iface -> hash

    def _set_managed(self, m: dict) -> None:
        self.store.set(vpn_managed=m)

    def start(self):
        if self._thread and self._thread.is_alive():
            self._wake.set()
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="rmm-vpn-sync")
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def now(self):
        self._wake.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.sync()
            except Exception as e:  # noqa: BLE001
                self.on_status(f"VPN: {e}")
            self._wake.wait(self.interval)
            self._wake.clear()

    def sync(self):
        try:
            self.store.reload()                 # pick up changes from other app processes (link starter)
        except Exception:
            pass
        auto_on = bool(self.store.get("vpn_auto", True))
        if not auto_on and not (self.store.get("vpn_manual") or self.managed()):
            return
        ok, why = supported()
        api = self.get_api()
        if api is None:
            return
        from .api import ApiError
        try:
            data = api.request("GET", vpn_my_path(self.store))
        except ApiError as e:
            if e.status == 404:
                return                                   # module "vpn" is off on the server
            if e.status != 401 or not self.refresh:
                raise
            self.refresh()
            data = self.get_api().request("GET", vpn_my_path(self.store))
        self._cleanup_legacy()
        manual = set(self.store.get("vpn_manual", []) or [])          # activated via the dashboard button
        off = set(self.store.get("vpn_off", []) or [])                 # deliberately disconnected in the VPN area
        want = {t["iface"]: t for t in data.get("tunnels", [])
                if ((auto_on and t.get("auto", True)) or t["iface"] in manual) and t["iface"] not in off}
        gone = manual - {t["iface"] for t in data.get("tunnels", [])}
        if gone:
            self.store.set(vpn_manual=sorted(manual - gone))
        have = self.managed()
        if not ok:
            if want:
                self.on_status(f"VPN: {len(want)} tunnels available - {why}")
            return
        changed = False
        for iface in [i for i in have if i not in want]:          # expired/removed/disconnected -> delete
            uninstall_locked(self.data_dir, self.store, iface)   # pflegt vpn_managed selbst
            have.pop(iface, None)
        errors = []
        for iface, t in want.items():
            h = hashlib.sha256(t["conf"].encode()).hexdigest()[:16]
            if have.get(iface) == h:
                continue
            if iface in manual:
                # manually activated tunnels are set up ONLY by the VPN area/link starter - otherwise the
                # background sync would start a second pass that tore down the first tunnel again.
                continue
            import time as _t
            key = f"{iface}:{h}"
            if _t.time() - self._failed.get(key, 0) < 900:      # 15 min pause (otherwise constant UAC/password prompts)
                errors.append(f"{t['name']}: setup failed - will retry later")
                continue
            try:
                install_locked(self.data_dir, self.store, iface, t["conf"])   # pflegt vpn_managed selbst
                have[iface] = h
                self._failed.pop(key, None)
            except Exception as e:  # noqa: BLE001
                self._failed[key] = _t.time()
                errors.append(f"{t['name']}: {str(e).strip().splitlines()[-1][:160] if str(e).strip() else e}")
        names = ", ".join(t["name"] for t in want.values())
        self.on_status(("VPN error: " + "; ".join(errors)) if errors else
                       (f"VPN connected: {names}" if want else "No VPN tunnel active"))

    def _cleanup_legacy(self):
        """Remove tunnels of older app versions ("rmv<tunnel ID>") - the backend no longer uses these names;
        they may have kept running invisibly (not in the WireGuard list) and occupied address and route."""
        if not IS_WINDOWS:
            return
        import time as _t
        if _t.time() - self._failed.get("legacy", 0) < 900:
            return
        try:
            r = subprocess.run(["sc", "query", "type=", "service", "state=", "all"], capture_output=True, text=True,
                               timeout=20, creationflags=0x08000000)
        except Exception:
            return
        old = []
        for line in (r.stdout or "").splitlines():
            if "WireGuardTunnel$rmv" in line:
                old.append(line.split("WireGuardTunnel$", 1)[1].strip())
        if not old:
            return
        exe = wg_exe()
        if not exe:
            return
        lines = [f"$exe = '{exe}'", "$d = Join-Path $env:ProgramFiles 'WireGuard\\Data\\Configurations'"]
        for n in old:
            lines += [f"& $exe /uninstalltunnelservice {n} | Out-Null",
                      f"Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $d '{n}.conf'), (Join-Path $d '{n}.conf.dpapi')"]
        code, _ = _elevated(lines)
        if code != 0:
            self._failed["legacy"] = _t.time()
        managed = dict(self.store.get("vpn_managed", {}) or {})
        for n in old:
            managed.pop(n, None)
        self.store.set(vpn_managed=managed)
        self.on_status(f"Old VPN tunnels removed: {', '.join(old)}")

    def remove_all(self):
        for iface in list(self.managed()):
            uninstall(self.data_dir, iface)
        self._set_managed({})
        self.store.set(vpn_manual=[])
