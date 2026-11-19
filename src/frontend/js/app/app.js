// app.js
// ------
// The central entry point of the frontend. Responsible for:
//   1. login flow (incl. forced password change on first login)
//   2. loading the data (profile, hierarchy, clients) after login
//   3. wiring up all buttons (start menu, user menu, sidebar, taskbar)
//   4. live updates via Socket.IO (client online/offline, metrics)
//   5. "content router": decides which app function renders the content of a
//      window (renderWindowContent)

import { state } from "./state.js";
import { hasAnyClientPerm, hasGlobalPerm, isAdmin } from "./state.js";
import { api, saveToken, clearToken } from "./api.js";
import { dashboardSocket } from "./socket.js";
import { applyTheme, applyAccent } from "./theme.js";
import { applyBaseStyle, runMetricsHooks } from "./styles.js";
import { setLanguage, getLanguage, applyStaticTranslations } from "./i18n_apply.js";

import { renderSidebar, setOnSelect, getExpandedIds, setExpandedIds, setOnTreeStateChanged, initFavorites, initSidebarNav } from "./sidebar.js";
import { renderMainContent } from "./panel.js";
import { renderTaskbar, initTaskbar, consumeFreshStart } from "./taskbar.js";
import { installGlobalContextGuard } from "./contextmenu.js";
import { initHelp } from "./help.js";
import { setContentRenderer, setOnWindowsChanged, openWindow, minimizeAll, closeWindow } from "./windowmanager.js";
import { recordMetrics } from "./metricshistory.js";
import { notify, notifyError } from "./notify.js";
import { configurePersistence, scheduleSave, saveNow, loadState, applyExpanded, setOrgDefaults, setOrgProfilePresets, hydrateFromServer } from "./persist.js";
import { initBars } from "./bars.js";
import { initCrashGuard } from "./crashguard.js";

// Make notify globally available so all modules (including error handling) can
// use the nice slide-down messages.
window.notify = notify;
window.notifyError = notifyError;

// App window renderers
// Registers window.showPermissionDenied - the dialog for missing permissions.
import "./permdenied.js";
// renderDist is loaded LAZILY (see FEATURE_MODULES), so a single missing file
// never blocks the login of the entire SPA.
import { loadModules, renderModuleApp, openModuleApp, moduleAppPerm, inactiveModuleBox, getModuleApi, onModulesChanged } from "./modules.js";
import { handleAction as _clientHandleAction } from "./panel.js";
import { selectClientExternal as _selectClientExternal } from "./sidebar.js";
import { renderNotifyCenter, attachUnreadDot } from "./notifycenter.js";
import { updateClientLayouts } from "./dashlayout.js";
import { updateBatteryIndicators, clientIconType } from "./deviceicons.js";
import { initStartMenu, refreshStartMenu, setStartMenuUser, unlockCatalogPruning } from "./startmenu.js";
import { t, initLanguages, loadLocale } from "./i18n.js";

// -----------------------------------------------------------------
// Content router: which renderer belongs to which appId?
// -----------------------------------------------------------------

const _featureWarned = new Set();

async function loadFeature(mod, fn) {
  // Load dynamically, catch errors. Returns: renderer function or null.
  try {
    const m = await import(mod);
    if (typeof m[fn] !== "function") throw new Error("Function " + fn + " is missing in " + mod);
    return m[fn];
  } catch (e) {
    if (!_featureWarned.has(mod)) {
      _featureWarned.add(mod);
      const msg = "A part of the RMM could not be loaded: " + mod.split("/").pop()
        + " (" + (e && e.message ? e.message : e) + "). The rest keeps working.";
      try { notifyError ? notifyError(msg) : notify(msg, "error", 9000); } catch { /* ignore */ }
      console.error("[feature]", mod, e);
    }
    return null;
  }
}

async function _lazyRender(mod, fn, body, win) {
  const r = await loadFeature(mod, fn);
  if (r) return r(body, win);
  body.innerHTML = '<div style="padding:16px;color:var(--danger)">This app could not be loaded.'
    + '<br><small>A file was probably not uploaded during the update. '
    + 'Login and the other functions keep working normally.</small></div>';
}

// Each app is loaded ONLY when opened (dynamic import). If a file fails (not
// uploaded during an update, syntax error, 404), ONLY this app is affected -
// login, shell and all other apps keep running. This is the
// "try: import ... except: hint" principle for the whole frontend.
const FEATURE_MODULES = {
  terminal:      ["../feature/terminal.js", "renderTerminal"],
  explorer:      ["../feature/explorer.js", "renderExplorer"],
  taskmanager:   ["../feature/taskmanager.js", "renderTaskManager"],
  vnc:           ["../feature/vnc.js", "renderVnc"],
  "edit-client": ["../feature/editclient.js", "renderEditClient"],
  "add-client":  ["../feature/addclient.js", "renderAddClient"],
  manage:        ["../feature/manage.js", "renderManage"],
  settings:      ["../feature/settings.js", "renderSettings"],
  node:          ["../feature/node.js", "renderNode"],
  modules:       ["../feature/modules.js", "renderModules"],
  dist:          ["../feature/dist.js", "renderDist"],
  permissions:   ["../feature/permissions.js", "renderPermissions"],
  audit:         ["../feature/audit.js", "renderAudit"],
  profile:       ["../feature/profile.js", "renderProfile"],
  panelpart:     ["../feature/panelpart.js", "renderPanelPart"],
  fleetwidget:   ["../feature/fleetwidget.js", "renderFleetWidget"],
};

const APP_RENDERERS = { notifycenter: renderNotifyCenter };
for (const [appId, [mod, fn]] of Object.entries(FEATURE_MODULES)) {
  APP_RENDERERS[appId] = (body, win) => _lazyRender(mod, fn, body, win);
}

function renderWindowContent(body, win) {
  const renderer = APP_RENDERERS[win.appId];
  if (renderer) renderer(body, win);
  else if (renderModuleApp(win.appId, body, win)) { /* app of an active module */ }
  else inactiveModuleBox(body, win.appId);   // module off or crashed
}

