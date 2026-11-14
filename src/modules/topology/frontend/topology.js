// topology.js (Modul "topology")
// ------------------------------
// Netzwerk-Topologie-Karte als Baumdiagramm:
//   Internet (oben) -> Provider/Upstream-Router -> Gateway -> alle Geraete.
//
// * Erkennung vom Backend oder aus dem Netz einer Node (Node-Modul "topology")
// * Hover auf ein Geraet -> IP, MAC, Hostname, Typ, offene Ports, Agent
// * Geraet mit installiertem Agenten: Klick oeffnet den Client als Fenster
// * Bearbeiten-Modus: verschieben, umbenennen, Icon-Typ aendern, ausblenden,
//   eigene Knoten anlegen, Linien ziehen/entfernen. Alles wird je Quelle
//   (Backend/Node) gespeichert.

import { moduleRequest } from "/js/app/modules.js";
import { api } from "/js/app/api.js";
import { esc } from "/js/app/utils.js";
import { openWindow, registerCleanup } from "/js/app/windowmanager.js";
import { nodeHint } from "/js/app/nodechoice.js";
import { findClient } from "/js/app/state.js";
import { iconBody, TYPE_LABELS, EDIT_TYPES } from "./icons.js";

const SVGNS = "http://www.w3.org/2000/svg";
const PHASES = { queued: "wartet …", ping: "Geräte suchen (Ping-Sweep)",
  trace: "Weg ins Internet verfolgen", fingerprint: "Geräte untersuchen (Ports)" };

