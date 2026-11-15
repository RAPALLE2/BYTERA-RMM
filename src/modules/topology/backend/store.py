# store.py (module "topology") - saved maps + manual edits
# ---------------------------------------------------------
# data/modules/topology/data.json:
# {
#   "maps":      { "<source>": { at, label, own_ip, gateway, nodes, edges } },
#   "overrides": { "<source>": {                         (the same for ALL users)
#       "pos":          { "<node-id>": {"x":.., "y":..} },
#       "meta":         { "<node-id>": {label, type, ip, os, agent_id, note, note_mode} },
#       "hidden":       ["<node-id>", ...],
#       "custom_nodes": [ {id, label, type, x, y, ip, os, agent_id, info}, ... ],
#       "lines":        [ {id, a, b, style, flow, color, label, la, lb, arrow}, ... ]   own lines
#       "edge_props":   { "a|b": {style, flow, color, label, la, lb, arrow} }            discovered lines
#       "removed_edges":[ ["a","b"], ... ],
#       "flows":        [ {id, name, color}, ... ]          named flows (legend), e.g. "Backup" orange
#       "boxes":        [ {id, x, y, w, h, label, color}, ... ]   areas, e.g. a PVE node + its CTs
#       "line_style":   "default" | "pcb" | "web" | "cable"       map-wide default style
#   } }
# }
# data/modules/topology/prefs.json: { "<user-id>": {"bg": "lines|squares|crosses|honeycomb|none", "snap": bool} }
# "Source" is "backend" or the client ID of the node the discovery ran on.

import json
import re
import threading

from app.paths import MODULES_DATA_DIR

DIR = MODULES_DATA_DIR / "topology"
FILE = DIR / "data.json"
PREFS = DIR / "prefs.json"
_lock = threading.Lock()

LINE_STYLES = ("default", "pcb", "web", "cable")
BACKGROUNDS = ("lines", "squares", "crosses", "honeycomb", "none")
ARROWS = ("none", "end", "start", "both")
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")

_EMPTY_OVR = {"pos": {}, "meta": {}, "hidden": [], "custom_nodes": [], "lines": [], "edge_props": {},
              "removed_edges": [], "flows": [], "boxes": [], "line_style": "default"}


