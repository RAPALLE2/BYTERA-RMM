// icons.js (module "scripts") - inline SVG icon set for this module.
// icon(name, size) returns an <svg> string using currentColor; drop into innerHTML.
const P = {
  kit: '<rect x="3.5" y="7.5" width="17" height="12" rx="2"/><path d="M8.5 7.5V6a2 2 0 0 1 2-2h3a2 2 0 0 1 2 2v1.5"/><path d="M3.5 12.5h17"/><path d="M10.5 12.5v2h3v-2"/>',
  windows: '<path d="M3.5 6.2 11 5.1v6.1H3.5Z"/><path d="M12.5 4.9 20.5 3.8v7.4h-8Z"/><path d="M3.5 12.8H11v6.1l-7.5-1.1Z"/><path d="M12.5 12.8h8v7.4l-8-1.1Z"/>',
  linux: '<path d="M9 4.5c-1.3 1.2-1.3 3.6-.3 5.4.6 1 .5 2-.4 3.2-1.4 1.8-2 3.4-1.3 4.6.6 1 2.3 1.3 4.5 1.3s3.9-.3 4.5-1.3c.7-1.2.1-2.8-1.3-4.6-.9-1.2-1-2.2-.4-3.2 1-1.8 1-4.2-.3-5.4a3 3 0 0 0-4.3 0Z"/><circle cx="10.3" cy="8" r=".5" fill="currentColor"/><circle cx="13.7" cy="8" r=".5" fill="currentColor"/>',
  eye: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z"/><circle cx="12" cy="12" r="2.8"/>',
  download: '<path d="M12 3v11"/><path d="m8 10 4 4 4-4"/><path d="M5 20h14"/>',
  save: '<path d="M5.5 4.5h10l4 4v11a1 1 0 0 1-1 1H5.5a1 1 0 0 1-1-1v-14a1 1 0 0 1 1-1Z"/><path d="M8 4.5v5h7"/><circle cx="12" cy="15" r="2.2"/>',
  rocket: '<path d="M12 3c3 1.5 5 4.5 5 8l-2.5 2.5h-5L7 11c0-3.5 2-6.5 5-8Z"/><circle cx="12" cy="9.5" r="1.4"/><path d="M9.5 14 7 16.5M14.5 14 17 16.5M12 15v4"/>',
  trash: '<path d="M4.5 7h15"/><path d="M9 7V5.5a1.5 1.5 0 0 1 1.5-1.5h3A1.5 1.5 0 0 1 15 5.5V7"/><path d="M6.5 7l.8 11a2 2 0 0 0 2 1.9h5.4a2 2 0 0 0 2-1.9l.8-11"/>',
  script: '<path d="M7 3.5h8l4 4v11.5a1.5 1.5 0 0 1-1.5 1.5H7A1.5 1.5 0 0 1 5.5 19V5A1.5 1.5 0 0 1 7 3.5Z"/><path d="M14.5 3.5V8h4"/><path d="M8.5 12.5h7M8.5 15.5h7"/>',
  folder: '<path d="M3.5 7.5a2 2 0 0 1 2-2h3.3l2 2.2H18a2 2 0 0 1 2 2v7.3a2 2 0 0 1-2 2H5.5a2 2 0 0 1-2-2Z"/>',
  file: '<path d="M7 3.5h8l4 4v11.5a1.5 1.5 0 0 1-1.5 1.5H7A1.5 1.5 0 0 1 5.5 19V5A1.5 1.5 0 0 1 7 3.5Z"/><path d="M14.5 3.5V8h4"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4 4"/>',
  list: '<path d="M8 6.5h11M8 12h11M8 17.5h11"/><circle cx="4.5" cy="6.5" r="1" fill="currentColor"/><circle cx="4.5" cy="12" r="1" fill="currentColor"/><circle cx="4.5" cy="17.5" r="1" fill="currentColor"/>',
  bolt: '<path d="M13 3 5 13h6l-1 8 8-10h-6Z"/>',
  play: '<path d="M7.5 5.5v13l11-6.5Z"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7"/>',
  cross: '<path d="m6 6 12 12"/><path d="m18 6-12 12"/>',
  pending: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  plus: '<path d="M12 5v14"/><path d="M5 12h14"/>',
};

export function icon(name, size = 16) {
  const body = P[name] || P.script;
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-3px;flex:none">${body}</svg>`;
}
