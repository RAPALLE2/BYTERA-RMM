// init.js (Modul "software") - Panel-Aktion "Software installieren" am Client.
import { registerClientAction } from "/js/app/modules.js";
import { openWindow } from "/js/app/windowmanager.js";

export function init() {
  registerClientAction("software", {
    label: () => "📦 Software",
    onClick: (client) => {
      openWindow({
        singleton: true,
        key: "software",
        appId: "software",
        title: "📦 Software-Rollout",
        props: { preselect: client?.id },
        w: 880, h: 620,
      });
    },
  });
}
