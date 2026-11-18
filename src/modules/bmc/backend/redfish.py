"""
redfish.py (module "bmc") - shared Redfish client for iLO and iDRAC.

The same (synchronous) class runs in the BACKEND (directly reachable) and in the
NODE part (node on the management network). Redfish is the DMTF standard REST API
spoken by both HPE iLO (4/5/6) and Dell iDRAC (7/8/9). Everything is HTTPS + JSON,
so httpx is enough - no ipmitool, no vendor SDKs.

Design:
  * Session login (X-Auth-Token), falling back to Basic auth.
  * Everything is DISCOVERED from the service root (no hard-coded paths), so iLO
    and iDRAC quirks are handled automatically.
  * dispatch(op, args) wraps the individual actions; "raw" allows any Redfish
    call -> whatever the BMC can do, the module can do too.
"""

from __future__ import annotations

import time


class RedfishError(RuntimeError):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


RESET_TYPES = ["On", "ForceOff", "GracefulShutdown", "GracefulRestart", "ForceRestart", "ForceOn", "Nmi", "PushPowerButton"]
BOOT_TARGETS = ["None", "Pxe", "Hdd", "Cd", "Usb", "BiosSetup", "Utilities", "Diags", "UefiShell", "UefiTarget", "SDCard", "FloppyRemovableMedia"]


