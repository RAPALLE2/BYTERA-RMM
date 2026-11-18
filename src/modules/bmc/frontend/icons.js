// icons.js (module "bmc") - inline SVG icons for this module (no emojis).
// icon(name, size) -> <svg> string; colors follow currentColor.
const P = {
  server: '<rect x="3" y="4" width="18" height="7" rx="1.5"/><rect x="3" y="13" width="18" height="7" rx="1.5"/><path d="M7 7.5h.01M7 16.5h.01"/>',
  link: '<path d="M9 15l6-6M10 6l1-1a4 4 0 0 1 6 6l-1 1M14 18l-1 1a4 4 0 0 1-6-6l1-1"/>',
  edit: '<path d="M4 20h4L19 9a2 2 0 0 0-3-3L5 17v3Z"/><path d="M14 7l3 3"/>',
  trash: '<path d="M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-2.6-6.3M21 4v4h-4"/>',
  power: '<path d="M12 3v9M6.5 7a8 8 0 1 0 11 0"/>',
  play: '<path d="M7 5l12 7-12 7V5Z"/>',
  shutdown: '<path d="M12 4v8M7 7a7 7 0 1 0 10 0"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="1.5"/>',
  restart: '<path d="M21 12a9 9 0 1 1-2.6-6.3M21 4v4h-4"/>',
  forcerestart: '<path d="M3 12a9 9 0 0 1 15-6.7L21 8M21 4v4h-4M21 12a9 9 0 0 1-15 6.7L3 16M3 20v-4h4"/>',
  button: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5"/>',
  warn: '<path d="M12 3 2 20h20L12 3Z"/><path d="M12 10v4M12 17h.01"/>',
  bulb: '<path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-4 10c1 1 1.5 1.5 1.5 3h5c0-1.5.5-2 1.5-3a6 6 0 0 0-4-10Z"/>',
  thermo: '<path d="M14 14.8V5a2 2 0 1 0-4 0v9.8a4 4 0 1 0 4 0Z"/>',
  fan: '<circle cx="12" cy="12" r="2"/><path d="M12 10c0-4 1-7 3-7s2 4-1 6M12 14c0 4-1 7-3 7s-2-4 1-6M10 12c-4 0-7-1-7-3s4-2 6 1M14 12c4 0 7 1 7 3s-4 2-6-1"/>',
  plug: '<path d="M9 2v6M15 2v6M7 8h10v3a5 5 0 0 1-10 0V8ZM12 16v6"/>',
  bolt: '<path d="M13 2 4 14h7l-1 8 9-12h-7l1-8Z"/>',
  cpu: '<rect x="7" y="7" width="10" height="10" rx="1.5"/><path d="M10 7V4M14 7V4M10 20v-3M14 20v-3M7 10H4M7 14H4M20 10h-3M20 14h-3"/>',
  ram: '<rect x="3" y="7" width="18" height="10" rx="1.5"/><path d="M7 7v3M11 7v3M13 7v3M17 7v3M6 20v-3M18 20v-3"/>',
  disk: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="2.5"/><path d="M19 16l-4-2"/>',
  net: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/><path d="M10 6h5a2 2 0 0 1 2 2v6"/>',
  logs: '<path d="M6 3h9l4 4v14H6V3Z"/><path d="M14 3v4h4M9 12h7M9 16h7"/>',
  boot: '<path d="M4 17h16M6 17V7l6-3 6 3v10M9 11h6"/>',
  media: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="2.4"/>',
  eject: '<path d="M6 16h12M6 13 12 6l6 7H6Z"/>',
  firmware: '<rect x="6" y="6" width="12" height="12" rx="1.5"/><path d="M9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3"/>',
  console: '<rect x="3" y="4" width="18" height="13" rx="1.5"/><path d="M7 8l3 2.5L7 13M12 13h4M8 21h8"/>',
  tools: '<path d="M14 7a3.5 3.5 0 0 1-4.6 4.6L5 16l3 3 4.4-4.4A3.5 3.5 0 0 1 17 10l-3-3Z"/>',
  external: '<path d="M14 5h5v5M19 5l-8 8M12 5H6a1 1 0 0 0-1 1v12a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-6"/>',
  check: '<path d="M5 12l4 4L19 6"/>',
  dot: '<circle cx="12" cy="12" r="6" fill="currentColor" stroke="none"/>',
};
export function icon(name, size = 16) {
  const body = P[name] || P.server;
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" ` +
    `stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-3px;flex:none">${body}</svg>`;
}
