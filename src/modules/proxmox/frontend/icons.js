// icons.js (module "proxmox") - inline SVG icon set for this module.
// icon(name, size) returns an <svg> string using currentColor, safe to drop
// into innerHTML. Use only where the target is innerHTML (not textContent).
const P = {
  cluster: '<rect x="3.5" y="4" width="17" height="6" rx="1.5"/><rect x="3.5" y="14" width="17" height="6" rx="1.5"/><circle cx="7" cy="7" r=".7" fill="currentColor" stroke="none"/><circle cx="7" cy="17" r=".7" fill="currentColor" stroke="none"/>',
  server: '<rect x="4" y="3.5" width="16" height="12" rx="2"/><path d="M8.5 19.5h7"/><path d="M12 15.5v4"/><path d="M7.5 7.5h9"/>',
  box: '<path d="m12 3 8 4.5v9L12 21l-8-4.5v-9Z"/><path d="m4 7.5 8 4.5 8-4.5"/><path d="M12 12v9"/>',
  gear: '<circle cx="12" cy="12" r="3.2"/><path d="M12 2.5v3M12 18.5v3M21.5 12h-3M5.5 12h-3M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1M18.4 18.4l-2.1-2.1M7.7 7.7 5.6 5.6"/>',
  trash: '<path d="M4.5 7h15"/><path d="M9 7V5.5a1.5 1.5 0 0 1 1.5-1.5h3A1.5 1.5 0 0 1 15 5.5V7"/><path d="M6.5 7l.8 11a2 2 0 0 0 2 1.9h5.4a2 2 0 0 0 2-1.9l.8-11"/>',
  link: '<path d="M10 14a3.5 3.5 0 0 0 5 0l3-3a3.5 3.5 0 0 0-5-5l-1.5 1.5"/><path d="M14 10a3.5 3.5 0 0 0-5 0l-3 3a3.5 3.5 0 0 0 5 5l1.5-1.5"/>',
  globe: '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17"/><path d="M12 3.5c2.5 2.3 2.5 14.7 0 17M12 3.5c-2.5 2.3-2.5 14.7 0 17"/>',
  lock: '<rect x="4.5" y="10.5" width="15" height="9.5" rx="2"/><path d="M8 10.5V7a4 4 0 0 1 8 0v3.5"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4 4"/>',
  save: '<path d="M5.5 4.5h10l4 4v11a1 1 0 0 1-1 1H5.5a1 1 0 0 1-1-1v-14a1 1 0 0 1 1-1Z"/><path d="M8 4.5v5h7"/><circle cx="12" cy="15" r="2.2"/>',
  users: '<circle cx="9" cy="8.5" r="3"/><path d="M3.8 19.5a5.2 5.2 0 0 1 10.4 0"/><path d="M16 5.8a3 3 0 0 1 0 5.7"/><path d="M17.3 14.3a5.2 5.2 0 0 1 3 4.7"/>',
  user: '<circle cx="12" cy="8.5" r="3.3"/><path d="M5.5 19.5a6.5 6.5 0 0 1 13 0"/>',
  edit: '<path d="M14.5 5.5 18.5 9.5"/><path d="M4.5 19.5 5 15.5 15.5 5a2 2 0 0 1 3 3L8 18.5Z"/>',
  compass: '<circle cx="12" cy="12" r="8.5"/><path d="m15.5 8.5-2 5-5 2 2-5Z"/>',
  download: '<path d="M12 3v11"/><path d="m8 10 4 4 4-4"/><path d="M5 20h14"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7"/>',
  cross: '<path d="m6 6 12 12"/><path d="m18 6-12 12"/>',
  help: '<circle cx="12" cy="12" r="8.5"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.4 2.3c-.8.3-1.4 1-1.4 1.9v.6"/><path d="M11.5 17.3v.2"/>',
  play: '<path d="M7.5 5.5v13l11-6.5Z"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="1.5"/>',
  power: '<path d="M12 3.5v8"/><path d="M6.8 7a8 8 0 1 0 10.4 0"/>',
  reboot: '<path d="M20 11a8 8 0 0 0-13.7-4.6L4 8.5"/><path d="M4 4v4.5h4.5"/><path d="M4 13a8 8 0 0 0 13.7 4.6L20 15.5"/><path d="M20 20v-4.5h-4.5"/>',
  fullscreen: '<path d="M4 9V4.5h4.5M20 9V4.5h-4.5M4 15v4.5h4.5M20 15v4.5h-4.5"/>',
  refresh: '<path d="M20 11a8 8 0 0 0-13.7-4.6L4 8.5"/><path d="M4 4v4.5h4.5"/><path d="M4 13a8 8 0 0 0 13.7 4.6L20 15.5"/><path d="M20 20v-4.5h-4.5"/>',
  close: '<path d="m6 6 12 12"/><path d="m18 6-12 12"/>',
  resize: '<path d="M9 4.5H4.5V9"/><path d="M15 19.5h4.5V15"/><path d="M4.5 4.5 10.5 10.5"/><path d="M19.5 19.5 13.5 13.5"/>',
  clone: '<rect x="8.5" y="8.5" width="11" height="11" rx="2"/><path d="M15.5 8.5V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v7.5a2 2 0 0 0 2 2h2.5"/>',
  migrate: '<path d="M4 12h14"/><path d="m13 7 5 5-5 5"/>',
  plus: '<path d="M12 5v14"/><path d="M5 12h14"/>',
  back: '<path d="M20 12H6"/><path d="m11 7-5 5 5 5"/>',
};

export function icon(name, size = 16) {
  const body = P[name] || P.cluster;
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-3px;flex:none">${body}</svg>`;
}
