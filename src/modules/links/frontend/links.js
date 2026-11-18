// links.js (Modul "links") - URL-Shortener & QR-Code-Generator
// -------------------------------------------------------------
// Kurzlinks:  rmm.url/s/<code>  (Klicks werden gezaehlt)
// QR ueber RMM: rmm.url/qr/<code> (Scans werden getrennt gezaehlt; Ziel bleibt
//               nachtraeglich aenderbar, weil der Code nur auf das RMM zeigt)
// QR direkt:  der QR-Code enthaelt die Zieladresse selbst (ohne RMM dazwischen)
//
// Die QR-Grafik entsteht KOMPLETT im Browser (qrcode.js, lokal im Modul -
// kein CDN, funktioniert im abgeschotteten LAN). Download als PNG oder SVG.

import { moduleRequest } from "/js/app/modules.js";
import { esc, uiConfirm } from "/js/app/utils.js";
import { qrcode } from "./qrcode.js";

const ORIGIN = () => window.location.origin;

// ---------------- QR-Erzeugung (Browser, offline) ----------------
function makeQr(text) {
  // Typ 0 = automatisch passende Groesse, Fehlerkorrektur M (Standard-Mix
  // aus Robustheit und Dichte).
  const qr = qrcode(0, "M");
  qr.addData(text);
  qr.make();
  return qr;
}

function qrCanvas(qr, scale = 6, margin = 4) {
  const n = qr.getModuleCount();
  const size = (n + margin * 2) * scale;
  const cv = document.createElement("canvas");
  cv.width = cv.height = size;
  const ctx = cv.getContext("2d");
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, size, size);
  ctx.fillStyle = "#000000";
  for (let r = 0; r < n; r++) {
    for (let c = 0; c < n; c++) {
      if (qr.isDark(r, c)) ctx.fillRect((c + margin) * scale, (r + margin) * scale, scale, scale);
    }
  }
  return cv;
}

function downloadUrl(url, name) {
  const a = document.createElement("a");
  a.href = url; a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
}

