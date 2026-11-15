# install.py (module "browser") - provide Chromium for the server browser
# ---------------------------------------------------------------------
# The Python package "playwright" is installed by the module itself (module.json
# "pip"). The browser engine is provided here - as a backend job (live in the
# dashboard), fully automatic:
#
#   1. Chromium already starts?                      -> done
#   2. python -m playwright install chromium         (download, kept in
#      data/modules/browser/ms-playwright - survives container rebuilds)
#   3. still does not start -> missing system libraries (typical for the slim
#      Docker image). As root:
#        a) python -m playwright install-deps chromium
#        b) whatever is STILL missing: ldd on the Chromium binary -> missing .so
#           files -> matching Debian/Ubuntu packages (old and new "t64" names)
#           -> apt-get install of those that exist
#   4. a system Chromium/Chrome is used as a last resort
#
# System libraries live in the container, not in the data volume: after a
# container REBUILD the startup check notices it and runs this job again
# (the download is skipped, only step 3 runs).
from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
from pathlib import Path

from app.paths import MODULES_DATA_DIR

BROWSERS_PATH = MODULES_DATA_DIR / "browser" / "ms-playwright"
SYSTEM_CANDIDATES = ["chromium", "chromium-browser", "google-chrome", "google-chrome-stable"]

# Libraries headless Chromium needs -> package candidates (Debian 12/13, Ubuntu).
# Only candidates apt knows are installed, so old and new names can be listed.
LIB_PACKAGES = {
    "libnss3.so": ["libnss3"], "libnssutil3.so": ["libnss3"], "libsmime3.so": ["libnss3"],
    "libnspr4.so": ["libnspr4"], "libplc4.so": ["libnspr4"], "libplds4.so": ["libnspr4"],
    "libatk-1.0.so.0": ["libatk1.0-0t64", "libatk1.0-0"],
    "libatk-bridge-2.0.so.0": ["libatk-bridge2.0-0t64", "libatk-bridge2.0-0"],
    "libatspi.so.0": ["libatspi2.0-0t64", "libatspi2.0-0"],
    "libcups.so.2": ["libcups2t64", "libcups2"],
    "libdrm.so.2": ["libdrm2"], "libgbm.so.1": ["libgbm1"],
    "libxkbcommon.so.0": ["libxkbcommon0"], "libxcomposite.so.1": ["libxcomposite1"],
    "libxdamage.so.1": ["libxdamage1"], "libxfixes.so.3": ["libxfixes3"],
    "libxrandr.so.2": ["libxrandr2"], "libXcomposite.so.1": ["libxcomposite1"],
    "libXdamage.so.1": ["libxdamage1"], "libXfixes.so.3": ["libxfixes3"],
    "libXrandr.so.2": ["libxrandr2"], "libXext.so.6": ["libxext6"], "libX11.so.6": ["libx11-6"],
    "libxcb.so.1": ["libxcb1"], "libXi.so.6": ["libxi6"], "libXtst.so.6": ["libxtst6"],
    "libXrender.so.1": ["libxrender1"], "libXcursor.so.1": ["libxcursor1"],
    "libasound.so.2": ["libasound2t64", "libasound2"],
    "libpango-1.0.so.0": ["libpango-1.0-0"], "libpangocairo-1.0.so.0": ["libpangocairo-1.0-0"],
    "libcairo.so.2": ["libcairo2"], "libglib-2.0.so.0": ["libglib2.0-0t64", "libglib2.0-0"],
    "libgobject-2.0.so.0": ["libglib2.0-0t64", "libglib2.0-0"],
    "libgio-2.0.so.0": ["libglib2.0-0t64", "libglib2.0-0"],
    "libdbus-1.so.3": ["libdbus-1-3"], "libexpat.so.1": ["libexpat1"],
    "libfontconfig.so.1": ["libfontconfig1"], "libfreetype.so.6": ["libfreetype6"],
    "libgtk-3.so.0": ["libgtk-3-0t64", "libgtk-3-0"], "libxshmfence.so.1": ["libxshmfence1"],
    "libudev.so.1": ["libudev1"], "libuuid.so.1": ["libuuid1"],
}
# Always installed as root in step 3b (alternatives: first installable name wins;
# fonts: otherwise pages show boxes instead of text)
BASE_PACKAGES = [["libnss3"], ["libnspr4"], ["libdbus-1-3"], ["libatk1.0-0t64", "libatk1.0-0"],
                 ["libatk-bridge2.0-0t64", "libatk-bridge2.0-0"], ["libatspi2.0-0t64", "libatspi2.0-0"],
                 ["libcups2t64", "libcups2"], ["libdrm2"], ["libgbm1"], ["libxkbcommon0"], ["libxcomposite1"],
                 ["libxdamage1"], ["libxfixes3"], ["libxrandr2"], ["libasound2t64", "libasound2"],
                 ["libpango-1.0-0"], ["libcairo2"], ["libxshmfence1"], ["fonts-liberation"],
                 ["fonts-noto-color-emoji"], ["fonts-dejavu-core"], ["ca-certificates"]]

