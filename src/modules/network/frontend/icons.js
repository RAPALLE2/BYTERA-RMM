// icons.js (module "network") - inline SVG icon set for this module.
// icon(name, size) returns an <svg> string using currentColor; drop into innerHTML.
// Note: do NOT use these inside <option> elements - selects render option text
// as plain text and strip markup.
const P = {
  gauge: '<path d="M4 15a8 8 0 0 1 16 0"/><path d="M12 15l4-3"/><circle cx="12" cy="15" r="1.3"/>',
  bolt: '<path d="M13 2.5L5 13.5h6l-1 8 8-11h-6z"/>',
  rocket: '<path d="M12 3c3.5 1.5 5 5 5 9l-2.5 2.5h-5L7 12c0-4 1.5-7.5 5-9z"/><circle cx="12" cy="9" r="1.5"/><path d="M9.5 16.5L7 20M14.5 16.5L17 20"/>',
  search: '<circle cx="10.5" cy="10.5" r="6"/><path d="M15 15l5 5"/>',
  stop: '<rect x="5.5" y="5.5" width="13" height="13" rx="2"/>',
  download: '<path d="M12 4v10M7.5 10l4.5 4 4.5-4M5 19h14"/>',
  upload: '<path d="M12 20V9M7.5 13l4.5-4.5 4.5 4.5M5 4.5h14"/>',
};
export function icon(name, size = 16) {
  const p = P[name];
  if (!p) return "";
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px">${p}</svg>`;
}
