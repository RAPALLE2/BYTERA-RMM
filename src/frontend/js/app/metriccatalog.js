// metriccatalog.js
// ----------------
// Modular library of metric "presets" that can be shown as dashboard widgets.
// Each preset knows how to compute a value (or a distribution) from the
// current client state - whether shown as a number, donut, line chart, pie
// or table.
//
// Two kinds:
//   - scope "fleet":   one aggregated value over ALL clients
//                      (e.g. total power draw, total CPU capacity)
//   - scope "perhost": one value PER client (for tables / pie / bar),
//                      e.g. CPU model, RAM speed, ping time
//
// The chart kinds a preset supports are listed in .charts.

import { formatBytes } from "./utils.js";
import { getFleetIncludeVirtual } from "./persist.js";
import { t } from "./i18n.js";
// (getFleetIncludeVirtual removed - the counting mode is now per widget)

const fmtPct = (v) => `${Math.round(v)}%`;
const fmtW = (v) => `${Math.round(v)} W`;
const fmtMs = (v) => `${v} ms`;
const fmtMHz = (v) => `${(v / 1000).toFixed(2)} GHz`;
const fmtC = (v) => `${v} °C`;
const fmtBps = (v) => `${formatBytes(v)}/s`;

// Active (non-child) clients.
// Per-widget setting: does this widget count ALL devices, only physical or
// only virtual ones (VMs & LXCs)? Set by dashwidgets/clientmetrics before
// rendering (setHostScope) and reset afterwards.
// Default null = the global profile setting applies.
let _hostScope = null;   // null | "all" | "physical" | "virtual"
export function setHostScope(scope) { _hostScope = scope; }
export function clearHostScope() { _hostScope = null; }

// Virtual = explicitly marked as VM/LXC OR subordinate to a host.
const isVirtualClient = (c) => !!c.parent_client_id || (c.device_type || "physical") !== "physical";

// Status colors: physical and VM/LXC get RELATED but visibly different
// base colors of the status categories (physical). The VM/LXC variant is
// derived from it via virtualShade() - as with every other widget.
const STATUS_BASE = { Online: "#3ecf8e", Offline: "#64748b", Maintenance: "#f5a524" };

// Online/Offline(/Maintenance) rows. With the "all devices" counting mode
// each category is split into physical and VM/LXC (2 become 4, or 3 become
// 6 categories); empty categories are dropped.
// items = hostnames for the hover tooltips.
function statusRows(s, withMaintenance) {
  const statusOf = (c) => (withMaintenance && c.status_override === "maintenance")
    ? "Maintenance" : (c.online ? "Online" : "Offline");
  const order = withMaintenance ? ["Online", "Maintenance", "Offline"] : ["Online", "Offline"];
  const rows = distByDevice(hosts(s), statusOf, (label) => STATUS_BASE[label] || "#64748b");
  // Stable order (Online first), independent of group size.
  const rank = (r) => order.findIndex((o) => r.label.startsWith(o));
  return rows.sort((a, b) => (rank(a) - rank(b)) || (a.kind === "phys" ? -1 : 1));
}

function effectiveHostScope() {
  let scope = _hostScope;
  if (scope == null) scope = getFleetIncludeVirtual() ? "all" : "physical";
  return scope;
}

function hosts(state) {
  let list = state.clients || [];
  // Determine the counting mode: an explicitly set per-widget mode takes
  // precedence; otherwise the global profile setting "count VMs & LXCs as
  // full devices" (getFleetIncludeVirtual) applies.
  //   physical   -> physical devices only
  //   virtual    -> VMs & LXCs only
  //   all        -> all devices (VMs/LXCs included)
  const scope = effectiveHostScope();
  if (scope === "physical") {
    // IMPORTANT: exclude only real VMs/LXCs / child clients. Anything without
    // a set type counts as physical (default) so devices never
    // disappear entirely by accident.
    list = list.filter((c) => !isVirtualClient(c));
  } else if (scope === "virtual") {
    list = list.filter(isVirtualClient);
  }
  // BUGFIX: child clients (parent_client_id set, e.g. a VM subordinate to a
  // physical host) used to ALWAYS be filtered out -
  // so they were missing from the dashboard even in "all devices" mode.
  // With scope "all", ALL devices are now really counted.
  return list;
}
function onlineHosts(state) { return hosts(state).filter((c) => c.online && c.metrics); }

// -----------------------------------------------------------------
// DEVICE KIND IN EVERY WIDGET
// Physical devices and VMs/LXCs should differ in color in EVERY chart -
// but stay recognizably related. So the color of the VM/LXC variant is
// DERIVED from the base color (slight hue rotation + lighter + a little
// less saturated) instead of chosen freely.
// -----------------------------------------------------------------
const deviceKind = (c) => (isVirtualClient(c) ? "virt" : "phys");
export const DEVICE_LABEL = { phys: "physical", virt: "VM/LXC" };

