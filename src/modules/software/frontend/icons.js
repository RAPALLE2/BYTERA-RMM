// icons.js (module "software") - inline SVG icon set for this module.
// icon(name, size) returns an <svg> string using currentColor; drop into innerHTML.
const P = {
  package: '<path d="M12 2.5l8.5 4.5v9.5L12 21l-8.5-4.5V7z"/><path d="M3.5 7l8.5 4.5L20.5 7M12 11.5V21"/>',
  truck: '<rect x="2.5" y="6" width="11" height="9" rx="1"/><path d="M13.5 9h4l3 3v3h-7z"/><circle cx="6.5" cy="17.5" r="1.8"/><circle cx="17" cy="17.5" r="1.8"/>',
  edit: '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M14 6l4 4"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
  file: '<path d="M6 3.5h8l4 4V20.5H6z"/><path d="M14 3.5v4h4"/>',
  upload: '<path d="M12 20V9M7.5 13l4.5-4.5 4.5 4.5M5 4.5h14"/>',
};
export function icon(name, size = 16) {
  const p = P[name];
  if (!p) return "";
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px">${p}</svg>`;
}
