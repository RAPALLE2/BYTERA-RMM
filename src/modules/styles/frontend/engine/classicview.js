// engine/classicview.js (Modul "styles")
// ----------------------------------------
// Inhalt des Tabs "Übersicht" im klassischen Design - aufgebaut wie Proxmox:
//
//   Seitenleiste (Baum)      | Kopfzeile: gewähltes Objekt + Status
//   Rechenzentrum            |-----------------------------------------------
//    └ Mandant               | Kategorien  | Inhalt der Kategorie
//       └ Standort           |  Übersicht  |  (feste Ansicht, KEINE Widgets,
//          └ Gerät           |  Leistung   |   nichts zu verschieben)
//                            |  …          |
//
// Gewählt wird im vorhandenen Geräte-Baum der Seitenleiste (sidebar.js):
// "Dashboard" = Rechenzentrum, dann Mandant -> Standort -> Gerät. Je nach Art
// des Objekts gibt es eigene Kategorien. Die zuletzt gewählte Kategorie wird
// je Objektart gemerkt (Proxmox-Verhalten: Gerät wechseln, Kategorie bleibt).
//
// Die Inhalte nutzen dieselben Bausteine wie die futuristische Ansicht
// (panel.js, clientmetrics.js) - nur fest angeordnet. Live-Werte werden über
// updateClassicView() aus app.js (client:metrics) aufgefrischt.

import { state, findClient, isAdmin, hasGlobalPerm, hasClientPerm } from "/js/app/state.js";
import { esc, formatBytes, formatUptime } from "/js/app/utils.js";
import { t, osIcon, osLabel } from "/js/app/i18n.js";
import {
  renderStatusPart, renderActionsPart, renderWebsitesPart, renderOverviewSub,
  renderChildrenPart, renderPatchesPart, childClientsOf,
} from "/js/app/panel.js";
import { renderClientMetric, clientPresetById, presetAvailable } from "/js/app/clientmetrics.js";
import { warrantyInfo } from "/js/app/dashwidgets.js";
import { api } from "/js/app/api.js";
import { clientIconSvg, batteryHtml } from "/js/app/deviceicons.js";

const CAT_KEY = "rmm_clview_cat";
let catByType = (() => { try { return JSON.parse(localStorage.getItem(CAT_KEY) || "{}"); } catch { return {}; } })();
let tableSort = { col: "name", dir: 1 };
let tableFilter = "";
let jobsStop = null;
let lastLive = 0;

// ---------------------------------------------------------------- Kategorien
const CLIENT_CATS = [
  { key: "overview", icon: "📋", label: "Übersicht", live: true },
  { key: "perf", icon: "📈", label: "Leistung", live: true },
  { key: "disks", icon: "💽", label: "Datenträger", live: true },
  { key: "hardware", icon: "🔧", label: "Hardware", live: true },
  { key: "network", icon: "🌐", label: "Netzwerk", live: true },
  { key: "actions", icon: "⚡", label: "Aktionen" },
  { key: "patches", icon: "⬆️", label: "Updates" },
  { key: "websites", icon: "🔗", label: "Websites" },
  { key: "children", icon: "🗂️", label: "Untergeordnet", when: (c) => childClientsOf(c.id).length > 0 },
  { key: "notes", icon: "📝", label: "Notizen" },
  { key: "warranty", icon: "🛡️", label: "Garantie" },
];
const GROUP_CATS = [
  { key: "overview", icon: "📋", label: "Übersicht", live: true },
  { key: "devices", icon: "🖥️", label: "Geräte", live: true },
  { key: "alerts", icon: "⚠️", label: "Warnungen", live: true },
];
const DC_CATS = [...GROUP_CATS, { key: "jobs", icon: "⏳", label: "Aufgaben" }];

const METRIC_SETS = {
  perf: ["c.cpuLoad", "c.ramPct", "c.cpuPerCore", "c.netBoth", "c.diskIO", "c.pingCf", "c.load", "c.swap"],
  hardware: ["c.sysinfo", "c.cpuModel", "c.gpuModel", "c.ramModules", "c.tempsAll", "c.fansAll", "c.gpus", "c.battery", "c.power"],
  network: ["c.netIdentity", "c.interfaces", "c.netBoth", "c.pingGoogle", "c.pingCf"],
  disks: ["c.disks", "c.diskRead", "c.diskWrite"],
};

