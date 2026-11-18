// gpodns.js (module "addir") - DNS records and Group Policy
// ------------------------------------------------------------------
// DNS: zones -> records (create/edit/delete A/AAAA/CNAME/MX/TXT/SRV/NS/PTR).
// GPO: list, create/rename/delete/status, links (order, enforced, enabled),
// block inheritance, security filtering, settings: ALL administrative templates
// dynamically from the domain's ADMX/ADML, custom registry values (Registry.pol)
// and security settings (GptTmpl.inf).
// Everything goes through POST /api/ad/{id}/core/{op} (backend OR node on the DC).

import { getToken } from "/js/app/api.js";
import { withBackground, resolveJob } from "/js/app/bgjobs.js";
import { esc, uiConfirm } from "/js/app/utils.js";
import { icon } from "./icons.js";

async function req(path, opts = {}) {
  const headers = { Authorization: "Bearer " + (getToken() || "") };
  if (opts.body !== undefined) { headers["Content-Type"] = "application/json"; opts = { ...opts, body: JSON.stringify(opts.body) }; }
  const res = await fetch(withBackground("/api/ad" + path, opts.method), { ...opts, headers });
  if (!res.ok) { let m = res.statusText; try { const j = await res.json(); m = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch { /* ignore */ } throw new Error(m); }
  return resolveJob(await res.json());
}
const core = async (conn, op, args = {}) => (await req(`/${conn}/core/${op}`, { method: "POST", body: args })).result;
const toast = (m, t = "success", ms = 1800) => window.notify?.(m, t, ms);
const errBox = (e) => `<div style="padding:14px;color:var(--danger);white-space:pre-wrap">${esc(e.message || e)}</div>`;
const card = "background:var(--panel);border:1px solid var(--border);border-radius:12px";

function overlay(html, width = 640) {
  const ov = document.createElement("div");
  ov.style.cssText = "position:fixed;inset:0;z-index:9500;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;padding:12px";
  ov.innerHTML = `<div style="background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:16px 18px;width:${width}px;max-width:100%;max-height:calc(100dvh - 24px);overflow:auto;font-size:13px">${html}</div>`;
  document.body.appendChild(ov);
  ov.addEventListener("click", (e) => { if (e.target === ov) ov.remove(); });
  ov.querySelector("[data-x]")?.addEventListener("click", () => ov.remove());
  return ov;
}

// ------------------------------------------------------------ Execution path
async function execBar(host, conn) {
  host.innerHTML = `<span style="color:var(--subtext)">Execution …</span>`;
  let d; try { d = await req(`/${conn}/exec`); } catch (e) { host.innerHTML = ""; return; }
  const opts = [`<option value="">Backend (LDAP + SYSVOL via SMB)</option>`]
    .concat((d.nodes || []).map((n) => `<option value="${esc(n.id || n.client_id)}">Node: ${esc(n.name || n.hostname || n.id)}${n.online === false ? " (offline)" : ""}</option>`));
  if (d.via && !(d.nodes || []).some((n) => (n.id || n.client_id) === d.via)) opts.push(`<option value="${esc(d.via)}">Node ${esc(d.via.slice(0, 8))} (unavailable)</option>`);
  host.innerHTML = `<label style="display:flex;gap:8px;align-items:center;font-size:12px;color:var(--subtext)">Run via
    <select data-via style="max-width:280px">${opts.join("")}</select></label>`;
  const sel = host.querySelector("[data-via]");
  sel.value = d.via || "";
  sel.addEventListener("change", async () => {
    try { await req(`/${conn}/exec`, { method: "PUT", body: { via: sel.value } }); ADMX.delete(conn); toast("Execution path saved"); }
    catch (e) { toast(e.message, "error", 5000); }
  });
}

// ============================================================ DNS
const TYPES = ["A", "AAAA", "CNAME", "MX", "TXT", "SRV", "NS", "PTR"];
const FIELDS = {
  A: [["address", "IPv4 address", "192.168.1.10"]],
  AAAA: [["address", "IPv6 address", "fd00::10"]],
  CNAME: [["target", "Target (FQDN)", "server.corp.local."]],
  NS: [["target", "Name server (FQDN)", "dc01.corp.local."]],
  PTR: [["target", "Hostname (FQDN)", "pc01.corp.local."]],
  MX: [["preference", "Priority", "10"], ["target", "Mail server (FQDN)", "mail.corp.local."]],
  TXT: [["text", "Text", "v=spf1 mx -all"]],
  SRV: [["priority", "Priority", "0"], ["weight", "Weight", "100"], ["port", "Port", "443"], ["target", "Target (FQDN)", "srv.corp.local."]],
};
const recText = (r) => {
  const v = r.value || {};
  switch (r.type) {
    case "A": case "AAAA": return v.address;
    case "MX": return `[${v.preference}] ${v.target}`;
    case "SRV": return `[${v.priority}] [${v.weight}] [${v.port}] ${v.target}`;
    case "TXT": return `"${v.text}"`;
    case "SOA": return `Serial ${v.serial}, ${v.primary}, ${v.admin}`;
    default: return v.target || v.raw || JSON.stringify(v);
  }
};

export async function drawDns(pane, conn) {
  pane.innerHTML = `<div style="padding:20px 26px;max-width:1300px;margin:0 auto">
    <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:14px">
      <h2 style="margin:0;font-size:20px;display:flex;align-items:center;gap:8px">${icon("globe", 20)} DNS</h2><span style="flex:1"></span><span data-exec></span></div>
    <div style="display:flex;gap:14px;align-items:flex-start;flex-wrap:wrap">
      <div style="${card};width:270px;flex:0 0 270px;padding:8px" data-zones><div style="padding:10px;color:var(--subtext)">Loading zones …</div></div>
      <div style="${card};flex:1;min-width:320px;padding:12px 14px" data-recs><div style="color:var(--subtext)">Select a zone on the left.</div></div>
    </div>
    <div style="font-size:11.5px;color:var(--subtext);margin-top:10px">AD-integrated zones only. The DNS server applies changes at the next AD sync (default ≈ 3 min; immediately: <code>dnscmd /zonereload</code> or restart the DNS service).</div></div>`;
  execBar(pane.querySelector("[data-exec]"), conn);
  const zEl = pane.querySelector("[data-zones]"), rEl = pane.querySelector("[data-recs]");
  let zones; try { zones = await core(conn, "dns_zones"); } catch (e) { zEl.innerHTML = errBox(e); return; }
  if (!zones.length) { zEl.innerHTML = `<div style="padding:10px;color:var(--subtext)">No AD-integrated zones found.</div>`; return; }
  let cur = null;
  const drawZones = () => {
    const grp = (rev) => zones.filter((z) => z.reverse === rev).map((z) => `<a data-z="${esc(z.name)}" style="display:block;padding:7px 9px;border-radius:8px;cursor:pointer;${cur === z.name ? "background:color-mix(in srgb,var(--accent) 16%,transparent);font-weight:600" : ""}">${esc(z.name)}<div style="font-size:11px;color:var(--subtext)">${esc(z.scope)}</div></a>`).join("");
    zEl.innerHTML = `<div style="font-size:10.5px;text-transform:uppercase;letter-spacing:.1em;color:var(--subtext);padding:6px 9px">Forward lookup zones</div>${grp(false) || `<div style="padding:4px 9px;color:var(--subtext)">—</div>`}
      <div style="font-size:10.5px;text-transform:uppercase;letter-spacing:.1em;color:var(--subtext);padding:10px 9px 6px">Reverse lookup zones</div>${grp(true) || `<div style="padding:4px 9px;color:var(--subtext)">—</div>`}`;
    zEl.querySelectorAll("[data-z]").forEach((a) => a.addEventListener("click", () => { cur = a.dataset.z; drawZones(); loadRecs(); }));
  };
  const loadRecs = async () => {
    rEl.innerHTML = `<div style="color:var(--subtext)">Loading records …</div>`;
    let recs; try { recs = await core(conn, "dns_records", { zone: cur }); } catch (e) { rEl.innerHTML = errBox(e); return; }
    rEl.innerHTML = `<div style="display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap">
        <b style="font-size:15px">${esc(cur)}</b><span style="color:var(--subtext)">${recs.length} records</span><span style="flex:1"></span>
        <input data-q placeholder="Filter …" style="width:180px"><select data-t><option value="">All types</option>${TYPES.concat("SOA").map((t) => `<option>${t}</option>`).join("")}</select>
        <button class="btn" data-reload>${icon("refresh", 14)}</button><button class="btn-primary" data-add>+ Record</button></div>
      <div style="overflow:auto"><table style="width:100%;border-collapse:collapse"><thead><tr style="text-align:left;color:var(--subtext);font-size:11.5px">
        <th style="padding:6px">Name</th><th>Type</th><th>Data</th><th>TTL</th><th></th></tr></thead><tbody data-body></tbody></table></div>`;
    const body = rEl.querySelector("[data-body]"), q = rEl.querySelector("[data-q]"), t = rEl.querySelector("[data-t]");
    const paint = () => {
      const f = q.value.trim().toLowerCase();
      const rows = recs.filter((r) => (!t.value || r.type === t.value) && (!f || `${r.name} ${recText(r)}`.toLowerCase().includes(f)));
      body.innerHTML = rows.map((r, i) => `<tr style="border-top:1px solid var(--border)">
        <td style="padding:6px;font-family:monospace">${esc(r.name === "@" ? "(same as parent folder)" : r.name)}</td>
        <td><span style="font-size:11px;font-weight:700;padding:2px 7px;border-radius:6px;background:color-mix(in srgb,var(--accent) 14%,transparent)">${esc(r.type)}</span></td>
        <td style="font-family:monospace;word-break:break-all">${esc(recText(r))}${r.static ? "" : ` <span title="dynamically registered" style="color:var(--subtext);font-size:11px">(dyn.)</span>`}</td>
        <td>${r.ttl}</td>
        <td style="white-space:nowrap;text-align:right">${TYPES.includes(r.type) ? `<button class="btn" data-ed="${recs.indexOf(r)}">${icon("edit", 13)}</button> <button class="btn" data-del="${recs.indexOf(r)}" style="color:var(--danger)">${icon("trash", 13)}</button>` : ""}</td></tr>`).join("")
        || `<tr><td colspan="5" style="padding:10px;color:var(--subtext)">No matches</td></tr>`;
      body.querySelectorAll("[data-ed]").forEach((b) => b.addEventListener("click", () => recDialog(conn, cur, recs[+b.dataset.ed], loadRecs)));
      body.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
        const r = recs[+b.dataset.del];
        if (!(await uiConfirm(`Delete ${r.type} record "${r.name}" -> ${recText(r)}?`))) return;
        try { await core(conn, "dns_delete", { zone: cur, name: r.name, rid: r.rid }); toast("Deleted"); loadRecs(); } catch (e) { toast(e.message, "error", 6000); }
      }));
    };
    q.addEventListener("input", paint); t.addEventListener("change", paint);
    rEl.querySelector("[data-reload]").addEventListener("click", loadRecs);
    rEl.querySelector("[data-add]").addEventListener("click", () => recDialog(conn, cur, null, loadRecs));
    paint();
  };
  cur = (zones.find((z) => !z.reverse) || zones[0]).name;
  drawZones(); loadRecs();
}

