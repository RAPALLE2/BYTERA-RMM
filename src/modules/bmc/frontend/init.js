// init.js (module "bmc") - "External management" button in the client actions
// ---------------------------------------------------------------------------
//   client with a linked iLO/iDRAC           -> "External management"
//   physical server without a BMC + manage_bmc -> "Connect iLO/iDRAC"
// Opens the module app focused on this client. Also exposes the links
// (client_id -> BMC) as a module API (e.g. for the topology).
import { registerClientAction, registerModuleApi, moduleRequest } from "/js/app/modules.js";
import { hasGlobalPerm, isAdmin, hasClientPerm } from "/js/app/state.js";
import { openWindow } from "/js/app/windowmanager.js";

let links = {};

async function refresh() {
  try { links = (await moduleRequest("/api/bmc/links")).links || {}; }
  catch { links = {}; }
}

function openApp(client, connId = null) {
  openWindow({ key: `bmc-${client.id}`, appId: "bmc", title: `iLO / iDRAC — ${client.hostname || ""}`,
              props: { clientId: client.id, connId }, w: 1120, h: 760, clientColor: client.color });
}

export function init() {
  window.RMM_BMC_REFRESH = refresh;
  refresh();
  setInterval(refresh, 60000);
  window.addEventListener("rmm-client-updated", () => { refresh(); });

  registerModuleApi("bmc", { refresh, linkOf: (id) => links[id] || null, openApp });

  registerClientAction("bmc", {
    label(client) {
      const l = links[client.id];
      if (l) {
        if (!(hasGlobalPerm("use_bmc") || isAdmin() || hasClientPerm(client.id, "c_power"))) return null;
        const v = (l.vendor || "").includes("HPE") ? "iLO" : (l.vendor || "").includes("Dell") ? "iDRAC" : "iLO/iDRAC";
        return `External management (${v})`;
      }
      // No BMC linked: don't offer for virtual machines/containers (they have no
      // BMC of their own); otherwise only with permission to create one.
      const kind = (client.detected_kind || client.device_type || "").toLowerCase();
      if (["vm", "lxc", "container", "docker"].includes(kind)) return null;
      if (!(hasGlobalPerm("manage_bmc") || isAdmin())) return null;
      return "Connect iLO/iDRAC";
    },
    onClick(client) {
      const l = links[client.id];
      openApp(client, l ? l.id : null);
    },
  });
}
