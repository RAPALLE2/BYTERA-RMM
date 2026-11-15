// styles.js (Core) - Andockstellen fuer Oberflaechen-Styles
// -----------------------------------------------------------
// Der Core bringt genau EINEN Style mit: "futuristic" (Desktop mit Fenstern).
// Alle weiteren Styles (Classic, Classic Hard, eigene) kommen aus dem Modul
// "styles" (src/modules/styles, siehe dessen README.md). Damit ein Style die
// Oberflaeche umbauen kann, ohne dass der Core ihn kennt, gibt es hier drei
// Andockstellen:
//
//   setMainContentRenderer(fn)  fn(el) zeichnet den Hauptbereich (Dashboard /
//                               Auswahl) selbst; Rueckgabe false = Core zeichnet
//   onMetrics(fn)               fn(clientId) bei neuen Live-Werten eines Clients,
//                               Rueckgabe > 0 = "habe aktualisiert"
//   onWindowsChanged(fn)        fn() wenn sich die Fensterliste aendert
//
// applyBaseStyle(id) setzt body[data-style] und die Klasse design-futuristic.
// Fehler in einem Haken werden geschluckt (Konsole) - ein kaputter Style darf
// die Oberflaeche nicht lahmlegen.

let _mainRenderer = null;
const _metrics = new Set();
const _windows = new Set();

export function applyBaseStyle(id) {
  const style = id || "futuristic";
  document.body.dataset.style = style;
  document.body.classList.toggle("design-futuristic", style === "futuristic");
}

export function setMainContentRenderer(fn) { _mainRenderer = typeof fn === "function" ? fn : null; }
export function onMetrics(fn) { if (typeof fn === "function") _metrics.add(fn); }
export function onWindowsChanged(fn) { if (typeof fn === "function") _windows.add(fn); }

/** true = ein Style hat den Hauptbereich gezeichnet */
export function renderMainByStyle(el) {
  if (!_mainRenderer) return false;
  try { return _mainRenderer(el) !== false; }
  catch (e) { console.error("[styles] Hauptbereich:", e); return false; }
}

export function runMetricsHooks(clientId) {
  let n = 0;
  for (const fn of _metrics) {
    try { n += Number(fn(clientId)) || 0; } catch (e) { console.error("[styles] metrics:", e); }
  }
  return n;
}

export function runWindowsHooks() {
  for (const fn of _windows) {
    try { fn(); } catch (e) { console.error("[styles] windows:", e); }
  }
}

// ---------------------------------------------------------------- Auswahl
const CORE_STYLE = { id: "futuristic", name: "Futuristisch", description: "Standard: Desktop mit Fenstern" };

/** Alle waehlbaren Styles. Ohne Modul "styles" nur der Core-Standard. */
export async function listStyles() {
  try {
    const tok = localStorage.getItem("rmm_token") || "";
    const r = await fetch("/api/styles", { headers: tok ? { Authorization: `Bearer ${tok}` } : {}, cache: "no-store" });
    if (!r.ok) return [CORE_STYLE];
    const d = await r.json();
    const list = (d.styles || []).filter((s) => s && s.id);
    return list.some((s) => s.id === "futuristic") ? list : [CORE_STYLE, ...list];
  } catch {
    return [CORE_STYLE];
  }
}

/** <select> mit allen Styles fuellen.
 *  opts.current    = gewaehlte ID ("" = Standard der Installation)
 *  opts.withDefault = Eintrag "Standard der Installation (<name>)" voranstellen
 *  opts.defaultId   = ID des Installations-Standards (fuer dessen Namen) */
export async function fillStyleSelect(sel, { current = "", withDefault = false, defaultId = "futuristic" } = {}) {
  if (!sel) return;
  const list = await listStyles();
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const name = (id) => list.find((s) => s.id === id)?.name || "Futuristisch";
  let html = withDefault ? `<option value="">Standard der Installation (${esc(name(defaultId))})</option>` : "";
  html += list.map((s) => `<option value="${esc(s.id)}" title="${esc(s.description || "")}">${esc(s.name)}${s.description ? " – " + esc(s.description) : ""}</option>`).join("");
  // gespeicherte, aber gerade nicht verfuegbare Wahl sichtbar lassen
  if (current && !list.some((s) => s.id === current)) {
    html += `<option value="${esc(current)}">${esc(current)} (nicht verfügbar – Modul „Styles“ aus?)</option>`;
  }
  sel.innerHTML = html;
  sel.value = current || (withDefault ? "" : "futuristic");
}
