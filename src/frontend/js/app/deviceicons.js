// deviceicons.js
// --------------
// Device icons shared by the sidebar, the client panel and the topology map
// (module "topology" re-exports them), so an agent and its topology node always
// look the same. Monochrome line icons in a 48x48 viewBox, coloured via
// "currentColor". Also: battery indicator for laptops.

const S = `fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"`;
const F = `fill="currentColor" stroke="none"`;

const ICONS = {
  // Wolke (Internet)
  www: `<path ${S} d="M13 32a7 7 0 0 1 1-13.9A10 10 0 0 1 33.5 16 8 8 0 0 1 35 32H13z"/>`,

  // Provider / oeffentlicher Hop (Wolke mit Pfeil)
  isp: `<path ${S} d="M13 30a7 7 0 0 1 1-13.9A10 10 0 0 1 33.5 14 8 8 0 0 1 35 30H13z"/>
        <path ${S} d="M24 34v8m0 0-4-4m4 4 4-4"/>`,

  // Router (flache Box mit zwei Antennen + LEDs)
  router: `<rect ${S} x="7" y="26" width="34" height="12" rx="3"/>
           <path ${S} d="M15 26V12m18 14V12"/>
           <circle ${F} cx="15" cy="11" r="2"/><circle ${F} cx="33" cy="11" r="2"/>
           <circle ${F} cx="14" cy="32" r="1.6"/><circle ${F} cx="20" cy="32" r="1.6"/>
           <circle ${F} cx="26" cy="32" r="1.6"/>`,

  // Switch (flache Box mit Ports und Pfeilen)
  switch: `<rect ${S} x="6" y="20" width="36" height="12" rx="2"/>
           <path ${F} d="M10 24h3v4h-3zm5 0h3v4h-3zm5 0h3v4h-3zm5 0h3v4h-3z"/>
           <path ${S} d="M31 14h8m0 0-3-3m3 3-3 3M39 38h-8m0 0 3-3m-3 3 3 3"/>`,

  // Access Point (Punkt mit Funkwellen)
  ap: `<circle ${F} cx="24" cy="30" r="3"/>
       <path ${S} d="M16 24a11 11 0 0 1 16 0M11 18a18 18 0 0 1 26 0"/>
       <path ${S} d="M24 33v9"/>`,

  // Server (Rack-Tower)
  server: `<rect ${S} x="12" y="6" width="24" height="36" rx="2"/>
           <path ${S} d="M12 16h24M12 26h24"/>
           <circle ${F} cx="17" cy="11" r="1.6"/><circle ${F} cx="17" cy="21" r="1.6"/>
           <circle ${F} cx="17" cy="31" r="1.6"/>
           <path ${S} d="M26 36h6"/>`,

  // Hypervisor (Rack mit Wuerfel)
  hypervisor: `<rect ${S} x="10" y="14" width="20" height="28" rx="2"/>
               <path ${S} d="M10 23h20M10 32h20"/>
               <circle ${F} cx="14.5" cy="18.5" r="1.4"/><circle ${F} cx="14.5" cy="27.5" r="1.4"/>
               <path ${S} d="M30 12l6-4 6 4v7l-6 4-6-4zM30 12l6 4 6-4M36 16v7"/>`,

  // NAS / Speicher (Box mit Platten-Einschueben)
  nas: `<rect ${S} x="10" y="10" width="28" height="30" rx="2"/>
        <path ${S} d="M10 18h28M10 26h28M10 34h28"/>
        <circle ${F} cx="33" cy="14" r="1.5"/><circle ${F} cx="33" cy="22" r="1.5"/>
        <circle ${F} cx="33" cy="30" r="1.5"/>`,

  // PC (Monitor + Tower)
  pc: `<rect ${S} x="5" y="10" width="26" height="18" rx="2"/>
       <path ${S} d="M14 34h8m-4-6v6"/>
       <rect ${S} x="35" y="8" width="9" height="30" rx="1.5"/>
       <circle ${F} cx="39.5" cy="33" r="1.4"/>`,

  // Laptop
  laptop: `<rect ${S} x="10" y="10" width="28" height="18" rx="2"/>
           <path ${S} d="M6 36h36l-4-8H10z"/>`,

  // Drucker
  printer: `<path ${S} d="M14 18v-8h20v8"/>
            <rect ${S} x="8" y="18" width="32" height="14" rx="2"/>
            <rect ${S} x="14" y="28" width="20" height="12"/>
            <circle ${F} cx="35" cy="23" r="1.6"/>`,

  // Smartphone
  phone: `<rect ${S} x="15" y="6" width="18" height="36" rx="3"/>
          <path ${S} d="M22 37h4"/>`,

  // TV / Media
  tv: `<rect ${S} x="6" y="10" width="36" height="22" rx="2"/>
       <path ${S} d="M16 40h16m-8-8v8"/>`,

  // Kamera
  camera: `<rect ${S} x="6" y="14" width="24" height="16" rx="3"/>
           <path ${S} d="M30 19l12-5v16l-12-5zM12 30v8"/>
           <circle ${S} cx="16" cy="22" r="3"/>`,

  // IoT (Chip)
  iot: `<rect ${S} x="14" y="14" width="20" height="20" rx="3"/>
        <circle ${S} cx="24" cy="24" r="4"/>
        <path ${S} d="M19 14V7m10 7V7M19 41v-7m10 7v-7M14 19H7m7 10H7m34-10h-7m7 10h-7"/>`,

  // SAN/Backup (Platten-Stapel)
  storage: `<ellipse ${S} cx="24" cy="12" rx="14" ry="5"/>
            <path ${S} d="M10 12v24c0 2.8 6.3 5 14 5s14-2.2 14-5V12"/>
            <path ${S} d="M10 24c0 2.8 6.3 5 14 5s14-2.2 14-5"/>`,

  // Firewall (Mauer + Schild)
  firewall: `<rect ${S} x="6" y="12" width="26" height="24" rx="2"/>
             <path ${S} d="M6 20h26M6 28h26M15 12v8m8 0v8m-8 0v8"/>
             <path ${S} d="M34 22l8-3v8c0 6-4 9-8 11z"/>`,

  // Management (Konsole mit Zahnrad)
  management: `<rect ${S} x="5" y="8" width="30" height="22" rx="2"/>
               <path ${S} d="M14 38h12m-6-8v8M10 15l5 4-5 4M18 23h7"/>
               <circle ${S} cx="38" cy="32" r="4"/>
               <path ${S} d="M38 25v3m0 8v3m7-7h-3m-8 0h-3m11.9-4.9-2.1 2.1m-5.6 5.6-2.1 2.1m9.8 0-2.1-2.1m-5.6-5.6-2.1-2.1"/>`,

  // Controller (WLAN-/SDN-Controller: Box mit Verteilung nach unten)
  controller: `<rect ${S} x="12" y="6" width="24" height="14" rx="2"/>
               <circle ${F} cx="18" cy="13" r="1.6"/><path ${S} d="M23 13h8"/>
               <path ${S} d="M24 20v6M10 26h28M10 26v5m14-5v5m14-5v5"/>
               <rect ${S} x="5" y="31" width="10" height="9" rx="1.5"/>
               <rect ${S} x="19" y="31" width="10" height="9" rx="1.5"/>
               <rect ${S} x="33" y="31" width="10" height="9" rx="1.5"/>`,

  // Subnetz (Netzwerk-Wolke aus Punkten)
  subnet: `<circle ${S} cx="24" cy="24" r="16" stroke-dasharray="4 3"/>
           <circle ${F} cx="24" cy="14" r="2.6"/><circle ${F} cx="15" cy="28" r="2.6"/>
           <circle ${F} cx="33" cy="28" r="2.6"/>
           <path ${S} d="M24 14l-9 14h18z"/>`,

  // LXC-Container (Box mit Linux-Kaestchen)
  lxc: `<path ${S} d="M24 6l16 8v20l-16 8-16-8V14z"/>
        <path ${S} d="M8 14l16 8 16-8M24 22v20"/>
        <path ${S} d="M13 25v6l5 2.5"/>`,

  // Virtuelle Maschine (Monitor mit Wuerfel)
  vm: `<rect ${S} x="5" y="8" width="38" height="26" rx="2"/>
       <path ${S} d="M17 42h14m-7-8v8"/>
       <path ${S} d="M24 13l8 4v8l-8 4-8-4v-8zM16 17l8 4 8-4M24 21v8"/>`,

  // Docker-Container (Container-Stapel auf dem Wal)
  docker: `<path ${S} d="M5 26h34c3 0 4-3 4-3s-2-2-5-1c0-3-2-5-2-5s-3 2-2 6c-1 0-3 0-29 3z"/>
           <path ${S} d="M5 26c1 7 7 12 17 12 9 0 16-5 19-13"/>
           <path ${S} d="M10 20h6v6h-6zm7 0h6v6h-6zm7 0h6v6h-6zm-7-7h6v6h-6zm7 0h6v6h-6z"/>`,

  // unknown device
  device: `<rect ${S} x="10" y="10" width="28" height="28" rx="4"/>
           <path ${S} d="M20 20a4 4 0 1 1 5.5 3.7c-1 .4-1.5 1-1.5 2.3"/>
           <circle ${F} cx="24" cy="31" r="1.8"/>`,
};

