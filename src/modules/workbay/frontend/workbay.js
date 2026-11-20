// workbay.js
// ----------
// Work Bay: a small, always-on-top personal mini window with three tabs:
//   📝 Note      - a scratch note, stored via the Notepad app of the "collab"
//                  module (so it lives in the user's personal Space, per user).
//   📋 Clipboard - a simple scratch clipboard, stored the same way; with copy /
//                  paste to the system clipboard.
//   🗃️ Apps      - parked windows. Drag any window onto the Work Bay (or use
//                  "Park in Work Bay" in its title-bar menu) and it leaves the
//                  taskbar and the desktop and sits here as an item instead.
//                  A click brings it back.
//
// There is no own backend: notes/clipboard go through /api/collab/notepad.

import { esc } from "/js/app/utils.js";
import { moduleRequest } from "/js/app/modules.js";
import {
  registerCleanup, registerWindowDropTarget, unregisterWindowDropTarget,
  listParkedWindows, unparkWindow, closeWindow,
} from "/js/app/windowmanager.js";

const NP = "/api/collab/notepad";               // Notepad API of the "collab" module
const NOTE_TITLE = "Work Bay";                  // scratch note holding the Work Bay note
const CLIP_TITLE = "Work Bay – Clipboard";      // scratch note holding the clipboard

// Find (or create) a scratch note with the given title and return it.
async function ensureNote(title) {
  const d = await moduleRequest(NP);            // { folders, notes }
  let n = (d.notes || []).find((x) => x.folder_id == null && x.title === title);
  if (!n) {
    n = await moduleRequest(NP + "/notes", {
      method: "POST", body: JSON.stringify({ title, text: "", folder_id: null }),
    });
  }
  return n;                                     // { id, title, text, ... }
}
const encId = (id) => id.split("/").map(encodeURIComponent).join("/");
function saveNote(id, text) {
  return moduleRequest(NP + "/notes/" + encId(id), { method: "PATCH", body: JSON.stringify({ text }) });
}

