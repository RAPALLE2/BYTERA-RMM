// app.js (Modul "geotrack") - Standorte: Geräteliste + Karte + Verlauf
import { state, findClient } from "/js/app/state.js";
import { moduleRequest } from "/js/app/modules.js";
import { hasGlobalPerm, isAdmin } from "/js/app/state.js";
import { esc, uiConfirm } from "/js/app/utils.js";

const api = (p, o = {}) => moduleRequest("/api/geo" + p, o);
const toast = (m, t = "success", ms = 1800) => window.notify?.(m, t, ms);
const canManage = () => hasGlobalPerm("manage_geotrack") || isAdmin();
const SRC = { os: "Betriebssystem", wifi: "WLAN", ip: "IP-Adresse" };
const fmtTs = (ts) => ts ? new Date(ts).toLocaleString() : "—";

export function renderGeo(body, win) {
  let clients = [], sel = win?.props?.clientId || null, hist = [], marker = null;

  body.innerHTML = `<div style="display:flex;height:100%;min-height:0;font-size:13px">
    <aside style="width:260px;flex:0 0 260px;border-right:1px solid var(--border);display:flex;flex-direction:column;min-height:0">
      <div style="padding:12px;display:flex;align-items:center;gap:8px"><b style="flex:1">📍 Getrackte Geräte</b><button class="btn" data-reload>↻</button></div>
      <div data-list style="flex:1;overflow:auto;padding:0 8px 10px"></div></aside>
    <main data-main style="flex:1;display:flex;flex-direction:column;min-height:0"></main></div>`;
  const listEl = body.querySelector("[data-list]"), main = body.querySelector("[data-main]");
  body.querySelector("[data-reload]").addEventListener("click", load);

  async function load() {
    try { clients = (await api("/clients")).clients; } catch (e) { listEl.innerHTML = `<div style="padding:10px;color:var(--danger)">${esc(e.message)}</div>`; return; }
    drawList();
    if (sel || clients[0]) openClient(sel || clients[0].client_id);
    else main.innerHTML = `<div style="margin:auto;color:var(--subtext);text-align:center;padding:40px">Keine getrackten Geräte.<br>Tracking aktivierst du im Standort-Widget beim Client (Standard: Laptops).</div>`;
  }

  function drawList() {
    listEl.innerHTML = clients.map((c) => {
      const l = c.location;
      return `<a data-c="${esc(c.client_id)}" style="display:block;padding:8px 10px;border-radius:9px;cursor:pointer;${sel === c.client_id ? "background:color-mix(in srgb,var(--accent) 16%,transparent);font-weight:600" : ""}">
        <div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">💻 ${esc(c.hostname || c.client_id)}</div>
        <div style="font-size:11px;color:var(--subtext);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${l ? esc(l.city || `${l.lat.toFixed(3)}, ${l.lon.toFixed(3)}`) : "kein Standort"}</div></a>`;
    }).join("") || `<div style="padding:10px;color:var(--subtext)">—</div>`;
    listEl.querySelectorAll("[data-c]").forEach((a) => a.addEventListener("click", () => openClient(a.dataset.c)));
  }

  async function openClient(cid) {
    sel = cid; drawList();
    const c = clients.find((x) => x.client_id === cid) || { client_id: cid, hostname: (findClient(cid) || {}).hostname };
    main.innerHTML = `<div style="padding:12px 16px;border-bottom:1px solid var(--border);display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <b style="font-size:16px;flex:1">💻 ${esc(c.hostname || cid)}</b>
        <button class="btn-primary" data-locate>📍 Jetzt orten</button>
        ${canManage() ? `<button class="btn" data-clear style="color:var(--danger)">Verlauf löschen</button>` : ""}</div>
      <div style="flex:1;display:flex;min-height:0">
        <div data-map style="flex:1;background:var(--panel-2,#0e1726)"></div>
        <div style="width:300px;flex:0 0 300px;border-left:1px solid var(--border);overflow:auto" data-hist></div></div>`;
    main.querySelector("[data-locate]").addEventListener("click", async (ev) => {
      ev.target.disabled = true; ev.target.textContent = "⏳ …";
      try { await api(`/${cid}/locate`, { method: "POST" }); toast("Geortet"); await openClient(cid); await load(); }
      catch (e) { toast(e.message, "error", 6000); ev.target.disabled = false; ev.target.textContent = "📍 Jetzt orten"; }
    });
    main.querySelector("[data-clear]")?.addEventListener("click", async () => {
      if (!(await uiConfirm("Gesamten Standortverlauf dieses Geräts löschen?"))) return;
      try { await api(`/${cid}/history`, { method: "DELETE" }); toast("Verlauf gelöscht"); openClient(cid); } catch (e) { toast(e.message, "error"); }
    });
    try { hist = (await api(`/${cid}/history?limit=500`)).history; } catch { hist = []; }
    marker = hist[0] || null;
    drawMap(); drawHist();
  }

  function drawMap() {
    const el = main.querySelector("[data-map]"); if (!el) return;
    if (!marker) { el.innerHTML = `<div style="height:100%;display:grid;place-items:center;color:var(--subtext)">Kein Standort vorhanden</div>`; return; }
    const d = 0.008;
    el.innerHTML = `<iframe title="map" style="width:100%;height:100%;border:0"
      src="https://www.openstreetmap.org/export/embed.html?bbox=${marker.lon - d}%2C${marker.lat - d}%2C${marker.lon + d}%2C${marker.lat + d}&layer=mapnik&marker=${marker.lat}%2C${marker.lon}"></iframe>`;
  }

  function drawHist() {
    const el = main.querySelector("[data-hist]"); if (!el) return;
    el.innerHTML = `<div style="padding:10px 12px;font-weight:600;border-bottom:1px solid var(--border)">Verlauf (${hist.length})</div>`
      + (hist.map((l) => `<a data-h="${esc(l.id)}" style="display:block;padding:8px 12px;border-bottom:1px solid var(--border);cursor:pointer;${marker && marker.id === l.id ? "background:color-mix(in srgb,var(--accent) 12%,transparent)" : ""}">
        <div style="font-size:12.5px">${esc(l.city || `${l.lat.toFixed(4)}, ${l.lon.toFixed(4)}`)}</div>
        <div style="font-size:11px;color:var(--subtext)">${esc(fmtTs(l.ts))} · ${esc(SRC[l.source] || l.source)} · ±${Math.round(l.accuracy || 0)} m</div></a>`).join("")
        || `<div style="padding:12px;color:var(--subtext)">Noch kein Verlauf.</div>`);
    el.querySelectorAll("[data-h]").forEach((a) => a.addEventListener("click", () => { marker = hist.find((x) => x.id === a.dataset.h); drawMap(); drawHist(); }));
  }

  load();
}
