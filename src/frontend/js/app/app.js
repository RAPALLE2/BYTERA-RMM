// app.js
// ------
// Der zentrale Einstiegspunkt des Frontends. Verantwortlich für:
//   1. Login-Ablauf (inkl. erzwungenem Passwortwechsel beim ersten Login)
//   2. Laden der Daten (Profil, Hierarchie, Clients) nach dem Login
//   3. Verkabeln aller Buttons (Startmenü, Benutzermenü, Sidebar, Taskbar)
//   4. Live-Updates über Socket.IO (Client online/offline, Metriken)
//   5. "Content-Router": entscheidet, welche App-Funktion den Inhalt eines
//      Fensters rendert (renderWindowContent)

import { state } from "./state.js";
import { hasAnyClientPerm, hasGlobalPerm, isAdmin } from "./state.js";
import { api, saveToken, clearToken } from "./api.js";
import { dashboardSocket } from "./socket.js";
import { applyTheme, applyAccent } from "./theme.js";
import { setLanguage, getLanguage, applyStaticTranslations } from "./i18n_apply.js";

import { renderSidebar, setOnSelect, getExpandedIds, setExpandedIds, setOnTreeStateChanged, initFavorites, initSidebarNav } from "./sidebar.js";
import { renderMainContent } from "./panel.js";
import { renderTaskbar, initTaskbar, consumeFreshStart } from "./taskbar.js";
import { initHelp } from "./help.js";
import { setContentRenderer, setOnWindowsChanged, openWindow, minimizeAll, closeWindow } from "./windowmanager.js";
import { recordMetrics } from "./metricshistory.js";
import { notify, notifyError } from "./notify.js";
import { configurePersistence, scheduleSave, saveNow, loadState, applyExpanded, setOrgDefaults, setOrgProfilePresets, hydrateFromServer } from "./persist.js";
import { initBars } from "./bars.js";
import { initCrashGuard } from "./crashguard.js";

// notify global verfügbar machen, damit alle Module (auch Fehlerbehandlung)
// die schönen Slide-Down-Meldungen nutzen können.
window.notify = notify;
window.notifyError = notifyError;

// App-Fenster-Renderer
// Registriert window.showPermissionDenied - der Dialog bei fehlenden Rechten.
import "./permdenied.js";
// renderDist wird LAZY geladen (siehe FEATURE_MODULES), damit eine
// einzelne fehlende Datei nie den Login der gesamten SPA blockiert.
import { loadModules, renderModuleApp, openModuleApp, moduleAppPerm, inactiveModuleBox, getModuleApi, onModulesChanged } from "./modules.js";
import { handleAction as _clientHandleAction } from "./panel.js";
import { selectClientExternal as _selectClientExternal } from "./sidebar.js";
import { renderNotifyCenter, attachUnreadDot } from "./notifycenter.js";
import { updateClientLayouts } from "./dashlayout.js";
import { initStartMenu, refreshStartMenu, setStartMenuUser, unlockCatalogPruning } from "./startmenu.js";
import { t, initLanguages, loadLocale } from "./i18n.js";

// -----------------------------------------------------------------
// Content-Router: welcher Renderer gehört zu welchem appId?
// -----------------------------------------------------------------

const _featureWarned = new Set();

async function loadFeature(mod, fn) {
  // Dynamisch laden, Fehler abfangen. Rueckgabe: Renderer-Funktion oder null.
  try {
    const m = await import(mod);
    if (typeof m[fn] !== "function") throw new Error("Funktion " + fn + " fehlt in " + mod);
    return m[fn];
  } catch (e) {
    if (!_featureWarned.has(mod)) {
      _featureWarned.add(mod);
      const msg = "Ein Teil des RMM konnte nicht geladen werden: " + mod.split("/").pop()
        + " (" + (e && e.message ? e.message : e) + "). Der Rest funktioniert weiter.";
      try { notifyError ? notifyError(msg) : notify(msg, "error", 9000); } catch { /* egal */ }
      console.error("[feature]", mod, e);
    }
    return null;
  }
}

async function _lazyRender(mod, fn, body, win) {
  const r = await loadFeature(mod, fn);
  if (r) return r(body, win);
  body.innerHTML = '<div style="padding:16px;color:var(--danger)">Diese App konnte nicht geladen werden.'
    + '<br><small>Vermutlich wurde beim Update eine Datei nicht mit hochgeladen. '
    + 'Login und die übrigen Funktionen laufen normal weiter.</small></div>';
}

// Jede App wird ERST beim Oeffnen geladen (dynamic import). Faellt eine Datei
// aus (nach einem Update nicht mit hochgeladen, Syntaxfehler, 404), bleibt NUR
// diese App betroffen - Login, Shell und alle anderen Apps laufen weiter. Das
// ist das "try: import ... except: Hinweis"-Prinzip fuer das ganze Frontend.
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
  else if (renderModuleApp(win.appId, body, win)) { /* App eines aktiven Moduls */ }
  else inactiveModuleBox(body, win.appId);   // Modul aus oder abgestuerzt
}

// -----------------------------------------------------------------
// Daten laden & Oberfläche aktualisieren
// -----------------------------------------------------------------

async function reloadHierarchy() {
  state.hierarchy = await api.getHierarchy();
  // Offene Fenster (Add/Edit Client, ...) informieren, damit sie ihre
  // Tenant-/Standort-Auswahlen SOFORT neu befüllen - ohne Fenster-Neuöffnen.
  try { window.dispatchEvent(new CustomEvent("rmm:hierarchy-changed")); } catch {}
}

