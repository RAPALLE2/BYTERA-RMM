// init.js (Modul "bmc") - Knopf "Externes Management" in den Client-Aktionen
// -------------------------------------------------------------------------
//   Client mit verknüpfter iLO/iDRAC          -> "🛉 Externes Management"
//   physischer Server ohne BMC + manage_bmc   -> "🛉 iLO/iDRAC verbinden"
// Öffnet die Modul-App, fokussiert auf diesen Client. Stellt ausserdem die
// Verknüpfungen (client_id -> BMC) als Modul-API bereit (z.B. für die Topologie).
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
        return `🛉 Externes Management (${v})`;
      }
      // Kein BMC verknüpft: nicht bei virtuellen Maschinen/Containern anbieten
      // (die haben keine eigene BMC), sonst nur mit Recht zum Anlegen.
      const kind = (client.detected_kind || client.device_type || "").toLowerCase();
      if (["vm", "lxc", "container", "docker"].includes(kind)) return null;
      if (!(hasGlobalPerm("manage_bmc") || isAdmin())) return null;
      return "🛉 iLO/iDRAC verbinden";
    },
    onClick(client) {
      const l = links[client.id];
      openApp(client, l ? l.id : null);
    },
  });
}
