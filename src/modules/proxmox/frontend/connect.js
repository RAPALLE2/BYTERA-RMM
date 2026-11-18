// connect.js (module "proxmox") - connect Proxmox to an agent
// ------------------------------------------------------------------
// Dialog: address (local / custom / RMM relay), port, login (API token or
// user + password), certificate (fingerprint / verification), test, remove.
// The help icon opens the guide on what to set up in Proxmox.

import { esc, uiConfirm } from "/js/app/utils.js";
import { preq } from "./common.js";
import { icon } from "./icons.js";

export const HELP_HTML = `
  <h4 style="margin:0 0 6px">How to set up Proxmox for the RMM</h4>
  <p style="margin:0 0 8px;color:var(--subtext)">Everything in the Proxmox web UI under <b>Datacenter &rarr; Permissions</b>.</p>
  <ol style="margin:0 0 10px 18px;padding:0;line-height:1.55">
    <li><b>Create a user:</b> Users &rarr; Add &rarr; name <code>rmm</code>, realm
        <i>Proxmox VE authentication server</i> &rarr; gives <code>rmm@pve</code>.</li>
    <li><b>Grant permissions:</b> Permissions &rarr; Add &rarr; <i>User permission</i>: path <code>/</code>,
        user <code>rmm@pve</code>, role <b>PVEAdmin</b> (control everything) or <b>PVEAuditor</b> (view only),
        "Propagate" checked.</li>
    <li><b>Choose a login:</b>
      <ul style="margin:4px 0 0 16px;padding:0">
        <li><b>API token (recommended):</b> API Tokens &rarr; Add &rarr; user <code>rmm@pve</code>, token ID
            <code>rmm</code>, <b>turn off "Privilege separation"</b> (otherwise the token has no permissions). The
            <b>secret</b> is shown only once - copy it. Enter here: user <code>rmm@pve</code>,
            token ID <code>rmm</code>, secret.</li>
        <li><b>User + password:</b> set a password on the user (Users &rarr; Password). Required for the
            <b>console directly in the dashboard</b> - Proxmox only issues console tickets for user logins.</li>
      </ul></li>
  </ol>
  <h4 style="margin:0 0 6px">Address</h4>
  <ul style="margin:0 0 10px 18px;padding:0;line-height:1.55">
    <li><b>Local address</b> - the agent's IP (agent runs on the Proxmox host); the RMM server must be able to reach it.</li>
    <li><b>Custom address</b> - e.g. the cluster IP or a DNS name, reachable from the RMM server (also over VPN).</li>
    <li><b>RMM relay</b> - the connection runs <b>through the agent</b>; Proxmox does not have to be reachable from the
        server. Target from the agent's point of view: <code>127.0.0.1</code> if the agent runs on the Proxmox host.
        Requires the "Network" module with port forwarding.</li>
  </ul>
  <p style="margin:0 0 4px">The Proxmox port is <code>8006</code>.</p>
  <h4 style="margin:0 0 6px">Certificate</h4>
  <p style="margin:0;line-height:1.55">Proxmox uses a self-signed certificate out of the box. Save the connection and
    click <b>"Fetch fingerprint"</b> - the RMM then trusts exactly this certificate (secure). You can compare the
    fingerprint in Proxmox under <i>Node &rarr; System &rarr; Certificates</i>. Only enable "Verify certificate" with a valid
    certificate (e.g. Let's Encrypt).</p>`;

export function showHelp() {
  const ov = document.createElement("div");
  ov.style.cssText = "position:fixed;inset:0;z-index:9600;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;padding:16px";
  ov.innerHTML = `<div style="background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:18px;width:640px;
      max-width:100%;max-height:90vh;overflow:auto;font-size:13px">${HELP_HTML}
    <div style="text-align:right;margin-top:12px"><button class="btn-primary" data-x>Got it</button></div></div>`;
  ov.addEventListener("click", (e) => { if (e.target === ov || e.target.dataset.x !== undefined) ov.remove(); });
  document.body.appendChild(ov);
}

