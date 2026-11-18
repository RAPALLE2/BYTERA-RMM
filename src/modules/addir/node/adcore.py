"""
adcore.py (module "addir") - DNS and Group Policy WITHOUT an agent/PowerShell.

The same file runs in the BACKEND (LDAP + SYSVOL via SMB) and in the NODE part on
the domain controller (LDAP locally + SYSVOL as a local folder). Standard library
+ ldap3 only.

DNS:  AD-integrated zones are stored as dnsZone/dnsNode objects in AD; each
      record is a binary dnsRecord value (MS-DNSP 2.3.2.2). The DNS service
      picks up changes at the next AD sync (default ~3 minutes).
GPO:  groupPolicyContainer in AD (name, status, version, extensions, links
      via gPLink) + files in SYSVOL (GPT.INI, Registry.pol, GptTmpl.inf).
      All "administrative templates" are read DYNAMICALLY from the ADMX/ADML
      files of the domain (central store in SYSVOL, otherwise
      C:\\Windows\\PolicyDefinitions on the DC) - nothing is hard-coded.
"""

from __future__ import annotations

import base64
import re
import struct
import time
import uuid
import xml.etree.ElementTree as ET

# ======================================================================
# Dateisystem-Abstraktion (SYSVOL). Pfade relativ, mit "/" getrennt.
# ======================================================================


class CoreError(RuntimeError):
    pass


class LocalFs:
    """SYSVOL als lokaler Ordner (Node auf dem DC)."""

    def __init__(self, root: str):
        import os
        self.os, self.root = os, root

    def _p(self, rel):
        parts = [p for p in rel.replace("\\", "/").split("/") if p and p not in (".", "..")]
        return self.os.path.join(self.root, *parts)

    def read(self, rel):
        try:
            with open(self._p(rel), "rb") as fh:
                return fh.read()
        except OSError:
            return None

    def write(self, rel, data: bytes):
        p = self._p(rel)
        self.os.makedirs(self.os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data)

    def listdir(self, rel):
        p = self._p(rel)
        try:
            return [(n, self.os.path.isdir(self.os.path.join(p, n))) for n in self.os.listdir(p)]
        except OSError:
            return []

    def makedirs(self, rel):
        self.os.makedirs(self._p(rel), exist_ok=True)

    def rmtree(self, rel):
        import shutil
        shutil.rmtree(self._p(rel), ignore_errors=True)


class SmbFs:
    """SYSVOL ueber SMB (Backend). root z.B. \\\\dc01\\SYSVOL\\corp.local"""

    def __init__(self, root: str, server: str, user: str, password: str):
        try:
            import smbclient
        except ImportError:
            raise CoreError("Python-Paket 'smbprotocol' fehlt (Modul neu aktivieren installiert es)")
        self.sc, self.root = smbclient, root.rstrip("\\")
        smbclient.register_session(server, username=user or None, password=password or None)

    def _p(self, rel):
        parts = [p for p in rel.replace("\\", "/").split("/") if p and p not in (".", "..")]
        return self.root + ("\\" + "\\".join(parts) if parts else "")

    def read(self, rel):
        try:
            with self.sc.open_file(self._p(rel), mode="rb") as fh:
                return fh.read()
        except OSError:
            return None

    def write(self, rel, data: bytes):
        parent = rel.replace("\\", "/").rsplit("/", 1)[0] if "/" in rel.replace("\\", "/") else ""
        if parent:
            self.makedirs(parent)
        with self.sc.open_file(self._p(rel), mode="wb") as fh:
            fh.write(data)

    def listdir(self, rel):
        try:
            return [(e.name, e.is_dir()) for e in self.sc.scandir(self._p(rel))]
        except OSError:
            return []

    def makedirs(self, rel):
        self.sc.makedirs(self._p(rel), exist_ok=True)

    def rmtree(self, rel):
        p = self._p(rel)
        try:
            for root, dirs, files in list(self.sc.walk(p, topdown=False)):
                for f in files:
                    self.sc.remove(root + "\\" + f)
                for d in dirs:
                    self.sc.rmdir(root + "\\" + d)
            self.sc.rmdir(p)
        except OSError:
            pass


# ======================================================================
# DNS: dnsRecord (MS-DNSP 2.3.2.2)
# ======================================================================
DNS_TYPES = {1: "A", 2: "NS", 5: "CNAME", 6: "SOA", 12: "PTR", 15: "MX", 16: "TXT", 28: "AAAA", 33: "SRV"}
DNS_TYPE_ID = {v: k for k, v in DNS_TYPES.items()}
EDITABLE_TYPES = ("A", "AAAA", "CNAME", "NS", "PTR", "MX", "TXT", "SRV")


def _name_enc(fqdn: str) -> bytes:
    """DNS_COUNT_NAME: Laenge, Label-Anzahl, Labels (len+bytes), 0-Terminator."""
    labels = [l for l in fqdn.strip().rstrip(".").split(".") if l]
    raw = b"".join(bytes([len(l.encode())]) + l.encode() for l in labels) + b"\x00"
    return bytes([len(raw), len(labels)]) + raw


def _name_dec(b: bytes, off: int = 0) -> tuple[str, int]:
    length, count = b[off], b[off + 1]
    raw = b[off + 2: off + 2 + length]
    labels, i = [], 0
    for _ in range(count):
        n = raw[i]
        labels.append(raw[i + 1: i + 1 + n].decode("utf-8", "replace"))
        i += n + 1
    return ".".join(labels) + ".", off + 2 + length


def dns_encode(rtype: str, value: dict, ttl: int, serial: int) -> bytes:
    t = rtype.upper()
    if t == "A":
        import ipaddress
        data = ipaddress.IPv4Address(value["address"].strip()).packed
    elif t == "AAAA":
        import ipaddress
        data = ipaddress.IPv6Address(value["address"].strip()).packed
    elif t in ("CNAME", "NS", "PTR"):
        data = _name_enc(value["target"])
    elif t == "MX":
        data = struct.pack(">H", int(value.get("preference", 10))) + _name_enc(value["target"])
    elif t == "SRV":
        data = struct.pack(">HHH", int(value.get("priority", 0)), int(value.get("weight", 0)),
                           int(value["port"])) + _name_enc(value["target"])
    elif t == "TXT":
        txt = str(value.get("text") or "").encode("utf-8")
        chunks = [txt[i:i + 255] for i in range(0, len(txt), 255)] or [b""]
        data = b"".join(bytes([len(c)]) + c for c in chunks)
    else:
        raise CoreError(f"Record type {rtype} is not supported")
    head = struct.pack("<HHBBHI", len(data), DNS_TYPE_ID[t], 5, 0xF0, 0, serial)
    head += struct.pack(">I", int(ttl)) + struct.pack("<II", 0, 0)   # TTL big-endian, Reserved, TimeStamp=0 (statisch)
    return head + data


