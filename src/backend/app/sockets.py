"""
sockets.py
----------
Manages all real-time connections (WebSockets via the Socket.IO library).

There are TWO separate "namespaces" (= independent communication channels):

  /agent      -> the agents connect here (the programs running on the managed
                 PCs/servers). Only who sends the correct AGENT_TOKEN may
                 connect here.

  /dashboard  -> the browser frontend connects here to get live updates (e.g.
                 "client X is now online", "new CPU load for client Y").

The flow for a command like "run 'dir' in the terminal" is:
  1. Frontend calls the REST route POST /api/clients/{id}/exec
  2. This route calls request_exec() from THIS file
  3. request_exec() sends an "exec" event via the agent namespace to exactly
     the right agent and WAITS (with an asyncio.Future) for its answer
  4. The agent runs the command and sends back an "exec-result" event
  5. The exec-result handler here finds the waiting future again and
     "fulfills" it with the result - this lets request_exec() in step 2 return
     its answer, and the REST route can send it back to the frontend.

This "send a request out, wait for the answer with the same request ID"
pattern is called "request/response over events" and is the standard way to
achieve something over WebSockets that feels like a normal function call.
"""

import asyncio
import time
import uuid
import json as _json


def _json_dumps_safe(obj) -> "str | None":
    """Metric snapshot as compact JSON (for the history). None on problems, so
    storing the data point never fails on an exotic value."""
    try:
        return _json.dumps(obj, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return None

import socketio

from app.config import AGENT_TOKEN
from app import db


def _int_env_early(name: str, default: int) -> int:
    """Integer from an environment variable - usable even before the server is
    created (the later _env_int is further below and is not needed here yet).
    Nonsensical values fall back to the default instead of preventing the
    start."""
    import os
    try:
        v = int(os.getenv(name, "") or default)
        return v if v > 0 else default
    except ValueError:
        return default


# The actual Socket.IO server. async_mode="asgi" means: it runs together with
# FastAPI in the same modern Python web-server standard (ASGI).
# ----------------------------------------------------------------------
# Server limits: MUST be set, otherwise the defaults of python-socketio apply -
# and those do not fit what this project sends.
#
# max_http_buffer_size (default: 1 MB)
#   A screen frame is a JPEG that the agent scales to up to 1920 px,
#   base64-encodes (+37 %) and sends in ONE packet. At quality 70-90 such a
#   frame regularly lies between 400 KB and 1.1 MB. If ONE packet exceeds the
#   limit, Engine.IO does not just drop the packet - it disconnects. That is
#   exactly why the screen works "halfway" (simple images get through,
#   detailed ones blow the limit) and exactly why the agent goes offline
#   doing it. 50 MB leave every frame room without a single client binding
#   noteworthy memory.
#
# ping_interval / ping_timeout (default: 25 s / 20 s)
#   Behind Cloudflare, unused WebSockets are closed after about 100 s of
#   silence. With a 20 s cadence several pings always fall within this window;
#   the 40 s timeout prevents a brief dropout (proxy switch, mobile network)
#   from being counted immediately as a disconnect.
_SOCKET_MAX_PACKET = _int_env_early("RMM_SOCKET_MAX_PACKET", 50_000_000)


from app import modules as _modules

# Optional module functions (all None-safe, see _Optional)
recording = _modules.optional("recordings", "recording")      # module 'recordings'
patching = _modules.optional("patching", "patching")          # module 'patching'
portforward = _modules.optional("network", "portforward")     # module 'network' (port forward)
from app import agent_streams                                 # TCP through agents (core building block)
rdp_gateway = _modules.optional("guacamole", "rdp_gateway")   # module 'guacamole'

sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins="*",
    max_http_buffer_size=_SOCKET_MAX_PACKET,
    ping_interval=20,
    ping_timeout=40,
)


# ======================================================================
# END-TO-END ENCRYPTION of the agent connection (app/security.py)
# ======================================================================
# Agents with the server key connect with an encrypted handshake (token inside).
# After that EVERY event in both directions travels as event "x" with one
# encrypted binary argument; plain events from such an agent are dropped (a
# man in the middle on plain http could otherwise inject commands). Handlers
# stay unchanged: incoming "x" is decrypted and dispatched to the normal
# handler, outgoing sio.emit(..., namespace="/agent") is encrypted per agent.
import contextvars as _ctxvars
from app import security as _sec

E2E_NS = ("/agent", "/node")       # namespaces of devices (agents, nodes)
_e2e: dict = {}                    # sid -> security.Channel
_sid_sec: dict = {}                # sid -> {"e2e": bool, "tls": bool}
_e2e_inner = _ctxvars.ContextVar("e2e_inner", default=False)
_raw_emit = sio.emit
_raw_trigger = sio._trigger_event


def _emit_args(data) -> list:
    if data is None:
        return []
    return list(data) if isinstance(data, tuple) else [data]


async def _emit(event, data=None, to=None, room=None, skip_sid=None, namespace=None,
                callback=None, ignore_queue=False):
    if namespace not in E2E_NS or event == "x" or not _e2e:
        return await _raw_emit(event, data, to=to, room=room, skip_sid=skip_sid, namespace=namespace,
                               callback=callback, ignore_queue=ignore_queue)
    target = to if to is not None else room
    if target is not None and target in _e2e:                       # one encrypted agent
        blob = _e2e[target].seal(_sec.pack(event, _emit_args(data)))
        return await _raw_emit("x", blob, to=target, namespace=namespace, callback=callback,
                               ignore_queue=ignore_queue)
    if target is not None and target in _sid_sec:                   # one plain agent
        return await _raw_emit(event, data, to=target, namespace=namespace, callback=callback,
                               ignore_queue=ignore_queue)
    # room / broadcast: every member gets its own (encrypted or plain) copy
    skip = set(skip_sid if isinstance(skip_sid, (list, tuple, set)) else [skip_sid] if skip_sid else [])
    try:
        members = [m[0] if isinstance(m, tuple) else m for m in sio.manager.get_participants(namespace, target)]
    except Exception:
        members = []
    for sid in members:
        if sid in skip:
            continue
        ch = _e2e.get(sid)
        if ch:
            await _raw_emit("x", ch.seal(_sec.pack(event, _emit_args(data))), to=sid, namespace=namespace)
        else:
            await _raw_emit(event, data, to=sid, namespace=namespace)


async def _trigger(event, namespace, *args):
    if (namespace in E2E_NS and event not in ("connect", "disconnect", "x") and args
            and args[0] in _e2e and not _e2e_inner.get()):
        print(f"[e2e] plain event '{event}' from encrypted agent dropped (injection?)")
        return None
    return await _raw_trigger(event, namespace, *args)


sio.emit = _emit
sio._trigger_event = _trigger


def e2e_accept(sid: str, environ: dict, auth) -> dict:
    """Connect of a device: returns the (decrypted) auth dict. Encrypted handshake ->
    the channel is registered for this sid. Raises ConnectionRefusedError."""
    auth = auth if isinstance(auth, dict) else {}
    e2e = auth.get("e2e")
    if not e2e:
        _sid_sec[sid] = {"e2e": False, "tls": _conn_tls(environ or {})}
        return auth
    try:
        if str(e2e.get("kid")) != _sec.key_id():
            raise socketio.exceptions.ConnectionRefusedError("e2e: unknown server key")
        channel = _sec.server_channel(_sec.b64d(e2e["epk"]), _sec.b64d(e2e["n"]), str(e2e["kid"]))
        inner = _json.loads(channel.open(_sec.b64d(e2e["t"]), aad=b"auth"))
        if abs(time.time() - float(inner.get("ts") or 0)) > _sec.MAX_SKEW:
            raise socketio.exceptions.ConnectionRefusedError("e2e: clock difference too large")
    except socketio.exceptions.ConnectionRefusedError:
        raise
    except Exception:
        raise socketio.exceptions.ConnectionRefusedError("e2e: handshake failed")
    _e2e[sid] = channel
    _sid_sec[sid] = {"e2e": True, "tls": _conn_tls(environ or {})}
    inner["_e2e"] = True
    return inner


def e2e_forget(sid: str) -> None:
    _e2e.pop(sid, None)
    _sid_sec.pop(sid, None)


def security_of(client_id: str) -> dict | None:
    """{"e2e": bool, "tls": bool} of an ONLINE agent, else None."""
    sid = state.client_id_to_sid.get(client_id) if "state" in globals() else None
    return _sid_sec.get(sid) if sid else None


def _conn_tls(environ: dict) -> bool:
    scope = environ.get("asgi.scope") or {}
    return (scope.get("scheme") in ("https", "wss")
            or str(environ.get("HTTP_X_FORWARDED_PROTO", "")).lower().startswith("https"))


# ======================================================================
# Secure ALL socket handlers centrally
# ======================================================================
# This file has around 50 event handlers. If one of them throws an exception,
# python-socketio does catch it - but the message lands in its own logger,
# which is silent by default. The agent then waits for an answer that never
# comes, and nothing is in the log. That is the most unpleasant kind of error:
# it looks as if someone did nothing.
#
# Equipping 50 handlers individually with try/except would be error-prone and
# incomplete again with the next new handler. Instead the registration
# mechanism is wrapped HERE, in a single place: every handler registered via
# @sio.on or @sio.event gets the safeguard automatically - including every
# future one.
#
# What the wrapper does:
#   * catches every exception and reports it WITH a code and the event name
#   * sends an error response back if the request carried a requestId -
#     otherwise the caller waits up to its time limit
#   * measures the duration and reports notably slow handlers

def _wrap_handler(handler, event_name: str):
    """
    Places the safeguard around a single event handler.

    GROUND RULE: this wrapper must never make a handler unusable. It is an
    improvement, not a core component - if anything fails here, the ORIGINAL
    handler is registered. An unguarded handler is still better than a broken
    one. That is exactly what once went wrong: the wrapper lost the signature,
    and as a result no dashboard could sign in anymore.
    """
    import functools
    import inspect

    if getattr(handler, "_rmm_guarded", False):
        return handler
    if not callable(handler):
        return handler

    async def _fail(sid, args, exc):
        from app.errors import report, Codes
        client_id = state.sid_to_client_id.get(sid) if isinstance(sid, str) else None
        code = report(Codes.SOCK_HANDLER, exc, f"event '{event_name}'",
                      sid=sid, client=client_id)
        # If the request carried an ID, an answer MUST go back - otherwise the
        # other side hangs up to its time limit.
        try:
            payload = next((a for a in args if isinstance(a, dict)), None)
            request_id = (payload or {}).get("requestId")
            if request_id:
                future = state.pending_requests.get(request_id)
                if future and not future.done():
                    future.set_result({"ok": False, "error_code": code,
                                       "error": str(exc)})
        except Exception:
            pass

    # ---------------------------------------------------------------
    # IMPORTANT: the SIGNATURE must be preserved.
    #
    # python-socketio looks at a handler's parameter list to decide how to call
    # it. The 'connect' handler gets (sid, environ, auth) if it has three
    # parameters, and only (sid, environ) if it has two.
    #
    # A wrapper with (*args, **kwargs) has NO named parameters from this point
    # of view - socketio then calls with two arguments, and the real handler
    # does not get its 'auth'. That is exactly what happened:
    #   TypeError: dashboard_connect() missing 1 required positional
    #   argument: 'auth'
    # As a result no dashboard could sign in anymore.
    #
    # functools.wraps copies __name__ and __doc__, but NOT the signature. That
    # is why it is taken over explicitly here: wraps() already sets __wrapped__,
    # and inspect.signature() follows it - as long as one does not overwrite
    # __signature__. To be safe it is set explicitly, so it does not depend on
    # implementation details.
    try:
        _sig = inspect.signature(handler)
    except (TypeError, ValueError):
        _sig = None

    # How many positional arguments does the handler really take? python-socketio
    # calls e.g. 'disconnect' with (sid, reason) and relies on retrying without
    # 'reason' on a TypeError. But this wrapper accepts everything and swallows
    # the TypeError - so it trims the arguments itself to the number the handler
    # expects (otherwise disconnect handlers would never run).
    _max_pos = None
    if _sig is not None:
        _params = list(_sig.parameters.values())
        if not any(p.kind == p.VAR_POSITIONAL for p in _params):
            _max_pos = sum(1 for p in _params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD))

    if inspect.iscoroutinefunction(handler):
        @functools.wraps(handler)
        async def guarded(*args, **kwargs):
            started = time.monotonic()
            if _max_pos is not None and len(args) > _max_pos:
                args = args[:_max_pos]
            try:
                return await handler(*args, **kwargs)
            except asyncio.CancelledError:
                raise
            except socketio.exceptions.ConnectionRefusedError:
                # Intentional rejection of a connection attempt (wrong token): MUST reach
                # python-socketio. Previously it was swallowed here - then the connection
                # counted as allowed, even with a wrong agent token.
                raise
            except Exception as exc:
                await _fail(args[0] if args else None, args, exc)
                return None
            finally:
                took = time.monotonic() - started
                if took > 2.0:
                    try:
                        from app.errors import report, Codes
                        report(Codes.SOCK_HANDLER, None,
                               f"event '{event_name}' was slow",
                               duration=f"{took:.1f}s")
                    except Exception:
                        pass
    else:
        @functools.wraps(handler)
        def guarded(*args, **kwargs):
            if _max_pos is not None and len(args) > _max_pos:
                args = args[:_max_pos]
            try:
                return handler(*args, **kwargs)
            except socketio.exceptions.ConnectionRefusedError:
                raise                                   # see above: do not swallow the rejection
            except Exception as exc:
                from app.errors import report, Codes
                report(Codes.SOCK_HANDLER, exc, f"event '{event_name}'")
                return None

    guarded._rmm_guarded = True
    if _sig is not None:
        guarded.__signature__ = _sig
    return guarded


_orig_on = sio.on
_orig_event = sio.event


