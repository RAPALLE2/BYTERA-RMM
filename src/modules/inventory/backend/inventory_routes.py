"""
inventory_routes.py (Modul "inventory")
---------------------------------------
Inventar: Geraete (Laptops, Handys, Monitore, Moebel ... alles) mit Notizen,
Kosten, Garantie und Besitzer - oder eingelagert in einem Lager/Regal.

Rechte:
  see_inventory     -> Inventar ansehen
  manage_inventory  -> Geraete/Lager anlegen, aendern, loeschen, Besitzer wechseln

Tabellen (werden beim Start angelegt, bleiben beim Deaktivieren erhalten):
  inv_warehouses  Lager        (id, name, notes, created_at)
  inv_shelves     Regale       (id, warehouse_id, name, notes, sort)
  inv_items       Geraete      (Stammdaten + Status + Besitzer ODER Lagerort)
  inv_history     Verlauf      (wer hat wann was geaendert)
  inv_places      Bueros + Abteilungen (kind 'office' | 'department') fuer
                  Geraete "im Einsatz" (inv_items.office_id / department_id)

Status eines Geraets:
  in_use   im Einsatz  -> hat einen Besitzer, kein Lagerort; optional Buero
                         und Abteilung
  storage  im Lager    -> hat Lager (+ optional Regal), keinen Besitzer
  repair   in Reparatur
  retired  ausgemustert

Endpunkte:
  GET    /api/inventory/overview                 alles fuer die App (Lager, Regale, Geraete, Benutzer)
  POST   /api/inventory/items                    Geraet anlegen
  PUT    /api/inventory/items/{id}               Geraet bearbeiten
  DELETE /api/inventory/items/{id}               Geraet loeschen
  POST   /api/inventory/items/{id}/assign        Besitzer setzen/aendern (-> im Einsatz)
  POST   /api/inventory/items/{id}/store         ins Lager stellen (-> im Lager)
  GET    /api/inventory/items/{id}/history       Verlauf
  POST   /api/inventory/warehouses               Lager anlegen
  PUT    /api/inventory/warehouses/{id}          Lager umbenennen/Notiz
  DELETE /api/inventory/warehouses/{id}          Lager loeschen (nur wenn leer)
  POST   /api/inventory/warehouses/{id}/shelves  Regal anlegen
  PUT    /api/inventory/shelves/{id}             Regal umbenennen
  DELETE /api/inventory/shelves/{id}             Regal loeschen (nur wenn leer)
  POST   /api/inventory/places                   Buero/Abteilung anlegen {kind, name, notes}
  PUT    /api/inventory/places/{id}              umbenennen/Notiz
  DELETE /api/inventory/places/{id}              loeschen (nur wenn kein Geraet zugeordnet)
"""

import re
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import db
from app.auth import get_current_user, require_perm, user_has_permission

router = APIRouter(prefix="/api/inventory", tags=["inventory"])

VALID_STATUS = ("in_use", "storage", "repair", "retired")
VALID_CATEGORIES = ("laptop", "pc", "monitor", "phone", "tablet", "printer", "network",
                    "server", "peripheral", "headset", "camera", "tv", "furniture",
                    "vehicle", "key", "license", "other")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ------------------------------------------------------------------
# Hilfen
# ------------------------------------------------------------------
def _c():
    return db._conn


def _now() -> int:
    return int(time.time() * 1000)


def _id() -> str:
    return uuid.uuid4().hex[:16]