def dns_decode(raw: bytes) -> dict:
    dlen, typ, _ver, _rank, _flags, serial = struct.unpack("<HHBBHI", raw[:12])
    ttl = struct.unpack(">I", raw[12:16])[0]
    stamp = struct.unpack("<I", raw[20:24])[0]
    d = raw[24:24 + dlen]
    t = DNS_TYPES.get(typ, f"TYPE{typ}")
    v: dict = {}
    try:
        if t == "A":
            v["address"] = ".".join(str(x) for x in d[:4])
        elif t == "AAAA":
            import ipaddress
            v["address"] = str(ipaddress.IPv6Address(d[:16]))
        elif t in ("CNAME", "NS", "PTR"):
            v["target"] = _name_dec(d)[0]
        elif t == "MX":
            v["preference"] = struct.unpack(">H", d[:2])[0]
            v["target"] = _name_dec(d, 2)[0]
        elif t == "SRV":
            v["priority"], v["weight"], v["port"] = struct.unpack(">HHH", d[:6])
            v["target"] = _name_dec(d, 6)[0]
        elif t == "TXT":
            parts, i = [], 0
            while i < len(d):
                n = d[i]
                parts.append(d[i + 1:i + 1 + n].decode("utf-8", "replace"))
                i += n + 1
            v["text"] = "".join(parts)
        elif t == "SOA":
            v["serial"], v["refresh"], v["retry"], v["expire"], v["minimum"] = struct.unpack(">IIIII", d[:20])
            v["primary"], off = _name_dec(d, 20)
            v["admin"] = _name_dec(d, off)[0]
    except Exception:
        v["raw"] = d.hex()
    return {"type": t, "ttl": ttl, "serial": serial, "static": stamp == 0,
            "value": v, "rid": base64.b64encode(raw).decode()}


# ======================================================================
# Registry.pol (PReg)
# ======================================================================
REG = {"REG_SZ": 1, "REG_EXPAND_SZ": 2, "REG_BINARY": 3, "REG_DWORD": 4, "REG_MULTI_SZ": 7, "REG_QWORD": 11}
REG_NAME = {v: k for k, v in REG.items()}


def pol_parse(data: bytes | None) -> list[dict]:
    if not data:
        return []
    if data[:8] != b"PReg\x01\x00\x00\x00":
        raise CoreError("Registry.pol hat ein unbekanntes Format")
    out, i = [], 8
    u = lambda s: s.encode("utf-16-le")
    while i < len(data):
        if data[i:i + 2] != u("["):
            break
        i += 2
        j = data.index(b"\x00\x00", i)
        while (j - i) % 2:
            j = data.index(b"\x00\x00", j + 1)
        key = data[i:j].decode("utf-16-le")
        i = j + 2 + 2                                   # NUL + ';'
        j = data.index(b"\x00\x00", i)
        while (j - i) % 2:
            j = data.index(b"\x00\x00", j + 1)
        name = data[i:j].decode("utf-16-le")
        i = j + 2 + 2
        typ = struct.unpack("<I", data[i:i + 4])[0]
        i += 4 + 2
        size = struct.unpack("<I", data[i:i + 4])[0]
        i += 4 + 2
        raw = data[i:i + size]
        i += size + 2                                   # Daten + ']'
        out.append({"key": key, "name": name, "type": REG_NAME.get(typ, str(typ)),
                    "value": _reg_value(typ, raw)})
    return out


def _reg_value(typ: int, raw: bytes):
    if typ in (1, 2):
        return raw.decode("utf-16-le", "replace").rstrip("\x00")
    if typ == 4:
        return struct.unpack("<I", raw[:4])[0] if len(raw) >= 4 else 0
    if typ == 11:
        return struct.unpack("<Q", raw[:8])[0] if len(raw) >= 8 else 0
    if typ == 7:
        return [s for s in raw.decode("utf-16-le", "replace").split("\x00") if s]
    return raw.hex()


def _reg_raw(typ: str, value) -> bytes:
    t = REG[typ]
    if t in (1, 2):
        return (str(value) + "\x00").encode("utf-16-le")
    if t == 4:
        return struct.pack("<I", int(value) & 0xFFFFFFFF)
    if t == 11:
        return struct.pack("<Q", int(value))
    if t == 7:
        vals = value if isinstance(value, list) else str(value).split("\n")
        return ("\x00".join(v for v in vals if v) + "\x00\x00").encode("utf-16-le")
    return bytes.fromhex(str(value))


def pol_build(entries: list[dict]) -> bytes:
    u = lambda s: s.encode("utf-16-le")
    out = bytearray(b"PReg\x01\x00\x00\x00")
    for e in entries:
        raw = _reg_raw(e["type"], e["value"])
        out += u("[") + u(e["key"]) + b"\x00\x00" + u(";") + u(e["name"]) + b"\x00\x00" + u(";")
        out += struct.pack("<I", REG[e["type"]]) + u(";") + struct.pack("<I", len(raw)) + u(";")
        out += raw + u("]")
    return bytes(out)


# ======================================================================
# ADMX/ADML - alle Administrativen Vorlagen dynamisch
# ======================================================================
def _strip_ns(root):
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _val_of(el):
    """<value><decimal value=1/></value> | <string>x</string> | <delete/>"""
    if el is None:
        return None
    for c in el:
        if c.tag == "decimal":
            return ("REG_DWORD", int(c.get("value")))
        if c.tag == "longDecimal":
            return ("REG_QWORD", int(c.get("value")))
        if c.tag == "string":
            return ("REG_SZ", c.text or "")
        if c.tag == "delete":
            return ("DELETE", None)
    return None


def _list_of(el, dkey):
    out = []
    if el is None:
        return out
    for it in el.findall("item"):
        v = _val_of(it.find("value"))
        if v:
            out.append({"key": it.get("key") or dkey, "name": it.get("valueName") or "", "val": v})
    return out


