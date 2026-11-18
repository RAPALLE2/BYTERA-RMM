// init.js (Modul "passwords") - Knopf "Passwörter" in den Client-Aktionen
// ----------------------------------------------------------------------
// Öffnet den Passwort-Manager, vorgefiltert auf den Client (Einträge, die per
// ip/client/service mit ihm verknüpft sind). Autofill-Haken: andere Module
// können window.RMM_PM_FILL(entry) setzen, um Zugangsdaten in ihre Dialoge /
// den internen Browser einzusetzen; der Manager ruft diesen Haken beim „⤵“.
import { registerClientAction, registerModuleApi } from "/js/app/modules.js";
import { hasGlobalPerm, isAdmin } from "/js/app/state.js";
import { openWindow } from "/js/app/windowmanager.js";

function open(client) {
  openWindow({ key: "passwords", appId: "passwords", title: "Passwörter",
              props: { clientId: client ? client.id : null }, w: 1100, h: 740, clientColor: client && client.color });
}

export function init() {
  registerModuleApi("passwords", { open });
  registerClientAction("passwords", {
    label(client) {
      if (!(hasGlobalPerm("use_passwords") || isAdmin())) return null;
      return "🔑 Passwörter";
    },
    onClick(client) { open(client); },
  });
}
