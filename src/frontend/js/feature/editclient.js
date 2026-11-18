// apps/editclient.js
// ------------------
// Inhalt des "Client bearbeiten"-Fensters (öffnet sich über den Bearbeiten-
// Button im Status-Panel). Erlaubt: Name, Tenant, Location, Ordner, Farbe,
// Wartungsmodus, Aktiv/Deaktiviert - sowie das Löschen des Clients.

import { state, findClient } from "../app/state.js";
import { api } from "../app/api.js";
import { moduleRequest } from "../app/modules.js";
import { esc, uiConfirm } from "../app/utils.js";
import { closeWindow, openWindow } from "../app/windowmanager.js";
// t() unter Alias: "t" ist hier bereits als lokaler Variablenname belegt.
import { t as tr } from "../app/i18n.js";
import { CLIENT_TYPES, TYPE_LABELS, clientIconType, deviceIconSvg } from "../app/deviceicons.js";

// Wird von app.js gesetzt, um nach Änderungen alles neu zu laden
let onChanged = null;
export function setEditOnChanged(fn) { onChanged = fn; }

export function renderEditClient(body, win) {
  const client = findClient(win.props.clientId);
  if (!client) {
    body.innerHTML = `<div style="padding:20px;color:var(--danger)">${tr("client_not_found")}</div>`;
    return;
  }

  // Auswahlmöglichkeiten für Tenant/Location/Ordner aus der Hierarchie
  const tenantOptions = state.hierarchy.tenants
    .map((t) => `<option value="${t.id}" ${client.tenant_id === t.id ? "selected" : ""}>${esc(t.name)}</option>`)
    .join("");

  // Locations werden nach gewähltem Tenant gefiltert (hier initial alle des aktuellen Tenants)
  function locationOptionsFor(tenantId) {
    return state.hierarchy.locations
      .filter((l) => l.tenant_id === tenantId)
      .map((l) => `<option value="${l.id}" ${client.location_id === l.id ? "selected" : ""}>${esc(l.name)}</option>`)
      .join("");
  }

  // Ordner einer Location als (verschachtelte) Optionsliste. Unterordner werden
  // mit Einrückung dargestellt. selectedId wird vorausgewählt.
  function folderOptionsFor(locationId, selectedId) {
    if (!locationId) return "";
    const all = state.hierarchy.folders.filter((f) => f.location_id === locationId);
    const out = [];
    const walk = (parentId, depth) => {
      all.filter((f) => (f.parent_folder_id || null) === parentId).forEach((f) => {
        const indent = depth > 0 ? "\u00A0\u00A0".repeat(depth) + "↳ " : "";
        out.push(`<option value="${f.id}" ${selectedId === f.id ? "selected" : ""}>${indent}${esc(f.name)}</option>`);
        walk(f.id, depth + 1);
      });
    };
    walk(null, 0);
    return out.join("");
  }

  body.innerHTML = `
    <div class="settings-section">
      <div class="form-row">
        <label>Anzeigename</label>
        <input type="text" id="ec-name" value="${esc(client.hostname)}" />
      </div>

      <div class="form-row">
        <label>Tenant</label>
        <select id="ec-tenant"><option value="">${tr("ec_unassigned")}</option>${tenantOptions}</select>
      </div>

      <div class="form-row">
        <label>${tr("ec_location")}</label>
        <select id="ec-location"><option value="">${tr("ac_none")}</option>${locationOptionsFor(client.tenant_id)}</select>
      </div>

      <div class="form-row">
        <label>${tr("ec_folder")}</label>
        <select id="ec-folder"><option value="">${tr("ac_none")}</option>${folderOptionsFor(client.location_id, client.folder_id)}</select>
      </div>

      <div class="form-row">
        <label>${tr("ec_color")}</label>
        <input type="color" id="ec-color" value="${esc(client.color || "#38bdf8")}" style="height:38px" />
      </div>

      <div class="form-row">
        <label>Status</label>
        <select id="ec-status">
          <option value="" ${!client.status_override ? "selected" : ""}>${tr("ec_status_auto")}</option>
          <option value="maintenance" ${client.status_override === "maintenance" ? "selected" : ""}>${tr("status_maintenance")}</option>
        </select>
      </div>

      <div class="form-row">
        <label><input type="checkbox" id="ec-active" ${client.active ? "checked" : ""} /> ${tr("ec_active")}</label>
      </div>

      <div class="form-row">
        <label title="Essentielle Geräte werden in der Sidebar ROT angezeigt, sobald sie offline gehen (normale Geräte nur grau).">
          <input type="checkbox" id="ec-essential" ${client.essential ? "checked" : ""} />
          ⭐ Essentiell (bei Offline rot markieren)
        </label>
      </div>

      <div style="display:flex;gap:10px">
        <div class="form-row" style="flex:1">
          <label>Kurzname (Anzeige; leer = Hostname)</label>
          <input id="ec-shortname" value="${esc(client.short_name || "")}" placeholder="z.B. Kasse 1" maxlength="40" />
        </div>
        <div class="form-row" style="flex:1">
          <label>Tags (kommagetrennt)</label>
          <input id="ec-tags" value="${esc(client.tags || "")}" placeholder="produktion, halle2, drucker" maxlength="300" />
        </div>
      </div>

      <div class="form-row">
        <label>${tr("ec_devtype")}</label>
        <select id="ec-devtype">
          <option value="physical" ${(client.device_type || "physical") === "physical" ? "selected" : ""}>💻 ${tr("ec_dev_physical")}</option>
          <option value="vm" ${client.device_type === "vm" ? "selected" : ""}>🖥️ ${tr("ec_dev_vm")}</option>
          <option value="lxc" ${client.device_type === "lxc" ? "selected" : ""}>📦 ${tr("ec_dev_lxc")}</option>
        </select>
      </div>

      <div class="form-row">
        <label>Icon <span style="font-weight:400;color:var(--subtext)">(sidebar, panel, topology)</span></label>
        <div style="display:flex;gap:8px;align-items:center">
          <span id="ec-icon-prev" style="display:inline-flex;color:var(--text)">${deviceIconSvg(clientIconType(client), 26)}</span>
          <select id="ec-icon" style="flex:1">
            <option value="">Automatic (${esc(TYPE_LABELS[clientIconType({ ...client, icon_type: "" })] || "")})</option>
            ${CLIENT_TYPES.map((k) => `<option value="${k}" ${client.icon_type === k ? "selected" : ""}>${esc(TYPE_LABELS[k] || k)}</option>`).join("")}
          </select>
        </div>
      </div>

      <div class="form-row" id="ec-host-row" style="${client.device_type === "vm" || client.device_type === "lxc" ? "" : "display:none"}">
        <label>${tr("ec_host")}</label>
        <select id="ec-host">
          <option value="">${tr("ec_no_host")}</option>
          ${state.clients
            .filter((c) => c.id !== client.id && (c.device_type || "physical") === "physical")
            .map((c) => `<option value="${c.id}" ${client.parent_client_id === c.id ? "selected" : ""}>${esc(c.hostname)}</option>`)
            .join("")}
        </select>
      </div>

      <div class="form-row">
        <label>${tr("ec_autoupdate")}</label>
        <select id="ec-autoupdate">
          <option value="global" ${(client.auto_update || "global") === "global" ? "selected" : ""}>${tr("ec_au_global")}</option>
          <option value="on" ${client.auto_update === "on" ? "selected" : ""}>${tr("ec_au_on")}</option>
          <option value="off" ${client.auto_update === "off" ? "selected" : ""}>${tr("ec_au_off")}</option>
        </select>
      </div>

      <div id="ec-error" class="form-error hidden"></div>

      <div style="display:flex;gap:8px;margin-top:16px">
        <button class="btn-primary" id="ec-save">${tr("save")}</button>
        <button class="action-btn btn-danger" id="ec-delete">${tr("delete")}</button>
      </div>

      <h3 style="margin-top:22px">🔗 ${tr("ec_websites")}</h3>
      <p style="color:var(--subtext);font-size:12px;margin:4px 0 10px">
        ${tr("ec_websites_hint")}
      </p>
      <button class="action-btn" id="ec-ws-goto">🔗 ${tr("ec_websites_open")}</button>

      <h3 style="margin-top:22px">🧰 Werkzeuge</h3>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="action-btn" id="ec-blackbox" title="Letzter gemeldeter Stand (Screenshot, Metriken, Logs) - nützlich, wenn der Client offline ist">🛟 Blackbox</button>
        <button class="action-btn" id="ec-tempuser" title="Temporären Benutzer auf dem Gerät anlegen (Einmal-Passwort, Verfall wählbar)">👤 Temp-Benutzer</button>
      </div>

      <h3 style="margin-top:22px">🛠️ Wartungsmodus</h3>
      <p style="color:var(--subtext);font-size:12px;margin:4px 0 8px">
        Während der Wartung schweigen alle Alarme für dieses Gerät, und Schutzmechanismen sind
        ausgesetzt: USB-Geräte werden immer zugelassen, Updates sind außerhalb des Patch-Zeitfensters
        freigegeben. Endet automatisch nach der gewählten Dauer.
      </p>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <span id="ec-maint-state" style="font-size:12px"></span>
        <select id="ec-maint-dur">
          <option value="30m">30 Minuten</option><option value="1h" selected>1 Stunde</option>
          <option value="2h">2 Stunden</option><option value="4h">4 Stunden</option>
          <option value="8h">8 Stunden</option><option value="24h">24 Stunden</option>
          <option value="manual">Bis ich es selbst beende</option>
        </select>
        <button class="action-btn" id="ec-maint-on">🛠️ Starten</button>
        <button class="action-btn btn-danger" id="ec-maint-off">Beenden</button>
      </div>

      <h3 style="margin-top:22px">⏳ Warteschlange (wenn online)</h3>
      <p style="color:var(--subtext);font-size:12px;margin:4px 0 8px">
        Vorgemerkte Aktionen laufen automatisch, sobald der Agent (wieder) online ist -
        ist er gerade online, sofort. Software-Rollouts legen hier ebenfalls Einträge an.
      </p>
      <div id="ec-queue" style="font-size:12px"></div>
      <div style="display:flex;gap:6px;margin-top:6px">
        <input id="ec-q-cmd" placeholder="Shell-Befehl bei nächstem Online ausführen…" style="flex:1">
        <label style="display:flex;align-items:center;gap:4px;font-size:11px;color:var(--subtext)">
          <input type="checkbox" id="ec-q-admin" checked>als Admin</label>
        <button class="btn" id="ec-q-add">＋ Vormerken</button>
      </div>

    </div>
  `;

  // Websites: komplette Verwaltung liegt im Websites-Widget (panel.js).
  // Hier nur noch ein Sprung dorthin - als eigenes Fenster, damit man den
  // Bearbeiten-Dialog nicht verliert.
  body.querySelector("#ec-ws-goto")?.addEventListener("click", () => {
    openWindow({
      singleton: true,
      key: `panelpart-${client.id}-websites-edit`, appId: "panelpart",
      title: `🔗 Websites — ${client.hostname}`,
      props: { clientId: client.id, part: "websites" },
      clientColor: client.color, w: 460, h: 520,
    });
  });

  // ---- Wartungsmodus (Core) ----
  function drawMaint(m) {
    const el = body.querySelector("#ec-maint-state");
    if (!el) return;
    const active = m ? m.active : (client.status_override === "maintenance"
      && (!client.maintenance_until || client.maintenance_until > Date.now()));
    const until = m ? m.until : client.maintenance_until;
    el.innerHTML = active
      ? `<b style="color:var(--accent)">● aktiv</b> ${until ? `bis ${esc(new Date(until).toLocaleString())}` : "(bis manuell beendet)"}`
      : `<span style="color:var(--subtext)">○ aus</span>`;
  }
  drawMaint(null);
  async function setMaint(duration) {
    try {
      const m = await moduleRequest(`/api/clients/${client.id}/maintenance`, {
        method: "POST", noJob: true, body: JSON.stringify({ duration }) });
      client.status_override = m.active ? "maintenance" : null;
      client.maintenance_until = m.until;
      drawMaint(m);
      window.notify?.(m.active ? "Wartungsmodus aktiv" : "Wartungsmodus beendet", "success");
    } catch (e) { window.notify?.(e.message, "error"); }
  }
  body.querySelector("#ec-maint-on")?.addEventListener("click", () =>
    setMaint(body.querySelector("#ec-maint-dur").value));
  body.querySelector("#ec-maint-off")?.addEventListener("click", () => setMaint("off"));

  // ---- Blackbox: letzter gemeldeter Stand ----
  body.querySelector("#ec-blackbox")?.addEventListener("click", async () => {
    let d;
    try { d = await moduleRequest(`/api/clients/${client.id}/blackbox`); }
    catch (e) { window.notify?.(e.message, "error"); return; }
    const ov = document.createElement("div");
    ov.style.cssText = "position:fixed;inset:0;z-index:11000;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center";
    const when = d.ts ? new Date(d.ts).toLocaleString() : null;
    const m = d.metrics || {};
    const ramPct = m.memTotal ? Math.round((m.memUsed || 0) / m.memTotal * 100) : null;
    ov.innerHTML = `
      <div style="background:var(--panel);border:1px solid var(--border);border-radius:12px;max-width:860px;width:92%;max-height:90%;overflow:auto;padding:16px">
        <div style="display:flex;align-items:center;gap:10px">
          <h3 style="margin:0;flex:1">🛟 Blackbox - ${esc(client.hostname)}</h3>
          <button class="btn" data-close>✕</button>
        </div>
        ${!d.ts ? `<p style="color:var(--subtext)">Noch kein Stand gemeldet. Blackbox in den Einstellungen (Allgemein) aktivieren;
          der Agent meldet dann im eingestellten Intervall.</p>` : `
        <p style="font-size:12px;color:var(--subtext)">Letzter Stand: <b>${esc(when)}</b>
          ${m.cpuLoad != null ? ` · CPU ${esc(String(m.cpuLoad))} %` : ""}${ramPct != null ? ` · RAM ${ramPct} %` : ""}</p>
        ${d.has_shot ? `<img id="bb-shot" style="max-width:100%;border:1px solid var(--border);border-radius:8px" alt="Screenshot">` : ""}
        ${d.deltas?.length ? `<h4 style="margin:12px 0 4px">Metrik-Deltas</h4>
          <pre style="max-height:160px;overflow:auto;font-size:11px;background:var(--panel-2);padding:8px;border-radius:8px">${esc(d.deltas.join("\n"))}</pre>` : ""}
        ${d.log ? `<h4 style="margin:12px 0 4px">Agent-Log (Schwanz)</h4>
          <pre style="max-height:200px;overflow:auto;font-size:11px;background:var(--panel-2);padding:8px;border-radius:8px">${esc(d.log)}</pre>` : ""}`}
      </div>`;
    document.body.appendChild(ov);
    ov.querySelector("[data-close]").addEventListener("click", () => ov.remove());
    ov.addEventListener("click", (e) => { if (e.target === ov) ov.remove(); });
    if (d.has_shot) {
      const { getToken } = await import("../app/api.js");
      fetch(`/api/clients/${client.id}/blackbox/screenshot`, { headers: { Authorization: `Bearer ${getToken()}` } })
        .then((r) => (r.ok ? r.blob() : null))
        .then((b) => { if (b) ov.querySelector("#bb-shot").src = URL.createObjectURL(b); })
        .catch(() => {});
    }
  });

  // ---- Temporärer Benutzer mit Einmal-Passwort ----
  body.querySelector("#ec-tempuser")?.addEventListener("click", async () => {
    const ov = document.createElement("div");
    ov.style.cssText = "position:fixed;inset:0;z-index:11000;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center";
    ov.innerHTML = `
      <div style="background:var(--panel);border:1px solid var(--border);border-radius:12px;width:420px;padding:16px">
        <h3 style="margin:0 0 8px">👤 Temp-Benutzer auf ${esc(client.hostname)}</h3>
        <p style="font-size:12px;color:var(--subtext)">Legt ein lokales Konto mit zufälligem Einmal-Passwort an
          (Linux und Windows). Das Passwort wird nur EINMAL angezeigt und nirgends gespeichert.
          Der Agent löscht das Konto automatisch gemäß Verfall.</p>
        <div class="form-row"><label>Verfall</label>
          <select id="tu-exp" style="width:100%">
            <option value="1h">Nach 1 Stunde</option>
            <option value="2h">Nach 2 Stunden</option>
            <option value="4h">Nach 4 Stunden</option>
            <option value="24h">Nach 24 Stunden</option>
            <option value="logoff">Bei Ende der Benutzung (nach Abmeldung)</option>
            <option value="never">Nie (manuell löschen)</option>
          </select></div>
        <div id="tu-out" style="margin:10px 0"></div>
        <div style="display:flex;gap:8px;justify-content:flex-end">
          <button class="btn" data-close>Schließen</button>
          <button class="btn-primary" id="tu-create">Anlegen</button>
        </div>
      </div>`;
    document.body.appendChild(ov);
    ov.querySelector("[data-close]").addEventListener("click", () => ov.remove());
    ov.querySelector("#tu-create").addEventListener("click", async () => {
      const out = ov.querySelector("#tu-out");
      out.innerHTML = `<span style="color:var(--subtext);font-size:12px">Lege Benutzer an …</span>`;
      ov.querySelector("#tu-create").disabled = true;
      try {
        const r = await moduleRequest(`/api/clients/${client.id}/tempuser`, {
          method: "POST", noJob: true,
          body: JSON.stringify({ expiry: ov.querySelector("#tu-exp").value }),
        });
        out.innerHTML = `
          <div class="panel" style="padding:10px;border:1px solid var(--online,#34d399)">
            <div style="font-size:12px;color:var(--subtext)">Einmalige Anzeige - jetzt kopieren:</div>
            <div style="font-family:monospace;font-size:14px;margin-top:4px">Benutzer: <b>${esc(r.username)}</b><br>Passwort: <b>${esc(r.password)}</b></div>
            <button class="btn btn-sm" style="margin-top:6px" id="tu-copy">📋 Kopieren</button>
            <div style="font-size:11px;color:var(--subtext);margin-top:4px">${esc(r.note || "")}</div>
          </div>`;
        out.querySelector("#tu-copy").addEventListener("click", () =>
          navigator.clipboard?.writeText(`Benutzer: ${r.username}\nPasswort: ${r.password}`));
      } catch (e) {
        out.innerHTML = `<div style="color:var(--danger);font-size:12px">${esc(e.message)}</div>`;
        ov.querySelector("#tu-create").disabled = false;
      }
    });
  });

  // ---- Offline-Warteschlange ----
  const Q_STATUS = { pending: "⏳ wartet", running: "▶ läuft", done: "✅ fertig", failed: "❌ fehlgeschlagen" };
  async function loadQueue() {
    const box = body.querySelector("#ec-queue");
    if (!box) return;
    try {
      const items = await moduleRequest(`/api/clients/${client.id}/queue`);
      box.innerHTML = items.length ? items.map((q) => `
        <div style="display:flex;gap:8px;align-items:center;padding:4px 0;border-bottom:1px solid var(--border)">
          <span style="flex:none">${Q_STATUS[q.status] || q.status}</span>
          <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis" title="${esc(q.result || "")}">${esc(q.label)}</span>
          <span style="color:var(--subtext)">${esc(q.created_by || "")}</span>
          <button class="action-btn btn-danger" data-qdel="${esc(q.id)}" title="Entfernen">✕</button>
        </div>`).join("")
        : `<div style="color:var(--subtext)">Keine vorgemerkten Aktionen.</div>`;
      box.querySelectorAll("[data-qdel]").forEach((b) => b.addEventListener("click", async () => {
        await moduleRequest(`/api/clients/${client.id}/queue/${b.dataset.qdel}`, { method: "DELETE", noJob: true });
        loadQueue();
      }));
    } catch (e) { box.innerHTML = `<div style="color:var(--danger)">${esc(e.message)}</div>`; }
  }
  body.querySelector("#ec-q-add")?.addEventListener("click", async () => {
    const cmd = body.querySelector("#ec-q-cmd").value.trim();
    if (!cmd) return;
    try {
      await moduleRequest(`/api/clients/${client.id}/queue`, {
        method: "POST", noJob: true,
        body: JSON.stringify({ kind: "shell", command: cmd,
                               elevated: body.querySelector("#ec-q-admin").checked }),
      });
      body.querySelector("#ec-q-cmd").value = "";
      loadQueue();
    } catch (e) { window.notify?.(e.message, "error"); }
  });
  loadQueue();

  const tenantSel = body.querySelector("#ec-tenant");
  // Live-Refresh der Tenant-Liste bei Hierarchie-Änderungen (Auswahl bleibt).
  const onHierarchyChanged = () => {
    if (!document.body.contains(body)) {
      window.removeEventListener("rmm:hierarchy-changed", onHierarchyChanged);
      return;
    }
    const cur = tenantSel.value;
    tenantSel.innerHTML = `<option value="">${tr("ec_unassigned")}</option>` +
      state.hierarchy.tenants.map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join("");
    if ([...tenantSel.options].some((o) => o.value === cur)) tenantSel.value = cur;
    tenantSel.dispatchEvent(new Event("change"));
  };
  window.addEventListener("rmm:hierarchy-changed", onHierarchyChanged);
  const locationSel = body.querySelector("#ec-location");
  const folderSel = body.querySelector("#ec-folder");
  const devTypeSel = body.querySelector("#ec-devtype");
  body.querySelector("#ec-icon").addEventListener("change", (e) => {
    body.querySelector("#ec-icon-prev").innerHTML = deviceIconSvg(clientIconType({ ...client, icon_type: e.target.value }), 26);
  });
  // Gerätetyp IMMER frisch: state.clients kann veraltet sein (z.B. wenn der
  // Agent den Typ per Auto-Erkennung gerade erst gemeldet hat). Deshalb wird
  // der Client beim Öffnen direkt vom Backend geholt und der Typ übernommen -
  // solange der Nutzer die Auswahl noch nicht selbst angefasst hat. Damit
  // steht bei einer VM/LXC nie mehr fälschlich "Physisches Gerät", auch nach
  // Fenster-/Seiten-Reload.
  let devTypeTouched = false;
  devTypeSel.addEventListener("change", () => { devTypeTouched = true; });
  (async () => {
    try {
      const fresh = await api.getClient(client.id);
      if (!fresh || !document.body.contains(body)) return;
      // Frische Felder in die state-Referenz übernehmen (gleiche Objekt-Ref).
      Object.assign(client, fresh);
      if (!devTypeTouched && fresh.device_type && devTypeSel.value !== fresh.device_type) {
        devTypeSel.value = fresh.device_type;
        devTypeSel.dispatchEvent(new Event("change"));   // Host-Zeile ein-/ausblenden
        devTypeTouched = false;                          // programmatisch, nicht vom Nutzer
      }
    } catch { /* offline o.ä. - Auswahl aus state bleibt */ }
  })();
  const hostRow = body.querySelector("#ec-host-row");

  // Ordner-Auswahl passend zur aktuell gewählten Location neu aufbauen.
  function refreshFolders() {
    folderSel.innerHTML = `<option value="">${tr("ac_none")}</option>` +
      folderOptionsFor(locationSel.value, null);
  }

  // Host-Auswahl nur zeigen, wenn VM oder LXC gewählt ist
  devTypeSel.addEventListener("change", () => {
    const isVirtual = devTypeSel.value === "vm" || devTypeSel.value === "lxc";
    hostRow.style.display = isVirtual ? "" : "none";
  });

  // Wenn der Tenant gewechselt wird, die Standort-Auswahl passend neu befüllen
  // (und danach die Ordner-Auswahl zurücksetzen, da sie an der Location hängt).
  tenantSel.addEventListener("change", () => {
    locationSel.innerHTML = `<option value="">${tr("ac_none")}</option>` +
      state.hierarchy.locations
        .filter((l) => l.tenant_id === tenantSel.value)
        .map((l) => `<option value="${l.id}">${esc(l.name)}</option>`)
        .join("");
    refreshFolders();
  });

  // Standortwechsel -> Ordner-Auswahl neu aufbauen.
  locationSel.addEventListener("change", refreshFolders);

  body.querySelector("#ec-save").addEventListener("click", async () => {
    try {
      await api.updateClient(client.id, {
        hostname: body.querySelector("#ec-name").value,
        tenant_id: tenantSel.value || null,
        location_id: locationSel.value || null,
        folder_id: folderSel.value || null,
        color: body.querySelector("#ec-color").value,
        status_override: body.querySelector("#ec-status").value || null,
        active: body.querySelector("#ec-active").checked,
        essential: body.querySelector("#ec-essential").checked ? 1 : 0,
        short_name: body.querySelector("#ec-shortname").value.trim(),
        tags: body.querySelector("#ec-tags").value.split(",").map((t) => t.trim()).filter(Boolean).join(","),
        device_type: devTypeSel.value,
        auto_update: body.querySelector("#ec-autoupdate").value,
        icon_type: body.querySelector("#ec-icon").value,
        // Host nur setzen, wenn VM/LXC — sonst kein Parent
        parent_client_id: (devTypeSel.value === "vm" || devTypeSel.value === "lxc")
          ? (body.querySelector("#ec-host").value || null)
          : null,
      });
      closeWindow(win.key);
      if (onChanged) await onChanged();
    } catch (e) {
      const err = body.querySelector("#ec-error");
      err.textContent = e.message;
      err.classList.remove("hidden");
    }
  });

  body.querySelector("#ec-delete").addEventListener("click", async () => {
    if (!(await uiConfirm(tr("ec_delete_q", { name: client.hostname }), { okText: tr("u_client_loschen"), danger: true }))) return;
    await api.deleteClient(client.id);
    state.selection = null;
    closeWindow(win.key);
    if (onChanged) await onChanged();
  });
}