class AdmxCatalog:
    def __init__(self):
        self.categories: dict = {}       # id -> {id,name,parent,explain}
        self.policies: dict = {}         # id -> definition
        self.errors: list = []
        self.lang = ""
        self.source = ""

    @staticmethod
    def load(fs, fallback_fs=None, langs=("de-DE", "en-US")):
        cat = AdmxCatalog()
        for src, f in (("Central Store (SYSVOL)", fs), ("PolicyDefinitions des DC", fallback_fs)):
            if f is None:
                continue
            base = "Policies/PolicyDefinitions" if f is fs else ""
            files = [n for n, isdir in f.listdir(base) if not isdir and n.lower().endswith(".admx")]
            if not files:
                continue
            cat.source = src
            dirs = {n.lower(): n for n, isdir in f.listdir(base) if isdir}
            lang = next((dirs[l.lower()] for l in langs if l.lower() in dirs),
                        next(iter(dirs.values()), ""))
            cat.lang = lang
            for name in sorted(files):
                try:
                    admx = f.read(f"{base}/{name}".lstrip("/"))
                    adml = f.read(f"{base}/{lang}/{name[:-5]}.adml".lstrip("/")) if lang else None
                    cat._add(name[:-5], admx, adml)
                except Exception as exc:
                    cat.errors.append(f"{name}: {exc}")
            break
        if not cat.policies:
            raise CoreError("Keine ADMX-Vorlagen gefunden (weder Central Store in SYSVOL noch "
                            "C:\\Windows\\PolicyDefinitions des DC erreichbar)")
        for c in cat.categories.values():       # Eltern-Referenzen aufloesen
            if c["parent"] and c["parent"] not in cat.categories:
                c["parent"] = cat._resolve(c["parent"], c["_ns"])
        for p in cat.policies.values():
            if p["cat"] and p["cat"] not in cat.categories:
                p["cat"] = cat._resolve(p["cat"], p["_ns"])
        return cat

    def _resolve(self, ref, ns_map):
        if ":" in ref:
            pre, nm = ref.split(":", 1)
            ns = ns_map.get(pre, pre)
            for cid in (f"{ns}:{nm}",):
                if cid in self.categories:
                    return cid
            for cid in self.categories:                 # fallback: name only
                if cid.endswith(":" + nm):
                    return cid
        return ref

    def _add(self, fname, admx: bytes, adml: bytes | None):
        root = _strip_ns(ET.fromstring(admx))
        strings, pres = {}, {}
        if adml:
            lr = _strip_ns(ET.fromstring(adml))
            for s in lr.iter("string"):
                strings[s.get("id")] = s.text or ""
            for p in lr.iter("presentation"):
                pres[p.get("id")] = p
        S = lambda ref: (strings.get(ref[9:-1], ref) if ref and ref.startswith("$(string.") else (ref or ""))
        target = root.find("policyNamespaces/target")
        ns = target.get("namespace") if target is not None else fname
        prefix = target.get("prefix") if target is not None else fname
        ns_map = {prefix: ns}
        for us in root.findall("policyNamespaces/using"):
            ns_map[us.get("prefix")] = us.get("namespace")

        def ref_id(ref):
            if not ref:
                return ""
            if ":" in ref:
                pre, nm = ref.split(":", 1)
                return f"{ns_map.get(pre, pre)}:{nm}"
            return f"{ns}:{ref}"

        for c in root.findall("categories/category"):
            cid = f"{ns}:{c.get('name')}"
            pc = c.find("parentCategory")
            self.categories[cid] = {"id": cid, "name": S(c.get("displayName")),
                                    "parent": ref_id(pc.get("ref")) if pc is not None else "",
                                    "explain": S(c.get("explainText")), "_ns": ns_map}
        for p in root.findall("policies/policy"):
            pid = f"{ns}:{p.get('name')}"
            key = p.get("key") or ""
            pc = p.find("parentCategory")
            elements = []
            els = p.find("elements")
            if els is not None:
                for e in els:
                    d = {"kind": e.tag, "id": e.get("id"), "key": e.get("key") or key,
                         "name": e.get("valueName") or "", "required": e.get("required") == "true"}
                    if e.tag == "boolean":
                        d["true"] = _val_of(e.find("trueValue")) or ("REG_DWORD", 1)
                        d["false"] = _val_of(e.find("falseValue")) or ("REG_DWORD", 0)
                        d["trueList"] = _list_of(e.find("trueList"), d["key"])
                        d["falseList"] = _list_of(e.find("falseList"), d["key"])
                    elif e.tag == "decimal":
                        d.update(min=int(e.get("minValue") or 0), max=int(e.get("maxValue") or 9999),
                                 text=e.get("storeAsText") == "true")
                    elif e.tag == "longDecimal":
                        d.update(min=int(e.get("minValue") or 0), max=int(e.get("maxValue") or 2 ** 63 - 1),
                                 text=e.get("storeAsText") == "true", long=True)
                        d["kind"] = "decimal"
                    elif e.tag == "text":
                        d.update(expand=e.get("expandable") == "true", maxlen=int(e.get("maxLength") or 1023))
                    elif e.tag == "enum":
                        d["items"] = [{"label": S(it.get("displayName")), "val": _val_of(it.find("value")),
                                       "list": _list_of(it.find("valueList"), d["key"])}
                                      for it in e.findall("item")]
                    elif e.tag == "list":
                        d.update(prefix=e.get("valuePrefix") or "", additive=e.get("additive") == "true",
                                 explicit=e.get("explicitValue") == "true", expand=e.get("expandable") == "true")
                    elif e.tag == "multiText":
                        pass
                    else:
                        continue
                    elements.append(d)
            # labels from the ADML presentation
            labels, order = {}, []
            pref = p.get("presentation") or ""
            pnode = pres.get(pref[15:-1]) if pref.startswith("$(presentation.") else None
            if pnode is not None:
                for c in pnode:
                    rid = c.get("refId")
                    label = (c.find("label").text if c.find("label") is not None else c.text) or ""
                    if rid:
                        labels[rid] = {"label": label.strip(), "default": c.get("defaultValue") or c.get("defaultItem")
                                       or ("true" if c.get("defaultChecked") == "true" else None)}
                    elif c.tag == "text" and (c.text or "").strip():
                        order.append({"kind": "label", "label": c.text.strip()})
            for d in elements:
                d.update(labels.get(d["id"], {"label": d["id"], "default": None}))
            self.policies[pid] = {
                "id": pid, "file": fname, "name": S(p.get("displayName")), "explain": S(p.get("explainText")),
                "class": p.get("class") or "Both", "key": key, "valueName": p.get("valueName") or "",
                "cat": ref_id(pc.get("ref")) if pc is not None else "",
                "enabledValue": _val_of(p.find("enabledValue")), "disabledValue": _val_of(p.find("disabledValue")),
                "enabledList": _list_of(p.find("enabledList"), key),
                "disabledList": _list_of(p.find("disabledList"), key),
                "elements": elements, "labels": order,
                "supported": S(p.find("supportedOn").get("ref")) if p.find("supportedOn") is not None else "",
                "_ns": ns_map}

    # ---------- Zustand lesen ----------
    @staticmethod
    def _find(entries, key, name):
        k, n = key.lower(), name.lower()
        for e in entries:
            if e["key"].lower() == k and e["name"].lower() == n:
                return e
        return None

    @staticmethod
    def _eq(entry, val):
        if not entry or not val or val[0] == "DELETE":
            return False
        a, b = entry["value"], val[1]
        return str(a).lower() == str(b).lower()

    def state(self, pol: dict, entries: list) -> dict:
        """{state: enabled|disabled|not_configured, values: {element_id: wert}}"""
        f = lambda k, n: self._find(entries, k, n)
        st = "not_configured"
        if pol["valueName"]:
            e = f(pol["key"], pol["valueName"])
            if e:
                ev = pol["enabledValue"] or ("REG_DWORD", 1)
                if self._eq(e, ev):
                    st = "enabled"
                elif pol["disabledValue"] and self._eq(e, pol["disabledValue"]):
                    st = "disabled"
                else:
                    st = "enabled"
            elif f(pol["key"], "**del." + pol["valueName"]):
                st = "disabled"
        if st == "not_configured" and pol["enabledList"] and all(
                self._eq(f(i["key"], i["name"]), i["val"]) for i in pol["enabledList"]):
            st = "enabled"
        if st == "not_configured" and pol["disabledList"] and all(
                self._eq(f(i["key"], i["name"]), i["val"]) for i in pol["disabledList"]):
            st = "disabled"
        values = {}
        for el in pol["elements"]:
            if el["kind"] == "list":
                items = [e for e in entries if e["key"].lower() == el["key"].lower()
                         and not e["name"].startswith("**")]
                if items:
                    values[el["id"]] = ([{"name": e["name"], "value": e["value"]} for e in items]
                                        if el["explicit"] else [str(e["value"]) for e in items])
                continue
            e = f(el["key"], el["name"])
            if e is None:
                if f(el["key"], "**del." + el["name"]) and st == "not_configured":
                    st = "disabled"
                continue
            if el["kind"] == "boolean":
                values[el["id"]] = self._eq(e, el["true"])
            elif el["kind"] == "enum":
                idx = next((i for i, it in enumerate(el["items"]) if self._eq(e, it["val"])), None)
                values[el["id"]] = idx
            else:
                values[el["id"]] = e["value"]
            if st == "not_configured":
                st = "enabled"
        return {"state": st, "values": values}

    # ---------- Zustand schreiben ----------
    def touched(self, pol) -> list[tuple]:
        """All (key, name) pairs that this policy can occupy."""
        pairs = []
        if pol["valueName"]:
            pairs.append((pol["key"], pol["valueName"]))
        for i in pol["enabledList"] + pol["disabledList"]:
            pairs.append((i["key"], i["name"]))
        for el in pol["elements"]:
            if el["kind"] == "list":
                pairs.append((el["key"], "*"))
            else:
                pairs.append((el["key"], el["name"]))
            for it in el.get("items") or []:
                pairs += [(x["key"], x["name"]) for x in it["list"]]
            for x in (el.get("trueList") or []) + (el.get("falseList") or []):
                pairs.append((x["key"], x["name"]))
        return pairs

    def apply(self, pol, entries: list, state: str, values: dict) -> list:
        """Returns the new list of entries (other policies remain untouched)."""
        drop = set()
        wild = set()
        for k, n in self.touched(pol):
            if n == "*":
                wild.add(k.lower())
            else:
                drop.add((k.lower(), n.lower()))
                drop.add((k.lower(), ("**del." + n).lower()))
        out = [e for e in entries
               if (e["key"].lower(), e["name"].lower()) not in drop and e["key"].lower() not in wild]
        if state == "not_configured":
            return out

        def put(key, name, val):
            if val[0] == "DELETE":
                out.append({"key": key, "name": "**del." + name, "type": "REG_SZ", "value": " "})
            else:
                out.append({"key": key, "name": name, "type": val[0], "value": val[1]})

        if state == "disabled":
            if pol["valueName"]:
                put(pol["key"], pol["valueName"], pol["disabledValue"] or ("DELETE", None))
            for i in pol["disabledList"]:
                put(i["key"], i["name"], i["val"])
            for el in pol["elements"]:
                if el["kind"] == "list":
                    out.append({"key": el["key"], "name": "**delvals.", "type": "REG_SZ", "value": " "})
                elif el["name"]:
                    put(el["key"], el["name"], ("DELETE", None))
            return out
        # enabled
        if pol["valueName"]:
            put(pol["key"], pol["valueName"], pol["enabledValue"] or ("REG_DWORD", 1))
        for i in pol["enabledList"]:
            put(i["key"], i["name"], i["val"])
        for el in pol["elements"]:
            v = values.get(el["id"])
            k = el["kind"]
            if k == "boolean":
                on = bool(v)
                put(el["key"], el["name"], el["true"] if on else el["false"])
                for x in el["trueList"] if on else el["falseList"]:
                    put(x["key"], x["name"], x["val"])
            elif k == "decimal":
                if v in (None, ""):
                    if el["required"]:
                        raise CoreError(f"\"{el['label']}\" is a required field")
                    continue
                n = int(v)
                if n < el["min"] or n > el["max"]:
                    raise CoreError(f"\"{el['label']}\" must be between {el['min']} and {el['max']}")
                put(el["key"], el["name"], ("REG_SZ", str(n)) if el["text"]
                    else ("REG_QWORD" if el.get("long") else "REG_DWORD", n))
            elif k == "text":
                if v in (None, ""):
                    if el["required"]:
                        raise CoreError(f"\"{el['label']}\" is a required field")
                    continue
                put(el["key"], el["name"], ("REG_EXPAND_SZ" if el["expand"] else "REG_SZ", str(v)[:el["maxlen"]]))
            elif k == "multiText":
                lines = v if isinstance(v, list) else str(v or "").split("\n")
                put(el["key"], el["name"], ("REG_MULTI_SZ", [l for l in lines if l]))
            elif k == "enum":
                if v in (None, ""):
                    continue
                it = el["items"][int(v)]
                if it["val"]:
                    put(el["key"], el["name"], it["val"])
                for x in it["list"]:
                    put(x["key"], x["name"], x["val"])
            elif k == "list":
                if not el["additive"]:
                    out.append({"key": el["key"], "name": "**delvals.", "type": "REG_SZ", "value": " "})
                typ = "REG_EXPAND_SZ" if el["expand"] else "REG_SZ"
                for n, item in enumerate(v or [], 1):
                    if el["explicit"]:
                        out.append({"key": el["key"], "name": str(item.get("name") or ""), "type": typ,
                                    "value": str(item.get("value") or "")})
                    else:
                        name = f"{el['prefix']}{n}" if el["prefix"] else str(item)
                        out.append({"key": el["key"], "name": name, "type": typ, "value": str(item)})
        return out

    def public_tree(self) -> dict:
        cats = [{"id": c["id"], "name": c["name"], "parent": c["parent"]} for c in self.categories.values()]
        pols = [{"id": p["id"], "name": p["name"], "cat": p["cat"], "class": p["class"]}
                for p in self.policies.values()]
        return {"categories": cats, "policies": pols, "lang": self.lang, "source": self.source,
                "errors": self.errors[:20]}

    def public_policy(self, pid) -> dict:
        p = self.policies.get(pid)
        if not p:
            raise CoreError("Policy not found")
        clean = {k: v for k, v in p.items() if not k.startswith("_")}
        return clean


