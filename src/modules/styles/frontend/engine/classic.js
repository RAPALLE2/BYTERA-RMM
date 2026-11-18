// engine/classic.js (module "styles")
// -----------------------------------
// Layout engine "classic" for styles (see ../../README.md):
//
//   style.json  "layout": "classic"       -> Classic: navigation on the left = folders/apps
//                                            of the start menu, tab "Overview" = dashboard
//               "layout": "classic-hard"  -> like Proxmox (body.cl-hard): navigation =
//                                            fixed categories (appcategories.js), tree root
//                                            "Data center", tab "Overview" = category
//                                            column + fixed view instead of widgets (classicview.js)
//
//   * Content as TABS in the main area instead of floating windows:
//     first tab "Overview", then one tab per open window
//     (click = show, x / middle click = close)
//   * the taskbar becomes a slim status bar (tasks, clock)
//
// Technically the window manager stays unchanged: each "window" fills the
// content area, only the active tab is visible. That is why all apps and modules
// work without adaptation. It hooks in via the style hooks of the core
// (/js/app/styles.js) - the core itself does not know this layout.
//
// Only on wide screens; on mobile the mobile view still applies
// (with the look of the style).

import { state } from "/js/app/state.js";
import { focusWindow, closeWindow, windowLink } from "/js/app/windowmanager.js";
import { getCatalogApps, getMenuModel, openMenuApp, onMenuChange, catalogEntry } from "/js/app/startmenu.js";
import { groupApps } from "/js/app/appcategories.js";
import { esc } from "/js/app/utils.js";
import { icon } from "./icons.js";

let active = "overview";      // "overview" oder Fenster-Key
let lastTop = null;           // zuletzt fokussiertes Fenster (Erkennung "neu geoeffnet/fokussiert")
let tabsEl = null, navEl = null, enabled = false;
const NAV_KEY = () => `rmm_clnav:${state.user?.username || "default"}`;

export const isClassicLayout = () => enabled;
const isHard = () => document.body.classList.contains("cl-hard");

/** Layout starten (vom Style-Modul nach dem Login, vor dem Wiederherstellen der Fenster).
 *  hard = Proxmox-Aufbau (Kategorien + classicview.js statt Dashboard). */
export async function start({ hard = false } = {}) {
  document.body.classList.toggle("cl-hard", !!hard);
  const hooks = await import("/js/app/styles.js");
  hooks.onWindowsChanged(renderClassicTabs);
  if (hard) {
    const cv = await import("./classicview.js");
    hooks.setMainContentRenderer((el) => {
      if (!document.body.classList.contains("cl-layout")) return false;   // Handy: normales Dashboard
      cv.renderClassicView(el);
      return true;
    });
    hooks.onMetrics((id) => cv.updateClassicView(id));
  }
  initClassic();
  // Uebersicht mit dem neuen Aufbau zeichnen
  try { (await import("/js/app/panel.js")).renderMainContent(); } catch {}
}

/** Klassische Bedienung aufbauen. */
export function initClassic() {
  if (enabled) return;
  if (new URLSearchParams(location.search).get("solo")) return;   // Einzelfenster-Modus
  if (window.matchMedia("(max-width: 900px)").matches) return;     // Handy: mobile Ansicht
  const mainArea = document.querySelector(".main-area");
  const main = document.getElementById("main-content");
  const layer = document.getElementById("window-layer");
  const sidebar = document.getElementById("sidebar");
  if (!mainArea || !main || !layer || !sidebar) return;
  enabled = true;
  document.body.classList.add("cl-layout");

  // ---- Inhaltsbereich: Tab-Leiste + (Uebersicht | Fenster) ----
  const stage = document.createElement("div");
  stage.id = "cl-stage";
  tabsEl = document.createElement("div");
  tabsEl.id = "cl-tabs";
  tabsEl.setAttribute("role", "tablist");
  const body = document.createElement("div");
  body.id = "cl-body";
  mainArea.insertBefore(stage, main);
  body.append(main, layer);
  stage.append(tabsEl, body);

  // Classic Hard: Wurzel des Geraete-Baums heisst wie in Proxmox "Rechenzentrum"
  const dash = document.getElementById("sidebar-dashboard");
  if (dash && isHard()) dash.innerHTML = `<span class="nav-ico">${icon("datacenter", 15)}</span> Data center`;

  // ---- Navigation (Apps) oben in der Seitenleiste ----
  navEl = document.createElement("div");
  navEl.id = "cl-nav";
  const header = sidebar.querySelector(".sidebar-header");
  sidebar.insertBefore(navEl, header ? header.nextSibling : sidebar.firstChild);
  renderNav();
  onMenuChange(renderNav);

  // ---- Statusleiste ----
  const tb = document.querySelector(".taskbar");
  if (tb && !document.getElementById("cl-status")) {
    const st = document.createElement("div");
    st.id = "cl-status";
    tb.insertBefore(st, tb.firstChild);
    renderStatus();
    setInterval(renderStatus, 30000);
  }

  // Klick in Geraete-Baum / Dashboard / Favoriten zeigt die Uebersicht -
  // ausser der Klick hat ein Fenster geoeffnet (dann gewinnt dessen Tab).
  sidebar.addEventListener("click", (e) => {
    if (e.target.closest("#cl-nav")) return;
    if (!e.target.closest("#sidebar-tree, #sidebar-dashboard, #sidebar-fav-list")) return;
    const before = state.focusOrder[state.focusOrder.length - 1];
    const count = state.windows.length;
    setTimeout(() => {
      const after = state.focusOrder[state.focusOrder.length - 1];
      if (after === before && state.windows.length === count) activate("overview");
    }, 60);
  });

  // Tastatur: Strg+Alt+Pfeil links/rechts wechselt den Tab
  document.addEventListener("keydown", (e) => {
    if (!(e.ctrlKey && e.altKey) || !["ArrowLeft", "ArrowRight"].includes(e.key)) return;
    const keys = ["overview", ...state.windows.map((w) => w.key)];
    const i = keys.indexOf(active);
    activate(keys[(i + (e.key === "ArrowRight" ? 1 : keys.length - 1)) % keys.length]);
    e.preventDefault();
  });
  renderClassicTabs();
}

