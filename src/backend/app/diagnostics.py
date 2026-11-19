"""
diagnostics.py
--------------
Maintenance mode: record everything, so a crash can be explained afterwards.

Why this module exists at all
-----------------------------
A crash that only happens "at some point after a while" is almost never found
at the spot where it surfaces. The usual causes are gradual: memory that is no
longer freed; file descriptors that stay open; background tasks that die
silently; exceptions in threads that nobody sees. None of that is in an
ordinary error message.

Therefore the maintenance mode records three things:

  1. EVERYTHING in output - stdout, stderr, the logging module, exceptions from
     threads and from asyncio tasks. Also what is otherwise silently swallowed.
  2. Regular measurements: memory, open file descriptors, threads, running
     asyncio tasks, Socket.IO connections. A curve that steadily rises shows
     the leak more clearly than any stack trace.
  3. The reports of the agents, so both sides lie on ONE timeline.

In addition faulthandler: if the process dies hard (segmentation fault, killed
by the kernel), it still writes out the stack traces of all threads. Exactly
these cases otherwise leave nothing at all.

The mode is deliberately NOT permanently on: it costs space and some compute
time. It runs until it is switched off or the configured time has elapsed - so
a forgotten session does not stay active forever.
"""

from __future__ import annotations

import asyncio
import faulthandler
import io
import json
import logging
import os
import sys
import threading
import time
import traceback
from collections import deque
from pathlib import Path

LOG_DIR = Path(os.getenv("RMM_LOG_DIR", "")
               or __import__("app.paths", fromlist=["BACKEND_LOG_DIR"]).BACKEND_LOG_DIR)
MAIN_LOG = "backend.log"
FAULT_LOG = "backend-fault.log"
AGENT_LOG = "agents.log"
BLOCK_LOG = "backend-blockaden.log"

# How many lines are kept in memory for the live view.
RING_SIZE = 4000
# Size at which a log file is rotated, and how many old versions are kept.
# 20 MB is enough for many hours without filling the disk.
MAX_BYTES = 20 * 1024 * 1024
BACKUPS = 3
# Interval of the data points.
SAMPLE_SECONDS = 15


class _State:
    def __init__(self):
        self.active = False
        self.until = 0.0            # 0 = no end
        self.started = 0.0
        self.ring: deque = deque(maxlen=RING_SIZE)
        self.lock = threading.Lock()
        self.samples: deque = deque(maxlen=2000)
        self.fault_file = None
        self.installed = False
        self.orig_stdout = None
        self.orig_stderr = None
        self.counters = {"errors": 0, "warnings": 0, "agent_errors": 0}


st = _State()

# Reused psutil instance (see sample()).
_proc = None


# ----------------------------------------------------------------------
# Writing
# ----------------------------------------------------------------------

def _ensure_dir() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def _rotate(path: Path) -> None:
    """Simple rotation - no logging.handlers, because raw stdout text also
    arrives here that never goes through the logging module."""
    try:
        if path.exists() and path.stat().st_size > MAX_BYTES:
            for i in range(BACKUPS - 1, 0, -1):
                older, newer = path.with_suffix(f".{i}"), path.with_suffix(f".{i+1}")
                if older.exists():
                    older.replace(newer)
            path.replace(path.with_suffix(".1"))
    except OSError:
        pass


def write(line: str, target: str = MAIN_LOG, level: str = "INFO",
          to_console: bool = False, from_stream: bool = False) -> None:
    """
    One line into the ring and the file. Must NEVER throw.

    This is not over-caution: this function hangs on stdout. If it throws, it
    tears down every print() call in the whole program - exactly when the
    output is needed most.

    'to_console' additionally writes into the real container output.

    Why this became necessary: until just now EVERYTHING landed only in the
    ring buffer and the log file. The 'docker logs' therefore held not a single
    line of the diagnostics - neither that it is installed at all, nor a
    reported block. For a container whose log files one cannot currently reach,
    that is the one output one really needs. Important messages therefore also
    go to stdout.
    """
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        entry = f"{stamp} [{level}] {line.rstrip()}"
        with st.lock:
            st.ring.append(entry)
            if level == "ERROR":
                st.counters["errors"] += 1
            elif level in ("WARN", "WARNING"):
                st.counters["warnings"] += 1
        # Write directly to the ORIGINAL, not via sys.stdout: the latter is our
        # own capture and would send the line through write() a second time.
        # 'from_stream' means: the line just came FROM stdout/stderr and is
        # already there. Printing it a second time would duplicate every
        # container output.
        if not from_stream and (to_console or level in ("ERROR", "WARN", "WARNING")):
            try:
                target_stream = st.orig_stdout or sys.__stdout__
                target_stream.write(entry + "\n")
                target_stream.flush()
            except Exception:
                pass
        if not st.active:
            return
        _ensure_dir()
        path = LOG_DIR / target
        _rotate(path)
        with open(path, "a", encoding="utf-8", errors="replace") as f:
            f.write(entry + "\n")
    except Exception:
        pass


