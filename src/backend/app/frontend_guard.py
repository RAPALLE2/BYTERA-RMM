"""
frontend_guard.py - Schutz gegen "does not provide an export named ..."
=======================================================================

Problem: Das Dashboard besteht aus vielen ES-Modulen. Importiert EINE Datei
eine Funktion, die es in der Zieldatei (noch/nicht mehr) gibt, verweigert der
Browser den GESAMTEN Modulbaum - die Seite bleibt leer, nicht einmal die
Anmeldung funktioniert, und damit auch kein Update/Hotfix ueber die Oberflaeche.
Typische Ausloeser: Dateien aus verschiedenen Staenden gemischt (Hotfix nur
teilweise eingespielt), Browser-/Proxy-Cache mit alten Dateien.

Loesung (zusammen mit dem sicheren Lader js/boot.js im Browser):
  * Der Server liest alle Frontend-Module (src/frontend/js/** und die
    Frontend-Ordner der Module) und gleicht ab, welche Namen importiert, aber
    in der Zieldatei nicht exportiert werden.
  * Beim AUSLIEFERN einer solchen Zieldatei wird fuer jeden fehlenden Namen ein
    Platzhalter angehaengt. Der Modulbaum laesst sich dadurch wieder laden;
    nur wer die fehlende Funktion wirklich aufruft, bekommt eine Meldung
    (window.__rmmMissingExport) statt einer leeren Seite.
      - Gibt es den Namen in der Datei schon auf oberster Ebene (nur das
        "export" fehlt), wird er einfach mit exportiert.
      - Sonst ein Platzhalter unter eigenem, kollisionsfreiem Namen.
  * Die Dateien auf der Platte werden NICHT veraendert.
  * GET /api/frontend/health (ohne Anmeldung, nur Dateinamen) meldet, was fehlt.

Die Pruefung ist bewusst einfach (regulaere Ausdruecke auf import/export am
Zeilenanfang) und wird bei Dateiaenderungen automatisch neu berechnet.
"""

import json
import os
import re
import threading
import time
from pathlib import Path

from app.paths import FRONTEND_DIR, MODULES_DIR

_lock = threading.Lock()
_cache: dict = {"sig": None, "patches": {}, "missing": [], "files": [], "at": 0, "checked": 0.0}
_RECHECK_SEC = 2.0
_logged: set = set()

# ---------------------------------------------------------------- Dateien
def _iter_files():
    """(URL, Pfad) aller ausgelieferten JS-Module."""
    js = FRONTEND_DIR / "js"
    if js.is_dir():
        for p in js.rglob("*.js"):
            if "vendor" in p.parts:
                continue
            yield "/" + p.relative_to(FRONTEND_DIR).as_posix(), p
    if MODULES_DIR.is_dir():
        for mod in MODULES_DIR.iterdir():
            fe = mod / "frontend"
            if fe.is_dir():
                for p in fe.rglob("*.js"):
                    yield f"/modules/frontend/{mod.name}/" + p.relative_to(fe).as_posix(), p


def _resolve(spec: str, from_url: str) -> str | None:
    spec = spec.split("?")[0].split("#")[0]
    if spec.startswith(("http:", "https:", "data:")) or not spec:
        return None
    if spec.startswith("/"):
        url = spec
    elif spec.startswith("."):
        base = from_url.rsplit("/", 1)[0]
        parts = (base + "/" + spec).split("/")
        out: list[str] = []
        for part in parts:
            if part in ("", "."):
                continue
            if part == "..":
                if out:
                    out.pop()
            else:
                out.append(part)
        url = "/" + "/".join(out)
    else:
        return None            # "bare" Bezeichner (npm) - gibt es hier nicht
    return url


# ---------------------------------------------------------------- Parsen
_NAME = r"[A-Za-z_$][\w$]*"
_RE_EXPORT_DECL = re.compile(
    rf"^export\s+(?:default\s+)?(?:async\s+)?(?:function\s*\*?|class|const|let|var)\s+({_NAME})", re.M)
_RE_EXPORT_DEFAULT = re.compile(r"^export\s+default\b", re.M)
_RE_EXPORT_DESTRUCT = re.compile(r"^export\s+(?:const|let|var)\s*[{\[]([^=]*)[}\]]\s*=", re.M)
_RE_TOP_DECL = r"^(?:async\s+)?(?:function\s*\*?|class|const|let|var)\s+{name}\b"