function recDialog(conn, zone, rec, after) {
  const ed = !!rec;
  let type = ed ? rec.type : "A";
  const ov = overlay(`<h3 style="margin:0 0 12px">${ed ? "Edit record" : "New DNS record"} <span style="color:var(--subtext);font-weight:400;font-size:13px">${esc(zone)}</span></h3>
    <div style="display:grid;grid-template-columns:150px 1fr;gap:8px 10px;align-items:center">
      <label>Type</label><select data-type ${ed ? "disabled" : ""}>${TYPES.map((t) => `<option ${t === type ? "selected" : ""}>${t}</option>`).join("")}</select>
      <label>Name</label><input data-name value="${esc(ed ? rec.name : "")}" ${ed ? "disabled" : ""} placeholder="e.g. server01 – empty = the zone itself (@)">
      <div data-fields style="display:contents"></div>
      <label>TTL (seconds)</label><input data-ttl type="number" min="0" value="${ed ? rec.ttl : 3600}">
    </div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px"><button class="btn" data-x>Cancel</button><button class="btn-primary" data-ok>${ed ? "Save" : "Create"}</button></div>`, 560);
  const fEl = ov.querySelector("[data-fields]");
  const paint = () => {
    fEl.innerHTML = FIELDS[type].map(([k, l, ph]) => `<label>${esc(l)}</label><input data-v="${k}" placeholder="${esc(ph)}" value="${esc(ed && rec.value[k] != null ? rec.value[k] : "")}">`).join("");
  };
  ov.querySelector("[data-type]").addEventListener("change", (e) => { type = e.target.value; paint(); });
  paint();
  ov.querySelector("[data-ok]").addEventListener("click", async () => {
    const value = {}; fEl.querySelectorAll("[data-v]").forEach((i) => { value[i.dataset.v] = i.value.trim(); });
    const ttl = parseInt(ov.querySelector("[data-ttl]").value || "3600", 10);
    try {
      if (ed) await core(conn, "dns_update", { zone, name: rec.name, rid: rec.rid, value, ttl });
      else await core(conn, "dns_add", { zone, name: ov.querySelector("[data-name]").value.trim() || "@", rtype: type, value, ttl });
      ov.remove(); toast("Saved"); after();
    } catch (e) { toast(e.message, "error", 7000); }
  });
}

// ============================================================ Group Policy
const ADMX = new Map();             // conn -> Katalog-Baum (Session-Cache)
async function admxTree(conn, refresh = false) {
  if (!refresh && ADMX.has(conn)) return ADMX.get(conn);
  const t = await core(conn, "admx_tree", { refresh });
  t.catById = Object.fromEntries(t.categories.map((c) => [c.id, c]));
  ADMX.set(conn, t);
  return t;
}
const statusTxt = (f) => ["Enabled", "User configuration disabled", "Computer configuration disabled", "All settings disabled"][f & 3];

