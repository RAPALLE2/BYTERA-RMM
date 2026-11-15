// livebrowser.js (module "browser") - Firefox-like browser UI on top of a live stream
// -----------------------------------------------------------------------------------
// The page itself runs in a real Chromium inside the backend (session.py). This file
// draws the browser around it - tab strip, toolbar, address bar with suggestions,
// bookmarks toolbar, menus, new-tab page, library (history/bookmarks/downloads),
// find bar - and the parts headless Chromium cannot draw itself (context menu,
// <select> lists, alert/confirm/prompt, file chooser).
//
// Protocol (WS /api/browser/ws?win=...):
//   client -> server  {cmd: ...}   see browser_routes._command
//   server -> client  JSON {t: state|tab|hover|ctx|select|dialog|dialog-close|
//                              filechooser|download|clipboard|find|error|starting}
//                     BINARY [1 byte id length][tab id][JPEG]  = picture of the active tab
import { getToken } from "/js/app/api.js";
import { esc, uiConfirm, uiPrompt } from "/js/app/utils.js";
import { registerCleanup } from "/js/app/windowmanager.js";
import { watchJobs } from "/js/app/bgjobs.js";

const NEWTAB = "about:newtab";
const WS_BASE = location.origin.replace(/^http/, "ws");

// ---------------------------------------------------------------- REST helper
async function bq(path, { method = "GET", body, form, raw } = {}) {
  const headers = { Authorization: "Bearer " + (getToken() || "") };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch("/api/browser" + path, { method, headers,
    body: form || (body !== undefined ? JSON.stringify(body) : undefined) });
  if (raw) { if (!res.ok) throw new Error(`Error ${res.status}`); return res; }
  const d = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(d.detail || `Error ${res.status}`);
  return d;
}

const fmtSize = (n) => n > 1e9 ? (n / 1e9).toFixed(1) + " GB" : n > 1e6 ? (n / 1e6).toFixed(1) + " MB"
  : n > 1e3 ? Math.round(n / 1e3) + " kB" : (n || 0) + " B";
const hostOf = (u) => { try { return new URL(u).host; } catch { return ""; } };
const isWeb = (u) => /^https?:/i.test(u || "");

// ---------------------------------------------------------------- styles (once)
function injectCss() {
  if (document.getElementById("fxb-css")) return;
  const st = document.createElement("style");
  st.id = "fxb-css";
  st.textContent = `
.fxb{--fx-bg:var(--panel-2,#f0f0f4);--fx-tab:var(--panel,#fff);--fx-field:var(--panel,#fff);--fx-hover:rgba(127,127,140,.18);
  --fx-line:var(--border,#d7d7db);--fx-sub:var(--subtext,#5b5b66);--fx-acc:var(--accent,#0061e0);
  display:flex;flex-direction:column;height:100%;min-height:0;background:var(--fx-bg);color:var(--text);font:13px/1.3 system-ui,-apple-system,"Segoe UI",sans-serif;position:relative;user-select:none}
.fxb [hidden]{display:none!important}
.fxb button{font:inherit;color:inherit}
.fxb-tabs{display:flex;align-items:flex-end;gap:2px;padding:5px 6px 0;min-height:38px;overflow:hidden}
.fxb-tablist{display:flex;gap:2px;flex:1;min-width:0;overflow-x:auto;scrollbar-width:none}
.fxb-tablist::-webkit-scrollbar{display:none}
.fxb-tab{position:relative;display:flex;align-items:center;gap:7px;height:32px;padding:0 6px 0 10px;border-radius:6px 6px 0 0;
  min-width:72px;max-width:225px;flex:1 1 225px;cursor:default;color:var(--fx-sub)}
.fxb-tab:hover{background:var(--fx-hover)}
.fxb-tab.on{background:var(--fx-tab);color:var(--text);box-shadow:0 0 4px rgba(0,0,0,.12)}
.fxb-tab .ttl{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;-webkit-mask-image:linear-gradient(90deg,#000 85%,transparent)}
.fxb-tab .ico{width:16px;height:16px;flex:0 0 16px;display:flex;align-items:center;justify-content:center;font-size:13px}
.fxb-tab .ico img{width:16px;height:16px}
.fxb-tab .x{width:22px;height:22px;border:0;background:none;border-radius:4px;display:flex;align-items:center;justify-content:center;opacity:.75;flex:0 0 22px}
.fxb-tab .x:hover{background:var(--fx-hover);opacity:1}
.fxb-tab .net{position:absolute;left:8px;right:8px;bottom:1px;height:2px;border-radius:2px}
.fxb-tab.sleep .ttl{opacity:.65;font-style:italic}
.fxb-tab.drag{opacity:.4}
.fxb-spin{width:12px;height:12px;border:2px solid var(--fx-acc);border-right-color:transparent;border-radius:50%;animation:fxspin .8s linear infinite}
@keyframes fxspin{to{transform:rotate(360deg)}}
.fxb-ib{width:30px;height:30px;border:0;background:none;border-radius:4px;display:inline-flex;align-items:center;justify-content:center;font-size:15px;flex:0 0 30px;cursor:default}
.fxb-ib:hover:not(:disabled){background:var(--fx-hover)}
.fxb-ib:disabled{opacity:.35}
.fxb-ib .badge{position:absolute;top:2px;right:2px;min-width:14px;height:14px;border-radius:7px;background:var(--fx-acc);color:#fff;font-size:9px;line-height:14px}
.fxb-bar{display:flex;align-items:center;gap:2px;padding:4px 6px;background:var(--fx-tab);border-bottom:1px solid var(--fx-line)}
.fxb-url{flex:1;display:flex;align-items:center;gap:4px;height:32px;margin:0 6px;padding:0 4px 0 6px;border-radius:5px;background:var(--fx-bg);border:1px solid transparent;position:relative}
.fxb-url:focus-within{background:var(--fx-field);border-color:var(--fx-acc);box-shadow:0 0 0 1px var(--fx-acc)}
.fxb-url input{flex:1;min-width:0;border:0;outline:0;background:transparent;color:var(--text);font:14px system-ui,sans-serif;padding:0 4px;user-select:text}
.fxb-ident{display:flex;align-items:center;gap:4px;height:24px;padding:0 6px;border-radius:4px;font-size:12px;color:var(--fx-sub);border:0;background:none;white-space:nowrap;max-width:200px;overflow:hidden}
.fxb-ident:hover{background:var(--fx-hover)}
.fxb-ident .dot{width:8px;height:8px;border-radius:50%}
.fxb-sugg{position:absolute;left:-1px;right:-1px;top:34px;background:var(--fx-field);border:1px solid var(--fx-line);border-radius:0 0 6px 6px;box-shadow:0 6px 18px rgba(0,0,0,.18);z-index:30;padding:4px 0;max-height:380px;overflow:auto}
.fxb-sugg div{display:flex;gap:10px;align-items:center;padding:6px 12px;white-space:nowrap;overflow:hidden}
.fxb-sugg div.sel,.fxb-sugg div:hover{background:var(--fx-hover)}
.fxb-sugg .u{color:var(--fx-acc);font-size:12px;overflow:hidden;text-overflow:ellipsis}
.fxb-marks{display:flex;align-items:center;gap:2px;padding:2px 6px;min-height:28px;background:var(--fx-tab);border-bottom:1px solid var(--fx-line);overflow:hidden}
.fxb-mark{display:flex;align-items:center;gap:5px;height:24px;padding:0 7px;border:0;background:none;border-radius:4px;max-width:170px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:12px}
.fxb-mark:hover{background:var(--fx-hover)}
.fxb-mark img{width:14px;height:14px}
.fxb-view{position:relative;flex:1;min-height:0;overflow:hidden;background:#fff}
.fxb-view canvas{position:absolute;left:0;top:0;outline:none}
.fxb-sink{position:absolute;left:-10000px;top:0;width:10px;height:10px;opacity:0}
.fxb-status{position:absolute;left:0;bottom:0;max-width:60%;padding:3px 8px;background:var(--fx-tab);color:var(--text);border:1px solid var(--fx-line);border-left:0;border-bottom:0;border-radius:0 4px 0 0;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:none;z-index:5;pointer-events:none}
.fxb-menu{position:absolute;z-index:60;min-width:220px;background:var(--fx-field);color:var(--text);border:1px solid var(--fx-line);border-radius:6px;box-shadow:0 8px 24px rgba(0,0,0,.22);padding:4px 0;max-height:80%;overflow:auto}
.fxb-menu .mi{display:flex;align-items:center;gap:10px;padding:6px 14px;white-space:nowrap;cursor:default}
.fxb-menu .mi:hover:not(.dis){background:var(--fx-hover)}
.fxb-menu .mi.dis{opacity:.4}
.fxb-menu .mi .k{margin-left:auto;color:var(--fx-sub);font-size:11px;padding-left:20px}
.fxb-menu .sep{height:1px;background:var(--fx-line);margin:4px 0}
.fxb-menu .hd{padding:6px 14px 2px;color:var(--fx-sub);font-size:11px;text-transform:uppercase;letter-spacing:.04em}
.fxb-menu .zoomrow{display:flex;align-items:center;gap:6px;padding:4px 14px}
.fxb-menu .zoomrow button{border:1px solid var(--fx-line);background:none;border-radius:4px;min-width:30px;height:26px}
.fxb-overlay{position:absolute;inset:0;z-index:40;display:flex;align-items:flex-start;justify-content:center;background:rgba(0,0,0,.25);padding-top:60px}
.fxb-dlg{background:var(--fx-field);color:var(--text);border:1px solid var(--fx-line);border-radius:8px;box-shadow:0 10px 30px rgba(0,0,0,.3);padding:16px 18px;min-width:340px;max-width:560px;user-select:text}
.fxb-dlg .row{display:flex;gap:8px;justify-content:flex-end;margin-top:14px}
.fxb-dlg input[type=text],.fxb-dlg select{width:100%;box-sizing:border-box}
.fxb-panel{position:absolute;top:0;right:0;bottom:0;width:min(440px,100%);z-index:45;background:var(--fx-field);color:var(--text);border-left:1px solid var(--fx-line);box-shadow:-6px 0 20px rgba(0,0,0,.18);display:flex;flex-direction:column;user-select:text}
.fxb-panel .ph{display:flex;align-items:center;gap:8px;padding:10px 12px;border-bottom:1px solid var(--fx-line)}
.fxb-panel .ph .tabs{display:flex;gap:4px;flex:1}
.fxb-panel .ph .tabs button{border:0;background:none;padding:5px 10px;border-radius:4px}
.fxb-panel .ph .tabs button.on{background:var(--fx-hover);font-weight:600}
.fxb-panel .pb{flex:1;overflow:auto;padding:6px 0}
.fxb-panel .it{display:flex;gap:10px;align-items:center;padding:6px 14px}
.fxb-panel .it:hover{background:var(--fx-hover)}
.fxb-panel .it .m{flex:1;min-width:0}
.fxb-panel .it .m div{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fxb-panel .it .s{font-size:11px;color:var(--fx-sub)}
.fxb-newtab{position:absolute;inset:0;z-index:4;overflow:auto;background:var(--fx-bg);display:flex;flex-direction:column;align-items:center;padding:8vh 20px 30px;user-select:none}
.fxb-newtab .logo{font-size:44px;margin-bottom:6px}
.fxb-newtab .srch{width:min(640px,92%);display:flex;align-items:center;gap:8px;height:48px;padding:0 16px;border-radius:8px;background:var(--fx-field);box-shadow:0 2px 10px rgba(0,0,0,.12);margin:14px 0 30px}
.fxb-newtab .srch input{flex:1;border:0;outline:0;background:none;color:var(--text);font-size:16px;user-select:text}
.fxb-newtab .tiles{display:grid;grid-template-columns:repeat(auto-fill,96px);gap:18px 22px;justify-content:center;width:min(760px,96%)}
.fxb-newtab .tile{display:flex;flex-direction:column;align-items:center;gap:6px;cursor:pointer;text-align:center}
.fxb-newtab .tile .sq{width:72px;height:72px;border-radius:10px;background:var(--fx-field);box-shadow:0 2px 8px rgba(0,0,0,.12);display:flex;align-items:center;justify-content:center;font-size:26px;font-weight:600;color:var(--fx-acc)}
.fxb-newtab .tile .sq img{width:32px;height:32px}
.fxb-newtab .tile .nm{font-size:12px;width:96px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fxb-newtab .tile:hover .sq{box-shadow:0 3px 14px rgba(0,0,0,.22)}
.fxb-find{display:flex;align-items:center;gap:6px;padding:4px 8px;background:var(--fx-tab);border-top:1px solid var(--fx-line)}
.fxb-find input{width:240px}
.fxb-msg{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:10px;color:var(--fx-sub);text-align:center;padding:20px;z-index:3;background:var(--fx-bg)}
.fxb-toast{position:absolute;right:12px;bottom:12px;z-index:50;background:var(--fx-field);border:1px solid var(--fx-line);border-radius:6px;box-shadow:0 6px 18px rgba(0,0,0,.2);padding:8px 12px;max-width:340px}
.fxb.app .fxb-tabs,.fxb.app .fxb-marks{display:none}
`;
  document.head.appendChild(st);
}

