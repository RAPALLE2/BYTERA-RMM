// apps/network.js
// ---------------
// Netzwerk-Scanner. Das Ziel kann angegeben werden als
//   192.168.1.      -> ein /24 (254 Adressen)
//   10.10           -> ein /16, also 256 x 254 Adressen
//   10.10.0.0/16    -> beliebiges CIDR (ab /8)
//   leer            -> eigenes Subnetz
// Der Scan laeuft als BACKEND-JOB (app/bgjobs.py): Diese Seite startet ihn nur
// und zeigt live an, was das Backend meldet. Neu laden, ein anderes Fenster oder
// ein anderer Benutzer sehen denselben Scan; das Ergebnis haelt das Backend.
// Der ⚡ Speed-Up verteilt die /24-Blöcke auf mehrere parallele Worker.

import { nodeHint } from "/js/app/nodechoice.js";
import { api } from "/js/app/api.js";
import { esc } from "/js/app/utils.js";
import { openWindow } from "/js/app/windowmanager.js";
import { registerCleanup } from "/js/app/windowmanager.js";
import { moduleRequest } from "/js/app/modules.js";
import { watchJobs, cancelJob, jobDuration } from "/js/app/bgjobs.js";

// Auswahl "Backend oder Node" fuellen (Nodes nur mit nachgeladenem Node-Modul "network")
function fillVia(sel, preset = "") {
  api.nodeChoices("network").then((r) => {
    for (const n of r.nodes || []) {
      const o = document.createElement("option");
      o.value = n.id; o.disabled = !n.active;
      o.textContent = `🖧 Node ${n.name}${nodeHint(n)}`;
      sel.appendChild(o);
    }
    if (preset) sel.value = preset;
  }).catch(() => {});
}
import { t } from "/js/app/i18n.js";

const SPEEDS = {
  normal: { label: "🐢 Normal", hint: t("u_schont_netz_und_cpu") },
  fast:   { label: "⚡ Speed-Up", hint: "8 Subnetze gleichzeitig – empfohlen" },
  turbo:  { label: "🚀 Turbo", hint: "24 Subnetze parallel (max. 512 Pings gleichzeitig)" },
};

