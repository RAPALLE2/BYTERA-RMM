"""
routers/enrollment_routes.py
------------------------------
Covers the 3 desired ways to add a new client:

  1. Download page with a link (+ step-by-step code next to it)
  2. Ready-made wget/curl one-liner (installs the agent incl. autostart)
  3. Manual ZIP download

Flow: the admin clicks "Add client" in the dashboard (optionally with a
preselected tenant/location) -> the frontend calls POST /api/enrollment/tokens
-> gets a token back -> then shows the landing page URL, the
install.sh/install.ps1 URLs and the plain download link.

These routes are deliberately NOT under /api and need NO login token, because
they are called from a brand-new machine without access to the dashboard - the
security comes instead from the random, hard-to-guess token in the URL itself.
"""

import io
import ipaddress
import zipfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import re as _re
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from app import bgjobs, db
from app.auth import get_current_user, require_admin, require_perm
from app.config import AGENT_TOKEN
# Language of the generated scripts: they run LATER on a foreign machine -
# where there is neither login nor server setting. The language must therefore
# be fixed at generation time (?lang=de|en).
from app.i18n import t as _t, _norm as _lang, server_lang

router = APIRouter(tags=["enrollment"])

# Path to the agent source code folder (packed for the ZIP download)
from app.paths import (
    AGENT_DIR as AGENT_SOURCE_DIR, PROJECT_ROOT as _PROJ_ROOT, BRANDING_DIR as _BRANDING,
    DIST_DIR as _DIST, APP_DIR as _APP_DIR,
)


class CreateEnrollmentBody(BaseModel):
    tenant_id: str | None = None
    location_id: str | None = None
    client_name: str | None = None


@router.post("/api/enrollment/tokens")
def create_enrollment_token(body: CreateEnrollmentBody, request: Request, user: dict = Depends(get_current_user)):
    """
    Creates a new onboarding token and returns ready-made URLs for it.

    IMPORTANT: the URLs are built ABSOLUTELY here (via _backend_url: settings
    server_url/server_domain/server_host take precedence). Previously the
    frontend prepended window.location.origin - whoever opens the dashboard via
    localhost/127.0.0.1 got unusable "localhost" links that way.
    """
    # Own permission instead of forced admin: 'add_client' allows adding new
    # clients without granting other admin privileges.
    require_perm(user, "add_client")
    token = db.create_enrollment_token(body.tenant_id, body.location_id, body.client_name)
    base = _backend_url(request)
    return {
        "token": token,
        "base_url": base,
        "landing_url": f"{base}/enroll/{token}",
        "install_sh_url": f"{base}/enroll/{token}/install.sh",
        "install_ps1_url": f"{base}/enroll/{token}/install.ps1",
        "download_url": f"{base}/enroll/{token}/agent.zip",
    }


def _require_valid_token(token: str) -> dict:
    row = db.get_enrollment_token(token)
    if not row:
        raise HTTPException(404, "Unknown or expired onboarding link")
    return row


def _ensure_scheme(url: str) -> str:
    """
    Ensures that a URL has a scheme. Without 'http(s)://' neither
    Invoke-WebRequest (Windows) nor curl (Linux) can establish a connection -
    exactly what the client install fails at when the 'Server URL' field holds
    only a bare IP/domain.
    """
    url = url.strip().rstrip("/")
    if url and "://" not in url:
        url = "http://" + url
    return url


def _configured_backend_port(default: int = 4000) -> int:
    try:
        return int(db.get_setting("server_backend_port"))
    except (TypeError, ValueError):
        return default


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _forwarded_scheme(request: Request) -> str:
    """Scheme under which the client REALLY reached us.

    Behind a reverse proxy (nginx, Traefik, Cloudflare) the request arrives
    internally as http, but from outside it was https. Without this evaluation
    one builds addresses that only work on the internal network. If the header
    is missing entirely, https is the safe assumption for a domain - a publicly
    reachable RMM server without TLS would be a problem anyway.
    """
    fproto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    if fproto in ("http", "https"):
        return fproto
    scheme = (request.url.scheme or "").strip()
    return scheme if scheme in ("http", "https") else "https"


def _update_target(request: Request) -> str:
    """Address that the agent UPDATE scripts write into the .env.

    Normally the own address. During a server move (setting
    "migration_target_url", set under Source -> Migration -> "Move agents") the
    address of the NEW server: every agent that updates itself then connects to
    the new backend automatically - without a prior agent update.
    """
    target = (db.get_setting("migration_target_url") or "").strip()
    return _ensure_scheme(target).rstrip("/") if target else _backend_url(request)


def _migration_token() -> str:
    """Optional: AGENT_TOKEN of the new server (only needed if the secrets were
    NOT carried over during the move)."""
    if not (db.get_setting("migration_target_url") or "").strip():
        return ""
    tok = (db.get_setting("migration_agent_token") or "").strip()
    return tok if all(c.isalnum() or c in "-_.~" for c in tok) else ""


def _backend_url(request: Request) -> str:
    """
    Returns the base address that is built into install commands/scripts.

    Order:
      1. Full URL (server_url) - takes precedence if set.
      2. Domain or IP/host + backend port (from the general settings).
      3. Derived automatically from the request (often gave "localhost" before).
    """
    full = (db.get_setting("server_url") or "").strip()
    if full:
        # Add a missing scheme (otherwise Invoke-WebRequest/curl cannot connect).
        full = _ensure_scheme(full)
        parts = urlsplit(full)
        has_port = parts.port is not None
        has_path = parts.path not in ("", "/")
        # Most common config mistake: the "Server URL" field holds only a bare
        # IP (e.g. 10.10.32.249) without a port -> would otherwise land on port
        # 80, but the backend listens on 4000. For a BARE IP without port/path
        # we therefore add the configured backend port automatically. Domains
        # stay untouched (there is practically always a reverse proxy behind
        # them), as do URLs already given with port/path and https://.
        if (parts.scheme == "http" and not has_port and not has_path
                and parts.hostname and _is_ip_literal(parts.hostname)):
            bport = _configured_backend_port()
            if bport not in (0, 80):
                full = urlunsplit((parts.scheme, f"{parts.hostname}:{bport}", "", "", ""))
        return full.rstrip("/")

    domain = (db.get_setting("server_domain") or "").strip()
    host = (db.get_setting("server_host") or "").strip()
    target = domain or host
    if target:
        # Remove any scheme that was entered too (we build it ourselves).
        target = target.replace("https://", "").replace("http://", "").rstrip("/")
        try:
            port = int(db.get_setting("server_backend_port"))
        except (TypeError, ValueError):
            port = 4000

        # If this is a DOMAIN (not a bare IP), there is practically always a
        # reverse proxy in front - mandatory with Cloudflare, because Cloudflare
        # only proxies 80/443 and a few alternative ports; 4000 is NOT among
        # them.
        #
        # Previously this stubbornly built "http://domain:<backend_port>". With
        # the default combination server_domain=my.domain + backend_port=4000
        # that gave http://my.domain:4000 - an address that is NEVER reachable
        # from the internet. The installer ran through anyway (it was loaded via
        # the correct address), the agent got the broken URL into its .env and
        # never appeared in the dashboard.
        #
        # Therefore: for a domain the INTERNAL backend port is ignored and the
        # scheme is taken from the running request (every common proxy sets
        # X-Forwarded-Proto, Cloudflare included).
        if not _is_ip_literal(target.split(":")[0]):
            if ":" in target:
                # The admin specified a port themselves - that one applies.
                return f"{_forwarded_scheme(request)}://{target}"
            return f"{_forwarded_scheme(request)}://{target}"

        # Bare IP: there is no proxy here, the port must be included.
        scheme = "https" if port == 443 else "http"
        if port in (80, 443):
            return f"{scheme}://{target}"
        return f"{scheme}://{target}:{port}"

    # 3. Derive automatically from the request - respecting reverse-proxy
    #    headers. If the backend runs behind a proxy (https://domain ->
    #    http://ip:4000), the proxy usually sets X-Forwarded-Proto/Host.
    #    Without these headers request.base_url would see only the internal
    #    address (http://ip:4000), which a public client CANNOT reach.
    fproto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    fhost = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    host = fhost or request.headers.get("host") or request.url.netloc
    scheme = fproto or request.url.scheme or "http"
    if host:
        return f"{scheme}://{host}".rstrip("/")
    return str(request.base_url).rstrip("/")


@router.get("/api/public-base")
def public_base(request: Request, user: dict = Depends(get_current_user)):
    """
    The CANONICAL address of this installation - i.e. the one at which clients
    and network drives should reach the server.

    Why this cannot be determined in the browser: `location.host` is only the
    address through which the admin HAPPENS to access it right now. Whoever
    opens the dashboard internally via IP would get a WebDAV address in the
    relay manager that nobody reaches from outside - and install commands that
    the new client does not know. The same order as for the install scripts
    applies here (see _backend_url): configured server URL, then domain/host +
    port, then the request itself (including X-Forwarded-*).

    Deliberately only `get_current_user` and no admin permission: ordinary users
    see relay addresses too and therefore need the correct value.
    """
    base = _backend_url(request)
    parts = urlsplit(base)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return {
        "base_url": base,
        "scheme": parts.scheme,
        "host": parts.hostname or "",
        "port": port,
        # netloc contains the port only if it is not the default - exactly what
        # one needs for a displayable address.
        "netloc": parts.netloc,
        # Is the address guessed (no server_url/server_domain set)? The
        # dashboard can point this out instead of presenting a wrong address as
        # the truth without comment.
        "configured": bool((db.get_setting("server_url") or "").strip()
                           or (db.get_setting("server_domain") or "").strip()
                           or (db.get_setting("server_host") or "").strip()),
    }