_state = {"status": "unknown", "detail": "", "executable": None}


def env() -> dict:
    e = dict(os.environ)
    e["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS_PATH)
    e["DEBIAN_FRONTEND"] = "noninteractive"
    return e


def prepare_env() -> None:
    """Call before Playwright starts (the driver reads the variable)."""
    BROWSERS_PATH.mkdir(parents=True, exist_ok=True)
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS_PATH)


def system_browser() -> str | None:
    if os.getenv("RMM_BROWSER_EXECUTABLE"):
        return os.getenv("RMM_BROWSER_EXECUTABLE")
    for name in SYSTEM_CANDIDATES:
        p = shutil.which(name)
        if p:
            return p
    return None


def status() -> dict:
    return dict(_state)


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _bundled_binaries() -> list[Path]:
    """Chromium binaries downloaded by Playwright (headless shell + full chromium)."""
    out = []
    if BROWSERS_PATH.is_dir():
        for pat in ("chromium_headless_shell-*/*/chrome-headless-shell", "chromium-*/chrome-linux*/chrome"):
            out += [p for p in BROWSERS_PATH.glob(pat) if p.is_file()]
    return out


def _short_error(e: Exception) -> str:
    """The useful line of a launch error (Playwright puts the browser log behind it)."""
    full = str(e)
    m = re.search(r"error while loading shared libraries: ([^\s:]+)", full)
    if m:
        return f"system library missing: {m.group(1)}"
    if "Host system is missing dependencies" in full:
        return "system libraries are missing"
    first = full.strip().splitlines()[0] if full.strip() else repr(e)
    return first[:400]


async def probe() -> tuple[bool, str]:
    """Start Chromium once. -> (ok, error text)"""
    try:
        from playwright.async_api import async_playwright
    except Exception:
        return False, "Python package 'playwright' is missing"
    prepare_env()
    try:
        async with async_playwright() as pw:
            exe = system_browser()
            kw = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"], "timeout": 30000}
            try:
                b = await pw.chromium.launch(**kw)
                _state["executable"] = None
            except Exception as first:
                if not exe:
                    raise first
                try:
                    b = await pw.chromium.launch(executable_path=exe, **kw)
                except Exception:
                    raise first
                _state["executable"] = exe
            page = await b.new_page()                # really render something
            await page.set_content("<p>ok</p>")
            await b.close()
        _state.update(status="ready", detail="")
        return True, ""
    except Exception as e:
        msg = _short_error(e)
        _state.update(status="missing", detail=msg, full=str(e)[-3000:])
        return False, msg


async def _run(job, args: list[str], progress: tuple[float, float] | None = None, label: str = "") -> int:
    """Run a command, log its output; download progress ("45%") moves the job bar."""
    job.log("$ " + " ".join(args))
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT, env=env())
    assert proc.stdout
    last_pct = -1
    buf = b""
    while True:
        chunk = await proc.stdout.read(4096)
        if not chunk:
            break
        buf += chunk
        parts = re.split(rb"[\r\n]", buf)
        buf = parts.pop()
        for raw in parts:
            txt = raw.decode(errors="replace").strip()
            if not txt:
                continue
            m = re.search(r"(\d{1,3})%\s+of\s+([\d.]+\s*\w+)", txt)
            if m and progress:
                pct = int(m.group(1))
                if pct != last_pct:
                    last_pct = pct
                    lo, hi = progress
                    job.update(pct=lo + (hi - lo) * pct / 100, detail=f"{label} {pct}% of {m.group(2)}")
                    if pct in (0, 25, 50, 75, 100):
                        job.log(f"{label} {pct}% of {m.group(2)}")
                continue
            clean = re.sub(r"[▀-▟|]+", "", txt).strip()   # no progress-bar blocks in the log
            if clean:
                job.log(clean[:300])
    return await proc.wait()


