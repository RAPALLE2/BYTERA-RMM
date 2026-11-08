// apps/webbrowser.js
// ------------------
// 🌐 Interner Webbrowser: Webseiten direkt in einem Fenster der Webconsole
// öffnen (iframe) - mit URL-Leiste, eigener Vor/Zurück-Historie und Reload.
//
// Highlight "Web-Apps": Jede Seite lässt sich mit ⭐ "Als App speichern"
// als INTERNE APP registrieren. Sie bekommt dann einen eigenen Eintrag im
// Startmenü (Icon + Name) und öffnet direkt in ihrem eigenen Fenster.
// Gespeichert pro Benutzer (localStorage, serverseitig gesynct).
//
// KEIN Proxy. Die Seite wird direkt in den iframe geladen, also von der
// echten Browser-Engine des Benutzers gerendert - Anmeldung, Cookies,
// WebSocket-Konsolen, alles genau wie in einem normalen Tab. Ein
// umschreibender Proxy davorzuschalten war der Versuch, Klicks auf
// target="_blank" abzufangen; er hat dabei zuverlässig mehr zerstört als
// gewonnen (Anmeldungen scheiterten, Bilder fehlten).
//
// Preis dieser Entscheidung, bewusst bezahlt: ein Klick auf einen Link mit
// target="_blank" öffnet einen echten Browser-Tab. Das lässt sich von aussen
// NICHT abfangen - fremde Seiten liegen in einem anderen Origin, ihr DOM ist
// unerreichbar, kein Klick darin kommt bei uns an.
//
// Zwei Dinge, die auch ein direkt geladener Rahmen nicht kann:
//   * Seiten mit X-Frame-Options/CSP frame-ancestors verweigern das
//     Einbetten (google.com etwa). Proxmox, OPNsense, die meisten
//     Geräte-Oberflächen setzen das NICHT und laufen problemlos.
//   * Ein noch nicht akzeptiertes selbstsigniertes Zertifikat lässt der
//     Browser im Rahmen still scheitern - er kann seine Warnseite dort
//     nicht anzeigen. Dafür gibt es unten die Erkennung mit dem Hinweis,
//     die Seite einmal in einem echten Tab zu öffnen und das Zertifikat
//     dort zu bestätigen. Danach lädt sie auch im Fenster.
import { nodeHint } from "/js/app/nodechoice.js";
import { esc, uiConfirm } from "/js/app/utils.js";
import { state } from "/js/app/state.js";
import { registerCleanup } from "/js/app/windowmanager.js";
import { api, getToken } from "/js/app/api.js";
// t() unter Alias: in dieser Datei ist "t" bereits als lokaler
// Variablenname belegt (Tenant/Target/Trigger/Token o.ä.).
import { t as tr } from "/js/app/i18n.js";

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

const normalizeUrl = (raw) => {
  const t = (raw || "").trim();
  if (!t) return "";
  return /^https?:\/\//i.test(t) ? t : "https://" + t;
};

// ---------------- Browser-UI ----------------
// props: { url, name, icon } -> gesperrter Start als Web-App (eigenes Fenster)
export function renderWebBrowser(body, win) {
  // NEU: vollwertiger Server-Browser mit Live-Stream (Firefox-ähnlich, Tabs,
  // Lesezeichen). Die alte Proxy-Ansicht ist abgelöst.
  import("./livebrowser.js").then(({ renderLiveBrowser }) => renderLiveBrowser(body, win))
    .catch((e) => { body.innerHTML = '<div style="padding:20px;color:var(--danger)">Browser konnte nicht geladen werden: ' + (e && e.message ? e.message : e) + '</div>'; });
}

// Renderer für gespeicherte Web-Apps (eigene Fenster, appId "webapp").
export function renderWebApp(body, win) {
  renderWebBrowser(body, win);
}

// Client-Website öffnen: je nach open_mode im INTERNEN Browser-Fenster
// (eigenes Fenster mit URL-Leiste) oder als externer Browser-Tab.
// Wird von Panel, Sidebar-Favoriten, Dashboard und Client-Bearbeiten genutzt.
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
