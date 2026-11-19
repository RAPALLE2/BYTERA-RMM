"""
loghub.py
---------
Captures the console output of the RUNNING backend process (what you see when
you start `python run.py`) and makes it available to the dashboard as a live
log (Source tab -> backend output).

Technique: sys.stdout / sys.stderr are replaced by a "tee" that still writes to
the real console AND puts every output into a ring buffer. New lines are pushed
to subscribed dashboard clients (admins).

install() must be called EARLY (in run.py, before uvicorn.run), so that
uvicorn's log output is captured too.
"""

import sys
import asyncio
import threading
import collections

_MAX_CHUNKS = 4000  # bounded ring buffer (last N output chunks)


class LogHub:
    def __init__(self):
        self.chunks = collections.deque(maxlen=_MAX_CHUNKS)
        self.lock = threading.Lock()
        self.subscribers: set[str] = set()   # Socket.IO SIDs of the listeners
        self.loop = None

    def set_loop(self, loop):
        self.loop = loop

    def append(self, text: str):
        if not text:
            return
        # Normalize line breaks to CRLF: many loggers (incl. uvicorn) write
        # only '\n'. In the terminal '\n' only moves the cursor down, without
        # jumping to the start of the line -> "staircase" effect. With '\r\n'
        # every message starts cleanly on a new line at the far left.
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
        with self.lock:
            self.chunks.append(text)
        # Only send if someone is listening and the event loop is known.
        if self.subscribers and self.loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._broadcast(text), self.loop)
            except Exception:
                pass

    async def _broadcast(self, text: str):
        # Lazy import to avoid a circular import with sockets.py.
        from app.sockets import sio
        for sid in list(self.subscribers):
            try:
                await sio.emit("backend-log", {"data": text}, to=sid, namespace="/dashboard")
            except Exception:
                pass

    def history(self) -> str:
        with self.lock:
            return "".join(self.chunks)


hub = LogHub()


class _Tee:
    """Writes to the original stream AND to the LogHub."""

    def __init__(self, orig):
        self._orig = orig

    def write(self, s):
        try:
            if self._orig is not None:
                self._orig.write(s)
        except Exception:
            pass
        try:
            hub.append(s)
        except Exception:
            pass
        return len(s) if s else 0

    def flush(self):
        try:
            if self._orig is not None:
                self._orig.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        # all other attributes (encoding, isatty, fileno, …) to the original.
        return getattr(self._orig, name)


def install():
    """Replaces stdout/stderr with tees (idempotent)."""
    if not isinstance(sys.stdout, _Tee):
        sys.stdout = _Tee(sys.stdout)
    if not isinstance(sys.stderr, _Tee):
        sys.stderr = _Tee(sys.stderr)
