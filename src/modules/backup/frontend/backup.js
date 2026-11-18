// backup.js (Modul "backup") - Backup & Snapshots im Proxmox-Stil
// ----------------------------------------------------------------
// Reiter: Backups | Snapshots (je Client) | Backup-Jobs | Speicher (Admin)

import { moduleRequest } from "/js/app/modules.js";
import { state } from "/js/app/state.js";
import { esc, uiConfirm } from "/js/app/utils.js";
import { displayName, osIcon } from "/js/app/deviceicons.js";

const COMP = { none: "Keine", fast: "Schnell (wie LZO)", good: "Gut (wie GZIP)", zstd: "Schnell & gut (ZSTD)" };
const ROUTE = { direct: "Direkt vom Client", backend: "Aufs Backend", via_backend: "Über das Backend" };
const ROUTES_FOR = { backend: ["backend"], smb: ["direct", "via_backend"], sftp: ["direct", "via_backend"],
                     ftp: ["via_backend"], pbs: ["direct"] };
const TYPE = { backend: "Backend (lokal)", smb: "SMB / CIFS", sftp: "SFTP", ftp: "FTP / FTPS", pbs: "Proxmox Backup Server" };
const KEEP = [["keep_last", "Letzte"], ["keep_hourly", "Stündlich"], ["keep_daily", "Täglich"],
              ["keep_weekly", "Wöchentlich"], ["keep_monthly", "Monatlich"], ["keep_yearly", "Jährlich"]];
const DAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];

