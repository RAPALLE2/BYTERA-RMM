// app.js (Modul "bmc") - iLO / iDRAC über Redfish
// -----------------------------------------------
// Links Verbindungsliste, rechts ein Steuerpult pro BMC: Übersicht/Power,
// Sensoren, Inventar, Protokolle, Boot, Virtual Media, Firmware, Konsole und
// ein roher Redfish-Explorer (alles, was die BMC kann).
import { state, hasGlobalPerm, isAdmin } from "/js/app/state.js";
import { moduleRequest } from "/js/app/modules.js";
import { esc, uiConfirm } from "/js/app/utils.js";

const api = (p, o = {}) => moduleRequest("/api/bmc" + p, o);
const call = async (id, op, args = {}) => (await api(`/${id}/call/${op}`, { method: "POST", body: JSON.stringify({ args }) })).result;
const toast = (m, t = "success", ms = 1900) => window.notify?.(m, t, ms);
const errBox = (e) => `<div style="padding:14px;color:var(--danger);white-space:pre-wrap">${esc(e.message || e)}</div>`;
const card = "background:var(--panel);border:1px solid var(--border);border-radius:12px";
const H = (v) => { const c = { OK: "var(--online,#16a34a)", Warning: "var(--warning,#d97706)", Critical: "var(--danger)" }[v] || "var(--subtext)"; return `<span style="color:${c};font-weight:600">${esc(v || "—")}</span>`; };
const clientName = (cid) => (state.clients || []).find((c) => c.id === cid)?.hostname || "";