export async function drawGpo(pane, conn) {
  pane.innerHTML = `<div style="padding:20px 26px;max-width:1300px;margin:0 auto">
    <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:14px">
      <h2 style="margin:0;font-size:20px;display:flex;align-items:center;gap:8px">${icon("scroll", 20)} Group Policy</h2><span style="flex:1"></span><span data-exec></span>
      <button class="btn" data-tab="targets">Links &amp; inheritance</button><button class="btn" data-tab="list">GPO list</button>
      <button class="btn-primary" data-new>+ New GPO</button></div>
    <div data-main><div style="color:var(--subtext)">Loading …</div></div></div>`;
  execBar(pane.querySelector("[data-exec]"), conn);
  const main = pane.querySelector("[data-main]");
  let data = null, tab = "list";
  const load = async () => {
    main.innerHTML = `<div style="color:var(--subtext)">Loading …</div>`;
    try { data = await core(conn, "gpo_list"); } catch (e) { main.innerHTML = errBox(e); return; }
    tab === "list" ? drawList() : drawTargets();
  };
  const drawList = () => {
    main.innerHTML = `<div style="${card};padding:4px 0;overflow:auto"><table style="width:100%;border-collapse:collapse">
      <thead><tr style="text-align:left;color:var(--subtext);font-size:11.5px"><th style="padding:8px 12px">Name</th><th>Status</th><th>Linked to</th><th>Version (C/U)</th><th>Changed</th></tr></thead>
      <tbody>${data.gpos.map((g, i) => `<tr data-g="${i}" style="border-top:1px solid var(--border);cursor:pointer">
        <td style="padding:8px 12px;font-weight:600"><span style="display:inline-flex;align-items:center;gap:6px">${icon("scroll", 14)} ${esc(g.name)}</span></td>
        <td style="${g.flags ? "color:var(--warning,#d97706)" : ""}">${esc(statusTxt(g.flags))}</td>
        <td>${g.links.length ? g.links.map((l) => `<span style="font-size:11.5px;${l.enabled ? "" : "opacity:.55;text-decoration:line-through"}">${l.enforced ? icon("lock", 12) + " " : ""}${esc(l.name)}</span>`).join(", ") : `<span style="color:var(--subtext)">—</span>`}</td>
        <td>${g.machine_version} / ${g.user_version}</td><td style="font-size:11.5px;color:var(--subtext)">${esc((g.changed || "").slice(0, 16))}</td></tr>`).join("")
        || `<tr><td colspan="5" style="padding:12px;color:var(--subtext)">No group policies.</td></tr>`}</tbody></table></div>`;
    main.querySelectorAll("[data-g]").forEach((tr) => tr.addEventListener("click", () => gpoDetail(conn, data.gpos[+tr.dataset.g], data, load)));
  };
  const drawTargets = () => {
    const nameOf = (guid) => (data.gpos.find((g) => g.guid === guid) || {}).name || guid;
    const tIcon = { domain: icon("globe", 15), ou: icon("ou", 15), site: icon("building", 15), other: icon("folder", 15) };
    main.innerHTML = data.targets.map((t, ti) => `<div style="${card};padding:10px 14px;margin-bottom:10px">
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap"><b style="display:inline-flex;align-items:center;gap:6px">${tIcon[t.kind] || icon("folder", 15)} ${esc(t.name)}</b>
        <span style="font-size:11px;color:var(--subtext);font-family:monospace">${esc(t.dn)}</span><span style="flex:1"></span>
        ${t.kind === "ou" || t.kind === "domain" ? `<label style="display:flex;gap:5px;align-items:center;font-size:12px"><input type="checkbox" data-block="${ti}" ${t.block ? "checked" : ""}> Block inheritance</label>` : ""}
        <button class="btn" data-link="${ti}">+ Link GPO</button></div>
      ${t.links.length ? `<table style="width:100%;border-collapse:collapse;margin-top:6px"><tbody>${t.links.slice().sort((a, b) => a.order - b.order).map((l) => `<tr style="border-top:1px solid var(--border)">
        <td style="padding:5px;width:40px;color:var(--subtext)">${l.order}</td><td><span style="display:inline-flex;align-items:center;gap:6px">${icon("scroll", 14)} ${esc(nameOf(l.guid))}</span></td>
        <td style="white-space:nowrap;text-align:right">
          <label style="font-size:12px"><input type="checkbox" data-en="${ti}|${l.guid}" ${l.enabled ? "checked" : ""}> enabled</label>
          <label style="font-size:12px;margin-left:8px"><input type="checkbox" data-enf="${ti}|${l.guid}" ${l.enforced ? "checked" : ""}> enforced</label>
          <button class="btn" data-up="${ti}|${l.guid}|${l.order}" ${l.order <= 1 ? "disabled" : ""} title="higher priority">▲</button>
          <button class="btn" data-down="${ti}|${l.guid}|${l.order}" ${l.order >= t.links.length ? "disabled" : ""} title="lower priority">▼</button>
          <button class="btn" data-unl="${ti}|${l.guid}" style="color:var(--danger)" title="Remove link">${icon("close", 13)}</button></td></tr>`).join("")}</tbody></table>`
        : `<div style="font-size:12px;color:var(--subtext);margin-top:4px">No linked GPOs</div>`}</div>`).join("");
    const act = async (fn) => { try { await fn(); await load(); } catch (e) { toast(e.message, "error", 6000); } };
    const sp = (s) => { const [ti, guid, o] = s.split("|"); return [data.targets[+ti].dn, guid, +o]; };
    main.querySelectorAll("[data-block]").forEach((c) => c.addEventListener("change", () => act(() => core(conn, "gpo_block", { target_dn: data.targets[+c.dataset.block].dn, block: c.checked }))));
    main.querySelectorAll("[data-en]").forEach((c) => c.addEventListener("change", () => { const [dn, g] = sp(c.dataset.en); act(() => core(conn, "gpo_link_set", { guid: g, target_dn: dn, enabled: c.checked })); }));
    main.querySelectorAll("[data-enf]").forEach((c) => c.addEventListener("change", () => { const [dn, g] = sp(c.dataset.enf); act(() => core(conn, "gpo_link_set", { guid: g, target_dn: dn, enforced: c.checked })); }));
    main.querySelectorAll("[data-up]").forEach((b) => b.addEventListener("click", () => { const [dn, g, o] = sp(b.dataset.up); act(() => core(conn, "gpo_link_set", { guid: g, target_dn: dn, order: o - 1 })); }));
    main.querySelectorAll("[data-down]").forEach((b) => b.addEventListener("click", () => { const [dn, g, o] = sp(b.dataset.down); act(() => core(conn, "gpo_link_set", { guid: g, target_dn: dn, order: o + 1 })); }));
    main.querySelectorAll("[data-unl]").forEach((b) => b.addEventListener("click", async () => {
      const [dn, g] = sp(b.dataset.unl);
      if (await uiConfirm(`Remove the link of "${nameOf(g)}" to ${dn}? (The GPO itself is kept.)`)) act(() => core(conn, "gpo_unlink", { guid: g, target_dn: dn }));
    }));
    main.querySelectorAll("[data-link]").forEach((b) => b.addEventListener("click", () => linkDialog(conn, data, data.targets[+b.dataset.link].dn, null, load)));
  };
  pane.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => { tab = b.dataset.tab; data ? (tab === "list" ? drawList() : drawTargets()) : load(); }));
  pane.querySelector("[data-new]").addEventListener("click", async () => {
    const ov = overlay(`<h3 style="margin:0 0 12px">New group policy</h3>
      <input data-n placeholder="Name, e.g. Company desktop wallpaper" style="width:100%">
      <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px"><button class="btn" data-x>Cancel</button><button class="btn-primary" data-ok>Create</button></div>`, 460);
    ov.querySelector("[data-n]").focus();
    ov.querySelector("[data-ok]").addEventListener("click", async () => {
      try { await core(conn, "gpo_create", { name: ov.querySelector("[data-n]").value.trim() }); ov.remove(); toast("GPO created"); load(); }
      catch (e) { toast(e.message, "error", 6000); }
    });
  });
  load();
}

