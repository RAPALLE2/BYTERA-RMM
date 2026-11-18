// icons.js (module "addir") - inline SVG icon set for this module.
// icon(name, size) returns an <svg> string using currentColor; drop into innerHTML.
// dot(color, size) returns a small filled circle in an explicit color, for status
// indicators (replaces the emoji status dots).
const P = {
  grid: '<rect x="3.5" y="3.5" width="7" height="7" rx="1"/><rect x="13.5" y="3.5" width="7" height="7" rx="1"/><rect x="3.5" y="13.5" width="7" height="7" rx="1"/><rect x="13.5" y="13.5" width="7" height="7" rx="1"/>',
  computer: '<rect x="3.5" y="4" width="17" height="12" rx="2"/><path d="M8.5 20h7"/><path d="M12 16v4"/>',
  server: '<rect x="3.5" y="4.5" width="17" height="6" rx="1.5"/><rect x="3.5" y="13.5" width="17" height="6" rx="1.5"/><path d="M7 7.5h.01M7 16.5h.01"/>',
  user: '<circle cx="12" cy="8.5" r="3.3"/><path d="M5.5 19.5a6.5 6.5 0 0 1 13 0"/>',
  users: '<circle cx="9" cy="8.5" r="3"/><path d="M3.8 19.5a5.2 5.2 0 0 1 10.4 0"/><path d="M16 5.8a3 3 0 0 1 0 5.7"/><path d="M17.3 14.3a5.2 5.2 0 0 1 3 4.7"/>',
  ou: '<path d="M3.5 7.5a2 2 0 0 1 2-2h3.3l2 2.2H18a2 2 0 0 1 2 2v7.3a2 2 0 0 1-2 2H5.5a2 2 0 0 1-2-2Z"/><path d="M3.5 10.5h17"/>',
  folder: '<path d="M3.5 7.5a2 2 0 0 1 2-2h3.3l2 2.2H18a2 2 0 0 1 2 2v7.3a2 2 0 0 1-2 2H5.5a2 2 0 0 1-2-2Z"/>',
  globe: '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17"/><path d="M12 3.5c2.5 2.3 2.5 14.7 0 17M12 3.5c-2.5 2.3-2.5 14.7 0 17"/>',
  scroll: '<path d="M6 4.5h9a1.5 1.5 0 0 1 1.5 1.5v12a2 2 0 0 0 2 2H8a2 2 0 0 1-2-2Z"/><path d="M16.5 20a2 2 0 0 0 2-2v-2h-4"/><path d="M8.5 8h6M8.5 11h6"/>',
  plug: '<path d="M9 3.5v5M15 3.5v5"/><path d="M6.5 8.5h11v2a5.5 5.5 0 0 1-11 0Z"/><path d="M12 16v4.5"/>',
  upload: '<path d="M12 14V3"/><path d="m8 7 4-4 4 4"/><path d="M5 20h14"/>',
  edit: '<path d="M14.5 5.5 18.5 9.5"/><path d="M4.5 19.5 5 15.5 15.5 5a2 2 0 0 1 3 3L8 18.5Z"/>',
  key: '<circle cx="8" cy="12" r="3.5"/><path d="M11.5 12H20l-2 2 2 2"/>',
  toggle: '<rect x="3.5" y="7.5" width="17" height="9" rx="4.5"/><circle cx="8" cy="12" r="2.6" fill="currentColor" stroke="none"/>',
  lock: '<rect x="4.5" y="10.5" width="15" height="9.5" rx="2"/><path d="M8 10.5V7a4 4 0 0 1 8 0v3.5"/>',
  unlock: '<rect x="4.5" y="10.5" width="15" height="9.5" rx="2"/><path d="M8 10.5V7a4 4 0 0 1 7.5-2.5"/>',
  trash: '<path d="M4.5 7h15"/><path d="M9 7V5.5a1.5 1.5 0 0 1 1.5-1.5h3A1.5 1.5 0 0 1 15 5.5V7"/><path d="M6.5 7l.8 11a2 2 0 0 0 2 1.9h5.4a2 2 0 0 0 2-1.9l.8-11"/>',
  pending: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7"/>',
  close: '<path d="m6 6 12 12"/><path d="m18 6-12 12"/>',
  building: '<rect x="5.5" y="3.5" width="13" height="17" rx="1.5"/><path d="M9 7h2M13 7h2M9 10.5h2M13 10.5h2M9 14h2M13 14h2"/><path d="M10 20.5v-3h4v3"/>',
  shield: '<path d="M12 3.5 19 6v5c0 4.5-3 7.8-7 9.5-4-1.7-7-5-7-9.5V6Z"/><path d="m9 12 2 2 4-4"/>',
  refresh: '<path d="M20 11a8 8 0 0 0-13.7-4.6L4 8.5"/><path d="M4 4v4.5h4.5"/><path d="M4 13a8 8 0 0 0 13.7 4.6L20 15.5"/><path d="M20 20v-4.5h-4.5"/>',
  ban: '<circle cx="12" cy="12" r="8.5"/><path d="m6 6 12 12"/>',
  plus: '<path d="M12 5v14"/><path d="M5 12h14"/>',
};

export function icon(name, size = 16) {
  const body = P[name] || P.folder;
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-3px;flex:none">${body}</svg>`;
}

export function dot(color, size = 9) {
  return `<svg viewBox="0 0 12 12" width="${size}" height="${size}" style="vertical-align:0;flex:none"><circle cx="6" cy="6" r="5" fill="${color}"/></svg>`;
}
