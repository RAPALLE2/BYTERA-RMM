// inventory.js (Modul "inventory")
// --------------------------------
// Inventar-App:
//   links   Ansichten (Status, Garantie), Besitzer, Büros, Abteilungen, Lager mit Regalen
//   Mitte   Geraeteliste mit Suche, Typ-Filter, Sortierung, Summen, CSV-Export
//   rechts  Details eines Geraets: Daten, Notizen, Aktionen, Verlauf
// Rechte: see_inventory (ansehen), manage_inventory (alles aendern).
// Die harte Pruefung macht das Backend - die App blendet nur Knoepfe aus.

import { moduleRequest } from "/js/app/modules.js";
import { esc, uiConfirm, uiPrompt, downloadText } from "/js/app/utils.js";
import { state, findClient } from "/js/app/state.js";
import { openWindow, registerCleanup } from "/js/app/windowmanager.js";

const CATEGORIES = {
  laptop: ["💻", "Laptop"], pc: ["🖥️", "PC"], monitor: ["🖵", "Monitor"],
  phone: ["📱", "Smartphone"], tablet: ["📲", "Tablet"], printer: ["🖨️", "Drucker"],
  network: ["📡", "Netzwerk"], server: ["🗄️", "Server"], peripheral: ["⌨️", "Zubehör"],
  headset: ["🎧", "Headset"], camera: ["📷", "Kamera"], tv: ["📺", "TV/Display"],
  furniture: ["🪑", "Möbel"], vehicle: ["🚗", "Fahrzeug"], key: ["🔑", "Schlüssel"],
  license: ["🧾", "Lizenz"], other: ["📦", "Sonstiges"],
};
const STATUS = {
  in_use: ["🟢", "Im Einsatz", "var(--ok,#34d399)"],
  storage: ["📦", "Im Lager", "var(--accent)"],
  repair: ["🔧", "Reparatur", "#f59e0b"],
  retired: ["🗄️", "Ausgemustert", "var(--subtext)"],
};
const WARN_DAYS = 60;

const cat = (c) => CATEGORIES[c] || CATEGORIES.other;
const money = (v) => v == null || v === "" ? "–"
  : Number(v).toLocaleString("de-DE", { style: "currency", currency: "EUR" });
const fmtDate = (d) => d ? new Date(d + "T00:00:00").toLocaleDateString("de-DE") : "–";
const daysUntil = (d) => d ? Math.floor((new Date(d + "T00:00:00") - new Date().setHours(0, 0, 0, 0)) / 86400000) : null;

function warrantyHtml(d) {
  if (!d) return `<span style="color:var(--subtext)">–</span>`;
  const n = daysUntil(d);
  if (n < 0) return `<span style="color:var(--danger)" title="abgelaufen">⛔ ${fmtDate(d)}</span>`;
  if (n <= WARN_DAYS) return `<span style="color:#f59e0b" title="läuft in ${n} Tagen ab">⚠️ ${fmtDate(d)}</span>`;
  return `<span>${fmtDate(d)}</span>`;
}

function badge(status) {
  const [ic, label, col] = STATUS[status] || STATUS.storage;
  return `<span style="display:inline-flex;gap:4px;align-items:center;padding:2px 8px;border-radius:999px;
    font-size:11px;border:1px solid ${col};color:${col};white-space:nowrap">${ic} ${label}</span>`;
}

