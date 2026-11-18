// init.js (Modul "geotrack") - Standort-Widget am Client + Aktionsknopf
// ---------------------------------------------------------------------
// Das Widget erscheint bei Laptops automatisch; für andere Geräte, sobald das
// Tracking dort eingeschaltet wurde (Status vom Backend). Zeigt den letzten
// Standort + Mini-Karte; Knöpfe „Jetzt orten“ und „Verlauf/Karte öffnen“.
import { registerClientWidget, registerClientAction, moduleRequest } from "/js/app/modules.js";
import { hasGlobalPerm, isAdmin, hasClientPerm } from "/js/app/state.js";
import { openWindow } from "/js/app/windowmanager.js";
import { esc } from "/js/app/utils.js";

const canSee = (cid) => hasGlobalPerm("use_geotrack") || isAdmin() || hasClientPerm(cid, "use_geotrack");
const canManage = () => hasGlobalPerm("manage_geotrack") || isAdmin();

function openApp(client) {
  openWindow({ key: "geotrack-" + (client ? client.id : "all"), appId: "geotrack",
              title: "Standorte" + (client ? " — " + (client.hostname || "") : ""),
              props: { clientId: client ? client.id : null }, w: 1000, h: 720, clientColor: client && client.color });
}

const fmtAgo = (ts) => { if (!ts) return "noch nie"; const s = Math.floor((Date.now() - ts) / 1000); return s < 60 ? "gerade eben" : s < 3600 ? Math.floor(s / 60) + " min" : s < 86400 ? Math.floor(s / 3600) + " h" : Math.floor(s / 86400) + " Tagen"; };

async function renderWidget(el, client) {
  el.innerHTML = `<div style="background:var(--panel);border:1px solid var(--border);border-radius:12px;overflow:hidden">
    <div style="padding:10px 12px;display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--border)">
      <b style="flex:1">📍 Standort</b><span data-state style="font-size:11px;color:var(--subtext)">lädt …</span></div>
    <div data-map style="height:150px;background:var(--panel-2,#0e1726)"></div>
    <div data-body style="padding:10px 12px"></div></div>`;
  let st;
  try { st = await moduleRequest(`/api/geo/${client.id}`); } catch (e) { el.querySelector("[data-state]").textContent = ""; el.querySelector("[data-body]").innerHTML = `<span style="color:var(--danger);font-size:12px">${esc(e.message)}</span>`; return; }
  const loc = st.location;
  el.querySelector("[data-state]").textContent = st.enabled ? "aktiv" : "aus";
  const mapEl = el.querySelector("[data-map]");
  if (loc) {
    const d = 0.01;
    mapEl.innerHTML = `<iframe title="map" style="width:100%;height:100%;border:0" loading="lazy"
      src="https://www.openstreetmap.org/export/embed.html?bbox=${loc.lon - d}%2C${loc.lat - d}%2C${loc.lon + d}%2C${loc.lat + d}&layer=mapnik&marker=${loc.lat}%2C${loc.lon}"></iframe>`;
  } else {
    mapEl.innerHTML = `<div style="height:100%;display:grid;place-items:center;color:var(--subtext);font-size:12px">Noch kein Standort</div>`;
  }
  const src = { os: "Betriebssystem", wifi: "WLAN", ip: "IP" }[loc && loc.source] || (loc && loc.source) || "";
  el.querySelector("[data-body]").innerHTML = `
    ${loc ? `<div style="font-weight:600">${esc(loc.city || "")}${loc.country ? ", " + esc(loc.country) : ""}</div>
      <div style="font-size:11.5px;color:var(--subtext)">${loc.lat.toFixed(4)}, ${loc.lon.toFixed(4)} · ±${Math.round(loc.accuracy || 0)} m · ${esc(src)}</div>
      <div style="font-size:11.5px;color:var(--subtext)">vor ${fmtAgo(loc.ts)}</div>` : `<div style="color:var(--subtext);font-size:12px">${st.enabled ? (st.online ? "Noch nicht geortet." : "Client offline.") : "Tracking ist für dieses Gerät aus."}</div>`}
    ${st.last_error ? `<div style="font-size:11px;color:var(--danger);margin-top:4px">${esc(st.last_error)}</div>` : ""}
    <div style="display:flex;gap:6px;margin-top:10px;flex-wrap:wrap">
      <button class="btn" data-locate ${st.online ? "" : "disabled"}>📍 Jetzt orten</button>
      <button class="btn" data-open>🗺 Karte & Verlauf</button>
      ${canManage() ? `<button class="btn" data-toggle>${st.enabled ? "Tracking aus" : "Tracking an"}</button>` : ""}</div>`;
  el.querySelector("[data-open]").addEventListener("click", () => openApp(client));
  el.querySelector("[data-locate]").addEventListener("click", async (ev) => {
    ev.target.disabled = true; ev.target.textContent = "⏳ orte …";
    try { await moduleRequest(`/api/geo/${client.id}/locate`, { method: "POST" }); window.notify?.("Standort aktualisiert"); renderWidget(el, client); }
    catch (e) { window.notify?.(e.message, "error", 6000); ev.target.disabled = false; ev.target.textContent = "📍 Jetzt orten"; }
  });
  el.querySelector("[data-toggle]")?.addEventListener("click", async () => {
    try { await moduleRequest(`/api/geo/${client.id}/enabled`, { method: "PUT", body: JSON.stringify({ enabled: !st.enabled }) }); renderWidget(el, client); }
    catch (e) { window.notify?.(e.message, "error"); }
  });
}

export function init() {
  registerClientWidget("geotrack", {
    match(client) {
      if (!canSee(client.id)) return false;
      const kind = (client.detected_kind || client.device_type || "").toLowerCase();
      // Laptops immer; andere Geräte nur, wenn jemand Tracking verwalten darf
      // (das Widget zeigt dann den Aus-Zustand + Einschalt-Knopf).
      return kind === "laptop" || canManage();
    },
    render: renderWidget,
  });
  registerClientAction("geotrack", {
    label(client) { return canSee(client.id) ? "📍 Standort" : null; },
    onClick(client) { openApp(client); },
  });
}
