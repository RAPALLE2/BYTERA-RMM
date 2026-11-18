// apps/portscan.js
// ----------------
// Standalone port scanner. Can be opened from the start menu (then you enter
// the IP yourself) or from the network scanner (then the IP is already
// prefilled, see win.props.ip).
//
// Scan modes:
//   - standard: the common ports (fast)
//   - all:      all ports 1-65535 (takes longer)
//   - custom:   freely given ports/ranges, e.g. "22,80,8000-8100"
//
// The scan runs as a BACKEND JOB (app/bgjobs.py) - closing the window or
// reloading the page does not abort it; on reopening it is shown.

import { nodeHint } from "/js/app/nodechoice.js";
import { api } from "/js/app/api.js";
import { esc } from "/js/app/utils.js";
import { moduleRequest } from "/js/app/modules.js";
import { registerCleanup } from "/js/app/windowmanager.js";
import { watchJobs, cancelJob, jobDuration } from "/js/app/bgjobs.js";

// Auswahl "Backend oder Node" fuellen (Nodes nur mit nachgeladenem Node-Modul "network")
function fillVia(sel, preset = "") {
  api.nodeChoices("network").then((r) => {
    for (const n of r.nodes || []) {
      const o = document.createElement("option");
      o.value = n.id; o.disabled = !n.active;
      o.textContent = `Node ${n.name}${nodeHint(n)}`;
      sel.appendChild(o);
    }
    if (preset) sel.value = preset;
  }).catch(() => {});
}
import { t } from "/js/app/i18n.js";
import { icon } from "./icons.js";

