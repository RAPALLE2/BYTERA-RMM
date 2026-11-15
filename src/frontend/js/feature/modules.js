// modules.js (feature) - the modules app (core)
// ---------------------------------------------
// See, enable/disable, configure modules, switch sub-features and read crash
// reports. Admins only (the backend checks that as well).
//
// Layout: list with search on the left, details of the selected module on the
// right (narrow windows: details replace the list). Every module with apps has
// an "Open app" button.

import { esc, uiConfirm } from "../app/utils.js";
import { moduleRequest as req, loadModules, getModules, reportModuleCrash } from "../app/modules.js";
import { api } from "../app/api.js";
import { state } from "../app/state.js";

const STATUS = {
  enabled:  { label: "active",   color: "#3ecf8e" },
  disabled: { label: "off",      color: "var(--subtext)" },
  crashed:  { label: "crashed",  color: "#ff4d6d" },
};
const PART = { backend: "Backend", frontend: "Frontend", agent: "Agent", client: "Client", node: "Node" };
const fmt = (ms) => (ms ? new Date(ms).toLocaleString() : "–");
const NARROW = 760;

function injectCss() {
  if (document.getElementById("mod-css")) return;
  const s = document.createElement("style");
  s.id = "mod-css";
  s.textContent = `
.mod-wrap{display:flex;height:100%;min-height:0}
.mod-left{flex:1 1 auto;min-width:0;overflow:auto;padding:14px 16px;box-sizing:border-box}
.mod-wrap.split .mod-left{flex:0 0 min(400px,42%);border-right:1px solid var(--border)}
.mod-right{flex:1 1 auto;min-width:0;overflow:auto;padding:14px 18px;box-sizing:border-box;display:none}
.mod-wrap.split .mod-right{display:block}
.mod-wrap.narrow.split .mod-left{display:none}
.mod-wrap.split .mod-grid{grid-template-columns:1fr !important}
.mod-card{cursor:pointer;background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:12px 14px;display:flex;flex-direction:column;gap:6px;transition:border-color .12s}
.mod-card:hover{border-color:var(--accent,#3b82f6)}
.mod-card.sel{border-color:var(--accent,#3b82f6);box-shadow:0 0 0 1px var(--accent,#3b82f6) inset}
.mod-wrap.split .mod-card .mod-desc{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.mod-search{display:flex;gap:8px;align-items:center;margin:4px 0 10px}
.mod-search input{flex:1;min-width:0}
.mod-chip{font-size:11px;padding:3px 9px;border-radius:99px;border:1px solid var(--border);background:none;color:var(--subtext);cursor:pointer}
.mod-chip.on{border-color:var(--accent,#3b82f6);color:var(--text)}
mark.mod-hit{background:rgba(245,165,36,.35);color:inherit;border-radius:2px}`;
  document.head.appendChild(s);
}

