// picker.js - gemeinsame Auswahl-Komponente im VPN-Stil (Filter + scrollbare
// Liste mit Icons), wie sie beim VPN-Tunnel fuer Benutzer/Agents/Nodes benutzt
// wird. Damit sehen Auswahlen ueberall gleich aus (nicht "alles als Button").
//
//   pickEntity(title, groups, opts) -> Promise<id | id[] | null>
//     groups: [{ title, icon?, items: [{ id, name, icon?, sub?, disabled? }] }]
//     opts.multi: Mehrfachauswahl (Checkboxen) statt Einzelauswahl.
//     opts.selected: vorausgewaehlte ids (Array).
//     Rueckgabe: bei multi ein Array von ids, sonst eine id; null = Abbruch.
//
//   entityListHtml / wireEntityList: dieselbe Liste INLINE in ein Formular.
import { esc } from "./utils.js";
import { t as tr } from "./i18n.js";

function rowHtml(it, multi, selected, icon) {
  const type = multi ? "checkbox" : "radio";
  const checked = selected.has(it.id) ? "checked" : "";
  const dis = it.disabled ? "disabled" : "";
  const ic = it.icon || icon || "";
  const sub = it.sub ? `<span style="color:var(--subtext);font-size:11px;margin-left:auto">${esc(it.sub)}</span>` : "";
  return `<label class="pick-row${it.disabled ? " pick-dis" : ""}" style="display:flex;gap:8px;align-items:center;padding:5px 6px;border-radius:6px;cursor:${it.disabled ? "default" : "pointer"}">
    <input type="${type}" name="pick" value="${esc(it.id)}" ${checked} ${dis}>
    ${ic ? `<span>${ic}</span>` : ""}<span>${esc(it.name)}</span>${sub}</label>`;
}

/** Eine filterbare Liste in `box` rendern und verdrahten. `sel` ist ein Set. */
export function wireEntityList(box, groups, sel, { multi = false, onChange } = {}) {
  const filter = box.querySelector("[data-pick-filter]");
  const list = box.querySelector("[data-pick-list]");
  function draw() {
    const f = (filter?.value || "").toLowerCase();
    let html = "";
    for (const g of groups) {
      const items = (g.items || []).filter((x) => sel.has(x.id) || !f || (x.name || "").toLowerCase().includes(f));
      if (!items.length && f) continue;
      if (g.title) html += `<div style="font-size:11px;color:var(--subtext);margin:8px 0 2px">${g.icon ? g.icon + " " : ""}${esc(g.title)}</div>`;
      html += items.map((it) => rowHtml(it, multi, sel, g.icon)).join("") || `<div style="color:var(--subtext);padding:4px 6px">${esc(tr("none") || "keine")}</div>`;
    }
    list.innerHTML = html || `<div style="color:var(--subtext);padding:4px 6px">${esc(tr("none") || "keine")}</div>`;
    list.querySelectorAll("input").forEach((inp) => inp.addEventListener("change", () => {
      if (!multi) sel.clear();
      if (inp.checked) sel.add(inp.value); else sel.delete(inp.value);
      onChange?.();
    }));
  }
  filter?.addEventListener("input", draw);
  draw();
  return draw;
}

export function entityListHtml({ filter = true } = {}) {
  return `${filter ? `<input data-pick-filter placeholder="${esc(tr("filter") || "filtern")}" style="width:100%;margin:0 0 6px;box-sizing:border-box">` : ""}
    <div data-pick-list style="max-height:260px;overflow:auto;border:1px solid var(--border);border-radius:8px;padding:6px 8px"></div>`;
}

/** Modal-Auswahl im VPN-Stil. */
export function pickEntity(title, groups, opts = {}) {
  const multi = !!opts.multi;
  const sel = new Set(opts.selected || []);
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.style.cssText = "position:fixed;inset:0;z-index:9500;background:rgba(0,0,0,0.5);display:flex;align-items:center;justify-content:center";
    overlay.innerHTML = `
      <div style="background:var(--panel,#131c2b);color:var(--text,#e8eef7);border:1px solid var(--border,#2a3648);
        border-radius:12px;min-width:360px;max-width:480px;width:90%;padding:18px;box-shadow:0 16px 48px rgba(0,0,0,0.5)">
        <div style="font-size:14px;font-weight:600;margin-bottom:10px">${esc(title)}</div>
        <div data-pick-box>${entityListHtml()}</div>
        <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px">
          <button class="btn" data-cancel>${esc(tr("cancel") || "Abbrechen")}</button>
          <button class="btn btn-primary" data-ok>${esc(tr("ok") || "OK")}</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    const box = overlay.querySelector("[data-pick-box]");
    const okBtn = overlay.querySelector("[data-ok]");
    const refresh = wireEntityList(box, groups, sel, { multi, onChange: () => { okBtn.disabled = !multi && sel.size === 0; } });
    okBtn.disabled = !multi && sel.size === 0;
    const done = (val) => { overlay.remove(); document.removeEventListener("keydown", onKey); resolve(val); };
    const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); done(null); } };
    document.addEventListener("keydown", onKey);
    overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) done(null); });
    overlay.querySelector("[data-cancel]").addEventListener("click", () => done(null));
    okBtn.addEventListener("click", () => {
      if (multi) done([...sel]);
      else done(sel.size ? [...sel][0] : null);
    });
    setTimeout(() => box.querySelector("[data-pick-filter]")?.focus(), 30);
    void refresh;
  });
}
