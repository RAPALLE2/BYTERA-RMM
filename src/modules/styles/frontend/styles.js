// styles.js (Modul "styles") - laedt den Style des angemeldeten Benutzers
// ------------------------------------------------------------------------
// Der Core kennt nur "futuristic" (Standard). Alle anderen Styles liegen in
// diesem Modul unter styles/<ordner>/style.json - siehe ../README.md.
//
// Ablauf nach dem Login (init wird von modules.js aufgerufen):
//   1. gewuenschten Style ermitteln (state.user.design, vom Backend aufgeloest)
//   2. dessen CSS-Dateien einbinden, Body-Klassen setzen
//   3. Layout-Engine starten ("classic" | "classic-hard" | "none")
//   4. optionales eigenes Script des Styles (style.json "js") ausfuehren
// Fehler in einem Style fallen auf "futuristic" zurueck, statt die Oberflaeche
// zu blockieren.

import { state } from "/js/app/state.js";
import { applyBaseStyle } from "/js/app/styles.js";

function addCss(href) {
  if (document.querySelector(`link[data-style-css="${CSS.escape(href)}"]`)) return;
  const l = document.createElement("link");
  l.rel = "stylesheet";
  l.href = href;
  l.dataset.styleCss = href;
  document.head.appendChild(l);
}

export async function init({ module, req }) {
  const want = state.user?.design || "futuristic";
  if (want === "futuristic") return;
  let style = null;
  try {
    const res = await req("/api/styles");
    style = (res.styles || []).find((s) => s.id === want) || null;
  } catch (e) {
    console.warn("[styles] Liste nicht ladbar:", e);
  }
  if (!style || style.builtin) { applyBaseStyle("futuristic"); return; }

  const base = `${module.base}styles/${style.dir}/`;
  try {
    applyBaseStyle(style.id);
    for (const cls of String(style.bodyClass || "").split(/\s+/).filter(Boolean)) document.body.classList.add(cls);
    if (style.layout === "classic" || style.layout === "classic-hard") addCss(`${module.base}engine/layout.css`);
    for (const c of style.css || []) addCss(new URL(c, location.origin + base).pathname);

    if (style.layout === "classic" || style.layout === "classic-hard") {
      const eng = await import(`${module.base}engine/classic.js`);
      await eng.start({ hard: style.layout === "classic-hard" });
    }
    if (style.js) {
      const mod = await import(new URL(style.js, location.origin + base).pathname);
      if (typeof mod.apply === "function") await mod.apply({ style, base, module, req });
    }
  } catch (e) {
    console.error(`[styles] Style "${style.id}" fehlgeschlagen - zurueck zu Futuristisch:`, e);
    window.notify?.(`Style "${style.name}" konnte nicht geladen werden: ${e.message}`, "error", 12000);
    for (const cls of String(style.bodyClass || "").split(/\s+/).filter(Boolean)) document.body.classList.remove(cls);
    applyBaseStyle("futuristic");
  }
}
