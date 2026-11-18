// init.js (module "geotrack") - per-client location widget + action button.
// The widget shows up automatically for laptops; for other devices once tracking
// has been enabled there (status comes from the backend). It shows the last
// location + a mini map, with "Locate now" and a tracking on/off toggle.
import { registerClientWidget, registerClientAction, moduleRequest } from "/js/app/modules.js";
import { hasGlobalPerm, isAdmin, hasClientPerm } from "/js/app/state.js";
import { openWindow } from "/js/app/windowmanager.js";
import { esc } from "/js/app/utils.js";
import { icon } from "./icons.js";

const canSee = (cid) => hasGlobalPerm("use_geotrack") || isAdmin() || hasClientPerm(cid, "use_geotrack");
const canManage = () => hasGlobalPerm("manage_geotrack") || isAdmin();

function openApp(client) {
  openWindow({ key: "geotrack-" + (client ? client.id : "all"), appId: "geotrack",
              title: "Locations" + (client ? " — " + (client.hostname || "") : ""),
              props: { clientId: client ? client.id : null }, w: 1000, h: 720, clientColor: client && client.color });
}

const fmtAgo = (ts) => {
  if (!ts) return "never";
  const s = Math.floor((Date.now() - ts) / 1000);
  return s < 60 ? "just now" : s < 3600 ? Math.floor(s / 60) + " min ago"
    : s < 86400 ? Math.floor(s / 3600) + " h ago" : Math.floor(s / 86400) + " d ago";
};

async function renderWidget(el, client) {
  el.innerHTML = `<div style="background:var(--panel);border:1px solid var(--border);border-radius:12px;overflow:hidden">
    <div style="padding:10px 12px;display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--border)">
      <b style="flex:1;display:flex;align-items:center;gap:6px">${icon("pin")} Location</b>
      <span data-state style="font-size:11px;color:var(--subtext)">loading…</span></div>
    <div data-map style="height:150px;background:var(--panel-2,#0e1726)"></div>
    <div data-body style="padding:10px 12px"></div></div>`;
  let st;
  try { st = await moduleRequest(`/api/geo/${client.id}`); }
  catch (e) { el.querySelector("[data-state]").textContent = ""; el.querySelector("[data-body]").innerHTML = `<span style="color:var(--danger);font-size:12px">${esc(e.message)}</span>`; return; }
  const loc = st.location;
  el.querySelector("[data-state]").textContent = st.enabled ? "active" : "off";
  const mapEl = el.querySelector("[data-map]");
  if (loc) {
    const d = 0.01;
    mapEl.innerHTML = `<iframe title="map" style="width:100%;height:100%;border:0" loading="lazy"
      src="https://www.openstreetmap.org/export/embed.html?bbox=${loc.lon - d}%2C${loc.lat - d}%2C${loc.lon + d}%2C${loc.lat + d}&layer=mapnik&marker=${loc.lat}%2C${loc.lon}"></iframe>`;
  } else {
    mapEl.innerHTML = `<div style="height:100%;display:grid;place-items:center;color:var(--subtext);font-size:12px">No location yet</div>`;
  }
  const src = { os: "operating system", wifi: "Wi-Fi", ip: "IP address" }[loc && loc.source] || (loc && loc.source) || "";
  el.querySelector("[data-body]").innerHTML = `
    ${loc ? `<div style="font-weight:600">${esc(loc.city || "")}${loc.country ? ", " + esc(loc.country) : ""}</div>
      <div style="font-size:11.5px;color:var(--subtext)">${loc.lat.toFixed(4)}, ${loc.lon.toFixed(4)} · ±${Math.round(loc.accuracy || 0)} m · ${esc(src)}</div>
      <div style="font-size:11.5px;color:var(--subtext)">${fmtAgo(loc.ts)}</div>`
      : `<div style="color:var(--subtext);font-size:12px">${st.enabled ? (st.online ? "Not located yet." : "Client offline.") : "Tracking is off for this device."}</div>`}
    ${st.last_error ? `<div style="font-size:11px;color:var(--danger);margin-top:4px">${esc(st.last_error)}</div>` : ""}
    <div style="display:flex;gap:6px;margin-top:10px;flex-wrap:wrap">
      <button class="btn" data-locate ${st.online ? "" : "disabled"}>${icon("locate")} Locate now</button>
      <button class="btn" data-open>${icon("map")} Map &amp; history</button>
      ${canManage() ? `<button class="btn" data-toggle>${st.enabled ? "Tracking off" : "Tracking on"}</button>` : ""}</div>`;
  el.querySelector("[data-open]").addEventListener("click", () => openApp(client));
  el.querySelector("[data-locate]").addEventListener("click", async (ev) => {
    ev.currentTarget.disabled = true; ev.currentTarget.innerHTML = `${icon("refresh")} locating…`;
    try { await moduleRequest(`/api/geo/${client.id}/locate`, { method: "POST" }); window.notify?.("Location updated"); renderWidget(el, client); }
    catch (e) { window.notify?.(e.message, "error", 6000); renderWidget(el, client); }
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
      // Laptops always; other devices only when someone may manage tracking
      // (the widget then shows the off state plus an enable button).
      return kind === "laptop" || canManage();
    },
    render: renderWidget,
  });
  registerClientAction("geotrack", {
    label(client) { return canSee(client.id) ? "Location" : null; },
    onClick(client) { openApp(client); },
  });
}
