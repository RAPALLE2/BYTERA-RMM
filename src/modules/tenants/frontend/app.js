// app.js (module "tenants") - connect & switch between multiple tenant backends
// --------------------------------------------------------------------------
// Each tenant is its own RMM backend (its own DB/settings/permissions/spaces/agents).
// This app lists the shared tenants, lets the user store their credentials per
// tenant and opens a tenant's dashboard EMBEDDED (iframe in the app window,
// auto-login via #client-token=). Open windows in the main RMM are preserved.
// Who may see which tenant is set by an admin (permission manage_tenants) in the
// administration. The fine-grained permissions live on the tenant.

import { nodeHint } from "/js/app/nodechoice.js";
import { getToken, api } from "/js/app/api.js";
import { state } from "/js/app/state.js";
import { esc, uiConfirm, uiPrompt } from "/js/app/utils.js";
import { hasGlobalPerm } from "/js/app/state.js";
import { registerCleanup } from "/js/app/windowmanager.js";
import { icon } from "./icons.js";

// Extend the embed URL with a deep-link target (?go=bytera://...). The tenant
// dashboard resolves it itself after auto-login (module url-scheme).
function withGo(url, go) {
  if (!go) return url;
  const i = url.indexOf("#");
  const base = i >= 0 ? url.slice(0, i) : url;
  const hash = i >= 0 ? url.slice(i) : "";
  return base + (base.includes("?") ? "&" : "?") + "go=" + encodeURIComponent(go) + hash;
}

