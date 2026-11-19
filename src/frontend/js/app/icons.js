// icons.js
// --------
// Colored SVG icons instead of emoji (option in Profile > Symbols).
//
// How it works: a MutationObserver replaces known UI emoji in text nodes LIVE
// with small inline SVGs (<span class="svgicon">). Thanks to this, NONE of the
// ~30 app files need to be touched - all existing and future `${"📁"}`
// occurrences are covered automatically, and on systems without an emoji font
// (Linux/Android without Noto Color Emoji) the icons look identical everywhere.
//
// Mode: localStorage "rmm_icon_mode" = "svg" (default) | "emoji".
// Switching takes effect immediately (revert replaces all spans with the
// original emoji from data-ch).
//
// Excluded (never touched): inputs (INPUT/TEXTAREA/SELECT/OPTION),
// contenteditable, <svg>, xterm terminals and terminal output (payload!).

const KEY = "rmm_icon_mode";

// ---- color palette (dark theme, deliberately boldly "painted") ----
const C = {
  amber: "#eab308", amberD: "#b45309",
  blue: "#4f8cff", blueD: "#2d5fd0", sky: "#7cc4ff",
  gray: "#9aa7b8", grayD: "#5b6a85", slate: "#3a4763", slateL: "#55648a",
  green: "#34d399", red: "#f75c5c", purple: "#a78bfa",
  paper: "#dfe6f2", paperD: "#b9c4d8", dark: "#232c42",
};
const svg = (inner, vb = "0 0 16 16") =>
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${vb}" aria-hidden="true">${inner}</svg>`;

