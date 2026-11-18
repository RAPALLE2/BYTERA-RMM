// icons.js (module "webserver") - inline SVG icon set for this module.
// icon(name, size) returns an <svg> string using currentColor; drop into innerHTML.
const P = {
  globe: '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.5 2.5 2.5 14.5 0 17M12 3.5c-2.5 2.5-2.5 14.5 0 17"/>',
  lock: '<rect x="5" y="10.5" width="14" height="9.5" rx="1.5"/><path d="M8 10.5V7a4 4 0 0 1 8 0v3.5"/>',
  link: '<path d="M9.5 14.5l5-5"/><path d="M8 11l-2.5 2.5a3.5 3.5 0 0 0 5 5L13 16"/><path d="M16 13l2.5-2.5a3.5 3.5 0 0 0-5-5L11 8"/>',
  folder: '<path d="M3.5 6.5h6l2 2.5h9v11h-17z"/>',
  gear: '<circle cx="12" cy="12" r="3.2"/><path d="M12 2.5v3M12 18.5v3M21.5 12h-3M5.5 12h-3M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1M18.4 18.4l-2.1-2.1M7.7 7.7L5.6 5.6"/>',
  user: '<circle cx="12" cy="8" r="3.4"/><path d="M5 20c0-3.3 3-6 7-6s7 2.7 7 6"/>',
  users: '<circle cx="9" cy="8" r="3.2"/><path d="M3.5 19c0-3 2.5-5 5.5-5s5.5 2 5.5 5"/><path d="M16 5.2a3.2 3.2 0 0 1 0 5.9M17 14c2.6.2 4.5 2.1 4.5 5"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
  note: '<path d="M6 3.5h9l4 4V20.5H6z"/><path d="M9 11h6M9 14.5h6M9 8h3"/>',
  upload: '<path d="M12 20V9M7.5 13l4.5-4.5 4.5 4.5M5 4.5h14"/>',
  file: '<path d="M6 3.5h8l4 4V20.5H6z"/><path d="M14 3.5v4h4"/>',
  edit: '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M14 6l4 4"/>',
  trash: '<path d="M4 6.5h16M9 6.5V4.5h6v2M6 6.5l1 14h10l1-14"/>',
};
export function icon(name, size = 16) {
  const p = P[name];
  if (!p) return "";
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px">${p}</svg>`;
}