# ----------------------------------------------------------------------
# Redirection of stdout/stderr
# ----------------------------------------------------------------------

class _Tee(io.TextIOBase):
    """
    Writes to the original AND into the log.

    Deliberately a tee and not a redirection: the container output
    ('docker logs') should keep running unchanged. A diagnostic tool that takes
    away the usual output makes troubleshooting harder, not easier.
    """

    def __init__(self, original, level: str):
        self.original = original
        self.level = level
        self._buffer = ""

    def write(self, text: str) -> int:
        try:
            self.original.write(text)
        except Exception:
            pass
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                write(line, level=self._level_for(line), from_stream=True)
        return len(text)

    def _level_for(self, line: str) -> str:
        """
        Determine the level from the content.

        Previously EVERYTHING on stderr counted as an error across the board.
        But uvicorn also writes ordinary access lines there - in the last log a
        harmless JWT warning therefore appeared as [ERROR], and the error
        counter in the dashboard was correspondingly useless. Now the text
        decides.
        """
        if self.level != "ERROR":
            return "INFO"
        low = line.lower()
        if "traceback" in low or "error" in low or "exception" in low:
            return "ERROR"
        if "warn" in low:
            return "WARN"
        return "INFO"

    def flush(self) -> None:
        try:
            self.original.flush()
        except Exception:
            pass

    def isatty(self) -> bool:
        try:
            return self.original.isatty()
        except Exception:
            return False


