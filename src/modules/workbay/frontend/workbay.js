// workbay.js
// ----------
// Work Bay: a small, always-on-top personal dock that opens in the bottom-left
// corner. Inspired by macOS tools like Dropzone, Yoink and Unclutter. Tabs:
//   📝 Note      - a scratch note (stored via the Notepad app of "collab",
//                  i.e. in the user's personal Space).
//   📋 Clipboard - a simple scratch clipboard, same storage; copy/paste to the
//                  system clipboard.
//   📎 Files     - a file shelf in the user's personal Space ("Mein Space/Work
//                  Bay"): drag files in to stash them, download them back out,
//                  or delete them.
//   ⭐ Favorites  - quick-launch tiles for the apps you use most.
//   🗃️ Apps      - parked windows. Drag any window onto the Work Bay (or use
//                  "Park in Work Bay"); click a parked app to bring it back,
//                  or ✕ to close it.
//
// No own backend: notes/clipboard go through /api/collab/notepad, the file
// shelf through /api/spaces, favorites live in localStorage.

import { esc } from "/js/app/utils.js";
import { moduleRequest } from "/js/app/modules.js";
import { getToken } from "/js/app/api.js";
import { getCatalogApps, openMenuApp, catalogEntry } from "/js/app/startmenu.js";
import {
  registerCleanup, registerWindowDropTarget, unregisterWindowDropTarget,
  listParkedWindows, unparkWindow, closeWindow,
} from "/js/app/windowmanager.js";

// ---- Notepad (note + clipboard), per user via the "collab" module ----
const NP = "/api/collab/notepad";
const NOTE_TITLE = "Work Bay";
const CLIP_TITLE = "Work Bay – Clipboard";
async function ensureNote(title) {
  const d = await moduleRequest(NP);
  let n = (d.notes || []).find((x) => x.folder_id == null && x.title === title);
  if (!n) n = await moduleRequest(NP + "/notes", { method: "POST", body: JSON.stringify({ title, text: "", folder_id: null }) });
  return n;
}
const encId = (id) => id.split("/").map(encodeURIComponent).join("/");
const saveNote = (id, text) => moduleRequest(NP + "/notes/" + encId(id), { method: "PATCH", body: JSON.stringify({ text }) });

