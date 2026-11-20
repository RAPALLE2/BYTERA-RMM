// api.js
// ------
// Small helper for all calls to the backend. Bundles two things that would
// otherwise have to be repeated in every single fetch() call:
//   1. Automatically send the login token (JWT) from localStorage
//   2. Handle errors uniformly (the backend returns {"detail": "..."} on errors)

import { BACKEND_URL } from "./config.js";
import { t } from "./i18n.js";

// Client app (module "client"): the app opens the dashboard with
// "#client-token=<session>". The token is adopted here IMMEDIATELY and removed
// from the address bar (it appears neither in the history nor in logs).
(function adoptClientToken() {
  try {
    const m = /(?:^#|&)client-token=([A-Za-z0-9._-]+)/.exec(location.hash || "");
    if (!m) return;
    localStorage.setItem("rmm_token", m[1]);
    // From client app 1.4: "&client-app=<version>" -> RDP/VNC/SSH can be opened
    // externally/locally (module "client", bytera-rmm://). The app uses its own browser profile,
    // so the marker applies only in its windows.
    const ca = /(?:^#|&)client-app=([A-Za-z0-9._-]+)/.exec(location.hash || "");
    if (ca) localStorage.setItem("rmm_client_app", ca[1]); else localStorage.removeItem("rmm_client_app");
    // Tenant module: "&embedded=1" -> this dashboard runs embedded in the main RMM.
    if (/(?:^#|&)embedded=1/.test(location.hash || "")) document.documentElement.classList.add("rmm-embedded");
    const rest = (location.hash || "").replace(/(^#|&)client-token=[A-Za-z0-9._-]+/, "$1")
      .replace(/(^#|&)client-app=[A-Za-z0-9._-]+/, "$1").replace(/(^#|&)embedded=1/, "$1").replace(/^#&+/, "#").replace(/&&+/g, "&");
    history.replaceState(null, "", location.pathname + location.search + (rest === "#" ? "" : rest));
  } catch { /* ignore */ }
})();

export function getToken() {
  return localStorage.getItem("rmm_token");
}

// A 403 from the server almost always means "permission missing". We try to
// derive the technical permission key from the message and a readable
// description from it, then show the dialog with the ticket offer.
//
// No hard import of permdenied.js: api.js is loaded by very many modules,
// so a circular import would be inevitable. Hence the global window hook.
let _lastPermDialog = 0;
function _permissionDialog(message, path) {
  try {
    const show = window.showPermissionDenied;
    if (typeof show !== "function") return;
    // Only for messages that really sound like a missing permission. A 403
    // can have other causes (expired share, lock) - a ticket offer would be
    // confusing there.
    const looksLikePerm = /Missing (right|permission)|not allowed|No access|Administrators only/i
      .test(message || "");
    if (!looksLikePerm) return;
    // Endpoints that run constantly in the background must not pop up windows -
    // otherwise the dialog appears unprompted while the dashboard loads.
    if (/\/api\/auth\/(effective|me)|\/api\/notify|\/api\/clients$/.test(path || "")) return;
    // With several parallel calls, do not stack multiple windows.
    const now = Date.now();
    if (now - _lastPermDialog < 8000) return;
    _lastPermDialog = now;

    const m = /Missing (?:right|permission):\s*([\w:.\-]+)/i.exec(message || "");
    const perm = m ? m[1] : null;
    // Pull the client ID from the path if one is present.
    const cm = /\/clients?\/([0-9a-f-]{8,})/i.exec(path || "");
    show({
      action: _actionFromPerm(perm) || message || "This action",
      perm,
      clientId: cm ? cm[1] : null,
      detail: perm ? "" : message,
    });
  } catch { /* the dialog is a convenience - never let it swallow the error */ }
}

// Technical permission keys in plain text. Unknown keys are passed through
// unchanged - better the key than nothing.
// Technical permission keys -> translation keys. Deliberately ONLY the key
// names: this table is evaluated when the module loads, where the language
// is not fixed yet. A t() directly in the table would freeze the text to the
// load-time language. Translation happens only in permText().
const _PERM_KEY = {
  admin_settings: "perm_admin_settings",
  manage_settings: "perm_manage_settings",
  see_source: "perm_see_source",
  edit_source: "perm_edit_source",
  see_audit: "perm_see_audit",
  see_permissions: "perm_see_permissions",
  manage_permissions: "perm_manage_permissions",
  create_users: "perm_create_users",
  manage_clients: "perm_manage_clients",
  manage_agent: "perm_manage_agent",
  access_clients: "perm_access_clients",
  c_terminal: "perm_c_terminal",
  c_screen: "perm_c_screen",
  c_explorer_view: "perm_c_explorer_view",
  c_explorer_edit: "perm_c_explorer_edit",
  c_taskmanager_view: "perm_c_taskmanager_view",
  c_taskmanager_kill: "perm_c_taskmanager_kill",
  c_power: "perm_c_power",
  c_registry: "perm_c_registry",
  c_win_update: "perm_c_win_update",
  c_relay: "perm_c_relay",
  c_delete: "perm_c_delete",
  use_relay: "perm_use_relay",
  c_vpn: "perm_c_vpn",
  manage_nodes: "perm_manage_nodes",
  manage_modules: "perm_manage_modules",
  get_user_space: "perm_get_user_space", use_public_space: "perm_use_public_space",
  see_all_spaces: "perm_see_all_spaces", edit_all_spaces: "perm_edit_all_spaces",
  manage_deployment: "perm_manage_deployment",
  use_api: "perm_use_api",
  manage_websites: "perm_manage_websites",
  manage_docker: "perm_manage_docker",
  manage_cloudflare: "perm_manage_cloudflare",
  manage_headless_screen: "perm_manage_headless_screen",
  manage_canary: "perm_manage_canary",
  manage_ad: "perm_manage_ad",
  use_bmc: "perm_use_bmc",
  manage_bmc: "perm_manage_bmc",
  use_passwords: "perm_use_passwords",
  use_geotrack: "perm_use_geotrack",
  manage_geotrack: "perm_manage_geotrack",
  manage_tenants: "perm_manage_tenants",
  use_tenants: "perm_use_tenants",
  manage_dist: "perm_manage_dist",
  use_console: "perm_use_console",
  use_vpn: "perm_use_vpn",
  ticket_create: "perm_ticket_create",
  manage_privacy: "perm_manage_privacy",
  manage_patching: "perm_manage_patching",
  see_inventory: "perm_see_inventory",
  manage_inventory: "perm_manage_inventory",
  customize_dashboard: "perm_customize_dashboard",
};

function _actionFromPerm(perm) {
  if (!perm) return null;
  return _PERM_KEY[perm] ? t(_PERM_KEY[perm]) : perm;
}

// Time limit for every API call.
//
// Why this is needed: fetch() waits INDEFINITELY on its own. If the backend
// has died or a connection is stuck in the reverse proxy, a loading bar
// spins forever in the dashboard - and the user does not see that nothing
// is coming anymore. That is exactly what the
// "issuing a tunnel loads forever" looked like recently: not a slow
// endpoint, but a backend that no longer responded.
//
// A clear error after 45 seconds is always better than an
// endless spinner.
const REQUEST_TIMEOUT_MS = 45000;
// Endpoints that naturally take longer (files, database backups,
// node Docker install). A larger limit applies to them.
// Deliberately narrow: only truly slow operations get more time.
// A blanket "/api/nodes/" would be wrong - it also covers the
// lightning-fast state query.
const SLOW_PATHS = [
  "/api/admin/database/", "/api/admin/update", "/api/files/",
  "/api/recordings/", "/api/scripts/run", "/api/speedtest",
];
// These endpoints are inherently long - recognizable by the END of the
// path.
//
// '/update-agent' explicitly belongs here: the endpoint waits up to
// 60 seconds for the agent to report back after the restart.
// The general 45-second limit was BELOW that - so the request
// was aborted while the update was in fact still running and
// succeeded right afterwards. In the dashboard that looked like a
// crashed backend ("no answer after 45s"), although nothing was
// broken. The time limit must always be LARGER than the wait time on the
// other side, otherwise you report errors that are none.
// Each with the time matching the other side - NOT blanket-large.
// A too-generous limit is not good either: then the loading bar spins
// for ten minutes although the server gave up long ago.
const SLOW_SUFFIXES = {
  // The server waits 60s for the agent's response -> a bit more.
  "/update-agent": 90000,
  // An agent's file system: depends on its response time.
  "/fs": 120000,
};

function _timeoutFor(path) {
  const clean = path.split("?")[0];
  if (SLOW_PATHS.some((p) => clean.startsWith(p))) return 600000;
  // Docker install and providing/starting node modules take time (loading images).
  if (/^\/api\/nodes\/[^/]+\/(docker-install|modules\/)/.test(clean)) return 600000;
  for (const [suffix, ms] of Object.entries(SLOW_SUFFIXES)) {
    if (clean.endsWith(suffix)) return ms;
  }
  return REQUEST_TIMEOUT_MS;
}

// ------------------------------------------------------------------
// Tasks as a BACKEND JOB (app/bgjobs.py): the endpoint gets
// ?background=1, starts the job and responds immediately; here we then wait
// for the result (live via socket). For the caller it looks like before -
// but the work runs in the backend and is NOT aborted by reloading,
// closing or a proxy timeout. Everyone permitted sees it in the task
// tray (taskbar).
//   options.onJob(job): progress (job.detail, job.pct, job.phase ...)
// ------------------------------------------------------------------
async function jobRequest(path, options = {}) {
  return request(path, { timeoutMs: 60000, ...options });
}

async function request(path, options = {}) {
  // Mutating calls run as a BACKEND JOB where the endpoint can do it
  // (app/bgjobs.py): ?background=1 -> job immediately, then wait live for the
  // result (_awaitJob). Endpoints without a job simply ignore the parameter.
  // options.noWait: return the job response directly (e.g. server update).
  // options.onJob(job): intermediate states of a job.
  const method = (options.method || "GET").toUpperCase();
  const wantJob = method !== "GET" && !options.noJob && !/[?&]background=/.test(path);
  if (wantJob) path += (path.includes("?") ? "&" : "?") + "background=1";
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;

  // A separate abort controller per call. If the caller already passes its
  // own, its signal is not overwritten.
  const controller = options.signal ? null : new AbortController();
  const limit = options.timeoutMs || _timeoutFor(path);
  const timer = controller
    ? setTimeout(() => controller.abort(), limit)
    : null;

  let res;
  try {
    res = await fetch(`${BACKEND_URL}${path}`, {
      ...options, headers,
      signal: options.signal || controller.signal,
    });
  } catch (err) {
    if (err?.name === "AbortError") {
      throw new Error(
        `No response from the server after ${Math.round(limit / 1000)}s `
        + `(${path}). The backend is probably not reachable right now – `
        + `it usually restarts itself.`);
    }
    // Make network errors understandable: "Failed to fetch" tells nobody
    // anything, "server not reachable" does.
    throw new Error(`Server not reachable (${path}): ${err?.message || err}`);
  } finally {
    if (timer) clearTimeout(timer);
  }

  if (!res.ok) {
    let message = `${t("error")} ${res.status}`;
    let hadDetail = false;
    try {
      const body = await res.json();
      if (body.detail) { message = body.detail; hadDetail = true; }
    } catch { /* response was not JSON, never mind */ }
    // A 429 without a JSON body does not come from the RMM: the backend only
    // answers with 429 on login (lock after failed attempts) and then always
    // includes a reason. If HTML or nothing comes instead, a proxy/rate
    // limiter in front (Cloudflare, nginx, ...) rejected it.
    if (res.status === 429 && !hadDetail) {
      message = t("api_proxy_429");
    }
    // If a permission is missing, the user should not just see a red line
    // but immediately get the option to contact support.
    // So it is caught centrally here - that way it applies to EVERY call
    // without every app having to build it in itself.
    if (res.status === 403) _permissionDialog(message, path);
    throw new Error(message);
  }

  // Some endpoints (e.g. script downloads) return no JSON
  const contentType = res.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    const data = await res.json();
    if (method !== "GET" && !options.noWait && data && data.job === true && data.id) {
      return _awaitJob(data, options.onJob);
    }
    return data;
  }
  return res.text();
}

// Waits for a backend job (live via socket) and returns its result -
// for the caller like a normal response. The job itself runs in the
// backend and is not aborted by reloading/closing.
async function _awaitJob(started, onJob) {
  const { waitJob, fetchJob } = await import("./bgjobs.js");
  try { onJob?.(started); } catch (e) { console.error(e); }
  const job = started.running ? await waitJob(started.id, onJob) : await fetchJob(started.id);
  if (job.status === "error") throw new Error(job.error || t("error"));
  if (job.status === "cancelled") {
    const err = new Error(`Cancelled${job.cancelled_by ? " by " + job.cancelled_by : ""}`);
    err.cancelled = true;
    throw err;
  }
  return job.result !== undefined ? job.result : (await fetchJob(job.id)).result;
}

export const api = {
  // --- Screen recording (Guacamole 1:1 video) ---
  uploadRecordingVideo: async (clientId, hostname, startedAt, endedAt, blob) => {
    const q = new URLSearchParams({ client_id: clientId, hostname: hostname || "", started_at: String(startedAt), ended_at: String(endedAt) });
    const token = getToken();
    const res = await fetch(`${BACKEND_URL}/api/recordings/upload?${q.toString()}`, {
      method: "POST",
      headers: { "Content-Type": "video/webm", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      body: blob,
    });
    if (!res.ok) throw new Error(`Upload failed (${res.status})`);
    return res.json();
  },
  getRecordingVideoBlob: async (recId) => {
    const token = getToken();
    const res = await fetch(`${BACKEND_URL}/api/recordings/${recId}/video`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!res.ok) throw new Error(`Loading video failed (${res.status})`);
    return res.blob();
  },
  // --- Auth ---
  login: (username, password, realm = "local", code = "") =>
    request("/api/auth/login", { method: "POST", body: JSON.stringify({ username, password, realm, code }) }),
  // --- Two-factor login (TOTP) ---
  totpStatus: () => request("/api/auth/2fa/status"),
  totpSetup: () => request("/api/auth/2fa/setup", { method: "POST" }),
  totpActivate: (code) => request("/api/auth/2fa/activate", { method: "POST", body: JSON.stringify({ code }) }),
  // Confirm either by password OR by code from the app - SSO accounts
  // have no local password at all.
  totpDisable: (password, code = "") => request("/api/auth/2fa/disable", { method: "POST", body: JSON.stringify({ password, code }) }),
  totpNewBackupCodes: (password, code = "") => request("/api/auth/2fa/backup-codes", { method: "POST", body: JSON.stringify({ password, code }) }),
  getLoginRealms: () => request("/api/auth/realms"),
  me: () => request("/api/auth/me"),
  // Server-side stored UI settings (the same in every browser)
  getUiPrefs: () => request("/api/auth/ui-prefs"),
  saveUiPrefs: (keys) => request("/api/auth/ui-prefs", {
    method: "PUT", body: JSON.stringify({ keys }) }),
  // Partial update (merge): change only the passed keys. Used by the
  // frontend for EVERY sync so that an empty browser cache can never
  // wipe the server state.
  mergeUiPrefs: (keys) => request("/api/auth/ui-prefs", {
    method: "PATCH", body: JSON.stringify({ keys }) }),
  // One-time silent mode for the remote screen
  getSilentScreen: () => request("/api/auth/silent-screen"),
  setSilentScreen: (enabled) => request("/api/auth/silent-screen", {
    method: "PUT", body: JSON.stringify({ enabled: !!enabled }) }),

  // ---- Spotify ----
  getSpotifyConfig: () => request("/api/auth/spotify-config"),

  // ---- AI-Chat ----
  aiConnections: () => request("/api/ai/connections"),
  aiCreateConnection: (body) => request("/api/ai/connections", {
    method: "POST", body: JSON.stringify(body) }),
  aiUpdateConnection: (id, body) => request(`/api/ai/connections/${id}`, {
    method: "PUT", body: JSON.stringify(body) }),
  aiDeleteConnection: (id) => request(`/api/ai/connections/${id}`, { method: "DELETE" }),
  aiShareSubjects: () => request("/api/ai/share-subjects"),
  aiChat: (connectionId, messages) => request("/api/ai/chat", {
    method: "POST", body: JSON.stringify({ connection_id: connectionId, messages }) }),

  // ---- Gaming-Hub-Scoreboard ----
  gameScores: () => request("/api/games/scores"),
  submitGameScore: (game, score) => request("/api/games/scores", {
    method: "POST", body: JSON.stringify({ game, score }) }),

  // ---- Tickets ----
  tickets: () => request("/api/tickets"),
  ticket: (id) => request(`/api/tickets/${id}`),
  createTicket: (body) => request("/api/tickets", {
    method: "POST", body: JSON.stringify(body) }),
  updateTicket: (id, body) => request(`/api/tickets/${id}`, {
    method: "PUT", body: JSON.stringify(body) }),
  deleteTicket: (id) => request(`/api/tickets/${id}`, { method: "DELETE" }),
  setTicketAssignees: (id, assignees) => request(`/api/tickets/${id}/assignees`, {
    method: "PUT", body: JSON.stringify({ assignees }) }),
  setTicketStatus: (id, status) => request(`/api/tickets/${id}/status`, {
    method: "PUT", body: JSON.stringify({ status }) }),
  addTicketComment: (id, text, visibility = "all", sharedWith = []) =>
    request(`/api/tickets/${id}/comments`, {
      method: "POST",
      body: JSON.stringify({ text, visibility, shared_with: sharedWith }) }),
  ticketSubjects: () => request("/api/tickets/meta/subjects"),
  // File upload as a raw body (no multipart) - file name as a query param.
  uploadTicketFile: async (id, file) => {
    const token = localStorage.getItem("rmm_token");
    const res = await fetch(`/api/tickets/${id}/files?filename=${encodeURIComponent(file.name)}`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": file.type || "application/octet-stream" },
      body: file,
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Upload failed (${res.status})`);
    return res.json();
  },
  // Load an attachment with the auth header and return it as a blob URL (for <img>/download).
  fetchTicketFile: async (id, fileId) => {
    const token = localStorage.getItem("rmm_token");
    const res = await fetch(`/api/tickets/${id}/files/${fileId}`, {
      headers: { Authorization: `Bearer ${token}` } });
    if (!res.ok) throw new Error(`Download failed (${res.status})`);
    return URL.createObjectURL(await res.blob());
  },
  deleteTicketFile: (id, fileId) => request(`/api/tickets/${id}/files/${fileId}`, { method: "DELETE" }),
  changePassword: (current_password, new_password) =>
    request("/api/auth/change-password", { method: "POST", body: JSON.stringify({ current_password, new_password }) }),
  updateProfile: (data) => request("/api/auth/profile", { method: "PUT", body: JSON.stringify(data) }),

  // --- Hierarchie ---
  getHierarchy: () => request("/api/hierarchy"),
  createTenant: (name, color) => request("/api/tenants", { method: "POST", body: JSON.stringify({ name, color }) }),
  createLocation: (tenant_id, name) => request("/api/locations", { method: "POST", body: JSON.stringify({ tenant_id, name }) }),
  deleteTenant: (tenant_id) => request(`/api/tenants/${tenant_id}`, { method: "DELETE" }),
  deleteLocation: (location_id) => request(`/api/locations/${location_id}`, { method: "DELETE" }),
  createFolder: (location_id, name, parent_folder_id = null) =>
    request("/api/folders", { method: "POST", body: JSON.stringify({ location_id, name, parent_folder_id }) }),
  deleteFolder: (folder_id) => request(`/api/folders/${folder_id}`, { method: "DELETE" }),

  // --- Clients ---
  getClients: () => request("/api/clients"),
  getClient: (id) => request(`/api/clients/${id}`),
  getMetricsHistory: (id) => request(`/api/clients/${id}/metrics/history`),
  // Remote support: session history (from the audit log, like the connection list in AnyDesk)
  getScreenHistory: (id) => request(`/api/clients/${encodeURIComponent(id)}/screen-history`),
  updateClient: (id, fields) => request(`/api/clients/${id}`, { method: "PUT", body: JSON.stringify(fields) }),
  deleteClient: (id) => request(`/api/clients/${id}`, { method: "DELETE" }),
  execOnClient: (id, command, session, shell = "auto", elevated = false) =>
    jobRequest(`/api/clients/${id}/exec`, { method: "POST", body: JSON.stringify({ command, session, shell, elevated }) }),
  bulkExec: (client_ids, command, onJob) => jobRequest("/api/clients/bulk-exec", { method: "POST", body: JSON.stringify({ client_ids, command }), onJob }),
  listClientFs: (id, path) => request(`/api/clients/${id}/fs?path=${encodeURIComponent(path)}`),
  listProcesses: (id) => request(`/api/clients/${id}/processes`),
  killProcess: (id, pid) => request(`/api/clients/${id}/processes/kill`, { method: "POST", body: JSON.stringify({ pid }) }),
  readClientFile: (id, path) => request(`/api/clients/${id}/fs/read?path=${encodeURIComponent(path)}`),
  writeClientFile: (id, path, data) => request(`/api/clients/${id}/fs/write`, { method: "POST", body: JSON.stringify({ path, data }) }),
  mkdirClient: (id, path) => request(`/api/clients/${id}/fs/mkdir`, { method: "POST", body: JSON.stringify({ path }) }),
  deleteClientPath: (id, path) => request(`/api/clients/${id}/fs/delete`, { method: "POST", body: JSON.stringify({ path }) }),
  renameClientPath: (id, src, dst) => request(`/api/clients/${id}/fs/rename`, { method: "POST", body: JSON.stringify({ src, dst }) }),

  // --- Server file system: read/write ---
  readServerFile: (path) => request(`/api/server-files/read?path=${encodeURIComponent(path)}`),
  writeServerFile: (path, data) => request(`/api/server-files/write`, { method: "POST", body: JSON.stringify({ path, data }) }),
  mkdirServer: (path) => request(`/api/server-files/mkdir`, { method: "POST", body: JSON.stringify({ path }) }),
  deleteServerPath: (path) => request(`/api/server-files/delete`, { method: "POST", body: JSON.stringify({ path }) }),
  renameServerPath: (src, dst) => request(`/api/server-files/rename`, { method: "POST", body: JSON.stringify({ src, dst }) }),

  // --- Client websites (quick access / favorites / uptime monitoring) ---
  getClientWebsites: (id) => request(`/api/clients/${id}/websites`),
  createClientWebsite: (id, data) => request(`/api/clients/${id}/websites`, { method: "POST", body: JSON.stringify(data) }),
  updateClientWebsite: (id, websiteId, data) => request(`/api/clients/${id}/websites/${websiteId}`, { method: "PUT", body: JSON.stringify(data) }),
  deleteClientWebsite: (id, websiteId) => request(`/api/clients/${id}/websites/${websiteId}`, { method: "DELETE" }),
  getServerAddress: () => request("/api/server-address"),
  getRelayStatus: (clientId) => request(`/api/relay/status?client_id=${encodeURIComponent(clientId)}`),
  toggleRelay: (clientId, autoCloseMinutes = 0) => request(`/api/relay/toggle?client_id=${encodeURIComponent(clientId)}${autoCloseMinutes ? `&auto_close_minutes=${encodeURIComponent(autoCloseMinutes)}` : ""}`, { method: "POST" }),
  getFavoriteWebsites: () => request("/api/clients/websites/favorites"),

  // --- Server file system (the backend machine itself) ---
  listServerFs: (path) => request(`/api/server-files?path=${encodeURIComponent(path)}`),

  // --- Network scan ---
  // --- Media library (audio player / media hub) ---
  getMedia: (kind) => request(`/api/media${kind ? `?kind=${encodeURIComponent(kind)}` : ""}`),
  addMedia: (data) => request("/api/media", { method: "POST", body: JSON.stringify(data) }),
  updateMedia: (id, data) => request(`/api/media/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
  deleteMedia: (id) => request(`/api/media/${id}`, { method: "DELETE" }),
  // Favorites (star) - anyone who may see the entry may set it
  toggleMediaFavorite: (id) => request(`/api/media/${id}/favorite`, { method: "POST" }),
  getFavoriteMedia: () => request("/api/media?favorite=true"),
  // Playlists (radio, local files, links - also mixed)
  getPlaylists: (favOnly = false) => request(`/api/media/playlists${favOnly ? "?favorite=true" : ""}`),
  createPlaylist: (data) => request("/api/media/playlists", { method: "POST", body: JSON.stringify(data) }),
  updatePlaylist: (id, data) => request(`/api/media/playlists/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
  setPlaylistItems: (id, item_ids) => request(`/api/media/playlists/${id}/items`, { method: "POST", body: JSON.stringify({ item_ids }) }),
  deletePlaylist: (id) => request(`/api/media/playlists/${id}`, { method: "DELETE" }),
  mediaFileUrl: (id) => `/api/media/${id}/file?token=${encodeURIComponent(localStorage.getItem("rmm_token") || "")}`,
  uploadMedia: async (file, shared = false, onProgress) => {
    const token = localStorage.getItem("rmm_token");
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `/api/media/upload?filename=${encodeURIComponent(file.name)}&shared=${shared}`);
      xhr.setRequestHeader("Authorization", `Bearer ${token}`);
      xhr.setRequestHeader("Content-Type", file.type || "application/octet-stream");
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
      };
      xhr.onload = () => {
        let data = null;
        try { data = JSON.parse(xhr.responseText); } catch {}
        if (xhr.status >= 200 && xhr.status < 300) resolve(data);
        else reject(new Error(data?.detail || `Upload failed (${xhr.status})`));
      };
      xhr.onerror = () => reject(new Error("Upload failed (network)"));
      xhr.send(file);
    });
  },

  // --- Client notes (visibility + activity log) ---
  getNotes: (clientId) => request(`/api/clients/${clientId}/notes`),
  getNotesActivity: (clientId) => request(`/api/clients/${clientId}/notes/activity`),
  getNotesUsers: (clientId) => request(`/api/clients/${clientId}/notes/users`),
  createNote: (clientId, data) => request(`/api/clients/${clientId}/notes`, { method: "POST", body: JSON.stringify(data) }),
  updateNote: (clientId, noteId, data) => request(`/api/clients/${clientId}/notes/${noteId}`, { method: "PUT", body: JSON.stringify(data) }),
  deleteNote: (clientId, noteId) => request(`/api/clients/${clientId}/notes/${noteId}`, { method: "DELETE" }),

  // --- Tickets: log, comment visibility ---
  getTicketActivity: (ticketId) => request(`/api/tickets/${ticketId}/activity`),
  getTicketUsers: () => request("/api/tickets/meta/users"),
  deleteTicketComment: (ticketId, commentId) => request(`/api/tickets/${ticketId}/comments/${commentId}`, { method: "DELETE" }),

  // --- Org chart (super/subordination, workspaces) ---
  getOrgTree: () => request("/api/org/tree"),
  setOrgParent: (data) => request("/api/org/parent", { method: "PUT", body: JSON.stringify(data) }),
  setWorkspace: (userId, workspace) => request(`/api/org/users/${userId}/workspace`, {
    method: "PUT", body: JSON.stringify({ workspace }) }),
  getWorkspaces: () => request("/api/org/workspaces"),

  // --- Calendar ---
  getEvents: (start, end) => request(`/api/calendar/events?start=${start}&end=${end}`),
  getEventTargets: () => request("/api/calendar/targets"),
  createEvent: (data) => request("/api/calendar/events", { method: "POST", body: JSON.stringify(data) }),
  updateEvent: (id, data) => request(`/api/calendar/events/${id}`, { method: "PUT", body: JSON.stringify(data) }),
  deleteEvent: (id) => request(`/api/calendar/events/${id}`, { method: "DELETE" }),

  // --- Software patching ---
  getPatchOverview: () => request("/api/patches/overview"),
  getClientPatches: (id, status) => request(
    `/api/patches/client/${id}${status ? `?status=${status}` : ""}`),
  scanPatches: (id) => request(`/api/patches/client/${id}/scan`, { method: "POST" }),
  // Scan and install keep running in the background; here the progress
  // is fetched. Important behind reverse proxies: a synchronous response
  // would run into a gateway timeout there (Cloudflare 524).
  getPatchJob: (id) => request(`/api/patches/client/${id}/job`),
  getPatchReadiness: (id) => request(`/api/patches/client/${id}/readiness`),
  // Quick functional test on the client (seconds, not a full scan).
  patchSelftest: (id) => jobRequest(`/api/patches/client/${id}/selftest`, { method: "POST" }),
  applyPatches: (id, items) => request(`/api/patches/client/${id}/apply`, {
    method: "POST", body: JSON.stringify({ items: items || [] }) }),
  excludePatch: (patchId, excluded) => request(`/api/patches/${patchId}/exclude`, {
    method: "POST", body: JSON.stringify({ excluded }) }),
  getPatchRules: () => request("/api/patches/rules"),
  setPatchGlobalSwitch: (enabled) => request("/api/patches/rules/global/switch", {
    method: "PUT", body: JSON.stringify({ enabled }) }),
  savePatchGlobalRule: (values) => request("/api/patches/rules/global", {
    method: "PUT", body: JSON.stringify(values) }),
  savePatchClientRule: (id, values) => request(`/api/patches/rules/client/${id}`, {
    method: "PUT", body: JSON.stringify(values) }),
  deletePatchClientRule: (id) => request(`/api/patches/rules/client/${id}`, { method: "DELETE" }),
  setPatchPolicy: (id, policy) => request(`/api/patches/policy/${id}`, {
    method: "PUT", body: JSON.stringify({ policy }) }),
  getPatchRuns: (clientId) => request(
    `/api/patches/runs${clientId ? `?client_id=${clientId}` : ""}`),
  runPatchAuto: () => request("/api/patches/auto/run", { method: "POST" }),

  // --- Privacy / GDPR ---
  // The Art. 15/20 disclosure comes as a file download, so a raw fetch
  // instead of request() - which would parse JSON instead of offering a file.
  getMyPrivacyData: () => request("/api/privacy/me"),
  downloadPrivacyExport: async (userId) => {
    const path = userId ? `/api/privacy/users/${userId}/export` : "/api/privacy/me/export";
    const res = await fetch(`${BACKEND_URL}${path}`, {
      headers: { Authorization: `Bearer ${getToken()}` } });
    if (!res.ok) {
      let msg = `${t("error")} ${res.status}`;
      try { msg = (await res.json()).detail || msg; } catch {}
      throw new Error(msg);
    }
    const blob = await res.blob();
    const cd = res.headers.get("content-disposition") || "";
    const match = cd.match(/filename="?([^"]+)"?/);
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = match ? match[1] : "data-export.json";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  },
  requestErasure: (kind, reason) => request("/api/privacy/me/erasure-request", {
    method: "POST", body: JSON.stringify({ kind, reason }) }),
  getMyErasureRequests: () => request("/api/privacy/me/erasure-request"),
  getPrivacyReport: () => request("/api/privacy/report"),
  setRetention: (values) => request("/api/privacy/retention", {
    method: "PUT", body: JSON.stringify({ values }) }),
  runPrivacyPurge: () => request("/api/privacy/purge", { method: "POST" }),
  getErasureRequests: (status) => request(
    `/api/privacy/erasure-requests${status ? `?status=${status}` : ""}`),
  resolveErasureRequest: (id, status, note) => request(
    `/api/privacy/erasure-requests/${id}/resolve`, {
      method: "POST", body: JSON.stringify({ status, note }) }),
  eraseUser: (userId, mode, confirmUsername) => request(
    `/api/privacy/users/${userId}/erase`, {
      method: "POST", body: JSON.stringify({ mode, confirm_username: confirmUsername }) }),

  // --- Todos (personal list, strictly private) ---
  // 'today' is always the BROWSER's date (YYYY-MM-DD). With it the server
  // resets recurring todos in the user's time zone.
  getTodos: (today) => request(`/api/todos?today=${encodeURIComponent(today)}`),
  createTodo: (data, today) => request(`/api/todos?today=${encodeURIComponent(today)}`, {
    method: "POST", body: JSON.stringify(data) }),
  updateTodo: (id, data) => request(`/api/todos/${id}`, {
    method: "PUT", body: JSON.stringify(data) }),
  toggleTodo: (id, done, today) => request(`/api/todos/${id}/toggle`, {
    method: "POST", body: JSON.stringify({ done, today }) }),
  moveTodo: (id, categoryId, order) => request(`/api/todos/${id}/move`, {
    method: "POST", body: JSON.stringify({ category_id: categoryId, order: order || [] }) }),
  archiveTodo: (id) => request(`/api/todos/${id}/archive`, { method: "POST" }),
  unarchiveTodo: (id) => request(`/api/todos/${id}/unarchive`, { method: "POST" }),
  archiveDoneTodos: () => request("/api/todos/archive-done", { method: "POST" }),
  deleteTodo: (id) => request(`/api/todos/${id}`, { method: "DELETE" }),
  createTodoCategory: (data) => request("/api/todos/categories", {
    method: "POST", body: JSON.stringify(data) }),
  updateTodoCategory: (id, data) => request(`/api/todos/categories/${id}`, {
    method: "PUT", body: JSON.stringify(data) }),
  deleteTodoCategory: (id) => request(`/api/todos/categories/${id}`, { method: "DELETE" }),
  reorderTodoCategories: (ids) => request("/api/todos/categories/order", {
    method: "PUT", body: JSON.stringify({ ids }) }),

  scanNetwork: (subnet) => request(`/api/network/scan${subnet ? `?subnet=${encodeURIComponent(subnet)}` : ""}`),
  // Job-based scan (large nets like 10.10 = /16, with progress + speed-up)
  scanPreview: (target) => request(`/api/network/scan/preview${target ? `?target=${encodeURIComponent(target)}` : ""}`),
  scanStart: (target, speed, via = "") => request(
    `/api/network/scan/start?speed=${encodeURIComponent(speed || "normal")}${target ? `&target=${encodeURIComponent(target)}` : ""}${via ? `&via=${encodeURIComponent(via)}` : ""}`,
    { method: "POST" }),
  scanJob: (jobId) => request(`/api/network/scan/job/${jobId}`),
  scanCancel: (jobId) => request(`/api/network/scan/job/${jobId}/cancel`, { method: "POST" }),
  lastScan: () => request("/api/network/scan/last"),
  portScan: (ip, mode = "standard", ports = "", via = "") => {
    const q = new URLSearchParams({ ip, mode });
    if (mode === "custom" && ports) q.set("ports", ports);
    if (via) q.set("via", via);
    return request(`/api/network/portscan?${q.toString()}`);
  },

  // Apache Guacamole (extern gehostet)
  guacStatus: () => request("/api/guac/status"),
  createGuacToken: (protocol, params, clientId, hostname, record = true) =>
    request("/api/guac/token", { method: "POST", body: JSON.stringify({ protocol, params, client_id: clientId, hostname, record }) }),
  getGuacProfile: (clientId) => request(`/api/guac/profile/${clientId}`),
  saveGuacProfile: (clientId, profile) =>
    request(`/api/guac/profile/${clientId}`, { method: "PUT", body: JSON.stringify(profile) }),
  // Several stored logins per client (WITH password)
  listGuacProfiles: (clientId) => request(`/api/guac/profiles/${clientId}`),
  addGuacProfile: (clientId, profile) =>
    request(`/api/guac/profiles/${clientId}`, { method: "POST", body: JSON.stringify(profile) }),
  deleteGuacProfile: (clientId, profileId) =>
    request(`/api/guac/profiles/${clientId}/${profileId}`, { method: "DELETE" }),

  // --- User management ---
  getUsers: () => request("/api/users"),
  createUser: (data) => request("/api/users", { method: "POST", body: JSON.stringify(data) }),
  deleteUser: (id) => request(`/api/users/${id}`, { method: "DELETE" }),

  // --- Onboarding ---
  createEnrollmentToken: (tenant_id, location_id, client_name) =>
    request("/api/enrollment/tokens", { method: "POST", body: JSON.stringify({ tenant_id, location_id, client_name }) }),
  // List or build ready installer packages (.exe/.msi/.deb/.rpm/.run)
  listInstallers: () => request("/api/enrollment/installers"),
  buildInstallers: (targets = "auto", onJob) =>
    jobRequest("/api/enrollment/installers/build", { method: "POST", body: JSON.stringify({ targets }), onJob }),

  // --- Audit log ---
  getAuditLog: () => request("/api/audit"),
  logError: (message, level = "error", context = null, action = null) =>
    request("/api/audit/log-error", { method: "POST", body: JSON.stringify({ message, level, context, action }) }),

  // --- Session recordings ---
  getRecordings: () => request("/api/recordings"),
  // Organization-wide default layouts (dashboard widgets & client panels)
  getDefaultLayouts: () => request("/api/admin/default-layouts"),
  setDefaultLayout: (kind, layout) =>
    request("/api/admin/default-layouts", { method: "POST", body: JSON.stringify({ kind, layout }) }),
  clearDefaultLayout: (kind) =>
    request(`/api/admin/default-layouts/${kind}`, { method: "DELETE" }),
  getRecordingFrames: (id) => request(`/api/recordings/${id}/frames`),
  deleteRecording: (id) => request(`/api/recordings/${id}`, { method: "DELETE" }),

  // --- Agent management ---
  // Agent update as a backend job; waits for the agent's response (onJob: progress)
  updateAgent: (clientId, onJob) => jobRequest(`/api/clients/${clientId}/update-agent`, { method: "POST", onJob }),
  updateAgentStatus: (clientId) => request(`/api/clients/${clientId}/update-agent/status`),
  updateAllAgents: (opts = {}) => request("/api/clients/update-all-agents?background=1", { noWait: true,
    method: "POST", body: JSON.stringify({ include_offline: !!opts.include_offline }) }),
  uninstallAgent: (clientId, onJob) => jobRequest(`/api/clients/${clientId}/uninstall-agent`, { method: "POST", onJob }),

  // --- Effective permissions of the logged-in user (frontend gating) ---
  getEffectivePermissions: () => request("/api/auth/effective"),

  // --- Fine-grained permission grants (permissions app) ---
  getPermissionCatalog: () => request("/api/admin/permission-catalog"),
  getGrants: (subjectType, subjectId) => request(`/api/admin/grants/${subjectType}/${subjectId}`),
  setGrants: (subjectType, subjectId, grants) =>
    request(`/api/admin/grants/${subjectType}/${subjectId}`, { method: "PUT", body: JSON.stringify({ grants }) }),

  // --- Restart backend (admin) ---
  restartBackend: () => request("/api/admin/restart", { method: "POST" }),
  stopBackend: () => request("/api/admin/stop", { method: "POST" }),

  // --- Latest shipped agent version (for the "outdated" hint) ---
  getAgentVersion: () => request("/api/agent/version"),

  // --- Source tab (admin): explorer + database ---
  sourceRoots: () => request("/api/source/roots"),
  // Installation kind: Docker container or native program
  sourceRuntime: () => request("/api/source/runtime"),
  // Relay: FTP access (shares the port with the dashboard)
  // --- Maintenance mode / diagnostics ---
  diagStatus: () => request("/api/diag/status"),
  diagEnable: (minutes, reason, includeAgents = true) => request("/api/diag/enable", {
    method: "POST",
    body: JSON.stringify({ minutes, reason: reason || "", include_agents: !!includeAgents }),
  }),
  diagDisable: () => request("/api/diag/disable", { method: "POST" }),
  // Returns plain text - request() automatically returns text for non-JSON.
  diagTail: (lines = 300) => request(`/api/diag/tail?lines=${lines}`),
  diagClear: () => request("/api/diag/clear", { method: "POST" }),
  diagBundleUrl: () => `${BACKEND_URL}/api/diag/bundle`,

  // --- Nodes (a separate process on the client, see feature/node.js) ---
  listNodes: () => request("/api/nodes"),
  nodeState: (clientId) => request(`/api/nodes/${encodeURIComponent(clientId)}`, { timeoutMs: 5000 }),
  nodeEnable: (clientId, onJob) => jobRequest(`/api/nodes/${encodeURIComponent(clientId)}/enable`, { method: "POST", onJob }),
  nodeDisable: (clientId, onJob) => jobRequest(`/api/nodes/${encodeURIComponent(clientId)}/disable`, { method: "POST", onJob }),
  nodeUpdate: (clientId, onJob) => jobRequest(`/api/nodes/${encodeURIComponent(clientId)}/update`, { method: "POST", onJob }),
  nodeRefresh: (clientId) => request(`/api/nodes/${encodeURIComponent(clientId)}/refresh`, { method: "POST" }),
  nodeDockerInstall: (clientId, onJob) => jobRequest(`/api/nodes/${encodeURIComponent(clientId)}/docker-install`, { method: "POST", onJob }),
  nodeLogs: (clientId) => request(`/api/nodes/${encodeURIComponent(clientId)}/logs`),
  nodeModules: () => request("/api/nodes/modules"),
  nodeChoices: (moduleId) => request(`/api/nodes/choices/${encodeURIComponent(moduleId)}`),
  nodeDeploy: (clientId, moduleId, params = {}, onJob) =>
    jobRequest(`/api/nodes/${encodeURIComponent(clientId)}/modules/${encodeURIComponent(moduleId)}`,
      { method: "POST", body: JSON.stringify({ params }), onJob }),
  nodeRemoveModule: (clientId, moduleId, onJob) =>
    jobRequest(`/api/nodes/${encodeURIComponent(clientId)}/modules/${encodeURIComponent(moduleId)}`, { method: "DELETE", onJob }),
  nodeModuleAction: (clientId, moduleId, action, data = {}, onJob) =>
    jobRequest(`/api/nodes/${encodeURIComponent(clientId)}/modules/${encodeURIComponent(moduleId)}/action`,
      { method: "POST", body: JSON.stringify({ action, data }), onJob }),

  // --- Port forwarding (without VPN) ---
  listForwards: () => request("/api/portforward"),
  createForward: (clientId, targetPort, opts = {}) => request("/api/portforward", {
    method: "POST",
    body: JSON.stringify({
      client_id: clientId, target_port: targetPort,
      target_host: opts.targetHost || "127.0.0.1",
      minutes: opts.minutes ?? 240, label: opts.label || "",
      allow_from: opts.allowFrom || "",
    }),
  }),
  deleteForward: (id) => request(`/api/portforward/${encodeURIComponent(id)}`,
    { method: "DELETE" }),

  vpnNetwork: () => request("/api/vpn/network"),
  vpnTunnels: (clientId) => request("/api/vpn/tunnels"
    + (clientId ? `?client_id=${encodeURIComponent(clientId)}` : "")),
  vpnCreateTunnel: (clientId, minutes, name, routes, opts = {}) => request("/api/vpn/tunnels", {
    method: "POST",
    body: JSON.stringify({
      client_id: clientId, minutes, name: name || "", routes: routes || "",
      // 'peer' = only this device, 'site' = the whole network behind it,
      // 'net' = virtual network via the backend
      mode: opts.mode || "peer",
    }),
  }),
  vpnRevokeTunnel: (tunnelId) =>
    request(`/api/vpn/tunnels/${encodeURIComponent(tunnelId)}/revoke`, { method: "POST" }),

  relayFtpConfig: () => request("/api/relay/ftp"),

  // --- Server-owned relay folders (Spaces / Deployment / Source) ---
  storageSections: () => request("/api/storage/sections"),
  storageList: (section, path = "") => request(
    `/api/storage/list?section=${encodeURIComponent(section)}&path=${encodeURIComponent(path)}`),
  storageDownloadUrl: (section, path) =>
    `/api/storage/download?section=${encodeURIComponent(section)}&path=${encodeURIComponent(path)}`,
  // File upload (multipart, hence not via request()).
  storageUpload: async (section, path, file) => {
    const token = localStorage.getItem("rmm_token");
    const fd = new FormData();
    fd.append("file", file, file.name);
    const q = `section=${encodeURIComponent(section)}&path=${encodeURIComponent(path || "")}`;
    const res = await fetch(`/api/storage/upload?${q}`, {
      method: "POST", headers: { Authorization: `Bearer ${token}` }, body: fd });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail
      || `Upload failed (${res.status})`);
    return res.json();
  },
  storageMkdir: (section, path) => request("/api/storage/mkdir", {    method: "POST", body: JSON.stringify({ section, path }) }),
  storageDelete: (section, path) => request("/api/storage/delete", {
    method: "POST", body: JSON.stringify({ section, path }) }),
  storageMove: (section, path, dst) => request("/api/storage/move", {
    method: "POST", body: JSON.stringify({ section, path, dst }) }),
  getDeployment: () => request("/api/storage/deployment"),  saveDeployment: (body) => request("/api/storage/deployment", {
    method: "POST", body: JSON.stringify(body) }),
  relayFtpMode: (mode, webdav) => request("/api/relay/ftp", {
    method: "POST",
    body: JSON.stringify(webdav === undefined ? { mode } : { mode, webdav }) }),
  // --- Container services (Docker mode only) ---
  dockerServices: () => request("/api/admin/docker/services"),
  dockerEnable: (key, onJob) => jobRequest(`/api/admin/docker/services/${key}/enable`, { method: "POST", onJob }),
  dockerDisable: (key, onJob) => jobRequest(`/api/admin/docker/services/${key}/disable`, { method: "POST", onJob }),
  dockerDbCredentials: () => request("/api/admin/docker/db-credentials"),
  // Clear dist/ (built agent installer packages)
  sourceClearDist: () => request("/api/source/dist/clear", { method: "POST" }),
  // --- Migration: move an entire instance ---
  migrateInfo: () => request("/api/source/migrate/info"),
  // Server move: send agents to the new backend
  migrateRedirectState: () => request("/api/source/migrate/redirect"),
  migrateRedirectTest: (url) => request("/api/source/migrate/redirect/test", { method: "POST", body: JSON.stringify({ url }) }),
  migrateRedirectStart: (url, agent_token) => request("/api/source/migrate/redirect?background=1", { method: "POST", noWait: true, body: JSON.stringify({ url, agent_token }) }),
  migrateRedirectStop: () => request("/api/source/migrate/redirect", { method: "DELETE" }),
  migrateExportUrl: (opts = {}) => {
    const q = new URLSearchParams({
      recordings: opts.recordings !== false,
      media: opts.media !== false,
      branding: opts.branding !== false,
      secrets: !!opts.secrets,
      modules: opts.modules !== false,
      storage: opts.storage !== false,
      token: localStorage.getItem("rmm_token") || "",
    });
    return `/api/source/migrate/export?${q}`;
  },
  migrateImport: (file, opts = {}, onProgress) => new Promise((resolve, reject) => {
    // XMLHttpRequest instead of fetch: only this gives a progress bar,
    // and the archive can well be several gigabytes.
    const q = new URLSearchParams({ secrets: !!opts.secrets, backup: opts.backup !== false });
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/source/migrate/import?${q}`);
    xhr.setRequestHeader("Authorization", `Bearer ${localStorage.getItem("rmm_token") || ""}`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
    xhr.onload = () => {
      let body = {};
      try { body = JSON.parse(xhr.responseText || "{}"); } catch {}
      if (xhr.status >= 200 && xhr.status < 300) resolve(body);
      else reject(new Error(body.detail || `${t("error")} ${xhr.status}`));
    };
    xhr.onerror = () => reject(new Error(t("api_conn_lost")));
    xhr.send(file);
  }),
  sourceList: (path = "") => request(`/api/source/list?path=${encodeURIComponent(path)}`),
  sourceRead: (path) => request(`/api/source/read?path=${encodeURIComponent(path)}`),
  sourceWrite: (path, content) =>
    request("/api/source/write", { method: "PUT", body: JSON.stringify({ path, content }) }),
  sourceDbTables: () => request("/api/source/db/tables"),
  sourceDbTable: (name, limit = 200, offset = 0) =>
    request(`/api/source/db/table?name=${encodeURIComponent(name)}&limit=${limit}&offset=${offset}`),
  sourceDbQuery: (sql) =>
    request("/api/source/db/query", { method: "POST", body: JSON.stringify({ sql }) }),
  // Explorer: create/delete/rename folders/files
  sourceMkdir: (path) => request("/api/source/mkdir", { method: "POST", body: JSON.stringify({ path }) }),
  sourceNewFile: (path) => request("/api/source/newfile", { method: "POST", body: JSON.stringify({ path }) }),
  sourceDelete: (path) => request("/api/source/delete", { method: "POST", body: JSON.stringify({ path }) }),
  sourceRename: (src, dst) => request("/api/source/rename", { method: "POST", body: JSON.stringify({ src, dst }) }),
  // Database: edit / delete / create / backup
  sourceDbSetCell: (table, rowid, column, value) =>
    request("/api/source/db/cell", { method: "PUT", body: JSON.stringify({ table, rowid, column, value }) }),
  sourceDbDeleteRow: (table, rowid) =>
    request("/api/source/db/delete-row", { method: "POST", body: JSON.stringify({ table, rowid }) }),
  sourceDbInsertRow: (table, values = {}) =>
    request("/api/source/db/insert-row", { method: "POST", body: JSON.stringify({ table, values }) }),
  sourceDbDropTable: (table) =>
    request("/api/source/db/drop-table", { method: "POST", body: JSON.stringify({ table }) }),
  sourceDbCreateTable: (name, columns) =>
    request("/api/source/db/create-table", { method: "POST", body: JSON.stringify({ name, columns }) }),
  sourceDbBackup: () => jobRequest("/api/source/db/backup", { method: "POST" }),
  // Upload + extract a ZIP (multipart; NOT via request() because of FormData).
  sourceUploadZip: async (fileObj, path = "") => {
    const fd = new FormData();
    fd.append("file", fileObj);
    fd.append("path", path || "");
    const res = await fetch(`${BACKEND_URL}/api/source/upload-zip`, {
      method: "POST",
      headers: { Authorization: `Bearer ${getToken()}` },   // NO Content-Type (the browser sets the boundary)
      body: fd,
    });
    if (!res.ok) {
      let msg = res.statusText;
      try { msg = (await res.json()).detail || msg; } catch {}
      throw new Error(msg);
    }
    return res.json();
  },

  // --- Load / import AD groups from a realm (admin) ---
  getRealmAdGroups: (realmId) => request(`/api/admin/realms/${realmId}/ad-groups`),
  importRealmAdGroups: (realmId, names = []) =>
    request(`/api/admin/realms/${realmId}/ad-groups/import`, { method: "POST", body: JSON.stringify({ names }) }),

  // --- Branding (replace logos by upload) ---
  getBranding: () => request("/api/admin/branding"),
  // Raw upload (NO multipart, NO JSON header) - the backend reads the body directly.
  uploadBranding: async (name, file) => {
    const token = getToken();
    const res = await fetch(`${BACKEND_URL}/api/admin/branding/${encodeURIComponent(name)}`, {
      method: "POST",
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      body: file,
    });
    if (!res.ok) {
      let msg = "Upload failed";
      try { const j = await res.json(); msg = j.detail || msg; } catch {}
      throw new Error(msg);
    }
    return res.json();
  },

  // --- Versions (backend + agent, basis for auto-update) ---
  getVersions: () => request("/api/version"),

  // Returns the URL to the .rdp file (the download opens the native RDP client)
  rdpFileUrl: (clientId) => `/api/clients/${clientId}/rdp-file`,
  // Loads the .rdp file (with auth) and triggers the download in the browser
  downloadRdpFile: async (clientId, hostname) => {
    const token = getToken();
    const res = await fetch(`/api/clients/${clientId}/rdp-file`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!res.ok) {
      let msg = t("u_rdp_datei_konnte_nicht_erstellt_we");
      try { const j = await res.json(); msg = j.detail || msg; } catch {}
      throw new Error(msg);
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${hostname || "client"}.rdp`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  },

  // --- Scripts ---
  getScripts: () => request("/api/scripts"),
  createScript: (data) => request("/api/scripts", { method: "POST", body: JSON.stringify(data) }),
  updateScript: (id, data) => request(`/api/scripts/${id}`, { method: "PUT", body: JSON.stringify(data) }),
  deleteScript: (id) => request(`/api/scripts/${id}`, { method: "DELETE" }),

  // --- Permissions / groups ---
  getSettings: () => request("/api/admin/settings"),  updateSettings: (data) => request("/api/admin/settings", { method: "PUT", body: JSON.stringify(data) }),
  getServerUpdateInfo: () => request("/api/admin/update/info"),
  setServerUpdateRepo: (url) => request("/api/admin/update/repo", { method: "PUT", body: JSON.stringify({ url }) }),
  // Server update as a backend job: immediate response, progress in the task tray
  // (the backend restarts at the end - so do not wait for the result).
  runServerUpdate: (target, tag) => request("/api/admin/update/run?background=1", { method: "POST", noWait: true, body: JSON.stringify({ target, tag: tag || null }) }),
  getDatabaseInfo: () => request("/api/admin/database/info"),
  testDatabase: (cfg) => request("/api/admin/database/test", { method: "POST", body: JSON.stringify(cfg) }),
  // The switch runs as a backend job; progress comes via databaseProgress()/task tray
  switchDatabase: (cfg) => request("/api/admin/database/switch", { method: "POST", noWait: true, body: JSON.stringify(cfg) }),
  databaseProgress: () => request("/api/admin/database/progress"),
  databaseDocker: () => request("/api/admin/database/docker"),     // module "sql"
  databaseBackups: () => request("/api/admin/database/backups"),
  databaseBackup: () => request("/api/admin/database/backup", { method: "POST", body: "{}" }),
  databaseRestoreBackup: (path) => request("/api/admin/database/restore-backup", {
    method: "POST", body: JSON.stringify({ path }) }),

  getPermissions: () => request("/api/admin/permissions"),
  getGroups: () => request("/api/admin/groups"),
  createGroup: (data) => request("/api/admin/groups", { method: "POST", body: JSON.stringify(data) }),
  updateGroup: (id, data) => request(`/api/admin/groups/${id}`, { method: "PUT", body: JSON.stringify(data) }),
  deleteGroup: (id) => request(`/api/admin/groups/${id}`, { method: "DELETE" }),
  setGroupUnmanaged: (id, unmanaged) => request(`/api/admin/groups/${id}/unmanaged`, { method: "PUT", body: JSON.stringify({ unmanaged }) }),
  getUserGroups: (uid) => request(`/api/admin/users/${uid}/groups`),
  setUserGroups: (uid, group_ids) => request(`/api/admin/users/${uid}/groups`, { method: "PUT", body: JSON.stringify({ group_ids }) }),

  // --- Realms (directory / AD) ---
  // --- SSO / realms: module "sso" (routes only when the module is active) ---
  getRealms: () => request("/api/admin/realms"),
  ssoLocalAdmins: () => request("/api/admin/sso/local-admins"),
  createRealm: (data) => request("/api/admin/realms", { method: "POST", body: JSON.stringify(data) }),
  updateRealm: (id, data) => request(`/api/admin/realms/${id}`, { method: "PUT", body: JSON.stringify(data) }),
  testRealm: (id) => request(`/api/admin/realms/${id}/test`, { method: "POST" }),
  deleteRealm: (id) => request(`/api/admin/realms/${id}`, { method: "DELETE" }),

  // --- Webhooks / notifications ---
  getWebhooks: () => request("/api/admin/webhooks"),
  createWebhook: (data) => request("/api/admin/webhooks", { method: "POST", body: JSON.stringify(data) }),
  updateWebhook: (id, data) => request(`/api/admin/webhooks/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
  testWebhook: (id) => request(`/api/admin/webhooks/${id}/test`, { method: "POST" }),
  deleteWebhook: (id) => request(`/api/admin/webhooks/${id}`, { method: "DELETE" }),

  // --- Notification rules + SMTP (notification rework) ---
  getNotifyCatalog: () => request("/api/notify/catalog"),
  getNotifyRules: () => request("/api/notify/rules"),
  createNotifyRule: (data) => request("/api/notify/rules", { method: "POST", body: JSON.stringify(data) }),
  updateNotifyRule: (id, data) => request(`/api/notify/rules/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
  deleteNotifyRule: (id) => request(`/api/notify/rules/${id}`, { method: "DELETE" }),
  testNotifyRule: (id) => request(`/api/notify/rules/${id}/test`, { method: "POST" }),
  getSmtp: () => request("/api/notify/smtp"),
  setSmtp: (data) => request("/api/notify/smtp", { method: "POST", body: JSON.stringify(data) }),
  testSmtp: (to) => request("/api/notify/smtp/test", { method: "POST", body: JSON.stringify({ to }) }),

  // --- Chat (DMs + groups) ---
  chatUsers: () => request("/api/chat/users"),
  chatConversations: () => request("/api/chat/conversations"),
  chatUnread: () => request("/api/chat/unread"),
  chatCreate: (data) => request("/api/chat/conversations", { method: "POST", body: JSON.stringify(data) }),
  chatMessages: (id, before) => request(`/api/chat/conversations/${id}/messages${before ? `?before=${before}` : ""}`),
  chatSend: (id, text) => request(`/api/chat/conversations/${id}/messages`, { method: "POST", body: JSON.stringify({ text }) }),
  chatRead: (id) => request(`/api/chat/conversations/${id}/read`, { method: "POST" }),
  chatRename: (id, name) => request(`/api/chat/conversations/${id}`, { method: "PATCH", body: JSON.stringify({ name }) }),
  chatAddMember: (id, userId) => request(`/api/chat/conversations/${id}/members`, { method: "POST", body: JSON.stringify({ user_id: userId }) }),
  chatRemoveMember: (id, userId) => request(`/api/chat/conversations/${id}/members/${userId}`, { method: "DELETE" }),
  chatSetAdmin: (id, userId, isAdmin) => request(`/api/chat/conversations/${id}/admins`, { method: "POST", body: JSON.stringify({ user_id: userId, is_admin: isAdmin }) }),
  chatDelete: (id) => request(`/api/chat/conversations/${id}`, { method: "DELETE" }),

  // --- Automations ---
  getAutomations: () => request("/api/admin/automations"),
  createAutomation: (data) => request("/api/admin/automations", { method: "POST", body: JSON.stringify(data) }),
  toggleAutomation: (id) => request(`/api/admin/automations/${id}/toggle`, { method: "POST" }),
  deleteAutomation: (id) => request(`/api/admin/automations/${id}`, { method: "DELETE" }),
  getAutomationRuns: (id) => request(`/api/admin/automations/${id}/runs`),
};

export function saveToken(token) {
  localStorage.setItem("rmm_token", token);
}

export function clearToken() {
  localStorage.removeItem("rmm_token");
}