export function renderLinks(body, win) {
  let links = [];

  body.innerHTML = `
    <div style="display:flex;height:100%;background:var(--panel);min-width:0">
      <div style="flex:1.2;min-width:0;display:flex;flex-direction:column;border-right:1px solid var(--border)">
        <div style="padding:10px 14px;border-bottom:1px solid var(--border)">
          <b>🔗 Kurzlinks</b>
          <span style="font-size:11px;color:var(--subtext)"> · ${esc(ORIGIN())}/s/&lt;code&gt;</span>
        </div>
        <div style="display:flex;gap:6px;padding:10px 14px;flex-wrap:wrap;border-bottom:1px solid var(--border)">
          <input id="lk-url" placeholder="https://ziel-seite.example.com/sehr/lange/adresse" style="flex:2;min-width:220px">
          <input id="lk-name" placeholder="Name (optional)" style="flex:1;min-width:120px">
          <input id="lk-code" placeholder="Wunsch-Code (optional)" style="width:150px">
          <button class="btn-primary" id="lk-add">＋ Kurzlink</button>
        </div>
        <div id="lk-err" class="form-error hidden" style="margin:6px 14px 0"></div>
        <div id="lk-list" style="flex:1;overflow:auto;padding:8px 14px"></div>
      </div>

      <div style="flex:1;min-width:300px;display:flex;flex-direction:column">
        <div style="padding:10px 14px;border-bottom:1px solid var(--border)"><b>⬛ QR-Code</b></div>
        <div style="padding:10px 14px;display:flex;flex-direction:column;gap:8px;overflow:auto">
          <input id="qr-text" placeholder="Adresse oder Text für den QR-Code">
          <label style="font-size:12px;color:var(--subtext)">
            <input type="checkbox" id="qr-via" checked>
            Über das RMM leiten (erzeugt einen Kurzlink; Scans werden gezählt und das Ziel bleibt änderbar).
            Ohne Haken zeigt der QR-Code direkt zur Seite.
          </label>
          <div style="display:flex;gap:6px">
            <button class="btn-primary" id="qr-make">QR erzeugen</button>
            <button class="btn" id="qr-png" disabled>⬇ PNG</button>
            <button class="btn" id="qr-svg" disabled>⬇ SVG</button>
          </div>
          <div id="qr-out" style="align-self:center;background:#fff;border-radius:8px;padding:8px;display:none"></div>
          <div id="qr-sub" style="font-size:11px;color:var(--subtext);text-align:center;word-break:break-all"></div>
        </div>
      </div>
    </div>`;

  const $ = (s) => body.querySelector(s);
  const err = $("#lk-err");
  const showErr = (e) => { err.textContent = e.message || String(e); err.classList.remove("hidden"); };

  // ---------------- Kurzlink-Liste ----------------
  function drawList() {
    const box = $("#lk-list");
    if (!links.length) {
      box.innerHTML = `<div style="color:var(--subtext);font-size:13px;padding:8px 0">
        Noch keine Kurzlinks. Oben eine Ziel-Adresse eintragen - der Kurzlink ist sofort unter
        <code>/s/&lt;code&gt;</code> erreichbar.</div>`;
      return;
    }
    box.innerHTML = links.map((l) => `
      <div class="panel" style="padding:8px 10px;margin-bottom:6px;display:flex;gap:10px;align-items:center">
        <div style="flex:1;min-width:0">
          <div style="font-size:13px;font-weight:600;display:flex;gap:6px;align-items:center">
            <code>/s/${esc(l.code)}</code>${l.name ? ` · ${esc(l.name)}` : ""}
          </div>
          <div style="font-size:11px;color:var(--subtext);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(l.url)}">→ ${esc(l.url)}</div>
          <div style="font-size:11px;color:var(--subtext)">👆 ${l.clicks || 0} Klicks · ⬛ ${l.qr_scans || 0} QR-Scans${l.created_by ? ` · von ${esc(l.created_by)}` : ""}</div>
        </div>
        <button class="btn btn-sm" data-copy="${esc(l.code)}" title="Kurzlink kopieren">📋</button>
        <button class="btn btn-sm" data-qr="${esc(l.code)}" title="QR-Code für diesen Kurzlink (läuft über das RMM)">⬛ QR</button>
        <button class="btn btn-sm" data-edit="${esc(l.code)}" title="Ziel ändern">✏️</button>
        <button class="action-btn btn-danger" data-del="${esc(l.code)}" title="Löschen">✕</button>
      </div>`).join("");

    box.querySelectorAll("[data-copy]").forEach((b) => b.addEventListener("click", () => {
      navigator.clipboard?.writeText(`${ORIGIN()}/s/${b.dataset.copy}`);
      window.notify?.("Kurzlink kopiert", "success");
    }));
    box.querySelectorAll("[data-qr]").forEach((b) => b.addEventListener("click", () => {
      showQr(`${ORIGIN()}/qr/${b.dataset.qr}`, `qr-${b.dataset.qr}`);
    }));
    box.querySelectorAll("[data-edit]").forEach((b) => b.addEventListener("click", async () => {
      const l = links.find((x) => x.code === b.dataset.edit);
      const { uiPrompt } = await import("/js/app/utils.js");
      const url = await uiPrompt("Neues Ziel für /s/" + l.code, { value: l.url });
      if (!url || url === l.url) return;
      try {
        await moduleRequest(`/api/links/${encodeURIComponent(l.code)}`, {
          method: "PATCH", noJob: true, body: JSON.stringify({ url }) });
        load();
      } catch (e) { showErr(e); }
    }));
    box.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
      if (!(await uiConfirm(`Kurzlink /s/${b.dataset.del} löschen? QR-Codes, die darauf zeigen, funktionieren dann nicht mehr.`, { danger: true }))) return;
      try {
        await moduleRequest(`/api/links/${encodeURIComponent(b.dataset.del)}`, { method: "DELETE", noJob: true });
        load();
      } catch (e) { showErr(e); }
    }));
  }

  $("#lk-add").addEventListener("click", async () => {
    err.classList.add("hidden");
    try {
      const l = await moduleRequest("/api/links", {
        method: "POST", noJob: true,
        body: JSON.stringify({ url: $("#lk-url").value, name: $("#lk-name").value, code: $("#lk-code").value }),
      });
      $("#lk-url").value = ""; $("#lk-name").value = ""; $("#lk-code").value = "";
      navigator.clipboard?.writeText(`${ORIGIN()}/s/${l.code}`);
      window.notify?.(`Kurzlink /s/${l.code} erstellt und kopiert`, "success");
      load();
    } catch (e) { showErr(e); }
  });

  // ---------------- QR-Bereich ----------------
  let lastQr = null;   // { qr, text, name }
  function showQr(text, name = "qrcode") {
    const out = $("#qr-out"), sub = $("#qr-sub");
    let qr;
    try { qr = makeQr(text); }
    catch (e) { showErr(new Error("QR-Code konnte nicht erzeugt werden: " + e.message)); return; }
    lastQr = { qr, text, name };
    out.style.display = "";
    out.innerHTML = "";
    const cv = qrCanvas(qr, 6);
    cv.style.cssText = "width:220px;height:220px;image-rendering:pixelated;display:block";
    out.appendChild(cv);
    sub.textContent = text;
    $("#qr-png").disabled = false;
    $("#qr-svg").disabled = false;
  }

  $("#qr-make").addEventListener("click", async () => {
    err.classList.add("hidden");
    const text = $("#qr-text").value.trim();
    if (!text) { showErr(new Error("Bitte eine Adresse oder einen Text eingeben")); return; }
    if ($("#qr-via").checked) {
      // Ueber das RMM: erst einen Kurzlink anlegen, dann /qr/<code> einbetten.
      if (!/^https?:\/\//i.test(text)) {
        showErr(new Error("Für den Weg über das RMM muss es eine http(s)-Adresse sein - Haken entfernen für freien Text"));
        return;
      }
      try {
        const l = await moduleRequest("/api/links", {
          method: "POST", noJob: true, body: JSON.stringify({ url: text }) });
        load();
        showQr(`${ORIGIN()}/qr/${l.code}`, `qr-${l.code}`);
      } catch (e) { showErr(e); }
    } else {
      showQr(text, "qrcode");
    }
  });

  $("#qr-png").addEventListener("click", () => {
    if (!lastQr) return;
    downloadUrl(qrCanvas(lastQr.qr, 12).toDataURL("image/png"), `${lastQr.name}.png`);
  });
  $("#qr-svg").addEventListener("click", () => {
    if (!lastQr) return;
    const svg = lastQr.qr.createSvgTag({ cellSize: 8, margin: 32 });
    const blob = new Blob([svg], { type: "image/svg+xml" });
    const u = URL.createObjectURL(blob);
    downloadUrl(u, `${lastQr.name}.svg`);
    setTimeout(() => URL.revokeObjectURL(u), 30000);
  });

  // ---------------- Laden ----------------
  async function load() {
    try {
      links = await moduleRequest("/api/links");
      drawList();
    } catch (e) {
      $("#lk-list").innerHTML = `<div style="color:var(--danger);font-size:13px;padding:8px 0">${esc(e.message)}</div>`;
    }
  }
  load();
}