// ---------------------------------------------------------------- Auswahl
function sel() {
  const s = state.selection;
  if (!s || s.type === "dashboard") return { type: "dc", id: "" };
  return s;
}
function catsFor(s, client) {
  if (s.type === "client") return CLIENT_CATS.filter((c) => !c.when || (client && c.when(client)));
  return s.type === "dc" ? DC_CATS : GROUP_CATS;
}
function currentCat(s, cats) {
  const k = catByType[s.type];
  return cats.some((c) => c.key === k) ? k : cats[0].key;
}
function setCat(type, key) {
  catByType[type] = key;
  try { localStorage.setItem(CAT_KEY, JSON.stringify(catByType)); } catch {}
}

// Geräte im Bereich der Auswahl (ohne VMs/CTs unter einem sichtbaren Host
// doppelt zu zaehlen: Kinder zaehlen mit, werden aber eingerueckt gelistet)
function clientsIn(s) {
  const all = state.clients || [];
  if (s.type === "tenant") return all.filter((c) => c.tenant_id === s.id);
  if (s.type === "location") return all.filter((c) => c.location_id === s.id);
  return all;
}

function pct(used, total) { return total ? Math.round((used / total) * 100) : null; }
function metricsOf(c) {
  const m = c.metrics || null;
  return {
    cpu: m && m.cpuLoad != null ? Math.round(m.cpuLoad) : null,
    ram: m ? pct(m.memUsed, m.memTotal) : null,
    disk: m ? pct(m.diskUsed, m.diskTotal) : null,
    uptime: m ? m.uptime : null,
  };
}
function statusOf(c) {
  if (c.status_override === "maintenance") return { key: "maint", label: t("status_maintenance"), color: "var(--warn)" };
  return c.online ? { key: "on", label: t("status_online"), color: "var(--online)" }
                  : { key: "off", label: t("status_offline"), color: "var(--subtext)" };
}
const dot = (color) => `<span class="clv-dot" style="background:${color}"></span>`;
const tenantName = (id) => state.hierarchy?.tenants?.find((x) => x.id === id)?.name || "–";
const locationName = (id) => state.hierarchy?.locations?.find((x) => x.id === id)?.name || "–";

// ---------------------------------------------------------------- Rendern
export function renderClassicView(el) {
  if (jobsStop) { try { jobsStop(); } catch {} jobsStop = null; }
  const s = sel();
  if (s.type === "dc" && !isAdmin() && !hasGlobalPerm("see_dashboard")) {
    el.innerHTML = `<div style="position:absolute;inset:0;background:#000"></div>`;
    return;
  }
  const client = s.type === "client" ? findClient(s.id) : null;
  if (s.type === "client" && !client) { el.innerHTML = `<div class="empty-state">${t("client_not_found")}</div>`; return; }
  const cats = catsFor(s, client);
  const cat = currentCat(s, cats);

  el.innerHTML = `
    <div class="clv" data-type="${esc(s.type)}" data-id="${esc(s.id)}">
      <div class="clv-head">${headHtml(s, client)}</div>
      <div class="clv-main">
        <nav class="clv-cats" role="tablist">
          ${cats.map((c) => `<button class="clv-cat${c.key === cat ? " active" : ""}" data-cat="${c.key}" role="tab">
            <span class="clv-cat-ico">${c.icon}</span><span>${esc(c.label)}</span></button>`).join("")}
        </nav>
        <section class="clv-content" data-cat="${cat}"></section>
      </div>
    </div>`;
  const root = el.querySelector(".clv");
  root.querySelectorAll(".clv-cat").forEach((b) => b.addEventListener("click", () => {
    setCat(s.type, b.dataset.cat);
    renderClassicView(el);
  }));
  fillContent(root.querySelector(".clv-content"), s, client, cat);
}