class RedfishClient:
    def __init__(self, conn: dict):
        import httpx
        host = str(conn.get("host") or "").strip()
        if not host:
            raise RedfishError("No address (host/IP) given")
        scheme = "http" if conn.get("insecure_http") else "https"
        port = conn.get("port")
        base = f"{scheme}://{host}" + (f":{int(port)}" if port else "")
        self.base = base.rstrip("/")
        self.user = conn.get("username") or ""
        self.pw = conn.get("password") or ""
        verify = bool(conn.get("verify_tls"))
        fp = (conn.get("fingerprint") or "").strip()
        self._fp = fp.replace(":", "").lower() or None
        self._httpx = httpx
        self._c = httpx.Client(verify=False if (not verify or self._fp) else True,
                               timeout=httpx.Timeout(connect=8.0, read=45.0, write=45.0, pool=8.0),
                               follow_redirects=False, headers={"Accept": "application/json",
                                                                "OData-Version": "4.0"})
        self._token = None
        self._session_uri = None
        self._root = None
        self._sysmembers = None

    # ------------------------------------------------ HTTP
    def _check_fp(self):
        if not self._fp:
            return
        import socket
        import ssl
        import hashlib
        from urllib.parse import urlparse
        u = urlparse(self.base)
        ctx = ssl._create_unverified_context()
        with socket.create_connection((u.hostname, u.port or 443), timeout=8) as s:
            with ctx.wrap_socket(s, server_hostname=u.hostname) as ss:
                der = ss.getpeercert(binary_form=True)
        got = hashlib.sha256(der).hexdigest()
        if got != self._fp:
            raise RedfishError(f"Certificate fingerprint mismatch (expected {self._fp[:16]}…, got {got[:16]}…)")

    def _headers(self):
        h = {}
        if self._token:
            h["X-Auth-Token"] = self._token
        elif self.user:
            import base64
            h["Authorization"] = "Basic " + base64.b64encode(f"{self.user}:{self.pw}".encode()).decode()
        return h

    def _req(self, method, path, json=None, raw_body=None, headers=None):
        url = path if path.startswith("http") else self.base + ("" if path.startswith("/") else "/") + path
        hs = self._headers()
        if headers:
            hs.update(headers)
        try:
            r = self._c.request(method, url, json=json, content=raw_body, headers=hs)
        except self._httpx.HTTPError as exc:
            raise RedfishError(f"Connection to the BMC failed: {exc}") from None
        if r.status_code == 401 and not self._token and self.user:
            # try a session and retry once
            if self._login():
                return self._req(method, path, json=json, raw_body=raw_body, headers=headers)
        return r

    def _json(self, r):
        try:
            return r.json()
        except Exception:
            return {}

    def _ok(self, r, what):
        if r.status_code >= 400:
            msg = ""
            try:
                err = r.json().get("error", {})
                ext = err.get("@Message.ExtendedInfo") or err.get("@Message.ExtendedInfo".lower()) or []
                if ext:
                    msg = "; ".join(m.get("Message", "") for m in ext if m.get("Message"))
                msg = msg or err.get("message") or err.get("Message") or ""
            except Exception:
                msg = (r.text or "")[:300]
            raise RedfishError(f"{what} failed (HTTP {r.status_code}): {msg}".strip(), r.status_code)
        return r

    def _get(self, path):
        return self._json(self._ok(self._req("GET", path), f"GET {path}"))

    # ------------------------------------------------ Login / Logout
    def _login(self) -> bool:
        if not self.user:
            return False
        root = self._get_root()
        ss = (root.get("SessionService") or {}).get("@odata.id") or "/redfish/v1/SessionService"
        sess = ss.rstrip("/") + "/Sessions"
        r = self._req("POST", sess, json={"UserName": self.user, "Password": self.pw})
        if r.status_code in (200, 201):
            self._token = r.headers.get("X-Auth-Token")
            self._session_uri = r.headers.get("Location")
            return bool(self._token)
        # some iLO firmware expects the Links->Sessions path
        links = root.get("Links", {}).get("Sessions", {}).get("@odata.id")
        if links and links != sess:
            r = self._req("POST", links, json={"UserName": self.user, "Password": self.pw})
            if r.status_code in (200, 201):
                self._token = r.headers.get("X-Auth-Token")
                self._session_uri = r.headers.get("Location")
                return bool(self._token)
        return False

    def connect(self):
        self._check_fp()
        self._get_root()
        if self.user and not self._token:
            self._login()
        return self

    def close(self):
        try:
            if self._token and self._session_uri:
                self._req("DELETE", self._session_uri)
        except Exception:
            pass
        try:
            self._c.close()
        except Exception:
            pass

    def __enter__(self):
        return self.connect()

    def __exit__(self, *a):
        self.close()

    # ------------------------------------------------ Entdeckung
    def _get_root(self):
        if self._root is None:
            self._root = self._get("/redfish/v1/")
        return self._root

    @staticmethod
    def _members(coll):
        return [m.get("@odata.id") for m in (coll.get("Members") or []) if m.get("@odata.id")]

    def _systems(self):
        if self._sysmembers is None:
            root = self._get_root()
            col = (root.get("Systems") or {}).get("@odata.id") or "/redfish/v1/Systems"
            self._sysmembers = self._members(self._get(col))
        return self._sysmembers

    def _system(self, idx=0):
        m = self._systems()
        if not m:
            raise RedfishError("No ComputerSystem found")
        return self._get(m[idx])

    def _chassis(self):
        root = self._get_root()
        col = (root.get("Chassis") or {}).get("@odata.id") or "/redfish/v1/Chassis"
        return [self._get(u) for u in self._members(self._get(col))]

    def _managers(self):
        root = self._get_root()
        col = (root.get("Managers") or {}).get("@odata.id") or "/redfish/v1/Managers"
        return [self._get(u) for u in self._members(self._get(col))]

    def vendor(self) -> str:
        root = self._get_root()
        oem = root.get("Oem") or {}
        if "Hpe" in oem or "Hp" in oem:
            return "HPE iLO"
        if "Dell" in oem:
            return "Dell iDRAC"
        v = (root.get("Vendor") or root.get("OemName") or "").strip()
        return v or "Redfish"

    # ================================================ operations
    def summary(self) -> dict:
        root = self._get_root()
        sys = self._system()
        mgrs = self._managers()
        mgr = mgrs[0] if mgrs else {}
        st = sys.get("Status") or {}
        boot = sys.get("Boot") or {}
        out = {
            "vendor": self.vendor(),
            "redfish_version": root.get("RedfishVersion"),
            "name": sys.get("HostName") or sys.get("Name"),
            "model": sys.get("Model"),
            "manufacturer": sys.get("Manufacturer"),
            "serial": sys.get("SerialNumber") or sys.get("SKU"),
            "asset_tag": sys.get("AssetTag"),
            "power_state": sys.get("PowerState"),
            "health": st.get("Health"),
            "health_rollup": st.get("HealthRollup"),
            "state": st.get("State"),
            "bios_version": sys.get("BiosVersion"),
            "indicator_led": sys.get("IndicatorLED") or sys.get("LocationIndicatorActive"),
            "boot": {"enabled": boot.get("BootSourceOverrideEnabled"),
                     "target": boot.get("BootSourceOverrideTarget"),
                     "mode": boot.get("BootSourceOverrideMode"),
                     "targets": boot.get("BootSourceOverrideTarget@Redfish.AllowableValues") or BOOT_TARGETS},
            "reset_types": (sys.get("Actions", {}).get("#ComputerSystem.Reset", {})
                            .get("ResetType@Redfish.AllowableValues") or RESET_TYPES),
            "manager": {"firmware": mgr.get("FirmwareVersion"), "model": mgr.get("Model"),
                        "type": mgr.get("ManagerType"), "name": mgr.get("Name")},
            "power_on_hint": "On",
        }
        proc = sys.get("ProcessorSummary") or {}
        mem = sys.get("MemorySummary") or {}
        out["cpu"] = {"count": proc.get("Count"), "model": proc.get("Model"),
                      "cores": proc.get("CoreCount"), "health": (proc.get("Status") or {}).get("Health")}
        out["memory_gib"] = mem.get("TotalSystemMemoryGiB")
        return out

    def _reset_action(self, sys):
        act = (sys.get("Actions") or {}).get("#ComputerSystem.Reset") or {}
        return act.get("target") or (self._systems()[0].rstrip("/") + "/Actions/ComputerSystem.Reset")

    def power(self, reset_type: str) -> dict:
        if reset_type not in RESET_TYPES:
            raise RedfishError(f"Unbekannter Power-Befehl {reset_type}")
        sys = self._system()
        target = self._reset_action(sys)
        self._ok(self._req("POST", target, json={"ResetType": reset_type}), f"Power {reset_type}")
        return {"ok": True, "reset_type": reset_type}

    def set_boot(self, target: str, mode: str = "", persistent: bool = False) -> dict:
        boot = {"BootSourceOverrideTarget": target,
                "BootSourceOverrideEnabled": "Continuous" if persistent else ("Disabled" if target == "None" else "Once")}
        if mode:
            boot["BootSourceOverrideMode"] = mode
        sysuri = self._systems()[0]
        self._ok(self._req("PATCH", sysuri, json={"Boot": boot}), "Set boot order")
        return {"ok": True, "boot": boot}

    def identify(self, on: bool) -> dict:
        sysuri = self._systems()[0]
        # Neuere Schemas: LocationIndicatorActive (bool); aeltere: IndicatorLED (Lit/Off)
        r = self._req("PATCH", sysuri, json={"LocationIndicatorActive": bool(on)})
        if r.status_code >= 400:
            r = self._req("PATCH", sysuri, json={"IndicatorLED": "Lit" if on else "Off"})
        self._ok(r, "Toggle identify LED")
        return {"ok": True, "on": on}

    def _iter_sensors(self):
        temps, fans, psus, power = [], [], [], []
        for ch in self._chassis():
            th = (ch.get("Thermal") or {}).get("@odata.id")
            if th:
                t = self._get(th)
                for x in t.get("Temperatures") or []:
                    temps.append({"name": x.get("Name"), "reading": x.get("ReadingCelsius"),
                                  "max": x.get("UpperThresholdCritical") or x.get("MaxReadingRangeTemp"),
                                  "health": (x.get("Status") or {}).get("Health"), "state": (x.get("Status") or {}).get("State")})
                for x in t.get("Fans") or []:
                    fans.append({"name": x.get("Name") or x.get("FanName"),
                                 "reading": x.get("Reading") if x.get("Reading") is not None else x.get("CurrentReading"),
                                 "units": x.get("ReadingUnits") or "RPM", "health": (x.get("Status") or {}).get("Health")})
            pw = (ch.get("Power") or {}).get("@odata.id")
            if pw:
                p = self._get(pw)
                for x in p.get("PowerSupplies") or []:
                    psus.append({"name": x.get("Name"), "model": x.get("Model"), "serial": x.get("SerialNumber"),
                                 "watts": x.get("PowerOutputWatts") or x.get("LastPowerOutputWatts"),
                                 "capacity": x.get("PowerCapacityWatts"), "health": (x.get("Status") or {}).get("Health"),
                                 "state": (x.get("Status") or {}).get("State")})
                for x in p.get("PowerControl") or []:
                    power.append({"name": x.get("Name"), "consumed": x.get("PowerConsumedWatts"),
                                  "limit": (x.get("PowerLimit") or {}).get("LimitInWatts"),
                                  "avg": (x.get("PowerMetrics") or {}).get("AverageConsumedWatts")})
        return {"temperatures": temps, "fans": fans, "power_supplies": psus, "power": power}

    def sensors(self) -> dict:
        return self._iter_sensors()

    def inventory(self) -> dict:
        sys = self._system()
        out = {"system": self.summary(), "processors": [], "memory": [], "storage": [], "network": []}
        for key, uri in (("Processors", (sys.get("Processors") or {}).get("@odata.id")),
                         ("Memory", (sys.get("Memory") or {}).get("@odata.id"))):
            if not uri:
                continue
            for m in self._members(self._get(uri)):
                d = self._get(m)
                if key == "Processors":
                    out["processors"].append({"name": d.get("Id"), "model": d.get("Model"),
                                              "cores": d.get("TotalCores"), "threads": d.get("TotalThreads"),
                                              "speed_mhz": d.get("MaxSpeedMHz"), "socket": d.get("Socket"),
                                              "health": (d.get("Status") or {}).get("Health")})
                else:
                    if d.get("Status", {}).get("State") == "Absent":
                        continue
                    out["memory"].append({"name": d.get("Id"), "capacity_mib": d.get("CapacityMiB"),
                                          "type": d.get("MemoryDeviceType"), "speed": d.get("OperatingSpeedMhz"),
                                          "manufacturer": d.get("Manufacturer"), "part": d.get("PartNumber"),
                                          "health": (d.get("Status") or {}).get("Health")})
        # drives via Storage and/or SimpleStorage
        for col_key in ("Storage", "SimpleStorage"):
            uri = (sys.get(col_key) or {}).get("@odata.id")
            if not uri:
                continue
            for m in self._members(self._get(uri)):
                d = self._get(m)
                for dev in (d.get("Devices") or []):
                    out["storage"].append({"name": dev.get("Name"), "model": dev.get("Model"),
                                           "capacity": dev.get("CapacityBytes"), "health": (dev.get("Status") or {}).get("Health")})
                for dref in (d.get("Drives") or []):
                    du = dref.get("@odata.id")
                    if du:
                        dd = self._get(du)
                        out["storage"].append({"name": dd.get("Name") or dd.get("Id"), "model": dd.get("Model"),
                                               "capacity": dd.get("CapacityBytes"), "type": dd.get("MediaType"),
                                               "proto": dd.get("Protocol"), "serial": dd.get("SerialNumber"),
                                               "health": (dd.get("Status") or {}).get("Health")})
        nic = (sys.get("EthernetInterfaces") or {}).get("@odata.id")
        if nic:
            for m in self._members(self._get(nic)):
                d = self._get(m)
                out["network"].append({"name": d.get("Id"), "mac": d.get("MACAddress" ) or d.get("PermanentMACAddress"),
                                       "speed_mbps": d.get("SpeedMbps"), "link": d.get("LinkStatus"),
                                       "hostname": d.get("HostName")})
        return out

    # ---------------- logs
    def _log_services(self):
        out = []
        srcs = [self._get(sysuri) for sysuri in self._systems()]
        srcs += self._managers()
        for parent in srcs:
            ls = (parent.get("LogServices") or {}).get("@odata.id")
            if not ls:
                continue
            for m in self._members(self._get(ls)):
                d = self._get(m)
                out.append({"id": d.get("Id") or m.rsplit("/", 1)[-1], "name": d.get("Name"),
                            "uri": m, "entries": (d.get("Entries") or {}).get("@odata.id"),
                            "clearable": "#LogService.ClearLog" in (d.get("Actions") or {})})
        return out

    def log_services(self) -> dict:
        return {"services": [{k: v for k, v in s.items() if k != "entries" or True} for s in self._log_services()]}

    def log_entries(self, uri: str, limit: int = 200) -> dict:
        svc = next((s for s in self._log_services() if s["uri"] == uri or s["id"] == uri), None)
        if not svc:
            raise RedfishError("Log service not found")
        ent = svc.get("entries")
        if not ent:
            return {"service": svc, "entries": []}
        data = self._get(ent + (("?$top=%d" % limit) if "?" not in ent else ""))
        rows = []
        for e in (data.get("Members") or [])[:limit]:
            rows.append({"id": e.get("Id"), "created": e.get("Created"), "severity": e.get("Severity"),
                         "type": e.get("EntryType"), "message": e.get("Message"),
                         "sensor": e.get("SensorType") or (e.get("Oem") or {}),
                         "resolved": e.get("Resolved")})
        return {"service": {k: v for k, v in svc.items()}, "entries": rows,
                "total": data.get("Members@odata.count")}

    def log_clear(self, uri: str) -> dict:
        svc = next((s for s in self._log_services() if s["uri"] == uri or s["id"] == uri), None)
        if not svc:
            raise RedfishError("Log service not found")
        d = self._get(svc["uri"])
        act = (d.get("Actions") or {}).get("#LogService.ClearLog") or {}
        target = act.get("target") or (svc["uri"].rstrip("/") + "/Actions/LogService.ClearLog")
        body = {}
        allow = act.get("ClearType@Redfish.AllowableValues")
        if allow:
            body["ClearType"] = allow[0]
        self._ok(self._req("POST", target, json=body), "Clear log")
        return {"ok": True}

    # ---------------- virtual media
    def _vm_items(self):
        out = []
        for mgr in self._managers():
            vm = (mgr.get("VirtualMedia") or {}).get("@odata.id")
            if not vm:
                continue
            for m in self._members(self._get(vm)):
                d = self._get(m)
                out.append({"uri": m, "id": d.get("Id"), "name": d.get("Name"),
                            "types": d.get("MediaTypes") or [], "inserted": d.get("Inserted"),
                            "image": d.get("Image"), "imagename": d.get("ImageName"),
                            "write_protected": d.get("WriteProtected"), "connected": d.get("ConnectedVia"),
                            "actions": list((d.get("Actions") or {}).keys())})
        return out

    def virtual_media(self) -> dict:
        return {"media": self._vm_items()}

    def vm_insert(self, uri: str, image: str, write_protected: bool = True) -> dict:
        d = self._get(uri)
        act = (d.get("Actions") or {}).get("#VirtualMedia.InsertMedia")
        body = {"Image": image, "Inserted": True, "WriteProtected": bool(write_protected)}
        if act and act.get("target"):
            self._ok(self._req("POST", act["target"], json=body), "Mount ISO")
        else:
            self._ok(self._req("PATCH", uri, json={"Image": image, "Inserted": True,
                                                   "WriteProtected": bool(write_protected)}), "Mount ISO")
        return {"ok": True}

    def vm_eject(self, uri: str) -> dict:
        d = self._get(uri)
        act = (d.get("Actions") or {}).get("#VirtualMedia.EjectMedia")
        if act and act.get("target"):
            self._ok(self._req("POST", act["target"], json={}), "Eject media")
        else:
            self._ok(self._req("PATCH", uri, json={"Image": None, "Inserted": False}), "Eject media")
        return {"ok": True}

    # ---------------- firmware
    def firmware(self) -> dict:
        root = self._get_root()
        out = []
        us = (root.get("UpdateService") or {}).get("@odata.id")
        if us:
            u = self._get(us)
            inv = (u.get("FirmwareInventory") or {}).get("@odata.id")
            if inv:
                for m in self._members(self._get(inv)):
                    d = self._get(m)
                    out.append({"name": d.get("Name"), "version": d.get("Version"),
                                "updateable": d.get("Updateable"), "state": (d.get("Status") or {}).get("State")})
        if not out:
            for mgr in self._managers():
                out.append({"name": mgr.get("Name") or "BMC", "version": mgr.get("FirmwareVersion")})
        return {"firmware": out}

    # ---------------- manager (BMC) reset / console info
    def manager_reset(self, reset_type: str = "GracefulRestart") -> dict:
        col = (self._get_root().get("Managers") or {}).get("@odata.id") or "/redfish/v1/Managers"
        for muri in self._members(self._get(col)):
            d = self._get(muri)
            act = (d.get("Actions") or {}).get("#Manager.Reset") or {}
            target = act.get("target") or (muri.rstrip("/") + "/Actions/Manager.Reset")
            self._ok(self._req("POST", target, json={"ResetType": reset_type}), "BMC reset")
            return {"ok": True}
        raise RedfishError("No manager found")

    def console_info(self) -> dict:
        out = {"graphical": None, "serial": None, "vendor": self.vendor()}
        for mgr in self._managers():
            g = mgr.get("GraphicalConsole") or {}
            if g:
                out["graphical"] = {"enabled": g.get("ServiceEnabled"),
                                    "max": g.get("MaxConcurrentSessions"),
                                    "types": g.get("ConnectTypesSupported")}
            s = mgr.get("SerialConsole") or {}
            if s:
                out["serial"] = {"enabled": s.get("ServiceEnabled"), "types": s.get("ConnectTypesSupported")}
        return out

    # ---------------- raw (any Redfish call)
    def raw(self, method: str, path: str, body=None) -> dict:
        method = (method or "GET").upper()
        if method not in ("GET", "POST", "PATCH", "PUT", "DELETE"):
            raise RedfishError("Method not allowed")
        if not str(path).startswith("/redfish"):
            raise RedfishError("Path must start with /redfish")
        r = self._req(method, path, json=body if body not in (None, "") else None)
        data = self._json(r)
        if r.status_code >= 400:
            self._ok(r, f"{method} {path}")
        return {"status": r.status_code, "data": data}

    # ---------------- dispatcher
    OPS = ("summary", "power", "set_boot", "identify", "sensors", "inventory", "log_services",
           "log_entries", "log_clear", "virtual_media", "vm_insert", "vm_eject", "firmware",
           "manager_reset", "console_info", "raw")

    def dispatch(self, op: str, args: dict):
        if op not in self.OPS:
            raise RedfishError(f"Unknown operation {op}")
        return getattr(self, op)(**(args or {}))