export function renderBmc(body, win) {
  let conns = [], sel = null;
  const focusConn = win?.props?.connId || null;
  const focusClient = win?.props?.clientId || null;

  body.innerHTML = `<div style="display:flex;height:100%;min-height:0;font-size:13px">
    <aside style="width:250px;flex:0 0 250px;border-right:1px solid var(--border);display:flex;flex-direction:column;min-height:0">
      <div style="padding:12px 12px 8px;display:flex;align-items:center;gap:9px">
        <div style="width:30px;height:30px;border-radius:8px;background:linear-gradient(135deg,var(--accent),var(--accent-2,#38bdf8));display:grid;place-items:center">🛉</div>
        <div><div style="font-weight:800">iLO / iDRAC</div><div style="font-size:11px;color:var(--subtext)">Redfish-Fernwartung</div></div></div>
      <div style="padding:0 10px 8px"><button class="btn-primary" data-new style="width:100%;font-size:12px">+ BMC hinzufügen</button></div>
      <div data-list style="flex:1;overflow:auto;padding:0 8px 10px"></div></div>
    <main data-main style="flex:1;overflow:auto;min-height:0;position:relative"></main></div>`;
  const listEl = body.querySelector("[data-list]"), main = body.querySelector("[data-main]");
  const canManage = () => isAdmin() || hasGlobalPerm("manage_bmc");

  const drawList = () => {
    listEl.innerHTML = conns.map((c) => {
      const vd = (c.vendor || "").includes("HPE") ? "🟢 iLO" : (c.vendor || "").includes("Dell") ? "🔵 iDRAC" : "⚙️";
      return `<a data-c="${esc(c.id)}" style="display:block;padding:8px 10px;border-radius:9px;cursor:pointer;${sel === c.id ? "background:color-mix(in srgb,var(--accent) 16%,transparent);font-weight:600" : ""}">
        <div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(c.name || c.host)}</div>
        <div style="font-size:11px;color:var(--subtext);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${vd} · ${esc(c.host)}${c.client_id ? " · 🔗 " + esc(clientName(c.client_id) || "Client") : ""}</div></a>`;
    }).join("") || `<div style="padding:10px;color:var(--subtext);font-size:12px">Noch keine BMC angelegt.</div>`;
    listEl.querySelectorAll("[data-c]").forEach((a) => a.addEventListener("click", () => open(a.dataset.c)));
  };

  const open = (id) => { sel = id; drawList(); drawPanel(); };

  const load = async () => {
    try { conns = (await api("/connections")).connections; } catch (e) { main.innerHTML = errBox(e); return; }
    if (focusConn) sel = focusConn;
    else if (focusClient) sel = (conns.find((c) => c.client_id === focusClient) || {}).id || sel;
    if (!sel && conns.length) sel = conns[0].id;
    drawList();
    sel ? drawPanel() : (main.innerHTML = `<div style="padding:40px;text-align:center;color:var(--subtext)">Links eine BMC wählen oder „+ BMC hinzufügen“.</div>`);
  };

  // ------------------------------------------------ Steuerpult
  let TAB = "overview";
  const TABS = [["overview", "⚡ Übersicht & Power"], ["sensors", "🌡 Sensoren"], ["inventory", "🧩 Inventar"],
                ["logs", "📜 Protokolle"], ["boot", "🥾 Boot"], ["media", "💿 Virtual Media"],
                ["firmware", "🧬 Firmware"], ["console", "🖥 Konsole"], ["raw", "🛠 Redfish (roh)"]];

  function drawPanel() {
    const c = conns.find((x) => x.id === sel);
    if (!c) return;
    main.innerHTML = `<div style="position:sticky;top:0;z-index:5;background:var(--panel);border-bottom:1px solid var(--border);padding:12px 18px">
        <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
          <b style="font-size:17px">${esc(c.name || c.host)}</b>
          <span style="font-size:11.5px;color:var(--subtext)">${esc(c.vendor || "Redfish")} · ${esc(c.host)}${c.via ? " · via Node" : ""}</span>
          <span data-power></span><span style="flex:1"></span>
          ${canManage() ? `<button class="btn" data-edit>✎ Bearbeiten</button><button class="btn" data-del style="color:var(--danger)">🗑</button>` : ""}
          <button class="btn" data-test>↻ Test</button></div>
        <div style="display:flex;gap:2px;flex-wrap:wrap;margin-top:10px" data-tabs>
          ${TABS.map(([k, l]) => `<a data-t="${k}" style="padding:7px 11px;cursor:pointer;border-bottom:2px solid transparent;font-size:12.5px;${TAB === k ? "border-bottom-color:var(--accent);font-weight:700" : ""}">${l}</a>`).join("")}</div></div>
      <div data-content style="padding:16px 18px"></div>`;
    main.querySelector("[data-edit]")?.addEventListener("click", () => connDialog(c));
    main.querySelector("[data-del]")?.addEventListener("click", () => delConn(c));
    main.querySelector("[data-test]").addEventListener("click", () => showTab());
    main.querySelectorAll("[data-t]").forEach((a) => a.addEventListener("click", () => { TAB = a.dataset.t; drawPanel(); }));
    showTab();
    refreshPower(c);
  }

  async function refreshPower(c) {
    const el = main.querySelector("[data-power]");
    if (!el) return;
    try {
      const s = await call(c.id, "summary");
      const on = s.power_state === "On";
      el.innerHTML = `<span style="padding:2px 9px;border-radius:20px;font-size:11.5px;font-weight:700;background:${on ? "color-mix(in srgb,var(--online,#16a34a) 20%,transparent)" : "color-mix(in srgb,var(--danger) 18%,transparent)"}">${on ? "● An" : "○ Aus"}</span> Health ${H(s.health || s.health_rollup)}`;
    } catch (e) { el.innerHTML = `<span style="color:var(--danger);font-size:11.5px">nicht erreichbar</span>`; }
  }

  const content = () => main.querySelector("[data-content]");
  async function showTab() {
    const c = conns.find((x) => x.id === sel);
    const el = content(); if (!el) return;
    el.innerHTML = `<div style="color:var(--subtext)">Lade …</div>`;
    try { await ({ overview: tabOverview, sensors: tabSensors, inventory: tabInventory, logs: tabLogs, boot: tabBoot, media: tabMedia, firmware: tabFirmware, console: tabConsole, raw: tabRaw }[TAB])(el, c); }
    catch (e) { el.innerHTML = errBox(e); }
  }

  // ---- Übersicht & Power
  async function tabOverview(el, c) {
    const s = await call(c.id, "summary");
    const row = (k, v) => v == null || v === "" ? "" : `<tr><td style="padding:4px 14px 4px 0;color:var(--subtext)">${esc(k)}</td><td style="font-weight:600">${typeof v === "string" || typeof v === "number" ? esc(String(v)) : v}</td></tr>`;
    const resets = [["On", "▶ Einschalten"], ["GracefulShutdown", "⏻ Herunterfahren"], ["ForceOff", "⏹ Hart aus"],
                    ["GracefulRestart", "↻ Neustart"], ["ForceRestart", "⟳ Hart neu"], ["PushPowerButton", "⎋ Power-Taste"], ["Nmi", "⚠ NMI"]];
    const allowed = new Set(s.reset_types || []);
    el.innerHTML = `<div style="display:flex;gap:16px;flex-wrap:wrap;align-items:flex-start">
      <div style="${card};padding:14px 16px;flex:1;min-width:280px"><h4 style="margin:0 0 8px">System</h4>
        <table><tbody>${row("Hersteller", s.vendor)}${row("Modell", s.model)}${row("Hostname", s.name)}${row("Seriennummer", s.serial)}${row("BIOS", s.bios_version)}
          ${row("CPU", s.cpu?.model ? `${s.cpu.count}× ${s.cpu.model} (${s.cpu.cores || "?"} Kerne)` : null)}${row("RAM", s.memory_gib ? s.memory_gib + " GiB" : null)}
          ${row("Energiezustand", s.power_state)}${row("Health", H(s.health))}${row("BMC-Firmware", s.manager?.firmware)}${row("Redfish", s.redfish_version)}</tbody></table></div>
      <div style="${card};padding:14px 16px;flex:1;min-width:280px"><h4 style="margin:0 0 10px">Power</h4>
        <div style="display:flex;flex-wrap:wrap;gap:8px">${resets.filter(([k]) => allowed.has(k)).map(([k, l]) => `<button class="btn" data-pw="${k}">${l}</button>`).join("")}</div>
        <h4 style="margin:16px 0 8px">Identify-LED</h4>
        <div style="display:flex;gap:8px"><button class="btn" data-led="1">💡 An</button><button class="btn" data-led="0">LED aus</button></div>
        <h4 style="margin:16px 0 8px">BMC</h4><button class="btn" data-mgr>↻ iLO/iDRAC neu starten</button></div></div>`;
    el.querySelectorAll("[data-pw]").forEach((b) => b.addEventListener("click", async () => {
      const k = b.dataset.pw;
      if (["ForceOff", "ForceRestart", "GracefulShutdown", "GracefulRestart", "Nmi"].includes(k) && !(await uiConfirm(`„${b.textContent.trim()}“ an ${c.name || c.host} senden?`))) return;
      try { await call(c.id, "power", { reset_type: k }); toast("Befehl gesendet"); setTimeout(() => refreshPower(c), 2500); } catch (e) { toast(e.message, "error", 6000); }
    }));
    el.querySelectorAll("[data-led]").forEach((b) => b.addEventListener("click", async () => { try { await call(c.id, "identify", { on: b.dataset.led === "1" }); toast("LED geschaltet"); } catch (e) { toast(e.message, "error", 6000); } }));
    el.querySelector("[data-mgr]").addEventListener("click", async () => { if (await uiConfirm("BMC (iLO/iDRAC) neu starten? Das System selbst läuft weiter.")) { try { await call(c.id, "manager_reset", {}); toast("BMC startet neu"); } catch (e) { toast(e.message, "error", 6000); } } });
  }

  // ---- Sensoren
  async function tabSensors(el, c) {
    const s = await call(c.id, "sensors");
    const tbl = (title, rows, cols) => `<div style="${card};padding:12px 14px;flex:1;min-width:280px"><h4 style="margin:0 0 8px">${title}</h4>
      ${rows.length ? `<table style="width:100%;border-collapse:collapse;font-size:12.5px"><tbody>${rows.map(cols).join("")}</tbody></table>` : `<div style="color:var(--subtext)">—</div>`}</div>`;
    el.innerHTML = `<div style="display:flex;gap:14px;flex-wrap:wrap;align-items:flex-start">
      ${tbl("🌡 Temperaturen", s.temperatures, (x) => `<tr><td style="padding:3px 0">${esc(x.name || "")}</td><td style="text-align:right;font-weight:600">${x.reading ?? "—"} °C</td><td style="text-align:right">${H(x.health)}</td></tr>`)}
      ${tbl("🌀 Lüfter", s.fans, (x) => `<tr><td style="padding:3px 0">${esc(x.name || "")}</td><td style="text-align:right;font-weight:600">${x.reading ?? "—"} ${esc(x.units || "")}</td><td style="text-align:right">${H(x.health)}</td></tr>`)}
      ${tbl("🔌 Netzteile", s.power_supplies, (x) => `<tr><td style="padding:3px 0">${esc(x.name || "")}</td><td style="text-align:right">${x.watts ?? "—"}/${x.capacity ?? "—"} W</td><td style="text-align:right">${H(x.health)}</td></tr>`)}
      ${tbl("⚡ Verbrauch", s.power, (x) => `<tr><td style="padding:3px 0">${esc(x.name || "System")}</td><td style="text-align:right;font-weight:600">${x.consumed ?? "—"} W</td><td style="text-align:right;color:var(--subtext)">Ø ${x.avg ?? "—"} W</td></tr>`)}</div>
      <div style="margin-top:10px"><button class="btn" data-r>↻ Aktualisieren</button></div>`;
    el.querySelector("[data-r]").addEventListener("click", showTab);
  }

  // ---- Inventar
  async function tabInventory(el, c) {
    const d = await call(c.id, "inventory");
    const sec = (title, rows, cols, head) => `<div style="${card};padding:12px 14px;margin-bottom:12px"><h4 style="margin:0 0 8px">${title} <span style="color:var(--subtext);font-weight:400">(${rows.length})</span></h4>
      ${rows.length ? `<table style="width:100%;border-collapse:collapse;font-size:12.5px"><thead><tr style="color:var(--subtext);text-align:left">${head.map((h) => `<th style="padding:3px 8px 3px 0">${h}</th>`).join("")}</tr></thead><tbody>${rows.map(cols).join("")}</tbody></table>` : `<div style="color:var(--subtext)">—</div>`}</div>`;
    const gib = (b) => b ? (b / 1073741824 >= 1024 ? (b / 1099511627776).toFixed(2) + " TB" : (b / 1073741824).toFixed(0) + " GB") : "—";
    el.innerHTML =
      sec("⚙ Prozessoren", d.processors, (x) => `<tr><td style="padding:2px 8px 2px 0">${esc(x.name || "")}</td><td>${esc(x.model || "")}</td><td>${x.cores || "?"}C/${x.threads || "?"}T</td><td>${x.speed_mhz || "?"} MHz</td><td>${H(x.health)}</td></tr>`, ["ID", "Modell", "Kerne", "Takt", "Health"])
      + sec("🧠 Arbeitsspeicher", d.memory, (x) => `<tr><td style="padding:2px 8px 2px 0">${esc(x.name || "")}</td><td>${x.capacity_mib ? (x.capacity_mib / 1024) + " GiB" : "—"}</td><td>${esc(x.type || "")}</td><td>${x.speed || "?"} MHz</td><td>${esc(x.part || "")}</td><td>${H(x.health)}</td></tr>`, ["Slot", "Größe", "Typ", "Takt", "Teil-Nr.", "Health"])
      + sec("💽 Datenträger", d.storage, (x) => `<tr><td style="padding:2px 8px 2px 0">${esc(x.name || "")}</td><td>${esc(x.model || "")}</td><td>${gib(x.capacity)}</td><td>${esc(x.type || "")} ${esc(x.proto || "")}</td><td>${H(x.health)}</td></tr>`, ["Name", "Modell", "Größe", "Typ", "Health"])
      + sec("🌐 Netzwerk", d.network, (x) => `<tr><td style="padding:2px 8px 2px 0">${esc(x.name || "")}</td><td style="font-family:monospace">${esc(x.mac || "")}</td><td>${x.speed_mbps || "?"} Mbps</td><td>${esc(x.link || "")}</td></tr>`, ["Port", "MAC", "Speed", "Link"]);
  }

  // ---- Protokolle
  async function tabLogs(el, c) {
    const d = await call(c.id, "log_services");
    if (!d.services.length) { el.innerHTML = `<div style="color:var(--subtext)">Keine Protokolldienste gefunden.</div>`; return; }
    el.innerHTML = `<div style="display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap">
      <select data-svc>${d.services.map((s) => `<option value="${esc(s.uri)}" data-clear="${s.clearable ? 1 : 0}">${esc(s.name || s.id)}</option>`).join("")}</select>
      <button class="btn" data-reload>↻</button><button class="btn" data-clear style="color:var(--danger)">🗑 Protokoll löschen</button></div>
      <div data-entries></div>`;
    const svc = el.querySelector("[data-svc]"), entries = el.querySelector("[data-entries]");
    const loadE = async () => {
      entries.innerHTML = `<div style="color:var(--subtext)">Lade …</div>`;
      try {
        const r = await call(c.id, "log_entries", { uri: svc.value, limit: 200 });
        entries.innerHTML = `<div style="${card};padding:4px 0;max-height:52vh;overflow:auto"><table style="width:100%;border-collapse:collapse;font-size:12px">
          <thead><tr style="color:var(--subtext);text-align:left"><th style="padding:6px 10px">Zeit</th><th>Schwere</th><th>Meldung</th></tr></thead>
          <tbody>${(r.entries || []).map((e) => `<tr style="border-top:1px solid var(--border)"><td style="padding:5px 10px;white-space:nowrap;color:var(--subtext)">${esc((e.created || "").replace("T", " ").slice(0, 19))}</td><td>${H(e.severity)}</td><td>${esc(e.message || "")}</td></tr>`).join("") || `<tr><td colspan="3" style="padding:10px;color:var(--subtext)">Keine Einträge</td></tr>`}</tbody></table></div>`;
      } catch (e) { entries.innerHTML = errBox(e); }
    };
    svc.addEventListener("change", loadE);
    el.querySelector("[data-reload]").addEventListener("click", loadE);
    el.querySelector("[data-clear]").addEventListener("click", async () => {
      if (!(await uiConfirm("Dieses Protokoll auf der BMC löschen? Das kann nicht rückgängig gemacht werden."))) return;
      try { await call(c.id, "log_clear", { uri: svc.value }); toast("Protokoll gelöscht"); loadE(); } catch (e) { toast(e.message, "error", 6000); }
    });
    loadE();
  }

  // ---- Boot
  async function tabBoot(el, c) {
    const s = await call(c.id, "summary");
    const targets = s.boot?.targets || ["None", "Pxe", "Hdd", "Cd", "Usb", "BiosSetup"];
    el.innerHTML = `<div style="${card};padding:14px 16px;max-width:560px">
      <p style="margin:0 0 12px;color:var(--subtext);font-size:12.5px">Aktuell: <b>${esc(s.boot?.target || "None")}</b> (${esc(s.boot?.enabled || "Disabled")})</p>
      <div style="display:grid;grid-template-columns:140px 1fr;gap:8px 12px;align-items:center">
        <label>Nächster Start von</label><select data-target>${targets.map((t) => `<option ${t === s.boot?.target ? "selected" : ""}>${esc(t)}</option>`).join("")}</select>
        <label>Modus</label><select data-mode><option value="">unverändert</option><option>UEFI</option><option>Legacy</option></select>
        <label></label><label style="font-size:12.5px"><input type="checkbox" data-persist> dauerhaft (statt nur einmal)</label></div>
      <button class="btn-primary" data-set style="margin-top:14px">Boot-Reihenfolge setzen</button>
      <p style="font-size:11.5px;color:var(--subtext);margin-top:10px">Tipp: „Cd“ + eingebundenes Virtual-Media-ISO bootet vom ISO.</p></div>`;
    el.querySelector("[data-set]").addEventListener("click", async () => {
      try {
        await call(c.id, "set_boot", { target: el.querySelector("[data-target]").value, mode: el.querySelector("[data-mode]").value, persistent: el.querySelector("[data-persist]").checked });
        toast("Boot-Reihenfolge gesetzt"); showTab();
      } catch (e) { toast(e.message, "error", 6000); }
    });
  }

  // ---- Virtual Media
  async function tabMedia(el, c) {
    const d = await call(c.id, "virtual_media");
    el.innerHTML = (d.media || []).map((m) => `<div style="${card};padding:12px 14px;margin-bottom:10px">
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap"><b>💿 ${esc(m.name || m.id)}</b>
        <span style="font-size:11.5px;color:var(--subtext)">${(m.types || []).join(", ")}</span><span style="flex:1"></span>
        ${m.inserted ? `<span style="color:var(--online,#16a34a)">eingebunden: ${esc(m.imagename || m.image || "")}</span> <button class="btn" data-eject="${esc(m.uri)}">⏏ Auswerfen</button>`
          : `<span style="color:var(--subtext)">leer</span>`}</div>
      ${m.inserted ? "" : `<div style="display:flex;gap:6px;margin-top:8px"><input data-img="${esc(m.uri)}" placeholder="ISO-URL (http/https/nfs/cifs), z.B. http://server/boot.iso" style="flex:1"><button class="btn-primary" data-insert="${esc(m.uri)}">Einbinden</button></div>`}</div>`).join("")
      || `<div style="color:var(--subtext)">Keine Virtual-Media-Geräte.</div>`;
    el.querySelectorAll("[data-insert]").forEach((b) => b.addEventListener("click", async () => {
      const img = el.querySelector(`[data-img="${CSS.escape(b.dataset.insert)}"]`).value.trim();
      if (!img) { toast("ISO-URL angeben", "error"); return; }
      try { await call(c.id, "vm_insert", { uri: b.dataset.insert, image: img }); toast("ISO eingebunden"); showTab(); } catch (e) { toast(e.message, "error", 7000); }
    }));
    el.querySelectorAll("[data-eject]").forEach((b) => b.addEventListener("click", async () => { try { await call(c.id, "vm_eject", { uri: b.dataset.eject }); toast("Ausgeworfen"); showTab(); } catch (e) { toast(e.message, "error", 6000); } }));
  }

  // ---- Firmware
  async function tabFirmware(el, c) {
    const d = await call(c.id, "firmware");
    el.innerHTML = `<div style="${card};padding:4px 0"><table style="width:100%;border-collapse:collapse;font-size:12.5px">
      <thead><tr style="color:var(--subtext);text-align:left"><th style="padding:7px 12px">Komponente</th><th>Version</th><th>Aktualisierbar</th></tr></thead>
      <tbody>${(d.firmware || []).map((f) => `<tr style="border-top:1px solid var(--border)"><td style="padding:6px 12px">${esc(f.name || "")}</td><td style="font-family:monospace">${esc(f.version || "")}</td><td>${f.updateable == null ? "" : (f.updateable ? "ja" : "nein")}</td></tr>`).join("")}</tbody></table></div>`;
  }

  // ---- Konsole / native UI
  async function tabConsole(el, c) {
    let d; try { d = await api(`/${c.id}/console`); } catch (e) { el.innerHTML = errBox(e); return; }
    const ci = await call(c.id, "console_info").catch(() => ({}));
    el.innerHTML = `<div style="${card};padding:16px 18px;max-width:640px">
      <h4 style="margin:0 0 6px">🖥 Remote-Konsole & native Oberfläche</h4>
      <p style="color:var(--subtext);font-size:12.5px;margin:0 0 14px">${esc(d.hint || "")}</p>
      <div style="display:flex;gap:10px;flex-wrap:wrap">
        <button class="btn-primary" data-direct>↗ Direkt öffnen (neuer Tab)</button>
        ${d.proxy_url ? `<button class="btn-primary" data-proxy>↗ Über Backend öffnen</button>` : ""}</div>
      ${d.proxy_url ? "" : `<p style="font-size:11.5px;color:var(--subtext);margin-top:10px">Tipp: Für isolierte Management-Netze in den Modul-Einstellungen „proxy_host“ setzen – dann reicht das Backend die Oberfläche inkl. HTML5-Konsole durch.</p>`}
      ${ci.graphical ? `<p style="font-size:11.5px;color:var(--subtext);margin-top:10px">Grafische Konsole laut BMC: ${ci.graphical.enabled ? "aktiv" : "deaktiviert"}${ci.graphical.types ? " (" + ci.graphical.types.join(", ") + ")" : ""}.</p>` : ""}</div>`;
    el.querySelector("[data-direct]").addEventListener("click", () => window.open(d.direct_url, "_blank", "noopener"));
    el.querySelector("[data-proxy]")?.addEventListener("click", () => window.open(d.proxy_url, "_blank", "noopener"));
  }

  // ---- Roher Redfish-Explorer
  async function tabRaw(el, c) {
    el.innerHTML = `<div style="${card};padding:14px 16px">
      <p style="margin:0 0 10px;color:var(--subtext);font-size:12.5px">Beliebiger Redfish-Aufruf – damit erreichst du alles, was iLO/iDRAC bieten. Pfad muss mit <code>/redfish</code> beginnen.</p>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px">
        <select data-m style="width:100px"><option>GET</option><option>POST</option><option>PATCH</option><option>DELETE</option></select>
        <input data-path value="/redfish/v1/" style="flex:1;font-family:monospace">
        <button class="btn-primary" data-go>Senden</button></div>
      <textarea data-body rows="4" placeholder='JSON-Body (für POST/PATCH), z.B. {"ResetType":"On"}' style="width:100%;font-family:monospace;font-size:12px;margin-bottom:8px"></textarea>
      <pre data-out style="background:var(--panel-2,#111);border:1px solid var(--border);border-radius:8px;padding:12px;overflow:auto;max-height:46vh;font-size:12px;margin:0;white-space:pre-wrap"></pre></div>`;
    el.querySelector("[data-go]").addEventListener("click", async () => {
      const out = el.querySelector("[data-out]"); out.textContent = "…";
      let body = null; const raw = el.querySelector("[data-body]").value.trim();
      if (raw) { try { body = JSON.parse(raw); } catch { out.textContent = "Body ist kein gültiges JSON."; return; } }
      try {
        const r = await call(c.id, "raw", { method: el.querySelector("[data-m]").value, path: el.querySelector("[data-path]").value.trim(), body });
        out.textContent = `HTTP ${r.status}\n` + JSON.stringify(r.data, null, 2);
      } catch (e) { out.textContent = "Fehler: " + e.message; }
    });
  }

  // ------------------------------------------------ Verbindungsdialog
  function connDialog(c) {
    const ed = !!c;
    const clientOpts = [`<option value="">— nicht verknüpft —</option>`].concat((state.clients || []).slice().sort((a, b) => (a.hostname || "").localeCompare(b.hostname || "")).map((x) => `<option value="${esc(x.id)}" ${ed && c.client_id === x.id ? "selected" : ""}>${esc(x.hostname || x.id)}</option>`));
    const ov = overlay(`<h3 style="margin:0 0 12px">${ed ? "BMC bearbeiten" : "BMC hinzufügen"}</h3>
      <div style="display:grid;grid-template-columns:150px 1fr;gap:8px 12px;align-items:center">
        <label>Name</label><input data-f="name" value="${esc(ed ? c.name : "")}" placeholder="z.B. srv01 iDRAC">
        <label>Adresse (Host/IP)</label><input data-f="host" value="${esc(ed ? c.host : "")}" placeholder="10.0.0.9">
        <label>Port</label><input data-f="port" type="number" value="${ed && c.port ? c.port : ""}" placeholder="443">
        <label>Benutzer</label><input data-f="username" value="${esc(ed ? c.username : "")}" placeholder="root / Administrator">
        <label>Passwort</label><input data-f="password" type="password" placeholder="${ed ? "(unverändert lassen)" : ""}">
        <label>Verknüpfter Client</label><select data-f="client_id">${clientOpts.join("")}</select>
        <label>Ausführen über</label><select data-f="via"><option value="">Backend</option></select>
        <label></label><label style="font-size:12.5px"><input type="checkbox" data-f="verify_tls" ${ed && c.verify_tls ? "checked" : ""}> TLS-Zertifikat prüfen</label>
        <label>Fingerabdruck (SHA-256)</label><input data-f="fingerprint" value="${esc(ed ? c.fingerprint || "" : "")}" placeholder="optional, für selbstsignierte Zertifikate" style="font-family:monospace">
        <label></label><label style="font-size:12.5px"><input type="checkbox" data-f="insecure_http" ${ed && c.insecure_http ? "checked" : ""}> nur HTTP (kein TLS) – nicht empfohlen</label>
      </div>
      <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px"><button class="btn" data-x>Abbrechen</button><button class="btn-primary" data-ok>${ed ? "Speichern" : "Anlegen"}</button></div>`, 620);
    // Nodes laden
    api("/nodes").then((d) => {
      const sel2 = ov.querySelector('[data-f="via"]');
      (d.nodes || []).forEach((n) => { const o = document.createElement("option"); o.value = n.id || n.client_id; o.textContent = "Node: " + (n.name || n.hostname || n.id); if (ed && c.via === o.value) o.selected = true; sel2.appendChild(o); });
    }).catch(() => {});
    ov.querySelector("[data-ok]").addEventListener("click", async () => {
      const g = (k) => ov.querySelector(`[data-f="${k}"]`);
      const payload = { name: g("name").value.trim(), host: g("host").value.trim(), port: g("port").value ? +g("port").value : null,
        username: g("username").value.trim(), vendor: ed ? c.vendor : "", client_id: g("client_id").value,
        via: g("via").value, verify_tls: g("verify_tls").checked, fingerprint: g("fingerprint").value.trim(), insecure_http: g("insecure_http").checked };
      if (g("password").value) payload.password = g("password").value;
      if (!payload.host) { toast("Adresse fehlt", "error"); return; }
      try {
        if (ed) await api(`/connections/${c.id}`, { method: "PUT", body: JSON.stringify(payload) });
        else { const n = await api("/connections", { method: "POST", body: JSON.stringify(payload) }); sel = n.id; }
        ov.remove(); toast("Gespeichert"); await load();
      } catch (e) { toast(e.message, "error", 7000); }
    });
  }

  async function delConn(c) {
    if (!(await uiConfirm(`BMC „${c.name || c.host}“ entfernen?`))) return;
    try { await api(`/connections/${c.id}`, { method: "DELETE" }); sel = null; toast("Entfernt"); await load(); } catch (e) { toast(e.message, "error", 6000); }
  }

  function overlay(html, width = 620) {
    const ov = document.createElement("div");
    ov.style.cssText = "position:fixed;inset:0;z-index:9500;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;padding:14px";
    ov.innerHTML = `<div style="background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:16px 18px;width:${width}px;max-width:100%;max-height:calc(100dvh - 28px);overflow:auto;font-size:13px">${html}</div>`;
    document.body.appendChild(ov);
    ov.addEventListener("click", (e) => { if (e.target === ov) ov.remove(); });
    ov.querySelector("[data-x]")?.addEventListener("click", () => ov.remove());
    return ov;
  }

  body.querySelector("[data-new]").addEventListener("click", () => { if (canManage()) connDialog(null); else toast("Keine Berechtigung zum Anlegen", "error"); });
  load().then(() => {
    // Vom Aktionsknopf eines Clients aufgerufen, aber noch keine BMC verknüpft:
    // direkt den Dialog mit vorausgewähltem Client öffnen.
    if (focusClient && !conns.some((c) => c.client_id === focusClient) && canManage()) {
      connDialog(null);
      const selC = main.ownerDocument.querySelector('[data-f="client_id"]');
      if (selC) selC.value = focusClient;
    }
  });
}