export function renderInventory(body, win) {
  let data = { warehouses: [], items: [], users: [], offices: [], departments: [], can_manage: false };
  let filter = { kind: "all", value: "" };
  let search = "", catFilter = "";
  let sort = { key: "name", dir: 1 };
  let selectedId = null;
  const openWh = new Set();          // aufgeklappte Lager in der Seitenleiste

  body.innerHTML = `
    <div style="display:flex;height:100%;position:relative;min-height:0">
      <div id="iv-side" style="width:230px;flex-shrink:0;border-right:1px solid var(--border);overflow:auto;padding:8px 6px;font-size:13px"></div>
      <div style="flex:1;display:flex;flex-direction:column;min-width:0">
        <div class="explorer-toolbar" style="flex-wrap:wrap;gap:8px">
          <input type="text" id="iv-search" placeholder="🔍 Suchen: Name, Seriennr., Inventarnr., Besitzer, Notiz …" style="flex:1;min-width:180px" />
          <select id="iv-cat" style="max-width:170px"><option value="">Alle Typen</option>
            ${Object.entries(CATEGORIES).map(([k, [ic, l]]) => `<option value="${k}">${ic} ${l}</option>`).join("")}
          </select>
          <button class="btn-primary" id="iv-new" style="display:none">➕ Gerät</button>
          <button class="btn" id="iv-wh" style="display:none">🏬 Lager verwalten</button>
          <button class="btn" id="iv-pl" style="display:none" title="Büros und Abteilungen für Geräte im Einsatz">🏢 Büros &amp; Abteilungen</button>
          <button class="btn" id="iv-csv" title="Aktuelle Liste als CSV herunterladen">⬇ CSV</button>
        </div>
        <div style="flex:1;overflow:auto;min-height:0">
          <table id="iv-table" style="width:100%;border-collapse:collapse;font-size:13px"></table>
        </div>
        <div id="iv-foot" style="padding:6px 10px;border-top:1px solid var(--border);font-size:12px;color:var(--subtext)"></div>
      </div>
      <div id="iv-detail" style="width:340px;flex-shrink:0;border-left:1px solid var(--border);overflow:auto;display:none"></div>
      <div id="iv-dlg" style="display:none;position:absolute;inset:0;background:rgba(0,0,0,0.55);align-items:center;justify-content:center;z-index:5"></div>
    </div>`;

  const $ = (s) => body.querySelector(s);
  const sideEl = $("#iv-side"), tableEl = $("#iv-table"), footEl = $("#iv-foot");
  const detailEl = $("#iv-detail"), dlgEl = $("#iv-dlg");
  const notify = (m, type = "info") => window.notify?.(m, type);

  // ---------------- Daten ----------------
  async function load() {
    try {
      data = await moduleRequest("/api/inventory/overview");
      $("#iv-new").style.display = data.can_manage ? "" : "none";
      $("#iv-wh").style.display = data.can_manage ? "" : "none";
      $("#iv-pl").style.display = data.can_manage ? "" : "none";
      data.offices = data.offices || [];
      data.departments = data.departments || [];
      drawAll();
    } catch (e) {
      tableEl.innerHTML = `<tr><td style="padding:20px;color:var(--danger)">${esc(e.message)}</td></tr>`;
    }
  }

  async function call(path, method = "GET", payload) {
    return moduleRequest(path, payload === undefined ? { method }
      : { method, body: JSON.stringify(payload) });
  }

  const whById = (id) => data.warehouses.find((w) => w.id === id);
  const shelfById = (id) => {
    for (const w of data.warehouses) {
      const s = (w.shelves || []).find((x) => x.id === id);
      if (s) return s;
    }
    return null;
  };
  function locationText(it) {
    if (!it.warehouse_id) return "";
    const w = whById(it.warehouse_id), s = shelfById(it.shelf_id);
    return (w ? w.name : "?") + (s ? ` / ${s.name}` : "");
  }
  const placeById = (id) => id ? [...data.offices, ...data.departments].find((p) => p.id === id) : null;
  // "Büro X · Abteilung Y" (nur Geräte im Einsatz)
  function placeText(it) {
    if (it.status !== "in_use") return "";
    const o = placeById(it.office_id), d = placeById(it.department_id);
    return [o ? `🏢 ${o.name}` : "", d ? `👥 ${d.name}` : ""].filter(Boolean).join(" · ");
  }
  function whereText(it) {
    if (it.status === "in_use") return `👤 ${it.owner_name}` + (placeText(it) ? ` · ${placeText(it)}` : "");
    const loc = locationText(it);
    return loc ? `🏬 ${loc}` : (it.owner_name ? `👤 ${it.owner_name}` : "–");
  }

  // ---------------- Filter ----------------
  function matchesFilter(it) {
    switch (filter.kind) {
      case "status": return it.status === filter.value;
      case "warranty": { const n = daysUntil(it.warranty_until); return n != null && n <= WARN_DAYS && it.status !== "retired"; }
      case "owner": return it.owner_name === filter.value && it.status === "in_use";
      case "office": return it.status === "in_use" && (it.office_id || "") === filter.value;
      case "department": return it.status === "in_use" && (it.department_id || "") === filter.value;
      case "warehouse": return it.warehouse_id === filter.value;
      case "shelf": return it.shelf_id === filter.value;
      case "noshelf": return it.warehouse_id === filter.value && !it.shelf_id;
      default: return true;
    }
  }
  function visibleItems() {
    const q = search.toLowerCase();
    const list = data.items.filter((it) => {
      if (!matchesFilter(it)) return false;
      if (catFilter && it.category !== catFilter) return false;
      if (!q) return true;
      return [it.name, it.manufacturer, it.model, it.serial, it.asset_tag, it.owner_name,
        it.notes, it.supplier, locationText(it), placeText(it), cat(it.category)[1]]
        .some((v) => (v || "").toLowerCase().includes(q));
    });
    const val = (it) => {
      switch (sort.key) {
        case "cost": return it.cost ?? -1;
        case "warranty": return it.warranty_until || "9999";
        case "where": return whereText(it).toLowerCase();
        case "status": return it.status;
        case "tag": return (it.asset_tag || "").toLowerCase();
        default: return (it.name || "").toLowerCase();
      }
    };
    return list.sort((a, b) => (val(a) > val(b) ? 1 : val(a) < val(b) ? -1 : 0) * sort.dir);
  }

  // ---------------- Seitenleiste ----------------
  function drawSide() {
    const items = data.items;
    const cnt = (fn) => items.filter(fn).length;
    const row = (kind, value, label, n, indent = 0, extra = "") => {
      const active = filter.kind === kind && filter.value === value;
      return `<div class="iv-nav" data-kind="${kind}" data-value="${esc(value)}"
        style="display:flex;align-items:center;gap:6px;padding:5px 8px;padding-left:${8 + indent}px;border-radius:6px;cursor:pointer;
        ${active ? "background:var(--accent);color:#fff;" : ""}">
        ${extra}<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${label}</span>
        <span style="font-size:11px;opacity:.75">${n}</span></div>`;
    };
    const head = (t, btn = "") => `<div style="display:flex;align-items:center;margin:12px 4px 4px;font-size:11px;
      font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--subtext)"><span style="flex:1">${t}</span>${btn}</div>`;

    let html = row("all", "", "📋 Alle Geräte", items.length);
    for (const [k, [ic, label]] of Object.entries(STATUS)) {
      html += row("status", k, `${ic} ${label}`, cnt((i) => i.status === k));
    }
    const warn = cnt((i) => { const n = daysUntil(i.warranty_until); return n != null && n <= WARN_DAYS && i.status !== "retired"; });
    html += row("warranty", "", "⚠️ Garantie läuft ab", warn);

    // Besitzer
    const owners = {};
    for (const i of items) if (i.status === "in_use" && i.owner_name) owners[i.owner_name] = (owners[i.owner_name] || 0) + 1;
    const ownerNames = Object.keys(owners).sort((a, b) => a.localeCompare(b, "de"));
    html += head(`Besitzer (${ownerNames.length})`);
    html += ownerNames.length
      ? ownerNames.map((o) => row("owner", o, `👤 ${esc(o)}`, owners[o])).join("")
      : `<div style="padding:4px 8px;color:var(--subtext);font-size:12px">noch niemand</div>`;

    // Bueros + Abteilungen (Geraete im Einsatz)
    const inUse = items.filter((i) => i.status === "in_use");
    for (const [kind, list, title, ico, field] of [
      ["office", data.offices, "Büros", "🏢", "office_id"],
      ["department", data.departments, "Abteilungen", "👥", "department_id"],
    ]) {
      if (!list.length && !data.can_manage) continue;
      html += head(`${title} (${list.length})`, data.can_manage
        ? `<button class="btn btn-sm iv-side-pl" title="Büros &amp; Abteilungen verwalten" style="padding:0 6px">⚙</button>` : "");
      html += list.length
        ? list.map((p) => row(kind, p.id, `${ico} ${esc(p.name)}`, inUse.filter((i) => i[field] === p.id).length)).join("")
          + (inUse.some((i) => !i[field]) ? row(kind, "", `<i>ohne ${kind === "office" ? "Büro" : "Abteilung"}</i>`, inUse.filter((i) => !i[field]).length, 0) : "")
        : `<div style="padding:4px 8px;color:var(--subtext);font-size:12px">Noch keine angelegt.</div>`;
    }

    // Lager + Regale
    html += head("Lager", data.can_manage
      ? `<button class="btn btn-sm" id="iv-side-wh" title="Lager verwalten" style="padding:0 6px">⚙</button>` : "");
    if (!data.warehouses.length) {
      html += `<div style="padding:4px 8px;color:var(--subtext);font-size:12px">Noch kein Lager angelegt.</div>`;
    }
    for (const w of data.warehouses) {
      const open = openWh.has(w.id);
      const tog = `<span class="iv-tog" data-wh="${esc(w.id)}" style="width:12px;display:inline-block;opacity:.7">${(w.shelves || []).length ? (open ? "▾" : "▸") : ""}</span>`;
      html += row("warehouse", w.id, `🏬 ${esc(w.name)}`, cnt((i) => i.warehouse_id === w.id), 0, tog);
      if (open) {
        for (const s of w.shelves || []) {
          html += row("shelf", s.id, `🗃️ ${esc(s.name)}`, cnt((i) => i.shelf_id === s.id), 18);
        }
        const loose = cnt((i) => i.warehouse_id === w.id && !i.shelf_id);
        if (loose) html += row("noshelf", w.id, `<i>ohne Regal</i>`, loose, 18);
      }
    }
    sideEl.innerHTML = html;

    sideEl.querySelectorAll(".iv-nav").forEach((el) => el.addEventListener("click", (ev) => {
      const tog = ev.target.closest(".iv-tog");
      if (tog) {
        ev.stopPropagation();
        const id = tog.dataset.wh;
        openWh.has(id) ? openWh.delete(id) : openWh.add(id);
        drawSide();
        return;
      }
      filter = { kind: el.dataset.kind, value: el.dataset.value };
      if (filter.kind === "warehouse") openWh.add(filter.value);
      drawAll();
    }));
    sideEl.querySelector("#iv-side-wh")?.addEventListener("click", openWarehouseManager);
    sideEl.querySelectorAll(".iv-side-pl").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); openPlaceManager(); }));
  }

  // ---------------- Tabelle ----------------
  function drawTable() {
    const list = visibleItems();
    const th = (key, label, align = "left") => {
      const arrow = sort.key === key ? (sort.dir > 0 ? " ▲" : " ▼") : "";
      return `<th data-sort="${key}" style="text-align:${align};padding:8px;position:sticky;top:0;background:var(--panel);
        border-bottom:1px solid var(--border);cursor:pointer;font-weight:600;font-size:12px;color:var(--subtext);white-space:nowrap">${label}${arrow}</th>`;
    };
    let html = `<thead><tr>${th("name", "Gerät")}${th("tag", "Inventar-Nr.")}${th("status", "Status")}
      ${th("where", "Besitzer / Lagerort")}${th("warranty", "Garantie")}${th("cost", "Kosten", "right")}</tr></thead><tbody>`;
    if (!list.length) {
      html += `<tr><td colspan="6" style="padding:30px;text-align:center;color:var(--subtext)">
        ${data.items.length ? "Keine Geräte für diesen Filter." : (data.can_manage
          ? "Noch keine Geräte – oben auf „➕ Gerät“ klicken." : "Noch keine Geräte erfasst.")}</td></tr>`;
    }
    for (const it of list) {
      const [ic, cl] = cat(it.category);
      const sub = [it.manufacturer, it.model].filter(Boolean).join(" ") || cl;
      const sel = it.id === selectedId;
      html += `<tr data-id="${esc(it.id)}" style="cursor:pointer;border-bottom:1px solid var(--border);${sel ? "background:var(--panel-2);" : ""}">
        <td style="padding:7px 8px"><div style="display:flex;gap:8px;align-items:center">
          <span style="font-size:20px;width:26px;text-align:center">${ic}</span>
          <div style="min-width:0"><div style="font-weight:600">${esc(it.name)}${it.client_id ? ` <span title="mit RMM-Agent verknüpft">🔗</span>` : ""}</div>
          <div style="font-size:11px;color:var(--subtext)">${esc(sub)}</div></div></div></td>
        <td style="padding:7px 8px;font-family:monospace;font-size:12px">${esc(it.asset_tag || "–")}</td>
        <td style="padding:7px 8px">${badge(it.status)}</td>
        <td style="padding:7px 8px">${esc(whereText(it))}</td>
        <td style="padding:7px 8px;white-space:nowrap">${warrantyHtml(it.warranty_until)}</td>
        <td style="padding:7px 8px;text-align:right;white-space:nowrap">${money(it.cost)}</td></tr>`;
    }
    tableEl.innerHTML = html + "</tbody>";
    tableEl.querySelectorAll("th[data-sort]").forEach((h) => h.addEventListener("click", () => {
      const k = h.dataset.sort;
      sort = { key: k, dir: sort.key === k ? -sort.dir : 1 };
      drawTable();
    }));
    tableEl.querySelectorAll("tr[data-id]").forEach((tr) => {
      tr.addEventListener("click", () => { selectedId = tr.dataset.id; drawTable(); drawDetail(); });
      tr.addEventListener("dblclick", () => { if (data.can_manage) openEditor(itemById(tr.dataset.id)); });
    });

    const total = list.reduce((s, i) => s + (Number(i.cost) || 0), 0);
    footEl.textContent = `${list.length} von ${data.items.length} Geräten · Summe Kosten: ${money(total)}`;
  }

  const itemById = (id) => data.items.find((i) => i.id === id);

  // ---------------- Details ----------------
  async function drawDetail() {
    const it = itemById(selectedId);
    if (!it) { detailEl.style.display = "none"; return; }
    detailEl.style.display = "";
    const [ic, cl] = cat(it.category);
    const field = (label, value) => value ? `<div style="display:flex;gap:8px;padding:3px 0">
      <span style="width:110px;flex-shrink:0;color:var(--subtext);font-size:12px">${label}</span>
      <span style="flex:1;word-break:break-word">${value}</span></div>` : "";
    const client = it.client_id ? (findClient(it.client_id) || { id: it.client_id, hostname: it.client_id }) : null;
    const m = data.can_manage;

    detailEl.innerHTML = `
      <div style="padding:14px">
        <div style="display:flex;gap:10px;align-items:flex-start">
          <div style="font-size:34px;line-height:1">${ic}</div>
          <div style="flex:1;min-width:0">
            <div style="font-size:16px;font-weight:700;word-break:break-word">${esc(it.name)}</div>
            <div style="font-size:12px;color:var(--subtext)">${esc(cl)}${it.asset_tag ? ` · ${esc(it.asset_tag)}` : ""}</div>
            <div style="margin-top:6px">${badge(it.status)}</div>
          </div>
          <button class="btn btn-sm btn-icon" id="ivd-close" title="Schließen">✕</button>
        </div>

        ${m ? `<div style="display:flex;flex-wrap:wrap;gap:6px;margin:12px 0">
          <button class="btn btn-sm" id="ivd-assign">👤 ${it.status === "in_use" ? "Besitzer ändern" : "Ausgeben"}</button>
          <button class="btn btn-sm" id="ivd-store">📦 Ins Lager</button>
          <button class="btn btn-sm" id="ivd-edit">✏️ Bearbeiten</button>
          <button class="btn btn-sm" id="ivd-dup" title="Gleiches Gerät nochmal anlegen">⧉</button>
          <button class="btn btn-sm btn-danger" id="ivd-del" title="Löschen">🗑</button>
        </div>` : `<div style="height:10px"></div>`}

        <div style="font-size:13px">
          ${field("Besitzer", it.status === "in_use" && it.owner_name ? `👤 ${esc(it.owner_name)}` : "")}
          ${field("Büro", it.status === "in_use" && placeById(it.office_id) ? `🏢 ${esc(placeById(it.office_id).name)}` : "")}
          ${field("Abteilung", it.status === "in_use" && placeById(it.department_id) ? `👥 ${esc(placeById(it.department_id).name)}` : "")}
          ${field("Lagerort", locationText(it) ? `🏬 ${esc(locationText(it))}` : "")}
          ${field("Hersteller", esc(it.manufacturer))}
          ${field("Modell", esc(it.model))}
          ${field("Seriennummer", it.serial ? `<span style="font-family:monospace">${esc(it.serial)}</span>` : "")}
          ${field("Kaufdatum", it.purchase_date ? fmtDate(it.purchase_date) : "")}
          ${field("Kosten", it.cost != null ? money(it.cost) : "")}
          ${field("Garantie bis", it.warranty_until ? warrantyHtml(it.warranty_until)
            + (daysUntil(it.warranty_until) >= 0 ? ` <span style="color:var(--subtext);font-size:11px">(noch ${daysUntil(it.warranty_until)} Tage)</span>` : "") : "")}
          ${field("Lieferant", esc(it.supplier))}
          ${field("RMM-Agent", client ? `<a href="#" id="ivd-agent">🖥️ ${esc(client.hostname || client.id)}</a>` : "")}
        </div>

        ${it.notes ? `<div style="margin-top:10px">
          <div style="font-size:12px;color:var(--subtext);margin-bottom:4px">Notizen</div>
          <div style="white-space:pre-wrap;background:var(--panel-2);border-radius:8px;padding:8px 10px;font-size:13px">${esc(it.notes)}</div>
        </div>` : ""}

        <div style="margin-top:14px">
          <div style="font-size:12px;color:var(--subtext);margin-bottom:4px">Verlauf</div>
          <div id="ivd-hist" style="font-size:12px;color:var(--subtext)">lädt …</div>
        </div>
        <div style="margin-top:10px;font-size:11px;color:var(--subtext)">
          Angelegt ${new Date(it.created_at).toLocaleString("de-DE")}${it.created_by ? ` von ${esc(it.created_by)}` : ""}
        </div>
      </div>`;

    detailEl.querySelector("#ivd-close").onclick = () => { selectedId = null; drawTable(); drawDetail(); };
    detailEl.querySelector("#ivd-agent")?.addEventListener("click", (e) => { e.preventDefault(); openClient(it.client_id); });
    if (m) {
      detailEl.querySelector("#ivd-assign").onclick = () => openAssign(it);
      detailEl.querySelector("#ivd-store").onclick = () => openStore(it);
      detailEl.querySelector("#ivd-edit").onclick = () => openEditor(it);
      detailEl.querySelector("#ivd-dup").onclick = () => openEditor({ ...it, id: null, name: `${it.name} (Kopie)`,
        serial: "", asset_tag: "", notes: it.notes, status: "storage", owner_name: "", owner_user_id: "",
        office_id: "", department_id: "" }, true);
      detailEl.querySelector("#ivd-del").onclick = async () => {
        if (!(await uiConfirm(`„${it.name}“ löschen?`, { danger: true, description: "Das Gerät und sein Verlauf werden endgültig entfernt." }))) return;
        try { await call(`/api/inventory/items/${it.id}`, "DELETE"); selectedId = null; await load(); }
        catch (e) { notify(e.message, "error"); }
      };
    }

    // Verlauf nachladen
    const histEl = detailEl.querySelector("#ivd-hist");
    try {
      const r = await call(`/api/inventory/items/${it.id}/history`);
      if (selectedId !== it.id) return;
      const ACT = { created: "➕ angelegt", edited: "✏️ bearbeitet", assigned: "👤 ausgegeben", stored: "📦 eingelagert" };
      histEl.innerHTML = r.history.length ? r.history.map((h) => `
        <div style="padding:5px 0;border-bottom:1px solid var(--border)">
          <div style="color:var(--text)">${ACT[h.action] || esc(h.action)}${h.details ? ` – ${esc(h.details)}` : ""}</div>
          <div style="font-size:11px">${new Date(h.ts).toLocaleString("de-DE")}${h.username ? ` · ${esc(h.username)}` : ""}</div>
        </div>`).join("") : "Noch keine Einträge.";
    } catch (e) { histEl.textContent = e.message; }
  }

  function openClient(clientId) {
    const c = findClient(clientId);
    if (!c) { notify("Dieser Agent ist nicht (mehr) sichtbar.", "error"); return; }
    openWindow({
      singleton: true, key: `panelpart-${c.id}-client-x`, appId: "panelpart",
      title: c.hostname || c.id, props: { clientId: c.id, part: "client" },
      clientColor: c.color, w: 900, h: 620,
    });
  }

  // ---------------- Dialog-Hilfen ----------------
  function showDialog(title, inner, width = 460) {
    dlgEl.style.display = "flex";
    dlgEl.innerHTML = `<div style="background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:18px;
      width:${width}px;max-width:94%;max-height:92%;overflow:auto">
      <h3 style="margin:0 0 12px">${title}</h3>${inner}</div>`;
    return dlgEl;
  }
  const closeDialog = () => { dlgEl.style.display = "none"; dlgEl.innerHTML = ""; };
  dlgEl.addEventListener("mousedown", (e) => { if (e.target === dlgEl) closeDialog(); });

  const ownerSuggestions = () => {
    const names = new Set(data.users.map((u) => u.name));
    for (const i of data.items) if (i.owner_name) names.add(i.owner_name);
    return [...names].sort((a, b) => a.localeCompare(b, "de"));
  };
  const ownerDatalist = (id) => `<datalist id="${id}">${ownerSuggestions().map((n) => `<option value="${esc(n)}">`).join("")}</datalist>`;
  const userIdFor = (name) => data.users.find((u) => u.name === name)?.id || "";

  function placeOptions(kind, sel) {
    const list = kind === "office" ? data.offices : data.departments;
    return `<option value="">– ${kind === "office" ? "kein Büro" : "keine Abteilung"} –</option>`
      + list.map((p) => `<option value="${esc(p.id)}" ${p.id === sel ? "selected" : ""}>${esc(p.name)}</option>`).join("");
  }
  function warehouseOptions(sel) {
    return `<option value="">– Lager wählen –</option>` + data.warehouses.map((w) =>
      `<option value="${esc(w.id)}" ${w.id === sel ? "selected" : ""}>${esc(w.name)}</option>`).join("");
  }
  function shelfOptions(wid, sel) {
    const w = whById(wid);
    return `<option value="">– ohne Regal –</option>` + (w?.shelves || []).map((s) =>
      `<option value="${esc(s.id)}" ${s.id === sel ? "selected" : ""}>${esc(s.name)}</option>`).join("");
  }
  function bindLocation(root, whSel, shSel) {
    root.querySelector(whSel).addEventListener("change", (e) => {
      root.querySelector(shSel).innerHTML = shelfOptions(e.target.value, "");
    });
  }

  // ---------------- Geraet anlegen / bearbeiten ----------------
  function openEditor(existing, isCopy = false) {
    const it = existing || { status: data.warehouses.length ? "storage" : "in_use", category: "laptop" };
    const isEdit = !!existing?.id && !isCopy;
    const v = (k) => esc(it[k] ?? "");
    const clients = [...(state.clients || [])].sort((a, b) => (a.hostname || "").localeCompare(b.hostname || ""));
    const d = showDialog(isEdit ? "Gerät bearbeiten" : "Neues Gerät", `
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:0 12px">
        <div class="form-row" style="grid-column:1/3"><label>Name *</label>
          <input id="ive-name" value="${v("name")}" placeholder="z. B. Laptop Max Mustermann" /></div>
        <div class="form-row"><label>Typ</label>
          <select id="ive-cat">${Object.entries(CATEGORIES).map(([k, [ic, l]]) =>
            `<option value="${k}" ${it.category === k ? "selected" : ""}>${ic} ${l}</option>`).join("")}</select></div>
        <div class="form-row"><label>Inventar-Nr.</label><input id="ive-tag" value="${v("asset_tag")}" placeholder="z. B. INV-0042" /></div>
        <div class="form-row"><label>Hersteller</label><input id="ive-man" value="${v("manufacturer")}" /></div>
        <div class="form-row"><label>Modell</label><input id="ive-model" value="${v("model")}" /></div>
        <div class="form-row" style="grid-column:1/3"><label>Seriennummer</label><input id="ive-serial" value="${v("serial")}" /></div>

        <div class="form-row"><label>Status</label>
          <select id="ive-status">${Object.entries(STATUS).map(([k, [ic, l]]) =>
            `<option value="${k}" ${it.status === k ? "selected" : ""}>${ic} ${l}</option>`).join("")}</select></div>
        <div class="form-row" id="ive-owner-row"><label>Besitzer *</label>
          <input id="ive-owner" list="ive-owners" value="${v("owner_name")}" placeholder="Name des Mitarbeiters" />${ownerDatalist("ive-owners")}</div>
        <div class="form-row ive-inuse"><label>Büro</label><select id="ive-office">${placeOptions("office", it.office_id)}</select></div>
        <div class="form-row ive-inuse"><label>Abteilung</label><select id="ive-dept">${placeOptions("department", it.department_id)}</select></div>
        <div class="form-row" id="ive-wh-row"><label>Lager</label><select id="ive-wh">${warehouseOptions(it.warehouse_id)}</select></div>
        <div class="form-row" id="ive-sh-row"><label>Regal</label><select id="ive-sh">${shelfOptions(it.warehouse_id, it.shelf_id)}</select></div>

        <div class="form-row"><label>Kaufdatum</label><input id="ive-buy" type="date" value="${v("purchase_date")}" /></div>
        <div class="form-row"><label>Kosten (€)</label><input id="ive-cost" type="number" min="0" step="0.01" value="${it.cost ?? ""}" /></div>
        <div class="form-row"><label>Garantie bis</label><input id="ive-war" type="date" value="${v("warranty_until")}" /></div>
        <div class="form-row"><label>Lieferant</label><input id="ive-sup" value="${v("supplier")}" /></div>
        <div class="form-row" style="grid-column:1/3"><label>Mit RMM-Agent verknüpfen (optional)</label>
          <select id="ive-client"><option value="">– keiner –</option>${clients.map((c) =>
            `<option value="${esc(c.id)}" ${it.client_id === c.id ? "selected" : ""}>${esc(c.hostname || c.id)}</option>`).join("")}</select></div>
        <div class="form-row" style="grid-column:1/3"><label>Notizen</label>
          <textarea id="ive-notes" rows="4" style="width:100%">${v("notes")}</textarea></div>
      </div>
      <div class="form-error hidden" id="ive-err"></div>
      <div style="display:flex;gap:8px;justify-content:flex-end">
        <button class="btn" id="ive-cancel">Abbrechen</button>
        <button class="btn-primary" id="ive-save">${isEdit ? "Speichern" : "Anlegen"}</button>
      </div>`, 560);

    const statusSel = d.querySelector("#ive-status");
    const syncStatus = () => {
      const s = statusSel.value;
      d.querySelector("#ive-owner-row").style.display = s === "in_use" ? "" : "none";
      d.querySelectorAll(".ive-inuse").forEach((r) => { r.style.display = s === "in_use" ? "" : "none"; });
      d.querySelector("#ive-wh-row").style.display = s === "in_use" ? "none" : "";
      d.querySelector("#ive-sh-row").style.display = s === "in_use" ? "none" : "";
    };
    statusSel.addEventListener("change", syncStatus); syncStatus();
    bindLocation(d, "#ive-wh", "#ive-sh");
    d.querySelector("#ive-cancel").onclick = closeDialog;
    d.querySelector("#ive-name").focus();

    d.querySelector("#ive-save").onclick = async () => {
      const g = (id) => d.querySelector(id).value.trim();
      const owner = g("#ive-owner");
      const costRaw = g("#ive-cost").replace(",", ".");
      const payload = {
        name: g("#ive-name"), category: g("#ive-cat"), asset_tag: g("#ive-tag"),
        manufacturer: g("#ive-man"), model: g("#ive-model"), serial: g("#ive-serial"),
        status: g("#ive-status"), owner_name: owner, owner_user_id: userIdFor(owner),
        warehouse_id: g("#ive-wh"), shelf_id: g("#ive-sh"),
        office_id: g("#ive-office"), department_id: g("#ive-dept"),
        purchase_date: g("#ive-buy"), cost: costRaw === "" ? null : Number(costRaw),
        warranty_until: g("#ive-war"), supplier: g("#ive-sup"),
        client_id: g("#ive-client"), notes: d.querySelector("#ive-notes").value,
      };
      const err = d.querySelector("#ive-err");
      try {
        const r = isEdit ? await call(`/api/inventory/items/${existing.id}`, "PUT", payload)
          : await call("/api/inventory/items", "POST", payload);
        selectedId = r.id;
        closeDialog();
        await load();
      } catch (e) { err.textContent = e.message; err.classList.remove("hidden"); }
    };
  }

  // ---------------- Besitzer aendern / ausgeben ----------------
  function openAssign(it) {
    const d = showDialog(it.status === "in_use" ? "Besitzer ändern" : "Gerät ausgeben", `
      <div style="font-size:13px;color:var(--subtext);margin-bottom:10px">${esc(it.name)} · derzeit: ${esc(whereText(it))}</div>
      <div class="form-row"><label>Neuer Besitzer</label>
        <input id="iva-owner" list="iva-owners" placeholder="Name des Mitarbeiters" />${ownerDatalist("iva-owners")}</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:0 12px">
        <div class="form-row"><label>Büro</label><select id="iva-office">${placeOptions("office", it.status === "in_use" ? it.office_id : "")}</select></div>
        <div class="form-row"><label>Abteilung</label><select id="iva-dept">${placeOptions("department", it.status === "in_use" ? it.department_id : "")}</select></div>
      </div>
      <div class="form-row"><label>Notiz (optional, landet im Verlauf)</label>
        <input id="iva-note" placeholder="z. B. Übergabe mit Netzteil + Tasche" /></div>
      <div class="form-error hidden" id="iva-err"></div>
      <div style="display:flex;gap:8px;justify-content:flex-end">
        <button class="btn" id="iva-cancel">Abbrechen</button>
        <button class="btn-primary" id="iva-ok">👤 Übergeben</button>
      </div>`);
    d.querySelector("#iva-owner").focus();
    d.querySelector("#iva-cancel").onclick = closeDialog;
    d.querySelector("#iva-ok").onclick = async () => {
      const owner = d.querySelector("#iva-owner").value.trim();
      try {
        await call(`/api/inventory/items/${it.id}/assign`, "POST",
          { owner_name: owner, owner_user_id: userIdFor(owner), note: d.querySelector("#iva-note").value,
            office_id: d.querySelector("#iva-office").value, department_id: d.querySelector("#iva-dept").value });
        closeDialog(); await load();
      } catch (e) { const el = d.querySelector("#iva-err"); el.textContent = e.message; el.classList.remove("hidden"); }
    };
  }

  // ---------------- Ins Lager stellen ----------------
  function openStore(it) {
    if (!data.warehouses.length) {
      notify("Bitte zuerst ein Lager anlegen (🏬 Lager verwalten).", "error");
      openWarehouseManager();
      return;
    }
    const wid = it.warehouse_id || (data.warehouses.length === 1 ? data.warehouses[0].id : "");
    const d = showDialog("Ins Lager stellen", `
      <div style="font-size:13px;color:var(--subtext);margin-bottom:10px">${esc(it.name)} · derzeit: ${esc(whereText(it))}</div>
      <div class="form-row"><label>Lager</label><select id="ivs-wh">${warehouseOptions(wid)}</select></div>
      <div class="form-row"><label>Regal</label><select id="ivs-sh">${shelfOptions(wid, it.shelf_id)}</select></div>
      <div class="form-row"><label>Notiz (optional, landet im Verlauf)</label>
        <input id="ivs-note" placeholder="z. B. zurückgegeben, Akku schwach" /></div>
      <div class="form-error hidden" id="ivs-err"></div>
      <div style="display:flex;gap:8px;justify-content:flex-end">
        <button class="btn" id="ivs-cancel">Abbrechen</button>
        <button class="btn-primary" id="ivs-ok">📦 Einlagern</button>
      </div>`);
    bindLocation(d, "#ivs-wh", "#ivs-sh");
    d.querySelector("#ivs-cancel").onclick = closeDialog;
    d.querySelector("#ivs-ok").onclick = async () => {
      try {
        await call(`/api/inventory/items/${it.id}/store`, "POST", {
          warehouse_id: d.querySelector("#ivs-wh").value, shelf_id: d.querySelector("#ivs-sh").value,
          note: d.querySelector("#ivs-note").value,
        });
        closeDialog(); await load();
      } catch (e) { const el = d.querySelector("#ivs-err"); el.textContent = e.message; el.classList.remove("hidden"); }
    };
  }

  // ---------------- Lager + Regale verwalten ----------------
  function openWarehouseManager() {
    const cntWh = (id) => data.items.filter((i) => i.warehouse_id === id).length;
    const cntSh = (id) => data.items.filter((i) => i.shelf_id === id).length;
    const d = showDialog("🏬 Lager & Regale", `
      <div id="ivw-list" style="display:flex;flex-direction:column;gap:10px;margin-bottom:12px">
        ${data.warehouses.length ? data.warehouses.map((w) => `
          <div style="border:1px solid var(--border);border-radius:10px;padding:10px">
            <div style="display:flex;gap:6px;align-items:center">
              <span style="flex:1;font-weight:600">🏬 ${esc(w.name)} <span style="font-weight:400;color:var(--subtext);font-size:12px">(${cntWh(w.id)} Geräte)</span></span>
              <button class="btn btn-sm" data-act="wren" data-id="${esc(w.id)}" title="Umbenennen">✏️</button>
              <button class="btn btn-sm btn-danger" data-act="wdel" data-id="${esc(w.id)}" title="Lager löschen">🗑</button>
            </div>
            ${w.notes ? `<div style="font-size:12px;color:var(--subtext);margin-top:2px">${esc(w.notes)}</div>` : ""}
            <div style="margin:8px 0 0 14px;display:flex;flex-direction:column;gap:4px">
              ${(w.shelves || []).map((s) => `
                <div style="display:flex;gap:6px;align-items:center;font-size:13px">
                  <span style="flex:1">🗃️ ${esc(s.name)} <span style="color:var(--subtext);font-size:11px">(${cntSh(s.id)})</span></span>
                  <button class="btn btn-sm" data-act="sren" data-id="${esc(s.id)}" title="Umbenennen">✏️</button>
                  <button class="btn btn-sm btn-danger" data-act="sdel" data-id="${esc(s.id)}" title="Regal löschen">🗑</button>
                </div>`).join("") || `<div style="font-size:12px;color:var(--subtext)">Noch keine Regale.</div>`}
              <div><button class="btn btn-sm" data-act="sadd" data-id="${esc(w.id)}">➕ Regal</button></div>
            </div>
          </div>`).join("") : `<div style="color:var(--subtext);font-size:13px">Noch kein Lager – lege dein erstes an.</div>`}
      </div>
      <div style="display:flex;gap:8px;justify-content:space-between">
        <button class="btn-primary" id="ivw-add">➕ Lager anlegen</button>
        <button class="btn" id="ivw-close">Fertig</button>
      </div>`, 480);

    d.querySelector("#ivw-close").onclick = closeDialog;
    const refresh = async () => { await load(); openWarehouseManager(); };
    const guarded = async (fn) => { try { await fn(); await refresh(); } catch (e) { notify(e.message, "error"); } };

    d.querySelector("#ivw-add").onclick = async () => {
      const name = await uiPrompt("Name des Lagers", { placeholder: "z. B. Keller Büro Bielefeld" });
      if (name?.trim()) guarded(async () => {
        const w = await call("/api/inventory/warehouses", "POST", { name });
        openWh.add(w.id);
      });
    };
    d.querySelectorAll("button[data-act]").forEach((b) => b.addEventListener("click", async () => {
      const id = b.dataset.id;
      const w = whById(id), s = shelfById(id);
      switch (b.dataset.act) {
        case "wren": {
          const name = await uiPrompt("Lager umbenennen", { value: w.name });
          if (name?.trim()) guarded(() => call(`/api/inventory/warehouses/${id}`, "PUT", { name, notes: w.notes || "" }));
          break;
        }
        case "wdel":
          if (await uiConfirm(`Lager „${w.name}“ mit allen Regalen löschen?`, { danger: true }))
            guarded(() => call(`/api/inventory/warehouses/${id}`, "DELETE"));
          break;
        case "sadd": {
          const name = await uiPrompt("Name des Regals", { placeholder: "z. B. Regal A / Fach 3" });
          if (name?.trim()) guarded(() => call(`/api/inventory/warehouses/${id}/shelves`, "POST", { name }));
          break;
        }
        case "sren": {
          const name = await uiPrompt("Regal umbenennen", { value: s.name });
          if (name?.trim()) guarded(() => call(`/api/inventory/shelves/${id}`, "PUT", { name, notes: s.notes || "" }));
          break;
        }
        case "sdel":
          if (await uiConfirm(`Regal „${s.name}“ löschen?`, { danger: true }))
            guarded(() => call(`/api/inventory/shelves/${id}`, "DELETE"));
          break;
      }
    }));
  }

  // ---------------- Bueros + Abteilungen verwalten ----------------
  function openPlaceManager() {
    const cnt = (field, id) => data.items.filter((i) => i.status === "in_use" && i[field] === id).length;
    const block = (kind, list, title, ico, field, ph) => `
      <div style="border:1px solid var(--border);border-radius:10px;padding:10px">
        <div style="display:flex;align-items:center;margin-bottom:6px">
          <b style="flex:1">${ico} ${title}</b>
          <button class="btn btn-sm" data-act="add" data-kind="${kind}" data-ph="${esc(ph)}">➕ ${kind === "office" ? "Büro" : "Abteilung"}</button>
        </div>
        ${list.length ? list.map((p) => `
          <div style="display:flex;gap:6px;align-items:center;font-size:13px;padding:2px 0">
            <span style="flex:1">${esc(p.name)} <span style="color:var(--subtext);font-size:11px">(${cnt(field, p.id)} Geräte)</span>
              ${p.notes ? `<div style="font-size:11px;color:var(--subtext)">${esc(p.notes)}</div>` : ""}</span>
            <button class="btn btn-sm" data-act="ren" data-id="${esc(p.id)}" title="Umbenennen">✏️</button>
            <button class="btn btn-sm btn-danger" data-act="del" data-id="${esc(p.id)}" title="Löschen">🗑</button>
          </div>`).join("") : `<div style="font-size:12px;color:var(--subtext)">Noch keine angelegt.</div>`}
      </div>`;
    const d = showDialog("🏢 Büros & Abteilungen", `
      <div style="font-size:12px;color:var(--subtext);margin-bottom:10px">Geräte „im Einsatz“ können einem Büro und einer Abteilung zugeordnet werden.</div>
      <div style="display:flex;flex-direction:column;gap:10px;margin-bottom:12px">
        ${block("office", data.offices, "Büros", "🏢", "office_id", "z. B. Büro Bielefeld 2. OG")}
        ${block("department", data.departments, "Abteilungen", "👥", "department_id", "z. B. Buchhaltung")}
      </div>
      <div style="display:flex;justify-content:flex-end"><button class="btn" id="ivp-close">Fertig</button></div>`, 480);
    d.querySelector("#ivp-close").onclick = closeDialog;
    const refresh = async () => { await load(); openPlaceManager(); };
    const guarded = async (fn) => { try { await fn(); await refresh(); } catch (e) { notify(e.message, "error"); } };
    d.querySelectorAll("button[data-act]").forEach((b) => b.addEventListener("click", async () => {
      const p = placeById(b.dataset.id);
      if (b.dataset.act === "add") {
        const name = await uiPrompt(b.dataset.kind === "office" ? "Name des Büros" : "Name der Abteilung", { placeholder: b.dataset.ph });
        if (name?.trim()) guarded(() => call("/api/inventory/places", "POST", { kind: b.dataset.kind, name }));
      } else if (b.dataset.act === "ren") {
        const name = await uiPrompt("Umbenennen", { value: p.name });
        if (name?.trim()) guarded(() => call(`/api/inventory/places/${p.id}`, "PUT", { kind: p.kind, name, notes: p.notes || "" }));
      } else if (b.dataset.act === "del") {
        if (await uiConfirm(`„${p.name}“ löschen?`, { danger: true })) guarded(() => call(`/api/inventory/places/${p.id}`, "DELETE"));
      }
    }));
  }

  // ---------------- CSV-Export ----------------
  // CSV export is always ENGLISH (column names + values), comma-separated,
  // ISO dates, dot as decimal separator - independent of the UI language.
  const CSV_TYPE = {
    laptop: "Laptop", pc: "PC", monitor: "Monitor", phone: "Smartphone", tablet: "Tablet",
    printer: "Printer", network: "Network", server: "Server", peripheral: "Peripheral",
    headset: "Headset", camera: "Camera", tv: "TV/Display", furniture: "Furniture",
    vehicle: "Vehicle", key: "Key", license: "License", other: "Other",
  };
  const CSV_STATUS = { in_use: "In use", storage: "In storage", repair: "In repair", retired: "Retired" };
  function exportCsv() {
    const q = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
    const head = ["Name", "Type", "Asset tag", "Manufacturer", "Model", "Serial number", "Status",
      "Owner", "Office", "Department", "Warehouse", "Shelf", "Purchase date", "Cost (EUR)", "Warranty until", "Supplier", "Notes"];
    const rows = visibleItems().map((i) => [
      i.name, CSV_TYPE[i.category] || i.category, i.asset_tag, i.manufacturer, i.model, i.serial, CSV_STATUS[i.status] || i.status,
      i.status === "in_use" ? i.owner_name : "",
      i.status === "in_use" ? placeById(i.office_id)?.name || "" : "", i.status === "in_use" ? placeById(i.department_id)?.name || "" : "",
      whById(i.warehouse_id)?.name || "", shelfById(i.shelf_id)?.name || "",
      i.purchase_date, i.cost != null ? Number(i.cost).toFixed(2) : "", i.warranty_until, i.supplier, i.notes,
    ].map(q).join(","));
    downloadText(`inventory-${new Date().toISOString().slice(0, 10)}.csv`,
      "\ufeff" + [head.map(q).join(","), ...rows].join("\r\n"), "text/csv");
  }

  // ---------------- Verdrahtung ----------------
  function drawAll() { drawSide(); drawTable(); drawDetail(); }

  let searchTimer = null;
  $("#iv-search").addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { search = e.target.value.trim(); drawTable(); }, 150);
  });
  $("#iv-cat").addEventListener("change", (e) => { catFilter = e.target.value; drawTable(); });
  $("#iv-new").addEventListener("click", () => {
    // Vorbelegung passend zum gewaehlten Filter (Lager/Regal/Besitzer)
    const pre = { category: catFilter || "laptop", status: data.warehouses.length ? "storage" : "in_use" };
    if (filter.kind === "warehouse" || filter.kind === "noshelf") Object.assign(pre, { status: "storage", warehouse_id: filter.value });
    if (filter.kind === "shelf") {
      const s = shelfById(filter.value);
      Object.assign(pre, { status: "storage", warehouse_id: s?.warehouse_id, shelf_id: filter.value });
    }
    if (filter.kind === "owner") Object.assign(pre, { status: "in_use", owner_name: filter.value });
    if (filter.kind === "office") Object.assign(pre, { status: "in_use", office_id: filter.value });
    if (filter.kind === "department") Object.assign(pre, { status: "in_use", department_id: filter.value });
    if (filter.kind === "status") pre.status = filter.value;
    openEditor(pre, true);
  });
  $("#iv-wh").addEventListener("click", openWarehouseManager);
  $("#iv-pl").addEventListener("click", openPlaceManager);
  $("#iv-csv").addEventListener("click", exportCsv);

  load();
  if (win?.key) registerCleanup(win.key, () => clearTimeout(searchTimer));
}