// -----------------------------------------------------------------
// Load data & refresh the UI
// -----------------------------------------------------------------

async function reloadHierarchy() {
  state.hierarchy = await api.getHierarchy();
  // Inform open windows (add/edit client, ...) so they refill their
  // tenant/location selections IMMEDIATELY - without reopening the window.
  try { window.dispatchEvent(new CustomEvent("rmm:hierarchy-changed")); } catch {}
}

async function reloadClients() {
  state.clients = await api.getClients();
}

// Load the user's effective permissions (for frontend gating).
async function reloadPerms() {
  try {
    state.perms = await api.getEffectivePermissions();
  } catch {
    // Fallback: no permission info -> treating it as "only admin sees all" would
    // be unsafe; instead empty permissions (the backend stays the hard boundary).
    state.perms = { admin: false, global: {}, clients: {} };
  }
}

// Apps available only to the super admin (backend: require_admin).
// App key -> required global permission. Apps not listed are freely accessible
// (e.g. dashboard/clients, speedtest). 'settings' is a special case.
const APP_REQUIRED_PERM = {
  audit: "see_audit",
  permissions: "see_permissions",
  manage: "manage_hierarchy",
  "add-client": "add_client",
  dist: "manage_dist",          // distribution (/dist) - core, formerly module "dist"
};

function _appAllowed(appKey) {
  if (isAdmin()) return true;
  // Settings: visible if the user may see/edit at least one settings area
  // (view settings, SSO, branding, source or create users). The individual
  // tabs are additionally gated within the app.
  if (appKey === "settings") {
    return hasGlobalPerm("see_settings") ||        // SSO: its own module "sso"
           hasGlobalPerm("manage_branding") || hasGlobalPerm("see_source") ||
           hasGlobalPerm("create_users");
  }
  if (appKey.startsWith("webapp:")) return true;          // saved web apps (module 'browser')
  if (appKey === "modules") return hasGlobalPerm("manage_modules");   // module app: global permission
  if (appKey === "nodes") return hasAnyClientPerm("manage_nodes");    // node overview: client permission on at least one client
  const mperm = moduleAppPerm(appKey);                     // app of a module: permission from module.json
  if (mperm !== undefined) return !mperm || hasGlobalPerm(mperm);
  const perm = APP_REQUIRED_PERM[appKey];
  if (!perm) return true;               // freely accessible app
  return hasGlobalPerm(perm);
}

function applyAppVisibility() {
  // Start-menu catalog (hidden original buttons) + live menu refresh.
  document.querySelectorAll("#start-menu [data-app]").forEach((btn) => {
    btn.style.display = _appAllowed(btn.dataset.app) ? "" : "none";
  });
  try { refreshStartMenu(); } catch {}
  // User menu: settings only if allowed.
  const settingsBtn = document.getElementById("btn-open-settings");
  if (settingsBtn) settingsBtn.style.display = _appAllowed("settings") ? "" : "none";
  // Sidebar: "Add client" (enrollment) depends on the 'add_client' permission,
  // "Manage hierarchy" on 'manage_hierarchy'.
  const addBtn = document.getElementById("btn-add-client");
  if (addBtn) addBtn.style.display = (isAdmin() || hasGlobalPerm("add_client")) ? "" : "none";
  const mgmtBtn = document.getElementById("btn-manage-hierarchy");
  if (mgmtBtn) mgmtBtn.style.display = (isAdmin() || hasGlobalPerm("manage_hierarchy")) ? "" : "none";
}

// Name in the user menu - with an ADMIN tag if full access exists. This applies
// both to the role 'admin' and to the permission 'super_admin'
// (admin_via === "permission" is marked with *).
function drawUserMenuName(user) {
  const nameEl = document.getElementById("user-menu-name");
  if (!nameEl || !user) return;
  nameEl.textContent = user.display_name;
  if (!user.is_admin) return;
  const tag = document.createElement("span");
  tag.textContent = user.admin_via === "permission" ? "ADMIN*" : "ADMIN";
  tag.title = user.admin_via === "permission"
    ? t("u_vollzugriff_uber_das_recht_super_a") : "Role admin";
  tag.style.cssText = "color:var(--warn,#f5a524);font-size:10px;font-weight:700;margin-left:5px";
  nameEl.appendChild(tag);
}

/**
 * Reloads user info AND effective permissions and redraws everything that
 * depends on them (ADMIN tag, start menu, sidebar buttons, main area).
 * Called after saving in the permission management so that changes are visible
 * immediately - without Ctrl+F5.
 */
export async function refreshPermissionsUi() {
  try {
    const [me, perms] = await Promise.all([
      api.me().catch(() => state.user),
      api.getEffectivePermissions().catch(() => state.perms),
    ]);
    if (me) state.user = me;
    if (perms) state.perms = perms;
    drawUserMenuName(state.user);
    applyAppVisibility();
    try { renderSidebar(); } catch {}
    try { renderMainContent(); } catch {}
  } catch (e) {
    try { console.error("[perms] refresh failed:", e); } catch {}
  }
}

async function refreshAll() {
  await Promise.all([reloadHierarchy(), reloadClients()]);
  renderSidebar();
  renderMainContent();
}

// -----------------------------------------------------------------
// Version: the single source of truth is backend/version.txt (via
// /api/version). All displays in the frontend are filled from here -
// never hardcode it anywhere!
// -----------------------------------------------------------------
async function loadVersion() {
  try {
    const v = await api.getVersions(); // { backend, agent }
    document.querySelectorAll(".topbar-version").forEach((el) => {
      el.textContent = v.backend || "";
      el.title = `Backend ${v.backend || "?"} · Agent ${v.agent || "?"}`;
    });
  } catch { /* version is cosmetic - never let an error block startup */ }
}
loadVersion();

// -----------------------------------------------------------------
// Login flow
// -----------------------------------------------------------------

const loginScreen = () => document.getElementById("login-screen");
const changePwScreen = () => document.getElementById("change-pw-screen");
const appScreen = () => document.getElementById("app");

function showOnly(el) {
  [loginScreen(), changePwScreen(), appScreen()].forEach((s) => s.classList.add("hidden"));
  el.classList.remove("hidden");
}

