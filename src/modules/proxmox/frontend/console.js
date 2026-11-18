// console.js (module "proxmox") - console of VMs/LXC
// --------------------------------------------------
// * Buttons in the guest view (event "rmm-pve-guest" from dashboard.js):
//     Console  -> own window with noVNC, tunneled through the RMM
//     Web UI   -> Proxmox web UI in the browser (direct / via forwarding)
// * renderConsole: window app "proxmox-console" (not in the start menu)
// Backend: console_routes.py

import { getToken } from "/js/app/api.js";
import { esc, uiPrompt } from "/js/app/utils.js";
import { openWindow } from "/js/app/windowmanager.js";
import { preq } from "./common.js";
import { icon } from "./icons.js";

// ------------------------------------------------------------------ Buttons in the guest view
window.addEventListener("rmm-pve-guest", (ev) => {
  const { slot, cid, guest } = ev.detail || {};
  if (!slot) return;
  slot.innerHTML = `<span style="display:inline-flex;gap:6px;position:relative">
      <button class="btn btn-sm btn-primary" data-con>${icon("server", 14)} Console</button>
      <button class="btn btn-sm" data-web>${icon("globe", 14)} Web UI ▾</button>
      <div data-menu style="display:none;position:absolute;right:0;top:100%;margin-top:4px;z-index:50;background:var(--panel);
           border:1px solid var(--border);border-radius:8px;padding:4px;min-width:250px;box-shadow:0 8px 24px rgba(0,0,0,.4)">
        <button class="btn btn-ghost btn-sm btn-block" data-via="direct" style="justify-content:flex-start">Open directly (Proxmox on the same network)</button>
        <button class="btn btn-ghost btn-sm btn-block" data-via="forward" style="justify-content:flex-start">Via RMM forwarding (my IP only)</button>
      </div></span>`;
  slot.querySelector("[data-con]").addEventListener("click", () => openConsole(cid, guest));
  const menu = slot.querySelector("[data-menu]");
  slot.querySelector("[data-web]").addEventListener("click", (e) => { e.stopPropagation(); menu.style.display = menu.style.display === "none" ? "" : "none"; });
  document.addEventListener("click", () => { menu.style.display = "none"; }, { once: true });
  slot.querySelectorAll("[data-via]").forEach((b) => b.addEventListener("click", async () => {
    menu.style.display = "none";
    // open the window immediately (popup blocker), set the address afterwards
    const w = window.open("about:blank", "_blank");
    try {
      const r = await preq(`/${encodeURIComponent(cid)}/guests/${encodeURIComponent(guest.node)}/${guest.type}/${guest.vmid}/webui`,
                           { method: "POST", body: { via: b.dataset.via } });
      if (w) w.location.href = r.url; else window.open(r.url, "_blank");
      if (r.note) window.notify?.(r.note, "info", 9000);
    } catch (e) { w?.close(); window.notify?.(e.message, "error", 9000); }
  }));
});

export function openConsole(cid, guest) {
  openWindow({ key: `pve-con-${cid}-${guest.vmid}`, appId: "proxmox-console",
               title: `Console — ${guest.name || guest.vmid} (${guest.type === "qemu" ? "VM" : "LXC"} ${guest.vmid})`,
               props: { clientId: cid, node: guest.node, type: guest.type, vmid: guest.vmid }, w: 1024, h: 700 });
}

// ------------------------------------------------------------------ Console window
export function renderConsole(body, win) {
  const p = win?.props || {};
  body.innerHTML = `
    <div style="height:100%;display:flex;flex-direction:column;background:#000;min-height:0">
      <div style="display:flex;gap:6px;align-items:center;padding:6px 8px;background:var(--panel);border-bottom:1px solid var(--border);flex-wrap:wrap">
        <span data-st style="flex:1;font-size:12px;color:var(--subtext)">Connecting …</span>
        <button class="btn btn-sm" data-cad title="Send Ctrl+Alt+Del">Ctrl+Alt+Del</button>
        <button class="btn btn-sm" data-paste title="Send text as keystrokes">Insert text</button>
        <button class="btn btn-sm active" data-scale title="Fit to window">Scale</button>
        <button class="btn btn-sm" data-full title="Fullscreen">${icon("fullscreen", 15)}</button>
        <button class="btn btn-sm" data-re title="Reconnect">${icon("refresh", 15)}</button>
      </div>
      <div data-screen style="flex:1;min-height:0;overflow:hidden"></div>
    </div>`;
  const st = body.querySelector("[data-st]");
  const screen = body.querySelector("[data-screen]");
  let rfb = null;
  let scale = true;
  const status = (t, bad) => { st.textContent = t; st.style.color = bad ? "var(--danger)" : "var(--subtext)"; };

  async function connect() {
    if (rfb) { try { rfb.disconnect(); } catch { /* ignore */ } rfb = null; }
    screen.innerHTML = "";
    status("Requesting console …");
    let s;
    try {
      s = await preq(`/${encodeURIComponent(p.clientId)}/guests/${encodeURIComponent(p.node)}/${p.type}/${p.vmid}/console`, { method: "POST" });
    } catch (e) { status(`Error: ${e.message}`, true); return; }
    let RFB;
    try { RFB = (await import("./novnc/core/rfb.js")).default; }
    catch (e) { status(`Error: noVNC could not be loaded: ${e.message}`, true); return; }
    const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${s.ws}`;
    status("Connecting …");
    // dashboard session as a WebSocket subprotocol (not in the address -> not in the server log)
    rfb = new RFB(screen, url, { credentials: { password: s.password }, wsProtocols: ["binary", `rmmtoken.${getToken() || ""}`] });
    rfb.scaleViewport = scale;
    rfb.resizeSession = false;
    rfb.background = "#000";
    rfb.addEventListener("connect", () => status(`Connected · ${p.type === "qemu" ? "VM" : "LXC"} ${p.vmid} on ${p.node}`));
    rfb.addEventListener("disconnect", (e) => status(e.detail?.clean ? "Disconnected" : "Connection lost - use reconnect", !e.detail?.clean));
    rfb.addEventListener("securityfailure", (e) => status(`Console authentication failed: ${esc(e.detail?.reason || "")}`, true));
    rfb.addEventListener("desktopname", (e) => { if (e.detail?.name) status(`Connected · ${e.detail.name}`); });
  }

  body.querySelector("[data-cad]").addEventListener("click", () => rfb?.sendCtrlAltDel());
  body.querySelector("[data-scale]").addEventListener("click", (e) => {
    scale = !scale; e.currentTarget.classList.toggle("active", scale); if (rfb) rfb.scaleViewport = scale;
  });
  body.querySelector("[data-full]").addEventListener("click", () => screen.requestFullscreen?.());
  body.querySelector("[data-re]").addEventListener("click", connect);
  body.querySelector("[data-paste]").addEventListener("click", async () => {
    const text = await uiPrompt("Send text", { description: "Text that is typed into the console as keystrokes" });
    if (!text || !rfb) return;
    for (const ch of text) {
      const code = ch === "\n" ? 0xff0d : ch.codePointAt(0) < 256 ? ch.codePointAt(0) : 0x01000000 + ch.codePointAt(0);
      rfb.sendKey(code, null, true); rfb.sendKey(code, null, false);
    }
  });
  connect();
  return () => { try { rfb?.disconnect(); } catch { /* ignore */ } };
}
