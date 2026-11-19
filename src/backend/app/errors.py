"""
errors.py
=========
An error must never vanish without a trace - and never take the server with it.

Why this module
---------------
The previous error handling was scattered across the whole backend: sometimes
a try/except with print(), sometimes none, sometimes a silent `except: pass`.
After an incident the log therefore held either nothing or something useless.

Here there is exactly one place that captures errors, and it guarantees three
things:

  1. EVERY error gets a CODE of the form RMM-AREA-NNN. It can then be searched
     in the log, and one immediately knows which part of the system is affected
     - without having to read the stack trace.

  2. EVERY error lands IMMEDIATELY in the container output ('docker logs').
     That is the decisive point: the diagnostics in the settings are useless if
     the backend is already no longer responding at the moment of the error.
     The container output survives that - it goes directly to Docker, not
     through our own process.

  3. Each error carries CONTEXT: what was being attempted, with which values,
     and a hint on what to do. "KeyError: 'id'" is not an error message but a
     riddle.

Usage
-----
    from app.errors import guard, report, Codes

    # a) As a context: the block may fail without tearing everything down
    with guard(Codes.DB_QUERY, "read metric history", client=client_id):
        rows = db.get_metrics_history(client_id)

    # b) Just report
    report(Codes.VPN_TUNNEL, exc, "create tunnel", client=client_id)

    # c) As a decorator for background work
    @guarded(Codes.TASK_LOOP, "patch engine")
    async def engine(): ...
"""

from __future__ import annotations

import functools
import sys
import time
import traceback
from contextlib import contextmanager


class Codes:
    """
    Codes by area.

    The numbers are deliberately fixed and are NOT re-sorted - a code in an old
    log should still mean the same thing a year later.
    """

    # --- Start and basic operation ---
    BOOT = "RMM-BOOT-001"            # error while booting
    BOOT_ROUTER = "RMM-BOOT-002"     # a router could not be loaded
    SHUTDOWN = "RMM-BOOT-003"        # error while shutting down

    # --- Datenbank ---
    DB_QUERY = "RMM-DB-001"          # query failed
    DB_SLOW = "RMM-DB-002"           # query takes too long
    DB_ABORT = "RMM-DB-003"          # query was aborted (time limit)
    DB_LOCKED = "RMM-DB-004"         # database locked
    DB_MIGRATE = "RMM-DB-005"        # migration failed
    DB_HUGE = "RMM-DB-006"           # table is dangerously large

    # --- HTTP ---
    API_UNHANDLED = "RMM-API-001"    # unhandled exception in a request
    API_SLOW = "RMM-API-002"         # request takes too long
    API_VALIDATION = "RMM-API-003"   # input unusable

    # --- Socket.IO / Agenten ---
    SOCK_HANDLER = "RMM-SOCK-001"    # event handling failed
    SOCK_REGISTER = "RMM-SOCK-002"   # registration of an agent
    SOCK_EMIT = "RMM-SOCK-003"       # send to an agent
    SOCK_TIMEOUT = "RMM-SOCK-004"    # agent did not answer

    # --- Background tasks ---
    TASK_LOOP = "RMM-TASK-001"       # continuous loop crashed
    TASK_ONESHOT = "RMM-TASK-002"    # one-off task failed

    # --- VPN / Node ---
    VPN_TUNNEL = "RMM-VPN-001"
    VPN_PACKET = "RMM-VPN-002"
    VPN_ENDPOINT = "RMM-VPN-003"
    NODE_MODULE = "RMM-NODE-001"
    NODE_PROXY = "RMM-NODE-002"
    NODE_L2 = "RMM-NODE-003"

    # --- Event loop ---
    LOOP_BLOCKED = "RMM-LOOP-001"
    LOOP_KILL = "RMM-LOOP-002"

    # --- Files / external ---
    FILE_IO = "RMM-IO-001"
    EXTERNAL = "RMM-EXT-001"


# What a code means and what to do about it. Included in the log - so nobody
# has to look it up in the documentation first.
HINTS = {
    Codes.DB_SLOW: "A query blocks the database and thus everyone else. "
                   "Usually an unbounded query on a large table. Check the "
                   "caller in the stack trace.",
    Codes.DB_ABORT: "The query was aborted after the time limit so the server "
                    "keeps running. That is the emergency stop, not the cause "
                    "- the query itself needs to be fixed.",
    Codes.DB_LOCKED: "Another operation holds the database. Is a backup or an "
                     "external access to the file running?",
    Codes.DB_HUGE: "A table is so large that queries on it stall the server. "
                   "Set a retention period in the privacy settings.",
    Codes.API_UNHANDLED: "The endpoint threw an exception it does not handle. "
                         "The caller gets a 500.",
    Codes.SOCK_TIMEOUT: "The agent was online but did not answer in time. "
                        "Network, load or a hanging agent.",
    Codes.TASK_LOOP: "A background loop crashed. The supervisor restarts it; "
                     "without it the feature would be silently gone.",
    Codes.LOOP_BLOCKED: "Something synchronous runs in the event loop. The "
                        "backend does not respond at all meanwhile.",
    Codes.NODE_L2: "The L2 bridge is not available. The tunnels keep running "
                   "in NAT mode - no outage, just a different sender address.",
}

