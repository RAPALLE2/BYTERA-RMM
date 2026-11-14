// icons.js (Modul "topology")
// ---------------------------
// Eigene SVG-Icons je Geraete-Typ (einfarbige Strich-Silhouetten, faerben sich
// ueber "currentColor" nach dem App-Thema). Jede Funktion liefert den INHALT
// eines 48x48-ViewBox-Symbols - gezeichnet wird in topology.js.

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

  // unbekanntes Geraet
  device: `<rect ${S} x="10" y="10" width="28" height="28" rx="4"/>
           <path ${S} d="M20 20a4 4 0 1 1 5.5 3.7c-1 .4-1.5 1-1.5 2.3"/>
           <circle ${F} cx="24" cy="31" r="1.8"/>`,
};

// Typ -> deutscher Name (fuer Tooltip und Bearbeiten-Dialog)
export const TYPE_LABELS = {
  www: "Internet", isp: "Provider", router: "Router", switch: "Switch",
  ap: "Access Point", server: "Server", hypervisor: "Hypervisor",
  nas: "NAS", storage: "Speicher/SAN", pc: "PC", laptop: "Laptop",
  printer: "Drucker", phone: "Smartphone", tv: "TV/Media", camera: "Kamera",
  iot: "IoT-Gerät", firewall: "Firewall", device: "Gerät",
};

// Typen, die sich im Bearbeiten-Dialog auswaehlen lassen
export const EDIT_TYPES = ["router", "switch", "ap", "firewall", "server", "hypervisor",
  "nas", "storage", "pc", "laptop", "printer", "phone", "tv", "camera", "iot", "device", "isp", "www"];

/** Inhalt eines 48x48-Icons fuer den Typ (unbekannte Typen -> Fragezeichen-Box). */
export function iconBody(type) {
  return ICONS[type] || ICONS.device;
}
