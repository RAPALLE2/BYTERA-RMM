// windowmanager.js
// -----------------
// Manages all open "windows" (terminal, file explorer, settings, ...).
//
// IMPORTANT (architecture): every window is created as a DOM element EXACTLY
// ONCE and then kept. Moving / resizing / focusing only updates individual CSS
// properties of the existing element - it does NOT rebuild the whole HTML. That
// way event handlers, input focus and running content (terminal output, live
// image) stay intact.
//
// A window object (in state.windows) looks like this:
//   { key, appId, title, x, y, w, h, minimized, props, clientColor,
//     _el (the DOM element), _rendered (whether the content was rendered yet) }

import { state } from "./state.js";
import { esc } from "./utils.js";
// t() is needed for the tooltips of the window buttons ("Close" etc.).
import { t } from "./i18n.js";
import { bindContextMenu } from "./contextmenu.js";

// Optional cleanup functions per window key (e.g. VNC: stop stream, remove
// socket listeners). Apps can register a function here.
const cleanupFns = {};
export function registerCleanup(key, fn) { cleanupFns[key] = fn; }

// --- Work Bay: parking windows + drop targets -----------------------------
// A "drop target" (e.g. the Work Bay) registers itself; dragging a window onto
// it, or picking "Park in Work Bay", parks that window: it is hidden from the
// desktop and the taskbar and shown as an item inside the target instead.
// Parked windows stay in state.windows with win.parked = true.
const _dropTargets = new Map();   // key -> { el, onDrop }
export function registerWindowDropTarget(key, el, onDrop) { _dropTargets.set(key, { el, onDrop }); }
export function unregisterWindowDropTarget(key) { _dropTargets.delete(key); }
export function listParkedWindows() { return state.windows.filter((w) => w.parked); }

export function parkWindow(key) {
  const win = state.windows.find((w) => w.key === key);
  if (!win || win.parked) return;
  win.parked = true;
  if (win.snap) { win.snap = null; updateSnapDividers(); }
  if (win._el) win._el.style.display = "none";
  notifyChanged();
}
export function unparkWindow(key) {
  const win = state.windows.find((w) => w.key === key);
  if (!win || !win.parked) return;
  win.parked = false;
  win.minimized = false;
  if (win._el) win._el.style.display = "flex";
  focusWindow(key);   // also calls notifyChanged()
}
// Which registered drop target (if any) is under this screen point? The dragged
// window is briefly made click-through so elementFromPoint sees what's beneath.
function _dropTargetAt(x, y, draggedEl, selfKey) {
  const prev = draggedEl ? draggedEl.style.pointerEvents : null;
  if (draggedEl) draggedEl.style.pointerEvents = "none";
  const hit = document.elementFromPoint(x, y);
  if (draggedEl) draggedEl.style.pointerEvents = prev || "";
  for (const [key, tgt] of _dropTargets) {
    if (key === selfKey) continue;
    if (tgt.el && hit && (tgt.el === hit || tgt.el.contains(hit))) return { key, ...tgt };
  }
  return null;
}

// appId of the window apps -> device action in the bytera:// scheme.
const _APP_TO_ACTION = {
  vnc: "remotescreen", explorer: "explorer", taskmanager: "tasks",
  terminal: "terminal", guacamole: "guacamole", "edit-client": "edit",
};
// Core / menu apps that open without a device.
const _APP_FUNCTION = new Set(["settings", "audit", "permissions", "modules", "nodes", "manage", "profile"]);

function _qs(obj) {
  const p = Object.entries(obj || {})
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`);
  return p.length ? "/" + p.join("&") : "";
}

// Default link when the app does not set its own win.getLink().
function defaultWindowLink(win) {
  const pr = win.props || {};
  const cid = pr.clientId;
  if (win.appId === "dist") return "bytera://dist";
  if (win.appId === "temp-usb") return "bytera://app/temp-usb";
  if (_APP_FUNCTION.has(win.appId)) return "bytera://function/" + win.appId;
  const action = _APP_TO_ACTION[win.appId];
  if (cid && action) {
    // carry along selected state parameters per app
    const params = {};
    if (win.appId === "vnc") {
      if (pr.screen != null) params.screen = pr.screen;
      if (pr.fps != null) params.fps = pr.fps;
      if (pr.resolution) params.resolution = pr.resolution;
    }
    if (win.appId === "explorer" && pr.path) params.path = pr.path;
    return `bytera://agent/${encodeURIComponent(cid)}/${action}` + _qs(params);
  }
  if (cid) return `bytera://agent/${encodeURIComponent(cid)}`;
  // module app without a device
  return "bytera://app/" + win.appId;
}

export function windowLink(win) {
  try {
    if (typeof win.getLink === "function") {
      const u = win.getLink();
      if (u) return u;
    }
  } catch { /* ignore */ }
  return defaultWindowLink(win);
}

function copyWindowLink(win) {
  const url = windowLink(win);
  import("./modules.js").then(({ getModuleApi }) => {
    const api = getModuleApi("url-scheme");
    if (api && api.copy) api.copy(url);
    else {
      try { navigator.clipboard?.writeText(url); } catch { /* ignore */ }
      window.notify?.("Link copied: " + url, "success", 2500);
    }
  }).catch(() => {
    try { navigator.clipboard?.writeText(url); } catch { /* ignore */ }
    window.notify?.(url, "info", 4000);
  });
}

// Set by app.js: function that renders the content of a window.
let contentRenderer = null;
export function setContentRenderer(fn) { contentRenderer = fn; }

// Set by app.js: callback when the window list changes
// (so the taskbar can be updated).
let onWindowsChanged = null;
export function setOnWindowsChanged(fn) { onWindowsChanged = fn; }

