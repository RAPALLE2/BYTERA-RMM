// usbguard.js (module "usbguard") - manage the trust list
// ----------------------------------------------------------
// * Trusted device IDs (VID:PID, optional serial) - e.g.
//   YubiKeys or other key devices without a file system.
// * Signed key files for storage sticks: bound to the VID/PID/
//   serial of ONE stick, individually revocable. The downloaded
//   file ".bytera-usbkey" goes into the stick's root directory.

import { moduleRequest } from "/js/app/modules.js";
import { getToken } from "/js/app/api.js";
import { esc, uiConfirm } from "/js/app/utils.js";
import { icon } from "./icons.js";

/** Generate a key file and download it (also used from init.js). */
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
        Block mode, timeout and the automatic decision are configured in the
        <b>module settings</b>. In a client's <b>maintenance mode</b> all USB devices are
        allowed. Here you define which devices are <b>always</b> allowed automatically.
      </div>

      <div class="panel" style="padding:12px 14px">
        <b>${icon("key", 15)} Trusted device IDs</b>
        <div style="font-size:12px;color:var(--subtext);margin:4px 0 8px">
          For devices without a file system (YubiKey: VID 1050). Without a PID the whole vendor applies,
          with a serial only this exact device (recommended).</div>
        <div id="ug-ids"></div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px">
          <input id="ug-id-label" placeholder="Label" style="flex:1;min-width:140px">
          <input id="ug-id-vid" placeholder="VID (e.g. 1050)" style="width:110px">
          <input id="ug-id-pid" placeholder="PID (optional)" style="width:110px">
          <input id="ug-id-serial" placeholder="Serial (optional)" style="width:150px">
          <button class="btn-primary" id="ug-id-add">+ Add</button>
        </div>
      </div>

      <div class="panel" style="padding:12px 14px">
        <b>${icon("file", 15)} Signed key files (storage sticks)</b>
        <div style="font-size:12px;color:var(--subtext);margin:4px 0 8px">
          Generates a file <code>.bytera-usbkey</code> signed with the backend key, bound to the
          stick's VID/PID/serial. Put it into the stick's root directory - copied to another
          stick it is worthless. Tip: the values are shown in the approval dialog when the stick is blocked.</div>
        <div id="ug-keys"></div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px">
          <input id="ug-k-label" placeholder="Label" style="flex:1;min-width:140px">
          <input id="ug-k-vid" placeholder="VID" style="width:90px">
          <input id="ug-k-pid" placeholder="PID" style="width:90px">
          <input id="ug-k-serial" placeholder="Serial" style="width:170px">
          <button class="btn-primary" id="ug-k-add">${icon("download", 14)} Generate file</button>
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
        <span style="font-size:11px;color:var(--subtext);min-width:90px">${esc(i.serial || "any serial")}</span>
        <button class="action-btn btn-danger" data-rmid="${esc(i.id)}">${icon("close", 13)}</button>
      </div>`).join("") : `<div style="font-size:12px;color:var(--subtext)">No entries.</div>`;
    $("#ug-keys").innerHTML = d.keys.length ? d.keys.map((k) => `
      <div style="display:flex;gap:8px;align-items:center;padding:4px 0;border-bottom:1px solid var(--border);font-size:13px;${k.revoked ? "opacity:.5" : ""}">
        <span style="flex:1">${esc(k.label)}</span>
        <code>${esc(k.vid)}:${esc(k.pid)}</code>
        <span style="font-size:11px;color:var(--subtext)">${esc(k.serial)}</span>
        <span style="font-size:11px;color:var(--subtext)">${new Date(k.created).toLocaleDateString()}</span>
        ${k.revoked ? `<span style="font-size:11px;color:var(--danger)">revoked</span>`
          : `<button class="action-btn btn-danger" data-revoke="${esc(k.kid)}" title="Invalidate the file">Revoke</button>`}
      </div>`).join("") : `<div style="font-size:12px;color:var(--subtext)">No key files yet.</div>`;

    body.querySelectorAll("[data-rmid]").forEach((b) => b.addEventListener("click", async () => {
      await moduleRequest(`/api/usbguard/trust/ids/${b.dataset.rmid}`, { method: "DELETE", noJob: true });
      load();
    }));
    body.querySelectorAll("[data-revoke]").forEach((b) => b.addEventListener("click", async () => {
      if (!(await uiConfirm("Revoke the key file? The stick will be blocked again afterwards.", { danger: true }))) return;
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
      window.notify?.("Key file generated - copy it into the stick's root directory", "success");
      load();
    } catch (e) { showErr(e); }
  });
  load();
}