function headHtml(s, client) {
  if (s.type === "client") {
    const st = statusOf(client);
    const path = [client.tenant_id && tenantName(client.tenant_id), client.location_id && locationName(client.location_id)].filter(Boolean);
    return `<div class="clv-title">${dot(st.color)}${clientIconSvg(client, 20, "margin:0 4px")}<b>${esc(client.hostname)}</b>
        ${batteryHtml(client, "full")}
        <span class="clv-sub">${osIcon(client.platform, client.release)} ${esc(osLabel(client.platform, client.release))}${client.ip ? " · " + esc(client.ip) : ""}</span></div>
      <div class="clv-crumbs">${path.map(esc).join(" › ")}${path.length ? " · " : ""}${esc(st.label)}</div>`;
  }
  const list = clientsIn(s);
  const on = list.filter((c) => c.online).length;
  const title = s.type === "tenant" ? `🏢 ${esc(tenantName(s.id))}`
    : s.type === "location" ? `📍 ${esc(locationName(s.id))}` : "🗄️ Rechenzentrum";
  const crumbs = s.type === "location"
    ? esc(tenantName(state.hierarchy?.locations?.find((l) => l.id === s.id)?.tenant_id))
    : s.type === "tenant" ? "Mandant" : "Alle Mandanten";
  return `<div class="clv-title"><b>${title}</b><span class="clv-sub">${on}/${list.length} ${esc(t("online"))}</span></div>
    <div class="clv-crumbs">${crumbs}</div>`;
}

function fillContent(box, s, client, cat) {
  box.innerHTML = "";
  box.dataset.cat = cat;
  if (s.type === "client") return fillClient(box, client, cat);
  if (cat === "overview") return groupOverview(box, s);
  if (cat === "devices") return devicesTable(box, s);
  if (cat === "alerts") return alertsList(box, s);
  if (cat === "jobs") return jobsList(box);
}

// ---------------------------------------------------------------- Gerät
function section(box, title, cls = "") {
  const sec = document.createElement("div");
  sec.className = `clv-section ${cls}`;
  sec.innerHTML = title ? `<div class="clv-section-head">${esc(title)}</div>` : "";
  const body = document.createElement("div");
  body.className = "clv-section-body";
  sec.appendChild(body);
  box.appendChild(sec);
  return body;
}

function metricGrid(box, client, ids) {
  const grid = document.createElement("div");
  grid.className = "clv-mgrid";
  let n = 0;
  for (const id of ids) {
    const preset = clientPresetById(id);
    if (!preset || !presetAvailable(preset, client.device_type)) continue;
    const card = document.createElement("div");
    card.className = "clv-mcard";
    card.innerHTML = `<div class="clv-mcard-head">${esc(preset.label)}</div><div class="clv-mcard-body cmetric-holder" data-metric="${esc(id)}"></div>`;
    grid.appendChild(card);
    try { renderClientMetric(card.querySelector(".cmetric-holder"), client, { id: `clv-${id}`, type: "cmetric", metric: id }); }
    catch (e) { card.querySelector(".cmetric-holder").textContent = String(e.message || e); }
    n++;
  }
  if (!n) grid.innerHTML = `<div class="clv-empty">Für dieses Gerät sind hier keine Werte verfügbar.</div>`;
  box.appendChild(grid);
}

function fillClient(box, client, cat) {
  const rerender = () => fillClient(box, findClient(client.id) || client, cat);
  if (cat === "overview") {
    renderStatusPart(section(box, "Status", "clv-status"), client);
    if (!client.metrics) { section(box, "Live-Werte").innerHTML = `<div class="clv-empty">${esc(t("no_live_data"))}</div>`; return; }
    const b = section(box, "Live-Werte", "overview-content");
    renderOverviewSub(b, client, "metrics", () => { b.innerHTML = ""; renderOverviewSub(b, client, "metrics", null); });
    return;
  }
  if (cat === "disks") {
    const b = section(box, "Belegung", "overview-content");
    renderOverviewSub(b, client, "disk", rerender);
    return metricGrid(box, client, METRIC_SETS.disks);
  }
  if (METRIC_SETS[cat]) {
    if (!client.metrics) { box.innerHTML = `<div class="clv-empty">${esc(t("no_live_data"))}</div>`; return; }
    return metricGrid(box, client, METRIC_SETS[cat]);
  }
  if (cat === "actions") { const b = section(box, ""); b.classList.add("actions-panel"); return renderActionsPart(b, client); }
  if (cat === "websites") { const b = section(box, ""); b.classList.add("actions-panel"); return renderWebsitesPart(b, client); }
  if (cat === "patches") { const b = section(box, ""); b.classList.add("actions-panel"); return renderPatchesPart(b, client); }
  if (cat === "children") { const b = section(box, ""); b.classList.add("actions-panel"); return renderChildrenPart(b, client); }
  if (cat === "notes") { const b = section(box, "", "overview-content"); return renderOverviewSub(b, client, "notes", rerender); }
  if (cat === "warranty") return warrantyView(box, client);
}