export function renderModules(body, win) {
  injectCss();
  let data = { modules: [], errors: [], crash_limit: 1 };
  let openId = null, tab = "overview", query = "", filter = "all";

  body.innerHTML = `<div class="mod-wrap"><div class="mod-left"></div><div class="mod-right"></div></div>`;
  const wrap = body.querySelector(".mod-wrap");
  const left = body.querySelector(".mod-left");
  const root = body.querySelector(".mod-right");          // detail pane (name kept: tab renderers use it)
  const ro = new ResizeObserver(() => wrap.classList.toggle("narrow", wrap.clientWidth < NARROW));
  ro.observe(wrap);

  async function load() {
    const y = left.scrollTop, yr = root.scrollTop;
    try { data = await req("/api/modules"); }
    catch (e) { left.innerHTML = `<div style="color:var(--danger)">${esc(e.message)}</div>`; return; }
    draw();
    left.scrollTop = y; root.scrollTop = yr;
  }
  const byId = (id) => data.modules.find((m) => m.id === id);
  const nameOf = (id) => byId(id)?.name || id;

  // ------------------------------------------------------------------ actions
  async function sync() { await loadModules(); await load(); }

  async function enable(id, withDeps = false) {
    try {
      await req(`/api/modules/${id}/enable${withDeps ? "?with_deps=1" : ""}`, { method: "POST" });
      window.notify?.(`${nameOf(id)} enabled`, "success");
    } catch (e) {
      if (!withDeps && /Benötigt zuerst|Benoetigt zuerst|requires first|needs first/i.test(e.message)) {
        if (await uiConfirm(`${nameOf(id)} needs other modules`, { description: e.message + ". Enable them as well?", okText: "Enable all" })) return enable(id, true);
        return;
      }
      window.notify?.(e.message, "error", 12000);
    }
    await sync();
  }
  async function disable(id, cascade = false) {
    try {
      await req(`/api/modules/${id}/disable${cascade ? "?cascade=1" : ""}`, { method: "POST" });
      window.notify?.(`${nameOf(id)} disabled`, "info");
    } catch (e) {
      if (!cascade && /Wird noch ben|still (needed|required)/i.test(e.message)) {
        if (await uiConfirm(`${nameOf(id)} is still needed`, { description: e.message + ". Disable those as well?", okText: "Disable all" })) return disable(id, true);
        return;
      }
      window.notify?.(e.message, "error", 12000);
    }
    await sync();
  }

  // apps of an active module (from the loaded frontend manifest)
  function appsOf(m) {
    const fm = getModules().find((x) => x.id === m.id);
    return (fm?.apps || []).filter((a) => a.startmenu !== false);
  }
  async function openApp(appId) {
    try {
      const sm = await import("../app/startmenu.js");
      sm.openMenuApp(appId);
    } catch (e) { window.notify?.(e.message, "error"); }
  }
  function appButtons(m, small = false) {
    if (!m.enabled) return "";
    const apps = appsOf(m);
    return apps.map((a) => `<button class="btn ${small ? "btn-sm " : ""}btn-primary" data-app="${esc(a.id)}" title="Open ${esc(a.label || a.id)}">▶ ${apps.length > 1 ? esc(a.label || a.id) : "Open app"}</button>`).join(" ");
  }
  function wireAppButtons(scope) {
    scope.querySelectorAll("[data-app]").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); openApp(b.dataset.app); }));
  }

  // ------------------------------------------------------------------ list
  function badge(m) {
    const s = STATUS[m.status] || STATUS.disabled;
    return `<span style="font-size:11px;padding:2px 8px;border-radius:99px;border:1px solid ${s.color};color:${s.color};white-space:nowrap">${s.label}</span>`;
  }
  const hl = (text) => {
    const t = esc(text || "");
    if (!query) return t;
    const q = query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return t.replace(new RegExp(`(${q})`, "ig"), `<mark class="mod-hit">$1</mark>`);
  };
  function matches(m) {
    if (filter === "active" && !m.enabled) return false;
    if (filter === "off" && m.enabled) return false;
    if (filter === "problems" && m.status !== "crashed" && m.available !== false) return false;
    if (!query) return true;
    const hay = [m.name, m.id, m.description, m.category, m.author, ...(m.parts || []), ...appsOf(m).map((a) => a.label)]
      .join(" ").toLowerCase();
    return query.toLowerCase().split(/\s+/).every((w) => hay.includes(w));
  }
  // relevance while searching: name > id/apps > category/description
  function score(m) {
    const q = query.toLowerCase(), words = q.split(/\s+/);
    const name = (m.name || "").toLowerCase();
    let sc = 0;
    if (name === q) sc += 100;
    if (name.startsWith(q)) sc += 50;
    if (name.includes(q)) sc += 30;
    sc += words.filter((w) => name.includes(w)).length * 10;
    const apps = [m.id, ...appsOf(m).map((a) => a.label || "")].join(" ").toLowerCase();
    sc += words.filter((w) => apps.includes(w)).length * 5;
    return sc;
  }
  function card(m) {
    const crash = m.status === "crashed" && m.last_crash
      ? `<div style="font-size:12px;color:#ff4d6d">⚠ ${esc(m.last_crash.error)}</div>` : "";
    const parts = m.parts.map((p) => `<span style="font-size:10px;padding:1px 6px;border-radius:6px;background:var(--panel-2);color:var(--subtext)">${PART[p] || p}</span>`).join(" ");
    return `<div class="mod-card ${m.id === openId ? "sel" : ""}" data-open="${m.id}">
      <div style="display:flex;align-items:center;gap:10px">
        <span style="font-size:24px">${esc(m.icon)}</span>
        <div style="flex:1;min-width:0"><div style="font-weight:600">${hl(m.name)} <span style="font-weight:400;color:var(--subtext);font-size:12px">v${esc(m.version)}</span></div>
          <div style="margin-top:2px">${parts}</div></div>
        ${badge(m)}
      </div>
      <div class="mod-desc" style="font-size:12px;color:var(--subtext);line-height:1.45">${hl(m.description)}</div>
      ${crash}
      <div style="display:flex;gap:8px;margin-top:2px;flex-wrap:wrap" data-stop>
        ${m.available === false ? `<span style="font-size:12px;color:var(--subtext)">${esc(m.unavailable_reason || "not available")}</span>`
          : m.enabled ? `<button class="btn btn-sm" data-act="disable" data-id="${m.id}">Disable</button>`
                      : `<button class="btn btn-sm btn-primary" data-act="enable" data-id="${m.id}">Enable</button>`}
        ${appButtons(m, true)}
      </div>
    </div>`;
  }
  function drawList() {
    const list = data.modules.filter(matches);
    const cats = {};
    for (const m of list) (cats[m.category] ||= []).push(m);
    const active = data.modules.filter((m) => m.enabled).length;
    const problems = data.modules.filter((m) => m.status === "crashed").length;
    const focused = document.activeElement?.id === "mod-q";
    const caret = focused ? document.activeElement.selectionStart : null;
    left.innerHTML = `
      <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:6px">
        <h2 style="margin:0;font-size:18px">🧩 Modules</h2>
        <span style="color:var(--subtext);font-size:12px">${active} of ${data.modules.length} active</span>
        <span style="flex:1"></span>
        <button class="btn btn-sm" id="mod-rescan" title="Read module folders again">↻ Rescan</button>
      </div>
      <div class="mod-search">
        <input id="mod-q" type="search" placeholder="Search modules, apps, descriptions …" value="${esc(query)}" autocomplete="off">
      </div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px">
        ${[["all", "All"], ["active", "Active"], ["off", "Off"], ["problems", `Problems${problems ? ` (${problems})` : ""}`]].map(([k, l]) =>
          `<button class="mod-chip ${filter === k ? "on" : ""}" data-filter="${k}">${l}</button>`).join("")}
      </div>
      ${openId ? "" : `<p style="margin:0 0 12px;color:var(--subtext);font-size:12px;line-height:1.5">The RMM runs without any module; all modules are off by default.
        If a module crashes it is disabled automatically and a crash report is stored – everything else keeps running.
        <label style="white-space:nowrap" title="After this many crashes within 10 minutes a module is disabled automatically.">· Disable after
          <input id="mod-limit" type="number" min="1" max="50" value="${data.crash_limit}" style="width:52px"> crash(es)</label></p>`}
      ${data.errors.length ? `<div style="background:rgba(255,77,109,.1);border:1px solid #ff4d6d;border-radius:8px;padding:8px 10px;margin-bottom:12px;font-size:12px">
        <b>Invalid module.json:</b><br>${data.errors.map((e) => `${esc(e.path)}: ${esc(e.error)}`).join("<br>")}</div>` : ""}
      ${list.length && query ? `<div class="mod-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px">
          ${[...list].sort((a, b) => score(b) - score(a) || a.name.localeCompare(b.name)).map(card).join("")}</div>`
      : list.length ? Object.keys(cats).sort().map((c) => `<h3 style="margin:14px 0 8px;font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--subtext)">${esc(c)}</h3>
        <div class="mod-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px">${cats[c].map(card).join("")}</div>`).join("")
        : `<div style="color:var(--subtext);padding:20px 0">No module matches “${esc(query)}”.</div>`}`;
    const q = left.querySelector("#mod-q");
    let t = null;
    q.addEventListener("input", () => { clearTimeout(t); t = setTimeout(() => { query = q.value.trim(); drawList(); }, 120); });
    q.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { q.value = ""; query = ""; drawList(); }
      if (e.key === "Enter") { const first = left.querySelector("[data-open]"); if (first) { openId = first.dataset.open; tab = "overview"; draw(); } }
    });
    if (focused) { q.focus(); try { q.setSelectionRange(caret, caret); } catch { /* ignore */ } }
    left.querySelectorAll("[data-filter]").forEach((b) => b.onclick = () => { filter = b.dataset.filter; drawList(); });
    left.querySelector("#mod-rescan").onclick = async () => { await req("/api/modules/rescan", { method: "POST" }); await sync(); };
    const lim = left.querySelector("#mod-limit");
    if (lim) lim.onchange = async (e) => {
      try { await req("/api/modules/crash-policy", { method: "PUT", body: JSON.stringify({ crash_limit: parseInt(e.target.value, 10) || 1 }) }); window.notify?.("Saved", "success", 1500); }
      catch (err) { window.notify?.(err.message, "error"); }
    };
    left.querySelectorAll("[data-open]").forEach((el) => el.addEventListener("click", () => {
      if (openId === el.dataset.open && !wrap.classList.contains("narrow")) return;
      openId = el.dataset.open; tab = "overview"; draw();
    }));
    left.querySelectorAll("[data-stop]").forEach((el) => el.addEventListener("click", (e) => e.stopPropagation()));
    left.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", async () => {
      b.disabled = true; await (b.dataset.act === "enable" ? enable(b.dataset.id) : disable(b.dataset.id));
    }));
    wireAppButtons(left);
  }

  // ------------------------------------------------------------------ detail (right pane)
  function drawDetail(m) {
    const tabs = [["overview", "Overview"]];
    if (m.features.length) tabs.push(["features", "Features"]);
    if (m.settings_schema.length || m.custom_settings_ui) tabs.push(["settings", "Settings"]);
    if ((m.parts || []).includes("node")) tabs.push(["nodes", "Nodes"]);
    tabs.push(["crashes", `Crashes${m.crash_count ? ` (${m.crash_count})` : ""}`]);
    if (!tabs.some(([k]) => k === tab)) tab = "overview";
    root.innerHTML = `
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap">
        <button class="btn btn-sm" id="mod-back" title="Close details">${wrap.classList.contains("narrow") ? "← All modules" : "✕"}</button>
        <span style="font-size:26px">${esc(m.icon)}</span>
        <div style="flex:1;min-width:160px"><div style="font-size:17px;font-weight:600">${esc(m.name)} <span style="font-weight:400;color:var(--subtext);font-size:12px">v${esc(m.version)}</span></div></div>
        ${badge(m)}
        ${appButtons(m)}
        ${m.available === false ? "" : m.enabled ? `<button class="btn" id="mod-toggle">Disable</button>` : `<button class="btn btn-primary" id="mod-toggle">Enable</button>`}
      </div>
      ${!m.enabled && (m.parts || []).includes("frontend") ? `<div style="font-size:12px;color:var(--subtext);margin:-4px 0 10px">Enable the module to open its app.</div>` : ""}
      <div class="tab-bar" style="padding-bottom:10px;border-bottom:1px solid var(--border);margin-bottom:12px">
        ${tabs.map(([k, l]) => `<button class="tab-btn${k === tab ? " active" : ""}" data-tab="${k}">${l}</button>`).join("")}
      </div>
      <div id="mod-tab"></div>`;
    root.querySelector("#mod-back").onclick = () => { openId = null; draw(); };
    const tg = root.querySelector("#mod-toggle");
    if (tg) tg.onclick = async () => { tg.disabled = true; await (m.enabled ? disable(m.id) : enable(m.id)); };
    root.querySelectorAll("[data-tab]").forEach((b) => b.onclick = () => { tab = b.dataset.tab; drawDetail(m); });
    wireAppButtons(root);
    const box = root.querySelector("#mod-tab");
    ({ overview: tabOverview, features: tabFeatures, settings: tabSettings, nodes: tabNodes, crashes: tabCrashes }[tab])(box, m);
  }

  // ------------------------------------------------------------------ Nodes
  // Install / enable the node part of this module per node - never automatic:
  // a node only holds the modules that are needed there.
  async function tabNodes(box, m) {
    box.innerHTML = `<div style="color:var(--subtext)">Loading nodes …</div>`;
    let nodes = [];
    try { nodes = (await api.listNodes()).nodes || []; }
    catch (e) { box.innerHTML = `<div style="color:var(--danger)">${esc(e.message)}</div>`; return; }
    const host = (n) => (state.clients || []).find((c) => c.id === n.client_id)?.hostname || n.hostname || n.client_id.slice(0, 8);
    const busy = new Set();
    const vtoo = (v) => { try { const p = String(v || "0").split(".").map(Number); return (p[0] || 0) * 1e6 + (p[1] || 0) * 1e3 + (p[2] || 0) < 3000; } catch { return false; } };
    const draw = () => {
      box.innerHTML = `
        <p style="margin:0 0 10px;color:var(--subtext);font-size:12.5px">Install the node part of <b>${esc(m.name)}</b> on single nodes and <b>enable</b> it.
          After installing, the module is off; only after “Enable” can the node be chosen as a target of this module.
          ${m.enabled ? "" : "<b style='color:#f5a524'>The module is off – enable it first.</b>"}</p>
        ${nodes.length ? `<table class="data-table" style="width:100%;font-size:13px"><thead><tr><th style="text-align:left">Node</th><th style="text-align:left">State</th><th style="text-align:left">Module</th><th></th></tr></thead><tbody>
          ${nodes.map((n) => { const mm = (n.modules || {})[m.id]; const on = n.online && n.state !== "off";
            const act = mm && (mm.state === "running" || mm.state === "partial"); const old = vtoo(n.version);
            return `<tr><td>🖧 <b>${esc(host(n))}</b>${old ? ` <span style="font-size:11px;color:#f5a524">node outdated – update it</span>` : n.update_available ? ` <span style="font-size:11px;color:#f5a524">node update available</span>` : ""}</td>
              <td>${on ? "<span style='color:#3ecf8e'>online</span>" : "<span style='color:var(--subtext)'>offline</span>"}</td>
              <td>${!mm ? "<span style='color:var(--subtext)'>not installed</span>"
                : act ? `<span style="color:#3ecf8e">active</span> · v${esc(mm.version || "?")}` : `<span style="color:#f5a524">off</span> · v${esc(mm.version || "?")}`}</td>
              <td style="text-align:right;white-space:nowrap">${busy.has(n.client_id) ? "⏳" : !on ? "" : !mm
                ? `<button class="btn btn-sm" data-inst="${esc(n.client_id)}" ${m.enabled ? "" : "disabled"}>Install</button>`
                : `${act ? `<button class="btn btn-sm" data-off="${esc(n.client_id)}">■ Disable</button>`
                         : `<button class="btn btn-sm btn-primary" data-on="${esc(n.client_id)}" ${old ? "disabled title='Update the node first'" : ""}>▶ Enable</button>`}
                   <button class="btn btn-sm" data-inst="${esc(n.client_id)}" ${m.enabled ? "" : "disabled"} title="Transfer the files again">Reinstall</button>
                   <button class="btn btn-sm btn-danger" data-rem="${esc(n.client_id)}">Remove</button>`}</td></tr>`; }).join("")}
          </tbody></table>` : `<div style="color:var(--subtext)">No nodes yet. Set up a node in the Nodes app.</div>`}`;
      const run = async (cid, fn, ok) => {
        busy.add(cid); draw();
        try { await fn(); window.notify?.(ok, "success", 3000); }
        catch (e) { window.notify?.(`${host(nodes.find((x) => x.client_id === cid) || { client_id: cid })}: ${e.message}`, "error", 8000); }
        busy.delete(cid);
        try { nodes = (await api.listNodes()).nodes || []; } catch { /* egal */ }
        draw();
      };
      box.querySelectorAll("[data-inst]").forEach((b) => b.onclick = () => run(b.dataset.inst, () => api.nodeDeploy(b.dataset.inst, m.id), `${m.name}: installed on the node (off)`));
      box.querySelectorAll("[data-on]").forEach((b) => b.onclick = () => run(b.dataset.on, () => api.nodeModuleAction(b.dataset.on, m.id, "up"), `${m.name}: enabled on the node`));
      box.querySelectorAll("[data-off]").forEach((b) => b.onclick = () => run(b.dataset.off, () => api.nodeModuleAction(b.dataset.off, m.id, "down"), `${m.name}: disabled on the node`));
      box.querySelectorAll("[data-rem]").forEach((b) => b.onclick = async () => {
        if (!(await uiConfirm(`Remove the node part of “${m.name}” from this node?`, { okText: "Remove", danger: true }))) return;
        run(b.dataset.rem, () => api.nodeRemoveModule(b.dataset.rem, m.id), `${m.name}: removed from the node`);
      });
    };
    draw();
  }

  function row(k, v) { return `<tr><td style="padding:4px 14px 4px 0;color:var(--subtext);vertical-align:top;white-space:nowrap">${k}</td><td style="padding:4px 0">${v}</td></tr>`; }

  function tabOverview(box, m) {
    const reqs = m.requires.map((r) => { const d = byId(r); const on = d?.enabled;
      return `<span style="color:${on ? "#3ecf8e" : "#f5a524"}">${on ? "●" : "○"} ${esc(nameOf(r))}</span>`; }).join(", ") || "–";
    const pip = m.pip.length ? m.pip.map((p) => `${esc(p)}${m.pip_missing.some((x) => x === p) ? ` <i style="color:#f5a524">(installed when enabled)</i>` : ""}`).join(", ") : "–";
    const crash = m.status === "crashed" && m.last_crash ? `
      <div style="background:rgba(255,77,109,.1);border:1px solid #ff4d6d;border-radius:8px;padding:10px 12px;margin-bottom:12px;font-size:13px">
        <b>⚠ This module crashed and was disabled.</b><br>
        <span style="color:var(--subtext)">${fmt(m.last_crash.ts)} · ${esc(m.last_crash.where)}</span><br>
        <code style="font-size:12px">${esc(m.last_crash.error)}</code><br>
        <a href="#" id="mod-showcrash" style="font-size:12px">View crash report →</a></div>` : "";
    box.innerHTML = `${crash}<p style="margin:0 0 12px;line-height:1.5">${esc(m.description)}</p>
      <table style="font-size:13px;border-collapse:collapse">
        ${row("ID", `<code>${esc(m.id)}</code>`)}${row("Author", esc(m.author || "–"))}${row("Category", esc(m.category))}
        ${row("Parts", m.parts.map((p) => PART[p] || p).join(", "))}
        ${row("Requires modules", reqs)}${row("Required by", m.required_by.map((r) => esc(nameOf(r))).join(", ") || "–")}
        ${row("Python packages", pip)}${row("Active since", fmt(m.started_at))}
      </table>`;
    box.querySelector("#mod-showcrash")?.addEventListener("click", (e) => { e.preventDefault(); tab = "crashes"; drawDetail(m); });
  }

  function tabFeatures(box, m) {
    box.innerHTML = `<p style="margin:0 0 10px;color:var(--subtext);font-size:12px">Switch the sub-features of this module on or off. ${m.enabled ? "The module is reloaded briefly." : "Takes effect the next time it is enabled."}</p>
      ${m.features.map((f) => `<label style="display:flex;align-items:flex-start;gap:10px;padding:8px 0;border-bottom:1px solid var(--border);cursor:pointer">
        <input type="checkbox" data-feat="${f.id}" ${f.enabled ? "checked" : ""} style="margin-top:3px">
        <span><b>${esc(f.label)}</b>${f.description ? `<br><span style="font-size:12px;color:var(--subtext)">${esc(f.description)}</span>` : ""}</span></label>`).join("")}`;
    box.querySelectorAll("[data-feat]").forEach((c) => c.addEventListener("change", async () => {
      c.disabled = true;
      try { await req(`/api/modules/${m.id}/features`, { method: "PUT", body: JSON.stringify({ features: { [c.dataset.feat]: c.checked } }) });
            window.notify?.("Saved", "success", 1500); await loadModules(); }
      catch (e) { window.notify?.(e.message, "error", 10000); c.checked = !c.checked; }
      await load();
    }));
  }

  function field(f, val) {
    const id = `ms-${f.key}`, base = `width:100%;max-width:420px`;
    const opts = (f.options || []).map((o) => (typeof o === "object" ? o : { value: o, label: o }));
    let input;
    if (f.type === "bool") input = `<label style="display:flex;gap:8px;align-items:center"><input type="checkbox" id="${id}" ${val ? "checked" : ""}> ${esc(f.label)}</label>`;
    else if (f.type === "select") input = `<select id="${id}" style="${base}">${opts.map((o) => `<option value="${esc(o.value)}" ${o.value === val ? "selected" : ""}>${esc(o.label)}</option>`).join("")}</select>`;
    else if (f.type === "text") input = `<textarea id="${id}" rows="4" style="${base}">${esc(val ?? "")}</textarea>`;
    else input = `<input id="${id}" type="${f.type === "password" ? "password" : f.type === "number" ? "number" : "text"}" value="${esc(val ?? "")}" ${f.placeholder ? `placeholder="${esc(f.placeholder)}"` : ""} ${f.min != null ? `min="${f.min}"` : ""} ${f.max != null ? `max="${f.max}"` : ""} style="${base}">`;
    return `<div class="form-row" style="margin-bottom:12px">${f.type === "bool" ? "" : `<label for="${id}">${esc(f.label)}</label>`}${input}
      ${f.description || f.help ? `<div style="font-size:11px;color:var(--subtext);margin-top:3px">${esc(f.description || f.help)}</div>` : ""}</div>`;
  }

  async function tabSettings(box, m) {
    box.innerHTML = "";
    if (m.settings_schema.length) {
      const form = document.createElement("div");
      form.innerHTML = m.settings_schema.map((f) => field(f, m.settings[f.key])).join("") +
        `<button class="btn-primary" id="ms-save">Save</button> <span id="ms-msg" style="font-size:12px;margin-left:8px"></span>`;
      box.appendChild(form);
      form.querySelector("#ms-save").onclick = async () => {
        const out = {};
        for (const f of m.settings_schema) {
          const el = form.querySelector(`#ms-${f.key}`);
          out[f.key] = f.type === "bool" ? el.checked : el.value;
        }
        const msg = form.querySelector("#ms-msg");
        try { await req(`/api/modules/${m.id}/settings`, { method: "PUT", body: JSON.stringify({ settings: out }) });
              msg.style.color = "#3ecf8e"; msg.textContent = "✓ saved"; await load(); }
        catch (e) { msg.style.color = "var(--danger)"; msg.textContent = e.message; }
      };
    }
    if (m.custom_settings_ui) {
      const fm = getModules().find((x) => x.id === m.id);
      const holder = document.createElement("div");
      holder.style.marginTop = m.settings_schema.length ? "22px" : "0";
      box.appendChild(holder);
      if (!fm?.settings_ui) { holder.innerHTML = `<div style="color:var(--subtext);font-size:13px">Enable the module to see its settings.</div>`; return; }
      try {
        const mod = await import(/* @vite-ignore */ fm.base + fm.settings_ui.entry);
        await mod[fm.settings_ui.export || "renderSettings"](holder, { module: fm, req });
      } catch (e) {
        holder.innerHTML = `<div style="color:var(--danger);font-size:13px">The settings could not be loaded: ${esc(e.message)}</div>`;
        reportModuleCrash(m.id, "settings menu", e, true);
      }
    }
  }

  async function tabCrashes(box, m) {
    box.innerHTML = `<div style="color:var(--subtext)">Loading …</div>`;
    let list = [];
    try { list = (await req(`/api/modules/${m.id}/crashes`)).crashes; } catch (e) { box.innerHTML = `<div style="color:var(--danger)">${esc(e.message)}</div>`; return; }
    if (!list.length) { box.innerHTML = `<div style="color:var(--subtext)">No crashes recorded. 👍</div>`; return; }
    box.innerHTML = `<div style="display:flex;justify-content:flex-end;margin-bottom:8px"><button class="btn btn-danger" id="mc-clear">Delete all</button></div>` +
      list.map((c, i) => `<details style="background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:8px 10px;margin-bottom:8px" ${i === 0 ? "open" : ""}>
        <summary style="cursor:pointer"><b>${fmt(c.ts)}</b> · ${esc(c.where || "")} <span style="color:var(--subtext)">[${esc(c.source || "backend")}${c.disabled === false ? ", logged only" : ""}]</span><br>
          <code style="font-size:12px;color:#ff4d6d">${esc(c.error || "")}</code></summary>
        <pre style="margin:8px 0 0;font-size:11px;white-space:pre-wrap;max-height:260px;overflow:auto;background:var(--panel-2);padding:8px;border-radius:6px">${esc(c.traceback || "(no traceback)")}</pre></details>`).join("");
    box.querySelector("#mc-clear").onclick = async () => {
      if (!(await uiConfirm("Delete the crash reports?", { okText: "Delete" }))) return;
      await req(`/api/modules/${m.id}/crashes`, { method: "DELETE" }); await load();
    };
  }

  function draw() {
    const m = openId && byId(openId);
    if (!m) openId = null;
    wrap.classList.toggle("split", !!m);
    wrap.classList.toggle("narrow", wrap.clientWidth < NARROW);
    drawList();
    if (m) drawDetail(m); else root.innerHTML = "";
  }
  load();
}