function linkDialog(conn, data, targetDn, guid, after) {
  const ov = overlay(`<h3 style="margin:0 0 12px">Link GPO</h3>
    <div style="display:grid;grid-template-columns:120px 1fr;gap:8px 10px;align-items:center">
      <label>GPO</label><select data-g ${guid ? "disabled" : ""}>${data.gpos.map((g) => `<option value="${esc(g.guid)}" ${g.guid === guid ? "selected" : ""}>${esc(g.name)}</option>`).join("")}</select>
      <label>Target</label><select data-t ${targetDn ? "disabled" : ""}>${data.targets.map((t) => `<option value="${esc(t.dn)}" ${t.dn === targetDn ? "selected" : ""}>${esc(t.dn)}</option>`).join("")}</select>
      <label></label><label style="font-size:12.5px"><input type="checkbox" data-enf> Enforced</label>
    </div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px"><button class="btn" data-x>Cancel</button><button class="btn-primary" data-ok>Link</button></div>`, 620);
  ov.querySelector("[data-ok]").addEventListener("click", async () => {
    try {
      await core(conn, "gpo_link", { guid: ov.querySelector("[data-g]").value, target_dn: ov.querySelector("[data-t]").value, enforced: ov.querySelector("[data-enf]").checked });
      ov.remove(); toast("Linked"); after();
    } catch (e) { toast(e.message, "error", 6000); }
  });
}

// ------------------------------------------------------------ GPO-Detail
function gpoDetail(conn, gpo, data, reloadList) {
  const ov = overlay(`<div style="display:flex;gap:10px;align-items:center;margin-bottom:10px;flex-wrap:wrap">
      <h3 style="margin:0;font-size:17px;display:flex;align-items:center;gap:6px">${icon("scroll", 17)} <span data-title>${esc(gpo.name)}</span></h3><span style="font-size:11px;color:var(--subtext);font-family:monospace">${esc(gpo.guid)}</span>
      <span style="flex:1"></span><button class="btn" data-x>Close</button></div>
    <div style="display:flex;gap:4px;border-bottom:1px solid var(--border);margin-bottom:12px;flex-wrap:wrap" data-tabs>
      ${[["admx", "Administrative templates"], ["report", "Configured settings"], ["reg", "Registry values"], ["sec", "Security settings"], ["scope", "Scope & filtering"], ["gen", "General"]]
        .map(([k, l]) => `<a data-t="${k}" style="padding:8px 12px;cursor:pointer;border-bottom:2px solid transparent">${l}</a>`).join("")}</div>
    <div data-body style="min-height:420px"></div>`, 1180);
  const body = ov.querySelector("[data-body]");
  const close = ov.querySelector("[data-x]");
  close.addEventListener("click", () => reloadList());
  const show = (k) => {
    ov.querySelectorAll("[data-t]").forEach((a) => { a.style.borderBottomColor = a.dataset.t === k ? "var(--accent)" : "transparent"; a.style.fontWeight = a.dataset.t === k ? "700" : ""; });
    ({ admx: tabAdmx, report: tabReport, reg: tabReg, sec: tabSec, scope: tabScope, gen: tabGen })[k](body, conn, gpo, data, { ov, reloadList, show });
  };
  ov.querySelectorAll("[data-t]").forEach((a) => a.addEventListener("click", () => show(a.dataset.t)));
  show("admx");
}