async function startSession(user) {
  state.user = user;
  applyTheme(user.theme);
  applyAccent(user.accent || "teal");
  // UI style: the core only knows "futuristic". Other styles are set by the
  // module "styles" after the modules load (futuristic until then).
  applyBaseStyle("futuristic");
  try { await loadLocale(user.language || "de"); } catch { /* buffer/fallback */ }
  setLanguage(user.language || "de");
  applyStaticTranslations();

  // Must the user change their password first? (first login / one-time password)
  if (user.must_change_pw) {
    showOnly(changePwScreen());
    return;
  }

  showOnly(appScreen());
  drawUserMenuName(user);

  // Set up persistence (per user). Expanded sidebar nodes must be set BEFORE
  // the first render so the tree looks correct right away.
  configurePersistence({
    username: user.username,
    getExpanded: getExpandedIds,
    setExpanded: setExpandedIds,
  });
  // Fetch server-side stored UI settings and apply them to localStorage
  // (server wins) - BEFORE initFavorites()/loadState(), so favorites, layouts
  // etc. are identical in every browser.
  await hydrateFromServer();
  // Auto-hide the top/task bar (per user, rmm_bars:<user> was hydrated)
  try { initBars(user.username); } catch (e) { try { console.error(e); } catch {} }
  // Switch the language to the server state if needed (rmm_lang was hydrated).
  try {
    const lang = localStorage.getItem("rmm_lang");
    if (lang && lang !== getLanguage()) setLanguage(lang);
  } catch {}
  // Switch the start menu to the REAL user. initStartMenu() already ran before
  // login (state.user was null) and would otherwise have kept the layout
  // permanently under "rmm_appmenu:default" - that key is not synced with the
  // server, which is why folders/arrangement were missing under another URL.
  try { setStartMenuUser(user.username); } catch (e) { try { console.error(e); } catch {} }
  initFavorites(user.username);   // load the user's favorites
  initSidebarNav();               // wire up dashboard tab + favorites header

  // Load effective permissions FIRST - they are needed both for the restore
  // logic (permission 'restore_session') and for UI gating. Previously
  // reloadPerms() ran only later, so 'restore_session' always counted as
  // "missing" and NOTHING was restored. (Permissions do not depend on
  // clients/hierarchy, so they can safely be loaded first.)
  await reloadPerms();
  // Load active modules (an error in one module affects only that one). If
  // loading fails entirely, the shell still runs - only the module apps are missing.
  try { await loadModules(); } catch (e) { console.error("[modules] load:", e); }
  unlockCatalogPruning();     // now the catalog also knows the module apps
  onModulesChanged(() => { try { applyAppVisibility(); } catch {} });

  const saved = loadState();
  // Personal restore settings (default: restore everything).
  // Additionally gated by the global permission 'restore_session': if missing,
  // it always starts cleanly from the dashboard.
  const mayRestore = hasGlobalPerm("restore_session");
  // Clean restart via the reload button in the taskbar: this one start ignores
  // the saved windows/apps and the client selection. The flag is consumed in
  // the process - the next reload behaves normally again.
  const freshStart = consumeFreshStart();
  // Single-window mode (client app, e.g. remote shell in its own window):
  //   /?solo=terminal&client=<id>   (also explorer, vnc)
  //   /?solo=console[&via=client]    RMM console (module "console", no device)
  // Shows only this one window, restores NOTHING and SAVES nothing - the user's
  // normal window arrangement stays untouched.
  const soloParams = new URLSearchParams(location.search);
  const solo = SOLO_APPS.includes(soloParams.get("solo")) ? soloParams.get("solo") : null;
  const savedPrefs = (saved && saved.restorePrefs) || { client: true, folder: true, apps: true };
  let restorePrefs;
  if (!mayRestore) {
    restorePrefs = { client: false, folder: false, apps: false };
  } else if (freshStart) {
    // Expanded sidebar folders are kept - only the selected client and the
    // open apps/windows are deliberately omitted.
    restorePrefs = { client: false, folder: !!savedPrefs.folder, apps: false };
  } else {
    restorePrefs = savedPrefs;
  }
  if (freshStart) {
    try { console.info("[startSession] clean start: open windows will not be restored."); } catch {}
  }
  // IMPORTANT: wrap the restore/crash-guard init defensively. An error here must
  // NEVER block the login/dashboard (otherwise you get stuck on the empty,
  // "blurry" app screen because the login is already hidden).
  try {
    if (saved) {
      applyExpanded(saved);           // expand state + layouts/profiles
      // Restore the "open folders" (sidebar tree) only if wanted.
      if (!restorePrefs.folder) {
        try { setExpandedIds([]); } catch {}
      }
      // Selection: client vs. folder controllable separately.
      if (saved.selection) {
        const selIsClient = saved.selection.type === "client";
        if ((selIsClient && restorePrefs.client) || (!selIsClient && restorePrefs.folder)) {
          state.selection = saved.selection;
        }
      }
      if (restorePrefs.folder && saved.sidebar) { state.sidebar = saved.sidebar; applySidebarState(); }
    }

    // Frontend crash guard: detects crashes/freezes, logs them and restarts the
    // UI in an emergency (loop-safe, see crashguard.js).
    initCrashGuard({
      username: user.username,
      onPanic: () => {
        try { for (const w of [...(state.windows || [])]) closeWindow(w.key); } catch {}
        try { saveNow(state); } catch {}   // save without open windows
      },
    });
  } catch (e) {
    try { console.error("[startSession] restore/crash-guard init failed:", e); } catch {}
  }

  // Load organization-wide default layouts (set by the admin). Used as the
  // basis for users WITHOUT their own layout and on "reset to default". Errors
  // here are non-critical (then the built-in default applies).
  try {
    const defs = await api.getDefaultLayouts();
    setOrgDefaults({ dash: defs.dash || null, fleet: defs.fleet || null });
    // Org-wide profile presets (physical/VMs/LXCs) for the client view.
    setOrgProfilePresets({
      "Physisch": defs.dash_profile_physical || null,
      "VMs": defs.dash_profile_vm || null,
      "LXCs": defs.dash_profile_lxc || null,
    });
  } catch { /* no org default reachable -> built-in default */ }

  // (Effective permissions were already loaded above - before the restore logic.)
  // FROM HERE no error may escape startSession anymore: otherwise the login
  // handler would hide the app screen again and you could not get in. Each step
  // is therefore wrapped individually - in case of doubt a (possibly empty)
  // dashboard appears instead of a login dead end.
  try { await refreshAll(); }
  catch (e) { try { console.error("[startSession] refreshAll failed:", e); } catch {} }

  try { applyAppVisibility(); } catch (e) { try { console.error(e); } catch {} }

  // Restore saved windows (after refreshAll, so clients/hierarchy are loaded
  // and the window contents render correctly) - only if the user has enabled
  // "restore open apps".
  if (solo) restorePrefs = { client: false, folder: false, apps: false };
  try { if (saved && restorePrefs.apps) restoreWindows(saved); }
  catch (e) { try { console.error("[startSession] restoreWindows failed:", e); } catch {} }

  // From now on save every window/tree change.
  try {
    if (solo) {
      setOnWindowsChanged(() => renderTaskbar());          // single window: save nothing
      setOnTreeStateChanged(() => {});
    } else {
      setOnWindowsChanged(() => { renderTaskbar(); scheduleSave(state); });
      setOnTreeStateChanged(() => scheduleSave(state));
    }
    renderTaskbar();
  } catch (e) { try { console.error(e); } catch {} }
  if (solo) { openSolo(solo, soloParams); return; }

  // Show unread chat messages as a notification on login
  // (only with the 'use_chat' permission; errors here are non-critical).
  try {
    if (isAdmin() || hasGlobalPerm("use_chat")) {
      const unread = await api.chatUnread();
      if (unread && unread.total > 0) {
        const lines = (unread.conversations || []).slice(0, 5)
          .map((c) => `• ${c.name}: ${t("app_unread_n", { n: c.unread })}`).join("\n");
        const more = unread.conversations.length > 5
          ? "\n… " + t("app_and_more", { n: unread.conversations.length - 5 }) : "";
        window.notify?.(
          `💬 ${t("app_unread_chats", { n: unread.total })}\n${lines}${more}`,
          "info", 12000, { source: "chat" });
      }
    }
  } catch (e) { try { console.error("[chat] unread check failed:", e); } catch {} }
}

