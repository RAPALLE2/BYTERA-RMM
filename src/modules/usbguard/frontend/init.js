// init.js (module "usbguard") - runs globally in the dashboard.
// Shows notifications for USB events and the allow/block dialog
// for blocked devices (with a visible timeout countdown).

import { dashboardSocket } from "/js/app/socket.js";
import { moduleRequest } from "/js/app/modules.js";
import { esc } from "/js/app/utils.js";
import { icon } from "./icons.js";

const open = new Map();   // key client|dev -> dialog element

function _close(key) {
  const el = open.get(key);
  if (el) { clearInterval(el._iv); el.remove(); open.delete(key); }
}

function _ask(ev) {
  const key = `${ev.client_id}|${ev.dev}`;
  _close(key);
  const box = document.createElement("div");
  open.set(key, box);
  box.style.cssText = `position:fixed;top:18px;right:18px;z-index:12000;background:var(--panel);
    border:1px solid var(--warn,#fbbf24);border-radius:12px;padding:14px 16px;width:320px;
    box-shadow:0 10px 40px rgba(0,0,0,.6);font-size:13px;color:var(--text)`;
  let left = ev.timeout_s || 60;
  box.innerHTML = `
    <div style="font-weight:700;font-size:14px;display:flex;align-items:center;gap:7px">${icon("plug", 15)} USB device blocked</div>
    <div style="margin:4px 0"><b>${esc(ev.host)}</b>: ${esc(ev.name)}</div>
    <div style="font-size:11px;color:var(--subtext);word-break:break-all">${esc(ev.dev)}
      ${ev.vid ? `<br>ID <code>${esc(ev.vid)}:${esc(ev.pid)}</code>${ev.serial ? ` · serial <code>${esc(ev.serial)}</code>` : " · no serial"}` : ""}</div>
    <div style="font-size:11px;color:var(--subtext);margin:4px 0">
      After <span data-left>${left}</span> s automatically: <b>${ev.timeout_action === "allow" ? "allow" : "block"}</b></div>
    <div style="display:flex;gap:8px;margin-top:8px">
      <button class="btn-primary" data-allow style="flex:1">${icon("check", 14)} Allow</button>
      <button class="btn btn-danger" data-deny style="flex:1">${icon("ban", 14)} Block</button>
    </div>
    ${ev.vid ? `<div style="display:flex;gap:6px;margin-top:6px">
      <button class="btn btn-sm" data-trust style="flex:1" title="Always allow this device (incl. serial) from now on and allow it now">${icon("star", 13)} Always trust</button>
      ${ev.storage && ev.serial ? `<button class="btn btn-sm" data-keyfile style="flex:1" title="Generate a signed key file for this stick">${icon("file", 13)} Key file</button>` : ""}
    </div>` : ""}`;
  document.body.appendChild(box);
  box._iv = setInterval(() => {
    left -= 1;
    const el = box.querySelector("[data-left]");
    if (el) el.textContent = String(Math.max(0, left));
    if (left <= 0) _close(key);
  }, 1000);
  const send = async (allow) => {
    try {
      await moduleRequest("/api/usbguard/decide", {
        method: "POST", noJob: true,
        body: JSON.stringify({ client_id: ev.client_id, dev: ev.dev, allow }),
      });
    } catch (e) { window.notify?.(e.message, "error"); }
    _close(key);
  };
  box.querySelector("[data-allow]").addEventListener("click", () => send(true));
  box.querySelector("[data-deny]").addEventListener("click", () => send(false));
  box.querySelector("[data-trust]")?.addEventListener("click", async () => {
    try {
      await moduleRequest("/api/usbguard/trust/ids", {
        method: "POST", noJob: true,
        body: JSON.stringify({ label: ev.name, vid: ev.vid, pid: ev.pid, serial: ev.serial }) });
      window.notify?.("Device saved as trusted", "success");
    } catch (e) { window.notify?.(e.message, "error"); return; }
    send(true);
  });
  box.querySelector("[data-keyfile]")?.addEventListener("click", async () => {
    try {
      const { downloadKeyFile } = await import("./usbguard.js");
      await downloadKeyFile({ label: ev.name, vid: ev.vid, pid: ev.pid, serial: ev.serial });
      window.notify?.("Key file generated - allow the stick now, copy the file into its root directory", "success", 8000);
    } catch (e) { window.notify?.(e.message, "error"); }
  });
}

export function init() {
  dashboardSocket.on("usb:event", (ev) => {
    const why = ev.trusted === "id" ? " - trusted (ID)"
      : ev.trusted === "maintenance" ? " - maintenance mode"
      : (ev.trusted || "").startsWith("key:") ? ` - key file "${ev.trusted.slice(4)}"` : "";
    window.notify?.(`${ev.host}: ${ev.name} ${ev.action === "add" ? "plugged in" : "unplugged"}${why}`,
      ev.action === "add" ? "info" : "warning");
  });
  dashboardSocket.on("usb:ask", _ask);
  dashboardSocket.on("usb:decided", (d) => {
    _close(`${d.client_id}|${d.dev}`);
    window.notify?.(`${d.allow ? "allowed" : "blocked"} (${esc(d.by)})`, d.allow ? "success" : "warning");
  });
}