function warrantyView(box, client) {
  const info = warrantyInfo(client.warranty_until);
  const mayEdit = isAdmin() || hasClientPerm(client.id, "set_warranty");
  const b = section(box, "Garantie");
  b.innerHTML = `
    <div style="font-size:22px;font-weight:700;color:${info.color}">${esc(info.text)}</div>
    <div style="color:var(--subtext);margin:4px 0 12px">${esc(info.sub)}</div>
    ${mayEdit ? `<label class="clv-label">Garantie bis</label>
      <div style="display:flex;gap:8px;max-width:320px">
        <input type="date" class="clv-date" value="${client.warranty_until ? new Date(client.warranty_until).toISOString().slice(0, 10) : ""}">
        <button class="btn btn-primary" data-a="save">Speichern</button>
      </div>` : ""}`;
  b.querySelector('[data-a="save"]')?.addEventListener("click", async () => {
    const v = b.querySelector(".clv-date").value;
    const until = v ? new Date(v + "T23:59:59").getTime() : null;
    try {
      await api.updateClient(client.id, { warranty_until: until });
      const c = (state.clients || []).find((x) => x.id === client.id);
      if (c) c.warranty_until = until;
      window.notify?.("Garantie-Datum gespeichert", "success");
      box.innerHTML = ""; warrantyView(box, c || client);
    } catch (e) { window.notify?.(`Speichern fehlgeschlagen: ${e.message}`, "error"); }
  });
}

// ---------------------------------------------------------------- Rechenzentrum / Mandant / Standort
function counts(list) {
  const r = { total: list.length, on: 0, off: 0, maint: 0 };
  for (const c of list) r[statusOf(c).key]++;
  return r;
}

function groupOverview(box, s) {
  const list = clientsIn(s);
  const k = counts(list);
  const alerts = collectAlerts(list);
  const tiles = section(box, "Status");
  tiles.innerHTML = `<div class="clv-tiles">
      <div class="clv-tile"><span>Geräte</span><b>${k.total}</b></div>
      <div class="clv-tile"><span>${dot("var(--online)")}Online</span><b>${k.on}</b></div>
      <div class="clv-tile"><span>${dot("var(--subtext)")}Offline</span><b>${k.off}</b></div>
      <div class="clv-tile"><span>${dot("var(--warn)")}Wartung</span><b>${k.maint}</b></div>
      <div class="clv-tile${alerts.length ? " warn" : ""}"><span>⚠️ Warnungen</span><b>${alerts.length}</b></div>
    </div>`;
  tiles.querySelector(".clv-tile.warn")?.addEventListener("click", () => { setCat(s.type, "alerts"); rerenderMain(); });

  // Auslastung (Durchschnitt der Online-Geraete) als schlichte Balken
  const online = list.filter((c) => c.online && c.metrics);
  const avg = (f) => { const v = online.map((c) => metricsOf(c)[f]).filter((x) => x != null); return v.length ? Math.round(v.reduce((a, b) => a + b, 0) / v.length) : null; };
  const bars = section(box, "Auslastung (Durchschnitt online)");
  bars.innerHTML = [["CPU", avg("cpu")], ["RAM", avg("ram")], ["Datenträger", avg("disk")]]
    .map(([l, v]) => `<div class="clv-bar-row"><span>${l}</span>${barHtml(v)}</div>`).join("");

  // Untergliederung: Rechenzentrum -> Mandanten, Mandant -> Standorte
  if (s.type === "dc" || s.type === "tenant") {
    const rows = s.type === "dc"
      ? (state.hierarchy?.tenants || []).map((x) => ({ type: "tenant", id: x.id, name: x.name, color: x.color, list: list.filter((c) => c.tenant_id === x.id) }))
      : (state.hierarchy?.locations || []).filter((l) => l.tenant_id === s.id).map((x) => ({ type: "location", id: x.id, name: x.name, list: list.filter((c) => c.location_id === x.id) }));
    const b = section(box, s.type === "dc" ? "Mandanten" : "Standorte");
    b.innerHTML = rows.length ? `<table class="clv-table"><thead><tr><th>Name</th><th>Geräte</th><th>Online</th><th>Offline</th><th>Warnungen</th></tr></thead><tbody>
      ${rows.map((r) => { const kk = counts(r.list); const al = collectAlerts(r.list).length; return `
        <tr class="clv-link" data-go-type="${r.type}" data-go-id="${esc(r.id)}">
          <td>${r.color ? dot(esc(r.color)) : "📍 "}${esc(r.name)}</td><td>${kk.total}</td><td>${kk.on}</td><td>${kk.off}</td>
          <td>${al ? `<span class="clv-warn">${al}</span>` : "–"}</td></tr>`; }).join("")}
      </tbody></table>` : `<div class="clv-empty">Keine Einträge.</div>`;
    b.querySelectorAll("[data-go-type]").forEach((tr) => tr.addEventListener("click", () => goTo(tr.dataset.goType, tr.dataset.goId)));
  }
}

