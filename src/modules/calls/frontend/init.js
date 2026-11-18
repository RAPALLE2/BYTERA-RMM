// init.js (Modul "calls") - läuft global im Dashboard
// ----------------------------------------------------
// 1) Registriert den Dashboard-Socket dieses Benutzers per REST beim Backend
//    (nur so erreichen ihn Klingeln + Signalisierung; die Zuordnung ist damit
//    an die Anmeldung gebunden, nicht an behauptete Namen am Socket).
// 2) Prüft die Backend-Signatur JEDER Anruf-Nachricht (HMAC-SHA256).
// 3) Lässt es überall im Dashboard klingeln - egal ob die Anruf-App offen ist.

import { dashboardSocket } from "/js/app/socket.js";
import { moduleRequest, registerModuleApi } from "/js/app/modules.js";
import { esc } from "/js/app/utils.js";

let _key = null;              // HMAC-Prüfschlüssel (hex), nur angemeldet abrufbar
const _handlers = new Set();  // Signal-Abonnenten der Anruf-Fenster

async function _verifyKey() {
  if (!_key) _key = (await moduleRequest("/api/calls/verify-key")).key;
  return _key;
}

function _hexToBytes(hex) {
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.substr(i * 2, 2), 16);
  return out;
}

async function _sha256hex(text) {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function _canonical(obj) {
  // JSON mit sortierten Schlüsseln - muss zur Backend-Seite passen
  // (json.dumps(..., sort_keys=True, separators=(",", ":"))).
  if (obj === null || typeof obj !== "object") return JSON.stringify(obj);
  if (Array.isArray(obj)) return "[" + obj.map(_canonical).join(",") + "]";
  return "{" + Object.keys(obj).sort().map((k) => JSON.stringify(k) + ":" + _canonical(obj[k])).join(",") + "}";
}

/** Backend-Signatur eines Anruf-Umschlags prüfen. false = Fälschung/verworfen. */
export async function verifyEnvelope(env) {
  try {
    const keyHex = await _verifyKey();
    const payloadHash = await _sha256hex(_canonical(env.payload ?? {}));
    const msg = `${env.event}|${env.room}|${env.from}|${env.to}|${env.ts}|${payloadHash}`;
    const key = await crypto.subtle.importKey("raw", _hexToBytes(keyHex),
      { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
    const mac = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(msg));
    const hex = [...new Uint8Array(mac)].map((b) => b.toString(16).padStart(2, "0")).join("");
    return hex === env.sig;
  } catch {
    return false;
  }
}

async function _register() {
  try {
    if (dashboardSocket.id) {
      await moduleRequest("/api/calls/register", {
        method: "POST", noJob: true, body: JSON.stringify({ sid: dashboardSocket.id }),
      });
    }
  } catch { /* nicht angemeldet o.ä. - beim nächsten Connect erneut */ }
}

// --------------------------- Klingeln ---------------------------
let _ringBox = null;
let _ringAudio = null;

function _stopRing() {
  _ringBox?.remove(); _ringBox = null;
  try { _ringAudio?.stop(); } catch { /* egal */ }
  _ringAudio = null;
}

function _ringTone() {
  // Dezenter Klingelton per WebAudio (keine Datei nötig, offline-tauglich).
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const gain = ctx.createGain(); gain.gain.value = 0.06; gain.connect(ctx.destination);
    const osc = ctx.createOscillator(); osc.type = "sine"; osc.frequency.value = 880; osc.connect(gain);
    const lfo = ctx.createOscillator(); lfo.frequency.value = 2;
    const lfoGain = ctx.createGain(); lfoGain.gain.value = 0.06;
    lfo.connect(lfoGain); lfoGain.connect(gain.gain); lfo.start();
    osc.start();
    return { stop: () => { try { osc.stop(); lfo.stop(); ctx.close(); } catch { /* egal */ } } };
  } catch { return null; }
}

async function _showRing(env) {
  _stopRing();
  const p = env.payload || {};
  _ringAudio = _ringTone();
  const box = document.createElement("div");
  _ringBox = box;
  box.style.cssText = `position:fixed;top:18px;right:18px;z-index:12000;background:var(--panel);
    border:1px solid var(--accent);border-radius:12px;padding:14px 16px;width:300px;
    box-shadow:0 10px 40px rgba(0,0,0,.6);font-size:13px;color:var(--text)`;
  box.innerHTML = `
    <div style="font-size:15px;font-weight:700;display:flex;align-items:center;gap:8px">
      ${p.video ? "📹" : "📞"} <span>${esc(env.from)}</span>
    </div>
    <div style="color:var(--subtext);margin:4px 0 10px">
      ${p.group ? "lädt dich zu einem Gruppenanruf ein" : "ruft dich an"}
      ${p.video ? " (mit Video)" : ""} - Ende-zu-Ende verschlüsselt</div>
    <div style="display:flex;gap:8px">
      <button class="btn-primary" data-acc style="flex:1">✅ Annehmen</button>
      <button class="btn btn-danger" data-dec style="flex:1">✖ Ablehnen</button>
    </div>`;
  document.body.appendChild(box);
  const to = setTimeout(_stopRing, 45000);
  box.querySelector("[data-dec]").addEventListener("click", async () => {
    clearTimeout(to); _stopRing();
    try { await moduleRequest("/api/calls/decline", { method: "POST", noJob: true, body: JSON.stringify({ room: env.room }) }); } catch { /* egal */ }
  });
  box.querySelector("[data-acc]").addEventListener("click", async () => {
    clearTimeout(to); _stopRing();
    const { openWindow } = await import("/js/app/windowmanager.js");
    openWindow({ key: `calls-${env.room}`, appId: "calls", title: "📞 Anruf",
                 props: { room: env.room, video: !!p.video }, w: 900, h: 640 });
  });
}

/** Anruf-Fenster abonnieren eingehende (geprüfte!) Umschläge. */
export function onCallMessage(fn) { _handlers.add(fn); return () => _handlers.delete(fn); }

export function init() {
  dashboardSocket.on("connect", _register);
  if (dashboardSocket.connected) _register();

  dashboardSocket.on("call-msg", async (env) => {
    if (!env || typeof env !== "object") return;
    if (!(await verifyEnvelope(env))) {
      console.warn("[calls] Nachricht mit UNGÜLTIGER Signatur verworfen", env.event);
      return;
    }
    if (env.event === "ring") { _showRing(env); return; }
    if (env.event === "declined" || env.event === "peer-leave") _stopRing();
    for (const fn of [..._handlers]) { try { fn(env); } catch (e) { console.error(e); } }
  });

  registerModuleApi("calls", { onCallMessage, verifyEnvelope });
}
