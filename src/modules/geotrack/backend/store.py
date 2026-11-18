"""
store.py (module "geotrack") - location tracker: configuration + location history.

Tables (SQLite, db._conn):
  geo_clients    per client: whether it is tracked (default: laptops on), last state.
  geo_locations  location history (time, lat/lon, accuracy, source os|wifi|ip,
                 city/country from resolution).
"""

from __future__ import annotations

import time
import uuid

from app import db


def _c():
    return db._conn


def _now() -> int:
    return int(time.time() * 1000)


def ensure_tables() -> None:
    c = _c()
    c.execute("""CREATE TABLE IF NOT EXISTS geo_clients (
        client_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0, set_by_user INTEGER NOT NULL DEFAULT 0,
        last_ts INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '')""")
    c.execute("""CREATE TABLE IF NOT EXISTS geo_locations (
        id TEXT PRIMARY KEY, client_id TEXT NOT NULL, ts INTEGER NOT NULL,
        lat REAL NOT NULL, lon REAL NOT NULL, accuracy REAL NOT NULL DEFAULT 0,
        source TEXT NOT NULL DEFAULT '', city TEXT NOT NULL DEFAULT '', country TEXT NOT NULL DEFAULT '',
        label TEXT NOT NULL DEFAULT '')""")
    c.execute("CREATE INDEX IF NOT EXISTS geo_loc_client ON geo_locations(client_id, ts)")
    c.commit()


# ------------------------------------------------ tracking state per client
def get_client(client_id: str) -> dict | None:
    r = _c().execute("SELECT * FROM geo_clients WHERE client_id=?", (client_id,)).fetchone()
    return dict(r) if r else None


def is_enabled(client_id: str, detected_kind: str = "") -> bool:
    row = get_client(client_id)
    if row:
        return bool(row["enabled"])
    return detected_kind == "laptop"          # default: laptops on, everything else off


def set_enabled(client_id: str, enabled: bool):
    _c().execute("""INSERT INTO geo_clients(client_id,enabled,set_by_user,last_ts,last_error)
        VALUES(?,?,1,0,'') ON CONFLICT(client_id) DO UPDATE SET enabled=excluded.enabled, set_by_user=1""",
        (client_id, 1 if enabled else 0))
    _c().commit()


def mark(client_id: str, error: str = ""):
    _c().execute("""INSERT INTO geo_clients(client_id,enabled,set_by_user,last_ts,last_error)
        VALUES(?,0,0,?,?) ON CONFLICT(client_id) DO UPDATE SET last_ts=excluded.last_ts, last_error=excluded.last_error""",
        (client_id, _now(), error[:300]))
    _c().commit()


def tracked_clients() -> list[dict]:
    return [dict(r) for r in _c().execute("SELECT * FROM geo_clients WHERE enabled=1").fetchall()]


# ------------------------------------------------ locations
def add_location(client_id, lat, lon, accuracy, source, city="", country="", label="") -> dict:
    lid = uuid.uuid4().hex[:16]
    ts = _now()
    _c().execute("""INSERT INTO geo_locations(id,client_id,ts,lat,lon,accuracy,source,city,country,label)
        VALUES(?,?,?,?,?,?,?,?,?,?)""", (lid, client_id, ts, float(lat), float(lon), float(accuracy or 0),
                                         source, city or "", country or "", label or ""))
    _c().commit()
    return {"id": lid, "client_id": client_id, "ts": ts, "lat": lat, "lon": lon, "accuracy": accuracy or 0,
            "source": source, "city": city, "country": country, "label": label}


def last_location(client_id) -> dict | None:
    r = _c().execute("SELECT * FROM geo_locations WHERE client_id=? ORDER BY ts DESC LIMIT 1", (client_id,)).fetchone()
    return dict(r) if r else None


def history(client_id, limit=500) -> list[dict]:
    rows = _c().execute("SELECT * FROM geo_locations WHERE client_id=? ORDER BY ts DESC LIMIT ?",
                        (client_id, int(limit))).fetchall()
    return [dict(r) for r in rows]


def clear_history(client_id):
    _c().execute("DELETE FROM geo_locations WHERE client_id=?", (client_id,))
    _c().commit()


def prune(retention_days: int):
    if retention_days <= 0:
        return
    cutoff = _now() - retention_days * 86400 * 1000
    _c().execute("DELETE FROM geo_locations WHERE ts<?", (cutoff,))
    _c().commit()