# ======================================================================
# security descriptor (DACL only) - for security filtering
# ======================================================================
APPLY_GP = uuid.UUID("edacfd8f-ffb3-11d1-b41d-00a0c968f939").bytes_le
READ_MASK = 0x20094                     # READ_CONTROL | LIST | READ_PROP | LIST_OBJECT
AUTH_USERS = "S-1-5-11"


def sid_to_str(b: bytes) -> str:
    rev, cnt = b[0], b[1]
    auth = int.from_bytes(b[2:8], "big")
    subs = struct.unpack("<" + "I" * cnt, b[8:8 + 4 * cnt])
    return f"S-{rev}-{auth}" + "".join(f"-{s}" for s in subs)


def sid_from_str(s: str) -> bytes:
    p = s.split("-")
    subs = [int(x) for x in p[3:]]
    return bytes([int(p[1]), len(subs)]) + int(p[2]).to_bytes(6, "big") + struct.pack("<" + "I" * len(subs), *subs)


def _sid_len(b: bytes, off: int) -> int:
    return 8 + 4 * b[off + 1]


def sd_parse(sd: bytes) -> dict:
    rev, _sbz, control, _o, _g, _s, odacl = struct.unpack("<BBHIIII", sd[:20])
    aces = []
    if odacl:
        _arev, _s1, _size, count, _s2 = struct.unpack("<BBHHH", sd[odacl:odacl + 8])
        i = odacl + 8
        for _ in range(count):
            atype, aflags, asize = struct.unpack("<BBH", sd[i:i + 4])
            aces.append({"type": atype, "flags": aflags, "raw": sd[i:i + asize]})
            i += asize
    return {"control": control, "aces": aces}


def ace_info(ace: dict) -> dict:
    raw, t = ace["raw"], ace["type"]
    mask = struct.unpack("<I", raw[4:8])[0]
    if t in (5, 6):                        # object ACE
        oflags = struct.unpack("<I", raw[8:12])[0]
        off = 12
        otype = None
        if oflags & 1:
            otype = raw[off:off + 16]
            off += 16
        if oflags & 2:
            off += 16
        return {"allow": t == 5, "mask": mask, "otype": otype, "sid": sid_to_str(raw[off:off + _sid_len(raw, off)])}
    return {"allow": t == 0, "mask": mask, "otype": None, "sid": sid_to_str(raw[8:8 + _sid_len(raw, 8)])}


def _ace(atype, flags, mask, sid, otype=None) -> bytes:
    sidb = sid_from_str(sid)
    body = struct.pack("<I", mask)
    if otype:
        body += struct.pack("<I", 1) + otype
    body += sidb
    return struct.pack("<BBH", atype, flags, 4 + len(body)) + body


