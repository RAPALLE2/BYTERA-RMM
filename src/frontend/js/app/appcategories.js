// appcategories.js (Core)
// -----------------------
// ONE grouping of all apps into categories, shared by both designs:
//   * Futuristic: default folders in the start menu (startmenu.js creates them
//     on first start resp. once for existing layouts; the users' own folders
//     stay untouched)
//   * Classic Hard (module "styles"): grouped app navigation on the left
//
// Modules can set an app's category themselves: in module.json under
// "apps": [{ "id": "...", "category": "netzwerk" }]. Without it the table
// APP_CATEGORY below applies, otherwise "weitere".

export const CATEGORIES = [
  { key: "geraete",        name: "Devices & management", icon: "🖥️" },
  { key: "fernzugriff",    name: "Remote access",        icon: "🖱️" },
  { key: "automatisierung",name: "Automation",           icon: "⚙️" },
  { key: "netzwerk",       name: "Network",              icon: "🌐" },
  { key: "infrastruktur",  name: "Infrastructure",       icon: "🧱" },
  { key: "zusammenarbeit", name: "Collaboration",        icon: "💬" },
  { key: "sicherheit",     name: "System & security",    icon: "🔐" },
  { key: "extras",         name: "Extras",               icon: "🧰" },
  { key: "weitere",        name: "More",                 icon: "📁" },
];

const APP_CATEGORY = {
  // Devices & management
  clients: "geraete", manage: "geraete", tenants: "geraete", inventory: "geraete",
  dist: "geraete", deployment: "geraete", patching: "geraete",
  "winadmin-updates": "geraete", "winadmin-registry": "geraete", "temp-usb": "geraete",
  "headless-screen": "geraete",
  // Remote access
  console: "fernzugriff", guacamole: "fernzugriff", "relay-explorer": "fernzugriff",
  "relay-manager": "fernzugriff", recordings: "fernzugriff", "proxmox-console": "fernzugriff",
  webbrowser: "fernzugriff", webapp: "fernzugriff",
  // Automation
  scripts: "automatisierung", bulk: "automatisierung", automation: "automatisierung",
  // Network
  network: "netzwerk", portscan: "netzwerk", speedtest: "netzwerk", topology: "netzwerk",
  vpn: "netzwerk", cloudflare: "netzwerk", webserver: "netzwerk",
  // Infrastructure
  proxmox: "infrastruktur", docker: "infrastruktur", nodes: "infrastruktur",
  "sql-database": "infrastruktur",
  // Collaboration
  tickets: "zusammenarbeit", chat: "zusammenarbeit", calendar: "zusammenarbeit",
  todos: "zusammenarbeit", orgchart: "zusammenarbeit", notepad: "zusammenarbeit",
  spaces: "zusammenarbeit", aichat: "zusammenarbeit", notifications: "zusammenarbeit",
  // System & security
  settings: "sicherheit", permissions: "sicherheit", modules: "sicherheit", audit: "sicherheit",
  "sso-realms": "sicherheit", addir: "sicherheit", privacy: "sicherheit",
  iptracker: "sicherheit", canary: "sicherheit",
  // Extras
  calculator: "extras", clock: "extras", audioplayer: "extras", gaminghub: "extras",
  towerdefense: "extras", example: "extras",
};

const _override = {};   // categories reported by modules (module.json)

export function setAppCategory(appId, key) {
  if (key && CATEGORIES.some((c) => c.key === key)) _override[appId] = key;
}

export function categoryOf(appId) {
  if (_override[appId]) return _override[appId];
  if (APP_CATEGORY[appId]) return APP_CATEGORY[appId];
  if (String(appId).startsWith("webapp")) return "fernzugriff";   // saved web apps
  return "weitere";
}

export function categoryInfo(key) {
  return CATEGORIES.find((c) => c.key === key) || CATEGORIES[CATEGORIES.length - 1];
}

/** apps: [{id, ...}] -> [{key, name, icon, apps:[...]}] in fixed order,
 *  empty categories omitted. Order within: as passed. */
export function groupApps(apps) {
  const by = new Map(CATEGORIES.map((c) => [c.key, []]));
  for (const a of apps) by.get(categoryOf(a.id))?.push(a);
  return CATEGORIES.map((c) => ({ ...c, apps: by.get(c.key) })).filter((g) => g.apps.length);
}