@router.get("/enroll/{token}", response_class=HTMLResponse)
def enrollment_landing_page(token: str, request: Request):
    """
    The actual download page: shows a download button AND the step-by-step
    commands for Linux/Windows right next to it.
    """
    _require_valid_token(token)
    backend_url = _backend_url(request)

    # Also offer ready-made installer packages (if built locally) directly -
    # the new machine has no access to the dashboard.
    pkgs = _installer_entries()
    if pkgs:
        rows = []
        for p in pkgs:
            hint = _installer_hint(p["name"], token, backend_url)
            rows.append(
                f'<p><a class="btn" href="/enroll/{token}/installer/{p["name"]}">'
                f'{p["icon"]} {p["label"]} ({p["size"] // 1024} KB)</a><br>'
                f'<code>{hint}</code></p>'
            )
        packages_block = ("<h3>Option C — Ready-made installer package</h3>"
                          + "".join(rows))
    else:
        packages_block = ""

    html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="UTF-8">
      <title>BYTERA RMM — Agent installation</title>
      <style>
        body {{ font-family: system-ui, sans-serif; background: #0c1420; color: #dbe4f0;
                max-width: 720px; margin: 40px auto; padding: 0 20px; line-height: 1.5; }}
        h1 {{ color: #2dd4bf; }}
        code, pre {{ background: #141d2b; padding: 10px 14px; border-radius: 8px;
                     display: block; overflow-x: auto; color: #7ff; }}
        .btn {{ display: inline-block; background: #2dd4bf; color: #0c1420; font-weight: 600;
                padding: 12px 22px; border-radius: 8px; text-decoration: none; margin: 10px 0; }}
        .note {{ color: #7f93ad; font-size: 14px; }}
        h3 {{ margin-top: 30px; }}
      </style>
    </head>
    <body>
      <h1>BYTERA RMM — Install the agent</h1>
      <p>This link installs the monitoring agent on this computer
         and connects it to your RMM dashboard automatically.</p>

      <h3>Option A — Download directly</h3>
      <a class="btn" id="dl" href="/enroll/{token}/agent.zip">Download agent (.zip)</a>
      <p class="note">Then: unpack it and follow the included README.txt.</p>

      <h3>Option B — One-liner (Linux)</h3>
      <pre>curl -sSL {backend_url}/enroll/{token}/install.sh | bash</pre>

      <h3>Option B — One-liner (Windows PowerShell, as administrator)</h3>
      <pre>iwr {backend_url}/enroll/{token}/install.ps1 -UseBasicParsing | iex</pre>

      {packages_block}

      <p class="note">This link is single-use and expires automatically after the
         agent has connected successfully for the first time.</p>

      <script>
        // After clicking the download button, briefly note that the page can
        // be closed now.
        document.getElementById('dl').addEventListener('click', function () {{
          setTimeout(function () {{
            document.body.innerHTML = '<h1>Done!</h1><p>The download has started. You can close this window now.</p>';
            try {{ window.close(); }} catch (e) {{}}
          }}, 800);
        }});
      </script>
    </body>
    </html>
    """
    return HTMLResponse(html)


def _build_agent_zip_bytes(backend_url: str, enrollment_token: str = "") -> io.BytesIO:
    """Builds the agent ZIP incl. a pre-filled .env (backend URL/token)."""
    env_content = (
        f"BACKEND_URL={backend_url}\n"
        f"AGENT_TOKEN={AGENT_TOKEN}\n"
        f"ENROLLMENT_TOKEN={enrollment_token}\n"
        f"DEVICE_NAME=\n"
        # Language of the dialogs ON THE DEVICE (consent to the remote screen).
        # This is NOT the dashboard language: the dialog is seen by the person
        # in front of the device, and several dashboard users with different
        # languages can view the same device. de|en, default de.
        f"AGENT_LANG=de\n"
    )
    # End-to-end encryption from the very first connection: the server's PUBLIC
    # key and the fingerprint of its TLS certificate are pinned in the agent.
    try:
        from app import security as _sec
        env_content += _sec.env_lines()
    except Exception as _e:
        print(f"[enroll] security keys not embedded: {_e}")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in AGENT_SOURCE_DIR.rglob("*"):
            if file_path.is_file() and ".env" not in file_path.name and "__pycache__" not in str(file_path):
                _rel = str(file_path.relative_to(AGENT_SOURCE_DIR)).replace("\\", "/")
                # Source is src/agent/main.py, on the client it stays agent.py
                # (services, updates and uninstall depend on this name).
                if _rel == "main.py":
                    _rel = "agent.py"
                zf.write(file_path, arcname=f"agent/{_rel}")
        zf.writestr("agent/.env", env_content)
        # Ship the logo: logo_r.png (prefers the branding version) lands as
        # agent/logo.png next to agent.py and is used by the Tkinter consent
        # window as icon/logo. Note: favicon.ico is actually a PNG and is not
        # read by Tk iconbitmap (blank icon).
        for _cand in (_BRANDING / "logo_r.png",):
            if _cand.is_file():
                zf.write(_cand, arcname="agent/logo.png")
                break
        zf.writestr(
            "agent/README.txt",
            "1. Set BACKEND_URL in .env to the real address of your server\n"
            "2. pip install -r requirements.txt\n"
            "3. python agent.py\n",
        )
    buffer.seek(0)
    return buffer


@router.get("/enroll/{token}/agent.zip")
async def download_agent_zip(token: str, request: Request):
    """Packs the agent folder incl. a pre-filled .env file into a ZIP."""
    _require_valid_token(token)
    buffer = _build_agent_zip_bytes(_backend_url(request), token)
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=bytera-rmm-agent.zip"},
    )


@router.get("/enroll/{token}/install.sh", response_class=PlainTextResponse)
def install_script_linux(token: str, request: Request, lang: str = ""):
    """Ready-made bash one-liner: installs the agent incl. systemd autostart (Linux)."""
    _require_valid_token(token)
    backend_url = _backend_url(request)

    # Language of this script: ?lang=de|en, otherwise the server language.
    _L = _lang(lang) if lang else server_lang()
    def L(key, **kw):
        return _t(key, _L, **kw)

    script = f"""#!/bin/bash
# BYTERA RMM - automatic agent installation (Linux)
set -e
INSTALL_DIR="/opt/bytera-rmm-agent"

# --- Root rights: restart via sudo ourselves if necessary -----------------
# If the script is read from a pipe ("curl ... | bash"), there is no $0 to
# restart - then just a clear hint. Otherwise we restart ourselves with sudo
# ("-E" keeps RMM_* variables alive).
if [ "$(id -u)" != "0" ]; then
  if [ -f "$0" ] && command -v sudo >/dev/null 2>&1; then
    echo "Restarting with root privileges (sudo) ..."
    exec sudo -E "$0" "$@"
  fi
  if ! command -v sudo >/dev/null 2>&1; then
    echo "Please run as root (sudo is not installed)."; exit 1
  fi
  echo "Note: started without root - the individual steps use sudo."
fi

# --- Install system prerequisites automatically ---------------------------
# Needs: python3, python3-venv (ensurepip!), python3-pip, unzip, curl.
# We detect the package manager and install missing packages ourselves, so the
# known error "ensurepip is not available / install python3-venv" no longer
# occurs.
ensure_pkgs() {{
  # Determine the Python minor (e.g. 3.13) for the matching venv package on Debian/Ubuntu.
  PYV="$(python3 -c 'import sys;print(f"{{sys.version_info.major}}.{{sys.version_info.minor}}")' 2>/dev/null || echo 3)"
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    export DEBCONF_NONINTERACTIVE_SEEN=true
    export TERM="${{TERM:-dumb}}"
    sudo -E apt-get update -y -qq >/dev/null 2>&1 || true
    # First the version-specific venv package, otherwise the generic one.
    sudo -E apt-get install -y -qq "python${{PYV}}-venv" >/dev/null 2>&1 \
      || sudo -E apt-get install -y -qq python3-venv >/dev/null 2>&1 || true
    sudo -E apt-get install -y -qq python3-pip unzip curl >/dev/null 2>&1 || true
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y python3 python3-pip unzip curl || true
  elif command -v yum >/dev/null 2>&1; then
    sudo yum install -y python3 python3-pip unzip curl || true
  elif command -v zypper >/dev/null 2>&1; then
    sudo zypper install -y python3 python3-pip unzip curl || true
  elif command -v pacman >/dev/null 2>&1; then
    sudo pacman -Sy --noconfirm python python-pip unzip curl || true
  elif command -v apk >/dev/null 2>&1; then
    sudo apk add --no-cache python3 py3-pip unzip curl || true
  else
    echo "WARNING: no known package manager found - make sure python3-venv, pip, unzip and curl are installed."
  fi
}}
ensure_pkgs

echo "Downloading agent..."
sudo mkdir -p "$INSTALL_DIR"

# --- Rename: adopt a legacy install (RAPALLE.net RMM) ---------------------
# If an agent still runs here under the old name, it is stopped and removed.
# The device ID (.device-id) is carried over so the device does NOT become a
# new client in the dashboard.
LEGACY_DIR="/opt/rapalle-rmm-agent"
if [ -d "$LEGACY_DIR" ] || [ -f /etc/systemd/system/rapalle-agent.service ]; then
  echo "Legacy install (RAPALLE.net RMM) found - migrating to $INSTALL_DIR..."
  sudo systemctl disable --now rapalle-agent 2>/dev/null || true
  sudo systemctl disable --now rapalle-node 2>/dev/null || true
  sudo pkill -f "$LEGACY_DIR/" 2>/dev/null || true
  if [ -f "$LEGACY_DIR/.device-id" ] && [ ! -f "$INSTALL_DIR/.device-id" ]; then
    sudo cp "$LEGACY_DIR/.device-id" "$INSTALL_DIR/.device-id"
  fi
  sudo rm -f /etc/systemd/system/rapalle-agent.service /etc/systemd/system/rapalle-node.service
  sudo systemctl daemon-reload 2>/dev/null || true
  sudo systemctl reset-failed rapalle-agent rapalle-node 2>/dev/null || true
  sudo rm -rf "$LEGACY_DIR"
fi
curl -sSL "{backend_url}/enroll/{token}/agent.zip" -o /tmp/bytera-agent.zip
sudo unzip -o /tmp/bytera-agent.zip -d /tmp/bytera-agent-extract
# Stop a running agent before files are replaced (otherwise the old version
# stays active until the service is restarted manually).
sudo systemctl stop bytera-agent 2>/dev/null || true
sudo cp -r /tmp/bytera-agent-extract/agent/. "$INSTALL_DIR/"

# Set .env to the real backend address (the machine running this script
# already knows the backend address, because it just called it)
sudo sed -i "s#BACKEND_URL=.*#BACKEND_URL={backend_url}#" "$INSTALL_DIR/.env"

echo "Installing Python dependencies..."
cd "$INSTALL_DIR"
# Create venv; if that fails despite package installation, install directly
# into the system without venv (--break-system-packages for PEP-668 distros).
if sudo python3 -m venv venv; then
  sudo ./venv/bin/python -m pip install --quiet --upgrade pip || true
  sudo ./venv/bin/pip install --quiet -r requirements.txt
  PYEXEC="$INSTALL_DIR/venv/bin/python"
else
  echo "venv not available - installing dependencies system-wide..."
  sudo python3 -m pip install --quiet --break-system-packages -r requirements.txt \
    || sudo python3 -m pip install --quiet -r requirements.txt
  PYEXEC="$(command -v python3)"
fi

echo "Setting up the autostart service..."
sudo tee /etc/systemd/system/bytera-agent.service > /dev/null <<EOF
[Unit]
Description=BYTERA RMM Agent
After=network.target

[Service]
WorkingDirectory=$INSTALL_DIR
ExecStart=$PYEXEC $INSTALL_DIR/agent.py
Restart=always
User=root

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now bytera-agent

# --- Final check ------------------------------------------------------------
# Previously this unconditionally said "Done!". That was a claim, not a
# finding: systemctl enable --now reports success as soon as the process has
# STARTED - whether it fails against the backend a second later or exits again
# right away, the installer does not see that. Exactly why one read "ran
# without errors" while a client never appeared in the dashboard.
FAIL=0

echo ""
echo "--- Checking installation ---"

# 1. Is the backend address reachable from THIS device at all?
#    This catches the most common case: a BACKEND_URL with an internal port
#    that does not exist from outside behind a reverse proxy/Cloudflare.
if curl -fsS --max-time 15 "{backend_url}/api/agent/version" >/dev/null 2>&1; then
  echo "[OK]     Backend reachable: {backend_url}"
else
  echo "[ERROR]  Backend NOT reachable: {backend_url}"
  echo "         The agent cannot register like this."
  echo "         Check the 'Server URL' in the dashboard settings."
  FAIL=1
fi

# 2. Is the service still running after a few seconds? A crash at startup
#    (missing dependency, wrong token) shows up exactly here.
sleep 6
if sudo systemctl is-active --quiet bytera-agent; then
  echo "[OK]     Service bytera-agent is running"
else
  echo "[ERROR]  Service bytera-agent is NOT running (anymore)"
  FAIL=1
fi

# 3. What does the agent itself say? Its last lines are more meaningful than
#    any guess by the installer.
if sudo journalctl -u bytera-agent -n 15 --no-pager 2>/dev/null | grep -qiE "Ungueltiges Agent-Token|Ungültiges Agent-Token|Invalid.*token"; then
  echo "[ERROR]  The backend rejects the AGENT_TOKEN."
  FAIL=1
fi

echo ""
if [ "$FAIL" = "0" ]; then
  echo "Done! The agent is running and connecting to {backend_url}"
  echo "It should appear in the dashboard within a minute."
else
  echo "INSTALLATION INCOMPLETE - the agent will NOT appear in the dashboard."
  echo "Last agent messages:"
  sudo journalctl -u bytera-agent -n 25 --no-pager 2>/dev/null || \
    sudo tail -n 25 "$INSTALL_DIR/agent.log" 2>/dev/null || true
fi
echo "Check status any time with: sudo systemctl status bytera-agent"
exit $FAIL
"""
    return PlainTextResponse(script, media_type="text/x-sh")


@router.get("/enroll/{token}/install.ps1", response_class=PlainTextResponse)
def install_script_windows(token: str, request: Request, lang: str = ""):
    """Ready-made PowerShell script: installs the agent incl. autostart (Windows)."""
    _require_valid_token(token)
    backend_url = _backend_url(request)

    # Sprache dieses Skripts: ?lang=de|en, sonst die Server-Sprache.
    _L = _lang(lang) if lang else server_lang()
    def L(key, **kw):
        return _t(key, _L, **kw)

    script = f"""# BYTERA RMM - automatic agent installation (Windows)
$ErrorActionPreference = "Stop"
$InstallDir = "C:\\Program Files\\ByteraRmmAgent"

# --- Admin rights: restart via UAC ourselves if necessary -----------------
# Important for the one-liner "iwr ... | iex": there is no script file that
# could be restarted. So in the elevated session we simply load the script
# from the server once more.
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {{
    Write-Host "Restarting with administrator rights (UAC prompt) ..." -ForegroundColor Yellow
    $cmd = "iwr '{backend_url}/enroll/{token}/install.ps1' -UseBasicParsing | iex"
    $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($cmd))
    try {{
        $p = Start-Process -FilePath "powershell.exe" -Verb RunAs -PassThru -Wait `
             -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", $enc)
        exit $p.ExitCode
    }} catch {{
        Write-Host "Elevation denied - please open PowerShell as administrator." -ForegroundColor Red
        exit 1
    }}
}}

# --- 1. Check whether Python is present, otherwise install automatically ---
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {{
    Write-Host "Python not found - installing it automatically..." -ForegroundColor Yellow
    try {{
        # Preferably via winget (present on current Windows versions)
        if (Get-Command winget -ErrorAction SilentlyContinue) {{
            winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
        }} else {{
            # Fallback: download the official installer and install silently
            $pyUrl = "https://www.python.org/ftp/python/3.12.4/python-3.12.4-amd64.exe"
            Invoke-WebRequest -Uri $pyUrl -OutFile "$env:TEMP\\python-setup.exe"
            Start-Process -Wait -FilePath "$env:TEMP\\python-setup.exe" -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1"
        }}
        # Update the PATH of the current session
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    }} catch {{
        Write-Host "Automatic Python installation failed." -ForegroundColor Red
        Write-Host "Please install Python manually: https://www.python.org/downloads/" -ForegroundColor Red
        Write-Host "(Tick 'Add Python to PATH' during setup!) Then run this script again."
        exit 1
    }}
}}

# --- 2. Download and unpack the agent ---
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

# --- Rename: adopt a legacy install (RAPALLE.net RMM) ---------------------
# If an agent still runs under the old name, it is stopped and removed.
# The device ID (.device-id) is carried over -> no new client in the dashboard.
$LegacyDir = "C:\\Program Files\\RapalleRmmAgent"
$LegacyFound = (Test-Path $LegacyDir) -or [bool](Get-ScheduledTask -TaskName "RapalleRmmAgent" -ErrorAction SilentlyContinue)
if ($LegacyFound) {{
    Write-Host "Legacy install (RAPALLE.net RMM) found - migrating to $InstallDir..."
    foreach ($t in @("RapalleRmmAgent", "RapalleNode")) {{
        try {{ Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue }} catch {{}}
        try {{ Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction SilentlyContinue }} catch {{}}
    }}
    try {{
        Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
            Where-Object {{ $_.CommandLine -like "*RapalleRmmAgent*" }} |
            ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}
    }} catch {{}}
    if ((Test-Path "$LegacyDir\\.device-id") -and -not (Test-Path "$InstallDir\\.device-id")) {{
        Copy-Item "$LegacyDir\\.device-id" "$InstallDir\\.device-id" -Force
    }}
}}
Write-Host "Downloading agent..."
Invoke-WebRequest -Uri "{backend_url}/enroll/{token}/agent.zip" -OutFile "$env:TEMP\\bytera-agent.zip"
if (Test-Path "$env:TEMP\\bytera-agent-extract") {{ Remove-Item -Recurse -Force "$env:TEMP\\bytera-agent-extract" }}
Expand-Archive -Path "$env:TEMP\\bytera-agent.zip" -DestinationPath "$env:TEMP\\bytera-agent-extract" -Force

# --- STOP a running agent before files are replaced ------------------------
# IMPORTANT: otherwise the OLD agent.py keeps running in memory after the
# update (new features like the interactive terminal are then missing). First
# pause the task, then kill all pythonw/python processes running agent.py.
Write-Host "Stopping a running agent (if any)..."
try {{ Stop-ScheduledTask -TaskName "ByteraRmmAgent" -ErrorAction SilentlyContinue }} catch {{}}
try {{
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
        Where-Object {{ $_.CommandLine -like "*agent.py*" }} |
        ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}
}} catch {{}}
Start-Sleep -Seconds 2

Copy-Item -Path "$env:TEMP\\bytera-agent-extract\\agent\\*" -Destination $InstallDir -Recurse -Force

# Set .env to the real backend address
(Get-Content "$InstallDir\\.env") -replace 'BACKEND_URL=.*', 'BACKEND_URL={backend_url}' | Set-Content "$InstallDir\\.env"

# --- 3. Install Python dependencies ---
Write-Host "Installing Python dependencies (may take 1-2 minutes)..."
python -m venv "$InstallDir\\venv"
& "$InstallDir\\venv\\Scripts\\python.exe" -m pip install --upgrade pip --quiet
& "$InstallDir\\venv\\Scripts\\pip.exe" install -r "$InstallDir\\requirements.txt"
if ($LASTEXITCODE -ne 0) {{
    Write-Host "ERROR during package installation! The agent cannot start like this." -ForegroundColor Red
    exit 1
}}

# --- 4. Set up autostart as a SERVICE (SYSTEM, at system start) ---
# IMPORTANT (servers!): previously the task ran under the installing user with
# trigger "AtLogOn". On servers where nobody (or another user) logs on, the
# agent therefore NEVER started.
# Now: Principal = SYSTEM (LogonType ServiceAccount), Trigger = AtStartup.
# The agent thus runs from boot - without any login.
#
# Screen capture: SYSTEM sits in session 0 and has no desktop. The agent
# therefore starts a helper process in the active user session itself when
# needed (agent.py --screen-helper, see session_bridge.py). Only SYSTEM may do
# that (WTSQueryUserToken) - so service operation is even a prerequisite.
# pythonw.exe = Python WITHOUT a console window -> runs invisibly in the background.
Write-Host "Setting up autostart as a service (SYSTEM, starts at boot)..."
$Action = New-ScheduledTaskAction -Execute "$InstallDir\\venv\\Scripts\\pythonw.exe" -Argument "`"$InstallDir\\agent.py`"" -WorkingDirectory $InstallDir
# Two triggers: at system start + every 5 minutes as a restart safety net.
# MultipleInstances=IgnoreNew prevents duplicate agents.
$TrigBoot  = New-ScheduledTaskTrigger -AtStartup
$TrigWatch = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
$Principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Seconds 0)  # 0 = NO time limit! Without it the task scheduler kills the task after 72h SILENTLY (no log, no restart, since no "error")
# Remove the old (user-bound) task so no second agent starts.
try {{ Unregister-ScheduledTask -TaskName "ByteraRmmAgent" -Confirm:$false -ErrorAction SilentlyContinue }} catch {{}}
Register-ScheduledTask -TaskName "ByteraRmmAgent" -Action $Action -Trigger $TrigBoot,$TrigWatch -Principal $Principal -Settings $Settings -Force | Out-Null

# --- 5. Start the agent IMMEDIATELY (invisible, as SYSTEM) ---
Start-ScheduledTask -TaskName "ByteraRmmAgent"
Start-Sleep -Seconds 3
$chk = Get-ScheduledTask -TaskName "ByteraRmmAgent" -ErrorAction SilentlyContinue
if ($chk -and $chk.Principal.UserId -notmatch "SYSTEM") {{
    Write-Host "WARNING: autostart is not running as SYSTEM - the agent will only start after a user logs on." -ForegroundColor Yellow
}}

# --- 6. Elevated maintenance tasks (update/uninstall as SYSTEM, via event) ---
# Fallback: the agent already runs as SYSTEM, but these tasks allow
# update/uninstall even when the agent process itself is no longer running or
# (legacy install) only hangs in a user session.
Write-Host "Setting up elevated maintenance tasks (update/uninstall)..."
try {{ iwr "{backend_url}/agent-dist/elevate.ps1" -UseBasicParsing | iex }} catch {{ Write-Host "  (maintenance tasks could not be set up: $_)" }}

Write-Host ""
Write-Host "=== DONE! ===" -ForegroundColor Green
Write-Host "The agent now runs as a service under SYSTEM and starts at boot -"
Write-Host "even if NOBODY logs on (servers)."
Write-Host "You can close this window - the agent keeps running."
Write-Host "The client should appear in the dashboard within a few seconds."
Write-Host ""
Write-Host "(Log file for troubleshooting: $InstallDir\\agent.log)"

# --- Rename: remove remnants of the legacy install (RAPALLE.net RMM) -------
# Deliberately at the very end: the old maintenance task may have started this
# very script. Whatever remains here is cleaned up by the new agent at startup.
if ($LegacyFound) {{
    Remove-Item -LiteralPath $LegacyDir -Recurse -Force -ErrorAction SilentlyContinue
    try {{ if ([System.Diagnostics.EventLog]::SourceExists("RapalleRMM")) {{ Remove-EventLog -Source "RapalleRMM" }} }} catch {{}}
    foreach ($t in @("RapalleRmmUninstall", "RapalleRmmUpdate")) {{ schtasks /delete /tn $t /f 2>$null | Out-Null }}
}}
"""
    return PlainTextResponse(script, media_type="text/plain")


# ==========================================================================
# TOKENLESS AGENT DISTRIBUTION: update & uninstall
# --------------------------------------------------------------------------
# These routes are deliberately under /agent-dist/... (NOT under /enroll/{token},
# otherwise "agent-dist" would be interpreted as a token). They need no
# onboarding token, because an already installed agent keeps its identity via
# the file ".device-id" - an update/uninstall does not change that.
# The agent calls these scripts itself when it receives the events "update-agent"
# resp. "uninstall-agent" (see agent.py, _run_dist_command).
#
# IMPORTANT (reliability):
#  - Update MUST RESTART the service at the end (rewrite the autostart unit if
#    needed, then restart/Start-ScheduledTask).
#  - Uninstall runs SYNCHRONOUSLY (no background job): the agent starts the
#    script in its own systemd scope resp. as a detached process, so that it
#    survives the stop of its own service and really deletes the files.
# ==========================================================================

@router.get("/agent-dist/agent.zip")
async def agent_dist_zip(request: Request):
    """Current agent ZIP WITHOUT an onboarding token (for self-update)."""
    buffer = _build_agent_zip_bytes(_backend_url(request), "")
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=bytera-rmm-agent.zip"},
    )


@router.get("/agent-dist/update.sh", response_class=PlainTextResponse)
def agent_update_sh(request: Request, lang: str = ""):
    """
    Updates the agent (Linux) and RESTARTS the autostart service. The systemd
    unit is rewritten for safety so that the restart works even if the unit was
    missing/outdated. An existing .device-id is kept -> same client identity.
    During a server move: new address (+ possibly new token), see _update_target.
    """
    backend_url = _update_target(request)
    new_token = _migration_token()
    # Language of this script: ?lang=de|en, otherwise the server language.
    _L = _lang(lang) if lang else server_lang()
    def L(key, **kw):
        return _t(key, _L, **kw)

    token_line = ('  sed -i "s#AGENT_TOKEN=.*#AGENT_TOKEN=' + new_token + '#" "$INSTALL_DIR/.env"') if new_token else ''
    script = f"""#!/bin/bash
# BYTERA RMM - agent update (Linux)
# ---------------------------------------------------------------------------
# SAFE UPDATE: nothing is deleted until the new version is proven to work.
#   1. download + unpack (agent keeps running if this fails)
#   2. stop agent, MOVE the current install to $INSTALL_DIR.bak (complete,
#      incl. venv - restorable in one step)
#   3. lay out the new version, build venv/packages (fallback: old venv)
#   4. start the agent and WAIT until it has registered with the server
#      (the agent writes the file .connected) - up to HEALTH_TIMEOUT seconds
#   5. success -> delete backup | any failure -> ROLLBACK to the old version,
#      write .update_failed (the old agent reports it to the dashboard)
# No "set -e": every step is checked explicitly, so the script can never
# stop half-way and leave the device without a running agent.
# ---------------------------------------------------------------------------
INSTALL_DIR="/opt/bytera-rmm-agent"
BACKUP_DIR="/opt/bytera-rmm-agent.bak"
LEGACY_DIR="/opt/rapalle-rmm-agent"
UNIT=/etc/systemd/system/bytera-agent.service
LOG=/var/log/bytera-agent-update.log
HEALTH_TIMEOUT=180
TMP=/tmp/bytera-agent-update.$$

if [ "$(id -u)" != "0" ]; then
  echo "Please run as root, e.g.: curl -fsSL '{backend_url}/agent-dist/update.sh' | sudo bash"
  exit 1
fi
exec > >(tee -a "$LOG") 2>&1
echo "=== {L('ins_upd_title')} ($(date)) ==="

SWAPPED=0
LEGACY_ONLY=0

restart_old() {{
  if [ "$LEGACY_ONLY" = "1" ]; then
    systemctl enable --now rapalle-agent 2>/dev/null || true
  else
    systemctl daemon-reload 2>/dev/null || true
    systemctl enable bytera-agent 2>/dev/null || true
    systemctl restart bytera-agent 2>/dev/null || true
  fi
}}

rollback() {{
  echo "!!! ROLLBACK: $1"
  systemctl stop bytera-agent 2>/dev/null || true
  pkill -f "$INSTALL_DIR/agent[.]py" 2>/dev/null || true
  sleep 1
  if [ -d "$BACKUP_DIR" ]; then
    rm -rf "$INSTALL_DIR.failed"
    [ -d "$INSTALL_DIR" ] && mv "$INSTALL_DIR" "$INSTALL_DIR.failed"
    mv "$BACKUP_DIR" "$INSTALL_DIR"
    [ -f "$INSTALL_DIR/.unit.bak" ] && cp "$INSTALL_DIR/.unit.bak" "$UNIT"
    echo "Previous version restored (failed version kept in $INSTALL_DIR.failed for analysis)."
  fi
  rm -f "$INSTALL_DIR/.connected" "$INSTALL_DIR/.updated"
  [ -d "$INSTALL_DIR" ] && echo "$1" > "$INSTALL_DIR/.update_failed"
  restart_old
  cp "$LOG" "$INSTALL_DIR/update.log" 2>/dev/null || true
  rm -rf "$TMP"
  exit 1
}}

fail() {{
  # Error BEFORE anything was replaced: just make sure the agent runs again.
  if [ "$SWAPPED" = "1" ]; then rollback "$1"; fi
  echo "!!! UPDATE ABORTED: $1"
  [ -d "$INSTALL_DIR" ] && echo "$1" > "$INSTALL_DIR/.update_failed"
  restart_old
  rm -rf "$TMP"
  exit 1
}}

# --- 1. Download + unpack (agent still running) -----------------------------
echo "{L('ins_downloading')}"
rm -rf "$TMP"; mkdir -p "$TMP/x"
curl -fsSL "{backend_url}/agent-dist/agent.zip" -o "$TMP/agent.zip" || fail "download of agent.zip failed"
if command -v unzip >/dev/null 2>&1; then
  unzip -oq "$TMP/agent.zip" -d "$TMP/x" || fail "agent.zip could not be unpacked"
else
  python3 -m zipfile -e "$TMP/agent.zip" "$TMP/x" || fail "agent.zip could not be unpacked (no unzip/python3)"
fi
[ -f "$TMP/x/agent/agent.py" ] || fail "agent.zip is incomplete (agent/agent.py missing)"

# --- 2. Stop agent, keep the current install as backup -----------------------
echo "{L('ins_stopping')}"
if [ ! -d "$INSTALL_DIR" ] && [ -d "$LEGACY_DIR" ]; then
  LEGACY_ONLY=1
  echo "Legacy install (RAPALLE.net RMM) found - migrating to $INSTALL_DIR."
fi
systemctl stop bytera-agent 2>/dev/null || true
pkill -f "$INSTALL_DIR/agent[.]py" 2>/dev/null || true
if [ -d "$LEGACY_DIR" ] || [ -f /etc/systemd/system/rapalle-agent.service ]; then
  systemctl stop rapalle-agent rapalle-node 2>/dev/null || true
  pkill -f "$LEGACY_DIR/agent[.]py" 2>/dev/null || true
fi
sleep 2

rm -rf "$BACKUP_DIR" "$INSTALL_DIR.failed"
if [ -d "$INSTALL_DIR" ]; then
  mv "$INSTALL_DIR" "$BACKUP_DIR" || fail "could not move $INSTALL_DIR to $BACKUP_DIR"
  [ -f "$UNIT" ] && cp "$UNIT" "$BACKUP_DIR/.unit.bak"
  SWAPPED=1
fi
mkdir -p "$INSTALL_DIR" || rollback "could not create $INSTALL_DIR"
[ -f "$BACKUP_DIR/agent.log" ] && cp "$BACKUP_DIR/agent.log" "$INSTALL_DIR/agent.log"

# --- 3. New files + identity ---------------------------------------------------
echo "{L('ins_deploying')}"
cp -a "$TMP/x/agent/." "$INSTALL_DIR/" || rollback "copying the new files failed"
for f in .env .device-id; do
  if [ -f "$BACKUP_DIR/$f" ]; then cp "$BACKUP_DIR/$f" "$INSTALL_DIR/$f"
  elif [ -f "$LEGACY_DIR/$f" ]; then cp "$LEGACY_DIR/$f" "$INSTALL_DIR/$f"; fi
done
[ -f "$INSTALL_DIR/.env" ] || rollback ".env (server address/token) missing - keeping the old version"
sed -i "s#BACKEND_URL=.*#BACKEND_URL={backend_url}#" "$INSTALL_DIR/.env"
{token_line}

# --- 4. Python environment -----------------------------------------------------
# New venv first. If that fails (no python3-venv, no internet for pip), the
# OLD venv is reused - it lives at the same path, so it works unchanged.
cd "$INSTALL_DIR"
PYEXEC=""
SYSPY="$(command -v python3 || true)"
if [ -n "$SYSPY" ] && "$SYSPY" -m venv "$INSTALL_DIR/venv" >/dev/null 2>&1 && [ -x "$INSTALL_DIR/venv/bin/python" ]; then
  echo "{L('ins_venv', py='$SYSPY')}"
  echo "{L('ins_deps')}"
  "$INSTALL_DIR/venv/bin/pip" install --upgrade pip --quiet >/dev/null 2>&1 || true
  if "$INSTALL_DIR/venv/bin/pip" install -r requirements.txt; then
    PYEXEC="$INSTALL_DIR/venv/bin/python"
  else
    echo "WARNING: package installation failed - trying the previous environment."
    rm -rf "$INSTALL_DIR/venv"
  fi
fi
if [ -z "$PYEXEC" ] && [ -x "$BACKUP_DIR/venv/bin/python" ]; then
  echo "Reusing the previous venv."
  cp -a "$BACKUP_DIR/venv" "$INSTALL_DIR/venv" || rollback "could not copy the previous venv"
  "$INSTALL_DIR/venv/bin/pip" install -r requirements.txt --quiet || echo "WARNING: some packages could not be updated (offline?) - continuing, health check decides."
  PYEXEC="$INSTALL_DIR/venv/bin/python"
fi
if [ -z "$PYEXEC" ] && [ -n "$SYSPY" ]; then
  echo "{L('ins_warn_venv')}"
  "$SYSPY" -m pip install -r requirements.txt --break-system-packages >/dev/null 2>&1 || \
    "$SYSPY" -m pip install -r requirements.txt >/dev/null 2>&1 || true
  PYEXEC="$SYSPY"
fi
[ -n "$PYEXEC" ] || rollback "no usable Python found"

# --- 5. Service unit + start -----------------------------------------------------
cat > "$UNIT" <<UNITEOF
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
UNITEOF
rm -f "$INSTALL_DIR/.connected" "$INSTALL_DIR/.update_failed"
touch "$INSTALL_DIR/.updated"
systemctl daemon-reload
systemctl enable bytera-agent >/dev/null 2>&1 || true
systemctl restart bytera-agent || rollback "systemctl restart bytera-agent failed"

# --- 6. Health check: new agent must register with the server -------------------
echo "Waiting for the new agent to connect (max. $HEALTH_TIMEOUT s)..."
OK=0
for i in $(seq 1 $HEALTH_TIMEOUT); do
  if [ -f "$INSTALL_DIR/.connected" ]; then OK=1; break; fi
  sleep 1
done
if [ "$OK" != "1" ]; then
  journalctl -u bytera-agent -n 30 --no-pager 2>/dev/null | sed 's/^/  [journal] /'
  rollback "new version did not connect to the server within $HEALTH_TIMEOUT s"
fi
echo "New agent is connected."

# --- 7. Clean up -----------------------------------------------------------------
rm -rf "$BACKUP_DIR" "$TMP"
if [ -d "$LEGACY_DIR" ] || [ -f /etc/systemd/system/rapalle-agent.service ]; then
  systemctl disable --now rapalle-agent rapalle-node 2>/dev/null || true
  rm -f /etc/systemd/system/rapalle-agent.service /etc/systemd/system/rapalle-node.service
  systemctl daemon-reload 2>/dev/null || true
  systemctl reset-failed rapalle-agent rapalle-node 2>/dev/null || true
  rm -rf "$LEGACY_DIR"
  echo "Legacy install (RAPALLE.net RMM) removed."
fi
cp "$LOG" "$INSTALL_DIR/update.log" 2>/dev/null || true
echo "Update finished - agent restarted."
"""
    return PlainTextResponse(script, media_type="text/x-sh")


@router.get("/agent-dist/update.ps1", response_class=PlainTextResponse)
def agent_update_ps1(request: Request, lang: str = ""):
    """Updates the agent (Windows) and RESTARTS the autostart task (the task is
    re-registered if needed). During a server move: new address (+ possibly new
    token), see _update_target."""
    backend_url = _update_target(request)
    self_url = _backend_url(request)          # UAC restart fetches the script from HERE again
    new_token = _migration_token()
    # Language of this script: ?lang=de|en, otherwise the server language.
    _L = _lang(lang) if lang else server_lang()
    def L(key, **kw):
        return _t(key, _L, **kw)

    script = f"""# BYTERA RMM - agent update (Windows) - run as administrator
# ---------------------------------------------------------------------------
# SAFE UPDATE: nothing is deleted until the new version is proven to work.
#   1. download + unpack (agent keeps running if this fails)
#   2. stop agent, keep the complete current install (incl. venv) as
#      "<InstallDir>.bak"
#   3. lay out the new version, build venv/packages (fallback: old venv)
#   4. start the agent and WAIT until it has registered with the server
#      (the agent writes .connected) - up to $HealthTimeout seconds
#   5. success -> delete backup | any failure -> ROLLBACK to the old version,
#      write .update_failed (the old agent reports it to the dashboard)
# ---------------------------------------------------------------------------
$ErrorActionPreference = "Continue"
$InstallDir = "C:\\Program Files\\ByteraRmmAgent"
$BackupDir  = "C:\\Program Files\\ByteraRmmAgent.bak"
$FailedDir  = "C:\\Program Files\\ByteraRmmAgent.failed"
$LegacyDir  = "C:\\Program Files\\RapalleRmmAgent"
$TaskName   = "ByteraRmmAgent"
$HealthTimeout = 180
# Admin rights: relaunch via UAC if needed (Program Files + Task Scheduler).
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {{
    $cmd = "iwr '{self_url}/agent-dist/update.ps1' -UseBasicParsing | iex"
    $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($cmd))
    $p = Start-Process -FilePath "powershell.exe" -Verb RunAs -PassThru -Wait `
         -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", $enc)
    exit $p.ExitCode
}}
# Log OUTSIDE the install folder - an open log file inside it would block
# moving the folder to the backup.
$LogFile = "$env:ProgramData\\ByteraRmmAgent-update.log"
try {{ Start-Transcript -Path $LogFile -Append -Force | Out-Null }} catch {{}}
Write-Host "=== {L('ins_upd_title')} ($(Get-Date)) ==="

$script:Swapped = $false
$script:LegacyOnly = (-not (Test-Path $InstallDir)) -and (Test-Path $LegacyDir)

function Stop-Agent {{
    try {{ Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue }} catch {{}}
    try {{
        Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
            Where-Object {{ $_.CommandLine -like "*ByteraRmmAgent*" -or $_.CommandLine -like "*RapalleRmmAgent*" -or $_.CommandLine -like "*_screen_helper.py*" }} |
            ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}
    }} catch {{}}
}}

function Start-Old {{
    if ($script:LegacyOnly) {{
        try {{ Enable-ScheduledTask -TaskName "RapalleRmmAgent" -ErrorAction SilentlyContinue | Out-Null; Start-ScheduledTask -TaskName "RapalleRmmAgent" -ErrorAction SilentlyContinue }} catch {{}}
    }} else {{
        try {{ Enable-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null }} catch {{}}
        try {{ Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue }} catch {{}}
    }}
}}

function Invoke-Rollback([string]$Reason) {{
    Write-Host "!!! ROLLBACK: $Reason" -ForegroundColor Red
    Stop-Agent
    Start-Sleep -Seconds 2
    if (Test-Path $BackupDir) {{
        if (Test-Path $FailedDir) {{ Remove-Item -Recurse -Force $FailedDir -ErrorAction SilentlyContinue }}
        $moved = $false
        if (Test-Path $InstallDir) {{
            try {{ Rename-Item $InstallDir (Split-Path $FailedDir -Leaf) -ErrorAction Stop; $moved = $true }} catch {{}}
        }} else {{ $moved = $true }}
        if ($moved) {{
            Rename-Item $BackupDir (Split-Path $InstallDir -Leaf) -ErrorAction SilentlyContinue
        }} else {{
            # Folder locked: restore by copying the backup back
            Get-ChildItem $InstallDir -Force -ErrorAction SilentlyContinue | Where-Object {{ $_.Name -ne "agent.log" }} |
                ForEach-Object {{ Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }}
            Copy-Item "$BackupDir\\*" $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
            Remove-Item -Recurse -Force $BackupDir -ErrorAction SilentlyContinue
        }}
        Write-Host "Previous version restored."
    }}
    Remove-Item "$InstallDir\\.connected", "$InstallDir\\.updated" -Force -ErrorAction SilentlyContinue
    if (Test-Path $InstallDir) {{ Set-Content -Path "$InstallDir\\.update_failed" -Value $Reason -Encoding UTF8 }}
    Start-Old
    try {{ Stop-Transcript | Out-Null }} catch {{}}
    Copy-Item $LogFile "$InstallDir\\update.log" -Force -ErrorAction SilentlyContinue
    exit 1
}}

function Stop-Update([string]$Reason) {{
    if ($script:Swapped) {{ Invoke-Rollback $Reason }}
    Write-Host "!!! UPDATE ABORTED: $Reason" -ForegroundColor Red
    if (Test-Path $InstallDir) {{ Set-Content -Path "$InstallDir\\.update_failed" -Value $Reason -Encoding UTF8 }}
    Start-Old
    try {{ Stop-Transcript | Out-Null }} catch {{}}
    exit 1
}}

# --- 1. Download + unpack (agent still running) -----------------------------
Write-Host "{L('ins_downloading')}"
$Tmp = "$env:TEMP\\bytera-agent-update"
Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $Tmp | Out-Null
try {{
    Invoke-WebRequest -Uri "{backend_url}/agent-dist/agent.zip" -OutFile "$Tmp\\agent.zip" -UseBasicParsing -ErrorAction Stop
    Expand-Archive -Path "$Tmp\\agent.zip" -DestinationPath "$Tmp\\x" -Force -ErrorAction Stop
}} catch {{ Stop-Update "download/unpack of agent.zip failed: $_" }}
$Src = "$Tmp\\x\\agent"
if (-not (Test-Path "$Src\\agent.py")) {{ Stop-Update "agent.zip is incomplete (agent\\agent.py missing)" }}

# --- 2. Stop agent (also the screen helper), keep current install as backup --
Write-Host "{L('ins_stopping')}"
# Pause the watchdog trigger of the task so it cannot start a half-installed agent.
try {{ Disable-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null }} catch {{}}
Stop-Agent
foreach ($t in @("RapalleRmmAgent", "RapalleNode")) {{
    try {{ Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue }} catch {{}}
}}
Start-Sleep -Seconds 3

Remove-Item -Recurse -Force $BackupDir -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force $FailedDir -ErrorAction SilentlyContinue
if (Test-Path $InstallDir) {{
    $renamed = $false
    for ($i = 0; $i -lt 5 -and -not $renamed; $i++) {{
        try {{ Rename-Item $InstallDir (Split-Path $BackupDir -Leaf) -ErrorAction Stop; $renamed = $true }}
        catch {{ Stop-Agent; Start-Sleep -Seconds 2 }}
    }}
    if (-not $renamed) {{
        # Something still holds a file open: copy as backup, then clear.
        Write-Host "Install folder is locked - using a copy as backup."
        try {{ Copy-Item $InstallDir $BackupDir -Recurse -Force -ErrorAction Stop }} catch {{ Stop-Update "could not create a backup of $InstallDir : $_" }}
        Get-ChildItem $InstallDir -Force -ErrorAction SilentlyContinue | Where-Object {{ $_.Name -ne "agent.log" }} |
            ForEach-Object {{ Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }}
    }}
    $script:Swapped = $true
}}
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
if ((Test-Path "$BackupDir\\agent.log") -and -not (Test-Path "$InstallDir\\agent.log")) {{
    Copy-Item "$BackupDir\\agent.log" "$InstallDir\\agent.log" -Force -ErrorAction SilentlyContinue
}}

# --- 3. New files + identity --------------------------------------------------
Write-Host "{L('ins_deploying')}"
try {{ Copy-Item -Path "$Src\\*" -Destination $InstallDir -Recurse -Force -ErrorAction Stop }}
catch {{ Invoke-Rollback "copying the new files failed: $_" }}
foreach ($f in @(".env", ".device-id")) {{
    if (Test-Path "$BackupDir\\$f") {{ Copy-Item "$BackupDir\\$f" "$InstallDir\\$f" -Force }}
    elseif (Test-Path "$LegacyDir\\$f") {{ Copy-Item "$LegacyDir\\$f" "$InstallDir\\$f" -Force }}
}}
if (-not (Test-Path "$InstallDir\\.env")) {{ Invoke-Rollback ".env (server address/token) missing - keeping the old version" }}
(Get-Content "$InstallDir\\.env") -replace 'BACKEND_URL=.*', 'BACKEND_URL={backend_url}' | Set-Content "$InstallDir\\.env"

# --- 4. Python environment ------------------------------------------------------
# New venv first. If that fails (SYSTEM often has no python in PATH, pip
# offline), the OLD venv is reused - same path, so it works unchanged.
$VenvPy = "$InstallDir\\venv\\Scripts\\python.exe"
$SysPy = $null
foreach ($cand in @("python.exe", "python3.exe", "py.exe")) {{
    $c = Get-Command $cand -ErrorAction SilentlyContinue
    if ($c) {{ $SysPy = $c.Source; break }}
}}
if (-not $SysPy) {{
    foreach ($g in @("$env:ProgramFiles\\Python3*\\python.exe", "C:\\Python3*\\python.exe",
                     "C:\\Users\\*\\AppData\\Local\\Programs\\Python\\Python3*\\python.exe")) {{
        $hit = Get-ChildItem $g -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($hit) {{ $SysPy = $hit.FullName; break }}
    }}
}}
$EnvOk = $false
if ($SysPy) {{
    Write-Host "{L('ins_venv', py='$SysPy')}"
    & $SysPy -m venv "$InstallDir\\venv" 2>&1 | Out-Host
    if ($LASTEXITCODE -eq 0 -and (Test-Path $VenvPy)) {{
        Write-Host "{L('ins_deps')}"
        & $VenvPy -m pip install --upgrade pip --quiet 2>&1 | Out-Null
        & $VenvPy -m pip install -r "$InstallDir\\requirements.txt" 2>&1 | Out-Host
        if ($LASTEXITCODE -eq 0) {{ $EnvOk = $true }}
        else {{ Write-Host "WARNING: package installation failed - trying the previous environment." -ForegroundColor Yellow }}
    }} else {{
        Write-Host "{L('ins_warn_venv')}" -ForegroundColor Yellow
    }}
}} else {{
    Write-Host "{L('ins_warn_nopy')}" -ForegroundColor Yellow
}}
if (-not $EnvOk) {{
    Remove-Item -Recurse -Force "$InstallDir\\venv" -ErrorAction SilentlyContinue
    if (Test-Path "$BackupDir\\venv\\Scripts\\python.exe") {{
        Write-Host "Reusing the previous venv."
        try {{ Copy-Item "$BackupDir\\venv" "$InstallDir\\venv" -Recurse -Force -ErrorAction Stop }}
        catch {{ Invoke-Rollback "could not copy the previous venv: $_" }}
        & $VenvPy -m pip install -r "$InstallDir\\requirements.txt" --quiet 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {{ Write-Host "WARNING: some packages could not be updated (offline?) - continuing, health check decides." -ForegroundColor Yellow }}
    }}
}}
if (-not (Test-Path $VenvPy)) {{ Invoke-Rollback "{L('ins_err_novenv')}" }}

# --- 5. Autostart task (always re-registered) -----------------------------------
Write-Host "{L('ins_task')}"
try {{
    $Action = New-ScheduledTaskAction -Execute "$InstallDir\\venv\\Scripts\\pythonw.exe" -Argument "`"$InstallDir\\agent.py`"" -WorkingDirectory $InstallDir
    $TrigBoot  = New-ScheduledTaskTrigger -AtStartup
    $TrigWatch = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
    $Principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    $Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Seconds 0)  # 0 = no time limit
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $TrigBoot,$TrigWatch -Principal $Principal -Settings $Settings -Force -ErrorAction Stop | Out-Null
}} catch {{ Invoke-Rollback "registering the autostart task failed: $_" }}

# Maintenance tasks (non-fatal)
try {{ iwr "{backend_url}/agent-dist/elevate.ps1" -UseBasicParsing | iex }} catch {{ Write-Host "  (maintenance tasks: $_)" }}

# --- 6. Start + health check: new agent must register with the server -----------
Remove-Item "$InstallDir\\.connected", "$InstallDir\\.update_failed" -Force -ErrorAction SilentlyContinue
New-Item -ItemType File -Force "$InstallDir\\.updated" | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Waiting for the new agent to connect (max. $HealthTimeout s)..."
$ok = $false
for ($i = 0; $i -lt $HealthTimeout; $i++) {{
    if (Test-Path "$InstallDir\\.connected") {{ $ok = $true; break }}
    Start-Sleep -Seconds 1
}}
if (-not $ok) {{
    if (Test-Path "$InstallDir\\agent.log") {{ Get-Content "$InstallDir\\agent.log" -Tail 30 | ForEach-Object {{ Write-Host "  [agent.log] $_" }} }}
    Invoke-Rollback "new version did not connect to the server within $HealthTimeout s"
}}
Write-Host "New agent is connected."

# --- 7. Clean up ---------------------------------------------------------------
Remove-Item -Recurse -Force $BackupDir -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
if ((Test-Path $LegacyDir) -or [bool](Get-ScheduledTask -TaskName "RapalleRmmAgent" -ErrorAction SilentlyContinue)) {{
    foreach ($t in @("RapalleRmmAgent", "RapalleNode", "RapalleRmmUninstall", "RapalleRmmUpdate")) {{
        try {{ Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction SilentlyContinue }} catch {{}}
    }}
    Remove-Item -LiteralPath $LegacyDir -Recurse -Force -ErrorAction SilentlyContinue
    try {{ if ([System.Diagnostics.EventLog]::SourceExists("RapalleRMM")) {{ Remove-EventLog -Source "RapalleRMM" }} }} catch {{}}
    Write-Host "Legacy install (RAPALLE.net RMM) removed."
}}
Write-Host "=== {L('ins_done')} ==="
try {{ Stop-Transcript | Out-Null }} catch {{}}
Copy-Item $LogFile "$InstallDir\\update.log" -Force -ErrorAction SilentlyContinue
"""
    # During a move with a new token: exactly the same line as for BACKEND_URL, just for AGENT_TOKEN
    if new_token:
        def _add_token(m):
            line = m.group(0)
            return line + "\n" + line.replace("BACKEND_URL=.*", "AGENT_TOKEN=.*").replace(
                "BACKEND_URL=" + backend_url, "AGENT_TOKEN=" + new_token)
        script = _re.sub(r"^.*-replace 'BACKEND_URL=\.\*'.*$", _add_token, script, count=1, flags=_re.M)
    return PlainTextResponse(script, media_type="text/plain")


@router.get("/agent-dist/elevate.ps1", response_class=PlainTextResponse)
def agent_elevate_ps1(request: Request):
    """
    Sets up ONCE (to be run elevated) two pre-authorized SYSTEM tasks that are
    triggered by a Windows event:
        ByteraRmmUpdate    (EventID 812)  -> update.ps1
        ByteraRmmUninstall (EventID 811)  -> uninstall.ps1
    After that the agent can trigger update/uninstall by just writing an event -
    the task scheduler then runs the script as SYSTEM.

    Note: since the agent itself runs as a SYSTEM service (autostart task with
    trigger "AtStartup"), it no longer strictly needs this detour. The tasks
    remain as a fallback - e.g. for legacy installs that still run in a user
    session.

    Call on the client (as administrator):
        powershell -NoProfile -ExecutionPolicy Bypass -Command "iwr '<BACKEND>/agent-dist/elevate.ps1' -UseBasicParsing | iex"
    """
    backend_url = _backend_url(request)
    template = r'''$ErrorActionPreference = "Continue"
$Backend = "__BACKEND__"

Write-Host "ByteraRMM: setting up elevated update/uninstall (SYSTEM, event-triggered)..."

# Create the event source (idempotent). The agent later writes events of this
# source; the SYSTEM tasks are triggered by that.
try {
    if (-not [System.Diagnostics.EventLog]::SourceExists("ByteraRMM")) {
        New-EventLog -LogName Application -Source "ByteraRMM" -ErrorAction Stop
        Write-Host "  Event source 'ByteraRMM' created."
    } else {
        Write-Host "  Event source 'ByteraRMM' exists."
    }
} catch {
    Write-Host "  WARNING: event source could not be created: $_"
}

function Register-RmmSystemTask($name, $eventId, $scriptName) {
    # Task definition as XML (robust across all Windows versions; no CIM needed).
    # SYSTEM = SID S-1-5-18. EventTrigger listens for Application events of the
    # source 'ByteraRMM' with the matching EventID.
    $argLine = "-NoProfile -ExecutionPolicy Bypass -Command ""iwr '$Backend/agent-dist/$scriptName' -UseBasicParsing | iex"""
    $xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>ByteraRMM $name</Description>
  </RegistrationInfo>
  <Triggers>
    <EventTrigger>
      <Enabled>true</Enabled>
      <Subscription>&lt;QueryList&gt;&lt;Query Id="0" Path="Application"&gt;&lt;Select Path="Application"&gt;*[System[Provider[@Name='ByteraRMM'] and (EventID=$eventId)]]&lt;/Select&gt;&lt;/Query&gt;&lt;/QueryList&gt;</Subscription>
    </EventTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>S-1-5-18</UserId>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>powershell.exe</Command>
      <Arguments>$argLine</Arguments>
    </Exec>
  </Actions>
</Task>
"@
    $tmp = Join-Path $env:TEMP ("rmm_task_" + $name + ".xml")
    [System.IO.File]::WriteAllText($tmp, $xml, [System.Text.Encoding]::Unicode)
    # Remove an existing task, then recreate it from XML (as SYSTEM).
    schtasks /delete /tn $name /f 2>$null | Out-Null
    $out = schtasks /create /tn $name /xml "$tmp" /ru "SYSTEM" /f 2>&1
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    # Verify.
    schtasks /query /tn $name 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  -> Task '$name' (EventID $eventId) OK."
        return $true
    } else {
        Write-Host "  -> Task '$name' ERROR: $out"
        return $false
    }
}

$ok1 = Register-RmmSystemTask "ByteraRmmUpdate" 812 "update.ps1"
$ok2 = Register-RmmSystemTask "ByteraRmmUninstall" 811 "uninstall.ps1"

Write-Host ""
if ($ok1 -and $ok2) {
    Write-Host "DONE. Dashboard update/uninstall now also work for a NON-admin agent." -ForegroundColor Green
} else {
    Write-Host "ATTENTION: at least one task could not be created. Please run this window AS ADMINISTRATOR." -ForegroundColor Yellow
}
Write-Host "Check with:  schtasks /query /tn ByteraRmmUninstall"
'''
    script = template.replace("__BACKEND__", backend_url)
    return PlainTextResponse(script, media_type="text/plain")


@router.get("/agent-dist/uninstall.sh", response_class=PlainTextResponse)
def agent_uninstall_sh(request: Request, lang: str = ""):
    """
    Uninstalls the agent (Linux) SYNCHRONOUSLY: stop/remove the service, kill
    processes, then delete the program folder. Runs in its own systemd scope
    (see agent.py), so the script survives the stop of its own service and
    really deletes the files. NO background job.
    """
    # Sprache dieses Skripts: ?lang=de|en, sonst die Server-Sprache.
    _L = _lang(lang) if lang else server_lang()
    def L(key, **kw):
        return _t(key, _L, **kw)

    script = """#!/bin/bash
# BYTERA RMM - agent uninstall (Linux)
# Runs in its own systemd scope (see agent.py) and therefore survives the stop
# of its own service. Order: stop/remove the service -> kill processes ->
# delete the program folder (with retries).
INSTALL_DIR="/opt/bytera-rmm-agent"

echo "1) Stopping and removing the agent service..."
sudo systemctl stop bytera-agent 2>/dev/null || true
sudo systemctl disable bytera-agent 2>/dev/null || true
sudo systemctl kill bytera-agent 2>/dev/null || true
sudo rm -f /etc/systemd/system/bytera-agent.service
sudo systemctl daemon-reload 2>/dev/null || true
sudo systemctl reset-failed bytera-agent 2>/dev/null || true
# Remove an install under the old name (RAPALLE.net RMM) as well
LEGACY_DIR="/opt/rapalle-rmm-agent"
for u in rapalle-agent rapalle-node; do
  sudo systemctl disable --now "$u" 2>/dev/null || true
  sudo rm -f "/etc/systemd/system/$u.service"
done
sudo systemctl daemon-reload 2>/dev/null || true
sudo pkill -9 -f "$LEGACY_DIR" 2>/dev/null || true
sudo rm -rf "$LEGACY_DIR" 2>/dev/null || true

echo "2) Ending agent processes..."
# Several patterns so the process is hit reliably (venv path, agent.py,
# install folder). Two rounds, then a check.
for i in 1 2 3; do
  sudo pkill -9 -f "$INSTALL_DIR/agent.py" 2>/dev/null || true
  sudo pkill -9 -f "$INSTALL_DIR" 2>/dev/null || true
  sudo pkill -9 -f "bytera-rmm-agent" 2>/dev/null || true
  sleep 1
  if ! pgrep -f "$INSTALL_DIR/agent.py" >/dev/null 2>&1; then break; fi
done

echo "3) Deleting all agent data..."
# Up to 5 attempts in case a file is still briefly held.
for i in 1 2 3 4 5; do
  sudo rm -rf "$INSTALL_DIR" 2>/dev/null || true
  [ ! -e "$INSTALL_DIR" ] && break
  sleep 1
done

if [ -e "$INSTALL_DIR" ]; then
  echo "WARNING: $INSTALL_DIR could not be removed completely."
else
  echo "Agent uninstalled - all data removed."
fi
"""
    return PlainTextResponse(script, media_type="text/x-sh")


@router.get("/agent-dist/uninstall.ps1", response_class=PlainTextResponse)
def agent_uninstall_ps1(request: Request, lang: str = ""):
    """
    Uninstalls the agent (Windows) SYNCHRONOUSLY: remove the task, kill
    processes, then delete the program folder (no Start-Job that would be
    aborted when the shell exits).
    """
    # Sprache dieses Skripts: ?lang=de|en, sonst die Server-Sprache.
    _L = _lang(lang) if lang else server_lang()
    def L(key, **kw):
        return _t(key, _L, **kw)

    script = """# BYTERA RMM - agent uninstall (Windows)
# Preferably run as a SYSTEM task (elevated). Order:
# remove autostart -> hard-kill processes -> delete all data.
$ErrorActionPreference = "SilentlyContinue"
$InstallDir = "C:\\Program Files\\ByteraRmmAgent"

# Admin rights: restart via UAC if needed. As a SYSTEM task the script already
# runs elevated anyway - then this block does not apply.
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Restarting with administrator rights (UAC prompt) ..."
    if ($PSCommandPath) {
        $p = Start-Process -FilePath "powershell.exe" -Verb RunAs -PassThru -Wait `
             -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"")
        exit $p.ExitCode
    }
    Write-Host "Please open PowerShell as administrator." -ForegroundColor Red
    exit 1
}

Write-Host "1) Stopping and removing the agent autostart..."
schtasks /end /tn "ByteraRmmAgent" 2>$null | Out-Null
schtasks /delete /tn "ByteraRmmAgent" /f 2>$null | Out-Null
Unregister-ScheduledTask -TaskName "ByteraRmmAgent" -Confirm:$false
# Remove an install under the old name (RAPALLE.net RMM) as well
$LegacyDir = "C:\\Program Files\\RapalleRmmAgent"
foreach ($t in @("RapalleRmmAgent", "RapalleNode")) {
    schtasks /end /tn $t 2>$null | Out-Null
    schtasks /delete /tn $t /f 2>$null | Out-Null
}
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like "*RapalleRmmAgent*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1
Remove-Item -LiteralPath $LegacyDir -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "2) Ending agent processes..."
for ($i = 0; $i -lt 4; $i++) {
    $ids = @()
    $ids += (Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path -like "$InstallDir*" }).Id
    $ids += (Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -like "*ByteraRmmAgent*" -or $_.CommandLine -like "*agent.py*" -or
        ($_.ExecutablePath -and $_.ExecutablePath -like "$InstallDir*")
    }).ProcessId
    $ids = $ids | Where-Object { $_ } | Sort-Object -Unique
    if (-not $ids) { break }
    foreach ($id in $ids) {
        Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
        cmd /c "taskkill /PID $id /F /T" 2>$null | Out-Null
    }
    Start-Sleep -Seconds 2
}
Start-Sleep -Seconds 2

Write-Host "3) Deleting all agent data..."
for ($i = 0; $i -lt 6; $i++) {
    if (-not (Test-Path $InstallDir)) { break }
    # Remove read-only/attributes, then delete using two methods.
    cmd /c "attrib -r -s -h `"$InstallDir\\*.*`" /s /d" 2>$null | Out-Null
    Remove-Item -LiteralPath $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path $InstallDir) { cmd /c "rmdir /s /q `"$InstallDir`"" 2>$null | Out-Null }
    Start-Sleep -Seconds 2
}

# If started via SYSTEM task/event: remove the helper tasks + event source again.
schtasks /delete /tn "ByteraRmmUninstall" /f 2>$null | Out-Null
schtasks /delete /tn "ByteraRmmUpdate" /f 2>$null | Out-Null
try { Remove-EventLog -Source "ByteraRMM" -ErrorAction SilentlyContinue } catch {}
schtasks /delete /tn "RapalleRmmUninstall" /f 2>$null | Out-Null
schtasks /delete /tn "RapalleRmmUpdate" /f 2>$null | Out-Null
try { Remove-EventLog -Source "RapalleRMM" -ErrorAction SilentlyContinue } catch {}

if (Test-Path $InstallDir) {
    Write-Host "WARNING: $InstallDir could not be removed completely (a file may still be locked). Try again after a reboot."
} else {
    Write-Host "Agent uninstalled - all data removed."
}
"""
    return PlainTextResponse(script, media_type="text/plain")


# ==========================================================================
# READY-MADE INSTALLER PACKAGES (.exe / .msi / .deb / .rpm / .run)
# --------------------------------------------------------------------------
# They are built locally with "python src/backend/app/build_installers.py" - the
# packages land in the dist/ folder at the project root. These routes make them
# usable in the dashboard: list them, build them at the push of a button and
# download them via the onboarding page.
#
# The onboarding token is NOT baked into the package (that would be a separate
# build per token). Instead the installer gets it at call time:
#     Windows:  ByteraRmmAgent-Setup.exe /S /TOKEN=<token>
#     Linux:    sudo RMM_ENROLLMENT_TOKEN=<token> ./bytera-rmm-agent.run
# Without a token the device lands in "Uncategorized" and can be assigned later.
# ==========================================================================

def _installer_hint(name: str, token: str, backend_url: str = "") -> str:
    """
    Matching call command per package kind - with the REAL values of this
    installation (backend address, onboarding token). The command can thus be
    copied unchanged; nobody has to replace placeholders anymore.
    """
    n = name.lower()
    if n.endswith(".exe"):
        return f"ByteraRmmAgent-Setup.exe /S /TOKEN={token}"
    if n.endswith(".msi"):
        # The Windows Installer does not pass custom properties to the
        # (deferred) action -> the device lands in "Uncategorized".
        return f"msiexec /i {name} /qn"
    if n.endswith(".bat"):
        return f"{name} {backend_url} {token}"
    if n.endswith(".ps1"):
        return f"powershell -ExecutionPolicy Bypass -File {name} -Token {token}"
    if n.endswith(".pkg.tar.xz"):
        return f"sudo RMM_ENROLLMENT_TOKEN={token} pacman -U {name}"
    if n.endswith(".deb"):
        return f"sudo RMM_ENROLLMENT_TOKEN={token} apt install ./{name}"
    if n.endswith(".rpm"):
        return f"sudo RMM_ENROLLMENT_TOKEN={token} dnf install ./{name}"
    if n.endswith((".tar.gz", ".tgz")):
        return f"tar xzf {name} && cd */ && sudo RMM_ENROLLMENT_TOKEN={token} ./install.sh"
    return f"sudo RMM_ENROLLMENT_TOKEN={token} ./{name}"


PROJECT_ROOT_DIR = _PROJ_ROOT
INSTALLER_DIST_DIR = _DIST / "agent"  # storage/dist/agent -> appears under /dist/agent
BUILD_SCRIPT = _APP_DIR / "build_installers.py"

# File-name pattern -> display in the dashboard.
# Order matters: check the more specific endings first.
_INSTALLER_KINDS = [
    (".exe", "Windows setup", "🪟", "exe"),
    (".msi", "Windows MSI (GPO/Intune)", "🪟", "msi"),
    (".bat", "Windows batch (double-click)", "🪟", "bat"),
    (".ps1", "Windows PowerShell script", "🪟", "ps1"),
    (".pkg.tar.xz", "Arch / Manjaro", "🐧", "pkg"),
    (".deb", "Debian / Ubuntu", "🐧", "deb"),
    (".rpm", "Fedora / RHEL / SUSE", "🐧", "rpm"),
    (".tar.gz", "Linux archive (tar.gz)", "🐧", "tgz"),
    (".tgz", "Linux archive (tgz)", "🐧", "tgz"),
    (".sh", "Linux (self-extracting)", "🐧", "sh"),
    (".run", "Linux (self-extracting, .run)", "🐧", "sh"),
]


def _installer_entries() -> list[dict]:
    """All built packages in dist/ (newest first)."""
    out = []
    if not INSTALLER_DIST_DIR.is_dir():
        return out
    for f in sorted(INSTALLER_DIST_DIR.iterdir()):
        if not f.is_file():
            continue
        for suffix, label, icon, target in _INSTALLER_KINDS:
            if f.name.lower().endswith(suffix):
                st = f.stat()
                out.append({
                    "name": f.name,
                    "label": label,
                    "icon": icon,
                    "target": target,
                    "size": st.st_size,
                    "built_at": int(st.st_mtime),
                })
                break
    out.sort(key=lambda e: e["built_at"], reverse=True)
    return out


@router.get("/api/enrollment/installers")
def list_installers(user: dict = Depends(get_current_user)):
    """
    What ready-made packages are available - and what could be built here?
    Used by the "Add client" window.
    """
    require_perm(user, "add_client")
    can = {}
    try:
        import shutil as _sh
        import platform as _pl
        # exe/msi: the build script pulls in the tools itself when needed
        # (PyInstaller via pip, WiX as a download) - so Windows is enough.
        # Script/archive packages need no tool at all.
        is_win = _pl.system() == "Windows"
        can = {
            "exe": is_win,
            "msi": is_win,
            "bat": True,
            "ps1": True,
            "sh": True,
            "tgz": True,
            "pkg": True,
            "deb": bool(_sh.which("dpkg-deb")),
            "rpm": bool(_sh.which("rpmbuild")),
        }
    except Exception:
        can = {"sh": True}
    return {
        "installers": _installer_entries(),
        "can_build": can,
        "build_available": BUILD_SCRIPT.is_file(),
        "dist_dir": str(INSTALLER_DIST_DIR),
    }


class BuildInstallersBody(BaseModel):
    targets: str = "auto"       # "auto" or e.g. "sh,deb"


@router.post("/api/enrollment/installers/build")
async def build_installers(body: BuildInstallersBody, request: Request, background: bool = False,
                           user: dict = Depends(get_current_user)):
    """
    Starts src/backend/app/build_installers.py as a BACKEND JOB (app/bgjobs.py):
    the output streams live into the job log, reloading does not abort the build.
    Without ?background=1 the request waits for the result as before.
    Admins only - a process is started on the server here.
    """
    require_admin(user)
    if not BUILD_SCRIPT.is_file():
        raise HTTPException(404, "src/backend/app/build_installers.py not found")

    import asyncio as _asyncio
    import sys as _sys

    targets = (body.targets or "auto").strip()
    if not all(c.isalnum() or c in ",_-" for c in targets):
        raise HTTPException(400, "Invalid target specification")

    cmd = [_sys.executable, str(BUILD_SCRIPT),
           "--targets", targets,
           "--backend-url", _backend_url(request),
           "--out", str(INSTALLER_DIST_DIR)]
    async def work(job):
        job.update(phase="build", detail=f"targets: {targets}")
        try:
            proc = await _asyncio.create_subprocess_exec(
                *cmd, cwd=str(PROJECT_ROOT_DIR),
                stdout=_asyncio.subprocess.PIPE, stderr=_asyncio.subprocess.STDOUT)
        except Exception as e:
            raise HTTPException(500, f"Build could not be started: {e}")
        lines: list[str] = []

        async def pump():
            async for raw in proc.stdout:
                ln = raw.decode("utf-8", "replace").rstrip()
                lines.append(ln)
                job.log(ln)
                job.update(detail=ln[:200])
        try:
            await _asyncio.wait_for(_asyncio.gather(pump(), proc.wait()), timeout=1800)
        except _asyncio.TimeoutError:
            proc.kill()
            raise HTTPException(504, "Build time limit (30 minutes) exceeded")
        except _asyncio.CancelledError:
            proc.kill()
            raise
        db.add_audit_entry(user["username"], "enrollment.build_installers",
                           details=f"targets={targets} rc={proc.returncode}")
        if proc.returncode != 0:
            job.update(detail=f"Build ended with error code {proc.returncode}")
        return {"ok": proc.returncode == 0, "returncode": proc.returncode,
                "log": "\n".join(lines)[-8000:], "installers": _installer_entries()}

    return await bgjobs.run("installers.build", "all", title=f"Build installers ({targets})", user=user,
                            work=work, background=background, perm="admin",
                            on_cancel=bgjobs.hard_cancel, meta={"targets": targets})


@router.get("/enroll/{token}/installer/{name}")
def download_installer(token: str, name: str):
    """
    Download a ready-made package via the onboarding URL (no login needed -
    the token in the URL is the proof, just like for agent.zip).
    """
    _require_valid_token(token)
    # No path escape: only exactly the files that count as a package in dist/.
    if name not in {e["name"] for e in _installer_entries()}:
        raise HTTPException(404, "Package not found")
    path = INSTALLER_DIST_DIR / name
    from fastapi.responses import FileResponse
    return FileResponse(path, filename=name, media_type="application/octet-stream")
