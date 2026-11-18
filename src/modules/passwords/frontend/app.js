// app.js (Modul "passwords") - Passwort-Manager (Ende-zu-Ende)
// -------------------------------------------------------------
// Entsperren mit Master-Passwort -> Tresore/Ordner (KeePass-artig) -> Einträge.
// Alle Krypto im Browser (crypto.js). Session (Schlüssel) lebt nur im Speicher
// dieses Fensters und wird beim Sperren verworfen.
import { state, findClient } from "/js/app/state.js";
import { moduleRequest } from "/js/app/modules.js";
import { esc, uiConfirm, uiPrompt } from "/js/app/utils.js";
import * as C from "./crypto.js";
import * as V from "./vault.js";

const api = (p, o = {}) => moduleRequest("/api/pm" + p, o);
const toast = (m, t = "success", ms = 1900) => window.notify?.(m, t, ms);

export function renderPasswords(body, win) {
  if (!C.cryptoReady) { body.innerHTML = `<div style="padding:30px;color:var(--danger)">Dieser Browser bietet keine Web-Crypto (nur über HTTPS verfügbar).</div>`; return; }
  const ctx = { session: null, account: null, vaults: [], vaultKeys: new Map(), focusClient: win?.props?.clientId || null };
  V.mount(body, ctx, { api, C, toast, state, findClient, esc, uiConfirm, uiPrompt, win });
}
