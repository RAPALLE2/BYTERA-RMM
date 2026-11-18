// software.js (module "software") - manage packages + multi-client rollout
// -------------------------------------------------------------------------
// Left: packages (installation files + command per platform).
// Right: rollout - tick clients (search, tags), online = immediately,
// offline = offline queue (runs at the next online check-in).

import { moduleRequest } from "/js/app/modules.js";
import { getToken } from "/js/app/api.js";
import { BACKEND_URL } from "/js/app/config.js";
import { state } from "/js/app/state.js";
import { esc, uiConfirm } from "/js/app/utils.js";
import { displayName, osIcon, clientTags } from "/js/app/deviceicons.js";
import { icon } from "./icons.js";

export function renderSoftware(body, win) {
  let packages = [];
  let selPkg = null;
  const preselect = win.props?.preselect || null;

  body.innerHTML = `
    <div style="display:flex;height:100%;background:var(--panel);min-width:0">
      <div style="flex:1.1;min-width:0;display:flex;flex-direction:column;border-right:1px solid var(--border)">
        <div style="padding:10px 14px;border-bottom:1px solid var(--border);display:flex;gap:8px;align-items:center">
          <b>${icon("package", 15)} Packages</b><span style="flex:1"></span>
          <button class="btn-primary" id="sw-new">+ Package</button>
        </div>
        <div id="sw-list" style="flex:1;overflow:auto;padding:8px 14px"></div>
      </div>
      <div style="flex:1;min-width:300px;display:flex;flex-direction:column">
        <div style="padding:10px 14px;border-bottom:1px solid var(--border)"><b>${icon("truck", 15)} Rollout</b>
          <span id="sw-selpkg" style="font-size:12px;color:var(--subtext)"></span></div>
        <div style="padding:8px 14px;display:flex;gap:6px">
          <input id="sw-filter" placeholder="Filter (name, tag, OS)…" style="flex:1">
          <button class="btn btn-sm" id="sw-all">All</button>
          <button class="btn btn-sm" id="sw-none">None</button>
        </div>
        <div id="sw-clients" style="flex:1;overflow:auto;padding:0 14px"></div>
        <div style="padding:10px 14px;border-top:1px solid var(--border);display:flex;gap:8px;align-items:center">
          <label style="font-size:12px;color:var(--subtext);display:flex;gap:4px;align-items:center">
            <input type="checkbox" id="sw-queue" checked> Queue offline clients</label>
          <span style="flex:1"></span>
          <button class="btn-primary" id="sw-deploy" disabled>${icon("truck", 14)} Roll out</button>
        </div>
        <div id="sw-log" class="hidden" style="max-height:170px;overflow:auto;border-top:1px solid var(--border);
             padding:8px 14px;font-family:monospace;font-size:11.5px;white-space:pre-wrap"></div>
      </div>
    </div>`;

  const $ = (s) => body.querySelector(s);

  // ---------------- packages ----------------
  function fmtSize(n) { return n > 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.round(n / 1024)} KB`; }

  function drawPackages() {
    const box = $("#sw-list");
    if (!packages.length) {
      box.innerHTML = `<div style="color:var(--subtext);font-size:13px;padding:8px 0">
        No packages yet. Create one with "+ Package", upload an installation file, provide a
        command (or leave it empty for silent automatic install of MSI/EXE/DEB/RPM).</div>`;
      return;
    }
    box.innerHTML = packages.map((p) => `
      <div class="panel" style="padding:8px 10px;margin-bottom:6px;border:1px solid ${selPkg === p.id ? "var(--accent)" : "var(--border)"}">
        <div style="display:flex;gap:8px;align-items:center;cursor:pointer" data-sel="${esc(p.id)}">
          <b style="flex:1">${icon("package", 14)} ${esc(p.name)}</b>
          <button class="btn btn-sm" data-edit="${esc(p.id)}">${icon("edit", 13)}</button>
          <button class="action-btn btn-danger" data-del="${esc(p.id)}">${icon("close", 13)}</button>
        </div>
        <div style="font-size:11.5px;color:var(--subtext);margin-top:3px">
          ${(p.files || []).map((f) => `<span style="margin-right:8px">${icon("file", 12)} ${esc(f.name)} (${fmtSize(f.size || 0)})
            <a href="#" data-fdel="${esc(p.id)}|${esc(f.name)}" title="Remove file">${icon("close", 11)}</a></span>`).join("") || "no files"}
          <label style="margin-left:4px;cursor:pointer;color:var(--accent)">${icon("upload", 13)} File…
            <input type="file" data-fup="${esc(p.id)}" style="display:none"></label>
        </div>
        ${p.note ? `<div style="font-size:11px;color:var(--subtext)">${esc(p.note)}</div>` : ""}
      </div>`).join("");

    box.querySelectorAll("[data-sel]").forEach((el) => el.addEventListener("click", (ev) => {
      if (ev.target.closest("button")) return;
      selPkg = el.dataset.sel;
      $("#sw-selpkg").textContent = " · " + (packages.find((p) => p.id === selPkg)?.name || "");
      $("#sw-deploy").disabled = false;
      drawPackages();
    }));
    box.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
      if (!(await uiConfirm("Delete the package including its files?", { danger: true }))) return;
      await moduleRequest(`/api/software/packages/${b.dataset.del}`, { method: "DELETE", noJob: true });
      if (selPkg === b.dataset.del) { selPkg = null; $("#sw-deploy").disabled = true; }
      load();
    }));
    box.querySelectorAll("[data-edit]").forEach((b) => b.addEventListener("click", () =>
      editPackage(packages.find((p) => p.id === b.dataset.edit))));
    box.querySelectorAll("[data-fup]").forEach((inp) => inp.addEventListener("change", async () => {
      const f = inp.files?.[0];
      if (!f) return;
      const fd = new FormData();
      fd.append("file", f);
      window.notify?.(`Uploading ${f.name} …`, "info");
      try {
        // Direct fetch: a multipart upload must NOT carry a JSON content type.
        const res = await fetch(`${BACKEND_URL}/api/software/packages/${inp.dataset.fup}/files`, {
          method: "POST", body: fd,
          headers: { Authorization: `Bearer ${getToken()}` },
        });
        if (!res.ok) throw new Error((await res.json().catch(() => ({})))?.detail || `HTTP ${res.status}`);
        window.notify?.("File uploaded", "success");
        load();
      } catch (e) { window.notify?.(e.message, "error"); }
    }));
    box.querySelectorAll("[data-fdel]").forEach((a) => a.addEventListener("click", async (ev) => {
      ev.preventDefault();
      const [pid, name] = a.dataset.fdel.split("|");
      await moduleRequest(`/api/software/packages/${pid}/files/${encodeURIComponent(name)}`, { method: "DELETE", noJob: true });
      load();
    }));
  }

  async function editPackage(pkg) {
    const { uiPrompt } = await import("/js/app/utils.js");
    const name = await uiPrompt(pkg ? "Package name" : "Name of the new package", { value: pkg?.name || "" });
    if (!name) return;
    const cmdWin = await uiPrompt("Installation command WINDOWS ({file}=file, {dir}=folder; empty = silent automatic)",
      { value: pkg?.commands?.win || "" });
    if (cmdWin === null) return;
    const cmdLin = await uiPrompt("Installation command LINUX ({file}, {dir}; empty = automatic)",
      { value: pkg?.commands?.linux || "" });
    if (cmdLin === null) return;
    const bodyData = { name, commands: { win: cmdWin || "", linux: cmdLin || "" }, note: pkg?.note || "" };
    try {
      if (pkg) await moduleRequest(`/api/software/packages/${pkg.id}`, { method: "PUT", noJob: true, body: JSON.stringify(bodyData) });
      else await moduleRequest("/api/software/packages", { method: "POST", noJob: true, body: JSON.stringify(bodyData) });
      load();
    } catch (e) { window.notify?.(e.message, "error"); }
  }
  $("#sw-new").addEventListener("click", () => editPackage(null));

  // ---------------- clients ----------------
  function drawClients() {
    const filter = ($("#sw-filter").value || "").toLowerCase();
    const list = (state.clients || []).filter((c) => {
      if (!filter) return true;
      const hay = [displayName(c), c.hostname, c.platform, ...clientTags(c)].join(" ").toLowerCase();
      return hay.includes(filter);
    });
    const prev = new Set([...body.querySelectorAll("[data-cl]:checked")].map((x) => x.dataset.cl));
    $("#sw-clients").innerHTML = list.map((c) => `
      <label style="display:flex;gap:8px;align-items:center;padding:5px 2px;border-bottom:1px solid var(--border);cursor:pointer">
        <input type="checkbox" data-cl="${esc(c.id)}" ${prev.has(c.id) || c.id === preselect ? "checked" : ""}>
        <span style="width:8px;height:8px;border-radius:50%;flex:none;background:${c.online ? "var(--online,#34d399)" : "var(--subtext)"}"></span>
        <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis">${osIcon(c)} ${esc(displayName(c))}</span>
        <span style="font-size:10.5px;color:var(--subtext)">${esc(clientTags(c).join(", "))}</span>
        <span style="font-size:10.5px;color:var(--subtext)">${c.online ? "online" : "offline"}</span>
      </label>`).join("") || `<div style="color:var(--subtext);font-size:13px;padding:8px 0">No clients.</div>`;
  }
  $("#sw-filter").addEventListener("input", drawClients);
  $("#sw-all").addEventListener("click", () => body.querySelectorAll("[data-cl]").forEach((c) => { c.checked = true; }));
  $("#sw-none").addEventListener("click", () => body.querySelectorAll("[data-cl]").forEach((c) => { c.checked = false; }));

  // ---------------- rollout ----------------
  $("#sw-deploy").addEventListener("click", async () => {
    const clients = [...body.querySelectorAll("[data-cl]:checked")].map((c) => c.dataset.cl);
    if (!selPkg || !clients.length) { window.notify?.("Select a package and tick clients", "info"); return; }
    const pkg = packages.find((p) => p.id === selPkg);
    if (!(await uiConfirm(`Roll out "${pkg?.name}" to ${clients.length} client(s) now?`))) return;
    const log = $("#sw-log");
    log.classList.remove("hidden");
    log.textContent = "Starting rollout …";
    $("#sw-deploy").disabled = true;
    try {
      await moduleRequest("/api/software/deploy", {
        method: "POST",
        body: JSON.stringify({ package_id: selPkg, clients, queue_offline: $("#sw-queue").checked }),
        onJob: (job) => {
          if (Array.isArray(job.logs) && job.logs.length) { log.textContent = job.logs.join("\n"); log.scrollTop = log.scrollHeight; }
        },
      });
      log.textContent += "\nRollout complete.";
    } catch (e) { log.textContent += `\nError: ${e.message}`; }
    $("#sw-deploy").disabled = false;
  });

  async function load() {
    try { packages = await moduleRequest("/api/software/packages"); } catch (e) { packages = []; window.notify?.(e.message, "error"); }
    drawPackages();
    drawClients();
  }
  load();
}