async function reloadClients() {
  state.clients = await api.getClients();
}

// Effektive Rechte des Benutzers laden (für Frontend-Gating).
async function reloadPerms() {
  try {
    state.perms = await api.getEffectivePermissions();
  } catch {
    // Fallback: keine Rechte-Info -> als "nur Admin sieht alles" behandeln wäre
    // unsicher; stattdessen leere Rechte (Backend bleibt die harte Grenze).
    state.perms = { admin: false, global: {}, clients: {} };
  }
}

// Apps, die ausschließlich dem Super-Admin offenstehen (Backend: require_admin).
// App-Key -> benötigtes globales Recht. Nicht gelistete Apps sind frei
// zugänglich (z.B. Dashboard/Clients, Speedtest). 'settings' ist ein Sonderfall.
const APP_REQUIRED_PERM = {
  audit: "see_audit",
  permissions: "see_permissions",
  manage: "manage_hierarchy",
  "add-client": "add_client",
  dist: "manage_dist",          // Verteilung (/dist) - Core, frueher Modul "dist"
};

function _appAllowed(appKey) {
  if (isAdmin()) return true;
  // Einstellungen: sichtbar, wenn der Nutzer wenigstens einen Settings-Bereich
  // sehen/bearbeiten darf (Einstellungen ansehen, SSO, Branding, Source oder
  // Benutzer erstellen). Die einzelnen Tabs werden in der App zusätzlich gegated.
  if (appKey === "settings") {
    return hasGlobalPerm("see_settings") ||        // SSO: eigenes Modul "sso"
           hasGlobalPerm("manage_branding") || hasGlobalPerm("see_source") ||
           hasGlobalPerm("create_users");
  }
  if (appKey.startsWith("webapp:")) return true;          // gespeicherte Web-Apps (Modul 'browser')
  if (appKey === "modules") return hasGlobalPerm("manage_modules");   // Modul-App: globales Recht
  if (appKey === "nodes") return hasAnyClientPerm("manage_nodes");    // Node-Uebersicht: Client-Recht auf mind. einem Client
  const mperm = moduleAppPerm(appKey);                     // App eines Moduls: Recht aus der module.json
  if (mperm !== undefined) return !mperm || hasGlobalPerm(mperm);
  const perm = APP_REQUIRED_PERM[appKey];
  if (!perm) return true;               // frei zugängliche App
  return hasGlobalPerm(perm);
}

function applyAppVisibility() {
  // Startmenü-Katalog (versteckte Original-Buttons) + Live-Menü aktualisieren.
  document.querySelectorAll("#start-menu [data-app]").forEach((btn) => {
    btn.style.display = _appAllowed(btn.dataset.app) ? "" : "none";
  });
  try { refreshStartMenu(); } catch {}
  // Benutzermenü: Einstellungen nur, wenn erlaubt.
  const settingsBtn = document.getElementById("btn-open-settings");
  if (settingsBtn) settingsBtn.style.display = _appAllowed("settings") ? "" : "none";
  // Sidebar: "Client hinzufügen" (Enrollment) hängt am Recht 'add_client',
  // "Hierarchie verwalten" an 'manage_hierarchy'.
  const addBtn = document.getElementById("btn-add-client");
  if (addBtn) addBtn.style.display = (isAdmin() || hasGlobalPerm("add_client")) ? "" : "none";
  const mgmtBtn = document.getElementById("btn-manage-hierarchy");
  if (mgmtBtn) mgmtBtn.style.display = (isAdmin() || hasGlobalPerm("manage_hierarchy")) ? "" : "none";
}

// Name im Benutzermenü - mit ADMIN-Tag, wenn Vollzugriff besteht. Das gilt
// sowohl für die Rolle 'admin' als auch für das Recht 'super_admin'
// (admin_via === "permission" wird mit * gekennzeichnet).
function drawUserMenuName(user) {
  const nameEl = document.getElementById("user-menu-name");
  if (!nameEl || !user) return;
  nameEl.textContent = user.display_name;
  if (!user.is_admin) return;
  const tag = document.createElement("span");
  tag.textContent = user.admin_via === "permission" ? "ADMIN*" : "ADMIN";
  tag.title = user.admin_via === "permission"
    ? t("u_vollzugriff_uber_das_recht_super_a") : "Rolle admin";
  tag.style.cssText = "color:var(--warn,#f5a524);font-size:10px;font-weight:700;margin-left:5px";
  nameEl.appendChild(tag);
}

/**
 * Lädt Benutzer-Infos UND effektive Rechte neu und zeichnet alles nach, was
 * davon abhängt (ADMIN-Tag, Startmenü, Sidebar-Buttons, Hauptbereich).
 * Wird nach dem Speichern in der Rechte-Verwaltung aufgerufen, damit
 * Änderungen sofort sichtbar sind - ohne Strg+F5.
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
    try { console.error("[perms] Aktualisieren fehlgeschlagen:", e); } catch {}
  }
}

async function refreshAll() {
  await Promise.all([reloadHierarchy(), reloadClients()]);
  renderSidebar();
  renderMainContent();
}

// -----------------------------------------------------------------
// Version: Single Source of Truth ist backend/version.txt (via
// /api/version). Alle Anzeigen im Frontend werden von hier befüllt -
// nirgends mehr hardcoden!
// -----------------------------------------------------------------
async function loadVersion() {
  try {
    const v = await api.getVersions(); // { backend, agent }
    document.querySelectorAll(".topbar-version").forEach((el) => {
      el.textContent = v.backend || "";
      el.title = `Backend ${v.backend || "?"} · Agent ${v.agent || "?"}`;
    });
  } catch { /* Version ist Kosmetik - Fehler nie den Start blockieren lassen */ }
}
loadVersion();

