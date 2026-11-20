// app.js (module "tenant-link") - manage which main RMMs may embed this backend
// -----------------------------------------------------------------------------
// This backend is a standalone RMM. "Tenant Sharing" records which main RMMs are
// approved to embed/open it (pairing with confirmation) and keeps a revocable
// trust record per partner. The admin either issues a one-time pairing code or
// approves an incoming request. Active pairings drive the frame-ancestors header.

import { getToken } from "/js/app/api.js";
import { esc, uiConfirm, uiPrompt } from "/js/app/utils.js";
import { icon } from "./icons.js";

async function tq(path, opts = {}) {
  const headers = { Authorization: "Bearer " + (getToken() || "") };
  if (opts.body !== undefined && typeof opts.body !== "string") { headers["Content-Type"] = "application/json"; opts = { ...opts, body: JSON.stringify(opts.body) }; }
  const res = await fetch("/api/tenant-link" + path, { ...opts, headers });
  if (!res.ok) { let m = res.statusText; const code = res.status; try { const j = await res.json(); m = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch { /* ignore */ } const e = new Error(m); e.status = code; throw e; }
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : res;
}

const STATUS = {
  active: { label: "Active", color: "var(--success, #22c55e)" },
  pending: { label: "Pending", color: "var(--warning, #f59e0b)" },
  revoked: { label: "Revoked", color: "var(--danger, #ef4444)" },
};

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); window.notify?.("Copied", "success", 1200); }
  catch { window.notify?.("Copy failed - select manually", "error", 3000); }
}

const ago = (ts) => {
  if (!ts) return "never";
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + " min ago";
  if (s < 86400) return Math.floor(s / 3600) + " h ago";
  return Math.floor(s / 86400) + " d ago";
};

