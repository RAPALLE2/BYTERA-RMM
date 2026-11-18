// software.js (Modul "software") - Pakete verwalten + Multi-Client-Rollout
// -------------------------------------------------------------------------
// Links: Pakete (Installationsdateien + Kommando je Plattform).
// Rechts: Rollout - Clients anhaken (Suche, Tags), online = sofort,
// offline = Offline-Warteschlange (laeuft beim naechsten Online-Gehen).

import { moduleRequest } from "/js/app/modules.js";
import { getToken } from "/js/app/api.js";
import { BACKEND_URL } from "/js/app/config.js";
import { state } from "/js/app/state.js";
import { esc, uiConfirm } from "/js/app/utils.js";
import { displayName, osIcon, clientTags } from "/js/app/deviceicons.js";

export function renderSoftware(body, win) {
  let packages = [];
  let selPkg = null;
  const preselect = win.props?.preselect || null;

  body.innerHTML = `
    <div style="display:flex;height:100%;background:var(--panel);min-width:0">
      <div style="flex:1.1;min-width:0;display:flex;flex-direction:column;border-right:1px solid var(--border)">
        <div style="padding:10px 14px;border-bottom:1px solid var(--border);display:flex;gap:8px;align-items:center">
          <b>📦 Pakete</b><span style="flex:1"></span>
          <button class="btn-primary" id="sw-new">＋ Paket</button>
        </div>
        <div id="sw-list" style="flex:1;overflow:auto;padding:8px 14px"></div>
      </div>
      <div style="flex:1;min-width:300px;display:flex;flex-direction:column">
        <div style="padding:10px 14px;border-bottom:1px solid var(--border)"><b>🚚 Rollout</b>
          <span id="sw-selpkg" style="font-size:12px;color:var(--subtext)"></span></div>
        <div style="padding:8px 14px;display:flex;gap:6px">
          <input id="sw-filter" placeholder="Filter (Name, Tag, OS)…" style="flex:1">
          <button class="btn btn-sm" id="sw-all">Alle</button>
          <button class="btn btn-sm" id="sw-none">Keine</button>
        </div>
        <div id="sw-clients" style="flex:1;overflow:auto;padding:0 14px"></div>
        <div style="padding:10px 14px;border-top:1px solid var(--border);display:flex;gap:8px;align-items:center">
          <label style="font-size:12px;color:var(--subtext);display:flex;gap:4px;align-items:center">
            <input type="checkbox" id="sw-queue" checked> Offline-Clients vormerken</label>
          <span style="flex:1"></span>
          <button class="btn-primary" id="sw-deploy" disabled>🚚 Ausrollen</button>
        </div>
        <div id="sw-log" class="hidden" style="max-height:170px;overflow:auto;border-top:1px solid var(--border);
             padding:8px 14px;font-family:monospace;font-size:11.5px;white-space:pre-wrap"></div>
      </div>
    </div>`;

  const $ = (s) => body.querySelector(s);

  // ---------------- Pakete ----------------
  function fmtSize(n) { return n > 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.round(n / 1024)} KB`; }

  function drawPackages() {
    const box = $("#sw-list");
    if (!packages.length) {
      box.innerHTML = `<div style="color:var(--subtext);font-size:13px;padding:8px 0">
        Noch keine Pakete. „＋ Paket“ anlegen, Installationsdatei hochladen, Kommando
        angeben (oder leer lassen für Silent-Automatik bei MSI/EXE/DEB/RPM).</div>`;
      return;
    }
    box.innerHTML = packages.map((p) => `
      <div class="panel" style="padding:8px 10px;margin-bottom:6px;border:1px solid ${selPkg === p.id ? "var(--accent)" : "var(--border)"}">
        <div style="display:flex;gap:8px;align-items:center;cursor:pointer" data-sel="${esc(p.id)}">
          <b style="flex:1">📦 ${esc(p.name)}</b>
          <button class="btn btn-sm" data-edit="${esc(p.id)}">✏️</button>
          <button class="action-btn btn-danger" data-del="${esc(p.id)}">✕</button>
        </div>
        <div style="font-size:11.5px;color:var(--subtext);margin-top:3px">
          ${(p.files || []).map((f) => `<span style="margin-right:8px">📄 ${esc(f.name)} (${fmtSize(f.size || 0)})
            <a href="#" data-fdel="${esc(p.id)}|${esc(f.name)}" title="Datei entfernen">✕</a></span>`).join("") || "keine Dateien"}
          <label style="margin-left:4px;cursor:pointer;color:var(--accent)">⬆ Datei…
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
      if (!(await uiConfirm("Paket samt Dateien löschen?", { danger: true }))) return;
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
      window.notify?.(`Lade ${f.name} hoch …`, "info");
      try {
        // Direkter fetch: Multipart-Upload darf KEINEN JSON-Content-Type tragen.
        const res = await fetch(`${BACKEND_URL}/api/software/packages/${inp.dataset.fup}/files`, {
          method: "POST", body: fd,
          headers: { Authorization: `Bearer ${getToken()}` },
        });
        if (!res.ok) throw new Error((await res.json().catch(() => ({})))?.detail || `HTTP ${res.status}`);
        window.notify?.("Datei hochgeladen", "success");
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
    const name = await uiPrompt(pkg ? "Paketname" : "Name des neuen Pakets", { value: pkg?.name || "" });
    if (!name) return;
    const cmdWin = await uiPrompt("Installationskommando WINDOWS ({file}=Datei, {dir}=Ordner; leer = Silent-Automatik)",
      { value: pkg?.commands?.win || "" });
    if (cmdWin === null) return;
    const cmdLin = await uiPrompt("Installationskommando LINUX ({file}, {dir}; leer = Automatik)",
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

  // ---------------- Clients ----------------
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
      </label>`).join("") || `<div style="color:var(--subtext);font-size:13px;padding:8px 0">Keine Clients.</div>`;
  }
  $("#sw-filter").addEventListener("input", drawClients);
  $("#sw-all").addEventListener("click", () => body.querySelectorAll("[data-cl]").forEach((c) => { c.checked = true; }));
  $("#sw-none").addEventListener("click", () => body.querySelectorAll("[data-cl]").forEach((c) => { c.checked = false; }));

  // ---------------- Rollout ----------------
  $("#sw-deploy").addEventListener("click", async () => {
    const clients = [...body.querySelectorAll("[data-cl]:checked")].map((c) => c.dataset.cl);
    if (!selPkg || !clients.length) { window.notify?.("Paket wählen und Clients anhaken", "info"); return; }
    const pkg = packages.find((p) => p.id === selPkg);
    if (!(await uiConfirm(`„${pkg?.name}“ jetzt auf ${clients.length} Client(s) ausrollen?`))) return;
    const log = $("#sw-log");
    log.classList.remove("hidden");
    log.textContent = "Starte Rollout …";
    $("#sw-deploy").disabled = true;
    try {
      await moduleRequest("/api/software/deploy", {
        method: "POST",
        body: JSON.stringify({ package_id: selPkg, clients, queue_offline: $("#sw-queue").checked }),
        onJob: (job) => {
          if (Array.isArray(job.logs) && job.logs.length) { log.textContent = job.logs.join("\n"); log.scrollTop = log.scrollHeight; }
        },
      });
      log.textContent += "\n✔ Rollout abgeschlossen.";
    } catch (e) { log.textContent += `\n❌ ${e.message}`; }
    $("#sw-deploy").disabled = false;
  });

  async function load() {
    try { packages = await moduleRequest("/api/software/packages"); } catch (e) { packages = []; window.notify?.(e.message, "error"); }
    drawPackages();
    drawClients();
  }
  load();
}