def _safe_wrap(fn, event):
    """Wraps - and falls back to the original on problems."""
    try:
        wrapped = _wrap_handler(fn, event)
        # Cross-check: the signature MUST be preserved, otherwise
        # python-socketio calls the handler with the wrong number of arguments.
        import inspect
        if inspect.signature(wrapped) != inspect.signature(fn):
            print(f"[sockets] safeguard for '{event}' skipped - "
                  f"signature would have changed.")
            return fn
        return wrapped
    except Exception as e:
        print(f"[sockets] safeguard for '{event}' not possible ({e}) - "
              f"handler continues unguarded.")
        return fn


def _guarded_on(event, handler=None, namespace=None):
    """Replacement for sio.on - inserts the safeguard in between."""
    if handler is not None:
        return _orig_on(event, _safe_wrap(handler, event), namespace)

    def decorator(fn):
        _orig_on(event, _safe_wrap(fn, event), namespace)
        return fn
    return decorator


def _guarded_event(*args, **kwargs):
    """Replacement for sio.event (usable with and without parentheses)."""
    if len(args) == 1 and callable(args[0]) and not kwargs:
        fn = args[0]
        return _orig_event(_safe_wrap(fn, fn.__name__))

    # BUG FIXED: previously this was _orig_event(handler, namespace=...). But
    # with arguments python-socketio returns only a DECORATOR there and registers
    # NOTHING. Affected were 'connect' and 'disconnect' in the /agent namespace:
    # the agent-token check and the acceptance gate never ran (every connection
    # was accepted, even with a wrong token), and disconnected agents were not
    # reported as offline. Now it is registered directly via sio.on.
    def decorator(fn):
        _orig_on(fn.__name__, _safe_wrap(fn, fn.__name__), kwargs.get("namespace"))
        return fn
    return decorator


sio.on = _guarded_on
sio.event = _guarded_event


def verify_handlers() -> dict:
    """
    Checks at start that every event handler still has the signature that
    python-socketio expects.

    Why: a handler with a wrong signature does NOT show up at start. It only
    fails when someone triggers the event - that is, when a dashboard signs in,
    a shell is opened or an agent reports. And then the log only shows a
    TypeError somewhere in a library. This check pulls the error forward to the
    start, where it belongs.
    """
    import inspect
    problems = []
    checked = 0
    handlers = getattr(sio, "handlers", {})
    for namespace, events in handlers.items():
        if not isinstance(events, dict):
            continue
        for event, fn in events.items():
            if not callable(fn):
                continue
            checked += 1
            try:
                params = list(inspect.signature(fn).parameters)
            except (TypeError, ValueError):
                problems.append(f"{namespace}:{event} (signature not readable)")
                continue
            # 'connect' gets (sid, environ) or (sid, environ, auth),
            # 'disconnect' (sid) or (sid, reason), everything else (sid, data).
            expected = {"connect": (2, 3), "disconnect": (1, 2)}.get(event)
            if expected and len(params) not in expected:
                problems.append(
                    f"{namespace or '/'}:{event} has {len(params)} parameters "
                    f"({', '.join(params)}), expected {' or '.join(map(str, expected))}")
    if problems:
        for p in problems:
            print(f"[sockets] SIGNATURE PROBLEM: {p}")
        try:
            from app.errors import report, Codes
            report(Codes.BOOT, None, "socket handlers with wrong signature",
                   count=len(problems), affected="; ".join(problems[:5]))
        except Exception:
            pass
    else:
        print(f"[sockets] {checked} event handlers checked - signatures OK.")
    return {"checked": checked, "problems": problems}


async def _e2e_dispatch(namespace: str, sid, data):
    """Encrypted event of a device -> decrypt -> the normal handler (its return value is the ack)."""
    ch = _e2e.get(sid)
    if ch is None or not isinstance(data, (bytes, bytearray)):
        return None
    try:
        event, args = _sec.unpack(ch.open(bytes(data)))
    except Exception as e:
        print(f"[e2e] message from {sid} rejected: {e}")
        return None
    if event in ("connect", "disconnect", "x"):
        return None
    tok = _e2e_inner.set(True)
    try:
        return await _raw_trigger(event, namespace, sid, *args)
    finally:
        _e2e_inner.reset(tok)


@sio.on("x", namespace="/agent")
async def _e2e_in(sid, data):
    return await _e2e_dispatch("/agent", sid, data)


@sio.on("x", namespace="/node")
async def _e2e_in_node(sid, data):
    return await _e2e_dispatch("/node", sid, data)


class ConnectionState:
    """Holds the current live state of all connected agents in memory."""

    def __init__(self) -> None:
        self.client_id_to_sid: dict[str, str] = {}   # client ID -> Socket.IO session ID
        self.sid_to_client_id: dict[str, str] = {}   # reverse mapping (for disconnect)
        self.live_metrics: dict[str, dict] = {}      # client ID -> last reported metrics
        self.last_metric_save: dict[str, int] = {}   # client ID -> timestamp of the last stored point
        self.pending_requests: dict[str, asyncio.Future] = {}  # request_id -> waiting future
        # client ID -> time (monotonic) at which the agent freshly registered
        # after an update ("updated: true"). Queried by the update endpoint to
        # confirm an update as successful.
        self.update_confirmed: dict[str, float] = {}
        # Running agent updates (triggered from the dashboard): stage/detail
        # from agent-action-log, updated=True after a successful re-register.
        self.update_jobs: dict[str, dict] = {}
        # Debounced offline display: on a connection drop a client is NOT
        # reported offline immediately, but only after a grace period
        # (AGENT_OFFLINE_GRACE_S, ~2 heartbeats). last_disconnect remembers the
        # time of the drop, _offline_tasks the planned offline report (cancelled
        # on a reconnect).
        self.last_disconnect: dict[str, float] = {}
        self._offline_tasks: dict = {}

    def is_online(self, client_id: str) -> bool:
        # Live socket NOW - decisive for actions (exec, screen, relay, ...)
        # that really need an open connection.
        return client_id in self.client_id_to_sid

    def ui_online(self, client_id: str) -> bool:
        """Online status for display/notifications - DEBOUNCED: a client stays
        'online' across a brief reconnect dropout and only counts as offline
        when it has missed the grace period (see disconnect() or
        _deferred_offline())."""
        if client_id in self.client_id_to_sid:
            return True
        ts = self.last_disconnect.get(client_id)
        return ts is not None and (time.monotonic() - ts) < AGENT_OFFLINE_GRACE_S


# A single, global state object for the whole app
state = ConnectionState()


# ======================================================================
# ADMISSION GATE FOR AGENTS  ("one after another")
# ----------------------------------------------------------------------
# Problem solved here: after a container restart (or a brief network outage)
# ALL agents want to connect at once. Every registration triggers several
# database writes, notifier rules and two broadcasts to all dashboards. With
# 50+ agents at once this produces thousands of parallel coroutines and DB
# accesses in the same second - the container is finished off by the OOM
# killer or the blocked event loop.
#
# Solution in three stages:
#   1. DOORMAN (semaphore): only a limited number of agents may be in the
#      registration process at the same time. Default: 1 - so really strictly
#      one after another.
#   2. QUEUE: whoever gets no slot waits. If the wait gets too long or the
#      queue is full, the connection is refused politely; the agent retries
#      later with backoff.
#   3. SERIALIZATION: the actual 'register' processing runs under a lock, so it
#      never overlaps - no matter how many sockets are open.
#
# All values are configurable via environment variables, so one can turn it
# back up on bigger hardware without changing code.
# ======================================================================

import os as _os


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(_os.getenv(name, "") or default))
    except ValueError:
        return default


# How many agents may register AT THE SAME TIME?
#
# Was 1 - that was too strict and broke more than it helped. A registration
# takes milliseconds; the problem was never two simultaneous registrations,
# but a hundred. With 4 slots the load stays capped, but a handful of agents
# gets back in immediately after a network outage, instead of pushing each
# other into ever longer waits.
AGENT_ADMISSION_SLOTS = _env_int("AGENT_ADMISSION_SLOTS", 4)
# How long may an agent wait for its slot before being refused?
AGENT_ADMISSION_WAIT_S = _env_int("AGENT_ADMISSION_WAIT_S", 45)
# How many agents may stand in the queue at most? Beyond that it is refused
# immediately, instead of filling ever more memory with waiters.
AGENT_ADMISSION_QUEUE = _env_int("AGENT_ADMISSION_QUEUE", 100)
# Emergency stop: if an agent does not register within this time after
# connecting, its slot is released again.
#
# Was 30 s. That was far too long: a registration follows the connect within
# milliseconds. If the connection drops exactly in between, the dead slot
# blocked the gate for half a minute - with few slots this cripples the
# re-entry of all agents.
AGENT_ADMISSION_HOLD_S = _env_int("AGENT_ADMISSION_HOLD_S", 10)
# How many heartbeats are processed at the same time? Heartbeats write to the
# DB - unlimited parallelism is exactly the load that kills the container.
AGENT_HEARTBEAT_SLOTS = _env_int("AGENT_HEARTBEAT_SLOTS", 4)

# Grace period before a disconnected agent is shown/reported as OFFLINE. The
# agent sends a heartbeat every ~5-6 s; ~16 s tolerates two missed heartbeats
# (plus reconnect time) without a brief dropout making the whole fleet jump to
# offline in the dashboard. A reconnect within the grace period cancels the
# offline report.
AGENT_OFFLINE_GRACE_S = _env_int("AGENT_OFFLINE_GRACE_S", 16)

_admission_sem: "asyncio.Semaphore | None" = None
_register_lock: "asyncio.Lock | None" = None
_heartbeat_sem: "asyncio.Semaphore | None" = None
_admission_waiting = 0                 # currently in the queue
_admission_held: dict[str, object] = {}  # sid -> watchdog task (holds a slot)
_admission_stats = {"granted": 0, "refused": 0, "healed": 0}


def _ensure_gate() -> None:
    """Creates the gate objects on first use.

    Deliberately late: asyncio primitives may only exist once the event loop
    is running - on module import it is not yet.
    """
    global _admission_sem, _register_lock, _heartbeat_sem
    if _admission_sem is None:
        _admission_sem = asyncio.Semaphore(AGENT_ADMISSION_SLOTS)
    if _register_lock is None:
        _register_lock = asyncio.Lock()
    if _heartbeat_sem is None:
        _heartbeat_sem = asyncio.Semaphore(AGENT_HEARTBEAT_SLOTS)


async def _admission_acquire(sid: str) -> bool:
    """Waits for a free slot in the gate. False = refused."""
    global _admission_waiting
    _ensure_gate()
    if _admission_waiting >= AGENT_ADMISSION_QUEUE:
        return False
    _admission_waiting += 1
    try:
        await asyncio.wait_for(_admission_sem.acquire(),
                               timeout=AGENT_ADMISSION_WAIT_S)
    except asyncio.TimeoutError:
        _admission_stats["refused"] += 1
        print(f"[gate] agent {sid} refused - all {AGENT_ADMISSION_SLOTS} "
              f"slots busy, {_admission_waiting - 1} more waiting")
        return False
    finally:
        _admission_waiting -= 1

    # Arm the emergency stop: release the slot automatically if no 'register'
    # arrives.
    async def _watchdog():
        try:
            await asyncio.sleep(AGENT_ADMISSION_HOLD_S)
        except asyncio.CancelledError:
            return
        print(f"[gate] agent {sid} did not register - slot released")
        _admission_release(sid)

    _admission_held[sid] = asyncio.ensure_future(_watchdog())
    _admission_stats["granted"] += 1
    return True


def _admission_release(sid: str) -> None:
    """Releases an agent\047s slot again (multiple calls are harmless)."""
    task = _admission_held.pop(sid, None)
    if task is None:
        return
    try:
        task.cancel()
    except Exception:
        pass
    if _admission_sem is not None:
        try:
            _admission_sem.release()
        except ValueError:
            pass


# ----------------------------------------------------------------------
# Bundled dashboard broadcasts
# ----------------------------------------------------------------------
# 'clients:changed' has no content - it only tells the frontend to "reload the
# list". 80 agents connecting within two seconds previously triggered 160 such
# broadcasts (and thus 160 reloads per dashboard). Now at most one per second
# is sent.
_clients_changed_task: "object | None" = None


def _emit_clients_changed_soon(delay: float = 1.0) -> None:
    """Tells the dashboards in a bundled way that the client list has changed."""
    global _clients_changed_task
    task = _clients_changed_task
    if task is not None and not task.done():
        return   # a report is already on the way

    async def _fire():
        await asyncio.sleep(delay)
        try:
            await sio.emit("clients:changed", namespace="/dashboard")
        except Exception:
            pass

    try:
        _clients_changed_task = asyncio.ensure_future(_fire())
    except RuntimeError:
        _clients_changed_task = None


def _new_request_id() -> str:
    return uuid.uuid4().hex



# ------------------------------------------------------------------
# Process the offline queue (on agent register and on a trigger from
# clients_routes when the agent is already online).
# ------------------------------------------------------------------
_queue_busy: set = set()


async def push_maintenance(client_id: str) -> dict:
    """Distribute the maintenance status to agent + dashboards (after set/expiry)."""
    m = db.client_maintenance(await db.call(db.get_client, client_id))
    try:
        await send_to_agent(client_id, "maintenance-mode", m)
    except Exception:
        pass
    await sio.emit("clients:changed", namespace="/dashboard")
    return m


async def maintenance_expiry_loop() -> None:
    """Ends expired maintenance modes (core background loop)."""
    while True:
        await asyncio.sleep(30)
        for cid in await db.call(db.expired_maintenance_ids):
            await db.call(db.set_maintenance, cid, None)
            await push_maintenance(cid)