// -----------------------------------------------------------------
// Login-Ablauf
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
  try { await loadLocale(user.language || "de"); } catch { /* Puffer/Fallback */ }
  setLanguage(user.language || "de");
  applyStaticTranslations();

  // Muss der User zuerst sein Passwort wechseln? (erster Login / Einmalpasswort)
  if (user.must_change_pw) {
    showOnly(changePwScreen());
    return;
  }

  showOnly(appScreen());
  drawUserMenuName(user);

  // Persistenz einrichten (pro Benutzer). Aufgeklappte Sidebar-Knoten müssen
  // VOR dem ersten Render gesetzt werden, damit der Baum sofort korrekt aussieht.
  configurePersistence({
    username: user.username,
    getExpanded: getExpandedIds,
    setExpanded: setExpandedIds,
  });
  // Serverseitig gespeicherte UI-Einstellungen holen und in localStorage
  // übernehmen (Server gewinnt) - VOR initFavorites()/loadState(), damit
  // Favoriten, Layouts & Co. in jedem Browser identisch sind.
  await hydrateFromServer();
  // Kopf-/Taskleiste automatisch ausblenden (pro Benutzer, rmm_bars:<user> wurde hydriert)
  try { initBars(user.username); } catch (e) { try { console.error(e); } catch {} }
  // Sprache ggf. auf den Server-Stand umschalten (rmm_lang wurde hydriert).
  try {
    const lang = localStorage.getItem("rmm_lang");
    if (lang && lang !== getLanguage()) setLanguage(lang);
  } catch {}
  // Startmenue auf den ECHTEN Benutzer umstellen. initStartMenu() lief schon
  // vor dem Login (state.user war null) und haette das Layout sonst dauerhaft
  // unter "rmm_appmenu:default" gehalten - dieser Schluessel wird nicht mit dem
  // Server abgeglichen, weshalb Ordner/Anordnung unter einer anderen URL fehlten.
  try { setStartMenuUser(user.username); } catch (e) { try { console.error(e); } catch {} }
  initFavorites(user.username);   // Favoriten des Benutzers laden
  initSidebarNav();               // Dashboard-Tab + Favoriten-Header verkabeln

  // Effektive Rechte ZUERST laden – sie werden sowohl für die Restore-Logik
  // (Recht 'restore_session') als auch fürs UI-Gating gebraucht. Früher lief
  // reloadPerms() erst später, wodurch 'restore_session' immer als "fehlt"
  // galt und NICHTS wiederhergestellt wurde. (Rechte hängen nicht an Clients/
  // Hierarchie, können also gefahrlos zuerst geladen werden.)
  await reloadPerms();
  // Aktive Module laden (ein Fehler in einem Modul betrifft nur dieses). Faellt
  // das Laden ganz aus, laeuft die Shell trotzdem - nur die Modul-Apps fehlen.
  try { await loadModules(); } catch (e) { console.error("[modules] laden:", e); }
  unlockCatalogPruning();     // jetzt kennt der Katalog auch die Modul-Apps
  onModulesChanged(() => { try { applyAppVisibility(); } catch {} });

  const saved = loadState();
  // Persönliche Wiederherstellungs-Einstellungen (Standard: alles wiederherstellen).
  // Zusätzlich durch das globale Recht 'restore_session' gegated: fehlt es, wird
  // immer sauber vom Dashboard gestartet.
  const mayRestore = hasGlobalPerm("restore_session");
  // Sauberer Neustart über den Reload-Knopf in der Taskleiste: dieser eine
  // Start ignoriert die gespeicherten Fenster/Apps und die Client-Auswahl.
  // Der Merker wird dabei verbraucht - der naechste Reload verhaelt sich
  // wieder normal.
  const freshStart = consumeFreshStart();
  // Einzelfenster-Modus (Client-App, z.B. Remote-Shell im eigenen Fenster):
  //   /?solo=terminal&client=<id>   (auch explorer, vnc)
  //   /?solo=console[&via=client]    RMM-Konsole (Modul "console", ohne Geraet)
  // Zeigt nur dieses eine Fenster, stellt NICHTS wieder her und SPEICHERT
  // nichts - die normale Fensteranordnung des Benutzers bleibt unberührt.
  const soloParams = new URLSearchParams(location.search);
  const solo = SOLO_APPS.includes(soloParams.get("solo")) ? soloParams.get("solo") : null;
  const savedPrefs = (saved && saved.restorePrefs) || { client: true, folder: true, apps: true };
  let restorePrefs;
  if (!mayRestore) {
    restorePrefs = { client: false, folder: false, apps: false };
  } else if (freshStart) {
    // Aufgeklappte Ordner der Sidebar bleiben erhalten - nur der ausgewählte
    // Client und die offenen Apps/Fenster werden bewusst weggelassen.
    restorePrefs = { client: false, folder: !!savedPrefs.folder, apps: false };
  } else {
    restorePrefs = savedPrefs;
  }
  if (freshStart) {
    try { console.info("[startSession] clean start: open windows will not be restored."); } catch {}
  }
  // WICHTIG: Restore/Crash-Guard-Init defensiv kapseln. Ein Fehler hier darf den
  // Login/das Dashboard NIEMALS blockieren (sonst bleibt man im leeren, „blurry"
  // App-Screen hängen, weil das Login schon ausgeblendet ist).
  try {
    if (saved) {
      applyExpanded(saved);           // Aufklapp-Zustand + Layouts/Profile
      // „Offene Ordner" (Sidebar-Baum) nur wiederherstellen, wenn gewünscht.
      if (!restorePrefs.folder) {
        try { setExpandedIds([]); } catch {}
      }
      // Auswahl: Client vs. Ordner getrennt steuerbar.
      if (saved.selection) {
        const selIsClient = saved.selection.type === "client";
        if ((selIsClient && restorePrefs.client) || (!selIsClient && restorePrefs.folder)) {
          state.selection = saved.selection;
        }
      }
      if (restorePrefs.folder && saved.sidebar) { state.sidebar = saved.sidebar; applySidebarState(); }
    }

    // Frontend-Crash-Guard: erkennt Abstürze/Einfrieren, protokolliert sie und
    // startet die Oberfläche im Ernstfall neu (schleifensicher, siehe crashguard.js).
    initCrashGuard({
      username: user.username,
      onPanic: () => {
        try { for (const w of [...(state.windows || [])]) closeWindow(w.key); } catch {}
        try { saveNow(state); } catch {}   // ohne offene Fenster sichern
      },
    });
  } catch (e) {
    try { console.error("[startSession] Restore/CrashGuard-Init fehlgeschlagen:", e); } catch {}
  }

  // Organisationsweite Standard-Layouts laden (vom Admin gesetzt). Werden als
  // Basis für Nutzer OHNE eigenes Layout und beim "Auf Standard zurücksetzen"
  // verwendet. Fehler hier sind unkritisch (dann greift der eingebaute
  // Standard).
  try {
    const defs = await api.getDefaultLayouts();
    setOrgDefaults({ dash: defs.dash || null, fleet: defs.fleet || null });
    // Org-weite Profil-Presets (Physisch/VMs/LXCs) für die Client-Ansicht.
    setOrgProfilePresets({
      "Physisch": defs.dash_profile_physical || null,
      "VMs": defs.dash_profile_vm || null,
      "LXCs": defs.dash_profile_lxc || null,
    });
  } catch { /* kein org-Standard erreichbar -> eingebauter Standard */ }

  // (Effektive Rechte wurden bereits oben – vor der Restore-Logik – geladen.)
  // AB HIER darf kein Fehler mehr aus startSession herausfliegen: sonst würde der
  // Login-Handler den App-Screen wieder ausblenden und man käme nicht mehr rein.
  // Jeder Schritt ist daher einzeln gekapselt – im Zweifel erscheint ein (ggf.
  // leeres) Dashboard statt einer Login-Sackgasse.
  try { await refreshAll(); }
  catch (e) { try { console.error("[startSession] refreshAll fehlgeschlagen:", e); } catch {} }

  try { applyAppVisibility(); } catch (e) { try { console.error(e); } catch {} }

  // Gespeicherte Fenster wiederherstellen (nach refreshAll, damit Clients/
  // Hierarchie geladen sind und die Fenster-Inhalte korrekt rendern) – nur wenn
  // der Benutzer „offene Apps wiederherstellen" aktiviert hat.
  if (solo) restorePrefs = { client: false, folder: false, apps: false };
  try { if (saved && restorePrefs.apps) restoreWindows(saved); }
  catch (e) { try { console.error("[startSession] restoreWindows fehlgeschlagen:", e); } catch {} }

  // Ab jetzt jede Fenster-/Baum-Änderung speichern.
  try {
    if (solo) {
      setOnWindowsChanged(() => renderTaskbar());          // Einzelfenster: nichts speichern
      setOnTreeStateChanged(() => {});
    } else {
      setOnWindowsChanged(() => { renderTaskbar(); scheduleSave(state); });
      setOnTreeStateChanged(() => scheduleSave(state));
    }
    renderTaskbar();
  } catch (e) { try { console.error(e); } catch {} }
  if (solo) { openSolo(solo, soloParams); return; }

  // Ungelesene Chat-Nachrichten beim Login als Notification anzeigen
  // (nur mit Recht 'use_chat'; Fehler hier sind unkritisch).
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
  } catch (e) { try { console.error("[chat] Ungelesen-Check fehlgeschlagen:", e); } catch {} }
}

