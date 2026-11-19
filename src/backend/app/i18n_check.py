#!/usr/bin/env python3
"""
src/backend/app/i18n_check.py
-------------------
Shows where the frontend still has German text hard-coded in the source instead
of going through t() - and whether DE and EN have the same keys.

    python3 src/backend/app/i18n_check.py            # summary
    python3 src/backend/app/i18n_check.py --detail   # with the concrete occurrences
    python3 src/backend/app/i18n_check.py file.js    # only one file

Detection is a heuristic (umlauts + typically German words). It does not find
every case, but it makes progress measurable - without such a number you never
know how far along you are with 700 strings.

IMPORTANT (2026-07): multi-line template literals used to be skipped entirely.
But that is exactly where most of the UI text sits (the innerHTML blocks of the
apps). The reported number was therefore several times too low - 137 instead of
really over 500. Template literals are now read too; t() calls and ${...}
expressions within them are removed first, so already translated spots do not
cause false alarms.
"""
import re
import sys
from pathlib import Path

# This file lives in src/backend/app/ -> src/ = two levels up.
ROOT = Path(__file__).resolve().parents[2] / "frontend" / "js"

DE_HINT = re.compile(
    r"[äöüÄÖÜß]|\b(und|oder|nicht|keine|kein|wird|werden|wurde|nur|noch|schon"
    r"|bitte|Bitte|Datei|Fehler|Einstellungen|Speichern|Abbrechen|Löschen"
    r"|erforderlich|verfügbar|ausgewählt|Benutzer|Aktualisierung|Verbindung)\b")

# Lines that are not UI text anyway.
SKIP_LINE = re.compile(r"^\s*(//|\*|/\*)")


# ${...} and t("...") inside template literals: that is inserted code resp.
# already translated text and must not count as an occurrence.
SUBST = re.compile(r"\$\{[^{}]*\}")
T_CALL = re.compile(r"""t\(\s*["'][^"']*["'][^)]*\)""")


# Internal keys that are deliberately German plain text and only get translated
# at display time by a lookup function - e.g. `group: "Identität"` in the metric
# catalog, which groupLabel() resolves. Such lines are NOT occurrences,
# otherwise the counter never reaches 0 and one gets used to a remainder in
# which real findings get lost.
# Also in object literals that start on one line:
#   { id: "c.ipAddr", group: "Identität", ... }
# Hence an optional "{ " at the start and detection of `group:` anywhere in the
# line.
INTERNAL_KEY = re.compile(
    r'^\s*\{?\s*(?:group|id|scope)\s*:\s*["\']'
    r'|[,{]\s*(?:group|scope)\s*:\s*["\']'
)


def strings_in(src: str):
    """
    All string literals: single quotes, double quotes AND template literals
    (also multi-line). Template literals are checked line by line, after ${...}
    and t(...) have been removed.
    """
    for m in re.finditer(r'"([^"\n]{4,200})"|\'([^\'\n]{4,200})\'', src):
        yield m.start(), (m.group(1) or m.group(2) or "")

    # Template literals one by one, so the line number is correct.
    for m in re.finditer(r"`([^`\\]*(?:\\.[^`\\]*)*)`", src, re.S):
        body = m.group(1)
        offset = m.start(1)
        pos = 0
        in_comment = False      # state across line boundaries
        for raw in body.split("\n"):
            # Multi-line HTML comments: the middle lines contain neither "<!--"
            # nor "-->", so a pure per-line check is not enough. Without this
            # state tracking, explanatory comments (e.g. in audioplayer.js) would
            # stay as occurrences permanently.
            if in_comment:
                if "-->" in raw:
                    in_comment = False
                    raw = raw.split("-->", 1)[1]
                else:
                    pos += len(raw) + 1
                    continue
            if "<!--" in raw and "-->" not in raw.split("<!--", 1)[1]:
                in_comment = True
                raw = raw.split("<!--", 1)[0]
            cleaned = T_CALL.sub("", SUBST.sub("", raw))
            # HTML comments are not UI text - but they appeared as occurrences
            # in the list and kept the counter from ever reaching 0 (e.g. the
            # explanatory comment in tickets.js).
            cleaned = re.sub(r"<!--.*?-->", " ", cleaned, flags=re.S)
            if "<!--" in cleaned or "-->" in cleaned:
                continue        # multi-line comment
            # Drop HTML attributes and tags, this is about visible text.
            cleaned = re.sub(r"<[^>]*>", " ", cleaned)
            cleaned = cleaned.strip()
            if len(cleaned) >= 4:
                yield offset + pos, cleaned[:200]
            pos += len(raw) + 1



