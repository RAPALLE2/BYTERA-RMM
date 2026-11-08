# cli_extra.py (Modul "console") - Gruppierung + Navigation + Spaces + Editor
# ----------------------------------------------------------------------------
# Ergaenzt die Konsole um:
#   * Gruppen in der Hilfe (GROUP-Zuordnung der bestehenden Befehle)
#   * zustandsbehaftete Navigation im Relay-Explorer: cd / pwd, ls/cat nutzen das
#   * Spaces im Terminal: spaces ls | cd | cat | tree
#   * edit <gerät> <datei> : einfacher Vollbild-Texteditor (wie nano) ueber das Relay
# Alles baut auf dem vorhandenen Befehls- und App-Gerüst auf (commands.py, shell.py).
from __future__ import annotations

import base64

from app import db
from .commands import (command, resolve, need, need_online, table, REGISTRY,
                       CommandError, UsageError)
from .shell import ESC, BOLD, RST, GREY, GREEN, CYAN, BLUE, RED, YELLOW

# ---------------------------------------------------------------- Relay-Navigation
def _cwd(con, client_id: str) -> str:
    return getattr(con, "_relay_cwd", {}).get(client_id, "")


def _set_cwd(con, client_id: str, path: str) -> None:
    if not hasattr(con, "_relay_cwd"):
        con._relay_cwd = {}
    con._relay_cwd[client_id] = path


def _join(cwd: str, rel: str) -> str:
    rel = (rel or "").replace("\\", "/")
    if rel.startswith("/") or (len(rel) > 1 and rel[1] == ":"):   # absolut (unix/windows)
        base = rel
    else:
        base = (cwd.rstrip("/") + "/" + rel) if cwd else rel
    # . und .. aufloesen
    parts = []
    for seg in base.replace("\\", "/").split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts and parts[-1] not in ("", ".."):
                parts.pop()
            continue
        parts.append(seg)
    out = "/".join(parts)
    if base[:1] == "/" and not out.startswith("/"):
        out = "/" + out
    return out


@command("pwd", "pwd <gerät>", "Aktuellen Ordner im Relay-Explorer zeigen", args=("client",))
async def _pwd(con, argv):
    c = resolve(con, argv[0] if argv else None)
    con.line(_cwd(con, c["id"]) or "/")


@command("cd", "cd <gerät> <pfad>", "In einen Ordner auf dem Gerät wechseln (.. = zurück)", args=("client",))
async def _cd(con, argv):
    if len(argv) < 2:
        raise UsageError("Gerät und Pfad angeben (z. B. cd pc1 /etc)")
    c = resolve(con, argv[0])
    need(con, c, "c_explorer_view")
    need_online(c)
    from app.sockets import request_fs_list
    target = _join(_cwd(con, c["id"]), argv[1])
    await request_fs_list(c["id"], target)   # wirft, wenn es den Ordner nicht gibt
    _set_cwd(con, c["id"], target)
    con.line(f"{GREY}{target or '/'}{RST}")


async def _ls_cwd(con, argv):
    c = resolve(con, argv[0] if argv else None)
    need(con, c, "c_explorer_view")
    need_online(c)
    from app.sockets import request_fs_list
    path = _join(_cwd(con, c["id"]), argv[1]) if len(argv) > 1 else _cwd(con, c["id"])
    entries = await request_fs_list(c["id"], path)
    rows = [[f"{BLUE}{e.get('name')}/{RST}" if (e.get("is_dir") or e.get("isDir") or e.get("type") == "dir") else str(e.get("name")),
             "" if (e.get("is_dir") or e.get("isDir")) else _fmt_b(e.get("size"))] for e in entries]
    table(con, rows, ["Name", "Größe"]) if rows else con.line(f"{GREY}(leer){RST}")


async def _cat_cwd(con, argv):
    if len(argv) < 2:
        raise UsageError("Gerät und Datei angeben")
    c = resolve(con, argv[0])
    need(con, c, "c_explorer_view")
    need_online(c)
    from app.sockets import request_fs_read
    res = await request_fs_read(c["id"], _join(_cwd(con, c["id"]), argv[1]))
    if res.get("error"):
        raise CommandError(res["error"])
    raw = base64.b64decode(res.get("data") or res.get("content") or "")
    if len(raw) > 262144:
        raise CommandError("Datei zu groß für die Anzeige (max. 256 KB)")
    con.line(raw.decode("utf-8", errors="replace").rstrip())


def _fmt_b(b):
    try:
        b = float(b or 0)
    except Exception:
        return ""
    for u in ("B", "K", "M", "G", "T"):
        if b < 1024:
            return f"{b:.0f}{u}" if u == "B" else f"{b:.1f}{u}"
        b /= 1024
    return f"{b:.1f}P"


# ls/cat so umbiegen, dass sie den aktuellen Ordner (cd) beruecksichtigen.
REGISTRY["ls"].fn = _ls_cwd
REGISTRY["cat"].fn = _cat_cwd


