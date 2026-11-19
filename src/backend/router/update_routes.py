"""
routers/update_routes.py
-------------------------
Server self-update from the GitHub repo (Settings -> Update).

The repo URL lives in backend/repo.txt (e.g.
https://github.com/RAPALLE2/BYTERA-RMM.git) and can be changed via the API.

Endpoints (admin only):
  GET  /api/admin/update/info   -> current version, repo, latest commit,
                                   all releases (alpha + full), suggestions
  PUT  /api/admin/update/repo   -> save the repo URL in repo.txt
  POST /api/admin/update/run    -> run the update
        body: { "target": "commit" | "full" | "any" | "custom", "tag": "..."? }

Flow of an update:
  1. Determine the target ref (commit SHA of the default branch or release tag)
  2. Load the ZIP from codeload.github.com and extract it to /tmp
  3. Copy files over the project directory - EXCEPT local data
     (data.sqlite, .env, repo.txt, recordings/, branding/)
  4. The backend restarts itself (os.execv) -> continues with the new code

Auto-update: main.py calls auto_update_tick() periodically. Depending on the
channel ("commit" | "full" | "any") it checks whether something newer exists and
runs the update automatically. A "last attempted target" (settings) prevents
endless loops if an update does not take effect.
"""

import asyncio
import io
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.request
import urllib.error
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import db
from app.auth import get_current_user, require_perm
from router.admin_routes import require_admin

router = APIRouter(prefix="/api/admin/update", tags=["update"])

# .../backend/app/routers/update_routes.py -> project root (.../rmm)
from app.paths import PROJECT_ROOT as _PROJECT_ROOT, REPO_FILE as _REPO_FILE, BACKEND_VERSION_FILE as _VERSION_FILE
_DEFAULT_REPO = "https://github.com/RAPALLE2/BYTERA-RMM.git"
# New repo name -> old name (fallback while GitHub still carries the old one)
_LEGACY_REPOS = {"bytera-rmm": "Rapalle.net-RMM"}

# These paths (relative to the project root) are NEVER overwritten or deleted
# during an update - local data and configuration live here.
_PRESERVE = (
    "src/backend/data",
    "src/backend/media",
    "src/backend/storage",
    "src/frontend/image",
    "src/backend/logs",
    "src/files",
)


def _read_repo_url() -> str:
    try:
        url = _REPO_FILE.read_text(encoding="utf-8").strip()
        if url:
            return url
    except OSError:
        pass
    # repo.txt missing -> create it with the default
    try:
        _REPO_FILE.write_text(_DEFAULT_REPO + "\n", encoding="utf-8")
    except OSError:
        pass
    return _DEFAULT_REPO


def _parse_owner_repo(url: str) -> tuple[str, str]:
    """'https://github.com/OWNER/REPO(.git)' -> ('OWNER', 'REPO')"""
    m = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$", url.strip())
    if not m:
        raise HTTPException(400, f"Repo URL not understood: {url}")
    return m.group(1), m.group(2)