async function tq(path, opts = {}) {
  const headers = { Authorization: "Bearer " + (getToken() || "") };
  if (opts.body !== undefined && typeof opts.body !== "string") { headers["Content-Type"] = "application/json"; opts = { ...opts, body: JSON.stringify(opts.body) }; }
  const res = await fetch("/api/tenants" + path, { ...opts, headers });
  if (!res.ok) { let m = res.statusText; let code = res.status; try { const j = await res.json(); m = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch { /* ignore */ } const e = new Error(m); e.status = code; throw e; }
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : res;
}

const canManage = () => { try { return hasGlobalPerm("manage_tenants"); } catch { return false; } };
const dot = (c) => `<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${esc(c || "var(--accent)")}"></span>`;

export async function renderTenants(body, win) {
  body.innerHTML = `
    <div style="display:flex;flex-direction:column;height:100%;min-height:0;font-size:13px">
      <div data-bar style="display:flex;align-items:center;gap:8px;padding:8px 12px;border-bottom:1px solid var(--border);flex-wrap:wrap">
        <b style="font-size:14px;display:inline-flex;align-items:center;gap:7px">${icon("building", 16)} Tenants</b>
        <div data-tabs style="display:flex;gap:4px;flex:1"></div>
        ${canManage() ? `<button class="btn btn-sm" data-admin>${icon("gear", 13)} Administration</button>` : ""}
      </div>
      <div data-view style="flex:1;min-height:0;position:relative"></div>
    </div>`;
  const view = body.querySelector("[data-view]");
  const tabsEl = body.querySelector("[data-tabs]");
  let tenants = [];
  const open = {};        // tid -> {iframe} open embedded dashboards (stay loaded)
  let active = "home";    // "home" | tid
  // Link/address bar: the currently open tenant by NAME (bytera://mandant/<name>).
  if (win) win.getLink = () => {
    const tn = active !== "home" ? tenants.find((x) => x.id === active) : null;
    return tn ? "bytera://mandant/" + encodeURIComponent(tn.name) : "bytera://app/tenants";
  };

  // Tenant by ID or name (bytera://<mandant>/agent/<name>)
  const findRef = (ref) => {
    const r = String(ref || "").trim().toLowerCase();
    return tenants.find((x) => x.id === ref) || tenants.find((x) => String(x.name || "").toLowerCase() === r)
      || tenants.find((x) => String(x.name || "").toLowerCase().replace(/\s+/g, "-") === r) || null;
  };
  const openByRef = (ref, go) => {
    const tn = findRef(ref);
    if (!tn) { window.notify?.(`Tenant "${ref}" not found or not shared.`, "error", 7000); return; }
    openTenant(tn.id, "", go || "");
  };
  // Already open window: accept deep links from the core here.
  const onDeepLink = (ev) => {
    if (!body.isConnected) return;
    ev.detail.handled = true;
    const run = () => openByRef(ev.detail.ref, ev.detail.go);
    if (tenants.length) run(); else load().then(run);
  };
  window.addEventListener("bytera-tenant-open", onDeepLink);
  if (win?.key) registerCleanup(win.key, () => window.removeEventListener("bytera-tenant-open", onDeepLink));

  const load = async () => { try { tenants = (await tq("")).tenants; } catch (e) { view.innerHTML = `<div style="padding:16px;color:var(--danger)">${esc(e.message)}</div>`; return; } drawTabs(); show(active); };

  const drawTabs = () => {
    tabsEl.innerHTML = `<button class="tab-btn ${active === "home" ? "active" : ""}" data-t="home">Overview</button>` +
      Object.keys(open).map((tid) => {
        const t = tenants.find((x) => x.id === tid) || { name: tid };
        return `<button class="tab-btn ${active === tid ? "active" : ""}" data-t="${esc(tid)}">${dot(t.color)} ${esc(t.name)} <span data-close="${esc(tid)}" title="Close" style="margin-left:4px;opacity:.6">${icon("close", 12)}</span></button>`;
      }).join("");
    tabsEl.querySelectorAll("[data-t]").forEach((b) => b.addEventListener("click", (e) => { if (e.target.dataset.close) return; show(b.dataset.t); }));
    tabsEl.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); closeTab(b.dataset.close); }));
  };

  const show = (which) => {
    active = which;
    // Hide all iframes, show the active one (they stay loaded -> state preserved)
    Object.entries(open).forEach(([tid, o]) => { o.wrap.style.display = tid === which ? "block" : "none"; });
    let home = view.querySelector("[data-home]");
    if (which === "home") { if (home) home.style.display = "block"; drawHome(); }
    else if (home) home.style.display = "none";
    drawTabs();
  };

  const closeTab = (tid) => { const o = open[tid]; if (o) { o.wrap.remove(); delete open[tid]; } if (active === tid) active = "home"; show(active); };

  const drawHome = () => {
    let home = view.querySelector("[data-home]");
    if (!home) { home = document.createElement("div"); home.dataset.home = "1"; home.style.cssText = "position:absolute;inset:0;overflow:auto;padding:14px 16px"; view.appendChild(home); }
    if (!tenants.length) { home.innerHTML = `<div style="color:var(--subtext)">No tenants shared yet.${canManage() ? " Create some in the administration." : ""}</div>`; return; }
    home.innerHTML = `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px">
      ${tenants.map((t) => `
        <div style="border:1px solid var(--border);border-radius:12px;padding:14px;display:flex;flex-direction:column;gap:8px">
          <div style="display:flex;align-items:center;gap:8px"><span style="font-size:20px">${dot(t.color)}</span><b style="font-size:15px">${esc(t.name)}</b></div>
          ${t.note ? `<div style="font-size:12px;color:var(--subtext)">${esc(t.note)}</div>` : ""}
          <div style="font-size:11.5px;color:var(--subtext)">${esc((t.url || "").replace(/^https?:\/\//, ""))}</div>
          ${t.paired ? `<div style="font-size:11.5px;color:${t.pair_status === "active" ? "var(--success,#22c55e)" : "var(--warning,#f59e0b)"}">${t.pair_status === "active" ? `${icon("check", 12)} verified pairing` : `${icon("warn", 12)} pairing ${esc(t.pair_status || "pending")}`}</div>` : ""}
          <div style="font-size:12px">${t.has_credentials ? `${icon("check", 13)} signed in as <b>${esc(t.username)}</b>` : `${icon("warn", 13)} no credentials`}</div>
          <div style="display:flex;gap:6px;margin-top:auto">
            <button class="btn-primary btn-sm" data-open="${esc(t.id)}">${t.has_credentials ? "Open" : "Sign in & open"}</button>
            <button class="btn btn-sm" data-cred="${esc(t.id)}">Credentials</button>
            <button class="btn btn-sm" data-spaces="${esc(t.id)}" ${t.has_credentials ? "" : "disabled"}>Files</button>
          </div>
        </div>`).join("")}</div>`;
    home.querySelectorAll("[data-open]").forEach((b) => b.addEventListener("click", () => openTenant(b.dataset.open)));
    home.querySelectorAll("[data-cred]").forEach((b) => b.addEventListener("click", () => credDialog(tenants.find((x) => x.id === b.dataset.cred), load)));
    home.querySelectorAll("[data-spaces]").forEach((b) => b.addEventListener("click", () => spacesDialog(tenants.find((x) => x.id === b.dataset.spaces))));
  };

  const openTenant = async (tid, code, go = "") => {
    const t = tenants.find((x) => x.id === tid);
    if (!t) return;
    if (!t.has_credentials) { credDialog(t, () => { load().then(() => openTenant(tid, "", go)); }); return; }
    if (open[tid] && !go) { show(tid); return; }
    let res;
    try { res = await tq(`/${tid}/open`, { method: "POST", body: { code: code || "" } }); }
    catch (e) {
      if (e.status === 401 && e.message === "2fa_required") { const c = await uiPrompt("2FA code", { description: "2FA code for " + t.name }); if (c) openTenant(tid, c, go); return; }
      if (e.status === 412) { credDialog(t, () => load().then(() => openTenant(tid, "", go))); return; }
      window.notify?.(e.message, "error", 7000); return;
    }
    if (open[tid]) {
      // Tab is already open -> reload with the new target.
      open[tid].iframe.src = withGo(res.url, go);
      show(tid);
      return;
    }
    const wrap = document.createElement("div");
    wrap.style.cssText = "position:absolute;inset:0;display:flex;flex-direction:column";
    wrap.innerHTML = `<div style="display:flex;align-items:center;gap:8px;padding:4px 8px;background:var(--panel-2);border-bottom:1px solid var(--border);font-size:12px">
        ${dot(t.color)} <b>${esc(t.name)}</b> <span style="color:var(--subtext)">${esc((res.base || "").replace(/^https?:\/\//, ""))}</span>
        <span style="flex:1"></span>
        <button class="btn btn-sm" data-reload>↻ Reload</button>
        <button class="btn btn-sm" data-ext title="In a new tab">↗</button></div>
      <iframe style="flex:1;border:0;width:100%;background:var(--bg)" src="${esc(withGo(res.url, go))}"></iframe>`;
    view.appendChild(wrap);
    const iframe = wrap.querySelector("iframe");
    wrap.querySelector("[data-reload]").addEventListener("click", async () => {
      try { const r = await tq(`/${tid}/open`, { method: "POST", body: {} }); iframe.src = r.url; } catch (e) { window.notify?.(e.message, "error"); }
    });
    wrap.querySelector("[data-ext]").addEventListener("click", () => window.open(res.url, "_blank", "noopener"));
    open[tid] = { wrap, iframe };
    show(tid);
  };

  body.querySelector("[data-admin]")?.addEventListener("click", () => adminDialog(load));
  load().then(() => {
    const p = win?.props || {};
    if (p.openTenant) openByRef(p.openTenant, p.go || "");
  });
}

// ------------------------------------------------------------------ credentials
function credDialog(t, done) {
  const ov = overlay(`<b style="font-size:15px">Credentials – ${esc(t.name)}</b>
    <div style="font-size:12px;color:var(--subtext);margin:4px 0 10px">Your personal login for this tenant backend. Stored encrypted and used only for your access.</div>
    <div style="display:grid;grid-template-columns:110px 1fr;gap:8px 10px;align-items:center">
      <label>Username</label><input data-u value="${esc(t.username || "")}">
      <label>Password</label><input data-p type="password" placeholder="${t.has_credentials ? "•••• (leave unchanged)" : ""}">
      <label>Realm</label><input data-r value="local" title="local or AD realm ID">
    </div>
    <div data-2fa style="display:none;margin-top:8px"><input data-code placeholder="2FA code (if the tenant requires it)" style="width:100%"></div>
    <div data-msg style="font-size:12px;margin-top:8px;color:var(--subtext)"></div>
    <div style="display:flex;gap:8px;justify-content:space-between;margin-top:14px">
      ${t.has_credentials ? `<button class="btn btn-sm btn-danger" data-clear>Remove</button>` : "<span></span>"}
      <span><button class="btn" data-x>Cancel</button><button class="btn-primary" data-ok>Save & check</button></span></div>`);
  const $ = (s) => ov.querySelector(s);
  $("[data-x]").addEventListener("click", () => ov.remove());
  $("[data-clear]")?.addEventListener("click", async () => { try { await tq(`/${t.id}/credentials`, { method: "DELETE" }); ov.remove(); done(); window.notify?.("Credentials removed", "success"); } catch (e) { window.notify?.(e.message, "error"); } });
  $("[data-ok]").addEventListener("click", async () => {
    const m = $("[data-msg]"); m.textContent = "Checking …";
    try {
      await tq(`/${t.id}/credentials`, { method: "PUT", body: { username: $("[data-u]").value.trim(), password: $("[data-p]").value, realm: $("[data-r]").value.trim() || "local", code: $("[data-code]").value.trim() } });
      ov.remove(); done(); window.notify?.("Credentials saved", "success");
    } catch (e) {
      if (e.status === 401 && e.message === "2fa_required") { $("[data-2fa]").style.display = "block"; m.textContent = "Please also enter the 2FA code."; return; }
      m.innerHTML = `<span style="color:var(--danger)">${esc(e.message)}</span>`;
    }
  });
}

// ------------------------------------------------------------------ tenant files (spaces)
async function spacesDialog(t, path = "") {
  const ov = overlay(`<b style="font-size:15px">Files – ${esc(t.name)}</b><div data-body style="margin-top:10px;min-height:120px">Loading …</div>
    <div style="text-align:right;margin-top:12px"><button class="btn" data-x>Close</button></div>`, 680);
  ov.querySelector("[data-x]").addEventListener("click", () => ov.remove());
  const b = ov.querySelector("[data-body]");
  const nav = async (p) => {
    b.innerHTML = "…";
    let d; try { d = await tq(`/${t.id}/spaces?path=${encodeURIComponent(p)}`); } catch (e) { b.innerHTML = `<div style="color:var(--danger)">${esc(e.message)}</div>`; return; }
    const entries = d.entries || d.items || [];
    const parent = p.includes("/") ? p.slice(0, p.lastIndexOf("/")) : "";
    b.innerHTML = `<div style="font-size:12px;color:var(--subtext);margin-bottom:6px;display:flex;align-items:center;gap:6px">${icon("folder", 13)} /${esc(p)}</div>
      ${p ? `<div data-up style="cursor:pointer;padding:4px 0;display:flex;align-items:center;gap:6px">${icon("arrowup", 13)} ..</div>` : ""}
      ${entries.length ? entries.map((e) => `<div style="display:flex;gap:8px;align-items:center;padding:4px 0;border-bottom:1px solid var(--border)">
        <span>${e.is_dir || e.dir ? icon("folder", 14) : icon("file", 14)}</span>
        <span style="flex:1;${e.is_dir || e.dir ? "cursor:pointer" : ""}" ${e.is_dir || e.dir ? `data-dir="${esc(e.path || (p ? p + "/" : "") + (e.name || ""))}"` : ""}>${esc(e.name || "")}</span>
        ${e.is_dir || e.dir ? "" : `<button class="btn btn-sm" data-dl="${esc(e.path || (p ? p + "/" : "") + (e.name || ""))}">${icon("download", 13)}</button>`}</div>`).join("") : `<div style="color:var(--subtext)">Empty.</div>`}`;
    b.querySelector("[data-up]")?.addEventListener("click", () => nav(parent));
    b.querySelectorAll("[data-dir]").forEach((el) => el.addEventListener("click", () => nav(el.dataset.dir)));
    b.querySelectorAll("[data-dl]").forEach((el) => el.addEventListener("click", () => window.open(`/api/tenants/${t.id}/spaces/file?path=${encodeURIComponent(el.dataset.dl)}`, "_blank")));
  };
  nav(path);
}

// ------------------------------------------------------------------ administration (admin)
async function adminDialog(done) {
  let ts = [], dir = { users: [], groups: [] };
  try { ts = (await tq("/admin")).tenants; dir = await tq("/directory"); } catch (e) { window.notify?.(e.message, "error"); return; }
  const ov = overlay(`<div style="display:flex;align-items:center;gap:8px"><b style="font-size:15px;flex:1">Tenant administration</b>
      <button class="btn-primary btn-sm" data-add>+ Tenant</button><button class="btn btn-sm" data-x>Close</button></div>
    <div data-list style="margin-top:12px"></div>`, 760);
  ov.querySelector("[data-x]").addEventListener("click", () => { ov.remove(); done(); });
  const list = ov.querySelector("[data-list]");
  const draw = () => {
    list.innerHTML = ts.length ? ts.map((t) => `
      <div style="border:1px solid var(--border);border-radius:10px;padding:10px 12px;margin-bottom:8px">
        <div style="display:flex;align-items:center;gap:8px"><b>${esc(t.name)}</b>
          <span style="font-size:11.5px;color:var(--subtext)">${esc((t.url || "").replace(/^https?:\/\//, ""))}</span>
          ${t.paired ? `<span title="Pairing status" style="font-size:10.5px;padding:1px 7px;border-radius:999px;background:var(--panel-2);color:${t.pair_status === "active" ? "var(--success,#22c55e)" : t.pair_status === "pending" ? "var(--warning,#f59e0b)" : "var(--danger,#ef4444)"}">${esc(t.pair_status || "paired")}</span>` : ""}
          <span style="flex:1"></span>
          <button class="btn btn-sm" data-edit="${esc(t.id)}">Edit</button>
          <button class="btn btn-sm btn-danger" data-del="${esc(t.id)}">Delete</button></div>
        <div style="font-size:12px;color:var(--subtext);margin-top:4px">Visible to: ${(t.allow && t.allow.length) ? esc(t.allow.map((a) => (a.type === "user" ? "@" : "#") + nameOf(a, dir)).join(", ")) : "everyone"}</div>
      </div>`).join("") : `<div style="color:var(--subtext)">No tenants yet.</div>`;
    list.querySelectorAll("[data-edit]").forEach((b) => b.addEventListener("click", () => tenantForm(ts.find((x) => x.id === b.dataset.edit), dir, reload)));
    list.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
      if (!(await uiConfirm("Remove tenant? (the credentials of all users for it will be deleted)", { okText: "Remove", danger: true }))) return;
      try { await tq(`/admin/${b.dataset.del}`, { method: "DELETE" }); reload(); } catch (e) { window.notify?.(e.message, "error"); }
    }));
  };
  const reload = async () => { try { ts = (await tq("/admin")).tenants; } catch { /* ignore */ } draw(); };
  ov.querySelector("[data-add]").addEventListener("click", () => tenantForm(null, dir, reload));
  draw();
}

function nameOf(a, dir) {
  if (a.type === "user") return (dir.users.find((u) => u.id === a.id) || {}).username || a.id.slice(0, 6);
  return (dir.groups.find((g) => g.id === a.id) || {}).name || a.id.slice(0, 6);
}

function tenantForm(t, dir, done) {
  const ed = !!t;
  t = t || { verify_tls: true, allow: [], color: "#2dd4bf" };
  const allow = new Set((t.allow || []).map((a) => a.type + ":" + a.id));
  const ov = overlay(`<b style="font-size:15px">${ed ? "Edit tenant" : "Create tenant"}</b>
    <div style="display:grid;grid-template-columns:120px 1fr;gap:8px 10px;align-items:center;margin-top:12px">
      <label>Name</label><input data-f="name" value="${esc(t.name || "")}" placeholder="e.g. Company North">
      <label>Backend URL</label><input data-f="url" value="${esc(t.url || "")}" placeholder="https://rmm-north.company.com">
      <label>Color</label><input data-f="color" type="color" value="${esc(t.color || "#2dd4bf")}" style="width:60px;height:30px;padding:0">
      <label>Note</label><input data-f="note" value="${esc(t.note || "")}">
      <label>Verify TLS</label><label style="display:flex;gap:6px;align-items:center;font-size:12.5px"><input type="checkbox" data-f="verify_tls" ${t.verify_tls ? "checked" : ""}> Verify the tenant certificate</label>
      <label>Connect via</label><select data-f="via"><option value="">Backend (RMM server)</option>${t.via ? `<option value="${esc(t.via)}" selected>Node (loading …)</option>` : ""}</select>
      <label>This RMM address</label><input data-f="parent_url" value="${esc(t.parent_url || "")}" placeholder="${esc(location.origin)}" title="Registered on the tenant for frame-ancestors during pairing">
    </div>
    <div style="font-size:11.5px;color:var(--subtext);margin-top:6px">Login and API calls (e.g. spaces) run through a node - needed when the tenant is only reachable in the node network.
      The embedded dashboard is still loaded by your browser directly from the backend URL.</div>
    <div data-pair style="margin-top:10px"></div>
    <div style="font-weight:600;margin:12px 0 4px;font-size:12.5px">Visible to (empty = everyone)</div>
    <div style="display:flex;gap:6px;margin-bottom:6px"><input data-filter placeholder="Filter users/groups" style="flex:1"></div>
    <div data-allow style="max-height:200px;overflow:auto;border:1px solid var(--border);border-radius:8px;padding:6px"></div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px"><button class="btn" data-x>Cancel</button><button class="btn-primary" data-ok>${ed ? "Save" : "Create"}</button></div>`, 620);
  const $ = (s) => ov.querySelector(s);
  const drawAllow = () => {
    const f = $("[data-filter]").value.toLowerCase();
    const row = (type, id, label, gl) => allow.has(type + ":" + id) || !f || label.toLowerCase().includes(f)
      ? `<label style="display:flex;gap:6px;align-items:center;padding:3px 0;cursor:pointer"><input type="checkbox" data-a="${type}:${esc(id)}" ${allow.has(type + ":" + id) ? "checked" : ""}> ${gl} ${esc(label)}</label>` : "";
    $("[data-allow]").innerHTML =
      `<div style="font-size:11px;color:var(--subtext)">Groups</div>` + dir.groups.map((g) => row("group", g.id, g.name, icon("users", 13))).join("") +
      `<div style="font-size:11px;color:var(--subtext);margin-top:4px">Users</div>` + dir.users.map((u) => row("user", u.id, u.username, icon("user", 13))).join("");
    $("[data-allow]").querySelectorAll("[data-a]").forEach((c) => c.addEventListener("change", () => { c.checked ? allow.add(c.dataset.a) : allow.delete(c.dataset.a); }));
  };
  $("[data-filter]").addEventListener("input", drawAllow); drawAllow();
  // Nodes for "connect via" (selectable only with the loaded node module "tenants")
  api.nodeChoices("tenants").then((r) => {
    const sel = $('[data-f="via"]');
    sel.querySelectorAll("option:not([value=''])").forEach((o) => o.remove());
    for (const n of r.nodes || []) {
      const o = document.createElement("option");
      o.value = n.id; o.disabled = !n.active && n.id !== t.via;
      o.textContent = `Node ${n.name}${nodeHint(n)}`;
      if (n.id === t.via) o.selected = true;
      sel.appendChild(o);
    }
    if (t.via && !(r.nodes || []).some((n) => n.id === t.via)) {
      const o = document.createElement("option"); o.value = t.via; o.selected = true; o.textContent = "Node (offline)"; sel.appendChild(o);
    }
  }).catch(() => {});
  // Pairing (tenant-link): only for an already-saved tenant (needs its id).
  const parentUrl = () => $('[data-f="parent_url"]').value.trim();
  const renderPair = async () => {
    const box = $("[data-pair]");
    if (!ed) { box.innerHTML = `<div style="font-size:11.5px;color:var(--subtext);border-top:1px solid var(--border);padding-top:8px">Pairing with confirmation becomes available once this tenant is saved.</div>`; return; }
    box.innerHTML = `<div style="font-size:11.5px;color:var(--subtext)">Checking pairing …</div>`;
    let d; try { d = await tq(`/admin/${t.id}/pair`); } catch (e) { box.innerHTML = `<div style="color:var(--danger);font-size:12px">${esc(e.message)}</div>`; return; }
    const pill = d.paired
      ? (d.status === "active" ? `<span style="color:var(--success,#22c55e)">● paired &amp; active</span>`
        : d.status === "pending" ? `<span style="color:var(--warning,#f59e0b)">● pending approval</span>`
          : `<span style="color:var(--danger,#ef4444)">● revoked</span>`)
      : `<span style="color:var(--subtext)">○ not paired (legacy open)</span>`;
    box.innerHTML = `<div style="border-top:1px solid var(--border);padding-top:10px;margin-top:4px">
      <div style="font-weight:600;font-size:12.5px;margin-bottom:4px">Pairing &nbsp;${pill}</div>
      <div style="font-size:11.5px;color:var(--subtext);margin-bottom:8px">A paired tenant must approve this RMM before it can be opened. The tenant admin issues a pairing code or approves your request in their <b>Tenant Sharing</b> app.${d.error ? ` <span style="color:var(--danger)">(${esc(d.error === "removed" ? "pairing removed on the tenant" : d.error)})</span>` : ""}</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        <button class="btn btn-sm btn-primary" data-pcode>Pair with code</button>
        <button class="btn btn-sm" data-preq>Request pairing</button>
        ${d.paired ? `<button class="btn btn-sm btn-danger" data-punpair>Unpair</button>` : ""}</div></div>`;
    box.querySelector("[data-pcode]").addEventListener("click", async () => {
      const code = await uiPrompt("Pairing code", { description: "Paste the code from the tenant's Tenant Sharing app." });
      if (!code) return;
      try { await tq(`/admin/${t.id}/pair/claim`, { method: "POST", body: { code: code.trim(), parent_url: parentUrl() } }); window.notify?.("Paired", "success"); renderPair(); }
      catch (e) { window.notify?.(e.message, "error", 6000); }
    });
    box.querySelector("[data-preq]").addEventListener("click", async () => {
      try { await tq(`/admin/${t.id}/pair/request`, { method: "POST", body: { parent_url: parentUrl() } }); window.notify?.("Pairing requested - waiting for the tenant to approve", "success", 5000); renderPair(); }
      catch (e) { window.notify?.(e.message, "error", 6000); }
    });
    box.querySelector("[data-punpair]")?.addEventListener("click", async () => {
      if (!(await uiConfirm("Remove the pairing key from this RMM? You can pair again later.", { okText: "Unpair", danger: true }))) return;
      try { await tq(`/admin/${t.id}/pair`, { method: "DELETE" }); renderPair(); } catch (e) { window.notify?.(e.message, "error"); }
    });
  };
  renderPair();
  $("[data-x]").addEventListener("click", () => ov.remove());
  $("[data-ok]").addEventListener("click", async () => {
    const payload = { name: $('[data-f="name"]').value.trim(), url: $('[data-f="url"]').value.trim(),
      color: $('[data-f="color"]').value, note: $('[data-f="note"]').value.trim(), verify_tls: $('[data-f="verify_tls"]').checked,
      via: $('[data-f="via"]').value, parent_url: $('[data-f="parent_url"]').value.trim(),
      allow: [...allow].map((x) => { const [type, id] = x.split(":"); return { type, id }; }) };
    try { if (ed) await tq(`/admin/${t.id}`, { method: "PUT", body: payload }); else await tq("/admin", { method: "POST", body: payload }); ov.remove(); done(); window.notify?.("Saved", "success", 1500); }
    catch (e) { window.notify?.(e.message, "error", 6000); }
  });
}

// ------------------------------------------------------------------ helpers
function overlay(html, width = 520) {
  const ov = document.createElement("div");
  ov.style.cssText = "position:fixed;inset:0;z-index:9500;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;padding:12px";
  ov.innerHTML = `<div style="background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:16px 18px;width:${width}px;max-width:100%;max-height:calc(100dvh - 24px);overflow:auto;font-size:13px">${html}</div>`;
  document.body.appendChild(ov);
  ov.addEventListener("click", (e) => { if (e.target === ov) ov.remove(); });
  return ov;
}
