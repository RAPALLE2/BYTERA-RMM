// usbguard.js (Modul "usbguard") - Vertrauensliste verwalten
// ----------------------------------------------------------
// * Vertrauenswürdige Geräte-IDs (VID:PID, optional Seriennummer) - z.B.
//   YubiKeys oder andere Schlüssel-Geräte ohne Dateisystem.
// * Signierte Schlüsseldateien für Speichersticks: gebunden an VID/PID/
//   Seriennummer EINES Sticks, einzeln widerrufbar. Die heruntergeladene
//   Datei ".bytera-usbkey" gehört ins Stammverzeichnis des Sticks.

import { moduleRequest } from "/js/app/modules.js";
import { getToken } from "/js/app/api.js";
import { esc, uiConfirm } from "/js/app/utils.js";

/** Schlüsseldatei erzeugen und herunterladen (auch aus init.js genutzt). */
export async function downloadKeyFile({ label, vid, pid, serial }) {
  const res = await fetch("/api/usbguard/trust/keys", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${getToken()}` },
    body: JSON.stringify({ label, vid, pid, serial }),
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({})))?.detail || `HTTP ${res.status}`);
  const blob = await res.blob();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = ".bytera-usbkey";
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 30000);
}

export function renderUsbguard(body) {
  body.innerHTML = `
    <div style="display:flex;flex-direction:column;height:100%;background:var(--panel);overflow:auto;padding:14px 18px;gap:14px">
      <div style="font-size:12px;color:var(--subtext);line-height:1.5">
        Blockier-Modus, Timeout und automatische Entscheidung stellst du in den
        <b>Modul-Einstellungen</b> ein. Im <b>Wartungsmodus</b> eines Clients werden dort alle USB-Geräte
        zugelassen. Hier legst du fest, welche Geräte <b>immer</b> automatisch zugelassen werden.
      </div>

      <div class="panel" style="padding:12px 14px">
        <b>🔑 Vertrauenswürdige Geräte-IDs</b>
        <div style="font-size:12px;color:var(--subtext);margin:4px 0 8px">
          Für Geräte ohne Dateisystem (YubiKey: VID 1050). Ohne PID gilt der ganze Hersteller,
          mit Seriennummer nur genau dieses eine Gerät (empfohlen).</div>
        <div id="ug-ids"></div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px">
          <input id="ug-id-label" placeholder="Bezeichnung" style="flex:1;min-width:140px">
          <input id="ug-id-vid" placeholder="VID (z.B. 1050)" style="width:110px">
          <input id="ug-id-pid" placeholder="PID (optional)" style="width:110px">
          <input id="ug-id-serial" placeholder="Seriennr. (optional)" style="width:150px">
          <button class="btn-primary" id="ug-id-add">＋ Hinzufügen</button>
        </div>
      </div>

      <div class="panel" style="padding:12px 14px">
        <b>📄 Signierte Schlüsseldateien (Speichersticks)</b>
        <div style="font-size:12px;color:var(--subtext);margin:4px 0 8px">
          Erzeugt eine mit dem Backend-Schlüssel signierte Datei <code>.bytera-usbkey</code>, gebunden an
          VID/PID/Seriennummer des Sticks. Lege sie ins Stammverzeichnis des Sticks - kopiert auf einen anderen
          Stick ist sie wertlos. Tipp: Die Werte stehen im Freigabe-Dialog, wenn der Stick blockiert wird.</div>
        <div id="ug-keys"></div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px">
          <input id="ug-k-label" placeholder="Bezeichnung" style="flex:1;min-width:140px">
          <input id="ug-k-vid" placeholder="VID" style="width:90px">
          <input id="ug-k-pid" placeholder="PID" style="width:90px">
          <input id="ug-k-serial" placeholder="Seriennummer" style="width:170px">
          <button class="btn-primary" id="ug-k-add">⬇ Datei erzeugen</button>
        </div>
      </div>
      <div id="ug-err" class="form-error hidden"></div>
    </div>`;

  const $ = (s) => body.querySelector(s);
  const err = $("#ug-err");
  const showErr = (e) => { err.textContent = e.message || String(e); err.classList.remove("hidden"); };

  async function load() {
    err.classList.add("hidden");
    let d;
    try { d = await moduleRequest("/api/usbguard/trust"); } catch (e) { showErr(e); return; }
    $("#ug-ids").innerHTML = d.ids.length ? d.ids.map((i) => `
      <div style="display:flex;gap:8px;align-items:center;padding:4px 0;border-bottom:1px solid var(--border);font-size:13px">
        <span style="flex:1">${esc(i.label)}</span>
        <code>${esc(i.vid)}:${esc(i.pid || "*")}</code>
        <span style="font-size:11px;color:var(--subtext);min-width:90px">${esc(i.serial || "jede Seriennr.")}</span>
        <button class="action-btn btn-danger" data-rmid="${esc(i.id)}">✕</button>
      </div>`).join("") : `<div style="font-size:12px;color:var(--subtext)">Keine Einträge.</div>`;
    $("#ug-keys").innerHTML = d.keys.length ? d.keys.map((k) => `
      <div style="display:flex;gap:8px;align-items:center;padding:4px 0;border-bottom:1px solid var(--border);font-size:13px;${k.revoked ? "opacity:.5" : ""}">
        <span style="flex:1">${esc(k.label)}</span>
        <code>${esc(k.vid)}:${esc(k.pid)}</code>
        <span style="font-size:11px;color:var(--subtext)">${esc(k.serial)}</span>
        <span style="font-size:11px;color:var(--subtext)">${new Date(k.created).toLocaleDateString()}</span>
        ${k.revoked ? `<span style="font-size:11px;color:var(--danger)">widerrufen</span>`
          : `<button class="action-btn btn-danger" data-revoke="${esc(k.kid)}" title="Datei ungültig machen">Widerrufen</button>`}
      </div>`).join("") : `<div style="font-size:12px;color:var(--subtext)">Noch keine Schlüsseldateien.</div>`;

    body.querySelectorAll("[data-rmid]").forEach((b) => b.addEventListener("click", async () => {
      await moduleRequest(`/api/usbguard/trust/ids/${b.dataset.rmid}`, { method: "DELETE", noJob: true });
      load();
    }));
    body.querySelectorAll("[data-revoke]").forEach((b) => b.addEventListener("click", async () => {
      if (!(await uiConfirm("Schlüsseldatei widerrufen? Der Stick wird danach wieder blockiert.", { danger: true }))) return;
      await moduleRequest(`/api/usbguard/trust/keys/${b.dataset.revoke}/revoke`, { method: "POST", noJob: true });
      load();
    }));
  }

  $("#ug-id-add").addEventListener("click", async () => {
    try {
      await moduleRequest("/api/usbguard/trust/ids", {
        method: "POST", noJob: true,
        body: JSON.stringify({ label: $("#ug-id-label").value, vid: $("#ug-id-vid").value,
                               pid: $("#ug-id-pid").value, serial: $("#ug-id-serial").value }),
      });
      ["#ug-id-label", "#ug-id-vid", "#ug-id-pid", "#ug-id-serial"].forEach((s) => { $(s).value = ""; });
      load();
    } catch (e) { showErr(e); }
  });
  $("#ug-k-add").addEventListener("click", async () => {
    try {
      await downloadKeyFile({ label: $("#ug-k-label").value, vid: $("#ug-k-vid").value,
                              pid: $("#ug-k-pid").value, serial: $("#ug-k-serial").value });
      window.notify?.("Schlüsseldatei erzeugt - ins Stammverzeichnis des Sticks kopieren", "success");
      load();
    } catch (e) { showErr(e); }
  });
  load();
}
