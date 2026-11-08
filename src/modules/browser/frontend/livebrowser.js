// livebrowser.js (Modul "browser") - Firefox-ähnliche Oberfläche + Live-Stream
// ---------------------------------------------------------------------------
// Verbindet sich per WebSocket mit der Server-Browser-Sitzung (browser_routes),
// zeigt die gestreamten JPEG-Frames und schickt Maus/Tastatur zurück. Tableiste,
// Adressleiste (zurück/vor/neu laden/Los), Lesezeichenleiste.
import { getToken } from "/js/app/api.js";
import { esc } from "/js/app/utils.js";

const BACKEND = location.origin.replace(/^http/, "ws");

export async function renderLiveBrowser(body, win, startUrl = "") {
  // Playwright da?
  let st = { ready: false, reason: "", may_use: false };
  try {
    st = await fetch("/api/browser/status", { headers: _h() }).then((r) => r.json());
  } catch { /* egal */ }
  if (!st.may_use) {
    body.innerHTML = `<div style="padding:24px;color:var(--subtext)">Keine Berechtigung für den Server-Browser.</div>`;
    return;
  }
  if (!st.ready) {
    body.innerHTML = `<div style="padding:24px;color:var(--danger)">Server-Browser nicht verfügbar:<br>${esc(st.reason || "")}
      <br><br><small>Playwright + Chromium auf dem Server installieren:<br><code>pip install playwright &amp;&amp; playwright install chromium</code></small></div>`;
    return;
  }

  body.innerHTML = `
    <div class="lb-root" style="display:flex;flex-direction:column;height:100%;min-height:0;background:var(--panel)">
      <div class="lb-tabs" style="display:flex;align-items:center;gap:2px;padding:4px 6px 0;overflow-x:auto;background:var(--panel-2)"></div>
      <div class="lb-bar" style="display:flex;gap:6px;align-items:center;padding:6px 8px;border-bottom:1px solid var(--border)">
        <button class="btn btn-sm" data-a="back" title="Zurück">◀</button>
        <button class="btn btn-sm" data-a="fwd" title="Vor">▶</button>
        <button class="btn btn-sm" data-a="reload" title="Neu laden">⟳</button>
        <input class="lb-url" placeholder="Adresse oder Suche eingeben" spellcheck="false" autocomplete="off"
               style="flex:1;padding:6px 10px;border-radius:16px;border:1px solid var(--border);background:var(--panel);color:var(--text)">
        <button class="btn btn-sm" data-a="star" title="Lesezeichen setzen">☆</button>
      </div>
      <div class="lb-marks" style="display:flex;gap:4px;align-items:center;padding:3px 8px;border-bottom:1px solid var(--border);overflow-x:auto;font-size:12px"></div>
      <div class="lb-view" style="position:relative;flex:1;min-height:0;overflow:hidden;background:#fff">
        <img class="lb-img" draggable="false" style="position:absolute;inset:0;width:100%;height:100%;object-fit:contain;user-select:none" />
        <div class="lb-status" style="position:absolute;bottom:6px;left:8px;font-size:11px;color:#333;background:rgba(255,255,255,.8);padding:1px 6px;border-radius:6px;display:none"></div>
      </div>
    </div>`;

  const tabsEl = body.querySelector(".lb-tabs");
  const marksEl = body.querySelector(".lb-marks");
  const urlEl = body.querySelector(".lb-url");
  const img = body.querySelector(".lb-img");
  const statusEl = body.querySelector(".lb-status");
  let tabs = [];
  let active = null;
  let imgMeta = { w: 1280, h: 800 };     // Frame-Größe (für Koordinaten-Umrechnung)

  // --- WebSocket ---
  const ws = new WebSocket(BACKEND + "/api/browser/ws", ["rmmtoken." + (getToken() || "")]);
  let open = false;
  ws.onopen = () => { open = true; sendResize(); };
  ws.onclose = () => { open = false; statusEl.style.display = "block"; statusEl.textContent = "Verbindung getrennt"; };
  ws.onmessage = (e) => {
    let m; try { m = JSON.parse(e.data); } catch { return; }
    if (m.t === "frame") {
      if (m.tab === active) {
        img.src = "data:image/jpeg;base64," + m.data;
      }
    } else if (m.t === "tabs") {
      tabs = m.tabs; active = m.active; renderTabs();
    } else if (m.t === "meta") {
      if (m.tab === active && document.activeElement !== urlEl) urlEl.value = m.url || "";
      const tb = tabs.find((x) => x.id === m.tab); if (tb) { tb.title = m.title; tb.url = m.url; tb.loading = m.loading; renderTabs(); }
    } else if (m.t === "error") {
      statusEl.style.display = "block"; statusEl.textContent = m.msg || "Fehler";
    }
  };
  const send = (o) => { if (open) try { ws.send(JSON.stringify(o)); } catch { /* egal */ } };

  function renderTabs() {
    tabsEl.innerHTML = tabs.map((t) => `
      <div class="lb-tab" data-tab="${esc(t.id)}" title="${esc(t.url || "")}"
        style="display:flex;align-items:center;gap:6px;max-width:200px;padding:6px 10px;border-radius:8px 8px 0 0;cursor:pointer;
        background:${t.id === active ? "var(--panel)" : "transparent"};border:1px solid ${t.id === active ? "var(--border)" : "transparent"};border-bottom:none">
        <span style="flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:12.5px">${t.loading ? "⏳ " : "🌐 "}${esc(t.title || "Neuer Tab")}</span>
        <span class="lb-close" data-close="${esc(t.id)}" style="opacity:.6">✕</span>
      </div>`).join("")
      + `<button class="btn btn-sm lb-newtab" title="Neuer Tab" style="margin-left:4px">＋</button>`;
    tabsEl.querySelectorAll(".lb-tab").forEach((el) => el.addEventListener("click", (ev) => {
      if (ev.target.classList.contains("lb-close")) { send({ cmd: "close-tab", tab: ev.target.dataset.close }); return; }
      send({ cmd: "active", tab: el.dataset.tab });
    }));
    tabsEl.querySelector(".lb-newtab")?.addEventListener("click", () => send({ cmd: "new-tab" }));
  }

  // --- Adressleiste ---
  body.querySelector('[data-a="back"]').addEventListener("click", () => send({ cmd: "back" }));
  body.querySelector('[data-a="fwd"]').addEventListener("click", () => send({ cmd: "forward" }));
  body.querySelector('[data-a="reload"]').addEventListener("click", () => send({ cmd: "reload" }));
  urlEl.addEventListener("keydown", (e) => { if (e.key === "Enter") send({ cmd: "navigate", url: urlEl.value }); });
  body.querySelector('[data-a="star"]').addEventListener("click", async () => {
    const t = tabs.find((x) => x.id === active);
    if (!t || !t.url) return;
    await fetch("/api/browser/bookmarks", { method: "POST", headers: { ..._h(), "Content-Type": "application/json" },
      body: JSON.stringify({ title: t.title, url: t.url }) });
    loadMarks();
    window.notify?.("Lesezeichen gesetzt", "success", 2000);
  });

  async function loadMarks() {
    let bm = [];
    try { bm = (await fetch("/api/browser/bookmarks", { headers: _h() }).then((r) => r.json())).bookmarks || []; } catch { /* egal */ }
    marksEl.innerHTML = bm.length
      ? bm.map((b) => `<span class="lb-mark" data-url="${esc(b.url)}" title="${esc(b.url)}"
          style="padding:2px 8px;border-radius:6px;background:var(--panel-2);cursor:pointer;white-space:nowrap">⭐ ${esc((b.title || b.url).slice(0, 28))}
          <span class="lb-mdel" data-del="${esc(b.id)}" style="opacity:.5;margin-left:4px">✕</span></span>`).join("")
      : `<span style="color:var(--subtext)">Noch keine Lesezeichen</span>`;
    marksEl.querySelectorAll(".lb-mark").forEach((el) => el.addEventListener("click", (ev) => {
      if (ev.target.classList.contains("lb-mdel")) {
        fetch("/api/browser/bookmarks/" + ev.target.dataset.del, { method: "DELETE", headers: _h() }).then(loadMarks);
        return;
      }
      send({ cmd: "navigate", url: el.dataset.url });
    }));
  }
  loadMarks();

  // --- Maus/Tastatur in die Seite ---
  function coords(e) {
    const r = img.getBoundingClientRect();
    // object-fit:contain -> echte Bildfläche innerhalb des Elements bestimmen
    const ar = imgMeta.w / imgMeta.h, er = r.width / r.height;
    let dw = r.width, dh = r.height, ox = 0, oy = 0;
    if (er > ar) { dw = r.height * ar; ox = (r.width - dw) / 2; } else { dh = r.width / ar; oy = (r.height - dh) / 2; }
    const x = (e.clientX - r.left - ox) / dw * imgMeta.w;
    const y = (e.clientY - r.top - oy) / dh * imgMeta.h;
    return { x: Math.max(0, Math.min(imgMeta.w, x)), y: Math.max(0, Math.min(imgMeta.h, y)) };
  }
  img.addEventListener("load", () => { if (img.naturalWidth) imgMeta = { w: img.naturalWidth, h: img.naturalHeight }; });
  const btn = (b) => (b === 2 ? "right" : b === 1 ? "middle" : "left");
  img.addEventListener("mousedown", (e) => { e.preventDefault(); const c = coords(e); send({ cmd: "input", ev: { type: "down", ...c, button: btn(e.button) } }); img.focus(); });
  img.addEventListener("mouseup", (e) => { const c = coords(e); send({ cmd: "input", ev: { type: "up", ...c, button: btn(e.button) } }); });
  let lastMove = 0;
  img.addEventListener("mousemove", (e) => { const now = Date.now(); if (now - lastMove < 30) return; lastMove = now; send({ cmd: "input", ev: { type: "move", ...coords(e) } }); });
  img.addEventListener("dblclick", (e) => { send({ cmd: "input", ev: { type: "dblclick", ...coords(e) } }); });
  img.addEventListener("contextmenu", (e) => e.preventDefault());
  img.addEventListener("wheel", (e) => { e.preventDefault(); send({ cmd: "input", ev: { type: "wheel", dx: e.deltaX, dy: e.deltaY } }); }, { passive: false });
  img.tabIndex = 0;
  img.addEventListener("keydown", (e) => {
    if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) { send({ cmd: "input", ev: { type: "text", text: e.key } }); }
    else { send({ cmd: "input", ev: { type: "key", key: _mapKey(e.key) } }); }
    e.preventDefault();
  });

  function sendResize() {
    const r = body.querySelector(".lb-view").getBoundingClientRect();
    send({ cmd: "resize", w: Math.round(r.width) || 1280, h: Math.round(r.height) || 800 });
  }
  const ro = new ResizeObserver(() => { clearTimeout(ro._t); ro._t = setTimeout(sendResize, 300); });
  ro.observe(body.querySelector(".lb-view"));

  // bytera://-Link zu dieser Ansicht (aktuelle URL)
  win.getLink = () => {
    const t = tabs.find((x) => x.id === active);
    return "bytera://app/webbrowser" + (t && t.url ? "/url=" + encodeURIComponent(t.url) : "");
  };

  // Startadresse (z.B. aus bytera://app/webbrowser/url=...)
  const first = startUrl || win.props?.url || "";
  if (first) setTimeout(() => send({ cmd: "navigate", url: first }), 600);

  // Aufräumen, wenn das Fenster schließt
  const { registerCleanup } = await import("/js/app/windowmanager.js");
  registerCleanup(win.key, () => { try { ro.disconnect(); } catch { /* egal */ } try { ws.close(); } catch { /* egal */ } });
}

function _h() { return { Authorization: "Bearer " + (getToken() || "") }; }

function _mapKey(k) {
  const map = { " ": "Space", ArrowUp: "ArrowUp", ArrowDown: "ArrowDown", ArrowLeft: "ArrowLeft", ArrowRight: "ArrowRight",
    Enter: "Enter", Backspace: "Backspace", Tab: "Tab", Escape: "Escape", Delete: "Delete", Home: "Home", End: "End",
    PageUp: "PageUp", PageDown: "PageDown" };
  return map[k] || k;
}
