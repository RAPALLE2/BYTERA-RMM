# install.py (Modul "browser") - Chromium fuer den Server-Browser bereitstellen
# ---------------------------------------------------------------------------
# Das Python-Paket "playwright" installiert das Modul (module.json "pip"). Den
# Browser selbst laedt dieser Teil - als Backend-Job (live im Dashboard):
#
#   1. vorhandenes System-Chromium/Chrome benutzen, falls da (kein Download)
#   2. sonst:  python -m playwright install chromium
#      Ziel: data/modules/browser/ms-playwright  (bleibt bei Container-Neubau)
#   3. fehlen Systembibliotheken (typisch im schlanken Docker-Image) und laeuft
#      das Backend als root:  python -m playwright install-deps chromium
#      (apt; nach einem Container-NEUBAU wird das beim Start automatisch wiederholt)
from __future__ import annotations

import asyncio
import os
import shutil
import sys

from app.paths import MODULES_DATA_DIR

BROWSERS_PATH = MODULES_DATA_DIR / "browser" / "ms-playwright"
SYSTEM_CANDIDATES = ["chromium", "chromium-browser", "google-chrome", "google-chrome-stable"]

_state = {"status": "unknown", "detail": "", "executable": None}


def env() -> dict:
    e = dict(os.environ)
    e["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS_PATH)
    return e


def prepare_env() -> None:
    """Vor dem Start von Playwright aufrufen (Driver liest die Variable)."""
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


async def probe() -> tuple[bool, str]:
    """Startet Chromium einmal kurz. -> (ok, Fehlertext)"""
    try:
        from playwright.async_api import async_playwright
    except Exception:
        return False, "Python package 'playwright' is missing"
    prepare_env()
    try:
        async with async_playwright() as pw:
            exe = system_browser()
            kw = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
            try:
                b = await pw.chromium.launch(**kw)
                _state["executable"] = None
            except Exception as first:
                if not exe:
                    raise first
                b = await pw.chromium.launch(executable_path=exe, **kw)
                _state["executable"] = exe
            await b.close()
        _state.update(status="ready", detail="")
        return True, ""
    except Exception as e:
        msg = str(e).strip().splitlines()[0][:400] if str(e).strip() else repr(e)
        _state.update(status="missing", detail=msg)
        return False, msg


async def _run(job, args: list[str]) -> int:
    job.log("$ " + " ".join(args))
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT, env=env())
    assert proc.stdout
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        txt = line.decode(errors="replace").rstrip()
        if txt:
            job.log(txt[:300])
            if "%" in txt:
                job.update(detail=txt[-120:])
    return await proc.wait()


async def install(job) -> dict:
    """Job-Arbeit: Chromium laden (+ Systembibliotheken), danach Probe-Start."""
    _state.update(status="installing", detail="")
    job.update(phase="check", pct=5, detail="Checking the installed browser …")
    ok, why = await probe()
    if ok:
        job.update(phase="done", pct=100, detail="The browser is ready")
        return {"ready": True}
    prepare_env()
    job.update(phase="download", pct=15, detail="Downloading Chromium …")
    rc = await _run(job, [sys.executable, "-m", "playwright", "install", "chromium"])
    if rc != 0:
        _state.update(status="missing", detail="Download failed")
        raise RuntimeError("Chromium download failed (does the server have internet access?) – see the log for details")
    ok, why = await probe()
    if not ok and ("shared lib" in why or "error while loading" in why or "dependencies" in why.lower()
                   or "Host system is missing" in why):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            job.update(phase="dependencies", pct=70, detail="Installing system libraries (apt) …")
            await _run(job, [sys.executable, "-m", "playwright", "install-deps", "chromium"])
            ok, why = await probe()
        else:
            why += " – system libraries are missing; run as root: python -m playwright install-deps chromium"
    if not ok:
        _state.update(status="missing", detail=why)
        raise RuntimeError(f"Chromium does not start: {why}")
    job.update(phase="done", pct=100, detail="The browser is ready")
    return {"ready": True}


def start_install_job(user: dict | None):
    from app import bgjobs
    job, _new = bgjobs.start("browser.install", "chromium", title="Server browser: install Chromium",
                             user=user, runner=install, perm="manage_modules")
    return job
