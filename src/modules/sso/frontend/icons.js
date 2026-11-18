// icons.js (module "sso") - inline SVG icon set for this module.
// icon(name, size) returns an <svg> string using currentColor; drop into innerHTML.
const P = {
  check: '<path d="M4 12.5l5 5 11-11"/>',
  warn: '<path d="M12 3.5L21.5 20h-19z"/><path d="M12 9.5v5M12 17.3v.2"/>',
};
export function icon(name, size = 16) {
  const p = P[name];
  if (!p) return "";
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px">${p}</svg>`;
}