// ---------- Administrative Vorlagen (dynamisch aus ADMX)
async function tabAdmx(body, conn, gpo) {
  body.innerHTML = `<div style="color:var(--subtext)">Loading administrative templates from the domain's ADMX files … (a few seconds the first time)</div>`;
  let t; try { t = await admxTree(conn); } catch (e) { body.innerHTML = errBox(e); return; }
  let side = "Machine", catSel = null, query = "";
  body.innerHTML = `<div style="display:flex;gap:10px;align-items:center;margin-bottom:10px;flex-wrap:wrap">
      <select data-side><option value="Machine">Computer configuration</option><option value="User">User configuration</option></select>
      <input data-q placeholder="Search all policies (e.g. wallpaper, screen saver, drive) …" style="flex:1;min-width:240px">
      <span style="font-size:11.5px;color:var(--subtext)">${t.policies.length} policies · ${esc(t.source)} · ${esc(t.lang || "")}</span>
      <button class="btn" data-refresh title="Reload ADMX">${icon("refresh", 14)}</button></div>
    ${t.errors?.length ? `<div style="font-size:11.5px;color:var(--warning,#d97706);margin-bottom:6px">${t.errors.length} ADMX file(s) not readable: ${esc(t.errors.slice(0, 3).join("; "))}</div>` : ""}
    <div style="display:flex;gap:12px;min-height:420px">
      <div data-tree style="width:340px;flex:0 0 340px;overflow:auto;max-height:62vh;border:1px solid var(--border);border-radius:10px;padding:6px"></div>
      <div data-list style="flex:1;overflow:auto;max-height:62vh;border:1px solid var(--border);border-radius:10px"></div></div>`;
  const treeEl = body.querySelector("[data-tree]"), listEl = body.querySelector("[data-list]");
  const visible = () => t.policies.filter((p) => p.class === "Both" || p.class === side);
  // Categories that (directly or indirectly) contain policies for this side
  const children = {};
  t.categories.forEach((c) => { (children[c.parent || ""] ||= []).push(c); });
  const roots = t.categories.filter((c) => !c.parent || !t.catById[c.parent]);
  const countCache = {};
  const count = (cid) => {
    if (countCache[cid] != null) return countCache[cid];
    countCache[cid] = 0;
    let n = visible().filter((p) => p.cat === cid).length;
    (children[cid] || []).forEach((c) => { n += count(c.id); });
    return (countCache[cid] = n);
  };
  const open = new Set();
  const drawTree = () => {
    Object.keys(countCache).forEach((k) => delete countCache[k]);
    const node = (c, depth) => {
      const n = count(c.id); if (!n) return "";
      const kids = (children[c.id] || []).slice().sort((a, b) => a.name.localeCompare(b.name));
      const has = kids.some((k) => count(k.id));
      const isOpen = open.has(c.id);
      return `<div><a data-c="${esc(c.id)}" style="display:flex;gap:6px;align-items:center;padding:4px 6px 4px ${6 + depth * 14}px;border-radius:6px;cursor:pointer;${catSel === c.id ? "background:color-mix(in srgb,var(--accent) 16%,transparent);font-weight:600" : ""}">
        <span style="width:12px;color:var(--subtext)">${has ? (isOpen ? "▾" : "▸") : ""}</span><span style="display:flex">${icon("folder", 14)}</span><span style="flex:1">${esc(c.name)}</span><span style="font-size:11px;color:var(--subtext)">${n}</span></a>
        ${has && isOpen ? kids.map((k) => node(k, depth + 1)).join("") : ""}</div>`;
    };
    treeEl.innerHTML = roots.slice().sort((a, b) => a.name.localeCompare(b.name)).map((c) => node(c, 0)).join("") || `<div style="padding:8px;color:var(--subtext)">No categories</div>`;
    treeEl.querySelectorAll("[data-c]").forEach((a) => a.addEventListener("click", () => {
      const id = a.dataset.c; open.has(id) && catSel === id ? open.delete(id) : open.add(id); catSel = id; query = ""; body.querySelector("[data-q]").value = ""; drawTree(); drawList();
    }));
  };
  const catPath = (cid) => { const out = []; const seen = new Set(); while (cid && t.catById[cid] && !seen.has(cid)) { seen.add(cid); out.unshift(t.catById[cid].name); cid = t.catById[cid].parent; } return out.join(" › "); };
  const drawList = () => {
    let rows;
    if (query) { const q = query.toLowerCase(); rows = visible().filter((p) => p.name.toLowerCase().includes(q)).slice(0, 300); }
    else rows = catSel ? visible().filter((p) => p.cat === catSel) : [];
    rows.sort((a, b) => a.name.localeCompare(b.name));
    listEl.innerHTML = rows.length ? rows.map((p) => `<a data-p="${esc(p.id)}" style="display:block;padding:8px 12px;border-bottom:1px solid var(--border);cursor:pointer">
        <div style="display:flex;align-items:center;gap:6px">${icon("file", 14)} ${esc(p.name)}</div>${query ? `<div style="font-size:11px;color:var(--subtext)">${esc(catPath(p.cat))}</div>` : ""}</a>`).join("")
      : `<div style="padding:14px;color:var(--subtext)">${query ? "No matches." : "Pick a category on the left or search above."}</div>`;
    listEl.querySelectorAll("[data-p]").forEach((a) => a.addEventListener("click", () => policyDialog(conn, gpo, a.dataset.p, side)));
  };
  body.querySelector("[data-side]").addEventListener("change", (e) => { side = e.target.value; drawTree(); drawList(); });
  let tm; body.querySelector("[data-q]").addEventListener("input", (e) => { clearTimeout(tm); tm = setTimeout(() => { query = e.target.value.trim(); drawList(); }, 180); });
  body.querySelector("[data-refresh]").addEventListener("click", async () => { ADMX.delete(conn); await admxTree(conn, true).catch(() => {}); tabAdmx(body, conn, gpo); });
  drawTree(); drawList();
}

function elementInput(el, val) {
  const id = `data-el="${esc(el.id)}"`;
  const dflt = val ?? el.default;
  switch (el.kind) {
    case "boolean": return `<label style="display:flex;gap:6px;align-items:center"><input type="checkbox" ${id} ${(val ?? el.default === "true") ? "checked" : ""}> ${esc(el.label)}</label>`;
    case "decimal": return `<label style="display:block">${esc(el.label)}<br><input type="number" ${id} min="${el.min}" max="${el.max}" value="${esc(dflt ?? "")}" style="width:180px"></label>`;
    case "text": return `<label style="display:block">${esc(el.label)}<br><input ${id} value="${esc(dflt ?? "")}" style="width:100%"></label>`;
    case "multiText": return `<label style="display:block">${esc(el.label)}<br><textarea ${id} rows="4" style="width:100%">${esc(Array.isArray(val) ? val.join("\n") : (val || ""))}</textarea></label>`;
    case "enum": return `<label style="display:block">${esc(el.label)}<br><select ${id}>${el.items.map((it, i) => `<option value="${i}" ${String(val ?? el.default ?? 0) === String(i) ? "selected" : ""}>${esc(it.label)}</option>`).join("")}</select></label>`;
    case "list": {
      const lines = (val || []).map((x) => (el.explicit ? `${x.name}=${x.value}` : x)).join("\n");
      return `<label style="display:block">${esc(el.label)} <span style="font-size:11px;color:var(--subtext)">(one line per entry${el.explicit ? ", format Name=Value" : ""})</span><br><textarea ${id} rows="5" style="width:100%;font-family:monospace">${esc(lines)}</textarea></label>`;
    }
    default: return "";
  }
}

function readElement(el, inp) {
  if (!inp) return undefined;
  switch (el.kind) {
    case "boolean": return inp.checked;
    case "decimal": return inp.value === "" ? "" : parseInt(inp.value, 10);
    case "enum": return parseInt(inp.value, 10);
    case "multiText": return inp.value.split("\n").map((s) => s.trim()).filter(Boolean);
    case "list": return inp.value.split("\n").map((s) => s.trim()).filter(Boolean).map((s) => {
      if (!el.explicit) return s; const i = s.indexOf("="); return i < 0 ? { name: s, value: "" } : { name: s.slice(0, i).trim(), value: s.slice(i + 1).trim() };
    });
    default: return inp.value;
  }
}