// ---------------------------------------------------------------- entry
// opts.app: {key, url, name, icon} -> web-app window (own server window, no tab strip)
export async function renderLiveBrowser(body, win, opts = {}) {
  injectCss();
  body.__fxb?.destroy();                       // window re-rendered -> old connection away
  body.__fxb = null;
  body.innerHTML = `<div class="fxb"><div class="fxb-msg">Starting browser …</div></div>`;
  const root = body.firstElementChild;
  let st;
  try { st = await bq("/status"); }
  catch (e) { root.innerHTML = `<div class="fxb-msg">⚠️ ${esc(e.message)}</div>`; return; }
  if (!st.may_use) {
    root.innerHTML = `<div class="fxb-msg">🔒 You don't have the permission “Use server browser”.</div>`;
    return;
  }
  if (!st.ready) { installScreen(root, st, () => renderLiveBrowser(body, win, opts)); return; }
  body.__fxb = new FxBrowser(root, win, opts);
}

// Chromium missing -> installation as a backend job (live progress for everyone)
function installScreen(root, st, retry) {
  let stopWatch = null;
  const draw = (job) => {
    const running = job && job.running;
    const failed = job && !running && job.status === "error";
    root.innerHTML = `<div class="fxb-msg">
      <div style="font-size:40px">🧭</div>
      <div style="font-size:16px;color:var(--text)">${running ? "The browser engine is being installed automatically …" : failed ? "The browser engine could not be installed" : "The browser engine is not installed yet"}</div>
      ${running ? `<div style="font-size:12px">This takes a few minutes (download + system libraries). You can close this window – the installation keeps running on the server.</div>` : ""}
      ${job ? `<div style="width:min(460px,90%)"><div style="height:6px;border-radius:3px;background:var(--fx-line);overflow:hidden">
          <div style="height:100%;width:${Math.round(job.pct || 0)}%;background:var(--fx-acc);transition:width .3s"></div></div>
          <div style="margin-top:6px;font-size:12px;word-break:break-word">${esc(job.detail || job.phase || "")}</div></div>` : ""}
      ${failed ? `<div style="max-width:640px;font-size:12px;color:var(--danger);word-break:break-word">${esc(job.error || "failed")}</div>
        <div style="font-size:12px">It is tried again automatically every 10 minutes. Details: task list (taskbar) → “Server browser: install Chromium”.</div>` : ""}
      ${!running && st.may_install ? `<button class="btn btn-primary" data-inst>${job ? "Try again now" : "Install browser engine"}</button>` : ""}
      ${!st.may_install && !running ? `<div>An administrator has to install it (Modules → Web Browser).</div>` : ""}
    </div>`;
    root.querySelector("[data-inst]")?.addEventListener("click", async () => {
      try { await bq("/install", { method: "POST" }); watch(); } catch (e) { window.notify?.(e.message, "error"); }
    });
    if (job && !job.running && job.status === "done") { stopWatch?.(); setTimeout(retry, 400); }
  };
  const watch = () => { stopWatch?.(); stopWatch = watchJobs({ kind: "browser.install", key: "chromium" }, draw); };
  draw(null);
  watch();                                   // running job or the result of the last one
}

// ---------------------------------------------------------------- the browser
class FxBrowser {
  constructor(root, win, opts) {
    this.root = root;
    this.win = win;
    this.app = opts.app || null;
    this.tabs = [];
    this.active = null;
    this.networks = [];
    this.bookmarks = { items: [], folders: [] };
    this.settings = {};
    this.engines = [];
    this.downloads = new Map();
    this.vp = { w: 0, h: 0 };
    this.frameTab = null;
    this.frameSeq = 0;
    this.drawnSeq = 0;
    this.closed = false;
    this.retry = 0;
    this.build();
    this.connect();
    this.loadBookmarks();
    bq("/settings").then((d) => { this.settings = d.settings; this.engines = d.engines; }).catch(() => {});
    if (win?.key) registerCleanup(win.key, () => this.destroy());
  }

  // ------------------------------------------------------------ DOM
  build() {
    const r = this.root;
    r.classList.toggle("app", !!this.app);
    r.innerHTML = `
      <div class="fxb-tabs">
        <div class="fxb-tablist"></div>
        <button class="fxb-ib" data-a="newtab" title="Open a new tab (Ctrl+T) – right-click: choose network">＋</button>
        <button class="fxb-ib" data-a="tablist" title="List all tabs">⌄</button>
      </div>
      <div class="fxb-bar">
        <button class="fxb-ib" data-a="back" title="Go back one page (Alt+←)">←</button>
        <button class="fxb-ib" data-a="fwd" title="Go forward one page (Alt+→)">→</button>
        <button class="fxb-ib" data-a="reload" title="Reload current page (Ctrl+R)">↻</button>
        <button class="fxb-ib" data-a="home" title="Home page">⌂</button>
        <div class="fxb-url">
          <button class="fxb-ident" data-a="ident" title="Network of this tab"></button>
          <input spellcheck="false" autocomplete="off" placeholder="Search with ${"DuckDuckGo"} or enter address">
          <button class="fxb-ib" data-a="star" title="Bookmark this page (Ctrl+D)" style="width:26px;height:26px;flex-basis:26px">☆</button>
          <div class="fxb-sugg" hidden></div>
        </div>
        <button class="fxb-ib" data-a="downloads" title="Downloads" style="position:relative">⬇<span class="badge" hidden></span></button>
        <button class="fxb-ib" data-a="menu" title="Open application menu">☰</button>
      </div>
      <div class="fxb-marks"></div>
      <div class="fxb-view" tabindex="-1">
        <canvas tabindex="0"></canvas>
        <textarea class="fxb-sink" tabindex="-1" aria-hidden="true"></textarea>
        <div class="fxb-status"></div>
        <div class="fxb-msg">Connecting …</div>
      </div>
      <div class="fxb-find" hidden>
        <input type="text" placeholder="Find in page">
        <button class="fxb-ib" data-f="prev" title="Previous">↑</button>
        <button class="fxb-ib" data-f="next" title="Next">↓</button>
        <span data-f="count" style="color:var(--fx-sub);font-size:12px;flex:1"></span>
        <button class="fxb-ib" data-f="close" title="Close">✕</button>
      </div>`;
    const q = (s) => r.querySelector(s);
    this.el = {
      tablist: q(".fxb-tablist"), url: q(".fxb-url input"), urlBox: q(".fxb-url"), sugg: q(".fxb-sugg"),
      ident: q("[data-a=ident]"), star: q("[data-a=star]"), back: q("[data-a=back]"), fwd: q("[data-a=fwd]"),
      reload: q("[data-a=reload]"), dlBadge: q("[data-a=downloads] .badge"), marks: q(".fxb-marks"),
      view: q(".fxb-view"), canvas: q("canvas"), sink: q(".fxb-sink"), status: q(".fxb-status"),
      msg: q(".fxb-view .fxb-msg"), find: q(".fxb-find"), findInput: q(".fxb-find input"),
      findCount: q("[data-f=count]"),
    };
    this.ctx2d = this.el.canvas.getContext("2d");

    r.querySelector(".fxb-bar").addEventListener("click", (e) => {
      const b = e.target.closest("[data-a]"); if (!b) return;
      this.action(b.dataset.a, b, e);
    });
    r.querySelector(".fxb-tabs").addEventListener("click", (e) => {
      const b = e.target.closest("[data-a]"); if (!b) return;
      this.action(b.dataset.a, b, e);
    });
    r.querySelector("[data-a=newtab]").addEventListener("contextmenu", (e) => {
      e.preventDefault(); this.networkMenu(e.currentTarget, (net) => this.send({ cmd: "new-tab", network: net }), "Open new tab in");
    });
    this.bindUrlbar();
    this.bindView();
    this.bindFind();
    this.bindShortcuts();
    this.ro = new ResizeObserver(() => this.layout());
    this.ro.observe(this.el.view);
    this.onDocDown = (e) => { if (this.menuEl && !this.menuEl.contains(e.target)) this.closeMenu(); };
    document.addEventListener("mousedown", this.onDocDown, true);
  }