def _statements(src: str) -> list[str]:
    """import-/export-Anweisungen am Zeilenanfang (auch mehrzeilig), ausserhalb
    von Block-Kommentaren."""
    out, buf, in_comment = [], None, False
    for line in src.splitlines():
        s = line.strip()
        if in_comment:
            if "*/" in s:
                in_comment = False
            continue
        if buf is None:
            if s.startswith("/*") and "*/" not in s:
                in_comment = True
                continue
            if line.startswith(("import ", "import{", "export ", "export{", "export*")):
                buf = line
                if _complete(buf):
                    out.append(buf); buf = None
        else:
            buf += "\n" + line
            if _complete(buf) or len(buf) > 4000:
                out.append(buf); buf = None
    return out


def _complete(stmt: str) -> bool:
    s = re.sub(r"//[^\n]*", "", stmt).strip()
    if s.startswith("import") and not s.startswith("import("):
        return bool(re.search(r"""from\s*["'][^"']+["']|^import\s*["'][^"']+["']""", s)) or s.endswith(";")
    if re.match(r"export\s*\{", s):
        if "}" not in s:
            return False
        rest = s[s.index("}") + 1:].strip()
        return rest in ("", ";") or bool(re.search(r"""from\s*["'][^"']+["']""", rest))
    if re.match(r"export\s*\*", s):
        return bool(re.search(r"""from\s*["'][^"']+["']""", s)) or s.endswith(";")
    return True


def _names_in_braces(txt: str, take_alias: bool) -> list[str]:
    """{ a, b as c, default as d } -> importiert: [a, b, default] / exportiert: [a, c, d]"""
    out = []
    for part in txt.split(","):
        part = re.sub(r"//[^\n]*|/\*.*?\*/", "", part, flags=re.S).strip()
        if not part:
            continue
        m = re.match(rf"(?:type\s+)?({_NAME}|\"[^\"]+\"|'[^']+')(?:\s+as\s+({_NAME}))?$", part)
        if not m:
            continue
        orig, alias = m.group(1).strip("\"'"), m.group(2)
        out.append(alias if (take_alias and alias) else orig)
    return out


def _parse(src: str, url: str) -> dict:
    exports: set[str] = set()
    star_from: list[str] = []
    imports: list[tuple[str, list[str]]] = []      # (Ziel-URL, Namen)
    for m in _RE_EXPORT_DECL.finditer(src):
        exports.add(m.group(1))
    if _RE_EXPORT_DEFAULT.search(src):
        exports.add("default")
    for m in _RE_EXPORT_DESTRUCT.finditer(src):
        for n in re.findall(rf"(?:{_NAME}\s*:\s*)?({_NAME})", m.group(1)):
            exports.add(n)
    for st in _statements(src):
        s = re.sub(r"//[^\n]*", "", st)
        fm = re.search(r"""from\s*["']([^"']+)["']""", s)
        target = _resolve(fm.group(1), url) if fm else None
        if s.startswith("export"):
            bm = re.search(r"\{([^}]*)\}", s)
            if re.match(r"export\s*\*\s*as\s+(" + _NAME + ")", s):
                exports.add(re.match(r"export\s*\*\s*as\s+(" + _NAME + ")", s).group(1))
            elif re.match(r"export\s*\*", s):
                if target:
                    star_from.append(target)
            elif bm:
                exports.update(_names_in_braces(bm.group(1), take_alias=True))
                if target:
                    imports.append((target, _names_in_braces(bm.group(1), take_alias=False)))
            continue
        if not s.startswith("import") or not target:
            continue
        head = s[len("import"):s.rfind("from")] if fm else ""
        names = []
        bm = re.search(r"\{([^}]*)\}", head)
        if bm:
            names += _names_in_braces(bm.group(1), take_alias=False)
        # Default-Import: "import X from" / "import X, { a } from" / "import X, * as ns from"
        rest = re.sub(r"\{[^}]*\}", "", head)
        rest = re.sub(r"\*\s*as\s+" + _NAME, "", rest)   # Namensraum-Import: kein Linkfehler
        rest = rest.replace(",", " ").strip()
        if re.fullmatch(_NAME, rest):
            names.append("default")
        if names:
            imports.append((target, names))
    return {"exports": exports, "star_from": star_from, "imports": imports}


# ---------------------------------------------------------------- Analyse
def _signature(files: list[tuple[str, Path]]) -> tuple:
    sig = []
    for url, p in files:
        try:
            st = p.stat()
            sig.append((url, st.st_mtime_ns, st.st_size))
        except OSError:
            pass
    return tuple(sorted(sig))


