// jobtray.js (Core)
// -----------------
// "Aufgaben" in der Taskleiste: zeigt ALLE Hintergrund-Jobs, die der Benutzer
// sehen darf (Scans, Updates, Skripte, Installationen, Builds ...), live.
// Weil die Jobs im Backend laufen, ist die Anzeige nach dem Neuladen, in jedem
// Fenster und fuer jeden berechtigten Benutzer dieselbe.
//
//   * Knopf mit Zaehler der laufenden Jobs (dreht sich, solange etwas laeuft)
//   * Klick: Liste laufender + zuletzt beendeter Jobs mit Fortschritt,
//     Abbrechen, Fehler und Log
//   * Meldung, wenn ein selbst gestarteter Job fertig wird

import { listJobs, fetchJob, cancelJob, jobDuration, jobStatusText } from "./bgjobs.js";
import { dashboardSocket } from "./socket.js";
import { state } from "./state.js";
import { esc } from "./utils.js";

let btn = null, panel = null, jobs = [], open = false;
const seenRunning = new Map();     // id -> job (laufend gesehen) -> Meldung bei Ende
const expanded = new Set();
let refreshTimer = null, inited = false;

export function initJobTray() {
  if (inited) return;
  const reload = document.getElementById("taskbar-reload-btn");
  if (!reload) return;
  inited = true;

  btn = document.createElement("button");
  btn.id = "taskbar-jobs-btn";
  btn.className = "taskbar-btn";
  btn.title = "Aufgaben (Hintergrund-Jobs)";
  btn.innerHTML = `<span class="jt-ico">⚙</span><span class="jt-count"></span>`;
  reload.parentNode.insertBefore(btn, reload);

  panel = document.createElement("div");
  panel.id = "jobtray-panel";
  panel.style.cssText = `position:fixed;right:10px;bottom:52px;width:420px;max-width:calc(100vw - 20px);
    max-height:62vh;overflow:auto;z-index:9000;display:none;background:var(--panel);border:1px solid var(--border);
    border-radius:12px;box-shadow:0 12px 34px rgba(0,0,0,.45);font-size:13px`;
  document.body.appendChild(panel);

  const style = document.createElement("style");
  style.textContent = `
    #taskbar-jobs-btn .jt-count:not(:empty){margin-left:5px;font-weight:700}
    #taskbar-jobs-btn.jt-busy .jt-ico{display:inline-block;animation:jtspin 1.6s linear infinite;color:var(--accent)}
    @keyframes jtspin{to{transform:rotate(360deg)}}
    #jobtray-panel .jt-item{padding:9px 12px;border-bottom:1px solid var(--border)}
    #jobtray-panel .jt-bar{height:5px;border-radius:3px;background:var(--panel-2);overflow:hidden;margin-top:5px}
    #jobtray-panel .jt-bar>div{height:100%;background:var(--accent);transition:width .3s}
    #jobtray-panel .jt-bar.jt-indef>div{width:30%!important;animation:jtindef 1.2s ease-in-out infinite alternate}
    @keyframes jtindef{from{margin-left:0}to{margin-left:70%}}
    #jobtray-panel pre{white-space:pre-wrap;word-break:break-word;max-height:220px;overflow:auto;font-size:11px;
      background:var(--panel-2);border-radius:6px;padding:6px 8px;margin:6px 0 0}`;
  document.head.appendChild(style);

  btn.addEventListener("click", (e) => { e.stopPropagation(); open = !open; panel.style.display = open ? "block" : "none"; if (open) refresh(); });
  document.addEventListener("mousedown", (e) => {
    if (open && !panel.contains(e.target) && e.target !== btn && !btn.contains(e.target)) {
      open = false; panel.style.display = "none";
    }
  });

  dashboardSocket.on("bgjob:update", () => scheduleRefresh());
  dashboardSocket.on("connect", () => scheduleRefresh());
  // Die Taskleiste entsteht schon vor der Anmeldung -> erst laden, wenn jemand
  // angemeldet ist (und nach einem Benutzerwechsel neu).
  let lastUser = null;
  setInterval(() => {
    const u = state.user?.username || null;
    if (u !== lastUser) { lastUser = u; jobs = []; seenRunning.clear(); if (u) refresh(); else render0(); }
  }, 1500);
  // Ersatz-Takt (falls der Socket hakt): langsam, nur solange etwas laeuft
  setInterval(() => { if (state.user && jobs.some((j) => j.running)) refresh(); }, 8000);
}

function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(refresh, 300);
}

function render0() {
  btn.querySelector(".jt-count").textContent = "";
  btn.classList.remove("jt-busy");
}

