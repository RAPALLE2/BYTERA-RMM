# cli_cmd.py (Modul "console") - CLI-Framework + Kernbefehle (Proxmox-artig)
# ----------------------------------------------------------------------------
# Leichter Argument-Parser (wie argparse, aber fuer die Konsole): jeder Befehl
# bekommt Positionen, Flags (--name wert / --schalter) und automatisch -h/--help.
# Darauf aufbauend: Schnellzugriff (open), Benutzer, Gruppen, Einstellungen.
# Weitere Core-Features lassen sich nach demselben Muster ergaenzen.
from __future__ import annotations

from .commands import command, resolve, need, CommandError, UsageError, REGISTRY
from .shell import BOLD, RST, GREY, GREEN, CYAN, RED, YELLOW

try:
    from app import db
    from app.auth import hash_password, is_super_admin, user_has_permission
except Exception:                       # beim reinen Syntaxcheck
    db = None


# ============================================================ Argument-Parser
class Opt:
    def __init__(self, name, help="", takes=True, multiple=False, default=None, choices=None):
        self.name, self.help, self.takes, self.multiple = name, help, takes, multiple
        self.default, self.choices = default, choices


class Arg:
    """Beschreibt die Argumente eines (Unter-)Befehls und wertet sie aus."""

    def __init__(self, prog, summary, positionals=None, options=None, rest=None):
        self.prog = prog
        self.summary = summary
        self.positionals = positionals or []        # [(name, help), ...]
        self.options = {o.name: o for o in (options or [])}
        self.rest = rest                             # (name, help) fuer "alles weitere", oder None

    def help_text(self) -> str:
        lines = [f"{BOLD}{self.prog}{RST} – {self.summary}", ""]
        usage = self.prog
        for n, _ in self.positionals:
            usage += f" <{n}>"
        if self.rest:
            usage += f" <{self.rest[0]}...>"
        if self.options:
            usage += " [optionen]"
        lines.append(f"{GREY}Aufruf:{RST} {usage}")
        if self.positionals:
            lines.append(f"\n{BOLD}Argumente{RST}")
            for n, h in self.positionals:
                lines.append(f"  {CYAN}{n:<18}{RST} {h}")
        if self.rest:
            lines.append(f"  {CYAN}{self.rest[0]+'...':<18}{RST} {self.rest[1]}")
        if self.options:
            lines.append(f"\n{BOLD}Optionen{RST}")
            for o in self.options.values():
                flag = f"--{o.name}" + (" <wert>" if o.takes else "")
                extra = f"  {GREY}(Auswahl: {', '.join(o.choices)}){RST}" if o.choices else ""
                lines.append(f"  {CYAN}{flag:<22}{RST} {o.help}{extra}")
        lines.append(f"  {CYAN}{'-h, --help':<22}{RST} diese Hilfe")
        return "\n".join(lines)

    def parse(self, con, argv: list[str]):
        if any(a in ("-h", "--help", "-?") for a in argv):
            con.line(self.help_text())
            return None
        pos, opts, i = [], {}, 0
        while i < len(argv):
            a = argv[i]
            # Einstellige Kurz-Flags: -c  ->  Option "c" (falls definiert)
            if len(a) == 2 and a[0] == "-" and a[1].isalpha() and a[1] in self.options and not self.options[a[1]].takes:
                opts[a[1]] = True
                i += 1
                continue
            if a.startswith("--"):
                name = a[2:]
                o = self.options.get(name)
                if not o:
                    raise UsageError(f"Unbekannte Option: --{name}  ({self.prog} -h für Hilfe)")
                if o.takes:
                    if i + 1 >= len(argv):
                        raise UsageError(f"--{name} braucht einen Wert")
                    val = argv[i + 1]
                    if o.choices and val not in o.choices:
                        raise UsageError(f"--{name}: nur {', '.join(o.choices)}")
                    if o.multiple:
                        opts.setdefault(name, []).append(val)
                    else:
                        opts[name] = val
                    i += 2
                else:
                    opts[name] = True
                    i += 1
            else:
                pos.append(a)
                i += 1
        # Positionen zuordnen
        result = {}
        for idx, (n, _) in enumerate(self.positionals):
            if idx >= len(pos):
                raise UsageError(f"Argument <{n}> fehlt  ({self.prog} -h für Hilfe)")
            result[n] = pos[idx]
        extra = pos[len(self.positionals):]
        if self.rest:
            result[self.rest[0]] = extra
        elif extra:
            raise UsageError(f"Zu viele Argumente: {' '.join(extra)}")
        for o in self.options.values():
            result[o.name] = opts.get(o.name, [] if o.multiple else o.default)
        return result