// ---- icon definitions (emoji -> colored SVG) ----
const ICONS = {
  "📁": svg(`<path d="M1 4a1.5 1.5 0 0 1 1.5-1.5h3.2l1.4 1.6h6.4A1.5 1.5 0 0 1 15 5.6V12a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 1 12z" fill="${C.amber}"/><path d="M1 6h14v6a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 1 12z" fill="#f5c542"/>`),
  "🗂️": svg(`<path d="M1 5h4l1 1.4h9V13a1.4 1.4 0 0 1-1.4 1.4H2.4A1.4 1.4 0 0 1 1 13z" fill="${C.amber}"/><rect x="2.6" y="2" width="10.8" height="2" rx=".6" fill="${C.paperD}"/><rect x="1.8" y="3.4" width="12.4" height="1.8" rx=".6" fill="${C.paper}"/>`),
  "📄": svg(`<path d="M3 1.5h7l3 3V14a.9.9 0 0 1-.9.9H3.9A.9.9 0 0 1 3 14z" fill="${C.paper}"/><path d="M10 1.5l3 3h-3z" fill="${C.paperD}"/><rect x="4.6" y="7" width="6.8" height="1.1" rx=".5" fill="${C.blue}"/><rect x="4.6" y="9.3" width="6.8" height="1.1" rx=".5" fill="${C.grayD}"/><rect x="4.6" y="11.6" width="4.6" height="1.1" rx=".5" fill="${C.grayD}"/>`),
  "📜": svg(`<path d="M4 2h9a1 1 0 0 1 1 1v10.2a1.3 1.3 0 0 1-2.6 0V4H4z" fill="${C.paperD}"/><path d="M2 3.3A1.3 1.3 0 0 1 3.3 2H11v11.2c0 .5.1.9.4 1.3H4a2 2 0 0 1-2-2z" fill="${C.paper}"/><rect x="4" y="4.6" width="5" height="1" rx=".4" fill="${C.amberD}"/><rect x="4" y="6.8" width="5" height="1" rx=".4" fill="${C.grayD}"/><rect x="4" y="9" width="3.6" height="1" rx=".4" fill="${C.grayD}"/>`),
  "📝": svg(`<path d="M2.5 2h9a.9.9 0 0 1 .9.9V14a.9.9 0 0 1-.9.9h-8A1.5 1.5 0 0 1 2 13.4V2.5z" fill="${C.paper}"/><rect x="4" y="4.4" width="6" height="1" rx=".4" fill="${C.grayD}"/><rect x="4" y="6.6" width="6" height="1" rx=".4" fill="${C.grayD}"/><path d="M9 12.7l4.6-4.6 1.6 1.6-4.6 4.6-2 .4z" fill="${C.amber}"/>`),
  "📋": svg(`<rect x="3" y="2.6" width="10" height="12" rx="1.2" fill="${C.paper}"/><rect x="5.4" y="1.2" width="5.2" height="2.8" rx=".9" fill="${C.blue}"/><rect x="5" y="6" width="6" height="1" rx=".4" fill="${C.grayD}"/><rect x="5" y="8.2" width="6" height="1" rx=".4" fill="${C.grayD}"/><rect x="5" y="10.4" width="4" height="1" rx=".4" fill="${C.grayD}"/>`),
  "🖥️": svg(`<rect x="1.4" y="2.4" width="13.2" height="8.6" rx="1.1" fill="${C.slate}"/><rect x="2.5" y="3.5" width="11" height="6.4" rx=".5" fill="${C.blue}"/><rect x="6.6" y="11" width="2.8" height="2" fill="${C.slateL}"/><rect x="4.6" y="13" width="6.8" height="1.3" rx=".6" fill="${C.slateL}"/>`),
  "💻": svg(`<rect x="2.4" y="3" width="11.2" height="7.4" rx="1" fill="${C.slate}"/><rect x="3.4" y="4" width="9.2" height="5.4" rx=".4" fill="${C.sky}"/><path d="M1.2 12.6l1.6-1.6h10.4l1.6 1.6a.8.8 0 0 1-.8 1H2a.8.8 0 0 1-.8-1z" fill="${C.slateL}"/>`),
  "🪟": svg(`<rect x="1.6" y="1.6" width="6" height="6" fill="#4f8cff"/><rect x="8.4" y="1.6" width="6" height="6" fill="#34d399"/><rect x="1.6" y="8.4" width="6" height="6" fill="#eab308"/><rect x="8.4" y="8.4" width="6" height="6" fill="#f75c5c"/>`),
  "🔄": svg(`<path d="M8 2.2a5.8 5.8 0 0 1 5.7 4.7h-1.9L14.6 10l2-3.1h-1.4A7.3 7.3 0 0 0 8 .7z" fill="${C.blue}" transform="translate(-.6 .4)"/><path d="M8 13.8a5.8 5.8 0 0 1-5.7-4.7h1.9L1.4 6l-2 3.1h1.4A7.3 7.3 0 0 0 8 15.3z" fill="${C.blue}" transform="translate(1.2 .2)"/>`),
  "🔁": svg(`<path d="M4.5 4h7v-1.8L15 5 11.5 7.8V6h-6a1 1 0 0 0-1 1v1.6H2.5V6a2 2 0 0 1 2-2z" fill="${C.green}"/><path d="M11.5 12h-7v1.8L1 9l3.5-2.8V10h6a1 1 0 0 0 1-1V7.4h2V10a2 2 0 0 1-2 2z" fill="${C.green}" transform="translate(0 2)"/>`),
  "🗑": svg(`<path d="M3.4 4.8h9.2l-.8 9.1a1.2 1.2 0 0 1-1.2 1.1H5.4a1.2 1.2 0 0 1-1.2-1.1z" fill="${C.gray}"/><rect x="2.4" y="2.9" width="11.2" height="1.6" rx=".6" fill="${C.grayD}"/><rect x="6" y="1.4" width="4" height="1.8" rx=".6" fill="${C.grayD}"/><rect x="5.8" y="6.6" width="1.2" height="6" rx=".55" fill="#727f97"/><rect x="9" y="6.6" width="1.2" height="6" rx=".55" fill="#727f97"/>`),
  "🧹": svg(`<rect x="8.6" y="1" width="1.6" height="7" rx=".8" fill="${C.amberD}" transform="rotate(20 9.4 4.5)"/><path d="M4.2 8.2c2.2-1.6 5-1.6 7.2 0l1.4 5.6c-3.2 1.6-6.8 1.6-10 0z" fill="${C.amber}"/><path d="M3.4 11.4c2.8 1.2 6.4 1.2 9.2 0" stroke="${C.amberD}" stroke-width="1" fill="none"/>`),
  "⚙️": svg(`<path d="M8 1.4l1 .2.4 1.6c.5.1 1 .3 1.4.6l1.5-.7 1.4 1.4-.7 1.5c.3.4.5.9.6 1.4l1.6.4v2l-1.6.4c-.1.5-.3 1-.6 1.4l.7 1.5-1.4 1.4-1.5-.7c-.4.3-.9.5-1.4.6l-.4 1.6h-2l-.4-1.6c-.5-.1-1-.3-1.4-.6l-1.5.7-1.4-1.4.7-1.5c-.3-.4-.5-.9-.6-1.4L1.4 9V7l1.6-.4c.1-.5.3-1 .6-1.4l-.7-1.5 1.4-1.4 1.5.7c.4-.3.9-.5 1.4-.6L7.6 1.6z" fill="${C.gray}"/><circle cx="8" cy="8" r="2.4" fill="${C.slate}"/>`),
  "🔍": svg(`<circle cx="6.6" cy="6.6" r="4.2" fill="none" stroke="${C.blue}" stroke-width="1.8"/><rect x="9.6" y="8.9" width="5.4" height="2" rx="1" fill="${C.blueD}" transform="rotate(45 9.6 8.9)"/>`),
  "🔗": svg(`<path d="M6.2 9.8l3.6-3.6" stroke="${C.gray}" stroke-width="1.6" stroke-linecap="round"/><path d="M7.4 4.4l1.6-1.6a2.8 2.8 0 0 1 4 4l-1.6 1.6a2.8 2.8 0 0 1-4 0" fill="none" stroke="${C.blue}" stroke-width="1.7" stroke-linecap="round"/><path d="M8.6 11.6L7 13.2a2.8 2.8 0 0 1-4-4l1.6-1.6a2.8 2.8 0 0 1 4 0" fill="none" stroke="${C.blue}" stroke-width="1.7" stroke-linecap="round"/>`),
  "🔌": svg(`<path d="M11.2 6.2l2.4-2.4 1 1-2.4 2.4.9.9a1.2 1.2 0 0 1 0 1.7l-2.9 2.9a3.4 3.4 0 0 1-4.4.35L4.4 14.4l-1-1 1.4-1.4a3.4 3.4 0 0 1 .35-4.4l2.9-2.9a1.2 1.2 0 0 1 1.7 0z" fill="${C.amber}"/><path d="M6.6 7.6l1.2 1.2M8.4 5.8l1.2 1.2" stroke="${C.amberD}" stroke-width="1.1" stroke-linecap="round"/><path d="M2 2.6c1.2-.2 2 .2 2.4 1.4" stroke="${C.sky}" stroke-width="1.2" fill="none" stroke-linecap="round"/><path d="M1.2 5c.8-.1 1.3.15 1.6.9" stroke="${C.sky}" stroke-width="1.2" fill="none" stroke-linecap="round"/>`),
  "⚡": svg(`<path d="M9.2 1L3.4 9h3.2l-1 6L11.6 7H8.4z" fill="${C.amber}"/>`),
  "📡": svg(`<path d="M2 8.2A6.4 6.4 0 0 1 8.2 2l.5 2A4.4 4.4 0 0 0 4 8.7z" fill="${C.blue}"/><path d="M2.6 12.4l4-4a1.7 1.7 0 1 1 1 1l-4 4z" fill="${C.gray}"/><circle cx="11.6" cy="4.4" r="1.4" fill="${C.red}"/><path d="M9.8 1.2a7.6 7.6 0 0 1 5 5l-1.6.5a5.9 5.9 0 0 0-3.9-3.9z" fill="${C.sky}"/>`),
  "🎯": svg(`<circle cx="8" cy="8" r="6.4" fill="${C.red}"/><circle cx="8" cy="8" r="4.3" fill="#fff"/><circle cx="8" cy="8" r="2.3" fill="${C.red}"/>`),
  "💾": svg(`<path d="M2 3a1 1 0 0 1 1-1h8.6L14 4.4V13a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1z" fill="${C.blueD}"/><rect x="4.4" y="2" width="6" height="3.6" rx=".4" fill="${C.sky}"/><rect x="4" y="8" width="8" height="6" rx=".5" fill="${C.paper}"/>`),
  "🏢": svg(`<rect x="3" y="1.6" width="10" height="12.8" rx=".8" fill="${C.slateL}"/><g fill="${C.amber}"><rect x="4.8" y="3.4" width="1.8" height="1.8"/><rect x="9.4" y="3.4" width="1.8" height="1.8"/><rect x="4.8" y="6.6" width="1.8" height="1.8"/><rect x="9.4" y="6.6" width="1.8" height="1.8"/><rect x="4.8" y="9.8" width="1.8" height="1.8"/></g><rect x="9" y="10.4" width="2.6" height="4" fill="${C.dark}"/>`),
  "📍": svg(`<path d="M8 1.4a4.8 4.8 0 0 1 4.8 4.8C12.8 9.6 8 14.8 8 14.8S3.2 9.6 3.2 6.2A4.8 4.8 0 0 1 8 1.4z" fill="${C.red}"/><circle cx="8" cy="6.2" r="1.9" fill="#fff"/>`),
  "📊": svg(`<rect x="2" y="8.6" width="3" height="5.6" rx=".5" fill="${C.blue}"/><rect x="6.5" y="4.6" width="3" height="9.6" rx=".5" fill="${C.green}"/><rect x="11" y="2" width="3" height="12.2" rx=".5" fill="${C.amber}"/>`),
  "🤖": svg(`<rect x="2.6" y="4.6" width="10.8" height="8" rx="1.6" fill="${C.gray}"/><circle cx="6" cy="8" r="1.4" fill="${C.blue}"/><circle cx="10" cy="8" r="1.4" fill="${C.blue}"/><rect x="5.6" y="10.6" width="4.8" height="1.2" rx=".6" fill="${C.slate}"/><rect x="7.3" y="1.6" width="1.4" height="2.4" fill="${C.gray}"/><circle cx="8" cy="1.6" r="1" fill="${C.red}"/>`),
  "👤": svg(`<circle cx="8" cy="5" r="3.1" fill="${C.blue}"/><path d="M2.4 14.4a5.6 5.6 0 0 1 11.2 0z" fill="${C.blueD}"/>`),
  "👥": svg(`<circle cx="6" cy="5.4" r="2.7" fill="${C.blue}"/><path d="M1.2 13.8a4.8 4.8 0 0 1 9.6 0z" fill="${C.blueD}"/><circle cx="11.4" cy="5.8" r="2.2" fill="${C.purple}"/><path d="M9.6 13.8a4.2 4.2 0 0 1 5.6-4 4 4 0 0 1 .3 4z" fill="#7c5fd0" transform="translate(-.7 0)"/>`),
  "🛡️": svg(`<path d="M8 1.4l5.6 2v4.4c0 3.4-2.3 5.9-5.6 7-3.3-1.1-5.6-3.6-5.6-7V3.4z" fill="${C.blue}"/><path d="M8 1.4l5.6 2v4.4c0 3.4-2.3 5.9-5.6 7z" fill="${C.blueD}"/><path d="M5.4 7.8l1.9 1.9 3.4-3.4" stroke="#fff" stroke-width="1.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>`),
  "🔑": svg(`<circle cx="5" cy="5.6" r="3.4" fill="none" stroke="${C.amber}" stroke-width="1.8"/><path d="M7.4 8l6.4 6.4M11.6 12.2l1.8-1.8M9.6 10.2l1.6-1.6" stroke="${C.amber}" stroke-width="1.7" stroke-linecap="round"/>`),
  "🔐": svg(`<rect x="3.4" y="7" width="9.2" height="7.4" rx="1.2" fill="${C.amber}"/><path d="M5.4 7V5.2a2.6 2.6 0 0 1 5.2 0V7h-1.8V5.2a.8.8 0 0 0-1.6 0V7z" fill="${C.gray}"/><circle cx="8" cy="10.2" r="1.1" fill="${C.dark}"/><rect x="7.5" y="10.6" width="1" height="2" rx=".5" fill="${C.dark}"/>`),
  "🔔": svg(`<path d="M8 1.8a4.4 4.4 0 0 1 4.4 4.4c0 2.8.8 4 1.6 4.8H2c.8-.8 1.6-2 1.6-4.8A4.4 4.4 0 0 1 8 1.8z" fill="${C.amber}"/><path d="M6.3 12.4a1.8 1.8 0 0 0 3.4 0z" fill="${C.amberD}"/>`),
  "📌": svg(`<path d="M9.4 1.6l5 5-1.4 1.4-.7-.2-2.6 2.6.2 2-1.2 1.2-3-3-3.6 3.6-.8-.8L4.9 9.8l-3-3 1.2-1.2 2 .2 2.6-2.6-.2-.7z" fill="${C.red}"/>`),
  "📴": svg(`<circle cx="8" cy="8" r="6.5" fill="${C.red}"/><rect x="7.2" y="3.4" width="1.6" height="4.8" rx=".8" fill="#fff"/><path d="M5 5.4a4.2 4.2 0 1 0 6 0" fill="none" stroke="#fff" stroke-width="1.5" stroke-linecap="round"/>`),
  "⬆️": svg(`<path d="M8 1.8l5.4 5.4h-3.2v6.6H5.8V7.2H2.6z" fill="${C.green}"/>`),
  "⬇️": svg(`<path d="M8 14.2L2.6 8.8h3.2V2.2h4.4v6.6h3.2z" fill="${C.blue}"/>`),
  "⬇": svg(`<path d="M8 14.2L2.6 8.8h3.2V2.2h4.4v6.6h3.2z" fill="${C.blue}"/>`),
  "↗️": svg(`<rect x="2" y="5.6" width="8.4" height="8.4" rx="1.2" fill="none" stroke="${C.gray}" stroke-width="1.5"/><path d="M7.6 2h6.4v6.4h-2V5.4L7.6 9.8 6.2 8.4l4.4-4.4H7.6z" fill="${C.blue}"/>`),
  "➕": svg(`<rect x="6.7" y="2" width="2.6" height="12" rx="1.1" fill="${C.green}"/><rect x="2" y="6.7" width="12" height="2.6" rx="1.1" fill="${C.green}"/>`),
  "❌": svg(`<path d="M3.2 4.6L4.6 3.2 8 6.6l3.4-3.4 1.4 1.4L9.4 8l3.4 3.4-1.4 1.4L8 9.4l-3.4 3.4-1.4-1.4L6.6 8z" fill="${C.red}"/>`),
  "⚠️": svg(`<path d="M8 1.6L15.2 14H.8z" fill="${C.amber}"/><rect x="7.2" y="6" width="1.6" height="4.4" rx=".8" fill="${C.dark}"/><circle cx="8" cy="12.2" r="1" fill="${C.dark}"/>`),
  "⚠": svg(`<path d="M8 1.6L15.2 14H.8z" fill="${C.amber}"/><rect x="7.2" y="6" width="1.6" height="4.4" rx=".8" fill="${C.dark}"/><circle cx="8" cy="12.2" r="1" fill="${C.dark}"/>`),
  "🎥": svg(`<rect x="1.4" y="4.4" width="9" height="7.2" rx="1.2" fill="${C.slateL}"/><path d="M10.8 7.2l3.8-2.2v6l-3.8-2.2z" fill="${C.gray}"/><circle cx="4.6" cy="7" r="1.1" fill="${C.sky}"/>`),
  "🎮": svg(`<path d="M4.6 4.6h6.8a3.9 3.9 0 0 1 3.8 4.8l-.5 2.2a2 2 0 0 1-3.5.8L10 11H6l-1.2 1.4a2 2 0 0 1-3.5-.8l-.5-2.2a3.9 3.9 0 0 1 3.8-4.8z" fill="${C.purple}"/><rect x="4" y="6.8" width="2.6" height="1" rx=".5" fill="#fff"/><rect x="4.8" y="6" width="1" height="2.6" rx=".5" fill="#fff"/><circle cx="11" cy="6.6" r=".8" fill="${C.amber}"/><circle cx="12.4" cy="8" r=".8" fill="${C.green}"/>`),
  "🕹️": svg(`<rect x="7.2" y="4.4" width="1.6" height="6" fill="${C.gray}"/><circle cx="8" cy="3.6" r="2.2" fill="${C.red}"/><path d="M3 10.4h10l1 3.2H2z" fill="${C.slateL}"/>`),
  "🗄️": svg(`<rect x="2.6" y="1.8" width="10.8" height="12.4" rx="1" fill="${C.slateL}"/><rect x="4" y="3.4" width="8" height="4.4" rx=".5" fill="${C.slate}"/><rect x="4" y="8.6" width="8" height="4.4" rx=".5" fill="${C.slate}"/><rect x="6.4" y="4.8" width="3.2" height="1" rx=".5" fill="${C.gray}"/><rect x="6.4" y="10" width="3.2" height="1" rx=".5" fill="${C.gray}"/>`),
  "🧱": svg(`<g fill="#c2543f"><rect x="1.4" y="2.6" width="6.4" height="3.4"/><rect x="8.4" y="2.6" width="6.2" height="3.4"/><rect x="1.4" y="9.8" width="6.4" height="3.4"/><rect x="8.4" y="9.8" width="6.2" height="3.4"/></g><g fill="#a34432"><rect x="1.4" y="6.2" width="3" height="3.4"/><rect x="5" y="6.2" width="6.4" height="3.4"/><rect x="12" y="6.2" width="2.6" height="3.4"/></g>`),
  "🦠": svg(`<circle cx="8" cy="8" r="4.4" fill="${C.green}"/><g stroke="${C.green}" stroke-width="1.4" stroke-linecap="round"><path d="M8 1.4v2M8 12.6v2M1.4 8h2M12.6 8h2M3.4 3.4l1.4 1.4M11.2 11.2l1.4 1.4M12.6 3.4l-1.4 1.4M4.8 11.2l-1.4 1.4"/></g><circle cx="6.6" cy="7" r="1" fill="#0f8a5f"/><circle cx="9.4" cy="9.2" r=".8" fill="#0f8a5f"/>`),
  "🚀": svg(`<path d="M8 1.6a6.6 6.6 0 0 1 6.6 6.6c0 1.9-.8 3.6-2 4.8l-1.5-1.4A4.6 4.6 0 1 0 4.9 11.6l-1.5 1.4a6.6 6.6 0 0 1 4.6-11.4z" fill="${C.slateL}"/><path d="M8 8.9L11.8 4 8.9 9.5a1.1 1.1 0 1 1-.9-.6z" fill="${C.red}"/><circle cx="8" cy="9.4" r=".9" fill="${C.red}"/><path d="M3.2 8.2h1.2M11.6 8.2h1.2M8 3v1.2" stroke="${C.paper}" stroke-width="1" stroke-linecap="round"/>`),
  "🌀": svg(`<path d="M8 1.6a6.4 6.4 0 1 1-6.4 6.4h2.2A4.2 4.2 0 1 0 8 3.8a2.1 2.1 0 1 0 0 4.2 1 1 0 1 1 0 2 4.1 4.1 0 1 1 0-8.2z" fill="${C.sky}"/>`),
  "🟠": svg(`<circle cx="8" cy="8" r="5.6" fill="#f59e0b"/>`),
  // --- module / app identity icons (added for the icon pass) ---
  "⌨️": svg(`<rect x="1" y="4" width="14" height="8.4" rx="1.3" fill="${C.slate}"/><g fill="${C.sky}"><rect x="2.4" y="5.4" width="1.4" height="1.4" rx=".3"/><rect x="4.4" y="5.4" width="1.4" height="1.4" rx=".3"/><rect x="6.4" y="5.4" width="1.4" height="1.4" rx=".3"/><rect x="8.4" y="5.4" width="1.4" height="1.4" rx=".3"/><rect x="10.4" y="5.4" width="1.4" height="1.4" rx=".3"/><rect x="2.4" y="7.6" width="1.4" height="1.4" rx=".3"/><rect x="4.4" y="7.6" width="1.4" height="1.4" rx=".3"/><rect x="6.4" y="7.6" width="1.4" height="1.4" rx=".3"/><rect x="8.4" y="7.6" width="3.4" height="1.4" rx=".3"/><rect x="4.4" y="9.8" width="7" height="1.4" rx=".3"/></g>`),
  "⏰": svg(`<circle cx="8" cy="9" r="5.2" fill="${C.red}"/><circle cx="8" cy="9" r="3.8" fill="${C.paper}"/><path d="M3.4 2.6l2.2 2M12.6 2.6l-2.2 2" stroke="${C.red}" stroke-width="1.6" stroke-linecap="round"/><path d="M8 9V6.4M8 9l2 1.2" stroke="${C.dark}" stroke-width="1" stroke-linecap="round"/>`),
  "⏻": svg(`<path d="M8 2v5.4" stroke="${C.green}" stroke-width="1.8" stroke-linecap="round"/><path d="M4.6 5.2a5 5 0 1 0 6.8 0" fill="none" stroke="${C.green}" stroke-width="1.8" stroke-linecap="round"/>`),
  "☁️": svg(`<path d="M4.6 12.6a3.3 3.3 0 0 1-.5-6.56 4.1 4.1 0 0 1 7.9 1.05 2.8 2.8 0 0 1-.3 5.51z" fill="${C.sky}"/>`),
  "✅": svg(`<circle cx="8" cy="8" r="6.4" fill="${C.green}"/><path d="M4.8 8.2l2.2 2.2 4.2-4.4" stroke="#fff" stroke-width="1.7" fill="none" stroke-linecap="round" stroke-linejoin="round"/>`),
  "🌐": svg(`<circle cx="8" cy="8" r="6.3" fill="${C.blue}"/><g fill="none" stroke="#dff0ff" stroke-width=".9"><ellipse cx="8" cy="8" rx="2.6" ry="6.3"/><path d="M1.8 6.2h12.4M1.8 9.8h12.4M8 1.7v12.6"/></g>`),
  "🌍": svg(`<circle cx="8" cy="8" r="6.3" fill="${C.blue}"/><path d="M3.6 6.4c1.3-.5 2 .4 3 .2s1.1-1.4 2.3-1.1.9 1.6 1.9 1.8 1.3-.3 1.7.2c-.2 2.2-1.3 3-2.3 3.4s-1 1.6-2.2 1.8-1.6-.6-2.3-1.4-1.6-.9-1.6-2.3-.2-2.3.8-2.4z" fill="${C.green}"/>`),
  "🎨": svg(`<path d="M8 2.2c3.5 0 6.3 2.4 6.3 5.3 0 1.9-1.7 2.7-3 2.7h-1.2c-.9 0-1.5.8-1.1 1.7.3.7-.1 1.6-1 1.6C4.6 13.5 1.7 10.9 1.7 7.5 1.7 4.5 4.5 2.2 8 2.2z" fill="${C.amber}"/><circle cx="5.4" cy="7" r="1" fill="${C.red}"/><circle cx="8" cy="5.2" r="1" fill="${C.blue}"/><circle cx="10.6" cy="6.4" r="1" fill="${C.green}"/>`),
  "🎫": svg(`<path d="M1.6 5.2a1 1 0 0 1 1-1h10.8a1 1 0 0 1 1 1V6a1.3 1.3 0 0 0 0 4v.8a1 1 0 0 1-1 1H2.6a1 1 0 0 1-1-1V10a1.3 1.3 0 0 0 0-4z" fill="${C.amber}"/><path d="M6 4.6v6.8" stroke="${C.amberD}" stroke-width="1" stroke-dasharray="1.2 1"/>`),
  "🎬": svg(`<rect x="1.6" y="6" width="12.8" height="8" rx="1" fill="${C.slate}"/><rect x="1.6" y="3.4" width="12.8" height="3" rx=".6" fill="${C.slateL}"/><path d="M3 3.4L4.6 6.4M6 3.4L7.6 6.4M9 3.4L10.6 6.4M12 3.4L13.6 6.4" stroke="${C.paper}" stroke-width="1"/>`),
  "🎵": svg(`<path d="M6 3.2l6-1.2v8.3a2 2 0 1 1-1.4-1.9V4.2L7.4 5v6.1A2 2 0 1 1 6 9.2z" fill="${C.blue}"/>`),
  "🏛️": svg(`<path d="M8 1.6l6 3v1.2H2V4.6z" fill="${C.paperD}"/><g fill="${C.paper}"><rect x="3" y="6.6" width="1.6" height="5.4"/><rect x="6" y="6.6" width="1.6" height="5.4"/><rect x="8.8" y="6.6" width="1.6" height="5.4"/><rect x="11.4" y="6.6" width="1.6" height="5.4"/></g><rect x="2" y="12.4" width="12" height="1.8" rx=".3" fill="${C.paperD}"/>`),
  "🏰": svg(`<path d="M2 6h2V4h1.6v2h1.6V4h1.6v2h1.6V4H14v2h-.4v8H2z" fill="${C.slateL}"/><rect x="6.6" y="9" width="2.8" height="5" fill="${C.dark}"/>`),
  "🐳": svg(`<path d="M2 8c0-2.2 2-4 4.6-4S11 5.8 11 8c1.4 0 2.6-.6 3.4-1.4-.2 1.6-1 2.8-2.2 3.6.6.3 1.3.4 2 .4-1 1-2.6 1.6-4.4 1.6H6.6C4 12.2 2 10.4 2 8z" fill="${C.blue}"/><circle cx="5.4" cy="7.4" r=".8" fill="#fff"/><path d="M6.6 3.2c0 1 .4 1.4 1 1.8" stroke="${C.sky}" stroke-width="1" fill="none" stroke-linecap="round"/>`),
  "💬": svg(`<path d="M2 4.2A1.8 1.8 0 0 1 3.8 2.4h8.4A1.8 1.8 0 0 1 14 4.2v4.6a1.8 1.8 0 0 1-1.8 1.8H6l-3 2.6v-2.6h-.2A1.8 1.8 0 0 1 2 8.8z" fill="${C.blue}"/>`),
  "📞": svg(`<path d="M3.4 2.2l2.4.4 1 2.8-1.6 1.2a8 8 0 0 0 3.8 3.8l1.2-1.6 2.8 1 .4 2.4a1.3 1.3 0 0 1-1.4 1.2A11.4 11.4 0 0 1 2.2 3.6 1.3 1.3 0 0 1 3.4 2.2z" fill="${C.green}"/>`),
  "📦": svg(`<path d="M8 1.8l6 3v6.4l-6 3-6-3V4.8z" fill="#c8903f"/><path d="M8 1.8l6 3-6 3-6-3z" fill="#e0a85a"/><path d="M8 7.8v6.4l6-3V4.8z" fill="#b07c33"/><path d="M2 4.8l6 3M8 7.8v6.4" stroke="#8a5f23" stroke-width=".6"/>`),
  "🔀": svg(`<path d="M2 11.6h2.6c1.6 0 2.8-1.4 4-3 .9-1.2 1.8-2.2 3-2.2H14" fill="none" stroke="${C.blue}" stroke-width="1.5" stroke-linecap="round"/><path d="M2 4.4h2.6c1 0 1.8.5 2.4 1.3" fill="none" stroke="${C.blue}" stroke-width="1.5" stroke-linecap="round"/><path d="M12 4.6l2 1.8-2 1.8M12 12.6l2-1.8-2-1.8" fill="none" stroke="${C.blue}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>`),
  "🕵️": svg(`<path d="M3 6.4C3 4 5.2 2.4 8 2.4s5 1.6 5 4L14 7H2z" fill="${C.dark}"/><ellipse cx="8" cy="7" rx="7" ry="1.3" fill="${C.slate}"/><circle cx="6" cy="10.4" r="2.4" fill="none" stroke="${C.gray}" stroke-width="1.2"/><path d="M7.7 12.1l2 2" stroke="${C.gray}" stroke-width="1.4" stroke-linecap="round"/>`),
  "🖧": svg(`<rect x="6" y="1.6" width="4" height="3.2" rx=".5" fill="${C.slateL}"/><rect x="1.4" y="11.2" width="4" height="3.2" rx=".5" fill="${C.slateL}"/><rect x="6" y="11.2" width="4" height="3.2" rx=".5" fill="${C.slateL}"/><rect x="10.6" y="11.2" width="4" height="3.2" rx=".5" fill="${C.slateL}"/><path d="M8 4.8v3.4M3.4 11.2v-3h9.2v3M8 8.2v3" stroke="${C.blue}" stroke-width="1.1" fill="none"/>`),
  "🗒️": svg(`<rect x="2.6" y="2.6" width="10.8" height="11.4" rx="1" fill="${C.paper}"/><rect x="2.6" y="2.6" width="10.8" height="2.2" fill="${C.red}"/><g stroke="${C.grayD}" stroke-width="1"><path d="M4.6 7.4h6.8M4.6 9.6h6.8M4.6 11.8h4.4"/></g>`),
  "🗓️": svg(`<rect x="2" y="3" width="12" height="11" rx="1.1" fill="${C.paper}"/><rect x="2" y="3" width="12" height="3" rx="1.1" fill="${C.red}"/><rect x="4.4" y="1.4" width="1.4" height="3" rx=".6" fill="${C.grayD}"/><rect x="10.2" y="1.4" width="1.4" height="3" rx=".6" fill="${C.grayD}"/><g fill="${C.slateL}"><rect x="4" y="7.6" width="1.6" height="1.6"/><rect x="7.2" y="7.6" width="1.6" height="1.6"/><rect x="10.4" y="7.6" width="1.6" height="1.6"/><rect x="4" y="10.4" width="1.6" height="1.6"/><rect x="7.2" y="10.4" width="1.6" height="1.6"/></g>`),
  "🗝️": svg(`<circle cx="4.6" cy="4.6" r="3.1" fill="${C.amberD}"/><circle cx="4.6" cy="4.6" r="1.1" fill="${C.dark}"/><path d="M6.6 6.6l6.6 6.6" stroke="${C.amberD}" stroke-width="1.7" stroke-linecap="round"/><path d="M11 11l1.4-1.4M12.2 12.2l1.2-1.2" stroke="${C.amberD}" stroke-width="1.5" stroke-linecap="round"/>`),
  "🗺️": svg(`<path d="M1.6 3.4L6 2v11l-4.4 1.4zM6 2l4 1.4v11L6 13zM10 3.4L14.4 2v11L10 14.4z" fill="${C.green}"/><path d="M6 2v11M10 3.4v11" stroke="#1f7a4d" stroke-width=".6"/><circle cx="7.8" cy="7" r="1.1" fill="${C.red}"/>`),
  "🛉": svg(`<path d="M3 2h.4a2 2 0 0 1 2 2v1.4" fill="none" stroke="${C.gray}" stroke-width="1.4" stroke-linecap="round"/><path d="M2.6 6.2a2.8 2.8 0 0 1 5.6 0z" fill="${C.slateL}"/><g stroke="${C.sky}" stroke-width="1" stroke-linecap="round"><path d="M4 8.4v1.6M5.4 8.4v2.4M6.8 8.4v1.6M3.2 11v1.4M5.4 12.2v1.4M7 11v1.4"/></g>`),
  "🤝": svg(`<path d="M1 7l3-2.4 3.4 2.2 3-2 4 2.4v3.4l-2.6 2.2-3-2-2.4 1.8L3 10.4z" fill="${C.amber}"/><path d="M7.4 6.8L9 8.2M9 8.8l1.4 1.2" stroke="${C.amberD}" stroke-width="1" stroke-linecap="round"/>`),
  "🧪": svg(`<path d="M6 2h4v1.4H9.4v7.2a1.9 1.9 0 0 1-3.8 0V3.4H5V2z" fill="${C.paper}"/><path d="M5.6 8h3.8v2.6a1.9 1.9 0 0 1-3.8 0z" fill="${C.green}"/>`),
  "🧭": svg(`<circle cx="8" cy="8" r="6.3" fill="${C.slateL}"/><circle cx="8" cy="8" r="4.6" fill="${C.dark}"/><path d="M8 8l3-4.4L8.8 8z" fill="${C.red}"/><path d="M8 8l-3 4.4L7.2 8z" fill="#cfd8e8"/>`),
  "🧮": svg(`<rect x="1.8" y="2" width="12.4" height="12" rx="1" fill="none" stroke="${C.amberD}" stroke-width="1.3"/><path d="M2 6h12M2 10h12" stroke="${C.amberD}" stroke-width="1"/><g fill="${C.red}"><circle cx="4.4" cy="4" r="1.1"/><circle cx="7" cy="4" r="1.1"/></g><g fill="${C.blue}"><circle cx="9.6" cy="8" r="1.1"/><circle cx="12" cy="8" r="1.1"/></g><g fill="${C.green}"><circle cx="4.4" cy="12" r="1.1"/><circle cx="7" cy="12" r="1.1"/></g>`),
  "🧰": svg(`<path d="M2 6h12v7a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1z" fill="${C.red}"/><rect x="1.4" y="6" width="13.2" height="2.2" fill="#d94a4a"/><path d="M5.4 6V4.6a1 1 0 0 1 1-1h3.2a1 1 0 0 1 1 1V6H9V5h-2v1z" fill="${C.slateL}"/><rect x="6.8" y="7" width="2.4" height="1.8" rx=".3" fill="${C.amber}"/>`),
  "🩹": svg(`<rect x="2.2" y="6" width="11.6" height="4" rx="2" fill="#e8c39a" transform="rotate(-30 8 8)"/><rect x="5.6" y="6.4" width="4.8" height="3.2" rx=".6" fill="#f3ddc2" transform="rotate(-30 8 8)"/><g fill="#c99b6a"><circle cx="8" cy="8" r=".5"/><circle cx="6.6" cy="8.8" r=".5"/><circle cx="9.4" cy="7.2" r=".5"/><circle cx="7" cy="6.8" r=".5"/><circle cx="9" cy="9.2" r=".5"/></g>`),
  "🧩": svg(`<path d="M3 3h3.2a1.2 1.2 0 0 1 2.4 0H12v3.6a1.2 1.2 0 0 0 0 2.4V13H8.4a1.2 1.2 0 0 0-2.4 0H3V9.4a1.2 1.2 0 0 1 0-2.4z" fill="${C.green}"/>`),
  "🐧": svg(`<ellipse cx="8" cy="8.6" rx="4.4" ry="5.6" fill="${C.dark}"/><ellipse cx="8" cy="9.4" rx="2.8" ry="4.2" fill="${C.paper}"/><circle cx="6.4" cy="5.4" r=".8" fill="#fff"/><circle cx="9.6" cy="5.4" r=".8" fill="#fff"/><circle cx="6.4" cy="5.6" r=".4" fill="${C.dark}"/><circle cx="9.6" cy="5.6" r=".4" fill="${C.dark}"/><path d="M6.6 6.6h2.8l-1.4 1.4z" fill="${C.amber}"/><path d="M5.8 13.6l1.4-1M10.2 13.6l-1.4-1" stroke="${C.amber}" stroke-width="1.4" stroke-linecap="round"/>`),
  "🍎": svg(`<path d="M8 4.4c-1-1-2.6-1.2-3.8-.3C2.8 5.2 2.6 7.8 3.6 10c.7 1.6 1.8 3 3 3 .7 0 1-.3 1.4-.3s.7.3 1.4.3c1.2 0 2.3-1.4 3-3 1-2.2.8-4.8-.6-5.9-1.2-.9-2.8-.7-3.8.3z" fill="${C.red}"/><path d="M8 4.4V2.6c0-.8.7-1.4 1.6-1.4" fill="none" stroke="#4a7a2a" stroke-width="1.1" stroke-linecap="round"/>`),
  "📂": svg(`<path d="M1 4a1.4 1.4 0 0 1 1.4-1.4h3l1.4 1.5h5.8A1.4 1.4 0 0 1 15 5.5V6H3.4L1.4 11z" fill="${C.amber}"/><path d="M3.6 6.4h11.8l-1.9 6.1a1 1 0 0 1-1 .5H2.4a1 1 0 0 1-1-1.3z" fill="#f5c542"/>`),
  "📈": svg(`<path d="M2 2v12h12" fill="none" stroke="${C.grayD}" stroke-width="1.3" stroke-linecap="round"/><path d="M4 10.5l3-3 2.4 1.6L13 4.5" fill="none" stroke="${C.green}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/><path d="M10.4 4.5H13v2.6" fill="none" stroke="${C.green}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>`),
  "🔒": svg(`<rect x="3.4" y="7" width="9.2" height="7" rx="1.3" fill="${C.amber}"/><path d="M5.2 7V5.2a2.8 2.8 0 0 1 5.6 0V7H9V5.2a1 1 0 0 0-2 0V7z" fill="${C.gray}"/><circle cx="8" cy="10.2" r="1.2" fill="${C.dark}"/>`),
  "🔓": svg(`<rect x="3.4" y="7" width="9.2" height="7" rx="1.3" fill="${C.amber}"/><path d="M5.2 7V5.2a2.8 2.8 0 0 1 5.6 0H9a1 1 0 0 0-2 0V7z" fill="${C.gray}"/><circle cx="8" cy="10.2" r="1.2" fill="${C.dark}"/>`),
  "👁": svg(`<path d="M1 8s2.6-4.4 7-4.4S15 8 15 8s-2.6 4.4-7 4.4S1 8 1 8z" fill="${C.paper}"/><circle cx="8" cy="8" r="2.4" fill="${C.blue}"/><circle cx="8" cy="8" r="1" fill="${C.dark}"/>`),
  "🛠️": svg(`<path d="M10.5 2.5a2.6 2.6 0 0 0-1 4.2l-6 6 1.6 1.6 6-6a2.6 2.6 0 0 0 3.4-3.2L12.2 7 11 5.8 13 3.7A2.6 2.6 0 0 0 10.5 2.5z" fill="${C.slateL}"/><path d="M2.8 11.2l2 2" stroke="${C.amber}" stroke-width="2" stroke-linecap="round"/>`),
  "🔧": svg(`<path d="M10.6 2a3.2 3.2 0 0 0-1.1 5.3L3 13.8l1.6 1.6 6.5-6.5A3.2 3.2 0 0 0 14 5.4l-1.8 1.8-1.4-1.4L12.6 4A3.2 3.2 0 0 0 10.6 2z" fill="${C.slateL}"/>`),
  "🔨": svg(`<path d="M3 13.4l5-5 1.6 1.6-5 5a1.1 1.1 0 0 1-1.6-1.6z" fill="${C.amberD}"/><path d="M8.6 3.4l4 4-1.4 1.4-1-1-2 2-1.6-1.6 2-2-1-1z" fill="${C.slateL}"/>`),
  "🎉": svg(`<path d="M2 14l3.4-9 5.6 5.6z" fill="${C.amber}"/><circle cx="12" cy="3" r="1" fill="${C.red}"/><circle cx="13.6" cy="6" r=".9" fill="${C.blue}"/><circle cx="9.6" cy="2.2" r=".9" fill="${C.green}"/><rect x="10.8" y="7.6" width="1.4" height="1.4" fill="${C.purple}" transform="rotate(20 11.5 8.3)"/>`),
  "🟢": svg(`<circle cx="8" cy="8" r="5.6" fill="${C.green}"/>`),
  "📱": svg(`<rect x="4" y="1.4" width="8" height="13.2" rx="1.6" fill="${C.slate}"/><rect x="5" y="3" width="6" height="9" rx=".4" fill="${C.sky}"/><circle cx="8" cy="13.2" r=".7" fill="${C.slateL}"/>`),
  "🩺": svg(`<path d="M4 2v3.4a3 3 0 0 0 6 0V2" fill="none" stroke="${C.gray}" stroke-width="1.4" stroke-linecap="round"/><path d="M7 8.4v2.2a3.4 3.4 0 0 0 6.8 0v-1" fill="none" stroke="${C.gray}" stroke-width="1.4" stroke-linecap="round"/><circle cx="13.8" cy="8.6" r="1.5" fill="${C.blue}"/>`),
  "🚚": svg(`<rect x="1" y="4.4" width="8" height="6" rx=".6" fill="${C.blue}"/><path d="M9 6h3l2.4 2.4v2H9z" fill="${C.sky}"/><circle cx="4.4" cy="11.4" r="1.6" fill="${C.dark}"/><circle cx="11.6" cy="11.4" r="1.6" fill="${C.dark}"/>`),
  "🛟": svg(`<circle cx="8" cy="8" r="6.2" fill="${C.red}"/><g stroke="#fff" stroke-width="2"><path d="M8 2v3M8 11v3M2 8h3M11 8h3"/></g><circle cx="8" cy="8" r="2.6" fill="${C.paper}"/>`),
  "📶": svg(`<g fill="${C.green}"><rect x="1.6" y="10" width="2.4" height="3.4" rx=".4"/><rect x="5" y="7.6" width="2.4" height="5.8" rx=".4"/><rect x="8.4" y="5.2" width="2.4" height="8.2" rx=".4"/><rect x="11.8" y="2.6" width="2.4" height="10.8" rx=".4"/></g>`),
};
// VS16 / no-VS16 glyph aliases (same picture for both forms)
ICONS["🗑️"] = ICONS["🗑"];
ICONS["⌨"] = ICONS["⌨️"];
ICONS["☁"] = ICONS["☁️"];
ICONS["🏛"] = ICONS["🏛️"];
ICONS["🕵"] = ICONS["🕵️"];
ICONS["🗒"] = ICONS["🗒️"];
ICONS["🗓"] = ICONS["🗓️"];
ICONS["🗝"] = ICONS["🗝️"];
ICONS["🗺"] = ICONS["🗺️"];
ICONS["🛠"] = ICONS["🛠️"];
ICONS["👁️"] = ICONS["👁"];