async def kick_offline_queue(client_id: str) -> None:
    if client_id in _queue_busy:
        return
    items = await db.call(db.queue_pending, client_id)
    if not items:
        return

    async def _work():
        _queue_busy.add(client_id)
        try:
            for it in items:
                await db.call(db.queue_set_status, it["id"], "running")
                try:
                    payload = _json.loads(it.get("payload") or "{}")
                    if it["kind"] == "shell":
                        res = await request_exec(client_id, payload.get("command", ""),
                                                 timeout_seconds=float(payload.get("timeout", 300)),
                                                 elevated=bool(payload.get("elevated")))
                        out = (res.get("stdout") or "") + (res.get("stderr") or "")
                        ok = int(res.get("code") or 0) == 0
                        await db.call(db.queue_set_status, it["id"],
                                      "done" if ok else "failed",
                                      f"exit {res.get('code')}\n{out[:3000]}")
                    elif it["kind"] == "software":
                        from app import modules as _mods
                        roll = _mods.use("software", "rollout") if _mods.is_enabled("software") else None
                        if roll is None:
                            await db.call(db.queue_set_status, it["id"], "failed",
                                          "module 'software' is not active")
                        else:
                            res = await roll.install(client_id, payload.get("package_id"))
                            await db.call(db.queue_set_status, it["id"],
                                          "done" if res.get("ok") else "failed",
                                          str(res.get("output") or res.get("error") or "")[:3000])
                    elif str(it["kind"]).startswith("mod:"):
                        # Generic for modules: "mod:<module>:<file>" -> <file>.queue_run(client, payload)
                        from app import modules as _mods
                        _p = str(it["kind"]).split(":")
                        hdl = _mods.use(_p[1], _p[2]) if len(_p) == 3 and _mods.is_enabled(_p[1]) else None
                        if hdl is None or not hasattr(hdl, "queue_run"):
                            await db.call(db.queue_set_status, it["id"], "failed",
                                          f"module '{_p[1] if len(_p) > 1 else '?'}' is not active")
                        else:
                            await db.call(db.queue_set_status, it["id"], "running")
                            res = await hdl.queue_run(client_id, payload)
                            await db.call(db.queue_set_status, it["id"],
                                          "done" if res.get("ok") else "failed",
                                          str(res.get("output") or "")[:3000])
                    else:
                        await db.call(db.queue_set_status, it["id"], "failed",
                                      f"unknown action kind {it['kind']}")
                except Exception as exc:
                    await db.call(db.queue_set_status, it["id"], "failed", str(exc)[:1000])
            await sio.emit("queue:changed", {"client_id": client_id}, namespace="/dashboard")
        finally:
            _queue_busy.discard(client_id)

    asyncio.get_running_loop().create_task(_work())


async def request_exec(client_id: str, command: str, timeout_seconds: float = 20.0,
                       session: str | None = None, shell: str = "auto",
                       elevated: bool = False) -> dict:
    """
    Sends a shell command to an agent and waits for the result.
    Returns: {"stdout": ..., "stderr": ..., "code": ...}
    Throws an exception if the client is offline or does not answer in time.

    'session' identifies a terminal window: the agent keeps a separate working
    directory per session, so 'cd' takes effect across several commands.
    'shell' = 'cmd'|'powershell'|'auto', 'elevated' = as administrator (Windows).
    """
    sid = state.client_id_to_sid.get(client_id)
    if not sid:
        raise RuntimeError("Client is offline")

    request_id = _new_request_id()
    future: asyncio.Future = asyncio.get_event_loop().create_future()
    state.pending_requests[request_id] = future

    await sio.emit(
        "exec",
        {"requestId": request_id, "command": command, "session": session,
         "shell": shell, "elevated": elevated},
        to=sid, namespace="/agent",
    )

    try:
        result = await asyncio.wait_for(future, timeout=timeout_seconds)
    finally:
        state.pending_requests.pop(request_id, None)
    return result


async def request_fs_list(client_id: str, path: str, timeout_seconds: float = 15.0) -> list[dict]:
    """
    Requests the contents of a folder from an agent.
    Returns a list of file/folder entries.
    """
    sid = state.client_id_to_sid.get(client_id)
    if not sid:
        raise RuntimeError("Client is offline")

    request_id = _new_request_id()
    future: asyncio.Future = asyncio.get_event_loop().create_future()
    state.pending_requests[request_id] = future

    await sio.emit("fs-list", {"requestId": request_id, "path": path}, to=sid, namespace="/agent")

    try:
        payload = await asyncio.wait_for(future, timeout=timeout_seconds)
    finally:
        state.pending_requests.pop(request_id, None)

    if payload.get("error"):
        raise RuntimeError(payload["error"])
    return payload.get("entries", [])


async def request_proc_list(client_id: str, timeout_seconds: float = 15.0) -> list[dict]:
    """Requests the running process list of a client (for the task manager)."""
    sid = state.client_id_to_sid.get(client_id)
    if not sid:
        raise RuntimeError("Client is offline")

    request_id = _new_request_id()
    future: asyncio.Future = asyncio.get_event_loop().create_future()
    state.pending_requests[request_id] = future

    await sio.emit("proc-list", {"requestId": request_id}, to=sid, namespace="/agent")

    try:
        payload = await asyncio.wait_for(future, timeout=timeout_seconds)
    finally:
        state.pending_requests.pop(request_id, None)

    if payload.get("error"):
        raise RuntimeError(payload["error"])
    return payload.get("processes", [])


async def request_proc_kill(client_id: str, pid: int, timeout_seconds: float = 10.0) -> dict:
    """Asks an agent to terminate a process."""
    sid = state.client_id_to_sid.get(client_id)
    if not sid:
        raise RuntimeError("Client is offline")

    request_id = _new_request_id()
    future: asyncio.Future = asyncio.get_event_loop().create_future()
    state.pending_requests[request_id] = future

    await sio.emit("proc-kill", {"requestId": request_id, "pid": pid}, to=sid, namespace="/agent")

    try:
        payload = await asyncio.wait_for(future, timeout=timeout_seconds)
    finally:
        state.pending_requests.pop(request_id, None)
    return payload


async def request_fs_read(client_id: str, path: str, timeout_seconds: float = 30.0) -> dict:
    """Requests a file from an agent (for the download in the dashboard)."""
    sid = state.client_id_to_sid.get(client_id)
    if not sid:
        raise RuntimeError("Client is offline")

    request_id = _new_request_id()
    future: asyncio.Future = asyncio.get_event_loop().create_future()
    state.pending_requests[request_id] = future

    await sio.emit("fs-read", {"requestId": request_id, "path": path}, to=sid, namespace="/agent")

    try:
        payload = await asyncio.wait_for(future, timeout=timeout_seconds)
    finally:
        state.pending_requests.pop(request_id, None)

    if payload.get("error"):
        raise RuntimeError(payload["error"])
    return payload


async def request_patch_ping(client_id: str, timeout_seconds: float = 8.0) -> dict:
    """
    Asks the agent whether it supports patching at all. If it does not answer
    within a few seconds, an agent without the patch handlers runs there - then
    there is no point sending a scan.
    """
    sid = state.client_id_to_sid.get(client_id)
    if not sid:
        raise RuntimeError("Client is offline")

    request_id = _new_request_id()
    future: asyncio.Future = asyncio.get_event_loop().create_future()
    state.pending_requests[request_id] = future
    await sio.emit("patch-ping", {"requestId": request_id}, to=sid, namespace="/agent")
    try:
        return await asyncio.wait_for(future, timeout=timeout_seconds)
    finally:
        state.pending_requests.pop(request_id, None)


async def request_patch_selftest(client_id: str, timeout_seconds: float = 90.0) -> dict:
    """
    Short function test on the client: are the update sources reachable, does
    the agent run elevated? Takes seconds instead of minutes and answers the
    question "is it the agent or the system?".
    """
    sid = state.client_id_to_sid.get(client_id)
    if not sid:
        raise RuntimeError("Client is offline")

    request_id = _new_request_id()
    future: asyncio.Future = asyncio.get_event_loop().create_future()
    state.pending_requests[request_id] = future
    await sio.emit("patch-selftest", {"requestId": request_id},
                   to=sid, namespace="/agent")
    try:
        payload = await asyncio.wait_for(future, timeout=timeout_seconds)
    finally:
        state.pending_requests.pop(request_id, None)
    if payload.get("error"):
        raise RuntimeError(payload["error"])
    return payload


async def request_patch_scan(client_id: str, timeout_seconds: float = 900.0) -> dict:
    """
    Lets an agent scan for available updates.

    Generous timeout: the Windows Update query regularly takes several minutes
    on sluggish systems. An agent from patch protocol 2 on always answers - if
    need be with an error message; if it stays silent anyway, something is
    wrong on the client.

    Unlike before, an error response is NOT turned into an exception here: the
    caller wants to evaluate the extra data provided (sources, capabilities)
    even on an error.
    """
    sid = state.client_id_to_sid.get(client_id)
    if not sid:
        raise RuntimeError("Client is offline")

    request_id = _new_request_id()
    future: asyncio.Future = asyncio.get_event_loop().create_future()
    state.pending_requests[request_id] = future

    await sio.emit("patch-scan", {"requestId": request_id}, to=sid, namespace="/agent")

    try:
        return await asyncio.wait_for(future, timeout=timeout_seconds)
    finally:
        state.pending_requests.pop(request_id, None)


async def request_patch_apply(client_id: str, items: list[dict],
                              timeout_seconds: float = 7200.0) -> dict:
    """
    Lets an agent install the named updates.
    Very long timeout - feature updates can run for two hours.
    """
    sid = state.client_id_to_sid.get(client_id)
    if not sid:
        raise RuntimeError("Client is offline")

    request_id = _new_request_id()
    future: asyncio.Future = asyncio.get_event_loop().create_future()
    state.pending_requests[request_id] = future

    await sio.emit("patch-apply", {"requestId": request_id, "items": items},
                   to=sid, namespace="/agent")

    try:
        return await asyncio.wait_for(future, timeout=timeout_seconds)
    finally:
        state.pending_requests.pop(request_id, None)


async def request_fs_op(client_id: str, event: str, data: dict,
                        timeout_seconds: float = 60.0) -> dict:
    """
    Generic helper for writing FS operations on a client:
    fs-write (upload/edit), fs-mkdir, fs-delete, fs-rename.
    All answer with the event 'fs-op-result'.
    """
    sid = state.client_id_to_sid.get(client_id)
    if not sid:
        raise RuntimeError("Client is offline")

    request_id = _new_request_id()
    future: asyncio.Future = asyncio.get_event_loop().create_future()
    state.pending_requests[request_id] = future

    await sio.emit(event, {"requestId": request_id, **data}, to=sid, namespace="/agent")

    try:
        payload = await asyncio.wait_for(future, timeout=timeout_seconds)
    finally:
        state.pending_requests.pop(request_id, None)

    if payload.get("error"):
        raise RuntimeError(payload["error"])
    return payload


# ====================================================================
# Handlers for the /agent namespace (connections from the agent programs)
# ====================================================================

# ------------------------------------------------------------------
# Automatic agent update
# ------------------------------------------------------------------
# Outdated agents are updated automatically on connecting when the feature
# applies. Effective mode per client:
#   auto_update = 'on'     -> always update
#   auto_update = 'off'    -> never update automatically
#   auto_update = 'global' -> the settings switch 'agent_auto_update' decides
# A cooldown per client prevents update loops if an update fails and the agent
# keeps reporting with the old version.

_AUTO_UPDATE_COOLDOWN_S = 1800   # 30 min between two auto-update attempts
_auto_update_last: dict[str, float] = {}
# After a backend restart ALL agents reconnect at the same time. Without a
# brake all outdated agents would get the update at the same moment (load spike
# on hosts with many guests, all clients briefly gone). Therefore: randomly
# delayed in the first minutes after the start, and at most _AUTO_UPDATE_PARALLEL
# updates are triggered at the same time. The cooldown is additionally in the DB
# so it survives a restart.
_PROC_START = time.time()
_AUTO_UPDATE_PARALLEL = 2
_auto_update_sem: "asyncio.Semaphore | None" = None
# Last heartbeat-based version check per client (max. 1x/60s).
_auto_update_hb_check: dict[str, float] = {}
# Throttle for the metric threshold check (notification rules).
_threshold_check: dict[str, float] = {}

# Path to the shipped agent version (…/backend/app/sockets.py -> …/agent/version.txt)
from pathlib import Path as _Path
from app.paths import AGENT_VERSION_FILE as _AGENT_VERSION_FILE, AGENT_MAIN_FILE as _AGENT_PY_FILE

# What each client said about itself on registering: code checksum and patch
# capability. In memory only - the values come in fresh on every 'register',
# and without a connection they are meaningless.
_agent_code_hashes: dict[str, str] = {}
_agent_caps: dict[str, dict] = {}


def agent_capabilities(client_id: str) -> dict:
    """
    Self-report of a connected agent.

    Replaces the earlier guessing via ping: whoever can patch says so on
    registering. An empty result means "reported nothing" - so agent code
    without the patch module.
    """
    return dict(_agent_caps.get(client_id) or {})

# Checksum of the shipped agent.py, cached by mtime.
_latest_hash_cache: tuple[float, str] = (0.0, "")


