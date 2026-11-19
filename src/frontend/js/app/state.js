// state.js
// --------
// Instead of a large frontend framework (React/Vue/...) we use a simple,
// central JavaScript object as the app's "data store". Other modules read/change
// this state and then call the render() functions of the affected areas. This is
// deliberately kept simple, so that as a Python developer without JS-framework
// experience you can see exactly where which data comes from.

export const state = {
  user: null,              // currently logged-in user ({username, role, ...})
  perms: null,             // effective permissions: {admin, global:{perm:bool}, clients:{id:{perm:bool}}}
  hierarchy: { tenants: [], locations: [], folders: [] },
  clients: [],             // all clients (from /api/clients)
  selection: null,         // currently selected in the sidebar: {type: "tenant"|"location"|"client", id}
  windows: [],             // currently open windows: {key, appId, title, x, y, w, h, minimized, props}
  focusOrder: [],          // window order by last focused (last = topmost)
  sidebar: { width: 260, collapsed: false },  // width + expand/collapse state (persisted)
};

export function findClient(id) {
  return state.clients.find((c) => c.id === id);
}

// -----------------------------------------------------------------
// Permission helpers for frontend gating. Source: /api/auth/effective.
// IMPORTANT: pure UI convenience - the hard check is done by the backend.
// -----------------------------------------------------------------
export function isAdmin() {
  return !!(state.perms && state.perms.admin);
}

// Global permission (e.g. "see_audit", "manage_users", "login").
export function hasGlobalPerm(perm) {
  if (isAdmin()) return true;
  return !!(state.perms && state.perms.global && state.perms.global[perm]);
}

// Client-specific permission. If the client is missing from perms.clients -> not visible.
export function hasClientPerm(clientId, perm) {
  if (isAdmin()) return true;
  const c = state.perms && state.perms.clients && state.perms.clients[clientId];
  return !!(c && c[perm]);
}

// Does the user have a client permission on AT LEAST one client?
// (e.g. "manage_nodes" -> node overview visible)
export function hasAnyClientPerm(perm) {
  if (isAdmin()) return true;
  const clients = (state.perms && state.perms.clients) || {};
  return Object.values(clients).some((c) => c && c[perm]);
}

// Is the client visible to the user at all?
export function clientVisible(clientId) {
  if (isAdmin()) return true;
  return !!(state.perms && state.perms.clients && state.perms.clients[clientId]);
}
