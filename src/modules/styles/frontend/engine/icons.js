// icons.js (module "styles") - inline SVG icon set for the classic view.
// icon(name, size) returns an <svg> string using currentColor; drop into innerHTML.
const P = {
  clipboard: '<rect x="6" y="4.5" width="12" height="16" rx="1.5"/><rect x="9" y="3" width="6" height="3.5" rx="1"/><path d="M8.5 10h7M8.5 13.5h7M8.5 17h4"/>',
  chart: '<path d="M4 20V4M4 20h16"/><path d="M8 16l3.5-4 3 2.5L20 8"/>',
  disk: '<rect x="3.5" y="3.5" width="17" height="17" rx="2"/><circle cx="12" cy="12" r="3"/><path d="M12 3.5v3M18 6l-2 2"/>',
  wrench: '<path d="M15 6a4 4 0 0 1-5 5l-6 6 2.5 2.5 6-6a4 4 0 0 0 5-5l-2.5 2.5-2.5-2.5z"/>',
  globe: '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.5 2.5 2.5 14.5 0 17M12 3.5c-2.5 2.5-2.5 14.5 0 17"/>',
  bolt: '<path d="M13 2.5L5 13.5h6l-1 8 8-11h-6z"/>',
  arrowup: '<path d="M12 20V5M6 11l6-6 6 6"/>',
  link: '<path d="M9.5 14.5l5-5"/><path d="M8 11l-2.5 2.5a3.5 3.5 0 0 0 5 5L13 16"/><path d="M16 13l2.5-2.5a3.5 3.5 0 0 0-5-5L11 8"/>',
  files: '<path d="M3.5 6.5h6l2 2.5h9v11h-17z"/>',
  note: '<path d="M6 3.5h9l4 4V20.5H6z"/><path d="M9 11h6M9 14.5h6M9 8h3"/>',
  shield: '<path d="M12 3l7 3v5c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z"/>',
  computer: '<rect x="3" y="4.5" width="18" height="12" rx="1.5"/><path d="M8.5 20h7M12 16.5V20"/>',
  warn: '<path d="M12 3.5L21.5 20h-19z"/><path d="M12 9.5v5M12 17.3v.2"/>',
  building: '<rect x="5" y="3.5" width="14" height="17" rx="1"/><path d="M9 7h2M13 7h2M9 11h2M13 11h2M9 15h2M13 15h2"/>',
  pin: '<path d="M12 21s7-5.5 7-11a7 7 0 1 0-14 0c0 5.5 7 11 7 11z"/><circle cx="12" cy="10" r="2.5"/>',
  datacenter: '<rect x="3.5" y="4" width="17" height="6" rx="1.5"/><rect x="3.5" y="14" width="17" height="6" rx="1.5"/><path d="M7 7v.01M7 17v.01"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7v5.2l3.5 2.3"/>',
  check: '<path d="M4 12.5l5 5 11-11"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  ban: '<circle cx="12" cy="12" r="8.5"/><path d="M6 6l12 12"/>',
  home: '<path d="M4 11l8-7 8 7"/><path d="M6 10v10h12V10"/><path d="M10 20v-6h4v6"/>',
  user: '<circle cx="12" cy="8" r="3.4"/><path d="M5 20c0-3.3 3-6 7-6s7 2.7 7 6"/>',
};
export function icon(name, size = 16) {
  const p = P[name];
  if (!p) return "";
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px">${p}</svg>`;
}