// Reopens the most recently open windows in their saved order, geometry and
// with their state (minimized/maximized).
// Apps that may be opened in single-window mode (all need a client).
const SOLO_APPS = ["terminal", "explorer", "vnc", "console"];

function openSolo(appId, params) {
  if (appId === "console") {                 // RMM console (module) - needs no device
    document.body.classList.add("solo-mode");
    document.title = "RMM console";
    openWindow({ key: "solo-console", appId: "console", title: "RMM console", maximized: true,
                 props: { via: params.get("via") || "" } });
    return;
  }
  const cid = params.get("client") || "";
  const client = (state.clients || []).find((c) => c.id === cid);
  document.body.classList.add("solo-mode");
  if (!client) {
    window.notify?.("Device not found or no access.", "error", 10000);
    return;
  }
  const names = { terminal: t("terminal"), explorer: t("file_explorer"), vnc: t("remote_screen") };
  const title = `${names[appId]} — ${client.hostname}`;
  document.title = title;
  openWindow({ key: `solo-${appId}-${client.id}`, appId, title, maximized: true, clientColor: client.color,
               props: { clientId: client.id, clientName: client.hostname, platform: client.platform } });
}

function restoreWindows(saved) {
  const wins = saved.windows || [];
  if (!wins.length) return;
  // Open in focus order (last = topmost). Windows not in focusOrder go last.
  const order = saved.focusOrder || [];
  wins.sort((a, b) => order.indexOf(a.key) - order.indexOf(b.key));
  for (const w of wins) {
    try {
      openWindow({
        singleton: true,   // restore: restore exactly this key
        key: w.key, appId: w.appId, title: w.title, props: w.props || {},
        clientColor: w.clientColor, w: w.w, h: w.h, x: w.x, y: w.y,
        minimized: w.minimized, maximized: w.maximized, pinned: !!w.pinned,
        focus: false,   // focus set at the end (order)
      });
    } catch (e) {
      console.warn("[persist] could not restore window:", w.key, e);
    }
  }
}

function initLoginForm() {
  // Load available AD realms into the dropdown (in addition to "Local")
  const realmSelect = document.getElementById("login-realm");
  const userField = document.getElementById("login-username");

  // Restore the last used username + realm (like Proxmox). Only the username
  // + realm are stored, NEVER the password.
  let savedRealm = null;
  try {
    const raw = localStorage.getItem("rmm_login_hint");
    if (raw) {
      const hint = JSON.parse(raw);
      if (hint.username && userField) userField.value = hint.username;
      savedRealm = hint.realm || null;
    }
  } catch {}

  api.getLoginRealms().then((realms) => {
    for (const r of realms) {
      const opt = document.createElement("option");
      opt.value = r.id;
      opt.textContent = r.name + " (AD)";
      realmSelect.appendChild(opt);
    }
    // Select the saved realm if it still exists.
    if (savedRealm && [...realmSelect.options].some((o) => o.value === savedRealm)) {
      realmSelect.value = savedRealm;
    }
  }).catch(() => { /* backend maybe not ready yet - no matter, "Local" is enough */ });

  document.getElementById("login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const errBox = document.getElementById("login-error");
    errBox.classList.add("hidden");
    try {
      const username = userField.value;
      const res = await api.login(
        username,
        document.getElementById("login-password").value,
        realmSelect.value,
        document.getElementById("login-code")?.value || ""
      );
      // Remember username + realm (NOT the password).
      try {
        localStorage.setItem("rmm_login_hint", JSON.stringify({ username, realm: realmSelect.value }));
      } catch {}
      saveToken(res.token);
      await startSession(res.user);
    } catch (err) {
      // startSession hides the login immediately and shows the app screen. If
      // something fails after that, the login MUST be shown again - otherwise
      // you get stuck on the empty, "blurry" app screen and cannot log in
      // anymore. Discard the token and show the error visibly.
      try { clearToken(); } catch {}
      try { showOnly(loginScreen()); } catch {}
      // The backend reports "2fa_required" when password and username are
      // correct but the one-time code is still missing. Then do NOT show it as
      // an error but reveal the code field and set the focus.
      if (err && err.message === "2fa_required") {
        const row = document.getElementById("login-2fa-row");
        const code = document.getElementById("login-code");
        row?.classList.remove("hidden");
        errBox.classList.add("hidden");
        code?.focus();
        return;
      }
      errBox.textContent = err && err.message ? err.message : "Login failed";
      errBox.classList.remove("hidden");
      // After a failed attempt discard the old code - it is single-use anyway.
      const codeField = document.getElementById("login-code");
      if (codeField) codeField.value = "";
    }
  });
}