  destroy() {
    this.closed = true;
    try { this.ws?.close(); } catch { /* ignore */ }
    this.ro?.disconnect();
    document.removeEventListener("mousedown", this.onDocDown, true);
    clearTimeout(this.retryTimer);
  }

  // ------------------------------------------------------------ connection
  connect() {
    if (this.closed) return;
    const qs = new URLSearchParams({ win: this.app ? "app:" + this.app.key : "main" });
    if (this.app?.url) qs.set("url", this.app.url);
    if (this.app?.network) qs.set("network", this.app.network);
    const ws = new WebSocket(`${WS_BASE}/api/browser/ws?${qs}`, ["rmmtoken." + (getToken() || "")]);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onopen = () => { this.retry = 0; this.vp = { w: 0, h: 0 }; this.layout(true); };
    ws.onmessage = (e) => (typeof e.data === "string" ? this.onJson(JSON.parse(e.data)) : this.onFrame(e.data));
    ws.onclose = (e) => {
      if (this.closed) return;
      if (e.code === 4403) { this.showMsg("🔒 No permission for the server browser."); return; }
      if (e.code === 4000) { this.connect(); return; }                 // session was reset on the server
      if (this.fatal) return;
      this.showMsg("Connection lost – reconnecting …");
      this.retryTimer = setTimeout(() => this.connect(), Math.min(10000, 800 * 2 ** this.retry++));
    };
  }

  send(obj) {
    if (this.ws?.readyState === 1) this.ws.send(JSON.stringify(obj));
  }

  showMsg(html) {
    this.el.msg.innerHTML = html;
    this.el.msg.hidden = !html;
  }

  layout(force = false) {
    const w = Math.round(this.el.view.clientWidth), h = Math.round(this.el.view.clientHeight);
    if (!w || !h) return;
    if (!force && w === this.vp.w && h === this.vp.h) return;
    this.vp = { w, h };
    const c = this.el.canvas;
    c.width = w; c.height = h;
    c.style.width = w + "px"; c.style.height = h + "px";
    if (this.lastBitmap) this.ctx2d.drawImage(this.lastBitmap, 0, 0);
    clearTimeout(this.resizeT);
    this.resizeT = setTimeout(() => this.send({ cmd: force ? "hello" : "resize", w, h }), force ? 0 : 150);
  }

  // ------------------------------------------------------------ messages
  onJson(m) {
    switch (m.t) {
      case "starting": this.showMsg("Starting browser …"); break;
      case "error":
        if (m.fatal) { this.fatal = true; this.showMsg(`⚠️ ${esc(m.msg)}`); }
        else this.toast(`⚠️ ${esc(m.msg)}`);
        break;
      case "state":
        this.tabs = m.tabs; this.active = m.active; this.networks = m.networks || this.networks;
        this.showMsg("");
        this.renderTabs(); this.renderChrome();
        break;
      case "tab": {
        const i = this.tabs.findIndex((t) => t.id === m.tab.id);
        if (i >= 0) this.tabs[i] = m.tab;
        this.renderTabs();
        if (m.tab.id === this.active) this.renderChrome();
        break;
      }
      case "hover":
        this.el.canvas.style.cursor = m.cursor || "default";
        this.el.status.textContent = m.link || "";
        this.el.status.style.display = m.link ? "block" : "none";
        break;
      case "ctx": this.pageMenu(m); break;
      case "select": this.selectList(m); break;
      case "dialog": this.pageDialog(m); break;
      case "dialog-close": this.root.querySelector(`[data-dialog="${m.id}"]`)?.remove(); break;
      case "filechooser": this.fileChooser(m); break;
      case "download": this.onDownload(m); break;
      case "clipboard": if (m.text) this.copyText(m.text); break;
      case "find":
        this.el.findCount.textContent = this.el.findInput.value ? (m.count ? `${m.count} matches` : "Phrase not found") : "";
        break;
    }
  }

  // Every received frame is acknowledged once it is drawn (or dropped) - the
  // server only sends new pictures while at most 2 are on the way. That keeps
  // the delay at one network round trip, even on slow links.
  async onFrame(buf) {
    const seq = ++this.frameSeq;
    const u8 = new Uint8Array(buf);
    const n = u8[0];
    const tid = new TextDecoder().decode(u8.subarray(1, 1 + n));
    try {
      if (tid !== this.active) return;
      const bmp = await createImageBitmap(new Blob([u8.subarray(1 + n)], { type: "image/jpeg" }));
      if (tid !== this.active || seq < this.drawnSeq) { bmp.close?.(); return; }   // a newer one is already up
      this.drawnSeq = seq;
      this.lastBitmap?.close?.();
      this.lastBitmap = bmp;
      this.ctx2d.drawImage(bmp, 0, 0, this.el.canvas.width, this.el.canvas.height);
      this.frameTab = tid;
    } catch { /* broken frame */ } finally {
      this.send({ cmd: "ack" });
    }
  }

  get tab() { return this.tabs.find((t) => t.id === this.active) || null; }
  netOf(id) { return this.networks.find((n) => n.id === id) || { id, name: id, kind: "direct", color: "" }; }

  // ------------------------------------------------------------ tab strip
  renderTabs() {
    const L = this.el.tablist;
    L.innerHTML = this.tabs.map((t) => {
      const net = this.netOf(t.network);
      const ico = t.loading ? `<span class="fxb-spin"></span>` : t.crashed ? "💥"
        : t.favicon ? `<img src="${esc(t.favicon)}" alt="">` : t.url === NEWTAB ? "🦊" : "🌐";
      return `<div class="fxb-tab ${t.id === this.active ? "on" : ""} ${t.sleeping ? "sleep" : ""}" draggable="true" data-id="${esc(t.id)}" title="${esc(t.title)}${t.url && t.url !== NEWTAB ? "\n" + esc(t.url) : ""}">
        <span class="ico">${ico}</span><span class="ttl">${esc(t.title)}</span>
        <button class="x" data-x title="Close tab (Ctrl+W)">✕</button>
        ${net.color ? `<span class="net" style="background:${esc(net.color)}"></span>` : ""}</div>`;
    }).join("");
    L.querySelectorAll(".fxb-tab").forEach((el) => {
      const id = el.dataset.id;
      el.addEventListener("mousedown", (e) => {
        if (e.button === 1) { e.preventDefault(); this.send({ cmd: "close-tab", tab: id }); return; }
        if (e.button === 0 && !e.target.closest("[data-x]") && id !== this.active) this.send({ cmd: "activate", tab: id });
      });
      el.querySelector("[data-x]").addEventListener("click", (e) => { e.stopPropagation(); this.send({ cmd: "close-tab", tab: id }); });
      el.addEventListener("dblclick", (e) => e.stopPropagation());
      el.addEventListener("contextmenu", (e) => { e.preventDefault(); this.tabMenu(id, e); });
      el.addEventListener("dragstart", (e) => { this.dragId = id; el.classList.add("drag"); e.dataTransfer.effectAllowed = "move"; });
      el.addEventListener("dragend", () => { el.classList.remove("drag"); this.dragId = null; });
      el.addEventListener("dragover", (e) => { if (this.dragId) e.preventDefault(); });
      el.addEventListener("drop", (e) => {
        e.preventDefault();
        if (!this.dragId || this.dragId === id) return;
        const to = this.tabs.findIndex((t) => t.id === id);
        this.send({ cmd: "move-tab", tab: this.dragId, index: to });
      });
    });
    L.ondblclick = (e) => { if (e.target === L) this.send({ cmd: "new-tab" }); };
    L.querySelector(".fxb-tab.on")?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }

  // ------------------------------------------------------------ toolbar / view state
  renderChrome() {
    const t = this.tab;
    const e = this.el;
    if (!t) return;
    e.back.disabled = !t.canBack;
    e.fwd.disabled = !t.canForward;
    e.reload.textContent = t.loading ? "✕" : "↻";
    e.reload.title = t.loading ? "Stop loading this page (Esc)" : "Reload current page (Ctrl+R)";
    if (document.activeElement !== e.url) e.url.value = t.url === NEWTAB ? "" : t.url;
    const net = this.netOf(t.network);
    const lock = t.url === NEWTAB ? "🔍" : /^https:/i.test(t.url) ? "🔒" : isWeb(t.url) ? "⚠️" : "ⓘ";
    e.ident.innerHTML = `${lock}${net.kind === "agent" ? ` <span class="dot" style="background:${esc(net.color)}"></span>${esc(net.name)}` : ""}`;
    e.ident.title = `${/^https:/i.test(t.url) ? "Secure connection" : isWeb(t.url) ? "Connection is not secure" : ""}\nNetwork: ${net.name}${net.detail ? " – " + net.detail : ""}\nClick to change the network of this tab`;
    const bm = this.bookmarks.items.find((b) => b.url === t.url);
    e.star.textContent = bm ? "★" : "☆";
    e.star.style.color = bm ? "var(--fx-acc)" : "";
    e.url.placeholder = `Search with ${this.engines.find((x) => x.id === this.settings.search)?.name || "DuckDuckGo"} or enter address`;
    // new-tab page / sleeping tab / crash
    const nt = this.root.querySelector(".fxb-newtab");
    if (t.url === NEWTAB) { if (!nt || nt.dataset.tab !== t.id) this.renderNewTab(t); }
    else nt?.remove();
    if (t.crashed) this.showMsg(`<div style="font-size:36px">💥</div><div>This tab crashed.</div><button class="btn btn-primary" data-restore>Restore this tab</button>`);
    else if (this.el.msg.querySelector("[data-restore]")) this.showMsg("");
    this.el.msg.querySelector("[data-restore]")?.addEventListener("click", () => this.send({ cmd: "reload", tab: t.id }));
    if (this.frameTab !== t.id && t.url !== NEWTAB) {
      this.ctx2d.fillStyle = "#fff"; this.ctx2d.fillRect(0, 0, this.vp.w, this.vp.h);
    }
  }

