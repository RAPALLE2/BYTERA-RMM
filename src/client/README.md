# BYTERA RMM Client

The RMM dashboard as a standalone app for **Windows 10/11, Linux and Android** – with no dependencies for the user.
The ready-made packages live in `release/` and are offered by the **"Client app" module**
(Profile → *Client app & devices*), just like the agent installation:

| Way | Windows 10/11 | Linux | Android 6+ |
|---|---|---|---|
| **Command** | `irm <server>/client-dist/install.ps1 \| iex` (PowerShell) or `install.cmd` (CMD) | `curl -fsSL <server>/client-dist/install.sh \| sh` | – |
| **Package** | `.zip` (exe to extract) | `.tar.gz`, `.tgz`, `.tar` (with `install.sh`) | – |
| **File** | `.exe` (runs directly), `.msi` (installation, GPO/Intune) | `.deb` (Debian/Ubuntu), `.rpm` (Fedora/RHEL/openSUSE), `.pkg.tar.xz` (Arch) | `.apk` |

The commands download the matching package, create start menu/desktop shortcuts and enter the
server address right away – you only have to sign in.

## Features

| | Windows | Linux | Android |
|---|---|---|---|
| Dashboard as an app window ("small browser") | Edge in app mode (always available) | Chromium/Chrome/Edge/Brave in app mode, otherwise Firefox | built-in WebView |
| Remember login (device token, no password) | Windows Credential Manager | keyring (`secret-tool`), otherwise a file (0600) | private app storage |
| 2FA / SSO (AD) | Yes | Yes | Yes |
| Relay as a drive | network drive (`net use`, WebDAV) | file manager (`gio mount`) | – (Android does not allow this; Explorer in the dashboard) |
| Remote shell in its own window | Yes | Yes | in the app window |
| Remote screen via Remote Desktop / VNC | mstsc / VNC viewer | FreeRDP/Remmina / vncviewer | "Remote Desktop" / bVNC |
| Remote-screen target selectable (gateway, local IP, last, custom + port) | Yes | Yes | Yes |
| Autostart | Yes | Yes | – |
| Sign out device (Profile → My devices) | immediately | immediately | immediately |
| WireGuard VPN ("VPN" module) | fully automatic: create tunnel, connect, delete on expiry (WireGuard for Windows, UAC prompt) | fully automatic (wireguard-tools, pkexec) | QR code/file from the VPN app into the WireGuard app |

## Updates
- **Automatic:** after signing in, the app checks whether a newer version is on the server and installs it
  (checkbox "Update automatically", on by default). Android asks and downloads the new APK via the browser.
- **Manual:** button "Check for updates" (Android: menu).
- The app detects its own installation type: single `.exe` (replace + restart), `.msi` under "Programs"
  (silent MSI update), `.deb`/`.rpm`/`.pkg.tar.xz` (admin prompt via `pkexec`: apt / dnf·zypper·yum / pacman),
  `.tar.gz` (replace files, without admin).
  For this, installed packages contain the file `.bytera-install`; a start from the source gets **no** auto-updates.

## Open externally from the dashboard (from 1.4)
When the dashboard runs in the client app window, an agent's **Terminal** and **Remote screen** open the matching
software locally: Remote Desktop (mstsc/xfreerdp), VNC viewer, SSH client in the terminal, or the RMM terminal in its own
app window. In a normal browser everything stays as before (web variant). Profile → "Client app & devices": externally by
default, in the browser, or ask every time; Shift+click always shows the choice.
- Target: local IP + default port, custom address/port, RMM gateway or **via RMM (Cloudflare tunnel)** – then the
  backend ("Cloudflare Tunnel" + "Network" modules) creates a tunnel address with its own service token and forwards to the
  agent; the app connects via `cloudflared access tcp` (downloads cloudflared once itself) on `127.0.0.1`.
  Open: until end of use, 10/30/60/120 minutes or permanent (permission "Relay unlimited").
- Mechanics: link scheme `bytera-rmm://launch?s=<server>&t=<launch-ticket>` (Windows: HKCU\Software\Classes, Linux:
  `x-scheme-handler/bytera-rmm`). The launch ticket is valid for 2 minutes, once, only for the signed-in user –
  so external pages cannot start anything. Android: JS bridge `RMM.launch(ticket)` (without a tunnel).

## Build packages on the server (like the agent packages)
*Modules → Client app → Settings → "Build client packages"* builds everything directly from `src/client` and puts the
packages into `backend/storage/client/` (takes precedence over the shipped ones, survives server updates). Beforehand you choose:
**Keep version** (existing packages of this version are overwritten – installed apps then do not update)
or **new version** (older packages are removed, installed apps fetch the update automatically). Live log and "Stop" included.

