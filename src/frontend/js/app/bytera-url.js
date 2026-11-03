// bytera-url.js - internes Adress-Schema  bytera://<bereich>/<ziel...>
// ---------------------------------------------------------------------------
// Wie steam://... ein Spiel startet, oeffnet bytera://... einen Bereich des
// Dashboards. Die Links funktionieren an drei Stellen:
//   * im Dashboard angeklickt (<a href="bytera://...">)         -> Fenster oeffnet sich
//   * beim Laden als  ?go=bytera://...  oder  #bytera://...      -> wird einmal ausgefuehrt
//   * vom Betriebssystem (Desktop-Client registriert das Schema) -> siehe Client
//
// Format:  bytera://<kind>/<p1>/<p2>?a=b
//   bytera://agent/<id>                 Geraet auswaehlen (Uebersicht)
//   bytera://agent/<id>/terminal        Terminal des Geraets
//   bytera://agent/<id>/explorer        Dateien
//   bytera://agent/<id>/screen          Fernwartung (Alias: vnc, remote)
//   bytera://agent/<id>/files           = explorer
//   bytera://agent/<id>/tasks           Task-Manager
//   bytera://agent/<id>/edit            Geraet bearbeiten
//   bytera://node/<id>                  Node eines Geraets
//   bytera://space/<name>               Space oeffnen (Modul "spaces")
//   bytera://dist[/<ordner>]            Verteilung (Core)
//   bytera://app/<appId>                beliebige Core-/Modul-App (terminal etc. brauchen ein Geraet)
//   bytera://clients                    Geraete-Uebersicht
// Unbekanntes Ziel -> Hinweis, kein Absturz.
//
// Diese Datei kennt die Oeffnen-Funktionen NICHT direkt, sondern bekommt sie
// per setByteraHandlers() aus app.js gereicht (keine Import-Zyklen, testbar).

let H = {
  openApp: () => false,          // (appId)            Core-/Menue-App
  openModuleApp: () => false,    // (appId)            App eines aktiven Moduls
  clientAction: () => false,     // (action, clientId) Terminal/Explorer/VNC/... fuer ein Geraet
  selectClient: () => false,     // (clientId)         Geraet in der Seitenleiste auswaehlen
  openNode: () => false,         // (clientId)         Node-Fenster
  openSpace: () => false,        // (name)             Space oeffnen
  openDist: () => false,         // (folder?)          Verteilung
  notify: (m) => { try { window.notify?.(m, "error", 7000); } catch { /* egal */ } },
};

export function setByteraHandlers(h) { H = { ...H, ...h }; }

// Aktionen je Geraet; linke Seite = im Link erlaubte Schreibweise.
const AGENT_ACTIONS = {
  terminal: "terminal", shell: "terminal", cmd: "terminal",
  explorer: "explorer", files: "explorer", dateien: "explorer",
  screen: "vnc", vnc: "vnc", remote: "vnc", fernwartung: "vnc",
  tasks: "taskmanager", taskmanager: "taskmanager",
  guacamole: "guacamole", guac: "guacamole",
  edit: "edit", bearbeiten: "edit",
};

export function isByteraUrl(s) {
  return typeof s === "string" && /^bytera:\/\//i.test(s.trim());
}

/** "bytera://agent/node-100/terminal?x=1" -> {kind, parts:[...], query:{}} | null */
export function parseByteraUrl(raw) {
  if (!isByteraUrl(raw)) return null;
  let rest = raw.trim().slice("bytera://".length);
  let query = {};
  const q = rest.indexOf("?");
  if (q >= 0) {
    try {
      new URLSearchParams(rest.slice(q + 1)).forEach((v, k) => { query[k] = v; });
    } catch { /* egal */ }
    rest = rest.slice(0, q);
  }
  const parts = rest.split("/").filter(Boolean).map((p) => {
    try { return decodeURIComponent(p); } catch { return p; }
  });
  if (!parts.length) return null;
  return { kind: parts[0].toLowerCase(), parts: parts.slice(1), query, raw: raw.trim() };
}