function barHtml(v) {
  if (v == null) return `<span class="clv-bar"><i style="width:0"></i></span><b>–</b>`;
  const col = v >= 90 ? "var(--danger, #e5484d)" : v >= 75 ? "var(--warn)" : "var(--accent)";
  return `<span class="clv-bar"><i style="width:${Math.min(100, v)}%;background:${col}"></i></span><b>${v} %</b>`;
}

function deviceRows(s) {
  const q = tableFilter.trim().toLowerCase();
  const rows = clientsIn(s).map((c) => {
    const m = metricsOf(c);
    return { c, st: statusOf(c), name: c.hostname || "", tenant: tenantName(c.tenant_id), location: c.location_id ? locationName(c.location_id) : "–",
      os: osLabel(c.platform, c.release), ip: c.ip || "", cpu: m.cpu, ram: m.ram, disk: m.disk, uptime: m.uptime, agent: c.agent_version || "" };
  }).filter((r) => !q || [r.name, r.tenant, r.location, r.os, r.ip, r.agent].some((x) => String(x).toLowerCase().includes(q)));
  const { col, dir } = tableSort;
  rows.sort((a, b) => {
    const x = col === "status" ? a.st.key : a[col], y = col === "status" ? b.st.key : b[col];
    if (x == null && y == null) return 0; if (x == null) return 1; if (y == null) return -1;
    return (typeof x === "number" ? x - y : String(x).localeCompare(String(y), "de", { numeric: true })) * dir;
  });
  return rows;
}

const DEV_COLS = [
  ["status", "Status"], ["name", "Name"], ["tenant", "Mandant"], ["location", "Standort"], ["os", "Betriebssystem"],
  ["ip", "IP"], ["cpu", "CPU"], ["ram", "RAM"], ["disk", "Disk"], ["uptime", "Laufzeit"], ["agent", "Agent"],
];

function devicesTable(box, s) {
  const cols = DEV_COLS.filter(([k]) => !(s.type !== "dc" && k === "tenant") && !(s.type === "location" && k === "location"));
  const bar = document.createElement("div");
  bar.className = "clv-toolbar";
  bar.innerHTML = `<input type="search" class="clv-search" placeholder="Filtern (Name, IP, OS …)" value="${esc(tableFilter)}"><span class="clv-count"></span>`;
  box.appendChild(bar);
  const wrap = document.createElement("div");
  wrap.className = "clv-table-wrap";
  box.appendChild(wrap);
  const draw = () => {
    const rows = deviceRows(s);
    bar.querySelector(".clv-count").textContent = `${rows.length} Gerät${rows.length === 1 ? "" : "e"}`;
    const pc = (v) => v == null ? "–" : `<span class="${v >= 90 ? "clv-warn" : ""}">${v} %</span>`;
    wrap.innerHTML = `<table class="clv-table clv-sortable"><thead><tr>${cols.map(([k, l]) =>
      `<th data-sort="${k}">${esc(l)}${tableSort.col === k ? (tableSort.dir > 0 ? " ▲" : " ▼") : ""}</th>`).join("")}</tr></thead><tbody>
      ${rows.map((r) => `<tr class="clv-link" data-client="${esc(r.c.id)}">${cols.map(([k]) => {
        if (k === "status") return `<td>${dot(r.st.color)}${esc(r.st.label)}</td>`;
        if (k === "name") return `<td>${r.c.parent_client_id ? "↳ " : ""}<b>${esc(r.name)}</b></td>`;
        if (k === "os") return `<td>${osIcon(r.c.platform, r.c.release)} ${esc(r.os)}</td>`;
        if (k === "cpu" || k === "ram" || k === "disk") return `<td>${pc(r[k])}</td>`;
        if (k === "uptime") return `<td>${r.uptime != null ? esc(formatUptime(r.uptime)) : "–"}</td>`;
        return `<td>${esc(r[k] || "–")}</td>`;
      }).join("")}</tr>`).join("") || `<tr><td colspan="${cols.length}" class="clv-empty">Keine Geräte.</td></tr>`}
      </tbody></table>`;
    wrap.querySelectorAll("th[data-sort]").forEach((th) => th.addEventListener("click", () => {
      const k = th.dataset.sort;
      tableSort = { col: k, dir: tableSort.col === k ? -tableSort.dir : 1 };
      draw();
    }));
    wrap.querySelectorAll("[data-client]").forEach((tr) => tr.addEventListener("click", () => goTo("client", tr.dataset.client)));
  };
  bar.querySelector(".clv-search").addEventListener("input", (e) => { tableFilter = e.target.value; draw(); });
  box._clvRedraw = draw;
  draw();
}