function initChangePwForm() {
  document.getElementById("change-pw-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const errBox = document.getElementById("cpw-error");
    errBox.classList.add("hidden");

    const current = document.getElementById("cpw-current").value;
    const newPw = document.getElementById("cpw-new").value;
    const repeat = document.getElementById("cpw-repeat").value;

    if (newPw !== repeat) {
      errBox.textContent = t("u_die_neuen_passworter_stimmen_nicht");
      errBox.classList.remove("hidden");
      return;
    }

    try {
      await api.changePassword(current, newPw);
      // After a successful change: fetch a fresh profile and start normally
      const user = await api.me();
      await startSession(user);
    } catch (err) {
      errBox.textContent = err.message;
      errBox.classList.remove("hidden");
    }
  });
}

// -----------------------------------------------------------------
// Wire up menus & global buttons
// -----------------------------------------------------------------

// Global search bar in the topbar: either enter a bytera:// address directly
// (-> module "url-scheme" opens the target) or search freely (devices + apps).
// Enter opens the top match resp. the entered address.
function initGlobalSearch() {
  const inp = document.getElementById("global-search");
  const box = document.getElementById("global-search-results");
  if (!inp || !box) return;

  function openUrl(url) {
    const api = getModuleApi("url-scheme");
    if (api && typeof api.resolve === "function") { api.resolve(url); return true; }
    window.notify?.('The "URL scheme" module is not active - bytera:// addresses only work once it is enabled.', "warning", 7000);
    return false;
  }

  function suggestions(q) {
    const out = [];
    const ql = q.toLowerCase();
    // devices
    for (const c of (state.clients || [])) {
      if (!ql || (c.hostname || "").toLowerCase().includes(ql)) {
        out.push({ icon: "🖥️", label: c.hostname || c.id, sub: "Open device",
                   run: () => window.byteraOpenTarget?.selectClient(c.id) });
      }
      if (out.length > 7) break;
    }
    // apps (tiles from the start menu)
    document.querySelectorAll("#start-menu [data-app], .app-catalog [data-app]").forEach((b) => {
      const label = b.textContent.trim();
      const app = b.dataset.app;
      if (app && (!ql || label.toLowerCase().includes(ql)) && out.length <= 12) {
        out.push({ icon: "📦", label, sub: "Open app", run: () => openAppFromMenu(app) });
      }
    });
    return out.slice(0, 10);
  }

  let items = [], active = -1;
  function render() {
    const q = inp.value.trim();
    if (!q) { box.classList.add("hidden"); box.innerHTML = ""; items = []; return; }
    if (/^bytera:\/\//i.test(q)) {
      items = [{ icon: "🔗", label: q, sub: "Open address", run: () => openUrl(q) }];
    } else {
      items = suggestions(q);
    }
    active = items.length ? 0 : -1;
    box.innerHTML = items.length
      ? items.map((it, i) => `<div class="gs-row${i === active ? " active" : ""}" data-i="${i}">
          <span class="gs-ic">${it.icon}</span><span class="gs-la">${esc(it.label)}</span><span class="gs-sub">${esc(it.sub)}</span></div>`).join("")
      : `<div class="gs-empty">No matches</div>`;
    box.classList.remove("hidden");
    box.querySelectorAll(".gs-row").forEach((r) =>
      r.addEventListener("mousedown", (e) => { e.preventDefault(); const it = items[+r.dataset.i]; close(); it && it.run(); }));
  }
  function close() { box.classList.add("hidden"); box.innerHTML = ""; }

  inp.addEventListener("input", render);
  inp.addEventListener("focus", render);
  inp.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { inp.value = ""; close(); inp.blur(); return; }
    if (!items.length) return;
    if (e.key === "ArrowDown") { e.preventDefault(); active = (active + 1) % items.length; }
    else if (e.key === "ArrowUp") { e.preventDefault(); active = (active - 1 + items.length) % items.length; }
    else if (e.key === "Enter") {
      e.preventDefault();
      const it = items[active] || items[0];
      const val = inp.value.trim();
      close(); inp.value = "";
      if (it) it.run(); else if (/^bytera:\/\//i.test(val)) openUrl(val);
      return;
    } else return;
    box.querySelectorAll(".gs-row").forEach((r, i) => r.classList.toggle("active", i === active));
  });
  document.addEventListener("click", (e) => { if (!e.target.closest(".topbar-search")) close(); });
}