async function policyDialog(conn, gpo, pid, side, after) {
  const ov = overlay(`<div style="color:var(--subtext)">Loading policy …</div>`, 900);
  const box = ov.firstElementChild;
  let d; try { d = await core(conn, "admx_policy", { guid: gpo.guid, pid }); } catch (e) { box.innerHTML = errBox(e) + `<div style="text-align:right"><button class="btn" data-x2>Close</button></div>`; box.querySelector("[data-x2]").onclick = () => ov.remove(); return; }
  const p = d.policy, st = d.states[side] || Object.values(d.states)[0];
  if (!d.states[side]) side = Object.keys(d.states)[0];
  box.innerHTML = `<h3 style="margin:0 0 4px">${esc(p.name)}</h3>
    <div style="font-size:11.5px;color:var(--subtext);margin-bottom:12px">${side === "Machine" ? "Computer configuration" : "User configuration"} · ${esc(p.file)}.admx · ${esc(p.key)}${p.valueName ? "\\" + esc(p.valueName) : ""}</div>
    <div style="display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap">
      <div style="flex:1;min-width:300px">
        <div style="display:flex;flex-direction:column;gap:6px;margin-bottom:12px">
          ${[["not_configured", "Not configured"], ["enabled", "Enabled"], ["disabled", "Disabled"]].map(([k, l]) => `<label style="display:flex;gap:6px;align-items:center"><input type="radio" name="st" value="${k}" ${st.state === k ? "checked" : ""}> ${l}</label>`).join("")}
        </div>
        ${p.elements.length ? `<div data-opts style="border:1px solid var(--border);border-radius:10px;padding:10px 12px;display:flex;flex-direction:column;gap:10px">
          <div style="font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:var(--subtext)">Options</div>
          ${(p.labels || []).map((l) => `<div style="font-size:12px;color:var(--subtext)">${esc(l.label)}</div>`).join("")}
          ${p.elements.map((el) => elementInput(el, st.values[el.id])).join("")}</div>` : ""}
        ${p.supported ? `<div style="font-size:11.5px;color:var(--subtext);margin-top:10px">Supported on: ${esc(p.supported)}</div>` : ""}
      </div>
      <div style="flex:1;min-width:280px;max-height:52vh;overflow:auto;white-space:pre-wrap;font-size:12.5px;line-height:1.5;border-left:1px solid var(--border);padding-left:14px">${esc(p.explain || "No description.")}</div>
    </div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px"><button class="btn" data-x>Cancel</button><button class="btn-primary" data-ok>Apply</button></div>`;
  const opts = box.querySelector("[data-opts]");
  const sync = () => { if (opts) { const on = box.querySelector('input[name="st"]:checked').value === "enabled"; opts.style.opacity = on ? "1" : ".5"; opts.querySelectorAll("input,select,textarea").forEach((i) => { i.disabled = !on; }); } };
  box.querySelectorAll('input[name="st"]').forEach((r) => r.addEventListener("change", sync)); sync();
  box.querySelector("[data-x]").addEventListener("click", () => ov.remove());
  box.querySelector("[data-ok]").addEventListener("click", async () => {
    const state = box.querySelector('input[name="st"]:checked').value;
    const values = {};
    p.elements.forEach((el) => { const v = readElement(el, box.querySelector(`[data-el="${CSS.escape(el.id)}"]`)); if (v !== undefined) values[el.id] = v; });
    try { await core(conn, "admx_set", { guid: gpo.guid, pid, side, state, values }); ov.remove(); toast("Policy saved"); after?.(); }
    catch (e) { toast(e.message, "error", 7000); }
  });
}

// ---------- Report: everything that is configured
async function tabReport(body, conn, gpo, _data, ctx) {
  body.innerHTML = `<div style="color:var(--subtext)">Building report …</div>`;
  let r; try { r = await core(conn, "configured", { guid: gpo.guid }); } catch (e) { body.innerHTML = errBox(e); return; }
  const stTxt = { enabled: `${icon("check", 13)} Enabled`, disabled: `${icon("ban", 13)} Disabled` };
  const fmtVals = (v) => Object.entries(v || {}).map(([k, x]) => `${esc(k)}: ${esc(Array.isArray(x) ? x.map((y) => (typeof y === "object" ? `${y.name}=${y.value}` : y)).join(", ") : x)}`).join(" · ");
  const sec = (side, label) => `<h4 style="margin:14px 0 6px">${label}</h4>
    ${r[side].length ? r[side].map((p) => `<a data-p="${esc(p.id)}" data-side="${side}" style="display:block;padding:7px 10px;border:1px solid var(--border);border-radius:8px;margin-bottom:5px;cursor:pointer">
      <div style="display:flex;align-items:center;gap:6px"><b>${esc(p.name)}</b> <span style="font-size:12px;display:inline-flex;align-items:center;gap:3px">${stTxt[p.state] || p.state}</span></div>
      <div style="font-size:11px;color:var(--subtext)">${esc(p.path)}</div>${Object.keys(p.values || {}).length ? `<div style="font-size:11.5px;margin-top:2px">${fmtVals(p.values)}</div>` : ""}</a>`).join("")
      : `<div style="color:var(--subtext);font-size:12px">No administrative templates configured.</div>`}
    ${r.raw[side]?.length ? `<div style="font-size:12px;margin-top:6px"><b>Other registry values:</b> ${r.raw[side].map((e) => `<code>${esc(e.key)}\\${esc(e.name)}</code>`).join(", ")}</div>` : ""}`;
  const secTxt = Object.entries(r.security || {}).filter(([s]) => !["Unicode", "Version"].includes(s))
    .map(([s, kv]) => `<div style="margin-bottom:6px"><b>[${esc(s)}]</b><br>${Object.entries(kv).map(([k, v]) => `<code>${esc(k)} = ${esc(v)}</code>`).join("<br>")}</div>`).join("");
  body.innerHTML = `${r.admx_error ? `<div style="color:var(--warning,#d97706)">${esc(r.admx_error)}</div>` : ""}
    ${sec("Machine", "Computer configuration")}${sec("User", "User configuration")}
    <h4 style="margin:14px 0 6px">Security settings</h4>${secTxt || `<div style="color:var(--subtext);font-size:12px">None.</div>`}`;
  body.querySelectorAll("[data-p]").forEach((a) => a.addEventListener("click", () => policyDialog(conn, gpo, a.dataset.p, a.dataset.side, () => ctx.show("report"))));
}