// Öffnet die zuletzt offenen Fenster in ihrer gespeicherten Reihenfolge,
// Geometrie und mit ihrem Status (minimiert/maximiert) erneut.
// Apps, die im Einzelfenster-Modus geöffnet werden dürfen (alle brauchen einen Client).
const SOLO_APPS = ["terminal", "explorer", "vnc", "console"];

function openSolo(appId, params) {
  if (appId === "console") {                 // RMM-Konsole (Modul) - braucht kein Geraet
    document.body.classList.add("solo-mode");
    document.title = "RMM-Konsole";
    openWindow({ key: "solo-console", appId: "console", title: "RMM-Konsole", maximized: true,
                 props: { via: params.get("via") || "" } });
    return;
  }
  const cid = params.get("client") || "";
  const client = (state.clients || []).find((c) => c.id === cid);
  document.body.classList.add("solo-mode");
  if (!client) {
    window.notify?.("Gerät nicht gefunden oder kein Zugriff.", "error", 10000);
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
  // In Fokus-Reihenfolge öffnen (letztes = oberstes). Fenster, die nicht in
  // focusOrder stehen, hinten anstellen.
  const order = saved.focusOrder || [];
  wins.sort((a, b) => order.indexOf(a.key) - order.indexOf(b.key));
  for (const w of wins) {
    try {
      openWindow({
        singleton: true,   // Restore: exakt diesen Key wiederherstellen
        key: w.key, appId: w.appId, title: w.title, props: w.props || {},
        clientColor: w.clientColor, w: w.w, h: w.h, x: w.x, y: w.y,
        minimized: w.minimized, maximized: w.maximized, pinned: !!w.pinned,
        focus: false,   // Fokus am Ende gesetzt (Reihenfolge)
      });
    } catch (e) {
      console.warn("[persist] could not restore window:", w.key, e);
    }
  }
}

function initLoginForm() {
  // Verfügbare AD-Realms ins Dropdown laden (zusätzlich zu "Lokal")
  const realmSelect = document.getElementById("login-realm");
  const userField = document.getElementById("login-username");

  // Zuletzt genutzten Benutzernamen + Realm wiederherstellen (wie bei Proxmox).
  // Gespeichert wird NUR Benutzername + Realm, NIEMALS das Passwort.
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
    // Gespeicherten Realm wählen, sofern noch vorhanden.
    if (savedRealm && [...realmSelect.options].some((o) => o.value === savedRealm)) {
      realmSelect.value = savedRealm;
    }
  }).catch(() => { /* Backend evtl. noch nicht bereit - egal, "Lokal" reicht */ });

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
      // Benutzername + Realm merken (Passwort NICHT).
      try {
        localStorage.setItem("rmm_login_hint", JSON.stringify({ username, realm: realmSelect.value }));
      } catch {}
      saveToken(res.token);
      await startSession(res.user);
    } catch (err) {
      // startSession blendet das Login sofort aus und zeigt den App-Screen.
      // Schlägt danach etwas fehl, MUSS das Login wieder eingeblendet werden –
      // sonst hängt man im leeren, „blurry" App-Screen fest und kann sich nicht
      // mehr anmelden. Token verwerfen und Fehler sichtbar anzeigen.
      try { clearToken(); } catch {}
      try { showOnly(loginScreen()); } catch {}
      // Das Backend meldet "2fa_required", wenn Passwort und Benutzername
      // stimmen, aber noch der Einmalcode fehlt. Dann NICHT als Fehler
      // darstellen, sondern das Code-Feld einblenden und den Fokus setzen.
      if (err && err.message === "2fa_required") {
        const row = document.getElementById("login-2fa-row");
        const code = document.getElementById("login-code");
        row?.classList.remove("hidden");
        errBox.classList.add("hidden");
        code?.focus();
        return;
      }
      errBox.textContent = err && err.message ? err.message : "Anmeldung fehlgeschlagen";
      errBox.classList.remove("hidden");
      // Nach einem Fehlversuch den alten Code verwerfen - er gilt ohnehin nur einmal.
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
      // Nach erfolgreicher Änderung: frisches Profil holen und normal starten
      const user = await api.me();
      await startSession(user);
    } catch (err) {
      errBox.textContent = err.message;
      errBox.classList.remove("hidden");
    }
  });
}