**Docker operation (all formats):** when the module is enabled, the RMM creates the build image once,
`bytera/rmm-client-builder:<tag>` (Ubuntu 24.04 + Wine + Windows Python with PyInstaller + wixl + rpmbuild + JDK 17 +
Android tools, about 1.5–2 GB; progress in the module settings). Each build runs in a throwaway container
without network. The RMM image itself stays small; module off → the build image is removed. If the image is missing (e.g. after
`docker image prune`), the module recreates it on the next start; "Re-prepare" forces it.
Requirement: the Docker socket in the RMM container (`/var/run/docker.sock`, set like this in `docker-compose.yml`).

**Without Docker (native):** Linux builds `.deb/.tar/.tar.gz/.tgz/.pkg.tar.xz` (+ `.rpm`, `.exe/.msi`, `.apk` if the tools
are installed), Windows additionally `.exe/.zip/.msi` (PyInstaller via pip and WiX as a download are fetched by the module on enable).

Manually: `python3 src/client/build_installers.py [--targets exe,msi,…] [--out DIR] [--set-version 1.2.3]`.

## RMM console
The **"RMM console"** button (Android: menu) opens the RMM command line ("RMM console" module, "Use RMM console" permission):
devices, live metrics (`top`, btop-like), terminal on devices (`shell`), commands (`exec`), processes, restart/shutdown,
restart agent, files, Docker – with tab completion. The same console is available via SSH (`ssh <user>@<server> -p 2222`)
and in the dashboard.

## Control window
- Every longer-running action shows a progress bar at the bottom with **"Stop"** (aborts commands/downloads).
- **"Close window when the dashboard opens":** closes the control window 4 s after the dashboard opens –
  with a "Keep open" button.

## Remote screen: where to connect?
After choosing the device, the app asks where to connect – the stored default is preselected,
otherwise the **local IP of the device**:

| Target | Port |
|---|---|
| **RMM gateway** – the server forwards, reachable only from your IP (needs the "Network" module + a running agent) | automatic |
| **Local IP of the device** – as reported by the agent (same network / VPN into the company network) | default (3389 / 5900) or custom |
| **Last connection** – exactly like last time | like last time |
| **Custom address** – IP or name (VPN, port forward, DNS) | default or custom |

"Remember as the default for this device" stores the choice **per user and target device** (and separately for RDP/VNC) on
the server – so it applies on all devices where you use the app. Direct connections do not need the agent to be
online, which is why the remote-screen selection also shows offline devices.

## Structure

```
src/client/
├── build_installers.py   builds ALL packages (exe, zip, msi, deb, rpm, pkg, tar, tgz, apk) – like src/backend/app/build_installers.py for the agent
├── desktop/              Windows + Linux: pure Python (standard library only, tkinter UI)
│   ├── bytera-client.py         entry file
│   └── bytera_client/           api.py, store.py, secrets_store.py, browser.py, features.py, autostart.py, gui.py
├── android/              native Android app (Java, WebView) - AndroidManifest.xml, src/, assets/login.html, res/
│   └── signing/          permanent signing key (updates must be signed the same way!)
├── resources/            program icons (from the RMM logo)
├── release/              READY PACKAGES (these are shipped)
└── tests/test_logic.py   tests (+ optionally against a running backend)
```

## Rebuild

On a Linux build system (e.g. Ubuntu 24.04) – builds everything the installed tools allow:

```bash
sudo apt install wine64 wixl openjdk-17-jdk-headless android-sdk-platform-23 aapt apksigner zipalign dalvik-exchange
# Windows Python with PyInstaller for Wine (once), e.g. to /opt/winpy:
#   https://github.com/astral-sh/python-build-standalone  (…-x86_64-pc-windows-msvc-install_only.tar.gz)
#   wine64 /opt/winpy/python/python.exe -m pip install pyinstaller
python3 src/client/build_installers.py                 # everything
python3 src/client/build_installers.py --targets deb,tar,apk   # only individual ones
```

On **Windows**, `.exe`, `.zip` (and with wixl/WiX `.msi`) can be built directly:
`py -m pip install pyinstaller && py src\client\build_installers.py --targets exe,zip`.
Alternatively, the GitHub workflow **"Build client app"** (Actions) builds all packages automatically.

Change the version: `desktop/bytera_client/__init__.py` (`__version__`). Android picks it up when building.

Tests: `cd src/client && python3 -m pytest tests`
(against a backend additionally `RMM_TEST_SERVER=… RMM_TEST_USER=… RMM_TEST_PASS=…`).

## Notes
- **Windows SmartScreen:** the `.exe`/`.msi` are not signed with a purchased code-signing certificate –
  on first start "More info → Run anyway".
- **Windows + HTTP:** by default Windows connects WebDAV network drives only over HTTPS
  (otherwise `BasicAuthLevel = 2` under `HKLM\SYSTEM\CurrentControlSet\Services\WebClient\Parameters`).
- **Android:** allow installation from "unknown sources". The signing key is in
  `android/signing/` – do not lose it and do not change it, otherwise updates cannot be installed over old versions.
- **Linux:** needs `python3` and `python3-tk` (the `.deb` and the command install them with it);
  for the drive `gio` (present on GNOME/KDE/XFCE).