  action(a, btn, ev) {
    const t = this.tab;
    switch (a) {
      case "newtab": this.send({ cmd: "new-tab" }); break;
      case "tablist": this.tabListMenu(btn); break;
      case "back": this.send({ cmd: "back", tab: t?.id }); break;
      case "fwd": this.send({ cmd: "forward", tab: t?.id }); break;
      case "reload": this.send({ cmd: t?.loading ? "stop" : "reload", tab: t?.id, hard: ev?.shiftKey }); break;
      case "home": this.send({ cmd: "home", tab: t?.id }); break;
      case "ident": this.networkMenu(btn, (net) => this.send({ cmd: "network", tab: t?.id, network: net }), "Open this tab in", t?.network); break;
      case "star": this.starPopup(); break;
      case "downloads": this.library("downloads"); break;
      case "menu": this.mainMenu(btn); break;
    }
  }

  // ------------------------------------------------------------ address bar
  bindUrlbar() {
    const inp = this.el.url, box = this.el.sugg;
    let items = [], sel = -1, timer = null, seq = 0;
    const close = () => { box.hidden = true; items = []; sel = -1; };
    const draw = () => {
      box.innerHTML = items.map((it, i) => `<div class="${i === sel ? "sel" : ""}" data-i="${i}">
        <span>${it.icon}</span><span style="overflow:hidden;text-overflow:ellipsis">${esc(it.title)}</span>
        ${it.url ? `<span class="u">— ${esc(it.url)}</span>` : ""}</div>`).join("");
      box.hidden = !items.length;
      box.querySelectorAll("[data-i]").forEach((el) => el.addEventListener("mousedown", (e) => {
        e.preventDefault(); go(items[+el.dataset.i].go);
      }));
    };
    const go = (val) => {
      close();
      const v = (val ?? inp.value).trim();
      if (!v) return;
      inp.blur();
      this.send({ cmd: "navigate", tab: this.active, url: v });
      this.el.canvas.focus();
    };
    const suggest = async () => {
      const v = inp.value.trim();
      const my = ++seq;
      if (!v) { close(); return; }
      const low = v.toLowerCase();
      const eng = this.engines.find((x) => x.id === this.settings.search)?.name || "DuckDuckGo";
      const out = [{ icon: "🔍", title: `${v} — Search with ${eng}`, go: v }];
      const marks = this.bookmarks.items.filter((b) => (b.title + " " + b.url).toLowerCase().includes(low)).slice(0, 4)
        .map((b) => ({ icon: "★", title: b.title || b.url, url: b.url, go: b.url }));
      let hist = [];
      try { hist = (await bq(`/history?q=${encodeURIComponent(v)}&limit=8`)).history; } catch { /* ignore */ }
      if (my !== seq) return;
      const seen = new Set(marks.map((m) => m.url));
      const h = hist.filter((x) => !seen.has(x.url) && seen.add(x.url)).slice(0, 6)
        .map((x) => ({ icon: "🕘", title: x.title || x.url, url: x.url, go: x.url }));
      items = [...out, ...marks, ...h];
      sel = -1;
      draw();
    };
    let selectOnUp = false;                     // like Firefox: one click selects the whole address
    inp.addEventListener("focus", () => { inp.select(); selectOnUp = true; });
    inp.addEventListener("mouseup", (e) => { if (selectOnUp) { e.preventDefault(); inp.select(); } selectOnUp = false; });
    inp.addEventListener("keydown", () => { selectOnUp = false; }, true);
    inp.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(suggest, 120); });
    inp.addEventListener("blur", () => setTimeout(close, 150));
    inp.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        if (!items.length) return;
        e.preventDefault();
        if (e.key === "ArrowDown") sel = sel + 1 >= items.length ? -1 : sel + 1;
        else sel = sel - 1 < -1 ? items.length - 1 : sel - 1;
        draw();
        if (sel >= 0 && items[sel].url) inp.value = items[sel].url;
      } else if (e.key === "Enter") {
        e.preventDefault();
        go(sel >= 0 ? items[sel].go : inp.value);
      } else if (e.key === "Escape") {
        close(); inp.value = this.tab?.url === NEWTAB ? "" : (this.tab?.url || ""); inp.select();
      }
    });
  }

  // ------------------------------------------------------------ page view input
  bindView() {
    const c = this.el.canvas, sink = this.el.sink;
    const pos = (e) => {
      const r = c.getBoundingClientRect();
      return { x: Math.round((e.clientX - r.left) * (c.width / r.width)), y: Math.round((e.clientY - r.top) * (c.height / r.height)) };
    };
    const BTN = ["left", "middle", "right"];
    const input = (ev) => this.send({ cmd: "input", tab: this.active, ev });
    let moveRaf = null, lastMove = null;
    c.addEventListener("mousedown", (e) => {
      this.closeMenu();
      sink.focus({ preventScroll: true });
      if (e.button === 2) return;                              // right button -> own context menu
      e.preventDefault();
      input({ type: "mouse", action: "down", button: BTN[e.button] || "left", clicks: e.detail || 1, ...pos(e) });
    });
    c.addEventListener("mouseup", (e) => {
      if (e.button === 2) return;
      input({ type: "mouse", action: "up", button: BTN[e.button] || "left", clicks: e.detail || 1, ...pos(e) });
    });
    c.addEventListener("mousemove", (e) => {
      lastMove = pos(e);
      if (moveRaf) return;
      moveRaf = requestAnimationFrame(() => { moveRaf = null; input({ type: "mouse", action: "move", ...lastMove }); });
    });
    c.addEventListener("mouseleave", () => { this.el.status.style.display = "none"; });
    c.addEventListener("wheel", (e) => {
      e.preventDefault();
      if (e.ctrlKey) { this.zoomBy(e.deltaY < 0 ? 0.1 : -0.1); return; }
      const k = e.deltaMode === 1 ? 40 : e.deltaMode === 2 ? this.vp.h : 1;
      input({ type: "wheel", dx: e.deltaX * k, dy: e.deltaY * k, ...pos(e) });
    }, { passive: false });
    c.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      const p = pos(e);
      this.ctxAt = { clientX: e.clientX, clientY: e.clientY };
      this.send({ cmd: "ctx", tab: this.active, ...p });
    });
    // keyboard: a hidden textarea keeps the focus, so paste/copy events work
    c.addEventListener("focus", () => sink.focus({ preventScroll: true }));
    sink.addEventListener("keydown", (e) => {
      if (this.handleShortcut(e)) return;
      const k = e.key;
      const mod = e.ctrlKey || e.metaKey;
      if (mod && !e.altKey && (k === "v" || k === "V")) return;         // -> paste event
      if (["Control", "Shift", "Alt", "Meta", "AltGraph", "CapsLock"].includes(k)) return;
      if (e.isComposing || k === "Process") return;
      e.preventDefault();
      input({ type: "key", key: k, ctrl: e.ctrlKey, alt: e.altKey, shift: e.shiftKey, meta: e.metaKey });
    });
    sink.addEventListener("paste", (e) => {
      e.preventDefault();
      const text = e.clipboardData?.getData("text/plain") || "";
      if (text) input({ type: "paste", text });
    });
    sink.addEventListener("input", () => {                         // IME / dictation / emoji picker
      if (sink.value) { input({ type: "text", text: sink.value }); sink.value = ""; }
    });
  }

  // browser shortcuts (handled by the dashboard, not sent to the page)
  handleShortcut(e) {
    const mod = e.ctrlKey || e.metaKey;
    const k = e.key.toLowerCase();
    const t = this.tab;
    const hit = (fn) => { e.preventDefault(); e.stopPropagation(); fn(); return true; };
    if (mod && !e.altKey) {
      if (k === "t") return hit(() => this.send({ cmd: "new-tab" }));
      if (k === "w") return hit(() => this.send({ cmd: "close-tab", tab: this.active }));
      if (k === "l") return hit(() => { this.el.url.focus(); this.el.url.select(); });
      if (k === "r") return hit(() => this.send({ cmd: "reload", tab: this.active, hard: e.shiftKey }));
      if (k === "f") return hit(() => this.openFind());
      if (k === "d") return hit(() => this.starPopup());
      if (k === "h") return hit(() => this.library("history"));
      if (k === "j") return hit(() => this.library("downloads"));
      if (k === "b" && e.shiftKey) return hit(() => this.library("bookmarks"));
      if (k === "+" || k === "=") return hit(() => this.zoomBy(0.1));
      if (k === "-") return hit(() => this.zoomBy(-0.1));
      if (k === "0") return hit(() => this.send({ cmd: "zoom", tab: this.active, zoom: 1 }));
      if (k === "tab") return hit(() => this.cycleTab(e.shiftKey ? -1 : 1));
      if (k === "pagedown") return hit(() => this.cycleTab(1));
      if (k === "pageup") return hit(() => this.cycleTab(-1));
      if (k === "u") return hit(() => t && isWeb(t.url) && this.send({ cmd: "new-tab", url: "view-source:" + t.url, next: true }));
      if (/^[1-9]$/.test(k)) return hit(() => {
        const i = k === "9" ? this.tabs.length - 1 : +k - 1;
        if (this.tabs[i]) this.send({ cmd: "activate", tab: this.tabs[i].id });
      });
    }
    if (e.altKey && !mod) {
      if (e.key === "ArrowLeft") return hit(() => this.send({ cmd: "back", tab: this.active }));
      if (e.key === "ArrowRight") return hit(() => this.send({ cmd: "forward", tab: this.active }));
      if (e.key === "Home") return hit(() => this.send({ cmd: "home", tab: this.active }));
    }
    if (e.key === "F5") return hit(() => this.send({ cmd: "reload", tab: this.active, hard: mod }));
    if (e.key === "F6") return hit(() => { this.el.url.focus(); this.el.url.select(); });
    if (e.key === "Escape" && t?.loading) { this.send({ cmd: "stop", tab: this.active }); }
    return false;
  }

  bindShortcuts() {
    // shortcuts also work while the focus is in the toolbar of this window
    this.root.addEventListener("keydown", (e) => {
      if (e.target === this.el.sink || e.target === this.el.url) return;
      if (e.target.closest?.(".fxb-dlg, .fxb-panel, .fxb-find, .fxb-newtab")) return;
      this.handleShortcut(e);
    });
  }

  cycleTab(d) {
    if (!this.tabs.length) return;
    const i = this.tabs.findIndex((t) => t.id === this.active);
    const n = this.tabs[(i + d + this.tabs.length) % this.tabs.length];
    this.send({ cmd: "activate", tab: n.id });
  }

  zoomBy(d) {
    const t = this.tab; if (!t) return;
    const steps = [0.3, 0.5, 0.67, 0.8, 0.9, 1, 1.1, 1.2, 1.33, 1.5, 1.7, 2, 2.4, 3];
    const cur = t.zoom || 1;
    const next = d > 0 ? steps.find((s) => s > cur + 0.001) : [...steps].reverse().find((s) => s < cur - 0.001);
    if (next) this.send({ cmd: "zoom", tab: t.id, zoom: next });
  }

  async copyText(text) {
    try { await navigator.clipboard.writeText(text); return; } catch { /* fallback */ }
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.cssText = "position:fixed;left:-9999px";
    document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); } catch { /* ignore */ }
    ta.remove();
    this.el.sink.focus({ preventScroll: true });
  }

  // ------------------------------------------------------------ menus (generic)
  openMenu(items, at) {
    this.closeMenu();
    const m = document.createElement("div");
    m.className = "fxb-menu";
    m.innerHTML = items.map((it, i) => it === "-" ? `<div class="sep"></div>`
      : it.header ? `<div class="hd">${esc(it.header)}</div>`
      : it.html ? `<div data-raw="${i}">${it.html}</div>`
      : `<div class="mi ${it.disabled ? "dis" : ""}" data-i="${i}">${it.icon ? `<span style="width:18px;text-align:center">${it.icon}</span>` : `<span style="width:18px"></span>`}<span>${esc(it.label)}</span>${it.key ? `<span class="k">${esc(it.key)}</span>` : ""}</div>`).join("");
    this.root.appendChild(m);
    const rb = this.root.getBoundingClientRect();
    let x, y;
    if (at instanceof Element) {
      const r = at.getBoundingClientRect();
      x = r.right - rb.left; y = r.bottom - rb.top + 2;
      m.style.left = Math.max(4, x - m.offsetWidth) + "px";
      if (at.closest(".fxb-tabs, .fxb-marks") || at.classList.contains("fxb-ident")) m.style.left = Math.max(4, Math.min(r.left - rb.left, rb.width - m.offsetWidth - 4)) + "px";
    } else {
      x = at.clientX - rb.left; y = at.clientY - rb.top;
      m.style.left = Math.max(4, Math.min(x, rb.width - m.offsetWidth - 4)) + "px";
    }
    m.style.top = Math.max(4, Math.min(y, rb.height - m.offsetHeight - 4)) + "px";
    m.querySelectorAll("[data-i]").forEach((el) => el.addEventListener("click", () => {
      const it = items[+el.dataset.i];
      if (it.disabled) return;
      if (!it.keep) this.closeMenu();
      it.run?.();
    }));
    items.forEach((it, i) => { if (it.html && it.bind) it.bind(m.querySelector(`[data-raw="${i}"]`)); });
    this.menuEl = m;
    return m;
  }

  closeMenu() { this.menuEl?.remove(); this.menuEl = null; }

  networkMenu(anchor, pick, header, current) {
    this.send({ cmd: "refresh-networks" });
    const items = [{ header }, ...this.networks.map((n) => ({
      icon: n.kind === "agent" ? `<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${esc(n.color)}"></span>` : "🗄",
      label: `${n.name}${n.kind === "agent" ? (n.online ? "" : " (agent offline)") : ""}${n.id === current ? "  ✓" : ""}`,
      run: () => pick(n.id), disabled: n.id === current,
    }))];
    if (this.networks.length < 2) items.push("-", { label: "Agent networks: create an agent link in the VPN app", disabled: true });
    this.openMenu(items, anchor);
  }

  tabMenu(id, e) {
    const t = this.tabs.find((x) => x.id === id);
    this.openMenu([
      { icon: "＋", label: "New Tab", key: "Ctrl+T", run: () => this.send({ cmd: "new-tab", next: true }) },
      "-",
      { icon: "↻", label: "Reload Tab", run: () => this.send({ cmd: "reload", tab: id }) },
      { icon: "⧉", label: "Duplicate Tab", run: () => this.send({ cmd: "duplicate", tab: id }) },
      { icon: "☆", label: "Bookmark Tab", run: () => t && this.addBookmark(t.title, t.url), disabled: !t || !isWeb(t.url) },
      { icon: "🔗", label: "Open in network …", run: () => setTimeout(() => this.networkMenu(e, (net) => this.send({ cmd: "network", tab: id, network: net }), "Open this tab in", t?.network)) },
      "-",
      { icon: "✕", label: "Close Tab", key: "Ctrl+W", run: () => this.send({ cmd: "close-tab", tab: id }) },
      { label: "Close Other Tabs", run: () => this.send({ cmd: "close-others", tab: id }), disabled: this.tabs.length < 2 },
    ], e);
  }

  tabListMenu(btn) {
    this.openMenu(this.tabs.map((t) => ({
      icon: t.favicon ? `<img src="${esc(t.favicon)}" style="width:14px;height:14px">` : "🌐",
      label: (t.id === this.active ? "• " : "") + (t.title || "").slice(0, 60),
      run: () => this.send({ cmd: "activate", tab: t.id }),
    })), btn);
  }

  mainMenu(btn) {
    const t = this.tab;
    const zoom = Math.round((t?.zoom || 1) * 100);
    this.openMenu([
      { icon: "＋", label: "New tab", key: "Ctrl+T", run: () => this.send({ cmd: "new-tab" }) },
      { icon: "🔗", label: "New tab in network …", run: () => setTimeout(() => this.networkMenu(btn, (net) => this.send({ cmd: "new-tab", network: net }), "Open new tab in")) },
      "-",
      { icon: "★", label: "Bookmarks", key: "Ctrl+Shift+B", run: () => this.library("bookmarks") },
      { icon: "🕘", label: "History", key: "Ctrl+H", run: () => this.library("history") },
      { icon: "⬇", label: "Downloads", key: "Ctrl+J", run: () => this.library("downloads") },
      "-",
      { html: `<div class="zoomrow"><span style="flex:1">Zoom</span><button data-z="-">−</button><button data-z="1" style="min-width:56px">${zoom} %</button><button data-z="+">＋</button></div>`,
        bind: (el) => el.querySelectorAll("[data-z]").forEach((b) => b.addEventListener("click", () => {
          if (b.dataset.z === "+") this.zoomBy(0.1); else if (b.dataset.z === "-") this.zoomBy(-0.1);
          else this.send({ cmd: "zoom", tab: t?.id, zoom: 1 });
          setTimeout(() => this.mainMenu(btn), 120);
        })) },
      "-",
      { icon: "🔍", label: "Find in page …", key: "Ctrl+F", run: () => this.openFind() },
      { icon: "</>", label: "View page source", key: "Ctrl+U", disabled: !t || !isWeb(t.url),
        run: () => this.send({ cmd: "new-tab", url: "view-source:" + t.url, next: true }) },
      "-",
      { icon: "⭐", label: "Save page as web app …", disabled: !t || !isWeb(t.url) || !!this.app, run: () => this.saveAsApp() },
      { icon: "⚙", label: "Settings", run: () => this.settingsDialog() },
      { icon: "🧹", label: "Clear cookies and site data …", run: () => this.clearCookies() },
    ], btn);
  }

  pageMenu(m) {
    const i = m.info || {};
    const t = this.tab;
    const at = this.ctxAt || { clientX: 100, clientY: 100 };
    const items = [];
    if (i.link) {
      items.push({ label: "Open Link in New Tab", run: () => this.send({ cmd: "new-tab", url: i.link, next: true, background: true }) },
        { label: "Open Link in New Tab and switch", run: () => this.send({ cmd: "new-tab", url: i.link, next: true }) },
        { label: "Bookmark Link", run: () => this.addBookmark(i.linkText || i.link, i.link) },
        { label: "Save Link As …", run: () => this.send({ cmd: "save-url", tab: t?.id, url: i.link }) },
        { label: "Copy Link", run: () => this.copyText(i.link) }, "-");
    }
    if (i.img) {
      items.push({ label: "Open Image in New Tab", run: () => this.send({ cmd: "new-tab", url: i.img, next: true }) },
        { label: "Save Image As …", run: () => this.send({ cmd: "save-url", tab: t?.id, url: i.img }) },
        { label: "Copy Image Link", run: () => this.copyText(i.img) }, "-");
    }
    if (i.selection) {
      const s = i.selection.trim().slice(0, 40);
      items.push({ label: "Copy", key: "Ctrl+C", run: () => this.copyText(i.selection) },
        { label: `Search for “${s}${i.selection.length > 40 ? "…" : ""}”`, run: () => this.send({ cmd: "new-tab", url: i.selection.slice(0, 300), next: true }) }, "-");
    }
    if (i.editable) {
      items.push({ label: "Paste", key: "Ctrl+V", run: async () => {
        try {
          const text = await navigator.clipboard.readText();
          if (text) this.send({ cmd: "input", tab: this.active, ev: { type: "paste", text } });
        } catch { this.toast("Press Ctrl+V to paste (the browser blocked clipboard access)."); }
      } }, "-");
    }
    items.push({ icon: "←", label: "Back", disabled: !t?.canBack, run: () => this.send({ cmd: "back", tab: t?.id }) },
      { icon: "→", label: "Forward", disabled: !t?.canForward, run: () => this.send({ cmd: "forward", tab: t?.id }) },
      { icon: "↻", label: "Reload", run: () => this.send({ cmd: "reload", tab: t?.id }) },
      { icon: "☆", label: "Bookmark Page", disabled: !t || !isWeb(t.url), run: () => this.addBookmark(t.title, t.url) },
      "-",
      { label: "Select All", key: "Ctrl+A", run: () => this.send({ cmd: "select-all", tab: t?.id }) },
      { label: "View Page Source", disabled: !t || !isWeb(t.url), run: () => this.send({ cmd: "new-tab", url: "view-source:" + t.url, next: true }) });
    this.openMenu(items, at);
  }

  // <select> of the page -> own list at the same place
  selectList(m) {
    const r = this.el.canvas.getBoundingClientRect(), rb = this.root.getBoundingClientRect();
    const k = r.width / this.el.canvas.width;
    const box = document.createElement("div");
    box.className = "fxb-menu";
    box.style.minWidth = Math.max(120, m.rect.w * k) + "px";
    const answer = (vals) => { box.remove(); this.menuEl = null; this.send({ cmd: "select", values: vals }); this.el.sink.focus(); };
    let group = null;
    box.innerHTML = m.options.map((o, i) => {
      let h = "";
      if (o.group && o.group !== group) { group = o.group; h += `<div class="hd">${esc(o.group)}</div>`; }
      return h + `<div class="mi ${o.disabled ? "dis" : ""}" data-o="${i}" style="${o.selected ? "background:var(--fx-hover);font-weight:600" : ""}">
        ${m.multiple ? `<input type="checkbox" ${o.selected ? "checked" : ""} style="pointer-events:none">` : ""}${esc(o.label)}</div>`;
    }).join("") + (m.multiple ? `<div class="sep"></div><div style="display:flex;justify-content:flex-end;padding:4px 10px"><button class="btn btn-sm btn-primary" data-ok>OK</button></div>` : "");
    this.closeMenu();
    this.root.appendChild(box);
    this.menuEl = box;
    const left = r.left - rb.left + m.rect.x * k, top = r.top - rb.top + (m.rect.y + m.rect.h) * k;
    box.style.left = Math.max(4, Math.min(left, rb.width - box.offsetWidth - 4)) + "px";
    box.style.top = (top + box.offsetHeight > rb.height ? Math.max(4, r.top - rb.top + m.rect.y * k - box.offsetHeight) : top) + "px";
    const chosen = new Set(m.options.map((o, i) => o.selected ? i : -1).filter((i) => i >= 0));
    box.querySelectorAll("[data-o]").forEach((el) => el.addEventListener("click", () => {
      const i = +el.dataset.o;
      if (m.options[i].disabled) return;
      if (!m.multiple) { answer([i]); return; }
      chosen.has(i) ? chosen.delete(i) : chosen.add(i);
      el.querySelector("input").checked = chosen.has(i);
    }));
    box.querySelector("[data-ok]")?.addEventListener("click", () => answer([...chosen]));
    const onAway = (e) => { if (!box.contains(e.target)) { document.removeEventListener("mousedown", onAway, true); if (box.isConnected) answer(null); } };
    setTimeout(() => document.addEventListener("mousedown", onAway, true));
  }

  // ------------------------------------------------------------ page dialogs / files / downloads
  pageDialog(m) {
    const ov = document.createElement("div");
    ov.className = "fxb-overlay";
    ov.dataset.dialog = m.id;
    const host = hostOf(this.tabs.find((t) => t.id === m.tab)?.url || "");
    ov.innerHTML = `<div class="fxb-dlg">
      <div style="font-weight:600;margin-bottom:8px">${esc(host || "This page")} says</div>
      <div style="white-space:pre-wrap;max-height:300px;overflow:auto">${esc(m.message)}</div>
      ${m.type === "prompt" ? `<input type="text" style="margin-top:10px" value="${esc(m.default)}">` : ""}
      <div class="row">${m.type !== "alert" ? `<button class="btn" data-no>Cancel</button>` : ""}<button class="btn btn-primary" data-yes>OK</button></div></div>`;
    this.el.view.appendChild(ov);
    const inp = ov.querySelector("input");
    const done = (accept) => { this.send({ cmd: "dialog", id: m.id, accept, text: inp?.value || "" }); ov.remove(); this.el.sink.focus(); };
    ov.querySelector("[data-yes]").onclick = () => done(true);
    ov.querySelector("[data-no]")?.addEventListener("click", () => done(false));
    ov.addEventListener("keydown", (e) => { e.stopPropagation(); if (e.key === "Enter") done(true); if (e.key === "Escape") done(m.type === "alert"); });
    setTimeout(() => (inp || ov.querySelector("[data-yes]")).focus(), 20);
  }

  fileChooser(m) {
    const ov = document.createElement("div");
    ov.className = "fxb-overlay";
    ov.innerHTML = `<div class="fxb-dlg">
      <div style="font-weight:600;margin-bottom:8px">📎 File upload</div>
      <div style="color:var(--fx-sub);margin-bottom:10px">The page wants ${m.multiple ? "one or more files" : "a file"}. The selected files are sent to the page through the server.</div>
      <input type="file" ${m.multiple ? "multiple" : ""}>
      <div data-st style="margin-top:8px;font-size:12px"></div>
      <div class="row"><button class="btn" data-no>Cancel</button><button class="btn btn-primary" data-yes>Upload</button></div></div>`;
    this.el.view.appendChild(ov);
    const fi = ov.querySelector("input");
    ov.querySelector("[data-no]").onclick = () => { this.send({ cmd: "chooser-cancel", id: m.id }); ov.remove(); };
    ov.querySelector("[data-yes]").onclick = async () => {
      if (!fi.files.length) return;
      const fd = new FormData();
      for (const f of fi.files) fd.append("files", f, f.name);
      ov.querySelector("[data-st]").textContent = "Uploading …";
      try { await bq(`/upload/${m.id}`, { method: "POST", form: fd }); ov.remove(); }
      catch (e) { ov.querySelector("[data-st]").innerHTML = `<span style="color:var(--danger)">${esc(e.message)}</span>`; }
    };
    setTimeout(() => fi.click(), 50);
  }

  onDownload(m) {
    this.downloads.set(m.id, m);
    const running = [...this.downloads.values()].filter((d) => d.state === "running").length;
    const fresh = [...this.downloads.values()].filter((d) => d.state === "done" && !d.seen).length;
    this.el.dlBadge.hidden = !(running || fresh);
    this.el.dlBadge.textContent = running || fresh;
    if (m.state === "done") this.toast(`⬇ <b>${esc(m.name)}</b> downloaded (${fmtSize(m.size)}) – <a href="#" data-get="${esc(m.id)}">save to this computer</a>`, 8000);
    if (m.state === "failed") this.toast(`⚠️ Download of <b>${esc(m.name)}</b> failed: ${esc(m.error || "")}`, 8000);
    if (this.panel?.dataset.kind === "downloads") this.library("downloads");
  }

  async fetchDownload(id, name) {
    try {
      const res = await bq(`/downloads/${encodeURIComponent(id)}`, { raw: true });
      const url = URL.createObjectURL(await res.blob());
      const a = Object.assign(document.createElement("a"), { href: url, download: name || "download" });
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 3000);
      const d = this.downloads.get(id); if (d) d.seen = true;
    } catch (e) { this.toast(`⚠️ ${esc(e.message)}`); }
  }

  toast(html, ms = 5000) {
    const el = document.createElement("div");
    el.className = "fxb-toast";
    el.innerHTML = html;
    this.root.querySelectorAll(".fxb-toast").forEach((x) => x.remove());
    this.root.appendChild(el);
    el.querySelector("[data-get]")?.addEventListener("click", (e) => {
      e.preventDefault(); const d = this.downloads.get(e.target.dataset.get); this.fetchDownload(d.id, d.name); el.remove();
    });
    setTimeout(() => el.remove(), ms);
  }

  // ------------------------------------------------------------ find bar
  bindFind() {
    const f = this.el.find, inp = this.el.findInput;
    let t = null;
    const run = (back = false) => this.send({ cmd: "find", tab: this.active, text: inp.value, backwards: back });
    inp.addEventListener("input", () => { clearTimeout(t); t = setTimeout(() => run(), 200); });
    inp.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "Enter") { e.preventDefault(); run(e.shiftKey); }
      if (e.key === "Escape") this.closeFind();
    });
    f.querySelector("[data-f=next]").onclick = () => run(false);
    f.querySelector("[data-f=prev]").onclick = () => run(true);
    f.querySelector("[data-f=close]").onclick = () => this.closeFind();
  }
  openFind() { this.el.find.hidden = false; this.layout(); this.el.findInput.focus(); this.el.findInput.select(); }
  closeFind() { this.el.find.hidden = true; this.el.findCount.textContent = ""; this.layout(); this.el.sink.focus(); }

  // ------------------------------------------------------------ new-tab page
  renderNewTab(t) {
    this.root.querySelector(".fxb-newtab")?.remove();
    const el = document.createElement("div");
    el.className = "fxb-newtab";
    el.dataset.tab = t.id;
    const tiles = this.bookmarks.items.slice(0, 24);
    const eng = this.engines.find((x) => x.id === this.settings.search)?.name || "DuckDuckGo";
    el.innerHTML = `<div class="logo">🧭</div>
      <div class="srch"><span>🔍</span><input placeholder="Search with ${esc(eng)} or enter address"></div>
      <div class="tiles">${tiles.map((b) => `<div class="tile" data-u="${esc(b.url)}" title="${esc(b.url)}">
          <div class="sq"><img src="${esc(this.faviconFor(b.url))}" alt="" onerror="this.replaceWith(document.createTextNode('${esc((b.title || hostOf(b.url) || "?").trim()[0] || "?").toUpperCase().replace(/'/g, "")}'))"></div>
          <div class="nm">${esc(b.title || hostOf(b.url))}</div></div>`).join("")}</div>
      ${tiles.length ? "" : `<div style="color:var(--fx-sub);margin-top:10px">Bookmarks you add (☆ in the address bar) show up here.</div>`}
      ${this.networks.length > 1 ? `<div style="margin-top:26px;color:var(--fx-sub);font-size:12px">Network of this tab: <b>${esc(this.netOf(t.network).name)}</b> – change it with the button left of the address.</div>` : ""}`;
    this.el.view.appendChild(el);
    const inp = el.querySelector("input");
    inp.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "Enter" && inp.value.trim()) this.send({ cmd: "navigate", tab: t.id, url: inp.value.trim() });
    });
    el.querySelectorAll("[data-u]").forEach((x) => x.addEventListener("click", (e) => {
      if (e.ctrlKey || e.metaKey) this.send({ cmd: "new-tab", url: x.dataset.u, background: true, next: true });
      else this.send({ cmd: "navigate", tab: t.id, url: x.dataset.u });
    }));
    setTimeout(() => { if (document.activeElement !== this.el.url) inp.focus(); }, 30);
  }

  // favicons of bookmarks: origin/favicon.ico can't be loaded from the dashboard
  // (internal hosts); use the stored icon if a tab showed the page before
  faviconFor(url) {
    const b = this.bookmarks.items.find((x) => x.url === url);
    if (b?.icon) return b.icon;
    const t = this.tabs.find((x) => hostOf(x.url) && hostOf(x.url) === hostOf(url) && x.favicon);
    return t?.favicon || "data:,";
  }

  // ------------------------------------------------------------ bookmarks
  async loadBookmarks() {
    try { this.bookmarks = await bq("/bookmarks"); } catch { /* no permission / offline */ }
    this.renderMarks();
    this.renderChrome();
  }

  renderMarks() {
    const el = this.el.marks;
    const bar = this.bookmarks.items.filter((b) => b.folder === "toolbar");
    const folders = (this.bookmarks.folders || []).filter((f) => f !== "toolbar" && f !== "menu");
    el.innerHTML = bar.map((b) => `<button class="fxb-mark" data-b="${esc(b.id)}" title="${esc(b.title)}\n${esc(b.url)}">
        <img src="${esc(this.faviconFor(b.url))}" onerror="this.style.display='none'">${esc(b.title || hostOf(b.url) || b.url)}</button>`).join("")
      + folders.map((f) => `<button class="fxb-mark" data-folder="${esc(f)}">📁 ${esc(f)}</button>`).join("")
      + `<span style="flex:1"></span><button class="fxb-mark" data-folder="menu" title="Other bookmarks">📁 Other Bookmarks</button>`;
    if (!bar.length && !folders.length) el.insertAdjacentHTML("afterbegin", `<span style="color:var(--fx-sub);font-size:12px;padding:0 6px">For quick access, place your bookmarks here on the bookmarks toolbar.</span>`);
    const open = (b, e) => {
      if (e.button === 1 || e.ctrlKey || e.metaKey) this.send({ cmd: "new-tab", url: b.url, background: true, next: true });
      else this.send({ cmd: "navigate", tab: this.active, url: b.url });
    };
    el.querySelectorAll("[data-b]").forEach((x) => {
      const b = this.bookmarks.items.find((i) => i.id === x.dataset.b);
      x.addEventListener("click", (e) => open(b, e));
      x.addEventListener("auxclick", (e) => { if (e.button === 1) open(b, e); });
      x.addEventListener("contextmenu", (e) => { e.preventDefault(); this.bookmarkMenu(b, e); });
    });
    el.querySelectorAll("[data-folder]").forEach((x) => x.addEventListener("click", () => {
      const items = this.bookmarks.items.filter((b) => b.folder === x.dataset.folder);
      this.openMenu(items.length ? items.map((b) => ({ icon: "☆", label: b.title || b.url, run: () => this.send({ cmd: "navigate", tab: this.active, url: b.url }) }))
        .concat(["-", { label: "Open All in Tabs", run: () => items.forEach((b) => this.send({ cmd: "new-tab", url: b.url, background: true })) }])
        : [{ label: "(Empty)", disabled: true }], x);
    }));
  }

  bookmarkMenu(b, e) {
    this.openMenu([
      { label: "Open", run: () => this.send({ cmd: "navigate", tab: this.active, url: b.url }) },
      { label: "Open in New Tab", run: () => this.send({ cmd: "new-tab", url: b.url, next: true }) },
      { label: "Open in network …", run: () => setTimeout(() => this.networkMenu(e, (net) => this.send({ cmd: "new-tab", url: b.url, network: net }), "Open in")) },
      "-",
      { label: "Edit Bookmark …", run: () => this.editBookmark(b) },
      { label: "Copy Link", run: () => this.copyText(b.url) },
      { label: "Delete Bookmark", run: () => this.deleteBookmark(b.id) },
    ], e);
  }

  async addBookmark(title, url, folder = "toolbar") {
    try {
      const t = this.tabs.find((x) => x.url === url);
      await bq("/bookmarks", { method: "POST", body: { title: title || url, url, folder } });
      await this.loadBookmarks();
      if (t?.favicon) { /* icon is taken from open tabs */ }
      this.toast("★ Bookmark added", 2500);
    } catch (e) { this.toast(`⚠️ ${esc(e.message)}`); }
  }

  async deleteBookmark(id) {
    try { await bq(`/bookmarks/${id}`, { method: "DELETE" }); await this.loadBookmarks(); }
    catch (e) { this.toast(`⚠️ ${esc(e.message)}`); }
  }

  starPopup() {
    const t = this.tab;
    if (!t || !isWeb(t.url)) return;
    const b = this.bookmarks.items.find((x) => x.url === t.url);
    if (b) { this.editBookmark(b, true); return; }
    bq("/bookmarks", { method: "POST", body: { title: t.title, url: t.url, folder: "toolbar" } })
      .then(async (nb) => { await this.loadBookmarks(); this.editBookmark(nb, true, true); })
      .catch((e) => this.toast(`⚠️ ${esc(e.message)}`));
  }

  editBookmark(b, fromStar = false, isNew = false) {
    const folders = [...new Set(["toolbar", "menu", ...(this.bookmarks.folders || [])])];
    const label = (f) => f === "toolbar" ? "Bookmarks Toolbar" : f === "menu" ? "Other Bookmarks" : f;
    const ov = document.createElement("div");
    ov.className = "fxb-overlay";
    ov.innerHTML = `<div class="fxb-dlg" style="min-width:380px">
      <div style="font-weight:600;font-size:15px;margin-bottom:12px">${isNew ? "Bookmark added" : "Edit bookmark"}</div>
      <label style="font-size:12px;color:var(--fx-sub)">Name</label><input type="text" data-n value="${esc(b.title)}">
      <label style="font-size:12px;color:var(--fx-sub);display:block;margin-top:8px">URL</label><input type="text" data-u value="${esc(b.url)}">
      <label style="font-size:12px;color:var(--fx-sub);display:block;margin-top:8px">Folder</label>
      <select data-f>${folders.map((f) => `<option value="${esc(f)}" ${f === b.folder ? "selected" : ""}>${esc(label(f))}</option>`).join("")}
        <option value="__new">New folder …</option></select>
      <div class="row"><button class="btn btn-danger" data-del style="margin-right:auto">Remove bookmark</button>
        <button class="btn" data-no>Cancel</button><button class="btn btn-primary" data-yes>Save</button></div></div>`;
    this.root.appendChild(ov);
    const sel = ov.querySelector("[data-f]");
    sel.addEventListener("change", async () => {
      if (sel.value !== "__new") return;
      const name = await uiPrompt("New bookmark folder", { placeholder: "Folder name" });
      if (!name) { sel.value = b.folder; return; }
      try { await bq("/folders", { method: "POST", body: { name } }); } catch { /* exists */ }
      sel.insertAdjacentHTML("afterbegin", `<option value="${esc(name)}">${esc(name)}</option>`);
      sel.value = name;
    });
    const close = () => { ov.remove(); this.el.sink.focus(); };
    ov.addEventListener("mousedown", (e) => { if (e.target === ov) close(); });
    ov.addEventListener("keydown", (e) => { e.stopPropagation(); if (e.key === "Escape") close(); });
    ov.querySelector("[data-no]").onclick = close;
    ov.querySelector("[data-del]").onclick = async () => { close(); await this.deleteBookmark(b.id); };
    ov.querySelector("[data-yes]").onclick = async () => {
      try {
        await bq(`/bookmarks/${b.id}`, { method: "PUT", body: { title: ov.querySelector("[data-n]").value,
          url: ov.querySelector("[data-u]").value, folder: sel.value === "__new" ? b.folder : sel.value } });
        close(); await this.loadBookmarks();
        if (this.panel?.dataset.kind === "bookmarks") this.library("bookmarks");
      } catch (e) { this.toast(`⚠️ ${esc(e.message)}`); }
    };
    setTimeout(() => ov.querySelector("[data-n]").select(), 20);
  }

  // ------------------------------------------------------------ library (history / bookmarks / downloads)
  async library(kind) {
    this.panel?.remove();
    const p = document.createElement("div");
    p.className = "fxb-panel";
    p.dataset.kind = kind;
    p.innerHTML = `<div class="ph"><div class="tabs">
        <button data-k="history" class="${kind === "history" ? "on" : ""}">🕘 History</button>
        <button data-k="bookmarks" class="${kind === "bookmarks" ? "on" : ""}">★ Bookmarks</button>
        <button data-k="downloads" class="${kind === "downloads" ? "on" : ""}">⬇ Downloads</button></div>
        <button class="fxb-ib" data-close title="Close">✕</button></div>
      <div style="padding:8px 12px;display:flex;gap:6px"><input type="text" data-q placeholder="Search" style="flex:1">
        ${kind === "history" ? `<button class="btn btn-sm" data-clear>Clear history</button>` : ""}
        ${kind === "bookmarks" ? `<button class="btn btn-sm" data-folder>New folder</button>` : ""}</div>
      <div class="pb">Loading …</div>`;
    this.root.appendChild(p);
    this.panel = p;
    p.querySelector("[data-close]").onclick = () => { p.remove(); this.panel = null; this.el.sink.focus(); };
    p.querySelectorAll("[data-k]").forEach((b) => b.onclick = () => this.library(b.dataset.k));
    p.addEventListener("keydown", (e) => { e.stopPropagation(); if (e.key === "Escape") p.querySelector("[data-close]").click(); });
    const pb = p.querySelector(".pb"), q = p.querySelector("[data-q]");
    const openUrl = (u, e) => (e.ctrlKey || e.metaKey || e.button === 1)
      ? this.send({ cmd: "new-tab", url: u, background: true }) : this.send({ cmd: "navigate", tab: this.active, url: u });
    const draw = async () => {
      const f = q.value.trim().toLowerCase();
      if (kind === "history") {
        let h = [];
        try { h = (await bq(`/history?q=${encodeURIComponent(f)}&limit=300`)).history; } catch (e) { pb.textContent = e.message; return; }
        let day = "";
        pb.innerHTML = h.map((x) => {
          const d = new Date(x.ts * 1000).toLocaleDateString();
          const hd = d !== day ? `<div class="hd" style="padding:8px 14px 2px;color:var(--fx-sub);font-size:11px">${(day = d)}</div>` : "";
          return `${hd}<div class="it" data-u="${esc(x.url)}"><div class="m"><div>${esc(x.title || x.url)}</div><div class="s">${esc(x.url)}</div></div>
            <span class="s">${new Date(x.ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span></div>`;
        }).join("") || `<div style="padding:14px;color:var(--fx-sub)">No history.</div>`;
        pb.querySelectorAll("[data-u]").forEach((el) => el.addEventListener("click", (e) => openUrl(el.dataset.u, e)));
      } else if (kind === "bookmarks") {
        const items = this.bookmarks.items.filter((b) => !f || (b.title + " " + b.url).toLowerCase().includes(f));
        const folders = [...new Set(["toolbar", "menu", ...(this.bookmarks.folders || [])])];
        const label = (x) => x === "toolbar" ? "Bookmarks Toolbar" : x === "menu" ? "Other Bookmarks" : x;
        pb.innerHTML = folders.map((fo) => {
          const list = items.filter((b) => b.folder === fo);
          if (!list.length && f) return "";
          return `<div style="padding:8px 14px 2px;color:var(--fx-sub);font-size:11px;display:flex;align-items:center">📁 ${esc(label(fo))}
            ${fo !== "toolbar" && fo !== "menu" ? `<button class="fxb-ib" data-delf="${esc(fo)}" title="Delete folder" style="margin-left:auto;width:22px;height:22px">🗑</button>` : ""}</div>`
            + (list.map((b) => `<div class="it" data-b="${esc(b.id)}"><div class="m"><div>${esc(b.title || b.url)}</div><div class="s">${esc(b.url)}</div></div>
              <button class="fxb-ib" data-edit="${esc(b.id)}" title="Edit">✎</button><button class="fxb-ib" data-del="${esc(b.id)}" title="Delete">🗑</button></div>`).join("")
              || `<div class="s" style="padding:2px 14px 6px;color:var(--fx-sub);font-size:12px">(empty)</div>`);
        }).join("");
        pb.querySelectorAll("[data-b]").forEach((el) => el.addEventListener("click", (e) => {
          if (e.target.closest("button")) return;
          openUrl(this.bookmarks.items.find((b) => b.id === el.dataset.b).url, e);
        }));
        pb.querySelectorAll("[data-edit]").forEach((b) => b.onclick = () => this.editBookmark(this.bookmarks.items.find((x) => x.id === b.dataset.edit)));
        pb.querySelectorAll("[data-del]").forEach((b) => b.onclick = async () => { await this.deleteBookmark(b.dataset.del); draw(); });
        pb.querySelectorAll("[data-delf]").forEach((b) => b.onclick = async () => {
          if (!(await uiConfirm(`Delete folder “${b.dataset.delf}”? Its bookmarks move to “Other Bookmarks”.`, { okText: "Delete", danger: true }))) return;
          try { await bq(`/folders/${encodeURIComponent(b.dataset.delf)}`, { method: "DELETE" }); } catch (e) { this.toast(esc(e.message)); }
          await this.loadBookmarks(); draw();
        });
      } else {
        let list = [];
        try { list = (await bq("/downloads")).downloads; } catch (e) { pb.textContent = e.message; return; }
        const running = [...this.downloads.values()].filter((d) => d.state === "running");
        pb.innerHTML = running.map((d) => `<div class="it"><span class="fxb-spin"></span><div class="m"><div>${esc(d.name)}</div><div class="s">Downloading on the server …</div></div></div>`).join("")
          + list.filter((d) => !f || d.name.toLowerCase().includes(f)).map((d) => `<div class="it">
            <span style="font-size:18px">📄</span><div class="m"><div>${esc(d.name)}</div><div class="s">${fmtSize(d.size)} · ${new Date(d.ts * 1000).toLocaleString()}</div></div>
            <button class="btn btn-sm" data-get="${esc(d.id)}" data-name="${esc(d.name)}">Save</button>
            <button class="fxb-ib" data-rm="${esc(d.id)}" title="Delete from server">🗑</button></div>`).join("")
          || `<div style="padding:14px;color:var(--fx-sub)">No downloads. Files the pages download are kept on the server for 7 days.</div>`;
        pb.querySelectorAll("[data-get]").forEach((b) => b.onclick = () => this.fetchDownload(b.dataset.get, b.dataset.name));
        pb.querySelectorAll("[data-rm]").forEach((b) => b.onclick = async () => {
          try { await bq(`/downloads/${encodeURIComponent(b.dataset.rm)}`, { method: "DELETE" }); } catch { /* ignore */ }
          this.downloads.delete(b.dataset.rm); draw();
        });
        this.downloads.forEach((d) => { d.seen = true; });
        this.el.dlBadge.hidden = !running.length;
      }
    };
    let t = null;
    q.addEventListener("input", () => { clearTimeout(t); t = setTimeout(draw, 200); });
    p.querySelector("[data-clear]")?.addEventListener("click", async () => {
      if (!(await uiConfirm("Clear the complete browsing history?", { okText: "Clear", danger: true }))) return;
      await bq("/history", { method: "DELETE" }).catch(() => {}); draw();
    });
    p.querySelector("[data-folder]")?.addEventListener("click", async () => {
      const name = await uiPrompt("New bookmark folder", { placeholder: "Folder name" });
      if (!name) return;
      try { await bq("/folders", { method: "POST", body: { name } }); await this.loadBookmarks(); draw(); }
      catch (e) { this.toast(esc(e.message)); }
    });
    draw();
  }

  // ------------------------------------------------------------ settings
  async settingsDialog() {
    let d;
    try { d = await bq("/settings"); } catch (e) { this.toast(esc(e.message)); return; }
    this.settings = d.settings; this.engines = d.engines;
    const s = d.settings;
    const ov = document.createElement("div");
    ov.className = "fxb-overlay";
    ov.innerHTML = `<div class="fxb-dlg" style="min-width:420px">
      <div style="font-weight:600;font-size:15px;margin-bottom:12px">⚙ Browser settings</div>
      <div style="display:grid;grid-template-columns:150px 1fr;gap:10px;align-items:center">
        <label>Home page</label><input type="text" data-home value="${s.homepage === NEWTAB ? "" : esc(s.homepage)}" placeholder="empty = New Tab page">
        <label>Search engine</label><select data-search>${d.engines.map((e) => `<option value="${esc(e.id)}" ${e.id === s.search ? "selected" : ""}>${esc(e.name)}</option>`).join("")}</select>
        <label>Default network</label><select data-net>${this.networks.map((n) => `<option value="${esc(n.id)}" ${n.id === s.default_network ? "selected" : ""}>${esc(n.name)}</option>`).join("")}</select>
        <label>Startup</label><label style="display:flex;gap:6px;align-items:center"><input type="checkbox" data-restore ${s.restore ? "checked" : ""}> Open previous tabs</label>
      </div>
      <div style="color:var(--fx-sub);font-size:12px;margin-top:12px">The browser runs on the RMM server and keeps running while this window is closed.
        Cookies and logins are stored encrypted per network on the server.</div>
      <div class="row"><button class="btn" data-no>Cancel</button><button class="btn btn-primary" data-yes>Save</button></div></div>`;
    this.root.appendChild(ov);
    const close = () => ov.remove();
    ov.addEventListener("keydown", (e) => { e.stopPropagation(); if (e.key === "Escape") close(); });
    ov.querySelector("[data-no]").onclick = close;
    ov.querySelector("[data-yes]").onclick = async () => {
      try {
        const r = await bq("/settings", { method: "PUT", body: { homepage: ov.querySelector("[data-home]").value.trim() || NEWTAB,
          search: ov.querySelector("[data-search]").value, default_network: ov.querySelector("[data-net]").value,
          restore: ov.querySelector("[data-restore]").checked } });
        this.settings = r.settings; close(); this.renderChrome();
      } catch (e) { this.toast(`⚠️ ${esc(e.message)}`); }
    };
  }

  async saveAsApp() {
    const t = this.tab;
    if (!t) return;
    const name = await uiPrompt("Save as web app", { value: t.title || hostOf(t.url),
      description: "The page gets its own entry in the start menu and opens in its own window." });
    if (!name) return;
    const { addWebApp } = await import("./webbrowser.js");
    addWebApp({ name, url: t.url, icon: "🌐" });
    this.toast(`⭐ “${esc(name)}” was added to the start menu`, 3500);
  }

  async clearCookies() {
    if (!(await uiConfirm("Clear cookies and site data?", { description: "You will be signed out of all websites in the server browser (all networks). Open tabs are reloaded.", okText: "Clear", danger: true }))) return;
    try {
      await bq("/cookies", { method: "DELETE" });                     // server closes all views -> reconnect
      this.toast("🧹 Cookies and site data cleared", 3000);
    } catch (e) { this.toast(`⚠️ ${esc(e.message)}`); }
  }
}