export function renderNetwork(body, win) {
  let speed = "fast";
  let job = null;            // aktueller Scan-Job der gewaehlten Quelle (vom Backend)
  let stopWatch = null;

  body.innerHTML = `
    <div style="display:flex;flex-direction:column;height:100%">
      <div class="explorer-toolbar" style="flex-wrap:wrap;gap:8px">
        <input type="text" id="net-subnet" placeholder="Ziel: 192.168.1.  ·  10.10 (= /16)  ·  10.10.0.0/16  ·  leer = eigenes Netz"
          style="flex:1;min-width:220px" />
        <div id="net-speeds" style="display:flex;gap:4px">
          ${Object.entries(SPEEDS).map(([k, v]) =>
            `<button class="btn net-speed" data-speed="${k}" title="${esc(v.hint)}">${v.label}</button>`).join("")}
        </div>
        <select id="net-via" title="Von wo aus scannen?"><option value="">🖥 Vom Backend</option></select>
        <button class="btn-primary" id="net-scan">🔍 Scannen</button>
        <button class="btn" id="net-stop" style="display:none">✋ ${t("cancel")}</button>
      </div>
      <div id="net-status" style="padding:6px 10px;font-size:12px;color:var(--subtext)"></div>
      <div id="net-progress" style="display:none;padding:0 10px 6px">
        <div style="height:8px;border-radius:4px;background:var(--panel-2);overflow:hidden">
          <div id="net-bar" style="height:100%;width:0%;background:var(--accent);transition:width .25s"></div>
        </div>
      </div>
      <div style="flex:1;overflow:auto">
        <table class="data-table">
          <thead><tr><th>IP</th><th>Hostname</th><th>MAC</th><th style="width:130px">Ports</th></tr></thead>
          <tbody id="net-body"><tr><td colspan="4" style="color:var(--subtext)">${t("nw_not_scanned")}</td></tr></tbody>
        </table>
      </div>
    </div>`;

  const subnetInput = body.querySelector("#net-subnet");
  const viaSel = body.querySelector("#net-via");
  fillVia(viaSel, win?.props?.via || "");
  const source = () => viaSel.value || "backend";
  const scanBtn = body.querySelector("#net-scan");
  const stopBtn = body.querySelector("#net-stop");
  const statusEl = body.querySelector("#net-status");
  const progEl = body.querySelector("#net-progress");
  const barEl = body.querySelector("#net-bar");
  const tbody = body.querySelector("#net-body");

  // ---- Geschwindigkeit wählen ----
  function drawSpeeds() {
    body.querySelectorAll(".net-speed").forEach((b) => {
      const on = b.dataset.speed === speed;
      b.style.background = on ? "var(--accent)" : "";
      b.style.color = on ? "#fff" : "";
    });
  }
  body.querySelectorAll(".net-speed").forEach((b) =>
    b.addEventListener("click", () => { speed = b.dataset.speed; drawSpeeds(); showPreview(); }));
  drawSpeeds();

  // ---- Vorschau: wie groß wird der Scan? ----
  let previewTimer = null;
  async function showPreview() {
    if (job?.running) return;
    clearTimeout(previewTimer);
    previewTimer = setTimeout(async () => {
      try {
        const p = await api.scanPreview(subnetInput.value.trim() || null);
        const est = estimate(p.hosts, speed);
        statusEl.textContent =
          `${p.label}: ${t("nw_subnets", { n: p.subnets })}, `
          + t("nw_addresses_est", { n: p.hosts.toLocaleString(), est });
        statusEl.style.color = "var(--subtext)";
      } catch (e) {
        statusEl.textContent = e.message;
        statusEl.style.color = "var(--danger)";
      }
    }, 250);
  }
  // Grobe Schätzung: Adressen / effektive Nebenläufigkeit * Ping-Timeout.
  // Das Backend deckelt die Gesamtzahl gleichzeitiger Pings (MAX_TOTAL_PINGS),
  // damit nicht tausende Systemprozesse gleichzeitig laufen - die Schätzung
  // muss dieselbe Obergrenze berücksichtigen, sonst verspricht sie zu viel.
  const MAX_PAR = 512;
  function estimate(hosts, sp) {
    const par = Math.min({ normal: 64, fast: 8 * 96, turbo: 24 * 128 }[sp] || 64, MAX_PAR);
    const sec = Math.max(2, Math.round((hosts / par) * 1.1));
    if (sec < 90) return `${sec} Sekunden`;
    if (sec < 5400) return `${Math.round(sec / 60)} Minuten`;
    return `${(sec / 3600).toFixed(1)} Stunden`;
  }
  subnetInput.addEventListener("input", showPreview);
  subnetInput.addEventListener("keydown", (e) => { if (e.key === "Enter") scan(); });

  // ---- Scan starten / verfolgen (Backend-Job) ----
  async function scan() {
    if (job?.running) return;
    scanBtn.disabled = true;
    try {
      const q = new URLSearchParams({ speed });
      const target = subnetInput.value.trim();
      if (target) q.set("target", target);
      if (viaSel.value) q.set("via", viaSel.value);
      const r = await moduleRequest(`/api/network/scan/job?${q}`, { method: "POST" });
      if (r.attached) window.notify?.(`Auf dieser Quelle läuft bereits ein Scan (von ${r.by}) – er wird angezeigt.`, "info");
      job = r; showJob();
      tbody.innerHTML = `<tr><td colspan="4" style="color:var(--subtext)">${t("nw_scanning")}</td></tr>`;
    } catch (e) {
      scanBtn.disabled = false;
      statusEl.style.color = "var(--danger)";
      statusEl.textContent = e.message;
    }
  }

  function showJob() {
    const running = !!job?.running;
    scanBtn.disabled = running;
    stopBtn.style.display = running ? "" : "none";
    stopBtn.disabled = !!job?.cancel_requested;
    progEl.style.display = running ? "" : "none";
    if (!job) return;
    const pr = job.progress || {};
    if (running) {
      barEl.style.width = `${(job.pct || 0).toFixed(1)}%`;
      statusEl.style.color = "var(--subtext)";
      statusEl.textContent =
        `${pr.label || ""} · ${pr.done_subnets || 0}/${pr.total_subnets || 0} ${t("nw_subnets_word")} · `
        + `${(pr.done_hosts || 0).toLocaleString()}/${(pr.total_hosts || 0).toLocaleString()} ${t("nw_addresses")} `
        + `(${(job.pct || 0).toFixed(1)} %) · ${t("nw_found", { n: pr.found || 0 })}`
        + (pr.max_parallel ? ` · ${t("nw_parallel", { n: pr.max_parallel })}` : "")
        + (job.by ? ` · gestartet von ${job.by}` : "")
        + (job.cancel_requested ? " · wird abgebrochen …" : "");
      return;
    }
    if (job.status === "error") {
      statusEl.style.color = "var(--danger)";
      statusEl.textContent = job.error || t("nw_scan_failed");
      tbody.innerHTML = `<tr><td colspan="4" style="color:var(--danger)">${esc(job.error || t("u_fehler"))}</td></tr>`;
      return;
    }
    const devices = job.result?.devices || [];
    renderDevices(devices);
    statusEl.style.color = "var(--subtext)";
    statusEl.textContent =
      t("nw_devices_in", { n: devices.length, label: job.result?.label || pr.label || "" })
      + ` · ${jobDuration(job)}`
      + (job.status === "cancelled" ? ` (${t("nw_cancelled")}${job.cancelled_by ? " – " + job.cancelled_by : ""})` : "")
      + ` · ${new Date(job.finished).toLocaleString()}${job.by ? ", " + job.by : ""}`;
  }

  // Quelle beobachten: laufender oder zuletzt beendeter Scan; sonst letztes Ergebnis
  function watchSource() {
    stopWatch?.();
    job = null;
    showJob();
    let first = true;
    stopWatch = watchJobs({ kind: "network.scan", key: source() }, (j) => {
      job = j;
      if (j) showJob();
      else if (first) loadLast();
      first = false;
    });
  }

  function loadLast() {
    moduleRequest(`/api/network/scan/last?source=${encodeURIComponent(source())}`).then((res) => {
      if (job) return;
      if (res.devices && res.devices.length) {
        renderDevices(res.devices);
        statusEl.textContent = t("nw_devices_last", { n: res.devices.length });
      } else {
        tbody.innerHTML = `<tr><td colspan="4" style="color:var(--subtext)">${t("nw_not_scanned")}</td></tr>`;
        showPreview();
      }
    }).catch(() => showPreview());
  }

  stopBtn.addEventListener("click", async () => {
    if (!job?.running) return;
    try { job = await cancelJob(job.id); showJob(); } catch (e) { statusEl.textContent = e.message; }
  });
  scanBtn.addEventListener("click", scan);
  viaSel.addEventListener("change", watchSource);

  function renderDevices(devices) {
    if (!devices.length) {
      tbody.innerHTML = `<tr><td colspan="4" style="color:var(--subtext)">${t("nw_none_found")}</td></tr>`;
      return;
    }
    tbody.innerHTML = devices.map((d) => `
      <tr>
        <td>${esc(d.ip)}</td>
        <td>${esc(d.hostname || "–")}</td>
        <td style="color:var(--subtext);font-family:monospace;font-size:11px">${esc(d.mac || "–")}</td>
        <td><button class="btn" data-portscan="${esc(d.ip)}">Portscan</button></td>
      </tr>`).join("");

    // Portscan-Button öffnet das Portscan-Programm mit schon eingetragener IP.
    tbody.querySelectorAll("[data-portscan]").forEach((btn) =>
      btn.addEventListener("click", () => {
        const ip = btn.dataset.portscan;
        openWindow({
          key: `portscan-${ip}`, appId: "portscan",
          title: `Portscan — ${ip}`, props: { ip, via: viaSel.value }, w: 560, h: 480,
        });
      }));
  }

  // Beim Öffnen: laufenden/letzten Scan der Quelle zeigen (vom Backend)
  watchSource();

  if (win?.key) registerCleanup(win.key, () => { stopWatch?.(); clearTimeout(previewTimer); });
}