async function refresh() {
  if (!state.user) return;
  try {
    jobs = await listJobs({ history: true, limit: 25 });
  } catch { return; }
  const me = state.user?.username;
  // Selbst gestartete Jobs, die gerade fertig geworden sind -> Meldung
  for (const j of jobs) {
    if (j.running) { seenRunning.set(j.id, j); continue; }
    if (seenRunning.has(j.id)) {
      seenRunning.delete(j.id);
      if (j.by === me) {
        const type = j.status === "done" ? "success" : j.status === "error" ? "error" : "info";
        window.notify?.(`${j.title}: ${jobStatusText(j)}`, type);
      }
    }
  }
  const running = jobs.filter((j) => j.running).length;
  btn.querySelector(".jt-count").textContent = running ? String(running) : "";
  btn.classList.toggle("jt-busy", running > 0);
  btn.title = running ? `${running} Aufgabe(n) laufen` : "Aufgaben (Hintergrund-Jobs)";
  if (open) draw();
}

function icon(j) {
  if (j.running) return "⏳";
  return { done: "✅", error: "⛔", cancelled: "✋" }[j.status] || "•";
}

function draw() {
  if (!jobs.length) {
    panel.innerHTML = `<div style="padding:16px;color:var(--subtext)">Keine Aufgaben.</div>`;
    return;
  }
  const run = jobs.filter((j) => j.running), fin = jobs.filter((j) => !j.running);
  const item = (j) => {
    const pct = j.pct != null ? Math.max(2, j.pct) : null;
    return `<div class="jt-item" data-id="${esc(j.id)}">
      <div style="display:flex;gap:8px;align-items:flex-start">
        <span>${icon(j)}</span>
        <div style="flex:1;min-width:0">
          <div style="font-weight:600;word-break:break-word">${esc(j.title)}</div>
          <div style="font-size:11px;color:var(--subtext)">${esc(jobStatusText(j))}
            · ${esc(j.by || "")} · ${new Date(j.started).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" })}
            ${j.running ? "" : ` · ${jobDuration(j)}`}</div>
          ${j.running ? `<div class="jt-bar ${pct == null ? "jt-indef" : ""}"><div style="width:${pct ?? 30}%"></div></div>` : ""}
          <div class="jt-more" style="display:${expanded.has(j.id) ? "block" : "none"}"></div>
        </div>
        <div style="display:flex;gap:4px">
          <button class="btn btn-sm" data-act="more" title="Details/Log">${expanded.has(j.id) ? "▴" : "▾"}</button>
          ${j.running && !j.cancel_requested ? `<button class="btn btn-sm btn-danger" data-act="cancel" title="Abbrechen">✋</button>` : ""}
        </div>
      </div></div>`;
  };
  panel.innerHTML = `
    <div style="padding:10px 12px;font-weight:700;border-bottom:1px solid var(--border);display:flex">
      <span style="flex:1">Aufgaben</span>
      <span style="font-weight:400;color:var(--subtext);font-size:12px">${run.length} laufend</span></div>
    ${run.map(item).join("")}
    ${fin.length ? `<div style="padding:6px 12px;font-size:11px;color:var(--subtext);text-transform:uppercase;letter-spacing:.04em">Zuletzt beendet</div>` : ""}
    ${fin.map(item).join("")}`;

  panel.querySelectorAll(".jt-item").forEach((el) => {
    const id = el.dataset.id;
    el.querySelector('[data-act="cancel"]')?.addEventListener("click", async () => {
      try { await cancelJob(id); scheduleRefresh(); } catch (e) { window.notify?.(e.message, "error"); }
    });
    el.querySelector('[data-act="more"]').addEventListener("click", () => {
      expanded.has(id) ? expanded.delete(id) : expanded.add(id);
      draw();
    });
    if (expanded.has(id)) loadDetails(id, el.querySelector(".jt-more"));
  });
}

async function loadDetails(id, box) {
  try {
    const j = await fetchJob(id);
    const parts = [];
    if (j.error) parts.push(`<div style="color:var(--danger);margin-top:5px">${esc(j.error)}</div>`);
    if (j.detail && !j.running) parts.push(`<div style="margin-top:4px">${esc(j.detail)}</div>`);
    if (j.logs?.length) parts.push(`<pre>${esc(j.logs.slice(-80).join("\n"))}</pre>`);
    if (j.result != null && !j.running) {
      // Ergebnis lesbar: bekannte Textfelder direkt, sonst gekuerztes JSON
      const r = j.result;
      const txt = typeof r !== "object" ? String(r)
        : (r.reply || r.output || r.message || r.detail || r.stdout
          || JSON.stringify(r, null, 1).slice(0, 2500));
      if (txt && !(j.logs?.length && typeof r === "object" && !r.reply)) parts.push(`<pre>${esc(String(txt).slice(0, 4000))}</pre>`);
    }
    box.innerHTML = parts.join("") || `<div style="font-size:11px;color:var(--subtext);margin-top:4px">Keine weiteren Details.</div>`;
  } catch (e) { box.textContent = e.message; }
}