def _need_admin(con):
    u = con.user
    if not (is_super_admin(u) or user_has_permission(u, "manage_users")):
        raise CommandError("Keine Berechtigung (Benutzerverwaltung).")


# ============================================================ open (Schnellzugriff)
_OPEN = Arg(
    "open", "Etwas schnell öffnen – im Dashboard (-d, Standard) oder im Client (-c)",
    positionals=[("ziel", "z. B. client, space, app, function")],
    rest=("rest", "Rest der Adresse, z. B. node-100 remotescreen"),
    options=[
        Opt("dashboard", "im Dashboard öffnen (Standard)", takes=False),
        Opt("client", "in der Client-App öffnen", takes=False),
        Opt("d", "Kurz für --dashboard", takes=False),
        Opt("c", "Kurz für --client", takes=False),
        Opt("screen", "Bildschirm-Nr. (für remotescreen)"),
        Opt("fps", "Bildrate (für remotescreen)"),
    ])


@command("open", "open <ziel> <rest...> [-d|-c]",
         "Schnellzugriff: client node-100 remotescreen, space name, app browser …",
         group="Allgemein", args=(["client", "space", "app", "function", "agent", "node", "dist"],))
async def _open(con, argv):
    r = _OPEN.parse(con, argv)
    if r is None:
        return
    kind = r["ziel"].lower()
    parts = [kind] + list(r["rest"])
    # Parameter aus Optionen anhaengen
    q = []
    if r.get("screen"):
        q.append("screen=" + r["screen"])
    if r.get("fps"):
        q.append("fps=" + r["fps"])
    path = "/".join(parts)
    url = "bytera://" + path + ("/" + "&".join(q) if q else "")
    where = "client" if (r.get("client") or r.get("c")) else "dashboard"
    # Das Dashboard-Terminal erkennt diese OSC-Sequenz und oeffnet den Link
    # (im Dashboard bzw. - mit where=client - in der Client-App).
    con.w(f"\x1b]1337;bytera-open;{where};{url}\x07")
    con.line(f"{GREEN}→ öffne{RST} {url}  {GREY}({'Client-App' if where == 'client' else 'Dashboard'}){RST}")


# ============================================================ user
_USER_CREATE = Arg(
    "user create", "Benutzer anlegen",
    positionals=[("name", "Benutzername")],
    options=[
        Opt("password", "Passwort setzen"),
        Opt("display", "Anzeigename"),
        Opt("email", "E-Mail"),
        Opt("role", "Rolle", choices=["user", "admin"], default="user"),
        Opt("groups", "Gruppe(n) zuweisen (mehrfach)", multiple=True),
        Opt("otp", "Einmal-Login/Passwortwechsel erzwingen", takes=False),
    ])


@command("user", "user <list|create|delete|groups> …",
         "Benutzer verwalten (user create name --password … --groups admin)",
         group="Verwaltung", args=(["list", "create", "delete", "groups"],))