import json as _json

def _lang_dirs():
    base = ROOT.parent  # .../src
    dirs = [ROOT.parent.parent / "backend" / "data" / "lang"] if False else []
    # ROOT = .../frontend/js ; we need the src root
    src_root = ROOT.parents[1]  # .../src
    out = [src_root / "backend" / "data" / "lang"]
    for area in ("backend", "frontend", "node"):
        b = src_root / "modules" / area
        if b.is_dir():
            for mod in sorted(b.iterdir()):
                ld = mod / "lang"
                if ld.is_dir():
                    out.append(ld)
    return out


def _keys_for(short_lang):
    """All keys of a language (de/en) from all lang folders (merged)."""
    # File name starts with the short code: de_* / en_*
    keys = set()
    for d in _lang_dirs():
        for f in d.glob(f"{short_lang}_*.json"):
            try:
                obj = _json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            keys |= {k for k in obj.keys() if not k.startswith("_")}
    return keys


def check_keys() -> None:
    de = _keys_for("de")
    en = _keys_for("en")
    only_de = sorted(de - en)
    only_en = sorted(en - de)
    print(f"Keys: de={len(de)} en={len(en)}")
    print(f"  only in DE: {only_de or 'none'}")
    print(f"  only in EN: {only_en or 'none'}")


# Calls of t("…") resp. tr("…"). The (?<![\w.]) is important: without this
# safeguard the expression also matched classList.toggle("active") and reported
# loads of CSS classes as missing keys.
T_KEY = re.compile(r'(?<![\w.])(?:t|tr)\(\s*"([a-zA-Z_][\w]*)"')


def check_used_keys() -> None:
    """
    Finds keys that are used in the code but missing from the language pack.
    This is the single most important safeguard: on an unknown key t() silently
    falls back to the key name - the UI then shows e.g. "set_db_hint" instead of
    a sentence, and nobody notices until a user reports it.
    """
    keys = _keys_for("de")
    used: dict = {}
    for f in sorted(ROOT.rglob("*.js")):
        if f.name.startswith("i18n") or "vendor" in f.parts:
            continue
        for mm in T_KEY.finditer(f.read_text(encoding="utf-8")):
            used.setdefault(mm.group(1), set()).add(f.relative_to(ROOT))
    missing = {k: v for k, v in used.items() if k not in keys}
    print(f"Keys used: {len(used)}   without entry: {len(missing) or 'none'}")
    for k, files in sorted(missing.items()):
        print(f"    {k} -> {', '.join(sorted(str(x) for x in files))}")
    unused = sorted(keys - set(used))
    print(f"  Never used: {len(unused)}")


# code="…" in src/agent/main.py: the agent sends translation keys instead of
# finished sentences to the dashboard (see _notify_screen_error). If such a key
# is missing from the language pack, t() silently falls back to the key name -
# the dashboard would then literally show "agent_wayland". Otherwise this only
# shows up in operation, and exactly in the error situation one is investigating.
# Both as an argument    code="agent_x"
# and in the dict        "code": "agent_x"
# The dict form is used when building the payload directly and would otherwise
# have slipped through the check.
AGENT_CODE = re.compile(r'(?:code=|"code"\s*:\s*)["\']([a-z0-9_]+)["\']')


