// icons.js (module "sql") - inline SVG icon set for this module.
// icon(name, size) returns an <svg> string using currentColor; drop into innerHTML.
// dot(color, size) returns a small filled circle in an explicit color, for status
// indicators (replaces the emoji status dots).
const P = {
  save: '<path d="M5 3.5h11l3 3V20.5H5z"/><path d="M8 3.5v5h7v-5"/><rect x="8" y="13" width="8" height="7.5"/>',
  globe: '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.5 2.5 2.5 14.5 0 17M12 3.5c-2.5 2.5-2.5 14.5 0 17"/>',
  docker: '<rect x="3" y="10" width="3" height="3"/><rect x="7" y="10" width="3" height="3"/><rect x="11" y="10" width="3" height="3"/><rect x="7" y="6" width="3" height="3"/><rect x="11" y="6" width="3" height="3"/><path d="M2.5 13.5h14c3.5 0 5-1.5 5.5-3.5-1.5 1-3 .5-3.5 0 .5-2-1-3.5-2-4-1 1.5-1 3.5 0 4.5-1 .5-2 .5-14 .5v2z"/>',
  files: '<path d="M3.5 6.5h6l2 2.5h9v11h-17z"/>',
  check: '<path d="M4 12.5l5 5 11-11"/>',
  warn: '<path d="M12 3.5L21.5 20h-19z"/><path d="M12 9.5v5M12 17.3v.2"/>',
  refresh: '<path d="M20 11a8 8 0 1 0-.5 4"/><path d="M20 4v5h-5"/>',
};
export function icon(name, size = 16) {
  const p = P[name];
  if (!p) return "";
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px">${p}</svg>`;
}
export function dot(color, size = 10) {
  return `<svg viewBox="0 0 12 12" width="${size}" height="${size}" style="vertical-align:0"><circle cx="6" cy="6" r="5" fill="${color}"/></svg>`;
}