function _hexToHsl(hex) {
  const m = String(hex).trim().replace("#", "");
  const full = m.length === 3 ? m.split("").map((x) => x + x).join("") : m;
  const r = parseInt(full.slice(0, 2), 16) / 255;
  const g = parseInt(full.slice(2, 4), 16) / 255;
  const b = parseInt(full.slice(4, 6), 16) / 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
  let h = 0;
  if (d) {
    if (max === r) h = ((g - b) / d) % 6;
    else if (max === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
  }
  h = (h * 60 + 360) % 360;
  const l = (max + min) / 2;
  const sat = d ? d / (1 - Math.abs(2 * l - 1)) : 0;
  return [h, sat, l];
}
function _hslToHex(h, sN, l) {
  h = ((h % 360) + 360) % 360;
  const c = (1 - Math.abs(2 * l - 1)) * sN, x = c * (1 - Math.abs(((h / 60) % 2) - 1)), m = l - c / 2;
  const [r, g, b] = h < 60 ? [c, x, 0] : h < 120 ? [x, c, 0] : h < 180 ? [0, c, x]
    : h < 240 ? [0, x, c] : h < 300 ? [x, 0, c] : [c, 0, x];
  const to = (v) => Math.round((v + m) * 255).toString(16).padStart(2, "0");
  return `#${to(r)}${to(g)}${to(b)}`;
}

// Derive the VM/LXC color from the base color (physical).
export function virtualShade(hex) {
  try {
    const [h, sN, l] = _hexToHsl(hex);
    return _hslToHex(h + 26, Math.max(0.28, sN * 0.85), Math.min(0.78, l + 0.10));
  } catch { return hex; }
}
// Color of a category depending on the device kind.
const kindColor = (base, kind) => (kind === "virt" ? virtualShade(base) : base);

// Splitting only makes sense when the widget counts both kinds at all.
const splitActive = () => effectiveHostScope() === "all";

const clientName = (c) => c.hostname || c.id;

// Generic distribution WITH device-kind split.
//   keyFn(c)   -> category key (e.g. "Windows")
// Result: [{label, value, color, items, kind}]
// With the "all devices" mode each category is split into "… (physical)"
// and "… (VM/LXC)"; otherwise the category gets the color tone matching
// the counting mode.
function distByDevice(list, keyFn, palette = CATEGORY_PALETTE) {
  const baseOf = typeof palette === "function"
    ? palette
    : (label, i) => palette[i % palette.length];
  const map = new Map();
  for (const c of list) {
    const key = keyFn(c) || "unknown";
    if (!map.has(key)) map.set(key, { phys: [], virt: [] });
    map.get(key)[deviceKind(c)].push(clientName(c));
  }
  const entries = [...map.entries()]
    .sort((a, b) => (b[1].phys.length + b[1].virt.length) - (a[1].phys.length + a[1].virt.length));
  const rows = [];
  entries.forEach(([label, g], i) => {
    const base = baseOf(label, i);
    if (splitActive()) {
      rows.push({ label: `${label} (physical)`, value: g.phys.length, color: base, items: g.phys, kind: "phys" });
      rows.push({ label: `${label} (VM/LXC)`, value: g.virt.length, color: virtualShade(base), items: g.virt, kind: "virt" });
    } else {
      const kind = effectiveHostScope() === "virtual" ? "virt" : "phys";
      rows.push({ label, value: g.phys.length + g.virt.length, color: kindColor(base, kind),
                  items: [...g.phys, ...g.virt], kind });
    }
  });
  // Drop empty categories (e.g. "Windows (VM/LXC)" with no such devices) -
  // otherwise tables/bars would show pointless zero rows.
  return rows.filter((r) => r.value > 0);
}
// Same data in donut format ({label, count, color, items}).
const segByDevice = (list, keyFn, palette) =>
  distByDevice(list, keyFn, palette)
    .map((r) => ({ label: r.label, count: r.value, color: r.color, items: r.items, kind: r.kind }));

// Base palette for categories (identical to the pie/donut palette so that
// split and non-split widgets look the same).
const CATEGORY_PALETTE = [
  "#4da6ff", "#3ecf8e", "#f5a524", "#ff4d6d", "#a78bfa",
  "#22d3ee", "#fb923c", "#e879f9", "#84cc16", "#f43f5e",
];

// Colors for rows that stand for EXACTLY ONE client (all "per client"
// widgets: bars, tables, lists, columns). Physical = blue, VM/LXC = the
// derived teal tone.
const HOST_BASE = "#4da6ff";
const HOST_COLORS = { phys: HOST_BASE, virt: virtualShade(HOST_BASE) };

// Sum / average helpers over online clients.
const sum = (arr, f) => arr.reduce((s, c) => s + (f(c) || 0), 0);
const avg = (arr, f) => (arr.length ? sum(arr, f) / arr.length : 0);

// -----------------------------------------------------------------
// Preset definitions. id is stable (stored in widgets).
// value(state)  -> number (for number/gauge/donut/line)
// donut(state)  -> {value, max, label, sub}  (optional, for donut/gauge)
// rows(state)   -> [{label, value, raw}]      (for table/pie/bar)
// charts        -> allowed chart kinds
// unit/format   -> display
// -----------------------------------------------------------------
export const PRESETS = [
  // ---------- SOFTWARE-PATCHING ----------
  // The numbers come ready from the backend: patching.refresh_summary()
  // writes them as columns on the client, so no extra query per widget is
  // needed here. 'other' (winget & co. without a rating) is deliberately
  // shown separately and not counted into "critical".
  {
    id: "fleet.patchesTotal", scope: "fleet", group: "Updates",
    get label() { return t("mk_fleet_patchesTotal"); }, charts: ["number", "stat", "line"],
    value: (s) => sum(hosts(s), (c) => c.patch_count || 0),
  },
  {
    id: "fleet.patchesSecurity", scope: "fleet", group: "Updates",
    get label() { return t("mk_fleet_patchesSecurity"); }, charts: ["number", "stat", "line"],
    value: (s) => sum(hosts(s), (c) => c.patch_security || 0),
  },
  {
    id: "fleet.patchClients", scope: "fleet", group: "Updates",
    get label() { return t("mk_fleet_patchClients"); }, charts: ["donut", "pie", "number"],
    value: (s) => hosts(s).filter((c) => (c.patch_count || 0) > 0).length,
    donut: (s) => ({
      value: hosts(s).filter((c) => (c.patch_count || 0) > 0).length,
      max: hosts(s).length || 1, label: "Affected",
      sub: `${hosts(s).length} Clients`,
    }),
    rows: (s) => {
      const all = hosts(s);
      const off = all.filter((c) => (c.patch_count || 0) > 0).length;
      return [
        { label: "Pending updates", value: off, color: "#f5a524" },
        { label: "Up to date", value: all.length - off, color: "#3ecf8e" },
      ];
    },
  },
  {
    id: "fleet.patchLevels", scope: "fleet", group: "Updates",
    get label() { return t("mk_fleet_patchLevels"); }, charts: ["bar", "pie", "column", "table"],
    rows: (s) => {
      const h = hosts(s);
      const sec = sum(h, (c) => c.patch_security || 0);
      const crit = sum(h, (c) => c.patch_critical || 0);
      const rest = Math.max(0, sum(h, (c) => c.patch_count || 0) - sec - crit);
      return [
        { label: "Security", value: sec, color: "#ff4d6d" },
        { label: "Critical", value: crit, color: "#f5a524" },
        { get label() { return t("u_ubrige"); }, value: rest, color: "#4da6ff" },
      ].filter((r) => r.value > 0);
    },
  },
  {
    id: "fleet.patchReboot", scope: "fleet", group: "Updates",
    get label() { return t("mk_fleet_patchReboot"); }, charts: ["number", "stat"],
    value: (s) => hosts(s).filter((c) => c.patch_reboot).length,
  },
  {
    id: "host.patchCount", scope: "perhost", group: "Updates",
    get label() { return t("mk_host_patchCount"); }, charts: ["table", "bar", "column"],
    rows: (s) => hosts(s)
      .filter((c) => (c.patch_count || 0) > 0)
      .map((c) => ({
        label: c.hostname || c.id,
        value: c.patch_count || 0,
        // Make security gaps visible, not just the total count.
        raw: (c.patch_security || 0)
          ? `${c.patch_count} pending (${c.patch_security} security)`
          : `${c.patch_count} pending`,
        color: (c.patch_security || 0) ? "#ff4d6d"
          : (c.patch_critical || 0) ? "#f5a524" : "#4da6ff",
      }))
      .sort((a, b) => b.value - a.value),
  },
  {
    id: "host.patchScanAge", scope: "perhost", group: "Updates",
    get label() { return t("mk_host_patchScanAge"); }, charts: ["table", "bar"],
    rows: (s) => hosts(s).map((c) => {
      if (!c.patch_last_scan) return { label: c.hostname || c.id, value: 999, raw: "never scanned" };
      const d = Math.floor((Date.now() - c.patch_last_scan) / 86400000);
      return { label: c.hostname || c.id, value: d,
               raw: d === 0 ? "today" : `${d} day(s) ago` };
    }).sort((a, b) => b.value - a.value),
  },

  // ---------- FLEET-AGGREGATE ----------
  {
    id: "fleet.count", scope: "fleet", group: "Flotte", get label() { return t("mk_fleet_count"); },
    charts: ["number"], value: (s) => hosts(s).length,
  },
  {
    id: "fleet.online", scope: "fleet", group: "Flotte", get label() { return t("mk_fleet_online"); },
    charts: ["donut", "pie", "number"],
    value: (s) => hosts(s).filter((c) => c.online).length,
    donut: (s) => ({ value: hosts(s).filter((c) => c.online).length, max: hosts(s).length, label: "Online", sub: `${hosts(s).length} total` }),
    rows: (s) => statusRows(s, false),
  },
  {
    id: "fleet.cpuCapacity", scope: "fleet", group: "CPU", get label() { return t("mk_fleet_cpuCapacity"); },
    charts: ["number"], format: (v) => `${v} Threads`,
    value: (s) => sum(onlineHosts(s), (c) => c.metrics.cpuThreads),
  },
  {
    id: "fleet.cpuLoadAvg", scope: "fleet", group: "CPU", get label() { return t("mk_fleet_cpuLoadAvg"); },
    charts: ["gauge", "donut", "number", "line"], unit: "%", format: fmtPct, max: 100,
    value: (s) => Math.round(avg(onlineHosts(s), (c) => c.metrics.cpuLoad)),
    donut: (s) => ({ value: Math.round(avg(onlineHosts(s), (c) => c.metrics.cpuLoad)), max: 100, label: "CPU Ø", sub: `${onlineHosts(s).length} online` }),
  },
  {
    id: "fleet.cpuLoadWeighted", scope: "fleet", group: "CPU", get label() { return t("mk_fleet_cpuLoadWeighted"); },
    charts: ["gauge", "donut", "number"], unit: "%", format: fmtPct, max: 100,
    value: (s) => {
      const h = onlineHosts(s);
      const cap = sum(h, (c) => c.metrics.cpuThreads);
      const used = sum(h, (c) => c.metrics.cpuThreads * (c.metrics.cpuLoad / 100));
      return cap ? Math.round((used / cap) * 100) : 0;
    },
  },
  {
    id: "fleet.ramTotal", scope: "fleet", group: "RAM", get label() { return t("mk_fleet_ramTotal"); },
    charts: ["number"], format: formatBytes,
    value: (s) => sum(onlineHosts(s), (c) => c.metrics.memTotal),
  },
  {
    id: "fleet.ramUsed", scope: "fleet", group: "RAM", get label() { return t("mk_fleet_ramUsed"); },
    charts: ["donut", "gauge", "number"], format: formatBytes,
    value: (s) => sum(onlineHosts(s), (c) => c.metrics.memUsed),
    donut: (s) => {
      const used = sum(onlineHosts(s), (c) => c.metrics.memUsed);
      const total = sum(onlineHosts(s), (c) => c.metrics.memTotal);
      return { value: used, max: total, label: "RAM", sub: `${formatBytes(used)} / ${formatBytes(total)}`, pctText: true };
    },
  },
  {
    id: "fleet.power", scope: "fleet", group: "Strom", get label() { return t("mk_fleet_power"); },
    charts: ["number", "line", "bar"], format: fmtW,
    value: (s) => Math.round(sum(onlineHosts(s), (c) => c.metrics.powerWatts || 0)),
    rows: (s) => onlineHosts(s).filter((c) => c.metrics.powerWatts).map((c) => ({ label: c.hostname, value: c.metrics.powerWatts, raw: fmtW(c.metrics.powerWatts) })),
  },
  {
    id: "fleet.diskTotal", scope: "fleet", group: "Disk", get label() { return t("mk_fleet_diskTotal"); },
    charts: ["number"], format: formatBytes,
    value: (s) => sum(onlineHosts(s), (c) => c.metrics.diskTotal),
  },
  {
    id: "fleet.netIn", scope: "fleet", group: "Netzwerk", get label() { return t("mk_fleet_netIn"); },
    charts: ["number", "line"], format: fmtBps,
    value: (s) => sum(onlineHosts(s), (c) => c.metrics.netIn || 0),
  },
  {
    id: "fleet.netOut", scope: "fleet", group: "Netzwerk", get label() { return t("mk_fleet_netOut"); },
    charts: ["number", "line"], format: fmtBps,
    value: (s) => sum(onlineHosts(s), (c) => c.metrics.netOut || 0),
  },
  {
    id: "fleet.osDist", scope: "fleet", group: "Verteilung", get label() { return t("mk_fleet_osDist"); },
    charts: ["pie", "donut", "table"],
    rows: (s) => distByDevice(hosts(s), (c) => osKey(c)),
  },
  {
    id: "fleet.gpuDist", scope: "fleet", group: "Verteilung", get label() { return t("mk_fleet_gpuDist"); },
    charts: ["pie", "table"],
    rows: (s) => distByDevice(hosts(s), (c) => (c.metrics?.gpuModels?.[0]) || "unknown"),
  },
  {
    id: "fleet.archDist", scope: "fleet", group: "Verteilung", get label() { return t("mk_fleet_archDist"); },
    charts: ["pie", "donut", "table"],
    rows: (s) => distByDevice(hosts(s), (c) => c.metrics?.arch || c.arch || "?"),
  },
  {
    id: "fleet.agentDist", scope: "fleet", group: "Verteilung", get label() { return t("mk_fleet_agentDist"); },
    charts: ["pie", "table"],
    rows: (s) => distByDevice(hosts(s), (c) => c.agent_version || t("unknown")),
  },
  // ---------- FLEET OVERVIEW (interactive donuts like on the dashboard) ----------
  // These three presets are the former fixed "fleet overview" - now as
  // normal widgets: freely movable, editable, deletable, re-insertable.
  // segments(state) returns groups INCLUDING the affected client names
  // (for the hover tooltip); rows() additionally allows pie/bar/table.
  {
    id: "fleet.statusDonut", scope: "fleet", group: "Flotten-Übersicht",
    get label() { return t("u_agenten_on_off"); }, charts: ["fleetdonut", "pie", "bar", "table"],
    // Same split as fleet.online (incl. maintenance): in "all devices" mode
    // each status category is separated into physical and VM/LXC.
    segments: (s) => statusRows(s, true)
      .map((r) => ({ label: r.label, count: r.value, color: r.color, items: r.items })),
    rows: (s) => statusRows(s, true),
  },
  {
    id: "fleet.osDonut", scope: "fleet", group: "Flotten-Übersicht",
    get label() { return t("u_betriebssysteme"); }, charts: ["fleetdonut", "pie", "bar", "table"],
    segments: (s) => segByDevice(hosts(s), (c) => osKey(c)),
    rows: (s) => distByDevice(hosts(s), (c) => osKey(c)),
  },
  {
    id: "fleet.versionDonut", scope: "fleet", group: "Flotten-Übersicht",
    get label() { return t("u_agent_versionen"); }, charts: ["fleetdonut", "pie", "bar", "table"],
    segments: (s) => segByDevice(hosts(s), (c) => c.agent_version || t("unknown")),
    rows: (s) => distByDevice(hosts(s), (c) => c.agent_version || t("unknown")),
  },
  {
    // Compact "fleet overview" as ONE widget: online/offline, operating
    // systems and agent versions as three small pie charts.
    id: "fleet.overview", scope: "fleet", group: "Flotte", get label() { return t("mk_fleet_overview"); },
    charts: ["overview"],
    sections: (s) => [
      { title: "Online / Offline", rows: statusRows(s, false) },
      { title: "Operating systems", rows: distByDevice(hosts(s), (c) => osKey(c)) },
      { title: "Agent versions", rows: distByDevice(hosts(s), (c) => c.agent_version || t("unknown")) },
    ],
  },

  // ---------- PER-HOST (tables / pie / bar) ----------
  // ---------- WARRANTY ----------
  {
    id: "fleet.warrantyExpiring", scope: "fleet", group: "Garantie",
    label: "Warranties expiring within 90 days", charts: ["number"],
    value: (s) => hosts(s).filter((c) => {
      if (!c.warranty_until) return false;
      const d = (c.warranty_until - Date.now()) / 86400000;
      return d >= 0 && d <= 90;
    }).length,
  },
  {
    id: "fleet.warrantyStatus", scope: "fleet", group: "Garantie",
    get label() { return t("mc_warranty_dist"); }, charts: ["donut", "pie", "table", "bar"],
    rows: (s) => {
      // The category is built as TRANSLATED text, but color and order hang
      // on an internal key. Previously the German text was display, color key
      // AND sort key at the same time - in English colors and ordering would
      // have been lost that way.
      const days = (c) => (c.warranty_until - Date.now()) / 86400000;
      const bucketKey = (c) => !c.warranty_until ? "none"
        : days(c) < 0 ? "expired"
        : days(c) <= 30 ? "d30"
        : days(c) <= 90 ? "d90" : "over90";
      const LABEL = { expired: t("mc_w_expired"), d30: "\u2264 30 " + t("au_days"),
                      d90: "\u2264 90 " + t("au_days"), over90: "> 90 " + t("au_days"),
                      none: t("mc_w_nodate") };
      const COLORS = { expired: "#ff4d6d", d30: "#ff8b3d", d90: "#f5a524",
                       over90: "#3ecf8e", none: "#64748b" };
      const order = ["expired", "d30", "d90", "over90", "none"];
      const bucket = (c) => LABEL[bucketKey(c)];
      const colorOf = {};
      for (const k of order) colorOf[LABEL[k]] = COLORS[k];
      const rows = distByDevice(hosts(s), bucket, (label) => colorOf[label] || "#64748b");
      const rank = (r) => order.findIndex((o) => r.label.startsWith(LABEL[o]));
      return rows.sort((a, b) => (rank(a) - rank(b)) || (a.kind === "phys" ? -1 : 1));
    },
  },
  {
    // Warranty overview of ALL clients as its own compact list
    // (the "warrantylist" rendering in dashwidgets.js). Sort: what expires
    // first is at the top; devices without a date at the very bottom.
    id: "fleet.warrantyOverview", scope: "fleet", group: "Garantie",
    get label() { return t("u_garantie_ubersicht_alle_clients"); },
    charts: ["warrantylist", "table", "bar"],
    rows: (s) => hosts(s).map((c) => {
      const has = !!c.warranty_until;
      const days = has ? Math.floor((c.warranty_until - Date.now()) / 86400000) : null;
      const date = has ? new Date(c.warranty_until).toLocaleDateString() : null;
      const color = !has ? "#64748b"
        : days < 0 ? "#ff4d6d" : days <= 30 ? "#ff8b3d" : days <= 90 ? "#f5a524" : "#3ecf8e";
      const text = !has ? t("mc_w_nodate")
        : days < 0 ? t("mc_w_expired_days", { n: Math.abs(days) })
        : days > 365 ? t("dw_years_left", { y: Math.floor(days / 365), m: Math.floor((days % 365) / 30) })
        : days > 60 ? t("dw_months_left", { n: Math.floor(days / 30) }) : t("dw_days_left", { n: days });
      return {
        label: c.hostname || c.id,
        value: has ? Math.max(0, days) : 0,
        raw: has ? t("mc_w_until", { text, date }) : t("mc_w_nodate"),
        status: text, until: date, days, color,
        kind: deviceKind(c),
      };
    }).sort((a, b) => {
      if (a.days === null && b.days === null) return a.label.localeCompare(b.label);
      if (a.days === null) return 1;
      if (b.days === null) return -1;
      return a.days - b.days;
    }),
  },
  {
    id: "host.warranty", scope: "perhost", group: "Garantie",
    label: "Warranty time left per client", charts: ["table", "bar"],
    rows: (s) => hosts(s).filter((c) => c.warranty_until).map((c) => {
      const d = Math.floor((c.warranty_until - Date.now()) / 86400000);
      const date = new Date(c.warranty_until).toLocaleDateString("de-DE");
      return { label: c.hostname || c.id, value: Math.max(0, d),
               raw: d < 0 ? `expired (${date})` : `${d} days (until ${date})` };
    }).sort((a, b) => a.value - b.value),
  },

  {
    id: "host.cpuLoad", scope: "perhost", group: "CPU", get label() { return t("mk_host_cpuLoad"); },
    charts: ["table", "bar"], unit: "%",
    rows: (s) => onlineHosts(s).map((c) => ({ label: c.hostname, value: c.metrics.cpuLoad, raw: fmtPct(c.metrics.cpuLoad) })).sort((a, b) => b.value - a.value),
  },
  // ---------- RAM je Client ----------
  {
    id: "host.ramPct", scope: "perhost", group: "RAM", get label() { return t("mk_host_ramPct"); },
    charts: ["bar", "table", "pie"], unit: "%", format: fmtPct,
    rows: (s) => onlineHosts(s).filter((c) => c.metrics.memTotal).map((c) => {
      const p = Math.round((c.metrics.memUsed / c.metrics.memTotal) * 100);
      return { label: c.hostname, value: p, raw: `${p}% (${formatBytes(c.metrics.memUsed)} / ${formatBytes(c.metrics.memTotal)})` };
    }).sort((a, b) => b.value - a.value),
  },
  {
    id: "host.ramUsed", scope: "perhost", group: "RAM", get label() { return t("mk_host_ramUsed"); },
    charts: ["bar", "table"], format: formatBytes,
    rows: (s) => onlineHosts(s).filter((c) => c.metrics.memUsed != null).map((c) => ({ label: c.hostname, value: c.metrics.memUsed, raw: formatBytes(c.metrics.memUsed) })).sort((a, b) => b.value - a.value),
  },
  {
    id: "host.ramTotal", scope: "perhost", group: "RAM", get label() { return t("mk_host_ramTotal"); },
    charts: ["bar", "table"], format: formatBytes,
    rows: (s) => hosts(s).filter((c) => c.metrics?.memTotal).map((c) => ({ label: c.hostname, value: c.metrics.memTotal, raw: formatBytes(c.metrics.memTotal) })).sort((a, b) => b.value - a.value),
  },
  {
    id: "host.swap", scope: "perhost", group: "RAM", get label() { return t("mk_host_swap"); },
    charts: ["bar", "table"], unit: "%", format: fmtPct,
    rows: (s) => onlineHosts(s).filter((c) => c.metrics.swapTotal).map((c) => {
      const p = Math.round((c.metrics.swapUsed / c.metrics.swapTotal) * 100);
      return { label: c.hostname, value: p, raw: `${p}%` };
    }).sort((a, b) => b.value - a.value),
  },
  // ---------- GPU je Client ----------
  {
    id: "host.gpuLoad", scope: "perhost", group: "GPU", get label() { return t("mk_host_gpuLoad"); },
    charts: ["bar", "table", "pie"], unit: "%", format: fmtPct,
    rows: (s) => onlineHosts(s).filter((c) => (c.metrics.gpus || [])[0]?.load != null).map((c) => ({ label: c.hostname, value: c.metrics.gpus[0].load, raw: fmtPct(c.metrics.gpus[0].load) })).sort((a, b) => b.value - a.value),
  },
  {
    id: "host.gpuTemp", scope: "perhost", group: "GPU", get label() { return t("mk_host_gpuTemp"); },
    charts: ["bar", "table"], format: fmtC,
    rows: (s) => onlineHosts(s).filter((c) => (c.metrics.gpus || [])[0]?.temp != null).map((c) => ({ label: c.hostname, value: c.metrics.gpus[0].temp, raw: fmtC(c.metrics.gpus[0].temp) })).sort((a, b) => b.value - a.value),
  },
  {
    id: "host.gpuMem", scope: "perhost", group: "GPU", get label() { return t("mk_host_gpuMem"); },
    charts: ["bar", "table"], format: formatBytes,
    rows: (s) => onlineHosts(s).filter((c) => (c.metrics.gpus || [])[0]?.memUsed != null).map((c) => ({ label: c.hostname, value: c.metrics.gpus[0].memUsed, raw: `${formatBytes(c.metrics.gpus[0].memUsed)}${c.metrics.gpus[0].memTotal ? " / " + formatBytes(c.metrics.gpus[0].memTotal) : ""}` })).sort((a, b) => b.value - a.value),
  },
  {
    id: "host.gpuPower", scope: "perhost", group: "GPU", get label() { return t("mk_host_gpuPower"); },
    charts: ["bar", "table"], format: fmtW,
    rows: (s) => onlineHosts(s).filter((c) => (c.metrics.gpus || [])[0]?.power != null).map((c) => ({ label: c.hostname, value: c.metrics.gpus[0].power, raw: fmtW(c.metrics.gpus[0].power) })).sort((a, b) => b.value - a.value),
  },
  // ---------- Disk & Netzwerk je Client ----------
  {
    id: "host.diskPct", scope: "perhost", group: "Disk", get label() { return t("mk_host_diskPct"); },
    charts: ["bar", "table", "pie"], unit: "%", format: fmtPct,
    rows: (s) => onlineHosts(s).filter((c) => c.metrics.diskTotal).map((c) => {
      const p = Math.round((c.metrics.diskUsed / c.metrics.diskTotal) * 100);
      return { label: c.hostname, value: p, raw: `${p}% (${formatBytes(c.metrics.diskUsed)} / ${formatBytes(c.metrics.diskTotal)})` };
    }).sort((a, b) => b.value - a.value),
  },
  {
    id: "host.netIn", scope: "perhost", group: "Netzwerk", get label() { return t("mk_host_netIn"); },
    charts: ["bar", "table"], format: fmtBps,
    rows: (s) => onlineHosts(s).filter((c) => c.metrics.netIn != null).map((c) => ({ label: c.hostname, value: c.metrics.netIn, raw: fmtBps(c.metrics.netIn) })).sort((a, b) => b.value - a.value),
  },
  {
    id: "host.netOut", scope: "perhost", group: "Netzwerk", get label() { return t("mk_host_netOut"); },
    charts: ["bar", "table"], format: fmtBps,
    rows: (s) => onlineHosts(s).filter((c) => c.metrics.netOut != null).map((c) => ({ label: c.hostname, value: c.metrics.netOut, raw: fmtBps(c.metrics.netOut) })).sort((a, b) => b.value - a.value),
  },
  {
    id: "host.ipList", scope: "perhost", group: "Identität", get label() { return t("mk_host_ipList"); },
    charts: ["table"],
    rows: (s) => hosts(s).map((c) => ({ label: c.hostname, raw: c.ip || c.metrics?.ip || "—" })),
  },
  {
    id: "host.macList", scope: "perhost", group: "Identität", get label() { return t("mk_host_macList"); },
    charts: ["table"],
    rows: (s) => hosts(s).map((c) => ({ label: c.hostname, raw: c.metrics?.mac || "—" })),
  },
  {
    id: "host.deviceTypeList", scope: "perhost", group: "Identität", get label() { return t("mk_host_deviceTypeList"); },
    charts: ["table"],
    rows: (s) => hosts(s).map((c) => ({ label: c.hostname, raw: ({ vm: "VM", lxc: "LXC", physical: t("mc_physical") }[c.device_type || "physical"]) })),
  },
  // ---------- RAM/GPU/Disk aggregates across the fleet ----------
  {
    id: "fleet.ramLoadAvg", scope: "fleet", group: "RAM", get label() { return t("mk_fleet_ramLoadAvg"); },
    charts: ["gauge", "donut", "number"], unit: "%", format: fmtPct, max: 100,
    value: (s) => Math.round(avg(onlineHosts(s).filter((c) => c.metrics.memTotal), (c) => (c.metrics.memUsed / c.metrics.memTotal) * 100)),
    donut: (s) => ({ value: Math.round(avg(onlineHosts(s).filter((c) => c.metrics.memTotal), (c) => (c.metrics.memUsed / c.metrics.memTotal) * 100)), max: 100, label: "RAM Ø", sub: `${onlineHosts(s).length} online` }),
  },
  {
    id: "fleet.gpuLoadAvg", scope: "fleet", group: "GPU", get label() { return t("mk_fleet_gpuLoadAvg"); },
    charts: ["gauge", "donut", "number"], unit: "%", format: fmtPct, max: 100,
    value: (s) => Math.round(avg(onlineHosts(s).filter((c) => (c.metrics.gpus || [])[0]), (c) => c.metrics.gpus[0].load || 0)),
    donut: (s) => ({ value: Math.round(avg(onlineHosts(s).filter((c) => (c.metrics.gpus || [])[0]), (c) => c.metrics.gpus[0].load || 0)), max: 100, label: "GPU Ø", sub: `${onlineHosts(s).filter((c) => (c.metrics.gpus || [])[0]).length} with GPU` }),
  },
  {
    id: "fleet.gpuPowerTotal", scope: "fleet", group: "GPU", get label() { return t("mk_fleet_gpuPowerTotal"); },
    charts: ["number", "line"], format: fmtW,
    value: (s) => Math.round(sum(onlineHosts(s), (c) => (c.metrics.gpus || [])[0]?.power || 0)),
  },
  {
    id: "fleet.diskLoadAvg", scope: "fleet", group: "Disk", get label() { return t("mk_fleet_diskLoadAvg"); },
    charts: ["gauge", "donut", "number"], unit: "%", format: fmtPct, max: 100,
    value: (s) => Math.round(avg(onlineHosts(s).filter((c) => c.metrics.diskTotal), (c) => (c.metrics.diskUsed / c.metrics.diskTotal) * 100)),
    donut: (s) => ({ value: Math.round(avg(onlineHosts(s).filter((c) => c.metrics.diskTotal), (c) => (c.metrics.diskUsed / c.metrics.diskTotal) * 100)), max: 100, label: "Disk Ø", sub: `${onlineHosts(s).length} online` }),
  },
  {
    id: "host.cpuModel", scope: "perhost", group: "Hardware", get label() { return t("mk_host_cpuModel"); },
    charts: ["table"],
    rows: (s) => hosts(s).map((c) => ({ label: c.hostname, raw: c.metrics?.cpuModel || "—" })),
  },
  {
    id: "host.cpuFreq", scope: "perhost", group: "CPU", get label() { return t("mk_host_cpuFreq"); },
    charts: ["table", "bar"], format: fmtMHz,
    rows: (s) => onlineHosts(s).filter((c) => c.metrics.cpuFreq).map((c) => ({ label: c.hostname, value: c.metrics.cpuFreq, raw: fmtMHz(c.metrics.cpuFreq) })).sort((a, b) => b.value - a.value),
  },
  {
    id: "host.cpuTemp", scope: "perhost", group: "Temperatur", get label() { return t("mk_host_cpuTemp"); },
    charts: ["table", "bar"], format: fmtC,
    rows: (s) => onlineHosts(s).filter((c) => c.metrics.cpuTemp != null).map((c) => ({ label: c.hostname, value: c.metrics.cpuTemp, raw: fmtC(c.metrics.cpuTemp) })).sort((a, b) => b.value - a.value),
  },
  {
    id: "host.ramModel", scope: "perhost", group: "Hardware", get label() { return t("mk_host_ramModel"); },
    charts: ["table"],
    rows: (s) => hosts(s).map((c) => {
      const mods = c.metrics?.ramModules || [];
      const desc = mods.length
        ? mods.map((m) => (m.vendor ? m.vendor + " " : "") + (m.size ? formatBytes(m.size) : (m.size_str || "")) + (m.speed ? " @" + m.speed + "MHz" : (m.speed_str ? " @" + m.speed_str : ""))).join(", ")
        : "—";
      return { label: c.hostname, raw: desc };
    }),
  },
  {
    id: "host.gpuModel", scope: "perhost", group: "Hardware", get label() { return t("mk_host_gpuModel"); },
    charts: ["table"],
    rows: (s) => hosts(s).map((c) => ({ label: c.hostname, raw: (c.metrics?.gpuModels || []).join(", ") || "—" })),
  },
  {
    id: "host.power", scope: "perhost", group: "Strom", get label() { return t("mk_host_power"); },
    charts: ["table", "bar", "pie"], format: fmtW,
    rows: (s) => onlineHosts(s).filter((c) => c.metrics.powerWatts != null).map((c) => ({ label: c.hostname, value: c.metrics.powerWatts, raw: fmtW(c.metrics.powerWatts) })).sort((a, b) => b.value - a.value),
  },
  {
    id: "host.fan", scope: "perhost", group: "Sensorik", get label() { return t("mk_host_fan"); },
    charts: ["table", "bar"], format: (v) => `${v} RPM`,
    rows: (s) => onlineHosts(s).filter((c) => c.metrics.fanSpeed != null).map((c) => ({ label: c.hostname, value: c.metrics.fanSpeed, raw: `${c.metrics.fanSpeed} RPM` })).sort((a, b) => b.value - a.value),
  },
  {
    id: "host.diskRW", scope: "perhost", group: "Disk", get label() { return t("mk_host_diskRW"); },
    charts: ["table"],
    rows: (s) => onlineHosts(s).filter((c) => c.metrics.diskRead != null).map((c) => ({ label: c.hostname, raw: `↓${fmtBps(c.metrics.diskRead)} ↑${fmtBps(c.metrics.diskWrite || 0)}` })),
  },
  {
    id: "host.io", scope: "perhost", group: "Disk", get label() { return t("mk_host_io"); },
    charts: ["bar", "table"], format: fmtBps,
    rows: (s) => onlineHosts(s).filter((c) => c.metrics.diskRead != null).map((c) => ({ label: c.hostname, value: (c.metrics.diskRead || 0) + (c.metrics.diskWrite || 0), raw: fmtBps((c.metrics.diskRead || 0) + (c.metrics.diskWrite || 0)) })).sort((a, b) => b.value - a.value),
  },
  {
    id: "host.pingGoogle", scope: "perhost", group: "Netzwerk", get label() { return t("mk_host_pingGoogle"); },
    charts: ["table", "bar"], format: fmtMs,
    rows: (s) => onlineHosts(s).filter((c) => c.metrics.ping?.google != null).map((c) => ({ label: c.hostname, value: c.metrics.ping.google, raw: fmtMs(c.metrics.ping.google) })).sort((a, b) => a.value - b.value),
  },
  {
    id: "host.pingCf", scope: "perhost", group: "Netzwerk", get label() { return t("mk_host_pingCf"); },
    charts: ["table", "bar"], format: fmtMs,
    rows: (s) => onlineHosts(s).filter((c) => c.metrics.ping?.cloudflare != null).map((c) => ({ label: c.hostname, value: c.metrics.ping.cloudflare, raw: fmtMs(c.metrics.ping.cloudflare) })).sort((a, b) => a.value - b.value),
  },
  {
    id: "host.load", scope: "perhost", group: "CPU", get label() { return t("mk_host_load"); },
    charts: ["table", "bar"],
    rows: (s) => onlineHosts(s).filter((c) => c.metrics.load1 != null).map((c) => ({ label: c.hostname, value: c.metrics.load1, raw: String(c.metrics.load1) })).sort((a, b) => b.value - a.value),
  },
  {
    id: "host.uptime", scope: "perhost", group: "System", get label() { return t("mk_host_uptime"); },
    charts: ["table"],
    rows: (s) => onlineHosts(s).map((c) => ({ label: c.hostname, value: c.metrics.uptime, raw: fmtUptime(c.metrics.uptime) })).sort((a, b) => b.value - a.value),
  },
  {
    id: "host.procs", scope: "perhost", group: "System", get label() { return t("mk_host_procs"); },
    charts: ["table", "bar"],
    rows: (s) => onlineHosts(s).filter((c) => c.metrics.procCount != null).map((c) => ({ label: c.hostname, value: c.metrics.procCount, raw: String(c.metrics.procCount) })).sort((a, b) => b.value - a.value),
  },
  {
    id: "host.battery", scope: "perhost", group: "Strom", get label() { return t("mk_host_battery"); },
    charts: ["table", "bar"], format: fmtPct,
    rows: (s) => onlineHosts(s).filter((c) => c.metrics.battery).map((c) => ({ label: c.hostname, value: c.metrics.battery.percent, raw: `${c.metrics.battery.percent}%${c.metrics.battery.plugged ? " ⚡" : ""}` })).sort((a, b) => a.value - b.value),
  },
];

// -----------------------------------------------------------------
// DEVICE KIND IN ALL "PER CLIENT" WIDGETS TOO
// Each row of a perhost preset stands for exactly ONE client. Instead of
// giving each preset its own color logic, it is retrofitted ONCE centrally
// here: rows get the color of their device kind (physical = blue,
// VM/LXC = the derived teal tone) and a 📦 marker in the label.
// This makes bars, columns, tables, lists and pies of all client widgets
// distinguishable by device kind.
// -----------------------------------------------------------------
for (const preset of PRESETS) {
  if (preset.scope !== "perhost" || typeof preset.rows !== "function") continue;
  const inner = preset.rows;
  preset.rows = (s) => {
    const rows = inner(s) || [];
    // Map row -> client via the hostname (all perhost presets use
    // c.hostname as the label).
    const byName = new Map();
    for (const c of (s.clients || [])) byName.set(c.hostname || c.id, c);
    return rows.map((r) => {
      if (r.color) return r;                    // an own color takes precedence
      const c = byName.get(r.label);
      if (!c) return r;
      const kind = deviceKind(c);
      return {
        ...r, kind,
        color: HOST_COLORS[kind],
        // Marker only when the widget shows both kinds.
        label: kind === "virt" && splitActive() ? `📦 ${r.label}` : r.label,
      };
    });
  };
}

export function presetById(id) { return PRESETS.find((p) => p.id === id) || null; }

// Presets sorted by group (for the picker dialog).
export function presetsByGroup() {
  const map = new Map();
  for (const p of PRESETS) {
    if (!map.has(p.group)) map.set(p.group, []);
    map.get(p.group).push(p);
  }
  return [...map.entries()];
}

// --- Helpers ---
function osKey(c) {
  const p = (c.platform || "").toLowerCase();
  if (p.includes("win")) return "Windows";
  if (p.includes("darwin") || p.includes("mac")) return "macOS";
  if (p.includes("linux")) return "Linux";
  return c.platform || "unknown";
}
function fmtUptime(sec) {
  if (!sec) return "—";
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
}

// Group names are translated for DISPLAY ONLY. The value of preset.group
// stays unchanged because it serves as a key:
// presetAvailable() checks it against HIDE_GROUPS_VM/_LXC. Translating it
// would silently break those filters and VMs would suddenly be offered
// GPU and hardware panels.
export function groupLabel(group) {
  const keys = {"CPU": "mg_cpu", "GPU": "mg_gpu", "RAM": "mg_ram", "Disk": "mg_disk", "Netzwerk": "mg_netzwerk", "Strom & Sensoren": "mg_strom_sensoren", "Hardware": "mg_hardware", "System": "mg_system", "Identität": "mg_identit_t", "Flotten-Übersicht": "mg_fleet_overview", "Garantie": "mg_warranty", "Flotte": "mg_flotte", "Verteilung": "mg_verteilung", "Strom": "mg_strom", "Sensorik": "mg_sensorik", "Temperatur": "mg_temperatur", "Updates": "mg_updates"};
  return keys[group] ? t(keys[group]) : group;
}