def _latest_agent_hash() -> str:
    """
    Checksum of the agent.py that this backend ships.

    Reason for this whole procedure: version.txt is the only source that so far
    indicated whether an agent is outdated. If it is not incremented when
    agent.py changes - and that happens regularly - the backend considers every
    client current and NEVER triggers an update. As a result months-old code
    ran on the clients; new capabilities like 'patch-ping' were simply missing
    there, although they existed in the project.
    """
    global _latest_hash_cache
    try:
        mtime = _AGENT_PY_FILE.stat().st_mtime
    except OSError:
        return ""
    if _latest_hash_cache[0] == mtime and _latest_hash_cache[1]:
        return _latest_hash_cache[1]
    try:
        import hashlib
        digest = hashlib.sha256(_AGENT_PY_FILE.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""
    _latest_hash_cache = (mtime, digest)
    return digest


def _latest_agent_version() -> str:
    """Currently shipped agent version (single source: agent/version.txt)."""
    try:
        return _AGENT_VERSION_FILE.read_text(encoding="utf-8").strip() or ""
    except OSError:
        return ""


def _version_tuple(v: str):
    """'1.2.10' -> (1, 2, 10). Non-numeric parts are counted as 0."""
    parts = []
    for p in str(v).strip().split("."):
        try:
            parts.append(int("".join(ch for ch in p if ch.isdigit()) or 0))
        except ValueError:
            parts.append(0)
    return tuple(parts) if parts else (0,)


def _agent_outdated(agent_v: str, latest_v: str, client_id: str | None = None) -> bool:
    """
    Is the agent on the client older than the shipped one?

    Two criteria, in this order:
      1. Lower version number -> outdated (as before).
      2. Same version number, but DIFFERENT code -> also outdated. Detected via
         the checksum the agent reports on registering. If it reports none, a
         version from before this change runs there - so also old code.

    Criterion 2 only applies if this backend can compute its own checksum;
    without agent.py on disk it stays with the pure version comparison.
    """
    if agent_v and latest_v:
        try:
            if _version_tuple(agent_v) < _version_tuple(latest_v):
                return True
        except Exception:
            if agent_v.strip() != latest_v.strip():
                return True

    if client_id is None:
        return False
    latest_hash = _latest_agent_hash()
    if not latest_hash:
        return False
    return _agent_code_hashes.get(client_id, "") != latest_hash


_migration_last: dict[str, float] = {}


async def _migration_redirect(client_id: str, hostname: str = "") -> bool:
    """Migration active? -> send the agent via an update to the new server (at
    most every 15 min per agent, so a failing update does not create a loop).
    True = triggered."""
    target = (await db.call(db.get_setting, "migration_target_url", "") or "").strip()
    if not target:
        return False
    now = time.time()
    if now - _migration_last.get(client_id, 0.0) < 900:
        return False
    _migration_last[client_id] = now
    await asyncio.sleep(3)                       # let the registration finish cleanly first
    if await send_to_agent(client_id, "update-agent", {}):
        await db.call(db.add_audit_entry, None, "migration.agent_redirected", target=client_id,
                      details=f"{hostname or client_id} -> {target}")
        return True
    return False


async def _maybe_auto_update(client_id: str, agent_version: str | None) -> None:
    """Triggers the self-update on an outdated version if auto-update applies."""
    latest = _latest_agent_version()
    if not _agent_outdated(agent_version or "", latest, client_id):
        _auto_update_last.pop(client_id, None)   # current -> reset the cooldown
        return

    client = await db.call(db.get_client, client_id)
    if not client or not client.get("active", 1):
        return
    mode = (client.get("auto_update") or "global").lower()
    if mode == "off":
        return
    if mode != "on" and await db.call(db.get_setting, "agent_auto_update", "0") != "1":
        return

    now = time.monotonic()
    last = _auto_update_last.get(client_id)
    if last is not None and now - last < _AUTO_UPDATE_COOLDOWN_S:
        return   # tried recently -> do not build an update loop
    try:   # cooldown also across a backend restart
        last_wall = float(await db.call(db.get_setting, f"agent_auto_update_last:{client_id}", "0") or 0)
    except Exception:
        last_wall = 0.0
    if time.time() - last_wall < _AUTO_UPDATE_COOLDOWN_S:
        _auto_update_last[client_id] = now
        return
    _auto_update_last[client_id] = now
    try:
        await db.call(db.set_setting, f"agent_auto_update_last:{client_id}", str(int(time.time())))
    except Exception:
        pass
    asyncio.create_task(_staggered_auto_update(client_id, client, agent_version, latest))


async def _staggered_auto_update(client_id: str, client: dict, agent_version, latest) -> None:
    global _auto_update_sem
    import random
    if time.time() - _PROC_START < 300:
        await asyncio.sleep(random.uniform(60, 240))       # shortly after the start: spread out
    if _auto_update_sem is None:
        _auto_update_sem = asyncio.Semaphore(_AUTO_UPDATE_PARALLEL)
    async with _auto_update_sem:
        if not state.is_online(client_id):
            return
        await _send_auto_update(client_id, client, agent_version, latest)
        await asyncio.sleep(45)                             # release the slot only after a while


async def _send_auto_update(client_id: str, client: dict, agent_version, latest) -> None:
    ok = await send_to_agent(client_id, "update-agent", {})
    if ok:
        await db.call(db.add_audit_entry, None, "agent.auto_update_triggered",
                      target=client_id,
                      details=f"{client.get('hostname')}: {agent_version} -> {latest}")
        print(f"[auto-update] {client.get('hostname')} ({client_id}): "
              f"agent {agent_version} -> {latest} being updated")
        # Inform the dashboard (same event as on the manual update start).
        await sio.emit("client:update-started", {"id": client_id, "auto": True},
                       namespace="/dashboard")


@sio.event(namespace="/agent")
async def connect(sid, environ, auth):
    """
    Called as soon as SOMEONE wants to connect to /agent.

    Two checks, in this order:
      1. AGENT_TOKEN - without the correct token it is refused immediately.
      2. Admission gate - the agent waits here until it is its turn. The wait
         happens BEFORE the connection counts as established, i.e. before any
         data flows. This way only a handful of agents ever really connect at
         the same time.

    Whoever does not get through in time is refused with a clear reason
    ("busy") - the agent then waits itself and tries again. That is much
    cheaper than holding hundreds of half-open connections.
    """
    inner = e2e_accept(sid, environ, auth)
    token = inner.get("token")
    if not inner.get("_e2e") and token == AGENT_TOKEN and _sec.require_e2e():
        e2e_forget(sid)
        raise socketio.exceptions.ConnectionRefusedError(
            "encryption required: update the agent (unencrypted agents are blocked on this server)")
    if token != AGENT_TOKEN:
        e2e_forget(sid)
        raise socketio.exceptions.ConnectionRefusedError("Invalid agent token")

    if not await _admission_acquire(sid):
        raise socketio.exceptions.ConnectionRefusedError(
            "busy: the server is currently admitting other agents - please retry later")


@sio.on("register", namespace="/agent")
async def on_register(sid, payload):
    """
    Outer shell of the registration.

    REBUILT (2026-09-20). The old version had two bugs that together produced
    exactly the picture seen in practice: "the agent installs cleanly, reports
    connected - and never appears in the dashboard."

      1. It caught EVERY exception from _do_register(), only wrote it to the
         console and still sent 'register-ack' with ok=True. The agent
         considered itself registered and started heartbeats, although no
         client record was ever created in the database. The failure was thus
         completely invisible.

      2. It held a global lock during the ENTIRE processing - and the
         processing made around fifteen SYNCHRONOUS database calls directly in
         the event loop. With a large database that was enough to jam the
         registrations so much that the gate refused with "busy" after 45 s.
         Even then the agent only saw a connection that was rebuilt over and
         over.

    Now: the lock is PER CLIENT (two different devices no longer interfere with
    each other), all database work runs in worker threads, and the
    'register-ack' tells the truth - including the reason if it went wrong.
    """
    _ensure_gate()
    client_id = (payload or {}).get("id") or ""
    ok, reason = False, "unknown"
    try:
        async with _register_lock_for(client_id):
            ok, reason = await _do_register(sid, payload)
    except Exception as e:
        from app.errors import report as _report, Codes as _Codes
        code = _report(_Codes.API_UNHANDLED, e, "agent registration",
                       client=client_id or sid)
        ok, reason = False, f"server error ({code})"
    finally:
        try:
            ack = {"ok": ok}
            if not ok:
                ack["reason"] = reason
            await sio.emit("register-ack", ack, to=sid, namespace="/agent")
            if ok:
                # encrypted agents: TLS port + fingerprint (to upgrade the transport);
                # plain agents: the server key, so an updated agent switches to e2e
                await sio.emit("security" if sid in _e2e else "e2e-offer", _sec.public_info(),
                               to=sid, namespace="/agent")
        except Exception:
            pass
        _admission_release(sid)

    if not ok:
        # Deliberately loud: a failed registration is the difference between
        # "device is in the dashboard" and "device does not exist".
        print(f"[register] FAILED for {client_id or sid}: {reason}")


# One lock per client instead of a global one. The gate (_admission_sem) still
# limits HOW MANY registrations may run at the same time; the lock only
# prevents the SAME agent from registering twice in parallel (which happens
# when it reconnects in quick succession).
_register_locks: dict = {}


def _register_lock_for(client_id: str):
    lock = _register_locks.get(client_id)
    if lock is None:
        lock = asyncio.Lock()
        _register_locks[client_id] = lock
        # Clean up so the dictionary does not grow unbounded.
        if len(_register_locks) > 5000:
            for key in [k for k, v in list(_register_locks.items())
                        if not v.locked()][:2500]:
                _register_locks.pop(key, None)
    return lock


async def _do_register(sid, payload) -> tuple[bool, str]:
    """
    An agent registers. Returns: (success, reason).

    The flow is deliberately split into two halves:

      MANDATORY   Everything without which the device does not appear in the
                  dashboard: create the record, assign it to a tenant, notify
                  dashboards. If something fails here, the function reports a
                  failure - the agent learns of it and retries, instead of
                  going into a silent idle.

      OPTIONAL    Everything else: auto-update, notification rules, patch info,
                  node configuration, crash report. Each of these points is in
                  its own try - an error there must no longer topple the
                  registration. Previously exactly that could happen: a broken
                  notification rule prevented a device from being admitted.

    All database calls run via db.call() in a worker thread. In the event loop
    nothing may wait on a file lock anymore - all agents, the shell and the
    remote screen hang on it.
    """
    payload = payload or {}
    client_id = payload.get("id")
    hostname = payload.get("hostname")
    if not client_id or not hostname:
        return False, "registration without id/hostname"

    # ---------------- MANDATORY --------------------------------------
    state.client_id_to_sid[client_id] = sid
    state.sid_to_client_id[sid] = client_id

    # Reconnect within the offline grace period: cancel a possibly already
    # planned offline report and discard the drop time, so a brief dropout does
    # NOT come through as "offline".
    state.last_disconnect.pop(client_id, None)
    _pending_off = state._offline_tasks.pop(client_id, None)
    if _pending_off is not None and not _pending_off.done():
        _pending_off.cancel()

    # Remember the reported code checksum - MUST happen before any
    # outdated-check, otherwise the first check would consider every agent
    # outdated.
    _agent_code_hashes[client_id] = str(payload.get("agent_code_hash") or "")
    _agent_caps[client_id] = {
        "protocol": int(payload.get("patch_protocol") or 0),
        "sources": list(payload.get("patch_sources") or []),
        "agent_version": payload.get("agent_version") or "",
        "agent_code_hash": payload.get("agent_code_hash") or "",
    }

    try:
        was_known = await db.call(db.get_client, client_id) is not None
        await db.call(
            db.upsert_client,
            client_id,
            hostname,
            payload.get("platform"),
            payload.get("arch"),
            payload.get("release"),
            payload.get("ip"),
        )
    except Exception as e:
        from app.errors import report as _report, Codes as _Codes
        code = _report(_Codes.API_UNHANDLED, e,
                       "write client record on register",
                       client=client_id)
        return False, f"database not writing ({code})"

    # Assignment to tenant/location. Without tenant_id the dashboard HIDES the
    # client in most views - so this belongs in the mandatory part and not in
    # the optional one.
    try:
        client = await db.call(db.get_client, client_id)
        if client and not client["tenant_id"]:
            token = payload.get("enrollment_token")
            token_row = (await db.call(db.get_enrollment_token, token)) if token else None

            if token_row and token_row["tenant_id"]:
                fields = {"tenant_id": token_row["tenant_id"],
                          "location_id": token_row["location_id"]}
                if token_row["client_name"]:
                    fields["hostname"] = token_row["client_name"]
            else:
                tenant, location = await db.call(db.ensure_uncategorized)
                fields = {"tenant_id": tenant, "location_id": location}
                if token_row and token_row["client_name"]:
                    fields["hostname"] = token_row["client_name"]

            await db.call(db.update_client, client_id, fields)
            if token:
                await db.call(db.mark_enrollment_token_used, token)
    except Exception as e:
        from app.errors import report as _report, Codes as _Codes
        code = _report(_Codes.API_UNHANDLED, e,
                       "assign client to a tenant", client=client_id)
        return False, f"assignment failed ({code})"

    # From here the registration counts as succeeded: the record exists, and
    # the dashboards learn of it. This happens BEFORE the optional part, so no
    # side task can delay the appearance in the dashboard.
    try:
        await sio.emit("client:online", {"id": client_id}, namespace="/dashboard")
        _emit_clients_changed_soon()
    except Exception as e:
        print(f"[register] dashboards not notified: {e!r}")

    # ---------------- OPTIONAL ---------------------------------------
    async def _side(name, coro_factory):
        """A side task - may fail without toppling the registration."""
        try:
            await coro_factory()
        except Exception as e:
            print(f"[register] '{name}' for {client_id} failed: {e!r}")

    async def _version():
        await db.call(db.set_agent_version, client_id, payload.get("agent_version"))

    async def _updated_flag():
        if not payload.get("updated"):
            return
        state.update_confirmed[client_id] = time.monotonic()
        job = state.update_jobs.get(client_id)
        if job:
            job.update(updated=True, stage="updated", version=payload.get("agent_version") or "")
        await sio.emit("client:updated", {"id": client_id}, namespace="/dashboard")
        from app import notifier
        from router.admin_routes import build_notification
        host = hostname or client_id
        await notifier.fire_event("agent_update", client_id=client_id,
            notification=build_notification(
                f"Agent on {host} was updated to version "
                f"{payload.get('agent_version') or '?'}.",
                head=f"Agent updated - {host}",
                client=host, service="Agent update", level="success"))

    async def _auto_update():
        # Server move active (Source -> Migration -> "move agents"): every agent
        # that still reports HERE gets the update command. The update script
        # writes the new server address -> the agent then connects to the new
        # backend. Applies regardless of the agent version.
        if await _migration_redirect(client_id, hostname):
            return
        pending = await db.call(db.get_client, client_id)
        if pending and pending.get("pending_agent_update"):
            await db.call(db.set_pending_agent_update, client_id, False)
            if _agent_outdated(payload.get("agent_version") or "",
                               _latest_agent_version(), client_id):
                if await send_to_agent(client_id, "update-agent", {}):
                    await db.call(db.add_audit_entry, None,
                                  "agent.pending_update_triggered",
                                  target=client_id,
                                  details=(pending.get("hostname") or client_id))
                    await sio.emit("client:update-started",
                                   {"id": client_id, "auto": True},
                                   namespace="/dashboard")
        elif await db.call(db.get_setting, "agent_auto_update_offline", "1") == "1":
            await _maybe_auto_update(client_id, payload.get("agent_version"))

    async def _device_type():
        changed = await db.call(db.apply_detected_device_type, client_id, payload.get("device_type"))
        if await db.call(db.apply_detected_kind, client_id, payload.get("device_kind")):
            changed = True
        if changed:
            _emit_clients_changed_soon()

    async def _crash_report():
        crash = payload.get("last_crash")
        if not crash:
            return
        row = await db.call(db.get_client, client_id)
        host = (row or {}).get("hostname") or hostname or client_id
        await db.call(db.add_audit_entry, None, "agent.crashed",
                      target=client_id, details=str(crash)[:1000])
        print(f"[agent-crash] {host}: agent crashed and was restarted:"
              f"\n{crash}")
        await sio.emit("client:agent-crashed", {
            "id": client_id, "hostname": host, "error": str(crash)[-2000:],
        }, namespace="/dashboard")

    async def _subnets():
        subnets = payload.get("subnets")
        if subnets:
            await db.call(db.set_client_subnets, client_id, subnets)

    async def _diag_mode():
        if await db.call(db.get_setting, "maintenance_agents", "0") == "1":
            await sio.emit("diag-mode", {"enabled": True, "minutes": 0},
                           to=sid, namespace="/agent")

    async def _notify_online():
        from app import notifier
        from router.admin_routes import build_notification
        host = hostname or client_id
        await notifier.fire_event("client_online", client_id=client_id,
            notification=build_notification(
                f"{host} is now online.",
                head=f"Client online - {host}",
                client=host, service="Status", level="success"))
        if not was_known:
            await notifier.fire_event("client_new", client_id=client_id,
                notification=build_notification(
                    f"New client {host} was registered.",
                    head=f"New client - {host}",
                    client=host, service="Onboarding", level="info"))

    async def _node_check():
        from app import node_manager
        await node_manager.on_agent_online(client_id)

    await _side("node-check", _node_check)
    await _side("agent-version", _version)
    await _side("update-confirmation", _updated_flag)
    await _side("auto-update", _auto_update)
    await _side("device-type", _device_type)
    await _side("crash-report", _crash_report)
    async def _maintenance_push():
        # Maintenance mode (Core) to the agent: suspend protection mechanisms.
        m = db.client_maintenance(await db.call(db.get_client, client_id))
        await sio.emit("maintenance-mode", m, to=sid, namespace="/agent")

    async def _blackbox_cfg():
        # Give the blackbox configuration (Settings -> General) to the agent.
        enabled = (await db.call(db.get_setting, "blackbox_enabled", "0")) == "1"
        try:
            interval = int(await db.call(db.get_setting, "blackbox_interval_min", "5") or 5)
        except (TypeError, ValueError):
            interval = 5
        await sio.emit("blackbox-config", {"enabled": enabled,
                                           "interval_min": max(1, min(interval, 240))},
                       to=sid, namespace="/agent")

    async def _offline_queue():
        # Offline queue: run the pre-marked actions now
        # ("if agent online, then install software XY").
        await kick_offline_queue(client_id)

    async def _pending_handoff():
        # Backend migration with a NEW address: agents that only come online
        # now get the new server address delivered afterwards (set by
        # POST /api/migration/handoff on the OLD backend).
        url = await db.call(db.get_setting, "migration_handoff_url", "")
        if url:
            await sio.emit("migrate-server", {"url": url}, to=sid, namespace="/agent")

    await _side("subnets", _subnets)
    await _side("diag-mode", _diag_mode)
    await _side("notifications", _notify_online)
    await _side("migrations-handoff", _pending_handoff)
    await _side("blackbox-config", _blackbox_cfg)
    await _side("maintenance-core", _maintenance_push)
    await _side("offline-queue", _offline_queue)

    return True, "ok"


@sio.on("heartbeat", namespace="/agent")
async def on_heartbeat(sid, payload):
    """An agent reports its current metrics every few seconds (CPU/RAM/disk/...)."""
    client_id = payload.get("id")
    if not client_id:
        return

    # Heartbeats are the second load factor: they write to the database and
    # send to all dashboards. Without a limit, arbitrarily many of them run at
    # once with many clients. We let only a fixed number through at a time -
    # and when registrations are running, THOSE take precedence: a skipped
    # heartbeat has no consequences, an aborted registration does.
    _ensure_gate()
    # Registrations take precedence: a skipped data point has no consequences,
    # a delayed registration does. The gate itself is now checked (waiting +
    # occupied slots) - the old global register lock no longer exists, it was
    # the bottleneck for registrations.
    if _admission_waiting or _admission_held:
        return
    if _heartbeat_sem.locked():
        return   # backlog -> skip this data point instead of piling up
    async with _heartbeat_sem:
        await _handle_heartbeat(client_id, payload)


# Time of the last cleanup of the metric history (see below).
_last_prune = [0]


async def _handle_heartbeat(client_id, payload):
    """
    Actual heartbeat processing (see on_heartbeat).

    The database accesses run in a thread, NOT in the event loop. Reason: the
    database is in the bind mount, so on a NAS on a slow file system. A single
    hanging write previously blocked the entire backend - no HTTP response, no
    Socket.IO pings, all agents drop at once. Exactly this pattern is in the
    log on 22.08. at 23:38:29.

    The heartbeat is by far the most frequent write path (every agent every
    five seconds), so it is offloaded first.
    """
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, db.touch_client, client_id)

    # Auto-update also for ALREADY connected clients: if a new agent version
    # is rolled out while the client is online, the register check no longer
    # applies. So here a sparing (max. 1x/60s per client) version check that
    # triggers the auto-update if needed.
    now_check = time.monotonic()
    if now_check - _auto_update_hb_check.get(client_id, 0.0) >= 60.0:
        _auto_update_hb_check[client_id] = now_check
        try:
            _c = await db.call(db.get_client, client_id)
            if _c:
                await _maybe_auto_update(client_id, _c.get("agent_version"))
        except Exception:
            pass

    metrics = payload.get("metrics", {})
    now_ms = int(time.time() * 1000)
    metrics["ts"] = now_ms
    state.live_metrics[client_id] = metrics

    # --- Persistence: store at most one point every metrics_interval_seconds ---
    # (Heartbeats come every few seconds, but are stored only at the configured
    #  cadence, so the DB does not fill up.)
    # The data point is written AND cleaned up - both are write accesses to a
    # table that quickly grows very large. They previously ran synchronously in
    # the event loop; a single slow fsync thereby froze the whole backend.
    # Now: worker thread.
    try:
        interval_ms = (await db.call(db.get_int_setting,
                                     "metrics_interval_seconds")) * 1000
        last_save = state.last_metric_save.get(client_id, 0)
        if now_ms - last_save >= interval_ms:
            state.last_metric_save[client_id] = now_ms
            mem_total = metrics.get("memTotal") or 0
            cpu = metrics.get("cpuLoad") or 0
            ram = (metrics.get("memUsed", 0) / mem_total * 100) if mem_total else 0
            await db.call(
                db.record_metric_point,
                client_id, cpu, ram,
                metrics.get("netIn") or 0, metrics.get("netOut") or 0,
                now_ms,
                extra=_json_dumps_safe(metrics),
            )
            # Clean up everything outside the retention period.
            # 0 (or negative) = keep unlimited -> NEVER prune.
            # The cleanup runs at most every five minutes, not on every data
            # point: with many agents it was the most expensive single
            # operation in the heartbeat path and yet hundredfold redundant -
            # deleted is deleted.
            retention_hours = await db.call(db.get_int_setting,
                                            "metrics_retention_hours")
            if retention_hours > 0 and now_ms - _last_prune[0] >= 300_000:
                _last_prune[0] = now_ms
                retention_ms = retention_hours * 3600 * 1000
                await db.call(db.prune_metrics_history, now_ms - retention_ms)
    except Exception as e:
        print(f"[metrics] could not store data point: {e}")

    await sio.emit("client:metrics", {"id": client_id, "metrics": metrics}, namespace="/dashboard")

    # Notification rules: metric thresholds (CPU/RAM/disk/temp). Check at most
    # every 30 s per client - the actual alarm cooldown (no spam) is handled by
    # the rule itself (notifier._cooled_down).
    now_check2 = time.monotonic()
    if now_check2 - _threshold_check.get(client_id, 0.0) >= 30.0:
        _threshold_check[client_id] = now_check2
        try:
            from app import notifier
            _c = await db.call(db.get_client, client_id)
            if _c:
                await notifier.check_metric_thresholds(_c, metrics)
        except Exception as e:
            print(f"[notify] threshold check failed: {e}")


