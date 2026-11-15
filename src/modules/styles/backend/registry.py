# registry.py (Modul "styles") - verfuegbare Styles aus frontend/styles/*/style.json
# ---------------------------------------------------------------------------------
# Jeder Unterordner mit einer gueltigen style.json ist ein Style. Wird bei jedem
# Aufruf frisch gelesen (kleine Dateien) - ein neu hinzugefuegter Ordner ist
# also sofort auswaehlbar, ohne Neustart.
from __future__ import annotations

import json
import re
from pathlib import Path

STYLES_DIR = Path(__file__).resolve().parents[1] / "frontend" / "styles"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
LAYOUTS = ("none", "classic", "classic-hard")


def styles() -> list[dict]:
    out, seen = [], {"futuristic"}
    if not STYLES_DIR.is_dir():
        return out
    for d in sorted(STYLES_DIR.iterdir()):
        f = d / "style.json"
        if not d.is_dir() or not f.is_file():
            continue
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[styles] {f}: ungueltiges JSON ({e}) - uebersprungen")
            continue
        sid = str(meta.get("id") or d.name).strip().lower()
        if not _ID_RE.match(sid) or sid in seen:
            print(f"[styles] {d.name}: ungueltige oder doppelte id '{sid}' - uebersprungen")
            continue
        seen.add(sid)
        css = meta.get("css") or []
        out.append({
            "id": sid,
            "dir": d.name,
            "name": str(meta.get("name") or sid)[:60],
            "description": str(meta.get("description") or "")[:300],
            "order": int(meta.get("order") or 100),
            "bodyClass": str(meta.get("bodyClass") or f"style-{sid}")[:200],
            "layout": meta.get("layout") if meta.get("layout") in LAYOUTS else "none",
            "css": [str(c) for c in (css if isinstance(css, list) else [css])][:20],
            "js": str(meta.get("js") or "") or None,
        })
    out.sort(key=lambda s: (s["order"], s["name"].lower()))
    return out


def ids() -> list[str]:
    return [s["id"] for s in styles()]