// -----------------------------------------------------------------
// Menüs & globale Buttons verkabeln
// -----------------------------------------------------------------

// Globale Suchleiste in der Topbar: entweder eine bytera://-Adresse direkt
// eingeben (-> Modul "url-scheme" oeffnet das Ziel) oder frei suchen (Geraete +
// Apps). Enter oeffnet den obersten Treffer bzw. die eingegebene Adresse.
function initGlobalSearch() {
  const inp = document.getElementById("global-search");
  const box = document.getElementById("global-search-results");
  if (!inp || !box) return;

  function openUrl(url) {
    const api = getModuleApi("url-scheme");
    if (api && typeof api.resolve === "function") { api.resolve(url); return true; }
    window.notify?.('Das Modul „URL-Schema" ist nicht aktiv - bytera://-Adressen gehen erst, wenn es eingeschaltet ist.', "warning", 7000);
    return false;
  }

  function suggestions(q) {
    const out = [];
    const ql = q.toLowerCase();
    // Geraete
    for (const c of (state.clients || [])) {
      if (!ql || (c.hostname || "").toLowerCase().includes(ql)) {
        out.push({ icon: "🖥️", label: c.hostname || c.id, sub: "Gerät öffnen",
                   run: () => window.byteraOpenTarget?.selectClient(c.id) });
      }
      if (out.length > 7) break;
    }
    // Apps (Kacheln aus dem Startmenue)
    document.querySelectorAll("#start-menu [data-app], .app-catalog [data-app]").forEach((b) => {
      const label = b.textContent.trim();
      const app = b.dataset.app;
      if (app && (!ql || label.toLowerCase().includes(ql)) && out.length <= 12) {
        out.push({ icon: "📦", label, sub: "App öffnen", run: () => openAppFromMenu(app) });
      }
    });
    return out.slice(0, 10);
  }

  let items = [], active = -1;
  function render() {
    const q = inp.value.trim();
    if (!q) { box.classList.add("hidden"); box.innerHTML = ""; items = []; return; }
    if (/^bytera:\/\//i.test(q)) {
      items = [{ icon: "🔗", label: q, sub: "Adresse öffnen", run: () => openUrl(q) }];
    } else {
      items = suggestions(q);
    }
    active = items.length ? 0 : -1;
    box.innerHTML = items.length
      ? items.map((it, i) => `<div class="gs-row${i === active ? " active" : ""}" data-i="${i}">
          <span class="gs-ic">${it.icon}</span><span class="gs-la">${esc(it.label)}</span><span class="gs-sub">${esc(it.sub)}</span></div>`).join("")
      : `<div class="gs-empty">Keine Treffer</div>`;
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
  // Benutzermenü (oben rechts)
  const userMenuBtn = document.getElementById("user-menu-btn");
  const userMenu = document.getElementById("user-menu");
  userMenuBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    userMenu.classList.toggle("hidden");
  });
  // Klick irgendwo anders schließt das Menü wieder
  document.addEventListener("click", (e) => {
    if (userMenu.classList.contains("hidden")) return;
    if (userMenu.contains(e.target) || userMenuBtn.contains(e.target)) return;
    userMenu.classList.add("hidden");
  });
  // Escape schließt ebenfalls
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") userMenu.classList.add("hidden");
  });

  document.getElementById("btn-logout").addEventListener("click", () => {
    clearToken();
    location.reload();
  });
  document.getElementById("btn-open-profile").addEventListener("click", () => {
    userMenu.classList.add("hidden");
    openWindow({ key: "profile", appId: "profile", title: "Profil", w: 460, h: 560 });
  });
  document.getElementById("btn-open-notifications").addEventListener("click", () => {
    userMenu.classList.add("hidden");
    openWindow({ key: "notifycenter", appId: "notifycenter", title: "Benachrichtigungen", w: 520, h: 600 });
  });
  // Punkt am Benutzermenü-Button + am Menüeintrag, solange ungelesene
  // Benachrichtigungen vorhanden sind (Farbe = schwerste ungelesene Stufe).
  attachUnreadDot(userMenuBtn);
  attachUnreadDot(document.getElementById("btn-open-notifications"));
  document.getElementById("btn-open-settings").addEventListener("click", () => {
    userMenu.classList.add("hidden");
    openWindow({ key: "settings", appId: "settings", title: t("settings"), w: 560, h: 620 });
  });

  // Startmenü (unten links)
  const startBtn = document.getElementById("taskbar-menu-btn");
  const startMenu = document.getElementById("start-menu");
  startBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    startMenu.classList.toggle("hidden");
  });
  // Klick auf den abgedunkelten Hintergrund (nicht auf eine Kachel) schließt das Menü
  startMenu.addEventListener("click", (e) => {
    if (e.target === startMenu) startMenu.classList.add("hidden");
  });
  // Escape schließt ebenfalls
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") startMenu.classList.add("hidden");
  });

  // App aus dem Anwendungsmenü öffnen (von startmenu.js aufgerufen).
  // Auch fuer Dashboard-Kacheln erreichbar: eine Kachel kann so eine App
  // oeffnen (z.B. die Musik-Favoriten den Audio-Player).
  window.openRmmApp = (app) => openAppFromMenu(app);

  function openAppFromMenu(app) {
    startMenu.classList.add("hidden");
    if (openModuleApp(app)) return true;         // App eines aktiven Moduls
    if (app === "nodes") openWindow({ key: "nodes", appId: "node", title: "Nodes", w: 760, h: 560 });
    else if (app === "modules") openWindow({ key: "modules", appId: "modules", title: "Module", w: 980, h: 680 });
    else if (app === "manage") openWindow({ key: "manage", appId: "manage", title: "Tenants & Standorte verwalten", w: 560, h: 620 });
    else if (app === "settings") openWindow({ key: "settings", appId: "settings", title: t("settings"), w: 560, h: 620 });
    else if (app === "permissions") openWindow({ key: "permissions", appId: "permissions", title: "Berechtigungen", w: 820, h: 640 });
    else if (app === "audit") openWindow({ key: "audit", appId: "audit", title: "Audit-Log", w: 720, h: 520 });
    else if (app === "dist") openWindow({ key: "dist", appId: "dist", title: "Verteilung (/dist)", w: 900, h: 640 });
    else if (app.startsWith("webapp:")) getModuleApi("browser")?.openWebApp?.(app);   // gespeicherte Web-App
    else if (app === "clients") { minimizeAll(); state.selection = null; renderMainContent(); }
    else return false;                           // unbekannt - der Aufrufer entscheidet
    return true;
  }

  // Oeffnen-Primitive fuer das URL-Schema-Modul ("url-scheme") global bereitstellen.
  // Das Modul besitzt das Format (parse/resolve) und ruft diese Funktionen auf.
  // So bleibt das Format ein Modul, die eigentliche Fenster-Logik aber im Core.
  window.byteraOpenTarget = {
    openApp: openAppFromMenu,
    openModuleApp,
    clientAction: (action, clientId) => {
      const c = (state.clients || []).find((x) => x.id === clientId);
      if (!c) return false;
      if (action === "edit") openWindow({ key: `edit-${c.id}`, appId: "edit-client",
        title: `${t("edit")} — ${c.hostname}`, props: { clientId: c.id }, w: 480, h: 520 });
      else _clientHandleAction(action, c, null, { forceWeb: true });
      return true;
    },
    selectClient: (id) => { try { _selectClientExternal(id); return true; } catch { state.selection = { type: "client", id }; renderMainContent(); return true; } },
    openNode: (id) => openWindow({ key: id ? `node-${id}` : "nodes", appId: "node",
      title: id ? `Node – ${id}` : "Nodes", props: id ? { clientId: id } : {}, w: 820, h: 680 }) ?? true,
    openSpace: (name) => {
      const api = getModuleApi("spaces");
      if (!api) return false;
      openWindow({ key: "spaces", appId: "spaces", title: "Spaces", w: 900, h: 640,
                   props: name ? { openSpace: name } : {} });
      return true;
    },
    openDist: (folder) => { openAppFromMenu("dist"); if (folder) window.notify?.("Verteilung geöffnet – Ordner: " + folder, "info", 3000); return true; },
    notify: (m) => { try { window.notify?.(m, "error", 7000); } catch { /* egal */ } },
  };
  // bytera://... aus dem Desktop-Client / Deep-Link weiterhin global aufrufbar -
  // leitet an das Modul weiter (wenn aktiv).
  window.byteraOpen = (url) => { try { getModuleApi("url-scheme")?.resolve?.(url); } catch { /* egal */ } };

  // Anpassbares Anwendungsmenü (Drag & Drop, Ordner) initialisieren.
  initStartMenu({
    root: startMenu,
    username: state.user?.username,
    onOpenApp: openAppFromMenu,
    allowed: _appAllowed,
  });
  // (Gespeicherte Web-Apps registriert das Modul 'browser' selbst im Startmenue.)

  // Sidebar: Ein-/Ausklappen, Breite ziehen, Klick-zum-Ausklappen. Details in
  // initSidebarResize() unten (inkl. Persistenz der Breite/Zustand).
  initSidebarResize();

  // Tenants/Standorte verwalten (Zahnrad in der Sidebar-Kopfzeile)
  document.getElementById("btn-manage-hierarchy").addEventListener("click", () => {
    openWindow({ key: "manage", appId: "manage", title: "Tenants & Standorte verwalten", w: 560, h: 620 });
  });

  // "Client hinzufügen"
  document.getElementById("btn-add-client").addEventListener("click", () => {
    openWindow({ key: "add-client", appId: "add-client", title: t("u_client_hinzufugen"), w: 560, h: 600 });
  });
}

