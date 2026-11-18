// terminalemu.js
// --------------
// ECHTER Terminal-Emulator für alle Shell-/Konsolen-Fenster des Dashboards.
//
// Unter der Haube läuft xterm.js - LOKAL gebündelt unter /js/vendor/xterm/
// (kein CDN, funktioniert also weiter im abgeschotteten LAN ohne Internet).
// Damit funktioniert alles, was ein richtiges Terminal kann:
//   * Farben: 16/256 Farben UND Truecolor (htop, ls --color, vim-Themes ...)
//   * Alle Tasten: Pfeile, Pos1/Ende, Bild auf/ab, F1-F12, Entf, Einfg,
//     Alt-/Strg-Kombinationen - 1:1 wie in einem echten xterm
//   * MAUS: Klicks, Scrollrad und Ziehen werden an Programme gemeldet, die
//     Maus-Unterstützung anfordern (htop, mc, tmux, vim :set mouse=a ...)
//   * Alternate Screen, Scrollback, Cursor-Formen, Bracketed Paste usw.
//
// Zwischenablage: Markieren per Maus; Strg+Shift+C kopiert, Strg+V /
// Strg+Shift+V fügt ein (Strg+C bleibt frei für SIGINT in der Shell).
// Hat ein Programm die Maus übernommen (htop, mc ...), markiert man mit
// GEDRÜCKTER Shift-Taste - exakt wie in xterm/gnome-terminal.
//
// Die Klasse ist ein Drop-in-Ersatz für den alten MiniTerm (gleiche API:
// new Term(host, { onData, onResize }) / write / fit / focus / dispose /
// cols / rows). xterm.js lädt asynchron; bis dahin wird write() gepuffert.
// Lässt sich xterm.js nicht laden (z.B. halb eingespieltes Update ohne die
// vendor-Dateien), springt automatisch der alte MiniTerm ein - das Terminal
// bleibt in jedem Fall benutzbar.

// Farbschema = die bisherigen MiniTerm-Farben (Tokyo-Night-Palette).
const THEME = {
  background: "#0b0f14", foreground: "#c0caf5",
  cursor: "#c0caf5", cursorAccent: "#0b0f14",
  selectionBackground: "rgba(122,162,247,.35)",
  black: "#0b0f14", red: "#f7768e", green: "#9ece6a", yellow: "#e0af68",
  blue: "#7aa2f7", magenta: "#bb9af7", cyan: "#7dcfff", white: "#c0caf5",
  brightBlack: "#414868", brightRed: "#ff9e9e", brightGreen: "#b9f27c",
  brightYellow: "#ffc777", brightBlue: "#9fb4ff", brightMagenta: "#d7b9ff",
  brightCyan: "#a4e8ff", brightWhite: "#ffffff",
};

let _libPromise = null;
function _loadLib() {
  if (!_libPromise) {
    _libPromise = (async () => {
      // Stylesheet von xterm.js einmalig einbinden.
      if (!document.getElementById("xterm-vendor-css")) {
        const link = document.createElement("link");
        link.id = "xterm-vendor-css";
        link.rel = "stylesheet";
        link.href = "/js/vendor/xterm/xterm.css";
        document.head.appendChild(link);
      }
      const [x, f] = await Promise.all([
        import("/js/vendor/xterm/xterm.js"),
        import("/js/vendor/xterm/addon-fit.js"),
      ]);
      if (!x?.Terminal || !f?.FitAddon) throw new Error("xterm-Vendor-Dateien unvollständig");
      return { Terminal: x.Terminal, FitAddon: f.FitAddon };
    })();
    // Fehlgeschlagenes Laden nicht einfrieren - nächster Versuch darf neu laden.
    _libPromise.catch(() => { _libPromise = null; });
  }
  return _libPromise;
}

export class Term {
  constructor(host, opts = {}) {
    this.host = host;
    this.onData = opts.onData || (() => {});
    this.onResize = opts.onResize || (() => {});
    this.cols = 80;
    this.rows = 24;
    this._buf = [];          // write()-Puffer, bis xterm bereit ist
    this._dead = false;
    this._wantFocus = false;
    this._term = null;       // xterm.js-Instanz
    this._fitAddon = null;
    this._fallback = null;   // alter MiniTerm, falls xterm nicht ladbar
    this._init();
  }

