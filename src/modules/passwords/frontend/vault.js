// vault.js (module "passwords") - the whole password-manager UI.
// Called by app.js with a context + helpers. All crypto in the browser.
import { getToken } from "/js/app/api.js";
import { icon } from "./icons.js";

export function mount(root, ctx, H) {
  const { api, C, toast, state, findClient, esc, uiConfirm, uiPrompt } = H;
  let curVault = null, curGroup = "", curEntry = null, data = null, clip = null;

  const token = () => getToken() || "";
  async function rawFetch(path, opts) {               // for file up-/download
    const r = await fetch("/api/pm" + path, { ...opts, headers: { Authorization: "Bearer " + token(), ...(opts.headers || {}) } });
    if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail || m; } catch {} throw new Error(m); }
    return r;
  }
  const vkey = (vid) => ctx.vaultKeys.get(vid);

  // ============================================================ Unlock / Setup
  async function boot() {
    root.innerHTML = `<div style="padding:40px;text-align:center;color:var(--subtext)">Loading …</div>`;
    try { ctx.account = await api("/account"); } catch (e) { root.innerHTML = err(e); return; }
    ctx.account.exists ? lockScreen() : setupScreen();
  }

  function shell(inner, max = 420) {
    root.innerHTML = `<div style="display:grid;place-items:center;height:100%;padding:20px">
      <div style="width:${max}px;max-width:100%;background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:26px 24px">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">
          <div style="width:34px;height:34px;border-radius:9px;background:linear-gradient(135deg,var(--accent),var(--accent-2,#38bdf8));display:grid;place-items:center;color:#fff">${icon("key", 18)}</div>
          <div><div style="font-weight:800;font-size:16px">Passwords</div><div style="font-size:11.5px;color:var(--subtext)">End-to-end encrypted</div></div></div>
        ${inner}</div></div>`;
  }

  function setupScreen() {
    shell(`<p style="font-size:12.5px;color:var(--subtext);margin:0 0 14px">Set a <b>master password</b>. It encrypts all of your passwords. It is <b>not stored</b> and cannot be recovered - choose it well and remember it.</p>
      <input id="p1" type="password" placeholder="Master password" style="width:100%;margin-bottom:8px">
      <input id="p2" type="password" placeholder="Repeat" style="width:100%;margin-bottom:6px">
      <div id="meter" style="height:5px;border-radius:3px;background:var(--border);margin-bottom:12px"></div>
      <button class="btn-primary" id="go" style="width:100%">Set up</button>
      <div id="msg" style="color:var(--danger);font-size:12px;margin-top:8px"></div>`);
    const p1 = root.querySelector("#p1"), p2 = root.querySelector("#p2");
    p1.focus();
    p1.addEventListener("input", () => { const s = C.strength(p1.value); root.querySelector("#meter").style.background = ["var(--border)", "#ef4444", "#f59e0b", "#eab308", "#22c55e", "#16a34a"][s + 1]; });
    root.querySelector("#go").addEventListener("click", async () => {
      const msg = root.querySelector("#msg");
      if (p1.value.length < 8) { msg.textContent = "At least 8 characters."; return; }
      if (p1.value !== p2.value) { msg.textContent = "Passwords do not match."; return; }
      try {
        const acc = await C.createAccount(p1.value);
        await api("/account", { method: "POST", body: JSON.stringify({ salt: acc.salt, iters: acc.iters, public_key: acc.public_key, enc_private_key: acc.enc_private_key, verifier: acc.verifier }) });
        ctx.session = await C.unlock(p1.value, { salt: acc.salt, iters: acc.iters, public_key: acc.public_key, enc_private_key: acc.enc_private_key, verifier: acc.verifier });
        // Create the personal vault
        const vk = await C.newVaultKey();
        await api("/vaults", { method: "POST", body: JSON.stringify({ name_enc: await C.encText(vk, "Personal"), wrapped_key: await C.wrapVaultKey(vk, acc.public_key) }) });
        toast("Password manager set up");
        await afterUnlock();
      } catch (e) { msg.textContent = e.message; }
    });
  }

  function lockScreen(errMsg) {
    shell(`<p style="font-size:12.5px;color:var(--subtext);margin:0 0 14px">Enter your master password to open the vault.</p>
      <input id="mp" type="password" placeholder="Master password" style="width:100%;margin-bottom:12px">
      <button class="btn-primary" id="go" style="width:100%">Unlock</button>
      <div id="msg" style="color:var(--danger);font-size:12px;margin-top:8px">${errMsg ? esc(errMsg) : ""}</div>`);
    const mp = root.querySelector("#mp"); mp.focus();
    const submit = async () => {
      try { ctx.session = await C.unlock(mp.value, ctx.account); await afterUnlock(); }
      catch (e) { root.querySelector("#msg").textContent = e.message; mp.select(); }
    };
    root.querySelector("#go").addEventListener("click", submit);
    mp.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
  }

  function lock() {
    ctx.session = null; ctx.vaultKeys.clear(); curVault = null; data = null; curEntry = null;
    if (clip) { clearTimeout(clip); clip = null; }
    boot();
  }

  async function afterUnlock() {
    await loadVaults();
    // When opened from the client button: pre-select the matching entry/vault
    if (ctx.focusClient) { renderMain(); openClientLookup(ctx.focusClient); }
    else { curVault = ctx.vaults[0]?.id || null; renderMain(); if (curVault) openVault(curVault); }
  }

  async function loadVaults() {
    const r = await api("/vaults");
    ctx.vaults = [];
    for (const v of r.vaults) {
      try {
        const vk = await C.unwrapVaultKey(v.wrapped_key, ctx.session.privateKey);
        ctx.vaultKeys.set(v.id, vk);
        v.name = await C.decText(vk, v.name_enc);
      } catch { v.name = "(cannot decrypt)"; }
      ctx.vaults.push(v);
    }
  }

  // ============================================================ Main view
  function renderMain() {
    root.innerHTML = `<div style="display:flex;height:100%;min-height:0;font-size:13px">
      <aside style="width:250px;flex:0 0 250px;border-right:1px solid var(--border);display:flex;flex-direction:column;min-height:0">
        <div style="padding:10px 12px;display:flex;align-items:center;gap:8px">
          <b style="flex:1;display:flex;align-items:center;gap:6px">${icon("key")} Vaults</b>
          <button class="btn" data-newvault title="New vault">${icon("plus")}</button>
          <button class="btn" data-lock title="Lock">${icon("lock")}</button></div>
        <div data-tree style="flex:1;overflow:auto;padding:0 8px 10px"></div>
        <div style="padding:8px 10px;border-top:1px solid var(--border);display:flex;gap:6px;flex-wrap:wrap">
          <button class="btn" data-import style="font-size:11.5px">${icon("import", 14)} Import</button>
          <button class="btn" data-export style="font-size:11.5px">${icon("export", 14)} Export</button>
          <button class="btn" data-master style="font-size:11.5px">Change master</button></div></aside>
      <section style="width:300px;flex:0 0 300px;border-right:1px solid var(--border);display:flex;flex-direction:column;min-height:0">
        <div style="padding:10px 12px;display:flex;gap:8px;align-items:center">
          <input data-q placeholder="Search …" style="flex:1"><button class="btn-primary" data-newentry title="New entry">${icon("plus")}</button></div>
        <div data-list style="flex:1;overflow:auto"></div></section>
      <main data-detail style="flex:1;overflow:auto;min-height:0"></main></div>`;
    root.querySelector("[data-lock]").addEventListener("click", lock);
    root.querySelector("[data-newvault]").addEventListener("click", newVault);
    root.querySelector("[data-newentry]").addEventListener("click", () => curVault ? editEntry(null) : toast("Pick a vault first", "error"));
    root.querySelector("[data-import]").addEventListener("click", importDialog);
    root.querySelector("[data-export]").addEventListener("click", exportDialog);
    root.querySelector("[data-master]").addEventListener("click", changeMaster);
    root.querySelector("[data-q]").addEventListener("input", (e) => drawList(e.target.value.trim().toLowerCase()));
    drawTree();
  }

  function drawTree() {
    const el = root.querySelector("[data-tree]"); if (!el) return;
    el.innerHTML = ctx.vaults.map((v) => {
      const groups = (curVault === v.id && data) ? data.groups : [];
      const sub = (parent, depth) => groups.filter((g) => (g.parent_id || "") === parent).map((g) => `
        <a data-g="${esc(g.id)}" data-v="${esc(v.id)}" style="display:flex;gap:5px;align-items:center;padding:4px 8px 4px ${16 + depth * 14}px;border-radius:7px;cursor:pointer;${curVault === v.id && curGroup === g.id ? "background:color-mix(in srgb,var(--accent) 16%,transparent);font-weight:600" : ""}">${icon("folder", 14)} ${esc(g._name || "…")}</a>${sub(g.id, depth + 1)}`).join("");
      return `<div style="margin-bottom:4px">
        <a data-v="${esc(v.id)}" data-root="1" style="display:flex;gap:6px;align-items:center;padding:7px 8px;border-radius:8px;cursor:pointer;${curVault === v.id && !curGroup ? "background:color-mix(in srgb,var(--accent) 18%,transparent)" : ""}">
          <span style="display:flex">${v.shared ? icon("users") : icon("vault")}</span><span style="flex:1;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(v.name)}</span>
          ${curVault === v.id ? `<span data-newgroup="${esc(v.id)}" title="Folder" style="cursor:pointer;display:flex">${icon("folderPlus", 15)}</span><span data-vmenu="${esc(v.id)}" title="More" style="cursor:pointer;display:flex">${icon("more", 15)}</span>` : ""}</a>
        ${curVault === v.id ? sub("", 0) : ""}</div>`;
    }).join("") || `<div style="padding:10px;color:var(--subtext)">No vault yet. Create one with +.</div>`;
    el.querySelectorAll("[data-root]").forEach((a) => a.addEventListener("click", () => { curGroup = ""; openVault(a.dataset.v); }));
    el.querySelectorAll("[data-g]").forEach((a) => a.addEventListener("click", (e) => { e.stopPropagation(); curGroup = a.dataset.g; if (curVault !== a.dataset.v) openVault(a.dataset.v); else { drawTree(); drawList(); } }));
    el.querySelectorAll("[data-newgroup]").forEach((a) => a.addEventListener("click", (e) => { e.stopPropagation(); newGroup(a.dataset.newgroup); }));
    el.querySelectorAll("[data-vmenu]").forEach((a) => a.addEventListener("click", (e) => { e.stopPropagation(); vaultMenu(a.dataset.vmenu, e); }));
  }

  async function openVault(vid) {
    curVault = vid; curEntry = null;
    const el = root.querySelector("[data-list]"); if (el) el.innerHTML = `<div style="padding:10px;color:var(--subtext)">Loading …</div>`;
    try {
      const r = await api(`/vaults/${vid}/data`);
      const vk = vkey(vid);
      for (const g of r.groups) { try { g._name = await C.decText(vk, g.name_enc); } catch { g._name = "(?)"; } }
      for (const e of r.entries) { try { e._d = await C.decJson(vk, e.blob); } catch { e._d = { name: "(cannot decrypt)" }; } }
      data = r;
    } catch (e) { if (el) el.innerHTML = err(e); return; }
    drawTree(); drawList(); renderDetail();
  }

  function drawList(filter = "") {
    const el = root.querySelector("[data-list]"); if (!el || !data) return;
    let rows = data.entries.filter((e) => !curGroup || e.group_id === curGroup);
    if (filter) {
      rows = data.entries.filter((e) => {
        const d = e._d || {};
        return [d.name, d.username, d.url, e.service, ...(Object.keys(d.fields || {}))]
          .some((x) => String(x || "").toLowerCase().includes(filter));
      });
    }
    rows.sort((a, b) => (a._d.name || "").localeCompare(b._d.name || ""));
    el.innerHTML = rows.map((e) => {
      const d = e._d || {};
      const sub = d.username || d.url || e.service || "";
      return `<a data-e="${esc(e.id)}" style="display:flex;gap:9px;align-items:center;padding:8px 12px;border-bottom:1px solid var(--border);cursor:pointer;${curEntry === e.id ? "background:color-mix(in srgb,var(--accent) 12%,transparent)" : ""}">
        <span style="display:flex">${e.client_id ? icon("device", 18) : icon("key", 18)}</span>
        <span style="flex:1;min-width:0"><div style="font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(d.name || "(no name)")}</div>
          <div style="font-size:11px;color:var(--subtext);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(sub)}</div></span>
        <button class="btn" data-copyp="${esc(e.id)}" title="Copy password" style="padding:2px 7px">${icon("copy", 14)}</button></a>`;
    }).join("") || `<div style="padding:14px;color:var(--subtext)">${filter ? "No matches." : "No entries. Create one with +."}</div>`;
    el.querySelectorAll("[data-e]").forEach((a) => a.addEventListener("click", () => { curEntry = a.dataset.e; drawList(root.querySelector("[data-q]").value.trim().toLowerCase()); renderDetail(); }));
    el.querySelectorAll("[data-copyp]").forEach((b) => b.addEventListener("click", (ev) => { ev.stopPropagation(); const e = data.entries.find((x) => x.id === b.dataset.copyp); copy(e._d.password, "Password"); }));
  }

  // ============================================================ Detail / editor
  function renderDetail() {
    const el = root.querySelector("[data-detail]"); if (!el) return;
    const e = data && data.entries.find((x) => x.id === curEntry);
    if (!e) { el.innerHTML = `<div style="padding:40px;text-align:center;color:var(--subtext)">Pick an entry, or + for a new one.</div>`; return; }
    const d = e._d || {};
    const cl = e.client_id ? findClient(e.client_id) : null;
    const field = (label, val, copyLabel, mono) => val ? `<div style="margin-bottom:12px"><div style="font-size:11px;color:var(--subtext)">${esc(label)}</div>
      <div style="display:flex;gap:6px;align-items:center"><div style="flex:1;${mono ? "font-family:monospace;" : ""}word-break:break-all">${esc(val)}</div>
      <button class="btn" data-copy="${esc(copyLabel)}" style="padding:2px 7px">${icon("copy", 14)}</button></div></div>` : "";
    el.innerHTML = `<div style="padding:18px 22px;max-width:620px">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:16px">
        <span style="display:flex">${e.client_id ? icon("device", 22) : icon("key", 22)}</span><h2 style="margin:0;font-size:19px;flex:1">${esc(d.name || "(no name)")}</h2>
        <button class="btn" data-edit>${icon("edit", 14)} Edit</button><button class="btn" data-del style="color:var(--danger)">${icon("trash", 14)}</button></div>
      ${field("Username", d.username, "user", false)}
      <div style="margin-bottom:12px"><div style="font-size:11px;color:var(--subtext)">Password</div>
        <div style="display:flex;gap:6px;align-items:center"><div data-pw style="flex:1;font-family:monospace;letter-spacing:1px">••••••••••</div>
          <button class="btn" data-reveal style="padding:2px 7px">${icon("eye", 14)}</button><button class="btn" data-copyp style="padding:2px 7px">${icon("copy", 14)}</button>
          <button class="btn" data-fill title="Insert into the focused field / RMM dialog" style="padding:2px 7px">${icon("fill", 14)}</button></div></div>
      ${field("URL", d.url, "url", true)}
      ${cl ? `<div style="margin-bottom:12px"><div style="font-size:11px;color:var(--subtext)">Linked client</div><div style="display:flex;gap:6px;align-items:center">${icon("device", 15)} ${esc(cl.hostname || e.client_id)}</div></div>` : ""}
      ${e.service ? field("Service", e.service, "service", false) : ""}
      ${d.notes ? `<div style="margin-bottom:12px"><div style="font-size:11px;color:var(--subtext)">Notes</div><div style="white-space:pre-wrap">${esc(d.notes)}</div></div>` : ""}
      ${Object.entries(d.fields || {}).map(([k, v]) => field(k, v, k, true)).join("")}
      <div style="font-size:11px;color:var(--subtext);margin-top:16px">Last changed: ${new Date(e.updated_at).toLocaleString()}</div></div>`;
    el.querySelector("[data-edit]").addEventListener("click", () => editEntry(e));
    el.querySelector("[data-del]").addEventListener("click", () => delEntry(e));
    el.querySelector("[data-copyp]").addEventListener("click", () => copy(d.password, "Password"));
    el.querySelector("[data-fill]").addEventListener("click", () => fill(d));
    el.querySelectorAll("[data-copy]").forEach((b) => b.addEventListener("click", () => {
      const map = { user: d.username, url: d.url, service: e.service, ...(d.fields || {}) };
      copy(map[b.dataset.copy], b.dataset.copy);
    }));
    let shown = false;
    el.querySelector("[data-reveal]").addEventListener("click", () => { shown = !shown; el.querySelector("[data-pw]").textContent = shown ? (d.password || "") : "••••••••••"; });
  }

  function editEntry(e) {
    if (!curVault) return;
    const d = (e && e._d) || {};
    const clients = (state.clients || []).slice().sort((a, b) => (a.hostname || "").localeCompare(b.hostname || ""));
    const customRows = Object.entries(d.fields || {});
    const ov = overlay(`<h3 style="margin:0 0 12px">${e ? "Edit entry" : "New entry"}</h3>
      <div style="display:grid;grid-template-columns:120px 1fr;gap:8px 10px;align-items:center">
        <label>Name</label><input data-f="name" value="${esc(d.name || "")}" placeholder="e.g. Office router">
        <label>Username</label><input data-f="username" value="${esc(d.username || "")}">
        <label>Password</label><div style="display:flex;gap:6px"><input data-f="password" type="text" value="${esc(d.password || "")}" style="flex:1;font-family:monospace"><button class="btn" data-gen title="Generate">${icon("dice", 15)}</button></div>
        <label>URL</label><input data-f="url" value="${esc(d.url || "")}" placeholder="https://…">
        <label>Linked client</label><select data-f="client_id"><option value="">— none —</option>${clients.map((c) => `<option value="${esc(c.id)}" ${e && e.client_id === c.id ? "selected" : ""}>${esc(c.hostname || c.id)}</option>`).join("")}</select>
        <label>Service</label><input data-f="service" value="${esc((e && e.service) || "")}" placeholder="e.g. SSH, RDP, web admin">
        <label style="align-self:start">Notes</label><textarea data-f="notes" rows="3">${esc(d.notes || "")}</textarea>
        <label style="align-self:start">Folder</label><select data-f="group_id">${groupOptions(e ? e.group_id : curGroup)}</select>
      </div>
      <div style="margin-top:12px"><div style="font-size:11px;color:var(--subtext);margin-bottom:4px">Custom fields</div><div data-custom></div>
        <button class="btn" data-addf style="margin-top:6px;font-size:12px">${icon("plus", 13)} Field</button></div>
      <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px"><button class="btn" data-x>Cancel</button><button class="btn-primary" data-ok>${e ? "Save" : "Create"}</button></div>`, 560);
    const cwrap = ov.querySelector("[data-custom]");
    const addField = (k = "", v = "") => { const row = document.createElement("div"); row.style.cssText = "display:flex;gap:6px;margin-bottom:5px"; row.innerHTML = `<input placeholder="Field name" value="${esc(k)}" data-ck style="width:40%"><input placeholder="Value" value="${esc(v)}" data-cv style="flex:1;font-family:monospace"><button class="btn" data-rm style="color:var(--danger)">${icon("close", 14)}</button>`; row.querySelector("[data-rm]").addEventListener("click", () => row.remove()); cwrap.appendChild(row); };
    customRows.forEach(([k, v]) => addField(k, v));
    ov.querySelector("[data-addf]").addEventListener("click", () => addField());
    ov.querySelector("[data-gen]").addEventListener("click", () => generatorDialog((pw) => { ov.querySelector('[data-f="password"]').value = pw; }));
    ov.querySelector("[data-ok]").addEventListener("click", async () => {
      const g = (k) => ov.querySelector(`[data-f="${k}"]`).value;
      const fields = {}; cwrap.querySelectorAll("div").forEach((r) => { const k = r.querySelector("[data-ck]").value.trim(); const v = r.querySelector("[data-cv]").value; if (k) fields[k] = v; });
      const obj = { name: g("name").trim(), username: g("username"), password: g("password"), url: g("url"), notes: g("notes"), fields };
      if (!obj.name) { toast("Name is missing", "error"); return; }
      try {
        const blob = await C.encJson(vkey(curVault), obj);
        const payload = { group_id: g("group_id"), client_id: g("client_id"), service: g("service").trim(), blob };
        if (e) await api(`/vaults/${curVault}/entries/${e.id}`, { method: "PUT", body: JSON.stringify(payload) });
        else { const n = await api(`/vaults/${curVault}/entries`, { method: "POST", body: JSON.stringify(payload) }); curEntry = n.id; }
        ov.remove(); toast("Saved"); await openVault(curVault);
      } catch (err2) { toast(err2.message, "error", 6000); }
    });
  }

  function groupOptions(sel) {
    const gs = (data ? data.groups : []);
    const opt = (parent, depth) => gs.filter((g) => (g.parent_id || "") === parent).map((g) => `<option value="${esc(g.id)}" ${sel === g.id ? "selected" : ""}>${" ".repeat(depth * 2)}${esc(g._name || "…")}</option>${opt(g.id, depth + 1)}`).join("");
    return `<option value="">(vault root)</option>` + opt("", 0);
  }

  async function delEntry(e) {
    if (!(await uiConfirm(`Delete entry "${(e._d || {}).name || ""}"?`))) return;
    try { await api(`/vaults/${curVault}/entries/${e.id}`, { method: "DELETE" }); curEntry = null; toast("Deleted"); await openVault(curVault); } catch (err2) { toast(err2.message, "error"); }
  }

  // ============================================================ Manage vaults/folders
  async function newVault() {
    const name = await uiPrompt("Name of the new vault:");
    if (!name) return;
    try {
      const vk = await C.newVaultKey();
      await api("/vaults", { method: "POST", body: JSON.stringify({ name_enc: await C.encText(vk, name), wrapped_key: await C.wrapVaultKey(vk, ctx.session.publicKey) }) });
      await loadVaults(); renderMain(); toast("Vault created");
    } catch (e) { toast(e.message, "error", 6000); }
  }

  async function newGroup(vid) {
    const name = await uiPrompt("Folder name:");
    if (!name) return;
    try { await api(`/vaults/${vid}/groups`, { method: "POST", body: JSON.stringify({ parent_id: (curVault === vid ? curGroup : ""), name_enc: await C.encText(vkey(vid), name) }) }); await openVault(vid); }
    catch (e) { toast(e.message, "error"); }
  }

  function vaultMenu(vid, ev) {
    const v = ctx.vaults.find((x) => x.id === vid);
    const owner = v.role === "owner";
    const ov = overlay(`<h3 style="margin:0 0 12px">${esc(v.name)}</h3>
      <div style="display:flex;flex-direction:column;gap:8px">
        <button class="btn" data-rename>${icon("edit", 14)} Rename</button>
        ${owner ? `<button class="btn" data-share>${icon("users", 14)} Share / members</button>` : ""}
        ${curGroup ? `<button class="btn" data-delgroup style="color:var(--danger)">${icon("folder", 14)} Delete folder</button>` : ""}
        ${owner ? `<button class="btn" data-delete style="color:var(--danger)">${icon("trash", 14)} Delete vault</button>`
                : `<button class="btn" data-leave style="color:var(--danger)">Leave vault</button>`}</div>`, 360);
    ov.querySelector("[data-rename]").addEventListener("click", async () => { const n = await uiPrompt("New name:", { value: v.name }); if (!n) return; try { await api(`/vaults/${vid}`, { method: "PUT", body: JSON.stringify({ name_enc: await C.encText(vkey(vid), n) }) }); ov.remove(); await loadVaults(); renderMain(); if (curVault) openVault(curVault); } catch (e) { toast(e.message, "error"); } });
    ov.querySelector("[data-share]")?.addEventListener("click", () => { ov.remove(); shareDialog(v); });
    ov.querySelector("[data-delgroup]")?.addEventListener("click", async () => { if (await uiConfirm("Delete folder? Entries move to the vault root.")) { try { await api(`/vaults/${vid}/groups/${curGroup}`, { method: "DELETE" }); curGroup = ""; ov.remove(); openVault(vid); } catch (e) { toast(e.message, "error"); } } });
    ov.querySelector("[data-delete]")?.addEventListener("click", async () => { if (await uiConfirm(`Permanently delete vault "${v.name}" and all its entries?`)) { try { await api(`/vaults/${vid}`, { method: "DELETE" }); ov.remove(); curVault = null; data = null; await loadVaults(); renderMain(); } catch (e) { toast(e.message, "error"); } } });
    ov.querySelector("[data-leave]")?.addEventListener("click", async () => { if (await uiConfirm("Leave this shared vault?")) { try { await api(`/vaults/${vid}/members/${myId()}`, { method: "DELETE" }); ov.remove(); curVault = null; data = null; await loadVaults(); renderMain(); } catch (e) { toast(e.message, "error"); } } });
  }

  function myId() { return String((state.user && state.user.id) || ""); }

  async function shareDialog(v) {
    let dir, mem;
    try { dir = (await api("/directory")).users; mem = (await api(`/vaults/${v.id}/members`)).members; } catch (e) { toast(e.message, "error"); return; }
    const ov = overlay(`<h3 style="margin:0 0 6px;display:flex;align-items:center;gap:6px">${icon("users", 16)} Share "${esc(v.name)}"</h3>
      <p style="font-size:11.5px;color:var(--subtext);margin:0 0 12px">The vault key is wrapped with the member's public key - end-to-end, the backend cannot decrypt anything.</p>
      <div style="font-size:12px;font-weight:600;margin-bottom:4px">Members</div>
      <div data-mem style="margin-bottom:12px"></div>
      <div style="display:flex;gap:6px"><select data-pick style="flex:1"><option value="">Pick a user …</option>${dir.filter((u) => !mem.some((m) => m.user_id === u.user_id)).map((u) => `<option value="${esc(u.user_id)}">${esc(u.username)}</option>`).join("")}</select>
        <button class="btn-primary" data-add>Add</button></div>
      <div style="display:flex;justify-content:flex-end;margin-top:14px"><button class="btn" data-x>Close</button></div>`, 460);
    const drawMem = () => { ov.querySelector("[data-mem]").innerHTML = mem.map((m) => `<div style="display:flex;gap:8px;align-items:center;padding:4px 0"><span style="flex:1;display:flex;align-items:center;gap:6px">${m.role === "owner" ? icon("crown", 15) : icon("user", 15)} ${esc(m.username)}</span>${m.role !== "owner" ? `<button class="btn" data-rm="${esc(m.user_id)}" style="color:var(--danger);padding:2px 7px">remove</button>` : ""}</div>`).join(""); ov.querySelectorAll("[data-rm]").forEach((b) => b.addEventListener("click", async () => { try { await api(`/vaults/${v.id}/members/${b.dataset.rm}`, { method: "DELETE" }); mem = mem.filter((m) => m.user_id !== b.dataset.rm); drawMem(); } catch (e) { toast(e.message, "error"); } })); };
    drawMem();
    ov.querySelector("[data-add]").addEventListener("click", async () => {
      const uid = ov.querySelector("[data-pick]").value; if (!uid) return;
      const u = dir.find((x) => x.user_id === uid);
      try {
        const wrapped = await C.wrapVaultKey(vkey(v.id), u.public_key);
        await api(`/vaults/${v.id}/members`, { method: "POST", body: JSON.stringify({ user_id: uid, wrapped_key: wrapped, role: "member" }) });
        mem.push({ user_id: uid, username: u.username, role: "member" }); drawMem();
        ov.querySelector("[data-pick]").querySelector(`option[value="${CSS.escape(uid)}"]`)?.remove();
        toast("Shared with " + u.username);
      } catch (e) { toast(e.message, "error", 6000); }
    });
  }

  function changeMaster() {
    const ov = overlay(`<h3 style="margin:0 0 12px">Change master password</h3>
      <p style="font-size:11.5px;color:var(--subtext);margin:0 0 12px">The vault contents stay intact - only your personal key is re-encrypted.</p>
      <div style="display:grid;grid-template-columns:150px 1fr;gap:8px 10px;align-items:center">
        <label>Current password</label><input type="password" data-cur>
        <label>New password</label><input type="password" data-n1>
        <label>Repeat</label><input type="password" data-n2></div>
      <div data-msg style="color:var(--danger);font-size:12px;margin-top:8px"></div>
      <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px"><button class="btn" data-x>Cancel</button><button class="btn-primary" data-ok>Change</button></div>`, 440);
    ov.querySelector("[data-ok]").addEventListener("click", async () => {
      const msg = ov.querySelector("[data-msg]");
      const cur = ov.querySelector("[data-cur]").value, n1 = ov.querySelector("[data-n1]").value, n2 = ov.querySelector("[data-n2]").value;
      try { await C.unlock(cur, ctx.account); } catch { msg.textContent = "Current master password is wrong"; return; }
      if (n1.length < 8) { msg.textContent = "New password: at least 8 characters."; return; }
      if (n1 !== n2) { msg.textContent = "New passwords do not match."; return; }
      try {
        const rk = await C.rekey(ctx.session, n1);
        await api("/account/rekey", { method: "POST", body: JSON.stringify({ salt: rk.salt, iters: rk.iters, enc_private_key: rk.enc_private_key, verifier: rk.verifier }) });
        ctx.account = { ...ctx.account, salt: rk.salt, iters: rk.iters, enc_private_key: rk.enc_private_key, verifier: rk.verifier };
        ov.remove(); toast("Master password changed");
      } catch (e) { msg.textContent = e.message; }
    });
  }

  // ============================================================ Generator
  function generatorDialog(onUse) {
    const ov = overlay(`<h3 style="margin:0 0 12px;display:flex;align-items:center;gap:6px">${icon("dice", 16)} Generate password</h3>
      <div style="display:flex;gap:6px;align-items:center;margin-bottom:10px"><input data-out readonly style="flex:1;font-family:monospace;font-size:14px"><button class="btn" data-re title="Regenerate">${icon("refresh", 15)}</button><button class="btn" data-cp title="Copy">${icon("copy", 15)}</button></div>
      <div style="display:flex;gap:6px;margin-bottom:10px"><button class="btn" data-mode="pw" style="flex:1">Characters</button><button class="btn" data-mode="phrase" style="flex:1">Words</button></div>
      <div data-opts></div>
      <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px"><button class="btn" data-x>Close</button>${onUse ? `<button class="btn-primary" data-use>Use</button>` : ""}</div>`, 420);
    let mode = "pw", len = 20, words = 5, o = { lower: true, upper: true, digits: true, symbols: true, avoidAmbiguous: true };
    const out = ov.querySelector("[data-out]");
    const gen = () => { out.value = mode === "pw" ? C.generatePassword({ length: len, ...o }) : C.generatePassphrase(words); };
    const drawOpts = () => {
      const el = ov.querySelector("[data-opts]");
      if (mode === "pw") el.innerHTML = `<label style="display:block;margin-bottom:6px">Length: <b data-l>${len}</b><input type="range" min="8" max="64" value="${len}" data-len style="width:100%"></label>
        ${["lower:Lowercase", "upper:Uppercase", "digits:Digits", "symbols:Symbols", "avoidAmbiguous:Avoid ambiguous"].map((s) => { const [k, l] = s.split(":"); return `<label style="display:block;font-size:12.5px"><input type="checkbox" data-o="${k}" ${o[k] ? "checked" : ""}> ${l}</label>`; }).join("")}`;
      else el.innerHTML = `<label style="display:block">Words: <b data-l>${words}</b><input type="range" min="3" max="8" value="${words}" data-words style="width:100%"></label>`;
      el.querySelector("[data-len]")?.addEventListener("input", (e) => { len = +e.target.value; el.querySelector("[data-l]").textContent = len; gen(); });
      el.querySelector("[data-words]")?.addEventListener("input", (e) => { words = +e.target.value; el.querySelector("[data-l]").textContent = words; gen(); });
      el.querySelectorAll("[data-o]").forEach((c) => c.addEventListener("change", () => { o[c.dataset.o] = c.checked; gen(); }));
    };
    ov.querySelectorAll("[data-mode]").forEach((b) => b.addEventListener("click", () => { mode = b.dataset.mode; drawOpts(); gen(); }));
    ov.querySelector("[data-re]").addEventListener("click", gen);
    ov.querySelector("[data-cp]").addEventListener("click", () => copy(out.value, "Password"));
    ov.querySelector("[data-use]")?.addEventListener("click", () => { onUse(out.value); ov.remove(); });
    drawOpts(); gen();
  }

  // ============================================================ KeePass import/export/sync
  function importDialog() {
    const ov = overlay(`<h3 style="margin:0 0 10px;display:flex;align-items:center;gap:6px">${icon("import", 16)} Import KeePass</h3>
      <p style="font-size:11.5px;color:var(--subtext);margin:0 0 12px">The .kdbx is briefly decrypted in the backend (server-side); the entries are then stored browser-encrypted in the chosen vault.</p>
      <div style="display:grid;grid-template-columns:120px 1fr;gap:8px 10px;align-items:center">
        <label>File (.kdbx)</label><input type="file" data-file accept=".kdbx">
        <label>KeePass password</label><input type="password" data-pw>
        <label>Target vault</label><select data-vault>${ctx.vaults.map((v) => `<option value="${esc(v.id)}">${esc(v.name)}</option>`).join("")}</select>
        <label></label><label style="font-size:12.5px"><input type="checkbox" data-sync> Sync instead of add (skip existing)</label></div>
      <div data-prog style="font-size:12px;color:var(--subtext);margin-top:10px"></div>
      <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px"><button class="btn" data-x>Cancel</button><button class="btn-primary" data-ok>Import</button></div>`, 480);
    ov.querySelector("[data-ok]").addEventListener("click", async () => {
      const f = ov.querySelector("[data-file]").files[0]; const vid = ov.querySelector("[data-vault]").value;
      if (!f || !vid) { toast("Pick a file and a vault", "error"); return; }
      const prog = ov.querySelector("[data-prog]"); prog.textContent = "Reading KeePass file …";
      try {
        const fd = new FormData(); fd.append("file", f); fd.append("password", ov.querySelector("[data-pw]").value);
        const parsed = await (await rawFetch("/kdbx/parse", { method: "POST", body: fd })).json();
        await importEntries(vid, parsed, ov.querySelector("[data-sync]").checked, prog);
        ov.remove(); await openVault(vid);
      } catch (e) { prog.innerHTML = `<span style="color:var(--danger)">${esc(e.message)}</span>`; }
    });
  }

  async function importEntries(vid, parsed, syncMode, prog) {
    const vk = vkey(vid);
    if (curVault !== vid || !data) await openVault(vid);
    // Collect existing group paths and entries (for sync)
    const groupByPath = new Map(); // "A/B" -> id
    const pathOf = (gid) => { const parts = []; let g = data.groups.find((x) => x.id === gid); while (g) { parts.unshift(g._name); g = data.groups.find((x) => x.id === g.parent_id); } return parts.join("/"); };
    data.groups.forEach((g) => groupByPath.set(pathOf(g.id), g.id));
    const existing = new Set(data.entries.map((e) => `${pathOf(e.group_id)}|${(e._d.name || "").toLowerCase()}|${(e._d.username || "").toLowerCase()}`));

    async function ensurePath(path) {
      let parent = "", acc = [];
      for (const name of path) {
        acc.push(name); const key = acc.join("/");
        if (groupByPath.has(key)) { parent = groupByPath.get(key); continue; }
        const r = await api(`/vaults/${vid}/groups`, { method: "POST", body: JSON.stringify({ parent_id: parent, name_enc: await C.encText(vk, name) }) });
        groupByPath.set(key, r.id); data.groups.push({ id: r.id, parent_id: parent, _name: name }); parent = r.id;
      }
      return parent;
    }
    let added = 0, skipped = 0, i = 0;
    for (const e of parsed.entries) {
      i++; if (prog) prog.textContent = `Importing ${i}/${parsed.entries.length} …`;
      const gid = await ensurePath(e.path || []);
      const pathKey = (e.path || []).join("/");
      if (syncMode && existing.has(`${pathKey}|${(e.title || "").toLowerCase()}|${(e.username || "").toLowerCase()}`)) { skipped++; continue; }
      const obj = { name: e.title, username: e.username, password: e.password, url: e.url, notes: e.notes, fields: e.fields || {} };
      if (e.otp) obj.fields.otp = e.otp;
      await api(`/vaults/${vid}/entries`, { method: "POST", body: JSON.stringify({ group_id: gid, blob: await C.encJson(vk, obj), uuid: e.uuid }) });
      added++;
    }
    toast(`Import done: ${added} added${skipped ? ", " + skipped + " skipped" : ""}`, "success", 4000);
  }

  function exportDialog() {
    const ov = overlay(`<h3 style="margin:0 0 10px;display:flex;align-items:center;gap:6px">${icon("export", 16)} Export as KeePass</h3>
      <p style="font-size:11.5px;color:var(--subtext);margin:0 0 12px">The entries are decrypted in the browser and written server-side into a .kdbx. Set a password for the file.</p>
      <div style="display:grid;grid-template-columns:120px 1fr;gap:8px 10px;align-items:center">
        <label>Vault</label><select data-vault>${ctx.vaults.map((v) => `<option value="${esc(v.id)}">${esc(v.name)}</option>`).join("")}</select>
        <label>KeePass password</label><input type="password" data-pw></div>
      <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px"><button class="btn" data-x>Cancel</button><button class="btn-primary" data-ok>Export</button></div>`, 460);
    ov.querySelector("[data-ok]").addEventListener("click", async () => {
      const vid = ov.querySelector("[data-vault]").value; const pw = ov.querySelector("[data-pw]").value;
      if (!pw) { toast("Set a password for the file", "error"); return; }
      try {
        const tree = await buildExportTree(vid);
        const r = await rawFetch("/kdbx/build", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password: pw, tree }) });
        const blob = await r.blob(); const url = URL.createObjectURL(blob); const a = document.createElement("a");
        const vname = ctx.vaults.find((v) => v.id === vid)?.name || "export";
        a.href = url; a.download = `${vname}.kdbx`; a.click(); URL.revokeObjectURL(url);
        ov.remove(); toast("Exported");
      } catch (e) { toast(e.message, "error", 6000); }
    });
  }

  async function buildExportTree(vid) {
    const r = await api(`/vaults/${vid}/data`); const vk = vkey(vid);
    const groups = r.groups; for (const g of groups) { try { g._name = await C.decText(vk, g.name_enc); } catch { g._name = "Folder"; } }
    const pathOf = (gid) => { const parts = []; let g = groups.find((x) => x.id === gid); while (g) { parts.unshift(g._name); g = groups.find((x) => x.id === g.parent_id); } return parts; };
    const entries = [];
    for (const e of r.entries) { let d; try { d = await C.decJson(vk, e.blob); } catch { continue; } entries.push({ path: pathOf(e.group_id), title: d.name, username: d.username, password: d.password, url: d.url, notes: d.notes, fields: d.fields || {} }); }
    return { entries };
  }

  // ============================================================ Autofill / client lookup
  async function openClientLookup(clientId) {
    // When opened via the client button: find matching entries
    try {
      const r = await api(`/lookup?client_id=${encodeURIComponent(clientId)}`);
      if (!r.entries.length) { toast("No passwords for this client - create one.", "info", 3500); editEntry(null); const sel = root.querySelector('[data-f="client_id"]'); if (sel) sel.value = clientId; return; }
      // open the first matching vault and show the entry
      const first = r.entries[0];
      curVault = first.vault_id; await openVault(first.vault_id);
      const match = data.entries.find((e) => e.id === first.id); if (match) { curEntry = match.id; drawList(); renderDetail(); }
    } catch (e) { toast(e.message, "error"); }
  }

  function fill(d) {
    // 1) built-in browser / focused field via module API, 2) otherwise copy
    if (window.RMM_PM_FILL && window.RMM_PM_FILL(d)) { toast("Inserted"); return; }
    const el = document.activeElement;
    if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA")) { el.value = d.password; el.dispatchEvent(new Event("input", { bubbles: true })); toast("Inserted into field"); return; }
    copy(d.password, "Password");
    toast("No target field - password copied", "info", 3000);
  }

  // ============================================================ Helpers
  async function copy(text, label) {
    if (text == null) return;
    try { await navigator.clipboard.writeText(text); } catch { toast("Copy failed", "error"); return; }
    toast(`${label} copied (cleared in 30 s)`, "success", 2500);
    if (clip) clearTimeout(clip);
    clip = setTimeout(() => { navigator.clipboard.writeText("").catch(() => {}); clip = null; }, 30000);
  }
  function overlay(html, width = 520) {
    const ov = document.createElement("div");
    ov.style.cssText = "position:fixed;inset:0;z-index:9600;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;padding:14px";
    ov.innerHTML = `<div style="background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:16px 18px;width:${width}px;max-width:100%;max-height:calc(100dvh - 28px);overflow:auto;font-size:13px">${html}</div>`;
    document.body.appendChild(ov);
    ov.addEventListener("click", (e) => { if (e.target === ov) ov.remove(); });
    ov.querySelector("[data-x]")?.addEventListener("click", () => ov.remove());
    return ov;
  }
  const err = (e) => `<div style="padding:16px;color:var(--danger);white-space:pre-wrap">${esc(e.message || e)}</div>`;

  boot();
}