def ensure_tables() -> None:
    c = _c()
    c.execute("""
        CREATE TABLE IF NOT EXISTS inv_warehouses (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL
        )""")
    c.execute("""
        CREATE TABLE IF NOT EXISTS inv_shelves (
            id TEXT PRIMARY KEY,
            warehouse_id TEXT NOT NULL,
            name TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            sort INTEGER NOT NULL DEFAULT 0
        )""")
    c.execute("""
        CREATE TABLE IF NOT EXISTS inv_items (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'other',
            manufacturer TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            serial TEXT NOT NULL DEFAULT '',
            asset_tag TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'storage',
            owner_name TEXT NOT NULL DEFAULT '',
            owner_user_id TEXT NOT NULL DEFAULT '',
            warehouse_id TEXT NOT NULL DEFAULT '',
            shelf_id TEXT NOT NULL DEFAULT '',
            purchase_date TEXT NOT NULL DEFAULT '',
            cost REAL,
            warranty_until TEXT NOT NULL DEFAULT '',
            supplier TEXT NOT NULL DEFAULT '',
            client_id TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            created_by TEXT NOT NULL DEFAULT ''
        )""")
    c.execute("""
        CREATE TABLE IF NOT EXISTS inv_history (
            id TEXT PRIMARY KEY,
            item_id TEXT NOT NULL,
            ts INTEGER NOT NULL,
            username TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT ''
        )""")
    c.execute("CREATE INDEX IF NOT EXISTS inv_history_item ON inv_history(item_id, ts)")
    c.execute("""
        CREATE TABLE IF NOT EXISTS inv_places (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL
        )""")
    # Bestehende Installationen: neue Spalten nachruesten
    cols = {r["name"] for r in c.execute("PRAGMA table_info(inv_items)")}
    for col in ("office_id", "department_id"):
        if col not in cols:
            c.execute(f"ALTER TABLE inv_items ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
    c.commit()


def _can_see(user: dict) -> None:
    if not (user_has_permission(user, "see_inventory")
            or user_has_permission(user, "manage_inventory")):
        raise HTTPException(403, "Fehlendes Recht: see_inventory")


def _can_manage(user: dict) -> None:
    require_perm(user, "manage_inventory")


def _history(item_id: str, user: dict, action: str, details: str = "") -> None:
    _c().execute(
        "INSERT INTO inv_history (id, item_id, ts, username, action, details) VALUES (?,?,?,?,?,?)",
        (_id(), item_id, _now(), user.get("username", ""), action, details[:1000]))


def _item(item_id: str) -> dict:
    r = _c().execute("SELECT * FROM inv_items WHERE id = ?", (item_id,)).fetchone()
    if not r:
        raise HTTPException(404, "Gerät nicht gefunden")
    return dict(r)


def _warehouse(wid: str) -> dict:
    r = _c().execute("SELECT * FROM inv_warehouses WHERE id = ?", (wid,)).fetchone()
    if not r:
        raise HTTPException(404, "Lager nicht gefunden")
    return dict(r)


def _shelf(sid: str) -> dict:
    r = _c().execute("SELECT * FROM inv_shelves WHERE id = ?", (sid,)).fetchone()
    if not r:
        raise HTTPException(404, "Regal nicht gefunden")
    return dict(r)


def _date(v: str | None, field: str) -> str:
    v = (v or "").strip()
    if v and not _DATE_RE.match(v):
        raise HTTPException(400, f"{field}: Datum bitte als JJJJ-MM-TT")
    return v


def _location_text(wid: str, sid: str) -> str:
    if not wid:
        return "ohne Lagerort"
    w = _c().execute("SELECT name FROM inv_warehouses WHERE id = ?", (wid,)).fetchone()
    s = _c().execute("SELECT name FROM inv_shelves WHERE id = ?", (sid,)).fetchone() if sid else None
    return (w["name"] if w else "?") + (f" / {s['name']}" if s else "")


def _check_location(wid: str, sid: str) -> tuple[str, str]:
    wid, sid = (wid or "").strip(), (sid or "").strip()
    if sid and not wid:
        wid = _shelf(sid)["warehouse_id"]
    if wid:
        _warehouse(wid)
    if sid and _shelf(sid)["warehouse_id"] != wid:
        raise HTTPException(400, "Regal gehört nicht zu diesem Lager")
    return wid, sid


PLACE_KINDS = {"office": "Büro", "department": "Abteilung"}


def _place(pid: str) -> dict:
    r = _c().execute("SELECT * FROM inv_places WHERE id = ?", (pid,)).fetchone()
    if not r:
        raise HTTPException(404, "Büro/Abteilung nicht gefunden")
    return dict(r)


def _check_place(pid: str, kind: str) -> str:
    pid = (pid or "").strip()
    if pid and _place(pid)["kind"] != kind:
        raise HTTPException(400, f"Ungültige Auswahl für {PLACE_KINDS[kind]}")
    return pid


def _place_name(pid: str) -> str:
    if not pid:
        return "–"
    r = _c().execute("SELECT name FROM inv_places WHERE id = ?", (pid,)).fetchone()
    return r["name"] if r else "?"


def _n_geraete(n: int) -> str:
    return "liegt noch 1 Gerät" if n == 1 else f"liegen noch {n} Geräte"


def _owner_text(name: str) -> str:
    return name or "niemand"


# ------------------------------------------------------------------
# Uebersicht
# ------------------------------------------------------------------
@router.get("/overview")
def overview(user: dict = Depends(get_current_user)):
    _can_see(user)
    ensure_tables()
    c = _c()
    warehouses = [dict(r) for r in c.execute("SELECT * FROM inv_warehouses ORDER BY name COLLATE NOCASE")]
    shelves = [dict(r) for r in c.execute("SELECT * FROM inv_shelves ORDER BY sort, name COLLATE NOCASE")]
    for w in warehouses:
        w["shelves"] = [s for s in shelves if s["warehouse_id"] == w["id"]]
    items = [dict(r) for r in c.execute("SELECT * FROM inv_items ORDER BY name COLLATE NOCASE")]
    places = [dict(r) for r in c.execute("SELECT * FROM inv_places ORDER BY name COLLATE NOCASE")]
    users = [{"id": u["id"], "name": u.get("display_name") or u["username"]}
             for u in db.list_users()]
    return {
        "warehouses": warehouses,
        "items": items,
        "offices": [p for p in places if p["kind"] == "office"],
        "departments": [p for p in places if p["kind"] == "department"],
        "users": users,
        "categories": list(VALID_CATEGORIES),
        "can_manage": user_has_permission(user, "manage_inventory"),
    }


# ------------------------------------------------------------------
# Geraete
# ------------------------------------------------------------------
class ItemBody(BaseModel):
    name: str
    category: str = "other"
    manufacturer: str = ""
    model: str = ""
    serial: str = ""
    asset_tag: str = ""
    status: str = "storage"
    owner_name: str = ""
    owner_user_id: str = ""
    warehouse_id: str = ""
    shelf_id: str = ""
    purchase_date: str = ""
    cost: float | None = None
    warranty_until: str = ""
    supplier: str = ""
    client_id: str = ""
    notes: str = ""
    office_id: str = ""
    department_id: str = ""


_TEXT_LIMITS = {"name": 160, "manufacturer": 120, "model": 120, "serial": 120,
                "asset_tag": 80, "owner_name": 160, "supplier": 160, "notes": 5000}

_FIELD_LABELS = {
    "name": "Name", "category": "Typ", "manufacturer": "Hersteller", "model": "Modell",
    "serial": "Seriennummer", "asset_tag": "Inventarnummer", "purchase_date": "Kaufdatum",
    "cost": "Kosten", "warranty_until": "Garantie bis", "supplier": "Lieferant",
    "client_id": "Agent", "notes": "Notizen",
}


def _clean_item(body: ItemBody) -> dict:
    d = body.model_dump() if hasattr(body, "model_dump") else body.dict()
    d["name"] = (d["name"] or "").strip()
    if not d["name"]:
        raise HTTPException(400, "Name fehlt")
    for k, lim in _TEXT_LIMITS.items():
        d[k] = (d.get(k) or "").strip()[:lim]
    if d["category"] not in VALID_CATEGORIES:
        d["category"] = "other"
    if d["status"] not in VALID_STATUS:
        raise HTTPException(400, "Ungültiger Status")
    d["purchase_date"] = _date(d["purchase_date"], "Kaufdatum")
    d["warranty_until"] = _date(d["warranty_until"], "Garantie bis")
    if d["cost"] is not None:
        if d["cost"] < 0 or d["cost"] > 1e9:
            raise HTTPException(400, "Kosten ungültig")
        d["cost"] = round(float(d["cost"]), 2)
    d["client_id"] = (d.get("client_id") or "").strip()[:80]
    d["owner_user_id"] = (d.get("owner_user_id") or "").strip()[:80]
    # Besitzer und Lagerort schliessen sich aus
    if d["status"] == "in_use":
        if not d["owner_name"]:
            raise HTTPException(400, "Für „im Einsatz“ bitte einen Besitzer angeben")
        d["warehouse_id"] = d["shelf_id"] = ""
        d["office_id"] = _check_place(d.get("office_id"), "office")
        d["department_id"] = _check_place(d.get("department_id"), "department")
    elif d["status"] == "storage":
        d["warehouse_id"], d["shelf_id"] = _check_location(d["warehouse_id"], d["shelf_id"])
        d["owner_name"] = d["owner_user_id"] = ""
    else:
        d["warehouse_id"], d["shelf_id"] = _check_location(d["warehouse_id"], d["shelf_id"])
    # Buero/Abteilung gibt es nur fuer Geraete im Einsatz
    if d["status"] != "in_use":
        d["office_id"] = d["department_id"] = ""
    return d


@router.post("/items")
def create_item(body: ItemBody, user: dict = Depends(get_current_user)):
    _can_manage(user)
    ensure_tables()
    d = _clean_item(body)
    d["id"] = _id()
    d["created_at"] = d["updated_at"] = _now()
    d["created_by"] = user.get("username", "")
    cols = list(d.keys())
    _c().execute(f"INSERT INTO inv_items ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                 [d[k] for k in cols])
    where = (f"Besitzer: {d['owner_name']}" + _place_suffix(d) if d["status"] == "in_use"
             else f"Lager: {_location_text(d['warehouse_id'], d['shelf_id'])}")
    _history(d["id"], user, "created", where)
    _c().commit()
    db.add_audit_entry(user["username"], "inventory.create", target=d["name"])
    return _item(d["id"])


@router.put("/items/{item_id}")
def update_item(item_id: str, body: ItemBody, user: dict = Depends(get_current_user)):
    _can_manage(user)
    old = _item(item_id)
    d = _clean_item(body)
    changes = []
    for k, label in _FIELD_LABELS.items():
        if (old.get(k) or "") != (d.get(k) or "") and not (old.get(k) is None and d.get(k) in ("", None)):
            changes.append(label)
    if old["status"] != d["status"]:
        changes.append(f"Status → {d['status']}")
    if (old["owner_name"], old["owner_user_id"]) != (d["owner_name"], d["owner_user_id"]):
        changes.append(f"Besitzer: {_owner_text(old['owner_name'])} → {_owner_text(d['owner_name'])}")
    if (old.get("office_id") or "") != d["office_id"]:
        changes.append(f"Büro: {_place_name(old.get('office_id'))} → {_place_name(d['office_id'])}")
    if (old.get("department_id") or "") != d["department_id"]:
        changes.append(f"Abteilung: {_place_name(old.get('department_id'))} → {_place_name(d['department_id'])}")
    if (old["warehouse_id"], old["shelf_id"]) != (d["warehouse_id"], d["shelf_id"]):
        changes.append(f"Lagerort → {_location_text(d['warehouse_id'], d['shelf_id'])}")
    d["updated_at"] = _now()
    sets = ", ".join(f"{k} = ?" for k in d)
    _c().execute(f"UPDATE inv_items SET {sets} WHERE id = ?", [*d.values(), item_id])
    if changes:
        _history(item_id, user, "edited", ", ".join(changes))
    _c().commit()
    return _item(item_id)


@router.delete("/items/{item_id}")
def delete_item(item_id: str, user: dict = Depends(get_current_user)):
    _can_manage(user)
    it = _item(item_id)
    _c().execute("DELETE FROM inv_items WHERE id = ?", (item_id,))
    _c().execute("DELETE FROM inv_history WHERE item_id = ?", (item_id,))
    _c().commit()
    db.add_audit_entry(user["username"], "inventory.delete", target=it["name"])
    return {"ok": True}


def _place_suffix(d: dict) -> str:
    parts = [f"🏢 {_place_name(d.get('office_id'))}" if d.get("office_id") else "",
             f"👥 {_place_name(d.get('department_id'))}" if d.get("department_id") else ""]
    parts = [p for p in parts if p]
    return f" ({', '.join(parts)})" if parts else ""


class AssignBody(BaseModel):
    owner_name: str
    owner_user_id: str = ""
    note: str = ""
    office_id: str = ""
    department_id: str = ""


@router.post("/items/{item_id}/assign")
def assign_item(item_id: str, body: AssignBody, user: dict = Depends(get_current_user)):
    """Besitzer setzen/wechseln -> Geraet ist 'im Einsatz', Lagerort wird geleert."""
    _can_manage(user)
    it = _item(item_id)
    name = (body.owner_name or "").strip()[:160]
    if not name:
        raise HTTPException(400, "Besitzer fehlt")
    office = _check_place(body.office_id, "office")
    dept = _check_place(body.department_id, "department")
    _c().execute(
        "UPDATE inv_items SET status='in_use', owner_name=?, owner_user_id=?, "
        "office_id=?, department_id=?, warehouse_id='', shelf_id='', updated_at=? WHERE id=?",
        (name, (body.owner_user_id or "").strip()[:80], office, dept, _now(), item_id))
    prev = (_owner_text(it["owner_name"]) if it["status"] == "in_use"
            else f"Lager {_location_text(it['warehouse_id'], it['shelf_id'])}")
    note = (body.note or "").strip()
    _history(item_id, user, "assigned", f"{prev} → {name}"
             + _place_suffix({"office_id": office, "department_id": dept}) + (f" · {note}" if note else ""))
    _c().commit()
    return _item(item_id)


class StoreBody(BaseModel):
    warehouse_id: str
    shelf_id: str = ""
    note: str = ""


@router.post("/items/{item_id}/store")
def store_item(item_id: str, body: StoreBody, user: dict = Depends(get_current_user)):
    """Ins Lager stellen -> Besitzer wird entfernt, Status 'im Lager'."""
    _can_manage(user)
    it = _item(item_id)
    if not (body.warehouse_id or "").strip():
        raise HTTPException(400, "Bitte ein Lager wählen")
    wid, sid = _check_location(body.warehouse_id, body.shelf_id)
    _c().execute(
        "UPDATE inv_items SET status='storage', owner_name='', owner_user_id='', "
        "office_id='', department_id='', warehouse_id=?, shelf_id=?, updated_at=? WHERE id=?",
        (wid, sid, _now(), item_id))
    prev = (f"von {it['owner_name']}" if it["status"] == "in_use" and it["owner_name"]
            else f"aus {_location_text(it['warehouse_id'], it['shelf_id'])}")
    note = (body.note or "").strip()
    _history(item_id, user, "stored",
             f"{prev} → {_location_text(wid, sid)}" + (f" · {note}" if note else ""))
    _c().commit()
    return _item(item_id)


@router.get("/items/{item_id}/history")
def item_history(item_id: str, user: dict = Depends(get_current_user)):
    _can_see(user)
    _item(item_id)
    rows = _c().execute("SELECT * FROM inv_history WHERE item_id = ? ORDER BY ts DESC LIMIT 200",
                        (item_id,)).fetchall()
    return {"history": [dict(r) for r in rows]}


# ------------------------------------------------------------------
# Lager + Regale
# ------------------------------------------------------------------
class NamedBody(BaseModel):
    name: str
    notes: str = ""


def _name(v: str) -> str:
    v = (v or "").strip()[:120]
    if not v:
        raise HTTPException(400, "Name fehlt")
    return v


@router.post("/warehouses")
def create_warehouse(body: NamedBody, user: dict = Depends(get_current_user)):
    _can_manage(user)
    ensure_tables()
    wid = _id()
    _c().execute("INSERT INTO inv_warehouses (id, name, notes, created_at) VALUES (?,?,?,?)",
                 (wid, _name(body.name), (body.notes or "")[:1000], _now()))
    _c().commit()
    return _warehouse(wid)


@router.put("/warehouses/{wid}")
def update_warehouse(wid: str, body: NamedBody, user: dict = Depends(get_current_user)):
    _can_manage(user)
    _warehouse(wid)
    _c().execute("UPDATE inv_warehouses SET name = ?, notes = ? WHERE id = ?",
                 (_name(body.name), (body.notes or "")[:1000], wid))
    _c().commit()
    return _warehouse(wid)


@router.delete("/warehouses/{wid}")
def delete_warehouse(wid: str, user: dict = Depends(get_current_user)):
    _can_manage(user)
    w = _warehouse(wid)
    n = _c().execute("SELECT COUNT(*) AS n FROM inv_items WHERE warehouse_id = ?", (wid,)).fetchone()["n"]
    if n:
        raise HTTPException(409, f"Im Lager „{w['name']}“ {_n_geraete(n)} – bitte erst umlagern oder ausgeben.")
    _c().execute("DELETE FROM inv_shelves WHERE warehouse_id = ?", (wid,))
    _c().execute("DELETE FROM inv_warehouses WHERE id = ?", (wid,))
    _c().commit()
    return {"ok": True}


@router.post("/warehouses/{wid}/shelves")
def create_shelf(wid: str, body: NamedBody, user: dict = Depends(get_current_user)):
    _can_manage(user)
    _warehouse(wid)
    sort = _c().execute("SELECT COALESCE(MAX(sort), 0) + 1 AS s FROM inv_shelves WHERE warehouse_id = ?",
                        (wid,)).fetchone()["s"]
    sid = _id()
    _c().execute("INSERT INTO inv_shelves (id, warehouse_id, name, notes, sort) VALUES (?,?,?,?,?)",
                 (sid, wid, _name(body.name), (body.notes or "")[:1000], sort))
    _c().commit()
    return _shelf(sid)


@router.put("/shelves/{sid}")
def update_shelf(sid: str, body: NamedBody, user: dict = Depends(get_current_user)):
    _can_manage(user)
    _shelf(sid)
    _c().execute("UPDATE inv_shelves SET name = ?, notes = ? WHERE id = ?",
                 (_name(body.name), (body.notes or "")[:1000], sid))
    _c().commit()
    return _shelf(sid)


@router.delete("/shelves/{sid}")
def delete_shelf(sid: str, user: dict = Depends(get_current_user)):
    _can_manage(user)
    s = _shelf(sid)
    n = _c().execute("SELECT COUNT(*) AS n FROM inv_items WHERE shelf_id = ?", (sid,)).fetchone()["n"]
    if n:
        raise HTTPException(409, f"Im Regal „{s['name']}“ {_n_geraete(n)} – bitte erst umlagern.")
    _c().execute("DELETE FROM inv_shelves WHERE id = ?", (sid,))
    _c().commit()
    return {"ok": True}


# ------------------------------------------------------------------
# Bueros + Abteilungen (fuer Geraete "im Einsatz")
# ------------------------------------------------------------------
class PlaceBody(BaseModel):
    kind: str = "office"
    name: str
    notes: str = ""


@router.post("/places")
def create_place(body: PlaceBody, user: dict = Depends(get_current_user)):
    _can_manage(user)
    ensure_tables()
    if body.kind not in PLACE_KINDS:
        raise HTTPException(400, "Art muss office oder department sein")
    name = _name(body.name)
    dup = _c().execute("SELECT 1 FROM inv_places WHERE kind = ? AND name = ? COLLATE NOCASE",
                       (body.kind, name)).fetchone()
    if dup:
        raise HTTPException(409, f"{PLACE_KINDS[body.kind]} „{name}“ gibt es schon")
    pid = _id()
    _c().execute("INSERT INTO inv_places (id, kind, name, notes, created_at) VALUES (?,?,?,?,?)",
                 (pid, body.kind, name, (body.notes or "")[:1000], _now()))
    _c().commit()
    return _place(pid)


@router.put("/places/{pid}")
def update_place(pid: str, body: PlaceBody, user: dict = Depends(get_current_user)):
    _can_manage(user)
    p = _place(pid)
    _c().execute("UPDATE inv_places SET name = ?, notes = ? WHERE id = ?",
                 (_name(body.name), (body.notes or "")[:1000], pid))
    _c().commit()
    return _place(p["id"])


@router.delete("/places/{pid}")
def delete_place(pid: str, user: dict = Depends(get_current_user)):
    _can_manage(user)
    p = _place(pid)
    col = "office_id" if p["kind"] == "office" else "department_id"
    n = _c().execute(f"SELECT COUNT(*) AS n FROM inv_items WHERE {col} = ?", (pid,)).fetchone()["n"]
    if n:
        word = "ist noch 1 Gerät" if n == 1 else f"sind noch {n} Geräte"
        raise HTTPException(409, f"{PLACE_KINDS[p['kind']]} „{p['name']}“: Hier {word} zugeordnet – bitte erst ändern.")
    _c().execute("DELETE FROM inv_places WHERE id = ?", (pid,))
    _c().commit()
    return {"ok": True}
