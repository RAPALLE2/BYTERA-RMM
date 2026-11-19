"""
app/memtrim.py - keep the backend's memory footprint small
==========================================================

Observation: with ~20 agents the backend used up to 500 MB, even though the
Python objects themselves account for only a fraction of that. The cause is
(almost always) not a leak but the C memory allocator (glibc malloc):

  * Each thread (thread pool for blocking calls, SFTP, Proxmox, ...) gets its
    own "arena". Up to 8 x CPU cores arenas, each holding on to freed memory.
  * Large, short-lived buffers (remote-screen images, file transfers, module
    bundles, proxy responses) leave gaps after being freed that glibc does not
    return to the operating system.

Remedies (all without loss of functionality):
  1. limit_arenas(): limit arenas to 2 (mallopt M_ARENA_MAX) - as early as
     possible, before threads are created. Equivalent to MALLOC_ARENA_MAX=2.
  2. Regular malloc_trim(0): return free pages to the system.
  3. Limit the thread pool (the default would be up to 32 threads).
  4. gc.freeze() after startup: the objects loaded at startup (modules, routes)
     are no longer walked by the garbage collector every time.

On Windows/macOS/musl mallopt/malloc_trim do not exist - then the module
quietly does nothing (points 3 and 4 still apply).

Configurable via environment: RMM_MEMTRIM=0 disables everything,
RMM_MEMTRIM_INTERVAL (seconds, default 60), RMM_EXECUTOR_THREADS (default 12-16 depending on cores).
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import gc
import os
import sys
import threading
import time

_ENABLED = os.getenv("RMM_MEMTRIM", "1") != "0"
_libc = None
STATS = {"trims": 0, "freed_kb": 0, "last_rss_kb": 0, "arena_limit": False, "executor": 0}


def _load_libc():
    global _libc
    if _libc is not None or not sys.platform.startswith("linux"):
        return _libc
    try:
        name = ctypes.util.find_library("c") or "libc.so.6"
        lib = ctypes.CDLL(name)
        if hasattr(lib, "malloc_trim") and hasattr(lib, "mallopt"):
            _libc = lib
    except OSError:
        _libc = None
    return _libc


def rss_kb() -> int:
    try:
        with open("/proc/self/status", encoding="ascii") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        pass
    try:
        import psutil
        return int(psutil.Process().memory_info().rss / 1024)
    except Exception:
        return 0


def limit_arenas(n: int = 2) -> None:
    """Call as early as possible (main.py, before any threads)."""
    if not _ENABLED:
        return
    lib = _load_libc()
    if lib is None:
        return
    try:
        M_ARENA_MAX = -8
        if lib.mallopt(M_ARENA_MAX, int(os.getenv("MALLOC_ARENA_MAX") or n)) == 1:
            STATS["arena_limit"] = True
    except Exception:
        pass


def trim() -> int:
    """Return free heap to the system. Returns: freed KB."""
    lib = _load_libc()
    before = rss_kb()
    gc.collect()
    if lib is not None:
        try:
            lib.malloc_trim(0)
        except Exception:
            pass
    after = rss_kb()
    freed = max(0, before - after)
    STATS["trims"] += 1
    STATS["freed_kb"] += freed
    STATS["last_rss_kb"] = after
    return freed


def _trim_loop() -> None:
    interval = max(15, int(os.getenv("RMM_MEMTRIM_INTERVAL") or 60))
    while True:
        time.sleep(interval)
        try:
            freed = trim()
            if freed > 20 * 1024:
                print(f"[memory] {freed // 1024} MB returned to the system "
                      f"(now {STATS['last_rss_kb'] // 1024} MB)", flush=True)
        except Exception:
            pass


_started = False


def start(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Call on backend startup (startup event)."""
    global _started
    if _started or not _ENABLED:
        return
    _started = True
    # Limit the thread pool: fewer threads = fewer stacks and arenas
    try:
        from concurrent.futures import ThreadPoolExecutor
        dflt = min(16, max(12, (os.cpu_count() or 1) + 4))   # never less than Python's default on <= 8 cores
        n = max(4, int(os.getenv("RMM_EXECUTOR_THREADS") or dflt))
        (loop or asyncio.get_event_loop()).set_default_executor(
            ThreadPoolExecutor(max_workers=n, thread_name_prefix="rmm-pool"))
        STATS["executor"] = n
    except Exception:
        pass
    threading.Thread(target=_trim_loop, name="rmm-memtrim", daemon=True).start()

    def _freeze():
        time.sleep(30)          # modules/routes are loaded by then
        try:
            gc.collect()
            gc.freeze()
        except Exception:
            pass
        trim()
    threading.Thread(target=_freeze, name="rmm-gcfreeze", daemon=True).start()