def sd_build(control: int, aces: list) -> bytes:
    acl_body = b"".join(a["raw"] for a in aces)
    acl = struct.pack("<BBHHH", 4, 0, 8 + len(acl_body), len(aces), 0) + acl_body
    control = (control | 0x0004 | 0x8000) & ~0x0010            # DACL present, self-relative, no SACL
    return struct.pack("<BBHIIII", 1, 0, control, 0, 0, 0, 20) + acl


def filter_list(sd: bytes) -> list[str]:
    out = []
    for a in sd_parse(sd)["aces"]:
        if a["flags"] & 0x10:              # geerbt
            continue
        i = ace_info(a)
        if i["allow"] and i["otype"] == APPLY_GP and i["sid"] not in out:
            out.append(i["sid"])
    return out


def filter_set(sd: bytes, sid: str, apply: bool) -> bytes:
    p = sd_parse(sd)
    aces = p["aces"]
    explicit = [a for a in aces if not a["flags"] & 0x10]
    inherited = [a for a in aces if a["flags"] & 0x10]
    keep = []
    for a in explicit:
        i = ace_info(a)
        if i["sid"] == sid and i["allow"] and i["otype"] == APPLY_GP:
            continue                       # remove this principal's Apply right (may be re-set)
        if not apply and i["sid"] == sid and sid != AUTH_USERS and i["allow"] and i["mask"] == READ_MASK:
            continue                       # also remove the associated read right
        keep.append(a)
    if apply:
        has_read = any(ace_info(a)["sid"] == sid and ace_info(a)["allow"] and
                       (ace_info(a)["mask"] & READ_MASK) == READ_MASK for a in keep)
        if not has_read:
            keep.append({"type": 0, "flags": 0x02, "raw": _ace(0, 0x02, READ_MASK, sid)})
        keep.append({"type": 5, "flags": 0x02, "raw": _ace(5, 0x02, 0x100, sid, APPLY_GP)})
    deny = [a for a in keep if a["type"] in (1, 6)]
    allow = [a for a in keep if a["type"] not in (1, 6)]
    return sd_build(p["control"], deny + allow + inherited)


# ======================================================================
# Der eigentliche Kern
# ======================================================================
CSE_REG = "{35378EAC-683F-11D2-A89A-0000F87A0E7B}"
TOOL_REG = {"Machine": "{D02B1F72-3407-48AE-BA88-E8213C6761F1}", "User": "{D02B1F73-3407-48AE-BA88-E8213C6761F1}"}
CSE_SEC = "{827D319E-6EAC-11D2-A4EA-00C04F79F83A}"
TOOL_SEC = "{803E14A0-B4FB-11D0-A0D0-00A0C90F574B}"
SECEDIT = "Microsoft/Windows NT/SecEdit/GptTmpl.inf"


def ext_merge(current: str, cse: str, tool: str) -> str:
    pairs: dict = {}
    for grp in re.findall(r"\[([^\]]*)\]", current or ""):
        guids = re.findall(r"\{[0-9A-Fa-f-]{36}\}", grp)
        if guids:
            pairs.setdefault(guids[0].upper(), set()).update(g.upper() for g in guids[1:])
    pairs.setdefault(cse.upper(), set()).add(tool.upper())
    return "".join("[" + c + "".join(sorted(t)) + "]" for c, t in sorted(pairs.items()))