function initMenusAndButtons() {
  // User menu (top right)
  const userMenuBtn = document.getElementById("user-menu-btn");
  const userMenu = document.getElementById("user-menu");
  userMenuBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    userMenu.classList.toggle("hidden");
  });
  // A click anywhere else closes the menu again
  document.addEventListener("click", (e) => {
    if (userMenu.classList.contains("hidden")) return;
    if (userMenu.contains(e.target) || userMenuBtn.contains(e.target)) return;
    userMenu.classList.add("hidden");
  });
  // Escape also closes it
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") userMenu.classList.add("hidden");
  });

  document.getElementById("btn-logout").addEventListener("click", () => {
    clearToken();
    location.reload();
  });
  document.getElementById("btn-open-profile").addEventListener("click", () => {
    userMenu.classList.add("hidden");
    openWindow({ key: "profile", appId: "profile", title: "Profile", w: 460, h: 560 });
  });
  document.getElementById("btn-open-notifications").addEventListener("click", () => {
    userMenu.classList.add("hidden");
    openWindow({ key: "notifycenter", appId: "notifycenter", title: "Notifications", w: 520, h: 600 });
  });
  // Dot on the user-menu button + on the menu entry while unread
  // notifications exist (color = most severe unread level).
  attachUnreadDot(userMenuBtn);
  attachUnreadDot(document.getElementById("btn-open-notifications"));
  document.getElementById("btn-open-settings").addEventListener("click", () => {
    userMenu.classList.add("hidden");
    openWindow({ key: "settings", appId: "settings", title: t("settings"), w: 560, h: 620 });
  });

  // Start menu (bottom left)
  const startBtn = document.getElementById("taskbar-menu-btn");
  const startMenu = document.getElementById("start-menu");
  startBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    startMenu.classList.toggle("hidden");
  });
  // Mobile: its own menu button in the topbar (three horizontal lines) opens
  // the app menu DIRECTLY, without showing/hiding the taskbar.
  const mobMenuBtn = document.getElementById("mobile-menu-btn");
  mobMenuBtn?.addEventListener("click", (e) => {
    e.stopPropagation();
    startMenu.classList.toggle("hidden");
  });
  // A click on the dimmed background (not on a tile) closes the menu
  startMenu.addEventListener("click", (e) => {
    if (e.target === startMenu) startMenu.classList.add("hidden");
  });
  // Escape also closes it
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") startMenu.classList.add("hidden");
  });

  // Open an app from the app menu (called by startmenu.js). Also reachable for
  // dashboard tiles: a tile can open an app this way (e.g. the music favorites
  // open the audio player).
  window.openRmmApp = (app) => openAppFromMenu(app);

  function openAppFromMenu(app) {
    startMenu.classList.add("hidden");
    if (openModuleApp(app)) return true;         // app of an active module
    if (app === "nodes") openWindow({ key: "nodes", appId: "node", title: "Nodes", w: 760, h: 560 });
    else if (app === "modules") openWindow({ key: "modules", appId: "modules", title: "Modules", w: 980, h: 680 });
    else if (app === "manage") openWindow({ key: "manage", appId: "manage", title: "Manage tenants & locations", w: 560, h: 620 });
    else if (app === "settings") openWindow({ key: "settings", appId: "settings", title: t("settings"), w: 560, h: 620 });
    else if (app === "permissions") openWindow({ key: "permissions", appId: "permissions", title: "Permissions", w: 820, h: 640 });
    else if (app === "audit") openWindow({ key: "audit", appId: "audit", title: "Audit log", w: 720, h: 520 });
    else if (app === "dist") openWindow({ key: "dist", appId: "dist", title: "Distribution (/dist)", w: 900, h: 640 });
    else if (app.startsWith("webapp:")) getModuleApi("browser")?.openWebApp?.(app);   // saved web app
    else if (app === "clients") { minimizeAll(); state.selection = null; renderMainContent(); }
    else return false;                           // unknown - the caller decides
    return true;
  }

  // Provide the open primitives for the URL-scheme module ("url-scheme") globally.
  // The module owns the format (parse/resolve) and calls these functions.
  // This keeps the format a module while the actual window logic stays in the core.
  window.byteraOpenTarget = {
    openApp: openAppFromMenu,
    openModuleApp,
    clientAction: (action, clientId, params) => {
      const c = (state.clients || []).find((x) => x.id === clientId);
      if (!c) return false;
      if (action === "edit") openWindow({ key: `edit-${c.id}`, appId: "edit-client",
        title: `${t("edit")} - ${c.hostname}`, props: { clientId: c.id }, w: 480, h: 520 });
      else _clientHandleAction(action, c, null, { forceWeb: true, params: params || null });
      return true;
    },
    // bytera://function/<name> -> open a core app/menu by name.
    runFunction: (name, _parts, _query) => {
      const n = String(name || "").toLowerCase();
      const map = { settings: "settings", audit: "audit", permissions: "permissions", modules: "modules",
                    nodes: "nodes", manage: "manage", dist: "dist", clients: "clients", profile: "profile" };
      if (map[n]) return openAppFromMenu(map[n]);
      return openAppFromMenu(n);     // possibly a module app
    },
    selectClient: (id) => { try { _selectClientExternal(id); return true; } catch { state.selection = { type: "client", id }; renderMainContent(); return true; } },
    openNode: (id) => openWindow({ key: id ? `node-${id}` : "nodes", appId: "node",
      title: id ? `Node - ${id}` : "Nodes", props: id ? { clientId: id } : {}, w: 820, h: 680 }) ?? true,
    openSpace: (name, fullPath) => {
      const api = getModuleApi("spaces");
      if (!api) return false;
      // If the link has a subpath (teams/project/folder), go there directly.
      const props = {};
      if (fullPath && fullPath.includes("/")) props.path = fullPath;
      else if (name) props.openSpace = name;
      openWindow({ key: "spaces", appId: "spaces", title: "Spaces", w: 900, h: 640, props });
      return true;
    },
    openDist: (folder) => { openAppFromMenu("dist"); if (folder) window.notify?.("Distribution opened - folder: " + folder, "info", 3000); return true; },
    notify: (m) => { try { window.notify?.(m, "error", 7000); } catch { /* ignore */ } },
  };
  // bytera://... from the desktop client / deep link still globally callable -
  // forwards to the module (if active).
  window.byteraOpen = (url) => { try { getModuleApi("url-scheme")?.resolve?.(url); } catch { /* ignore */ } };

  // Initialize the customizable app menu (drag & drop, folders).
  initStartMenu({
    root: startMenu,
    username: state.user?.username,
    onOpenApp: openAppFromMenu,
    allowed: _appAllowed,
  });
  // (Saved web apps are registered by the 'browser' module itself in the start menu.)

  // Sidebar: collapse/expand, drag width, click-to-expand. Details in
  // initSidebarResize() below (incl. persistence of width/state).
  initSidebarResize();

  // Manage tenants/locations (gear in the sidebar header)
  document.getElementById("btn-manage-hierarchy").addEventListener("click", () => {
    openWindow({ key: "manage", appId: "manage", title: "Manage tenants & locations", w: 560, h: 620 });
  });

  // "Add client"
  document.getElementById("btn-add-client").addEventListener("click", () => {
    openWindow({ key: "add-client", appId: "add-client", title: t("u_client_hinzufugen"), w: 560, h: 600 });
  });
}