def _load() -> dict:
    try:
        with open(FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    data.setdefault("maps", {})
    data.setdefault("overrides", {})
    return data


def _save(data: dict) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    tmp.replace(FILE)


def get_map(source: str) -> dict | None:
    with _lock:
        return _load()["maps"].get(source)


def set_map(source: str, result: dict) -> None:
    with _lock:
        data = _load()
        data["maps"][source] = result
        _save(data)


def _migrate(ovr: dict) -> dict:
    """Older format: "custom_edges" [[a,b], ...] -> "lines" objects."""
    if ovr.get("custom_edges") and not ovr.get("lines"):
        ovr["lines"] = [{"id": f"l{i}", "a": a, "b": b}
                        for i, e in enumerate(ovr["custom_edges"])
                        if isinstance(e, (list, tuple)) and len(e) == 2 for a, b in [e]]
    ovr.pop("custom_edges", None)
    return ovr


def get_overrides(source: str) -> dict:
    with _lock:
        ovr = dict(_load()["overrides"].get(source) or {})
    return {**_EMPTY_OVR, **_migrate(ovr)}


# ------------------------------------------------------------------ cleaning (never trust the client)
def _s(v, n=120) -> str:
    return str(v if v is not None else "")[:n]


def _f(v, default=0.0) -> float:
    try:
        x = float(v)
        return x if abs(x) < 1e7 else default
    except (TypeError, ValueError):
        return default


def _color(v) -> str:
    v = str(v or "")
    return v if _COLOR.match(v) else ""


def _clean_meta(v: dict) -> dict:
    out = {k: _s(v.get(k)) for k in ("label", "type", "ip", "os", "agent_id") if v.get(k)}
    note = _s(v.get("note"), 1000).strip()
    if note:
        out["note"] = note
        out["note_mode"] = "always" if v.get("note_mode") == "always" else "hover"
    return out


def _clean_line_props(v: dict) -> dict:
    out = {}
    if v.get("style") in LINE_STYLES:
        out["style"] = v["style"]
    for k in ("flow", "label", "la", "lb"):
        if v.get(k):
            out[k] = _s(v[k], 60)
    c = _color(v.get("color"))
    if c:
        out["color"] = c
    if v.get("arrow") in ARROWS and v["arrow"] != "none":
        out["arrow"] = v["arrow"]
    return out


def set_overrides(source: str, overrides: dict) -> dict:
    o = _migrate(dict(overrides or {}))
    clean = {
        "pos": {_s(k): {"x": _f(v.get("x")), "y": _f(v.get("y"))}
                for k, v in (o.get("pos") or {}).items() if isinstance(v, dict)},
        "meta": {_s(k): _clean_meta(v) for k, v in (o.get("meta") or {}).items()
                 if isinstance(v, dict) and _clean_meta(v)},
        "hidden": [_s(x) for x in (o.get("hidden") or [])][:1000],
        "custom_nodes": [
            {"id": _s(n.get("id")), "label": _s(n.get("label")), "type": _s(n.get("type") or "", 32),
             "x": _f(n.get("x")), "y": _f(n.get("y")), "ip": _s(n.get("ip"), 120), "os": _s(n.get("os"), 120),
             "agent_id": _s(n.get("agent_id"), 64), "info": _s(n.get("info"), 500)}
            for n in (o.get("custom_nodes") or []) if isinstance(n, dict) and n.get("id")
        ][:500],
        "lines": [
            {"id": _s(l.get("id"), 40), "a": _s(l.get("a")), "b": _s(l.get("b")), **_clean_line_props(l)}
            for l in (o.get("lines") or []) if isinstance(l, dict) and l.get("a") and l.get("b") and l.get("id")
        ][:2000],
        "edge_props": {_s(k, 250): _clean_line_props(v) for k, v in (o.get("edge_props") or {}).items()
                       if isinstance(v, dict) and _clean_line_props(v)},
        "removed_edges": [[_s(a), _s(b)] for e in (o.get("removed_edges") or [])
                          if isinstance(e, (list, tuple)) and len(e) == 2 for a, b in [e]][:1000],
        "flows": [{"id": _s(f.get("id"), 40), "name": _s(f.get("name"), 60), "color": _color(f.get("color")) or "#f59e0b"}
                  for f in (o.get("flows") or []) if isinstance(f, dict) and f.get("id")][:100],
        "boxes": [{"id": _s(b.get("id"), 40), "x": _f(b.get("x")), "y": _f(b.get("y")),
                   "w": max(60.0, _f(b.get("w"), 300)), "h": max(40.0, _f(b.get("h"), 200)),
                   "label": _s(b.get("label"), 80), "color": _color(b.get("color")) or "#64748b"}
                  for b in (o.get("boxes") or []) if isinstance(b, dict) and b.get("id")][:300],
        "line_style": o.get("line_style") if o.get("line_style") in LINE_STYLES else "default",
    }
    with _lock:
        data = _load()
        data["overrides"][source] = clean
        _save(data)
    return clean


def list_sources() -> list[str]:
    with _lock:
        data = _load()
    return sorted(set(data["maps"]) | set(data["overrides"]))


# ------------------------------------------------------------------ per-user view settings
def get_prefs(uid: str) -> dict:
    try:
        p = json.loads(PREFS.read_text(encoding="utf-8")).get(uid) or {}
    except Exception:
        p = {}
    return {"bg": p.get("bg") if p.get("bg") in BACKGROUNDS else "crosses", "snap": bool(p.get("snap", True))}


def set_prefs(uid: str, prefs: dict) -> dict:
    with _lock:
        try:
            allp = json.loads(PREFS.read_text(encoding="utf-8"))
        except Exception:
            allp = {}
        cur = get_prefs(uid)
        if prefs.get("bg") in BACKGROUNDS:
            cur["bg"] = prefs["bg"]
        if "snap" in prefs:
            cur["snap"] = bool(prefs["snap"])
        allp[uid] = cur
        DIR.mkdir(parents=True, exist_ok=True)
        tmp = PREFS.with_suffix(".tmp")
        tmp.write_text(json.dumps(allp), encoding="utf-8")
        tmp.replace(PREFS)
    return cur