// ---- mode ----
export function getIconMode() {
  const v = localStorage.getItem(KEY);
  return v === "emoji" ? "emoji" : "svg";   // default: SVG
}
export function setIconMode(mode) {
  localStorage.setItem(KEY, mode === "emoji" ? "emoji" : "svg");
  // Also persist the icon mode server-side (identical in every browser).
  import("./persist.js").then((m) => m.syncToServerSoon()).catch(() => {});
  if (mode === "emoji") { stop(); revertAll(); }
  else { scan(document.body); start(); }
}

// ---- replace / revert ----
const EMOJI_RE = new RegExp(
  "(" + Object.keys(ICONS).sort((a, b) => b.length - a.length)
        .map((e) => e.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")", "g");

const SKIP = new Set(["INPUT", "TEXTAREA", "SELECT", "OPTION", "SCRIPT", "STYLE", "SVG", "CANVAS"]);

function skippable(el) {
  for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
    if (SKIP.has(n.tagName)) return true;
    if (n.isContentEditable) return true;
    if (n.classList) {
      // Never touch payload: terminal output & xterm
      if (n.classList.contains("xterm") || n.classList.contains("terminal-output") ||
          n.classList.contains("svgicon")) return true;
    }
  }
  return false;
}

function replaceTextNode(node) {
  const text = node.nodeValue;
  if (!text || !EMOJI_RE.test(text)) { EMOJI_RE.lastIndex = 0; return; }
  EMOJI_RE.lastIndex = 0;
  const frag = document.createDocumentFragment();
  let last = 0, m;
  while ((m = EMOJI_RE.exec(text))) {
    if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
    const span = document.createElement("span");
    span.className = "svgicon";
    span.dataset.ch = m[1];
    span.innerHTML = ICONS[m[1]];
    frag.appendChild(span);
    last = m.index + m[1].length;
  }
  if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
  node.parentNode?.replaceChild(frag, node);
}