/** Connection dialog for an agent. onDone(connected:boolean) */
export async function openConnectionDialog(client, onDone) {
  let info;
  try { info = await preq(`/${encodeURIComponent(client.id)}/connection`); }
  catch (e) { window.notify?.(e.message, "error"); return; }
  const c = info.connection || { addr_mode: "local", port: 8006, auth: "token", relay_host: "127.0.0.1", verify_tls: false };
  const ov = document.createElement("div");
  ov.style.cssText = "position:fixed;inset:0;z-index:9500;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;padding:16px";
  ov.innerHTML = `<style>.pxchk{display:flex;align-items:center;gap:8px;margin:5px 0;flex-wrap:wrap;cursor:pointer}
      .pxchk input[type=radio],.pxchk input[type=checkbox]{appearance:auto;-webkit-appearance:auto;width:16px;height:16px;min-height:0;
        margin:0;padding:0;flex:0 0 auto;accent-color:var(--accent)}
      .pxchk input[type=text],.pxchk input:not([type]){height:var(--ui-h-sm,26px)}</style>
    <div style="background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:18px;width:560px;max-width:100%;
                max-height:92vh;overflow:auto;font-size:13px">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
        <b style="font-size:15px;flex:1;display:flex;align-items:center;gap:6px">${icon("cluster", 16)} Proxmox – ${esc(info.hostname || client.hostname || "")}</b>
        <button class="btn-tool" data-help title="Guide: what do I set up in Proxmox?">${icon("help", 15)}</button>
        <button class="btn-tool" data-close title="Close">${icon("close", 15)}</button>
      </div>
      <div class="form-row"><label>Label (optional)</label><input data-f="label" value="${esc(c.label || "")}" placeholder="e.g. Office cluster"></div>
      <label style="font-weight:600;display:block;margin:6px 0 4px">Address</label>
      <label class="pxchk"><input type="radio" name="pxa" value="local"> Local address of the agent: <b>${esc(info.agent_ip || "unknown")}</b></label>
      <label class="pxchk"><input type="radio" name="pxa" value="custom"> Custom address:
        <input data-f="host" value="${esc(c.host || "")}" placeholder="pve.firma.lan / 10.0.0.5" style="flex:1;margin-left:6px"></label>
      <label class="pxchk"><input type="radio" name="pxa" value="relay"> RMM relay (through the agent), target at the agent:
        <input data-f="relay_host" value="${esc(c.relay_host || "127.0.0.1")}" style="width:130px;margin-left:6px"></label>
      <div class="form-row" style="margin-top:6px"><label>Port</label><input data-f="port" type="number" min="1" max="65535" value="${esc(String(c.port || 8006))}" style="width:110px"></div>
      <label style="font-weight:600;display:block;margin:6px 0 4px">Login</label>
      <label class="pxchk"><input type="radio" name="pxu" value="token"> API token (recommended)</label>
      <label class="pxchk"><input type="radio" name="pxu" value="password"> User + password (needed for the console in the dashboard)</label>
      <div class="form-row"><label>User (name@realm)</label><input data-f="user" value="${esc(c.user || "")}" placeholder="rmm@pve"></div>
      <div data-auth="token">
        <div class="form-row"><label>Token-ID</label><input data-f="token_id" value="${esc(c.token_id || "")}" placeholder="rmm"></div>
        <div class="form-row"><label>Secret ${c.has_secret ? "(stored - leave empty to keep)" : ""}</label><input data-f="secret" type="password" autocomplete="off"></div>
      </div>
      <div data-auth="password">
        <div class="form-row"><label>Password ${c.has_password ? "(stored - leave empty to keep)" : ""}</label><input data-f="password" type="password" autocomplete="off"></div>
      </div>
      <label style="font-weight:600;display:block;margin:6px 0 4px">Certificate</label>
      <div class="form-row"><label>SHA-256 fingerprint (self-signed certificate)</label>
        <input data-f="fingerprint" value="${esc(c.fingerprint || "")}" placeholder="AB:CD:…" style="font-family:ui-monospace,monospace;font-size:11.5px"></div>
      <div style="margin:-2px 0 6px"><button class="btn btn-sm" data-fp ${info.connection ? "" : "disabled"}
        title="${info.connection ? "Fetch the certificate over the configured address" : "Save first"}">${icon("lock", 14)} Fetch fingerprint</button></div>
      <label class="pxchk"><input type="checkbox" data-f="verify_tls" ${c.verify_tls ? "checked" : ""}> Verify certificate (only with a valid certificate)</label>
      <div data-status style="min-height:18px;margin:10px 0;color:var(--subtext)"></div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn-primary" data-save>Save &amp; test</button>
        ${info.connection ? `<button class="btn btn-danger" data-del>Remove connection</button>` : ""}
      </div>
    </div>`;
  document.body.appendChild(ov);
  const $ = (s) => ov.querySelector(s);
  const f = (k) => ov.querySelector(`[data-f="${k}"]`);
  const status = (t, err) => { $("[data-status]").innerHTML = t; $("[data-status]").style.color = err ? "var(--danger)" : "var(--subtext)"; };
  ov.querySelector(`input[name="pxa"][value="${c.addr_mode || "local"}"]`).checked = true;
  ov.querySelector(`input[name="pxu"][value="${c.auth || "token"}"]`).checked = true;
  const syncAuth = () => {
    const a = ov.querySelector('input[name="pxu"]:checked').value;
    ov.querySelectorAll("[data-auth]").forEach((el) => { el.style.display = el.dataset.auth === a ? "" : "none"; });
  };
  ov.querySelectorAll('input[name="pxu"]').forEach((r) => r.addEventListener("change", syncAuth));
  syncAuth();
  f("host").addEventListener("focus", () => { ov.querySelector('input[name="pxa"][value="custom"]').checked = true; });
  f("relay_host").addEventListener("focus", () => { ov.querySelector('input[name="pxa"][value="relay"]').checked = true; });
  let changed = false;
  const close = () => { ov.remove(); onDone?.(changed); };
  $("[data-close]").addEventListener("click", close);
  $("[data-help]").addEventListener("click", showHelp);

  const body = () => ({
    addr_mode: ov.querySelector('input[name="pxa"]:checked').value, host: f("host").value.trim(),
    relay_host: f("relay_host").value.trim() || "127.0.0.1", port: parseInt(f("port").value, 10) || 8006,
    auth: ov.querySelector('input[name="pxu"]:checked').value, user: f("user").value.trim(),
    token_id: f("token_id").value.trim(), secret: f("secret").value, password: f("password").value,
    verify_tls: f("verify_tls").checked, fingerprint: f("fingerprint").value.trim(), label: f("label").value.trim(),
  });
  const cid = encodeURIComponent(client.id);
  $("[data-save]").addEventListener("click", async () => {
    status("Saving …");
    try {
      await preq(`/${cid}/connection`, { method: "PUT", body: body() });
      changed = true;
      $("[data-fp]").disabled = false;
      status("Saved - testing connection …");
      const t = await preq(`/${cid}/test`, { method: "POST" });
      status(`${icon("check", 14)} Connected: Proxmox VE ${esc(t.version || "?")} · nodes: ${esc((t.nodes || []).join(", "))} · ${esc(t.address)}`);
    } catch (e) {
      const hint = e.status === 495 ? " - tip: click \"Fetch fingerprint\"." : "";
      status(`${icon("cross", 14)} ${esc(e.message)}${hint}`, true);
    }
  });
  $("[data-fp]").addEventListener("click", async () => {
    status("Fetching fingerprint …");
    try {
      const r = await preq(`/${cid}/fingerprint`, { method: "POST" });
      f("fingerprint").value = r.fingerprint;
      status("Fingerprint applied - now \"Save & test\". (Compare in Proxmox: Node \u2192 System \u2192 Certificates)");
    } catch (e) { status(`${icon("cross", 14)} ${esc(e.message)}`, true); }
  });
  $("[data-del]")?.addEventListener("click", async () => {
    if (!(await uiConfirm("Remove this agent\u2019s Proxmox connection?", { description: "Credentials and guest shares are deleted. Nothing changes in Proxmox itself.", okText: "Remove", danger: true }))) return;
    try { await preq(`/${cid}/connection`, { method: "DELETE" }); changed = true; close(); }
    catch (e) { status(`${icon("cross", 14)} ${esc(e.message)}`, true); }
  });
}
