// icons.js (module "notify") - inline SVG icon set for this module.
// icon(name, size) returns an <svg> string using currentColor; drop into innerHTML.
// Note: do NOT use these inside <option> elements - selects render option text
// as plain text and strip markup.
const P = {
  list: '<path d="M8 6h12M8 12h12M8 18h12"/><circle cx="4" cy="6" r="1.2"/><circle cx="4" cy="12" r="1.2"/><circle cx="4" cy="18" r="1.2"/>',
  link: '<path d="M9.5 14.5l5-5"/><path d="M8 11l-2.5 2.5a3.5 3.5 0 0 0 5 5L13 16"/><path d="M16 13l2.5-2.5a3.5 3.5 0 0 0-5-5L11 8"/>',
  mail: '<rect x="3" y="5.5" width="18" height="13" rx="2"/><path d="M4 7l8 6 8-6"/>',
};
export function icon(name, size = 16) {
  const p = P[name];
  if (!p) return "";
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px">${p}</svg>`;
}