/** Tab aktivieren: "overview" oder Fenster-Key. */
export function activate(key) {
  if (!enabled) return;
  if (key !== "overview" && !state.windows.some((w) => w.key === key)) key = "overview";
  active = key;
  if (key !== "overview") {
    focusWindow(key);                      // ruft renderClassicTabs() ueber die Taskleiste auf
    lastTop = key;
  }
  apply();
}

/** Wird bei jeder Aenderung der Fensterliste aufgerufen (taskbar.js). */
export function renderClassicTabs() {
  if (!enabled || !tabsEl) return;
  const top = state.focusOrder[state.focusOrder.length - 1] || null;
  // Neues/fokussiertes Fenster -> dessen Tab wird aktiv
  if (top && top !== lastTop && state.windows.some((w) => w.key === top)) active = top;
  lastTop = top;
  if (active !== "overview" && !state.windows.some((w) => w.key === active)) {
    const rest = [...state.focusOrder].reverse().find((k) => state.windows.some((w) => w.key === k));
    active = rest || "overview";
  }
  apply();
}

function apply() {
  for (const w of state.windows) {
    if (!w._el) continue;
    const on = w.key === active;
    w._el.classList.toggle("cl-active", on);
    if (on && w.minimized) { w.minimized = false; w._el.style.display = "flex"; }
  }
  document.body.classList.toggle("cl-overview", active === "overview");
  drawTabs();
  markNav();
  // Apps mit Diagrammen/Terminals passen ihre Groesse beim Sichtbarwerden an.
  // NUR bei echtem Tab-Wechsel: Die Taskleiste zeichnet sich bei "resize" neu
  // (-> renderClassicTabs) - ein unbedingtes Ereignis waere eine Endlosschleife.
  if (active !== shownActive) {
    shownActive = active;
    requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));
  }
}
let shownActive = null;

function winIcon(w) {
  const c = catalogEntry(w.appId) || catalogEntry(w.props?.part) || null;
  return c?.icon || "◻";
}

function drawTabs() {
  const tab = (key, icon, title, dot, closable) => `
    <div class="cl-tab${key === active ? " active" : ""}" data-key="${esc(key)}" role="tab"
         aria-selected="${key === active}" title="${esc(title)}">
      ${dot ? `<span class="cl-dot" style="background:${esc(dot)}"></span>` : ""}
      <span class="cl-tab-ico">${icon}</span>
      <span class="cl-tab-label">${esc(title)}</span>
      ${closable ? `<button class="cl-tab-x" title="Close" aria-label="Close">×</button>` : ""}
    </div>`;
  tabsEl.innerHTML = tab("overview", icon("home", 15), "Overview", null, false)
    + state.windows.map((w) => tab(w.key, winIcon(w), w.title || w.appId, w.clientColor, true)).join("");
  tabsEl.querySelectorAll(".cl-tab").forEach((el) => {
    const key = el.dataset.key;
    el.addEventListener("click", (e) => {
      if (e.target.closest(".cl-tab-x")) { closeWindow(key); return; }
      activate(key);
    });
    el.addEventListener("auxclick", (e) => { if (e.button === 1 && key !== "overview") { e.preventDefault(); closeWindow(key); } });
    el.addEventListener("contextmenu", (e) => { if (key !== "overview") { e.preventDefault(); tabMenu(key, e); } });
  });
  tabsEl.querySelector(".cl-tab.active")?.scrollIntoView?.({ block: "nearest", inline: "nearest" });
}