# ------------------------------------------------------------------
# VPN: reports from the nodes
# ------------------------------------------------------------------
# Since the tunnels end on the nodes (real WireGuard), the backend no longer
# carries payload data. What arrives here is only state reports and - if the
# node sits behind symmetric NAT - the relay's packets. The handlers are
# deliberately thin; the logic lies in the port forwarding.

# VPN REMOVED (2026-09-20)
# ---------------------------
# 'node-wg-state', 'wg-relay' and 'node-net-packet' were here. The last was the
# dangerous one: EVERY single IP packet of a tunnel ran as a Socket.IO event
# through this event loop and was distributed from there via ensure_future. An
# active tunnel thus flooded the same loop that also serves registrations, the
# shell and the screen.
#
# The events are now deliberately NO longer accepted. An old agent that still
# sends them simply gets no reaction from python-socketio - that is harmless
# and costs nothing.


# --- Port forwarding ----------------------------------------------
# It uses the same events as the VPN used to (vpn-open/-data/-close). The agent
# already knows them; only the counterpart in the backend is now a different
# one - no longer the (removed) userspace stack, but directly the forwarding.

# Additionally: app/agent_streams.py (TCP through an agent for modules, e.g.
# the server browser). Both sides only know their own stream IDs.

@sio.on("vpn-open-result", namespace="/agent")
async def on_forward_open_result(sid, payload):
    portforward.on_open_result(payload or {})
    agent_streams.on_open_result(payload or {})


@sio.on("vpn-data", namespace="/agent")
async def on_forward_data(sid, payload):
    portforward.on_data(payload or {})
    agent_streams.on_data(payload or {})


@sio.on("vpn-close", namespace="/agent")
async def on_forward_close(sid, payload):
    portforward.on_close(payload or {})
    agent_streams.on_close(payload or {})


# 'node-wg-result' and request_node_wg() were dropped with the VPN: they
# served only to set up WireGuard on a node.


async def request_tempuser(client_id: str, expiry: str, timeout_seconds: float = 60.0) -> dict:
    """Creates a temporary user with a one-time password on the agent.
    expiry: '1h' | '2h' | '4h' | '24h' | 'logoff' | 'never'."""
    sid = state.client_id_to_sid.get(client_id)
    if not sid:
        raise RuntimeError("Client is offline")
    request_id = _new_request_id()
    future: asyncio.Future = asyncio.get_event_loop().create_future()
    state.pending_requests[request_id] = future
    await sio.emit("tempuser-create", {"requestId": request_id, "expiry": expiry},
                   to=sid, namespace="/agent")
    try:
        return await asyncio.wait_for(future, timeout=timeout_seconds)
    finally:
        state.pending_requests.pop(request_id, None)


@sio.on("tempuser-result", namespace="/agent")
async def on_tempuser_result(sid, payload):
    """An agent's answer to 'tempuser-create'."""
    future = state.pending_requests.get((payload or {}).get("requestId"))
    if future and not future.done():
        future.set_result(payload)


@sio.on("exec-result", namespace="/agent")
async def on_exec_result(sid, payload):
    """An agent's answer to a previously sent 'exec' command."""
    future = state.pending_requests.get(payload.get("requestId"))
    if future and not future.done():
        future.set_result(payload)


@sio.on("agent-action-log", namespace="/agent")
async def on_agent_action_log(sid, payload):
    """
    Progress/error report of the agent regarding update/uninstall. Passed
    directly to the dashboards, so one sees in real time what happens on the
    client (instead of only a generic 60s timeout).
    """
    try:
        job = state.update_jobs.get(payload.get("id") or "")
        if job and payload.get("kind") == "update" and not job.get("updated"):
            job["stage"] = str(payload.get("stage") or "")[:40]
            job["detail"] = str(payload.get("detail") or "")[:2000]
    except Exception:
        pass
    await sio.emit("client:action-log", {
        "id": payload.get("id"),
        "kind": payload.get("kind"),
        "stage": payload.get("stage"),
        "detail": payload.get("detail"),
        "agent_version": payload.get("agent_version"),
    }, namespace="/dashboard")


@sio.on("fs-result", namespace="/agent")
async def on_fs_result(sid, payload):
    """An agent's answer to a previously sent 'fs-list' request."""
    future = state.pending_requests.get(payload.get("requestId"))
    if future and not future.done():
        future.set_result(payload)


@sio.on("patch-ping-result", namespace="/agent")
async def on_patch_ping_result(sid, payload):
    """
    Answer to the capability query. The reported capabilities are taken over
    right away - this way the state is current even if something changed on the
    client after registering (winget installed afterwards, rights changed).
    """
    client_id = state.sid_to_client_id.get(sid)
    if client_id and payload.get("protocol"):
        _agent_caps[client_id] = {
            "protocol": int(payload.get("protocol") or 0),
            "sources": list(payload.get("sources") or []),
            "agent_version": payload.get("agent_version") or "",
            "agent_code_hash": payload.get("agent_code_hash") or "",
            "elevated": bool(payload.get("elevated")),
            "winget": payload.get("winget") or "",
        }
    future = state.pending_requests.get(payload.get("requestId"))
    if future and not future.done():
        future.set_result(payload)


