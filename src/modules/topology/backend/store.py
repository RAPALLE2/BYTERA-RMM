# store.py (Modul "topology") - gespeicherte Karten + manuelle Bearbeitungen
# ---------------------------------------------------------------------------
# data/modules/topology/data.json:
# {
#   "maps":      { "<quelle>": { at, label, own_ip, gateway, nodes, edges } },
#   "overrides": { "<quelle>": {
#       "pos":          { "<node-id>": {"x":.., "y":..} },   Position aus dem Edit-Modus
#       "meta":         { "<node-id>": {"label":.., "type":..} },  Umbenennung/Icon
#       "hidden":       ["<node-id>", ...],                  ausgeblendete Geraete
#       "custom_nodes": [ {id,label,type,x,y,info}, ... ],   selbst angelegte Knoten
#       "custom_edges": [ ["a","b"], ... ],                  selbst gezogene Linien
#       "removed_edges":[ ["a","b"], ... ]                   geloeschte automatische Linien
#   } }
# }
# "Quelle" ist "backend" oder die Client-ID der Node, von der aus erkannt wurde.

import json
import threading

from app.paths import MODULES_DATA_DIR

DIR = MODULES_DATA_DIR / "topology"
FILE = DIR / "data.json"
_lock = threading.Lock()

_EMPTY_OVR = {"pos": {}, "meta": {}, "hidden": [],
              "custom_nodes": [], "custom_edges": [], "removed_edges": []}


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


def get_overrides(source: str) -> dict:
    with _lock:
        ovr = _load()["overrides"].get(source) or {}
    return {**_EMPTY_OVR, **ovr}


def set_overrides(source: str, overrides: dict) -> dict:
    clean = {
        "pos": {str(k): {"x": float(v.get("x", 0)), "y": float(v.get("y", 0))}
                for k, v in (overrides.get("pos") or {}).items()
                if isinstance(v, dict)},
        "meta": {str(k): {kk: str(vv)[:120] for kk, vv in v.items() if kk in ("label", "type")}
                 for k, v in (overrides.get("meta") or {}).items()
                 if isinstance(v, dict)},
        "hidden": [str(x)[:120] for x in (overrides.get("hidden") or [])][:500],
        "custom_nodes": [
            {"id": str(n.get("id", ""))[:120], "label": str(n.get("label", ""))[:120],
             "type": str(n.get("type", "device"))[:32],
             "x": float(n.get("x", 0)), "y": float(n.get("y", 0)),
             "info": str(n.get("info", ""))[:500]}
            for n in (overrides.get("custom_nodes") or [])
            if isinstance(n, dict) and n.get("id")
        ][:200],
        "custom_edges": [[str(a)[:120], str(b)[:120]]
                         for e in (overrides.get("custom_edges") or [])
                         if isinstance(e, (list, tuple)) and len(e) == 2
                         for a, b in [e]][:500],
        "removed_edges": [[str(a)[:120], str(b)[:120]]
                          for e in (overrides.get("removed_edges") or [])
                          if isinstance(e, (list, tuple)) and len(e) == 2
                          for a, b in [e]][:500],
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
