// app.js (module "cloudflare") - manage the Cloudflare Tunnel
// ---------------------------------------------------------
// Tabs: Overview | Connection | Routes | Nodes | Help
// Backend: cf_routes.py (/api/cloudflare), login for Access: oidc_routes.py (/cf-oidc/...)

import { getToken } from "/js/app/api.js";
import { withBackground, resolveJob } from "/js/app/bgjobs.js";
import { state } from "/js/app/state.js";
import { esc, uiConfirm } from "/js/app/utils.js";
import { initSubjectPicker, readSubjectPicker, subjectPickerHtml } from "/js/app/subjectpicker.js";
import { icon, dot } from "./icons.js";

async function cq(path, opts = {}) {
  const token = getToken() || "";
  const headers = { ...(token ? { Authorization: "Bearer " + token } : {}) };
  if (opts.body !== undefined && typeof opts.body !== "string") opts = { ...opts, body: JSON.stringify(opts.body) };
  if (opts.body) headers["Content-Type"] = "application/json";
  const res = await fetch(withBackground("/api/cloudflare" + path, opts.method), { ...opts, headers });   // backend job, see bgjobs.js
  if (!res.ok) {
    let msg = res.statusText;
    try { const j = await res.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch { /* ignore */ }
    throw new Error(msg);
  }
  return resolveJob(await res.json());
}

const KIND = {
  dashboard: "RMM dashboard", webdav: "WebDAV", sftp: "SFTP (relay)", ftp: "FTP (relay)",
  console_ssh: "RMM console (SSH)", website: "Website / web host", docker: "Docker container",
  agent: "Agent: RDP / SSH / VNC", custom: "Custom address",
};
const MODE = {
  http: "In the browser (https://…)", browser: "In the browser via Cloudflare Access",
  client: "With cloudflared on the PC", warp: "Private network (WARP client)",
};
const ACCESS = { none: "Open (without Cloudflare login)", rmm: "RMM login (local / AD, with 2FA)", otp: "One-time PIN by email" };
const ACC_SHORT = { none: "open", rmm: "RMM login", otp: "email PIN", service: "service token" };
const lifeText = (rm) => !rm ? "" : rm.lifetime === 0 ? "permanent" : rm.lifetime === "session" ? `until end of use${rm.leases && Object.keys(rm.leases).length ? " · active" : ""}`
  : `until ${new Date((rm.expires || 0) * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
const clientName = (id) => state.clients?.find((c) => c.id === id)?.hostname || id?.slice(0, 8) || "?";
const box = (html) => `<div style="border:1px solid var(--border);border-radius:10px;padding:12px 14px;margin-bottom:12px">${html}</div>`;
const fmtTime = (s) => (s ? new Date(s * 1000).toLocaleString() : "–");

export function renderCloudflare(body) {
  body.innerHTML = `
    <div style="display:flex;flex-direction:column;height:100%;min-height:0;font-size:13px">
      <div class="tab-bar" style="padding:8px 12px 0;flex:0 0 auto" data-tabs>
        ${["overview:Overview", "connect:Connection", "routes:Routes", "nodes:Nodes", "help:Help"].map((t, i) => {
          const [k, l] = t.split(":");
          return `<button class="tab-btn ${i ? "" : "active"}" data-tab="${k}">${l}</button>`;
        }).join("")}
      </div>
      <div data-pane style="flex:1;overflow:auto;padding:12px 14px;min-height:0"></div>
    </div>`;
  const pane = body.querySelector("[data-pane]");
  let tab = "overview";
  let S = null;
  let timer = null;
  const load = async () => { S = await cq("/state"); return S; };
  const draw = async (reload = true) => {
    if (!body.isConnected) { clearInterval(timer); return; }
    try { if (reload || !S) await load(); } catch (e) { pane.innerHTML = `<div style="color:var(--danger)">${esc(e.message)}</div>`; return; }
    ({ overview: drawOverview, connect: drawConnect, routes: drawRoutes, nodes: drawNodes, help: drawHelp })[tab](pane, S, draw, (t) => select(t));
  };
  const select = (t) => {
    tab = t;
    body.querySelectorAll("[data-tab]").forEach((b) => b.classList.toggle("active", b.dataset.tab === t));
    draw(false);
  };
  body.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => select(b.dataset.tab)));
  draw();
  timer = setInterval(() => { if (tab === "overview" && body.isConnected) draw(); else if (!body.isConnected) clearInterval(timer); }, 5000);
}

// ------------------------------------------------------------------ overview
function drawOverview(pane, S, redraw, select) {
  const c = S.connector || {};
  const ready = S.auth.configured && S.account.id;
  const t = S.tunnels.server || {};
  const st = !t.id ? [dot("var(--subtext,#9ca3af)", 13), "No tunnel yet"] : c.running && c.connected ? [dot("var(--online,#16a34a)", 13), "Connected"]
    : c.running ? [dot("var(--warning,#f59e0b)", 13), "Connecting …"] : c.download ? [dot("var(--warning,#f59e0b)", 13), c.download] : [dot("var(--danger,#e5484d)", 13), "Disconnected"];
  pane.innerHTML = `
    ${!ready ? box(`<b>Getting started</b><ol style="margin:6px 0 0 18px;padding:0;line-height:1.7">
        <li>Under <a href="#" data-go="connect" style="color:var(--accent)">Connection</a>, enter the API token and choose the account.</li>
        <li>Under <a href="#" data-go="routes" style="color:var(--accent)">Routes</a>, define what should be reachable – the tunnel is created automatically.</li></ol>`) : ""}
    ${box(`<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <span style="font-size:15px">${st[0]} <b>${esc(st[1])}</b></span>
        <span style="color:var(--subtext)">${t.name ? `Tunnel <code>${esc(t.name)}</code>` : ""} · cloudflared ${esc(c.version || "")}${c.installed ? "" : " (loading)"}</span>
        <span style="flex:1"></span>
        <button class="btn btn-sm" data-sync ${ready ? "" : "disabled"}>⟳ Sync now</button>
        ${c.running ? `<button class="btn btn-sm" data-conn="restart">Reconnect</button><button class="btn btn-sm btn-danger" data-conn="stop">Disconnect</button>`
          : `<button class="btn btn-sm" data-conn="start" ${t.id ? "" : "disabled"}>Connect</button>`}
      </div>
      <div style="color:var(--subtext);font-size:12px;margin-top:6px">Account: <b>${esc(S.account.name || "–")}</b> · Team: ${esc(S.team_domain || "–")}
        · Last sync: ${fmtTime(S.last_sync)} · ${S.routes.length} route(s)${c.restarts ? ` · restarts: ${c.restarts}` : ""}</div>
      ${S.last_error ? `<div style="color:var(--danger);font-size:12px;margin-top:6px">${icon("warn", 13)} ${esc(S.last_error)}</div>` : ""}`)}
    ${S.routes.length ? box(`<b>Routes</b>${routeTable(S.routes, false)}`) : ""}
    ${box(`<b>Connector log</b>
      <pre style="background:var(--panel-2);border:1px solid var(--border);border-radius:8px;padding:8px;max-height:220px;overflow:auto;font-size:11.5px;margin:6px 0 0">${esc((c.log || []).join("\n") || "–")}</pre>`)}`;
  pane.querySelectorAll("[data-go]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); select(a.dataset.go); }));
  pane.querySelector("[data-sync]")?.addEventListener("click", (e) => doSync(e.currentTarget, redraw));
  pane.querySelectorAll("[data-conn]").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true;
    try { await cq(`/connector/${b.dataset.conn}`, { method: "POST" }); } catch (e) { window.notify?.(e.message, "error"); }
    setTimeout(redraw, 800);
  }));
}

async function doSync(btn, redraw) {
  btn.disabled = true;
  btn.textContent = "Syncing …";
  try {
    const r = await cq("/sync", { method: "POST" });
    window.notify?.(r.errors?.length ? `Synced with errors: ${r.errors.join("; ")}` : "Synced with Cloudflare",
                    r.errors?.length ? "warning" : "success", 7000);
  } catch (e) { window.notify?.(e.message, "error", 8000); }
  redraw();
}

function routeTable(routes, actions) {
  return `<table class="data-table" style="width:100%;margin-top:6px">
    <thead><tr><th style="text-align:left">Name</th><th style="text-align:left">Target</th><th style="text-align:left">Reachable via</th>
      <th>Protection</th><th>Status</th>${actions ? "<th></th>" : ""}</tr></thead>
    <tbody>${routes.map((r) => `<tr>
      <td><b>${esc(r.name)}</b><div style="font-size:11px;color:var(--subtext)">${esc(KIND[r.kind] || r.kind)}${r.kind === "agent"
        ? ` · ${esc(clientName(r.ref?.client_id))} ${esc((r.ref?.proto || "").toUpperCase())}${r.endpoint === "node" ? ` · node ${esc(clientName(r.node_id))}` : " · via RMM server"}` : ""}</div></td>
      <td style="font-size:11.5px;color:var(--subtext)"><code>${esc(r.state?.service || "–")}</code></td>
      <td style="font-size:12px">${r.mode === "warp" ? `WARP: <code>${esc(r.state?.warp_address || "…")}</code>`
        : r.url ? `<a href="${esc(r.url)}" target="_blank" rel="noopener" style="color:var(--accent)">${esc(r.url.replace("https://", ""))}</a>` : "–"}
        <div style="font-size:11px;color:var(--subtext)">${esc(MODE[r.mode] || r.mode || "")}${r.remote ? ` · client app, ${esc(lifeText(r.remote))}` : ""}</div></td>
      <td style="text-align:center;font-size:12px">${r.mode === "warp" ? "Zero Trust" : esc(ACC_SHORT[r.access] || r.access)}</td>
      <td style="text-align:center" title="${esc(r.state?.error || "")}">${r.state?.error ? icon("warn", 14) : r.state?.synced ? icon("check", 14) : "…"}</td>
      ${actions ? `<td style="white-space:nowrap;text-align:right">
        ${r.mode === "client" || r.mode === "warp" ? `<button class="btn btn-sm" data-how="${esc(r.id)}">Connect</button>` : ""}
        ${r.remote ? "" : `<button class="btn-tool" data-edit="${esc(r.id)}" title="Edit">${icon("edit", 14)}</button>`}
        <button class="btn btn-sm btn-danger" data-del="${esc(r.id)}">Remove</button></td>` : ""}
    </tr>${r.state?.error ? `<tr><td colspan="${actions ? 6 : 5}" style="color:var(--danger);font-size:11.5px;padding-top:0">${icon("warn", 13)} ${esc(r.state.error)}</td></tr>` : ""}`).join("")}</tbody></table>`;
}

// ------------------------------------------------------------------ connection
function drawConnect(pane, S, redraw) {
  const a = S.auth || {};
  pane.innerHTML = `
    ${box(`<b>1. Cloudflare access</b>
      <p style="color:var(--subtext);font-size:12px;margin:4px 0 8px">Recommended: <b>API token</b> (Cloudflare → My Profile → API Tokens → Create Token) with the permissions from "Help".
        Alternatively the Global API Key with the account email. Stored encrypted.</p>
      <div style="display:flex;gap:14px;margin-bottom:8px">
        <label style="display:flex;gap:6px;align-items:center;cursor:pointer"><input type="radio" name="cfat" value="token" ${a.type !== "key" ? "checked" : ""} style="appearance:auto;width:15px;height:15px"> API-Token</label>
        <label style="display:flex;gap:6px;align-items:center;cursor:pointer"><input type="radio" name="cfat" value="key" ${a.type === "key" ? "checked" : ""} style="appearance:auto;width:15px;height:15px"> Global API Key</label>
      </div>
      <div data-tok><input type="password" data-token placeholder="${a.configured && a.type !== "key" ? "•••••••• (stored – leave empty to keep)" : "API token"}" style="width:100%;max-width:520px"></div>
      <div data-key style="display:none;gap:8px;flex-wrap:wrap">
        <input type="email" data-email placeholder="Email of the Cloudflare account" value="${esc(a.email || "")}" style="width:260px">
        <input type="password" data-keyv placeholder="${a.configured && a.type === "key" ? "•••••••• (stored)" : "Global API Key"}" style="width:260px">
      </div>
      <div style="margin-top:8px"><button class="btn-primary btn-sm" data-auth>Check &amp; save</button>
        <span data-authmsg style="margin-left:8px;font-size:12px;color:var(--subtext)">${a.configured ? icon("check", 13) + " configured" : ""}</span></div>`)}
    ${box(`<b>2. Account and public RMM address</b>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:8px">
        <select data-acc style="min-width:260px">${S.account.id ? `<option value="${esc(S.account.id)}">${esc(S.account.name)}</option>` : `<option value="">– check access first –</option>`}</select>
        <input data-pub placeholder="https://rmm.company.com" value="${esc(S.public_base || "")}" style="width:280px" title="Public address of the RMM (for the RMM login at Cloudflare Access)">
        <button class="btn-primary btn-sm" data-accsave>Apply</button>
        <button class="btn btn-sm" data-refresh ${S.account.id ? "" : "disabled"}>Reload zones</button>
      </div>
      <p style="color:var(--subtext);font-size:12px;margin:8px 0 0">You need the public RMM address for the "RMM login" protection: Cloudflare sends the sign-in there
        (<code>/cf-oidc/…</code>). The easiest way is to create an "RMM dashboard" route and enter its address here.</p>
      <div style="margin-top:8px;font-size:12px">Zones: ${S.zones.length ? S.zones.map((z) => `<code>${esc(z.name)}</code>`).join(" ") : "–"}
        · Zero Trust team: ${S.team_domain ? `<code>${esc(S.team_domain)}</code>` : `<span style="color:var(--warning, #f59e0b)">not set up – open "Zero Trust" once in Cloudflare for Access/login</span>`}</div>`)}
    ${box(`<b>3. Remote access of the client app</b>
      <p style="color:var(--subtext);font-size:12px;margin:4px 0 8px">If someone opens RDP, VNC or SSH "via RMM" in the client app, the module creates a tunnel address for it itself
        <code>rmm-&lt;protocol&gt;-&lt;agent&gt;.&lt;zone&gt;</code> – protected with its own service token per agent. How long it stays
        (until end of use, minutes, permanent) is chosen when connecting.</p>
      <div style="display:flex;gap:8px;align-items:center"><select data-rzone style="min-width:220px">
        ${S.zones.map((z) => `<option value="${esc(z.name)}" ${z.name === (S.remote?.zone || S.zones[0]?.name) ? "selected" : ""}>${esc(z.name)}</option>`).join("") || "<option value=''>– keine Zone –</option>"}
      </select><button class="btn btn-sm" data-rsave ${S.zones.length ? "" : "disabled"}>Save</button></div>`)}
    ${box(`<b>Remove everything</b>
      <p style="color:var(--subtext);font-size:12px;margin:4px 0 8px">Deletes all routes, DNS records, Access applications, logins, tunnels (also on the nodes) and the stored access.</p>
      <button class="btn btn-sm btn-danger" data-reset ${a.configured ? "" : "disabled"}>Remove everything</button>`)}`;
  const kind = () => pane.querySelector('input[name="cfat"]:checked').value;
  const sw = () => {
    pane.querySelector("[data-tok]").style.display = kind() === "token" ? "" : "none";
    pane.querySelector("[data-key]").style.display = kind() === "key" ? "flex" : "none";
  };
  pane.querySelectorAll('input[name="cfat"]').forEach((r) => r.addEventListener("change", sw));
  sw();
  pane.querySelector("[data-auth]").addEventListener("click", async (e) => {
    const b = e.currentTarget; b.disabled = true;
    const msg = pane.querySelector("[data-authmsg]");
    msg.textContent = "Checking …";
    try {
      const r = await cq("/auth", { method: "POST", body: { type: kind(), token: pane.querySelector("[data-token]").value.trim(),
        key: pane.querySelector("[data-keyv]").value.trim(), email: pane.querySelector("[data-email]").value.trim() } });
      msg.textContent = `${icon("check", 13)} ${r.kind} valid – ${r.accounts.length} account(s)`;
      const sel = pane.querySelector("[data-acc]");
      sel.innerHTML = r.accounts.map((x) => `<option value="${esc(x.id)}" ${x.id === S.account.id ? "selected" : ""}>${esc(x.name)}</option>`).join("");
    } catch (err) { msg.textContent = icon("x", 13) + " " + err.message; }
    b.disabled = false;
  });
  pane.querySelector("[data-accsave]").addEventListener("click", async () => {
    try {
      const r = await cq("/account", { method: "POST", body: { account_id: pane.querySelector("[data-acc]").value, public_base: pane.querySelector("[data-pub]").value.trim() } });
      window.notify?.(`Account applied – ${r.zones.length} zone(s)`, "success");
      redraw();
    } catch (e) { window.notify?.(e.message, "error", 7000); }
  });
  pane.querySelector("[data-rsave]").addEventListener("click", async () => {
    try { await cq("/remote", { method: "POST", body: { zone: pane.querySelector("[data-rzone]").value } }); window.notify?.("Saved", "success"); redraw(); }
    catch (e) { window.notify?.(e.message, "error"); }
  });
  pane.querySelector("[data-refresh]").addEventListener("click", async () => {
    try { await cq("/refresh", { method: "POST" }); redraw(); } catch (e) { window.notify?.(e.message, "error"); }
  });
  pane.querySelector("[data-reset]").addEventListener("click", async () => {
    if (!(await uiConfirm("Really remove everything?", { description: "All routes, tunnels and entries in Cloudflare will be deleted.", okText: "Remove everything", danger: true }))) return;
    try { const r = await cq("/all", { method: "DELETE" }); window.notify?.(r.errors?.length ? r.errors.join("; ") : "Everything removed", r.errors?.length ? "warning" : "success"); redraw(); }
    catch (e) { window.notify?.(e.message, "error"); }
  });
}

// ------------------------------------------------------------------ routes
function drawRoutes(pane, S, redraw) {
  const ready = S.auth.configured && S.account.id;
  const cat = S.catalog || {};
  const used = new Set(S.routes.map((r) => r.kind + ":" + JSON.stringify(r.ref || {})));
  const sugg = [
    ...(cat.services || []).map((s) => ({ label: s.label, hint: s.hint, preset: { kind: s.kind, name: s.label, ref: {} } })),
    ...(cat.websites || []).map((w) => ({ label: `Website ${w.name}`, preset: { kind: "website", name: `Website ${w.title}`, ref: { site: w.name } } })),
    ...(cat.docker || []).map((d) => ({ label: `${d.stack_name} / ${d.service}:${d.port}`, hint: d.mode === "path" ? `rmm/${d.name}/` : `Host-Port ${d.public_port}`,
                                        preset: { kind: "docker", name: `${d.stack_name} ${d.service}`, ref: { stack: d.stack, pub: d.pub } } })),
  ].filter((s) => !used.has(s.preset.kind + ":" + JSON.stringify(s.preset.ref)));
  pane.innerHTML = `
    ${!ready ? `<div style="color:var(--subtext);margin-bottom:10px">First set up access and the account under "Connection".</div>` : ""}
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
      <button class="btn-primary btn-sm" data-add ${ready ? "" : "disabled"}>+ Route</button>
      <button class="btn btn-sm" data-sync ${ready ? "" : "disabled"}>⟳ Sync</button>
      <span style="color:var(--subtext);font-size:12px">The targets are determined from the currently used ports on every sync.</span>
    </div>
    ${sugg.length && ready ? `<div style="margin-bottom:10px"><div style="font-size:12px;color:var(--subtext);margin-bottom:4px">Used services – tap to expose:</div>
      <div style="display:flex;flex-wrap:wrap;gap:6px">${sugg.map((s, i) => `<button class="btn btn-sm" data-sugg="${i}" title="${esc(s.hint || "")}">+ ${esc(s.label)}</button>`).join("")}
      <button class="btn btn-sm" data-sugg="agent">+ Agent (RDP/SSH/VNC)</button></div></div>` : ""}
    ${S.routes.length ? routeTable(S.routes, true) : `<div style="color:var(--subtext)">No routes yet.</div>`}`;
  pane.querySelector("[data-add]")?.addEventListener("click", () => routeDialog(S, null, redraw));
  pane.querySelector("[data-sync]")?.addEventListener("click", (e) => doSync(e.currentTarget, redraw));
  pane.querySelectorAll("[data-sugg]").forEach((b) => b.addEventListener("click", () =>
    routeDialog(S, b.dataset.sugg === "agent" ? { kind: "agent", name: "", ref: {} } : sugg[+b.dataset.sugg].preset, redraw)));
  pane.querySelectorAll("[data-edit]").forEach((b) => b.addEventListener("click", () => routeDialog(S, S.routes.find((r) => r.id === b.dataset.edit), redraw)));
  pane.querySelectorAll("[data-how]").forEach((b) => b.addEventListener("click", () => howTo(S.routes.find((r) => r.id === b.dataset.how))));
  pane.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
    const r = S.routes.find((x) => x.id === b.dataset.del);
    if (!(await uiConfirm(`Remove "${r.name}"?`, { description: "The DNS record, Access application, login and forward will be deleted.", okText: "Remove", danger: true }))) return;
    try { await cq(`/routes/${r.id}`, { method: "DELETE" }); window.notify?.("Route removed", "success"); redraw(); }
    catch (e) { window.notify?.(e.message, "error"); }
  }));
}

function protoOf(kind, ref) {
  if (["dashboard", "webdav", "website", "docker"].includes(kind)) return "http";
  if (kind === "sftp") return "tcp";
  if (kind === "ftp") return "ftp";
  if (kind === "console_ssh") return "ssh";
  if (kind === "agent") return ref.proto || "rdp";
  const u = (ref.url || "").toLowerCase();
  return ["ssh", "rdp", "tcp"].find((p) => u.startsWith(p + "://")) || "http";
}

async function routeDialog(S, r, redraw) {
  const ed = r?.id ? r : null;
  r = { name: "", kind: "dashboard", ref: {}, endpoint: "server", node_id: "", mode: "", hostname: "", access: "none", allow: [], emails: [], ...(r || {}) };
  r.ref = { ...(r.ref || {}) };
  let dir = { users: [], groups: [] };
  try { dir = await cq("/directory"); } catch { /* without selection */ }
  const zones = S.zones || [];
  const zoneOf = (h) => zones.find((z) => h === z.name || h.endsWith("." + z.name));
  const z0 = zoneOf(r.hostname || "") || zones[0];
  const sub0 = r.hostname && z0 ? (r.hostname === z0.name ? "" : r.hostname.slice(0, -(z0.name.length + 1))) : "";
  const ov = document.createElement("div");
  ov.style.cssText = "position:fixed;inset:0;z-index:9500;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;padding:12px";
  const cat = S.catalog || {};
  const clients = [...(state.clients || [])].sort((a, b) => (a.hostname || "").localeCompare(b.hostname || ""));
  ov.innerHTML = `<div style="background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:16px 18px;width:620px;max-width:100%;max-height:calc(100dvh - 24px);overflow:auto;font-size:13px">
    <b style="font-size:15px">${ed ? "Edit route" : "New route"}</b>
    <div style="display:grid;grid-template-columns:150px 1fr;gap:8px 10px;align-items:center;margin-top:12px">
      <label>Name</label><input data-f="name" value="${esc(r.name)}" placeholder="e.g. Dashboard">
      <label>What?</label><select data-f="kind">${Object.entries(KIND).map(([k, l]) => `<option value="${k}" ${k === r.kind ? "selected" : ""}>${l}</option>`).join("")}</select>
      <label data-row="site">Website</label><select data-row="site" data-ref="site">${(cat.websites || []).map((w) => `<option value="${esc(w.name)}" ${w.name === r.ref.site ? "selected" : ""}>${esc(w.title)} (/${esc(w.name)}/)</option>`).join("") || "<option value=''>– no websites –</option>"}</select>
      <label data-row="docker">Container</label><select data-row="docker" data-ref="dockerpub">${(cat.docker || []).map((d) => `<option value="${esc(d.stack + "|" + d.pub)}" ${d.stack === r.ref.stack && d.pub === r.ref.pub ? "selected" : ""}>${esc(d.stack_name)} / ${esc(d.service)}:${d.port} (${d.mode === "path" ? "rmm/" + esc(d.name) + "/" : "port " + d.public_port})</option>`).join("") || "<option value=''>– no publications (Docker module) –</option>"}</select>
      <label data-row="agent">Agent</label><select data-row="agent" data-ref="client_id">${clients.map((c) => `<option value="${esc(c.id)}" ${c.id === r.ref.client_id ? "selected" : ""}>${esc(c.hostname)}${c.ip ? " – " + esc(c.ip) : ""}</option>`).join("")}</select>
      <label data-row="agent">Protocol / port</label><div data-row="agent" style="display:flex;gap:6px">
        <select data-ref="proto">${["rdp", "ssh", "vnc"].map((p) => `<option value="${p}" ${p === (r.ref.proto || "rdp") ? "selected" : ""}>${p.toUpperCase()}</option>`).join("")}</select>
        <input data-ref="port" type="number" min="1" max="65535" value="${esc(r.ref.port || "")}" placeholder="Default" style="width:110px">
        <select data-ref="host_mode"><option value="local">The device itself</option><option value="custom" ${r.ref.host_mode === "custom" ? "selected" : ""}>Another address</option></select>
        <input data-ref="host" value="${esc(r.ref.host || "")}" placeholder="IP/name" style="width:120px"></div>
      <label data-row="agent">Tunnel ends</label><div data-row="agent" style="display:flex;gap:6px;flex-wrap:wrap">
        <select data-f="endpoint"><option value="server">at the RMM server (gateway, Network module)</option><option value="node" ${r.endpoint === "node" ? "selected" : ""}>on a node in the agent network</option></select>
        <select data-f="node_id" data-nodesel>${(S.nodes || []).map((n) => `<option value="${esc(n.client_id)}" ${n.client_id === r.node_id ? "selected" : ""}>${esc(clientName(n.client_id))}${n.online ? "" : " (offline)"}</option>`).join("") || "<option value=''>– no node –</option>"}</select></div>
      <label data-row="custom">Address</label><input data-row="custom" data-ref="url" value="${esc(r.ref.url || "")}" placeholder="http://10.0.0.5:8080 · tcp://host:port · ssh://host:22 · rdp://host:3389">
      <label>Reachable via</label><select data-f="mode"></select>
      <label data-row="host">Address</label><div data-row="host" style="display:flex;gap:4px;align-items:center">
        <input data-sub value="${esc(sub0)}" placeholder="e.g. rmm" style="width:160px"><span>.</span>
        <select data-zone>${zones.map((z) => `<option value="${esc(z.name)}" ${z0 && z.name === z0.name ? "selected" : ""}>${esc(z.name)}</option>`).join("")}</select></div>
      <label data-row="acc">Protection</label><select data-row="acc" data-f="access">${Object.entries(ACCESS).map(([k, l]) => `<option value="${k}" ${k === r.access ? "selected" : ""}>${l}</option>`).join("")}</select>
      <label data-row="allow" style="align-self:start">Allowed</label><div data-row="allow"><div style="font-size:11.5px;color:var(--subtext);margin-bottom:4px">Nothing selected = any RMM account.</div><div data-pick>${subjectPickerHtml(dir, r.allow || [], { name: "cfal" })}</div></div>
      <label data-row="otp" style="align-self:start">Email addresses</label><textarea data-row="otp" data-f="emails" rows="3" placeholder="one per line">${esc((r.emails || []).join("\n"))}</textarea>
    </div>
    <div data-note style="font-size:12px;color:var(--subtext);margin-top:10px"></div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px"><button class="btn" data-x>Cancel</button><button class="btn-primary" data-ok>${ed ? "Save" : "Create"}</button></div></div>`;
  document.body.appendChild(ov);
  initSubjectPicker(ov.querySelector("[data-pick]"));
  const $ = (s) => ov.querySelector(s);
  const ref = () => {
    const o = {};
    const kind = $('[data-f="kind"]').value;
    if (kind === "website") o.site = $('[data-ref="site"]').value;
    if (kind === "docker") { const [s, p] = $('[data-ref="dockerpub"]').value.split("|"); o.stack = s; o.pub = p; }
    if (kind === "agent") {
      o.client_id = $('[data-ref="client_id"]').value; o.proto = $('[data-ref="proto"]').value;
      const p = parseInt($('[data-ref="port"]').value, 10); if (p) o.port = p;
      o.host_mode = $('[data-ref="host_mode"]').value; if (o.host_mode === "custom") o.host = $('[data-ref="host"]').value.trim();
    }
    if (kind === "custom") o.url = $('[data-ref="url"]').value.trim();
    return o;
  };
  const sync = () => {
    const kind = $('[data-f="kind"]').value;
    const proto = protoOf(kind, ref());
    const modes = S.modes[proto] || ["http"];
    const ms = $('[data-f="mode"]');
    const keep = ms.value || r.mode;
    ms.innerHTML = modes.map((m) => `<option value="${m}" ${m === keep ? "selected" : ""}>${MODE[m]}</option>`).join("");
    const mode = ms.value;
    const show = (key, on) => ov.querySelectorAll(`[data-row="${key}"]`).forEach((el) => { el.style.display = on ? "" : "none"; });
    show("site", kind === "website"); show("docker", kind === "docker"); show("agent", kind === "agent"); show("custom", kind === "custom");
    show("host", mode !== "warp"); show("acc", mode !== "warp");
    const acc = $('[data-f="access"]');
    acc.querySelector('option[value="none"]').disabled = mode === "browser";
    if (mode === "browser" && acc.value === "none") acc.value = "rmm";
    show("allow", mode !== "warp" && acc.value === "rmm"); show("otp", mode !== "warp" && acc.value === "otp");
    $("[data-nodesel]").style.display = $('[data-f="endpoint"]').value === "node" ? "" : "none";
    $('[data-ref="host"]').style.display = $('[data-ref="host_mode"]').value === "custom" ? "" : "none";
    const notes = {
      browser: "Cloudflare shows the terminal or screen directly in the browser – after signing in.",
      client: proto === "rdp" ? "On the PC: cloudflared access rdp … and then Remote Desktop to localhost (instructions under \"Connect\")."
        : "cloudflared is needed on the PC (instructions under \"Connect\" in the route list).",
      warp: "No public address: reachable for devices with the WARP client in the Zero Trust team (private network).",
      http: kind === "website" ? "The website is then reachable at https://<address>/<name>/." : "",
    };
    $("[data-note]").textContent = (kind === "ftp" ? "FTP opens a new connection for every transfer and therefore only works via WARP. Better: SFTP or WebDAV. " : "")
      + (notes[mode] || "") + (acc.value === "rmm" && mode !== "warp" && !S.public_base ? " The public RMM address is still missing for the RMM login (Connection)." : "");
    if (!$('[data-f="name"]').dataset.touched && !ed) {
      const nm = kind === "agent" ? `${clientName($('[data-ref="client_id"]').value)} ${($('[data-ref="proto"]').value || "").toUpperCase()}` : $('[data-f="name"]').value || KIND[kind];
      $('[data-f="name"]').value = nm;
    }
  };
  $('[data-f="name"]').addEventListener("input", (e) => { e.target.dataset.touched = "1"; });
  ov.querySelectorAll("select, input").forEach((el) => el.addEventListener("change", sync));
  sync();
  $("[data-x]").addEventListener("click", () => ov.remove());
  $("[data-ok]").addEventListener("click", async (e) => {
    const b = e.currentTarget;
    const mode = $('[data-f="mode"]').value;
    const sub = $("[data-sub]").value.trim().toLowerCase().replace(/\.$/, "");
    const zone = $("[data-zone]").value;
    const body = {
      id: ed?.id || "", name: $('[data-f="name"]').value.trim(), kind: $('[data-f="kind"]').value, ref: ref(),
      endpoint: $('[data-f="endpoint"]').value, node_id: $('[data-f="node_id"]').value || "", mode,
      hostname: mode === "warp" ? "" : (sub ? `${sub}.${zone}` : zone), access: mode === "warp" ? "none" : $('[data-f="access"]').value,
      allow: readSubjectPicker(ov.querySelector("[data-pick]")),
      emails: $('[data-f="emails"]').value.split(/[\n,;]+/).map((x) => x.trim()).filter(Boolean),
    };
    b.disabled = true; b.textContent = "Setting up …";
    try {
      const res = await cq("/routes", { method: "POST", body });
      ov.remove();
      const err = res.route?.state?.error;
      window.notify?.(err ? `Saved, but: ${err}` : "Route set up", err ? "warning" : "success", 8000);
      redraw();
    } catch (err) { window.notify?.(err.message, "error", 8000); b.disabled = false; b.textContent = ed ? "Save" : "Create"; }
  });
}