def _current_version() -> str:
    try:
        return _VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def _http_json(url: str):
    """GitHub API call (synchronous; runs in the executor)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "bytera-rmm-updater",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "bytera-rmm-updater"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def _is_alpha(release: dict) -> bool:
    tag = (release.get("tag_name") or "").lower()
    return bool(release.get("prerelease")) or "alpha" in tag or "beta" in tag or "rc" in tag


def _fetch_github_state() -> dict:
    """Fetches the default branch, latest commit and all releases from the repo."""
    owner, repo = _parse_owner_repo(_read_repo_url())
    base = f"https://api.github.com/repos/{owner}/{repo}"
    try:
        info = _http_json(base)
    except urllib.error.HTTPError as e:
        # Rename RAPALLE.net RMM -> BYTERA RMM: if the repo on GitHub has not
        # been renamed yet, the new name does not exist - then use the old one.
        legacy = _LEGACY_REPOS.get(repo.lower())
        if e.code != 404 or not legacy:
            raise
        repo = legacy
        base = f"https://api.github.com/repos/{owner}/{repo}"
        info = _http_json(base)
    default_branch = info.get("default_branch") or "main"
    head = _http_json(f"{base}/commits/{default_branch}")
    releases = _http_json(f"{base}/releases?per_page=100")
    rel_list = [{
        "tag": r.get("tag_name"),
        "name": r.get("name") or r.get("tag_name"),
        "alpha": _is_alpha(r),
        "published_at": r.get("published_at"),
    } for r in releases if r.get("tag_name") and not r.get("draft")]
    latest_any = rel_list[0]["tag"] if rel_list else None
    latest_full = next((r["tag"] for r in rel_list if not r["alpha"]), None)
    return {
        "owner": owner, "repo": repo, "default_branch": default_branch,
        "latest_commit": {
            "sha": head.get("sha"),
            "message": ((head.get("commit") or {}).get("message") or "").split("\n")[0][:120],
            "date": ((head.get("commit") or {}).get("committer") or {}).get("date"),
        },
        "releases": rel_list,
        "latest_full_tag": latest_full,
        "latest_any_tag": latest_any,
    }


# ------------------------------------------------------------------
# Run the update
# ------------------------------------------------------------------

def _apply_zip(ref: str, owner: str, repo: str) -> None:
    """Loads the ZIP of the ref and copies it over the project directory."""
    data = _http_bytes(f"https://codeload.github.com/{owner}/{repo}/zip/{ref}")
    tmpdir = Path(tempfile.mkdtemp(prefix="rmm_update_"))
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            z.extractall(tmpdir)
        roots = [p for p in tmpdir.iterdir() if p.is_dir()]
        if not roots:
            raise RuntimeError("Update ZIP was empty")
        src_root = roots[0]

        preserve_abs = [(_PROJECT_ROOT / p).resolve() for p in _PRESERVE]

        def _preserved(dst: Path) -> bool:
            d = dst.resolve()
            for p in preserve_abs:
                if d == p or str(d).startswith(str(p) + os.sep):
                    return True
            # Never overwrite .env files in general (configuration/token)
            return dst.name == ".env"

        for src in src_root.rglob("*"):
            rel = src.relative_to(src_root)
            dst = _PROJECT_ROOT / rel
            if _preserved(dst):
                continue
            if src.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _schedule_restart(delay: float = 1.5) -> None:
    """Restarts the backend process (replaces itself -> new code)."""
    def _restart():
        print("[update] Restarting backend with new code (os.execv)...")
        try:
            sys.stdout.flush(); sys.stderr.flush()
        except Exception:
            pass
        os.execv(sys.executable, [sys.executable] + sys.argv)
    loop = asyncio.get_event_loop()
    loop.call_later(delay, _restart)


def _resolve_target(gh: dict, target: str, tag: str | None) -> tuple[str, str]:
    """Returns (ref, description) for the desired target."""
    if target == "commit":
        sha = (gh.get("latest_commit") or {}).get("sha")
        if not sha:
            raise HTTPException(502, "Could not determine the latest commit")
        return sha, f"Commit {sha[:10]}"
    if target == "full":
        t = gh.get("latest_full_tag")
        if not t:
            raise HTTPException(404, "No full release found in the repo")
        return t, f"Release {t}"
    if target == "any":
        t = gh.get("latest_any_tag")
        if not t:
            raise HTTPException(404, "No release found in the repo")
        return t, f"Release {t}"
    if target == "custom":
        if not tag or not any(r["tag"] == tag for r in gh.get("releases", [])):
            raise HTTPException(400, "Unknown release tag")
        return tag, f"Release {tag}"
    raise HTTPException(400, "Invalid target (commit|full|any|custom)")


async def _run_update(target: str, tag: str | None, username: str | None, job=None) -> dict:
    loop = asyncio.get_event_loop()
    if job:
        job.update(phase="check", pct=5, detail="Querying the GitHub revision")
    gh = await loop.run_in_executor(None, _fetch_github_state)
    ref, desc = _resolve_target(gh, target, tag)
    if job:
        job.log(f"Target: {desc} ({ref})")
        job.update(phase="download", pct=20, detail=f"{desc} is being loaded and applied")

    await loop.run_in_executor(None, _apply_zip, ref, gh["owner"], gh["repo"])

    # Remember the applied revision (basis for the commit channel and against loops).
    db.set_setting("server_current_commit", ref)
    db.set_setting("server_last_update_target", ref)
    db.add_audit_entry(username, "server.update_applied", details=desc)
    print(f"[update] {desc} applied - restart follows.")
    if job:
        job.log(f"{desc} applied - backend is restarting")
        # Phase "restart": the restart ends this job; after the start it is marked
        # as successfully completed in the history (bgjobs.startup_recover).
        job.update(phase="restart", pct=95, detail=f"{desc} applied - backend is restarting")
    _schedule_restart()
    return {"ok": True, "applied": desc, "ref": ref,
            "restart": "Backend restarts in a few seconds"}


# ------------------------------------------------------------------
# API-Endpunkte
# ------------------------------------------------------------------

class RepoBody(BaseModel):
    url: str


class RunBody(BaseModel):
    target: str            # "commit" | "full" | "any" | "custom"
    tag: str | None = None  # nur bei target == "custom"


@router.get("/info")
async def update_info(user: dict = Depends(get_current_user)):
    require_perm(user, "admin_settings")
    loop = asyncio.get_event_loop()
    try:
        gh = await loop.run_in_executor(None, _fetch_github_state)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"GitHub not reachable: {e}")
    return {
        "current_version": _current_version(),
        "current_commit": db.get_setting("server_current_commit", "") or "",
        "repo_url": _read_repo_url(),
        **gh,
    }


@router.put("/repo")
def set_repo(body: RepoBody, user: dict = Depends(get_current_user)):
    require_perm(user, "admin_settings")
    url = body.url.strip()
    _parse_owner_repo(url)   # validates the format
    _REPO_FILE.write_text(url + "\n", encoding="utf-8")
    db.add_audit_entry(user["username"], "server.update_repo_set", details=url)
    return {"ok": True, "repo_url": url}


@router.post("/run")
async def run_update(body: RunBody, background: bool = False, user: dict = Depends(get_current_user)):
    """Server update as a BACKEND JOB (app/bgjobs.py): loading + applying run in
    the backend; every admin sees the progress in the task view, even after
    reloading. After the restart the result is in the task history."""
    require_perm(user, "admin_settings")
    from app import bgjobs

    async def work(job):
        try:
            return await _run_update(body.target, body.tag, user["username"], job=job)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, f"Update failed: {e}")
    return await bgjobs.run("server.update", "server", title=f"Server-Update ({body.target}{' ' + body.tag if body.tag else ''})",
                            user=user, work=work, background=background, perm="admin_settings",
                            meta={"target": body.target, "tag": body.tag})


# ------------------------------------------------------------------
# Auto-update (called periodically by main.py)
# ------------------------------------------------------------------

async def auto_update_tick() -> None:
    """
    Checks whether, according to the channel, something newer is available and
    applies it.
    Channels (setting 'server_auto_update_channel'):
      commit -> latest commit of the default branch
      full   -> latest full release (no alpha/beta/RC)
      any    -> latest release (alpha AND full)
    """
    if db.get_setting("server_auto_update", "0") != "1":
        return
    channel = (db.get_setting("server_auto_update_channel", "full") or "full").lower()
    if channel not in ("commit", "full", "any"):
        channel = "full"

    loop = asyncio.get_event_loop()
    try:
        gh = await loop.run_in_executor(None, _fetch_github_state)
    except Exception as e:
        print(f"[auto-update] GitHub not reachable: {e}")
        return

    if channel == "commit":
        ref = (gh.get("latest_commit") or {}).get("sha") or ""
        known = db.get_setting("server_current_commit", "") or ""
        if not ref:
            return
        if not known:
            # First run: only remember the current revision as a basis, do not update.
            db.set_setting("server_current_commit", ref)
            return
        if ref == known:
            return
    else:
        ref = gh.get("latest_full_tag") if channel == "full" else gh.get("latest_any_tag")
        if not ref:
            return
        cur = _current_version().lstrip("vV")
        if ref.lstrip("vV") == cur:
            return

    # Loop guard: do not try the same target several times in a row
    # (e.g. when a release does not change the version).
    if (db.get_setting("server_last_update_target", "") or "") == ref:
        return

    print(f"[auto-update] New target found ({channel}): {ref} - update starting.")
    try:
        # The automatic update also runs as a visible job (task view)
        from app import bgjobs
        tgt = "commit" if channel == "commit" else ("full" if channel == "full" else "any")
        job, _ = bgjobs.start("server.update", "server", title=f"Automatisches Server-Update ({ref})",
                              user=None, perm="admin_settings",
                              runner=lambda j: _run_update(tgt, None, None, job=j))
        await job.done.wait()
        if job.status == "error":
            raise RuntimeError(job.error)
    except Exception as e:
        # Mark the target as "attempted" anyway so no endless loop occurs.
        db.set_setting("server_last_update_target", ref)
        print(f"[auto-update] Update failed: {e}")
