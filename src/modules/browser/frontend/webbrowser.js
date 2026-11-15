// webbrowser.js (module "browser")
// --------------------------------
// Browser apps of the dashboard:
//   renderWebBrowser  the Firefox-like server browser (livebrowser.js) - tabs,
//                     bookmarks, history; the pages run in a real Chromium on the
//                     RMM server and are streamed live (internal networks of
//                     agents work through the VPN module, too).
//   renderWebApp      a saved web app / client website in its own window: same
//                     engine, but its own server window without tab strip.
//
// Web apps are stored per user (localStorage, synced to the server) and get
// their own start-menu entry.
import { state } from "/js/app/state.js";

// ---------------- Web-App-Speicher (pro Benutzer) ----------------
const storeKey = () => `rmm_webapps:${state.user?.username || "default"}`;

export function getWebApps() {
  try { return JSON.parse(localStorage.getItem(storeKey()) || "[]"); }
  catch { return []; }
}
function saveWebApps(list) {
  try { localStorage.setItem(storeKey(), JSON.stringify(list)); } catch {}
  import("/js/app/persist.js").then((m) => m.syncToServerSoon()).catch(() => {});
}

// Neue Web-App speichern und sofort im Startmenü registrieren.
export function addWebApp({ name, url, icon }) {
  const list = getWebApps();
  const id = "wa" + Date.now().toString(36) + Math.floor(Math.random() * 1e4).toString(36);
  const app = { id, name: name.trim(), url: url.trim(), icon: (icon || "🌐").trim() || "🌐" };
  list.push(app);
  saveWebApps(list);
  import("/js/app/startmenu.js").then((m) =>
    m.addCatalogEntry?.(`webapp:${id}`, app.icon, app.name)).catch(() => {});
  return app;
}

export function removeWebApp(id) {
  saveWebApps(getWebApps().filter((a) => a.id !== id));
  import("/js/app/startmenu.js").then((m) => m.removeCatalogEntry?.(`webapp:${id}`)).catch(() => {});
}

// ---------------- Browser UI ----------------
const fail = (body, what, e) => {
  body.innerHTML = `<div style="padding:20px;color:var(--danger)">${what} could not be loaded: ${String(e?.message || e).replace(/</g, "&lt;")}</div>`;
};

export function renderWebBrowser(body, win) {
  import("./livebrowser.js").then(({ renderLiveBrowser }) => renderLiveBrowser(body, win))
    .catch((e) => fail(body, "The browser", e));
}

// Saved web app / client website (appId "webapp"): props {url, name, icon, network}
export function renderWebApp(body, win) {
  const p = win?.props || {};
  const key = String(win?.key || p.url || "app").replace(/[^\w:.\-/@]/g, "_").slice(0, 180);
  import("./livebrowser.js").then(({ renderLiveBrowser }) =>
    renderLiveBrowser(body, win, { app: { key, url: p.url || "", name: p.name || "", network: p.network || "" } }))
    .catch((e) => fail(body, "The web app", e));
}

// Open a client website: open_mode "internal" -> own window in the server browser,
// otherwise a normal tab of the user's browser (panel, sidebar favourites, dashboard).
export function openWebsiteEntry({ url, name, open_mode }) {
  if (!url) return;
  if (open_mode === "internal") {
    import("/js/app/windowmanager.js").then((m) =>
      m.openWindow({
        key: `website:${url}`, appId: "webapp",
        title: `🌐 ${name || url}`, props: { url, name: name || url, icon: "🌐" },
        w: 1020, h: 720,
      }));
  } else {
    window.open(url, "_blank", "noopener");
  }
}