async def _user(con, argv):
    if not argv or argv[0] in ("-h", "--help"):
        con.line(f"{BOLD}user{RST} – Benutzer verwalten")
        con.line(f"  {CYAN}user list{RST}                     alle Benutzer")
        con.line(f"  {CYAN}user create <name> [opt]{RST}      anlegen ({GREY}user create -h{RST})")
        con.line(f"  {CYAN}user delete <name>{RST}            löschen")
        con.line(f"  {CYAN}user groups <name>{RST}            Gruppen eines Benutzers")
        return
    sub = argv[0].lower()
    rest = argv[1:]
    if sub == "list":
        rows = [[u["username"], u.get("display_name") or "", u.get("role") or "user"] for u in db.list_users()]
        from .commands import table
        table(con, rows, ["Name", "Anzeige", "Rolle"]) if rows else con.line(f"{GREY}keine{RST}")
    elif sub == "create":
        r = _USER_CREATE.parse(con, rest)
        if r is None:
            return
        _need_admin(con)
        if any(u["username"].lower() == r["name"].lower() for u in db.list_users()):
            raise CommandError(f"Benutzer '{r['name']}' gibt es schon.")
        pw = r.get("password") or ""
        must_change = bool(r.get("otp")) or not pw
        ph = hash_password(pw or "changeme")
        groups = r.get("groups") or []
        gmap = {g["name"].lower(): g["id"] for g in db.list_groups()}
        for g in groups:
            if g.lower() not in gmap:
                raise CommandError(f"Gruppe '{g}' gibt es nicht.")
        u = db.create_user(r["name"], ph, r.get("display") or r["name"], r.get("role") or "user",
                           must_change, r.get("email"))
        if groups:
            try:
                db.set_user_groups(u["id"], [gmap[g.lower()] for g in groups])
            except Exception:
                pass
        db.add_audit_entry(con.user["username"], "console.user_create", target=u["id"], details=r["name"])
        con.line(f"{GREEN}Benutzer '{r['name']}' angelegt{RST}"
                 + (f"  {GREY}(Passwortwechsel beim ersten Login){RST}" if must_change else "")
                 + (f"  {GREY}Gruppen: {', '.join(groups)}{RST}" if groups else ""))
    elif sub == "delete":
        _need_admin(con)
        if not rest:
            raise UsageError("Name angeben")
        u = next((x for x in db.list_users() if x["username"].lower() == rest[0].lower()), None)
        if not u:
            raise CommandError("Benutzer nicht gefunden.")
        if not await con.confirm(f"Benutzer '{u['username']}' wirklich löschen?"):
            con.line(f"{GREY}abgebrochen{RST}")
            return
        db.delete_user(u["id"])
        db.add_audit_entry(con.user["username"], "console.user_delete", target=u["id"], details=u["username"])
        con.line(f"{GREEN}gelöscht{RST}")
    elif sub == "groups":
        if not rest:
            raise UsageError("Name angeben")
        u = next((x for x in db.list_users() if x["username"].lower() == rest[0].lower()), None)
        if not u:
            raise CommandError("Benutzer nicht gefunden.")
        gids = set(db.get_user_group_ids(u["id"]))
        names = [g["name"] for g in db.list_groups() if g["id"] in gids]
        con.line(f"{u['username']}: " + (", ".join(names) if names else f"{GREY}keine Gruppen{RST}"))
    else:
        raise UsageError(f"Unbekannt: {sub}")


# ============================================================ group
@command("group", "group <list|create|delete> …", "Gruppen verwalten",
         group="Verwaltung", args=(["list", "create", "delete"],))
async def _group(con, argv):
    if not argv or argv[0] in ("-h", "--help"):
        con.line(f"{BOLD}group{RST} – Gruppen verwalten")
        con.line(f"  {CYAN}group list{RST}                 alle Gruppen")
        con.line(f"  {CYAN}group create <name>{RST}        anlegen")
        con.line(f"  {CYAN}group delete <name>{RST}        löschen")
        return
    sub = argv[0].lower()
    rest = argv[1:]
    if sub == "list":
        from .commands import table
        rows = [[g["name"], str(len((g.get("permissions") or "").split(",")) if g.get("permissions") else 0)]
                for g in db.list_groups()]
        table(con, rows, ["Gruppe", "Rechte"]) if rows else con.line(f"{GREY}keine{RST}")
    elif sub == "create":
        _need_admin(con)
        if not rest:
            raise UsageError("Name angeben")
        db.create_group(rest[0], [])
        con.line(f"{GREEN}Gruppe '{rest[0]}' angelegt{RST}")
    elif sub == "delete":
        _need_admin(con)
        if not rest:
            raise UsageError("Name angeben")
        g = next((x for x in db.list_groups() if x["name"].lower() == rest[0].lower()), None)
        if not g:
            raise CommandError("Gruppe nicht gefunden.")
        db.delete_group(g["id"])
        con.line(f"{GREEN}gelöscht{RST}")
    else:
        raise UsageError(f"Unbekannt: {sub}")


# ============================================================ settings
@command("settings", "settings <list|get|set> [schlüssel] [wert]", "Server-Einstellungen",
         group="Verwaltung", args=(["list", "get", "set"],))
async def _settings(con, argv):
    if not argv or argv[0] in ("-h", "--help"):
        con.line(f"{BOLD}settings{RST} – Server-Einstellungen")
        con.line(f"  {CYAN}settings get <schlüssel>{RST}")
        con.line(f"  {CYAN}settings set <schlüssel> <wert>{RST}")
        return
    sub = argv[0].lower()
    if sub == "get":
        if len(argv) < 2:
            raise UsageError("Schlüssel angeben")
        con.line(f"{argv[1]} = {db.get_setting(argv[1]) or GREY + '(leer)' + RST}")
    elif sub == "set":
        if not (is_super_admin(con.user) or user_has_permission(con.user, "see_settings")):
            raise CommandError("Keine Berechtigung.")
        if len(argv) < 3:
            raise UsageError("Schlüssel und Wert angeben")
        db.set_setting(argv[1], " ".join(argv[2:]))
        con.line(f"{GREEN}gesetzt:{RST} {argv[1]} = {' '.join(argv[2:])}")
    else:
        raise UsageError(f"Unbekannt: {sub}")