class _LogHandler(logging.Handler):
    """Brings the logging module (uvicorn, FastAPI) into the same file too."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            write(f"{record.name}: {record.getMessage()}",
                  level=record.levelname)
            if record.exc_info:
                write("".join(traceback.format_exception(*record.exc_info)),
                      level="ERROR")
        except Exception:
            pass


# ----------------------------------------------------------------------
# Collect exceptions that nobody else sees
# ----------------------------------------------------------------------

def _thread_hook(args) -> None:
    write(f"Unhandled exception in thread {args.thread.name if args.thread else '?'}:\n"
          + "".join(traceback.format_exception(args.exc_type, args.exc_value,
                                               args.exc_traceback)),
          level="ERROR")


def _sys_hook(exc_type, exc_value, tb) -> None:
    write("Unhandled exception in the main thread:\n"
          + "".join(traceback.format_exception(exc_type, exc_value, tb)),
          level="ERROR")
    try:
        sys.__excepthook__(exc_type, exc_value, tb)
    except Exception:
        pass


def _loop_handler(loop, context: dict) -> None:
    """
    Exceptions from asyncio tasks.

    Without this hook Python only reports "Task exception was never retrieved" -
    and that at some later point, when the garbage collector comes by. The
    timing then has nothing to do with the cause anymore.
    """
    exc = context.get("exception")
    message = context.get("message") or "asyncio error"
    detail = ("".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
              if exc else json.dumps({k: str(v) for k, v in context.items()},
                                     ensure_ascii=False))
    write(f"{message}\n{detail}", level="ERROR")


def install() -> None:
    """
    Hooks into everything. Runs ONCE at start, regardless of whether the
    maintenance mode is on right now - the ring buffers anyway, and when
    switched on the last minutes are immediately included.
    """
    if st.installed:
        return
    st.installed = True
    st.orig_stdout, st.orig_stderr = sys.stdout, sys.stderr
    sys.stdout = _Tee(sys.stdout, "INFO")
    sys.stderr = _Tee(sys.stderr, "ERROR")

    root = logging.getLogger()
    root.addHandler(_LogHandler())
    if root.level > logging.INFO:
        root.setLevel(logging.INFO)

    threading.excepthook = _thread_hook
    sys.excepthook = _sys_hook
    try:
        asyncio.get_event_loop().set_exception_handler(_loop_handler)
    except RuntimeError:
        pass
    write("Diagnostics installed (stdout, stderr, logging, threads, asyncio)", to_console=True)


# ----------------------------------------------------------------------
# Watchdog for event-loop blocks
# ----------------------------------------------------------------------
# This is the tool for the error on 22.08.: the process was alive, had stable
# memory and no exception - but no longer responded. All agents dropped at
# once, the Docker health check ran into its 5-second limit, Cloudflare
# reported 524.
#
# Something like that produces NO stack trace, because nothing crashes. It is a
# blocking operation in the event-loop thread - typically an fsync on a slow
# file system, a network call without a time limit or a long loop.
#
# The watchdog runs in its OWN thread (not in the loop - that is blocked right
# now) and measures how punctually the loop updates a timestamp. If the stamp
# stops, it writes out the stack traces of ALL threads. This way, next time, it
# is in black and white which line the backend is stuck on.

# From which delay on it is logged.
LOOP_LAG_WARN = 2.0
# From when all stack traces are additionally saved.
LOOP_LAG_DUMP = 5.0
# EMERGENCY STOP: from this delay on, the backend counts as stuck and ends
# itself, so the restart chain takes effect. 0 disables this.
#
# Why self-termination is the right answer: a process whose event loop is stuck
# for minutes is, for any user, indistinguishable from a crashed one - except
# that nobody restarts it. Docker does NOT react to a failed health check with
# a restart, but merely marks the container as unhealthy. A stuck backend thus
# stays stuck arbitrarily long. If it ends itself, however, run.py or Docker's
# restart rule catch it and it is back after seconds. A controlled exit is
# better than an undead process.
LOOP_LAG_KILL = float(os.getenv("RMM_LOOP_KILL_S", "90") or 90)

_loop_beat = {"t": 0.0, "worst": 0.0, "dumps": 0}


async def loop_heartbeat() -> None:
    """Sets a timestamp twice per second. Nothing more - exactly that makes it
    a reliable yardstick: if it does not get through, the loop is stuck."""
    while True:
        _loop_beat["t"] = time.monotonic()
        await asyncio.sleep(0.5)


def _watchdog_thread() -> None:
    """
    Measures from the outside how long the heartbeat is missing.

    Two time locks, and they are DELIBERATELY separate:

      'reported' throttles the text message, so a long block does not write the
      same line every second.

      'dumped' throttles the stack dump - but per BLOCK, not per time.

    Why separate: with a shared lock the following happened. After two seconds
    of delay it warned and set the lock. When the delay then grew to six
    seconds - exactly the moment the stack dump would have shown the culprit -
    the lock took effect and the dump was skipped. The block was reported, but
    never explained. Exactly this case showed up in testing.
    """
    reported = 0.0
    dumped_this_stall = False
    while True:
        time.sleep(1.0)
        last = _loop_beat["t"]
        if not last:
            continue
        lag = time.monotonic() - last
        if lag > _loop_beat["worst"]:
            _loop_beat["worst"] = lag

        if lag < LOOP_LAG_WARN:
            # The block is over - armed again for the next one.
            dumped_this_stall = False
            continue

        if time.monotonic() - reported >= 20:
            reported = time.monotonic()
            write(f"EVENT LOOP BLOCKED for {lag:.1f}s - the backend is not "
                  f"responding right now (health check and agents drop in "
                  f"the process)", level="ERROR")

        # The dump depends ONLY on the severity, not on the report lock.
        if lag >= LOOP_LAG_DUMP and not dumped_this_stall:
            dumped_this_stall = True
            _dump_all_stacks(lag)

        # Emergency stop: no healthy loop ever stands still that long.
        if LOOP_LAG_KILL > 0 and lag >= LOOP_LAG_KILL:
            _emergency_exit(lag)


def _dump_all_stacks(lag: float) -> None:
    """Stack traces of all threads - this shows the blocking line."""
    _loop_beat["dumps"] += 1
    try:
        lines = [f"===== STACK DUMP (loop stuck {lag:.1f}s) ====="]
        # First: which requests are being processed RIGHT NOW? That is usually
        # already the answer - if the server is blocked, the action blocking it
        # is here. Without this list one has to dig it out of the stack traces.
        try:
            from app.main import INFLIGHT
            if INFLIGHT:
                lines.append("--- running requests ---")
                now = time.monotonic()
                for path, started in sorted(INFLIGHT.values(),
                                            key=lambda x: x[1]):
                    lines.append(f"  for {now - started:5.1f}s  {path}")
            else:
                lines.append("--- no request in processing "
                             "(the block comes from a background task) ---")
        except Exception:
            pass
        frames = sys._current_frames()
        names = {t.ident: t.name for t in threading.enumerate()}
        for tid, frame in frames.items():
            lines.append(f"--- thread {names.get(tid, '?')} ({tid}) ---")
            lines.extend(l.rstrip() for l in traceback.format_stack(frame))
        text = "\n".join(lines)
        write(text, level="ERROR")
        # Additionally into its own file: these dumps are the most valuable
        # thing in the whole package and should not drown in the rest of the
        # log. Deliberately INDEPENDENT of the maintenance mode: a block is so
        # rare and so telling that it belongs recorded even when nobody is
        # currently having things logged. Otherwise one stands empty-handed
        # again after the incident.
        if True:
            _ensure_dir()
            with open(LOG_DIR / BLOCK_LOG, "a", encoding="utf-8",
                      errors="replace") as f:
                f.write(f"\n{time.strftime('%Y-%m-%d %H:%M:%S')}\n{text}\n")
    except Exception as e:
        write(f"stack dump failed: {e}", level="WARN")


def start_watchdog() -> None:
    """Starts the watchdog thread. Runs permanently, not only in maintenance mode."""
    t = threading.Thread(target=_watchdog_thread, name="loop-watchdog",
                         daemon=True)
    t.start()
    write(f"Loop watchdog active (warning from {LOOP_LAG_WARN}s, "
          f"stack dump from {LOOP_LAG_DUMP}s)", to_console=True)


# ----------------------------------------------------------------------
# Switching on and off
# ----------------------------------------------------------------------

def enable(minutes: int = 0, reason: str = "") -> dict:
    """Switches on the maintenance mode. minutes=0 means no end."""
    _ensure_dir()
    st.active = True
    st.started = time.time()
    st.until = time.time() + minutes * 60 if minutes and minutes > 0 else 0.0

    # faulthandler writes the stack traces of all threads on a hard abort. The
    # file stays open - exactly why it still works even when nothing runs in an
    # orderly way anymore.
    try:
        st.fault_file = open(LOG_DIR / FAULT_LOG, "a", buffering=1,
                             encoding="utf-8", errors="replace")
        faulthandler.enable(file=st.fault_file, all_threads=True)
    except Exception as e:
        write(f"faulthandler could not be enabled: {e}", level="WARN")

    write("=" * 70)
    write(f"MAINTENANCE MODE ON - reason: {reason or 'not specified'}",
          to_console=True)
    write(f"Python {sys.version.split()[0]}, PID {os.getpid()}, "
          f"directory {LOG_DIR}")
    write("=" * 70)
    _dump_environment()

    from app import db
    db.set_setting("maintenance_mode", "1")
    db.set_setting("maintenance_until", str(int(st.until * 1000)))
    return status()


def disable() -> dict:
    write("MAINTENANCE MODE OFF", to_console=True)
    st.active = False
    st.until = 0.0
    try:
        faulthandler.disable()
        if st.fault_file:
            st.fault_file.close()
    except Exception:
        pass
    st.fault_file = None
    from app import db
    db.set_setting("maintenance_mode", "0")
    return status()


def status() -> dict:
    files = []
    try:
        for p in sorted(LOG_DIR.glob("*")):
            if p.is_file():
                files.append({"name": p.name, "size": p.stat().st_size,
                              "modified": int(p.stat().st_mtime * 1000)})
    except OSError:
        pass
    return {
        "active": st.active,
        "since": int(st.started * 1000) if st.started else 0,
        "until": int(st.until * 1000) if st.until else 0,
        "dir": str(LOG_DIR),
        "files": files,
        "counters": dict(st.counters),
        # Errors by code - so one sees in the UI immediately WHICH kind of
        # error occurs, without reading the log.
        "error_codes": _error_summary(),
        # How often the event loop was stuck and how long it was blocked in
        # the worst case. If something > 0 is here, that is the trail to
        # follow.
        "loop_worst_s": round(_loop_beat["worst"], 2),
        "loop_dumps": _loop_beat["dumps"],
        "samples": list(st.samples)[-240:],
        "ring_size": len(st.ring),
    }


def _error_summary() -> dict:
    try:
        from app import errors
        return errors.summary()
    except Exception:
        return {"counters": {}, "recent": [], "total": 0}


def tail(lines: int = 300) -> list[str]:
    with st.lock:
        return list(st.ring)[-max(1, min(lines, RING_SIZE)):]


# ----------------------------------------------------------------------
# Measurements
# ----------------------------------------------------------------------

def _dump_environment() -> None:
    """One-off snapshot at switch-on - context for later."""
    try:
        import platform
        write(f"System: {platform.platform()}")
        write(f"CPUs: {os.cpu_count()}")
        try:
            import resource
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            write(f"file descriptor limit: soft {soft}, hard {hard}")
        except Exception:
            pass
        # Memory limit of the container - the most common reason for a sudden
        # disappearance without any error message.
        for path in ("/sys/fs/cgroup/memory.max",
                     "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
            try:
                with open(path) as f:
                    write(f"memory limit ({path}): {f.read().strip()}")
                break
            except OSError:
                continue
    except Exception as e:
        write(f"environment info incomplete: {e}", level="WARN")


def sample() -> dict:
    """One data point. Deliberately cheap - it runs every few seconds."""
    data = {"t": int(time.time() * 1000)}
    # Delay of the event loop - the most important value of all. If it is over
    # a second, the backend is not responding right now.
    try:
        if _loop_beat["t"]:
            data["lag"] = round(time.monotonic() - _loop_beat["t"], 2)
            data["lag_max"] = round(_loop_beat["worst"], 2)
    except Exception:
        pass
    try:
        import psutil
        # IMPORTANT: reuse the same Process instance. cpu_percent() measures
        # the usage SINCE THE LAST CALL on the same object - with a freshly
        # created instance it therefore always returns 0.0. Exactly why the log
        # on 22.08. showed "cpu": 0.0 throughout, and a high CPU load would
        # have stayed invisible.
        global _proc
        if _proc is None:
            _proc = psutil.Process()
            _proc.cpu_percent(interval=None)   # set the zero point
        proc = _proc
        with proc.oneshot():
            data["rss_mb"] = round(proc.memory_info().rss / 1048576, 1)
            data["cpu"] = round(proc.cpu_percent(interval=None), 1)
            data["threads"] = proc.num_threads()
            try:
                data["fds"] = proc.num_fds()
            except Exception:
                data["fds"] = len(proc.open_files())
            data["conns"] = len(proc.net_connections(kind="inet"))
        data["sys_mem"] = round(psutil.virtual_memory().percent, 1)
    except Exception:
        pass
    try:
        data["tasks"] = len(asyncio.all_tasks())
    except Exception:
        pass
    # How many requests are being processed right now? Since most endpoints
    # run in worker threads, this is the most telling load figure: if it is
    # permanently at the thread pool's limit (default 40), requests are waiting
    # for a free thread.
    try:
        from app.main import INFLIGHT
        data["inflight"] = len(INFLIGHT)
        if INFLIGHT:
            oldest = min(v[1] for v in INFLIGHT.values())
            data["oldest_req_s"] = round(time.monotonic() - oldest, 1)
    except Exception:
        pass
    try:
        from app.sockets import state as sock_state
        data["agents"] = len(sock_state.client_id_to_sid)
        data["pending"] = len(sock_state.pending_requests)
    except Exception:
        pass
    return data


# Interval of the status lines in the container output. One minute is often
# enough to see a trend, and rare enough not to clutter the log.
BEAT_INTERVAL = float(os.getenv("RMM_BEAT_INTERVAL_S", "60") or 60)
_last_beat = [0.0]


def _console_beat(point: dict) -> None:
    """A compact status line directly to Docker."""
    try:
        parts = []
        for key, label in (("rss_mb", "memory"), ("cpu", "CPU%"),
                           ("threads", "threads"), ("fds", "files"),
                           ("tasks", "tasks"), ("inflight", "requests"),
                           ("agents", "agents"), ("lag", "lag"),
                           ("lag_max", "max.lag")):
            if point.get(key) is not None:
                parts.append(f"{label}={point[key]}")
        try:
            from app.errors import counters as _ec
            if _ec:
                parts.append("errors=" + ",".join(f"{k}:{v}" for k, v in
                                                  sorted(_ec.items())))
        except Exception:
            pass
        line = "[status] " + "  ".join(parts)
        stream = st.orig_stdout or sys.__stdout__
        stream.write(line + "\n")
        stream.flush()
    except Exception:
        pass


def _estimate_rows(table: str) -> int:
    """
    Estimates the row count without blocking the database.

    COUNT(*) reads the WHOLE table. With 600,000 rows that took 9 seconds - and
    because all queries go through the same lock, of all things the monitoring
    stalled the server for nine seconds. A check that itself causes the problem
    it is meant to report is worse than none.

    Therefore the highest rowid is taken: a single access to the end of the
    table, independent of the size. After many deletions this overestimates -
    for a warning threshold that is exactly the right way round.
    """
    try:
        from app import db
        row = db._conn.execute(f"SELECT MAX(rowid) AS n FROM {table}").fetchone()
        return int(row["n"] or 0) if row else 0
    except Exception:
        return -1


async def table_size_watch() -> None:
    """
    Warns BEFORE a table becomes a problem.

    The crash on 23.08. was not caused by a program bug, but by growth:
    metrics_history had grown unbounded over weeks, until a query on it took
    125 seconds. Nothing in the system warned about it - one saw it only when
    it was too late. This loop checks hourly.
    """
    await asyncio.sleep(120)
    while True:
        try:
            from app import db
            # First check which tables exist at all. On the first run the
            # check asked for 'notifications', which does not exist in this
            # installation - and produced an error that looked like a real
            # problem.
            existing = {r["name"] for r in await db.call(
                lambda: db._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall())}

            for table, limit in (("metrics_history", 500_000),
                                 ("audit_log", 300_000),
                                 ("notifications", 100_000)):
                if table not in existing:
                    continue
                count = await db.call(_estimate_rows, table)
                if count < 0:
                    continue
                if count > limit:
                    from app.errors import report, Codes
                    report(Codes.DB_HUGE, None,
                           f"table '{table}' has {count:,} rows "
                           f"(warning threshold {limit:,})".replace(",", "."),
                           recommendation="set a retention period in the "
                                          "privacy settings")
        except Exception as e:
            write(f"table check: {e}", level="WARN")
        await asyncio.sleep(3600)


async def sampler_loop() -> None:
    """
    Writes a data point regularly.

    Runs ALWAYS, not only in maintenance mode - but only into the ring buffer.
    This way, after a crash, the minutes before it are also available, when
    nobody had yet thought of diagnostics.
    """
    last_warn = 0.0
    while True:
        try:
            point = sample()
            st.samples.append(point)
            if st.active:
                write("METRIC " + json.dumps(point, ensure_ascii=False),
                      level="DEBUG")
            # HEARTBEAT INTO THE CONTAINER OUTPUT.
            #
            # The reason is the sore point of every diagnostic: when the
            # backend collapses, it can no longer write down the cause - and
            # the view in the settings is unreachable then anyway. What REMAINS
            # is 'docker logs'; the lines there are already at Docker before
            # anything happens.
            #
            # Therefore a short status line regularly goes there. After a crash
            # one sees from it how the backend was doing in the minutes before
            # - memory, lag, requests - without anything having had to survive.
            now_t = time.time()
            if now_t - _last_beat[0] >= BEAT_INTERVAL:
                _last_beat[0] = now_t
                _console_beat(point)
            # Early warning: these two values are the usual suspects when a
            # process is "simply gone".
            now = time.time()
            if now - last_warn > 120:
                if point.get("fds", 0) > 900:
                    write(f"Many open file descriptors: {point['fds']}",
                          level="WARN")
                    last_warn = now
                if point.get("lag", 0) > 1.0:
                    write(f"Event loop lagging {point['lag']}s behind - "
                          f"something is blocking the main thread", level="WARN")
                    last_warn = now
                if point.get("inflight", 0) >= 35:
                    write(f"Almost all worker threads busy "
                          f"({point['inflight']} requests at once) - "
                          f"further requests are waiting.", level="WARN")
                    last_warn = now
                if point.get("sys_mem", 0) > 92:
                    write(f"Memory almost full: {point['sys_mem']} %",
                          level="WARN")
                    last_warn = now
            # Expiry of the maintenance mode
            if st.active and st.until and time.time() > st.until:
                write("Maintenance mode expired - switching off")
                disable()
        except Exception as e:
            try:
                write(f"sampler loop: {e}", level="WARN")
            except Exception:
                pass
        await asyncio.sleep(SAMPLE_SECONDS)


# ----------------------------------------------------------------------
# Agent reports
# ----------------------------------------------------------------------

def agent_log(client_id: str, hostname: str, lines: list) -> None:
    """Takes log lines of an agent and files them alongside."""
    if not lines:
        return
    for line in lines:
        if not isinstance(line, str):
            line = str(line)
        if "[ERROR]" in line or "Traceback" in line:
            st.counters["agent_errors"] += 1
        write(f"<{hostname or client_id}> {line}", target=AGENT_LOG)


def bundle() -> bytes:
    """
    Packs all log files into a ZIP - that is the file one passes on.

    The ring buffer is included too, even if the maintenance mode was only just
    switched on: then at least the prior history is in it.
    """
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        try:
            for p in sorted(LOG_DIR.glob("*")):
                if p.is_file():
                    z.write(p, p.name)
        except OSError:
            pass
        z.writestr("ringbuffer.txt", "\n".join(tail(RING_SIZE)))
        z.writestr("metrics.json",
                   json.dumps(list(st.samples), indent=1, ensure_ascii=False))
        z.writestr("status.json", json.dumps(status(), indent=1,
                                             ensure_ascii=False, default=str))
    return buf.getvalue()


def clear() -> None:
    try:
        for p in LOG_DIR.glob("*"):
            if p.is_file():
                p.unlink()
    except OSError:
        pass
    with st.lock:
        st.ring.clear()
    st.samples.clear()
    st.counters = {"errors": 0, "warnings": 0, "agent_errors": 0}
    write("logs cleared")


def report_previous_shutdown() -> None:
    """
    Says at start WHY the last run ended.

    Without this message every restart looks the same - a planned one like a
    crash. Exactly this is where the classification failed so far: the log only
    said "backend starting", never why it had stopped before.
    """
    try:
        from app import db
        mark = db.get_setting("last_watchdog_kill", "") or ""
        if mark:
            ts, _, lag = mark.partition("|")
            when = time.strftime("%Y-%m-%d %H:%M:%S",
                                 time.localtime(int(ts) / 1000))
            write(f"PREVIOUS RUN: the watchdog ended the backend at {when} "
                  f"because the event loop was stuck for {lag}s. The cause "
                  f"is in backend-blockaden.log.",
                  level="WARN", to_console=True)
            db.set_setting("last_watchdog_kill", "")
        if db.get_setting("backend_crash_pending", "0") == "1":
            write("PREVIOUS RUN: the backend crashed. The report is in the "
                  "audit log under 'backend.crash'.",
                  level="WARN", to_console=True)
            db.set_setting("backend_crash_pending", "0")
    except Exception as e:
        print(f"[diag] reason for the last ending not readable: {e}")


def restore_from_settings() -> None:
    """Resume the maintenance mode after a restart."""
    try:
        from app import db
        if db.get_setting("maintenance_mode", "0") != "1":
            return
        until = int(db.get_setting("maintenance_until", "0") or 0)
        if until and until / 1000 < time.time():
            db.set_setting("maintenance_mode", "0")
            return
        minutes = int((until / 1000 - time.time()) / 60) if until else 0
        enable(max(minutes, 0), reason="resumed after restart")
        write("The process was restarted - this may have been a crash. "
              "Please check backend-fault.log.", level="WARN")
    except Exception as e:
        print(f"[diag] maintenance mode not resumed: {e}")


def _emergency_exit(lag: float) -> None:
    """
    Ends the process hard so it is restarted.

    Flow, deliberately in this order:
      1. Save stack traces - otherwise the cause is gone after the restart.
      2. A note in the settings, so after the restart the dashboard shows WHY
         it was restarted.
      3. os._exit() instead of sys.exit(): a clean exit would have to go
         through the same event loop that is stuck right now - it would never
         arrive.
    """
    try:
        write(f"EMERGENCY STOP: the event loop has been stuck for {lag:.0f}s. "
              f"The backend is now ending itself so it gets "
              f"restarted.", level="ERROR", to_console=True)
        _dump_all_stacks(lag)
    except Exception:
        pass
    try:
        from app import db
        db.set_setting("last_watchdog_kill",
                       f"{int(time.time() * 1000)}|{lag:.0f}")
    except Exception:
        pass
    try:
        if st.fault_file:
            st.fault_file.flush()
        sys.__stdout__.flush()
        sys.__stderr__.flush()
    except Exception:
        pass
    os._exit(75)     # 75 = EX_TEMPFAIL: "try again"