// ---------- Registry.pol direkt
async function tabReg(body, conn, gpo, _d, ctx) {
  body.innerHTML = `<div style="color:var(--subtext)">Loading …</div>`;
  let r; try { r = await core(conn, "registry_list", { guid: gpo.guid }); } catch (e) { body.innerHTML = errBox(e); return; }
  const fmt = (v) => (Array.isArray(v) ? v.join(" | ") : String(v));
  const table = (side) => `<h4 style="margin:12px 0 6px">${side === "Machine" ? "HKLM (computer)" : "HKCU (user)"}</h4>
    <table style="width:100%;border-collapse:collapse;font-size:12.5px"><tbody>${r[side].map((e, i) => `<tr style="border-top:1px solid var(--border)">
      <td style="padding:5px;font-family:monospace;word-break:break-all">${esc(e.key)}</td><td style="font-family:monospace">${esc(e.name)}</td><td>${esc(e.type)}</td>
      <td style="font-family:monospace;word-break:break-all">${esc(fmt(e.value))}</td>
      <td style="text-align:right;white-space:nowrap"><button class="btn" data-e="${side}|${i}">${icon("edit", 13)}</button> <button class="btn" data-d="${side}|${i}" style="color:var(--danger)">${icon("trash", 13)}</button></td></tr>`).join("")
      || `<tr><td style="padding:6px;color:var(--subtext)">No values</td></tr>`}</tbody></table>`;
  body.innerHTML = `<div style="display:flex;gap:8px;align-items:center"><span style="font-size:12px;color:var(--subtext);flex:1">Custom registry values (e.g. for programs without ADMX). <code>**del.Name</code> = delete a value, <code>**delvals.</code> = delete all values of the key.</span>
    <button class="btn-primary" data-add>+ Value</button></div>${table("Machine")}${table("User")}`;
  const edit = (side, e) => {
    const ov = overlay(`<h3 style="margin:0 0 12px">${e ? "Edit registry value" : "New registry value"}</h3>
      <div style="display:grid;grid-template-columns:120px 1fr;gap:8px 10px;align-items:center">
        <label>Scope</label><select data-s ${e ? "disabled" : ""}><option value="Machine">Computer (HKLM)</option><option value="User">User (HKCU)</option></select>
        <label>Key</label><input data-k value="${esc(e?.key || "")}" placeholder="Software\\Policies\\Company\\App" ${e ? "disabled" : ""}>
        <label>Value name</label><input data-n value="${esc(e?.name || "")}" ${e ? "disabled" : ""}>
        <label>Type</label><select data-t>${["REG_SZ", "REG_EXPAND_SZ", "REG_DWORD", "REG_QWORD", "REG_MULTI_SZ", "REG_BINARY"].map((t) => `<option ${e?.type === t ? "selected" : ""}>${t}</option>`).join("")}</select>
        <label>Value</label><textarea data-v rows="3" placeholder="MULTI_SZ: one line per value · BINARY: hex">${esc(e ? (Array.isArray(e.value) ? e.value.join("\n") : String(e.value)) : "")}</textarea>
      </div><div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px"><button class="btn" data-x>Cancel</button><button class="btn-primary" data-ok>Save</button></div>`, 620);
    ov.querySelector("[data-s]").value = side;
    ov.querySelector("[data-ok]").addEventListener("click", async () => {
      const typ = ov.querySelector("[data-t]").value; let v = ov.querySelector("[data-v]").value;
      if (typ === "REG_MULTI_SZ") v = v.split("\n").filter(Boolean);
      try {
        await core(conn, "registry_set", { guid: gpo.guid, side: ov.querySelector("[data-s]").value, key: ov.querySelector("[data-k]").value.trim(), name: ov.querySelector("[data-n]").value.trim(), typ, value: v });
        ov.remove(); toast("Saved"); ctx.show("reg");
      } catch (err) { toast(err.message, "error", 6000); }
    });
  };
  body.querySelector("[data-add]").addEventListener("click", () => edit("Machine", null));
  body.querySelectorAll("[data-e]").forEach((b) => b.addEventListener("click", () => { const [s, i] = b.dataset.e.split("|"); edit(s, r[s][+i]); }));
  body.querySelectorAll("[data-d]").forEach((b) => b.addEventListener("click", async () => {
    const [s, i] = b.dataset.d.split("|"); const e = r[s][+i];
    if (!(await uiConfirm(`Remove ${e.key}\\${e.name} from the GPO?`))) return;
    try { await core(conn, "registry_set", { guid: gpo.guid, side: s, key: e.key, name: e.name, typ: e.type, value: "", delete: true }); ctx.show("reg"); } catch (err) { toast(err.message, "error", 6000); }
  }));
}

// ---------- GptTmpl.inf (Kennwort-/Kontorichtlinien, Benutzerrechte, Sicherheitsoptionen, Dienste ...)
const SEC_HINTS = {
  "System Access": ["MinimumPasswordLength", "PasswordComplexity", "MaximumPasswordAge", "MinimumPasswordAge", "PasswordHistorySize", "LockoutBadCount", "ResetLockoutCount", "LockoutDuration", "ClearTextPassword", "EnableGuestAccount", "EnableAdminAccount"],
  "Event Audit": ["AuditSystemEvents", "AuditLogonEvents", "AuditObjectAccess", "AuditPrivilegeUse", "AuditPolicyChange", "AuditAccountManage", "AuditProcessTracking", "AuditDSAccess", "AuditAccountLogon"],
  "Privilege Rights": ["SeInteractiveLogonRight", "SeDenyInteractiveLogonRight", "SeRemoteInteractiveLogonRight", "SeDenyRemoteInteractiveLogonRight", "SeShutdownPrivilege", "SeBackupPrivilege", "SeRestorePrivilege", "SeSystemtimePrivilege", "SeNetworkLogonRight", "SeDenyNetworkLogonRight"],
  "Registry Values": ["MACHINE\\System\\CurrentControlSet\\Control\\Lsa\\LimitBlankPasswordUse", "MACHINE\\Software\\Microsoft\\Windows\\CurrentVersion\\Policies\\System\\DontDisplayLastUserName", "MACHINE\\Software\\Microsoft\\Windows\\CurrentVersion\\Policies\\System\\LegalNoticeCaption", "MACHINE\\Software\\Microsoft\\Windows\\CurrentVersion\\Policies\\System\\LegalNoticeText", "MACHINE\\Software\\Microsoft\\Windows\\CurrentVersion\\Policies\\System\\InactivityTimeoutSecs"],
  "Service General Setting": [],
};
async function tabSec(body, conn, gpo, _d, ctx) {
  body.innerHTML = `<div style="color:var(--subtext)">Loading …</div>`;
  let s; try { s = await core(conn, "security_get", { guid: gpo.guid }); } catch (e) { body.innerHTML = errBox(e); return; }
  const sections = Object.keys(s).filter((k) => !["Unicode", "Version"].includes(k));
  body.innerHTML = `<div style="display:flex;gap:8px;align-items:center;margin-bottom:8px"><span style="flex:1;font-size:12px;color:var(--subtext)">Computer configuration › Windows settings › Security settings (GptTmpl.inf). User rights as <code>*SID,*SID</code>, registry values as <code>type,value</code> (4 = DWORD, 1 = text).</span>
    <button class="btn-primary" data-add>+ Setting</button></div>
    ${sections.length ? sections.map((sec) => `<div style="border:1px solid var(--border);border-radius:10px;padding:8px 12px;margin-bottom:8px"><b>[${esc(sec)}]</b>
      <table style="width:100%;border-collapse:collapse;font-size:12.5px;margin-top:4px"><tbody>${Object.entries(s[sec]).map(([k, v]) => `<tr style="border-top:1px solid var(--border)">
        <td style="padding:4px;font-family:monospace;word-break:break-all">${esc(k)}</td><td style="font-family:monospace;word-break:break-all">${esc(v)}</td>
        <td style="text-align:right;white-space:nowrap"><button class="btn" data-e="${esc(sec)}|${esc(k)}">${icon("edit", 13)}</button> <button class="btn" data-d="${esc(sec)}|${esc(k)}" style="color:var(--danger)">${icon("trash", 13)}</button></td></tr>`).join("")}</tbody></table></div>`).join("")
      : `<div style="color:var(--subtext)">No security settings in this GPO.</div>`}`;
  const edit = (sec, key, val) => {
    const ov = overlay(`<h3 style="margin:0 0 12px">Security setting</h3>
      <div style="display:grid;grid-template-columns:110px 1fr;gap:8px 10px;align-items:center">
        <label>Section</label><input data-s list="gpo-sec-s" value="${esc(sec || "System Access")}"><datalist id="gpo-sec-s">${Object.keys(SEC_HINTS).map((k) => `<option value="${esc(k)}">`).join("")}</datalist>
        <label>Name</label><input data-k list="gpo-sec-k" value="${esc(key || "")}"><datalist id="gpo-sec-k"></datalist>
        <label>Value</label><input data-v value="${esc(val ?? "")}">
      </div><div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px"><button class="btn" data-x>Cancel</button><button class="btn-primary" data-ok>Save</button></div>`, 640);
    const fill = () => { ov.querySelector("#gpo-sec-k").innerHTML = (SEC_HINTS[ov.querySelector("[data-s]").value] || []).map((k) => `<option value="${esc(k)}">`).join(""); };
    ov.querySelector("[data-s]").addEventListener("input", fill); fill();
    ov.querySelector("[data-ok]").addEventListener("click", async () => {
      try { await core(conn, "security_set", { guid: gpo.guid, section: ov.querySelector("[data-s]").value.trim(), key: ov.querySelector("[data-k]").value.trim(), value: ov.querySelector("[data-v]").value.trim() }); ov.remove(); toast("Saved"); ctx.show("sec"); }
      catch (e) { toast(e.message, "error", 6000); }
    });
  };
  body.querySelector("[data-add]").addEventListener("click", () => edit(null, null, null));
  body.querySelectorAll("[data-e]").forEach((b) => b.addEventListener("click", () => { const [sec, k] = b.dataset.e.split("|"); edit(sec, k, s[sec][k]); }));
  body.querySelectorAll("[data-d]").forEach((b) => b.addEventListener("click", async () => {
    const [sec, k] = b.dataset.d.split("|");
    if (!(await uiConfirm(`Remove ${k}?`))) return;
    try { await core(conn, "security_set", { guid: gpo.guid, section: sec, key: k, value: "", delete: true }); ctx.show("sec"); } catch (e) { toast(e.message, "error", 6000); }
  }));
}