export function renderPortscan(body, win) {
  const presetIp = win.props?.ip || "";
  setTimeout(() => { const v = body.querySelector(`#ps-via-${win.key}`); if (v) fillVia(v, win.props?.via || ""); }, 0);

  body.innerHTML = `
    <div style="display:flex;flex-direction:column;height:100%">
      <div class="explorer-toolbar" style="flex-wrap:wrap;gap:8px">
        <input type="text" id="ps-ip-${win.key}" placeholder="${t("ps_ip_placeholder")}" value="${esc(presetIp)}"
          style="flex:1;min-width:150px" />
        <select id="ps-mode-${win.key}">
          <option value="standard">${t("ps_mode_standard")}</option>
          <option value="all">${t("ps_mode_all")}</option>
          <option value="custom">${t("ps_mode_custom")}</option>
        </select>
        <select id="ps-via-${win.key}" title="Where to scan from?"><option value="">From the backend</option></select>
        <button class="btn-primary" id="ps-scan-${win.key}">${icon("search", 14)} ${t("ps_scan")}</button>
        <button class="btn" id="ps-stop-${win.key}" style="display:none">${icon("stop", 14)} ${t("cancel")}</button>
      </div>
      <div id="ps-custom-row-${win.key}" class="explorer-toolbar" style="display:none">
        <input type="text" id="ps-ports-${win.key}" placeholder="${t("ps_custom_placeholder")}"
          style="flex:1" />
      </div>
      <div id="ps-status-${win.key}" style="padding:6px 10px;font-size:12px;color:var(--subtext)"></div>
      <div id="ps-result-${win.key}" style="flex:1;overflow:auto;padding:4px 10px"></div>
    </div>
  `;

  const ipInput = body.querySelector(`#ps-ip-${win.key}`);
  const modeSel = body.querySelector(`#ps-mode-${win.key}`);
  const customRow = body.querySelector(`#ps-custom-row-${win.key}`);
  const portsInput = body.querySelector(`#ps-ports-${win.key}`);
  const scanBtn = body.querySelector(`#ps-scan-${win.key}`);
  const statusEl = body.querySelector(`#ps-status-${win.key}`);
  const resultEl = body.querySelector(`#ps-result-${win.key}`);

  modeSel.addEventListener("change", () => {
    customRow.style.display = modeSel.value === "custom" ? "flex" : "none";
  });

  const stopBtn = body.querySelector(`#ps-stop-${win.key}`);
  const viaOf = () => body.querySelector(`#ps-via-${win.key}`).value;
  let job = null, stopWatch = null, watchedKey = null;

  function watch(key) {
    if (key === watchedKey) return;
    stopWatch?.();
    watchedKey = key;
    job = null;
    stopWatch = watchJobs({ kind: "network.portscan", key }, (j) => {
      job = j;
      if (j?.meta?.ip && !ipInput.value.trim()) ipInput.value = j.meta.ip;   // nach Neuladen: IP wieder eintragen
      showJob();
    });
  }

  function showJob() {
    const running = !!job?.running;
    scanBtn.disabled = running;
    stopBtn.style.display = running ? "" : "none";
    if (!job) return;
    if (running) {
      statusEl.textContent = (job.meta?.mode === "all" ? t("ps_scanning_all") : t("ps_scanning"))
        + (job.by ? ` · gestartet von ${job.by}` : "") + ` · ${jobDuration(job)}`;
      resultEl.innerHTML = "";
      return;
    }
    if (job.status === "error" || job.status === "cancelled") {
      statusEl.textContent = "";
      resultEl.innerHTML = `<div style="color:var(--danger)">${esc(job.status === "error" ? job.error : t("nw_cancelled"))}</div>`;
      return;
    }
    const res = job.result || { ports: [] };
    const scannedInfo = res.scanned ? ` (${res.scanned} ${t("ps_ports_checked")})` : "";
    const when = ` · ${new Date(job.finished).toLocaleString()}`;
    if (!res.ports.length) {
      statusEl.textContent = t("ps_none") + scannedInfo + when;
      resultEl.innerHTML = `<div style="color:var(--subtext)">${t("ps_none")}</div>`;
    } else {
      statusEl.textContent = `${res.ports.length} ${t("ps_open_found")}${scannedInfo}${when}`;
      resultEl.innerHTML = `<b>${t("ps_open_ports")}:</b><div style="margin-top:6px">` + res.ports.map((p) =>
        `<span style="display:inline-block;background:var(--panel);border:1px solid var(--accent);border-radius:5px;padding:2px 8px;margin:3px;font-size:12px">
          ${p.port} <span style="color:var(--subtext)">${esc(p.service)}</span>
        </span>`).join("") + `</div>`;
    }
  }

  async function scan() {
    const ip = ipInput.value.trim();
    if (!ip) { statusEl.textContent = t("ps_need_ip"); return; }
    const mode = modeSel.value;
    const ports = portsInput.value.trim();
    if (mode === "custom" && !ports) { statusEl.textContent = t("ps_need_ports"); return; }
    scanBtn.disabled = true;
    try {
      const q = new URLSearchParams({ ip, mode });
      if (mode === "custom") q.set("ports", ports);
      const via = viaOf();
      if (via) q.set("via", via);
      const r = await moduleRequest(`/api/network/portscan/job?${q}`, { method: "POST" });
      watch(`${via || "backend"}:${ip}`);
      job = r; showJob();
    } catch (e) {
      scanBtn.disabled = false;
      statusEl.textContent = "";
      resultEl.innerHTML = `<div style="color:var(--danger)">${esc(e.message)}</div>`;
    }
  }

  stopBtn.addEventListener("click", async () => {
    if (!job?.running) return;
    try { job = await cancelJob(job.id); showJob(); } catch (e) { statusEl.textContent = e.message; }
  });
  if (win?.key) registerCleanup(win.key, () => stopWatch?.());

  scanBtn.addEventListener("click", scan);
  ipInput.addEventListener("keydown", (e) => { if (e.key === "Enter") scan(); });
  portsInput.addEventListener("keydown", (e) => { if (e.key === "Enter") scan(); });

  // If the IP is already prefilled (from the network scanner), scan right away
  // (if a scan is already running for this IP, the window just attaches to it).
  if (presetIp) setTimeout(scan, 50);
  else {
    watch(undefined);          // ohne IP: zuletzt gelaufenen Portscan zeigen (z.B. nach Neuladen)
    setTimeout(() => ipInput.focus(), 50);
  }
}
