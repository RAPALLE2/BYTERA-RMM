// init.js (module "passwords") - "Passwords" button in the client actions.
// ------------------------------------------------------------------------
// Opens the password manager pre-filtered to the client (entries linked to it
// by ip/client/service). Autofill hook: other modules can set
// window.RMM_PM_FILL(entry) to insert credentials into their dialogs / the
// built-in browser; the manager calls this hook on the "insert" action.
import { registerClientAction, registerModuleApi } from "/js/app/modules.js";
import { hasGlobalPerm, isAdmin } from "/js/app/state.js";
import { openWindow } from "/js/app/windowmanager.js";

function open(client) {
  openWindow({ key: "passwords", appId: "passwords", title: "Passwords",
              props: { clientId: client ? client.id : null }, w: 1100, h: 740, clientColor: client && client.color });
}

export function init() {
  registerModuleApi("passwords", { open });
  registerClientAction("passwords", {
    label(client) {
      if (!(hasGlobalPerm("use_passwords") || isAdmin())) return null;
      return "Passwords";
    },
    onClick(client) { open(client); },
  });
}
