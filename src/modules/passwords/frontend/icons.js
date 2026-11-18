// icons.js (module "passwords") - inline SVG icon set for this module.
// icon(name, size) returns an <svg> string that inherits the text color
// (currentColor), so it can be dropped straight into innerHTML.
const P = {
  key: '<path d="M15.5 7.5a3.5 3.5 0 1 1-3.4 4.4L4 20"/><path d="m7 15 2 2"/><path d="m10 12 2 2"/><circle cx="16" cy="8" r=".6" fill="currentColor"/>',
  lock: '<rect x="4.5" y="10.5" width="15" height="9.5" rx="2"/><path d="M8 10.5V7a4 4 0 0 1 8 0v3.5"/>',
  import: '<path d="M12 3v11"/><path d="m8 10 4 4 4-4"/><path d="M5 20h14"/>',
  export: '<path d="M12 14V3"/><path d="m8 7 4-4 4 4"/><path d="M5 20h14"/>',
  folder: '<path d="M3.5 7.5a2 2 0 0 1 2-2h3.3l2 2.2H18a2 2 0 0 1 2 2v7.3a2 2 0 0 1-2 2H5.5a2 2 0 0 1-2-2Z"/>',
  folderPlus: '<path d="M3.5 7.5a2 2 0 0 1 2-2h3.3l2 2.2H18a2 2 0 0 1 2 2v7.3a2 2 0 0 1-2 2H5.5a2 2 0 0 1-2-2Z"/><path d="M11.8 11v5"/><path d="M9.3 13.5h5"/>',
  users: '<circle cx="9" cy="8.5" r="3"/><path d="M3.8 19.5a5.2 5.2 0 0 1 10.4 0"/><path d="M16 5.8a3 3 0 0 1 0 5.7"/><path d="M17.3 14.3a5.2 5.2 0 0 1 3 4.7"/>',
  vault: '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><circle cx="11" cy="12" r="3.2"/><path d="M11 8.8v1.2"/><path d="M11 14v1.2"/>',
  more: '<circle cx="5.5" cy="12" r="1.4" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none"/><circle cx="18.5" cy="12" r="1.4" fill="currentColor" stroke="none"/>',
  device: '<rect x="3.5" y="4.5" width="17" height="11" rx="2"/><path d="M8.5 19.5h7"/><path d="M12 15.5v4"/>',
  copy: '<rect x="8.5" y="8.5" width="11" height="11" rx="2"/><path d="M15.5 8.5V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v7.5a2 2 0 0 0 2 2h2.5"/>',
  edit: '<path d="M14.5 5.5 18.5 9.5"/><path d="M4.5 19.5 5 15.5 15.5 5a2 2 0 0 1 3 3L8 18.5Z"/>',
  trash: '<path d="M4.5 7h15"/><path d="M9 7V5.5a1.5 1.5 0 0 1 1.5-1.5h3A1.5 1.5 0 0 1 15 5.5V7"/><path d="M6.5 7l.8 11a2 2 0 0 0 2 1.9h5.4a2 2 0 0 0 2-1.9l.8-11"/>',
  eye: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z"/><circle cx="12" cy="12" r="2.8"/>',
  fill: '<path d="M12 3v11"/><path d="m8 10 4 4 4-4"/><path d="M5 20h14"/>',
  dice: '<rect x="4.5" y="4.5" width="15" height="15" rx="3"/><circle cx="9" cy="9" r="1.1" fill="currentColor" stroke="none"/><circle cx="15" cy="9" r="1.1" fill="currentColor" stroke="none"/><circle cx="9" cy="15" r="1.1" fill="currentColor" stroke="none"/><circle cx="15" cy="15" r="1.1" fill="currentColor" stroke="none"/>',
  close: '<path d="m6 6 12 12"/><path d="m18 6-12 12"/>',
  refresh: '<path d="M20 11a8 8 0 0 0-13.7-4.6L4 8.5"/><path d="M4 4v4.5h4.5"/><path d="M4 13a8 8 0 0 0 13.7 4.6L20 15.5"/><path d="M20 20v-4.5h-4.5"/>',
  crown: '<path d="M4 8.5 7.5 13 12 6l4.5 7L20 8.5 18 19H6Z"/>',
  user: '<circle cx="12" cy="8.5" r="3.3"/><path d="M5.5 19.5a6.5 6.5 0 0 1 13 0"/>',
  plus: '<path d="M12 5v14"/><path d="M5 12h14"/>',
  warn: '<path d="M12 4 21 19H3Z"/><path d="M12 10v4"/><path d="M12 16.5v.2"/>',
};

export function icon(name, size = 16) {
  const body = P[name] || P.key;
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-3px;flex:none">${body}</svg>`;
}