// -----------------------------------------------------------------
// Sidebar: drag width, collapse/expand, click-to-expand
// -----------------------------------------------------------------
const SB_COLLAPSED_W = 46;   // width when collapsed (see CSS)
const SB_MIN_W = 170;        // smallest "expanded" width
const SB_MAX_W = 560;        // largest width
const SB_SNAP = 90;          // dragged narrower than this -> counts as collapsed

// Applies state.sidebar (width + collapsed) to the DOM.
function applySidebarState() {
  const sb = document.getElementById("sidebar");
  if (!sb) return;
  const s = state.sidebar || (state.sidebar = { width: 260, collapsed: false });
  if (s.collapsed) {
    sb.classList.add("collapsed");
    // Width stays at 46px via CSS; inline width remembers the expanded value.
    sb.style.width = (s.width || 260) + "px";
  } else {
    sb.classList.remove("collapsed");
    sb.style.width = (s.width || 260) + "px";
  }
}

function initSidebarResize() {
  const sb = document.getElementById("sidebar");
  const resizer = document.getElementById("sidebar-resizer");
  const collapseBtn = document.getElementById("sidebar-collapse-btn");
  if (!sb || !resizer) return;

  applySidebarState();

  const persist = () => { try { scheduleSave(state); } catch {} };

  function setCollapsed(collapsed) {
    state.sidebar.collapsed = collapsed;
    applySidebarState();
    persist();
  }

  // « button: collapse (only relevant when expanded).
  collapseBtn?.addEventListener("click", (e) => {
    e.stopPropagation();
    if (!state.sidebar.collapsed) setCollapsed(true);
    else setCollapsed(false);
  });

  // A click ANYWHERE on the collapsed sidebar -> expand again.
  sb.addEventListener("click", () => {
    if (state.sidebar.collapsed) setCollapsed(false);
  });

  // --- drag width ---
  let dragging = false;
  function onMove(clientX) {
    const left = sb.getBoundingClientRect().left;
    let w = clientX - left;
    if (w <= SB_SNAP) {
      // Dragged as narrow as collapsed -> mark as collapsed, but keep the
      // last chosen expanded width (for expanding again).
      if (!state.sidebar.collapsed) { state.sidebar.collapsed = true; sb.classList.add("collapsed"); }
      return;
    }
    // expanded again
    if (state.sidebar.collapsed) { state.sidebar.collapsed = false; sb.classList.remove("collapsed"); }
    w = Math.max(SB_MIN_W, Math.min(SB_MAX_W, w));
    state.sidebar.width = w;
    sb.style.width = w + "px";
  }
  resizer.addEventListener("mousedown", (e) => {
    e.preventDefault();
    dragging = true;
    sb.classList.add("resizing");
    resizer.classList.add("active");
    document.body.style.userSelect = "none";
  });
  window.addEventListener("mousemove", (e) => { if (dragging) onMove(e.clientX); });
  window.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false;
    sb.classList.remove("resizing");
    resizer.classList.remove("active");
    document.body.style.userSelect = "";
    persist();
  });
}

// -----------------------------------------------------------------
// Live updates via Socket.IO
// -----------------------------------------------------------------

function initLiveUpdates() {
  // The client list changed (new agent, online/offline) -> reload. We also
  // reload the hierarchy, because a newly registered client may have triggered
  // a new tenant (e.g. "Uncategorized") that would otherwise not appear in the
  // sidebar.
  dashboardSocket.on("clients:changed", async () => {
    await refreshAll();
    await reloadPerms();
    applyAppVisibility();
  });

  // Live feedback from the agent about update/uninstall (diagnostics): shows
  // what happens on the client - instead of just a 60s timeout.
  dashboardSocket.on("client:action-log", (d) => {
    const c = state.clients.find((x) => x.id === d.id);
    const name = c ? c.hostname : (d.id || "Client");
    const kind = d.kind === "uninstall" ? "Uninstall" : "Update";
    const level = d.stage === "error" ? "error"
      : (d.stage === "launched-fallback" ? "warn" : "info");
    let msg = `${name}: ${kind} - ${d.stage}`;
    if (d.detail) msg += `\n${d.detail}`;
    if (d.agent_version) msg += `\n(agent version: ${d.agent_version})`;
    window.notify?.(msg, level, 10000, { tag: `agent-${d.kind}:${d.id}` });
  });

  // Error during screen transmission (e.g. headless VM) as a notification
  // + entry in the audit log
  dashboardSocket.on("screen-error", (data) => {
    notifyError(data.error || t("u_fehler_bei_der_bildschirmubertragu"), "error", "remote-screen", 12000);
  });

  // Agent crash: the agent restarted itself and reports the error - show it
  // clearly to the user (the last traceback line is enough).
  dashboardSocket.on("client:agent-crashed", (d) => {
    const lastLine = String(d.error || "").trim().split("\n").filter(Boolean).slice(-1)[0] || t("app_unknown_error");
    notifyError(`⚠️ ${t("app_agent_crashed", { host: d.hostname || d.id, err: lastLine })}`,
      "error", `agent-crash-${d.id}`, 20000);
  });

  // New metrics for a client
  dashboardSocket.on("client:metrics", ({ id, metrics }) => {
    const client = state.clients.find((c) => c.id === id);
    const wasOnline = client ? !!client.online : true;
    const iconBefore = client ? clientIconType(client) : null;
    if (client) {
      client.metrics = metrics;
      client.online = true;
    }
    recordMetrics(id, metrics);
    try { updateBatteryIndicators(client); } catch {}
    // icon derived from the metrics (e.g. a battery appeared) -> redraw the tree
    if (client && wasOnline && iconBefore !== clientIconType(client)) { try { renderSidebar(); } catch {} }

    // Live refresh for dashboard widgets (aggregated fleet values).
    try { window.dispatchEvent(new CustomEvent("metrics-updated", { detail: { id } })); } catch {}

    // Targeted update: overwrite only the values/SVGs of this client's visible
    // panels - the client panel is NOT re-inserted.
    let touched = 0;
    try {
      touched = updateClientLayouts(id);
    } catch { touched = 0; }
    try { touched += runMetricsHooks(id); } catch {}   // styles (module "styles")
    // Fallback only for views that (still) cannot do a layout-host update.
    const sel = state.selection;
    if (!touched && sel && (
      (sel.type === "client" && sel.id === id) ||
      sel.type === "tenant" || sel.type === "location"
    )) {
      renderMainContent();
    }
    // Do NOT rebuild the sidebar on every metric tick (caused hover flicker/
    // jumping of the client rows) - only when the online status changed
    // visibly (offline -> online).
    if (!wasOnline) renderSidebar();
  });

  // Chat: new message -> toast if I am a member and not the sender myself.
  // (The chat app itself updates via the same event.)
  dashboardSocket.on("chat:message", (d) => {
    try {
      const me = state.user?.id;
      if (!me || d.sender_id === me) return;
      if (!(d.member_ids || []).includes(me)) return;
      window.notify?.(
        `💬 ${d.conversation_type === "group"
          ? `${d.sender_name} in "${d.conversation_name}"` : d.sender_name}:\n${d.preview || ""}`,
        "info", 8000, { source: "chat", tag: `chat:${d.conversation_id}` });
    } catch {}
  });

  // Notification rules with channel "Dashboard": the server pushes finished
  // notifications (head/body/level) to all connected dashboards.
  dashboardSocket.on("notify:push", (n) => {
    try {
      const level = ["info", "success", "warn", "error"].includes(n.level) ? n.level
        : (n.level === "warning" ? "warn" : (n.level === "danger" ? "error" : "info"));
      window.notify?.(`${n.head || ""}\n${n.body || n.message || ""}`.trim(),
        level, 10000, { source: "rule" });
    } catch {}
  });

  dashboardSocket.on("client:offline", ({ id }) => {
    const client = state.clients.find((c) => c.id === id);
    if (client) { client.online = false; client.metrics = null; }
    renderSidebar();
    renderMainContent();
  });
}

