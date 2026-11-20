// icons.js (module "tenant-link") - inline SVG icon set for this module.
// icon(name, size) returns an <svg> string using currentColor; drop into innerHTML.
const P = {
  link: '<path d="M9 15l6-6M10.5 6.5l1.4-1.4a4 4 0 0 1 5.7 5.7l-2.1 2.1M13.5 17.5l-1.4 1.4a4 4 0 0 1-5.7-5.7l2.1-2.1"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  check: '<path d="M4 12.5l5 5 11-11"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/>',
  shield: '<path d="M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6z"/><path d="M9 12l2 2 4-4"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  trash: '<path d="M4 7h16M9 7V4.5h6V7M6 7l1 13h10l1-13"/>',
  warn: '<path d="M12 3.5L21.5 20h-19z"/><path d="M12 9.5v5M12 17.3v.2"/>',
  refresh: '<path d="M4 12a8 8 0 0 1 14-5.3L20 8M20 4v4h-4M20 12a8 8 0 0 1-14 5.3L4 16M4 20v-4h4"/>',
  user: '<circle cx="12" cy="8" r="3.4"/><path d="M5 20c0-3.3 3-6 7-6s7 2.7 7 6"/>',
};
export function icon(name, size = 16) {
  const p = P[name];
  if (!p) return "";
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px">${p}</svg>`;
}