def _analyse() -> None:
    files = list(_iter_files())
    sig = _signature(files)
    if sig == _cache["sig"]:
        return
    parsed: dict[str, dict] = {}
    sources: dict[str, str] = {}
    paths = dict(files)
    for url, p in files:
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        sources[url] = src
        try:
            parsed[url] = _parse(src, url)
        except Exception as e:                       # nie am Pruefer scheitern
            print(f"[frontend-guard] {url} nicht lesbar: {e}")

    def all_exports(url: str, seen=None) -> set[str] | None:
        """Exporte inkl. "export * from"; None = unbekannt (nicht pruefbar)."""
        seen = seen or set()
        if url in seen:
            return set()
        seen.add(url)
        info = parsed.get(url)
        if info is None:
            return None
        out = set(info["exports"])
        for sub in info["star_from"]:
            e = all_exports(sub, seen)
            if e is None:
                return None
            out |= e - {"default"}
        return out

    need: dict[str, dict[str, set[str]]] = {}        # Ziel -> Name -> {Importeure}
    for url, info in parsed.items():
        for target, names in info["imports"]:
            if target not in parsed:
                continue                              # Datei fehlt ganz -> nicht reparierbar
            have = all_exports(target)
            if have is None:
                continue
            for n in names:
                if n not in have:
                    need.setdefault(target, {}).setdefault(n, set()).add(url)

    patches: dict[str, str] = {}
    missing = []
    for target, names in sorted(need.items()):
        src = sources.get(target, "")
        lines = ["", "",
                 "/* ---- BYTERA frontend_guard: fehlende Exporte (Platzhalter, Datei passt nicht zur Version) ---- */"]
        for n in sorted(names):
            if n == "default":
                lines.append(f"export default (function __rmm_missing_default(){{"
                             f"try{{globalThis.__rmmMissingExport?.({json.dumps(target)},'default');}}catch(e){{}}"
                             f"return undefined;}});")
                continue
            if re.search(_RE_TOP_DECL.format(name=re.escape(n)), src, re.M):
                lines.append(f"export {{ {n} }};   // vorhanden, aber nicht exportiert")
                continue
            local = f"__rmm_missing_{n}"
            lines.append(
                f"const {local} = function(){{"
                f"try{{globalThis.__rmmMissingExport?.({json.dumps(target)},{json.dumps(n)});}}catch(e){{}}"
                f"return undefined;}};")
            lines.append(f"export {{ {local} as {n} }};")
        patches[target] = "\n".join(lines) + "\n"
        for n, importers in sorted(names.items()):
            missing.append({"file": target, "name": n, "importers": sorted(importers)})
            key = (target, n)
            if key not in _logged:
                _logged.add(key)
                print(f"[frontend-guard] WARNUNG: {', '.join(sorted(importers))} importiert '{n}' aus "
                      f"{target} - dort nicht exportiert. Platzhalter wird ausgeliefert "
                      f"(Dateien aus verschiedenen Staenden? Update/Hotfix vollstaendig einspielen).")
    _cache.update(sig=sig, patches=patches, missing=missing,
                  files=sorted(u for u, _ in files), at=int(time.time()))
    _cache["paths"] = paths


def _ensure() -> None:
    now = time.monotonic()
    if now - _cache["checked"] < _RECHECK_SEC and _cache["sig"] is not None:
        return
    with _lock:
        if now - _cache["checked"] < _RECHECK_SEC and _cache["sig"] is not None:
            return
        try:
            _analyse()
        except Exception as e:                       # Schutz darf nie selbst stoeren
            print(f"[frontend-guard] Pruefung fehlgeschlagen: {e}")
        _cache["checked"] = time.monotonic()


# ---------------------------------------------------------------- Oeffentlich
def patch_for(url: str) -> str:
    """Anhang (Platzhalter-Exporte) fuer diese Datei oder ''."""
    try:
        _ensure()
        return _cache["patches"].get(url, "")
    except Exception:
        return ""


def health() -> dict:
    _ensure()
    return {
        "ok": not _cache["missing"],
        "missing": _cache["missing"],
        "files": _cache["files"],
        "checked_at": _cache["at"],
    }


def check_on_startup() -> None:
    """Beim Start einmal pruefen und Warnungen ins Log schreiben."""
    try:
        _ensure()
        n = len(_cache["missing"])
        if n:
            print(f"[frontend-guard] {n} fehlende Export(e) im Frontend - Platzhalter aktiv, "
                  f"Details: GET /api/frontend/health")
    except Exception as e:
        print(f"[frontend-guard] Start-Pruefung fehlgeschlagen: {e}")
