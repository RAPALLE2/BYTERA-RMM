// topology.js (module "topology")
// ------------------------------
// Network topology map - an editor in the spirit of the game "Upload Labs".
//
//  * discovery from the backend or from the network of a node (backend job)
//  * nodes: type icon, name, IP, OS; agents are detected automatically or assigned
//    by hand (also to own nodes) - a click opens the client
//  * lines: style per map (Default / PCB / Web / Cable) and per line, colour,
//    name, labels at both ends (e.g. "8080", "port 4", "WAN", "LAN2"), arrows,
//    named FLOWS with colour + legend (e.g. "Backup" orange, "Sync" blue)
//  * boxes to frame areas (e.g. a PVE node with all its CTs) - moving a box
//    moves what is inside
//  * snap grid, shift+drag selects, line tool stays on until switched off
//  * background (lines / squares / crosses / honeycomb / none) and snapping are
//    personal; everything else on the map is the same for all users.

import { moduleRequest } from "/js/app/modules.js";
import { api } from "/js/app/api.js";
import { esc, uiConfirm, uiPrompt } from "/js/app/utils.js";
import { openWindow, registerCleanup } from "/js/app/windowmanager.js";
import { nodeHint } from "/js/app/nodechoice.js";
import { findClient } from "/js/app/state.js";
import { watchJobs, cancelJob, jobDuration } from "/js/app/bgjobs.js";
import { dashboardSocket } from "/js/app/socket.js";
import { iconBody, TYPE_LABELS, EDIT_TYPES, clientIconType } from "./icons.js";

const NS = "http://www.w3.org/2000/svg";
const GRID = 20;
const HALF = 34;                       // half size of a node tile
const LINE_COLOR = "#35c26a";
const STYLES = [["default", "Default"], ["pcb", "PCB"], ["web", "Web"], ["cable", "Cable"]];
const BGS = [["lines", "Lines"], ["squares", "Squares"], ["crosses", "Crosses"], ["honeycomb", "Honeycomb"], ["none", "None"]];
const PALETTE = ["#35c26a", "#f59e0b", "#3b82f6", "#ec4899", "#a855f7", "#06b6d4", "#ef4444", "#eab308", "#94a3b8"];
const ARROWS = [["none", "none"], ["end", "→ to B"], ["start", "← to A"], ["both", "↔ both"]];

const el = (tag, attrs = {}, parent = null) => {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null) e.setAttribute(k, v);
  if (parent) parent.appendChild(e);
  return e;
};
const rid = (p) => `${p}${Math.random().toString(36).slice(2, 10)}`;
const edgeKey = (a, b) => (a < b ? `${a}|${b}` : `${b}|${a}`);

