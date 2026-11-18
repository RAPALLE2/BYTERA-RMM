// dashboard.js (module "proxmox") - Proxmox dashboard of an agent
// -----------------------------------------------------------------
// Tabs (depending on permission): Overview | VMs | LXC | Storage | Users | Tasks |
// Shares | API explorer. Click a guest -> guest view (status, actions,
// configuration, snapshots, backups). Backend: proxmox_routes.py (/api/proxmox/...)

import { state } from "/js/app/state.js";
import { esc, uiConfirm, uiPrompt } from "/js/app/utils.js";
import { bar, dot, fmtBytes, fmtUptime, pct, preq, STATUS } from "./common.js";
import { openConnectionDialog, showHelp } from "./connect.js";
import { openSuggestDialog } from "./suggest.js";
import { icon } from "./icons.js";
import * as M from "./manage.js";

export function renderProxmox(body, win) {
  const cid = win?.props?.clientId;
  body.innerHTML = `<div class="pve" style="height:100%;display:flex;flex-direction:column;font-size:13px;min-height:0"></div>`;
  const root = body.firstElementChild;
  if (!cid) return renderPicker(root);
  return renderDashboard(root, cid, win?.props?.guest || null);
}

// ------------------------------------------------------------------ Pick an agent (start menu)
async function renderPicker(root) {
  root.innerHTML = `<div style="padding:16px;overflow:auto">Loading …</div>`;
  let r;
  try { r = await preq("/clients"); } catch (e) { root.innerHTML = `<div style="padding:16px;color:var(--danger)">${esc(e.message)}</div>`; return; }
  root.innerHTML = `<div style="padding:16px;overflow:auto">
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
      <b style="font-size:15px;flex:1;display:flex;align-items:center;gap:6px">${icon("cluster", 16)} Proxmox instances</b>
      <button class="btn-tool" data-help title="Guide">${icon("help", 15)}</button></div>
    ${r.clients.length ? r.clients.map((c) => `
      <div data-open="${esc(c.client_id)}" style="display:flex;align-items:center;gap:10px;padding:10px 12px;border:1px solid var(--border);
           border-radius:8px;margin-bottom:6px;cursor:pointer">
        <span style="display:flex">${icon("cluster", 20)}</span>
        <div style="flex:1"><b>${esc(c.label || c.hostname || c.client_id)}</b>
          <div style="font-size:11.5px;color:var(--subtext)">Agent ${esc(c.hostname || "")} · ${{ local: "local address", custom: "custom address", relay: "RMM relay" }[c.addr_mode] || ""}</div></div>
        <span style="color:var(--subtext)">›</span></div>`).join("")
      : `<div style="color:var(--subtext)">No Proxmox instance connected yet (or no access). To connect, click
         "Connect Proxmox" in the agent panel.</div>`}</div>`;
  root.querySelector("[data-help]").addEventListener("click", showHelp);
  root.querySelectorAll("[data-open]").forEach((el) => el.addEventListener("click", () => renderDashboard(root, el.dataset.open)));
}

