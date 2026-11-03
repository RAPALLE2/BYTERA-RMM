// apps/vnc.js
// -----------
// Vollwertiger Remote Screen: empfängt Live-Bildschirm-Frames vom Agenten
// (als JPEG-Base64 über Socket.IO) und zeigt sie an. Maus- und Tastatur-
// Eingaben werden zurück an den Agenten geschickt, der sie lokal ausführt.
//
// Ablauf:
//   1. Fenster öffnet -> "screen-start" ans Backend -> Agent beginnt zu streamen
//   2. Eingehende "screen-frame" Events -> Bild ins <img> setzen
//   3. Maus/Tastatur im Bild -> Koordinaten umrechnen -> "screen-input" senden
//   4. Fenster schließt -> "screen-stop" senden (siehe cleanup in app.js)

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
  // Alle Steuer-Events an das Geraet tragen den Benutzernamen - das Backend
  // prueft damit das Recht use_screen (frueher war screen-input ungeprueft).
  const _me = () => state.user?.username || "";
  const sendInput = (d) => dashboardSocket.emit("screen-input", { clientId, username: _me(), ...d });
  const getClip = () => dashboardSocket.emit("screen-clipboard-get", { clientId, username: _me() });
  const setMonitor = (monitor) => dashboardSocket.emit("screen-set-monitor", { clientId, username: _me(), monitor });
  const stopScreen = () => dashboardSocket.emit("screen-stop", { clientId, username: _me() });

  // WICHTIG zum Layout: Der Fenster-Body (.rmm-window-body) ist ein Flex-Item
  // mit "flex:1" und "overflow:auto", hat also KEINE feste Hoehe. Ein
  // "height:100%" darauf laeuft ins Leere (die Hoehe haengt am Inhalt, der
  // Inhalt an der Hoehe). Genau daran lag das nicht zentrierte Bild: Der
  // Bildbereich wuchs auf die volle Bildhoehe, "max-height:100%" griff nie,
  // das Bild wurde auf die volle Fensterbreite gestreckt und unten
  // abgeschnitten - man sah nur noch den mittleren Ausschnitt (die untere
  // Leiste "Text senden" war deshalb ebenfalls aus dem Fenster geschoben).
  // Loesung: absolut im Body verankern (der ist position:relative) - damit
  // hat der Rahmen IMMER die echte Fenstergroesse als Bezug.
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
        <button class="btn" id="vnc-mod-ctrl-${win.key}" title="${t("u_strg_gedruckt_halten_toggle_wirkt_")}">Strg</button>
        <button class="btn" id="vnc-mod-alt-${win.key}" title="${t("u_alt_gedruckt_halten_toggle_wirkt_a")}">Alt</button>
        <button class="btn" id="vnc-mod-win-${win.key}" title="${t("u_windows_taste_gedruckt_halten_togg")}">Win</button>
        <button class="btn" id="vnc-key-tab-${win.key}" title="Tab senden">Tab</button>
        <button class="btn" id="vnc-key-esc-${win.key}" title="Esc senden">Esc</button>
        <button class="btn" id="vnc-ctrlaltdel-${win.key}">Strg+Alt+Entf</button>
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

  // Echte Bildschirmauflösung des Clients (kommt mit jedem Frame mit),
  // um Klick-Koordinaten korrekt umzurechnen. Muss VOR layoutImage() stehen.
  let remoteWidth = 1920;
  let remoteHeight = 1080;
  const img = body.querySelector(`#vnc-img-${win.key}`);
  const imgArea = body.querySelector(`#vnc-area-${win.key}`);

  // ---- Bild einpassen: Groesse UND Position werden hier gerechnet ----------
  // Warum nicht per CSS (max-width/max-height + object-fit:contain + flex-
  // Zentrierung)? Weil dann DREI Dinge zusammenpassen muessen: die
  // Elementgroesse, der von object-fit gewaehlte Ausschnitt und die
  // Ruecktransformation der Mauskoordinaten. Sobald eines davon abweicht
  // (Vollbild, Fenster-Resize, ein Frame mit anderer Groesse), sitzt das Bild
  // schief UND die Klicks landen daneben - genau das Fehlerbild.
  // Jetzt gilt: Das <img>-Element ist IMMER exakt so gross wie das
  // dargestellte Bild und wird selbst mittig gesetzt. Damit ist
  // getBoundingClientRect() gleichbedeutend mit der Bildflaeche und die
  // Umrechnung der Klicks ist eine einfache Division.
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

  // Neu einpassen, wenn sich die Flaeche aendert: Fenster ziehen, maximieren,
  // Vollbild, Seitenleiste ein-/ausklappen. ResizeObserver deckt all das ab,
  // ohne dass jede Stelle im Fenstermanager davon wissen muss.
  let areaObserver = null;
  try {
    areaObserver = new ResizeObserver(() => layoutImage());
    areaObserver.observe(imgArea);
  } catch (e) {
    window.addEventListener("resize", layoutImage);
  }

  // ---- Browser-Vollbild (Desktop + Handy): Button toggelt, Esc beendet ----
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

  // Bildschirm wechseln (Multi-Monitor): direkte Auswahl per Dropdown.
  monitorSel.addEventListener("change", () => {
    const next = parseInt(monitorSel.value, 10);   // 0 = Alle Bildschirme
    if (!isNaN(next) && next !== monitorIndex) {
      setMonitor(next);
    }
  });

  // --- Sticky-Modifier (wie in Proxmox): Strg/Alt/Win als Toggle. Aktive
  //     Modifier werden auf alle folgenden Tastatureingaben angewendet. ---
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
    // Doppelklick: Taste SOFORT einmal drücken+loslassen (z.B. Win öffnet das
    // Startmenü). Die zwei Einzelklicks davor haben den Toggle an+aus
    // geschaltet - hier sicherheitshalber auf "aus" setzen.
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
  // Schickt eine Taste unter Berücksichtigung der aktiven Toggles. Nach einer
  // Kombination werden die Toggles automatisch gelöst (wie in Proxmox).
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

  // --- Zwischenablage synchronisieren ---
  // Senden: lokale Zwischenablage lesen -> Agent setzt sie am Remote-PC.
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
  // Holen: Agent liest die Remote-Zwischenablage -> hier in die lokale schreiben.
  body.querySelector(`#vnc-clip-get-${win.key}`).addEventListener("click", () => {
    getClip();
  });
  async function onClipboard(data) {
    if (data.id !== clientId) return;
    if (data.error) { window.notify?.(t("vnc_clip_remote") + ": " + (data.code ? t(data.code) : data.error), "error"); return; }
    const text = data.text || "";
    try {
      await navigator.clipboard.writeText(text);
      window.notify?.(t("u_remote_zwischenablage_ubernommen") + text.length + " Zeichen)", "success");
    } catch {
      // Clipboard-API blockiert (z.B. HTTP): Text zum manuellen Kopieren zeigen.
      uiPrompt(t("vnc_clip_remote"), {
        description: t("guac_copy_manual"),
        value: text, okText: t("close") });
    }
  }
  dashboardSocket.on("screen-clipboard", onClipboard);

  // --- Chat-/Steuerfenster der Fernwartung (Benutzer <-> Admin) ---
  // Die Person vor dem PC bekommt ein Fenster (nur wenn NICHT Silent-Modus);
  // hier ist die Admin-Seite: Chat, Anzeige der erteilten Admin-Rechte und ein
  // Hinweis, wenn der Benutzer die Verbindung trennt.
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

  // Berechtigungen, die die Person vor dem PC im Sitzungsfenster schaltet (wie AnyDesk):
  // Steuerung (Maus/Tastatur), Zwischenablage, Administrator-Rechte.
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
    // Fenster schliessen -> Aufraeumen sendet screen-stop -> Agent beendet die Sitzung
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

  // --- Datei-Uebertragung (wie AnyDesk) ---
  // Admin -> Benutzer: Datei waehlen oder auf das Bild ziehen. Gesendet wird in
  // Stuecken zu 256 KB; jedes Stueck wird vom Geraet bestaetigt, erst dann geht
  // das naechste raus. Die Datei landet im Ordner "Downloads" des Benutzers.
  // Benutzer -> Admin: "📎" im Sitzungsfenster -> Datei kommt hier stueckweise an
  // und wird als Download angeboten.
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

  // --- Ton (wie AnyDesk "Ton uebertragen") ---
  // Das Geraet schickt 100-ms-Stuecke (16 kHz mono, 8-Bit-mu-law). Hier werden
  // sie dekodiert und lueckenlos hintereinander abgespielt (Web Audio). Laeuft
  // die Wiedergabe mehr als 0,6 s hinterher, wird neu angesetzt (keine Verzoegerung
  // aufbauen). Standard: AUS - der Admin schaltet ihn bewusst ein, der Benutzer
  // sieht es im Sitzungsfenster.
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
    setAudio(on);                         // im Klick erzeugen: Browser erlauben Ton nur nach Benutzeraktion
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
    const bin = atob(d.data);
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

  // --- Sitzungsverlauf ---
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
      st = { name: d.name || "datei", size: d.size || 0, parts: [], got: 0, row: fileSysRow(`📥 ${d.name} – 0 %`) };
      incoming.set(d.tid, st);
    }
    const bin = atob(d.data || "");
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

  // --- Strg+C / Strg+V automatisch: echte Zwischenablage mitsynchronisieren ---
  // Strg+V: lokale Zwischenablage lesen -> Remote setzen -> dann Strg+V am Remote.
  // Strg+C: Strg+C am Remote -> kurz warten -> Remote-Zwischenablage hierher holen.
  let _autoClipWorking = false;
  async function pasteToRemote() {
    let text = "";
    try { text = await navigator.clipboard.readText(); } catch { /* Browser blockt evtl. */ }
    if (text) sendInput({ type: "clipboard-set", text });
    // Kurze Pause, damit der Agent die Zwischenablage gesetzt hat, dann einfügen.
    await new Promise((r) => setTimeout(r, text ? 60 : 0));
    sendInput({ type: "combo", keys: ["Control", "v"] });
  }
  function copyFromRemote() {
    sendInput({ type: "combo", keys: ["Control", "c"] });
    // Dem Remote-PC Zeit geben, die Auswahl zu kopieren, dann abholen.
    setTimeout(() => getClip(), 140);
  }


  let framesReceived = 0;
  let monitorCount = 1;
  let monitorIndex = 1;
  let rdpActive = false;  // true, wenn gerade das experimentelle RDP-Streaming läuft
  let shellActive = false; // true, sobald auf Shell-Modus umgeschaltet wurde

  // Öffnet den Guacamole-Client (RDP/VNC/SSH über extern gehostetes guacd)
  // für diesen Client in einem eigenen Fenster.
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

  // Schaltet das Fenster auf eine eingebettete Shell um (headless/Shell-only).
  // Wird automatisch ausgelöst, wenn der Agent 'screen-mode: shell' meldet.
  function switchToShell(reason) {
    if (shellActive) return;
    shellActive = true;
    // Screen-Streaming beenden und alle Screen-Listener abmelden
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
    window.removeEventListener("mouseup", onMouseUp);

    // Fenster leeren, Hinweis-Banner (inkl. Guacamole-Option) + Terminal einsetzen
    // Gleiche Falle wie oben: height:100% auf dem Fenster-Body wirkt nicht.
    // Absolut verankern, damit das Terminal die echte Fensterhoehe bekommt.
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
    // Bestehendes Terminal-Programm im selben Fenster rendern (eigene Session,
    // persistentes Arbeitsverzeichnis usw. - läuft über denselben Agent-Kanal).
    renderTerminal(termHost, win);
  }

  // --- Eingehende Frames verarbeiten ---
  function onFrame(data) {
    if (data.id !== clientId) return; // nur Frames für diesen Client
    // Falls das RDP-/Fehler-Overlay noch offen ist und jetzt ein Bild kommt: weg damit
    const overlay = body.querySelector(`#rdp-offer-${win.key}`);
    if (overlay) overlay.remove();
    img.src = "data:image/jpeg;base64," + data.image;
    const sizeChanged = (remoteWidth !== data.width || remoteHeight !== data.height);
    remoteWidth = data.width;
    remoteHeight = data.height;
    // Beim ersten Bild und bei jedem Auflösungswechsel (Monitorwechsel,
    // Aufloesungsaenderung am Client) neu einpassen.
    if (sizeChanged || !img.style.width) layoutImage();
    // Multi-Monitor: Button anzeigen/aktualisieren, wenn mehr als ein Bildschirm da ist.
    if (typeof data.monitor_count === "number") {
      monitorCount = data.monitor_count;
      monitorIndex = (typeof data.monitor_index === "number") ? data.monitor_index : 1;
      if (monitorSel) {
        monitorSel.style.display = monitorCount > 1 ? "" : "none";
        // Optionen nur bei Änderung neu aufbauen (nicht bei jedem Frame).
        // Index 0 = Gesamtfläche ALLER Bildschirme nebeneinander.
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

  // Der Agent schickt seit dem Sprachumbau nicht mehr nur fertigen Text,
  // sondern zusätzlich `code` (Übersetzungsschlüssel) und `params`. Übersetzt
  // wird hier, weil nur der Browser die Sprache des Benutzers kennt - mehrere
  // Personen mit verschiedenen Spracheinstellungen können dasselbe Gerät
  // ansehen. Fehlt `code` (älterer Agent), bleibt der mitgelieferte Text.
  function agentText(data, fallbackKey) {
    if (data && data.code) return t(data.code, data.params || {});
    if (data && (data.error || data.reason)) return data.error || data.reason;
    return fallbackKey ? t(fallbackKey) : "";
  }

  function onError(data) {
    if (data.id !== clientId) return;
    // Rohen Fehlertext nur in die Konsole - die Oberfläche bleibt freundlich.
    console.warn("[screen] error from agent:", data.error);
    if (data.consent_denied) {
      // Der Benutzer am Gerät hat nicht zugestimmt - das ist KEIN technischer
      // Fehler, deshalb ohne Alternativen-Angebot anzeigen.
      statusEl.textContent = t("u_am_gerat_abgelehnt_keine_bestatigu");
      statusEl.style.color = "var(--danger)";
      return;
    }
    statusEl.textContent = t("u_kein_bildschirm_verfugbar");
    statusEl.style.color = "var(--danger)";
    showRdpOffer(agentText(data));
  }

  // Agent meldet den möglichen Zugriffsmodus. mode='shell' -> direkt Shell öffnen.
  function onMode(data) {
    if (data.id !== clientId) return;
    if (data.mode === "shell") switchToShell(agentText(data));
    if (data.mode === "consent") {
      // Am Gerät wird gerade um Zustimmung gebeten.
      statusEl.textContent = t("u_warte_auf_bestatigung_am_gerat");
      statusEl.style.color = "var(--subtext)";
    }
  }

  // Zeigt bei einem Bildschirm-Fehler (typisch: headless VM) eine freundliche
  // Auswahl an Alternativen. Bewusst OHNE die rohe Fehlermeldung (die steht
  // klein in der Statusleiste und im agent.log) und OHNE RDP-Datei-Download /
  // experimentelles Browser-RDP - beides wurde entfernt.
  function showRdpOffer(errorText) {
    // Nur einmal anzeigen
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

    // Shell im selben Fenster öffnen (funktioniert immer, auch headless)
    overlay.querySelector(`#rdp-shell-${win.key}`).addEventListener("click", () => {
      switchToShell(t("u_shell_geoffnet"));
    });

    // Über Guacamole verbinden (RDP/VNC/SSH über guacd) - eigenes Fenster
    overlay.querySelector(`#rdp-guac-${win.key}`).addEventListener("click", openGuacWindow);

    // Erneut unser eigenes Streaming versuchen
    overlay.querySelector(`#rdp-retry-${win.key}`).addEventListener("click", () => {
      overlay.remove();
      statusEl.textContent = t("vnc_connecting");
      statusEl.style.color = "var(--subtext)";
      startScreen(chosenSession);
    });
  }

  // Lokales Signal "Remote-Session gestartet": Das Profil (falls offen) schaltet
  // den einmaligen Silent-Modus-Toggle damit SOFORT wieder aus - unabhängig
  // davon, ob das Backend-Event ankommt.
  function _announceSessionStart(cid) {
    try {
      window.dispatchEvent(new CustomEvent("screen-session-started", { detail: { clientId: cid } }));
    } catch {}
  }

  dashboardSocket.on("screen-frame", onFrame);
  dashboardSocket.on("screen-error", onError);
  dashboardSocket.on("screen-mode", onMode);

  // --- Mehrbenutzer (Windows): "erst einen Benutzer waehlen" ---
  // Sind mehrere Benutzer angemeldet, wird zuerst gefragt, WESSEN Bildschirm
  // gesehen/gesteuert werden soll. Dieser Benutzer bekommt dann am Geraet die
  // Freigabe-Abfrage. Bei 0/1 Benutzer startet es direkt (wie bisher).
  let chosenSession = null;

  function startScreen(session) {
    chosenSession = session ?? null;
    const payload = { clientId, username: state.user?.username || "unbekannt" };
    if (chosenSession !== null && chosenSession !== undefined) payload.session = chosenSession;
    dashboardSocket.emit("screen-start", payload);
    _announceSessionStart(clientId);
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
    // Fallback: antwortet der Agent nicht (aelter/kein Windows), normal starten.
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      dashboardSocket.off("screen-users", onUsers);
      startScreen(null);
    }, 3500);
    dashboardSocket.emit("screen-users", { clientId, username: state.user?.username || "unbekannt" });
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
        + `<div style="font-size:11px;color:var(--subtext)">${esc(tags)} \u00b7 Sitzung ${u.session}</div></span>`;
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

  // --- Koordinaten vom angezeigten Bild auf die echte Auflösung umrechnen ---
  function mapCoords(clientX, clientY) {
    // Seit layoutImage() ist das <img>-Element GENAU die dargestellte
    // Bildfläche - keine Balken durch object-fit, keine Offsets. Deshalb
    // reicht eine Division. Vorher wurde der Offset ein zweites Mal
    // herausgerechnet, obwohl das Element schon versetzt war: Klicks landeten
    // umso weiter daneben, je stärker das Fenster von der Bildform abwich.
    const rect = img.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return { x: 0, y: 0 };
    const x = Math.round(((clientX - rect.left) / rect.width) * remoteWidth);
    const y = Math.round(((clientY - rect.top) / rect.height) * remoteHeight);
    // Auf den Bildschirm begrenzen - ein Klick 2 px neben dem Rand soll den
    // Rand treffen und nicht auf einem Nachbarmonitor landen.
    return {
      x: Math.max(0, Math.min(x, remoteWidth - 1)),
      y: Math.max(0, Math.min(y, remoteHeight - 1)),
    };
  }

  function controlEnabled() {
    return controlToggle.checked;
  }

  // --- Maus mit echtem Drag&Drop ---
  // Wir merken uns, ob gerade eine Taste gedrückt ist, damit "move" auch
  // während des Ziehens korrekt gesendet wird (Betriebssystem am Client zieht dann).
  let mouseDown = false;

  img.addEventListener("mousemove", (e) => {
    if (!controlEnabled()) return;
    const { x, y } = mapCoords(e.clientX, e.clientY);
    // Beim Ziehen ebenfalls "move" senden - der Client hält ja die Taste gedrückt
    sendInput({ type: "move", x, y });
  });

  // ---- Touch: Zoomen/Schieben und Halten = Rechtsklick -------------------
  // Auf dem Handy gibt es keine rechte Maustaste, und die Gegenstelle ist ohne
  // Zoom kaum bedienbar. mapCoords() rechnet weiterhin richtig, weil der Zoom
  // nur die Darstellung veraendert - die getBoundingClientRect() des Bildes
  // waechst mit.
  const zoom = attachPinchZoom(img.parentElement, img, { min: 1, max: 6 });
  registerCleanup(win.key, () => zoom.destroy());

  // ---- Touch-Bedienung -----------------------------------------------
  // Ein Finger gehört der Gegenstelle, damit sich dort Fenster ziehen und
  // Symbole verschieben lassen. Unterschieden wird nach Bewegung und Zeit:
  //
  //   kurz tippen           -> Linksklick
  //   bewegen               -> ziehen mit gedrückter linker Taste
  //   halten ohne Bewegung  -> Rechtsklick
  //
  // Die Entscheidung fällt erst NACH dem Aufsetzen: Würde beim Aufsetzen
  // sofort "gedrückt" gesendet, käme beim Halten zusätzlich ein Linksklick an
  // und aus einem Rechtsklick würde ein versehentliches Ziehen.
  const TOUCH_HOLD_MS = 500;     // ab hier gilt es als Halten
  const TOUCH_MOVE_PX = 10;      // ab hier gilt es als Ziehen
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
      // Gehalten ohne Bewegung -> Rechtsklick.
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
      // Erst jetzt die Taste drücken - und zwar am AUFSETZPUNKT. Nur so
      // greift die Gegenstelle den Fenstertitel dort, wo der Finger startete.
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
      // Kurzes Tippen -> vollständiger Linksklick an derselben Stelle.
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

  // Das Auswahl-/Lupenmenü des Browsers stört beim Ziehen.
  img.addEventListener("contextmenu", (e) => {
    if (e.pointerType !== "mouse") e.preventDefault();
  });

  img.addEventListener("mousedown", (e) => {
    if (!controlEnabled()) return;
    e.preventDefault();
    img.focus({ preventScroll: true }); // damit Tastatureingaben ankommen
    mouseDown = true;
    const { x, y } = mapCoords(e.clientX, e.clientY);
    const button = e.button === 2 ? "right" : "left";
    // Taste DRÜCKEN (nicht sofort klicken) -> ermöglicht Halten + Ziehen
    sendInput({ type: "down", x, y, button });
  });

  // mouseup auf dem gesamten Fenster abfangen (falls man außerhalb des Bildes loslässt)
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

  // Rechtsklick-Menü des Browsers im Bild unterdrücken (wir leiten Rechtsklick weiter)
  img.addEventListener("contextmenu", (e) => e.preventDefault());

  img.addEventListener("wheel", (e) => {
    if (!controlEnabled()) return;
    e.preventDefault();
    sendInput({ type: "scroll", dy: e.deltaY > 0 ? -1 : 1 });
  });

  // --- Tastatur ---
  // Wir hören auf keydown, während das Bild fokussiert ist.
  img.addEventListener("keydown", (e) => {
    if (!controlEnabled()) return;
    e.preventDefault();

    // Strg+C / Strg+V: echte Zwischenablage automatisch mitnehmen (ohne die
    // Knöpfe in der Leiste). Nur bei reinem Strg (nicht mit Alt/Meta/Shift).
    if (e.ctrlKey && !e.altKey && !e.metaKey && !e.shiftKey && (e.key === "v" || e.key === "V")) {
      if (!_autoClipWorking) { _autoClipWorking = true; pasteToRemote().finally(() => { _autoClipWorking = false; }); }
      return;
    }
    if (e.ctrlKey && !e.altKey && !e.metaKey && !e.shiftKey && (e.key === "c" || e.key === "C")) {
      copyFromRemote();
      return;
    }
    if (e.ctrlKey && !e.altKey && !e.metaKey && !e.shiftKey && (e.key === "x" || e.key === "X")) {
      // Ausschneiden = kopieren + Agent führt Strg+X ohnehin aus; danach abholen.
      sendInput({ type: "combo", keys: ["Control", "x"] });
      setTimeout(() => getClip(), 140);
      return;
    }

    // Tastenkombination mit Modifiern (Strg/Alt) -> als "combo" senden
    if (e.ctrlKey || e.altKey || e.metaKey) {
      const keys = [];
      if (e.ctrlKey) keys.push("Control");
      if (e.altKey) keys.push("Alt");
      if (e.metaKey) keys.push("Meta");
      if (e.shiftKey) keys.push("Shift");
      // Die eigentliche Taste (nicht die Modifier selbst)
      if (!["Control", "Alt", "Meta", "Shift"].includes(e.key)) {
        keys.push(e.key.length === 1 ? e.key.toLowerCase() : e.key);
      }
      if (keys.length) sendInput({ type: "combo", keys });
      return;
    }

    // Sticky-Modifier (Toolbar-Toggles) auf die Taste anwenden.
    if (activeMods().length) {
      sendKeyWithMods(e.key.length === 1 ? e.key.toLowerCase() : e.key);
      return;
    }

    // Normales druckbares Zeichen -> als Text tippen
    if (e.key.length === 1) {
      sendInput({ type: "text", text: e.key });
    } else {
      // Sondertaste (Enter, Backspace, Pfeile, ...)
      sendInput({ type: "key", key: e.key });
    }
  });

  // Strg+Alt+Entf Button (lässt sich aus dem Browser nicht als echte Taste abfangen)
  ctrlAltDelBtn.addEventListener("click", () => {
    sendInput({ type: "combo", keys: ["Control", "Alt", "Delete"] });
  });

  // --- Text senden (kompletten Text auf einmal an den Cursor des Clients tippen) ---
  function sendText() {
    const text = textInput.value;
    if (!text) return;
    const layout = layoutSel.value;
    // Layout-Kompensation (andersrum, auf Nutzerwunsch): bei Auswahl "us"
    // werden die Zeichen vor dem Senden ueber die Positions-Tabelle ersetzt
    // (Reverse-Effekt); "de"/"raw" = 1:1 senden.
    const mapped = mapKeyboardText(text, layout);
    sendInput({ type: "text", text: mapped, layout });
    textInput.value = "";
    textInput.focus();
  }
  sendBtn.addEventListener("click", sendText);
  textInput.addEventListener("keydown", (e) => {
    // Enter sendet; Strg+V (Einfügen) im Feld normal zulassen
    if (e.key === "Enter") { e.preventDefault(); sendText(); }
  });

  // --- Aufräumen, wenn das Fenster geschlossen wird ---
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
    stopScreen();
    if (rdpActive) dashboardSocket.emit("rdp-stop", { clientId });
    window.removeEventListener("mouseup", onMouseUp);
  });
}