@sio.on("patch-selftest-result", namespace="/agent")
async def on_patch_selftest_result(sid, payload):
    """Answer to the function test."""
    future = state.pending_requests.get(payload.get("requestId"))
    if future and not future.done():
        future.set_result(payload)


@sio.on("patch-progress", namespace="/agent")
async def on_patch_progress(sid, payload):
    """
    Intermediate status of a running scan or installation.

    Without these reports a patch job was a black box: minutes of silence, then
    either a result or a timeout - without any hint where it got stuck.
    """
    client_id = state.sid_to_client_id.get(sid) or payload.get("id")
    if not client_id:
        return
    try:
        patching.on_progress(client_id, payload)
    except Exception:
        pass
    await sio.emit("patch:progress", {"id": client_id, **payload},
                   namespace="/dashboard")


@sio.on("patch-scan-result", namespace="/agent")
async def on_patch_scan_result(sid, payload):
    """An agent\047s answer with the list of available updates."""
    future = state.pending_requests.get(payload.get("requestId"))
    if future and not future.done():
        future.set_result(payload)


@sio.on("patch-apply-result", namespace="/agent")
async def on_patch_apply_result(sid, payload):
    """An agent\047s answer after installing updates."""
    future = state.pending_requests.get(payload.get("requestId"))
    if future and not future.done():
        future.set_result(payload)


@sio.on("proc-result", namespace="/agent")
async def on_proc_result(sid, payload):
    """An agent\047s answer with the process list."""
    future = state.pending_requests.get(payload.get("requestId"))
    if future and not future.done():
        future.set_result(payload)


@sio.on("proc-kill-result", namespace="/agent")
async def on_proc_kill_result(sid, payload):
    """An agent\047s answer to a process-kill request."""
    future = state.pending_requests.get(payload.get("requestId"))
    if future and not future.done():
        future.set_result(payload)


@sio.on("fs-read-result", namespace="/agent")
async def on_fs_read_result(sid, payload):
    """An agent\047s answer with the content of a file (download)."""
    future = state.pending_requests.get(payload.get("requestId"))
    if future and not future.done():
        future.set_result(payload)


@sio.on("fs-op-result", namespace="/agent")
async def on_fs_op_result(sid, payload):
    """An agent\047s answer to a writing FS operation
    (upload/create folder/delete/rename)."""
    future = state.pending_requests.get(payload.get("requestId"))
    if future and not future.done():
        future.set_result(payload)



# ------------------------------------------------------------------
# BLACKBOX "last state": agents periodically report a screenshot + metric
# deltas + log tail. If a client goes offline, one sees its last state here.
# Storage: storage/blackbox/<client_id>/ - the screenshot is DISCARDED
# (overwritten) on every new one, metrics appended as deltas to metrics.jsonl
# (capped), logs overwritten.
# ------------------------------------------------------------------

def _blackbox_dir(client_id: str):
    from app.paths import STORAGE_DIR
    d = STORAGE_DIR / "blackbox" / client_id
    d.mkdir(parents=True, exist_ok=True)
    return d


@sio.on("blackbox-snap", namespace="/agent")
async def on_blackbox_snap(sid, data):
    data = data or {}
    client_id = state.sid_to_client_id.get(sid)
    if not client_id or (data.get("id") and data.get("id") != client_id):
        return
    import base64 as _b64

    def _store():
        d = _blackbox_dir(client_id)
        ts = int(time.time() * 1000)
        state_file = d / "state.json"
        try:
            prev = _json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prev = {}
        metrics = data.get("metrics") or {}
        # DELTA: only store fields that changed since the last time.
        last = prev.get("metrics") or {}
        delta = {k: v for k, v in metrics.items() if last.get(k) != v}
        if delta:
            line = _json.dumps({"ts": ts, **delta}, ensure_ascii=False)
            mfile = d / "metrics.jsonl"
            try:
                lines = mfile.read_text(encoding="utf-8").splitlines()[-1999:]
            except OSError:
                lines = []
            lines.append(line)
            mfile.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if data.get("log"):
            (d / "log.txt").write_text(str(data["log"])[-100000:], encoding="utf-8")
        if data.get("shot"):
            try:
                (d / "screenshot.jpg").write_bytes(_b64.b64decode(data["shot"]))
            except Exception:
                pass
        state_file.write_text(_json.dumps({"ts": ts, "metrics": metrics,
                                           "has_shot": bool(data.get("shot")) or prev.get("has_shot", False)},
                                          ensure_ascii=False), encoding="utf-8")

    try:
        await asyncio.get_running_loop().run_in_executor(None, _store)
    except Exception:
        pass   # blackbox is best effort - never disturb the agent connection


@sio.on("screen-frame", namespace="/agent")
async def on_screen_frame(sid, payload):
    """
    An agent sends a screen frame. We forward it to all dashboard clients AND
    record it if a recording is currently running for this client.
    """
    recording.record_frame(
        payload.get("id"),
        payload.get("image", ""),
        payload.get("width", 0),
        payload.get("height", 0),
    )
    await sio.emit("screen-frame", payload, namespace="/dashboard")


@sio.on("screen-error", namespace="/agent")
async def on_screen_error(sid, payload):
    """An agent reports that screen streaming is not possible."""
    # A recording was already created on start - it would now be an empty
    # 0-frame replay. Delete it immediately (file + DB). The audit entry is
    # deliberately kept (the access attempt stays traceable).
    recording.abort_recording(payload.get("id"))
    await sio.emit("screen-error", payload, namespace="/dashboard")


@sio.on("screen-mode", namespace="/agent")
async def on_screen_mode(sid, payload):
    """
    An agent reports HOW remote access is possible (e.g. mode='shell' when
    there is no graphical screen). The dashboard then opens a shell directly
    instead of showing an error.
    """
    await sio.emit("screen-mode", payload, namespace="/dashboard")


# --- Interactive terminal: forward agent -> dashboard ---
# Running terminal sessions: session_id -> history buffer (for ONE audit log
# per session with the full history, instead of per command).
_term_sessions: dict = {}


# Terminal sessions whose output does NOT go to the dashboards, but to a
# specific recipient (e.g. the RMM console via SSH/web): session -> {"output": async fn(str), "exit": async fn()}
term_listeners: dict[str, dict] = {}


@sio.on("term-output", namespace="/agent")
async def on_term_output(sid, payload):
    """Forward shell output to all dashboards + write into the running terminal
    recording (replay instead of a per-command log)."""
    session = payload.get("session")
    listener = term_listeners.get(session)
    if listener:                                  # targeted session (console) - not to all dashboards
        try:
            await listener["output"](payload.get("data", ""))
        except Exception:
            pass
        return
    if session in _term_sessions:
        recording.record_term_data(session, payload.get("data", ""))
    await sio.emit("term-output", payload, namespace="/dashboard")


@sio.on("term-exit", namespace="/agent")
async def on_term_exit(sid, payload):
    listener = term_listeners.pop(payload.get("session"), None)
    if listener:
        try:
            await listener["exit"]()
        except Exception:
            pass
        return
    await sio.emit("term-exit", payload, namespace="/dashboard")
    await _finalize_term_session(payload.get("session"))


# --- Agent console: the dashboard reads along the agent's log ---
# Dashboard -> agent: open/close stream; agent -> dashboard: forward history
# and live lines (filtered in the frontend via payload.id).

@sio.on("agent-console-open", namespace="/dashboard")
async def dashboard_agent_console_open(sid, data):
    client_id = data.get("clientId")
    if not await _ws_user_may(data.get("username"), "use_terminal", client_id):
        await sio.emit("agent-console", {
            "id": client_id,
            "data": "\r\n\x1b[31mNo permission for the agent console of this client.\x1b[0m\r\n",
        }, namespace="/dashboard")
        return
    ok = await send_to_agent(client_id, "agent-console-open", {})
    await sio.emit("agent-console-ack", {"id": client_id, "agent_online": bool(ok)},
                   namespace="/dashboard")


@sio.on("agent-console-close", namespace="/dashboard")
async def dashboard_agent_console_close(sid, data):
    await send_to_agent(data.get("clientId"), "agent-console-close", {})


@sio.on("agent-console", namespace="/agent")
async def on_agent_console(sid, payload):
    await sio.emit("agent-console", payload, namespace="/dashboard")


@sio.on("agent-console-history", namespace="/agent")
async def on_agent_console_history(sid, payload):
    await sio.emit("agent-console-history", payload, namespace="/dashboard")


@sio.event(namespace="/agent")
async def disconnect(sid):
    """An agent lost/ended the connection -> mark as offline."""
    # Release the gate slot first: if the connection drops BEFORE the agent has
    # registered, the waiters must not stay blocked until the watchdog timeout.
    _admission_release(sid)
    e2e_forget(sid)
    client_id = state.sid_to_client_id.pop(sid, None)
    if client_id:
        # Detach only the REAL socket mapping - and only if this socket is
        # still the current one. If the agent reported in the meantime with a
        # NEW socket (reconnect before this disconnect event), the old, dead
        # socket must not topple the new connection.
        if state.client_id_to_sid.get(client_id) == sid:
            state.client_id_to_sid.pop(client_id, None)
        agent_streams.close_all_for(client_id)          # end TCP streams through this agent
        # Already reconnected? Then there is nothing more to do.
        if client_id in state.client_id_to_sid:
            return
        # Debounce the offline DISPLAY: do not report immediately, but only
        # after the grace period check whether the agent is really gone. This
        # way a brief dropout (or a brief loop/network hang that disconnects
        # several agents at once) does not put the whole fleet on offline
        # immediately. live_metrics are kept as the "last state" until the
        # client really counts as offline.
        state.last_disconnect[client_id] = time.monotonic()
        _old = state._offline_tasks.get(client_id)
        if _old is not None and not _old.done():
            _old.cancel()
        state._offline_tasks[client_id] = asyncio.create_task(_deferred_offline(client_id))


async def _deferred_offline(client_id: str) -> None:
    """Reports a client as offline only after AGENT_OFFLINE_GRACE_S - and only
    if it has NOT reconnected by then. A reconnect within the grace period
    cancels this task (see the register handler), so a brief dropout does not
    come through as offline."""
    try:
        await asyncio.sleep(AGENT_OFFLINE_GRACE_S)
    except asyncio.CancelledError:
        return
    # Back in the meantime? Then it was only a brief dropout.
    if client_id in state.client_id_to_sid:
        return
    state.last_disconnect.pop(client_id, None)
    state._offline_tasks.pop(client_id, None)
    state.live_metrics.pop(client_id, None)
    try:
        await sio.emit("client:offline", {"id": client_id}, namespace="/dashboard")
        _emit_clients_changed_soon()
    except Exception as _e:
        print(f"[offline] dashboards not notified: {_e!r}")
    # Notification rules: "client goes offline".
    try:
        from app import notifier
        from router.admin_routes import build_notification
        _c = await db.call(db.get_client, client_id)
        _host = (_c or {}).get("hostname") or client_id
        await notifier.fire_event("client_offline", client_id=client_id,
            notification=build_notification(
                f"{_host} went offline.",
                head=f"Client offline - {_host}",
                client=_host, service="Status", level="warn"))
    except Exception as _e:
        print(f"[notify] offline event failed: {_e}")


async def send_to_agent(client_id: str, event: str, data: dict) -> bool:
    """
    Sends an arbitrary event to a client's agent (without waiting for an
    answer). Used for screen start/stop/input.
    Returns True if the client was online.
    """
    sid = state.client_id_to_sid.get(client_id)
    if not sid:
        return False
    await sio.emit(event, data, to=sid, namespace="/agent")
    return True


# ====================================================================
# Handlers for the /dashboard namespace (connections from the browser frontend)
# ====================================================================
# Note: deliberately (still) no check of the login token on the pure connection
# setup here - the REST routes are already secured via JWT. For even stricter
# protection one could additionally check the JWT in the "auth" object here,
# analogous to the agent token above.

@sio.on("connect", namespace="/dashboard")
async def dashboard_connect(sid, environ, auth):
    # On connect, send the BOOT_ID of the running backend process. The frontend
    # detects a restart from it (BOOT_ID changes) and reloads - this avoids the
    # per-second polling of /api/boot-id.
    try:
        from app.main import BOOT_ID
        await sio.emit("boot:id", {"boot_id": BOOT_ID}, to=sid, namespace="/dashboard")
    except Exception:
        pass


@sio.on("disconnect", namespace="/dashboard")
async def dashboard_disconnect(sid):
    # If this client had subscribed to the backend output, unsubscribe.
    try:
        from app.loghub import hub as _h
        _h.subscribers.discard(sid)
    except Exception:
        pass


# --- Permission check in the (unauthenticated) dashboard namespace ---------
# The dashboard namespace has no JWT handshake; we trust the username sent by
# the frontend (best effort, analogous to the audit log). The REST routes
# remain the hard security boundary; here we additionally prevent
# accidental/naive accesses without a right.
async def _ws_user_may(username: str | None, perm: str,
                       client_id: str | None) -> bool:
    # Now async: the permission check reads the user from the database.
    # Previously this read ran synchronously in the event loop - on every start
    # of a remote screen and every terminal session.
    from app import db as _db
    from app.auth import user_has_permission
    if not username:
        return False
    u = await _db.call(_db.get_user_by_username, username)
    if not u:
        return False
    return user_has_permission(u, perm, client_id)