export function renderTopology(body, win) {
  const ORIGIN = rid("o");
  let source = "backend";
  let maps = [];                        // user-created maps: [{source, label}]
  let mapData = null;
  let ovr = emptyOvr();
  let agentsById = {};                  // agents assigned by hand
  let agentList = null;                 // for the assign dialog
  let prefs = { bg: "crosses", snap: true };
  let edit = false;
  let lineMode = false, lineFrom = null;
  let sel = new Set();                  // selected node ids and "box:<id>"
  let flowFocus = null;                 // highlighted flow
  const flowHidden = new Set();
  let currentFlow = "";                 // flow for new lines
  let view = { x: 40, y: 40, z: 1 };
  let saveTimer = null;
  let bmcLinks = {};                    // Modul "bmc": agent-client-id -> {id,name,vendor}
  let staleBackend = false;             // backend still runs the old module code

  function emptyOvr() {
    return { pos: {}, meta: {}, hidden: [], custom_nodes: [], lines: [], edge_props: {},
             removed_edges: [], flows: [], boxes: [], line_style: "default" };
  }

  // ---------------------------------------------------------------- frame
  body.innerHTML = `
    <div class="tp-root" style="display:flex;flex-direction:column;height:100%;position:relative">
      <style>
        .tp-root .tp-bar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;padding:6px 8px}
        .tp-root .tp-sep{width:1px;height:22px;background:var(--border);margin:0 2px}
        .tp-root .btn.active{background:var(--accent);color:#fff;border-color:var(--accent)}
        .tp-root .tp-legend{position:absolute;left:10px;bottom:10px;z-index:20;background:rgba(15,23,42,.82);
          border:1px solid var(--border);border-radius:8px;padding:6px 8px;font-size:12px;max-width:260px;backdrop-filter:blur(4px)}
        .tp-root .tp-legend .row{display:flex;align-items:center;gap:6px;padding:2px 4px;border-radius:5px;cursor:pointer;user-select:none}
        .tp-root .tp-legend .row:hover{background:rgba(148,163,184,.15)}
        .tp-root .tp-legend .row.off{opacity:.4}
        .tp-root .tp-legend .row.focus{outline:1px solid currentColor}
        .tp-root .tp-sw{width:16px;height:4px;border-radius:2px;flex:0 0 16px}
        .tp-root .tp-dlg label{display:flex;flex-direction:column;gap:3px;font-size:12px;color:var(--subtext)}
        .tp-root .tp-dlg .row2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
        .tp-root .tp-pal{display:flex;gap:4px;flex-wrap:wrap}
        .tp-root .tp-pal button{width:20px;height:20px;border-radius:50%;border:2px solid transparent;cursor:pointer;padding:0}
        .tp-root .tp-pal button.on{border-color:#fff}
      </style>
      <div class="tp-bar explorer-toolbar">
        <select id="tp-map" title="Map / canvas – independent networks, e.g. one per subnet" style="max-width:180px"></select>
        <button class="btn btn-sm" id="tp-map-new" title="New map">➕</button>
        <button class="btn btn-sm" id="tp-map-ren" title="Rename this map" style="display:none">✏️</button>
        <button class="btn btn-sm" id="tp-map-del" title="Delete this map" style="display:none">🗑️</button>
        <span class="tp-sep"></span>
        <select id="tp-via" title="Discover from where?" style="max-width:200px"><option value="">🖥 From the backend</option></select>
        <input type="text" id="tp-target" placeholder="Target: empty = own network · 192.168.1. · 10.10.0.0/16" style="flex:1;min-width:160px">
        <button class="btn-primary" id="tp-scan">📡 Discover</button>
        <button class="btn" id="tp-stop" style="display:none">✋ Cancel</button>
        <span class="tp-sep"></span>
        <button class="btn" id="tp-edit" title="Edit the map">✏️ Edit</button>
        <span id="tp-edittools" style="display:none;gap:6px;align-items:center;flex-wrap:wrap">
          <button class="btn" id="tp-add" title="Add own node">➕ Node</button>
          <button class="btn" id="tp-box" title="Add a box (around the selection if something is selected)">▭ Box</button>
          <button class="btn" id="tp-line" title="Line tool: click two nodes (or drag from one to the other). Stays on until you switch it off.">🔗 Line</button>
          <select id="tp-curflow" title="Flow for new lines"></select>
          <select id="tp-style" title="Line style of the whole map (lines can override it)"></select>
          <button class="btn" id="tp-flows" title="Flows: named colours, e.g. Backup, Sync, VPN">🎨 Flows</button>
          <button class="btn" id="tp-reset" title="Moved discovered nodes back to the automatic layout">↺ Layout</button>
        </span>
        <span class="tp-sep"></span>
        <select id="tp-bg" title="Background (only for you)"></select>
        <button class="btn" id="tp-snap" title="Snap to grid (only for you)">▦ Snap</button>
      </div>
      <div id="tp-stale" style="display:none;margin:2px 8px 4px;padding:8px 10px;border-radius:6px;font-size:12.5px;
        background:rgba(248,113,113,.14);border:1px solid var(--danger,#f87171);color:var(--text)">
        ⚠️ <b>The backend still runs an older version of the topology module.</b> New fields (IP, OS, agent, lines,
        colours, flows, boxes, line styles) would be thrown away when saving, so <b>saving is paused</b>.
        Restart the backend (Settings → Source code → Restart backend) and reload this window.</div>
      <div style="padding:2px 10px 4px;font-size:12px;color:var(--subtext);display:flex;gap:10px;align-items:center">
        <span id="tp-text">Loading map …</span><span id="tp-saved" style="color:var(--ok,#34d399);display:none">✓ saved</span>
      </div>
      <div id="tp-progress" style="display:none;padding:0 10px 6px">
        <div style="height:6px;border-radius:3px;background:var(--panel-2);overflow:hidden">
          <div id="tp-barfill" style="height:100%;width:0%;background:var(--accent);transition:width .25s"></div></div>
      </div>
      <div id="tp-wrap" style="flex:1;position:relative;overflow:hidden;background:var(--panel-2);border-radius:6px;margin:0 6px 6px" tabindex="0">
        <svg id="tp-svg" style="width:100%;height:100%;display:block;cursor:grab;user-select:none;outline:none"></svg>
        <div class="tp-legend" id="tp-legend" style="display:none"></div>
        <div style="position:absolute;right:10px;bottom:10px;display:flex;flex-direction:column;gap:4px;z-index:20">
          <button class="btn btn-sm" id="tp-zin">＋</button><button class="btn btn-sm" id="tp-zout">－</button>
          <button class="btn btn-sm" id="tp-fit" title="Fit all">⛶</button>
        </div>
        <div id="tp-tip" style="position:absolute;display:none;z-index:30;pointer-events:none;max-width:300px;background:var(--panel);
          border:1px solid var(--border);border-radius:8px;padding:8px 10px;font-size:12px;line-height:1.45;box-shadow:0 6px 18px rgba(0,0,0,.35)"></div>
        <div id="tp-dlg" class="tp-dlg" style="position:absolute;display:none;z-index:40;left:50%;top:50%;transform:translate(-50%,-50%);
          background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px;min-width:300px;max-width:440px;
          max-height:90%;overflow:auto;box-shadow:0 10px 30px rgba(0,0,0,.45)"></div>
      </div>
    </div>`;

  const $ = (q) => body.querySelector(q);
  const svg = $("#tp-svg"), wrap = $("#tp-wrap"), tip = $("#tp-tip"), dlg = $("#tp-dlg"), legend = $("#tp-legend");
  const textEl = $("#tp-text"), savedEl = $("#tp-saved");
  const scanBtn = $("#tp-scan"), stopBtn = $("#tp-stop"), editBtn = $("#tp-edit"), lineBtn = $("#tp-line");

  $("#tp-style").innerHTML = STYLES.map(([k, l]) => `<option value="${k}">Lines: ${l}</option>`).join("");
  $("#tp-bg").innerHTML = BGS.map(([k, l]) => `<option value="${k}">▒ ${l}</option>`).join("");

  // ---------------------------------------------------------------- source / load / save
  // Two independent selectors:
  //   #tp-map  = which map is shown/saved: "" = automatic (by scanner), or a user-created "map:<id>"
  //   #tp-via  = where a discovery runs:   "" = backend, or a node's client ID
  // Without a chosen map the two are the same, exactly as before.
  const viaSel = $("#tp-via"), mapSel = $("#tp-map");
  const customMap = () => mapSel.value.startsWith("map:");
  function computeSource() { return mapSel.value || viaSel.value || "backend"; }
  function applySource() {
    source = computeSource();
    $("#tp-map-ren").style.display = customMap() ? "" : "none";
    $("#tp-map-del").style.display = customMap() ? "" : "none";
  }

  async function loadMaps(select) {
    let r = { maps: [] };
    try { r = await moduleRequest("/api/topology/sources"); } catch { /* keep going */ }
    maps = r.maps || [];
    const cur = select !== undefined ? select : mapSel.value;
    mapSel.innerHTML = `<option value="">🗺️ Auto (by scanner)</option>`
      + maps.map((m) => `<option value="${esc(m.source)}">🗺️ ${esc(m.label)}</option>`).join("");
    mapSel.value = maps.some((m) => m.source === cur) ? cur : "";
    applySource();
  }

  api.nodeChoices("topology").then((r) => {
    for (const n of r.nodes || []) {
      const o = document.createElement("option");
      o.value = n.id; o.disabled = !n.active;
      o.textContent = `🖧 Node ${n.name}${nodeHint(n)}`;
      viaSel.appendChild(o);
    }
  }).catch(() => {});

  const reselect = () => { applySource(); lineFrom = null; sel.clear(); loadMap(true); watchSource(); };
  viaSel.addEventListener("change", reselect);
  mapSel.addEventListener("change", reselect);

  $("#tp-map-new").addEventListener("click", async () => {
    const label = await uiPrompt("New map", {
      description: "An independent canvas – e.g. for another subnet. Pick a scanner and a target, then Discover into it.",
      placeholder: "e.g. Office · 192.168.10.0/24", okText: "Create" });
    if (label == null || !label.trim()) return;
    try {
      const r = await moduleRequest("/api/topology/maps", { method: "POST", body: JSON.stringify({ label: label.trim() }) });
      await loadMaps(r.source);
      reselect();
    } catch (e) { window.notify?.(esc(e.message), "error"); }
  });
  $("#tp-map-ren").addEventListener("click", async () => {
    if (!customMap()) return;
    const cur = maps.find((m) => m.source === mapSel.value);
    const label = await uiPrompt("Rename map", { value: cur?.label || "", okText: "Rename" });
    if (label == null || !label.trim()) return;
    try {
      await moduleRequest("/api/topology/maps", { method: "PATCH", body: JSON.stringify({ source: mapSel.value, label: label.trim() }) });
      await loadMaps(mapSel.value);
    } catch (e) { window.notify?.(esc(e.message), "error"); }
  });
  $("#tp-map-del").addEventListener("click", async () => {
    if (!customMap()) return;
    const cur = maps.find((m) => m.source === mapSel.value);
    if (!(await uiConfirm(`Delete map “${esc(cur?.label || "")}”?`, {
      description: "The canvas and everything on it is removed. Devices on other maps stay untouched.",
      okText: "Delete", danger: true }))) return;
    try {
      await moduleRequest(`/api/topology/maps?source=${encodeURIComponent(mapSel.value)}`, { method: "DELETE" });
      await loadMaps("");
      reselect();
    } catch (e) { window.notify?.(esc(e.message), "error"); }
  });

  async function loadMap(refit = true) {
    try {
      const want = source;
      const r = await moduleRequest(`/api/topology/map?source=${encodeURIComponent(want)}`);
      if (want !== source) return;
      mapData = r.map;
      // an old backend (Python not reloaded after an update) knows neither "agents" nor "lines"
      staleBackend = !("agents" in r) || !Array.isArray(r.overrides?.lines);
      $("#tp-stale").style.display = staleBackend ? "" : "none";
      ovr = { ...emptyOvr(), ...(r.overrides || {}) };
      agentsById = r.agents || {};
      $("#tp-style").value = ovr.line_style || "default";
      fillFlowSelect();
      if (!job?.running) setText(mapInfo());
      draw();
      if (refit) fit();
    } catch (e) { setText(e.message, true); }
  }

  async function loadPrefs() {
    try { prefs = { ...prefs, ...(await moduleRequest("/api/topology/prefs")) }; } catch { /* defaults */ }
    $("#tp-bg").value = prefs.bg;
    $("#tp-snap").classList.toggle("active", prefs.snap);
    drawBackground();
  }
  function savePrefs() {
    moduleRequest("/api/topology/prefs", { method: "PUT", body: JSON.stringify(prefs) }).catch(() => {});
  }

  function setText(msg, isErr = false) {
    textEl.innerHTML = msg;
    textEl.style.color = isErr ? "var(--danger)" : "var(--subtext)";
  }

  function saveOvr() {
    if (staleBackend) { $("#tp-stale").style.display = ""; setText("Not saved – the backend must be restarted first (see the red note).", true); return; }
    clearTimeout(saveTimer);
    saveTimer = setTimeout(async () => {
      try {
        const r = await moduleRequest(`/api/topology/map?source=${encodeURIComponent(source)}&origin=${ORIGIN}`,
          { method: "PUT", body: JSON.stringify(ovr) });
        savedEl.style.display = "";
        setTimeout(() => { savedEl.style.display = "none"; }, 1200);
        // newly assigned agents -> fetch their status
        const need = [...new Set([...ovr.custom_nodes.map((n) => n.agent_id), ...Object.values(ovr.meta).map((m) => m.agent_id)])]
          .filter((id) => id && !agentsById[id]);
        if (need.length) { pendingReload = true; }
      } catch (e) { setText(`Saving failed: ${esc(e.message)}`, true); }
      saveTimer = null;
      maybeReload();
    }, 350);
  }

  // Delete selected items for real: own nodes and boxes drop out of the
  // overrides, discovered devices are removed from the saved map on the
  // server (no soft-hide). A later scan or a manual add brings a device back.
  async function deleteItems(keys) {
    if (staleBackend) { $("#tp-stale").style.display = ""; setText("Not deleted – the backend must be restarted first (see the red note).", true); return; }
    clearTimeout(saveTimer); saveTimer = null;
    const discovered = [];
    for (const k of keys) {
      if (k.startsWith("box:")) { ovr.boxes = ovr.boxes.filter((b) => "box:" + b.id !== k); continue; }
      if (ovr.custom_nodes.some((c) => c.id === k)) {
        ovr.custom_nodes = ovr.custom_nodes.filter((c) => c.id !== k);
        ovr.lines = ovr.lines.filter((l) => l.a !== k && l.b !== k);
        delete ovr.pos[k]; delete ovr.meta[k];
      } else discovered.push(k);
    }
    try {
      await moduleRequest(`/api/topology/map?source=${encodeURIComponent(source)}&origin=${ORIGIN}`,
        { method: "PUT", body: JSON.stringify(ovr) });
    } catch (e) { setText(`Saving failed: ${esc(e.message)}`, true); }
    if (discovered.length) {
      try {
        await moduleRequest(`/api/topology/map/delete-node?source=${encodeURIComponent(source)}&origin=${ORIGIN}`,
          { method: "POST", body: JSON.stringify({ ids: discovered }) });
      } catch (e) { setText(`Delete failed: ${esc(e.message)}`, true); }
    }
    await loadMap(false);
  }

  // ---------------------------------------------------------------- discovery = backend job
  let job = null, stopWatch = null;
  function watchSource() {
    stopWatch?.();
    job = null; showJob();
    let wasRunning = null;
    stopWatch = watchJobs({ kind: "topology.discover", key: source }, (j) => {
      job = j; showJob();
      if (j && wasRunning === j.id && !j.running) loadMap(true);
      wasRunning = j?.running ? j.id : null;
    });
  }
  function showJob() {
    const running = !!job?.running;
    scanBtn.disabled = running;
    scanBtn.textContent = running ? "⏳ Discovering" : "📡 Discover";
    stopBtn.style.display = running ? "" : "none";
    stopBtn.disabled = !!job?.cancel_requested;
    $("#tp-progress").style.display = running ? "" : "none";
    if (running) {
      $("#tp-barfill").style.width = `${(job.pct || 0).toFixed(1)}%`;
      setText(`Discovery running${job.by ? ` (started by ${esc(job.by)})` : ""}: ${esc(job.detail || job.phase || "")}`
        + (job.cancel_requested ? " · cancelling …" : ""));
    } else if (job?.status === "error") setText(`Last discovery failed: ${esc(job.error || "")}`, true);
    else setText(mapInfo());
  }
  const countDevices = () => (mapData?.nodes || []).filter((n) => n.id?.startsWith("dev:")).length;
  function mapInfo() {
    const own = (ovr.custom_nodes || []).length;
    let t = mapData
      ? `${countDevices()} devices${own ? ` + ${own} own` : ""} · ${esc(mapData.label || "")} · as of ${mapData.at ? new Date(mapData.at).toLocaleString() : "?"}`
      : own ? `${own} own nodes · not discovered yet – “📡 Discover” adds the devices of the network`
      : "No map for this source yet – click “📡 Discover” or add own nodes in ✏️ Edit.";
    if (job && !job.running && job.finished) {
      const how = job.status === "cancelled" ? `cancelled${job.cancelled_by ? " by " + esc(job.cancelled_by) : ""}` : "finished";
      t += ` · last discovery ${how} (${jobDuration(job)}${job.by ? ", " + esc(job.by) : ""})`;
    }
    if (edit) t += lineMode
      ? " · <b>Line tool on</b>: click node A then node B (or drag) · click a line to edit it · Esc cancels"
      : " · Drag to move · Shift+drag selects · double-click edits · Del removes";
    return t;
  }
  scanBtn.addEventListener("click", async () => {
    scanBtn.disabled = true;
    try {
      const q = new URLSearchParams();
      const target = $("#tp-target").value.trim();
      if (target) q.set("target", target);
      if (viaSel.value) q.set("via", viaSel.value);
      if (mapSel.value) q.set("map", mapSel.value);   // scan into the chosen map
      const r = await moduleRequest(`/api/topology/discover/start?${q}`, { method: "POST" });
      if (r.attached) window.notify?.(`A discovery is already running for this source (by ${r.by}) – showing it.`, "info");
      job = r; showJob();
    } catch (e) { scanBtn.disabled = false; setText(esc(e.message), true); }
  });
  stopBtn.addEventListener("click", async () => {
    if (!job?.running) return;
    try { job = await cancelJob(job.id); showJob(); } catch (e) { setText(esc(e.message), true); }
  });

  // changes elsewhere (other user/window, finished discovery) -> reload when idle
  let pendingReload = false, dragging = false;
  const busy = () => dragging || dlg.style.display === "block" || !!saveTimer;
  function maybeReload() { if (pendingReload && !busy()) { pendingReload = false; loadMap(false); } }
  const onTopoChanged = (p) => {
    if (!p || (p.origin && p.origin === ORIGIN)) return;
    if (p.what === "maps") { loadMaps(); return; }   // a map was created/renamed/deleted elsewhere
    if (p.source !== source) return;
    if (busy()) pendingReload = true; else loadMap(p.what === "map");
  };
  dashboardSocket.on("topology:changed", onTopoChanged);

  // ---------------------------------------------------------------- graph (discovery + edits)
  function nodeView(n, custom) {
    const meta = custom ? {} : ((ovr.meta || {})[n.id] || {});
    const agentId = custom ? n.agent_id : meta.agent_id;
    const agent = (agentId && agentsById[agentId]) || (agentId ? null : n.agent) || null;
    // a node with an agent shows the agent's icon (sidebar/panel) unless the map sets its own
    const agentType = agent ? clientIconType(findClient(agent.id) || agent) : "";
    const ownType = custom ? n.type : meta.type;
    return {
      ...n, custom, _meta: meta, agentType, ownType: ownType || "",
      type: ownType || agentType || n.type || "device",
      label: (custom ? n.label : meta.label || n.label) || "",
      ip: (custom ? n.ip : meta.ip || n.ip) || "",
      os: (custom ? n.os : meta.os || n.os) || (agent?.platform || ""),
      agent, agentId: agentId || "",
    };
  }
  function graph() {
    const nodes = [];
    // No soft-hide any more: discovered devices are either on the map or
    // really deleted. Old "hidden" entries are ignored so nothing stays stuck.
    for (const n of mapData?.nodes || []) nodes.push(nodeView(n, false));
    for (const c of ovr.custom_nodes || []) nodes.push(nodeView(c, true));
    const have = new Set(nodes.map((n) => n.id));
    const removed = new Set((ovr.removed_edges || []).map(([a, b]) => edgeKey(a, b)));
    const edges = [];
    for (const [a, b] of mapData?.edges || []) {
      const k = edgeKey(a, b);
      if (!have.has(a) || !have.has(b) || removed.has(k)) continue;
      edges.push({ id: "auto:" + k, a, b, auto: true, ...(ovr.edge_props?.[k] || {}) });
    }
    for (const l of ovr.lines || []) if (have.has(l.a) && have.has(l.b)) edges.push({ ...l, auto: false });
    return { nodes, edges };
  }

  // tree layout for discovered nodes (rows top -> bottom), own positions win
  const NODE_W = 160, ROW_H = 160;
  function layout(nodes) {
    const levels = new Map();
    for (const n of nodes) {
      if (n.custom) continue;
      const lvl = n.level ?? 99;
      if (!levels.has(lvl)) levels.set(lvl, []);
      levels.get(lvl).push(n);
    }
    const sorted = [...levels.keys()].sort((x, y) => x - y);
    const maxLvl = sorted.length ? sorted[sorted.length - 1] : 0;
    const pos = {};
    let rowY = 0;
    for (const lvl of sorted) {
      const row = levels.get(lvl);
      if (lvl < maxLvl || row.length <= 8) {
        row.forEach((n, i) => { pos[n.id] = { x: (i - (row.length - 1) / 2) * NODE_W, y: rowY }; });
        rowY += ROW_H;
      } else {
        const perRow = Math.max(4, Math.ceil(Math.sqrt(row.length * 1.8)));
        row.forEach((n, i) => {
          const r = Math.floor(i / perRow), c = i % perRow, w = Math.min(perRow, row.length - r * perRow);
          pos[n.id] = { x: (c - (w - 1) / 2) * NODE_W, y: rowY + r * ROW_H };
        });
        rowY += ROW_H * Math.ceil(row.length / perRow);
      }
    }
    for (const n of nodes) {
      const p = (ovr.pos || {})[n.id];
      if (p) pos[n.id] = { x: p.x, y: p.y };
      else if (n.custom) pos[n.id] = { x: n.x || 0, y: n.y || 0 };
      else if (!pos[n.id]) pos[n.id] = { x: 0, y: rowY };
    }
    return pos;
  }

  // ---------------------------------------------------------------- background (per user)
  let bgRect = null, bgPat = null;
  function drawBackground() {
    if (!bgPat) return;
    bgPat.innerHTML = "";
    const c1 = "rgba(148,163,184,.13)", c2 = "rgba(148,163,184,.30)";
    let w = GRID, h = GRID;
    switch (prefs.bg) {
      case "lines":
        w = h = GRID * 5;
        for (let i = 0; i < 5; i++) {
          el("path", { d: `M${i * GRID} 0V${h}M0 ${i * GRID}H${w}`, stroke: i === 0 ? c2 : c1, "stroke-width": i === 0 ? 1.2 : 0.8 }, bgPat);
        }
        break;
      case "squares":
        el("rect", { x: -1.2, y: -1.2, width: 2.4, height: 2.4, fill: c2 }, bgPat);
        break;
      case "crosses":
        w = h = GRID * 5;
        for (let i = 0; i < 5; i++) for (let j = 0; j < 5; j++) {
          const big = i === 0 && j === 0, s = big ? 5 : 2.5;
          el("path", { d: `M${i * GRID - s} ${j * GRID}h${2 * s}M${i * GRID} ${j * GRID - s}v${2 * s}`,
                       stroke: big ? c2 : c1, "stroke-width": big ? 1.4 : 1 }, bgPat);
        }
        break;
      case "honeycomb": {
        const r = 14, hw = Math.sqrt(3) * r;
        w = hw; h = 3 * r;
        const hex = (cx, cy) => {
          let d = "";
          for (let k = 0; k < 6; k++) {
            const a = Math.PI / 180 * (60 * k - 30);
            d += `${k ? "L" : "M"}${(cx + r * Math.cos(a)).toFixed(2)} ${(cy + r * Math.sin(a)).toFixed(2)}`;
          }
          return d + "Z";
        };
        el("path", { d: hex(0, 0) + hex(hw, 0) + hex(hw / 2, 1.5 * r) + hex(0, 3 * r) + hex(hw, 3 * r),
                     fill: "none", stroke: c1, "stroke-width": 0.9 }, bgPat);
        break;
      }
      default:
        w = h = GRID;
    }
    bgPat.setAttribute("width", w);
    bgPat.setAttribute("height", h);
    bgRect.style.display = prefs.bg === "none" ? "none" : "";
    applyView();
  }

  // ---------------------------------------------------------------- line geometry
  // anchor on the side of the tile that faces the other node; parallel lines are spread out
  function anchors(pa, pb, spread, ia, ib) {
    const dx = pb.x - pa.x, dy = pb.y - pa.y;
    if (Math.abs(dy) >= Math.abs(dx) * 0.6) {
      const s = dy >= 0 ? 1 : -1;
      const ya = s > 0 ? pa.y + (bottomOf[ia] || HALF) : pa.y - HALF;
      const yb = s > 0 ? pb.y - HALF : pb.y + (bottomOf[ib] || HALF);
      return { x1: pa.x + spread, y1: ya, x2: pb.x + spread, y2: yb, vert: true };
    }
    const s = dx >= 0 ? 1 : -1;
    return { x1: pa.x + s * HALF, y1: pa.y + spread, x2: pb.x - s * HALF, y2: pb.y + spread, vert: false };
  }
  function roundedPoly(pts, r) {
    let d = `M${pts[0][0]} ${pts[0][1]}`;
    for (let i = 1; i < pts.length - 1; i++) {
      const [px, py] = pts[i - 1], [x, y] = pts[i], [nx, ny] = pts[i + 1];
      const l1 = Math.hypot(x - px, y - py), l2 = Math.hypot(nx - x, ny - y);
      const rr = Math.min(r, l1 / 2, l2 / 2);
      if (rr < 0.5) { d += `L${x} ${y}`; continue; }
      const ax = x - ((x - px) / l1) * rr, ay = y - ((y - py) / l1) * rr;
      const bx = x + ((nx - x) / l2) * rr, by = y + ((ny - y) / l2) * rr;
      d += `L${ax} ${ay}Q${x} ${y} ${bx} ${by}`;
    }
    const last = pts[pts.length - 1];
    return d + `L${last[0]} ${last[1]}`;
  }
  function linePath(style, a) {
    const { x1, y1, x2, y2, vert } = a;
    if (style === "web") return `M${x1} ${y1}L${x2} ${y2}`;
    if (style === "cable") {
      const k = Math.max(40, (vert ? Math.abs(y2 - y1) : Math.abs(x2 - x1)) * 0.5);
      return vert
        ? `M${x1} ${y1}C${x1} ${y1 + Math.sign(y2 - y1 || 1) * k} ${x2} ${y2 - Math.sign(y2 - y1 || 1) * k} ${x2} ${y2}`
        : `M${x1} ${y1}C${x1 + Math.sign(x2 - x1 || 1) * k} ${y1} ${x2 - Math.sign(x2 - x1 || 1) * k} ${y2} ${x2} ${y2}`;
    }
    // default + pcb: right angles (default rounded, pcb sharp)
    const pts = vert
      ? [[x1, y1], [x1, (y1 + y2) / 2], [x2, (y1 + y2) / 2], [x2, y2]]
      : [[x1, y1], [(x1 + x2) / 2, y1], [(x1 + x2) / 2, y2], [x2, y2]];
    return roundedPoly(pts, style === "pcb" ? 0 : 10);
  }

  // ---------------------------------------------------------------- drawing
  let positions = {}, edgeRecs = [], nodeEls = new Map(), boxEls = new Map(), vp = null, bottomOf = {};
  let flowById = {};
  const lineColor = (e) => e.color || flowById[e.flow]?.color || LINE_COLOR;
  const lineStyle = (e) => e.style || ovr.line_style || "default";

  async function loadBmcLinks() {
    try { bmcLinks = (await moduleRequest("/api/bmc/links")).links || {}; }
    catch { bmcLinks = {}; }
  }
  function openBmc(agentId) {
    const cl = findClient(agentId) || { id: agentId };
    openWindow({ key: `bmc-${agentId}`, appId: "bmc", title: `iLO / iDRAC — ${cl.hostname || ""}`,
                 props: { clientId: agentId }, w: 1120, h: 760, clientColor: cl.color });
  }

  function draw() {
    const { nodes, edges } = graph();
    positions = layout(nodes);
    flowById = Object.fromEntries((ovr.flows || []).map((f) => [f.id, f]));
    svg.innerHTML = "";
    const defs = el("defs", {}, svg);
    bgPat = el("pattern", { id: "tp-bgpat", patternUnits: "userSpaceOnUse", width: GRID, height: GRID }, defs);
    el("marker", { id: "tp-arrow", viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7,
                   orient: "auto-start-reverse", markerUnits: "userSpaceOnUse" }, defs)
      .innerHTML = `<path d="M0 0L10 5L0 10z" fill="context-stroke"/>`;
    bgRect = el("rect", { x: 0, y: 0, width: "100%", height: "100%", fill: "url(#tp-bgpat)" }, svg);
    vp = el("g", { id: "tp-vp" }, svg);
    const gBoxes = el("g", {}, vp), gEdges = el("g", {}, vp), gNodes = el("g", {}, vp), gLabels = el("g", {}, vp);
    bottomOf = {};
    for (const n of nodes) {
      const d = nodeDisplay(n);
      bottomOf[n.id] = 56 + (n.ip && n.ip !== d.name ? 13 : 0) + (n.os ? 13 : 0) + (d.tags.length ? 12 : 0);
    }
    drawBackground();

    boxEls = new Map();
    for (const b of ovr.boxes || []) drawBox(gBoxes, b);

    // lines between the same two nodes are spread apart
    const pairCount = {}, pairIdx = {};
    for (const e of edges) pairCount[edgeKey(e.a, e.b)] = (pairCount[edgeKey(e.a, e.b)] || 0) + 1;
    edgeRecs = [];
    for (const e of edges) {
      if (e.flow && flowHidden.has(e.flow)) continue;
      const k = edgeKey(e.a, e.b);
      const i = (pairIdx[k] = (pairIdx[k] ?? -1) + 1);
      const spread = (i - (pairCount[k] - 1) / 2) * 10;
      edgeRecs.push(drawEdge(gEdges, gLabels, e, spread));
    }
    nodeEls = new Map();
    for (const n of nodes) drawNode(gNodes, n, positions[n.id]);
    drawLegend();
    applyView();
  }

  function drawEdge(gEdges, gLabels, e, spread) {
    const color = lineColor(e);
    const dim = flowFocus && e.flow !== flowFocus;
    const g = el("g", { opacity: dim ? 0.15 : 1 }, gEdges);
    const hit = el("path", { fill: "none", stroke: "transparent", "stroke-width": 14, "pointer-events": "stroke" }, g);
    const path = el("path", { fill: "none", stroke: color, "stroke-width": e.flow || e.color ? 2.4 : 1.8,
                              "stroke-linejoin": "round", "stroke-linecap": "round", "pointer-events": "none" }, g);
    if (e.arrow === "end" || e.arrow === "both") path.setAttribute("marker-end", "url(#tp-arrow)");
    if (e.arrow === "start" || e.arrow === "both") path.setAttribute("marker-start", "url(#tp-arrow)");
    const dotA = el("circle", { r: 3.6, fill: color, "pointer-events": "none" }, g), dotB = el("circle", { r: 3.6, fill: color, "pointer-events": "none" }, g);
    const lg = el("g", { opacity: dim ? 0.15 : 1, "pointer-events": "none" }, gLabels);
    const rec = { e, g, hit, path, dotA, dotB, lg, spread };
    placeEdge(rec);
    if (edit || lineMode) {
      hit.style.cursor = "pointer";
      hit.addEventListener("pointerdown", (ev) => ev.stopPropagation());
      hit.addEventListener("click", (ev) => { ev.stopPropagation(); openLineDialog(e); });
      hit.addEventListener("mouseenter", () => path.setAttribute("stroke-width", 3.6));
      hit.addEventListener("mouseleave", () => path.setAttribute("stroke-width", e.flow || e.color ? 2.4 : 1.8));
    }
    hit.addEventListener("mouseenter", (ev) => showLineTip(e, ev));
    hit.addEventListener("mousemove", moveTip);
    hit.addEventListener("mouseleave", hideTip);
    return rec;
  }

  function pill(parent, text, x, y, color, size = 10) {
    const g = el("g", {}, parent);
    const t = el("text", { x, y: y + size * 0.35, "text-anchor": "middle", "font-size": size, "font-weight": 600,
                           fill: "#e2e8f0", "font-family": "ui-monospace,monospace" }, g);
    t.textContent = text;
    const w = text.length * size * 0.62 + 10;
    g.insertBefore(el("rect", { x: x - w / 2, y: y - size * 0.75, width: w, height: size * 1.5, rx: 4,
                                fill: "rgba(15,23,42,.92)", stroke: color, "stroke-width": 1 }), t);
  }

  function placeEdge(rec) {
    const { e } = rec;
    const pa = positions[e.a], pb = positions[e.b];
    if (!pa || !pb) return;
    const a = anchors(pa, pb, rec.spread, e.a, e.b);
    const d = linePath(lineStyle(e), a);
    rec.path.setAttribute("d", d);
    rec.hit.setAttribute("d", d);
    rec.dotA.setAttribute("cx", a.x1); rec.dotA.setAttribute("cy", a.y1);
    rec.dotB.setAttribute("cx", a.x2); rec.dotB.setAttribute("cy", a.y2);
    rec.lg.innerHTML = "";
    const color = lineColor(e);
    if (!(e.label || e.la || e.lb)) return;
    let len = 0;
    try { len = rec.path.getTotalLength(); } catch { return; }
    if (!len) return;
    const at = (l) => rec.path.getPointAtLength(Math.max(0, Math.min(len, l)));
    if (e.label) {
      const p = at(len / 2);
      // short line with end labels: put the name beside the line instead of on top of them
      const crowded = (e.la || e.lb) && len < 130;
      const w = e.label.length * 11 * 0.62 + 10;
      if (crowded) {
        const q = at(len / 2 + 1), vert = Math.abs(q.y - p.y) > Math.abs(q.x - p.x);
        pill(rec.lg, e.label, vert ? p.x + w / 2 + 10 : p.x, vert ? p.y : p.y - 18, color, 11);
      } else pill(rec.lg, e.label, p.x, p.y, color, 11);
    }
    if (e.la) { const p = at(Math.min(24, len / 3)); pill(rec.lg, e.la, p.x, p.y, color); }
    if (e.lb) { const p = at(len - Math.min(24, len / 3)); pill(rec.lg, e.lb, p.x, p.y, color); }
  }

  function drawBox(parent, b) {
    const selected = sel.has("box:" + b.id);
    const g = el("g", { transform: `translate(${b.x},${b.y})` }, parent);
    const rect = el("rect", { width: b.w, height: b.h, rx: 10, fill: b.color + "14", stroke: b.color,
                              "stroke-width": selected ? 2.4 : 1.4, "stroke-dasharray": selected ? "" : "7 5" }, g);
    const head = el("g", {}, g);
    const tw = Math.max(70, (b.label || "Box").length * 7.4 + 22);
    el("rect", { x: 0, y: -22, width: tw, height: 22, rx: 6, fill: b.color + "cc" }, head);
    const t = el("text", { x: 10, y: -7, "font-size": 12, "font-weight": 600, fill: "#fff" }, head);
    t.textContent = b.label || "Box";
    const handle = el("rect", { x: b.w - 12, y: b.h - 12, width: 12, height: 12, fill: b.color, rx: 2,
                                opacity: edit ? 0.9 : 0 }, g);
    boxEls.set(b.id, { g, rect, handle });
    if (!edit) { rect.setAttribute("pointer-events", "none"); return; }
    rect.setAttribute("pointer-events", "stroke");
    rect.style.cursor = "move";
    head.style.cursor = "move";
    handle.style.cursor = "nwse-resize";
    const startMove = (ev) => onBoxDown(ev, b);
    rect.addEventListener("pointerdown", startMove);
    head.addEventListener("pointerdown", startMove);
    head.addEventListener("dblclick", (ev) => { ev.stopPropagation(); openBoxDialog(b); });
    rect.addEventListener("dblclick", (ev) => { ev.stopPropagation(); openBoxDialog(b); });
    handle.addEventListener("pointerdown", (ev) => onBoxResize(ev, b));
  }

  function drawNode(parent, n, p) {
    if (!p) return;
    const g = el("g", { transform: `translate(${p.x},${p.y})` }, parent);
    g.style.cursor = edit || lineMode ? "move" : (n.agent ? "pointer" : "default");
    g.dataset.nid = n.id;
    g.dataset.bs = n.agent ? (n.agent.color || "var(--accent)") : "var(--border)";
    g.dataset.bw = n.agent ? 2.2 : 1.2;
    el("rect", { x: -HALF, y: -HALF, width: 2 * HALF, height: 2 * HALF, rx: 12, fill: "var(--panel)" }, g);
    const icon = el("g", { transform: "translate(-24,-26)",
                           color: n.type === "www" || n.type === "isp" ? "var(--accent)" : "var(--text, #d8dee9)" }, g);
    icon.innerHTML = iconBody(n.type);
    if (n.agent) el("circle", { cx: 27, cy: -27, r: 6, fill: n.agent.online ? "var(--ok, #34d399)" : "var(--danger, #f87171)",
                                stroke: "var(--panel)", "stroke-width": 2 }, g);
    if (n.self) el("text", { x: -28, y: -22, "font-size": 13 }, g).textContent = "📍";
    if (n.agent && bmcLinks[n.agent.id]) {
      const bg = el("g", { transform: "translate(-27,-27)" }, g);
      bg.style.cursor = "pointer";
      el("circle", { r: 8.5, fill: "var(--panel)", stroke: "var(--accent)", "stroke-width": 1.5 }, bg);
      el("text", { x: 0, y: 3.5, "text-anchor": "middle", "font-size": 10 }, bg).textContent = "🛉";
      const lk = bmcLinks[n.agent.id];
      el("title", {}, bg).textContent = "Externes Management: " + ((lk.vendor || "").includes("HPE") ? "iLO" : (lk.vendor || "").includes("Dell") ? "iDRAC" : "iLO/iDRAC");
      bg.addEventListener("pointerdown", (ev) => ev.stopPropagation());
      bg.addEventListener("click", (ev) => { ev.stopPropagation(); openBmc(n.agent.id); });
    }
    const disp = nodeDisplay(n);
    const name = disp.os + disp.name;
    const txt = (s, y, size, fill, weight = "400", mono = false) => {
      const t = el("text", { x: 0, y, "text-anchor": "middle", "font-size": size, "font-weight": weight, fill,
                             "font-family": mono ? "ui-monospace,monospace" : null }, g);
      t.textContent = s;
    };
    txt(name.length > 22 ? name.slice(0, 21) + "…" : name, 50, 12, n.agent ? (n.agent.color || "var(--text)") : "var(--text, #d8dee9)", "600");
    let y = 65;
    if (n.ip && n.ip !== name) { txt(n.ip, y, 10.5, "var(--subtext, #94a3b8)", "400", true); y += 13; }
    if (n.os) { txt(n.os.length > 24 ? n.os.slice(0, 23) + "…" : n.os, y, 10, "var(--subtext, #94a3b8)"); y += 13; }
    if (disp.tags.length) {
      const tl = disp.tags.join(" · ");
      txt("🏷 " + (tl.length > 26 ? tl.slice(0, 25) + "…" : tl), y, 9.5, "var(--subtext, #94a3b8)");
      y += 12;
    }
    drawNote(g, n, y);
    g.addEventListener("contextmenu", (ev) => { ev.preventDefault(); openNodeDialog(n, !edit); });
    g.addEventListener("mouseenter", (ev) => { if (!dragging) showTip(n, ev); });
    g.addEventListener("mousemove", moveTip);
    g.addEventListener("mouseleave", hideTip);
    g.addEventListener("pointerdown", (ev) => onNodeDown(ev, n));
    g.addEventListener("dblclick", (ev) => { ev.stopPropagation(); if (edit && !lineMode) openNodeDialog(n); });
    nodeEls.set(n.id, g);
    paintNode(n.id);
  }


  // Kurzname/OS-Icon/Tags des verknuepften RMM-Clients (Core-Spalten short_name/tags).
  function nodeDisplay(n) {
    const cl = n.agent ? findClient(n.agent.id) : null;
    const name = n.label || (cl && cl.short_name) || n.hostname || n.ip || "?";
    const p = String((cl && cl.platform) || n.os || "").toLowerCase();
    const os = p.includes("win") ? "🪟 " : (p.includes("mac") || p.includes("darwin")) ? "🍎 "
      : (p.includes("linux") || p.includes("debian") || p.includes("ubuntu") || p.includes("proxmox")) ? "🐧 " : "";
    const tags = String((cl && cl.tags) || "").split(",").map((t) => t.trim()).filter(Boolean);
    return { name, os, tags };
  }

  // selection / line start: only recolour (no rebuild - keeps double-click working)
  function paintNode(id) {
    const g = nodeEls.get(id);
    if (!g) return;
    const isSel = sel.has(id), isFrom = lineFrom === id;
    const r = g.firstChild;
    r.setAttribute("stroke", isFrom ? "#f59e0b" : isSel ? "var(--accent)" : g.dataset.bs);
    r.setAttribute("stroke-width", isFrom || isSel ? 3 : g.dataset.bw);
  }
  function paintSel() {
    for (const id of nodeEls.keys()) paintNode(id);
    for (const [id, be] of boxEls) {
      const on = sel.has("box:" + id);
      be.rect.setAttribute("stroke-width", on ? 2.4 : 1.4);
      be.rect.setAttribute("stroke-dasharray", on ? "" : "7 5");
    }
  }

  const noteOf = (n) => ((ovr.meta || {})[n.id]?.note ?? n.info ?? "").trim();
  const noteMode = (n) => ((ovr.meta || {})[n.id]?.note_mode === "always" ? "always" : "hover");
  function drawNote(g, n, top) {
    const note = noteOf(n);
    if (!note) return;
    if (noteMode(n) === "hover") { el("text", { x: 22, y: 30, "font-size": 13 }, g).textContent = "📝"; return; }
    const lines = [];
    for (const para of note.split("\n")) {
      let cur = "";
      for (const w of para.split(/\s+/)) {
        if ((cur + " " + w).trim().length > 26 && cur) { lines.push(cur); cur = w; } else cur = (cur + " " + w).trim();
      }
      lines.push(cur);
    }
    if (lines.length > 6) { lines.length = 6; lines[5] += " …"; }
    const w = 170, lh = 14, h = lines.length * lh + 12;
    el("rect", { x: -w / 2, y: top - 4, width: w, height: h, rx: 6, fill: "rgba(250,204,21,.14)", stroke: "rgba(250,204,21,.7)" }, g);
    lines.forEach((ln, i) => {
      el("text", { x: -w / 2 + 8, y: top + 12 + i * lh, "font-size": 11, fill: "var(--text, #d8dee9)" }, g).textContent = ln;
    });
  }

  // ---------------------------------------------------------------- legend (flows)
  function drawLegend() {
    const flows = ovr.flows || [];
    if (!flows.length) { legend.style.display = "none"; return; }
    const { edges } = graph();
    legend.style.display = "";
    legend.innerHTML = `<div style="font-weight:600;margin-bottom:3px;color:var(--subtext)">Flows</div>` + flows.map((f) => {
      const n = edges.filter((e) => e.flow === f.id).length;
      return `<div class="row ${flowHidden.has(f.id) ? "off" : ""} ${flowFocus === f.id ? "focus" : ""}" data-f="${esc(f.id)}" style="color:${f.color}"
        title="Click: highlight · right-click: show/hide"><span class="tp-sw" style="background:${f.color}"></span>
        <span style="color:var(--text);flex:1">${esc(f.name)}</span><span style="color:var(--subtext)">${n}</span></div>`;
    }).join("");
    legend.querySelectorAll("[data-f]").forEach((r) => {
      r.onclick = () => { flowFocus = flowFocus === r.dataset.f ? null : r.dataset.f; draw(); };
      r.oncontextmenu = (ev) => {
        ev.preventDefault();
        const id = r.dataset.f;
        flowHidden.has(id) ? flowHidden.delete(id) : flowHidden.add(id);
        draw();
      };
    });
  }
  function fillFlowSelect() {
    const s = $("#tp-curflow");
    s.innerHTML = `<option value="">New lines: no flow</option>` +
      (ovr.flows || []).map((f) => `<option value="${esc(f.id)}">New lines: ${esc(f.name)}</option>`).join("");
    if ((ovr.flows || []).some((f) => f.id === currentFlow)) s.value = currentFlow; else currentFlow = "";
  }

  // ---------------------------------------------------------------- tooltip
  function showTip(n, ev) {
    const rows = [`<div style="font-weight:600;margin-bottom:3px">${esc(n.label || n.hostname || n.ip || "Node")}</div>`];
    const add = (k, v) => { if (v) rows.push(`<div><span style="color:var(--subtext)">${k}:</span> ${esc(String(v))}</div>`); };
    add("Type", TYPE_LABELS[n.type] || n.type);
    add("IP", n.ip); add("OS", n.os); add("MAC", n.mac); add("Hostname", n.hostname);
    add("Tags", nodeDisplay(n).tags.join(", "));
    if (n.ports?.length) add("Open ports", n.ports.join(", "));
    if (n.gateway) rows.push(`<div>🌐 Default gateway</div>`);
    if (n.self) rows.push(`<div>📍 Discovered from here</div>`);
    if (n.custom) rows.push(`<div style="color:var(--subtext)">own node</div>`);
    const note = noteOf(n);
    if (note) rows.push(`<div style="margin-top:4px;padding:5px 7px;border-radius:6px;white-space:pre-wrap;background:rgba(250,204,21,.12);border:1px solid rgba(250,204,21,.5)">📝 ${esc(note)}</div>`);
    if (n.agent) rows.push(`<div style="margin-top:4px;padding-top:4px;border-top:1px solid var(--border)">
      <span style="color:${n.agent.online ? "var(--ok,#34d399)" : "var(--danger)"}">●</span> Agent: <b>${esc(n.agent.hostname || n.agent.id)}</b>
      (${n.agent.online ? "online" : "offline"})${n.agentId ? " · assigned" : ""}<br><span style="color:var(--subtext)">${edit ? "" : "Click opens the client"}</span></div>`);
    tip.innerHTML = rows.join("");
    tip.style.display = "block";
    moveTip(ev);
  }
  function showLineTip(e, ev) {
    if (dragging) return;
    const nameOf = (id) => { const n = graph().nodes.find((x) => x.id === id); return n ? (n.label || n.hostname || n.ip || id) : id; };
    const f = flowById[e.flow];
    tip.innerHTML = `<div style="font-weight:600">${esc(e.label || "Line")}</div>
      <div>${esc(nameOf(e.a))}${e.la ? ` <span style="color:var(--subtext)">[${esc(e.la)}]</span>` : ""}
        ${e.arrow === "both" ? "↔" : e.arrow === "start" ? "←" : e.arrow === "end" ? "→" : "—"}
        ${esc(nameOf(e.b))}${e.lb ? ` <span style="color:var(--subtext)">[${esc(e.lb)}]</span>` : ""}</div>
      ${f ? `<div><span class="tp-sw" style="display:inline-block;background:${f.color}"></span> ${esc(f.name)}</div>` : ""}
      <div style="color:var(--subtext)">${STYLES.find(([k]) => k === lineStyle(e))?.[1] || ""}${e.auto ? " · discovered" : ""}${edit || lineMode ? " · click to edit" : ""}</div>`;
    tip.style.display = "block";
    moveTip(ev);
  }
  function moveTip(ev) {
    const r = wrap.getBoundingClientRect();
    let x = ev.clientX - r.left + 14, y = ev.clientY - r.top + 12;
    if (x + tip.offsetWidth > r.width - 8) x = ev.clientX - r.left - tip.offsetWidth - 10;
    if (y + tip.offsetHeight > r.height - 8) y = ev.clientY - r.top - tip.offsetHeight - 8;
    tip.style.left = `${Math.max(4, x)}px`; tip.style.top = `${Math.max(4, y)}px`;
  }
  function hideTip() { tip.style.display = "none"; }

  // ---------------------------------------------------------------- coordinates / snapping
  const toWorld = (cx, cy) => { const r = svg.getBoundingClientRect(); return { x: (cx - r.left - view.x) / view.z, y: (cy - r.top - view.y) / view.z }; };
  const snap = (v) => (prefs.snap ? Math.round(v / GRID) * GRID : v);

  function setNodePos(id, x, y) {
    positions[id] = { x, y };
    nodeEls.get(id)?.setAttribute("transform", `translate(${x},${y})`);
  }
  function commitNodePos(id) {
    const p = positions[id];
    const c = (ovr.custom_nodes || []).find((x) => x.id === id);
    if (c) { c.x = p.x; c.y = p.y; } else ovr.pos[id] = { x: p.x, y: p.y };
  }
  function refreshEdgesOf(ids) {
    for (const rec of edgeRecs) if (ids.has(rec.e.a) || ids.has(rec.e.b)) placeEdge(rec);
  }

  // ---------------------------------------------------------------- dragging nodes / selection
  function onNodeDown(ev, n) {
    ev.stopPropagation();
    if (ev.button !== 0) return;
    wrap.focus({ preventScroll: true });
    if (lineMode) { lineDragStart(ev, n); return; }
    const start = { x: ev.clientX, y: ev.clientY };
    let moved = false;
    if (edit) {
      if (ev.shiftKey) { sel.has(n.id) ? sel.delete(n.id) : sel.add(n.id); paintSel(); return; }
      if (!sel.has(n.id)) { sel = new Set([n.id]); paintSel(); }
    }
    // everything selected moves together (nodes + boxes + the nodes inside moved boxes)
    const moveIds = new Set(edit ? [...sel].filter((x) => !x.startsWith("box:")) : []);
    const moveBoxes = edit ? (ovr.boxes || []).filter((b) => sel.has("box:" + b.id)) : [];
    for (const b of moveBoxes) for (const id of nodesInBox(b)) moveIds.add(id);
    const p0 = Object.fromEntries([...moveIds].map((id) => [id, { ...positions[id] }]));
    const b0 = moveBoxes.map((b) => ({ b, x: b.x, y: b.y }));
    const anchor0 = { ...positions[n.id] };
    dragging = true;
    const onMove = (e) => {
      if (!moved && Math.hypot(e.clientX - start.x, e.clientY - start.y) < 4) return;
      if (!edit) return;
      moved = true; hideTip();
      let dx = (e.clientX - start.x) / view.z, dy = (e.clientY - start.y) / view.z;
      if (prefs.snap) { dx = snap(anchor0.x + dx) - anchor0.x; dy = snap(anchor0.y + dy) - anchor0.y; }
      for (const id of moveIds) setNodePos(id, p0[id].x + dx, p0[id].y + dy);
      for (const { b, x, y } of b0) { b.x = x + dx; b.y = y + dy; boxEls.get(b.id)?.g.setAttribute("transform", `translate(${b.x},${b.y})`); }
      refreshEdgesOf(moveIds);
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      dragging = false;
      if (moved) { for (const id of moveIds) commitNodePos(id); saveOvr(); draw(); return; }
      if (!edit && n.agent) openClientWindow(n.agent);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }

  function nodesInBox(b) {
    return Object.entries(positions).filter(([, p]) => p.x >= b.x && p.x <= b.x + b.w && p.y >= b.y && p.y <= b.y + b.h)
      .map(([id]) => id);
  }

  function onBoxDown(ev, b) {
    ev.stopPropagation();
    if (ev.button !== 0 || lineMode) return;
    if (ev.shiftKey) { const k = "box:" + b.id; sel.has(k) ? sel.delete(k) : sel.add(k); paintSel(); return; }
    // box + everything inside moves (and other selected things)
    if (!sel.has("box:" + b.id)) { sel = new Set(["box:" + b.id]); paintSel(); }
    const ids = new Set([...sel].filter((x) => !x.startsWith("box:")));
    const boxes = (ovr.boxes || []).filter((x) => sel.has("box:" + x.id));
    for (const bx of boxes) for (const id of nodesInBox(bx)) ids.add(id);
    const p0 = Object.fromEntries([...ids].map((id) => [id, { ...positions[id] }]));
    const b0 = boxes.map((x) => ({ b: x, x: x.x, y: x.y }));
    const start = { x: ev.clientX, y: ev.clientY };
    let moved = false;
    dragging = true;
    const onMove = (e) => {
      if (!moved && Math.hypot(e.clientX - start.x, e.clientY - start.y) < 4) return;
      moved = true;
      let dx = (e.clientX - start.x) / view.z, dy = (e.clientY - start.y) / view.z;
      const q0 = b0.find((q) => q.b === b);
      if (prefs.snap && q0) { dx = snap(q0.x + dx) - q0.x; dy = snap(q0.y + dy) - q0.y; }
      for (const id of ids) setNodePos(id, p0[id].x + dx, p0[id].y + dy);
      for (const q of b0) { q.b.x = q.x + dx; q.b.y = q.y + dy; boxEls.get(q.b.id)?.g.setAttribute("transform", `translate(${q.b.x},${q.b.y})`); }
      refreshEdgesOf(ids);
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      dragging = false;
      if (moved) { for (const id of ids) commitNodePos(id); saveOvr(); draw(); }
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }

  function onBoxResize(ev, b) {
    ev.stopPropagation();
    const start = { x: ev.clientX, y: ev.clientY, w: b.w, h: b.h };
    dragging = true;
    const onMove = (e) => {
      b.w = Math.max(80, snap(start.w + (e.clientX - start.x) / view.z));
      b.h = Math.max(60, snap(start.h + (e.clientY - start.y) / view.z));
      const be = boxEls.get(b.id);
      be.rect.setAttribute("width", b.w); be.rect.setAttribute("height", b.h);
      be.handle.setAttribute("x", b.w - 12); be.handle.setAttribute("y", b.h - 12);
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      dragging = false; saveOvr(); draw();
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }

  // ---------------------------------------------------------------- line tool (toggle)
  let preview = null;
  function lineDragStart(ev, n) {
    const start = { x: ev.clientX, y: ev.clientY };
    let moved = false;
    dragging = true; hideTip();
    const onMove = (e) => {
      if (!moved && Math.hypot(e.clientX - start.x, e.clientY - start.y) < 6) return;
      moved = true;
      const w = toWorld(e.clientX, e.clientY), p = positions[n.id];
      if (!preview) preview = el("path", { fill: "none", stroke: flowById[currentFlow]?.color || "#f59e0b", "stroke-width": 2,
                                           "stroke-dasharray": "6 4", "pointer-events": "none" }, vp);
      preview.setAttribute("d", `M${p.x} ${p.y}L${w.x} ${w.y}`);
    };
    const onUp = (e) => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      dragging = false;
      preview?.remove(); preview = null;
      if (moved) {                                  // dragged onto another node?
        const target = document.elementFromPoint(e.clientX, e.clientY)?.closest?.("[data-nid]");
        if (target && target.dataset.nid !== n.id) addLine(n.id, target.dataset.nid);
        lineFrom = null; draw();
        return;
      }
      if (!lineFrom) { lineFrom = n.id; paintSel(); return; }    // first click
      if (lineFrom !== n.id) { addLine(lineFrom, n.id); lineFrom = null; draw(); return; }   // second click
      lineFrom = null; paintSel();
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }
  function addLine(a, b) {
    const l = { id: rid("l"), a, b };
    if (currentFlow) l.flow = currentFlow;
    ovr.lines.push(l);
    saveOvr();
  }
  function setLineMode(on) {
    lineMode = on; lineFrom = null;
    lineBtn.classList.toggle("active", on);
    setText(mapInfo()); draw();
  }
  lineBtn.addEventListener("click", () => setLineMode(!lineMode));
  $("#tp-curflow").addEventListener("change", (e) => { currentFlow = e.target.value; });

  // ---------------------------------------------------------------- canvas: pan / rubber band
  svg.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0 || ev.target.closest("[data-nid]")) return;
    wrap.focus({ preventScroll: true });
    if (lineFrom) { lineFrom = null; paintSel(); }
    if (ev.shiftKey && edit) { rubberBand(ev); return; }
    if (edit && sel.size) { sel.clear(); paintSel(); }
    const start = { x: ev.clientX, y: ev.clientY, vx: view.x, vy: view.y };
    svg.style.cursor = "grabbing";
    const onMove = (e) => { view.x = start.vx + (e.clientX - start.x); view.y = start.vy + (e.clientY - start.y); applyView(); };
    const onUp = () => {
      svg.style.cursor = "grab";
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  });

  function rubberBand(ev) {
    const a = toWorld(ev.clientX, ev.clientY);
    const r = el("rect", { fill: "rgba(59,130,246,.12)", stroke: "#3b82f6", "stroke-dasharray": "4 3", "stroke-width": 1 / view.z }, vp);
    const keep = new Set(sel);
    dragging = true;
    const rect = (b) => ({ x: Math.min(a.x, b.x), y: Math.min(a.y, b.y), w: Math.abs(b.x - a.x), h: Math.abs(b.y - a.y) });
    const onMove = (e) => {
      const q = rect(toWorld(e.clientX, e.clientY));
      r.setAttribute("x", q.x); r.setAttribute("y", q.y); r.setAttribute("width", q.w); r.setAttribute("height", q.h);
    };
    const onUp = (e) => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      dragging = false;
      const q = rect(toWorld(e.clientX, e.clientY));
      r.remove();
      sel = keep;
      for (const [id, p] of Object.entries(positions))
        if (p.x >= q.x && p.x <= q.x + q.w && p.y >= q.y && p.y <= q.y + q.h) sel.add(id);
      for (const b of ovr.boxes || [])
        if (b.x >= q.x && b.y >= q.y && b.x + b.w <= q.x + q.w && b.y + b.h <= q.y + q.h) sel.add("box:" + b.id);
      setText(sel.size ? `${sel.size} selected · drag to move · ▭ Box frames them · Del removes` : mapInfo());
      paintSel();
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }

  // keyboard
  wrap.addEventListener("keydown", async (ev) => {
    if (dlg.style.display === "block" || ev.target.closest?.("input,textarea,select")) return;
    if (ev.key === "Escape") { lineFrom = null; sel.clear(); paintSel(); }
    if (!edit) return;
    if (ev.key === "l" || ev.key === "L") setLineMode(!lineMode);
    if ((ev.key === "Delete" || ev.key === "Backspace") && sel.size) {
      ev.preventDefault();
      if (!(await uiConfirm(`Delete ${sel.size} selected item(s)?`, { description: "Own nodes and boxes are removed; discovered devices are really deleted from the map – a scan or a manual add brings them back.", okText: "Delete", danger: true }))) return;
      const keys = [...sel]; sel.clear();
      await deleteItems(keys);
    }
  });

  function openClientWindow(agent) {
    const c = findClient(agent.id) || agent;
    openWindow({ singleton: true, key: `panelpart-${agent.id}-client-x`, appId: "panelpart",
                 title: c.hostname || agent.hostname || agent.id, props: { clientId: agent.id, part: "client" },
                 clientColor: c.color, w: 900, h: 620 });
  }

  // ---------------------------------------------------------------- dialogs
  function openDlg(html) { hideTip(); dlg.innerHTML = html; dlg.style.display = "block"; }
  function closeDialog() { dlg.style.display = "none"; setTimeout(maybeReload, 0); wrap.focus({ preventScroll: true }); }
  const palette = (cur, name) => `<div class="tp-pal" data-pal="${name}">${PALETTE.map((c) =>
    `<button type="button" data-c="${c}" class="${c === cur ? "on" : ""}" style="background:${c}"></button>`).join("")}
    <input type="color" data-cc="${name}" value="${cur || "#35c26a"}" style="width:28px;height:22px;padding:0;border:0;background:none" title="Own colour"></div>`;
  function wirePalette(name, onPick) {
    const box = dlg.querySelector(`[data-pal="${name}"]`);
    box.querySelectorAll("[data-c]").forEach((b) => b.onclick = () => {
      box.querySelectorAll("[data-c]").forEach((x) => x.classList.remove("on"));
      b.classList.add("on"); onPick(b.dataset.c);
      box.querySelector("[data-cc]").value = b.dataset.c;
    });
    box.querySelector("[data-cc]").oninput = (e) => {
      box.querySelectorAll("[data-c]").forEach((x) => x.classList.remove("on")); onPick(e.target.value);
    };
  }

  async function openNodeDialog(n, noteOnly = false) {
    if (!edit && !noteOnly) return;
    if (!agentList) { try { agentList = (await moduleRequest("/api/topology/agents")).agents; } catch { agentList = []; } }
    const typeOpts = (n.agent ? `<option value="">Same as agent (${TYPE_LABELS[n.agentType] || n.agentType})</option>` : "")
      + EDIT_TYPES.map((t) => `<option value="${t}" ${t === (n.agent ? n.ownType : n.type) ? "selected" : ""}>${TYPE_LABELS[t] || t}</option>`).join("");
    const agOpts = `<option value="">${n.agent && !n.agentId ? `automatic: ${esc(n.agent.hostname || "")}` : "— none —"}</option>` +
      agentList.map((a) => `<option value="${esc(a.id)}" ${a.id === n.agentId ? "selected" : ""}>${a.online ? "●" : "○"} ${esc(a.hostname)}${a.ip ? " · " + esc(a.ip) : ""}</option>`).join("");
    const mode = noteMode(n);
    openDlg(`
      <div style="font-weight:600;margin-bottom:10px">${noteOnly ? "Note: " + esc(n.label || n.ip || "node") : n.custom ? "Own node" : "Node"}</div>
      <div style="display:flex;flex-direction:column;gap:8px">
        ${noteOnly ? "" : `
        <label>Name<input type="text" id="d-name" value="${esc(n.label || n.hostname || "")}"></label>
        <label>Type / icon<select id="d-type">${typeOpts}</select></label>
        <div class="row2"><label>IP address<input type="text" id="d-ip" value="${esc(n.ip || "")}" placeholder="e.g. 192.168.1.10"></label>
          <label>Operating system<input type="text" id="d-os" value="${esc(n.os || "")}" placeholder="e.g. Debian 12, Proxmox VE 8"></label></div>
        <label>Agent<select id="d-agent">${agOpts}</select></label>`}
        <label>📝 Note<textarea id="d-note" rows="3" style="resize:vertical" placeholder="location, contact, hints …">${esc(noteOf(n))}</textarea></label>
        <label>Show note<select id="d-nmode"><option value="hover" ${mode === "hover" ? "selected" : ""}>on hover</option>
          <option value="always" ${mode === "always" ? "selected" : ""}>always on the map</option></select></label>
        ${n.mac ? `<div style="font-size:11px;color:var(--subtext)">MAC ${esc(n.mac)}</div>` : ""}
        <div style="display:flex;gap:6px;justify-content:flex-end;margin-top:4px;flex-wrap:wrap">
          ${noteOnly ? "" : `<button class="btn btn-danger" id="d-del" style="margin-right:auto">Delete</button>`}
          <button class="btn" id="d-cancel">Cancel</button><button class="btn-primary" id="d-ok">Apply</button>
        </div></div>`);
    dlg.querySelector("#d-cancel").onclick = closeDialog;
    const agSel = dlg.querySelector("#d-agent"), tySel = dlg.querySelector("#d-type");
    if (agSel) agSel.onchange = () => {             // picking an agent -> take over its icon
      const c = agSel.value ? (findClient(agSel.value) || agentList.find((x) => x.id === agSel.value)) : (n.agent && !n.agentId ? n.agent : null);
      let opt = tySel.querySelector('option[value=""]');
      if (!c) { if (opt && !n.agent) opt.remove(); return; }
      const t = clientIconType(c);
      if (!opt) { opt = document.createElement("option"); opt.value = ""; tySel.prepend(opt); }
      opt.textContent = `Same as agent (${TYPE_LABELS[t] || t})`;
      tySel.value = "";
    };
    dlg.querySelector("#d-ok").onclick = () => {
      const v = (id) => dlg.querySelector(id)?.value.trim() ?? null;
      const meta = { ...(ovr.meta[n.id] || {}) };
      if (!noteOnly) {
        const fields = { label: v("#d-name"), type: v("#d-type"), ip: v("#d-ip"), os: v("#d-os"), agent_id: v("#d-agent") };
        if (n.custom) {
          const c = ovr.custom_nodes.find((x) => x.id === n.id);
          if (c) Object.assign(c, { ...fields, label: fields.label || c.label });
        } else {
          for (const [k, val] of Object.entries(fields)) {
            const auto = k === "label" ? (n.hostname || "") : k === "ip" ? (mapData?.nodes?.find((x) => x.id === n.id)?.ip || "")
              : k === "type" && !n.agent ? (mapData?.nodes?.find((x) => x.id === n.id)?.type || "") : "";
            if (val && val !== auto) meta[k] = val; else delete meta[k];
          }
        }
      }
      const note = v("#d-note");
      if (note) { meta.note = note; meta.note_mode = v("#d-nmode"); } else { delete meta.note; delete meta.note_mode; }
      if (Object.keys(meta).length) ovr.meta[n.id] = meta; else delete ovr.meta[n.id];
      const newAgent = !noteOnly && v("#d-agent");
      if (newAgent && !agentsById[newAgent]) {
        const a = agentList.find((x) => x.id === newAgent);
        if (a) agentsById[newAgent] = a;
      }
      saveOvr(); closeDialog(); draw();
    };
    const del = dlg.querySelector("#d-del");
    if (del) del.onclick = async () => {
      if (n.custom) {        // own node: just drop it from the overrides
        ovr.custom_nodes = ovr.custom_nodes.filter((x) => x.id !== n.id);
        ovr.lines = ovr.lines.filter((l) => l.a !== n.id && l.b !== n.id);
        delete ovr.pos[n.id]; delete ovr.meta[n.id];
        saveOvr(); closeDialog(); draw();
      } else {               // discovered device: really remove it from the map
        closeDialog();
        await deleteItems([n.id]);
      }
    };
  }

  function openLineDialog(e) {
    if (!edit && !lineMode) return;
    const nodes = graph().nodes;
    const nm = (id) => { const n = nodes.find((x) => x.id === id); return esc(n ? (n.label || n.hostname || n.ip || id) : id); };
    const flowOpts = `<option value="">— none —</option>` + (ovr.flows || []).map((f) =>
      `<option value="${esc(f.id)}" ${f.id === e.flow ? "selected" : ""}>${esc(f.name)}</option>`).join("");
    let color = e.color || "";
    openDlg(`
      <div style="font-weight:600;margin-bottom:10px">Line: ${nm(e.a)} ↔ ${nm(e.b)}</div>
      <div style="display:flex;flex-direction:column;gap:8px">
        <label>Name (shown in the middle)<input type="text" id="l-name" value="${esc(e.label || "")}" placeholder="e.g. tunneled via, sync, uplink"></label>
        <div class="row2">
          <label>End at ${nm(e.a)}<input type="text" id="l-la" value="${esc(e.la || "")}" placeholder="e.g. 8080, port 4, WAN"></label>
          <label>End at ${nm(e.b)}<input type="text" id="l-lb" value="${esc(e.lb || "")}" placeholder="e.g. LAN2, eth0"></label></div>
        <div class="row2">
          <label>Flow<select id="l-flow">${flowOpts}</select></label>
          <label>Style<select id="l-style"><option value="">map default (${STYLES.find(([k]) => k === (ovr.line_style || "default"))[1]})</option>
            ${STYLES.map(([k, l]) => `<option value="${k}" ${k === e.style ? "selected" : ""}>${l}</option>`).join("")}</select></label></div>
        <label>Arrow<select id="l-arrow">${ARROWS.map(([k, l]) => `<option value="${k}" ${k === (e.arrow || "none") ? "selected" : ""}>${l}</option>`).join("")}</select></label>
        <label>Colour <span style="font-size:11px">(empty = colour of the flow)</span>${palette(color, "line")}
          <button type="button" class="btn btn-sm" id="l-nocol" style="align-self:flex-start;margin-top:4px">Use flow colour</button></label>
        <div style="display:flex;gap:6px;justify-content:flex-end;margin-top:4px;flex-wrap:wrap">
          <button class="btn btn-danger" id="l-del" style="margin-right:auto">${e.auto ? "Remove" : "Delete"}</button>
          <button class="btn" id="l-cancel">Cancel</button><button class="btn-primary" id="l-ok">Apply</button>
        </div></div>`);
    wirePalette("line", (c) => { color = c; });
    dlg.querySelector("#l-nocol").onclick = () => { color = ""; dlg.querySelectorAll('[data-pal="line"] [data-c]').forEach((x) => x.classList.remove("on")); };
    dlg.querySelector("#l-cancel").onclick = closeDialog;
    dlg.querySelector("#l-ok").onclick = () => {
      const v = (id) => dlg.querySelector(id).value.trim();
      const props = { label: v("#l-name"), la: v("#l-la"), lb: v("#l-lb"), flow: v("#l-flow"), style: v("#l-style"),
                      arrow: v("#l-arrow"), color };
      for (const k of Object.keys(props)) if (!props[k] || props[k] === "none") delete props[k];
      if (e.auto) {
        const k = edgeKey(e.a, e.b);
        if (Object.keys(props).length) ovr.edge_props[k] = props; else delete ovr.edge_props[k];
      } else {
        const i = ovr.lines.findIndex((l) => l.id === e.id);
        if (i >= 0) ovr.lines[i] = { id: e.id, a: e.a, b: e.b, ...props };
      }
      saveOvr(); closeDialog(); draw();
    };
    dlg.querySelector("#l-del").onclick = () => {
      if (e.auto) { ovr.removed_edges.push([e.a, e.b]); delete ovr.edge_props[edgeKey(e.a, e.b)]; }
      else ovr.lines = ovr.lines.filter((l) => l.id !== e.id);
      saveOvr(); closeDialog(); draw();
    };
  }

  function openBoxDialog(b) {
    let color = b.color;
    openDlg(`
      <div style="font-weight:600;margin-bottom:10px">Box</div>
      <div style="display:flex;flex-direction:column;gap:8px">
        <label>Name<input type="text" id="b-name" value="${esc(b.label || "")}" placeholder="e.g. PVE node 1, DMZ, Office"></label>
        <label>Colour${palette(color, "box")}</label>
        <div style="font-size:11px;color:var(--subtext)">Moving the box moves everything inside. Drag the corner to resize.</div>
        <div style="display:flex;gap:6px;justify-content:flex-end;margin-top:4px">
          <button class="btn btn-danger" id="b-del" style="margin-right:auto">Delete box</button>
          <button class="btn" id="b-cancel">Cancel</button><button class="btn-primary" id="b-ok">Apply</button>
        </div></div>`);
    wirePalette("box", (c) => { color = c; });
    dlg.querySelector("#b-cancel").onclick = closeDialog;
    dlg.querySelector("#b-ok").onclick = () => { b.label = dlg.querySelector("#b-name").value.trim(); b.color = color; saveOvr(); closeDialog(); draw(); };
    dlg.querySelector("#b-del").onclick = () => { ovr.boxes = ovr.boxes.filter((x) => x.id !== b.id); saveOvr(); closeDialog(); draw(); };
    setTimeout(() => dlg.querySelector("#b-name").focus(), 20);
  }

  function openFlowsDialog() {
    const drawList = () => {
      openDlg(`
        <div style="font-weight:600;margin-bottom:6px">🎨 Flows</div>
        <div style="font-size:12px;color:var(--subtext);margin-bottom:10px">Named colours for lines, e.g. <b>Backup</b> (orange) from the backup
          server to every server it backs up, <b>Sync</b> (blue) between synchronised servers, <b>VPN</b>. Shown in the legend
          (click = highlight, right-click = hide).</div>
        <div id="f-list" style="display:flex;flex-direction:column;gap:6px;margin-bottom:10px">
          ${(ovr.flows || []).map((f) => `<div style="display:flex;gap:6px;align-items:center">
            <input type="color" data-fc="${esc(f.id)}" value="${f.color}" style="width:30px;height:24px;padding:0;border:0;background:none">
            <input type="text" data-fn="${esc(f.id)}" value="${esc(f.name)}" style="flex:1">
            <button class="btn btn-sm btn-danger" data-fd="${esc(f.id)}" title="Delete">✕</button></div>`).join("")
            || `<div style="color:var(--subtext);font-size:12px">No flows yet.</div>`}
        </div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px">
          ${[["Backup", "#f59e0b"], ["Sync", "#3b82f6"], ["VPN / Tunnel", "#a855f7"], ["Production", "#35c26a"], ["Management", "#ec4899"]]
            .filter(([n]) => !(ovr.flows || []).some((f) => f.name === n))
            .map(([n, c]) => `<button class="btn btn-sm" data-add="${esc(n)}" data-addc="${c}"><span class="tp-sw" style="display:inline-block;background:${c}"></span> ${esc(n)}</button>`).join("")}
          <button class="btn btn-sm" data-add="" data-addc="#06b6d4">＋ Own flow</button>
        </div>
        <div style="display:flex;justify-content:flex-end;gap:6px"><button class="btn-primary" id="f-close">Done</button></div>`);
      dlg.querySelectorAll("[data-fc]").forEach((i) => i.oninput = () => { const f = ovr.flows.find((x) => x.id === i.dataset.fc); f.color = i.value; saveOvr(); draw(); });
      dlg.querySelectorAll("[data-fn]").forEach((i) => i.onchange = () => { const f = ovr.flows.find((x) => x.id === i.dataset.fn); f.name = i.value.trim() || f.name; saveOvr(); fillFlowSelect(); draw(); });
      dlg.querySelectorAll("[data-fd]").forEach((b) => b.onclick = () => {
        const id = b.dataset.fd;
        ovr.flows = ovr.flows.filter((f) => f.id !== id);
        for (const l of ovr.lines) if (l.flow === id) delete l.flow;
        for (const p of Object.values(ovr.edge_props)) if (p.flow === id) delete p.flow;
        saveOvr(); fillFlowSelect(); draw(); drawList();
      });
      dlg.querySelectorAll("[data-add]").forEach((b) => b.onclick = () => {
        ovr.flows.push({ id: rid("f"), name: b.dataset.add || "New flow", color: b.dataset.addc });
        saveOvr(); fillFlowSelect(); draw(); drawList();
        if (!b.dataset.add) dlg.querySelector("[data-fn]:last-of-type")?.focus();
      });
      dlg.querySelector("#f-close").onclick = closeDialog;
    };
    drawList();
  }

  // ---------------------------------------------------------------- toolbar
  editBtn.addEventListener("click", () => {
    edit = !edit;
    editBtn.classList.toggle("active", edit);
    $("#tp-edittools").style.display = edit ? "inline-flex" : "none";
    if (!edit) { lineMode = false; lineFrom = null; lineBtn.classList.remove("active"); sel.clear(); closeDialog(); }
    setText(mapInfo()); draw();
  });
  $("#tp-add").addEventListener("click", () => {
    const r = wrap.getBoundingClientRect();
    const id = `c:${Math.random().toString(36).slice(2, 10)}`;
    const x = snap((r.width / 2 - view.x) / view.z), y = snap((r.height / 2 - view.y) / view.z);
    ovr.custom_nodes.push({ id, label: "New node", type: "server", x, y, ip: "", os: "", agent_id: "", info: "" });
    sel = new Set([id]);
    saveOvr(); draw();
    const n = graph().nodes.find((m) => m.id === id);
    if (n) openNodeDialog(n);
  });
  $("#tp-box").addEventListener("click", () => {
    const ids = [...sel].filter((k) => !k.startsWith("box:") && positions[k]);
    let b;
    if (ids.length) {                                 // frame the selection
      const xs = ids.map((k) => positions[k].x), ys = ids.map((k) => positions[k].y);
      const pad = 60;
      b = { x: snap(Math.min(...xs) - pad), y: snap(Math.min(...ys) - pad), w: snap(Math.max(...xs) - Math.min(...xs) + 2 * pad),
            h: snap(Math.max(...ys) - Math.min(...ys) + 2 * pad + 30) };
    } else {
      const r = wrap.getBoundingClientRect();
      b = { x: snap((r.width / 2 - view.x) / view.z - 160), y: snap((r.height / 2 - view.y) / view.z - 100), w: 320, h: 200 };
    }
    const box = { id: rid("b"), label: "", color: "#64748b", ...b };
    ovr.boxes.push(box);
    sel = new Set(["box:" + box.id]);
    saveOvr(); draw(); openBoxDialog(box);
  });
  $("#tp-style").addEventListener("change", (e) => { ovr.line_style = e.target.value; saveOvr(); draw(); });
  $("#tp-flows").addEventListener("click", openFlowsDialog);
  $("#tp-reset").addEventListener("click", () => { ovr.pos = {}; saveOvr(); draw(); fit(); });
  $("#tp-bg").addEventListener("change", (e) => { prefs.bg = e.target.value; drawBackground(); savePrefs(); });
  $("#tp-snap").addEventListener("click", () => {
    prefs.snap = !prefs.snap;
    $("#tp-snap").classList.toggle("active", prefs.snap);
    savePrefs();
  });

  // ---------------------------------------------------------------- pan / zoom
  function applyView() {
    vp?.setAttribute("transform", `translate(${view.x},${view.y}) scale(${view.z})`);
    bgPat?.setAttribute("patternTransform", `translate(${view.x},${view.y}) scale(${view.z})`);
  }
  svg.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    const r = wrap.getBoundingClientRect();
    const mx = ev.clientX - r.left, my = ev.clientY - r.top;
    const z = Math.min(3, Math.max(0.15, view.z * (ev.deltaY < 0 ? 1.15 : 1 / 1.15)));
    view.x = mx - ((mx - view.x) / view.z) * z;
    view.y = my - ((my - view.y) / view.z) * z;
    view.z = z;
    applyView();
  }, { passive: false });
  $("#tp-zin").addEventListener("click", () => { view.z = Math.min(3, view.z * 1.25); applyView(); });
  $("#tp-zout").addEventListener("click", () => { view.z = Math.max(0.15, view.z / 1.25); applyView(); });
  $("#tp-fit").addEventListener("click", fit);
  function fit() {
    const pts = Object.values(positions);
    for (const b of ovr.boxes || []) pts.push({ x: b.x, y: b.y - 30 }, { x: b.x + b.w, y: b.y + b.h });
    if (!pts.length) return;
    const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y);
    const minX = Math.min(...xs) - 100, maxX = Math.max(...xs) + 100, minY = Math.min(...ys) - 80, maxY = Math.max(...ys) + 110;
    const r = wrap.getBoundingClientRect();
    const z = Math.min(1.6, Math.max(0.15, Math.min(r.width / (maxX - minX || 1), r.height / (maxY - minY || 1))));
    view = { z, x: (r.width - (maxX + minX) * z) / 2, y: (r.height - (maxY + minY) * z) / 2 };
    applyView();
  }

  // ---------------------------------------------------------------- start
  loadPrefs();
  loadMaps();
  loadMap();
  loadBmcLinks().then(() => { try { draw(); } catch { /* vor erstem Laden */ } });
  watchSource();
  if (win?.key) registerCleanup(win.key, () => {
    stopWatch?.();
    dashboardSocket.off("topology:changed", onTopoChanged);
    clearTimeout(saveTimer);
  });
}
