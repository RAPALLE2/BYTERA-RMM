"""
bootstrap_deps.py
-----------------
Makes sure that ALL required Python libraries are present - and installs
missing ones automatically (pip). So nobody has to run
"pip install -r requirements.txt" by hand; a missing ldap3 (or another lib) no
longer blocks startup.

Flow:
  1. Read requirements.txt (extract package names).
  2. For each package, check whether it is importable.
  3. If something is missing: first try a bulk "pip install -r requirements.txt",
     then the still-missing individual packages specifically.

Deliberately kept lean and without hard errors: if pip cannot install
(e.g. no internet, no write permission), the backend keeps running anyway -
the affected feature then reports it itself later (e.g. LDAP login).
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

# Package on PyPI  ->  importable module name (only where they differ).
_IMPORT_NAME = {
    "python-socketio": "socketio",
    "python-dotenv": "dotenv",
    "PyJWT": "jwt",
    "Pillow": "PIL",
    "uvicorn[standard]": "uvicorn",
    "python-multipart": "multipart",
    "psycopg2-binary": "psycopg2",
    "PyMySQL": "pymysql",
    "pyinstaller": "PyInstaller",
}


# Safety net: without these packages the backend does NOT start AT ALL. They are
# installed even if they are accidentally missing from requirements.txt - that is
# exactly what happened with "python-multipart" and caused an endless
# crash-restart loop (FastAPI requires it as soon as a router offers uploads or
# forms).
_ESSENTIAL = {
    # import name : package name on PyPI
    "fastapi": "fastapi",
    "uvicorn": "uvicorn[standard]",
    "socketio": "python-socketio",
    "dotenv": "python-dotenv",
    "jwt": "PyJWT",
    "bcrypt": "bcrypt",
    "pydantic": "pydantic",
    "multipart": "python-multipart",
    # Database drivers for switching to MySQL/MariaDB resp. PostgreSQL.
    # They are in requirements.txt, but existing installations were built
    # before this change - then the switch failed with "driver missing". Here it
    # belongs in the safety net so it is pulled in on the next start without
    # any action.
    "pymysql": "PyMySQL",
    "psycopg2": "psycopg2-binary",
    # SSH part of the SFTP access at the relay.
    "paramiko": "paramiko",
}


def _pkg_to_module(pkg: str) -> str:
    base = pkg.split("[")[0]
    return _IMPORT_NAME.get(pkg, _IMPORT_NAME.get(base, base)).replace("-", "_")


def _parse_requirements(req_file: Path) -> list[str]:
    pkgs: list[str] = []
    try:
        for raw in req_file.read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            # Keep only the package name (without version constraint).
            name = line
            for sep in ("==", ">=", "<=", "~=", ">", "<", "!="):
                if sep in name:
                    name = name.split(sep, 1)[0].strip()
                    break
            pkgs.append(name)
    except OSError:
        pass
    return pkgs


def _missing(pkgs: list[str]) -> list[str]:
    miss = []
    for pkg in pkgs:
        mod = _pkg_to_module(pkg)
        try:
            importlib.import_module(mod)
        except Exception:
            miss.append(pkg)
    return miss


def _user_site() -> str | None:
    """The personal package folder (~/.local/lib/...) - if usable."""
    try:
        import site
        return site.getusersitepackages()
    except Exception:
        return None


def _refresh_sys_path() -> None:
    """
    After an installation with "--user" the package sits in a folder this
    process may not know about yet: Python determines sys.path ONCE at startup,
    and if ~/.local/lib/... did not exist back then, the entry is missing.
    Without this refresh the import would still fail right after a successful
    installation.
    """
    us = _user_site()
    if us and us not in sys.path:
        sys.path.append(us)
    try:
        import site
        site.main()          # rebuilds the path list
    except Exception:
        pass
    importlib.invalidate_caches()


def _pip(args: list[str]) -> bool:
    """
    Call pip - with fallback levels for environments without write permissions.

    Typical cases:
      * unprivileged container / process does not run as root
        -> system-wide site-packages is read-only
      * newer distributions (PEP 668, "externally-managed-environment")
        -> pip refuses the system-wide installation outright

    Therefore in order: normal -> --user -> --user --break-system-packages.
    As soon as an attempt works, sys.path is refreshed.
    """
    import os
    attempts = [[], ["--user"], ["--user", "--break-system-packages"],
                ["--break-system-packages"]]
    env = dict(os.environ)
    # pip needs a writable HOME (cache, ~/.local). If HOME is not usable -
    # often the case in a container - we fall back to /tmp.
    home = env.get("HOME") or ""
    if not home or not os.access(home, os.W_OK):
        env["HOME"] = "/tmp"
    env.setdefault("PIP_CACHE_DIR", os.path.join(env["HOME"], ".cache", "pip"))

    last = ""
    for extra in attempts:
        try:
            subprocess.run([sys.executable, "-m", "pip", *args, *extra],
                           check=True, capture_output=True, text=True,
                           timeout=600, env=env)
            if extra:
                print(f"[bootstrap] pip installation succeeded with {' '.join(extra)}")
            _refresh_sys_path()
            return True
        except subprocess.CalledProcessError as e:
            last = ((e.stderr or "") + (e.stdout or "")).strip()
            low = last.lower()
            # The next attempt is only worthwhile for permission / PEP-668 issues.
            if not any(k in low for k in ("permission denied", "externally-managed",
                                          "read-only file system", "errno 13",
                                          "consider using the `--user` option",
                                          "defaulting to user installation")):
                break
        except Exception as e:
            last = str(e)
            break

    print(f"[bootstrap] pip {' '.join(args)} failed: {last[:600]}")
    return False


def ensure_dependencies(req_file: Path | None = None) -> None:
    """Installs missing dependencies from requirements.txt."""
    if req_file is None:
        # backend/app/bootstrap_deps.py -> src/backend/data/requirements.txt
        req_file = Path(__file__).resolve().parent.parent / "data" / "requirements.txt"

    pkgs = _parse_requirements(req_file)

    # Add essential packages in case they are missing from requirements.txt.
    for mod, pypi in _ESSENTIAL.items():
        if not any(_pkg_to_module(p) == mod for p in pkgs):
            print(f"[bootstrap] Note: {pypi} is not in requirements.txt, "
                  f"but is strictly required - adding it.")
            pkgs.append(pypi)

    if not pkgs:
        return
    missing = _missing(pkgs)
    if not missing:
        return

    print(f"[bootstrap] Missing packages: {', '.join(missing)} - installing automatically...")
    # First the bundled attempt (resolves version conflicts best).
    if req_file.is_file():
        _pip(["install", "-r", str(req_file)])

    # Whatever is still missing afterwards, pull in individually.
    still = _missing(missing)
    for pkg in still:
        print(f"[bootstrap] installing {pkg}...")
        _pip(["install", pkg])

    final = _missing(pkgs)
    if final:
        print(f"[bootstrap] WARNING: could not install: {', '.join(final)} "
              f"- affected features may be limited.")
        _explain_environment(final)
    else:
        print("[bootstrap] All dependencies present.")


def _explain_environment(missing: list[str]) -> None:
    """
    On failure, says specifically WHAT the cause is - otherwise one only sees
    "ModuleNotFoundError" and looks in the wrong place.

    Most common cause in unprivileged containers: the process runs under a UID
    that can neither write to site-packages nor read the existing package files
    (permissions 0700 resp. shifted UIDs with LXC bind mounts).
    """
    import getpass
    import os
    import site

    try:
        who = f"{getpass.getuser()} (uid={os.getuid()}, gid={os.getgid()})"
    except Exception:
        who = "unknown"

    print("[bootstrap] ---------------------------------------------------")
    print(f"[bootstrap] Process runs as: {who}")
    print(f"[bootstrap] Python:             {sys.executable}")
    try:
        targets = site.getsitepackages()
    except Exception:
        targets = []
    for d in targets[:3]:
        if not os.path.isdir(d):
            print(f"[bootstrap]   {d}  ->  does not exist")
            continue
        state = "writable" if os.access(d, os.W_OK) else "READ ONLY"
        readable = "readable" if os.access(d, os.R_OK | os.X_OK) else "NOT READABLE"
        print(f"[bootstrap]   {d}  ->  {state}, {readable}")
    us = _user_site()
    if us:
        print(f"[bootstrap]   personal: {us} "
              f"({'present' if os.path.isdir(us) else 'missing'})")
    print("[bootstrap] Possible remedy:")
    print("[bootstrap]   * start the container as root OR give the user read")
    print("[bootstrap]     permissions:  chmod -R a+rX <site-packages> /app")
    print("[bootstrap]   * unprivileged LXC: bind mounts are UID-shifted -")
    print("[bootstrap]     files must be readable/traversable for 'others' (a+rX)")
    print("[bootstrap]   * install packages in advance:")
    print(f"[bootstrap]     {sys.executable} -m pip install --user -r src/backend/data/requirements.txt")
    print("[bootstrap] ---------------------------------------------------")