# ---------------------------------------------------------------- Spaces im Terminal
def _spaces_vfs():
    # Spaces-Modul ueber den Modul-Lader holen (pfadunabhaengig, nur wenn aktiv).
    from app import modules as _m
    vfs = _m.use("spaces", "vfs")
    store = _m.use("spaces", "store")
    if not vfs or not store:
        raise CommandError("Spaces-Modul ist nicht aktiv")
    return vfs, store


@command("spaces", "spaces ls [pfad] | cd <pfad> | cat <datei> | tree [pfad]",
         "Spaces im Terminal durchstöbern (Teams/<n>, Öffentlich/<n>, „Mein Space“)",
         module="spaces", group="Spaces",
         args=(["ls", "cd", "cat", "tree"],))
async def _spaces(con, argv):
    try:
        vfs, _store = _spaces_vfs()
    except Exception as e:
        raise CommandError(f"Spaces-Modul nicht verfügbar: {e}")
    sub = (argv[0] if argv else "ls").lower()
    cwd = getattr(con, "_spaces_cwd", "")

    def parts(path):
        return [p for p in (path or "").replace("\\", "/").split("/") if p]

    if sub == "cd":
        if len(argv) < 2:
            raise UsageError("Pfad angeben (z. B. spaces cd Teams/Projekt)")
        target = _join(cwd, argv[1])
        vfs.listdir(con.user, parts(target))     # wirft, wenn unzulaessig/leer-unbekannt
        con._spaces_cwd = target
        con.line(f"{GREY}{target or '/'}{RST}")
        return
    if sub == "ls":
        path = _join(cwd, argv[1]) if len(argv) > 1 else cwd
        entries = vfs.listdir(con.user, parts(path))
        rows = [[f"{BLUE}{e.get('name')}/{RST}" if e.get("is_dir") else str(e.get("name")),
                 "" if e.get("is_dir") else _fmt_b(e.get("size"))] for e in entries]
        table(con, rows, ["Name", "Größe"]) if rows else con.line(f"{GREY}(leer){RST}")
        return
    if sub == "cat":
        if len(argv) < 2:
            raise UsageError("Datei angeben")
        p = vfs.file_path(con.user, parts(_join(cwd, argv[1])))
        raw = p.read_bytes()
        if len(raw) > 262144:
            raise CommandError("Datei zu groß (max. 256 KB)")
        con.line(raw.decode("utf-8", errors="replace").rstrip())
        return
    if sub == "tree":
        path = _join(cwd, argv[1]) if len(argv) > 1 else cwd

        def walk(rel, depth):
            if depth > 4:
                return
            try:
                for e in vfs.listdir(con.user, parts(rel)):
                    con.line("  " * depth + (f"{BLUE}{e['name']}/{RST}" if e.get("is_dir") else e["name"]))
                    if e.get("is_dir"):
                        walk(rel + "/" + e["name"], depth + 1)
            except Exception:
                pass
        walk(path, 0)
        return
    raise UsageError(f"Unbekannt: {sub}")


