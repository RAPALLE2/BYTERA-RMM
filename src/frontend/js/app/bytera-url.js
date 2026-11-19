// bytera-url.js - internal address scheme  bytera://<area>/<target...>
// ---------------------------------------------------------------------------
// Like steam://... starts a game, bytera://... opens an area of the dashboard.
// The links work in three places:
//   * clicked in the dashboard (<a href="bytera://...">)         -> a window opens
//   * on load as  ?go=bytera://...  or  #bytera://...             -> runs once
//   * from the operating system (desktop client registers the scheme) -> see client
//
// Format:  bytera://<kind>/<p1>/<p2>?a=b
//   bytera://agent/<id>                 select device (overview)
//   bytera://agent/<id>/terminal        terminal of the device
//   bytera://agent/<id>/explorer        files
//   bytera://agent/<id>/screen          remote maintenance (alias: vnc, remote)
//   bytera://agent/<id>/files           = explorer
//   bytera://agent/<id>/tasks           task manager
//   bytera://agent/<id>/edit            edit device
//   bytera://node/<id>                  node of a device
//   bytera://space/<name>               open space (module "spaces")
//   bytera://dist[/<folder>]            distribution (Core)
//   bytera://app/<appId>                any core/module app (terminal etc. need a device)
//   bytera://clients                    device overview
// Unknown target -> hint, no crash.
//
// This file does NOT know the open functions directly but gets them via
// setByteraHandlers() from app.js (no import cycles, testable).

let H = {
  openApp: () => false,          // (appId)            core/menu app
  openModuleApp: () => false,    // (appId)            app of an active module
  clientAction: () => false,     // (action, clientId) terminal/explorer/VNC/... for a device
  selectClient: () => false,     // (clientId)         select device in the sidebar
  openNode: () => false,         // (clientId)         node window
  openSpace: () => false,        // (name)             open space
  openDist: () => false,         // (folder?)          distribution
  notify: (m) => { try { window.notify?.(m, "error", 7000); } catch { /* ignore */ } },
};

export function setByteraHandlers(h) { H = { ...H, ...h }; }

// Actions per device; left side = spelling allowed in the link.
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
        // "clients" (overview) is handled below as its own kind; here with an ID:
        const id = parts[0];
        if (!id) { H.openApp("clients"); return true; }
        const action = (parts[1] || "").toLowerCase();
        if (!action) { H.selectClient(id); return true; }
        if (action === "node") { H.openNode(id); return true; }
        const mapped = AGENT_ACTIONS[action];
        if (!mapped) { H.notify(`Unknown device action: ${action}`); return true; }
        if (!H.clientAction(mapped, id)) H.notify("Device not found or no access.");
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
        if (!H.openSpace(parts[0] || "")) H.notify("The Spaces module is not active.");
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
        H.notify(`Unknown app: ${appId}`);
        return true;
      }
      default:
        H.notify(`Unknown bytera:// target: ${kind}`);
        return true;
    }
  } catch (e) {
    H.notify("Could not open the link: " + (e?.message || e));
    return true;
  }
}

/** Builds a link, e.g. makeByteraUrl("agent", id, "terminal"). */
export function makeByteraUrl(kind, ...segments) {
  const path = segments
    .filter((s) => s !== undefined && s !== null && s !== "")
    .map((s) => encodeURIComponent(String(s)))
    .join("/");
  return `bytera://${kind}` + (path ? "/" + path : "");
}

/** Copy a link to the clipboard (with a short confirmation). */
export function copyByteraUrl(url) {
  try {
    navigator.clipboard?.writeText(url).then(
      () => window.notify?.("Link copied: " + url, "success", 2500),
      () => window.notify?.(url, "info", 4000),
    );
  } catch { window.notify?.(url, "info", 4000); }
}

// Intercept clicks on <a href="bytera://..."> across the whole dashboard.
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

// On load: run ?go=bytera://...  or  #bytera://...  once and then remove it
// from the address (no re-triggering on reload).
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
  // A little later: devices/modules must be loaded first.
  setTimeout(() => resolveByteraUrl(target), 400);
  return true;
}
