// app.js (module "passwords") - password manager (end-to-end encrypted)
// -----------------------------------------------------------------------
// Unlock with the master password -> vaults/folders (KeePass-like) -> entries.
// All crypto happens in the browser (crypto.js). The session (keys) lives only
// in this window's memory and is discarded on lock.
import { state, findClient } from "/js/app/state.js";
import { moduleRequest } from "/js/app/modules.js";
import { esc, uiConfirm, uiPrompt } from "/js/app/utils.js";
import * as C from "./crypto.js";
import * as V from "./vault.js";

const api = (p, o = {}) => moduleRequest("/api/pm" + p, o);
const toast = (m, t = "success", ms = 1900) => window.notify?.(m, t, ms);

export function renderPasswords(body, win) {
  if (!C.cryptoReady) { body.innerHTML = `<div style="padding:30px;color:var(--danger)">This browser does not provide Web Crypto (only available over HTTPS).</div>`; return; }
  const ctx = { session: null, account: null, vaults: [], vaultKeys: new Map(), focusClient: win?.props?.clientId || null };
  V.mount(body, ctx, { api, C, toast, state, findClient, esc, uiConfirm, uiPrompt, win });
}
