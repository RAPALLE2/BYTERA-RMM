// icons.js (module "tenants") - inline SVG icon set for this module.
// icon(name, size) returns an <svg> string using currentColor; drop into innerHTML.
// Note: do NOT use these inside <option> elements - selects render option text
// as plain text and strip markup.
const P = {
  building: '<rect x="5" y="3.5" width="14" height="17" rx="1"/><path d="M9 7h2M13 7h2M9 11h2M13 11h2M9 15h2M13 15h2"/>',
  gear: '<circle cx="12" cy="12" r="3.2"/><path d="M12 2.5v3M12 18.5v3M21.5 12h-3M5.5 12h-3M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1M18.4 18.4l-2.1-2.1M7.7 7.7L5.6 5.6"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
  check: '<path d="M4 12.5l5 5 11-11"/>',
  warn: '<path d="M12 3.5L21.5 20h-19z"/><path d="M12 9.5v5M12 17.3v.2"/>',
  folder: '<path d="M3.5 6.5h6l2 2.5h9v11h-17z"/>',
  file: '<path d="M6 3.5h8l4 4V20.5H6z"/><path d="M14 3.5v4h4"/>',
  arrowup: '<path d="M12 20V5M6 11l6-6 6 6"/>',
  download: '<path d="M12 4v10M7.5 10l4.5 4 4.5-4M5 19h14"/>',
  user: '<circle cx="12" cy="8" r="3.4"/><path d="M5 20c0-3.3 3-6 7-6s7 2.7 7 6"/>',
  users: '<circle cx="9" cy="8" r="3.2"/><path d="M3.5 19c0-3 2.5-5 5.5-5s5.5 2 5.5 5"/><path d="M16 5.2a3.2 3.2 0 0 1 0 5.9M17 14c2.6.2 4.5 2.1 4.5 5"/>',
  computer: '<rect x="3" y="4.5" width="18" height="12" rx="1.5"/><path d="M8.5 20h7M12 16.5V20"/>',
  network: '<rect x="3" y="3" width="7" height="5" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="8.5" y="16" width="7" height="5" rx="1"/><path d="M6.5 8v3h11V8M12 11v5"/>',
};
export function icon(name, size = 16) {
  const p = P[name];
  if (!p) return "";
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px">${p}</svg>`;
}