@sio.on("screen-start", namespace="/dashboard")
async def dashboard_screen_start(sid, data):
    """Dashboard wants to see a client\047s screen -> instruct agent + start recording."""
    from app import db as _db
    client_id = data.get("clientId")
    if not await _ws_user_may(data.get("username"), "use_screen", client_id):
        await sio.emit("screen-error",
                       {"id": client_id, "error": "No permission for the remote screen of this client."},
                       namespace="/dashboard")
        return

    # Pass the recording quality/FPS from the settings to the agent, so it
    # produces the frames accordingly (better/worse quality, more/fewer
    # images). The agent uses these values for the JPEG encoding & the send rate.
    quality = await _db.call(_db.get_int_setting, "screen_record_quality") or 40
    fps = await _db.call(_db.get_int_setting, "screen_record_fps") or 5

    # Consent on the device: physical devices AND VMs get a prompt shown (e.g.
    # when someone works in the VM via RDP) - the screen starts only after
    # confirmation. If nobody is logged in, the agent connects directly. Only
    # LXC containers are exempt (no graphical login possible).
    client = await _db.call(_db.get_client, client_id)
    needs_consent = bool(client) and (client.get("device_type") or "physical") != "lxc"

    # Silent mode (one-off): if the user enabled the mode in the profile AND
    # has the right 'screen_silent' (global) or 'c_screen_silent' on this
    # client, the consent prompt on the device is skipped. Right on the
    # connection setup the mode turns itself OFF again, so it really applies
    # only to THIS one session.
    _u = await _db.call(_db.get_user_by_username, data.get("username") or "")
    if needs_consent and _u and _u.get("silent_screen"):
        from app.auth import is_super_admin as _is_sa
        if (_is_sa(_u) or user_has_permission(_u, "screen_silent")
                or user_has_permission(_u, "c_screen_silent", client_id)):
            needs_consent = False
            await _db.call(_db.set_user_silent_screen, _u["id"], False)  # one-off
            await _db.call(_db.add_audit_entry, _u["username"],
                           "screen.silent_used", target=client_id,
                           details=client.get("hostname") if client else client_id)
            print(f"[screen] silent mode used by {_u['username']} -> no "
                  f"prompt, mode deactivated again")
            # Inform all dashboards: the profile (if open) then visibly turns
            # the toggle OFF again immediately.
            await sio.emit("silent-screen:consumed",
                           {"username": _u["username"],
                            "client": client.get("hostname") if client else client_id},
                           namespace="/dashboard")
    print(f"[screen] start for {client.get('hostname') if client else client_id}: "
          f"device_type={client.get('device_type') if client else '?'} -> require_consent={needs_consent}")

    await send_to_agent(client_id, "screen-start", {
        "quality": quality, "fps": fps,
        "require_consent": needs_consent,
        "requested_by": data.get("username", ""),
        # Multi-user (Windows): the user session chosen by the dashboard whose
        # screen is to be seen/controlled.
        "session": data.get("session"),
    })

    # Start the recording only if globally enabled.
    recording_on = await _db.call(_db.get_setting, "recording_enabled", "1") == "1"

    # Start the recording (if client known and enabled) + audit log.
    if client:
        rec_id = recording.start_recording(
            client_id, client["hostname"], data.get("username", "unknown"),
        ) if recording_on else None
        # Audit entry ALWAYS (log the access) - with a replay link if a
        # recording was made.
        await _db.call(
            _db.add_audit_entry,
            data.get("username", "unknown"),
            "screen.session_started",
            target=client_id,
            details=f"rec:{rec_id}" if rec_id else client["hostname"],
        )
        _screen_open[client_id] = {"user": data.get("username", "unknown"), "ts": time.time()}


# Running sessions (for the session history: end + duration into the audit log)
_screen_open: dict[str, dict] = {}


async def _log_session_end(client_id: str | None, by: str) -> None:
    info = _screen_open.pop(client_id or "", None)
    if not info:
        return
    from app import db as _db
    secs = int(time.time() - info["ts"])
    await _db.call(_db.add_audit_entry, info["user"], "screen.session_ended", target=client_id,
                   details=f"{by};{secs}")


@sio.on("screen-stop", namespace="/dashboard")
async def dashboard_screen_stop(sid, data):
    """Dashboard closed the VNC window -> stop streaming + recording."""
    client_id = data.get("clientId")
    # Anyone with view rights may stop (no harm; only prevents watching).
    if not await _ws_user_may(data.get("username"), "c_screen_view", client_id):
        return
    await send_to_agent(client_id, "screen-stop", {})
    recording.stop_recording(client_id)
    await _log_session_end(client_id, "admin")


@sio.on("screen-audio", namespace="/dashboard")
async def dashboard_screen_audio(sid, data):
    """Turn the remote PC\047s sound on/off (like AnyDesk "transmit sound")."""
    from app import db as _db
    client_id = data.get("clientId")
    if not await _ws_user_may(data.get("username"), "use_screen", client_id):
        return
    on = bool(data.get("enabled"))
    await _db.call(_db.add_audit_entry, data.get("username") or "system",
                   "screen.audio_on" if on else "screen.audio_off", target=client_id, details="")
    await send_to_agent(client_id, "screen-audio", {"enabled": on})


@sio.on("screen-audio", namespace="/agent")
async def on_screen_audio(sid, payload):
    """Audio chunk (mu-law, mono) or status/error from the agent -> dashboard (without recording)."""
    await sio.emit("screen-audio", payload, namespace="/dashboard")


@sio.on("screen-input", namespace="/dashboard")
async def dashboard_screen_input(sid, data):
    """Forward mouse/keyboard input from the dashboard to the agent.
    Former gap: this event checked NO right - whoever knew the client ID could
    send input to a device without use_screen. Now, like all other remote-support
    events, with a permission check."""
    client_id = data.pop("clientId", None)
    username = data.pop("username", None)
    if not client_id:
        return
    if not await _ws_user_may(username, "use_screen", client_id):
        return
    await send_to_agent(client_id, "screen-input", data)


@sio.on("screen-clipboard-get", namespace="/dashboard")
async def dashboard_screen_clipboard_get(sid, data):
    """Dashboard wants to fetch the remote PC\047s clipboard (clipboard sync)."""
    client_id = data.get("clientId")
    if client_id:
        if not await _ws_user_may(data.get("username"), "use_screen", client_id):
            return
        ok = await send_to_agent(client_id, "screen-clipboard-get", {})
        if not ok:
            await sio.emit("screen-clipboard",
                           {"id": client_id, "error": "Client is offline."},
                           namespace="/dashboard")


# --- Chat/control window of the running remote-support session ---
# --- Registry (Windows only, right c_registry per client) ---
@sio.on("registry", namespace="/dashboard")
async def dashboard_registry(sid, data):
    client_id = data.get("clientId")
    op = data.get("op") or "list"
    # Write/delete needs c_registry; reading (list/get) too (the registry can be sensitive).
    if not await _ws_user_may(data.get("username"), "c_registry", client_id):
        await sio.emit("registry-result", {"id": client_id, "rid": data.get("rid"), "op": op,
                       "ok": False, "error": "No permission (registry)."}, namespace="/dashboard")
        return
    ok = await send_to_agent(client_id, "registry", {k: data.get(k) for k in ("op", "path", "name", "type", "data", "rid")})
    if not ok:
        await sio.emit("registry-result", {"id": client_id, "rid": data.get("rid"), "op": op,
                       "ok": False, "error": "Client is offline."}, namespace="/dashboard")


@sio.on("registry-result", namespace="/agent")
async def on_registry_result(sid, payload):
    await sio.emit("registry-result", payload, namespace="/dashboard")


# --- Windows automatic updates off/on (right c_win_update) ---
@sio.on("wu-auto", namespace="/dashboard")
async def dashboard_wu_auto(sid, data):
    from app import db as _db
    client_id = data.get("clientId")
    setting = "disabled" in data
    perm = "c_win_update" if setting else "c_patch_view"
    if not await _ws_user_may(data.get("username"), perm, client_id):
        await sio.emit("wu-auto", {"id": client_id, "supported": True,
                       "error": "No permission."}, namespace="/dashboard")
        return
    payload = {"disabled": bool(data.get("disabled"))} if setting else {}
    ok = await send_to_agent(client_id, "wu-auto", payload)
    if setting and ok:
        await _db.call(_db.add_audit_entry, data.get("username", "?"),
                       "winupdate.auto_" + ("disabled" if data.get("disabled") else "enabled"), target=client_id)
    if not ok:
        await sio.emit("wu-auto", {"id": client_id, "supported": True, "error": "Client is offline."},
                       namespace="/dashboard")


@sio.on("wu-auto", namespace="/agent")
async def on_wu_auto(sid, payload):
    await sio.emit("wu-auto", payload, namespace="/dashboard")


@sio.on("screen-session-chat", namespace="/dashboard")
async def dashboard_session_chat(sid, data):
    """Admin types in the VNC window -> to the agent (shows it to the person at the PC)."""
    client_id = data.get("clientId")
    if not await _ws_user_may(data.get("username"), "use_screen", client_id):
        return
    await send_to_agent(client_id, "screen-session-chat", {"text": (data.get("text") or "")[:2000]})


@sio.on("screen-session-chat", namespace="/agent")
async def on_session_chat(sid, payload):
    await sio.emit("screen-session-chat", payload, namespace="/dashboard")


@sio.on("screen-session-request-grant", namespace="/dashboard")
async def dashboard_session_request_grant(sid, data):
    """Admin asks the person at the PC for administrator rights (like AnyDesk)."""
    from app import db as _db
    client_id = data.get("clientId")
    if not await _ws_user_may(data.get("username"), "use_screen", client_id):
        return
    await _db.call(_db.add_audit_entry, data.get("username") or "system",
                   "screen.admin_requested", target=client_id, details="")
    await send_to_agent(client_id, "screen-session-request-grant", {})


# --- File transfer in the remote-support session (like AnyDesk) ---
# --- Digital USB stick: files that the agent sends back ----------------
_tempusb_in: dict = {}


@sio.on("temp-usb-status", namespace="/agent")
async def on_tempusb_status(sid, payload):
    """Agent status (attached/detached/error) -> dashboard."""
    await sio.emit("temp-usb-status", payload, namespace="/dashboard")


@sio.on("temp-usb-upload", namespace="/agent")
async def on_tempusb_upload(sid, payload):
    """Agent -> area: a new/changed file in the device folder (in chunks)."""
    import base64 as _b64
    from app import modules as _modules
    _store = _modules.use("temp-usb", "store")   # only if the module is active
    d = payload or {}
    area_id = str(d.get("id") or "")
    name = str(d.get("name") or "")
    if not _store or not area_id or not name or not _store.get(area_id):
        return
    key = f"{area_id}:{sid}:{name}"
    buf = _tempusb_in.get(key, b"")
    try:
        buf += _b64.b64decode(d.get("data") or "")
    except Exception:
        _tempusb_in.pop(key, None)
        return
    if len(buf) > 1024 * 1024 * 1024:     # 1 GB per file - protection against a constant stream
        _tempusb_in.pop(key, None)
        return
    if d.get("last"):
        _tempusb_in.pop(key, None)
        rel, _, fname = name.replace("\\", "/").rpartition("/")
        try:
            await _db_run(_store.write_file, area_id, rel, fname or name, buf)
        except Exception:
            return
    else:
        _tempusb_in[key] = buf


async def _db_run(fn, *a):
    import asyncio as _aio
    return await _aio.get_event_loop().run_in_executor(None, fn, *a)


@sio.on("screen-file-chunk", namespace="/dashboard")
async def dashboard_screen_file_chunk(sid, data):
    """Admin -> user: one chunk of a file (base64, ~256 KB) to the agent."""
    from app import db as _db
    client_id = data.get("clientId")
    if not await _ws_user_may(data.get("username"), "use_screen", client_id):
        await sio.emit("screen-file-ack", {"id": client_id, "tid": data.get("tid"), "seq": data.get("seq"),
                                           "error": "forbidden"}, to=sid, namespace="/dashboard")
        return
    if int(data.get("seq") or 0) == 0:
        await _db.call(_db.add_audit_entry, data.get("username") or "system", "screen.file_sent",
                       target=client_id, details=f"{data.get('name')} ({data.get('size')} Bytes)")
    payload = {k: data.get(k) for k in ("tid", "seq", "name", "size", "data", "last")}
    if not await send_to_agent(client_id, "screen-file-chunk", payload):
        await sio.emit("screen-file-ack", {"id": client_id, "tid": data.get("tid"), "seq": data.get("seq"),
                                           "error": "offline"}, to=sid, namespace="/dashboard")


@sio.on("screen-file-ack", namespace="/agent")
async def on_screen_file_ack(sid, payload):
    await sio.emit("screen-file-ack", payload, namespace="/dashboard")


@sio.on("screen-file-out", namespace="/agent")
async def on_screen_file_out(sid, payload):
    """User -> admin: file from the session window (in chunks) to the dashboard."""
    from app import db as _db
    if int((payload or {}).get("seq") or 0) == 0:
        await _db.call(_db.add_audit_entry, "system", "screen.file_received",
                       target=payload.get("id"), details=f"{payload.get('name')} ({payload.get('size')} Bytes)")
    await sio.emit("screen-file-out", payload, namespace="/dashboard")


@sio.on("screen-session-perms", namespace="/agent")
async def on_session_perms(sid, payload):
    """Permissions the user toggled in the session window -> dashboard."""
    await sio.emit("screen-session-perms", payload, namespace="/dashboard")


@sio.on("screen-session-grant", namespace="/agent")
async def on_session_grant(sid, payload):
    # User allowed/revoked admin rights -> to the dashboard + audit.
    from app import db as _db
    cid = (payload or {}).get("id")
    action = "granted" if payload.get("granted") else ("denied" if payload.get("denied") else "revoked")
    detail = "silent (without user approval)" if payload.get("silent") else "by user"
    await _db.call(_db.add_audit_entry, "system",
                   "screen.admin_" + action, target=cid, details=detail)
    await sio.emit("screen-session-grant", payload, namespace="/dashboard")


@sio.on("screen-session-disconnect", namespace="/agent")
async def on_session_disconnect(sid, payload):
    # User disconnected the remote support -> stop recording + inform the dashboard.
    cid = (payload or {}).get("id")
    recording.stop_recording(cid)
    await _log_session_end(cid, "user")
    await sio.emit("screen-session-disconnect", payload, namespace="/dashboard")


@sio.on("screen-clipboard", namespace="/agent")
async def on_screen_clipboard(sid, payload):
    """Agent delivers the content of the remote clipboard -> to the dashboard."""
    await sio.emit("screen-clipboard", payload, namespace="/dashboard")