// ------------------------------------------------------------------ Dashboard
async function renderDashboard(root, cid, initialGuest = null) {
  const C = encodeURIComponent(cid);
  const client = (state.clients || []).find((c) => c.id === cid) || { id: cid, hostname: cid };
  root.innerHTML = `
    <div style="display:flex;align-items:center;gap:8px;padding:10px 12px;border-bottom:1px solid var(--border)">
      <span style="display:flex">${icon("cluster", 18)}</span>
      <div style="flex:1;min-width:0"><b data-title>Proxmox</b>
        <div data-sub style="font-size:11.5px;color:var(--subtext)">Agent ${esc(client.hostname || "")}</div></div>
      <button class="btn-tool" data-reload title="Refresh">${icon("refresh", 15)}</button>
      <button class="btn-tool" data-conn title="Connection" style="display:none">${icon("gear", 15)}</button>
      <button class="btn-tool" data-help title="Guide">${icon("help", 15)}</button>
    </div>
    <div class="tab-bar" data-tabs style="padding:8px 12px 0;flex-wrap:wrap"></div>
    <div data-pane style="flex:1;overflow:auto;padding:12px;min-height:0">Loading …</div>`;
  const pane = root.querySelector("[data-pane]");
  const tabsEl = root.querySelector("[data-tabs]");
  root.querySelector("[data-help]").addEventListener("click", showHelp);
  let ov = null;
  let tab = "overview";
  const err = (e) => `<div style="color:var(--danger);padding:6px 0">${esc(e.message || e)}</div>`;

  async function load() {
    pane.innerHTML = "Loading …";
    try { ov = await preq(`/${C}/overview`); }
    catch (e) {
      pane.innerHTML = err(e) + `<div style="color:var(--subtext);margin-top:6px">Check the connection: gear icon top right (permission "manage Proxmox").</div>`;
      try { const conn = await preq(`/${C}/connection`); if (conn) root.querySelector("[data-conn]").style.display = ""; } catch { /* no permission */ }
      return;
    }
    const P = ov.perms;
    root.querySelector("[data-title]").textContent = `Proxmox VE ${ov.version || ""}`;
    root.querySelector("[data-sub]").textContent = `Agent ${client.hostname || ""} · ${ov.nodes.length} Node(s)`;
    root.querySelector("[data-conn]").style.display = P.pve_manage ? "" : "none";
    const tabs = [["overview", "Overview"], ["qemu", `VMs (${ov.counts.vms})`], ["lxc", `LXC (${ov.counts.lxc})`],
      P.pve_storage && ["storage", "Storage"], P.pve_users && ["users", "Users"], P.pve_nodes && ["tasks", "Tasks"],
      P.pve_manage && ["acl", "Shares"], P.pve_manage && ["match", "Mapping"], P.pve_api && ["api", "API explorer"]].filter(Boolean);
    tabsEl.innerHTML = tabs.map(([k, l]) => `<button class="tab-btn ${k === tab ? "active" : ""}" data-t="${k}">${esc(l)}</button>`).join("");
    tabsEl.querySelectorAll("[data-t]").forEach((b) => b.addEventListener("click", () => { tab = b.dataset.t; show(); }));
    if (initialGuest) {                          // from the agent panel "Open in Proxmox": straight to the guest
      const g = initialGuest; initialGuest = null;
      tab = g.type === "lxc" ? "lxc" : "qemu";
      tabsEl.querySelectorAll("[data-t]").forEach((b) => b.classList.toggle("active", b.dataset.t === tab));
      drawGuest(g);
      return;
    }
    show();
  }
  function show() {
    tabsEl.querySelectorAll("[data-t]").forEach((b) => b.classList.toggle("active", b.dataset.t === tab));
    ({ overview: drawOverview, qemu: () => drawGuests("qemu"), lxc: () => drawGuests("lxc"), storage: drawStorage,
       users: drawUsers, tasks: drawTasks, acl: drawAcl, match: drawMatch, api: drawApi })[tab]?.();
  }
  root.querySelector("[data-reload]").addEventListener("click", load);
  root.querySelector("[data-conn]").addEventListener("click", () => openConnectionDialog(client, (ch) => ch && load()));

  // -------------------------------------------------------------- Track tasks
  async function followTask(node, upid, label) {
    if (!upid || typeof upid !== "string" || !upid.startsWith("UPID:")) { window.notify?.(`${label}: triggered`, "success"); return; }
    window.notify?.(`${label}: started …`, "info");
    for (let i = 0; i < 40; i++) {
      await new Promise((r) => setTimeout(r, 1500));
      try {
        const t = await preq(`/${C}/tasks/${encodeURIComponent(node)}/${encodeURIComponent(upid)}`);
        if (t.status.status === "stopped") {
          const ok = t.status.exitstatus === "OK";
          window.notify?.(`${label}: ${ok ? "succeeded" : `error - ${t.status.exitstatus}`}`, ok ? "success" : "error", ok ? 4000 : 10000);
          return;
        }
      } catch { return; }
    }
  }

  // -------------------------------------------------------------- Overview
  function drawOverview() {
    const n = ov.nodes, c = ov.counts;
    pane.innerHTML = `
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:8px;margin-bottom:14px">
        ${[["VMs", `${c.vms_running} / ${c.vms}`, "running"], ["LXC", `${c.lxc_running} / ${c.lxc}`, "running"], ["Nodes", n.length, "in the cluster"]]
          .map(([k, v, s]) => `<div style="border:1px solid var(--border);border-radius:8px;padding:10px">
            <div style="font-size:11px;color:var(--subtext)">${k}</div><div style="font-size:20px;font-weight:700">${v}</div>
            <div style="font-size:11px;color:var(--subtext)">${s}</div></div>`).join("")}
      </div>
      <h4 style="margin:0 0 6px">Nodes</h4>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:8px;margin-bottom:14px">
        ${n.map((x) => `<div style="border:1px solid var(--border);border-radius:8px;padding:10px">
          <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">${dot(x.status === "online" ? "running" : "stopped")}
            <b>${esc(x.node)}</b><span style="flex:1"></span><span style="font-size:11px;color:var(--subtext)">${fmtUptime(x.uptime)}</span></div>
          <div style="font-size:11.5px;color:var(--subtext)">CPU ${Math.round((x.cpu || 0) * 100)} % of ${x.maxcpu || "?"} cores</div>${bar(Math.round((x.cpu || 0) * 100))}
          <div style="font-size:11.5px;color:var(--subtext);margin-top:6px">RAM ${fmtBytes(x.mem)} / ${fmtBytes(x.maxmem)}</div>${bar(pct(x.mem, x.maxmem))}
        </div>`).join("")}
      </div>
      ${ov.storage.length ? `<h4 style="margin:0 0 6px">Storage</h4>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:8px">
        ${ov.storage.map((s) => `<div style="border:1px solid var(--border);border-radius:8px;padding:10px">
          <b>${esc(s.storage)}</b> <span style="font-size:11px;color:var(--subtext)">${esc(s.node)} · ${esc(s.plugintype || "")}</span>
          <div style="font-size:11.5px;color:var(--subtext);margin:4px 0">${fmtBytes(s.disk)} / ${fmtBytes(s.maxdisk)}</div>${bar(pct(s.disk, s.maxdisk))}
        </div>`).join("")}</div>` : ""}`;
  }

  // -------------------------------------------------------------- Guests
  let search = "";
  async function drawGuests(type) {
    pane.innerHTML = "Loading …";
    let r, agents = {};
    try { r = await preq(`/${C}/guests?type=${type}`); } catch (e) { pane.innerHTML = err(e); return; }
    try {                                         // which agent runs in which guest?
      const gm = await preq("/guest-map");
      for (const [acid, m] of Object.entries(gm.map || {})) if (m.host === cid) agents[m.vmid] = (state.clients || []).find((c) => c.id === acid)?.hostname || acid;
    } catch { /* ignore */ }
    const P = ov.perms;
    const render = () => {
      const q = search.toLowerCase();
      const list = r.guests.filter((g) => !q || `${g.vmid} ${g.name} ${g.node} ${g.tags || ""}`.toLowerCase().includes(q));
      pane.querySelector("[data-list]").innerHTML = list.length ? `<table class="data-table" style="width:100%"><thead><tr>
          <th></th><th style="text-align:left">ID</th><th style="text-align:left">Name</th><th>Node</th><th>Agent</th><th>CPU</th><th>RAM</th><th>Uptime</th>
          ${P.pve_power ? "<th></th>" : ""}</tr></thead><tbody>
        ${list.map((g) => `<tr data-g="${g.vmid}" style="cursor:pointer">
          <td>${dot(g.status)}</td><td>${g.vmid}</td><td><b>${esc(g.name || "")}</b>${g.template ? ` <span style="font-size:10.5px;color:var(--subtext)">template</span>` : ""}
            ${g.lock ? ` <span style="font-size:10.5px;color:var(--warn);display:inline-flex;align-items:center;gap:3px">${icon("lock", 12)} ${esc(g.lock)}</span>` : ""}</td>
          <td>${esc(g.node)}</td><td>${agents[g.vmid] ? `<span style="display:inline-flex;align-items:center;gap:3px">${icon("link", 13)} ${esc(agents[g.vmid])}</span>` : `<span style="color:var(--subtext)">–</span>`}</td>
          <td>${g.status === "running" ? Math.round((g.cpu || 0) * 100) + " %" : "–"}</td>
          <td>${g.status === "running" ? `${fmtBytes(g.mem)} / ${fmtBytes(g.maxmem)}` : fmtBytes(g.maxmem)}</td><td>${fmtUptime(g.uptime)}</td>
          ${P.pve_power ? `<td style="white-space:nowrap;text-align:right">${g.status === "running"
            ? `<button class="btn-tool" data-a="shutdown" title="Shut down">${icon("power", 15)}</button><button class="btn-tool" data-a="reboot" title="Reboot">${icon("reboot", 15)}</button><button class="btn-tool" data-a="stop" title="Stop now">${icon("stop", 15)}</button>`
            : `<button class="btn-tool" data-a="start" title="Start">${icon("play", 15)}</button>`}</td>` : ""}</tr>`).join("")}
        </tbody></table>` : `<div style="color:var(--subtext)">${r.guests.length ? "No matches." : `No ${type === "qemu" ? "VMs" : "containers"} visible.`}</div>`;
      pane.querySelectorAll("[data-g]").forEach((tr) => {
        const g = r.guests.find((x) => String(x.vmid) === tr.dataset.g);
        tr.addEventListener("click", (e) => { if (!e.target.closest("[data-a]")) drawGuest(g); });
        tr.querySelectorAll("[data-a]").forEach((b) => b.addEventListener("click", () => guestAction(g, b.dataset.a, () => drawGuests(type))));
      });
    };
    pane.innerHTML = `<div style="display:flex;gap:8px;margin-bottom:8px;align-items:center">
        <input type="text" data-q placeholder="Search (ID, name, node, tag) …" value="${esc(search)}" style="flex:1;max-width:340px">
        ${P.pve_config ? `<button class="btn-primary btn-sm" data-create>${icon("plus", 14)} ${type === "qemu" ? "VM" : "Container"}</button>` : ""}</div>
      <div data-list></div>`;
    pane.querySelector("[data-q]").addEventListener("input", (e) => { search = e.target.value; render(); });
    pane.querySelector("[data-create]")?.addEventListener("click", () => M.createDialog(C, type, () => drawGuests(type)));
    render();
  }

  const ACTION_TEXT = { start: "Start", stop: "Stop now", shutdown: "Shut down", reboot: "Reboot",
                        suspend: "Suspend", resume: "Resume", reset: "Hard reset" };
  async function guestAction(g, action, after) {
    if (["stop", "reset"].includes(action) && !(await uiConfirm(`${g.name || g.vmid}: ${ACTION_TEXT[action]}?`,
      { description: "Like pulling the plug - unsaved data in the guest is lost.", okText: ACTION_TEXT[action], danger: true }))) return;
    try {
      const res = await preq(`/${C}/guests/${encodeURIComponent(g.node)}/${g.type}/${g.vmid}/action/${action}`, { method: "POST" });
      followTask(g.node, res.upid, `${g.name || g.vmid}: ${ACTION_TEXT[action]}`).then(() => after?.());
      setTimeout(() => after?.(), 1500);
    } catch (e) { window.notify?.(e.message, "error"); }
  }

  // -------------------------------------------------------------- Guest view
  async function drawGuest(g, sub = "status") {
    const P = ov.perms;
    const base = `/${C}/guests/${encodeURIComponent(g.node)}/${g.type}/${g.vmid}`;
    pane.innerHTML = "Lade …";
    let d;
    try { d = await preq(base); } catch (e) { pane.innerHTML = err(e); return; }
    const st = d.status, running = st.status === "running";
    const subs = [["status", "Status"], ["config", "Configuration"], P.pve_snapshot && ["snap", "Snapshots"], P.pve_backup && ["backup", "Backups"]].filter(Boolean);
    const acts = !P.pve_power ? [] : running
      ? ["shutdown", "reboot", ...(g.type === "qemu" ? ["suspend"] : []), "stop", ...(g.type === "qemu" ? ["reset"] : [])]
      : st.status === "paused" || st.qmpstatus === "paused" ? ["resume", "stop"] : ["start"];
    pane.innerHTML = `
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:10px">
        <button class="btn btn-sm" data-back>${icon("back", 13)} Back</button>
        ${dot(st.status)} <b style="font-size:15px">${esc(st.name || g.name || "")}</b>
        <span style="color:var(--subtext)">${g.type === "qemu" ? "VM" : "LXC"} ${g.vmid} · ${esc(g.node)} · ${(STATUS[st.status] || {}).t || esc(st.status)}</span>
        <span style="flex:1"></span>
        ${acts.map((a) => `<button class="btn btn-sm ${["stop", "reset"].includes(a) ? "btn-danger" : a === "start" ? "btn-primary" : ""}" data-act="${a}">${ACTION_TEXT[a]}</button>`).join("")}
        ${P.pve_config ? `<button class="btn btn-sm" data-manage>${icon("gear", 13)} Manage</button>` : ""}
        ${P.pve_console ? `<span data-console-slot></span>` : ""}
      </div>
      <div class="tab-bar" style="margin-bottom:10px">${subs.map(([k, l]) => `<button class="tab-btn ${k === sub ? "active" : ""}" data-sub="${k}">${l}</button>`).join("")}</div>
      <div data-sub-pane></div>`;
    pane.querySelector("[data-back]").addEventListener("click", () => drawGuests(g.type));
    pane.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () => guestAction(g, b.dataset.act, () => drawGuest(g, sub))));
    pane.querySelectorAll("[data-sub]").forEach((b) => b.addEventListener("click", () => drawGuest(g, b.dataset.sub)));
    pane.querySelector("[data-manage]")?.addEventListener("click", () => M.guestMenu(C, g, () => drawGuests(g.type)));
    const sp = pane.querySelector("[data-sub-pane]");
    window.dispatchEvent(new CustomEvent("rmm-pve-guest", { detail: { slot: pane.querySelector("[data-console-slot]"), cid, guest: g, running } }));

    if (sub === "status") {
      sp.innerHTML = `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px">
        ${[["CPU", running ? `${Math.round((st.cpu || 0) * 100)} % of ${st.cpus || st.maxcpu || "?"}` : "–", running ? bar(Math.round((st.cpu || 0) * 100)) : ""],
           ["RAM", `${fmtBytes(st.mem)} / ${fmtBytes(st.maxmem)}`, bar(pct(st.mem, st.maxmem))],
           ["Disk", st.maxdisk ? `${fmtBytes(st.disk)} / ${fmtBytes(st.maxdisk)}` : "–", st.maxdisk ? bar(pct(st.disk, st.maxdisk)) : ""],
           ["Uptime", fmtUptime(st.uptime), ""], ["Network", `↓ ${fmtBytes(st.netin)} · ↑ ${fmtBytes(st.netout)}`, ""],
           ["HA / lock", `${esc(st.ha?.state || "–")} / ${esc(st.lock || "–")}`, ""]]
          .map(([k, v, b]) => `<div style="border:1px solid var(--border);border-radius:8px;padding:10px"><div style="font-size:11px;color:var(--subtext)">${k}</div>
            <div style="font-weight:600;margin:2px 0 4px">${v}</div>${b}</div>`).join("")}</div>`;
    } else if (sub === "config") {
      const keys = Object.keys(d.config).filter((k) => k !== "digest").sort();
      sp.innerHTML = `<table class="data-table" style="width:100%"><thead><tr><th style="text-align:left">Key</th><th style="text-align:left">Value</th>${P.pve_config ? "<th></th>" : ""}</tr></thead>
        <tbody>${keys.map((k) => `<tr><td><code>${esc(k)}</code></td><td style="word-break:break-all"><code>${esc(String(d.config[k]))}</code></td>
          ${P.pve_config ? `<td style="white-space:nowrap;text-align:right"><button class="btn-tool" data-edit="${esc(k)}" title="Edit">${icon("edit", 14)}</button><button class="btn-tool" data-rm="${esc(k)}" title="Remove">${icon("trash", 14)}</button></td>` : ""}</tr>`).join("")}
        </tbody></table>
        ${P.pve_config ? `<div style="margin-top:8px"><button class="btn btn-sm" data-add>${icon("plus", 13)} Add entry</button>
          <span style="color:var(--subtext);font-size:11.5px;margin-left:6px">Some changes only take effect after the guest restarts (Proxmox: "pending").</span></div>` : ""}`;
      const put = async (params, del = []) => {
        try { await preq(`${base}/config`, { method: "PUT", body: { params, delete: del } }); window.notify?.("Configuration saved", "success"); drawGuest(g, "config"); }
        catch (e) { window.notify?.(e.message, "error"); }
      };
      sp.querySelectorAll("[data-edit]").forEach((b) => b.addEventListener("click", async () => {
        const k = b.dataset.edit;
        const v = await uiPrompt(`Edit ${k}`, { value: String(d.config[k]) });
        if (v !== null && v !== undefined && v !== String(d.config[k])) put({ [k]: v });
      }));
      sp.querySelectorAll("[data-rm]").forEach((b) => b.addEventListener("click", async () => {
        if (await uiConfirm(`Remove "${b.dataset.rm}" from the configuration?`, { okText: "Remove", danger: true })) put({}, [b.dataset.rm]);
      }));
      sp.querySelector("[data-add]")?.addEventListener("click", async () => {
        const k = await uiPrompt("Key (e.g. description, tags, onboot, cores, memory)", { placeholder: "description" });
        if (!k) return;
        const v = await uiPrompt(`Value for ${k}`, {});
        if (v !== null && v !== undefined) put({ [k.trim()]: v });
      });
    } else if (sub === "snap") {
      sp.innerHTML = "Loading …";
      let s;
      try { s = await preq(`${base}/snapshots`); } catch (e) { sp.innerHTML = err(e); return; }
      const snaps = s.snapshots.filter((x) => x.name !== "current").sort((a, b) => (b.snaptime || 0) - (a.snaptime || 0));
      sp.innerHTML = `<div style="margin-bottom:8px"><button class="btn-primary btn-sm" data-new>${icon("plus", 14)} Create snapshot</button></div>
        ${snaps.length ? `<table class="data-table" style="width:100%"><thead><tr><th style="text-align:left">Name</th><th style="text-align:left">Description</th><th>Time</th><th></th></tr></thead>
        <tbody>${snaps.map((x) => `<tr><td><b>${esc(x.name)}</b>${x.vmstate ? ` ${icon("save", 12)}` : ""}</td><td>${esc(x.description || "")}</td>
          <td>${x.snaptime ? new Date(x.snaptime * 1000).toLocaleString() : "–"}</td>
          <td style="white-space:nowrap;text-align:right"><button class="btn btn-sm" data-rb="${esc(x.name)}">Roll back</button>
            <button class="btn btn-sm btn-danger" data-del="${esc(x.name)}">Delete</button></td></tr>`).join("")}</tbody></table>`
        : `<div style="color:var(--subtext)">No snapshots yet.</div>`}`;
      sp.querySelector("[data-new]").addEventListener("click", async () => {
        const name = await uiPrompt("Snapshot name", { placeholder: "before_update" });
        if (!name) return;
        const desc = (await uiPrompt("Description (optional)", {})) || "";
        try { const r = await preq(`${base}/snapshots`, { method: "POST", body: { name: name.trim(), description: desc } });
          await followTask(g.node, r.upid, `Snapshot ${name}`); drawGuest(g, "snap"); }
        catch (e) { window.notify?.(e.message, "error"); }
      });
      sp.querySelectorAll("[data-rb]").forEach((b) => b.addEventListener("click", async () => {
        if (!(await uiConfirm(`Roll back to snapshot "${b.dataset.rb}"?`, { description: "The current state since the snapshot is lost.", okText: "Roll back", danger: true }))) return;
        try { const r = await preq(`${base}/snapshots/${encodeURIComponent(b.dataset.rb)}/rollback`, { method: "POST" });
          await followTask(g.node, r.upid, `Roll back to ${b.dataset.rb}`); drawGuest(g, "snap"); }
        catch (e) { window.notify?.(e.message, "error"); }
      }));
      sp.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
        if (!(await uiConfirm(`Delete snapshot "${b.dataset.del}"?`, { okText: "Delete", danger: true }))) return;
        try { const r = await preq(`${base}/snapshots/${encodeURIComponent(b.dataset.del)}`, { method: "DELETE" });
          await followTask(g.node, r.upid, `Delete snapshot ${b.dataset.del}`); drawGuest(g, "snap"); }
        catch (e) { window.notify?.(e.message, "error"); }
      }));
    } else if (sub === "backup") {
      sp.innerHTML = "Loading …";
      let b;
      try { b = await preq(`${base}/backups`); } catch (e) { sp.innerHTML = err(e); return; }
      sp.innerHTML = `<div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:8px">
          <select data-store>${b.storages.map((s) => `<option>${esc(s)}</option>`).join("")}</select>
          <select data-mode><option value="snapshot">Snapshot (running)</option><option value="suspend">Suspend</option><option value="stop">Stop</option></select>
          <button class="btn-primary btn-sm" data-run ${b.storages.length ? "" : "disabled"}>Back up now</button></div>
        ${b.backups.length ? `<table class="data-table" style="width:100%"><thead><tr><th style="text-align:left">File</th><th>Storage</th><th>Size</th><th>Time</th></tr></thead>
        <tbody>${b.backups.map((x) => `<tr><td style="word-break:break-all"><code>${esc(x.volid)}</code>${x.notes ? `<div style="font-size:11px;color:var(--subtext)">${esc(x.notes)}</div>` : ""}</td>
          <td>${esc(x.storage)}</td><td>${fmtBytes(x.size)}</td><td>${x.ctime ? new Date(x.ctime * 1000).toLocaleString() : "–"}</td></tr>`).join("")}</tbody></table>`
        : `<div style="color:var(--subtext)">${b.storages.length ? "No backups yet." : "No storage for backups on this node."}</div>`}`;
      sp.querySelector("[data-run]").addEventListener("click", async () => {
        try { const r = await preq(`${base}/backups`, { method: "POST", body: { storage: sp.querySelector("[data-store]").value, mode: sp.querySelector("[data-mode]").value } });
          await followTask(g.node, r.upid, `Backup ${g.name || g.vmid}`); drawGuest(g, "backup"); }
        catch (e) { window.notify?.(e.message, "error"); }
      });
    }
  }

  // -------------------------------------------------------------- Storage
  async function drawStorage() {
    pane.innerHTML = "Loading …";
    let r;
    try { r = await preq(`/${C}/storage`); } catch (e) { pane.innerHTML = err(e); return; }
    pane.innerHTML = `<table class="data-table" style="width:100%"><thead><tr><th style="text-align:left">Storage</th><th>Node</th><th>Type</th><th style="text-align:left">Content</th><th style="width:180px">Used</th></tr></thead>
      <tbody>${r.storage.map((s) => `<tr data-s="${esc(s.storage)}" data-n="${esc(s.node)}" data-ct="${esc(s.content || "")}" style="cursor:pointer"><td>${dot(s.status === "available" ? "running" : "stopped")} <b>${esc(s.storage)}</b></td>
        <td>${esc(s.node)}</td><td>${esc(s.plugintype || "")}</td><td style="font-size:11.5px">${esc(s.content || "")}</td>
        <td><div style="font-size:11px;color:var(--subtext)">${fmtBytes(s.disk)} / ${fmtBytes(s.maxdisk)}</div>${bar(pct(s.disk, s.maxdisk))}</td></tr>`).join("")}</tbody></table>
      <div data-content style="margin-top:12px"></div>`;
    const canStore = ov.perms.pve_storage;
    pane.querySelectorAll("[data-s]").forEach((tr) => tr.addEventListener("click", async () => {
      const box = pane.querySelector("[data-content]");
      const node = tr.dataset.n, store = tr.dataset.s, ct = tr.dataset.ct;
      const reload = () => tr.dispatchEvent(new Event("click"));
      box.innerHTML = "Loading …";
      try {
        const c = await preq(`/${C}/storage/${encodeURIComponent(node)}/${encodeURIComponent(store)}/content`);
        const actions = canStore ? M.storageActions(C, node, store, ct, reload) : { html: "", wire: () => {} };
        box.innerHTML = `<h4 style="margin:0 0 6px">Content of ${esc(store)} (${esc(node)})</h4>${actions.html}
          ${c.content.length ? `<table class="data-table" style="width:100%"><thead><tr><th style="text-align:left">Volume</th><th>Kind</th><th>Size</th><th>Guest</th>${canStore ? "<th></th>" : ""}</tr></thead>
          <tbody>${c.content.map((x) => `<tr><td style="word-break:break-all"><code>${esc(x.volid)}</code></td><td>${esc(x.content || "")}</td><td>${fmtBytes(x.size)}</td><td>${esc(String(x.vmid || ""))}</td>${canStore ? `<td style="text-align:right"><button class="btn-tool" data-del="${esc(x.volid)}" title="Delete">${icon("trash", 14)}</button></td>` : ""}</tr>`).join("")}</tbody></table>`
          : `<div style="color:var(--subtext)">Empty.</div>`}`;
        actions.wire(box);
        box.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", () => M.deleteVolume(C, node, store, b.dataset.del, reload)));
      } catch (e) { box.innerHTML = err(e); }
    }));
  }

  // -------------------------------------------------------------- Users
  async function drawUsers() {
    pane.innerHTML = "Loading …";
    let r;
    try { r = await preq(`/${C}/users`); } catch (e) { pane.innerHTML = err(e); return; }
    const tbl = (rows, cols) => rows.length ? `<table class="data-table" style="width:100%;margin-bottom:12px"><thead><tr>${cols.map((c) => `<th style="text-align:left">${c[1]}</th>`).join("")}</tr></thead>
      <tbody>${rows.map((x) => `<tr>${cols.map((c) => `<td>${esc(String(x[c[0]] ?? ""))}</td>`).join("")}</tr>`).join("")}</tbody></table>` : `<div style="color:var(--subtext);margin-bottom:12px">None.</div>`;
    pane.innerHTML = `<h4 style="margin:0 0 6px">Users</h4>${tbl(r.users.map((u) => ({ ...u, enable: u.enable === 0 ? "no" : "yes" })), [["userid", "User"], ["firstname", "First name"], ["lastname", "Last name"], ["email", "Email"], ["enable", "Enabled"]])}
      <h4 style="margin:0 0 6px">Groups</h4>${tbl(r.groups, [["groupid", "Group"], ["comment", "Comment"], ["users", "Members"]])}
      <h4 style="margin:0 0 6px">Permissions (ACL)</h4>${tbl(r.acl, [["path", "Path"], ["ugid", "User/group"], ["type", "Type"], ["roleid", "Role"], ["propagate", "Propagate"]])}
      <div style="color:var(--subtext);font-size:11.5px">Creating/editing users is done via the API explorer (e.g. <code>POST access/users</code>) - permission "arbitrary API calls".</div>`;
  }

  // -------------------------------------------------------------- Tasks
  async function drawTasks() {
    pane.innerHTML = "Loading …";
    let r;
    try { r = await preq(`/${C}/tasks?limit=100`); } catch (e) { pane.innerHTML = err(e); return; }
    pane.innerHTML = r.tasks.length ? `<table class="data-table" style="width:100%"><thead><tr><th style="text-align:left">Start</th><th>Node</th><th>Type</th><th>ID</th><th>User</th><th>Status</th></tr></thead>
      <tbody>${r.tasks.map((x) => `<tr data-u="${esc(x.upid)}" data-n="${esc(x.node || "")}" style="cursor:pointer"><td>${x.starttime ? new Date(x.starttime * 1000).toLocaleString() : "–"}</td>
        <td>${esc(x.node || "")}</td><td>${esc(x.type || "")}</td><td>${esc(String(x.id || ""))}</td><td>${esc(x.user || "")}</td>
        <td style="color:${x.status && x.status !== "OK" ? "var(--danger)" : "var(--accent)"}">${esc(x.status || "running …")}</td></tr>`).join("")}</tbody></table>
      <pre data-log style="display:none;margin-top:10px;background:var(--panel-2);border:1px solid var(--border);border-radius:8px;padding:10px;max-height:300px;overflow:auto;font-size:11.5px"></pre>`
      : `<div style="color:var(--subtext)">No tasks.</div>`;
    pane.querySelectorAll("[data-u]").forEach((tr) => tr.addEventListener("click", async () => {
      const box = pane.querySelector("[data-log]");
      box.style.display = ""; box.textContent = "Loading …";
      try { const t = await preq(`/${C}/tasks/${encodeURIComponent(tr.dataset.n)}/${encodeURIComponent(tr.dataset.u)}`);
        box.textContent = (t.log || []).map((l) => l.t).join("\n") || "(no log)"; }
      catch (e) { box.textContent = e.message; }
    }));
  }

  // -------------------------------------------------------------- Shares (specific VMs/LXCs)
  async function drawAcl() {
    pane.innerHTML = "Loading …";
    let a, g;
    try { [a, g] = await Promise.all([preq(`/${C}/acl`), preq(`/${C}/guests`)]); } catch (e) { pane.innerHTML = err(e); return; }
    const subjects = [...Object.entries(a.users).map(([id, n]) => [`user:${id}`, n]), ...Object.entries(a.groups).map(([id, n]) => [`group:${id}`, n])];
    pane.innerHTML = `<p style="color:var(--subtext);margin-top:0">Here you grant individual users/groups <b>specific</b> VMs/LXCs - in addition to the
        "all VMs" / "all LXC" permissions. What they may do with them (power, console, snapshots …) is governed by the Proxmox permissions in the permission management.</p>
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px">
        <select data-subj style="min-width:240px">${subjects.map(([k, l]) => `<option value="${esc(k)}">${esc(l)}${a.acl[k] ? ` (${a.acl[k].length})` : ""}</option>`).join("")}</select>
        <button class="btn-primary btn-sm" data-save>Save</button></div>
      <div data-guests style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:4px"></div>`;
    const drawChecks = () => {
      const sel = new Set(a.acl[pane.querySelector("[data-subj]").value] || []);
      pane.querySelector("[data-guests]").innerHTML = g.guests.map((x) => `<label style="display:flex;gap:6px;align-items:center;padding:4px 6px;border:1px solid var(--border);border-radius:6px">
        <input type="checkbox" value="${x.vmid}" ${sel.has(x.vmid) ? "checked" : ""}> ${x.type === "qemu" ? icon("server", 14) : icon("box", 14)} ${x.vmid} ${esc(x.name || "")}</label>`).join("")
        || `<div style="color:var(--subtext)">No guests visible.</div>`;
    };
    pane.querySelector("[data-subj]").addEventListener("change", drawChecks);
    drawChecks();
    pane.querySelector("[data-save]").addEventListener("click", async () => {
      const subject = pane.querySelector("[data-subj]").value;
      const vmids = [...pane.querySelectorAll("[data-guests] input:checked")].map((i) => parseInt(i.value, 10));
      try { const r = await preq(`/${C}/acl`, { method: "PUT", body: { subject, vmids } }); a.acl = r.acl; window.notify?.("Shares saved", "success"); }
      catch (e) { window.notify?.(e.message, "error"); }
    });
  }

  // -------------------------------------------------------------- Mapping agents <-> guests
  async function drawMatch() {
    pane.innerHTML = "Loading …";
    let gm;
    try { gm = await preq("/guest-map"); } catch (e) { pane.innerHTML = err(e); return; }
    const linked = Object.entries(gm.map || {}).filter(([, m]) => m.host === cid);
    const sug = (gm.suggestions || []).filter((x) => x.host === cid);
    const name = (acid) => (state.clients || []).find((c) => c.id === acid)?.hostname || acid;
    const when = gm.scanned?.[cid];
    pane.innerHTML = `
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px">
        <p style="color:var(--subtext);margin:0;flex:1">Agents that run in VMs/containers of this Proxmox - detected via MAC address, IP address or hostname
          (methods, interval and "map safe matches immediately" in the module settings)${when ? `. Last matched ${new Date(when * 1000).toLocaleTimeString()}` : ""}.
          Guests are subordinated to the <b>head</b> of their Proxmox node.</p>
        ${sug.length ? `<button class="btn btn-sm" data-sort>${icon("compass", 14)} Sort / choose head</button>` : ""}
        <button class="btn btn-primary btn-sm" data-scan>${icon("search", 14)} Match now</button></div>
      ${sug.length ? `<h4 style="margin:6px 0">Suggestions (${sug.length})</h4>${sug.map((x) => `
        <div data-s="${esc(x.client_id)}" style="display:flex;gap:8px;align-items:center;padding:6px 0;border-bottom:1px solid var(--border)">
          <span style="display:flex">${x.type === "qemu" ? icon("server", 16) : icon("box", 16)}</span><span style="flex:1"><b>${esc(x.client_name)}</b> &rarr; ${x.type === "qemu" ? "VM" : "CT"} ${x.vmid} "${esc(x.name)}" · node ${esc(x.node)} · head ${esc(x.head_name || "")}
          <span style="color:var(--subtext);font-size:12px">(${esc(x.reason)})</span></span>
          <button class="btn btn-sm" data-ign>No</button><button class="btn btn-primary btn-sm" data-acc>Accept</button></div>`).join("")}` : ""}
      <h4 style="margin:14px 0 6px">Mapped (${linked.length})</h4>
      ${linked.length ? `<table class="data-table" style="width:100%"><thead><tr><th style="text-align:left">Agent</th><th style="text-align:left">Guest</th><th>Node</th><th style="text-align:left">Head</th><th></th></tr></thead><tbody>
        ${linked.map(([acid, m]) => `<tr><td><span style="display:inline-flex;align-items:center;gap:4px">${icon("link", 14)} <b>${esc(name(acid))}</b></span></td><td>${m.type === "qemu" ? "VM" : "CT"} ${m.vmid} "${esc(m.name || "")}"</td><td>${esc(m.node)}</td>
          <td>${esc(name((state.clients || []).find((c) => c.id === acid)?.parent_client_id || ""))}</td>
          <td style="text-align:right"><button class="btn-tool" data-unlink="${esc(acid)}" title="Unmap">${icon("close", 14)}</button></td></tr>`).join("")}</tbody></table>`
        : `<div style="color:var(--subtext)">No agents mapped yet.</div>`}`;
    pane.querySelector("[data-scan]").addEventListener("click", async (e) => {
      e.target.disabled = true; e.target.textContent = "Matching …";
      try { const r = await preq("/scan", { method: "POST", body: { host: cid } });
        const x = Object.values(r.results)[0] || {};
        window.notify?.(x.error ? `Match: ${x.error}` : `${x.found} agent(s) detected, ${x.new} new${x.assigned ? `, ${x.assigned} mapped automatically` : ""}`, x.error ? "error" : "success");
      } catch (err2) { window.notify?.(err2.message, "error"); }
      drawMatch(); window.RMM_PVE_REFRESH?.();
    });
    pane.querySelector("[data-sort]")?.addEventListener("click", () =>
      openSuggestDialog(sug, () => { drawMatch(); window.RMM_PVE_REFRESH?.(); }, { candidates: gm.candidates || [] }));
    pane.querySelectorAll("[data-s]").forEach((row) => {
      const go = async (what) => {
        try { const r = await preq(`/suggestions/${encodeURIComponent(row.dataset.s)}/${what}`, { method: "POST" });
          if (r.client) window.dispatchEvent(new CustomEvent("rmm-client-updated", { detail: r.client })); drawMatch(); }
        catch (e2) { window.notify?.(e2.message, "error"); }
      };
      row.querySelector("[data-acc]").onclick = () => go("accept");
      row.querySelector("[data-ign]").onclick = () => go("ignore");
    });
    pane.querySelectorAll("[data-unlink]").forEach((b) => b.addEventListener("click", async () => {
      try { await preq(`/guest-map/${encodeURIComponent(b.dataset.unlink)}`, { method: "DELETE" }); drawMatch(); }
      catch (e3) { window.notify?.(e3.message, "error"); }
    }));
  }

  // -------------------------------------------------------------- API-Explorer
  async function drawApi() {
    pane.innerHTML = `<p style="color:var(--subtext);margin-top:0">Any call to the Proxmox API (<code>/api2/json/&lt;path&gt;</code>) with the permissions of the
        connected Proxmox user. Docs: <i>pve.proxmox.com/pve-docs/api-viewer</i>.</p>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px">
        <select data-m><option>GET</option><option>POST</option><option>PUT</option><option>DELETE</option></select>
        <input type="text" data-p value="nodes" style="flex:1;min-width:220px;font-family:ui-monospace,monospace" placeholder="nodes/pve1/qemu">
        <button class="btn-primary" data-go>Send</button></div>
      <div style="display:flex;gap:4px;flex-wrap:wrap;margin-bottom:6px">${["version", "nodes", "cluster/resources", "cluster/status", "access/users", "storage", "pools", "cluster/ha/resources"]
        .map((p) => `<button class="btn btn-sm" data-quick="${p}">${p}</button>`).join("")}</div>
      <textarea data-b rows="3" placeholder='Parameters as JSON, e.g. {"vmid": 105, "name": "test"}' style="width:100%;font-family:ui-monospace,monospace;font-size:12px"></textarea>
      <pre data-out style="margin-top:8px;background:var(--panel-2);border:1px solid var(--border);border-radius:8px;padding:10px;max-height:420px;overflow:auto;font-size:11.5px">–</pre>`;
    const out = pane.querySelector("[data-out]");
    const send = async () => {
      const m = pane.querySelector("[data-m]").value, path = pane.querySelector("[data-p]").value.trim().replace(/^\/+/, "");
      let params = {};
      const raw = pane.querySelector("[data-b]").value.trim();
      if (raw) { try { params = JSON.parse(raw); } catch { out.textContent = "Parameters are not valid JSON."; return; } }
      if (m !== "GET" && !(await uiConfirm(`Really run ${m} ${path}?`, { description: "Write calls change Proxmox directly.", okText: "Run", danger: m === "DELETE" }))) return;
      out.textContent = "…";
      try {
        const q = m === "GET" || m === "DELETE" ? "?" + new URLSearchParams(params).toString() : "";
        const r = await preq(`/${C}/api/${path.split("/").map(encodeURIComponent).join("/")}${q === "?" ? "" : q}`,
          m === "GET" || m === "DELETE" ? { method: m } : { method: m, body: params });
        out.textContent = JSON.stringify(r.data, null, 2);
      } catch (e) { out.textContent = `Error: ${e.message}`; }
    };
    pane.querySelector("[data-go]").addEventListener("click", send);
    pane.querySelector("[data-p]").addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
    pane.querySelectorAll("[data-quick]").forEach((b) => b.addEventListener("click", () => { pane.querySelector("[data-p]").value = b.dataset.quick; pane.querySelector("[data-m]").value = "GET"; send(); }));
  }

  load();
}