export function renderTopology(body, win) {
  // ---------------- Zustand ----------------
  let source = "backend";            // "backend" oder Node-Client-ID
  let mapData = null;                // { nodes, edges, at, label, gateway, own_ip }
  let ovr = emptyOvr();              // Bearbeitungen (Positionen, eigene Knoten ...)
  let edit = false;                  // Bearbeiten-Modus
  let connectFrom = null;            // Linien-Modus: erster angeklickter Knoten
  let jobId = null, poll = null, destroyed = false;
  let view = { x: 40, y: 40, z: 1 }; // Pan/Zoom
  let saveTimer = null;

  function emptyOvr() {
    return { pos: {}, meta: {}, hidden: [], custom_nodes: [], custom_edges: [], removed_edges: [] };
  }

  // ---------------- Grundgeruest ----------------
  body.innerHTML = `
    <div style="display:flex;flex-direction:column;height:100%;position:relative">
      <div class="explorer-toolbar" style="flex-wrap:wrap;gap:8px">
        <select id="tp-via" title="Von wo aus erkennen?" style="max-width:220px">
          <option value="">🖥 Vom Backend</option>
        </select>
        <input type="text" id="tp-target" placeholder="Ziel: leer = eigenes Netz · 192.168.1. · 10.10.0.0/16"
          style="flex:1;min-width:180px" />
        <button class="btn-primary" id="tp-scan">📡 Erkennen</button>
        <button class="btn" id="tp-stop" style="display:none">✋ Abbrechen</button>
        <button class="btn" id="tp-edit" title="Karte bearbeiten">✏️ Bearbeiten</button>
        <button class="btn" id="tp-add" style="display:none" title="Eigenen Knoten hinzufügen">➕ Knoten</button>
        <button class="btn" id="tp-link" style="display:none" title="Linie zwischen zwei Knoten ziehen">🔗 Linie</button>
        <button class="btn" id="tp-reset" title="Verschobene Knoten zurück ins Auto-Layout">↺ Layout</button>
      </div>
      <div id="tp-status" style="padding:4px 10px;font-size:12px;color:var(--subtext);display:flex;gap:10px;align-items:center">
        <span id="tp-text">Karte wird geladen …</span>
        <span id="tp-saved" style="color:var(--ok,#34d399);display:none">✓ gespeichert</span>
      </div>
      <div id="tp-progress" style="display:none;padding:0 10px 6px">
        <div style="height:8px;border-radius:4px;background:var(--panel-2);overflow:hidden">
          <div id="tp-bar" style="height:100%;width:0%;background:var(--accent);transition:width .25s"></div>
        </div>
      </div>
      <div id="tp-wrap" style="flex:1;position:relative;overflow:hidden;background:var(--panel-2);border-radius:6px;margin:0 6px 6px">
        <svg id="tp-svg" style="width:100%;height:100%;display:block;cursor:grab;user-select:none"></svg>
        <div style="position:absolute;right:10px;bottom:10px;display:flex;flex-direction:column;gap:4px">
          <button class="btn btn-sm" id="tp-zin">＋</button>
          <button class="btn btn-sm" id="tp-zout">－</button>
          <button class="btn btn-sm" id="tp-fit" title="Alles einpassen">⛶</button>
        </div>
        <div id="tp-tip" style="position:absolute;display:none;z-index:30;pointer-events:none;max-width:280px;
          background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:8px 10px;
          font-size:12px;line-height:1.45;box-shadow:0 6px 18px rgba(0,0,0,.35)"></div>
        <div id="tp-dlg" style="position:absolute;display:none;z-index:40;left:50%;top:50%;transform:translate(-50%,-50%);
          background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px;min-width:260px;
          box-shadow:0 10px 30px rgba(0,0,0,.45)"></div>
      </div>
    </div>`;

  const $ = (sel) => body.querySelector(sel);
  const svg = $("#tp-svg"), wrap = $("#tp-wrap"), tip = $("#tp-tip"), dlg = $("#tp-dlg");
  const textEl = $("#tp-text"), savedEl = $("#tp-saved");
  const progEl = $("#tp-progress"), barEl = $("#tp-bar");
  const scanBtn = $("#tp-scan"), stopBtn = $("#tp-stop");
  const editBtn = $("#tp-edit"), addBtn = $("#tp-add"), linkBtn = $("#tp-link");

  // ---------------- Quelle (Backend/Node) ----------------
  const viaSel = $("#tp-via");
  api.nodeChoices("topology").then((r) => {
    for (const n of r.nodes || []) {
      const o = document.createElement("option");
      o.value = n.id; o.disabled = !n.active;
      o.textContent = `🖧 Node ${n.name}${nodeHint(n)}`;
      viaSel.appendChild(o);
    }
  }).catch(() => {});
  viaSel.addEventListener("change", () => {
    source = viaSel.value || "backend";
    connectFrom = null;
    loadMap();
  });

  // ---------------- Karte laden / speichern ----------------
  async function loadMap() {
    try {
      const r = await moduleRequest(`/api/topology/map?source=${encodeURIComponent(source)}`);
      mapData = r.map;
      ovr = { ...emptyOvr(), ...(r.overrides || {}) };
      if (mapData) {
        const when = mapData.at ? new Date(mapData.at).toLocaleString() : "";
        setText(`${countDevices()} Geräte · ${esc(mapData.label || "")} · Stand: ${when}`);
      } else {
        setText("Noch keine Karte für diese Quelle – oben auf „📡 Erkennen“ klicken.");
      }
      draw(); fit();
    } catch (e) {
      setText(e.message, true);
    }
  }

  function countDevices() {
    return (mapData?.nodes || []).filter((n) => n.id?.startsWith("dev:")).length;
  }

  function setText(msg, isErr = false) {
    textEl.innerHTML = msg;
    textEl.style.color = isErr ? "var(--danger)" : "var(--subtext)";
  }

  function saveOvr() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(async () => {
      try {
        await moduleRequest(`/api/topology/map?source=${encodeURIComponent(source)}`,
          { method: "PUT", body: JSON.stringify(ovr) });
        savedEl.style.display = "";
        setTimeout(() => { savedEl.style.display = "none"; }, 1200);
      } catch (e) { setText(`Speichern fehlgeschlagen: ${e.message}`, true); }
    }, 350);
  }

  // ---------------- Erkennung ----------------
  async function startScan() {
    if (jobId) return;
    scanBtn.disabled = true;
    try {
      const target = $("#tp-target").value.trim();
      const q = new URLSearchParams();
      if (target) q.set("target", target);
      if (viaSel.value) q.set("via", viaSel.value);
      const r = await moduleRequest(`/api/topology/discover/start?${q}`, { method: "POST" });
      jobId = r.job_id;
      stopBtn.style.display = ""; progEl.style.display = "";
      poll = setInterval(pollJob, 800); pollJob();
    } catch (e) {
      scanBtn.disabled = false;
      setText(e.message, true);
    }
  }

  async function pollJob() {
    if (!jobId || destroyed) return;
    let job;
    try { job = await moduleRequest(`/api/topology/discover/job/${encodeURIComponent(jobId)}`); }
    catch (e) { finishScan(); setText(e.message, true); return; }

    let pct = 0, detail = PHASES[job.phase] || job.phase || "";
    if (job.phase === "ping" && job.total_hosts) {
      pct = (job.done_hosts / job.total_hosts) * 80;
      detail += ` · ${job.done_hosts}/${job.total_hosts} Adressen · ${job.found} gefunden`;
    } else if (job.phase === "trace") pct = 84;
    else if (job.phase === "fingerprint") {
      pct = 86 + (job.fp_total ? (job.fp_done / job.fp_total) * 14 : 0);
      detail += ` · ${job.fp_done}/${job.fp_total}`;
    }
    barEl.style.width = Math.min(100, pct).toFixed(1) + "%";
    setText(`Erkennung läuft: ${detail}`);

    if (["done", "cancelled", "error"].includes(job.status)) {
      finishScan();
      if (job.status === "error") { setText(job.error || "Erkennung fehlgeschlagen", true); return; }
      if (job.result) {
        mapData = job.result;
        const dur = job.finished && job.started ? Math.round((job.finished - job.started) / 1000) : null;
        setText(`${countDevices()} Geräte erkannt (${esc(job.result.label || "")})`
          + (dur != null ? ` · ${dur} s` : "") + (job.status === "cancelled" ? " (abgebrochen)" : ""));
        draw(); fit();
      }
    }
  }

  function finishScan() {
    clearInterval(poll); poll = null; jobId = null;
    scanBtn.disabled = false;
    stopBtn.style.display = "none"; progEl.style.display = "none";
  }

  scanBtn.addEventListener("click", startScan);
  stopBtn.addEventListener("click", async () => {
    if (jobId) try { await moduleRequest(`/api/topology/discover/job/${encodeURIComponent(jobId)}/cancel`, { method: "POST" }); } catch {}
  });

  // ---------------- Graph zusammenbauen (Erkennung + Bearbeitungen) ----------------
  function mergedGraph() {
    const hidden = new Set(ovr.hidden || []);
    const nodes = [];
    for (const n of (mapData?.nodes || [])) {
      if (hidden.has(n.id)) continue;
      const meta = (ovr.meta || {})[n.id] || {};
      nodes.push({ ...n, type: meta.type || n.type, label: meta.label || n.label, _meta: meta });
    }
    for (const c of (ovr.custom_nodes || [])) nodes.push({ ...c, custom: true });

    const have = new Set(nodes.map((n) => n.id));
    const removed = new Set((ovr.removed_edges || []).map((e) => e.join("|")));
    const edges = [];
    for (const [a, b] of (mapData?.edges || [])) {
      if (!have.has(a) || !have.has(b)) continue;
      if (removed.has(`${a}|${b}`) || removed.has(`${b}|${a}`)) continue;
      edges.push({ a, b, custom: false });
    }
    for (const [a, b] of (ovr.custom_edges || [])) {
      if (have.has(a) && have.has(b)) edges.push({ a, b, custom: true });
    }
    return { nodes, edges };
  }

  // ---------------- Layout (Baum: Ebenen von oben nach unten) ----------------
  const NODE_W = 150, ROW_H = 150;
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
        // unterste Ebene (viele Geraete) -> in mehrere Reihen umbrechen
        const perRow = Math.max(4, Math.ceil(Math.sqrt(row.length * 1.8)));
        row.forEach((n, i) => {
          const r = Math.floor(i / perRow), c = i % perRow;
          const w = Math.min(perRow, row.length - r * perRow);
          pos[n.id] = { x: (c - (w - 1) / 2) * NODE_W, y: rowY + r * ROW_H };
        });
        rowY += ROW_H * Math.ceil(row.length / perRow);
      }
    }
    // manuell verschobene Positionen + eigene Knoten
    for (const n of nodes) {
      const p = (ovr.pos || {})[n.id];
      if (p) pos[n.id] = { x: p.x, y: p.y };
      else if (n.custom) pos[n.id] = { x: n.x || 0, y: n.y || 0 };
      else if (!pos[n.id]) pos[n.id] = { x: 0, y: rowY };
    }
    return pos;
  }

  // ---------------- Zeichnen ----------------
  let positions = {};
  function draw() {
    const { nodes, edges } = mergedGraph();
    positions = layout(nodes);
    svg.innerHTML = "";
    const vp = document.createElementNS(SVGNS, "g");
    vp.setAttribute("id", "tp-vp");
    applyView(vp);
    svg.appendChild(vp);

    const lineCol = "var(--ok, #35c26a)";

    // Linien
    for (const e of edges) {
      const pa = positions[e.a], pb = positions[e.b];
      if (!pa || !pb) continue;
      const g = document.createElementNS(SVGNS, "g");
      const line = document.createElementNS(SVGNS, "line");
      line.setAttribute("x1", pa.x); line.setAttribute("y1", pa.y + 34);
      line.setAttribute("x2", pb.x); line.setAttribute("y2", pb.y - 34);
      line.setAttribute("stroke", lineCol);
      line.setAttribute("stroke-width", "1.6");
      line.setAttribute("opacity", "0.85");
      if (e.custom) line.setAttribute("stroke-dasharray", "6 5");
      g.appendChild(line);
      if (edit) {
        const hit = line.cloneNode();
        hit.setAttribute("stroke", "transparent");
        hit.setAttribute("stroke-width", "12");
        hit.style.cursor = "pointer";
        hit.addEventListener("click", (ev) => { ev.stopPropagation(); removeEdge(e); });
        hit.addEventListener("mouseenter", () => line.setAttribute("stroke", "var(--danger)"));
        hit.addEventListener("mouseleave", () => line.setAttribute("stroke", lineCol));
        g.appendChild(hit);
        g.querySelector("line").setAttribute("stroke-width", "2");
      }
      vp.appendChild(g);
    }

    // Knoten
    for (const n of nodes) drawNode(vp, n, positions[n.id]);
  }

  function drawNode(vp, n, p) {
    if (!p) return;
    const g = document.createElementNS(SVGNS, "g");
    g.setAttribute("transform", `translate(${p.x},${p.y})`);
    g.style.cursor = edit ? "move" : (n.agent ? "pointer" : "default");
    g.dataset.nid = n.id;

    const isSel = connectFrom === n.id;
    const ring = document.createElementNS(SVGNS, "rect");
    ring.setAttribute("x", -34); ring.setAttribute("y", -34);
    ring.setAttribute("width", 68); ring.setAttribute("height", 68);
    ring.setAttribute("rx", 12);
    ring.setAttribute("fill", "var(--panel)");
    ring.setAttribute("stroke", isSel ? "var(--accent)"
      : n.agent ? (n.agent.color || "var(--accent)") : "var(--border)");
    ring.setAttribute("stroke-width", isSel ? 3 : n.agent ? 2.2 : 1.2);
    g.appendChild(ring);

    const icon = document.createElementNS(SVGNS, "g");
    icon.setAttribute("transform", "translate(-24,-26)");
    icon.setAttribute("color", n.type === "www" || n.type === "isp"
      ? "var(--accent)" : "var(--text, #d8dee9)");
    icon.innerHTML = iconBody(n.type);
    g.appendChild(icon);

    // Status-Punkt: Agent online/offline
    if (n.agent) {
      const dot = document.createElementNS(SVGNS, "circle");
      dot.setAttribute("cx", 27); dot.setAttribute("cy", -27); dot.setAttribute("r", 6);
      dot.setAttribute("fill", n.agent.online ? "var(--ok, #34d399)" : "var(--danger, #f87171)");
      dot.setAttribute("stroke", "var(--panel)"); dot.setAttribute("stroke-width", 2);
      g.appendChild(dot);
    }
    if (n.self) {
      const star = document.createElementNS(SVGNS, "text");
      star.setAttribute("x", -27); star.setAttribute("y", -22);
      star.setAttribute("font-size", "13");
      star.textContent = "📍";
      g.appendChild(star);
    }

    const name = n.label || n.hostname || n.ip || "?";
    const l1 = textEl2(name.length > 20 ? name.slice(0, 19) + "…" : name, 0, 48, "12",
      n.agent ? (n.agent.color || "var(--text)") : "var(--text, #d8dee9)", "600");
    g.appendChild(l1);
    const sub = n.ip && n.ip !== name ? n.ip : (TYPE_LABELS[n.type] || "");
    if (sub) g.appendChild(textEl2(sub, 0, 63, "10.5", "var(--subtext, #94a3b8)"));

    // Hover-Tooltip
    g.addEventListener("mouseenter", (ev) => showTip(n, ev));
    g.addEventListener("mousemove", (ev) => moveTip(ev));
    g.addEventListener("mouseleave", hideTip);

    // Ziehen / Klicken
    g.addEventListener("pointerdown", (ev) => onNodeDown(ev, n, g));
    vp.appendChild(g);
  }

  function textEl2(str, x, y, size, fill, weight = "400") {
    const t = document.createElementNS(SVGNS, "text");
    t.setAttribute("x", x); t.setAttribute("y", y);
    t.setAttribute("text-anchor", "middle");
    t.setAttribute("font-size", size);
    t.setAttribute("font-weight", weight);
    t.setAttribute("fill", fill);
    t.textContent = str;
    return t;
  }

  // ---------------- Tooltip ----------------
  function showTip(n, ev) {
    const rows = [];
    const title = n.label || n.hostname || n.ip || "Knoten";
    rows.push(`<div style="font-weight:600;margin-bottom:3px">${esc(title)}</div>`);
    const add = (k, v) => { if (v) rows.push(`<div><span style="color:var(--subtext)">${k}:</span> ${esc(String(v))}</div>`); };
    add("Typ", TYPE_LABELS[n.type] || n.type);
    add("IP", n.ip);
    add("MAC", n.mac);
    add("Hostname", n.hostname);
    if (n.ports?.length) add("Offene Ports", n.ports.join(", "));
    if (n.gateway) rows.push(`<div>🌐 Standard-Gateway</div>`);
    if (n.hop) rows.push(`<div>↕ Zwischenstation Richtung Internet</div>`);
    if (n.self) rows.push(`<div>📍 Von hier wurde erkannt</div>`);
    if (n.custom) rows.push(`<div style="color:var(--subtext)">manuell angelegter Knoten</div>`);
    if (n.info) add("Info", n.info);
    if (n.agent) {
      rows.push(`<div style="margin-top:4px;padding-top:4px;border-top:1px solid var(--border)">
        <span style="color:${n.agent.online ? "var(--ok,#34d399)" : "var(--danger)"}">●</span>
        Agent: <b>${esc(n.agent.hostname || n.agent.id)}</b> (${n.agent.online ? "online" : "offline"})<br>
        <span style="color:var(--subtext)">Klick öffnet den Client</span></div>`);
    }
    tip.innerHTML = rows.join("");
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

  // ---------------- Klick / Ziehen auf Knoten ----------------
  function onNodeDown(ev, n, g) {
    ev.stopPropagation();
    if (ev.button !== 0) return;
    const start = { x: ev.clientX, y: ev.clientY };
    const p0 = { ...positions[n.id] };
    let moved = false;

    const onMove = (e) => {
      const dx = (e.clientX - start.x) / view.z, dy = (e.clientY - start.y) / view.z;
      if (!moved && Math.hypot(dx * view.z, dy * view.z) < 4) return;
      if (!edit) return;               // verschieben nur im Bearbeiten-Modus
      moved = true;
      positions[n.id] = { x: p0.x + dx, y: p0.y + dy };
      g.setAttribute("transform", `translate(${positions[n.id].x},${positions[n.id].y})`);
      hideTip();
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      if (moved) {
        ovr.pos[n.id] = { ...positions[n.id] };
        if (n.custom) {
          const c = (ovr.custom_nodes || []).find((x) => x.id === n.id);
          if (c) { c.x = positions[n.id].x; c.y = positions[n.id].y; }
        }
        saveOvr(); draw();
        return;
      }
      onNodeClick(n);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }

  function onNodeClick(n) {
    if (connectFrom === "?") {         // Linien-Modus: erster Knoten
      connectFrom = n.id;
      setText("Linie ziehen: jetzt den zweiten Knoten anklicken.");
      draw();
      return;
    }
    if (connectFrom) {                 // Linien-Modus: zweiten Knoten waehlen
      if (connectFrom !== n.id) {
        ovr.custom_edges.push([connectFrom, n.id]);
        saveOvr();
      }
      connectFrom = null;
      linkBtn.classList.remove("active");
      draw();
      return;
    }
    if (edit) { openDialog(n); return; }
    if (n.agent) openClientWindow(n.agent);
  }

  function openClientWindow(agent) {
    const c = findClient(agent.id) || agent;
    openWindow({
      singleton: true,
      key: `panelpart-${agent.id}-client-x`,
      appId: "panelpart",
      title: c.hostname || agent.hostname || agent.id,
      props: { clientId: agent.id, part: "client" },
      clientColor: c.color, w: 900, h: 620,
    });
  }

  function removeEdge(e) {
    if (e.custom) {
      ovr.custom_edges = (ovr.custom_edges || []).filter(([a, b]) =>
        !((a === e.a && b === e.b) || (a === e.b && b === e.a)));
    } else {
      ovr.removed_edges.push([e.a, e.b]);
    }
    saveOvr(); draw();
  }

  // ---------------- Bearbeiten-Dialog ----------------
  function openDialog(n) {
    hideTip();
    const typeOpts = EDIT_TYPES.map((t) =>
      `<option value="${t}" ${t === n.type ? "selected" : ""}>${TYPE_LABELS[t] || t}</option>`).join("");
    dlg.innerHTML = `
      <div style="font-weight:600;margin-bottom:10px">Knoten bearbeiten</div>
      <div style="display:flex;flex-direction:column;gap:8px">
        <label style="display:flex;flex-direction:column;gap:3px;font-size:12px;color:var(--subtext)">Name
          <input type="text" id="tpd-name" value="${esc(n.label || n.hostname || n.ip || "")}"></label>
        <label style="display:flex;flex-direction:column;gap:3px;font-size:12px;color:var(--subtext)">Icon / Typ
          <select id="tpd-type">${typeOpts}</select></label>
        ${n.custom ? `<label style="display:flex;flex-direction:column;gap:3px;font-size:12px;color:var(--subtext)">Info (Tooltip)
          <input type="text" id="tpd-info" value="${esc(n.info || "")}"></label>` : ""}
        <div style="font-size:11px;color:var(--subtext)">${esc(n.ip || "")} ${esc(n.mac ? "· " + n.mac : "")}</div>
        <div style="display:flex;gap:6px;justify-content:flex-end;margin-top:4px;flex-wrap:wrap">
          ${n.custom
            ? `<button class="btn btn-danger" id="tpd-del">Löschen</button>`
            : `<button class="btn btn-danger" id="tpd-hide">Ausblenden</button>`}
          <button class="btn" id="tpd-cancel">Abbrechen</button>
          <button class="btn-primary" id="tpd-ok">Übernehmen</button>
        </div>
      </div>`;
    dlg.style.display = "block";
    dlg.querySelector("#tpd-cancel").onclick = closeDialog;
    dlg.querySelector("#tpd-ok").onclick = () => {
      const label = dlg.querySelector("#tpd-name").value.trim();
      const type = dlg.querySelector("#tpd-type").value;
      if (n.custom) {
        const c = (ovr.custom_nodes || []).find((x) => x.id === n.id);
        if (c) {
          c.label = label || c.label; c.type = type;
          c.info = dlg.querySelector("#tpd-info")?.value.trim() || "";
        }
      } else {
        ovr.meta[n.id] = { ...(ovr.meta[n.id] || {}) };
        if (label) ovr.meta[n.id].label = label; else delete ovr.meta[n.id].label;
        ovr.meta[n.id].type = type;
      }
      saveOvr(); closeDialog(); draw();
    };
    const del = dlg.querySelector("#tpd-del");
    if (del) del.onclick = () => {
      ovr.custom_nodes = (ovr.custom_nodes || []).filter((x) => x.id !== n.id);
      ovr.custom_edges = (ovr.custom_edges || []).filter(([a, b]) => a !== n.id && b !== n.id);
      delete ovr.pos[n.id];
      saveOvr(); closeDialog(); draw();
    };
    const hide = dlg.querySelector("#tpd-hide");
    if (hide) hide.onclick = () => {
      if (!ovr.hidden.includes(n.id)) ovr.hidden.push(n.id);
      saveOvr(); closeDialog(); draw();
    };
  }
  function closeDialog() { dlg.style.display = "none"; }

  // ---------------- Toolbar: Bearbeiten / Knoten / Linie / Layout ----------------
  editBtn.addEventListener("click", () => {
    edit = !edit;
    editBtn.classList.toggle("active", edit);
    addBtn.style.display = edit ? "" : "none";
    linkBtn.style.display = edit ? "" : "none";
    if (!edit) { connectFrom = null; linkBtn.classList.remove("active"); closeDialog(); }
    setText(edit
      ? "Bearbeiten: Knoten ziehen zum Verschieben · Klick zum Umbenennen/Ausblenden · Klick auf eine Linie löscht sie"
      : `${countDevices()} Geräte · Änderungen gespeichert`);
    draw();
  });

  addBtn.addEventListener("click", () => {
    const id = `c:${Math.random().toString(36).slice(2, 10)}`;
    // Mitte des sichtbaren Ausschnitts
    const r = wrap.getBoundingClientRect();
    const x = (r.width / 2 - view.x) / view.z, y = (r.height / 2 - view.y) / view.z;
    ovr.custom_nodes.push({ id, label: "Neuer Knoten", type: "device", x, y, info: "" });
    saveOvr(); draw();
    const n = mergedGraph().nodes.find((m) => m.id === id);
    if (n) openDialog(n);
  });

  linkBtn.addEventListener("click", () => {
    if (connectFrom) { connectFrom = null; linkBtn.classList.remove("active"); draw(); return; }
    connectFrom = "?";                 // wartet auf ersten Klick
    linkBtn.classList.add("active");
    setText("Linie ziehen: ersten Knoten anklicken, dann den zweiten.");
  });
  $("#tp-reset").addEventListener("click", () => {
    ovr.pos = {};
    saveOvr(); draw(); fit();
  });

  // ---------------- Pan & Zoom ----------------
  function applyView(vp) {
    (vp || svg.querySelector("#tp-vp"))?.setAttribute(
      "transform", `translate(${view.x},${view.y}) scale(${view.z})`);
  }
  svg.addEventListener("pointerdown", (ev) => {
    if (ev.target !== svg && !ev.target.closest("#tp-vp")) return;
    if (ev.target.closest("[data-nid]")) return;
    const start = { x: ev.clientX, y: ev.clientY, vx: view.x, vy: view.y };
    svg.style.cursor = "grabbing";
    const onMove = (e) => {
      view.x = start.vx + (e.clientX - start.x);
      view.y = start.vy + (e.clientY - start.y);
      applyView();
    };
    const onUp = () => {
      svg.style.cursor = "grab";
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  });
  svg.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    const r = wrap.getBoundingClientRect();
    const mx = ev.clientX - r.left, my = ev.clientY - r.top;
    const factor = ev.deltaY < 0 ? 1.15 : 1 / 1.15;
    const z = Math.min(3, Math.max(0.2, view.z * factor));
    view.x = mx - ((mx - view.x) / view.z) * z;
    view.y = my - ((my - view.y) / view.z) * z;
    view.z = z;
    applyView();
  }, { passive: false });
  $("#tp-zin").addEventListener("click", () => { view.z = Math.min(3, view.z * 1.25); applyView(); });
  $("#tp-zout").addEventListener("click", () => { view.z = Math.max(0.2, view.z / 1.25); applyView(); });
  $("#tp-fit").addEventListener("click", fit);

  function fit() {
    const vals = Object.values(positions);
    if (!vals.length) return;
    const xs = vals.map((p) => p.x), ys = vals.map((p) => p.y);
    const minX = Math.min(...xs) - 90, maxX = Math.max(...xs) + 90;
    const minY = Math.min(...ys) - 70, maxY = Math.max(...ys) + 90;
    const r = wrap.getBoundingClientRect();
    const z = Math.min(2, Math.max(0.2,
      Math.min(r.width / (maxX - minX || 1), r.height / (maxY - minY || 1))));
    view = { z, x: (r.width - (maxX + minX) * z) / 2, y: (r.height - (maxY + minY) * z) / 2 };
    applyView();
  }

  // ---------------- Start ----------------
  loadMap();
  if (win?.key) registerCleanup(win.key, () => { destroyed = true; clearInterval(poll); clearTimeout(saveTimer); });
}