// type -> name (tooltip, edit dialogs)
export const TYPE_LABELS = {
  www: "Internet", isp: "Provider", router: "Router", switch: "Switch", firewall: "Firewall",
  ap: "Access point", controller: "Controller", management: "Management", subnet: "Subnet",
  server: "Server", hypervisor: "Hypervisor", vm: "Virtual machine", lxc: "LXC container",
  docker: "Docker container", nas: "NAS", storage: "Storage/SAN", pc: "PC", laptop: "Laptop",
  printer: "Printer", phone: "Smartphone", tv: "TV/Media", camera: "Camera",
  iot: "IoT device", device: "Device",
};

// selectable in edit dialogs
export const EDIT_TYPES = ["router", "switch", "firewall", "ap", "controller", "management", "subnet",
  "server", "hypervisor", "vm", "lxc", "docker", "nas", "storage", "pc", "laptop", "printer", "phone",
  "tv", "camera", "iot", "device", "isp", "www"];

// sensible icons for agents (edit-client dialog)
export const CLIENT_TYPES = ["laptop", "pc", "server", "hypervisor", "vm", "lxc", "docker", "nas",
  "storage", "firewall", "router", "management", "controller", "iot", "device"];

/** Inner SVG of the 48x48 icon of a type (unknown types -> question-mark box). */
export function iconBody(type) {
  return ICONS[type] || ICONS.device;
}