function howTo(r) {
  const h = r.hostname;
  const proto = r.proto;
  let html;
  if (r.mode === "warp") {
    html = `<p>The service has no public address. It is reachable for devices with the <b>Cloudflare WARP client</b> that are signed in to the Zero Trust team
      (device rules in Cloudflare Zero Trust → Settings → WARP client; "Split Tunnels" must include the network).</p>
      <p>Address: <code>${esc(r.state?.warp_address || "…")}</code>${proto === "rdp" ? " – enter it in Remote Desktop" : proto === "ftp" ? " – enter it in the FTP program (passive)" : ""}</p>`;
  } else {
    const cmds = proto === "ssh"
      ? [`ssh -o ProxyCommand="cloudflared access ssh --hostname %h" USER@${h}`,
         `# or permanently in ~/.ssh/config:\nHost ${h}\n  ProxyCommand cloudflared access ssh --hostname %h`]
      : proto === "rdp"
        ? [`cloudflared access rdp --hostname ${h} --url rdp://localhost:13389`, "# then connect Remote Desktop (mstsc) to localhost:13389"]
        : [`cloudflared access tcp --hostname ${h} --url localhost:${proto === "vnc" ? 15900 : 12022}`,
           proto === "vnc" ? "# then connect a VNC viewer to localhost:15900" : "# then connect e.g. an SFTP program to localhost:12022"];
    html = `<p><b>cloudflared</b> is needed on the PC (<a href="https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/" target="_blank" rel="noopener" style="color:var(--accent)">download</a>).
      ${r.access !== "none" ? "On the first connection the browser opens for sign-in." : ""}</p>
      ${cmds.map((c) => `<pre style="background:var(--panel-2);border:1px solid var(--border);border-radius:8px;padding:8px;font-size:12px;white-space:pre-wrap;user-select:all">${esc(c)}</pre>`).join("")}`;
  }
  const ov = document.createElement("div");
  ov.style.cssText = "position:fixed;inset:0;z-index:9500;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;padding:12px";
  ov.innerHTML = `<div style="background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:16px 18px;width:560px;max-width:100%;font-size:13px">
    <b style="font-size:15px">Connect: ${esc(r.name)}</b>${html}
    <div style="text-align:right;margin-top:10px"><button class="btn" data-x>Close</button></div></div>`;
  document.body.appendChild(ov);
  ov.querySelector("[data-x]").addEventListener("click", () => ov.remove());
}