function notifyChanged() {
  if (onWindowsChanged) onWindowsChanged();
  // Let other views (e.g. the Work Bay dock) react to window changes too.
  try { window.dispatchEvent(new CustomEvent("rmm-windows-changed")); } catch { /* ignore */ }
}

let cascade = 0; // gives new windows a slightly offset position

// -----------------------------------------------------------------
// Multiple instances: by default, opening again brings the already existing
// window to the front. Whoever really wants a SECOND instance holds Shift
// while clicking OR clicks with the RIGHT mouse button - both mean the same
// and are far handier without a keyboard (tablet, mouse in one hand).
//
// Why a global flag instead of a parameter: the apps open windows in dozens of
// places (start menu, tiles, context menus, shortcuts). Threading the modifier
// through everywhere would be error-prone - here it is captured centrally once.
// -----------------------------------------------------------------
let _shiftDown = false;
// Flag for the right mouse button. It is CONSUMED on the next openWindow()
// (see below) and reset afterwards: a right-click does not produce a "click"
// event that would otherwise clear it - it would stay set and the next normal
// open would wrongly get a second window.
let _rightClick = false;
let _rightClickAt = 0;
try {
  window.addEventListener("keydown", (e) => { if (e.key === "Shift") _shiftDown = true; }, true);
  window.addEventListener("keyup", (e) => { if (e.key === "Shift") _shiftDown = false; }, true);
  // Mouse/touch events carry the real state - more reliable than keydown/keyup
  // when focus changed in between.
  window.addEventListener("mousedown", (e) => {
    _shiftDown = !!e.shiftKey;
    // button 2 = right button. Captured on mousedown because some places
    // intercept the contextmenu event and do not pass it on.
    if (e.button === 2) { _rightClick = true; _rightClickAt = Date.now(); }
  }, true);
  window.addEventListener("click", (e) => { _shiftDown = !!e.shiftKey; }, true);
  window.addEventListener("blur", () => { _shiftDown = false; _rightClick = false; });
} catch {}

/**
 * Was the open triggered by a right-click?
 * The flag is only valid briefly (1.5 s). Without this time window a
 * right-click that does NOT open a window (e.g. on a context menu that is then
 * clicked away) would taint the next perfectly normal open.
 */
function _consumeRightClick() {
  const hit = _rightClick && (Date.now() - _rightClickAt) < 1500;
  _rightClick = false;
  return hit;
}

// For apps that want to decide themselves (e.g. the "New terminal" button).
export function wantsNewInstance() { return _shiftDown || _rightClick; }

const layer = () => document.getElementById("window-layer");

// On browser-window resize, clamp all open windows fully back into the visible
// area (otherwise, when shrinking, they can slip behind the topbar/taskbar and
// become unreachable).
let _resizeClampTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(_resizeClampTimer);
  _resizeClampTimer = setTimeout(() => {
    for (const win of state.windows) {
      if (!win.minimized && !win.maximized && !win.snap) clampWindowIntoView(win);
    }
    relayoutSnapped(false);   // adapt snapped windows to the new layer size
    updateSnapDividers();
  }, 100);
});

// -----------------------------------------------------------------
// Open / close / focus / minimize
// -----------------------------------------------------------------

