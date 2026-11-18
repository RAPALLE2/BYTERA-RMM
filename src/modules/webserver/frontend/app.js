// app.js (module "webserver") - the "Webserver" app
// --------------------------------------------
// Left: websites (create with the "Manage websites" permission). Right: the selected
// website with the tabs Files | Settings (title, public, file listing, members + roles,
// change address, delete).
// Backend: webserver_routes.py (/api/webserver)

import { getToken } from "/js/app/api.js";
import { initSubjectPicker, readSubjectPicker, subjectPickerHtml } from "/js/app/subjectpicker.js";
import { esc, uiConfirm, uiPrompt } from "/js/app/utils.js";
import { renderFileManager } from "./files.js";
import { icon } from "./icons.js";

async function wreq(path, opts = {}) {
  const token = getToken() || "";
  const headers = { ...(token ? { Authorization: "Bearer " + token } : {}) };
  if (opts.body !== undefined && typeof opts.body !== "string") opts = { ...opts, body: JSON.stringify(opts.body) };
  if (opts.body) headers["Content-Type"] = "application/json";
  const res = await fetch("/api/webserver" + path, { ...opts, headers });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch { /* ignore */ }
    throw new Error(msg);
  }
  return res.json();
}

export function renderWebserver(body) {
  body.innerHTML = `
    <div style="display:flex;height:100%;min-height:0;font-size:13px">
      <div style="width:230px;flex:0 0 auto;border-right:1px solid var(--border);display:flex;flex-direction:column;min-height:0">
        <div style="padding:10px;display:flex;gap:6px;align-items:center;border-bottom:1px solid var(--border)">
          <b style="flex:1;display:inline-flex;align-items:center;gap:6px">${icon("globe", 15)} Websites</b><button class="btn btn-sm" data-new style="display:none">+ New</button></div>
        <div data-list style="flex:1;overflow:auto;padding:6px"></div>
      </div>
      <div data-main style="flex:1;min-width:0;overflow:auto;padding:12px">
        <div style="color:var(--subtext)">Select a website on the left.</div></div>
    </div>`;
  const listEl = body.querySelector("[data-list]");
  const main = body.querySelector("[data-main]");
  let data = { sites: [], manage: false };
  let current = null;

  async function loadList(select) {
    try { data = await wreq("/sites"); } catch (e) { listEl.innerHTML = `<div style="color:var(--danger)">${esc(e.message)}</div>`; return; }
    body.querySelector("[data-new]").style.display = data.manage ? "" : "none";
    listEl.innerHTML = data.sites.length ? data.sites.map((s) => `
      <div data-s="${esc(s.name)}" style="padding:8px 10px;border-radius:6px;cursor:pointer;margin-bottom:2px;
           ${s.name === (select || current) ? "background:var(--panel-2);" : ""}">
        <div style="display:flex;gap:6px;align-items:center"><b style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis">${esc(s.title || s.name)}</b>
          <span title="${s.kind === "proxy" ? "Forward" : s.public ? "public" : "not shared"}">${s.kind === "proxy" ? "↪ " : ""}${s.public ? icon("globe", 13) : icon("lock", 13)}</span></div>
        <div style="font-size:11.5px;color:var(--subtext)">/${esc(s.name)}</div></div>`).join("")
      : `<div style="color:var(--subtext);padding:8px">${data.manage ? "No website yet – \"+ New\"." : "You are not assigned to any website yet."}</div>`;
    listEl.querySelectorAll("[data-s]").forEach((el) => el.addEventListener("click", () => openSite(el.dataset.s)));
    if (select) openSite(select);
  }

  function openSite(name, tab = "files") {
    current = name;
    listEl.querySelectorAll("[data-s]").forEach((el) => { el.style.background = el.dataset.s === name ? "var(--panel-2)" : ""; });
    const s = data.sites.find((x) => x.name === name);
    if (!s) return;
    const url = `${location.origin}/web/${s.name}/`;
    main.innerHTML = `
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px">
        <b style="font-size:15px">${esc(s.title || s.name)}</b>
        <a href="${esc(url)}" target="_blank" rel="noopener" style="font-size:12px">${esc(url)}</a>
        <button class="btn-tool" data-copy title="Copy address">${icon("link", 14)}</button>
        <span style="flex:1"></span>
        <span style="font-size:12px;color:${s.public ? "var(--warn)" : "var(--subtext)"};display:inline-flex;align-items:center;gap:5px">${s.public ? icon("globe", 13) + " public – reachable without login" : icon("lock", 13) + " not shared"}</span>
      </div>
      <div class="tab-bar" style="margin-bottom:10px">
        <button class="tab-btn ${tab === "files" ? "active" : ""}" data-t="files">${icon("folder", 14)} Files</button>
        ${s.level >= 2 ? `<button class="tab-btn ${tab === "settings" ? "active" : ""}" data-t="settings">${icon("gear", 14)} Settings</button>` : ""}
      </div>
      <div data-pane></div>`;
    main.querySelector("[data-copy]").addEventListener("click", () => { navigator.clipboard?.writeText(url); window.notify?.("Address copied", "success"); });
    main.querySelectorAll("[data-t]").forEach((b) => b.addEventListener("click", () => openSite(name, b.dataset.t)));
    const pane = main.querySelector("[data-pane]");
    if (tab === "files" && s.kind === "proxy") {
      const up = s.upstream || {};
      pane.innerHTML = `<div style="border:1px solid var(--border);border-radius:8px;padding:12px">↪ <b>Forward</b> – this website has no files,
        it forwards every request to ${up.provider === "docker" ? `the Docker service <b>${esc(up.ref?.service || "")}</b> (port ${esc(String(up.ref?.port || ""))})
        – managed in the "Docker" app under "Publish"` : `<code>${esc(up.url || "")}</code>`}.</div>`;
    } else if (tab === "files") renderFileManager(pane, { api: `/api/webserver/sites/${encodeURIComponent(name)}`, publicBase: `/web/${name}` });
    else drawSettings(pane, s);
  }

  async function drawSettings(pane, s) {
    let dir = { users: [], groups: [] };
    try { dir = await wreq("/directory"); } catch { /* without list */ }
    const uname = (id) => dir.users.find((u) => u.id === id)?.username || id;
    const gname = (id) => dir.groups.find((g) => g.id === id)?.name || id;
    let members = (s.members || []).map((m) => ({ ...m }));
    pane.innerHTML = `
      <div class="form-row"><label>Title</label><input data-f="title" value="${esc(s.title || "")}"></div>
      <label style="display:flex;gap:8px;align-items:center;margin:8px 0;cursor:pointer">
        <input type="checkbox" data-f="public" ${s.public ? "checked" : ""} style="width:16px;height:16px;appearance:auto">
        <span><b>Publicly reachable</b> at <code>/${esc(s.name)}/</code> – <b>without login</b>. Only enable it if there is nothing confidential in the folder.</span></label>
      <label style="display:flex;gap:8px;align-items:center;margin:8px 0;cursor:pointer">
        <input type="checkbox" data-f="listing" ${s.listing ? "checked" : ""} style="width:16px;height:16px;appearance:auto">
        <span>Automatic file listing for folders without <code>index.html</code></span></label>
      <h4 style="margin:14px 0 6px">Members <span style="font-weight:400;color:var(--subtext);font-size:12px">– see the website folder in the app and in the Explorer/relay under "Websites"</span></h4>
      <div data-mem></div>
      <div style="display:flex;gap:6px;margin:6px 0 14px"><button class="btn btn-sm" data-add>+ Add members</button></div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn-primary" data-save>Speichern</button>
        ${data.manage ? `<button class="btn" data-rename>Change address …</button><button class="btn btn-danger" data-del>Delete website</button>` : ""}
      </div>`;
    const memBox = pane.querySelector("[data-mem]");
    const drawMem = () => {
      memBox.innerHTML = members.length ? members.map((m, i) => `
        <div style="display:flex;gap:8px;align-items:center;padding:4px 0;border-bottom:1px solid var(--border)">
          <span style="flex:1;display:inline-flex;align-items:center;gap:6px">${m.type === "user" ? icon("user", 14) + " " + esc(uname(m.id)) : icon("users", 14) + " " + esc(gname(m.id))}</span>
          <select data-role="${i}" style="width:auto"><option value="editor" ${m.role !== "admin" ? "selected" : ""}>Edit (files)</option>
            <option value="admin" ${m.role === "admin" ? "selected" : ""}>Admin (+ settings)</option></select>
          <button class="btn-tool" data-rm="${i}" title="Remove">${icon("close", 13)}</button></div>`).join("")
        : `<div style="color:var(--subtext)">No members yet. Users with "Manage websites" always have access.</div>`;
      memBox.querySelectorAll("[data-role]").forEach((sel) => sel.addEventListener("change", () => { members[+sel.dataset.role].role = sel.value; }));
      memBox.querySelectorAll("[data-rm]").forEach((b) => b.addEventListener("click", () => { members.splice(+b.dataset.rm, 1); drawMem(); }));
    };
    drawMem();
    pane.querySelector("[data-add]").addEventListener("click", () => {
      const ov = document.createElement("div");
      ov.style.cssText = "position:fixed;inset:0;z-index:9500;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;padding:16px";
      ov.innerHTML = `<div style="background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:16px;width:440px;max-width:100%;max-height:85vh;display:flex;flex-direction:column">
          <b style="margin-bottom:8px">Add members</b>
          <div data-pick style="flex:1;overflow:auto">${subjectPickerHtml(dir, [], { name: "wsm" })}</div>
          <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:10px"><button class="btn" data-x>Cancel</button><button class="btn-primary" data-ok>Add</button></div></div>`;
      document.body.appendChild(ov);
      initSubjectPicker(ov.querySelector("[data-pick]"));
      ov.querySelector("[data-x]").addEventListener("click", () => ov.remove());
      ov.querySelector("[data-ok]").addEventListener("click", () => {
        for (const sbj of readSubjectPicker(ov.querySelector("[data-pick]"))) {
          if (!members.some((m) => m.type === sbj.type && m.id === sbj.id)) members.push({ ...sbj, role: "editor" });
        }
        ov.remove(); drawMem();
      });
    });
    pane.querySelector("[data-save]").addEventListener("click", async () => {
      const pub = pane.querySelector('[data-f="public"]').checked;
      if (pub && !s.public && !(await uiConfirm(`Make "${s.name}" public?`, {
        description: "Everything in the website folder is then retrievable by anyone with the link – without login.", okText: "Make public" }))) return;
      try {
        await wreq(`/sites/${encodeURIComponent(s.name)}`, { method: "PATCH", body: {
          title: pane.querySelector('[data-f="title"]').value, public: pub,
          listing: pane.querySelector('[data-f="listing"]').checked, members } });
        window.notify?.("Saved", "success");
        await loadList(s.name);
        openSite(s.name, "settings");
      } catch (e) { window.notify?.(e.message, "error"); }
    });
    pane.querySelector("[data-rename]")?.addEventListener("click", async () => {
      const n = await uiPrompt("New address (rmm.url/web/…)", { value: s.name });
      if (!n?.trim() || n.trim() === s.name) return;
      try { const r = await wreq(`/sites/${encodeURIComponent(s.name)}`, { method: "PATCH", body: { new_name: n.trim() } });
        current = r.name; await loadList(r.name); } catch (e) { window.notify?.(e.message, "error"); }
    });
    pane.querySelector("[data-del]")?.addEventListener("click", async () => {
      if (!(await uiConfirm(`Delete website "${s.name}"?`, { description: "The website and ALL its files will be permanently deleted.", okText: "Delete permanently", danger: true }))) return;
      try { await wreq(`/sites/${encodeURIComponent(s.name)}`, { method: "DELETE" }); current = null;
        main.innerHTML = `<div style="color:var(--subtext)">Website deleted.</div>`; loadList(); } catch (e) { window.notify?.(e.message, "error"); }
    });
  }

  body.querySelector("[data-new]").addEventListener("click", async () => {
    const name = await uiPrompt("Address of the new website (rmm.url/web/…)", { placeholder: "e.g. company-wiki" });
    if (!name?.trim()) return;
    const title = (await uiPrompt("Title (optional)", { placeholder: name.trim() })) || "";
    try { const r = await wreq("/sites", { method: "POST", body: { name: name.trim(), title } });
      window.notify?.(`Website /${r.name}/ created (not public yet)`, "success"); loadList(r.name); }
    catch (e) { window.notify?.(e.message, "error", 8000); }
  });
  loadList();
}
