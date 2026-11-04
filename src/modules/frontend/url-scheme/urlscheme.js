// urlscheme.js (Modul "url-scheme") - internes Adress-Schema bytera://
// ---------------------------------------------------------------------------
// Das Format lebt in DIESEM Modul. Geoeffnet wird ueber die Core-Primitive in
// window.byteraOpenTarget (openApp, clientAction, selectClient, openNode,
// openSpace, openDist, notify) - so bleibt das Format ein Modul, die
// Fenster-Logik aber im Core.
//
// Format:  bytera://<kind>/<p1>/<p2>?a=b
//   bytera://agent/<id>[/terminal|explorer|screen|files|tasks|edit|node]
//   bytera://node/<id> | bytera://node
//   bytera://space/<name>
//   bytera://dist[/<ordner>]
//   bytera://app/<appId>
//   bytera://clients

const AGENT_ACTIONS = {
  terminal: "terminal", shell: "terminal", cmd: "terminal",
  explorer: "explorer", files: "explorer", dateien: "explorer",
  screen: "vnc", vnc: "vnc", remote: "vnc", fernwartung: "vnc",
  tasks: "taskmanager", taskmanager: "taskmanager",
  guacamole: "guacamole", guac: "guacamole",
  edit: "edit", bearbeiten: "edit",
};

function H() {
  return window.byteraOpenTarget || {
    openApp: () => false, openModuleApp: () => false, clientAction: () => false,
    selectClient: () => false, openNode: () => false, openSpace: () => false,
    openDist: () => false, notify: (m) => { try { window.notify?.(m, "error", 7000); } catch { /* egal */ } },
  };
}

export function isByteraUrl(s) {
  return typeof s === "string" && /^bytera:\/\//i.test(s.trim());
}

export function parseByteraUrl(raw) {
  if (!isByteraUrl(raw)) return null;
  let rest = raw.trim().slice("bytera://".length);
  const query = {};
  const q = rest.indexOf("?");
  if (q >= 0) {
    try { new URLSearchParams(rest.slice(q + 1)).forEach((v, k) => { query[k] = v; }); } catch { /* egal */ }
    rest = rest.slice(0, q);
  }
  const parts = rest.split("/").filter(Boolean).map((p) => { try { return decodeURIComponent(p); } catch { return p; } });
  if (!parts.length) return null;
  return { kind: parts[0].toLowerCase(), parts: parts.slice(1), query, raw: raw.trim() };
}

export function makeByteraUrl(kind, ...segments) {
  const path = segments.filter((s) => s !== undefined && s !== null && s !== "")
    .map((s) => encodeURIComponent(String(s))).join("/");
  return `bytera://${kind}` + (path ? "/" + path : "");
}

export function resolveByteraUrl(raw) {
  const u = parseByteraUrl(raw);
  const h = H();
  if (!u) return false;
  const { kind, parts } = u;
  try {
    switch (kind) {
      case "agent": case "device": case "client": {
        const id = parts[0];
        if (!id) { h.openApp("clients"); return true; }
        const action = (parts[1] || "").toLowerCase();
        if (!action) { h.selectClient(id); return true; }
        if (action === "node") { h.openNode(id); return true; }
        const mapped = AGENT_ACTIONS[action];
        if (!mapped) { h.notify("Unbekannte Geräte-Aktion: " + action); return true; }
        if (!h.clientAction(mapped, id)) h.notify("Gerät nicht gefunden oder kein Zugriff.");
        return true;
      }
      case "clients": case "devices": h.openApp("clients"); return true;
      case "node": if (parts[0]) h.openNode(parts[0]); else h.openApp("nodes"); return true;
      case "space": case "spaces":
        if (!h.openSpace(parts[0] || "")) h.notify("Spaces-Modul ist nicht aktiv.");
        return true;
      case "dist": case "verteilung": h.openDist(parts[0] || ""); return true;
      case "app": {
        const appId = parts[0];
        if (!appId) return true;
        if (h.openApp(appId)) return true;
        if (h.openModuleApp(appId)) return true;
        h.notify("Unbekannte App: " + appId);
        return true;
      }
      default: h.notify("Unbekanntes bytera://-Ziel: " + kind); return true;
    }
  } catch (e) {
    h.notify("Konnte den Link nicht öffnen: " + (e && e.message ? e.message : e));
    return true;
  }
}

export function copyByteraUrl(url) {
  try {
    navigator.clipboard?.writeText(url).then(
      () => window.notify?.("Link kopiert: " + url, "success", 2500),
      () => window.notify?.(url, "info", 4000));
  } catch { window.notify?.(url, "info", 4000); }
}