export async function renderTenantLink(body) {
  body.innerHTML = `
    <div style="display:flex;flex-direction:column;height:100%;min-height:0;font-size:13px">
      <div style="display:flex;align-items:center;gap:8px;padding:10px 14px;border-bottom:1px solid var(--border)">
        <b style="font-size:15px;display:inline-flex;align-items:center;gap:7px">${icon("shield", 17)} Tenant Sharing</b>
        <span data-name style="font-size:12px;color:var(--subtext)"></span>
        <span style="flex:1"></span>
        <button class="btn btn-sm" data-refresh title="Refresh">${icon("refresh", 13)}</button>
        <button class="btn-primary btn-sm" data-add>${icon("plus", 13)} New pairing code</button>
      </div>
      <div data-status style="padding:10px 14px;border-bottom:1px solid var(--border);font-size:12px;color:var(--subtext)"></div>
      <div data-list style="flex:1;min-height:0;overflow:auto;padding:12px 14px"></div>
    </div>`;
  const listEl = body.querySelector("[data-list]");
  const statusEl = body.querySelector("[data-status]");
  const nameEl = body.querySelector("[data-name]");

  const load = async () => {
    let d;
    try { d = await tq("/admin/pairings"); }
    catch (e) { listEl.innerHTML = `<div style="color:var(--danger)">${esc(e.message)}</div>`; return; }
    nameEl.textContent = d.name ? "· " + d.name : "";
    const anc = d.frame_ancestors || [];
    statusEl.innerHTML = anc.length
      ? `${icon("check", 13)} Embeddable by <b>${anc.length}</b> approved main RMM${anc.length > 1 ? "s" : ""}: ${anc.map((a) => `<code style="background:var(--panel-2);padding:1px 5px;border-radius:4px">${esc(a)}</code>`).join(" ")}`
      : `${icon("warn", 13)} No main RMM approved yet. Issue a pairing code or approve a request below.`;
    drawList(d.pairings || []);
  };

  const drawList = (ps) => {
    if (!ps.length) { listEl.innerHTML = `<div style="color:var(--subtext)">No pairings yet. Click <b>New pairing code</b> and enter the code in the main RMM's tenant settings.</div>`; return; }
    ps.sort((a, b) => (b.created || 0) - (a.created || 0));
    listEl.innerHTML = ps.map((p) => {
      const st = STATUS[p.status] || STATUS.pending;
      const showCode = p.status === "pending" && p.mode === "code" && p.has_code;
      return `<div style="border:1px solid var(--border);border-radius:10px;padding:10px 12px;margin-bottom:8px">
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
          <span style="width:9px;height:9px;border-radius:50%;background:${st.color};display:inline-block"></span>
          <b>${esc(p.name || "Main RMM")}</b>
          <span style="font-size:11px;padding:1px 7px;border-radius:999px;background:var(--panel-2);color:${st.color}">${st.label}</span>
          <span style="font-size:11px;color:var(--subtext)">${p.mode === "request" ? "requested" : "code"}</span>
          <span style="flex:1"></span>
          ${p.status === "pending" ? `<button class="btn btn-sm btn-primary" data-approve="${esc(p.id)}">${icon("check", 12)} Approve</button>` : ""}
          ${p.status === "active" ? `<button class="btn btn-sm" data-revoke="${esc(p.id)}">${icon("warn", 12)} Revoke</button>` : ""}
          ${p.status === "revoked" ? `<button class="btn btn-sm" data-approve="${esc(p.id)}">${icon("check", 12)} Re-approve</button>` : ""}
          <button class="btn btn-sm btn-danger" data-del="${esc(p.id)}" title="Remove">${icon("trash", 12)}</button>
        </div>
        <div style="font-size:11.5px;color:var(--subtext);margin-top:5px;display:flex;gap:14px;flex-wrap:wrap">
          ${p.parent_url ? `<span>${icon("link", 12)} ${esc(p.parent_url)}</span>` : `<span style="opacity:.7">address set on claim</span>`}
          <span>${icon("clock", 12)} last seen ${esc(ago(p.last_seen))}</span>
        </div>
        ${showCode ? `<div style="margin-top:8px;display:flex;align-items:center;gap:8px;background:var(--panel-2);border-radius:8px;padding:8px 10px">
          <span style="font-size:11px;color:var(--subtext)">Pairing code</span>
          <code style="font-size:15px;letter-spacing:2px;font-weight:700">${esc((p.code.match(/.{1,4}/g) || [p.code]).join("-"))}</code>
          <button class="btn btn-sm" data-copy="${esc(p.code)}">${icon("copy", 12)} Copy</button>
          <span style="flex:1"></span>
          <span style="font-size:11px;color:var(--subtext)">valid ~30 min</span></div>` : ""}
      </div>`;
    }).join("");
    listEl.querySelectorAll("[data-approve]").forEach((b) => b.addEventListener("click", async () => {
      try { await tq(`/admin/pairings/${b.dataset.approve}/approve`, { method: "POST" }); window.notify?.("Pairing approved", "success"); load(); } catch (e) { window.notify?.(e.message, "error"); }
    }));
    listEl.querySelectorAll("[data-revoke]").forEach((b) => b.addEventListener("click", async () => {
      if (!(await uiConfirm("Revoke this pairing? The main RMM can no longer open this backend until re-approved.", { okText: "Revoke", danger: true }))) return;
      try { await tq(`/admin/pairings/${b.dataset.revoke}/revoke`, { method: "POST" }); load(); } catch (e) { window.notify?.(e.message, "error"); }
    }));
    listEl.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
      if (!(await uiConfirm("Remove this pairing permanently?", { okText: "Remove", danger: true }))) return;
      try { await tq(`/admin/pairings/${b.dataset.del}`, { method: "DELETE" }); load(); } catch (e) { window.notify?.(e.message, "error"); }
    }));
    listEl.querySelectorAll("[data-copy]").forEach((b) => b.addEventListener("click", () => copyText(b.dataset.copy)));
  };

  body.querySelector("[data-refresh]").addEventListener("click", load);
  body.querySelector("[data-add]").addEventListener("click", async () => {
    const name = await uiPrompt("Label for this pairing", { description: "A name to recognize the main RMM, e.g. \"HQ RMM\"." });
    if (name === null) return;
    let r;
    try { r = await tq("/admin/pairings", { method: "POST", body: { name: name || "" } }); }
    catch (e) { window.notify?.(e.message, "error"); return; }
    const code = r.pairing.code || "";
    const grouped = (code.match(/.{1,4}/g) || [code]).join("-");
    const ov = overlay(`<b style="font-size:15px;display:flex;align-items:center;gap:7px">${icon("link", 16)} Pairing code created</b>
      <div style="font-size:12px;color:var(--subtext);margin:8px 0 12px">Open the <b>Tenants</b> app in your main RMM, add this backend, and paste this code to pair. The code is valid for about 30 minutes and can be used once.</div>
      <div style="display:flex;align-items:center;gap:10px;background:var(--panel-2);border-radius:10px;padding:14px 16px;justify-content:center">
        <code style="font-size:24px;letter-spacing:4px;font-weight:800">${esc(grouped)}</code>
        <button class="btn btn-sm" data-copy>${icon("copy", 13)} Copy</button></div>
      <div style="text-align:right;margin-top:14px"><button class="btn-primary" data-x>Done</button></div>`);
    ov.querySelector("[data-copy]").addEventListener("click", () => copyText(code));
    ov.querySelector("[data-x]").addEventListener("click", () => { ov.remove(); load(); });
    load();
  });

  load();
}

function overlay(html, width = 480) {
  const ov = document.createElement("div");
  ov.style.cssText = "position:fixed;inset:0;z-index:9500;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;padding:12px";
  ov.innerHTML = `<div style="background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:16px 18px;width:${width}px;max-width:100%;max-height:calc(100dvh - 24px);overflow:auto;font-size:13px">${html}</div>`;
  document.body.appendChild(ov);
  ov.addEventListener("click", (e) => { if (e.target === ov) ov.remove(); });
  return ov;
}