function collectAlerts(list) {
  const out = [];
  const now = Date.now();
  for (const c of list) {
    const m = metricsOf(c);
    if (!c.online && c.status_override !== "maintenance") out.push({ c, level: "warn", text: "Offline" });
    if (m.cpu != null && m.cpu >= 90) out.push({ c, level: "crit", text: `CPU-Last ${m.cpu} %` });
    if (m.ram != null && m.ram >= 90) out.push({ c, level: "crit", text: `Arbeitsspeicher ${m.ram} % belegt` });
    for (const d of (c.metrics?.disks || [])) {
      const p = pct(d.used, d.total);
      if (p != null && p >= 90) out.push({ c, level: "crit", text: `Datenträger ${d.mountpoint || d.device} ${p} % voll (${formatBytes((d.total || 0) - (d.used || 0))} frei)` });
    }
    if (!(c.metrics?.disks || []).length && m.disk != null && m.disk >= 90) out.push({ c, level: "crit", text: `Datenträger ${m.disk} % voll` });
    if (c.warranty_until) {
      const days = Math.floor((c.warranty_until - now) / 86400000);
      if (days < 0) out.push({ c, level: "info", text: "Garantie abgelaufen" });
      else if (days <= 30) out.push({ c, level: "info", text: `Garantie endet in ${days} Tag${days === 1 ? "" : "en"}` });
    }
  }
  const rank = { crit: 0, warn: 1, info: 2 };
  return out.sort((a, b) => rank[a.level] - rank[b.level] || String(a.c.hostname).localeCompare(String(b.c.hostname)));
}

function alertsList(box, s) {
  const list = collectAlerts(clientsIn(s));
  const b = section(box, `Warnungen (${list.length})`);
  const ico = { crit: "🔴", warn: "🟠", info: "🔵" };
  b.innerHTML = list.length ? `<table class="clv-table"><thead><tr><th></th><th>Gerät</th><th>Meldung</th><th>Mandant</th><th>Standort</th></tr></thead><tbody>
    ${list.map((a) => `<tr class="clv-link" data-client="${esc(a.c.id)}"><td>${ico[a.level]}</td><td><b>${esc(a.c.hostname)}</b></td>
      <td>${esc(a.text)}</td><td>${esc(tenantName(a.c.tenant_id))}</td><td>${a.c.location_id ? esc(locationName(a.c.location_id)) : "–"}</td></tr>`).join("")}
    </tbody></table>` : `<div class="clv-empty">✓ Keine Warnungen.</div>`;
  b.querySelectorAll("[data-client]").forEach((tr) => tr.addEventListener("click", () => goTo("client", tr.dataset.client)));
}