// -----------------------------------------------------------------
// Sidebar: Breite ziehen, Ein-/Ausklappen, Klick-zum-Ausklappen
// -----------------------------------------------------------------
const SB_COLLAPSED_W = 46;   // Breite im eingeklappten Zustand (siehe CSS)
const SB_MIN_W = 170;        // kleinste "ausgeklappte" Breite
const SB_MAX_W = 560;        // größte Breite
const SB_SNAP = 90;          // zieht man schmaler als das -> gilt als eingeklappt

// Wendet state.sidebar (Breite + collapsed) auf die DOM an.
function applySidebarState() {
  const sb = document.getElementById("sidebar");
  if (!sb) return;
  const s = state.sidebar || (state.sidebar = { width: 260, collapsed: false });
  if (s.collapsed) {
    sb.classList.add("collapsed");
    // Breite bleibt per CSS auf 46px; inline-Breite merkt sich den Ausklapp-Wert.
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

  // « -Button: einklappen (nur relevant, wenn ausgeklappt).
  collapseBtn?.addEventListener("click", (e) => {
    e.stopPropagation();
    if (!state.sidebar.collapsed) setCollapsed(true);
    else setCollapsed(false);
  });

  // Klick IRGENDWO auf die eingeklappte Sidebar -> wieder ausklappen.
  sb.addEventListener("click", () => {
    if (state.sidebar.collapsed) setCollapsed(false);
  });

  // --- Breite ziehen ---
  let dragging = false;
  function onMove(clientX) {
    const left = sb.getBoundingClientRect().left;
    let w = clientX - left;
    if (w <= SB_SNAP) {
      // So schmal wie eingeklappt gezogen -> als eingeklappt markieren,
      // aber die zuletzt gewählte Ausklapp-Breite behalten (für's Wieder-Auf).
      if (!state.sidebar.collapsed) { state.sidebar.collapsed = true; sb.classList.add("collapsed"); }
      return;
    }
    // wieder ausgeklappt
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
// Live-Updates über Socket.IO
// -----------------------------------------------------------------

function initLiveUpdates() {
  // Client-Liste hat sich geändert (neuer Agent, online/offline) -> neu laden.
  // Wir laden auch die Hierarchie neu, weil ein neu registrierter Client einen
  // neuen Tenant (z.B. "Uncategorized") ausgelöst haben könnte, der sonst nicht
  // in der Sidebar auftauchen würde.
  dashboardSocket.on("clients:changed", async () => {
    await refreshAll();
    await reloadPerms();
    applyAppVisibility();
  });

  // Live-Rückmeldungen des Agenten zu Update/Uninstall (Diagnose): zeigt an,
  // was auf dem Client passiert - statt nur eines 60s-Timeouts.
  dashboardSocket.on("client:action-log", (d) => {
    const c = state.clients.find((x) => x.id === d.id);
    const name = c ? c.hostname : (d.id || "Client");
    const kind = d.kind === "uninstall" ? "Deinstallation" : "Update";
    const level = d.stage === "error" ? "error"
      : (d.stage === "launched-fallback" ? "warn" : "info");
    let msg = `${name}: ${kind} – ${d.stage}`;
    if (d.detail) msg += `\n${d.detail}`;
    if (d.agent_version) msg += `\n(Agent-Version: ${d.agent_version})`;
    window.notify?.(msg, level, 10000, { tag: `agent-${d.kind}:${d.id}` });
  });

  // Fehler bei der Bildschirmübertragung (z.B. headless VM) als Notification
  // + Eintrag im Audit-Log
  dashboardSocket.on("screen-error", (data) => {
    notifyError(data.error || t("u_fehler_bei_der_bildschirmubertragu"), "error", "remote-screen", 12000);
  });

  // Agent-Absturz: Der Agent hat sich selbst neu gestartet und meldet den
  // Fehler - dem Nutzer deutlich anzeigen (letzte Traceback-Zeile reicht).
  dashboardSocket.on("client:agent-crashed", (d) => {
    const lastLine = String(d.error || "").trim().split("\n").filter(Boolean).slice(-1)[0] || t("app_unknown_error");
    notifyError(`⚠️ ${t("app_agent_crashed", { host: d.hostname || d.id, err: lastLine })}`,
      "error", `agent-crash-${d.id}`, 20000);
  });

  // Neue Metriken für einen Client
  dashboardSocket.on("client:metrics", ({ id, metrics }) => {
    const client = state.clients.find((c) => c.id === id);
    const wasOnline = client ? !!client.online : true;
    if (client) {
      client.metrics = metrics;
      client.online = true;
    }
    recordMetrics(id, metrics);

    // Live-Refresh für Dashboard-Widgets (aggregierte Flotten-Werte).
    try { window.dispatchEvent(new CustomEvent("metrics-updated", { detail: { id } })); } catch {}

    // Gezieltes Update: nur die Werte/SVGs der sichtbaren Panels dieses
    // Clients überschreiben - das Client-Panel wird NICHT neu eingefügt.
    let touched = 0;
    try {
      touched = updateClientLayouts(id);
    } catch { touched = 0; }
    // Fallback nur für Ansichten, die (noch) kein Layout-Host-Update können.
    const sel = state.selection;
    if (!touched && sel && (
      (sel.type === "client" && sel.id === id) ||
      sel.type === "tenant" || sel.type === "location"
    )) {
      renderMainContent();
    }
    // Sidebar NICHT bei jedem Metrik-Tick neu bauen (verursachte Hover-
    // Flackern/Springen der Client-Zeilen) - nur wenn sich der Online-Status
    // sichtbar geändert hat (offline -> online).
    if (!wasOnline) renderSidebar();
  });

  // Chat: neue Nachricht -> Toast, wenn ich Mitglied bin und nicht selbst
  // der Absender. (Die Chat-App selbst aktualisiert sich über dasselbe Event.)
  dashboardSocket.on("chat:message", (d) => {
    try {
      const me = state.user?.id;
      if (!me || d.sender_id === me) return;
      if (!(d.member_ids || []).includes(me)) return;
      window.notify?.(
        `💬 ${d.conversation_type === "group"
          ? `${d.sender_name} in „${d.conversation_name}“` : d.sender_name}:\n${d.preview || ""}`,
        "info", 8000, { source: "chat", tag: `chat:${d.conversation_id}` });
    } catch {}
  });

  // Benachrichtigungs-Regeln mit Kanal "Dashboard": Server pusht fertige
  // Notifications (head/body/level) an alle verbundenen Dashboards.
  dashboardSocket.on("notify:push", (n) => {
    try {
      const level = ["info", "success", "warn", "error"].includes(n.level) ? n.level
        : (n.level === "warning" ? "warn" : (n.level === "danger" ? "error" : "info"));
      window.notify?.(`${n.head || ""}\n${n.body || n.message || ""}`.trim(),
        level, 10000, { source: "regel" });
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

// Legt einen Client-Drop auf dem Desktop (Fenster-Layer) als eigenes
// Client-Fenster ab. Der Client bleibt in der Seitenleiste erhalten.
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

// Ein einzelner Init-Schritt darf den Start nicht abbrechen: jeder laeuft in
// seinem eigenen try/catch. So bleibt der Login bedienbar, selbst wenn ein
// spaeterer Teil (Menues, Live-Updates, ...) Probleme macht.
function _step(name, fn) {
  try { fn(); } catch (e) { console.error("[init] " + name + ":", e); }
}

async function main() {
  // Sprachen aus allen lang-Ordnern laden (Login-Fenster + Puffer übersetzen).
  try { await initLanguages(); } catch { /* offline -> Puffer/Fallback */ }
  _step("translations", applyStaticTranslations);

  // ZUERST den Login verkabeln - bevor irgendetwas anderes schiefgehen kann.
  // Damit ist Anmelden immer moeglich, solange der Kern (dieses Modul) laedt.
  _step("login-form", initLoginForm);
  _step("change-pw-form", initChangePwForm);

  _step("content-renderer", () => setContentRenderer(renderWindowContent));
  _step("on-select", () => setOnSelect(renderMainContent));
  _step("dashedit", () => window.addEventListener("dashedit-changed", () => renderMainContent()));
  // "Aenderungen -> alles neu laden" der Feature-Module lazy verkabeln.
  loadFeature("../feature/editclient.js", "setEditOnChanged").then((fn) => fn && fn(refreshAll)).catch(() => {});
  loadFeature("../feature/manage.js", "setManageOnChanged").then((fn) => fn && fn(refreshAll)).catch(() => {});

  _step("menus", initMenusAndButtons);
  _step("global-search", initGlobalSearch);
  _step("taskbar", initTaskbar);
  _step("help", initHelp);
  _step("live-updates", initLiveUpdates);
  _step("desktop-drop", initDesktopDrop);

  // Ist noch ein gültiges Token vorhanden? Dann direkt einloggen.
  const token = localStorage.getItem("rmm_token");
  if (token) {
    try {
      const user = await api.me();
      await startSession(user);
      return;
    } catch {
      clearToken(); // Token abgelaufen/ungültig -> normaler Login
    }
  }
  showOnly(loginScreen());
}

main().catch((e) => {
  // Selbst wenn main() unerwartet scheitert: Login-Formular sichtbar machen und
  // ein letztes Mal verkabeln, damit man sich trotzdem anmelden kann.
  console.error("[init] main:", e);
  try { initLoginForm(); } catch { /* egal */ }
  try { showOnly(loginScreen()); } catch { /* egal */ }
});