// -----------------------------------------------------------------
// Start
// -----------------------------------------------------------------

// Drops a client onto the desktop (window layer) as its own client window.
// The client stays in the sidebar.
function initDesktopDrop() {
  const layer = document.getElementById("window-layer");
  const dropZone = document.getElementById("main-content") || document.body;
  if (!dropZone) return;
  dropZone.addEventListener("dragover", (e) => {
    if (e.dataTransfer && [...e.dataTransfer.types].includes("text/x-rmm-client")) {
      e.preventDefault();
      e.dataTransfer.dropEffect = "copy";
      dropZone.classList.add("desktop-drop-hint");
    }
  });
  dropZone.addEventListener("dragleave", (e) => {
    if (e.target === dropZone) dropZone.classList.remove("desktop-drop-hint");
  });
  dropZone.addEventListener("drop", (e) => {
    const clientId = e.dataTransfer?.getData("text/x-rmm-client");
    dropZone.classList.remove("desktop-drop-hint");
    if (!clientId) return;
    e.preventDefault();
    const client = state.clients.find((c) => c.id === clientId);
    if (!client) return;
    const r = (layer || dropZone).getBoundingClientRect();
    openWindow({
      singleton: true,
      key: `panelpart-${clientId}-client-x`, appId: "panelpart",
      title: client.hostname,
      props: { clientId, part: "client" },
      clientColor: client.color,
      x: Math.max(0, e.clientX - r.left - 60),
      y: Math.max(0, e.clientY - r.top - 20),
      w: 900, h: 620,
    });
  });
}

// A single init step must not abort startup: each runs in its own try/catch.
// This keeps the login usable even if a later part (menus, live updates, ...)
// has problems.
function _step(name, fn) {
  try { fn(); } catch (e) { console.error("[init] " + name + ":", e); }
}

async function main() {
  // Load languages from all lang folders (translate the login window + buffer).
  try { await initLanguages(); } catch { /* offline -> buffer/fallback */ }
  _step("translations", applyStaticTranslations);

  // Wire up the login FIRST - before anything else can go wrong. This way
  // logging in is always possible as long as the core (this module) loads.
  _step("login-form", initLoginForm);
  _step("change-pw-form", initChangePwForm);

  _step("content-renderer", () => setContentRenderer(renderWindowContent));
  _step("on-select", () => setOnSelect(renderMainContent));
  _step("dashedit", () => window.addEventListener("dashedit-changed", () => renderMainContent()));
  // Lazily wire up the feature modules' "changes -> reload everything".
  loadFeature("../feature/editclient.js", "setEditOnChanged").then((fn) => fn && fn(refreshAll)).catch(() => {});
  loadFeature("../feature/manage.js", "setManageOnChanged").then((fn) => fn && fn(refreshAll)).catch(() => {});

  _step("rightclick-guard", installGlobalContextGuard);
  _step("menus", initMenusAndButtons);
  _step("global-search", initGlobalSearch);
  _step("taskbar", initTaskbar);
  _step("help", initHelp);
  _step("live-updates", initLiveUpdates);
  _step("desktop-drop", initDesktopDrop);

  // Is a valid token still present? Then log in directly.
  const token = localStorage.getItem("rmm_token");
  if (token) {
    try {
      const user = await api.me();
      await startSession(user);
      return;
    } catch {
      clearToken(); // token expired/invalid -> normal login
    }
  }
  showOnly(loginScreen());
}

main().catch((e) => {
  // Even if main() fails unexpectedly: show the login form and wire it up one
  // last time so you can still log in.
  console.error("[init] main:", e);
  try { initLoginForm(); } catch { /* ignore */ }
  try { showOnly(loginScreen()); } catch { /* ignore */ }
});