class Core:
    _catalog_cache: dict = {}

    def __init__(self, conn, base_dn: str, fs, admx_fallback=None, root_dn: str = ""):
        import ldap3
        self.l3, self.c, self.base, self.fs = ldap3, conn, base_dn, fs
        self.admx_fallback = admx_fallback
        self.domain = ".".join(p.split("=", 1)[1] for p in base_dn.split(",") if p.strip().upper().startswith("DC="))
        info = getattr(conn.server, "info", None)
        other = getattr(info, "other", {}) or {}
        self.root = root_dn or (other.get("rootDomainNamingContext") or [base_dn])[0]
        self.config = (other.get("configurationNamingContext") or [f"CN=Configuration,{self.root}"])[0]

    # ------------------------------------------------ LDAP-Helfer
    def _search(self, base, flt, attrs, scope="SUBTREE", controls=None):
        from ldap3.core.exceptions import LDAPException, LDAPNoSuchObjectResult
        sc = getattr(self.l3, scope)
        try:
            ok = self.c.search(base, flt, search_scope=sc, attributes=attrs, controls=controls)
        except LDAPNoSuchObjectResult:
            return []                          # object/base does not exist
        except LDAPException as exc:
            raise CoreError(f"LDAP-Suche fehlgeschlagen: {exc}") from None
        if not ok and self.c.result.get("result") not in (0, 32):
            raise CoreError(f"LDAP-Suche fehlgeschlagen: {self.c.result.get('description')}")
        return [r for r in (self.c.response or []) if r.get("type") == "searchResEntry"]

    def _check(self, ok, what):
        if not ok:
            r = self.c.result
            raise CoreError(f"{what} fehlgeschlagen: {r.get('description')} {r.get('message') or ''}".strip())

    @staticmethod
    def _a(r, name, default=None):
        v = (r.get("attributes") or {}).get(name, default)
        if isinstance(v, list) and len(v) == 1:
            return v[0]
        return v if v not in ([], None) else default

    # ================================================ DNS
    def _zone_bases(self):
        return [("Domain", f"CN=MicrosoftDNS,DC=DomainDnsZones,{self.base}"),
                ("Gesamtstruktur", f"CN=MicrosoftDNS,DC=ForestDnsZones,{self.root}"),
                ("Legacy", f"CN=MicrosoftDNS,CN=System,{self.base}")]

    def dns_zones(self) -> list:
        out = []
        for scope, base in self._zone_bases():
            for r in self._search(base, "(objectClass=dnsZone)", ["name"], "LEVEL"):
                name = self._a(r, "name", "")
                if name in ("RootDNSServers", "..TrustAnchors") or name.startswith("..InProgress"):
                    continue
                out.append({"name": name, "dn": r["dn"], "scope": scope,
                            "reverse": name.endswith(".in-addr.arpa") or name.endswith(".ip6.arpa")})
        return sorted(out, key=lambda z: (z["reverse"], z["name"]))

    def _zone_dn(self, zone: str) -> str:
        for z in self.dns_zones():
            if z["name"].lower() == zone.lower():
                return z["dn"]
        raise CoreError(f"Zone {zone} not found (AD-integrated zones only)")

    def dns_records(self, zone: str) -> list:
        zdn = self._zone_dn(zone)
        out = []
        for r in self._search(zdn, "(objectClass=dnsNode)", ["name", "dnsRecord", "dNSTombstoned"], "LEVEL"):
            if str(self._a(r, "dNSTombstoned", False)).upper() == "TRUE":
                continue
            name = self._a(r, "name", "")
            for raw in (r.get("raw_attributes") or {}).get("dnsRecord", []):
                rec = dns_decode(raw)
                if rec["type"] == "TYPE0":
                    continue
                out.append({"name": name, **rec})
        return sorted(out, key=lambda x: (x["name"] != "@", x["name"].lower(), x["type"]))

    def _soa_serial(self, zdn: str) -> int:
        r = self._search(f"DC=@,{zdn}", "(objectClass=dnsNode)", ["dnsRecord"], "BASE")
        for raw in (r[0].get("raw_attributes") or {}).get("dnsRecord", []) if r else []:
            d = dns_decode(raw)
            if d["type"] == "SOA":
                return int(d["value"].get("serial") or 1)
        return 1

    def _node_dn(self, name: str, zdn: str) -> str:
        from ldap3.utils.dn import escape_rdn
        name = (name or "@").strip().rstrip(".")
        if not name or "," in name or "=" in name:
            raise CoreError("Invalid name")
        return f"DC={escape_rdn(name)},{zdn}"

    def dns_add(self, zone, name, rtype, value, ttl=3600):
        if rtype.upper() not in EDITABLE_TYPES:
            raise CoreError(f"Type {rtype} cannot be edited")
        zdn = self._zone_dn(zone)
        raw = dns_encode(rtype, value, int(ttl), self._soa_serial(zdn))
        ndn = self._node_dn(name, zdn)
        ex = self._search(ndn, "(objectClass=dnsNode)", ["dnsRecord", "dNSTombstoned"], "BASE")
        if ex:
            tomb = str(self._a(ex[0], "dNSTombstoned", False)).upper() == "TRUE"
            if tomb:
                self._check(self.c.modify(ndn, {"dnsRecord": [(self.l3.MODIFY_REPLACE, [raw])],
                                                "dNSTombstoned": [(self.l3.MODIFY_REPLACE, [False])]}),
                            "Eintrag anlegen")
            else:
                existing = (ex[0].get("raw_attributes") or {}).get("dnsRecord", [])
                if any(dns_decode(e)["value"] == dns_decode(raw)["value"] and dns_decode(e)["type"] == rtype.upper()
                       for e in existing):
                    raise CoreError("This record already exists")
                if rtype.upper() == "CNAME" or any(dns_decode(e)["type"] == "CNAME" for e in existing):
                    raise CoreError("A CNAME cannot coexist with other records of the same name")
                self._check(self.c.modify(ndn, {"dnsRecord": [(self.l3.MODIFY_ADD, [raw])]}), "Eintrag anlegen")
        else:
            self._check(self.c.add(ndn, ["top", "dnsNode"], {"dnsRecord": [raw]}), "Eintrag anlegen")
        return dns_decode(raw)

    def dns_update(self, zone, name, rid, value, ttl):
        zdn = self._zone_dn(zone)
        ndn = self._node_dn(name, zdn)
        old = base64.b64decode(rid)
        rtype = dns_decode(old)["type"]
        if rtype not in EDITABLE_TYPES:
            raise CoreError(f"Type {rtype} cannot be edited")
        new = dns_encode(rtype, value, int(ttl), self._soa_serial(zdn))
        self._check(self.c.modify(ndn, {"dnsRecord": [(self.l3.MODIFY_DELETE, [old]),
                                                      (self.l3.MODIFY_ADD, [new])]}), "modify record")
        return dns_decode(new)

    def dns_delete(self, zone, name, rid):
        zdn = self._zone_dn(zone)
        ndn = self._node_dn(name, zdn)
        old = base64.b64decode(rid)
        if dns_decode(old)["type"] == "SOA":
            raise CoreError("The SOA record cannot be deleted")
        ex = self._search(ndn, "(objectClass=dnsNode)", ["dnsRecord"], "BASE")
        vals = (ex[0].get("raw_attributes") or {}).get("dnsRecord", []) if ex else []
        if len(vals) <= 1:
            self._check(self.c.delete(ndn), "delete record")
        else:
            self._check(self.c.modify(ndn, {"dnsRecord": [(self.l3.MODIFY_DELETE, [old])]}), "delete record")
        return {"ok": True}

    # ================================================ GPO: Verwaltung
    @property
    def pol_base(self):
        return f"CN=Policies,CN=System,{self.base}"

    @staticmethod
    def _parse_gplink(s: str) -> list:
        out = []
        for dn, opt in re.findall(r"\[LDAP://([^;\]]+);(\d+)\]", s or "", re.I):
            m = re.search(r"\{[0-9A-Fa-f-]{36}\}", dn)
            out.append({"dn": dn, "guid": m.group(0).upper() if m else "", "opt": int(opt)})
        return out

    @staticmethod
    def _build_gplink(entries: list) -> str:
        return "".join(f"[LDAP://{e['dn']};{e['opt']}]" for e in entries)

    def _link_targets(self) -> list:
        res = self._search(self.base, "(|(gPLink=*)(objectClass=organizationalUnit)(objectClass=domainDNS))",
                           ["gPLink", "gPOptions", "name", "objectClass"])
        try:
            res += self._search(f"CN=Sites,{self.config}", "(objectClass=site)", ["gPLink", "gPOptions", "name", "objectClass"], "LEVEL")
        except CoreError:
            pass
        out = []
        for r in res:
            oc = [str(x).lower() for x in ((r.get("attributes") or {}).get("objectClass") or [])]
            kind = "domain" if "domaindns" in oc else "site" if "site" in oc else "ou" if "organizationalunit" in oc else "other"
            out.append({"dn": r["dn"], "name": self._a(r, "name", r["dn"]), "kind": kind,
                        "block": int(self._a(r, "gPOptions", 0) or 0) == 1,
                        "links": self._parse_gplink(self._a(r, "gPLink", "") or "")})
        return out

    def gpo_list(self) -> dict:
        rows = self._search(self.pol_base, "(objectClass=groupPolicyContainer)",
                            ["cn", "displayName", "flags", "versionNumber", "whenCreated", "whenChanged",
                             "gPCMachineExtensionNames", "gPCUserExtensionNames", "gPCWQLFilter"], "LEVEL")
        targets = self._link_targets()
        gpos = []
        for r in rows:
            guid = str(self._a(r, "cn", "")).upper()
            v = int(self._a(r, "versionNumber", 0) or 0)
            links = []
            for t in targets:
                ls = t["links"]
                for idx, l in enumerate(ls):
                    if l["guid"] == guid:
                        links.append({"target": t["dn"], "name": t["name"], "kind": t["kind"],
                                      "order": len(ls) - idx, "enabled": not l["opt"] & 1,
                                      "enforced": bool(l["opt"] & 2)})
            gpos.append({"guid": guid, "dn": r["dn"], "name": self._a(r, "displayName", guid),
                         "flags": int(self._a(r, "flags", 0) or 0), "user_version": v >> 16,
                         "machine_version": v & 0xFFFF, "created": str(self._a(r, "whenCreated", "")),
                         "changed": str(self._a(r, "whenChanged", "")), "links": links,
                         "wmi": bool(self._a(r, "gPCWQLFilter", ""))})
        gpos.sort(key=lambda g: g["name"].lower())
        tl = [{"dn": t["dn"], "name": t["name"], "kind": t["kind"], "block": t["block"],
               "links": [{"guid": l["guid"], "order": len(t["links"]) - i, "enabled": not l["opt"] & 1,
                          "enforced": bool(l["opt"] & 2)} for i, l in enumerate(t["links"])]}
              for t in targets]
        return {"gpos": gpos, "targets": sorted(tl, key=lambda t: (t["kind"] != "domain", t["dn"][::-1].lower()))}

    def _gpo(self, guid: str) -> dict:
        guid = guid.upper()
        r = self._search(f"CN={guid},{self.pol_base}", "(objectClass=groupPolicyContainer)",
                         ["displayName", "versionNumber", "gPCMachineExtensionNames", "gPCUserExtensionNames", "flags"], "BASE")
        if not r:
            raise CoreError("GPO not found")
        return {"dn": r[0]["dn"], "guid": guid, "name": self._a(r[0], "displayName", guid),
                "version": int(self._a(r[0], "versionNumber", 0) or 0),
                "mext": self._a(r[0], "gPCMachineExtensionNames", "") or "",
                "uext": self._a(r[0], "gPCUserExtensionNames", "") or ""}

    def _gpt_ini(self, guid, version, name=None):
        rel = f"Policies/{guid}/GPT.INI"
        cur = (self.fs.read(rel) or b"").decode("utf-8", "replace")
        lines = [l for l in cur.replace("\r\n", "\n").split("\n") if l.strip()]
        if not lines or lines[0].strip().lower() != "[general]":
            lines = ["[General]"] + [l for l in lines if not l.strip().startswith("[")]
        lines = [l for l in lines if not l.lower().startswith("version=")
                 and not (name is not None and l.lower().startswith("displayname="))]
        lines.insert(1, f"Version={version}")
        if name is not None:
            lines.append(f"displayName={name}")
        self.fs.write(rel, ("\r\n".join(lines) + "\r\n").encode("utf-8"))

    def gpo_create(self, name: str) -> dict:
        name = (name or "").strip()
        if not name:
            raise CoreError("Name fehlt")
        guid = "{" + str(uuid.uuid4()).upper() + "}"
        dn = f"CN={guid},{self.pol_base}"
        path = f"\\\\{self.domain}\\SysVol\\{self.domain}\\Policies\\{guid}"
        self.fs.makedirs(f"Policies/{guid}/Machine")
        self.fs.makedirs(f"Policies/{guid}/User")
        self._gpt_ini(guid, 0, name)
        self._check(self.c.add(dn, ["top", "container", "groupPolicyContainer"],
                               {"displayName": name, "flags": 0, "versionNumber": 0,
                                "gPCFunctionalityVersion": 2, "gPCFileSysPath": path}), "GPO anlegen")
        for sub in ("User", "Machine"):
            self._check(self.c.add(f"CN={sub},{dn}", ["top", "container"]), "GPO anlegen")
        return {"guid": guid, "name": name}

    def gpo_delete(self, guid: str) -> dict:
        g = self._gpo(guid)
        for t in self._link_targets():
            if any(l["guid"] == g["guid"] for l in t["links"]):
                self.gpo_unlink(g["guid"], t["dn"])
        for r in sorted(self._search(g["dn"], "(objectClass=*)", ["cn"]), key=lambda x: -len(x["dn"])):
            self._check(self.c.delete(r["dn"]), "delete GPO")
        self.fs.rmtree(f"Policies/{g['guid']}")
        return {"ok": True}

    def gpo_rename(self, guid, name):
        g = self._gpo(guid)
        self._check(self.c.modify(g["dn"], {"displayName": [(self.l3.MODIFY_REPLACE, [name])]}), "Umbenennen")
        self._gpt_ini(g["guid"], g["version"], name)
        return {"ok": True}

    def gpo_status(self, guid, flags: int):
        g = self._gpo(guid)
        self._check(self.c.modify(g["dn"], {"flags": [(self.l3.MODIFY_REPLACE, [int(flags) & 3])]}), "Status setzen")
        return {"ok": True}

    def _set_gplink(self, target_dn, entries):
        val = self._build_gplink(entries)
        op = [(self.l3.MODIFY_REPLACE, [val])] if val else [(self.l3.MODIFY_REPLACE, [])]
        self._check(self.c.modify(target_dn, {"gPLink": op}), "modify link")

    def _links_of(self, target_dn):
        r = self._search(target_dn, "(objectClass=*)", ["gPLink"], "BASE")
        if not r:
            raise CoreError("Target not found")
        return self._parse_gplink(self._a(r[0], "gPLink", "") or "")

    def gpo_link(self, guid, target_dn, enabled=True, enforced=False):
        g = self._gpo(guid)
        ls = self._links_of(target_dn)
        if any(l["guid"] == g["guid"] for l in ls):
            raise CoreError("The GPO is already linked to this target")
        opt = (0 if enabled else 1) | (2 if enforced else 0)
        # a new link gets - as in the GPMC - the lowest priority
        # (= vorne im gPLink-String; Verknuepfungsreihenfolge 1 steht hinten).
        self._set_gplink(target_dn, [{"dn": g["dn"], "opt": opt}] + ls)
        return {"ok": True}

    def gpo_unlink(self, guid, target_dn):
        guid = guid.upper()
        self._set_gplink(target_dn, [l for l in self._links_of(target_dn) if l["guid"] != guid])
        return {"ok": True}

    def gpo_link_set(self, guid, target_dn, enabled=None, enforced=None, order=None):
        guid = guid.upper()
        ls = self._links_of(target_dn)
        cur = next((l for l in ls if l["guid"] == guid), None)
        if not cur:
            raise CoreError("Link not found")
        if enabled is not None:
            cur["opt"] = (cur["opt"] & ~1) | (0 if enabled else 1)
        if enforced is not None:
            cur["opt"] = (cur["opt"] & ~2) | (2 if enforced else 0)
        if order is not None:
            ls.remove(cur)
            pos = max(0, min(len(ls), len(ls) + 1 - int(order)))   # Reihenfolge 1 = ganz hinten
            ls.insert(pos, cur)
        self._set_gplink(target_dn, ls)
        return {"ok": True}

    def gpo_block(self, target_dn, block: bool):
        self._check(self.c.modify(target_dn, {"gPOptions": [(self.l3.MODIFY_REPLACE, [1 if block else 0])]}),
                    "change inheritance")
        return {"ok": True}

    # ------------------------------------------------ Sicherheitsfilterung
    def _sd(self, dn):
        from ldap3.protocol.microsoft import security_descriptor_control
        r = self._search(dn, "(objectClass=*)", ["nTSecurityDescriptor"], "BASE",
                         controls=security_descriptor_control(sdflags=0x04))
        sd = (r[0].get("raw_attributes") or {}).get("nTSecurityDescriptor", [b""])[0] if r else b""
        if not sd:
            raise CoreError("Security descriptor not readable (bind user permissions?)")
        return sd

    def _sid_name(self, sid):
        if sid == AUTH_USERS:
            return "Authentifizierte Benutzer"
        b = sid_from_str(sid)
        flt = "(objectSid=" + "".join(f"\\{x:02x}" for x in b) + ")"
        r = self._search(self.base, flt, ["sAMAccountName", "objectClass"])
        return self._a(r[0], "sAMAccountName", sid) if r else sid

    def gpo_filter(self, guid) -> list:
        g = self._gpo(guid)
        return [{"sid": s, "name": self._sid_name(s)} for s in filter_list(self._sd(g["dn"]))]

    def gpo_filter_set(self, guid, principal: str, apply: bool) -> list:
        from ldap3.protocol.microsoft import security_descriptor_control
        from ldap3.utils.conv import escape_filter_chars
        g = self._gpo(guid)
        if principal.upper().startswith("S-1-"):
            sid = principal.upper()
        else:
            r = self._search(self.base, f"(sAMAccountName={escape_filter_chars(principal)})", ["objectSid"])
            raw = (r[0].get("raw_attributes") or {}).get("objectSid", [b""])[0] if r else b""
            if not raw:
                raise CoreError(f"\"{principal}\" not found")
            sid = sid_to_str(raw)
        new = filter_set(self._sd(g["dn"]), sid, apply)
        self._check(self.c.modify(g["dn"], {"nTSecurityDescriptor": [(self.l3.MODIFY_REPLACE, [new])]},
                                  controls=security_descriptor_control(sdflags=0x04)), "change security filter")
        return self.gpo_filter(guid)

    # ------------------------------------------------ Versionen/Erweiterungen
    def _bump(self, g, side: str, cse: str, tool: str):
        v = g["version"]
        user, mach = v >> 16, v & 0xFFFF
        if side == "User":
            user = (user + 1) & 0xFFFF
        else:
            mach = (mach + 1) & 0xFFFF
        nv = (user << 16) | mach
        attr = "gPCUserExtensionNames" if side == "User" else "gPCMachineExtensionNames"
        ext = ext_merge(g["uext"] if side == "User" else g["mext"], cse, tool)
        self._gpt_ini(g["guid"], nv)
        self._check(self.c.modify(g["dn"], {"versionNumber": [(self.l3.MODIFY_REPLACE, [nv])],
                                            attr: [(self.l3.MODIFY_REPLACE, [ext])]}), "bump version")
        g["version"] = nv

    # ------------------------------------------------ Registry.pol
    def _pol(self, guid, side):
        return pol_parse(self.fs.read(f"Policies/{guid}/{side}/Registry.pol"))

    def _pol_write(self, g, side, entries):
        self.fs.write(f"Policies/{g['guid']}/{side}/Registry.pol", pol_build(entries))
        self._bump(g, side, CSE_REG, TOOL_REG[side])

    def registry_list(self, guid) -> dict:
        g = self._gpo(guid)
        return {"Machine": self._pol(g["guid"], "Machine"), "User": self._pol(g["guid"], "User")}

    def registry_set(self, guid, side, key, name, typ, value, delete=False):
        if side not in ("Machine", "User"):
            raise CoreError("Scope must be Machine or User")
        if not delete and typ not in REG:
            raise CoreError("Invalid registry type")
        g = self._gpo(guid)
        ents = [e for e in self._pol(g["guid"], side)
                if not (e["key"].lower() == key.lower() and e["name"].lower() == name.lower())]
        if not delete:
            ents.append({"key": key, "name": name, "type": typ, "value": value})
        self._pol_write(g, side, ents)
        return {"ok": True}

    # ------------------------------------------------ ADMX
    def catalog(self, refresh=False) -> AdmxCatalog:
        key = self.domain
        hit = Core._catalog_cache.get(key)
        if hit and not refresh and time.time() - hit[0] < 3600:
            return hit[1]
        cat = AdmxCatalog.load(self.fs, self.admx_fallback)
        Core._catalog_cache[key] = (time.time(), cat)
        return cat

    def admx_tree(self, refresh=False):
        return self.catalog(refresh).public_tree()

    def _sides(self, pol):
        return ["Machine", "User"] if pol["class"] == "Both" else [pol["class"]]

    def admx_policy(self, guid, pid) -> dict:
        cat = self.catalog()
        g = self._gpo(guid)
        pol = cat.policies.get(pid)
        if not pol:
            raise CoreError("Policy not found")
        states = {side: cat.state(pol, self._pol(g["guid"], side)) for side in self._sides(pol)}
        return {"policy": cat.public_policy(pid), "states": states}

    def admx_set(self, guid, pid, side, state, values) -> dict:
        if state not in ("enabled", "disabled", "not_configured"):
            raise CoreError("Invalid state")
        cat = self.catalog()
        pol = cat.policies.get(pid)
        if not pol or side not in self._sides(pol):
            raise CoreError("Invalid policy/scope")
        g = self._gpo(guid)
        ents = cat.apply(pol, self._pol(g["guid"], side), state, values or {})
        self._pol_write(g, side, ents)
        return {"ok": True, "state": cat.state(pol, ents)}

    def configured(self, guid) -> dict:
        """Alle konfigurierten Einstellungen (wie ein GPO-Bericht)."""
        g = self._gpo(guid)
        res = {"guid": g["guid"], "name": g["name"], "Machine": [], "User": [], "raw": {}, "security": {}}
        try:
            cat = self.catalog()
        except CoreError as exc:
            cat, res["admx_error"] = None, str(exc)
        for side in ("Machine", "User"):
            ents = self._pol(g["guid"], side)
            used = set()
            if cat:
                for pid, pol in cat.policies.items():
                    if side not in self._sides(pol):
                        continue
                    st = cat.state(pol, ents)
                    if st["state"] != "not_configured":
                        res[side].append({"id": pid, "name": pol["name"], "state": st["state"],
                                          "values": st["values"], "path": self._cat_path(cat, pol["cat"])})
                        for k, n in cat.touched(pol):
                            used.add((k.lower(), n.lower()))
            res["raw"][side] = [e for e in ents if (e["key"].lower(), e["name"].lower()) not in used
                                and (e["key"].lower(), e["name"].lower().replace("**del.", "")) not in used
                                and (e["key"].lower(), "*") not in used]
        res["security"] = self.security_get(guid)
        return res

    @staticmethod
    def _cat_path(cat, cid):
        out, seen = [], set()
        while cid and cid in cat.categories and cid not in seen:
            seen.add(cid)
            out.insert(0, cat.categories[cid]["name"])
            cid = cat.categories[cid]["parent"]
        return " / ".join(out)

    # ------------------------------------------------ Sicherheitseinstellungen (GptTmpl.inf)
    def security_get(self, guid) -> dict:
        raw = self.fs.read(f"Policies/{guid.upper()}/Machine/{SECEDIT}")
        if not raw:
            return {}
        txt = raw.decode("utf-16", "replace") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8", "replace")
        out, sec = {}, None
        for line in txt.replace("\r\n", "\n").split("\n"):
            line = line.strip()
            if not line or line.startswith(";"):
                continue
            if line.startswith("[") and line.endswith("]"):
                sec = line[1:-1]
                out.setdefault(sec, {})
            elif sec and "=" in line:
                k, v = line.split("=", 1)
                out[sec][k.strip()] = v.strip()
        return out

    def security_set(self, guid, section, key, value, delete=False):
        if section in ("Unicode", "Version") and not delete:
            pass
        g = self._gpo(guid)
        data = self.security_get(g["guid"]) or {}
        data.setdefault("Unicode", {"Unicode": "yes"})
        data.setdefault("Version", {"signature": '"$CHICAGO$"', "Revision": "1"})
        sec = data.setdefault(section, {})
        if delete:
            sec.pop(key, None)
        else:
            sec[key] = str(value)
        order = ["Unicode"] + [s for s in data if s not in ("Unicode", "Version")] + ["Version"]
        lines = []
        for s in order:
            lines.append(f"[{s}]")
            lines += [f"{k} = {v}" if s not in ("Unicode", "Version") else f"{k}={v}" for k, v in data[s].items()]
        body = ("\r\n".join(lines) + "\r\n").encode("utf-16-le")
        self.fs.write(f"Policies/{g['guid']}/Machine/{SECEDIT}", b"\xff\xfe" + body)
        self._bump(g, "Machine", CSE_SEC, TOOL_SEC)
        return {"ok": True}

    # ------------------------------------------------ Verteiler
    OPS = ("dns_zones", "dns_records", "dns_add", "dns_update", "dns_delete",
           "gpo_list", "gpo_create", "gpo_delete", "gpo_rename", "gpo_status", "gpo_link", "gpo_unlink",
           "gpo_link_set", "gpo_block", "gpo_filter", "gpo_filter_set", "registry_list", "registry_set",
           "admx_tree", "admx_policy", "admx_set", "configured", "security_get", "security_set")

    def dispatch(self, op: str, args: dict):
        from ldap3.core.exceptions import LDAPException
        if op not in self.OPS:
            raise CoreError(f"Unbekannte Operation {op}")
        try:
            return getattr(self, op)(**(args or {}))
        except LDAPException as exc:          # raise_exceptions=True on the connection
            raise CoreError(f"LDAP: {exc}") from None
        except TypeError as exc:
            raise CoreError(f"Invalid parameters for {op}: {exc}") from None