export function openWindow({ key, appId, title, props = {}, clientColor = null, w = 640, h = 460,
                            x = null, y = null, minimized = false, maximized = false, focus = true,
                            singleton = false, pinned = false, newInstance = null }) {
  // Repair old keys still persisted with '#' (CSS-selector safe).
  key = String(key).replace(/#/g, "--");
  const existing = state.windows.find((win) => win.key === key);
  // Should a SECOND instance really be created?
  //   newInstance === true/false -> caller decides firmly
  //   newInstance === null       -> the Shift key decides
  // A right-click counts like Shift. _consumeRightClick() resets the flag in
  // the process so it does not carry over to the next open.
  const rightNew = _consumeRightClick();
  const forceNew = newInstance === true
    || (newInstance !== false && (_shiftDown || rightNew));
  if (existing && !singleton && forceNew) {
    // Multiple instances: derive a unique key for the new instance
    // (key--2, key--3, ...) and build a FRESH window.
    // IMPORTANT: NO '#' in the suffix! The apps build element IDs from win.key
    // (e.g. querySelector(`#term-text-${win.key}`)) - a '#' in the key would
    // make those selectors invalid, which made every 2nd/3rd instance
    // "non-functional" (especially multiple terminals of the same client).
    // Suffix "--n".
    let n = 2;
    while (state.windows.some((win) => win.key === `${key}--${n}`)) n++;
    key = `${key}--${n}`;
  } else if (existing) {
    // Window already exists -> update content/props (important when the same
    // "slot" is detached with different content), restore it and bring it to
    // the front.
    existing.props = props;
    existing.title = title;
    existing.clientColor = clientColor;
    existing.minimized = false;
    if (existing._el) {
      existing._el.style.display = "flex";
      const titleEl = existing._el.querySelector(".rmm-window-title-text, .rmm-window-title");
      if (titleEl) titleEl.textContent = title;
      const body = existing._el.querySelector(".rmm-window-body");
      if (body && contentRenderer) { body.innerHTML = ""; contentRenderer(body, existing); }
    }
    focusWindow(key);
    return;
  }

  cascade = (cascade + 1) % 6;
  const win = {
    key, appId, title, props, clientColor,
    x: x != null ? x : 60 + cascade * 26,
    y: y != null ? y : 30 + cascade * 22,
    w, h,
    minimized: !!minimized,
    maximized: !!maximized,
    pinned: !!pinned,
    _el: null,
    _rendered: false,
  };
  state.windows.push(win);
  state.focusOrder.push(key);

  createWindowElement(win); // build the DOM element ONCE
  if (win.maximized) {
    // Restore maximized: remember the saved normal geometry, then maximize.
    win._restore = { x: win.x, y: win.y, w: win.w, h: win.h };
    win.maximized = false;      // toggleMaximize expects the starting state
    toggleMaximize(key);
  } else {
    clampWindowIntoView(win);
  }
  if (win.minimized && win._el) win._el.style.display = "none";
  applyFocusZIndex();
  if (focus && !win.minimized) focusWindow(key);
  notifyChanged();
}

// Clamps a window fully into the visible window area (layer).
export function clampWindowIntoView(win) {
  const lay = layer();
  if (!lay || !win._el) return;
  const maxX = Math.max(0, lay.clientWidth - win._el.offsetWidth);
  const maxY = Math.max(0, lay.clientHeight - win._el.offsetHeight);
  win.x = Math.min(Math.max(0, win.x), maxX);
  win.y = Math.min(Math.max(0, win.y), maxY);
  win._el.style.left = `${win.x}px`;
  win._el.style.top = `${win.y}px`;
}

export function closeWindow(key) {
  // If the app registered a cleanup function (e.g. stop the VNC stream)
  const win = state.windows.find((w) => w.key === key);
  // Play the close animation, then actually remove it.
  if (win && win._el && !win._closing) {
    win._closing = true;
    win._el.classList.add("win-closing");
    setTimeout(() => _removeWindow(key), 150);
    return;
  }
  _removeWindow(key);
}

function _removeWindow(key) {
  if (cleanupFns[key]) {
    try { cleanupFns[key](); } catch (e) { console.error(e); }
    delete cleanupFns[key];
  }

  const win = state.windows.find((w) => w.key === key);
  if (win && win._el) win._el.remove(); // actually remove the DOM element

  state.windows = state.windows.filter((w) => w.key !== key);
  state.focusOrder = state.focusOrder.filter((k) => k !== key);
  applyFocusZIndex();
  clearSnapAssist();
  updateSnapDividers();
  notifyChanged();
}

export function focusWindow(key) {
  state.focusOrder = state.focusOrder.filter((k) => k !== key);
  state.focusOrder.push(key);
  applyFocusZIndex();
  notifyChanged();
}

export function toggleMinimize(key) {
  const win = state.windows.find((w) => w.key === key);
  if (!win) return;
  win.minimized = !win.minimized;
  if (win._el) {
    if (win.minimized) {
      // Collapse animation, then hide.
      win._el.classList.add("win-minimizing");
      setTimeout(() => {
        win._el.style.display = "none";
        win._el.classList.remove("win-minimizing");
      }, 190);
    } else {
      win._el.style.display = "flex";
      win._el.classList.remove("win-minimizing");
      win._el.style.animation = "win-open 0.18s cubic-bezier(0.22,1,0.36,1)";
      setTimeout(() => { if (win._el) win._el.style.animation = ""; }, 200);
    }
  }
  if (!win.minimized) focusWindow(key);
  updateSnapDividers();
  notifyChanged();
}

export function toggleMaximize(key) {
  const win = state.windows.find((w) => w.key === key);
  if (!win || !win._el) return;

  // A snapped window leaves the snap layout when maximized.
  if (win.snap) { win.snap = null; updateSnapDividers(); }
  clearSnapAssist();

  // Enable a smooth geometry transition for the duration of the switch.
  win._el.classList.add("win-animate-geo");
  setTimeout(() => { if (win._el) win._el.classList.remove("win-animate-geo"); }, 230);

  if (win.maximized) {
    // Restore: reset the remembered position/size
    win.maximized = false;
    win._el.style.left = `${win._restore.x}px`;
    win._el.style.top = `${win._restore.y}px`;
    win._el.style.width = `${win._restore.w}px`;
    win._el.style.height = `${win._restore.h}px`;
    win.x = win._restore.x; win.y = win._restore.y;
    win.w = win._restore.w; win.h = win._restore.h;
  } else {
    // Maximize: remember current dimensions, then stretch over the whole window area
    win._restore = { x: win.x, y: win.y, w: win.w, h: win.h };
    win.maximized = true;
    win._el.style.left = "0px";
    win._el.style.top = "0px";
    win._el.style.width = "100%";
    win._el.style.height = "100%";
  }
  focusWindow(key);
  notifyChanged();
}

export function minimizeAll() {
  state.windows.forEach((win) => {
    win.minimized = true;
    if (win._el) win._el.style.display = "none";
  });
  notifyChanged();
}

// -----------------------------------------------------------------
// z-index (which window is on top) based on the focus order
// -----------------------------------------------------------------

function applyFocusZIndex() {
  // Pinned windows (📌) ALWAYS sit above all normal windows - among themselves
  // the focus order still applies. Offset 2000 stays safely below dialogs
  // (9500), pickers (8000) and drag cards (9999).
  state.windows.forEach((win) => {
    if (win._el) {
      win._el.style.zIndex = (win.pinned ? 2000 : 100) + state.focusOrder.indexOf(win.key);
    }
  });
}

export function togglePin(key) {
  const win = state.windows.find((w) => w.key === key);
  if (!win) return;
  win.pinned = !win.pinned;
  if (win._pinBtn) {
    win._pinBtn.classList.toggle("pinned", win.pinned);
    win._pinBtn.title = win.pinned ? "Unpin" : "Pin (always on top)";
  }
  focusWindow(key);   // also applies the new z-index
}

// -----------------------------------------------------------------
// Create a window's DOM element ONCE
// -----------------------------------------------------------------

function createWindowElement(win) {
  const el = document.createElement("div");
  el.className = "rmm-window";
  el.style.left = `${win.x}px`;
  el.style.top = `${win.y}px`;
  el.style.width = `${win.w}px`;
  el.style.height = `${win.h}px`;
  el.style.display = win.minimized ? "none" : "flex";

  // Click anywhere on the window -> bring it to the front
  el.addEventListener("mousedown", () => focusWindow(win.key));

  // --- Title bar ---
  const titlebar = document.createElement("div");
  titlebar.className = "rmm-window-titlebar";
  bindContextMenu(titlebar, (e) => (e.target.closest("button") ? null : windowMenu(win.key)));

  const titleSpan = document.createElement("span");
  titleSpan.className = "rmm-window-title";
  titleSpan.textContent = win.title;

  const controls = document.createElement("span");
  controls.className = "win-controls";

  // 🔗 Copy URL: builds a bytera:// link that reflects the current state of the
  // window (app, device, path, settings). The app can set win.getLink() for
  // this (returns the full link); otherwise there is a default.
  const linkBtn = document.createElement("button");
  linkBtn.className = "win-link-btn";
  linkBtn.textContent = "🔗";
  linkBtn.title = "Copy URL (link to this view)";
  linkBtn.addEventListener("click", (e) => { e.stopPropagation(); copyWindowLink(win); });
  linkBtn.addEventListener("mousedown", (e) => e.stopPropagation());

  const pinBtn = document.createElement("button");
  // Only the icon (glyph) is slanted/rotated, NOT the button box. That way the
  // hover / pinned background always stays straight.
  pinBtn.innerHTML = '<span class="win-pin-glyph">📌</span>';
  pinBtn.className = "win-pin-btn" + (win.pinned ? " pinned" : "");
  pinBtn.title = win.pinned ? "Unpin" : "Pin (always on top)";
  pinBtn.addEventListener("click", (e) => { e.stopPropagation(); togglePin(win.key); });
  pinBtn.addEventListener("mousedown", (e) => e.stopPropagation());
  win._pinBtn = pinBtn;

  const minBtn = document.createElement("button");
  minBtn.textContent = "–";
  minBtn.title = "Minimize";
  // Important: mousedown instead of click prevents drag/focus from interfering
  minBtn.addEventListener("click", (e) => { e.stopPropagation(); toggleMinimize(win.key); });
  minBtn.addEventListener("mousedown", (e) => e.stopPropagation());

  const maxBtn = document.createElement("button");
  maxBtn.className = "win-max-btn";   // only hidden via CSS on mobile (windows are fullscreen there anyway)
  maxBtn.textContent = "□";
  maxBtn.title = "Fullscreen / Restore";
  maxBtn.addEventListener("click", (e) => { e.stopPropagation(); toggleMaximize(win.key); });
  maxBtn.addEventListener("mousedown", (e) => e.stopPropagation());

  const closeBtn = document.createElement("button");
  closeBtn.textContent = "✕";
  closeBtn.title = t("close");
  closeBtn.addEventListener("click", (e) => { e.stopPropagation(); closeWindow(win.key); });
  closeBtn.addEventListener("mousedown", (e) => e.stopPropagation());

  controls.appendChild(linkBtn);
  controls.appendChild(pinBtn);
  controls.appendChild(minBtn);
  controls.appendChild(maxBtn);
  controls.appendChild(closeBtn);
  titlebar.appendChild(titleSpan);
  titlebar.appendChild(controls);
  makeDraggable(titlebar, el, win);
  // Double-click on the title bar = maximize/restore (as usual)
  titlebar.addEventListener("dblclick", (e) => {
    if (e.target.closest(".win-controls")) return;
    toggleMaximize(win.key);
  });
  el.appendChild(titlebar);

  // --- Content ---
  const body = document.createElement("div");
  body.className = "rmm-window-body";
  el.appendChild(body);

  // --- Resize handles: corner (both axes) + edges (right, bottom) ---
  const resizeHandle = document.createElement("div");
  resizeHandle.className = "rmm-window-resize-handle";
  makeResizable(resizeHandle, el, win, "both");
  el.appendChild(resizeHandle);

  const edgeR = document.createElement("div");
  edgeR.className = "rmm-window-resize-edge-r";
  makeResizable(edgeR, el, win, "x");
  el.appendChild(edgeR);

  const edgeB = document.createElement("div");
  edgeB.className = "rmm-window-resize-edge-b";
  makeResizable(edgeB, el, win, "y");
  el.appendChild(edgeB);

  win._el = el;
  layer().appendChild(el);

  // Render the content ONCE (after that it lives on independently)
  if (contentRenderer) {
    contentRenderer(body, win);
    win._rendered = true;
  }
}

// -----------------------------------------------------------------
// Moving (drag) - updates live, without re-rendering
// -----------------------------------------------------------------

function makeDraggable(handle, windowEl, win) {
  handle.addEventListener("mousedown", (e) => {
    // Do not treat clicks on the control buttons as a drag
    if (e.target.closest(".win-controls")) return;
    e.preventDefault();
    focusWindow(win.key);
    clearSnapAssist();

    const startX = e.clientX;
    const startY = e.clientY;
    let startLeft = win.x;
    let startTop = win.y;
    // Where (relative, 0..1) did the user grab the title bar? Needed when
    // releasing from snap/maximized so the window "snaps" under the cursor
    // instead of jumping away (Windows behaviour).
    const grabFrac = (e.clientX - windowEl.getBoundingClientRect().left) /
                     Math.max(1, windowEl.offsetWidth);
    let dragging = false;
    let released = false;   // has a snapped/maximized window already been released?

    function releaseFromSnap(ev) {
      // Release a window from snap/maximized: restore the old size and position
      // it so the cursor stays proportionally on the title bar.
      const r = (win.maximized ? win._restore : win._snapRestore) || { w: 640, h: 460 };
      win.maximized = false;
      win.snap = null;
      win.w = r.w; win.h = r.h;
      const lay = layer();
      const layRect = lay ? lay.getBoundingClientRect() : { left: 0, top: 0 };
      win.x = Math.round(ev.clientX - layRect.left - grabFrac * r.w);
      win.y = Math.max(0, Math.round(ev.clientY - layRect.top - 14));
      windowEl.style.width = `${win.w}px`;
      windowEl.style.height = `${win.h}px`;
      // Reset the reference points so the drag formula continues seamlessly.
      startLeft = win.x - (ev.clientX - startX);
      startTop = win.y - (ev.clientY - startY);
      updateSnapDividers();
    }

    function onMove(ev) {
      if (!dragging && Math.abs(ev.clientX - startX) + Math.abs(ev.clientY - startY) < 4) return;
      dragging = true;
      if (!released && (win.snap || win.maximized)) { releaseFromSnap(ev); released = true; }

      let nx = startLeft + (ev.clientX - startX);
      let ny = startTop + (ev.clientY - startY);
      // Clamp into the bounds of the window area (layer) so no edge ever ends
      // up outside. layer sits between the topbar (top) and taskbar (bottom);
      // window coordinates are relative to it.
      const lay = layer();
      if (lay) {
        const maxX = Math.max(0, lay.clientWidth - windowEl.offsetWidth);
        const maxY = Math.max(0, lay.clientHeight - windowEl.offsetHeight);
        nx = Math.min(Math.max(0, nx), maxX);
        ny = Math.min(Math.max(0, ny), maxY);
      }
      win.x = nx;
      win.y = ny;
      windowEl.style.left = `${win.x}px`;
      windowEl.style.top = `${win.y}px`;

      // Determine the snap zone under the cursor + show a preview.
      showSnapPreview(zoneForPointer(ev.clientX, ev.clientY));
    }
    function onUp(ev) {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      showSnapPreview(null);
      // Dropped onto a drop target (the Work Bay)? Park the window there
      // instead of snapping. The bay itself cannot be parked.
      if (dragging && win.appId !== "workbay") {
        const tgt = _dropTargetAt(ev.clientX, ev.clientY, windowEl, win.key);
        if (tgt) { parkWindow(win.key); try { tgt.onDrop?.(win.key); } catch { /* ignore */ } return; }
      }
      const zone = dragging ? zoneForPointer(ev.clientX, ev.clientY) : null;
      if (zone) snapWindowTo(win.key, zone);
      notifyChanged();   // save the new position
    }
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
}

// -----------------------------------------------------------------
// Resizing - also live
// -----------------------------------------------------------------

function makeResizable(handle, windowEl, win, axis = "both") {
  handle.addEventListener("mousedown", (e) => {
    e.preventDefault();
    e.stopPropagation();
    // If maximized, restore first (otherwise resizing makes no sense)
    if (win.maximized) return;
    // Resizing manually releases the window from the snap layout (the current
    // geometry stays as the starting point).
    if (win.snap) { win.snap = null; updateSnapDividers(); }

    const startX = e.clientX;
    const startY = e.clientY;
    const startW = win.w;
    const startH = win.h;
    // Prevent text selection while dragging (otherwise everything gets selected)
    document.body.style.userSelect = "none";

    function onMove(ev) {
      const lay = layer();
      if (axis === "x" || axis === "both") {
        let nw = Math.max(380, startW + (ev.clientX - startX));
        if (lay) nw = Math.min(nw, lay.clientWidth - win.x);  // not past the right edge
        win.w = nw;
        windowEl.style.width = `${win.w}px`;
      }
      if (axis === "y" || axis === "both") {
        let nh = Math.max(260, startH + (ev.clientY - startY));
        if (lay) nh = Math.min(nh, lay.clientHeight - win.y);  // not past the bottom edge
        win.h = nh;
        windowEl.style.height = `${win.h}px`;
      }
    }
    function onUp() {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      document.body.style.userSelect = "";
      notifyChanged();   // save the new size
    }
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
}

// =================================================================
// SNAP SYSTEM: Windows-like window arrangement
// -----------------------------------------------------------------
// - Drag a window to an edge/corner -> it snaps in with animation:
//     top center             = maximize
//     left/right             = half width  (2 windows side by side)
//     top/bottom (on a side) = half height (2 windows on top of each other)
//     corners                = quarter (3-/4-window layouts)
// - The dividers remember global split ratios (splits.v/h). When you drag a
//   border that has snapped windows on BOTH sides, ALL adjacent windows are
//   resized together (like Windows).
// - Snap assist: after snapping, each still-free neighbour zone shows a choice
//   of the remaining windows - a click snaps the window there.
// =================================================================

// Splits PER SEGMENT (0..1): so that with 4 windows, for example, the border
// between the two LOWER ones can be moved independently of the upper ones.
//   vt = vertical line in the top row     vb = in the bottom row
//   hl = horizontal line in the left col  hr = in the right col
// When a window spans the full height/width (left/right or top/bottom), the
// affected segments are automatically coupled.
const splits = { vt: 0.5, vb: 0.5, hl: 0.5, hr: 0.5 };
let _snapPreviewEl = null;
let _dividerEls = [];
let _assistEls = [];

function _activeZones() {
  return state.windows.filter((w) => w.snap && !w.minimized).map((w) => w.snap);
}

// Effective lines: full-height / full-width windows force ONE line.
function _lines() {
  const zones = _activeZones();
  const fullH = zones.includes("left") || zones.includes("right");
  const fullW = zones.includes("top") || zones.includes("bottom");
  return {
    vt: splits.vt,
    vb: fullH ? splits.vt : splits.vb,
    hl: splits.hl,
    hr: fullW ? splits.hl : splits.hr,
  };
}

// Zones that lie left/right or top/bottom of the divider.
const _LEFT_ZONES = ["left", "tl", "bl"];
const _RIGHT_ZONES = ["right", "tr", "br"];
const _TOP_ZONES = ["top", "tl", "tr"];
const _BOTTOM_ZONES = ["bottom", "bl", "br"];

// Target rectangle (px) of a zone, depending on the current splits.
function snapRect(zone) {
  const lay = layer();
  if (!lay) return null;
  const W = lay.clientWidth, H = lay.clientHeight;
  const L = _lines();
  const vt = Math.round(W * L.vt), vb = Math.round(W * L.vb);
  const hl = Math.round(H * L.hl), hr = Math.round(H * L.hr);
  switch (zone) {
    case "max":    return { x: 0, y: 0, w: W, h: H };
    case "left":   return { x: 0, y: 0, w: vt, h: H };
    case "right":  return { x: vt, y: 0, w: W - vt, h: H };
    case "top":    return { x: 0, y: 0, w: W, h: hl };
    case "bottom": return { x: 0, y: hl, w: W, h: H - hl };
    case "tl":     return { x: 0, y: 0, w: vt, h: hl };
    case "tr":     return { x: vt, y: 0, w: W - vt, h: hr };
    case "bl":     return { x: 0, y: hl, w: vb, h: H - hl };
    case "br":     return { x: vb, y: hr, w: W - vb, h: H - hr };
  }
  return null;
}

// Which zone lies under the mouse pointer? (null = none)
function zoneForPointer(cx, cy) {
  const lay = layer();
  if (!lay) return null;
  const r = lay.getBoundingClientRect();
  const x = cx - r.left, y = cy - r.top, W = r.width, H = r.height;
  const T = 14;    // edge proximity in px
  const C = 150;   // corner area along the edge in px
  const nearL = x < T, nearR = x > W - T, nearT = y < T, nearB = y > H - T;
  if (!nearL && !nearR && !nearT && !nearB) return null;
  if (nearT) {
    if (x < C) return "tl";
    if (x > W - C) return "tr";
    if (x > W / 3 && x < (2 * W) / 3) return "max";   // top CENTER = maximize
    return "top";                                       // top on a side = top half
  }
  if (nearB) {
    if (x < C) return "bl";
    if (x > W - C) return "br";
    return "bottom";
  }
  if (nearL) return y < C ? "tl" : (y > H - C ? "bl" : "left");
  return y < C ? "tr" : (y > H - C ? "br" : "right");
}

// Semi-transparent preview of the target zone while dragging.
function showSnapPreview(zone) {
  const lay = layer();
  if (!lay) return;
  if (!zone) {
    if (_snapPreviewEl) { _snapPreviewEl.style.opacity = "0"; }
    return;
  }
  const rect = snapRect(zone);
  if (!rect) return;
  if (!_snapPreviewEl) {
    _snapPreviewEl = document.createElement("div");
    _snapPreviewEl.className = "snap-preview";
    lay.appendChild(_snapPreviewEl);
  }
  _snapPreviewEl.style.opacity = "1";
  _snapPreviewEl.style.left = `${rect.x}px`;
  _snapPreviewEl.style.top = `${rect.y}px`;
  _snapPreviewEl.style.width = `${rect.w}px`;
  _snapPreviewEl.style.height = `${rect.h}px`;
}

// Apply geometry (optionally with a smooth animation via win-animate-geo).
function applySnapGeometry(win, rect, animate = true) {
  if (!win._el || !rect) return;
  if (animate) {
    win._el.classList.add("win-animate-geo");
    setTimeout(() => { if (win._el) win._el.classList.remove("win-animate-geo"); }, 230);
  }
  win.x = rect.x; win.y = rect.y; win.w = rect.w; win.h = rect.h;
  win._el.style.left = `${rect.x}px`;
  win._el.style.top = `${rect.y}px`;
  win._el.style.width = `${rect.w}px`;
  win._el.style.height = `${rect.h}px`;
}

// Snap a window into a zone.
export function snapWindowTo(key, zone, { assist = true, animate = true } = {}) {
  const win = state.windows.find((w) => w.key === key);
  if (!win || !win._el) return;
  const rect = snapRect(zone);
  if (!rect) return;
  // Special case "max" (window pulled up to the top center): that is a REAL
  // maximize, not a snap. Previously win.snap="max" was set here and
  // win.maximized stayed false - a click on the maximize button then
  // "maximized" the window again (no visible change) instead of restoring it.
  // Now the case goes through toggleMaximize so the button correctly brings
  // the window back to its old size.
  if (zone === "max") {
    win.snap = null;
    win.minimized = false;
    win._el.style.display = "flex";
    if (!win.maximized) toggleMaximize(key);
    else { focusWindow(key); notifyChanged(); }
    updateSnapDividers();
    return;
  }
  // Before the FIRST snap, remember the normal geometry (for pulling out).
  if (!win.snap && !win.maximized) {
    win._snapRestore = { x: win.x, y: win.y, w: win.w, h: win.h };
  }
  win.maximized = false;
  win.snap = zone;
  win.minimized = false;
  // When a window snaps across the full height/width, merge the separated
  // segments back onto ONE line (otherwise there would be gaps).
  if (zone === "left" || zone === "right") splits.vb = splits.vt;
  if (zone === "top" || zone === "bottom") splits.hr = splits.hl;
  win._el.style.display = "flex";
  applySnapGeometry(win, rect, animate);
  focusWindow(key);
  updateSnapDividers();
  notifyChanged();
  if (assist) showSnapAssist(win, zone);
}

// Adapt all snapped windows to the current splits / layer size.
function relayoutSnapped(animate = false) {
  for (const win of state.windows) {
    if (win.snap && !win.minimized) {
      applySnapGeometry(win, snapRect(win.snap), animate);
    }
  }
}

// -----------------------------------------------------------------
// Shared borders: invisible handles on the dividers. Dragging moves the split
// and resizes ALL adjacent windows live.
// -----------------------------------------------------------------
function updateSnapDividers() {
  const lay = layer();
  if (!lay) return;
  // Remove old handles and rebuild from the current layout.
  _dividerEls.forEach((el) => { try { el.remove(); } catch {} });
  _dividerEls = [];

  const zones = _activeZones();
  const has = (z) => zones.includes(z);
  const W = lay.clientWidth, H = lay.clientHeight;
  const L = _lines();
  const fullH = has("left") || has("right");   // is a full-height window present?
  const fullW = has("top") || has("bottom");   // is a full-width window present?

  // Who borders the vertical line, and where?
  const leftTop = has("tl") || has("left"),  rightTop = has("tr") || has("right");
  const leftBot = has("bl") || has("left"),  rightBot = has("br") || has("right");
  // ... and the horizontal line?
  const topLeft = has("tl") || has("top"),   botLeft = has("bl") || has("bottom");
  const topRight = has("tr") || has("top"),  botRight = has("br") || has("bottom");

  const segs = [];
  const hMid = H * ((L.hl + L.hr) / 2);   // handover point between the upper/lower v-segment
  const vMid = W * ((L.vt + L.vb) / 2);   // ... between the left/right h-segment

  // Vertical segments. keys = which splits this segment controls: if a
  // full-height window borders it, the handle moves BOTH (vt+vb) -> the whole
  // line; otherwise only its own segment (e.g. just the border of the lower two).
  const vBoth = leftTop && rightTop && leftBot && rightBot;
  if (leftTop && rightTop) {
    segs.push({ axis: "v", keys: fullH ? ["vt", "vb"] : ["vt"],
                x: W * L.vt, y0: 0, y1: vBoth ? hMid : H });
  }
  if (leftBot && rightBot) {
    segs.push({ axis: "v", keys: fullH ? ["vt", "vb"] : ["vb"],
                x: W * L.vb, y0: vBoth ? hMid : 0, y1: H });
  }
  // Horizontal segments (analogous, coupled for full-width windows).
  const hBoth = topLeft && botLeft && topRight && botRight;
  if (topLeft && botLeft) {
    segs.push({ axis: "h", keys: fullW ? ["hl", "hr"] : ["hl"],
                y: H * L.hl, x0: 0, x1: hBoth ? vMid : W });
  }
  if (topRight && botRight) {
    segs.push({ axis: "h", keys: fullW ? ["hl", "hr"] : ["hr"],
                y: H * L.hr, x0: hBoth ? vMid : 0, x1: W });
  }

  for (const seg of segs) {
    const el = document.createElement("div");
    el.className = "snap-divider " + (seg.axis === "v" ? "snap-divider-v" : "snap-divider-h");
    if (seg.axis === "v") {
      el.style.left = `${Math.round(seg.x) - 4}px`;
      el.style.top = `${Math.round(seg.y0)}px`;
      el.style.width = "8px";
      el.style.height = `${Math.round(seg.y1 - seg.y0)}px`;
    } else {
      el.style.top = `${Math.round(seg.y) - 4}px`;
      el.style.left = `${Math.round(seg.x0)}px`;
      el.style.height = "8px";
      el.style.width = `${Math.round(seg.x1 - seg.x0)}px`;
    }
    _makeDividerDraggable(el, seg.axis, seg.keys);
    lay.appendChild(el);
    _dividerEls.push(el);
  }
}

function _makeDividerDraggable(el, axis, keys) {
  el.addEventListener("contextmenu", (e) => e.preventDefault());
  el.addEventListener("mousedown", (e) => {
    e.preventDefault();
    e.stopPropagation();
    // LEFT-CLICK: move the whole divider -> ALL adjacent windows are adjusted
    //   together (Windows default).
    // RIGHT-CLICK: only this segment -> ONLY the two windows at this border.
    const rightClick = e.button === 2;
    const affected = rightClick ? keys
      : (axis === "v" ? ["vt", "vb"] : ["hl", "hr"]);
    el.classList.add("dragging");
    document.body.style.userSelect = "none";
    document.body.style.cursor = axis === "v" ? "ew-resize" : "ns-resize";

    function onMove(ev) {
      const lay = layer();
      if (!lay) return;
      const r = lay.getBoundingClientRect();
      const frac = axis === "v"
        ? Math.min(0.85, Math.max(0.15, (ev.clientX - r.left) / r.width))
        : Math.min(0.85, Math.max(0.15, (ev.clientY - r.top) / r.height));
      for (const k of affected) splits[k] = frac;
      relayoutSnapped(false);   // live, without animation (follows the cursor)
      // Do NOT rebuild the handles (that would abort this drag), only move the
      // active handle along.
      if (axis === "v") el.style.left = `${Math.round(r.width * frac) - 4}px`;
      else el.style.top = `${Math.round(r.height * frac) - 4}px`;
    }
    function onUp() {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      el.classList.remove("dragging");
      document.body.style.userSelect = "";
      document.body.style.cursor = "";
      updateSnapDividers();   // recompute segments/lengths cleanly now
      notifyChanged();
    }
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
}

// -----------------------------------------------------------------
// Snap assist: free neighbour zones show a choice of the remaining windows
// (like Windows) - a click snaps the chosen window there.
// -----------------------------------------------------------------
const _COMPLEMENTS = {
  left: ["right"], right: ["left"], top: ["bottom"], bottom: ["top"],
  tl: ["tr", "bl", "br"], tr: ["tl", "br", "bl"],
  bl: ["br", "tl", "tr"], br: ["bl", "tr", "tl"], max: [],
};

export function clearSnapAssist() {
  _assistEls.forEach((el) => { try { el.remove(); } catch {} });
  _assistEls = [];
  document.removeEventListener("keydown", _assistEsc, true);
  document.removeEventListener("mousedown", _assistOutside, true);
  window.removeEventListener("blur", clearSnapAssist);
}
function _assistEsc(e) { if (e.key === "Escape") clearSnapAssist(); }

// A click ANYWHERE outside the suggestion areas closes ALL suggestions.
// Previously they stayed up when you clicked e.g. into a window, onto the
// desktop or into the taskbar. Capture phase, so it also fires when the target
// stops the click itself.
function _assistOutside(e) {
  if (!_assistEls.length) return;
  // Clicks INSIDE a suggestion area are handled by showSnapAssist itself
  // (tile = snap the window, free area = close only that zone).
  for (const el of _assistEls) {
    if (el === e.target || el.contains(e.target)) return;
  }
  clearSnapAssist();
}

function showSnapAssist(sourceWin, zone) {
  clearSnapAssist();
  const lay = layer();
  if (!lay) return;
  const occupied = new Set(
    state.windows.filter((w) => w.snap && w.key !== sourceWin.key).map((w) => w.snap)
  );
  const empty = (_COMPLEMENTS[zone] || []).filter((z) => !occupied.has(z));
  // Candidates: all other windows that are not snapped yet.
  let candidates = state.windows.filter((w) => w.key !== sourceWin.key && !w.snap);
  if (!empty.length || !candidates.length) return;

  for (const z of empty) {
    const rect = snapRect(z);
    if (!rect) continue;
    const overlay = document.createElement("div");
    overlay.className = "snap-assist";
    overlay.style.left = `${rect.x + 8}px`;
    overlay.style.top = `${rect.y + 8}px`;
    overlay.style.width = `${rect.w - 16}px`;
    overlay.style.height = `${rect.h - 16}px`;
    overlay.dataset.zone = z;
    lay.appendChild(overlay);
    _assistEls.push(overlay);
  }
  if (!_assistEls.length) return;

  function renderTiles() {
    candidates = candidates.filter((w) => state.windows.includes(w) && !w.snap);
    for (const overlay of [..._assistEls]) {
      overlay.innerHTML = "";
      if (!candidates.length) { clearSnapAssist(); return; }
      for (const cand of candidates) {
        const tile = document.createElement("button");
        tile.className = "snap-assist-tile";
        tile.innerHTML = `${cand.clientColor ? `<span class="client-dot" style="background:${esc(cand.clientColor)}"></span>` : ""}<span>${esc(cand.title)}</span>`;
        tile.addEventListener("click", (e) => {
          e.stopPropagation();
          const targetZone = overlay.dataset.zone;
          overlay.remove();
          _assistEls = _assistEls.filter((x) => x !== overlay);
          snapWindowTo(cand.key, targetZone, { assist: false });
          if (_assistEls.length) renderTiles(); else clearSnapAssist();
        });
        overlay.appendChild(tile);
      }
      // Click on the free area = cancel the choice (only this zone).
      overlay.addEventListener("mousedown", (e) => {
        if (e.target === overlay) {
          overlay.remove();
          _assistEls = _assistEls.filter((x) => x !== overlay);
          if (!_assistEls.length) clearSnapAssist();
        }
      });
    }
  }
  renderTiles();
  document.addEventListener("keydown", _assistEsc, true);
  document.addEventListener("mousedown", _assistOutside, true);
  // If focus leaves the window (tab switch), clean up as well.
  window.addEventListener("blur", clearSnapAssist);
}

// ------------------------------------------------------------------
// Window menu (right-click on the title bar). Same actions as the taskbar;
// inline here to avoid import cycles with taskbar.js.
// ------------------------------------------------------------------
export function windowMenu(key) {
  const win = state.windows.find((w) => w.key === key);
  if (!win) return null;
  const snap = (zone) => () => { if (win.minimized) toggleMinimize(key); snapWindowTo(key, zone); };
  return [
    { header: win.title },
    { separator: true },
    { label: win.maximized ? "Restore" : "Maximize", icon: win.maximized ? "🗗" : "🗖",
      onClick: () => { toggleMaximize(key); } },
    { label: "Minimize", icon: "🔽", onClick: () => { toggleMinimize(key); } },
    { label: "Dock", icon: "▦", submenu: [
      { label: "Left half", icon: "◧", onClick: snap("left") },
      { label: "Right half", icon: "◨", onClick: snap("right") },
      { label: "Top half", icon: "▀", onClick: snap("top") },
      { label: "Bottom half", icon: "▄", onClick: snap("bottom") },
    ] },
    { label: win.pinned ? "Unpin (no longer on top)" : "Always on top", icon: "📌",
      onClick: () => { togglePin(key); } },
    ...(state.windows.some((w) => w.appId === "workbay") && win.appId !== "workbay"
      ? [{ label: "Park in Work Bay", icon: "🗃️", onClick: () => {
            parkWindow(key);
            const bay = state.windows.find((w) => w.appId === "workbay");
            if (bay) focusWindow(bay.key);
          } }]
      : []),
    { separator: true },
    { label: "Open in new window", icon: "🗔", onClick: () => {
      openWindow({ key: win.appId + "-" + Math.random().toString(36).slice(2, 7), appId: win.appId,
                   title: win.title, props: { ...(win.props || {}) }, clientColor: win.clientColor, w: win.w, h: win.h });
    } },
    { separator: true },
    { label: "Close", icon: "✖️", danger: true, onClick: () => { closeWindow(key); } },
  ];
}
