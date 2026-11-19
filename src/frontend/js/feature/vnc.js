// apps/vnc.js
// -----------
// Full remote screen: receives live screen frames from the agent
// (as JPEG base64 over Socket.IO) and displays them. Mouse and keyboard
// input is sent back to the agent, which executes it locally.
//
// Flow:
//   1. Window opens -> "screen-start" to the backend -> agent starts streaming
//   2. Incoming "screen-frame" events -> set the image into <img>
//   3. Mouse/keyboard on the image -> convert coordinates -> send "screen-input"
//   4. Window closes -> send "screen-stop" (see cleanup in app.js)

import { dashboardSocket } from "../app/socket.js";
import { attachPinchZoom } from "../app/touchgestures.js";
import { registerCleanup } from "../app/windowmanager.js";
import { state } from "../app/state.js";
import { api } from "../app/api.js";
import { esc, mapKeyboardText, uiPrompt } from "../app/utils.js";
import { renderTerminal } from "./terminal.js";
import { t } from "../app/i18n.js";

export function renderVnc(body, win) {
  const { clientId, clientName } = win.props;
  // Copy URL: remote support of this device (currently shown monitor + parameters).
  win.getLink = () => {
    const p = [];
    const m = (typeof monitorIndex === "number" && monitorIndex > 0) ? monitorIndex
      : parseInt(win.props?.screen, 10);
    if (!isNaN(m) && m) p.push("screen=" + m);
    if (win.props?.fps) p.push("fps=" + win.props.fps);
    if (win.props?.resolution) p.push("resolution=" + win.props.resolution);
    return `bytera://agent/${encodeURIComponent(clientId)}/remotescreen` + (p.length ? "/" + p.join("&") : "");
  };
  // All control events to the device carry the username - the backend
  // uses it to check the use_screen permission (screen-input was unchecked before).
  const _me = () => state.user?.username || "";
  const sendInput = (d) => dashboardSocket.emit("screen-input", { clientId, username: _me(), ...d });
  const getClip = () => dashboardSocket.emit("screen-clipboard-get", { clientId, username: _me() });
  const setMonitor = (monitor) => dashboardSocket.emit("screen-set-monitor", { clientId, username: _me(), monitor });
  const stopScreen = () => dashboardSocket.emit("screen-stop", { clientId, username: _me() });

  // IMPORTANT about layout: the window body (.rmm-window-body) is a flex item
  // with "flex:1" and "overflow:auto", so it has NO fixed height. A
  // "height:100%" on it goes nowhere (the height depends on the content, the
  // content on the height). That was exactly the cause of the off-center image: the
  // image area grew to the full image height, "max-height:100%" never took effect,
  // the image was stretched to the full window width and cut off at the
  // bottom - only the middle part was visible (the lower
  // "send text" bar was therefore pushed out of the window too).
  // Solution: anchor absolutely in the body (which is position:relative) - so
  // the frame ALWAYS uses the real window size as its reference.
  body.style.overflow = "hidden";
  body.innerHTML = `
    <div style="position:absolute;inset:0;display:flex;flex-direction:column;background:#000">
      <div style="display:flex;align-items:center;gap:10px;padding:6px 10px;background:var(--panel-2);font-size:12px;flex-wrap:wrap">
        <button class="btn bar-opts-toggle" title="Optionen ein-/ausklappen">⚙️</button>
        <span id="vnc-status-${win.key}" style="color:var(--subtext)">${t("vnc_connecting")}</span>
        <button class="btn" id="vnc-fs-${win.key}" title="${t("vnc_fs_title")}">${t("vnc_fullscreen")}</button>
        <span style="flex:1"></span>
        <span class="bar-opts">
        <label style="color:var(--subtext);display:flex;align-items:center;gap:4px">
          <input type="checkbox" id="vnc-control-${win.key}" checked /> ${t("vnc_control_active")}
        </label>
        <select id="vnc-monitor-${win.key}" style="display:none"
          title="${t("u_bildschirm_des_remote_pcs_auswahle")}"></select>
        <span style="width:1px;height:16px;background:var(--border)"></span>
        <button class="btn" id="vnc-mod-ctrl-${win.key}" title="${t("u_strg_gedruckt_halten_toggle_wirkt_")}">Ctrl</button>
        <button class="btn" id="vnc-mod-alt-${win.key}" title="${t("u_alt_gedruckt_halten_toggle_wirkt_a")}">Alt</button>
        <button class="btn" id="vnc-mod-win-${win.key}" title="${t("u_windows_taste_gedruckt_halten_togg")}">Win</button>
        <button class="btn" id="vnc-key-tab-${win.key}" title="Send Tab">Tab</button>
        <button class="btn" id="vnc-key-esc-${win.key}" title="Send Esc">Esc</button>
        <button class="btn" id="vnc-ctrlaltdel-${win.key}">Ctrl+Alt+Del</button>
        <span style="width:1px;height:16px;background:var(--border)"></span>
        <button class="btn" id="vnc-clip-send-${win.key}" title="${t("vnc_clip_send_title")}">📋→</button>
        <button class="btn" id="vnc-clip-get-${win.key}" title="${t("vnc_clip_get_title")}">→📋</button>
        <span style="width:1px;height:16px;background:var(--border)"></span>
        <button class="btn" id="vnc-audio-${win.key}" title="${t("vnc_audio_title")}">🔇</button>
        <button class="btn" id="vnc-hist-${win.key}" title="${t("vnc_hist_title")}">🕘</button>
        <button class="btn" id="vnc-files-${win.key}" title="${t("vnc_files_title")}">📁</button>
        <input type="file" id="vnc-fileinput-${win.key}" multiple style="display:none" />
        <button class="btn" id="vnc-chat-${win.key}" title="${t("vnc_chat_title")}">💬 <span id="vnc-chat-badge-${win.key}" style="display:none;background:var(--danger);color:#fff;border-radius:8px;padding:0 5px;font-size:10px">0</span></button>
        </span>
      </div>
      <div id="vnc-area-${win.key}" style="flex:1 1 0;min-height:0;min-width:0;overflow:hidden;position:relative">
        <img id="vnc-img-${win.key}" style="position:absolute;left:0;top:0;display:block"
             tabindex="0" />
        <div id="vnc-histpanel-${win.key}" style="display:none;position:absolute;top:0;left:0;bottom:0;width:360px;max-width:80%;background:var(--panel);border-right:1px solid var(--border);flex-direction:column;z-index:21">
          <div style="display:flex;align-items:center;gap:6px;padding:8px 10px;border-bottom:1px solid var(--border);font-size:13px">
            <b style="flex:1">🕘 ${t("vnc_hist_title")}</b>
            <button class="btn btn-sm" id="vnc-histclose-${win.key}">✕</button>
          </div>
          <div id="vnc-histlist-${win.key}" style="flex:1;overflow:auto;padding:8px 10px;font-size:12px"></div>
        </div>
        <div id="vnc-chatpanel-${win.key}" style="display:none;position:absolute;top:0;right:0;bottom:0;width:300px;max-width:70%;background:var(--panel);border-left:1px solid var(--border);display:none;flex-direction:column;z-index:20">
          <div style="display:flex;align-items:center;gap:6px;padding:8px 10px;border-bottom:1px solid var(--border);font-size:13px">
            <b style="flex:1">💬 ${t("vnc_chat_title")}</b>
            <span id="vnc-grant-${win.key}" style="font-size:11px;color:var(--subtext)"></span>
            <button class="btn btn-sm" id="vnc-chat-close-${win.key}">✕</button>
          </div>
          <div id="vnc-perms-${win.key}" style="display:flex;gap:6px;flex-wrap:wrap;padding:6px 10px;border-bottom:1px solid var(--border);font-size:11px"></div>
          <div id="vnc-chatlog-${win.key}" style="flex:1;overflow:auto;padding:8px 10px;font-size:12.5px;display:flex;flex-direction:column;gap:6px"></div>
          <div style="display:flex;gap:6px;padding:8px 10px;border-top:1px solid var(--border)">
            <input type="text" id="vnc-chatinput-${win.key}" placeholder="${t("vnc_chat_ph")}" style="flex:1" />
            <button class="btn btn-sm btn-primary" id="vnc-chatsend-${win.key}">${t("send")}</button>
          </div>
          <div style="display:flex;gap:6px;padding:0 10px 10px">
            <button class="btn btn-sm" id="vnc-reqgrant-${win.key}" style="flex:1" title="${t("vnc_chat_request_title")}">🛡️ ${t("vnc_chat_request")}</button>
            <button class="btn btn-sm btn-danger" id="vnc-endsess-${win.key}" style="flex:1">⏹ ${t("vnc_chat_end")}</button>
          </div>
        </div>
      </div>
      <div class="bar-optrow" style="display:flex;align-items:center;gap:6px;padding:6px 10px;background:var(--panel-2);font-size:12px;flex-wrap:wrap">
        <span style="color:var(--subtext)">${t("vnc_send_text")}</span>
        <select id="vnc-layout-${win.key}" title="${t("vnc_layout_title")}"
>
          <option value="raw">${t("vnc_layout_raw")}</option>
          <option value="us">${t("vnc_layout_us")}</option>
          <option value="de">${t("vnc_layout_de")}</option>
        </select>
        <input type="text" id="vnc-text-${win.key}" placeholder="${t("guac_text_ph")}"
          style="flex:1" />
        <button class="btn" id="vnc-send-${win.key}">${t("send")}</button>
      </div>
    </div>
  `;

  const layoutSel = body.querySelector(`#vnc-layout-${win.key}`);
  const textInput = body.querySelector(`#vnc-text-${win.key}`);
  const sendBtn = body.querySelector(`#vnc-send-${win.key}`);

  // The client's real screen resolution (sent with every frame),
  // to convert click coordinates correctly. Must come BEFORE layoutImage().
  let remoteWidth = 1920;
  let remoteHeight = 1080;
  const img = body.querySelector(`#vnc-img-${win.key}`);
  const imgArea = body.querySelector(`#vnc-area-${win.key}`);

  // ---- Fit the image: size AND position are computed here ----------
  // Why not via CSS (max-width/max-height + object-fit:contain + flex
  // centering)? Because then THREE things must match: the
  // element size, the crop chosen by object-fit and the
  // back-transform of the mouse coordinates. As soon as one of them deviates
  // (fullscreen, window resize, a frame with a different size), the image sits
  // crooked AND clicks land off - exactly the failure pattern.
  // Now: the <img> element is ALWAYS exactly as big as the
  // displayed image and is centered itself. That makes
  // getBoundingClientRect() equivalent to the image area and the
  // conversion of clicks a simple division.
  function layoutImage() {
    if (!imgArea || !remoteWidth || !remoteHeight) return;
    const bw = imgArea.clientWidth;
    const bh = imgArea.clientHeight;
    if (bw <= 0 || bh <= 0) return;
    const scale = Math.min(bw / remoteWidth, bh / remoteHeight);
    const w = Math.max(1, Math.round(remoteWidth * scale));
    const h = Math.max(1, Math.round(remoteHeight * scale));
    img.style.width = w + "px";
    img.style.height = h + "px";
    img.style.left = Math.round((bw - w) / 2) + "px";
    img.style.top = Math.round((bh - h) / 2) + "px";
    img.style.cursor = "crosshair";
  }

  // Re-fit when the area changes: drag the window, maximize,
  // fullscreen, expand/collapse the sidebar. ResizeObserver covers all of that,
  // without every place in the window manager having to know about it.
  let areaObserver = null;
  try {
    areaObserver = new ResizeObserver(() => layoutImage());
    areaObserver.observe(imgArea);
  } catch (e) {
    window.addEventListener("resize", layoutImage);
  }

  // ---- Browser fullscreen (desktop + mobile): button toggles, Esc exits ----
  const fsBtnV = body.querySelector(`#vnc-fs-${win.key}`);
  if (fsBtnV) {
    const fsRoot = () => body.closest(".rmm-window") || body;
    const fsLabel = () => { fsBtnV.textContent = document.fullscreenElement ? t("vnc_fullscreen_exit") : t("vnc_fullscreen"); };
    fsBtnV.addEventListener("click", () => {
      if (document.fullscreenElement) { document.exitFullscreen?.(); return; }
      const el = fsRoot();
      (el.requestFullscreen?.() || el.webkitRequestFullscreen?.());
    });
    const onFsChange = () => {
      if (!document.body.contains(fsBtnV)) { document.removeEventListener("fullscreenchange", onFsChange); return; }
      fsLabel();
    };
    document.addEventListener("fullscreenchange", onFsChange);
    fsLabel();
  }
  const statusEl = body.querySelector(`#vnc-status-${win.key}`);
  const controlToggle = body.querySelector(`#vnc-control-${win.key}`);
  const ctrlAltDelBtn = body.querySelector(`#vnc-ctrlaltdel-${win.key}`);
  const monitorSel = body.querySelector(`#vnc-monitor-${win.key}`);

  // Switch screen (multi-monitor): direct selection via dropdown.
  monitorSel.addEventListener("change", () => {
    const next = parseInt(monitorSel.value, 10);   // 0 = all screens
    if (!isNaN(next) && next !== monitorIndex) {
      setMonitor(next);
    }
  });

  // --- Sticky modifiers (like in Proxmox): Ctrl/Alt/Win as a toggle. Active
  //     modifiers are applied to all following keyboard input. ---
  const modState = { Control: false, Alt: false, Meta: false };
  const modBtns = {
    Control: body.querySelector(`#vnc-mod-ctrl-${win.key}`),
    Alt: body.querySelector(`#vnc-mod-alt-${win.key}`),
    Meta: body.querySelector(`#vnc-mod-win-${win.key}`),
  };
  function updateModBtn(name) {
    const b = modBtns[name];
    if (!b) return;
    b.style.background = modState[name] ? "var(--accent)" : "";
    b.style.color = modState[name] ? "#0b0f14" : "";
  }
  Object.keys(modBtns).forEach((name) => {
    modBtns[name].addEventListener("click", () => {
      modState[name] = !modState[name];
      updateModBtn(name);
      img.focus({ preventScroll: true });
    });
    // Double-click: press+release the key ONCE immediately (e.g. Win opens the
    // start menu). The two single clicks before toggled it on+off,
    // so set it to "off" here to be safe.
    modBtns[name].addEventListener("dblclick", (e) => {
      e.preventDefault();
      modState[name] = false;
      updateModBtn(name);
      sendInput({ type: "key", key: name });
      img.focus({ preventScroll: true });
    });
  });
  function activeMods() {
    return Object.keys(modState).filter((k) => modState[k]);
  }
  // Sends a key taking the active toggles into account. After a
  // combination the toggles are released automatically (like in Proxmox).
  function sendKeyWithMods(key) {
    const mods = activeMods();
    if (mods.length) {
      sendInput({ type: "combo", keys: [...mods, key] });
      releaseMods();
    } else if (key.length === 1) {
      sendInput({ type: "text", text: key });
    } else {
      sendInput({ type: "key", key });
    }
  }
  function releaseMods() {
    Object.keys(modState).forEach((k) => { modState[k] = false; updateModBtn(k); });
  }
  body.querySelector(`#vnc-key-tab-${win.key}`).addEventListener("click", () => { sendKeyWithMods("Tab"); img.focus({ preventScroll: true }); });
  body.querySelector(`#vnc-key-esc-${win.key}`).addEventListener("click", () => { sendKeyWithMods("Escape"); img.focus({ preventScroll: true }); });

  // --- Synchronize the clipboard ---
  // Send: read the local clipboard -> agent sets it on the remote PC.
  body.querySelector(`#vnc-clip-send-${win.key}`).addEventListener("click", async () => {
    let text = "";
    try { text = await navigator.clipboard.readText(); } catch {}
    if (!text) {
      window.notify?.(t("guac_clip_empty"), "warn");
      return;
    }
    sendInput({ type: "clipboard-set", text });
    window.notify?.(t("vnc_clip_sent"), "success");
  });
  // Get: agent reads the remote clipboard -> write it into the local one here.
  body.querySelector(`#vnc-clip-get-${win.key}`).addEventListener("click", () => {
    getClip();
  });
  // Automatic sync in BOTH directions (like AnyDesk):
  //  * device -> here: the agent pushes changes on its own (push=true) -
  //    they land silently in the local clipboard.
  //  * here -> device: when the window is (re)focused, the local
  //    clipboard is read and, if changed, sent to the device.
  // _clipLast prevents ping-pong (last synced text in both directions).
  let _clipLast = null;
  async function onClipboard(data) {
    if (data.id !== clientId) return;
    if (data.error) { window.notify?.(t("vnc_clip_remote") + ": " + (data.code ? t(data.code) : data.error), "error"); return; }
    const text = data.text || "";
    if (data.push) {
      // Automatic push from the device: apply silently, no dialog spam.
      if (!text || text === _clipLast) return;
      _clipLast = text;
      try {
        await navigator.clipboard.writeText(text);
        window.notify?.("📋 " + t("u_remote_zwischenablage_ubernommen") + text.length + " chars)", "info", 2500);
      } catch { /* window not focused / HTTP: the next push tries again */ }
      return;
    }
    _clipLast = text;
    try {
      await navigator.clipboard.writeText(text);
      window.notify?.(t("u_remote_zwischenablage_ubernommen") + text.length + " chars)", "success");
    } catch {
      // Clipboard API blocked (e.g. HTTP): show the text for manual copying.
      uiPrompt(t("vnc_clip_remote"), {
        description: t("guac_copy_manual"),
        value: text, okText: t("close") });
    }
  }
  dashboardSocket.on("screen-clipboard", onClipboard);

  // Send local changes to the device when the window is focused.
  async function _clipSyncLocal() {
    let text = "";
    try { text = await navigator.clipboard.readText(); } catch { return; }
    if (!text || text === _clipLast || text.length > 512 * 1024) return;
    _clipLast = text;
    sendInput({ type: "clipboard-set", text });
  }
  const _onClipFocus = () => { _clipSyncLocal(); };
  window.addEventListener("focus", _onClipFocus);
  body.addEventListener("focusin", _onClipFocus);
  // The listener is removed in the central cleanup block further below
  // (registerCleanup overwrites per window - so do not register it here).

  // --- Chat/control window of the remote session (user <-> admin) ---
  // The person at the PC gets a window (only when NOT in silent mode);
  // this is the admin side: chat, display of the granted admin rights and a
  // notice when the user disconnects.
  const chatPanel = body.querySelector(`#vnc-chatpanel-${win.key}`);
  const chatLog = body.querySelector(`#vnc-chatlog-${win.key}`);
  const chatInput = body.querySelector(`#vnc-chatinput-${win.key}`);
  const chatBadge = body.querySelector(`#vnc-chat-badge-${win.key}`);
  const grantLbl = body.querySelector(`#vnc-grant-${win.key}`);
  let chatOpen = false, unread = 0;
  function chatSetOpen(open) {
    chatOpen = open;
    chatPanel.style.display = open ? "flex" : "none";
    if (open) { unread = 0; chatBadge.style.display = "none"; chatInput.focus(); layoutImage(); }
    else layoutImage();
  }
  function addChat(sender, text, sysMsg) {
    const row = document.createElement("div");
    if (sysMsg) {
      row.style.cssText = "color:var(--subtext);font-style:italic;text-align:center;font-size:11.5px";
      row.textContent = text;
    } else {
      const mine = sender === "admin";
      row.style.cssText = `align-self:${mine ? "flex-end" : "flex-start"};max-width:80%`;
      row.innerHTML = `<div style="font-size:10px;color:var(--subtext);margin-bottom:2px">${mine ? t("vnc_chat_you") : t("vnc_chat_user")}</div>`
        + `<div style="background:${mine ? "var(--accent)" : "var(--panel-2)"};color:${mine ? "#0b0f14" : "var(--text)"};border-radius:10px;padding:6px 9px">${esc(text)}</div>`;
    }
    chatLog.appendChild(row);
    chatLog.scrollTop = chatLog.scrollHeight;
    if (!chatOpen && !sysMsg) { unread++; chatBadge.textContent = unread; chatBadge.style.display = "inline-block"; }
  }
  function sendChat() {
    const txt = chatInput.value.trim();
    if (!txt) return;
    chatInput.value = "";
    addChat("admin", txt);
    dashboardSocket.emit("screen-session-chat", { clientId, username: state.user?.username || "", text: txt });
  }
  body.querySelector(`#vnc-chat-${win.key}`).addEventListener("click", () => chatSetOpen(!chatOpen));
  body.querySelector(`#vnc-chat-close-${win.key}`).addEventListener("click", () => chatSetOpen(false));
  body.querySelector(`#vnc-chatsend-${win.key}`).addEventListener("click", sendChat);
  chatInput.addEventListener("keydown", (e) => { if (e.key === "Enter") sendChat(); });

  // Permissions the person at the PC toggles in the session window (like AnyDesk):
  // control (mouse/keyboard), clipboard, administrator rights.
  const permsEl = body.querySelector(`#vnc-perms-${win.key}`);
  const reqBtn = body.querySelector(`#vnc-reqgrant-${win.key}`);
  let perms = { input: true, clipboard: true, files: true, admin: false };
  function renderPerms() {
    const chip = (on, icon, label) =>
      `<span style="padding:2px 7px;border-radius:9px;background:${on ? "rgba(46,125,87,.25)" : "rgba(180,69,90,.22)"};`
      + `color:${on ? "var(--success, #3fb27f)" : "var(--danger)"}">${icon} ${label}</span>`;
    permsEl.innerHTML = chip(perms.input, "🖱️", t(perms.input ? "vnc_perm_input_on" : "vnc_perm_input_off"))
      + chip(perms.clipboard, "📋", t(perms.clipboard ? "vnc_perm_clip_on" : "vnc_perm_clip_off"))
      + chip(perms.files !== false, "📁", t(perms.files !== false ? "vnc_perm_files_on" : "vnc_perm_files_off"))
      + chip(perms.admin, "🛡️", t(perms.admin ? "vnc_perm_admin_on" : "vnc_perm_admin_off"));
    reqBtn.disabled = !!perms.admin;
    grantLbl.textContent = perms.admin ? "🛡️" : "";
  }
  renderPerms();
  reqBtn.addEventListener("click", () => {
    dashboardSocket.emit("screen-session-request-grant", { clientId, username: state.user?.username || "" });
    addChat(null, t("vnc_chat_requested"), true);
  });
  body.querySelector(`#vnc-endsess-${win.key}`).addEventListener("click", () => {
    // Close the window -> cleanup sends screen-stop -> agent ends the session
    import("../app/windowmanager.js").then(({ closeWindow }) => closeWindow(win.key));
  });

  function onSessChat(d) { if (d.id !== clientId) return; addChat("user", d.text || ""); }
  function onSessGrant(d) {
    if (d.id !== clientId) return;
    perms.admin = !!d.granted;
    renderPerms();
    let msg = d.granted ? t("vnc_chat_granted") : t("vnc_chat_revoked");
    if (d.granted && d.silent) msg = t("vnc_chat_granted_silent");
    if (d.denied) msg = t("vnc_chat_denied");
    addChat(null, msg, true);
    if (d.granted) window.notify?.(msg, "success");
    else if (d.denied) window.notify?.(msg, "warning");
  }
  function onSessPerms(d) {
    if (d.id !== clientId || !d.perms) return;
    const before = { ...perms };
    perms = { ...perms, ...d.perms };
    renderPerms();
    if (before.input !== perms.input) {
      const m = t(perms.input ? "vnc_perm_input_on" : "vnc_perm_input_off");
      addChat(null, m, true);
      window.notify?.(m, perms.input ? "info" : "warning");
    }
    if (before.clipboard !== perms.clipboard) addChat(null, t(perms.clipboard ? "vnc_perm_clip_on" : "vnc_perm_clip_off"), true);
    if (before.files !== perms.files) addChat(null, t(perms.files !== false ? "vnc_perm_files_on" : "vnc_perm_files_off"), true);
  }

  // --- File transfer (like AnyDesk) ---
  // Admin -> user: choose a file or drag it onto the image. It is sent in
  // chunks of 256 KB; each chunk is acknowledged by the device, only then does
  // the next one go out. The file ends up in the user's "Downloads" folder.
  // User -> admin: "📎" in the session window -> the file arrives here in chunks
  // and is offered as a download.
  const FILE_CHUNK = 256 * 1024;
  const FILE_MAX = 1024 * 1024 * 1024;
  const fileInput = body.querySelector(`#vnc-fileinput-${win.key}`);
  const pendingAcks = new Map();          // "tid:seq" -> {resolve, reject, timer}
  let fileQueue = Promise.resolve();
  function fileSysRow(text) {
    addChat(null, text, true);
    return chatLog.lastElementChild;
  }
  function waitAck(tid, seq) {
    return new Promise((resolve, reject) => {
      const key = `${tid}:${seq}`;
      const timer = setTimeout(() => { pendingAcks.delete(key); reject(new Error("timeout")); }, 60000);
      pendingAcks.set(key, { resolve, reject, timer });
    });
  }
  function onFileAck(d) {
    if (d.id !== clientId) return;
    const key = `${d.tid}:${d.seq}`;
    const p = pendingAcks.get(key);
    if (!p) return;
    pendingAcks.delete(key);
    clearTimeout(p.timer);
    if (d.error) p.reject(new Error(d.code ? t(d.code) : d.error)); else p.resolve(d);
  }
  function b64Of(buf) {
    const bytes = new Uint8Array(buf);
    let bin = "";
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(bin);
  }
  async function sendOneFile(file) {
    if (perms.files === false) { window.notify?.(t("vnc_perm_files_off"), "warning"); return; }
    if (file.size > FILE_MAX) { window.notify?.(t("vnc_file_too_big"), "error"); return; }
    if (!chatOpen) chatSetOpen(true);
    const row = fileSysRow(`📤 ${file.name} – 0 %`);
    const tid = `a${Date.now().toString(36)}${Math.random().toString(36).slice(2, 7)}`;
    const total = Math.max(1, Math.ceil(file.size / FILE_CHUNK));
    try {
      for (let seq = 0; seq < total; seq++) {
        const buf = await file.slice(seq * FILE_CHUNK, (seq + 1) * FILE_CHUNK).arrayBuffer();
        const ack = waitAck(tid, seq);
        dashboardSocket.emit("screen-file-chunk", {
          clientId, username: state.user?.username || "", tid, seq, name: file.name, size: file.size,
          data: b64Of(buf), last: seq === total - 1,
        });
        await ack;
        row.textContent = `📤 ${file.name} – ${Math.round(((seq + 1) / total) * 100)} %`;
      }
      row.textContent = `📤 ${t("vnc_file_sent", { name: file.name })}`;
    } catch (e) {
      row.textContent = `⚠️ ${t("vnc_file_failed", { name: file.name })}: ${e.message || e}`;
      row.style.color = "var(--danger)";
    }
  }
  function sendFiles(list) {
    for (const f of Array.from(list || [])) fileQueue = fileQueue.then(() => sendOneFile(f));
  }
  body.querySelector(`#vnc-files-${win.key}`).addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => { sendFiles(fileInput.files); fileInput.value = ""; });
  const dropArea = body.querySelector(`#vnc-area-${win.key}`);
  dropArea.addEventListener("dragover", (e) => {
    if (e.dataTransfer?.types?.includes("Files")) { e.preventDefault(); dropArea.style.outline = "2px dashed var(--accent)"; }
  });
  dropArea.addEventListener("dragleave", () => { dropArea.style.outline = ""; });
  dropArea.addEventListener("drop", (e) => {
    if (!e.dataTransfer?.files?.length) return;
    e.preventDefault();
    dropArea.style.outline = "";
    sendFiles(e.dataTransfer.files);
  });

  // --- Audio (like AnyDesk "transmit sound") ---
  // The device sends 100 ms chunks (16 kHz mono, 8-bit mu-law). Here they are
  // decoded and played back seamlessly one after another (Web Audio). If
  // playback lags more than 0.6 s behind, it restarts (do not build up
  // delay). Default: OFF - the admin turns it on deliberately, the user
  // sees it in the session window.
  const audioBtn = body.querySelector(`#vnc-audio-${win.key}`);
  let audioOn = false, audioCtx = null, audioNext = 0;
  const MU_TABLE = new Float32Array(256);
  for (let i = 0; i < 256; i++) {
    const y = i / 127.5 - 1;
    MU_TABLE[i] = Math.sign(y) * (Math.pow(256, Math.abs(y)) - 1) / 255;
  }
  function setAudio(on) {
    audioOn = on;
    audioBtn.textContent = on ? "🔊" : "🔇";
    audioBtn.classList.toggle("btn-primary", on);
    if (on && !audioCtx) {
      try { audioCtx = new (window.AudioContext || window.webkitAudioContext)(); } catch { audioCtx = null; }
    }
    if (audioCtx) { on ? audioCtx.resume() : audioCtx.suspend(); }
    audioNext = 0;
  }
  audioBtn.addEventListener("click", () => {
    const on = !audioOn;
    setAudio(on);                         // create inside the click: browsers allow audio only after a user action
    dashboardSocket.emit("screen-audio", { clientId, username: state.user?.username || "", enabled: on });
  });
  function onAudio(d) {
    if (d.id !== clientId) return;
    if (d.error) {
      setAudio(false);
      window.notify?.(t("vnc_audio_unavailable") + (d.code ? "" : ": " + d.error), "warning");
      return;
    }
    if (!d.data || !audioOn || !audioCtx) return;
    let bin;
    try { bin = atob(d.data); } catch { return; }   // ignore a corrupt audio chunk
    const rate = d.rate || 16000;
    const buf = audioCtx.createBuffer(1, bin.length, rate);
    const ch = buf.getChannelData(0);
    for (let i = 0; i < bin.length; i++) ch[i] = MU_TABLE[bin.charCodeAt(i)];
    const src = audioCtx.createBufferSource();
    src.buffer = buf;
    src.connect(audioCtx.destination);
    const now = audioCtx.currentTime;
    if (audioNext < now + 0.02 || audioNext > now + 0.6) audioNext = now + 0.08;
    src.start(audioNext);
    audioNext += buf.duration;
  }

  // --- Session history ---
  const histPanel = body.querySelector(`#vnc-histpanel-${win.key}`);
  const histList = body.querySelector(`#vnc-histlist-${win.key}`);
  const EVENT_LABEL = {
    "screen.file_sent": "📤", "screen.file_received": "📥", "screen.admin_granted": "🛡️",
    "screen.admin_revoked": "🛡️✕", "screen.admin_denied": "🛡️✕", "screen.admin_requested": "🛡️?",
    "screen.audio_on": "🔊", "screen.audio_off": "🔇", "screen.silent_used": "🤫",
  };
  function fmtDur(s) {
    if (s == null) return t("vnc_hist_running");
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(sec).padStart(2, "0");
  }
  async function loadHistory() {
    histList.innerHTML = `<div style="color:var(--subtext)">${esc(t("loading"))}</div>`;
    try {
      const res = await api.getScreenHistory(clientId);
      const list = res.sessions || [];
      if (!list.length) { histList.innerHTML = `<div style="color:var(--subtext)">${esc(t("vnc_hist_empty"))}</div>`; return; }
      histList.innerHTML = list.map((s) => {
        const ev = (s.events || []).map((e) =>
          `<div style="color:var(--subtext);margin-left:10px">${new Date(e.ts).toLocaleTimeString()} ${EVENT_LABEL[e.action] || "•"} `
          + `${esc(t("vnc_ev_" + e.action.slice(7)))}`
          + `${e.details && !e.details.startsWith("rec:") ? " – " + esc(e.details) : ""}</div>`).join("");
        const by = s.ended_by === "user" ? t("vnc_hist_by_user") : s.ended_by === "admin" ? t("vnc_hist_by_admin") : "";
        return `<div style="padding:7px 0;border-bottom:1px solid var(--border)">
          <div style="display:flex;gap:6px"><b style="flex:1">${new Date(s.start).toLocaleString()}</b><span>${esc(fmtDur(s.duration_s))}</span></div>
          <div style="color:var(--subtext)">👤 ${esc(s.user || "?")}${by ? " · " + esc(by) : ""}${s.rec_id ? " · 🎞️" : ""}</div>${ev}</div>`;
      }).join("");
    } catch (e) {
      histList.innerHTML = `<div style="color:var(--danger)">${esc(e.message || String(e))}</div>`;
    }
  }
  body.querySelector(`#vnc-hist-${win.key}`).addEventListener("click", () => {
    const open = histPanel.style.display !== "flex";
    histPanel.style.display = open ? "flex" : "none";
    if (open) loadHistory();
  });
  body.querySelector(`#vnc-histclose-${win.key}`).addEventListener("click", () => { histPanel.style.display = "none"; });

  const incoming = new Map();             // tid -> {name, size, parts[], got, row}
  function onFileOut(d) {
    if (d.id !== clientId || !d.tid) return;
    let st = incoming.get(d.tid);
    if (!st) {
      if (!chatOpen) { unread++; chatBadge.textContent = unread; chatBadge.style.display = "inline-block"; }
      st = { name: d.name || "file", size: d.size || 0, parts: [], got: 0, row: fileSysRow(`📥 ${d.name} – 0 %`) };
      incoming.set(d.tid, st);
    }
    let bin;
    try { bin = atob(d.data || ""); } catch { bin = ""; }   // skip a corrupt chunk, keep the transfer alive
    const arr = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
    st.parts[d.seq || 0] = arr;
    st.got += arr.length;
    if (st.size) st.row.textContent = `📥 ${st.name} – ${Math.min(100, Math.round((st.got / st.size) * 100))} %`;
    if (d.last) {
      incoming.delete(d.tid);
      const url = URL.createObjectURL(new Blob(st.parts));
      st.row.innerHTML = `📥 ${esc(t("vnc_file_received", { name: st.name }))} – <a href="${url}" download="${esc(st.name)}">${esc(t("vnc_file_save"))}</a>`;
      const a = document.createElement("a");
      a.href = url; a.download = st.name; document.body.appendChild(a); a.click(); a.remove();
      window.notify?.(t("vnc_file_received", { name: st.name }), "success");
    }
  }
  function onSessDisc(d) {
    if (d.id !== clientId) return;
    addChat(null, t("vnc_chat_user_left"), true);
    window.notify?.(t("vnc_chat_user_left"), "warning", 6000);
  }
  dashboardSocket.on("screen-session-chat", onSessChat);
  dashboardSocket.on("screen-session-grant", onSessGrant);
  dashboardSocket.on("screen-session-disconnect", onSessDisc);
  dashboardSocket.on("screen-session-perms", onSessPerms);
  dashboardSocket.on("screen-file-ack", onFileAck);
  dashboardSocket.on("screen-file-out", onFileOut);
  dashboardSocket.on("screen-audio", onAudio);

  // --- Ctrl+C / Ctrl+V automatic: synchronize the real clipboard too ---
  // Ctrl+V: read the local clipboard -> set it on the remote -> then Ctrl+V on the remote.
  // Ctrl+C: Ctrl+C on the remote -> wait briefly -> fetch the remote clipboard here.
  let _autoClipWorking = false;
  async function pasteToRemote() {
    let text = "";
    try { text = await navigator.clipboard.readText(); } catch { /* browser may block */ }
    if (text) sendInput({ type: "clipboard-set", text });
    // Short pause so the agent has set the clipboard, then paste.
    await new Promise((r) => setTimeout(r, text ? 60 : 0));
    sendInput({ type: "combo", keys: ["Control", "v"] });
  }
  function copyFromRemote() {
    sendInput({ type: "combo", keys: ["Control", "c"] });
    // Give the remote PC time to copy the selection, then fetch it.
    setTimeout(() => getClip(), 140);
  }


  let framesReceived = 0;
  let monitorCount = 1;
  let monitorIndex = 1;
  let rdpActive = false;  // true while the experimental RDP streaming is running
  let shellActive = false; // true once switched to shell mode

  // Opens the Guacamole client (RDP/VNC/SSH via externally hosted guacd)
  // for this client in its own window.
  function openGuacWindow() {
    import("../app/windowmanager.js").then(({ openWindow }) => {
      const c = state.clients?.find((x) => x.id === clientId);
      openWindow({
        key: `guac-${clientId}`, appId: "guacamole",
        title: `Guacamole — ${clientName}`,
        props: { clientId, clientName, host: c?.ip || "", platform: c?.platform },
        w: 900, h: 640,
      });
    });
  }

  // Switches the window to an embedded shell (headless/shell-only).
  // Triggered automatically when the agent reports 'screen-mode: shell'.
  function switchToShell(reason) {
    if (shellActive) return;
    shellActive = true;
    // Stop screen streaming and unregister all screen listeners
    try { stopScreen(); } catch {}
    dashboardSocket.off("screen-frame", onFrame);
    dashboardSocket.off("screen-error", onError);
    dashboardSocket.off("screen-mode", onMode);
    dashboardSocket.off("screen-clipboard", onClipboard);
    dashboardSocket.off("screen-session-chat", onSessChat);
    dashboardSocket.off("screen-session-grant", onSessGrant);
    dashboardSocket.off("screen-session-disconnect", onSessDisc);
    dashboardSocket.off("screen-session-perms", onSessPerms);
    dashboardSocket.off("screen-file-ack", onFileAck);
    dashboardSocket.off("screen-file-out", onFileOut);
    dashboardSocket.off("screen-audio", onAudio);
    try { audioCtx?.close(); } catch {}
    try { areaObserver?.disconnect(); } catch (e) {}
    // renderTerminal() below calls registerCleanup() again for this window key,
    // which OVERWRITES the vnc cleanup block - so it would never run. Tear down
    // every window-level listener and the pinch-zoom here, while we still can.
    window.removeEventListener("mouseup", onMouseUp);
    window.removeEventListener("focus", _onClipFocus);
    window.removeEventListener("resize", layoutImage);
    try { zoom.destroy(); } catch {}
    for (const p of pendingAcks.values()) { try { clearTimeout(p.timer); } catch {} }
    pendingAcks.clear();

    // Clear the window, insert the notice banner (incl. Guacamole option) + terminal
    // Same trap as above: height:100% on the window body has no effect.
    // Anchor absolutely so the terminal gets the real window height.
    body.innerHTML = "";
    body.style.display = "block";
    body.style.overflow = "hidden";
    const shellRoot = document.createElement("div");
    shellRoot.style.cssText = "position:absolute;inset:0;display:flex;flex-direction:column";
    body.appendChild(shellRoot);
    const banner = document.createElement("div");
    banner.style.cssText = "display:flex;align-items:center;gap:10px;padding:6px 10px;background:var(--panel-2);font-size:12px;color:var(--subtext);border-bottom:1px solid var(--border);flex-wrap:wrap";
    const label = document.createElement("span");
    label.style.flex = "1";
    label.textContent = "🖥️ → ⌨️ " + (reason || t("u_kein_grafischer_bildschirm_shell_g"));
    const guacBtn = document.createElement("button");
    guacBtn.className = "btn";
    guacBtn.textContent = t("u_uber_guacamole_rdp_vnc_ssh");
    guacBtn.addEventListener("click", openGuacWindow);
    banner.appendChild(label);
    banner.appendChild(guacBtn);
    shellRoot.appendChild(banner);
    const termHost = document.createElement("div");
    termHost.style.cssText = "flex:1;min-height:0";
    shellRoot.appendChild(termHost);
    // Render the existing terminal program in the same window (own session,
    // persistent working directory etc. - runs over the same agent channel).
    renderTerminal(termHost, win);
  }

  // --- Process incoming frames ---
  function onFrame(data) {
    if (data.id !== clientId) return; // only frames for this client
    // If the RDP/error overlay is still open and an image now arrives: remove it
    const overlay = body.querySelector(`#rdp-offer-${win.key}`);
    if (overlay) overlay.remove();
    // Ignore malformed frames (no image payload) instead of setting a broken
    // data URI - keeps the last good frame on screen during a hiccup.
    if (!data.image) return;
    img.src = "data:image/jpeg;base64," + data.image;
    const sizeChanged = (remoteWidth !== data.width || remoteHeight !== data.height);
    remoteWidth = data.width;
    remoteHeight = data.height;
    // On the first image and on every resolution change (monitor switch,
    // resolution change on the client) re-fit.
    if (sizeChanged || !img.style.width) layoutImage();
    // Multi-monitor: show/update the button when more than one screen is present.
    if (typeof data.monitor_count === "number") {
      monitorCount = data.monitor_count;
      monitorIndex = (typeof data.monitor_index === "number") ? data.monitor_index : 1;
      if (monitorSel) {
        monitorSel.style.display = monitorCount > 1 ? "" : "none";
        // Rebuild the options only on change (not on every frame).
        // Index 0 = the combined area of ALL screens side by side.
        if (monitorSel.options.length !== monitorCount + 1) {
          monitorSel.innerHTML = `<option value="0">🖥️ ${t("vnc_all_monitors")}</option>` +
            Array.from({ length: monitorCount }, (_, i) =>
              `<option value="${i + 1}">🖥️ ${t("vnc_monitor_n")} ${i + 1}/${monitorCount}</option>`).join("");
        }
        if (parseInt(monitorSel.value, 10) !== monitorIndex) monitorSel.value = String(monitorIndex);
      }
    }
    framesReceived++;
    const mode = rdpActive ? "RDP" : "Live";
    statusEl.textContent = `${t("vnc_connected")} (${mode}) · ${remoteWidth}×${remoteHeight} · ${framesReceived} ${t("vnc_frames")}`;
    statusEl.style.color = "var(--accent)";
  }

  // Since the language rework the agent no longer sends just finished text,
  // but additionally `code` (translation key) and `params`. Translation
  // happens here because only the browser knows the user's language - several
  // people with different language settings can view the same device
  // at once. If `code` is missing (older agent), the delivered text stays.
  function agentText(data, fallbackKey) {
    if (data && data.code) return t(data.code, data.params || {});
    if (data && (data.error || data.reason)) return data.error || data.reason;
    return fallbackKey ? t(fallbackKey) : "";
  }

  function onError(data) {
    if (data.id !== clientId) return;
    // Raw error text only to the console - the UI stays friendly.
    console.warn("[screen] error from agent:", data.error);
    if (data.consent_denied) {
      // The user at the device did not consent - this is NOT a technical
      // error, so show it without offering alternatives.
      statusEl.textContent = t("u_am_gerat_abgelehnt_keine_bestatigu");
      statusEl.style.color = "var(--danger)";
      return;
    }
    statusEl.textContent = t("u_kein_bildschirm_verfugbar");
    statusEl.style.color = "var(--danger)";
    showRdpOffer(agentText(data));
  }

  // Agent reports the possible access mode. mode='shell' -> open the shell directly.
  function onMode(data) {
    if (data.id !== clientId) return;
    if (data.mode === "shell") switchToShell(agentText(data));
    if (data.mode === "consent") {
      // Consent is currently being requested at the device.
      statusEl.textContent = t("u_warte_auf_bestatigung_am_gerat");
      statusEl.style.color = "var(--subtext)";
    }
  }

  // On a screen error (typically: headless VM) shows a friendly
  // choice of alternatives. Deliberately WITHOUT the raw error message (it is
  // shown small in the status bar and in agent.log) and WITHOUT RDP file download /
  // experimental browser RDP - both were removed.
  function showRdpOffer(errorText) {
    // Show only once
    if (body.querySelector(`#rdp-offer-${win.key}`)) return;
    const imgArea = img.parentElement;
    const overlay = document.createElement("div");
    overlay.id = `rdp-offer-${win.key}`;
    overlay.style.cssText = `
      position:absolute; inset:0; display:flex; flex-direction:column;
      align-items:center; justify-content:center; gap:10px; padding:24px;
      background:radial-gradient(ellipse at center, rgba(16,28,44,0.97), rgba(8,14,24,0.99));
      text-align:center; z-index:5;`;
    overlay.innerHTML = `
      <div style="width:72px;height:72px;border-radius:50%;display:flex;align-items:center;justify-content:center;
                  background:var(--panel-2);border:1px solid var(--border);font-size:32px;margin-bottom:4px">🖥️</div>
      <div style="color:var(--text);font-size:16px;font-weight:600">${t("vnc_no_screen_title")}</div>
      <div style="color:var(--subtext);font-size:13px;max-width:400px;line-height:1.55">
        ${t("vnc_no_screen_text")}
      </div>
      <div style="display:flex;flex-direction:column;gap:8px;margin-top:10px;width:100%;max-width:340px">
        <button class="btn-primary" id="rdp-guac-${win.key}" style="width:100%;display:flex;align-items:center;justify-content:center">
          🕹️ ${t("vnc_open_remote_session")}
        </button>
        <button class="btn" id="rdp-shell-${win.key}" style="width:100%;display:flex;align-items:center;justify-content:center">
          ⌨️ ${t("vnc_open_shell")}
        </button>
        <button class="btn" id="rdp-retry-${win.key}" style="width:100%;display:flex;align-items:center;justify-content:center">
          🔄 ${t("vnc_retry_screen")}
        </button>
      </div>
    `;
    imgArea.appendChild(overlay);

    // Open the shell in the same window (always works, even headless)
    overlay.querySelector(`#rdp-shell-${win.key}`).addEventListener("click", () => {
      switchToShell(t("u_shell_geoffnet"));
    });

    // Connect via Guacamole (RDP/VNC/SSH over guacd) - its own window
    overlay.querySelector(`#rdp-guac-${win.key}`).addEventListener("click", openGuacWindow);

    // Try our own streaming again
    overlay.querySelector(`#rdp-retry-${win.key}`).addEventListener("click", () => {
      overlay.remove();
      statusEl.textContent = t("vnc_connecting");
      statusEl.style.color = "var(--subtext)";
      startScreen(chosenSession);
    });
  }

  // Local signal "remote session started": the profile (if open) turns
  // the one-time silent-mode toggle off IMMEDIATELY with it - regardless
  // of whether the backend event arrives.
  function _announceSessionStart(cid) {
    try {
      window.dispatchEvent(new CustomEvent("screen-session-started", { detail: { clientId: cid } }));
    } catch {}
  }

  dashboardSocket.on("screen-frame", onFrame);
  dashboardSocket.on("screen-error", onError);
  dashboardSocket.on("screen-mode", onMode);

  // --- Multi-user (Windows): "choose a user first" ---
  // If several users are logged in, it first asks WHOSE screen
  // should be viewed/controlled. That user then gets the
  // consent prompt at the device. With 0/1 user it starts directly (as before).
  let chosenSession = null;

  function startScreen(session) {
    chosenSession = session ?? null;
    const payload = { clientId, username: state.user?.username || "unknown" };
    if (chosenSession !== null && chosenSession !== undefined) payload.session = chosenSession;
    dashboardSocket.emit("screen-start", payload);
    _announceSessionStart(clientId);
    // bytera://agent/<id>/remotescreen/screen=2&fps=10 -> screen/FPS/quality
    // apply once the stream is running.
    const wantScreen = parseInt(win.props?.screen, 10);
    if (!isNaN(wantScreen)) setTimeout(() => { try { setMonitor(wantScreen); } catch { /* egal */ } }, 1500);
    const cfg = {};
    const wantFps = parseInt(win.props?.fps, 10);
    const wantQ = parseInt(win.props?.quality, 10);
    if (!isNaN(wantFps)) cfg.fps = wantFps;
    if (!isNaN(wantQ)) cfg.quality = wantQ;
    // resolution=WIDTHxHEIGHT (e.g. 1024x512) -> target width to the agent.
    const res = String(win.props?.resolution || "").match(/^(\d{3,4})\s*[x\u00d7]/);
    if (res) cfg.width = parseInt(res[1], 10);
    if (Object.keys(cfg).length) {
      setTimeout(() => { try { dashboardSocket.emit("screen-config", { clientId, username: _me(), ...cfg }); } catch { /* egal */ } }, 1500);
    }
  }

  function pickUserThenStart() {
    let settled = false;
    const onUsers = (data) => {
      if (settled || data?.id !== clientId) return;
      settled = true;
      dashboardSocket.off("screen-users", onUsers);
      clearTimeout(timer);
      const users = Array.isArray(data?.users) ? data.users : [];
      if (!data?.multi || users.length <= 1) {
        startScreen(users.length === 1 ? users[0].session : null);
        return;
      }
      showUserPicker(users);
    };
    dashboardSocket.on("screen-users", onUsers);
    // Fallback: if the agent does not answer (older/not Windows), start normally.
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      dashboardSocket.off("screen-users", onUsers);
      startScreen(null);
    }, 3500);
    dashboardSocket.emit("screen-users", { clientId, username: state.user?.username || "unknown" });
  }

  function showUserPicker(users) {
    statusEl.textContent = t("vnc_pick_user");
    const ov = document.createElement("div");
    ov.style.cssText = "position:absolute;inset:0;z-index:30;display:flex;align-items:center;"
      + "justify-content:center;background:rgba(0,0,0,.72);padding:16px";
    ov.innerHTML = `<div style="background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:16px 18px;width:420px;max-width:100%;color:var(--text)">
      <b style="font-size:15px">${esc(t("vnc_pick_user_title"))}</b>
      <div style="color:var(--subtext);font-size:12.5px;margin:4px 0 10px">${esc(t("vnc_pick_user_hint"))}</div>
      <div data-list style="display:flex;flex-direction:column;gap:6px;max-height:50vh;overflow:auto"></div>
    </div>`;
    const list = ov.querySelector("[data-list]");
    users.forEach((u) => {
      const b = document.createElement("button");
      b.className = "btn";
      b.style.cssText = "width:100%;text-align:left;display:flex;align-items:center;gap:8px;padding:8px 10px";
      const dot = u.active ? "🟢" : "⚪";
      const tags = [u.console ? t("vnc_user_console") : "", u.active ? t("vnc_user_active") : t("vnc_user_disconnected")]
        .filter(Boolean).join(" · ");
      b.innerHTML = `<span>${dot}</span><span style="flex:1"><b>${esc(u.user)}</b>`
        + `<div style="font-size:11px;color:var(--subtext)">${esc(tags)} \u00b7 Session ${u.session}</div></span>`;
      b.addEventListener("click", () => {
        ov.remove();
        statusEl.textContent = t("vnc_connecting");
        startScreen(u.session);
      });
      list.appendChild(b);
    });
    imgArea.appendChild(ov);
  }

  pickUserThenStart();

  // --- Convert coordinates from the shown image to the real resolution ---
  function mapCoords(clientX, clientY) {
    // Since layoutImage() the <img> element is EXACTLY the displayed
    // image area - no bars from object-fit, no offsets. Therefore
    // a division is enough. Previously the offset was subtracted a second
    // time even though the element was already offset: clicks landed
    // further off the more the window deviated from the image shape.
    const rect = img.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return { x: 0, y: 0 };
    const x = Math.round(((clientX - rect.left) / rect.width) * remoteWidth);
    const y = Math.round(((clientY - rect.top) / rect.height) * remoteHeight);
    // Clamp to the screen - a click 2 px past the edge should hit the
    // edge and not land on a neighbouring monitor.
    return {
      x: Math.max(0, Math.min(x, remoteWidth - 1)),
      y: Math.max(0, Math.min(y, remoteHeight - 1)),
    };
  }

  function controlEnabled() {
    return controlToggle.checked;
  }

  // --- Mouse with real drag & drop ---
  // We remember whether a button is currently pressed so "move" is also
  // sent correctly while dragging (the OS on the client then drags).
  let mouseDown = false;

  img.addEventListener("mousemove", (e) => {
    if (!controlEnabled()) return;
    const { x, y } = mapCoords(e.clientX, e.clientY);
    // Send "move" while dragging too - the client is holding the button down
    sendInput({ type: "move", x, y });
  });

  // ---- Touch: zoom/pan and hold = right-click -------------------
  // On mobile there is no right mouse button, and the remote end is barely
  // usable without zoom. mapCoords() still computes correctly because the zoom
  // only changes the display - the image's getBoundingClientRect()
  // grows with it.
  // NOTE: do NOT call registerCleanup() here. registerCleanup stores exactly
  // one function per window key (it overwrites), and the main cleanup block at
  // the end of this function registers the real teardown. Registering here as
  // well would be overwritten there and zoom.destroy() would never run (leaking
  // the pinch-zoom listeners on every close). zoom.destroy() is therefore part
  // of the single cleanup below.
  const zoom = attachPinchZoom(img.parentElement, img, { min: 1, max: 6 });

  // ---- Touch operation -----------------------------------------------
  // One finger belongs to the remote end so that windows can be dragged and
  // icons moved there. It is distinguished by movement and time:
  //
  //   short tap             -> left click
  //   move                  -> drag with the left button held
  //   hold without moving   -> right click
  //
  // The decision is made only AFTER touchdown: if on touchdown a
  // "pressed" were sent immediately, holding would also produce a left click
  // and a right click would turn into an accidental drag.
  const TOUCH_HOLD_MS = 500;     // from here it counts as a hold
  const TOUCH_MOVE_PX = 10;      // from here it counts as a drag
  let touchStart = null;
  let touchDragging = false;
  let touchHoldTimer = null;

  const touchSend = (type, cx, cy, button = "left") => {
    const { x, y } = mapCoords(cx, cy);
    sendInput({ type, x, y, button });
  };

  img.addEventListener("pointerdown", (e) => {
    if (e.pointerType === "mouse" || !controlEnabled()) return;
    img.focus({ preventScroll: true });
    touchStart = { cx: e.clientX, cy: e.clientY };
    touchDragging = false;
    clearTimeout(touchHoldTimer);
    touchHoldTimer = setTimeout(() => {
      // Held without moving -> right click.
      if (!touchStart || touchDragging) return;
      try { navigator.vibrate?.(18); } catch {}
      const { cx, cy } = touchStart;
      touchSend("down", cx, cy, "right");
      setTimeout(() => touchSend("up", cx, cy, "right"), 60);
      touchStart = null;
    }, TOUCH_HOLD_MS);
  }, { passive: true });

  img.addEventListener("pointermove", (e) => {
    if (e.pointerType === "mouse" || !touchStart || !controlEnabled()) return;
    const moved = Math.abs(e.clientX - touchStart.cx) + Math.abs(e.clientY - touchStart.cy);
    if (!touchDragging && moved > TOUCH_MOVE_PX) {
      // Only now press the button - and at the TOUCHDOWN point. Only then
      // does the remote end grab the window title where the finger started.
      clearTimeout(touchHoldTimer);
      touchDragging = true;
      touchSend("down", touchStart.cx, touchStart.cy, "left");
    }
    if (touchDragging) {
      touchSend("move", e.clientX, e.clientY);
      e.preventDefault();
    }
  }, { passive: false });

  img.addEventListener("pointerup", (e) => {
    if (e.pointerType === "mouse" || !touchStart) return;
    clearTimeout(touchHoldTimer);
    if (touchDragging) {
      touchSend("up", e.clientX, e.clientY, "left");
    } else if (controlEnabled()) {
      // Short tap -> full left click at the same spot.
      const { cx, cy } = touchStart;
      touchSend("down", cx, cy, "left");
      setTimeout(() => touchSend("up", cx, cy, "left"), 40);
    }
    touchStart = null;
    touchDragging = false;
  });

  img.addEventListener("pointercancel", () => {
    clearTimeout(touchHoldTimer);
    if (touchDragging && touchStart) touchSend("up", touchStart.cx, touchStart.cy, "left");
    touchStart = null;
    touchDragging = false;
  }, { passive: true });

  // The browser's selection/magnifier menu gets in the way while dragging.
  img.addEventListener("contextmenu", (e) => {
    if (e.pointerType !== "mouse") e.preventDefault();
  });

  img.addEventListener("mousedown", (e) => {
    if (!controlEnabled()) return;
    e.preventDefault();
    img.focus({ preventScroll: true }); // so keyboard input arrives
    mouseDown = true;
    const { x, y } = mapCoords(e.clientX, e.clientY);
    const button = e.button === 2 ? "right" : "left";
    // PRESS the button (not an immediate click) -> enables hold + drag
    sendInput({ type: "down", x, y, button });
  });

  // Catch mouseup on the whole window (in case you release outside the image)
  function onMouseUp(e) {
    if (!mouseDown || !controlEnabled()) return;
    mouseDown = false;
    const { x, y } = mapCoords(e.clientX, e.clientY);
    const button = e.button === 2 ? "right" : "left";
    sendInput({ type: "up", x, y, button });
  }
  img.addEventListener("mouseup", onMouseUp);
  window.addEventListener("mouseup", onMouseUp);

  img.addEventListener("dblclick", (e) => {
    if (!controlEnabled()) return;
    e.preventDefault();
    const { x, y } = mapCoords(e.clientX, e.clientY);
    sendInput({ type: "double", x, y });
  });

  // Suppress the browser's right-click menu on the image (we forward right-click)
  img.addEventListener("contextmenu", (e) => e.preventDefault());

  img.addEventListener("wheel", (e) => {
    if (!controlEnabled()) return;
    e.preventDefault();
    sendInput({ type: "scroll", dy: e.deltaY > 0 ? -1 : 1 });
  });

  // --- Keyboard ---
  // We listen for keydown while the image is focused.
  img.addEventListener("keydown", (e) => {
    if (!controlEnabled()) return;
    e.preventDefault();

    // Ctrl+C / Ctrl+V: carry the real clipboard automatically (without the
    // buttons in the bar). Only on plain Ctrl (not with Alt/Meta/Shift).
    if (e.ctrlKey && !e.altKey && !e.metaKey && !e.shiftKey && (e.key === "v" || e.key === "V")) {
      if (!_autoClipWorking) { _autoClipWorking = true; pasteToRemote().finally(() => { _autoClipWorking = false; }); }
      return;
    }
    if (e.ctrlKey && !e.altKey && !e.metaKey && !e.shiftKey && (e.key === "c" || e.key === "C")) {
      copyFromRemote();
      return;
    }
    if (e.ctrlKey && !e.altKey && !e.metaKey && !e.shiftKey && (e.key === "x" || e.key === "X")) {
      // Cut = copy + the agent runs Ctrl+X anyway; fetch afterwards.
      sendInput({ type: "combo", keys: ["Control", "x"] });
      setTimeout(() => getClip(), 140);
      return;
    }

    // Key combination with modifiers (Ctrl/Alt) -> send as "combo"
    if (e.ctrlKey || e.altKey || e.metaKey) {
      const keys = [];
      if (e.ctrlKey) keys.push("Control");
      if (e.altKey) keys.push("Alt");
      if (e.metaKey) keys.push("Meta");
      if (e.shiftKey) keys.push("Shift");
      // The actual key (not the modifiers themselves)
      if (!["Control", "Alt", "Meta", "Shift"].includes(e.key)) {
        keys.push(e.key.length === 1 ? e.key.toLowerCase() : e.key);
      }
      if (keys.length) sendInput({ type: "combo", keys });
      return;
    }

    // Apply sticky modifiers (toolbar toggles) to the key.
    if (activeMods().length) {
      sendKeyWithMods(e.key.length === 1 ? e.key.toLowerCase() : e.key);
      return;
    }

    // Normal printable character -> type as text
    if (e.key.length === 1) {
      sendInput({ type: "text", text: e.key });
    } else {
      // Special key (Enter, Backspace, arrows, ...)
      sendInput({ type: "key", key: e.key });
    }
  });

  // Ctrl+Alt+Del button (cannot be intercepted from the browser as a real key)
  ctrlAltDelBtn.addEventListener("click", () => {
    sendInput({ type: "combo", keys: ["Control", "Alt", "Delete"] });
  });

  // --- Send text (type the whole text at once at the client's cursor) ---
  function sendText() {
    const text = textInput.value;
    if (!text) return;
    const layout = layoutSel.value;
    // Layout compensation (reversed, by user request): with "us" selected
    // the characters are replaced via the position table before sending
    // (reverse effect); "de"/"raw" = send 1:1.
    const mapped = mapKeyboardText(text, layout);
    sendInput({ type: "text", text: mapped, layout });
    textInput.value = "";
    textInput.focus();
  }
  sendBtn.addEventListener("click", sendText);
  textInput.addEventListener("keydown", (e) => {
    // Enter sends; allow Ctrl+V (paste) in the field normally
    if (e.key === "Enter") { e.preventDefault(); sendText(); }
  });

  // --- Cleanup when the window is closed ---
  registerCleanup(win.key, () => {
    dashboardSocket.off("screen-frame", onFrame);
    dashboardSocket.off("screen-error", onError);
    dashboardSocket.off("screen-mode", onMode);
    dashboardSocket.off("screen-clipboard", onClipboard);
    dashboardSocket.off("screen-session-chat", onSessChat);
    dashboardSocket.off("screen-session-grant", onSessGrant);
    dashboardSocket.off("screen-session-disconnect", onSessDisc);
    dashboardSocket.off("screen-session-perms", onSessPerms);
    dashboardSocket.off("screen-file-ack", onFileAck);
    dashboardSocket.off("screen-file-out", onFileOut);
    dashboardSocket.off("screen-audio", onAudio);
    try { audioCtx?.close(); } catch {}
    try { areaObserver?.disconnect(); } catch (e) {}
    try { zoom.destroy(); } catch {}
    // Pending file-transfer acks hold a 60 s timeout each - clear them so a
    // window closed mid-transfer leaves nothing running.
    for (const p of pendingAcks.values()) { try { clearTimeout(p.timer); } catch {} }
    pendingAcks.clear();
    stopScreen();
    if (rdpActive) dashboardSocket.emit("rdp-stop", { clientId });
    window.removeEventListener("mouseup", onMouseUp);
    window.removeEventListener("focus", _onClipFocus);
    window.removeEventListener("resize", layoutImage);   // only added when ResizeObserver is unavailable; harmless otherwise
  });
}
