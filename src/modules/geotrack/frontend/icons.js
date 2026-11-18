// icons.js (module "geotrack") - inline SVG icons used across this module.
// One small helper per module (no emojis): icon(name, size) -> <svg> string.
// Icons inherit the current text color via `currentColor`.
const P = {
  pin: '<path d="M12 21s-6-5.3-6-10a6 6 0 1 1 12 0c0 4.7-6 10-6 10Z"/><circle cx="12" cy="11" r="2.2"/>',
  map: '<path d="m9 4-6 2v14l6-2 6 2 6-2V4l-6 2-6-2Z"/><path d="M9 4v14M15 6v14"/>',
  locate: '<circle cx="12" cy="12" r="3.2"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/><circle cx="12" cy="12" r="8"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-2.6-6.3M21 4v4h-4"/>',
  device: '<rect x="3" y="4" width="18" height="12" rx="1.5"/><path d="M2 20h20M9 16l-1 4M15 16l1 4"/>',
  on: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3.5" fill="currentColor" stroke="none"/>',
  off: '<circle cx="12" cy="12" r="9"/>',
  trash: '<path d="M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13"/>',
  close: '<path d="M6 6l12 12M18 6 6 18"/>',
};
export function icon(name, size = 16) {
  const body = P[name] || P.pin;
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" ` +
    `stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;flex:none">${body}</svg>`;
}