// ---------- Scope: links + security filtering
async function tabScope(body, conn, gpo, data, ctx) {
  body.innerHTML = `<div style="color:var(--subtext)">Loading …</div>`;
  let flt; try { flt = await core(conn, "gpo_filter", { guid: gpo.guid }); } catch (e) { flt = null; body.innerHTML = errBox(e); }
  const g = data.gpos.find((x) => x.guid === gpo.guid) || gpo;
  body.innerHTML = `<h4 style="margin:0 0 6px">Links</h4>
    ${g.links.length ? g.links.map((l) => `<div style="padding:6px 10px;border:1px solid var(--border);border-radius:8px;margin-bottom:5px">${l.enforced ? icon("lock", 13) + " " : ""}${esc(l.name)} <span style="font-size:11px;color:var(--subtext)">${esc(l.target)} · order ${l.order}${l.enabled ? "" : " · disabled"}</span></div>`).join("") : `<div style="color:var(--subtext);font-size:12px">Not linked.</div>`}
    <button class="btn" data-link style="margin-top:4px">+ Link</button>
    <h4 style="margin:18px 0 6px">Security filtering</h4>
    <div style="font-size:12px;color:var(--subtext);margin-bottom:6px">The GPO applies only to these users, groups and computers (right "Apply Group Policy").</div>
    <div data-flt>${flt ? (flt.map((f) => `<div style="display:flex;gap:8px;align-items:center;padding:5px 10px;border:1px solid var(--border);border-radius:8px;margin-bottom:4px"><span style="flex:1;display:inline-flex;align-items:center;gap:6px">${icon("users", 14)} ${esc(f.name)} <span style="font-size:11px;color:var(--subtext)">${esc(f.sid)}</span></span><button class="btn" data-rm="${esc(f.sid)}" style="color:var(--danger)">Remove</button></div>`).join("") || `<div style="color:var(--warning,#d97706);font-size:12px">Nobody – the GPO is not applied to any object.</div>`) : ""}</div>
    <div style="display:flex;gap:6px;margin-top:6px"><input data-p placeholder="sAMAccountName (group, user, PC$) or SID" style="flex:1"><button class="btn-primary" data-addf>Add</button></div>`;
  body.querySelector("[data-link]").addEventListener("click", () => linkDialog(conn, data, null, gpo.guid, async () => { const d = await core(conn, "gpo_list"); Object.assign(data, d); ctx.show("scope"); }));
  const set = async (principal, apply) => { try { await core(conn, "gpo_filter_set", { guid: gpo.guid, principal, apply }); toast("Security filter changed"); ctx.show("scope"); } catch (e) { toast(e.message, "error", 7000); } };
  body.querySelectorAll("[data-rm]").forEach((b) => b.addEventListener("click", async () => { if (await uiConfirm("Remove from security filtering?")) set(b.dataset.rm, false); }));
  body.querySelector("[data-addf]").addEventListener("click", () => { const v = body.querySelector("[data-p]").value.trim(); if (v) set(v, true); });
}

// ---------- General: name, status, delete
function tabGen(body, conn, gpo, _d, ctx) {
  body.innerHTML = `<div style="display:grid;grid-template-columns:160px 1fr;gap:10px 12px;align-items:center;max-width:720px">
      <label>Name</label><div style="display:flex;gap:6px"><input data-n value="${esc(gpo.name)}" style="flex:1"><button class="btn" data-ren>Rename</button></div>
      <label>GPO status</label><select data-s>${[0, 1, 2, 3].map((f) => `<option value="${f}" ${gpo.flags === f ? "selected" : ""}>${esc(statusTxt(f))}</option>`).join("")}</select>
      <label>Version</label><div>Computer ${gpo.machine_version} · user ${gpo.user_version}</div>
      <label>Created</label><div>${esc(gpo.created || "")}</div>
      <label>Unique ID</label><code>${esc(gpo.guid)}</code>
    </div>
    <div style="margin-top:24px;padding-top:14px;border-top:1px solid var(--border)"><button class="btn" data-del style="color:var(--danger)">${icon("trash", 13)} Delete GPO</button>
      <span style="font-size:12px;color:var(--subtext);margin-left:8px">Removes all links, the AD object and the SYSVOL folder.</span></div>`;
  body.querySelector("[data-ren]").addEventListener("click", async () => {
    const n = body.querySelector("[data-n]").value.trim();
    try { await core(conn, "gpo_rename", { guid: gpo.guid, name: n }); gpo.name = n; ctx.ov.querySelector("[data-title]").textContent = n; toast("Renamed"); } catch (e) { toast(e.message, "error", 6000); }
  });
  body.querySelector("[data-s]").addEventListener("change", async (e) => {
    try { await core(conn, "gpo_status", { guid: gpo.guid, flags: +e.target.value }); gpo.flags = +e.target.value; toast("Status set"); } catch (err) { toast(err.message, "error", 6000); }
  });
  body.querySelector("[data-del]").addEventListener("click", async () => {
    if (!(await uiConfirm(`Permanently delete GPO "${gpo.name}"? This cannot be undone.`))) return;
    try { await core(conn, "gpo_delete", { guid: gpo.guid }); ctx.ov.remove(); toast("GPO deleted"); ctx.reloadList(); } catch (e) { toast(e.message, "error", 7000); }
  });
}