  async _init() {
    let lib = null;
    try {
      lib = await _loadLib();
    } catch (e) {
      console.warn("[terminal] xterm.js nicht ladbar - benutze MiniTerm-Fallback:", e);
    }
    if (this._dead) return;

    if (!lib) {
      // ---- Fallback: alter, eingebauter MiniTerm ----
      const { MiniTerm } = await import("./miniterm.js");
      if (this._dead) return;
      this._fallback = new MiniTerm(this.host, {
        onData: (d) => this.onData(d),
        onResize: (c, r) => { this.cols = c; this.rows = r; this.onResize(c, r); },
      });
      this.cols = this._fallback.cols;
      this.rows = this._fallback.rows;
      const buf = this._buf; this._buf = null;
      for (const d of buf) this._fallback.write(d);
      if (this._wantFocus) this._fallback.focus();
      return;
    }

    // ---- Normalfall: xterm.js ----
    const { Terminal, FitAddon } = lib;
    const term = new Terminal({
      allowProposedApi: true,
      cursorBlink: true,
      scrollback: 8000,
      fontSize: 13,
      fontFamily: "Menlo, Consolas, 'DejaVu Sans Mono', monospace",
      theme: THEME,
    });
    this._fitAddon = new FitAddon();
    term.loadAddon(this._fitAddon);
    this.host.style.background = THEME.background;
    term.open(this.host);

    // Strg+Shift+C kopiert die Markierung (Strg+C geht als SIGINT an die
    // Shell), Strg+Shift+V fügt ein. Strg+V läuft über das normale
    // Browser-Paste-Event, das xterm.js selbst behandelt.
    term.attachCustomKeyEventHandler((ev) => {
      if (ev.type !== "keydown") return true;
      if (ev.ctrlKey && ev.shiftKey && (ev.key === "C" || ev.key === "c")) {
        const sel = term.getSelection();
        if (sel) { navigator.clipboard?.writeText(sel).catch(() => {}); return false; }
        return true;
      }
      if (ev.ctrlKey && ev.shiftKey && (ev.key === "V" || ev.key === "v")) {
        navigator.clipboard?.readText?.()
          .then((txt) => { if (txt) term.paste(txt); })
          .catch(() => {});
        return false;
      }
      return true;
    });

    term.onData((d) => this.onData(d));
    // Maus-Reports u.ä. kommen teils binär - 1:1 durchreichen.
    term.onBinary((d) => this.onData(d));
    term.onResize(({ cols, rows }) => {
      this.cols = cols; this.rows = rows;
      this.onResize(cols, rows);
    });

    this._term = term;
    this.fit();
    const buf = this._buf; this._buf = null;
    for (const d of buf) term.write(d);
    if (this._wantFocus) term.focus();
  }

  /** Ausgabe vom Server einspeisen. */
  write(data) {
    if (this._buf) { this._buf.push(data); return; }
    if (this._fallback) { this._fallback.write(data); return; }
    this._term?.write(data);
  }

  /** Terminal an die Größe des Hosts anpassen (löst onResize aus). */
  fit() {
    if (this._fallback) { try { this._fallback.fit(); } catch { /* egal */ } return; }
    if (!this._term || !this._fitAddon) return;
    // Unsichtbare/0-Pixel-Hosts überspringen - FitAddon rechnet sonst mit NaN.
    if (!this.host.clientWidth || !this.host.clientHeight) return;
    try { this._fitAddon.fit(); } catch { /* egal */ }
  }

  focus() {
    this._wantFocus = true;
    if (this._fallback) this._fallback.focus();
    else this._term?.focus();
  }

  /** Kompletten Zelleninhalt löschen (für Replays/Logs). */
  clear() {
    if (this._fallback) { this._fallback.write("\x1b[2J\x1b[H"); return; }
    if (this._buf) { this._buf.length = 0; return; }
    this._term?.reset();
  }

  dispose() {
    this._dead = true;
    try { this._fallback?.dispose(); } catch { /* egal */ }
    try { this._term?.dispose(); } catch { /* egal */ }
    this._fallback = null;
    this._term = null;
  }
}

// Drop-in-Alias: Aufrufer, die bisher `MiniTerm` importiert haben, können
// einfach den Import-Pfad auf diese Datei ändern.
export { Term as MiniTerm };
