import { t } from "./i18n.js";
// theme.js
// --------
// Controls two aspects of the appearance:
//   1. Light/dark mode (class "theme-light" on <body>)
//   2. Accent color palette (overrides --accent and --accent-2 via inline style
//      on the <html> element, so it overrides the values from style.css)

// Available color palettes. Each has a primary (accent) and a secondary color
// (accent2) for gradients. "name" is shown in the profile.
export const ACCENT_PALETTES = {
  teal:    { name: t("u_turkis"),   accent: "#2dd4bf", accent2: "#38bdf8" },
  violet:  { name: "Violet",   accent: "#a78bfa", accent2: "#f472b6" },
  blue:    { name: "Blue",     accent: "#3b82f6", accent2: "#06b6d4" },
  emerald: { name: "Emerald",  accent: "#34d399", accent2: "#a3e635" },
  amber:   { name: "Amber",    accent: "#f59e0b", accent2: "#f97316" },
  rose:    { name: "Rose",     accent: "#fb7185", accent2: "#f472b6" },
  crimson: { name: "Crimson",  accent: "#ef4444", accent2: "#f97316" },
  slate:   { name: "Slate",    accent: "#94a3b8", accent2: "#64748b" },
};

export function applyTheme(theme) {
  if (theme === "light") {
    document.body.classList.add("theme-light");
  } else {
    document.body.classList.remove("theme-light");
  }
}

// Sets the accent color palette. The key must exist in ACCENT_PALETTES;
// unknown values fall back to "teal".
export function applyAccent(accentKey) {
  const palette = ACCENT_PALETTES[accentKey] || ACCENT_PALETTES.teal;
  const root = document.documentElement;
  root.style.setProperty("--accent", palette.accent);
  root.style.setProperty("--accent-2", palette.accent2);
  // Also as an "R, G, B" triple, so the CSS can use rgba(var(--accent-2-rgb), .15)
  // for semi-transparent backgrounds (sidebar selection, prioritized taskbar
  // window).
  const toRgb = (hex) => {
    const h = hex.replace("#", "");
    const n = parseInt(h.length === 3 ? h.split("").map((c) => c + c).join("") : h, 16);
    return `${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}`;
  };
  root.style.setProperty("--accent-rgb", toRgb(palette.accent));
  root.style.setProperty("--accent-2-rgb", toRgb(palette.accent2));
}