/** Complete inline SVG of a type icon. */
export function deviceIconSvg(type, size = 16, { title = "", style = "" } = {}) {
  return `<svg class="dev-ico" viewBox="0 0 48 48" width="${size}" height="${size}" aria-hidden="${title ? "false" : "true"}"`
    + ` style="flex:none;vertical-align:-0.2em;${style}">${title ? `<title>${title}</title>` : ""}${iconBody(type)}</svg>`;
}

/**
 * Icon type of an agent: manual choice > detected kind (agent) > derived
 * from what the backend already knows (vm/lxc, battery, platform).
 */
export function clientIconType(c) {
  if (!c) return "device";
  if (c.icon_type && ICONS[c.icon_type]) return c.icon_type;
  if (c.detected_kind && ICONS[c.detected_kind]) return c.detected_kind;
  if (c.device_type === "vm" || c.device_type === "lxc") return c.device_type;
  if (c.metrics?.battery) return "laptop";
  const p = String(c.platform || "").toLowerCase();
  const rel = String(c.release || "").toLowerCase();
  if (p.startsWith("win")) return rel.includes("server") ? "server" : "pc";
  if (p === "darwin" || p.startsWith("mac")) return "pc";
  if (p === "android" || p === "ios") return "phone";
  return "server";
}

export function clientIconSvg(c, size = 16, style = "") {
  const type = clientIconType(c);
  return deviceIconSvg(type, size, { title: TYPE_LABELS[type] || type, style });
}

// ---------------------------------------------------------------- battery
function batteryColor(pct, plugged) {
  if (plugged) return "var(--online, #34d399)";
  if (pct <= 15) return "var(--danger, #f87171)";
  if (pct <= 35) return "var(--warn, #fbbf24)";
  return "var(--text, #d8dee9)";
}
function fmtLeft(secs) {
  if (!secs || secs < 0) return "";
  const h = Math.floor(secs / 3600), m = Math.round((secs % 3600) / 60);
  return h ? `${h} h ${m} min` : `${m} min`;
}

/**
 * Small battery indicator (only if the client reports a battery). The element
 * carries data-bat-client so updateBatteryIndicators() can refresh it live.
 * variant "compact" = symbol + percent (sidebar), "full" = plus state (panel).
 */
export function batteryHtml(c, variant = "compact") {
  const b = c?.online !== false ? c?.metrics?.battery : null;
  if (!b || b.percent == null) return `<span class="bat-ind" data-bat-client="${c?.id || ""}" data-bat-variant="${variant}" style="display:none"></span>`;
  const pct = Math.max(0, Math.min(100, Math.round(b.percent)));
  const col = batteryColor(pct, b.plugged);
  const w = 14 * pct / 100;
  const left = fmtLeft(b.secsleft);
  const tip = `Battery ${pct}%${b.plugged ? " · charging / plugged in" : left ? ` · ${left} left` : ""}`;
  const svg = `<svg viewBox="0 0 24 12" width="20" height="10" style="flex:none" aria-hidden="true">
      <rect x="0.75" y="0.75" width="19.5" height="10.5" rx="2" fill="none" stroke="${col}" stroke-width="1.5"/>
      <rect x="21" y="3.5" width="2.2" height="5" rx="1" fill="${col}"/>
      <rect x="3" y="3" width="${w.toFixed(1)}" height="6" rx="0.8" fill="${col}"/>
      ${b.plugged ? `<path d="M11.5 1.8 8 6.6h3l-1 3.8 3.6-5H10.5z" fill="var(--panel, #0f172a)" stroke="var(--panel, #0f172a)" stroke-width=".6"/>` : ""}
    </svg>`;
  const extra = variant === "full" && (b.plugged || left)
    ? ` <span style="color:var(--subtext)">${b.plugged ? "⚡ plugged in" : left}</span>` : "";
  return `<span class="bat-ind" data-bat-client="${c.id}" data-bat-variant="${variant}" title="${tip}"
    style="display:inline-flex;align-items:center;gap:3px;font-size:${variant === "full" ? 13 : 11}px;color:${col};flex:none;white-space:nowrap">${svg}${pct}%${extra}</span>`;
}

/** Refreshes all battery indicators of a client (after new metrics). */
export function updateBatteryIndicators(client) {
  if (!client) return;
  document.querySelectorAll(`.bat-ind[data-bat-client="${CSS.escape(client.id)}"]`).forEach((el) => {
    const html = batteryHtml(client, el.dataset.batVariant || "compact");
    if (el.outerHTML !== html) el.outerHTML = html;
  });
}