function scan(root) {
  if (!root) return;
  if (root.nodeType === 3) {
    if (root.parentElement && !skippable(root.parentElement)) replaceTextNode(root);
    return;
  }
  if (root.nodeType !== 1 || skippable(root)) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => (n.parentElement && !skippable(n.parentElement))
      ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT,
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  nodes.forEach(replaceTextNode);
}

function revertAll() {
  document.querySelectorAll("span.svgicon").forEach((s) =>
    s.replaceWith(document.createTextNode(s.dataset.ch || "")));
}

// ---- observer (batched) ----
let observer = null;
let pending = new Set();
let raf = 0;
function flush() {
  raf = 0;
  const roots = [...pending]; pending.clear();
  roots.forEach((r) => { if (r.isConnected || r === document.body) scan(r); });
}
function start() {
  if (observer) return;
  observer = new MutationObserver((muts) => {
    for (const m of muts) {
      if (m.type === "characterData") pending.add(m.target);
      for (const n of m.addedNodes) pending.add(n);
    }
    if (pending.size && !raf) raf = requestAnimationFrame(flush);
  });
  observer.observe(document.body, { childList: true, subtree: true, characterData: true });
}
function stop() {
  observer?.disconnect(); observer = null;
  pending.clear(); if (raf) { cancelAnimationFrame(raf); raf = 0; }
}

// ---- init ----
function init() {
  if (getIconMode() === "svg") { scan(document.body); start(); }
}
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
else init();