# Counter per code, for the diagnostics view.
counters: dict[str, int] = {}
# Last report per code - against repetition floods.
_last_seen: dict[str, float] = {}
# Short history of the last errors, so the UI can show them.
recent: list[dict] = []
MAX_RECENT = 200
# The same code is fully printed at most every so many seconds.
REPEAT_WINDOW = 10.0


def _console(text: str) -> None:
    """
    Directly into the container output.

    Deliberately sys.__stderr__ and not print(): print() goes through our own
    capture, which could itself be affected in an error case. The original
    stream goes directly to Docker.
    """
    try:
        stream = sys.__stderr__ or sys.stderr
        stream.write(text if text.endswith("\n") else text + "\n")
        stream.flush()
    except Exception:
        pass


def report(code: str, exc: BaseException | None = None, doing: str = "",
           **context) -> str:
    """
    Reports an error. Returns the code so the caller can also show it to the
    user.

    NEVER throws. An error in the error handling would be the worst thing that
    can happen - then one loses the trail exactly when it is needed.
    """
    try:
        counters[code] = counters.get(code, 0) + 1
        now = time.time()
        seen = _last_seen.get(code, 0.0)
        _last_seen[code] = now
        repeated = (now - seen) < REPEAT_WINDOW

        where = f" | {doing}" if doing else ""
        details = ", ".join(f"{k}={v!r}" for k, v in context.items() if v is not None)
        head = f"[ERROR {code}]{where}"
        if details:
            head += f" | {details}"
        if exc is not None:
            head += f" | {type(exc).__name__}: {exc}"

        entry = {"code": code, "at": int(now * 1000), "doing": doing,
                 "context": {k: str(v) for k, v in context.items()},
                 "error": f"{type(exc).__name__}: {exc}" if exc else "",
                 "count": counters[code]}
        recent.append(entry)
        if len(recent) > MAX_RECENT:
            del recent[:len(recent) - MAX_RECENT]

        if repeated:
            # Repetition. A recurring error must NOT flood the log - otherwise
            # it hides exactly the one other message that matters. It is
            # therefore reported only at certain counts: 2, 3, 5, 10, 25, 50,
            # 100 - after that only every 100.
            n = counters[code]
            milestones = (2, 3, 5, 10, 25, 50, 100)
            if n in milestones or (n > 100 and n % 100 == 0):
                _console(f"{head} (repetition, {n}x total)")
            return code

        lines = [head]
        hint = HINTS.get(code)
        if hint:
            lines.append(f"    -> {hint}")
        if exc is not None:
            tb = "".join(traceback.format_exception(type(exc), exc,
                                                    exc.__traceback__))
            lines.append("    " + tb.rstrip().replace("\n", "\n    "))
        _console("\n".join(lines))

        # Additionally into the diagnostics file if maintenance mode is running.
        try:
            from app import diagnostics
            diagnostics.write("\n".join(lines), level="ERROR", from_stream=True)
        except Exception:
            pass
        return code
    except Exception:
        # Very last fallback level.
        try:
            _console(f"[ERROR {code}] (reporting itself failed)")
        except Exception:
            pass
        return code


@contextmanager
def guard(code: str, doing: str = "", reraise: bool = False, **context):
    """
    Catches everything that goes wrong in this block.

    'reraise=True' reports and still re-raises - for places where the caller
    really must see the error (e.g. an HTTP endpoint that should return a 500),
    but one still wants the context in the log.
    """
    try:
        yield
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:      # noqa: BLE001 - intentional here
        report(code, exc, doing, **context)
        if reraise:
            raise


def guarded(code: str, doing: str = "", **context):
    """Decorator - works for ordinary and for async functions."""

    def wrap(fn):
        if _is_async(fn):
            @functools.wraps(fn)
            async def inner_async(*a, **kw):
                try:
                    return await fn(*a, **kw)
                except (KeyboardInterrupt, SystemExit):
                    raise
                except BaseException as exc:      # noqa: BLE001
                    report(code, exc, doing or fn.__name__, **context)
                    return None
            return inner_async

        @functools.wraps(fn)
        def inner(*a, **kw):
            try:
                return fn(*a, **kw)
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:          # noqa: BLE001
                report(code, exc, doing or fn.__name__, **context)
                return None
        return inner

    return wrap


def _is_async(fn) -> bool:
    import inspect
    return inspect.iscoroutinefunction(fn)


def summary() -> dict:
    """For the diagnostics view: which errors how often, and the latest ones."""
    return {
        "counters": dict(sorted(counters.items(), key=lambda x: -x[1])),
        "recent": recent[-50:],
        "total": sum(counters.values()),
    }


def reset() -> None:
    counters.clear()
    _last_seen.clear()
    recent.clear()
