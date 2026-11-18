// calls.js (Modul "calls") - Anruf-Fenster (Teams-ähnlich)
// ---------------------------------------------------------
// Einzel- und Gruppenanrufe per WebRTC-Mesh: Jeder Teilnehmer verbindet sich
// direkt mit jedem anderen. Medien (Mikrofon, Kamera, Bildschirm) laufen
// Peer-zu-Peer und sind Ende-zu-Ende verschlüsselt (DTLS-SRTP) - das Backend
// vermittelt nur signierte Signalisierung (siehe init.js / calls_routes.py).
//
// Läuft im Browser und in der Client-App (deren Dashboard-Fenster ist ein
// Chromium-App-Fenster mit vollem WebRTC). Im reinen LAN ohne Internet
// funktionieren Host-Kandidaten direkt; STUN wird nicht benötigt.

import { moduleRequest, getModuleApi } from "/js/app/modules.js";
import { state } from "/js/app/state.js";
import { esc, uiConfirm } from "/js/app/utils.js";
import { registerCleanup } from "/js/app/windowmanager.js";

const RTC_CONFIG = { iceServers: [] };   // LAN: direkte Kandidaten; STUN/TURN optional später

export function renderCalls(body, win) {
  const me = state.user?.username || "";
  let room = win.props?.room || null;
  let wantVideo = !!win.props?.video;
  let users = [];
  const peers = new Map();    // username -> { pc, videoEl, tileEl, streams }
  let localStream = null;     // Mikro (+ ggf. Kamera)
  let screenStream = null;
  let camOn = false, micOn = true, shareOn = false;
  let alive = true;

  body.innerHTML = `
    <div style="display:flex;flex-direction:column;height:100%;background:var(--panel)">
      <div id="cl-start" style="flex:1;display:flex;flex-direction:column;min-height:0">
        <div style="padding:10px 14px;border-bottom:1px solid var(--border)">
          <b>📞 Neuen Anruf starten</b>
          <span style="font-size:11px;color:var(--subtext)"> · Mehrfachauswahl = Gruppenanruf · Ende-zu-Ende verschlüsselt</span>
        </div>
        <div id="cl-users" style="flex:1;overflow:auto;padding:8px 14px"></div>
        <div style="display:flex;gap:8px;padding:10px 14px;border-top:1px solid var(--border)">
          <button class="btn-primary" id="cl-audio">📞 Anrufen</button>
          <button class="btn" id="cl-video">📹 Mit Video anrufen</button>
        </div>
      </div>

      <div id="cl-call" style="flex:1;display:none;flex-direction:column;min-height:0">
        <div id="cl-grid" style="flex:1;display:grid;gap:8px;padding:10px;overflow:auto;
             grid-template-columns:repeat(auto-fit,minmax(260px,1fr));align-content:start;background:#0b0f14"></div>
        <div style="display:flex;gap:8px;padding:10px 14px;border-top:1px solid var(--border);align-items:center">
          <button class="btn" id="cl-mic" title="Mikrofon an/aus">🎙️ An</button>
          <button class="btn" id="cl-cam" title="Kamera an/aus">📷 Aus</button>
          <button class="btn" id="cl-share" title="Bildschirm teilen">🖥️ Teilen</button>
          <button class="btn" id="cl-add" title="Weitere Person einladen">＋👤</button>
          <span id="cl-info" style="flex:1;font-size:11px;color:var(--subtext)">🔒 Ende-zu-Ende verschlüsselt (DTLS-SRTP, Peer-zu-Peer)</span>
          <button class="btn btn-danger" id="cl-hangup">📵 Auflegen</button>
        </div>
      </div>
    </div>`;

  const $ = (s) => body.querySelector(s);
  const grid = $("#cl-grid");

  // ---------------- Kacheln ----------------
  function addTile(name, stream, label) {
    let p = peers.get(name);
    const tile = document.createElement("div");
    tile.style.cssText = "position:relative;background:#101722;border:1px solid var(--border);border-radius:10px;overflow:hidden;min-height:180px";
    tile.innerHTML = `
      <video autoplay playsinline ${name === me ? "muted" : ""} style="width:100%;height:100%;min-height:180px;object-fit:contain;background:#000"></video>
      <div style="position:absolute;left:8px;bottom:6px;background:rgba(0,0,0,.55);border-radius:6px;padding:2px 8px;font-size:12px;color:#fff">${esc(label || name)}</div>`;
    const videoEl = tile.querySelector("video");
    if (stream) videoEl.srcObject = stream;
    grid.appendChild(tile);
    if (p) { p.tileEl?.remove(); p.tileEl = tile; p.videoEl = videoEl; }
    return { tileEl: tile, videoEl };
  }

  function removePeer(name) {
    const p = peers.get(name);
    if (!p) return;
    try { p.pc.close(); } catch { /* egal */ }
    p.tileEl?.remove();
    peers.delete(name);
  }

  // ---------------- WebRTC-Mesh ----------------
  function currentTracks() {
    const out = [];
    if (localStream) for (const t of localStream.getTracks()) out.push([t, localStream]);
    if (screenStream) for (const t of screenStream.getTracks()) out.push([t, screenStream]);
    return out;
  }

  function newPeer(name) {
    removePeer(name);
    const pc = new RTCPeerConnection(RTC_CONFIG);
    const ui = addTile(name, null, name);
    // "Perfect Negotiation": BEIDE Seiten dürfen Angebote erstellen (nötig,
    // damit auch die Gegenseite Kamera/Screenshare mitten im Anruf zuschalten
    // kann). Kollisionen löst die höfliche Seite (lexikografisch kleinerer
    // Name) durch Nachgeben auf.
    const p = { pc, ...ui, queue: [], makingOffer: false, polite: me < name };
    peers.set(name, p);

    for (const [t, s] of currentTracks()) { try { pc.addTrack(t, s); } catch { /* egal */ } }
    // Datenkanal erzwingt die erste Verhandlung auch ohne Mikrofon/Kamera.
    try { pc.createDataChannel("keepalive"); } catch { /* egal */ }

    pc.ontrack = (ev) => {
      const stream = ev.streams[0] || new MediaStream([ev.track]);
      p.videoEl.srcObject = stream;
    };
    pc.onicecandidate = (ev) => {
      if (ev.candidate) sendSignal(name, { type: "ice", candidate: ev.candidate.toJSON() });
    };
    pc.onconnectionstatechange = () => {
      if (["failed", "closed"].includes(pc.connectionState)) removePeer(name);
    };
    pc.onnegotiationneeded = async () => {
      try {
        p.makingOffer = true;
        await pc.setLocalDescription();
        sendSignal(name, { type: "offer", sdp: pc.localDescription.sdp });
      } catch (e) { console.warn("[calls] offer:", e); }
      finally { p.makingOffer = false; }
    };
    return p;
  }

  async function sendSignal(to, data) {
    try {
      await moduleRequest("/api/calls/signal", {
        method: "POST", noJob: true,
        body: JSON.stringify({ room, to, data }),
      });
    } catch (e) { console.warn("[calls] signal:", e.message); }
  }

  async function handleSignal(env) {
    const from = env.from, d = env.payload || {};
    let p = peers.get(from);
    try {
      if (d.type === "offer") {
        if (!p) p = newPeer(from);
        // Kollision: Wir machen selbst gerade ein Angebot. Die UNhoefliche
        // Seite ignoriert das fremde Angebot, die hoefliche gibt nach
        // (impliziter Rollback beim setRemoteDescription - Standard-Muster).
        const collision = p.makingOffer || p.pc.signalingState !== "stable";
        if (collision && !p.polite) return;
        await p.pc.setRemoteDescription({ type: "offer", sdp: d.sdp });
        await p.pc.setLocalDescription();
        sendSignal(from, { type: "answer", sdp: p.pc.localDescription.sdp });
        for (const c of p.queue.splice(0)) { try { await p.pc.addIceCandidate(c); } catch { /* egal */ } }
      } else if (d.type === "answer" && p) {
        await p.pc.setRemoteDescription({ type: "answer", sdp: d.sdp });
        for (const c of p.queue.splice(0)) { try { await p.pc.addIceCandidate(c); } catch { /* egal */ } }
      } else if (d.type === "ice" && p) {
        if (p.pc.remoteDescription) { try { await p.pc.addIceCandidate(d.candidate); } catch { /* egal */ } }
        else p.queue.push(d.candidate);
      }
    } catch (e) { console.warn("[calls] signal:", e); }
  }

  function renegotiateAll() {
    // Neue Spuren (Kamera/Screenshare) an alle bestehenden Verbindungen
    // haengen - onnegotiationneeded verhandelt dann automatisch nach.
    for (const [, p] of peers) {
      if (!p.pc) continue;
      for (const [t, s] of currentTracks()) {
        if (![...p.pc.getSenders()].some((sn) => sn.track === t)) {
          try { p.pc.addTrack(t, s); } catch { /* egal */ }
        }
      }
    }
  }

  // ---------------- Medien ----------------
  async function openMedia(withVideo) {
    try {
      localStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: withVideo });
      camOn = withVideo;
    } catch (e) {
      if (withVideo) {
        try { localStream = await navigator.mediaDevices.getUserMedia({ audio: true }); camOn = false; }
        catch (e2) { throw e2; }
      } else throw e;
    }
    addTile(me, localStream, `${me} (du)`);
    peers.set(me, { ...peers.get(me), ...addTileRef(me) });
    updateBtns();
  }
  function addTileRef() { return {}; }   // Selbst-Kachel braucht keine PC-Verwaltung

  function updateBtns() {
    $("#cl-mic").textContent = micOn ? "🎙️ An" : "🔇 Aus";
    $("#cl-cam").textContent = camOn ? "📷 An" : "📷 Aus";
    $("#cl-share").textContent = shareOn ? "🖥️ Stopp" : "🖥️ Teilen";
  }

  $("#cl-mic").addEventListener("click", () => {
    micOn = !micOn;
    localStream?.getAudioTracks().forEach((t) => { t.enabled = micOn; });
    updateBtns();
  });

  $("#cl-cam").addEventListener("click", async () => {
    if (camOn) {
      localStream?.getVideoTracks().forEach((t) => { t.stop(); localStream.removeTrack(t); });
      camOn = false;
    } else {
      try {
        const v = await navigator.mediaDevices.getUserMedia({ video: true });
        const track = v.getVideoTracks()[0];
        localStream.addTrack(track);
        camOn = true;
      } catch (e) { window.notify?.("Kamera nicht verfügbar: " + e.message, "error"); return; }
    }
    const selfTile = [...grid.querySelectorAll("video")].find((v) => v.muted);
    if (selfTile) selfTile.srcObject = localStream;
    renegotiateAll();
    updateBtns();
  });

  $("#cl-share").addEventListener("click", async () => {
    if (shareOn) {
      screenStream?.getTracks().forEach((t) => t.stop());
      screenStream = null; shareOn = false;
      renegotiateAll(); updateBtns(); return;
    }
    try {
      screenStream = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: false });
      shareOn = true;
      screenStream.getVideoTracks()[0].addEventListener("ended", () => {
        screenStream = null; shareOn = false; renegotiateAll(); updateBtns();
      });
      renegotiateAll(); updateBtns();
    } catch { /* abgebrochen */ }
  });

  $("#cl-add").addEventListener("click", async () => {
    const { uiPrompt } = await import("/js/app/utils.js");
    const who = await uiPrompt("Benutzername, der eingeladen werden soll:");
    if (!who) return;
    try {
      await moduleRequest("/api/calls/invite", {
        method: "POST", noJob: true,
        body: JSON.stringify({ to: [who.trim()], video: wantVideo, room }),
      });
      window.notify?.(`${who} wird angerufen …`, "info");
    } catch (e) { window.notify?.(e.message, "error"); }
  });

  $("#cl-hangup").addEventListener("click", () => hangup(true));

  async function hangup(closeWin) {
    if (!alive) return;
    alive = false;
    try { if (room) await moduleRequest("/api/calls/leave", { method: "POST", noJob: true, body: JSON.stringify({ room }) }); } catch { /* egal */ }
    for (const name of [...peers.keys()]) removePeer(name);
    localStream?.getTracks().forEach((t) => t.stop());
    screenStream?.getTracks().forEach((t) => t.stop());
    if (closeWin) {
      const { closeWindow } = await import("/js/app/windowmanager.js").catch(() => ({}));
      try { closeWindow?.(win.key); } catch { /* egal */ }
    }
  }

  // ---------------- Signal-Abo ----------------
  const callsApi = getModuleApi("calls");
  const unsub = callsApi?.onCallMessage?.((env) => {
    if (!room || env.room !== room || env.to !== me) return;
    if (env.event === "signal") handleSignal(env);
    else if (env.event === "peer-join") { newPeer(env.from); window.notify?.(`${env.from} ist dem Anruf beigetreten`, "info"); }
    else if (env.event === "peer-leave") { removePeer(env.from); window.notify?.(`${env.from} hat den Anruf verlassen`, "info"); }
    else if (env.event === "declined") window.notify?.(`${env.from} hat abgelehnt`, "warning");
  });

  registerCleanup(win.key, () => { unsub?.(); hangup(false); });

  // ---------------- Start ----------------
  async function enterCall() {
    $("#cl-start").style.display = "none";
    $("#cl-call").style.display = "flex";
    try {
      await openMedia(wantVideo);
    } catch (e) {
      window.notify?.("Mikrofon/Kamera nicht verfügbar: " + e.message, "error");
    }
    const res = await moduleRequest("/api/calls/join", {
      method: "POST", noJob: true, body: JSON.stringify({ room }) });
    wantVideo = wantVideo || !!res.video;
    // Bestehende Teilnehmer rufen UNS an (peer-join wurde ihnen zugestellt) -
    // wir warten also einfach auf deren Angebote.
  }

  async function startCall(video) {
    const sel = [...body.querySelectorAll("[data-user]:checked")].map((c) => c.dataset.user);
    if (!sel.length) { window.notify?.("Bitte mindestens einen Benutzer anhaken", "info"); return; }
    wantVideo = video;
    try {
      const res = await moduleRequest("/api/calls/invite", {
        method: "POST", noJob: true, body: JSON.stringify({ to: sel, video }) });
      room = res.room;
      const offline = (res.rang || []).filter((r) => !r.online).map((r) => r.user);
      if (offline.length) window.notify?.(`Nicht erreichbar (offline): ${offline.join(", ")}`, "warning");
      await enterCall();
    } catch (e) { window.notify?.(e.message, "error"); }
  }
  $("#cl-audio").addEventListener("click", () => startCall(false));
  $("#cl-video").addEventListener("click", () => startCall(true));

  async function loadUsers() {
    try { users = await moduleRequest("/api/calls/users"); } catch (e) { users = []; }
    $("#cl-users").innerHTML = users.map((u) => `
      <label style="display:flex;align-items:center;gap:8px;padding:6px 4px;border-bottom:1px solid var(--border);cursor:pointer">
        <input type="checkbox" data-user="${esc(u.username)}">
        <span style="width:8px;height:8px;border-radius:50%;background:${u.online ? "var(--online,#34d399)" : "var(--subtext)"}"></span>
        <span style="flex:1">${esc(u.display_name)} <span style="color:var(--subtext);font-size:11px">(${esc(u.username)})</span></span>
        <span style="font-size:11px;color:var(--subtext)">${u.online ? "online" : "offline"}</span>
      </label>`).join("") || `<div style="color:var(--subtext);font-size:13px;padding:8px">Keine anderen Benutzer gefunden.</div>`;
  }

  if (room) enterCall();      // per Klingel-Popup geöffnet -> direkt beitreten
  else loadUsers();
}
