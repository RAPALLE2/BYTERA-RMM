// init.js (module "proxmox") - button in the agent panel + suggestions
//   guest (agent runs in a Proxmox VM/CT and is mapped)      -> "Open in Proxmox" (dashboard at the guest)
//   physical device with Proxmox + permission "view dashboard" -> "Proxmox"
//   physical device without Proxmox + permission "manage Proxmox" -> "Connect Proxmox"
//   VM/CT without mapping                                      -> no button (Proxmox runs on hosts)
// Suggestions (agents that, per Proxmox, run in VMs/CTs) appear as a hint in the bottom right.
import { registerClientAction, registerModuleApi } from "/js/app/modules.js";
import { renderMainContent } from "/js/app/panel.js";
import { hasClientPerm, state } from "/js/app/state.js";
import { openWindow } from "/js/app/windowmanager.js";
import { preq } from "./common.js";
import { openConnectionDialog } from "./connect.js";
import "./console.js";      // console buttons in the guest view (event "rmm-pve-guest")
import { maybeShowBanner, openSuggestDialog } from "./suggest.js";

let connected = new Set();
let guestMap = {};
let suggestions = [];
let candidates = [];

async function refresh() {
  try { connected = new Set((await preq("/clients")).clients.map((c) => c.client_id)); } catch { /* module off / no access */ }
  try {
    const g = await preq("/guest-map");
    guestMap = g.map || {};
    suggestions = g.suggestions || [];
    candidates = g.candidates || [];
    maybeShowBanner(suggestions, refresh, { candidates });
  } catch { /* ignore */ }
}

export function openDashboard(client, guest = null) {
  openWindow({ key: `proxmox-${client.id}`, appId: "proxmox", title: `Proxmox — ${client.hostname || ""}`,
               props: { clientId: client.id, guest }, w: 1000, h: 680, clientColor: client.color });
}

function hostOf(entry) {
  return (state.clients || []).find((c) => c.id === entry.host) || { id: entry.host, hostname: entry.host_name };
}

export function init() {
  window.RMM_PVE_REFRESH = refresh;
  refresh();
  setInterval(refresh, 60000);
  window.addEventListener("rmm-client-updated", (e) => {
    const c = (state.clients || []).find((x) => x.id === e.detail?.id);
    if (c) Object.assign(c, e.detail);
    refresh().then(() => { try { renderMainContent(); } catch { /* ignore */ } });
  });
  registerModuleApi("proxmox", { openDashboard, refresh, isConnected: (id) => connected.has(id),
                                 guestOf: (id) => guestMap[id] || null,
                                 openSuggestions: () => openSuggestDialog(suggestions, refresh, { candidates }) });
  registerClientAction("proxmox", {
    label(client) {
      const g = guestMap[client.id];
      if (g && g.can_open) return "Open in Proxmox";
      if ((client.device_type || "physical") !== "physical") return null;
      if (connected.has(client.id) && hasClientPerm(client.id, "pve_access")) return "Proxmox";
      if (!connected.has(client.id) && hasClientPerm(client.id, "pve_manage")) return "Connect Proxmox";
      return null;
    },
    onClick(client) {
      const g = guestMap[client.id];
      if (g && g.can_open) return openDashboard(hostOf(g), { node: g.node, type: g.type, vmid: g.vmid, name: g.name });
      if (connected.has(client.id)) return openDashboard(client);
      openConnectionDialog(client, async (changed) => {
        if (!changed) return;
        await refresh();
        if (connected.has(client.id)) openDashboard(client);
      });
    },
  });
}