# ---------------------------------------------------------------- Editor (Vollbild)
class _Editor:
    """Einfacher Vollbild-Texteditor (wie ein kleines nano) fuer das Terminal.
    Pfeiltasten, Eingabe, Enter, Backspace/Entf, Pos1/Ende; Strg+S speichert,
    Strg+Q/Esc beendet."""

    def __init__(self, con, title, text, on_save):
        self.con, self.title, self.on_save = con, title, on_save
        self.lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n") or [""]
        self.cy = self.cx = self.top = 0
        self.running = True
        self.dirty = False
        self.msg = "Strg+S speichern · Strg+Q beenden"

    def stop(self):
        self.running = False

    def resize(self):
        self.paint()

    def _clamp(self):
        self.cy = max(0, min(self.cy, len(self.lines) - 1))
        self.cx = max(0, min(self.cx, len(self.lines[self.cy])))
        h = max(1, self.con.rows - 2)
        if self.cy < self.top:
            self.top = self.cy
        elif self.cy >= self.top + h:
            self.top = self.cy - h + 1

    def key(self, k):
        ln = self.lines[self.cy]
        if k in ("\x11",):                       # Strg+Q
            self.running = False
        elif k in ("\x13",):                     # Strg+S
            try:
                self.on_save("\n".join(self.lines))
                self.dirty = False
                self.msg = f"{GREEN}Gespeichert.{RST}  Strg+S speichern · Strg+Q beenden"
            except Exception as e:
                self.msg = f"{RED}Speichern fehlgeschlagen: {e}{RST}"
        elif k == "up":
            self.cy -= 1
        elif k == "down":
            self.cy += 1
        elif k == "left":
            if self.cx > 0:
                self.cx -= 1
            elif self.cy > 0:
                self.cy -= 1
                self.cx = len(self.lines[self.cy])
        elif k == "right":
            if self.cx < len(ln):
                self.cx += 1
            elif self.cy < len(self.lines) - 1:
                self.cy += 1
                self.cx = 0
        elif k == "home":
            self.cx = 0
        elif k == "end":
            self.cx = len(ln)
        elif k in ("pgup",):
            self.cy -= max(1, self.con.rows - 3)
        elif k in ("pgdn",):
            self.cy += max(1, self.con.rows - 3)
        elif k in ("\r", "\n"):
            rest = ln[self.cx:]
            self.lines[self.cy] = ln[:self.cx]
            self.lines.insert(self.cy + 1, rest)
            self.cy += 1
            self.cx = 0
            self.dirty = True
        elif k in ("\x7f", "\x08"):              # Backspace
            if self.cx > 0:
                self.lines[self.cy] = ln[:self.cx - 1] + ln[self.cx:]
                self.cx -= 1
                self.dirty = True
            elif self.cy > 0:
                prev = self.lines[self.cy - 1]
                self.cx = len(prev)
                self.lines[self.cy - 1] = prev + ln
                del self.lines[self.cy]
                self.cy -= 1
                self.dirty = True
        elif k == "delete":
            if self.cx < len(ln):
                self.lines[self.cy] = ln[:self.cx] + ln[self.cx + 1:]
                self.dirty = True
            elif self.cy < len(self.lines) - 1:
                self.lines[self.cy] = ln + self.lines[self.cy + 1]
                del self.lines[self.cy + 1]
                self.dirty = True
        elif k == "\t":
            self.lines[self.cy] = ln[:self.cx] + "    " + ln[self.cx:]
            self.cx += 4
            self.dirty = True
        elif len(k) == 1 and (k >= " "):          # druckbares Zeichen
            self.lines[self.cy] = ln[:self.cx] + k + ln[self.cx:]
            self.cx += 1
            self.dirty = True
        self._clamp()
        self.paint()

    def paint(self):
        self._clamp()
        h = max(1, self.con.rows - 2)
        w = self.con.cols
        out = [f"{ESC}[H{ESC}[2J"]
        star = "*" if self.dirty else ""
        head = f"{BOLD} {self.title}{star} {RST}"[:w + 20]
        out.append(head + f"{ESC}[K\r\n")
        for i in range(h):
            idx = self.top + i
            if idx < len(self.lines):
                out.append(self.lines[idx][:w].replace("\t", "    ") + f"{ESC}[K\r\n")
            else:
                out.append(f"{GREY}~{RST}{ESC}[K\r\n")
        out.append(f"{ESC}[7m {self.msg} {ESC}[0m{ESC}[K")
        # Cursor positionieren (Zeile 2..., Spalte 1-basiert)
        out.append(f"{ESC}[{(self.cy - self.top) + 2};{self.cx + 1}H")
        self.con.w("".join(out))

    async def run(self):
        self.con.w(f"{ESC}[?25h")
        self.paint()
        import asyncio
        while self.running:
            await asyncio.sleep(0.05)


@command("edit", "edit <gerät> <datei>", "Textdatei auf dem Gerät im Editor bearbeiten",
         args=("client",), group="Dateien (Relay)")
async def _edit(con, argv):
    if len(argv) < 2:
        raise UsageError("Gerät und Datei angeben")
    c = resolve(con, argv[0])
    need(con, c, "c_explorer_view")
    need_online(c)
    from app.sockets import request_fs_read, request_fs_op
    path = _join(_cwd(con, c["id"]), argv[1])
    res = await request_fs_read(c["id"], path)
    if res.get("error"):
        raise CommandError(res["error"])
    raw = base64.b64decode(res.get("data") or res.get("content") or "")
    if len(raw) > 262144:
        raise CommandError("Datei zu groß zum Bearbeiten (max. 256 KB)")
    text = raw.decode("utf-8", errors="replace")

    saved = {"ok": False}

    def on_save(new_text):
        # key() laeuft synchron im Event-Loop (vom awaitenden feed() aufgerufen),
        # deshalb den Schreibvorgang als Task starten.
        import asyncio
        data = base64.b64encode(new_text.encode("utf-8")).decode("ascii")
        asyncio.get_event_loop().create_task(
            request_fs_op(c["id"], "fs-write", {"path": path, "data": data}))
        saved["ok"] = True
        db.add_audit_entry(con.user["username"], "console.edit", target=c["id"], details=path)

    await con.run_app(_Editor(con, f"{c['hostname']}: {argv[1]}", text, on_save))
    con.line(f"{GREEN}Editor beendet.{RST}" if saved["ok"] else f"{GREY}Editor beendet (nicht gespeichert).{RST}")


# ---------------------------------------------------------------- Gruppen zuordnen (ganz am Ende,
# damit auch die oben neu definierten Befehle cd/pwd/edit/spaces schon registriert sind).
_GROUPS = {
    "Allgemein": ["help", "clear", "exit", "whoami", "modules", "audit"],
    "Geräte": ["clients", "info", "top", "shell", "exec", "ps", "kill", "reboot", "shutdown", "agent"],
    "Dateien (Relay)": ["ls", "cat", "cd", "pwd", "edit"],
    "Spaces": ["spaces"],
    "Docker": ["docker"],
    "Netzwerk": ["net", "pve"],
    "Verwaltung": ["api", "user", "group", "module", "settings", "storage", "web"],
}
for _grp, _names in _GROUPS.items():
    for _n in _names:
        if _n in REGISTRY:
            REGISTRY[_n].group = _grp