// ------------------------------------------------------------------ nodes
function drawNodes(pane, S, redraw) {
  const nt = S.tunnels.nodes || {};
  const nodes = S.nodes || [];
  pane.innerHTML = `<p style="color:var(--subtext);font-size:12px;margin-top:0">For "Agent: RDP/SSH/VNC" routes with <b>tunnel ends on a node</b>, each node gets its own tunnel.
      The module distributes the connector itself (container <code>rmm-cloudflared</code>, Docker required on the node) – it reaches the devices in the node network directly.</p>
    ${Object.keys(nt).length ? `<table class="data-table" style="width:100%"><thead><tr><th style="text-align:left">Node</th><th style="text-align:left">Tunnel</th><th>Routes</th><th>Connector</th><th></th></tr></thead><tbody>
      ${Object.entries(nt).map(([cid, t]) => {
        const n = nodes.find((x) => x.client_id === cid);
        const cnt = S.routes.filter((r) => r.endpoint === "node" && r.node_id === cid).length;
        return `<tr><td><b>${esc(clientName(cid))}</b> ${n?.online ? dot("var(--online,#16a34a)", 12) : dot("var(--subtext,#9ca3af)", 12)}</td><td><code>${esc(t.name || "")}</code></td><td style="text-align:center">${cnt}</td>
          <td style="text-align:center" title="${esc(t.error || "")}">${t.deployed ? icon("check", 13) + " deployed" : t.error ? icon("warn", 13) + " " + esc(t.error) : "…"}</td>
          <td style="text-align:right"><button class="btn btn-sm btn-danger" data-rmnode="${esc(cid)}" ${cnt ? "disabled title='Remove the routes first'" : ""}>Remove</button></td></tr>`;
      }).join("")}</tbody></table>` : `<div style="color:var(--subtext)">No node tunnels yet.</div>`}
    <div style="margin-top:12px;font-size:12px;color:var(--subtext)">Nodes in the RMM: ${nodes.length ? nodes.map((n) => `${esc(clientName(n.client_id))}${n.online ? "" : " (offline)"}`).join(", ") : "none (Agent -> Manage node)"}</div>`;
  pane.querySelectorAll("[data-rmnode]").forEach((b) => b.addEventListener("click", async () => {
    if (!(await uiConfirm("Remove node tunnel?", { okText: "Remove", danger: true }))) return;
    try { await cq(`/nodes/${b.dataset.rmnode}`, { method: "DELETE" }); redraw(); } catch (e) { window.notify?.(e.message, "error"); }
  }));
}