const fmtSize = (n) => (n == null ? "" : n > 1073741824 ? `${(n / 1073741824).toFixed(2)} GB`
  : n > 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.round(n / 1024)} KB`);
const fmtTime = (ms) => (ms ? new Date(ms).toLocaleString() : "");
const clientName = (id) => { const c = (state.clients || []).find((x) => x.id === id); return c ? displayName(c) : id; };

function modal(title, html, { width = 560 } = {}) {
  const ov = document.createElement("div");
  ov.style.cssText = "position:fixed;inset:0;z-index:11000;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center";
  ov.innerHTML = `<div style="background:var(--panel);border:1px solid var(--border);border-radius:12px;width:${width}px;max-width:95vw;max-height:90vh;overflow:auto;padding:16px">
    <div style="display:flex;align-items:center;margin-bottom:10px"><h3 style="margin:0;flex:1">${title}</h3>
    <button class="btn" data-x>✕</button></div><div data-body>${html}</div></div>`;
  document.body.appendChild(ov);
  const close = () => ov.remove();
  ov.querySelector("[data-x]").addEventListener("click", close);
  return { el: ov.querySelector("[data-body]"), close };
}

function scheduleText(s) {
  if (!s || s.type === "manual") return "manuell";
  if (s.type === "hourly") return `alle ${s.every || 1} h`;
  if (s.type === "daily") return `täglich ${s.time || "02:00"}`;
  return `${(s.days || []).map((d) => DAYS[d - 1]).join(",")} ${s.time || ""}`;
}

export function renderBackup(body, win) {
  let tab = win.props?.tab || "backups";
  let cid = win.props?.clientId || "";
  let storages = [];
  let isAdmin = false;

  body.innerHTML = `
    <div style="display:flex;flex-direction:column;height:100%;background:var(--panel)">
      <div style="display:flex;gap:4px;padding:8px 12px;border-bottom:1px solid var(--border);align-items:center;flex-wrap:wrap">
        <button class="btn" data-tab="backups">💾 Backups</button>
        <button class="btn" data-tab="snapshots">📸 Snapshots</button>
        <button class="btn" data-tab="jobs">🗓️ Backup-Jobs</button>
        <button class="btn" data-tab="storage">🗄️ Speicher</button>
        <span style="flex:1"></span>
        <select id="bk-client" style="min-width:220px"></select>
      </div>
      <div id="bk-main" style="flex:1;overflow:auto;padding:12px 14px"></div>
    </div>`;
  const main = body.querySelector("#bk-main");
  const sel = body.querySelector("#bk-client");

  function drawClientSel() {
    const list = [...(state.clients || [])].sort((a, b) => displayName(a).localeCompare(displayName(b)));
    if (!cid && list.length) cid = list[0].id;
    sel.innerHTML = list.map((c) => `<option value="${esc(c.id)}" ${c.id === cid ? "selected" : ""}>
      ${osIcon(c)} ${esc(displayName(c))}${c.online ? "" : " (offline)"}</option>`).join("");
    sel.style.display = ["backups", "snapshots"].includes(tab) ? "" : "none";
  }
  sel.addEventListener("change", () => { cid = sel.value; render(); });
  body.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => { tab = b.dataset.tab; render(); }));

  async function loadStorages() {
    try { storages = await moduleRequest("/api/backup/storages"); isAdmin = true; }
    catch { storages = []; isAdmin = false; }
  }

  function render() {
    body.querySelectorAll("[data-tab]").forEach((b) => {
      b.className = b.dataset.tab === tab ? "btn-primary" : "btn";
      if (["jobs", "storage"].includes(b.dataset.tab)) b.style.display = isAdmin ? "" : "none";
    });
    drawClientSel();
    ({ backups: renderBackups, snapshots: renderSnapshots, jobs: renderJobs, storage: renderStorage })[tab]();
  }

  // ================================================================ BACKUPS
  async function renderBackups() {
    if (!cid) { main.innerHTML = `<p style="color:var(--subtext)">Keine Clients vorhanden.</p>`; return; }
    main.innerHTML = `<p style="color:var(--subtext)">Lade Backups …</p>`;
    let d;
    try { d = await moduleRequest(`/api/backup/clients/${cid}/backups`); }
    catch (e) { main.innerHTML = `<div class="form-error">${esc(e.message)}</div>`; return; }
    const usedStorages = [...new Set(d.backups.filter((b) => b.engine !== "pve").map((b) => b.storage))];
    main.innerHTML = `
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap">
        <button class="btn-primary" id="bk-now">💾 Jetzt sichern</button>
        ${usedStorages.map((s) => `<button class="btn btn-sm" data-refresh="${esc(s)}" title="Liste vom Speicherziel neu einlesen (Client muss online sein)">⟳ ${esc(s)}</button>`).join("")}
        <span style="flex:1"></span>
        ${d.pve ? `<span style="font-size:12px;color:var(--subtext)">🟧 Proxmox-${d.pve.type === "qemu" ? "VM" : "LXC"} ${esc(String(d.pve.vmid))} auf ${esc(d.pve.node)}</span>` : ""}
      </div>
      ${d.pve_error ? `<div class="form-error">Proxmox: ${esc(d.pve_error)}</div>` : ""}
      <table style="width:100%;border-collapse:collapse;font-size:13px">
        <thead><tr style="text-align:left;color:var(--subtext);font-size:12px">
          <th>Zeit</th><th>Name / Notizen</th><th>Art</th><th>Ziel</th><th>Größe</th><th></th><th></th></tr></thead>
        <tbody>${d.backups.map((b, i) => `
          <tr style="border-top:1px solid var(--border)">
            <td style="padding:6px 4px;white-space:nowrap">${esc(fmtTime(b.time))}</td>
            <td>${esc(b.name || b.notes || "")}${b.notes && b.name && b.notes !== b.name ? `<div style="font-size:11px;color:var(--subtext)">${esc(b.notes)}</div>` : ""}</td>
            <td>${b.engine === "pve" ? "vzdump" : b.mode === "image" ? "💽 Image" : "📁 Ordner"}</td>
            <td style="font-size:12px">${esc(b.storage_label || b.storage)}</td>
            <td style="white-space:nowrap">${esc(fmtSize(b.size))}</td>
            <td>${b.protected ? "🔒" : ""}</td>
            <td style="white-space:nowrap;text-align:right">
              <button class="btn btn-sm" data-restore="${i}">↩ Wiederherstellen</button>
              ${b.engine === "restic" && b.mode !== "image" ? `<button class="btn btn-sm" data-browse="${i}" title="Dateien ansehen">📂</button>` : ""}
              <button class="btn btn-sm" data-edit="${i}" title="Name / Notizen">✏️</button>
              <button class="btn btn-sm" data-protect="${i}" title="${b.protected ? "Schutz aufheben" : "Schützen (wird von der Aufbewahrung nie gelöscht)"}">${b.protected ? "🔓" : "🔒"}</button>
              <button class="action-btn btn-danger" data-del="${i}" title="Löschen">✕</button>
            </td></tr>`).join("") || `<tr><td colspan="7" style="color:var(--subtext);padding:10px 4px">Noch keine Backups.</td></tr>`}
        </tbody></table>`;

    main.querySelector("#bk-now").addEventListener("click", () => backupNowDialog(d));
    main.querySelectorAll("[data-refresh]").forEach((b) => b.addEventListener("click", async () => {
      try {
        const r = await moduleRequest(`/api/backup/clients/${cid}/refresh`, { method: "POST", noJob: true,
          body: JSON.stringify({ storage: b.dataset.refresh }) });
        window.notify?.(`${r.count} Backup(s) eingelesen`, "success"); renderBackups();
      } catch (e) { window.notify?.(e.message, "error"); }
    }));
    const pick = (el, k) => d.backups[+el.dataset[k]];
    main.querySelectorAll("[data-restore]").forEach((b) => b.addEventListener("click", () => restoreDialog(pick(b, "restore"), d)));
    main.querySelectorAll("[data-browse]").forEach((b) => b.addEventListener("click", () => browseDialog(pick(b, "browse"), d)));
    main.querySelectorAll("[data-edit]").forEach((b) => b.addEventListener("click", () => editMetaDialog(pick(b, "edit"))));
    main.querySelectorAll("[data-protect]").forEach((b) => b.addEventListener("click", async () => {
      const it = pick(b, "protect");
      try {
        await moduleRequest(`/api/backup/clients/${cid}/backups`, { method: "PATCH", noJob: true,
          body: JSON.stringify({ storage: it.storage, id: it.id, protected: !it.protected }) });
        renderBackups();
      } catch (e) { window.notify?.(e.message, "error"); }
    }));
    main.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
      const it = pick(b, "del");
      if (!(await uiConfirm(`Backup vom ${fmtTime(it.time)} endgültig löschen?`, { danger: true }))) return;
      try {
        await moduleRequest(`/api/backup/clients/${cid}/backups/delete`, { method: "POST", noJob: true,
          body: JSON.stringify({ storage: it.storage, id: it.id }) });
        window.notify?.("Backup gelöscht", "success"); renderBackups();
      } catch (e) { window.notify?.(e.message, "error"); }
    }));
  }

  function storageOptions(selected) {
    return storages.filter((s) => s.enabled !== false).map((s) =>
      `<option value="${esc(s.id)}" ${s.id === selected ? "selected" : ""}>${esc(s.label || s.id)} (${esc(TYPE[s.type] || s.type)})</option>`).join("");
  }
  function routeOptions(sid, selected) {
    const st = storages.find((s) => s.id === sid);
    return (ROUTES_FOR[st?.type] || []).map((r) =>
      `<option value="${r}" ${r === selected ? "selected" : ""}>${ROUTE[r]}</option>`).join("");
  }
  const compOptions = (v = "zstd") => Object.entries(COMP).map(([k, l]) =>
    `<option value="${k}" ${k === v ? "selected" : ""}>${l}</option>`).join("");

  function backupNowDialog(d) {
    const m = modal("💾 Jetzt sichern", `
      ${d.pve ? `<div class="form-row"><label>Methode</label><select id="bn-engine" style="width:100%">
        <option value="agent">BYTERA-Agent (Ordner/Image)</option>
        <option value="pve">Proxmox vzdump (ganze ${d.pve.type === "qemu" ? "VM" : "LXC"})</option></select></div>` : ""}
      <div data-agent>
        ${isAdmin ? "" : `<p style="font-size:12px;color:var(--warn)">Speicherziele kann nur ein Admin verwalten.</p>`}
        <div style="display:flex;gap:8px"><div class="form-row" style="flex:1"><label>Speicher</label><select id="bn-storage" style="width:100%">${storageOptions("local")}</select></div>
        <div class="form-row" style="flex:1"><label>Weg</label><select id="bn-route" style="width:100%"></select></div></div>
        <div style="display:flex;gap:8px"><div class="form-row" style="flex:1"><label>Modus</label><select id="bn-mode" style="width:100%">
          <option value="files">📁 Ordner/Dateien</option><option value="image">💽 Image (ganzer Datenträger)</option></select></div>
        <div class="form-row" style="flex:1"><label>Komprimierung</label><select id="bn-comp" style="width:100%">${compOptions()}</select></div></div>
        <div class="form-row" data-files><label>Ordner (eine Zeile je Ordner; leer = Standard: Benutzerprofile bzw. /etc /home /root)</label>
          <textarea id="bn-paths" rows="3" style="width:100%"></textarea></div>
        <div class="form-row" data-files><label>Ausschlüsse (Muster, eine Zeile je Muster)</label><textarea id="bn-ex" rows="2" style="width:100%" placeholder="*.tmp&#10;**/node_modules"></textarea></div>
        <div class="form-row" data-image style="display:none"><label>Datenträger (nur Linux, leer = Systemplatte; Windows: immer C: per VSS)</label><input id="bn-dev" placeholder="/dev/sda" style="width:100%"></div>
      </div>
      <div data-pve style="display:none">
        <div class="form-row"><label>Proxmox-Storage</label><select id="bn-pvest" style="width:100%">${(d.pve_storages || []).map((s) => `<option>${esc(s)}</option>`).join("")}</select></div>
        <div style="display:flex;gap:8px"><div class="form-row" style="flex:1"><label>Modus</label><select id="bn-pvemode" style="width:100%"><option value="snapshot">Snapshot</option><option value="suspend">Suspend</option><option value="stop">Stop</option></select></div>
        <div class="form-row" style="flex:1"><label>Komprimierung</label><select id="bn-pvecomp" style="width:100%">${compOptions()}</select></div></div>
      </div>
      <div class="form-row"><label>Name</label><input id="bn-name" style="width:100%" placeholder="z.B. vor Update"></div>
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:10px">
        <button class="btn-primary" id="bn-go">Sichern</button></div>`);
    const $ = (s) => m.el.querySelector(s);
    const syncRoute = () => { $("#bn-route").innerHTML = routeOptions($("#bn-storage").value); };
    syncRoute();
    $("#bn-storage").addEventListener("change", syncRoute);
    $("#bn-mode").addEventListener("change", () => {
      const img = $("#bn-mode").value === "image";
      m.el.querySelectorAll("[data-files]").forEach((e) => { e.style.display = img ? "none" : ""; });
      m.el.querySelector("[data-image]").style.display = img ? "" : "none";
    });
    $("#bn-engine")?.addEventListener("change", () => {
      const pve = $("#bn-engine").value === "pve";
      m.el.querySelector("[data-agent]").style.display = pve ? "none" : "";
      m.el.querySelector("[data-pve]").style.display = pve ? "" : "none";
    });
    $("#bn-go").addEventListener("click", async () => {
      const pve = $("#bn-engine")?.value === "pve";
      const lines = (id) => $(id).value.split("\n").map((x) => x.trim()).filter(Boolean);
      const payload = pve
        ? { engine: "pve", pve_storage: $("#bn-pvest").value, pve_mode: $("#bn-pvemode").value,
            compression: $("#bn-pvecomp").value, name: $("#bn-name").value }
        : { engine: "agent", storage: $("#bn-storage").value, route: $("#bn-route").value,
            mode: $("#bn-mode").value, paths: lines("#bn-paths"), excludes: lines("#bn-ex"),
            image_device: $("#bn-dev").value.trim(), compression: $("#bn-comp").value, name: $("#bn-name").value };
      m.close();
      try {
        await moduleRequest(`/api/backup/clients/${cid}/backup`, { method: "POST", body: JSON.stringify(payload) });
        window.notify?.("Backup abgeschlossen", "success");
      } catch (e) { window.notify?.(e.message, "error"); }
      renderBackups();
    });
  }

  function restoreDialog(b, d, include = []) {
    const isPve = b.engine === "pve";
    const isImg = b.mode === "image";
    const clients = [...(state.clients || [])];
    const m = modal("↩ Wiederherstellen", `
      <p style="font-size:12px;color:var(--subtext)">Backup vom <b>${esc(fmtTime(b.time))}</b> ${esc(b.name || "")}</p>
      ${isPve ? `
        <div class="form-row"><label>Ziel-VMID (leer = ${esc(String(d.pve.vmid))}, überschreibt - Gast muss gestoppt sein)</label>
          <input id="rs-vmid" type="number" style="width:100%"></div>
        <label style="font-size:13px"><input type="checkbox" id="rs-start"> Nach dem Wiederherstellen starten</label>`
      : `
        <div class="form-row"><label>Auf Client</label><select id="rs-client" style="width:100%">
          ${clients.map((c) => `<option value="${esc(c.id)}" ${c.id === cid ? "selected" : ""}>${osIcon(c)} ${esc(displayName(c))}</option>`).join("")}</select></div>
        ${isImg ? `
          <div class="form-row"><label>Ziel (Datei, z.B. D:\\restore\\c.img oder /mnt/x/disk.img)</label><input id="rs-img" style="width:100%"></div>
          <label style="font-size:13px"><input type="checkbox" id="rs-dev"> Direkt auf einen Datenträger schreiben (nur Linux, Ziel z.B. /dev/sdb - darf NICHT eingehängt sein!)</label>
          <p style="font-size:11px;color:var(--subtext)">Ein komplettes System-Image wird für den Bare-Metal-Restore typischerweise aus einem Rettungssystem zurückgespielt.</p>`
        : `
          <div class="form-row"><label>Wohin?</label><select id="rs-target" style="width:100%">
            <option value="original">An den Originalort (überschreibt vorhandene Dateien)</option>
            <option value="path">In einen Ordner</option></select></div>
          <div class="form-row" id="rs-pathrow" style="display:none"><label>Zielordner</label><input id="rs-path" style="width:100%"></div>
          ${include.length ? `<p style="font-size:12px">Nur ausgewählt: <code>${include.map(esc).join("</code>, <code>")}</code></p>` : ""}`}`}
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:10px"><button class="btn-primary" id="rs-go">Wiederherstellen</button></div>`);
    const $ = (s) => m.el.querySelector(s);
    $("#rs-target")?.addEventListener("change", () => { $("#rs-pathrow").style.display = $("#rs-target").value === "path" ? "" : "none"; });
    $("#rs-go").addEventListener("click", async () => {
      if (!(await uiConfirm("Wiederherstellung jetzt starten? Vorhandene Daten am Ziel können überschrieben werden.", { danger: true }))) return;
      const payload = { storage: b.storage, id: b.id, include };
      if (isPve) { payload.pve_vmid = $("#rs-vmid").value ? +$("#rs-vmid").value : null; payload.pve_start = $("#rs-start").checked; }
      else {
        payload.target_client = $("#rs-client").value;
        if (isImg) { payload.image_dest = $("#rs-img").value.trim(); payload.image_to_device = $("#rs-dev").checked; }
        else { payload.target = $("#rs-target").value; payload.path = $("#rs-path").value.trim(); }
      }
      m.close();
      try {
        await moduleRequest(`/api/backup/clients/${cid}/restore`, { method: "POST", body: JSON.stringify(payload) });
        window.notify?.("Wiederherstellung abgeschlossen", "success");
      } catch (e) { window.notify?.(e.message, "error"); }
    });
  }

  function browseDialog(b, d) {
    const m = modal(`📂 Dateien - ${esc(fmtTime(b.time))}`, `<div id="br-path" style="font-family:monospace;font-size:12px;margin-bottom:6px"></div>
      <div id="br-list" style="max-height:52vh;overflow:auto;font-size:13px"></div>
      <div style="display:flex;gap:8px;margin-top:10px;align-items:center"><span id="br-count" style="flex:1;font-size:12px;color:var(--subtext)"></span>
      <button class="btn-primary" id="br-go" disabled>↩ Auswahl wiederherstellen</button></div>`, { width: 680 });
    const chosen = new Set();
    const $ = (s) => m.el.querySelector(s);
    async function go(path) {
      $("#br-path").textContent = path;
      $("#br-list").innerHTML = "Lade …";
      try {
        const r = await moduleRequest(`/api/backup/clients/${cid}/ls?storage=${encodeURIComponent(b.storage)}&id=${encodeURIComponent(b.id)}&path=${encodeURIComponent(path)}`);
        const up = path !== "/" ? `<div style="padding:3px 0;cursor:pointer" data-go="${esc(path.replace(/\/[^/]*\/?$/, "") || "/")}">⬆ ..</div>` : "";
        $("#br-list").innerHTML = up + r.entries.map((e) => `
          <div style="display:flex;gap:6px;align-items:center;padding:3px 0;border-bottom:1px solid var(--border)">
            <input type="checkbox" data-pick="${esc(e.path)}" ${chosen.has(e.path) ? "checked" : ""}>
            <span style="flex:1;cursor:${e.type === "dir" ? "pointer" : "default"}" ${e.type === "dir" ? `data-go="${esc(e.path)}"` : ""}>${e.type === "dir" ? "📁" : "📄"} ${esc(e.name)}</span>
            <span style="font-size:11px;color:var(--subtext)">${esc(fmtSize(e.size))}</span></div>`).join("");
        m.el.querySelectorAll("[data-go]").forEach((x) => x.addEventListener("click", () => go(x.dataset.go)));
        m.el.querySelectorAll("[data-pick]").forEach((x) => x.addEventListener("change", () => {
          if (x.checked) chosen.add(x.dataset.pick); else chosen.delete(x.dataset.pick);
          $("#br-count").textContent = `${chosen.size} ausgewählt`; $("#br-go").disabled = !chosen.size;
        }));
      } catch (e) { $("#br-list").innerHTML = `<div class="form-error">${esc(e.message)}</div>`; }
    }
    $("#br-go").addEventListener("click", () => { m.close(); restoreDialog(b, d, [...chosen]); });
    go((b.paths && b.paths.length === 1) ? b.paths[0] : "/");
  }

  function editMetaDialog(b) {
    const m = modal("✏️ Name & Notizen", `
      <div class="form-row"><label>Name</label><input id="em-name" style="width:100%" value="${esc(b.name || "")}"></div>
      <div class="form-row"><label>Notizen</label><textarea id="em-notes" rows="3" style="width:100%">${esc(b.notes || "")}</textarea></div>
      <div style="display:flex;justify-content:flex-end"><button class="btn-primary" id="em-go">Speichern</button></div>`);
    m.el.querySelector("#em-go").addEventListener("click", async () => {
      try {
        await moduleRequest(`/api/backup/clients/${cid}/backups`, { method: "PATCH", noJob: true,
          body: JSON.stringify({ storage: b.storage, id: b.id, name: m.el.querySelector("#em-name").value,
                                 notes: m.el.querySelector("#em-notes").value }) });
        m.close(); renderBackups();
      } catch (e) { window.notify?.(e.message, "error"); }
    });
  }

  // ================================================================ SNAPSHOTS
  async function renderSnapshots() {
    if (!cid) { main.innerHTML = ""; return; }
    main.innerHTML = `<p style="color:var(--subtext)">Lade Snapshots …</p>`;
    let d;
    try { d = await moduleRequest(`/api/backup/clients/${cid}/snapshots`); }
    catch (e) { main.innerHTML = `<div class="form-error">${esc(e.message)}</div>`; return; }
    const pve = d.kind === "pve";
    const canRollback = pve || d.kind === "zfs" || d.kind === "lvm";
    main.innerHTML = `
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
        <button class="btn-primary" id="sn-new">📸 Snapshot erstellen</button>
        <span style="font-size:12px;color:var(--subtext);flex:1">${pve ? `Proxmox-Snapshots der ${d.pve.type === "qemu" ? "VM" : "LXC"} ${esc(String(d.pve.vmid))}` : esc(d.info || "")}</span>
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:13px">
        <thead><tr style="text-align:left;color:var(--subtext);font-size:12px"><th>Name</th><th>Zeit</th><th>${pve ? "RAM" : "Volume"}</th><th>Notizen</th><th></th></tr></thead>
        <tbody>${d.snapshots.map((s, i) => `<tr style="border-top:1px solid var(--border)">
          <td style="padding:6px 4px">${esc(s.name || s.id)}</td><td>${esc(fmtTime(s.time))}</td>
          <td>${pve ? (s.vmstate ? "✓" : "") : esc(s.volume || "")}</td><td style="font-size:12px">${esc(s.notes || "")}</td>
          <td style="text-align:right;white-space:nowrap">
            ${canRollback ? `<button class="btn btn-sm" data-rb="${i}">⏪ Zurücksetzen</button>` : ""}
            ${!pve ? `<button class="btn btn-sm" data-rp="${i}" title="Datei/Ordner aus dem Snapshot zurückholen">↩ Datei</button>` : ""}
            <button class="action-btn btn-danger" data-sdel="${i}">✕</button></td></tr>`).join("")
          || `<tr><td colspan="5" style="color:var(--subtext);padding:10px 4px">Keine Snapshots.</td></tr>`}</tbody></table>`;
    const act = async (s, action, extra = {}) => {
      try {
        const r = await moduleRequest(`/api/backup/clients/${cid}/snapshots/action`, { method: "POST", noJob: true,
          body: JSON.stringify({ id: s.id, action, ...extra }) });
        window.notify?.(r.note || "Erledigt", "success"); renderSnapshots();
      } catch (e) { window.notify?.(e.message, "error"); }
    };
    main.querySelector("#sn-new").addEventListener("click", () => {
      const m = modal("📸 Snapshot erstellen", `
        <div class="form-row"><label>Name${pve ? " (Buchstabe am Anfang, a-z 0-9 - _)" : ""}</label><input id="sn-name" style="width:100%" value="vor-update"></div>
        ${pve ? `<div class="form-row"><label>Beschreibung</label><input id="sn-notes" style="width:100%"></div>
          ${d.pve.type === "qemu" ? `<label style="font-size:13px"><input type="checkbox" id="sn-ram"> RAM einschließen</label>` : ""}`
        : d.kind === "vss" ? `<div class="form-row"><label>Laufwerk</label><input id="sn-vol" style="width:100%" value="C:"></div>` : ""}
        <div style="display:flex;justify-content:flex-end;margin-top:10px"><button class="btn-primary" id="sn-go">Erstellen</button></div>`);
      m.el.querySelector("#sn-go").addEventListener("click", async () => {
        const v = (s) => m.el.querySelector(s);
        try {
          await moduleRequest(`/api/backup/clients/${cid}/snapshots`, { method: "POST", noJob: true,
            body: JSON.stringify({ name: v("#sn-name").value.trim(), notes: v("#sn-notes")?.value || "",
                                   vmstate: !!v("#sn-ram")?.checked, volume: v("#sn-vol")?.value || "" }) });
          m.close(); window.notify?.("Snapshot erstellt", "success"); renderSnapshots();
        } catch (e) { window.notify?.(e.message, "error"); }
      });
    });
    main.querySelectorAll("[data-rb]").forEach((b) => b.addEventListener("click", async () => {
      const s = d.snapshots[+b.dataset.rb];
      if (await uiConfirm(`Auf Snapshot „${s.name || s.id}“ zurücksetzen? Alle späteren Änderungen gehen verloren.`, { danger: true })) act(s, "rollback");
    }));
    main.querySelectorAll("[data-rp]").forEach((b) => b.addEventListener("click", () => {
      const s = d.snapshots[+b.dataset.rp];
      const m = modal("↩ Datei/Ordner aus Snapshot", `
        <div class="form-row"><label>Pfad (wie er auf dem Gerät heißt)</label><input id="rp-path" style="width:100%" placeholder="${d.kind === "vss" ? "C:\\Users\\max\\Dokumente" : "/etc/nginx"}"></div>
        <div class="form-row"><label>Ziel (leer = Originalort überschreiben)</label><input id="rp-dest" style="width:100%"></div>
        <div style="display:flex;justify-content:flex-end"><button class="btn-primary" id="rp-go">Zurückholen</button></div>`);
      m.el.querySelector("#rp-go").addEventListener("click", () => {
        const path = m.el.querySelector("#rp-path").value.trim();
        const dest = m.el.querySelector("#rp-dest").value.trim();
        m.close(); act(s, "restore-path", { path, dest });
      });
    }));
    main.querySelectorAll("[data-sdel]").forEach((b) => b.addEventListener("click", async () => {
      const s = d.snapshots[+b.dataset.sdel];
      if (await uiConfirm(`Snapshot „${s.name || s.id}“ löschen?`, { danger: true })) act(s, "delete");
    }));
  }

  // ================================================================ JOBS
  async function renderJobs() {
    main.innerHTML = `<p style="color:var(--subtext)">Lade Jobs …</p>`;
    let jobs;
    try { jobs = await moduleRequest("/api/backup/jobs"); }
    catch (e) { main.innerHTML = `<div class="form-error">${esc(e.message)}</div>`; return; }
    main.innerHTML = `
      <div style="margin-bottom:8px"><button class="btn-primary" id="jb-new">＋ Backup-Job</button></div>
      <table style="width:100%;border-collapse:collapse;font-size:13px">
        <thead><tr style="text-align:left;color:var(--subtext);font-size:12px"><th></th><th>Name</th><th>Zeitplan</th><th>Ziele</th><th>Speicher / Weg</th><th>Aufbewahrung</th><th>Letzter Lauf</th><th></th></tr></thead>
        <tbody>${jobs.map((j, i) => `<tr style="border-top:1px solid var(--border)">
          <td>${j.enabled === false ? "⏸" : "✅"}</td>
          <td style="padding:6px 4px">${esc(j.name)}<div style="font-size:11px;color:var(--subtext)">${j.engine === "pve" ? "Proxmox vzdump" : j.mode === "image" ? "💽 Image" : "📁 Ordner"} · ${esc(COMP[j.compression] || "")}</div></td>
          <td>${esc(scheduleText(j.schedule))}</td>
          <td title="${esc(j.targets.map(clientName).join(", "))}">${j.targets.length} Client(s)${(j.tags || []).length ? `<div style="font-size:11px;color:var(--subtext)">🏷 ${esc(j.tags.join(", "))}</div>` : ""}</td>
          <td style="font-size:12px">${j.engine === "pve" ? esc(j.pve_storage) : `${esc(j.storage)} · ${esc(ROUTE[j.route] || "")}`}</td>
          <td style="font-size:12px">${esc(Object.entries(j.retention || {}).filter(([, v]) => +v).map(([k, v]) => `${k.replace("keep_", "")}=${v}`).join(", ") || "alle")}</td>
          <td style="font-size:12px">${esc(fmtTime(j.last_run))}<div style="color:${(j.last_status || "").startsWith("ok") ? "var(--online)" : "var(--danger)"}">${esc(j.last_status || "")}</div></td>
          <td style="text-align:right;white-space:nowrap"><button class="btn btn-sm" data-run="${i}">▶ Jetzt</button>
            <button class="btn btn-sm" data-jedit="${i}">✏️</button><button class="action-btn btn-danger" data-jdel="${i}">✕</button></td></tr>`).join("")
          || `<tr><td colspan="8" style="color:var(--subtext);padding:10px 4px">Noch keine Backup-Jobs.</td></tr>`}</tbody></table>`;
    main.querySelector("#jb-new").addEventListener("click", () => jobDialog(null));
    main.querySelectorAll("[data-jedit]").forEach((b) => b.addEventListener("click", () => jobDialog(jobs[+b.dataset.jedit])));
    main.querySelectorAll("[data-jdel]").forEach((b) => b.addEventListener("click", async () => {
      const j = jobs[+b.dataset.jdel];
      if (!(await uiConfirm(`Job „${j.name}“ löschen? Vorhandene Backups bleiben erhalten.`, { danger: true }))) return;
      await moduleRequest(`/api/backup/jobs/${j.id}`, { method: "DELETE", noJob: true }); renderJobs();
    }));
    main.querySelectorAll("[data-run]").forEach((b) => b.addEventListener("click", async () => {
      const j = jobs[+b.dataset.run];
      try { await moduleRequest(`/api/backup/jobs/${j.id}/run`, { method: "POST" }); window.notify?.(`Job „${j.name}“ ausgeführt`, "success"); }
      catch (e) { window.notify?.(e.message, "error"); }
      renderJobs();
    }));
  }

  function jobDialog(j) {
    j = j || { name: "", enabled: true, engine: "agent", clients: [], tags: [], storage: "local", route: "backend",
               mode: "files", paths: [], excludes: [], image_device: "", compression: "zstd",
               schedule: { type: "daily", time: "02:00", days: [1, 2, 3, 4, 5, 6, 7], every: 4 },
               repeat_missed: true, retention: { keep_last: 3 }, name_template: "{{client}} {{date}} {{time}}",
               pve_storage: "", pve_mode: "snapshot" };
    const sch = j.schedule || {};
    const clients = [...(state.clients || [])].sort((a, b) => displayName(a).localeCompare(displayName(b)));
    const m = modal(j.id ? "✏️ Backup-Job" : "＋ Backup-Job", `
      <div style="display:flex;gap:8px"><div class="form-row" style="flex:2"><label>Name</label><input id="jf-name" style="width:100%" value="${esc(j.name)}"></div>
        <div class="form-row" style="flex:1"><label>Methode</label><select id="jf-engine" style="width:100%">
          <option value="agent" ${j.engine === "agent" ? "selected" : ""}>BYTERA-Agent</option>
          <option value="pve" ${j.engine === "pve" ? "selected" : ""}>Proxmox vzdump</option></select></div></div>
      <label style="font-size:13px"><input type="checkbox" id="jf-on" ${j.enabled !== false ? "checked" : ""}> Aktiv</label>

      <h4 style="margin:12px 0 4px">Clients</h4>
      <input id="jf-filter" placeholder="Filter …" style="width:100%;margin-bottom:4px">
      <div id="jf-clients" style="max-height:140px;overflow:auto;border:1px solid var(--border);border-radius:6px;padding:4px 8px">
        ${clients.map((c) => `<label style="display:block;font-size:13px" data-cname="${esc(displayName(c).toLowerCase())}">
          <input type="checkbox" value="${esc(c.id)}" ${(j.clients || []).includes(c.id) ? "checked" : ""}> ${osIcon(c)} ${esc(displayName(c))}</label>`).join("")}</div>
      <div class="form-row"><label>…und alle Clients mit diesen Tags (kommagetrennt)</label><input id="jf-tags" style="width:100%" value="${esc((j.tags || []).join(", "))}"></div>

      <div data-agent>
        <div style="display:flex;gap:8px"><div class="form-row" style="flex:1"><label>Speicher</label><select id="jf-storage" style="width:100%">${storageOptions(j.storage)}</select></div>
          <div class="form-row" style="flex:1"><label>Weg</label><select id="jf-route" style="width:100%"></select></div></div>
        <div style="display:flex;gap:8px"><div class="form-row" style="flex:1"><label>Modus</label><select id="jf-mode" style="width:100%">
          <option value="files" ${j.mode === "files" ? "selected" : ""}>📁 Ordner/Dateien</option>
          <option value="image" ${j.mode === "image" ? "selected" : ""}>💽 Image</option></select></div>
          <div class="form-row" style="flex:1"><label>Komprimierung</label><select id="jf-comp" style="width:100%">${compOptions(j.compression)}</select></div></div>
        <div class="form-row"><label>Ordner (eine Zeile je Ordner; leer = Standard)</label><textarea id="jf-paths" rows="2" style="width:100%">${esc((j.paths || []).join("\n"))}</textarea></div>
        <div class="form-row"><label>Ausschlüsse</label><textarea id="jf-ex" rows="2" style="width:100%">${esc((j.excludes || []).join("\n"))}</textarea></div>
        <div class="form-row"><label>Image-Datenträger (Linux, leer = Systemplatte)</label><input id="jf-dev" style="width:100%" value="${esc(j.image_device || "")}"></div>
      </div>
      <div data-pve style="display:none">
        <p style="font-size:12px;color:var(--subtext)">Nur Clients, die einer Proxmox-VM/-LXC zugeordnet sind. Ein im Proxmox eingebundener PBS kann als Storage angegeben werden.</p>
        <div style="display:flex;gap:8px"><div class="form-row" style="flex:1"><label>Proxmox-Storage-ID</label><input id="jf-pvest" style="width:100%" value="${esc(j.pve_storage || "")}" placeholder="z.B. local oder pbs"></div>
          <div class="form-row" style="flex:1"><label>Modus</label><select id="jf-pvemode" style="width:100%">
            ${["snapshot", "suspend", "stop"].map((x) => `<option ${j.pve_mode === x ? "selected" : ""}>${x}</option>`).join("")}</select></div>
          <div class="form-row" style="flex:1"><label>Komprimierung</label><select id="jf-pvecomp" style="width:100%">${compOptions(j.compression)}</select></div></div>
      </div>

      <h4 style="margin:12px 0 4px">Zeitplan</h4>
      <div style="display:flex;gap:8px;align-items:end;flex-wrap:wrap">
        <div class="form-row"><label>Art</label><select id="jf-stype">
          ${[["daily", "Täglich"], ["weekly", "Wöchentlich"], ["hourly", "Alle N Stunden"], ["manual", "Nur manuell"]].map(([v, l]) => `<option value="${v}" ${sch.type === v ? "selected" : ""}>${l}</option>`).join("")}</select></div>
        <div class="form-row" data-stime><label>Uhrzeit</label><input id="jf-time" type="time" value="${esc(sch.time || "02:00")}"></div>
        <div class="form-row" data-severy><label>Alle … Stunden</label><input id="jf-every" type="number" min="1" max="24" value="${esc(String(sch.every || 4))}" style="width:80px"></div>
        <div data-sdays style="display:flex;gap:6px;font-size:12px;padding-bottom:10px">${DAYS.map((dname, i) =>
          `<label><input type="checkbox" value="${i + 1}" ${(sch.days || []).includes(i + 1) ? "checked" : ""}>${dname}</label>`).join("")}</div>
      </div>
      <label style="font-size:13px"><input type="checkbox" id="jf-missed" ${j.repeat_missed !== false ? "checked" : ""}> Verpasste nachholen (offline Clients sichern, sobald sie online kommen)</label>

      <h4 style="margin:12px 0 4px">Aufbewahrung (leer/0 = unbegrenzt)</h4>
      <div style="display:flex;gap:6px;flex-wrap:wrap">${KEEP.map(([k, l]) => `<div class="form-row" style="width:96px"><label>${l}</label>
        <input type="number" min="0" data-keep="${k}" value="${esc(String((j.retention || {})[k] || ""))}" style="width:100%"></div>`).join("")}</div>
      <div class="form-row"><label>Namensvorlage ({{client}}, {{hostname}}, {{date}}, {{time}}, {{job}})</label><input id="jf-tpl" style="width:100%" value="${esc(j.name_template || "")}"></div>
      <div id="jf-err" class="form-error hidden"></div>
      <div style="display:flex;justify-content:flex-end;margin-top:10px"><button class="btn-primary" id="jf-go">Speichern</button></div>`, { width: 680 });
    const $ = (s) => m.el.querySelector(s);
    const syncRoute = () => { $("#jf-route").innerHTML = routeOptions($("#jf-storage").value, j.route); };
    const syncEngine = () => {
      const pve = $("#jf-engine").value === "pve";
      m.el.querySelector("[data-agent]").style.display = pve ? "none" : "";
      m.el.querySelector("[data-pve]").style.display = pve ? "" : "none";
    };
    const syncSched = () => {
      const t = $("#jf-stype").value;
      m.el.querySelector("[data-stime]").style.display = ["daily", "weekly"].includes(t) ? "" : "none";
      m.el.querySelector("[data-sdays]").style.display = t === "weekly" ? "flex" : "none";
      m.el.querySelector("[data-severy]").style.display = t === "hourly" ? "" : "none";
    };
    syncRoute(); syncEngine(); syncSched();
    $("#jf-storage").addEventListener("change", syncRoute);
    $("#jf-engine").addEventListener("change", syncEngine);
    $("#jf-stype").addEventListener("change", syncSched);
    $("#jf-filter").addEventListener("input", () => {
      const f = $("#jf-filter").value.toLowerCase();
      m.el.querySelectorAll("[data-cname]").forEach((l) => { l.style.display = l.dataset.cname.includes(f) ? "" : "none"; });
    });
    $("#jf-go").addEventListener("click", async () => {
      const lines = (id) => $(id).value.split("\n").map((x) => x.trim()).filter(Boolean);
      const pve = $("#jf-engine").value === "pve";
      const retention = {};
      m.el.querySelectorAll("[data-keep]").forEach((i) => { if (+i.value) retention[i.dataset.keep] = +i.value; });
      const payload = {
        name: $("#jf-name").value, enabled: $("#jf-on").checked, engine: $("#jf-engine").value,
        clients: [...m.el.querySelectorAll("#jf-clients input:checked")].map((i) => i.value),
        tags: $("#jf-tags").value.split(",").map((t) => t.trim()).filter(Boolean),
        storage: $("#jf-storage").value, route: $("#jf-route").value, mode: $("#jf-mode").value,
        paths: lines("#jf-paths"), excludes: lines("#jf-ex"), image_device: $("#jf-dev").value.trim(),
        compression: pve ? $("#jf-pvecomp").value : $("#jf-comp").value,
        schedule: { type: $("#jf-stype").value, time: $("#jf-time").value, every: +$("#jf-every").value || 4,
                    days: [...m.el.querySelectorAll("[data-sdays] input:checked")].map((i) => +i.value) },
        repeat_missed: $("#jf-missed").checked, retention, name_template: $("#jf-tpl").value,
        pve_storage: $("#jf-pvest").value.trim(), pve_mode: $("#jf-pvemode").value,
      };
      try {
        await moduleRequest(j.id ? `/api/backup/jobs/${j.id}` : "/api/backup/jobs",
          { method: j.id ? "PUT" : "POST", noJob: true, body: JSON.stringify(payload) });
        m.close(); renderJobs();
      } catch (e) { $("#jf-err").textContent = e.message; $("#jf-err").classList.remove("hidden"); }
    });
  }

  // ================================================================ SPEICHER
  async function renderStorage() {
    await loadStorages();
    main.innerHTML = `
      <div style="margin-bottom:8px"><button class="btn-primary" id="st-new">＋ Speicherziel</button></div>
      <table style="width:100%;border-collapse:collapse;font-size:13px">
        <thead><tr style="text-align:left;color:var(--subtext);font-size:12px"><th>ID</th><th>Bezeichnung</th><th>Typ</th><th>Ort</th><th>Wege</th><th></th></tr></thead>
        <tbody>${storages.map((s, i) => `<tr style="border-top:1px solid var(--border)">
          <td style="padding:6px 4px"><code>${esc(s.id)}</code></td><td>${esc(s.label || "")}${s.enabled === false ? " (aus)" : ""}</td>
          <td>${esc(TYPE[s.type] || s.type)}</td>
          <td style="font-size:12px">${esc(s.type === "smb" ? `\\\\${s.server}\\${s.share}${s.path ? "\\" + s.path : ""}`
            : s.type === "pbs" ? `${s.server}:${s.port || 8007} / ${s.datastore}` : s.type === "backend" ? "storage/backup"
            : `${s.host}:${s.port || ""}${s.path || ""}`)}</td>
          <td style="font-size:12px">${(ROUTES_FOR[s.type] || []).map((r) => ROUTE[r]).join(", ")}</td>
          <td style="text-align:right;white-space:nowrap"><button class="btn btn-sm" data-test="${i}">🔌 Testen</button>
            ${s.type !== "backend" ? `<button class="btn btn-sm" data-sedit="${i}">✏️</button><button class="action-btn btn-danger" data-sdel="${i}">✕</button>` : ""}</td></tr>`).join("")}</tbody></table>
      <p style="font-size:12px;color:var(--subtext);margin-top:10px;line-height:1.5">
        <b>Wege:</b> <i>Direkt</i> - der Client schreibt selbst aufs NAS (SMB: Windows per UNC, Linux per CIFS-Mount; SFTP mit Schlüssel; PBS mit proxmox-backup-client, nur Linux).
        <i>Aufs Backend</i> - Daten liegen am Backend. <i>Über das Backend</i> - der Client spricht nur mit dem Backend, das die Daten aufs NAS legt (auch FTP).
        Die Daten werden immer schon auf dem Client verschlüsselt.</p>`;
    main.querySelector("#st-new").addEventListener("click", () => storageDialog(null));
    main.querySelectorAll("[data-sedit]").forEach((b) => b.addEventListener("click", () => storageDialog(storages[+b.dataset.sedit])));
    main.querySelectorAll("[data-sdel]").forEach((b) => b.addEventListener("click", async () => {
      const s = storages[+b.dataset.sdel];
      if (!(await uiConfirm(`Speicherziel „${s.label || s.id}“ entfernen? Die Daten auf dem Ziel bleiben unberührt.`, { danger: true }))) return;
      try { await moduleRequest(`/api/backup/storages/${s.id}`, { method: "DELETE", noJob: true }); renderStorage(); }
      catch (e) { window.notify?.(e.message, "error"); }
    }));
    main.querySelectorAll("[data-test]").forEach((b) => b.addEventListener("click", async () => {
      const s = storages[+b.dataset.test];
      b.disabled = true; b.textContent = "…";
      try {
        const r = await moduleRequest(`/api/backup/storages/${s.id}/test`, { method: "POST", noJob: true });
        window.notify?.(`✅ ${s.label || s.id}: Verbindung ok${r.free ? ` · ${fmtSize(r.free)} frei` : ""}${r.fingerprint ? ` · Fingerprint ${r.fingerprint}` : ""}`, "success", 9000);
      } catch (e) { window.notify?.(e.message, "error", 9000); }
      b.disabled = false; b.textContent = "🔌 Testen";
    }));
  }

  function storageDialog(s) {
    const isNew = !s;
    s = s || { type: "smb", enabled: true };
    const f = (k, label, ph = "", type = "text") => `<div class="form-row" data-f="${k}"><label>${label}</label>
      <input id="sf-${k}" type="${type}" style="width:100%" placeholder="${esc(ph)}" value="${esc(s[k] == null ? "" : String(s[k]))}"></div>`;
    const m = modal(isNew ? "＋ Speicherziel" : `✏️ ${esc(s.label || s.id)}`, `
      <div style="display:flex;gap:8px">
        <div class="form-row" style="flex:1"><label>ID</label><input id="sf-id" style="width:100%" value="${esc(s.id || "")}" ${isNew ? "" : "disabled"} placeholder="nas1"></div>
        <div class="form-row" style="flex:1"><label>Typ</label><select id="sf-type" style="width:100%" ${isNew ? "" : "disabled"}>
          ${["smb", "sftp", "ftp", "pbs"].map((t) => `<option value="${t}" ${s.type === t ? "selected" : ""}>${TYPE[t]}</option>`).join("")}</select></div></div>
      ${f("label", "Bezeichnung", "NAS im Keller")}
      <div data-t="smb">${f("server", "Server", "nas.local oder 192.168.1.10")}${f("share", "Freigabe", "backup")}${f("domain", "Domäne (optional)")}</div>
      <div data-t="sftp ftp">${f("host", "Host")}${f("port", "Port", "22 / 21", "number")}</div>
      <div data-t="ftp"><label style="font-size:13px"><input type="checkbox" id="sf-tls" ${s.tls ? "checked" : ""}> FTPS (TLS)</label></div>
      <div data-t="pbs">${f("server", "PBS-Server", "pbs.local")}${f("port", "Port", "8007", "number")}${f("datastore", "Datastore")}${f("namespace", "Namespace (optional)")}
        ${f("fingerprint", "Zertifikats-Fingerprint (SHA-256)", "aa:bb:…")}</div>
      <div data-t="smb sftp ftp">${f("path", "Unterordner (optional)", "bytera")}</div>
      ${f("user", "Benutzer", "PBS: root@pam oder user@pbs!token")}
      <div data-t="smb sftp ftp">${f("password", "Passwort", "", "password")}</div>
      <div data-t="pbs">${f("secret", "Passwort / Token-Secret", "", "password")}</div>
      <div data-t="sftp" style="font-size:12px;color:var(--subtext)">
        ${s.public_key ? `Öffentlicher Schlüssel (in ~/.ssh/authorized_keys auf dem NAS eintragen):<textarea readonly rows="3" style="width:100%;font-size:11px">${esc(s.public_key)}</textarea>` : ""}
        ${isNew ? "Nach dem Anlegen einen SSH-Schlüssel erzeugen - der direkte Weg (restic) braucht Schlüssel-Login." :
          `<button class="btn btn-sm" id="sf-key">🔑 ${s.public_key ? "Neuen " : ""}SSH-Schlüssel erzeugen</button>`}</div>
      <label style="font-size:13px"><input type="checkbox" id="sf-on" ${s.enabled !== false ? "checked" : ""}> Aktiv</label>
      <div id="sf-err" class="form-error hidden"></div>
      <div style="display:flex;justify-content:flex-end;margin-top:10px"><button class="btn-primary" id="sf-go">Speichern</button></div>`);
    const $ = (q) => m.el.querySelector(q);
    const sync = () => {
      const t = $("#sf-type").value;
      m.el.querySelectorAll("[data-t]").forEach((el) => { el.style.display = el.dataset.t.split(" ").includes(t) ? "" : "none"; });
    };
    sync(); $("#sf-type").addEventListener("change", sync);
    $("#sf-key")?.addEventListener("click", async () => {
      try {
        const r = await moduleRequest(`/api/backup/storages/${s.id}/sftp-key`, { method: "POST", noJob: true });
        m.close(); window.notify?.("SSH-Schlüssel erzeugt", "success");
        await loadStorages(); storageDialog({ ...s, public_key: r.public_key });
      } catch (e) { window.notify?.(e.message, "error"); }
    });
    $("#sf-go").addEventListener("click", async () => {
      const t = $("#sf-type").value;
      // Bei doppelt vorhandenen Feldern (server/port je Typ) das sichtbare nehmen.
      const val = (k) => { const all = [...m.el.querySelectorAll(`[data-f="${k}"]`)].filter((e) => e.offsetParent !== null); return (all[0]?.querySelector("input") || {}).value ?? ""; };
      const payload = { id: $("#sf-id").value.trim(), type: t, label: val("label"), enabled: $("#sf-on").checked,
        server: val("server"), share: val("share"), domain: val("domain"), host: val("host"),
        port: val("port") ? +val("port") : null, tls: $("#sf-tls").checked, path: val("path"), user: val("user"),
        password: val("password"), datastore: val("datastore"), secret: val("secret"),
        fingerprint: val("fingerprint"), namespace: val("namespace") };
      try {
        await moduleRequest(isNew ? "/api/backup/storages" : `/api/backup/storages/${s.id}`,
          { method: isNew ? "POST" : "PUT", noJob: true, body: JSON.stringify(payload) });
        m.close(); renderStorage();
      } catch (e) { $("#sf-err").textContent = e.message; $("#sf-err").classList.remove("hidden"); }
    });
  }

  (async () => { await loadStorages(); render(); })();
}
