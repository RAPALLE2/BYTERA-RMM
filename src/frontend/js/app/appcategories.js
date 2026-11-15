// appcategories.js (Core)
// -----------------------
// EINE Gruppierung aller Apps in Kategorien, die beide Designs teilen:
//   * Futuristisch: Standardordner im Startmenü (startmenu.js legt sie beim
//     ersten Start bzw. einmalig für bestehende Layouts an; eigene Ordner der
//     Benutzer bleiben unberührt)
//   * Standard (klassisch): gruppierte App-Navigation links (classic.js)
//
// Module können die Kategorie einer App selbst festlegen: in module.json
// unter "apps": [{ "id": "...", "category": "netzwerk" }]. Ohne Angabe gilt
// die Tabelle APP_CATEGORY unten, sonst "weitere".

export const CATEGORIES = [
  { key: "geraete",        name: "Geräte & Verwaltung", icon: "🖥️" },
  { key: "fernzugriff",    name: "Fernzugriff",         icon: "🖱️" },
  { key: "automatisierung",name: "Automatisierung",     icon: "⚙️" },
  { key: "netzwerk",       name: "Netzwerk",            icon: "🌐" },
  { key: "infrastruktur",  name: "Infrastruktur",       icon: "🧱" },
  { key: "zusammenarbeit", name: "Zusammenarbeit",      icon: "💬" },
  { key: "sicherheit",     name: "System & Sicherheit", icon: "🔐" },
  { key: "extras",         name: "Extras",              icon: "🧰" },
  { key: "weitere",        name: "Weitere",             icon: "📁" },
];

const APP_CATEGORY = {
  // Geräte & Verwaltung
  clients: "geraete", manage: "geraete", tenants: "geraete", inventory: "geraete",
  dist: "geraete", deployment: "geraete", patching: "geraete",
  "winadmin-updates": "geraete", "winadmin-registry": "geraete", "temp-usb": "geraete",
  "headless-screen": "geraete",
  // Fernzugriff
  console: "fernzugriff", guacamole: "fernzugriff", "relay-explorer": "fernzugriff",
  "relay-manager": "fernzugriff", recordings: "fernzugriff", "proxmox-console": "fernzugriff",
  webbrowser: "fernzugriff", webapp: "fernzugriff",
  // Automatisierung
  scripts: "automatisierung", bulk: "automatisierung", automation: "automatisierung",
  // Netzwerk
  network: "netzwerk", portscan: "netzwerk", speedtest: "netzwerk", topology: "netzwerk",
  vpn: "netzwerk", cloudflare: "netzwerk", webserver: "netzwerk",
  // Infrastruktur
  proxmox: "infrastruktur", docker: "infrastruktur", nodes: "infrastruktur",
  "sql-database": "infrastruktur",
  // Zusammenarbeit
  tickets: "zusammenarbeit", chat: "zusammenarbeit", calendar: "zusammenarbeit",
  todos: "zusammenarbeit", orgchart: "zusammenarbeit", notepad: "zusammenarbeit",
  spaces: "zusammenarbeit", aichat: "zusammenarbeit", notifications: "zusammenarbeit",
  // System & Sicherheit
  settings: "sicherheit", permissions: "sicherheit", modules: "sicherheit", audit: "sicherheit",
  "sso-realms": "sicherheit", addir: "sicherheit", privacy: "sicherheit",
  iptracker: "sicherheit", canary: "sicherheit",
  // Extras
  calculator: "extras", clock: "extras", audioplayer: "extras", gaminghub: "extras",
  towerdefense: "extras", example: "extras",
};

const _override = {};   // von Modulen gemeldete Kategorien (module.json)

export function setAppCategory(appId, key) {
  if (key && CATEGORIES.some((c) => c.key === key)) _override[appId] = key;
}

export function categoryOf(appId) {
  if (_override[appId]) return _override[appId];
  if (APP_CATEGORY[appId]) return APP_CATEGORY[appId];
  if (String(appId).startsWith("webapp")) return "fernzugriff";   // gespeicherte Web-Apps
  return "weitere";
}

export function categoryInfo(key) {
  return CATEGORIES.find((c) => c.key === key) || CATEGORIES[CATEGORIES.length - 1];
}

/** apps: [{id, ...}] -> [{key, name, icon, apps:[...]}] in fester Reihenfolge,
 *  leere Kategorien ausgelassen. Reihenfolge innerhalb: wie übergeben. */
export function groupApps(apps) {
  const by = new Map(CATEGORIES.map((c) => [c.key, []]));
  for (const a of apps) by.get(categoryOf(a.id))?.push(a);
  return CATEGORIES.map((c) => ({ ...c, apps: by.get(c.key) })).filter((g) => g.apps.length);
}