@sio.on("screen-config", namespace="/dashboard")
async def dashboard_screen_config(sid, data):
    """Change quality/FPS of the running remote support (e.g. from bytera://.../remotescreen/fps=10)."""
    client_id = data.get("clientId")
    if not client_id:
        return
    if not await _ws_user_may(data.get("username"), "use_screen", client_id):
        return
    out = {}
    if data.get("fps") is not None:
        try: out["fps"] = max(1, min(30, int(data["fps"])))
        except Exception: pass
    if data.get("quality") is not None:
        try: out["quality"] = max(10, min(95, int(data["quality"])))
        except Exception: pass
    if data.get("width") is not None:
        try: out["width"] = max(0, min(3840, int(data["width"])))   # 0 = automatic
        except Exception: pass
    if out:
        await send_to_agent(client_id, "screen-config", out)


@sio.on("screen-set-monitor", namespace="/dashboard")
async def dashboard_screen_set_monitor(sid, data):
    """Forward a screen switch (multi-monitor) to the agent."""
    client_id = data.get("clientId")
    monitor = data.get("monitor")
    if client_id and monitor is not None:
        if not await _ws_user_may(data.get("username"), "use_screen", client_id):
            return
        await send_to_agent(client_id, "screen-set-monitor", {"monitor": int(monitor)})


# --- Multi-user selection: "pick a user first" ---
@sio.on("screen-users", namespace="/dashboard")
async def dashboard_screen_users(sid, data):
    """Dashboard wants to know which users are logged in on the device."""
    from app import db as _db
    client_id = data.get("clientId")
    if not await _ws_user_may(data.get("username"), "use_screen", client_id):
        return
    ok = await send_to_agent(client_id, "screen-users", {})
    if not ok:
        await sio.emit("screen-users", {"id": client_id, "users": [], "multi": False,
                                        "error": "Client is offline."}, namespace="/dashboard")


@sio.on("screen-users", namespace="/agent")
async def on_screen_users(sid, payload):
    await sio.emit("screen-users", payload, namespace="/dashboard")


# --- Virtual display driver (headless, module "headless-screen") ---
def _vdisplay_pkg():
    """URL + SHA256 of the driver package, if the module provided it."""
    try:
        from app import modules as _m
        if _m.is_enabled("headless-screen"):
            return _m.use("headless-screen", "vdriver").package_info()
    except Exception as e:
        print(f"[vdisplay] package info not available: {e}")
    return {"url": "", "sha256": ""}


@sio.on("vdisplay-status", namespace="/dashboard")
async def dashboard_vdisplay_status(sid, data):
    client_id = data.get("clientId")
    if not await _ws_user_may(data.get("username"), "use_screen", client_id):
        return
    await send_to_agent(client_id, "vdisplay-status", {})


@sio.on("vdisplay-install", namespace="/dashboard")
async def dashboard_vdisplay_install(sid, data):
    from app import db as _db
    client_id = data.get("clientId")
    if not await _ws_user_may(data.get("username"), "manage_headless_screen", client_id):
        await sio.emit("vdisplay-result", {"id": client_id, "action": "install", "ok": False,
                       "error": "No permission (manage_headless_screen)."}, namespace="/dashboard")
        return
    pkg = _vdisplay_pkg()
    if not pkg.get("url"):
        await sio.emit("vdisplay-result", {"id": client_id, "action": "install", "ok": False,
                       "error": "No driver package on the server. Enable the module \"Headless screen\"."},
                       namespace="/dashboard")
        return
    await _db.call(_db.add_audit_entry, data.get("username", "?"), "headless.vdisplay_install",
                   target=client_id)
    await _vdisplay_job("install", client_id, data.get("username"), pkg)


@sio.on("vdisplay-remove", namespace="/dashboard")
async def dashboard_vdisplay_remove(sid, data):
    from app import db as _db
    client_id = data.get("clientId")
    if not await _ws_user_may(data.get("username"), "manage_headless_screen", client_id):
        await sio.emit("vdisplay-result", {"id": client_id, "action": "remove", "ok": False,
                       "error": "No permission (manage_headless_screen)."}, namespace="/dashboard")
        return
    await _db.call(_db.add_audit_entry, data.get("username", "?"), "headless.vdisplay_remove",
                   target=client_id)
    await _vdisplay_job("remove", client_id, data.get("username"), {})


# Driver install/remove as a BACKEND JOB (app/bgjobs.py): the backend waits for
# the agent's response - a reload or other users see the same state in the task
# display.
_vdisplay_waiters: dict[str, "asyncio.Future"] = {}


async def _vdisplay_job(action: str, client_id: str, username: str | None, pkg: dict) -> None:
    from app import bgjobs, db as _db
    user = (await _db.call(_db.get_user_by_username, username)) if username else None
    client = (await _db.call(_db.get_client, client_id)) or {}

    async def work(job):
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        _vdisplay_waiters[client_id] = fut
        try:
            job.update(phase=action, detail="order sent to the agent")
            if not await send_to_agent(client_id, f"vdisplay-{action}", pkg):
                raise RuntimeError("Client is offline")
            job.update(detail="agent working (loading/installing driver)")
            res = await asyncio.wait_for(fut, timeout=600)
        except asyncio.TimeoutError:
            raise RuntimeError("no response from the agent within 10 minutes")
        finally:
            if _vdisplay_waiters.get(client_id) is fut:
                _vdisplay_waiters.pop(client_id, None)
        if not res.get("ok", True):
            raise RuntimeError(res.get("error") or "agent reports an error")
        return res

    bgjobs.start("vdisplay." + action, client_id,
                 title=f"{'Install' if action == 'install' else 'Remove'} virtual display: "
                       f"{client.get('hostname') or client_id}",
                 user=user, runner=work, perm="manage_headless_screen", client_id=client_id,
                 meta={"client": client_id})


@sio.on("vdisplay-status", namespace="/agent")
async def on_vdisplay_status(sid, payload):
    await sio.emit("vdisplay-status", payload, namespace="/dashboard")


@sio.on("vdisplay-result", namespace="/agent")
async def on_vdisplay_result(sid, payload):
    fut = _vdisplay_waiters.get((payload or {}).get("id") or "")
    if fut and not fut.done():
        fut.set_result(payload or {})
    await sio.emit("vdisplay-result", payload, namespace="/dashboard")


# ------------------------------------------------------------------
# EXPERIMENTAL RDP GATEWAY (for headless VMs)
# The dashboard asks the backend to connect to the VM itself via RDP and stream
# the frames over the same "screen-frame" channel.
# ------------------------------------------------------------------

async def _rdp_emit_frame(client_id, jpeg_b64, width, height):
    await sio.emit("screen-frame", {
        "id": client_id, "image": jpeg_b64, "width": width, "height": height,
    }, namespace="/dashboard")


async def _rdp_emit_error(client_id, message):
    await sio.emit("screen-error", {"id": client_id, "error": message}, namespace="/dashboard")


@sio.on("rdp-start", namespace="/dashboard")
async def dashboard_rdp_start(sid, data):
    """
    Starts an RDP connection attempt from the backend to the VM. Only if pyrdp
    is available - otherwise the dashboard gets a clear hint about the native
    .rdp way.
    """
    from app import db as _db
    client_id = data.get("clientId")
    client = (await _db.call(_db.get_client, client_id)) if client_id else None
    if not client:
        await _rdp_emit_error(client_id, "Client not found.")
        return

    if not rdp_gateway.is_available():
        await _rdp_emit_error(
            client_id,
            "Embedded RDP (pyrdp) is not installed on the server. "
            "Please use the 'Connect via RDP (native)' button - it opens the "
            "Windows RDP client and works reliably even with headless VMs."
        )
        return

    host = client.get("ip")
    if not host:
        await _rdp_emit_error(client_id, "No IP address known for this client.")
        return

    loop = asyncio.get_event_loop()
    started = rdp_gateway.start_session(
        client_id, host, data.get("port", 3389),
        data.get("username", ""), data.get("password", ""),
        loop, _rdp_emit_frame, _rdp_emit_error,
    )
    if not started:
        await _rdp_emit_error(client_id, "RDP session could not be started.")


@sio.on("rdp-stop", namespace="/dashboard")
async def dashboard_rdp_stop(sid, data):
    client_id = data.get("clientId")
    if client_id:
        rdp_gateway.stop_session(client_id)


_term_sessions_UNUSED = None  # (buffer defined above in the agent section)


# --- Interactive terminal: forward dashboard -> agent ---
@sio.on("term-open", namespace="/dashboard")
async def dashboard_term_open(sid, data):
    """Starts an interactive shell session on the target client."""
    client_id = data.get("clientId")
    shell = data.get("shell", "auto")
    # Login mode: "admin" = direct root/system shell (as before),
    # "user" = the agent starts /bin/login in the PTY (username/password like SSH).
    mode = data.get("mode") if data.get("mode") in ("admin", "user") else "admin"
    session = data.get("session")
    if not await _ws_user_may(data.get("username"), "use_terminal", client_id):
        await sio.emit("term-output", {
            "id": client_id, "session": session,
            "data": "\r\n\x1b[31mNo permission for the terminal of this client.\x1b[0m\r\n",
        }, namespace="/dashboard")
        await sio.emit("term-exit", {"id": client_id, "session": session}, namespace="/dashboard")
        return
    ok = await send_to_agent(client_id, "term-open", {
        "session": session,
        "shell": shell,
        "mode": mode,
        "cols": data.get("cols", 80),
        "rows": data.get("rows", 24),
    })
    # Confirmation to the dashboard: proves that THIS (current) backend
    # processed the event, and reports whether the target agent is online. This
    # lets the frontend distinguish exactly: no ack = backend outdated/not
    # restarted; ack with agent_online=false = client offline; ack but no output
    # = agent outdated (no PTY).
    await sio.emit("term-ack", {"session": session, "agent_online": bool(ok)},
                   namespace="/dashboard")
    if ok:
        # Create the session + start recording: the entire session is recorded
        # as a terminal replay (like the screen recorder), instead of logging
        # commands and output individually. An audit entry with a replay link
        # follows on closing (term-close/exit).
        import time as _t
        from app import db as _db
        rec_id = None
        if await _db.call(_db.get_setting, "recording_enabled", "1") == "1":
            client = await _db.call(_db.get_client, client_id)
            rec_id = recording.start_term_recording(
                session, client_id,
                (client or {}).get("hostname") or client_id,
                data.get("username", "unknown"),
            )
        _term_sessions[session] = {
            "client_id": client_id,
            "username": data.get("username", "unknown"),
            "shell": shell if mode == "admin" else f"{shell} (user login)",
            "mode": mode,
            "started": _t.time(),
            "rec_id": rec_id,
        }
    else:
        await sio.emit("term-output", {
            "id": client_id, "session": session,
            "data": "\r\n\x1b[31mClient is offline.\x1b[0m\r\n",
        }, namespace="/dashboard")
        await sio.emit("term-exit", {"id": client_id, "session": session},
                       namespace="/dashboard")


@sio.on("term-input", namespace="/dashboard")
async def dashboard_term_input(sid, data):
    await send_to_agent(data.get("clientId"), "term-input", {
        "session": data.get("session"), "data": data.get("data", ""),
    })


@sio.on("term-resize", namespace="/dashboard")
async def dashboard_term_resize(sid, data):
    await send_to_agent(data.get("clientId"), "term-resize", {
        "session": data.get("session"),
        "cols": data.get("cols", 80), "rows": data.get("rows", 24),
    })


@sio.on("term-close", namespace="/dashboard")
async def dashboard_term_close(sid, data):
    await send_to_agent(data.get("clientId"), "term-close", {
        "session": data.get("session"),
    })
    await _finalize_term_session(data.get("session"))


async def _finalize_term_session(session: str) -> None:
    """Ends the terminal recording (replay) and writes ONE compact audit entry
    referencing the replay - instead of writing commands/output individually
    into the log."""
    from app import db as _db
    meta = _term_sessions.pop(session, None)
    if not meta:
        return
    import time as _t
    rec_id = recording.stop_term_recording(session)
    dur = int(_t.time() - meta["started"])
    details = f"shell:{meta['shell']} duration:{dur}s"
    if rec_id:
        # Same link as for the screen recording: the audit log can make a
        # "view recording" link from it.
        details = f"rec:{rec_id} " + details
    await _db.call(_db.add_audit_entry, meta["username"], "terminal.session",
                   target=meta["client_id"], details=details)


# ==========================================================================
# BACKEND OUTPUT (live log) - "Source" tab
# --------------------------------------------------------------------------
# Read-only: streams the console output (stdout/stderr) of the running backend
# process (run.py) to subscribed admin dashboards. There is NO input/shell
# anymore - display only. Auth via the admin's JWT.
# ==========================================================================

from app.loghub import hub as _loghub_hub


def _admin_from_token(token: str):
    """Returns the admin user if the token is valid and role=='admin'."""
    try:
        from app.auth import decode_access_token
        payload = decode_access_token(token)
        u = db.get_user_by_id(payload.get("sub"))
        from app.auth import is_super_admin
        if u and is_super_admin(u):
            return u
    except Exception:
        pass
    return None


@sio.on("backend-log-open", namespace="/dashboard")
async def backend_log_open(sid, data):
    """Admin subscribes to the backend output; gets the history first."""
    if not _admin_from_token(data.get("token")):
        await sio.emit("backend-log-history",
                       {"data": "[Not authorized - super admins only]\n"},
                       to=sid, namespace="/dashboard")
        return
    # Remember the event loop (for broadcasts from foreign threads) + register the listener.
    _loghub_hub.set_loop(asyncio.get_event_loop())
    _loghub_hub.subscribers.add(sid)
    await sio.emit("backend-log-history", {"data": _loghub_hub.history()},
                   to=sid, namespace="/dashboard")


@sio.on("backend-log-close", namespace="/dashboard")
async def backend_log_close(sid, data):
    _loghub_hub.subscribers.discard(sid)