def check_agent_codes() -> None:
    # ROOT points to src/frontend/js -> src/ is two levels up.
    agent = ROOT.parents[1] / "agent" / "main.py"
    if not agent.is_file():
        print("  src/agent/main.py not found - skipped.")
        return
    src = (ROOT / "app" / "i18n.js").read_text(encoding="utf-8")
    m = re.search(r"\n  de: \{\n(.*?)\n  \},?\n", src, re.S)
    keys = set(re.findall(r"(?:^|,)\s*([a-zA-Z_][\w]*)\s*:", m.group(1), re.M)) if m else set()
    used = set(AGENT_CODE.findall(agent.read_text(encoding="utf-8")))
    missing = sorted(used - keys)
    print(f"Agent codes: {len(used)}   without entry: {len(missing) or 'none'}")
    for k in missing:
        print(f"    {k}")


def check_backend_and_agent_dicts() -> None:
    """
    Check parity of the two ADDITIONAL dictionaries:
      - backend/app/i18n.py  -> TEXTS (server messages, logs, installer)
      - src/agent/main.py   -> _AGENT_TEXTS (dialogs on the device + agent logs)
    Both are separate from the frontend language pack because they have other
    recipients (see the header of backend/app/i18n.py). Without this check a
    missing translation only shows up on the target machine.
    """
    root = ROOT.parents[1]
    for label, path, var in (
        ("Backend", root / "backend" / "app" / "i18n.py", "TEXTS"),
        ("Agent", root / "agent" / "main.py", "_AGENT_TEXTS"),
    ):
        if not path.is_file():
            print(f"  {label}: {path.name} not found - skipped.")
            continue
        src = path.read_text(encoding="utf-8")
        m = re.search(var + r"[^=]*= \{.*?\n\}\n", src, re.S)
        if not m:
            print(f"  {label}: {var} not found.")
            continue
        ns: dict = {}
        try:
            exec(var + " = " + m.group(0).split("=", 1)[1], ns)  # noqa: S102
            table = ns[var]
        except Exception as exc:
            print(f"  {label}: {var} not evaluable ({exc}).")
            continue
        de, en = set(table.get("de", {})), set(table.get("en", {}))
        only_de, only_en = sorted(de - en), sorted(en - de)
        print(f"{label}: de={len(de)} en={len(en)}")
        print(f"    only in DE: {', '.join(only_de) if only_de else 'none'}")
        print(f"    only in EN: {', '.join(only_en) if only_en else 'none'}")


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    detail = "--detail" in sys.argv

    print("=== Key parity ===")
    check_keys()

    print("\n=== Keys in the code ===")
    check_used_keys()

    print("\n=== Agent translation codes ===")
    check_agent_codes()

    print("\n=== Backend and agent dictionaries ===")
    check_backend_and_agent_dicts()

    files = [ROOT / a for a in args] if args else sorted(ROOT.rglob("*.js"))
    print("\n=== Hard-coded German text ===")
    total = 0
    rows = []
    for f in files:
        if f.name == "i18n.js":
            continue
        src = f.read_text(encoding="utf-8")
        hits = []
        for pos, s in strings_in(src):
            line_start = src.rfind("\n", 0, pos) + 1
            line = src[line_start:src.find("\n", pos)]
            if SKIP_LINE.match(line):
                continue
            if INTERNAL_KEY.match(line):
                continue
            # If the text appears as an OBJECT KEY ("Identität": "mg_..."), it
            # is not UI text but the left-hand side of a mapping to a
            # translation key - as built e.g. by the keys table in groupLabel().
            if re.search(re.escape(s) + r'["\']\s*:', line):
                continue
            # A comment at the end of the line (// ...) is not UI text.
            cpos = line.find("//")
            if cpos != -1 and (pos - line_start) > cpos:
                continue
            if s.startswith(("http", "data:", "/api/")):
                continue
            if DE_HINT.search(s):
                hits.append((src.count("\n", 0, pos) + 1, s[:70]))
        if hits:
            rows.append((len(hits), f.relative_to(ROOT), hits))
            total += len(hits)

    for n, rel, hits in sorted(rows, reverse=True):
        print(f"{n:5}  {rel}")
        if detail:
            for ln, s in hits:
                print(f"         {ln:5}: {s}")
    print(f"\nFiles: {len(rows)}   occurrences: {total}")


if __name__ == "__main__":
    main()
