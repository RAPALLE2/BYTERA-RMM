// tickettarget.js
// ---------------
// A ticket can refer to a client - or to the RMM itself ("Main RMM"). For the
// second case there is no real client, hence this fixed identifier. It is
// stored like a client ID; the backend puts it unchanged into ticket_clients
// and needs no special cases for it.

import { state } from "./state.js";

export const RMM_TARGET = "__rmm__";
export const RMM_LABEL = "Main RMM (server)";

export function isRmmTarget(id) {
  return id === RMM_TARGET;
}

// Display name of a ticket target - whether a client or the RMM itself.
export function targetLabel(id) {
  if (isRmmTarget(id)) return `🏢 ${RMM_LABEL}`;
  const c = state.clients.find((x) => x.id === id);
  return c ? `🖥️ ${c.hostname}` : `🖥️ ${String(id).slice(0, 8)}...`;
}
