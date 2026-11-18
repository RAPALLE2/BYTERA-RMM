// icons.js (module "cloudflare") - inline SVG icon set for this module.
// icon(name, size) returns an <svg> string using currentColor; drop into innerHTML.
// dot(color, size) returns a small filled circle in an explicit color, for status
// indicators (replaces the emoji status dots).
const P = {
  check: '<path d="M4 12.5l5 5 11-11"/>',
  warn: '<path d="M12 3.5L21.5 20h-19z"/><path d="M12 9.5v5M12 17.3v.2"/>',
  edit: '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M14 6l4 4"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
};
export function icon(name, size = 16) {
  const p = P[name];
  if (!p) return "";
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px">${p}</svg>`;
}
export function dot(color, size = 11) {
  return `<svg viewBox="0 0 12 12" width="${size}" height="${size}" style="vertical-align:0"><circle cx="6" cy="6" r="5" fill="${color}"/></svg>`;
}