async function jobsList(box) {
  const b = section(box, "Aufgaben (Backend-Jobs)");
  b.innerHTML = `<div class="clv-empty">Wird geladen …</div>`;
  let mod;
  try { mod = await import("/js/app/bgjobs.js"); } catch (e) { b.innerHTML = `<div class="clv-empty">${esc(e.message)}</div>`; return; }
  const { listJobs, jobStatusText, cancelJob } = mod;
  const draw = async () => {
    let jobs = [];
    try { jobs = await listJobs({ history: true, limit: 60 }); }
    catch (e) { b.innerHTML = `<div class="clv-empty">${esc(e.message)}</div>`; return; }
    if (!document.body.contains(b)) return;
    b.innerHTML = jobs.length ? `<table class="clv-table"><thead><tr><th>Aufgabe</th><th>Benutzer</th><th>Gestartet</th><th>Status</th><th></th></tr></thead><tbody>
      ${jobs.map((j) => `<tr><td>${j.running ? "⏳" : j.status === "error" ? "❌" : j.status === "cancelled" ? "⛔" : "✅"} ${esc(j.title || j.kind)}</td>
        <td>${esc(j.user || "–")}</td><td>${j.started ? esc(new Date(j.started).toLocaleString("de-DE")) : "–"}</td>
        <td>${esc(jobStatusText(j))}</td>
        <td>${j.running && j.can_cancel !== false ? `<button class="btn" data-cancel="${esc(j.id)}">Abbrechen</button>` : ""}</td></tr>`).join("")}
      </tbody></table>` : `<div class="clv-empty">Noch keine Aufgaben.</div>`;
    b.querySelectorAll("[data-cancel]").forEach((x) => x.addEventListener("click", () => cancelJob(x.dataset.cancel).catch((e) => window.notify?.(e.message, "error"))));
  };
  await draw();
  // Live: bei jeder Job-Aenderung neu laden (gedrosselt)
  let tmr = null;
  const onUpd = () => { clearTimeout(tmr); tmr = setTimeout(draw, 500); };
  try {
    const { dashboardSocket } = await import("/js/app/socket.js");
    dashboardSocket.on("bgjob:update", onUpd);
    jobsStop = () => { clearTimeout(tmr); try { dashboardSocket.off("bgjob:update", onUpd); } catch {} };
  } catch { /* ohne Socket: statisch */ }
}

// ---------------------------------------------------------------- Navigation
function rerenderMain() {
  const el = document.getElementById("main-content");
  if (el) renderClassicView(el);
}

async function goTo(type, id) {
  if (type === "client") {
    try { const m = await import("/js/app/sidebar.js"); m.selectClientExternal(id); return; } catch { /* Fallback */ }
  }
  state.selection = { type, id };
  try { const m = await import("/js/app/sidebar.js"); m.renderSidebar(); } catch {}
  rerenderMain();
}

// ---------------------------------------------------------------- Live
/** Aus app.js bei client:metrics. Gibt 1 zurueck, wenn die klassische
 *  Ansicht das Update uebernommen hat (dann kein kompletter Neuaufbau). */
export function updateClassicView(clientId) {
  const root = document.querySelector("#main-content > .clv");
  if (!root) return 0;
  const box = root.querySelector(".clv-content");
  const s = sel();
  const client = s.type === "client" ? findClient(s.id) : null;
  if (s.type === "client" && s.id !== clientId) return 1;
  if (s.type !== "client" && s.type !== "dc" && !clientsIn(s).some((c) => c.id === clientId)) return 1;
  const cats = catsFor(s, client);
  const cat = currentCat(s, cats);
  if (!cats.find((c) => c.key === cat)?.live) return 1;
  // Nichts unter Cursor/Fingern wegrendern, max. alle 2 s
  const focus = document.activeElement;
  if (focus && box.contains(focus) && /^(INPUT|TEXTAREA|SELECT)$/.test(focus.tagName)) {
    if (box._clvRedraw) return 1;
  }
  const now = Date.now();
  if (now - lastLive < 2000) return 1;
  lastLive = now;
  root.querySelector(".clv-head").innerHTML = headHtml(s, client);
  if (s.type === "client") {
    // nur die Metrik-Karten bzw. den Live-Bereich ersetzen
    const holders = box.querySelectorAll(".cmetric-holder[data-metric]");
    if (holders.length && !box.querySelector(".clv-status")) {
      holders.forEach((h) => { if (!h.matches(":hover")) renderClientMetric(h, client, { id: `clv-${h.dataset.metric}`, type: "cmetric", metric: h.dataset.metric }); });
      return 1;
    }
    if (box.matches(":hover")) return 1;
    fillContent(box, s, client, cat);
    return 1;
  }
  if (box._clvRedraw) { if (!box.matches(":hover")) box._clvRedraw(); return 1; }
  if (!box.matches(":hover")) fillContent(box, s, client, cat);
  return 1;
}