async def _missing_libs() -> list[str]:
    libs: set[str] = set()
    for binary in _bundled_binaries():
        try:
            proc = await asyncio.create_subprocess_exec("ldd", str(binary), stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.STDOUT)
            out, _ = await proc.communicate()
            for line in out.decode(errors="replace").splitlines():
                if "not found" in line:
                    libs.add(line.split("=>")[0].strip())
        except Exception:
            pass
    return sorted(libs)


async def _installable(pkg: str) -> bool:
    """apt has a real candidate (virtual packages like 'libasound2' on Debian 13 have none)."""
    proc = await asyncio.create_subprocess_exec("apt-cache", "policy", pkg, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL, env=env())
    out, _ = await proc.communicate()
    m = re.search(r"Candidate:\s*(\S+)", out.decode(errors="replace"))
    return bool(m and m.group(1) != "(none)")


async def _pick(groups: list[list[str]]) -> list[str]:
    """One package per group: the first name apt can install."""
    out = []
    for g in groups:
        for name in g:
            if name in out:
                break
            if await _installable(name):
                out.append(name)
                break
    return out


async def _install_system_libs(job) -> None:
    if not _is_root():
        raise RuntimeError("System libraries for Chromium are missing and the backend does not run as root. "
                           "Run once as root on the server: python -m playwright install-deps chromium")
    if not shutil.which("apt-get"):
        raise RuntimeError("System libraries for Chromium are missing and this system has no apt-get. "
                           "Install Chromium with your package manager (it is then used automatically).")
    job.update(phase="dependencies", pct=70, detail="Installing system libraries (apt) …")
    await _run(job, ["apt-get", "update"])
    # a) Playwright's own list (fails on distributions it doesn't know - that's fine)
    await _run(job, [sys.executable, "-m", "playwright", "install-deps", "chromium"])
    if await _missing_libs() or not (await probe())[0]:
        # b) exactly what is still missing + base set (fonts etc.)
        missing = await _missing_libs()
        if missing:
            job.log("still missing: " + ", ".join(missing))
        groups = [LIB_PACKAGES[lib] for lib in missing if lib in LIB_PACKAGES] + BASE_PACKAGES
        pkgs = await _pick(groups)
        unknown = [lib for lib in missing if lib not in LIB_PACKAGES]
        if unknown:
            job.log("no known package for: " + ", ".join(unknown))
        if pkgs:
            job.update(phase="dependencies", pct=85, detail=f"Installing {len(pkgs)} system packages …")
            rc = await _run(job, ["apt-get", "install", "-y", "--no-install-recommends", *pkgs])
            if rc != 0:                                # one bad package must not block the rest
                for pk in pkgs:
                    await _run(job, ["apt-get", "install", "-y", "--no-install-recommends", pk])


async def install(job) -> dict:
    """Job: make Chromium start (download + system libraries), fully automatic."""
    _state.update(status="installing", detail="")
    job.update(phase="check", pct=5, detail="Checking the installed browser …")
    ok, why = await probe()
    if ok:
        job.update(phase="done", pct=100, detail="The browser is ready")
        return {"ready": True}
    job.log(f"Chromium does not start yet: {why}")
    job.log(_state.get("full") or "")
    prepare_env()
    if not _bundled_binaries():
        job.update(phase="download", pct=10, detail="Downloading Chromium …")
        rc = await _run(job, [sys.executable, "-m", "playwright", "install", "chromium"],
                        progress=(10, 65), label="Downloading Chromium …")
        if rc != 0:
            _state.update(status="missing", detail="Download failed")
            raise RuntimeError("Chromium download failed (does the server have internet access?) – see the log")
        ok, why = await probe()
    if not ok:
        await _install_system_libs(job)
        job.update(phase="check", pct=95, detail="Starting Chromium …")
        ok, why = await probe()
    if not ok:
        job.log(_state.get("full") or "")
        missing = await _missing_libs()
        if missing:
            why += " – still missing: " + ", ".join(missing)
        _state.update(status="missing", detail=why)
        raise RuntimeError(f"Chromium does not start: {why}")
    job.update(phase="done", pct=100, detail="The browser is ready")
    return {"ready": True}


def start_install_job(user: dict | None):
    from app import bgjobs
    job, _new = bgjobs.start("browser.install", "chromium", title="Server browser: install Chromium",
                             user=user, runner=install, perm="manage_modules")
    return job