export function renderWorkbay(body, win) {
  body.innerHTML = `
    <div class="wb-root" style="display:flex;flex-direction:column;height:100%;min-height:0;font-size:13px">
      <div class="wb-tabs" style="display:flex;gap:4px;padding:6px 6px 0">
        <button class="btn btn-sm wb-tab" data-tab="note">📝 Note</button>
        <button class="btn btn-sm wb-tab" data-tab="clip">📋 Clipboard</button>
        <button class="btn btn-sm wb-tab" data-tab="apps">🗃️ Apps <span class="wb-badge" style="display:none;background:var(--accent);color:#fff;border-radius:8px;padding:0 5px;font-size:11px"></span></button>
      </div>
      <div class="wb-body" style="flex:1;min-height:0;padding:6px;display:flex;flex-direction:column"></div>
    </div>`;

  const tabsEl = body.querySelector(".wb-tabs");
  const bodyEl = body.querySelector(".wb-body");
  const badge = body.querySelector(".wb-badge");
  let tab = "note";
  let noteRef = null, clipRef = null;           // cached {id,text} once loaded
  let saveTimer = null;

  const setActiveTabStyle = () => {
    tabsEl.querySelectorAll(".wb-tab").forEach((b) => {
      b.classList.toggle("btn-primary", b.dataset.tab === tab);
    });
  };

  // ---- a note/clipboard textarea wired to a scratch note ----
  function noteEditor(ref, title, extraTop) {
    const wrap = document.createElement("div");
    wrap.style.cssText = "flex:1;min-height:0;display:flex;flex-direction:column;gap:6px";
    if (extraTop) wrap.appendChild(extraTop);
    const ta = document.createElement("textarea");
    ta.style.cssText = "flex:1;min-height:0;width:100%;resize:none;box-sizing:border-box";
    ta.placeholder = ref ? "" : "Loading…";
    ta.disabled = !ref;
    ta.value = ref ? (ref.text || "") : "";
    const autosave = () => {
      if (!ref) return;
      clearTimeout(saveTimer);
      saveTimer = setTimeout(() => { ref.text = ta.value; saveNote(ref.id, ta.value).catch(() => {}); }, 600);
    };
    ta.addEventListener("input", autosave);
    ta.addEventListener("blur", () => {
      if (!ref) return;
      clearTimeout(saveTimer); ref.text = ta.value; saveNote(ref.id, ta.value).catch(() => {});
    });
    wrap.appendChild(ta);
    return { wrap, ta };
  }

  async function renderNote() {
    bodyEl.innerHTML = "";
    const { wrap, ta } = noteEditor(noteRef, NOTE_TITLE);
    bodyEl.appendChild(wrap);
    if (!noteRef) {
      try { noteRef = await ensureNote(NOTE_TITLE); ta.disabled = false; ta.placeholder = ""; ta.value = noteRef.text || ""; }
      catch { ta.placeholder = "Notepad module not available."; }
    }
  }

  async function renderClip() {
    bodyEl.innerHTML = "";
    const bar = document.createElement("div");
    bar.style.cssText = "display:flex;gap:6px";
    bar.innerHTML = `<button class="btn btn-sm" id="wb-copy">📋 Copy</button>
                     <button class="btn btn-sm" id="wb-paste">📥 Paste</button>`;
    const { wrap, ta } = noteEditor(clipRef, CLIP_TITLE, bar);
    bodyEl.appendChild(wrap);
    if (!clipRef) {
      try { clipRef = await ensureNote(CLIP_TITLE); ta.disabled = false; ta.placeholder = ""; ta.value = clipRef.text || ""; }
      catch { ta.placeholder = "Notepad module not available."; }
    }
    bar.querySelector("#wb-copy").onclick = async () => {
      try { await navigator.clipboard.writeText(ta.value); window.notify?.("Copied to clipboard", "success", 1500); }
      catch { window.notify?.("Clipboard not available", "warn"); }
    };
    bar.querySelector("#wb-paste").onclick = async () => {
      try {
        const t = await navigator.clipboard.readText();
        ta.value = ta.value ? (ta.value + "\n" + t) : t;
        if (clipRef) { clipRef.text = ta.value; saveNote(clipRef.id, ta.value).catch(() => {}); }
      } catch { window.notify?.("Clipboard not available", "warn"); }
    };
  }

  function renderApps() {
    bodyEl.innerHTML = "";
    const parked = listParkedWindows();
    const hint = document.createElement("div");
    hint.style.cssText = "font-size:11.5px;color:var(--subtext);padding:2px 2px 6px";
    hint.textContent = "Drag a window here (or use “Park in Work Bay”) to tuck it away.";
    bodyEl.appendChild(hint);
    if (!parked.length) {
      const empty = document.createElement("div");
      empty.style.cssText = "flex:1;display:flex;align-items:center;justify-content:center;color:var(--subtext);font-size:12px;border:1px dashed var(--border);border-radius:8px";
      empty.textContent = "No parked apps";
      bodyEl.appendChild(empty);
      return;
    }
    const list = document.createElement("div");
    list.style.cssText = "flex:1;min-height:0;overflow:auto;display:flex;flex-direction:column;gap:4px";
    for (const w of parked) {
      const row = document.createElement("div");
      row.style.cssText = "display:flex;align-items:center;gap:8px;padding:5px 7px;border:1px solid var(--border);border-radius:7px;cursor:pointer";
      row.title = "Click to bring it back";
      row.innerHTML =
        `${w.clientColor ? `<span style="width:9px;height:9px;border-radius:50%;flex:none;background:${esc(w.clientColor)}"></span>` : "<span>🗔</span>"}
         <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(w.title)}</span>
         <button class="btn btn-sm wb-close" title="Close">✕</button>`;
      row.addEventListener("click", (e) => {
        if (e.target.closest(".wb-close")) return;
        unparkWindow(w.key);                    // restore the window
      });
      row.querySelector(".wb-close").addEventListener("click", (e) => {
        e.stopPropagation();
        closeWindow(w.key);
      });
      list.appendChild(row);
    }
    bodyEl.appendChild(list);
  }

  function render() {
    setActiveTabStyle();
    if (tab === "note") renderNote();
    else if (tab === "clip") renderClip();
    else renderApps();
  }

  function updateBadge() {
    const n = listParkedWindows().length;
    badge.style.display = n ? "" : "none";
    badge.textContent = n;
  }

  tabsEl.querySelectorAll(".wb-tab").forEach((b) => {
    b.addEventListener("click", () => { tab = b.dataset.tab; render(); });
  });

  // Dragging a window onto the Work Bay parks it (handled by the window
  // manager); we just jump to the Apps tab and refresh.
  registerWindowDropTarget(win.key, win._el, () => { tab = "apps"; render(); updateBadge(); });

  // Keep the Apps tab + badge in sync when windows change elsewhere.
  const onWindows = () => { updateBadge(); if (tab === "apps") renderApps(); };
  window.addEventListener("rmm-windows-changed", onWindows);

  // When the Work Bay closes, bring every parked window back so nothing is lost.
  registerCleanup(win.key, () => {
    unregisterWindowDropTarget(win.key);
    window.removeEventListener("rmm-windows-changed", onWindows);
    for (const w of listParkedWindows()) unparkWindow(w.key);
  });

  updateBadge();
  render();
}