function tabMenu(key, ev) {
  document.getElementById("cl-tabmenu")?.remove();
  const m = document.createElement("div");
  m.id = "cl-tabmenu";
  m.className = "cl-menu";
  m.style.left = `${ev.clientX}px`;
  m.style.top = `${ev.clientY}px`;
  m.innerHTML = `
    <button data-a="link">${icon("link", 14)} Copy a link to this view</button>
    <button data-a="close">Close</button>
    <button data-a="others">Close others</button>
    <button data-a="right">Close to the right</button>
    <button data-a="all">Close all</button>`;
  document.body.appendChild(m);
  const keys = state.windows.map((w) => w.key);
  m.addEventListener("click", (e) => {
    const a = e.target.closest("button")?.dataset.a;
    if (!a) return;
    if (a === "link") {
      const w = state.windows.find((x) => x.key === key);
      try { navigator.clipboard.writeText(windowLink(w)); window.notify?.("Link kopiert", "success", 2000); } catch {}
      m.remove();
      return;
    }
    const i = keys.indexOf(key);
    const kill = a === "close" ? [key] : a === "others" ? keys.filter((k) => k !== key)
      : a === "right" ? keys.slice(i + 1) : keys;
    kill.forEach((k) => closeWindow(k));
    m.remove();
  });
  setTimeout(() => document.addEventListener("mousedown", function off(e) {
    if (!m.contains(e.target)) { m.remove(); document.removeEventListener("mousedown", off); }
  }), 0);
}

// ---------------------------------------------------------------- Navigation
function navState() {
  try { return JSON.parse(localStorage.getItem(NAV_KEY()) || "{}"); } catch { return {}; }
}
function saveNavState(s) {
  try { localStorage.setItem(NAV_KEY(), JSON.stringify(s)); } catch {}
}

function renderNav() {
  if (!navEl) return;
  const st = navState();
  // Classic Hard: apps grouped by the fixed categories (the same as the
  // default folders in the start menu). Classic: exactly the start menu arrangement.
  const hard = isHard();
  const model = hard
    ? groupApps(getCatalogApps()).map((g) => ({ t: "folder", id: `cat_${g.key}`, name: g.name, icon: g.icon, apps: g.apps }))
    : getMenuModel();
  const item = (a, indent) => `
    <div class="cl-nav-item" data-app="${esc(a.id)}" style="padding-left:${10 + indent}px" title="${esc(a.label)}">
      <span class="cl-nav-ico">${a.icon}</span><span class="cl-nav-label">${esc(a.label)}</span>
    </div>`;
  const body = model.map((it) => {
    if (it.t === "app") return item(it, 0);
    const open = hard ? st[it.id] === true : st[it.id] !== false;   // Hard: Kategorien zu, Classic: Ordner offen
    return `<div class="cl-nav-folder${open ? " open" : ""}" data-folder="${esc(it.id)}">
        <span class="cl-caret">${open ? "▾" : "▸"}</span><span class="cl-nav-ico">${it.icon || ""}</span><span class="cl-nav-label">${esc(it.name)}</span>
        <span class="cl-nav-count">${it.apps.length}</span>
      </div>
      ${open ? it.apps.map((a) => item(a, 14)).join("") : ""}`;
  }).join("");
  const collapsed = st.__all === false;
  navEl.innerHTML = `
    <div class="cl-nav-head">
      <span class="cl-caret">${collapsed ? "▸" : "▾"}</span>
      <span class="cl-nav-title">Anwendungen</span>
      <button class="cl-nav-btn" data-a="menu" title="Open the start menu (edit arrangement and folders)">⋯</button>
    </div>
    <div class="cl-nav-list" style="${collapsed ? "display:none" : ""}">${body || `<div class="cl-nav-empty">Keine Anwendungen</div>`}</div>`;

  navEl.querySelector(".cl-nav-head").addEventListener("click", (e) => {
    if (e.target.closest('[data-a="menu"]')) {
      document.getElementById("taskbar-menu-btn")?.click();
      return;
    }
    const s = navState(); s.__all = collapsed ? true : false; saveNavState(s); renderNav();
  });
  navEl.querySelectorAll(".cl-nav-folder").forEach((el) => el.addEventListener("click", () => {
    const s = navState(); const id = el.dataset.folder;
    const wasOpen = isHard() ? s[id] === true : s[id] !== false;
    s[id] = !wasOpen; saveNavState(s); renderNav();
  }));
  navEl.querySelectorAll(".cl-nav-item").forEach((el) => el.addEventListener("click", () => openMenuApp(el.dataset.app)));
  markNav();
}

function markNav() {
  if (!navEl) return;
  const w = state.windows.find((x) => x.key === active);
  navEl.querySelectorAll(".cl-nav-item").forEach((el) => {
    el.classList.toggle("active", !!w && (el.dataset.app === w.appId));
  });
}

// ---------------------------------------------------------------- Statusleiste
function renderStatus() {
  const el = document.getElementById("cl-status");
  if (!el) return;
  const u = state.user;
  const online = (state.clients || []).filter((c) => c.online).length;
  const total = (state.clients || []).length;
  el.innerHTML = `<span>${icon("user", 13)} ${esc(u?.display_name || u?.username || "")}</span>`
    + (total ? `<span class="cl-sep">|</span><span>${icon("computer", 13)} ${online}/${total} online</span>` : "");
}