// ------------------------------------------------------------------ help
function drawHelp(pane) {
  pane.innerHTML = `<div style="max-width:760px;line-height:1.6">
    <h3 style="margin-top:0">Create an API token</h3>
    <p>Cloudflare → My Profile → API Tokens → <i>Create Token</i> → <i>Custom token</i> with these permissions:</p>
    <ul style="margin-top:0">
      <li>Account → <b>Cloudflare Tunnel</b>: Edit</li>
      <li>Account → <b>Access: Apps and Policies</b>: Edit</li>
      <li>Account → <b>Access: Organizations, Identity Providers and Groups</b>: Edit</li>
      <li>Account → <b>Zero Trust</b>: Edit (private networks / WARP)</li>
      <li>Zone → <b>DNS</b>: Edit · Zone → <b>Zone</b>: Read (for all zones you want to use)</li>
    </ul>
    <h3>Reachable via</h3>
    <ul style="margin-top:0">
      <li><b>In the browser</b> – dashboard, WebDAV, websites, containers: normal https address.</li>
      <li><b>In the browser via Cloudflare Access</b> – SSH as a web terminal, VNC as a web screen (always with login).</li>
      <li><b>With cloudflared on the PC</b> – SSH, SFTP, RDP, VNC over a local port (instructions per route under "Connect").</li>
      <li><b>Private network (WARP)</b> – no public address; devices with the WARP client in the Zero Trust team reach the IP directly. The only way for FTP.</li>
    </ul>
    <h3>Protection</h3>
    <ul style="margin-top:0">
      <li><b>RMM login</b> – before access, Cloudflare Access requests the RMM sign-in (local accounts or AD/LDAP, incl. 2FA). Users/groups can be restricted per route.
        Requirement: public RMM address (Connection) and a configured Zero Trust team. The module automatically exempts the login addresses <code>/cf-oidc/…</code> from protection.</li>
      <li><b>One-time PIN by email</b> – Cloudflare sends a code to the entered addresses.</li>
      <li><b>Open</b> – only the service own sign-in (e.g. RMM login in the dashboard).</li>
    </ul>
    <h3>Agents: where does the tunnel end?</h3>
    <ul style="margin-top:0">
      <li><b>At the RMM server</b>: the module creates a permanent forward (Network module) to the agent – the agent must be online.</li>
      <li><b>On a node</b>: cloudflared runs on the node and connects directly to the device in the local network (also devices without an agent via "another address").</li>
    </ul></div>`;
}
