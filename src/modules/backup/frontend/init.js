// init.js (Modul "backup") - Panel-Aktion "Backup" am Client.
import { registerClientAction } from "/js/app/modules.js";
import { openWindow } from "/js/app/windowmanager.js";

export function init() {
  registerClientAction("backup", {
    label: () => "💾 Backup",
    onClick: (client) => {
      openWindow({
        key: `backup-${client?.id || ""}`,
        appId: "backup",
        title: `💾 Backup - ${client?.short_name || client?.hostname || ""}`,
        props: { clientId: client?.id, tab: "backups" },
        w: 1000, h: 680,
      });
    },
  });
}
