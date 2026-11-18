"""
geo.py (module "geotrack") - locate agents and resolve coordinates.

A single location run:
  1. The backend asks the agent (socket event "geo-locate") for:
       - OS location (Windows Location API / geoclue / CoreLocation), if allowed
       - nearby Wi-Fi access points (BSSID + signal)
       - the public IP (the agent queries an IP-echo service)
  2. Resolution, in this order (best source wins):
       OS coordinates  >  Wi-Fi via Google Geolocation API (key required)  >  IP geolocation
None of this is persisted in clear; only the result (lat/lon) goes into the history (store.py).
"""

from __future__ import annotations

import asyncio

from app import modules

GOOGLE_URL = "https://www.googleapis.com/geolocation/v1/geolocate"


async def request_agent(client_id: str, timeout: float = 25.0) -> dict:
    """Send "geo-locate" to the agent and wait for "geo-result"."""
    from app import sockets
    from app.sockets import state, _new_request_id
    sid = state.client_id_to_sid.get(client_id)
    if not sid:
        raise RuntimeError("Client is offline")
    rid = _new_request_id()
    fut = asyncio.get_event_loop().create_future()
    state.pending_requests[rid] = fut
    try:
        await sockets.sio.emit("geo-locate", {"requestId": rid}, to=sid, namespace="/agent")
        payload = await asyncio.wait_for(fut, timeout=timeout)
    finally:
        state.pending_requests.pop(rid, None)
    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError(payload["error"])
    return payload or {}


def _norm_mac(mac: str) -> str:
    return (mac or "").strip().lower()


async def _google_wifi(wifi: list, key: str) -> dict | None:
    aps = [{"macAddress": _norm_mac(w.get("mac")), "signalStrength": int(w.get("signal", -65))}
           for w in wifi if w.get("mac")]
    if len(aps) < 2:
        return None
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(GOOGLE_URL, params={"key": key}, json={"considerIp": False, "wifiAccessPoints": aps})
        if r.status_code != 200:
            return None
        d = r.json()
        loc = d.get("location") or {}
        if "lat" not in loc:
            return None
        return {"lat": loc["lat"], "lon": loc["lng"], "accuracy": d.get("accuracy", 0), "source": "wifi"}
    except Exception:
        return None


async def _ip_geo(ip: str) -> dict | None:
    if not ip:
        return None
    svc = str(modules.setting("geotrack", "ip_service", "ip-api") or "ip-api")
    import httpx
    try:
        async with httpx.AsyncClient(timeout=12) as c:
            if svc == "ipinfo":
                tok = str(modules.setting("geotrack", "ipinfo_token", "") or "")
                r = await c.get(f"https://ipinfo.io/{ip}/json", params=({"token": tok} if tok else {}))
                d = r.json()
                if "loc" not in d:
                    return None
                lat, lon = d["loc"].split(",")
                return {"lat": float(lat), "lon": float(lon), "accuracy": 25000, "source": "ip",
                        "city": d.get("city", ""), "country": d.get("country", "")}
            # default: ip-api.com (free, no key)
            r = await c.get(f"http://ip-api.com/json/{ip}", params={"fields": "status,country,city,lat,lon"})
            d = r.json()
            if d.get("status") != "success":
                return None
            return {"lat": d["lat"], "lon": d["lon"], "accuracy": 25000, "source": "ip",
                    "city": d.get("city", ""), "country": d.get("country", "")}
    except Exception:
        return None


async def _reverse(lat, lon) -> dict:
    """Resolve city/country for coordinates (only if not already known), via OSM Nominatim."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=12, headers={"User-Agent": "BYTERA-RMM"}) as c:
            r = await c.get("https://nominatim.openstreetmap.org/reverse",
                            params={"lat": lat, "lon": lon, "format": "json", "zoom": 12})
        a = (r.json() or {}).get("address", {})
        city = a.get("city") or a.get("town") or a.get("village") or a.get("county") or ""
        return {"city": city, "country": a.get("country", "")}
    except Exception:
        return {"city": "", "country": ""}


async def resolve(client_id: str, agent_data: dict, public_ip_hint: str = "") -> dict:
    """agent_data: {os:{lat,lon,accuracy}|null, wifi:[{mac,signal}], public_ip}. -> location dict."""
    key = str(modules.setting("geotrack", "google_api_key", "") or "")
    sources_cfg = modules.setting("geotrack", "sources", "os,wifi,ip") or "os,wifi,ip"
    allowed = {s.strip() for s in str(sources_cfg).split(",")}
    res = None
    os_loc = agent_data.get("os") or {}
    if "os" in allowed and os_loc.get("lat") is not None:
        res = {"lat": os_loc["lat"], "lon": os_loc["lon"], "accuracy": os_loc.get("accuracy", 0), "source": "os"}
    if res is None and "wifi" in allowed and key and agent_data.get("wifi"):
        res = await _google_wifi(agent_data["wifi"], key)
    if res is None and "ip" in allowed:
        res = await _ip_geo(agent_data.get("public_ip") or public_ip_hint)
    if res is None:
        raise RuntimeError("Could not determine location (no source returned a result)")
    if not res.get("city"):
        res.update(await _reverse(res["lat"], res["lon"]))
    return res