/** Fuehrt einen bytera://-Link aus. true = erkannt (auch wenn Ziel fehlt). */
export function resolveByteraUrl(raw) {
  const u = parseByteraUrl(raw);
  if (!u) return false;
  const { kind, parts } = u;
  try {
    switch (kind) {
      case "agent":
      case "device":
      case "client": {
        // "clients" (Uebersicht) wird unten als eigener kind behandelt; hier mit ID:
        const id = parts[0];
        if (!id) { H.openApp("clients"); return true; }
        const action = (parts[1] || "").toLowerCase();
        if (!action) { H.selectClient(id); return true; }
        if (action === "node") { H.openNode(id); return true; }
        const mapped = AGENT_ACTIONS[action];
        if (!mapped) { H.notify(`Unbekannte Geräte-Aktion: ${action}`); return true; }
        if (!H.clientAction(mapped, id)) H.notify("Gerät nicht gefunden oder kein Zugriff.");
        return true;
      }
      case "clients":
      case "devices":
        H.openApp("clients");
        return true;
      case "node":
        if (parts[0]) H.openNode(parts[0]); else H.openApp("nodes");
        return true;
      case "space":
      case "spaces":
        if (!H.openSpace(parts[0] || "")) H.notify("Spaces-Modul ist nicht aktiv.");
        return true;
      case "dist":
      case "verteilung":
        H.openDist(parts[0] || "");
        return true;
      case "app": {
        const appId = parts[0];
        if (!appId) return true;
        if (H.openApp(appId)) return true;
        if (H.openModuleApp(appId)) return true;
        H.notify(`Unbekannte App: ${appId}`);
        return true;
      }
      default:
        H.notify(`Unbekanntes bytera://-Ziel: ${kind}`);
        return true;
    }
  } catch (e) {
    H.notify("Konnte den Link nicht öffnen: " + (e?.message || e));
    return true;
  }
}

/** Baut einen Link, z.B. makeByteraUrl("agent", id, "terminal"). */
export function makeByteraUrl(kind, ...segments) {
  const path = segments
    .filter((s) => s !== undefined && s !== null && s !== "")
    .map((s) => encodeURIComponent(String(s)))
    .join("/");
  return `bytera://${kind}` + (path ? "/" + path : "");
}

/** Link in die Zwischenablage legen (mit kurzer Rueckmeldung). */
export function copyByteraUrl(url) {
  try {
    navigator.clipboard?.writeText(url).then(
      () => window.notify?.("Link kopiert: " + url, "success", 2500),
      () => window.notify?.(url, "info", 4000),
    );
  } catch { window.notify?.(url, "info", 4000); }
}

// Klicks auf <a href="bytera://..."> im ganzen Dashboard abfangen.
let _clickHooked = false;
export function installByteraLinkHandler() {
  if (_clickHooked) return;
  _clickHooked = true;
  document.addEventListener("click", (ev) => {
    const a = ev.target?.closest?.("a[href^='bytera://']");
    if (!a) return;
    ev.preventDefault();
    resolveByteraUrl(a.getAttribute("href"));
  });
}

// Beim Laden: ?go=bytera://...  oder  #bytera://...  einmal ausfuehren und
// danach aus der Adresse entfernen (kein erneutes Ausloesen bei Reload).
export function consumeDeepLinkFromLocation() {
  let target = null;
  try {
    const go = new URLSearchParams(location.search).get("go");
    if (isByteraUrl(go)) target = go;
    else if (isByteraUrl(decodeURIComponent(location.hash.replace(/^#/, "")))) {
      target = decodeURIComponent(location.hash.replace(/^#/, ""));
    }
  } catch { /* egal */ }
  if (!target) return false;
  try {
    const url = new URL(location.href);
    url.searchParams.delete("go");
    if (isByteraUrl(decodeURIComponent(url.hash.replace(/^#/, "")))) url.hash = "";
    history.replaceState(null, "", url.toString());
  } catch { /* egal */ }
  // Etwas spaeter: erst muessen Geraete/Module geladen sein.
  setTimeout(() => resolveByteraUrl(target), 400);
  return true;
}