// ---- File shelf, in the user's personal Space via the "spaces" module ----
const SHELF = "Mein Space/Work Bay";
const fmtSize = (b) => b > 1048576 ? (b / 1048576).toFixed(1) + " MB" : b > 1024 ? (b / 1024).toFixed(0) + " KB" : b + " B";
async function ensureShelf() {
  try { await moduleRequest("/api/spaces/fs/mkdir", { method: "POST", body: JSON.stringify({ path: SHELF }) }); } catch { /* exists */ }
}
async function listShelf() {
  const r = await moduleRequest(`/api/spaces/fs/list?path=${encodeURIComponent(SHELF)}`);
  return (r.entries || []).filter((e) => !e.is_dir);
}
async function uploadShelf(fileList) {
  const fd = new FormData();
  fd.append("path", SHELF);
  for (const f of fileList) fd.append("files", f, f.name);
  const res = await fetch("/api/spaces/fs/upload", {
    method: "POST", headers: { Authorization: `Bearer ${getToken()}` }, body: fd });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Upload failed (${res.status})`);
}
const deleteShelf = (name) => moduleRequest("/api/spaces/fs/delete", { method: "POST", body: JSON.stringify({ path: SHELF + "/" + name }) });
async function downloadShelf(name) {
  const res = await fetch(`/api/spaces/fs/download?path=${encodeURIComponent(SHELF + "/" + name)}`,
    { headers: { Authorization: `Bearer ${getToken()}` } });
  if (!res.ok) { window.notify?.("Download failed", "error"); return; }
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = url; a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

// ---- Favorites (quick-launch apps), per browser ----
const FAV_KEY = "rmm_workbay_favs";
const favs = () => { try { return JSON.parse(localStorage.getItem(FAV_KEY) || "[]"); } catch { return []; } };
const setFavs = (a) => localStorage.setItem(FAV_KEY, JSON.stringify(a));

export function renderWorkbay(body, win) {
  body.innerHTML = `
    <div class="wb-root" style="display:flex;flex-direction:column;height:100%;min-height:0;font-size:13px">
      <div class="wb-tabs" style="display:flex;gap:3px;padding:6px 6px 0;flex-wrap:wrap">
        <button class="btn btn-sm wb-tab" data-tab="note" title="Note">📝</button>
        <button class="btn btn-sm wb-tab" data-tab="clip" title="Clipboard">📋</button>
        <button class="btn btn-sm wb-tab" data-tab="files" title="Files">📎</button>
        <button class="btn btn-sm wb-tab" data-tab="favs" title="Favorites">⭐</button>
        <button class="btn btn-sm wb-tab" data-tab="apps" title="Parked apps">🗃️ <span class="wb-badge" style="display:none;background:var(--accent);color:#fff;border-radius:8px;padding:0 5px;font-size:11px"></span></button>
      </div>
      <div class="wb-body" style="flex:1;min-height:0;padding:6px;display:flex;flex-direction:column"></div>
    </div>`;

  const tabsEl = body.querySelector(".wb-tabs");
  const bodyEl = body.querySelector(".wb-body");
  const badge = body.querySelector(".wb-badge");
  let tab = "note";
  let noteRef = null, clipRef = null;
  let saveTimer = null;

  const setActiveTabStyle = () => tabsEl.querySelectorAll(".wb-tab")
    .forEach((b) => b.classList.toggle("btn-primary", b.dataset.tab === tab));

  // ---- note / clipboard editor backed by a scratch note ----
  function noteEditor(ref, extraTop) {
    const wrap = document.createElement("div");
    wrap.style.cssText = "flex:1;min-height:0;display:flex;flex-direction:column;gap:6px";
    if (extraTop) wrap.appendChild(extraTop);
    const ta = document.createElement("textarea");
    ta.style.cssText = "flex:1;min-height:0;width:100%;resize:none;box-sizing:border-box";
    ta.disabled = !ref; ta.value = ref ? (ref.text || "") : "";
    ta.placeholder = ref ? "" : "Loading…";
    const save = () => { if (ref) { clearTimeout(saveTimer); saveTimer = setTimeout(() => { ref.text = ta.value; saveNote(ref.id, ta.value).catch(() => {}); }, 600); } };
    ta.addEventListener("input", save);
    ta.addEventListener("blur", () => { if (ref) { clearTimeout(saveTimer); ref.text = ta.value; saveNote(ref.id, ta.value).catch(() => {}); } });
    wrap.appendChild(ta);
    return { wrap, ta };
  }

  async function renderNote() {
    bodyEl.innerHTML = "";
    const { wrap, ta } = noteEditor(noteRef);
    bodyEl.appendChild(wrap);
    if (!noteRef) { try { noteRef = await ensureNote(NOTE_TITLE); ta.disabled = false; ta.placeholder = ""; ta.value = noteRef.text || ""; } catch { ta.placeholder = "Notepad module not available."; } }
  }

  async function renderClip() {
    bodyEl.innerHTML = "";
    const bar = document.createElement("div");
    bar.style.cssText = "display:flex;gap:6px";
    bar.innerHTML = `<button class="btn btn-sm" id="wb-copy">📋 Copy</button><button class="btn btn-sm" id="wb-paste">📥 Paste</button>`;
    const { wrap, ta } = noteEditor(clipRef, bar);
    bodyEl.appendChild(wrap);
    if (!clipRef) { try { clipRef = await ensureNote(CLIP_TITLE); ta.disabled = false; ta.placeholder = ""; ta.value = clipRef.text || ""; } catch { ta.placeholder = "Notepad module not available."; } }
    bar.querySelector("#wb-copy").onclick = async () => {
      try { await navigator.clipboard.writeText(ta.value); window.notify?.("Copied to clipboard", "success", 1500); } catch { window.notify?.("Clipboard not available", "warn"); }
    };
    bar.querySelector("#wb-paste").onclick = async () => {
      try { const t = await navigator.clipboard.readText(); ta.value = ta.value ? (ta.value + "\n" + t) : t; if (clipRef) { clipRef.text = ta.value; saveNote(clipRef.id, ta.value).catch(() => {}); } } catch { window.notify?.("Clipboard not available", "warn"); }
    };
  }

  // ---- file shelf (personal Space) ----
  async function renderFiles() {
    bodyEl.innerHTML = "";
    const drop = document.createElement("div");
    drop.style.cssText = "border:1px dashed var(--border);border-radius:8px;padding:8px;text-align:center;color:var(--subtext);font-size:12px;cursor:pointer";
    drop.textContent = "Drop files here to stash — or click to pick";
    const input = document.createElement("input");
    input.type = "file"; input.multiple = true; input.style.display = "none";
    const list = document.createElement("div");
    list.style.cssText = "flex:1;min-height:0;overflow:auto;display:flex;flex-direction:column;gap:4px;margin-top:6px";
    bodyEl.appendChild(drop); bodyEl.appendChild(input); bodyEl.appendChild(list);

    const refresh = async () => {
      list.innerHTML = `<div style="color:var(--subtext);font-size:12px">Loading…</div>`;
      try {
        await ensureShelf();
        const files = await listShelf();
        list.innerHTML = "";
        if (!files.length) { list.innerHTML = `<div style="color:var(--subtext);font-size:12px;text-align:center;padding:8px">No files stashed</div>`; return; }
        for (const f of files) {
          const row = document.createElement("div");
          row.style.cssText = "display:flex;align-items:center;gap:8px;padding:5px 7px;border:1px solid var(--border);border-radius:7px";
          row.innerHTML = `<span>📄</span>
            <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(f.name)}">${esc(f.name)}</span>
            <span style="color:var(--subtext);font-size:11px">${fmtSize(f.size || 0)}</span>
            <button class="btn btn-sm wb-dl" title="Download">⬇️</button>
            <button class="btn btn-sm wb-rm" title="Remove">✕</button>`;
          row.querySelector(".wb-dl").onclick = () => downloadShelf(f.name);
          row.querySelector(".wb-rm").onclick = async () => { try { await deleteShelf(f.name); refresh(); } catch (e) { window.notify?.(esc(e.message), "error"); } };
          list.appendChild(row);
        }
      } catch (e) { list.innerHTML = `<div style="color:var(--danger);font-size:12px">${esc(e.message || "Spaces module not available")}</div>`; }
    };

    const doUpload = async (fl) => {
      if (!fl || !fl.length) return;
      list.innerHTML = `<div style="color:var(--subtext);font-size:12px">Uploading…</div>`;
      try { await ensureShelf(); await uploadShelf(fl); } catch (e) { window.notify?.(esc(e.message), "error"); }
      refresh();
    };
    drop.onclick = () => input.click();
    input.onchange = () => doUpload(input.files);
    drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.style.background = "var(--hover)"; });
    drop.addEventListener("dragleave", () => { drop.style.background = ""; });
    drop.addEventListener("drop", (e) => { e.preventDefault(); drop.style.background = ""; if (e.dataTransfer?.files?.length) doUpload(e.dataTransfer.files); });
    refresh();
  }

  // ---- favorites (quick-launch apps) ----
  function renderFavs() {
    bodyEl.innerHTML = "";
    const grid = document.createElement("div");
    grid.style.cssText = "flex:1;min-height:0;overflow:auto;display:grid;grid-template-columns:repeat(4,1fr);gap:8px;align-content:start";
    const ids = favs();
    for (const id of ids) {
      const c = catalogEntry(id);
      if (!c) continue;
      const tile = document.createElement("button");
      tile.className = "btn"; tile.title = c.label;
      tile.style.cssText = "display:flex;flex-direction:column;align-items:center;gap:3px;padding:8px 4px;position:relative";
      tile.innerHTML = `<span style="font-size:20px">${c.icon || "📦"}</span>
        <span style="font-size:10.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100%">${esc(c.label)}</span>
        <span class="wb-fav-x" title="Remove favorite" style="position:absolute;top:1px;right:3px;font-size:11px;opacity:.6">✕</span>`;
      tile.querySelector(".wb-fav-x").onclick = (e) => { e.stopPropagation(); setFavs(favs().filter((x) => x !== id)); renderFavs(); };
      tile.addEventListener("click", () => openMenuApp(id));
      grid.appendChild(tile);
    }
    if (!ids.length) {
      const empty = document.createElement("div");
      empty.style.cssText = "grid-column:1/-1;color:var(--subtext);font-size:12px;text-align:center;padding:8px";
      empty.textContent = "No favorites yet — add one below";
      grid.appendChild(empty);
    }
    bodyEl.appendChild(grid);

    // add picker
    const add = document.createElement("select");
    add.style.cssText = "margin-top:6px;width:100%";
    const pool = getCatalogApps().filter((a) => !favs().includes(a.id));
    add.innerHTML = `<option value="">＋ Add favorite…</option>` + pool.map((a) => `<option value="${esc(a.id)}">${esc(a.label)}</option>`).join("");
    add.onchange = () => { if (add.value && !favs().includes(add.value)) { setFavs([...favs(), add.value]); renderFavs(); } };
    bodyEl.appendChild(add);
  }

  // ---- parked apps ----
  function renderApps() {
    bodyEl.innerHTML = "";
    const parked = listParkedWindows();
    const hint = document.createElement("div");
    hint.style.cssText = "font-size:11.5px;color:var(--subtext);padding:2px 2px 6px";
    hint.textContent = "Drag a window here (or “Park in Work Bay”) to tuck it away. Click to bring it back.";
    bodyEl.appendChild(hint);
    if (!parked.length) {
      const empty = document.createElement("div");
      empty.style.cssText = "flex:1;display:flex;align-items:center;justify-content:center;color:var(--subtext);font-size:12px;border:1px dashed var(--border);border-radius:8px";
      empty.textContent = "No parked apps";
      bodyEl.appendChild(empty); return;
    }
    const wrapList = document.createElement("div");
    wrapList.style.cssText = "flex:1;min-height:0;overflow:auto;display:flex;flex-direction:column;gap:4px";
    for (const w of parked) {
      const row = document.createElement("div");
      row.style.cssText = "display:flex;align-items:center;gap:8px;padding:5px 7px;border:1px solid var(--border);border-radius:7px;cursor:pointer";
      row.title = "Click to bring it back";
      row.innerHTML = `${w.clientColor ? `<span style="width:9px;height:9px;border-radius:50%;flex:none;background:${esc(w.clientColor)}"></span>` : "<span>🗔</span>"}
        <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(w.title)}</span>
        <button class="btn btn-sm wb-eject" title="Bring back">⏏</button>
        <button class="btn btn-sm wb-close" title="Close">✕</button>`;
      const back = () => unparkWindow(w.key);
      row.addEventListener("click", (e) => { if (e.target.closest(".wb-close")) return; back(); });
      row.querySelector(".wb-eject").addEventListener("click", (e) => { e.stopPropagation(); back(); });
      row.querySelector(".wb-close").addEventListener("click", (e) => { e.stopPropagation(); closeWindow(w.key); });
      wrapList.appendChild(row);
    }
    bodyEl.appendChild(wrapList);
  }

  function render() {
    setActiveTabStyle();
    if (tab === "note") renderNote();
    else if (tab === "clip") renderClip();
    else if (tab === "files") renderFiles();
    else if (tab === "favs") renderFavs();
    else renderApps();
  }
  const updateBadge = () => { const n = listParkedWindows().length; badge.style.display = n ? "" : "none"; badge.textContent = n; };

  tabsEl.querySelectorAll(".wb-tab").forEach((b) => b.addEventListener("click", () => { tab = b.dataset.tab; render(); }));

  // dragging a window onto the bay parks it -> jump to Apps tab
  registerWindowDropTarget(win.key, win._el, () => { tab = "apps"; render(); updateBadge(); });
  const onWindows = () => { updateBadge(); if (tab === "apps") renderApps(); };
  window.addEventListener("rmm-windows-changed", onWindows);

  registerCleanup(win.key, () => {
    unregisterWindowDropTarget(win.key);
    window.removeEventListener("rmm-windows-changed", onWindows);
    for (const w of listParkedWindows()) unparkWindow(w.key);   // never lose a parked window
  });

  updateBadge();
  render();
}
