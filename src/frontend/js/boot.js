/*
 * boot.js - SICHERER LADER fuer BYTERA RMM
 * =========================================
 * Einzige Aufgabe: alles andere laden - und wenn dabei etwas schiefgeht, eine
 * verstaendliche Fehlermeldung zeigen, statt das ganze RMM lahmzulegen.
 *
 * Warum ein eigener Lader?
 *   Das Dashboard besteht aus vielen ES-Modulen. Fehlt in EINER Datei ein
 *   Export (z.B. "does not provide an export named clientLinkPath") oder hat
 *   sie einen Syntaxfehler, startet der Browser den GESAMTEN Modulbaum nicht:
 *   leere Seite, keine Anmeldung, kein Update, kein Hotfix.
 *
 * Deshalb gilt hier:
 *   * Diese Datei ist ein KLASSISCHES Script (kein Modul, keine Imports). Sie
 *     kann selbst nie an einem kaputten Modul scheitern.
 *   * Die eigentliche Oberflaeche wird per import() geladen - jeder Teil fuer
 *     sich, mit try/catch.
 *   * Scheitert der Hauptteil (app.js), erscheint der NOTFALL-MODUS: Fehler im
 *     Klartext, Hinweise des Servers (GET /api/frontend/health), "Cache leeren
 *     & neu laden", Anmeldung und - fuer Admins - Server-Update sowie
 *     Hotfix-ZIP einspielen und Backend neu starten. Alles ohne die normale
 *     Oberflaeche.
 *   * Der Server ergaenzt fehlende Exporte beim Ausliefern durch Platzhalter
 *     (app/frontend_guard.py). Ruft die Oberflaeche einen solchen Platzhalter
 *     auf, zeigt dieser Lader einen Hinweis-Balken (window.__rmmMissingExport),
 *     statt dass etwas stillschweigend fehlt.
 *
 * Bewusst einfaches JavaScript ohne Abhaengigkeiten, damit es auch dann noch
 * laeuft, wenn sonst gar nichts mehr geht.
 */
(function () {
  "use strict";

  var BOOT = (window.__rmmBoot = { errors: [], ready: false, started: Date.now() });
  var TOKEN_KEY = "rmm_token";

  // Haupt-Oberflaeche (critical) + Zusatzteile, die einzeln scheitern duerfen
  var PARTS = [
    { url: "/js/app/icons.js", name: "Symbole", critical: false },
    { url: "/js/app/app.js", name: "Oberfläche", critical: true },
    { url: "/js/app/mobile.js", name: "Handy-Ansicht", critical: false },
  ];

  // ------------------------------------------------------------ Hilfen
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function el(html) {
    var d = document.createElement("div");
    d.innerHTML = html.trim();
    return d.firstChild;
  }
  function token() { try { return localStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; } }
  function api(path, opts) {
    opts = opts || {};
    var headers = opts.headers || {};
    var t = token();
    if (t) headers.Authorization = "Bearer " + t;
    if (opts.json !== undefined) {
      headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.json);
    }
    return fetch(path, { method: opts.method || "GET", headers: headers, body: opts.body, cache: "no-store" })
      .then(function (res) {
        return res.text().then(function (txt) {
          var data = null;
          try { data = txt ? JSON.parse(txt) : null; } catch (e) { data = txt; }
          if (!res.ok) {
            var msg = data && data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : ("HTTP " + res.status);
            var err = new Error(msg); err.status = res.status; throw err;
          }
          return data;
        });
      });
  }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function describe(err) {
    if (!err) return "Unbekannter Fehler";
    var m = String(err.message || err);
    var ex = /does not provide an export named ['"]?([\w$]+)['"]?/.exec(m) || /export named ['"]?([\w$]+)/.exec(m);
    var mod = /module ['"]([^'"]+)['"]/.exec(m);
    if (ex) {
      return "Die Datei " + (mod ? "„" + mod[1] + "“" : "?") + " stellt die Funktion „" + ex[1] +
        "“ nicht bereit, eine andere Datei braucht sie aber. Meist sind Dateien aus verschiedenen Versionen gemischt " +
        "(Update/Hotfix nur teilweise eingespielt) oder der Browser/Proxy liefert alte Dateien aus dem Cache.";
    }
    if (err.name === "SyntaxError") return "Eine Datei enthält einen Syntaxfehler: " + m;
    if (/Failed to fetch dynamically imported module|error loading dynamically imported module|Importing a module script failed/i.test(m)) {
      return "Eine Datei der Oberfläche konnte nicht geladen werden (Netzwerk, Proxy-Drossel oder fehlende Datei): " + m;
    }
    return m;
  }

  // ------------------------------------------------------------ Hinweis-Balken (Oberflaeche laeuft)
  var seenMissing = {};
  function banner(html, key) {
    if (key && document.getElementById("rmm-boot-banner-" + key)) return;
    var b = el('<div class="rmm-boot-banner"' + (key ? ' id="rmm-boot-banner-' + esc(key) + '"' : "") + ">" + html +
      ' <button data-a="reload">Cache leeren &amp; neu laden</button><button data-a="x" title="Ausblenden">×</button></div>');
    b.addEventListener("click", function (e) {
      var a = e.target && e.target.getAttribute && e.target.getAttribute("data-a");
      if (a === "x") b.remove();
      if (a === "reload") hardReload();
    });
    var host = document.getElementById("rmm-boot-banners");
    if (!host) { host = el('<div id="rmm-boot-banners"></div>'); document.body.appendChild(host); }
    host.appendChild(b);
  }

  // Wird von den Platzhaltern aufgerufen, die der Server fuer fehlende Exporte einsetzt
  window.__rmmMissingExport = function (file, name) {
    var k = file + "#" + name;
    if (seenMissing[k]) return;
    seenMissing[k] = true;
    try { console.warn("[BYTERA] fehlende Funktion '" + name + "' aus " + file + " aufgerufen (Platzhalter)."); } catch (e) {}
    banner("⚠ Ein Teil der Oberfläche passt nicht zur Server-Version: Funktion <b>" + esc(name) + "</b> fehlt in <code>" +
      esc(file) + "</code>. Diese Funktion ist vorübergehend ohne Wirkung.", "m" + Object.keys(seenMissing).length);
  };

  // ------------------------------------------------------------ Cache leeren + neu laden
  function hardReload() {
    var p = api("/api/frontend/health").catch(function () { return { files: [] }; }).then(function (h) {
      var files = (h && h.files) || [];
      files = files.concat(["/index.html", "/js/boot.js", "/css/style.css", "/css/ui.css", "/css/mobile.css", "/css/classic.css"]);
      // Jede Datei einmal frisch vom Server holen -> ersetzt alte Fassungen im Browser-Cache
      var jobs = files.map(function (u) { return fetch(u, { cache: "reload" }).catch(function () {}); });
      return Promise.all(jobs);
    });
    var clearCaches = (window.caches && caches.keys) ? caches.keys().then(function (ks) {
      return Promise.all(ks.map(function (k) { return caches.delete(k); }));
    }).catch(function () {}) : Promise.resolve();
    var sw = (navigator.serviceWorker && navigator.serviceWorker.getRegistrations)
      ? navigator.serviceWorker.getRegistrations().then(function (rs) { return Promise.all(rs.map(function (r) { return r.unregister(); })); }).catch(function () {})
      : Promise.resolve();
    Promise.all([p, clearCaches, sw]).then(function () { location.reload(); }, function () { location.reload(); });
  }
  window.__rmmHardReload = hardReload;

  // ------------------------------------------------------------ Styles (eigenstaendig)
  function injectStyles() {
    if (document.getElementById("rmm-boot-style")) return;
    var css = [
      "#rmm-boot-fail{position:fixed;inset:0;z-index:2147483000;overflow:auto;background:#0f141b;color:#e3e7ec;",
      "font:14px/1.5 system-ui,'Segoe UI',sans-serif;display:flex;justify-content:center;padding:28px 14px}",
      "#rmm-boot-fail .bx{width:100%;max-width:760px}",
      "#rmm-boot-fail h1{font-size:20px;margin:0 0 4px}#rmm-boot-fail h2{font-size:15px;margin:22px 0 8px}",
      "#rmm-boot-fail .sub{color:#9aa6b2;margin:0 0 16px}",
      "#rmm-boot-fail .card{background:#18202b;border:1px solid #2b3747;border-radius:6px;padding:14px 16px;margin:10px 0}",
      "#rmm-boot-fail .err{border-color:#7a3a3a;background:#241819}",
      "#rmm-boot-fail pre{white-space:pre-wrap;word-break:break-word;background:#0b0f15;border:1px solid #2b3747;border-radius:4px;",
      "padding:8px 10px;font-size:12px;max-height:220px;overflow:auto;margin:8px 0 0}",
      "#rmm-boot-fail button,#rmm-boot-fail select,#rmm-boot-fail input{font:inherit;font-size:13px}",
      "#rmm-boot-fail button{background:#243041;color:#e3e7ec;border:1px solid #3a4a60;border-radius:4px;padding:6px 12px;cursor:pointer;margin:4px 6px 0 0}",
      "#rmm-boot-fail button.p{background:#2563eb;border-color:#2563eb;color:#fff}#rmm-boot-fail button:disabled{opacity:.5;cursor:default}",
      "#rmm-boot-fail input,#rmm-boot-fail select{background:#0b0f15;color:#e3e7ec;border:1px solid #3a4a60;border-radius:4px;padding:6px 8px;width:100%;box-sizing:border-box;margin:3px 0 8px}",
      "#rmm-boot-fail label{font-size:12px;color:#9aa6b2}#rmm-boot-fail .msg{font-size:13px;margin-top:8px}",
      "#rmm-boot-fail .ok{color:#4ade80}#rmm-boot-fail .bad{color:#f87171}#rmm-boot-fail code{background:#0b0f15;padding:1px 5px;border-radius:3px}",
      "#rmm-boot-fail li{margin:3px 0}",
      "#rmm-boot-banners{position:fixed;left:50%;transform:translateX(-50%);top:6px;z-index:2147482000;display:flex;flex-direction:column;gap:6px;width:min(900px,96vw)}",
      ".rmm-boot-banner{background:#3a2a0b;color:#fde7b0;border:1px solid #a16207;border-radius:6px;padding:8px 12px;",
      "font:13px/1.4 system-ui,'Segoe UI',sans-serif;box-shadow:0 6px 20px rgba(0,0,0,.35)}",
      ".rmm-boot-banner code{background:rgba(0,0,0,.3);padding:0 4px;border-radius:3px}",
      ".rmm-boot-banner button{margin-left:8px;background:#a16207;color:#fff;border:0;border-radius:4px;padding:3px 9px;cursor:pointer;font:inherit}",
      ".rmm-boot-banner button[data-a=x]{background:transparent;color:#fde7b0;font-size:16px;padding:0 4px}",
    ].join("");
    var s = document.createElement("style");
    s.id = "rmm-boot-style";
    s.textContent = css;
    document.head.appendChild(s);
  }

  // ------------------------------------------------------------ Notfall-Modus
  function emergency(part, err) {
    injectStyles();
    var old = document.getElementById("rmm-boot-fail");
    if (old) old.remove();
    var details = BOOT.errors.map(function (e) { return e.part + ": " + e.message + (e.stack ? "\n" + e.stack : ""); }).join("\n\n");
    var root = el(
      '<div id="rmm-boot-fail"><div class="bx">' +
      "<h1>⚠ BYTERA RMM konnte die Oberfläche nicht laden</h1>" +
      '<p class="sub">Die Seite ist absichtlich nicht komplett ausgefallen: Hier kannst du dich trotzdem anmelden, ' +
      "ein Update oder einen Hotfix einspielen und neu starten.</p>" +
      '<div class="card err"><b>Was ist passiert?</b><div class="msg">' + esc(describe(err)) + "</div>" +
      "<pre>" + esc(details || String(err && err.stack || err)) + "</pre></div>" +
      '<div class="card" id="bf-health"><b>Prüfung durch den Server</b><div class="msg">wird geladen …</div></div>' +
      '<div class="card"><b>Schnelle Hilfe</b><div class="msg">Oft reicht es, alte Dateien aus dem Browser-Cache zu werfen.</div>' +
      '<button class="p" data-a="hard">Cache leeren &amp; neu laden</button><button data-a="retry">Nur neu laden</button></div>' +
      '<div class="card" id="bf-login"></div>' +
      '<div id="bf-tools"></div>' +
      "</div></div>");
    document.body.appendChild(root);
    root.querySelector('[data-a="hard"]').onclick = hardReload;
    root.querySelector('[data-a="retry"]').onclick = function () { location.reload(); };
    // Normale Oberflaeche ausblenden (sie ist ohnehin nicht nutzbar)
    try { ["login-screen", "app", "change-pw-screen"].forEach(function (id) { var x = document.getElementById(id); if (x) x.style.display = "none"; }); } catch (e) {}
    loadHealth();
    showLogin();
  }

  function loadHealth() {
    var box = document.querySelector("#bf-health .msg");
    api("/api/frontend/health").then(function (h) {
      if (!h || !h.missing || !h.missing.length) {
        box.innerHTML = '<span class="ok">✓</span> Der Server findet keine fehlenden Funktionen zwischen den Dateien. ' +
          "Wahrscheinlich ist es ein Syntaxfehler, eine fehlende Datei oder der Cache – siehe oben. " +
          "Ein vollständiges Update bzw. ein Hotfix mit den betroffenen Dateien behebt das.";
        return;
      }
      box.innerHTML = "Diese Dateien passen nicht zusammen (der Server ergänzt beim Ausliefern Platzhalter – nach " +
        "„Cache leeren &amp; neu laden“ startet die Oberfläche meist wieder; dauerhaft hilft ein vollständiges Update):<ul>" +
        h.missing.map(function (m) {
          return "<li><code>" + esc(m.importers.join(", ")) + "</code> braucht <b>" + esc(m.name) + "</b> aus <code>" + esc(m.file) + "</code></li>";
        }).join("") + "</ul>";
    }).catch(function (e) {
      box.innerHTML = '<span class="bad">Server nicht erreichbar:</span> ' + esc(e.message) +
        " – läuft das Backend? (Bei Neustart/Update kurz warten und neu laden.)";
    });
  }

  // ---- Anmeldung (ohne die normale Oberflaeche) ----
  function showLogin() {
    var box = document.getElementById("bf-login");
    if (token()) {
      api("/api/auth/me").then(function (u) { loggedIn(u); }, function () {
        try { localStorage.removeItem(TOKEN_KEY); } catch (e) {}
        drawLoginForm(box);
      });
      return;
    }
    drawLoginForm(box);
  }

  function drawLoginForm(box) {
    box.innerHTML = "<b>Notfall-Anmeldung</b>" +
      '<div class="msg">Für Update, Hotfix und Neustart (Administratoren).</div>' +
      '<form id="bf-form" autocomplete="on" style="margin-top:8px">' +
      '<div id="bf-realm-row" style="display:none"><label>Anmeldung über</label><select id="bf-realm"><option value="local">Lokal</option></select></div>' +
      '<label>Benutzername</label><input id="bf-user" autocomplete="username" required>' +
      '<label>Passwort</label><input id="bf-pass" type="password" autocomplete="current-password" required>' +
      '<div id="bf-code-row" style="display:none"><label>Einmalcode (Authenticator) oder Wiederherstellungscode</label>' +
      '<input id="bf-code" inputmode="numeric" autocomplete="one-time-code"></div>' +
      '<button class="p" type="submit">Anmelden</button><div class="msg" id="bf-login-msg"></div></form>';
    api("/api/auth/realms").then(function (r) {
      var list = (r && (r.realms || r)) || [];
      if (!Array.isArray(list) || !list.length) return;
      var sel = box.querySelector("#bf-realm");
      list.forEach(function (x) {
        if (!x || x.id === "local") return;
        var o = document.createElement("option"); o.value = x.id; o.textContent = x.name || x.id; sel.appendChild(o);
      });
      if (sel.options.length > 1) box.querySelector("#bf-realm-row").style.display = "";
    }).catch(function () {});
    box.querySelector("#bf-form").onsubmit = function (e) {
      e.preventDefault();
      var msg = box.querySelector("#bf-login-msg");
      msg.textContent = "Anmeldung …";
      api("/api/auth/login", { method: "POST", json: {
        username: box.querySelector("#bf-user").value.trim(),
        password: box.querySelector("#bf-pass").value,
        realm: box.querySelector("#bf-realm").value || "local",
        code: box.querySelector("#bf-code").value.trim(),
      } }).then(function (r) {
        try { localStorage.setItem(TOKEN_KEY, r.token); } catch (err) {}
        loggedIn(r.user);
      }).catch(function (err) {
        if (/2fa_required/.test(err.message)) {
          box.querySelector("#bf-code-row").style.display = "";
          box.querySelector("#bf-code").focus();
          msg.innerHTML = "Bitte den Einmalcode eingeben.";
          return;
        }
        msg.innerHTML = '<span class="bad">' + esc(err.message) + "</span>";
      });
    };
  }

  function loggedIn(u) {
    var box = document.getElementById("bf-login");
    box.innerHTML = '<b>Angemeldet</b><div class="msg">als <b>' + esc(u.display_name || u.username) + "</b>" +
      (u.must_change_pw ? " – Passwort muss geändert werden (nach der Reparatur in der normalen Oberfläche)." : "") +
      '</div><button data-a="logout">Abmelden</button>';
    box.querySelector('[data-a="logout"]').onclick = function () {
      try { localStorage.removeItem(TOKEN_KEY); } catch (e) {}
      document.getElementById("bf-tools").innerHTML = "";
      drawLoginForm(box);
    };
    drawTools();
  }

  // ---- Werkzeuge: Server-Update, Hotfix-ZIP, Neustart ----
  function drawTools() {
    var host = document.getElementById("bf-tools");
    host.innerHTML =
      '<div class="card"><b>Server-Update (aus dem Repository)</b><div class="msg" id="bf-upd-info">Stand wird abgefragt …</div>' +
      '<label>Was installieren?</label><select id="bf-upd-target">' +
      '<option value="full">Neueste stabile Version (Release)</option>' +
      '<option value="commit">Neuester Entwicklungsstand (Commit)</option>' +
      '<option value="any">Neuestes von beidem</option></select>' +
      '<button class="p" data-a="update">⬇ Update installieren</button><div class="msg" id="bf-upd-msg"></div></div>' +
      '<div class="card"><b>Hotfix einspielen (ZIP)</b>' +
      '<div class="msg">ZIP mit Projektdateien (z. B. <code>BYTERA-RMM-main/src/…</code>). Vorhandene Dateien werden überschrieben.</div>' +
      '<input type="file" id="bf-zip" accept=".zip,application/zip">' +
      '<button class="p" data-a="zip">⬆ Hotfix einspielen</button><div class="msg" id="bf-zip-msg"></div></div>' +
      '<div class="card"><b>Backend neu starten</b><div class="msg">Nötig, wenn ein Hotfix Server-Dateien (Python) geändert hat.</div>' +
      '<button data-a="restart">⟳ Backend neu starten</button><div class="msg" id="bf-rs-msg"></div></div>';

    api("/api/admin/update/info").then(function (i) {
      var latest = i.latest_release || i.latest_tag || i.latest || "";
      host.querySelector("#bf-upd-info").innerHTML = "Installiert: <b>" + esc(i.current_version || "?") + "</b>" +
        (i.current_commit ? " (" + esc(String(i.current_commit).slice(0, 8)) + ")" : "") +
        (latest ? " · Neueste: <b>" + esc(typeof latest === "string" ? latest : (latest.tag || latest.name || JSON.stringify(latest))) + "</b>" : "") +
        (i.repo_url ? "<br>Quelle: " + esc(i.repo_url) : "");
    }).catch(function (e) {
      host.querySelector("#bf-upd-info").innerHTML = '<span class="bad">' + esc(e.message) + "</span>" +
        (e.status === 403 ? " – dafür sind Administrator-Rechte nötig." : "");
    });

    host.querySelector('[data-a="update"]').onclick = function () {
      var btn = this, msg = host.querySelector("#bf-upd-msg");
      btn.disabled = true;
      msg.textContent = "Update wird gestartet …";
      api("/api/admin/update/run?background=1", { method: "POST", json: { target: host.querySelector("#bf-upd-target").value } })
        .then(function (job) { return followJob(job, msg); })
        .then(function () { waitForServerAndReload(msg); })
        .catch(function (e) { btn.disabled = false; msg.innerHTML = '<span class="bad">' + esc(e.message) + "</span>"; });
    };

    host.querySelector('[data-a="zip"]').onclick = function () {
      var btn = this, msg = host.querySelector("#bf-zip-msg");
      var f = host.querySelector("#bf-zip").files[0];
      if (!f) { msg.innerHTML = '<span class="bad">Bitte zuerst eine ZIP-Datei auswählen.</span>'; return; }
      var fd = new FormData();
      fd.append("path", "");
      fd.append("file", f);
      btn.disabled = true;
      msg.textContent = "Wird hochgeladen und entpackt …";
      fetch("/api/source/upload-zip", { method: "POST", headers: { Authorization: "Bearer " + token() }, body: fd })
        .then(function (r) { return r.json().then(function (d) { if (!r.ok) throw new Error(d && d.detail || ("HTTP " + r.status)); return d; }); })
        .then(function (d) {
          var py = (d.extracted || []).some(function (x) { return /\.py$/i.test(x); });
          msg.innerHTML = '<span class="ok">✓</span> ' + esc(d.count) + " Datei(en) eingespielt" +
            (d.skipped && d.skipped.length ? ", " + d.skipped.length + " übersprungen" : "") + ". " +
            (py ? "<b>Server-Dateien geändert → bitte Backend neu starten.</b>" : "Nur Oberflächen-Dateien → „Cache leeren &amp; neu laden“ genügt.") +
            '<br><button class="p" data-a="hard2">Cache leeren &amp; neu laden</button>';
          msg.querySelector('[data-a="hard2"]').onclick = hardReload;
          btn.disabled = false;
        })
        .catch(function (e) { btn.disabled = false; msg.innerHTML = '<span class="bad">' + esc(e.message) + "</span>"; });
    };

    host.querySelector('[data-a="restart"]').onclick = function () {
      var btn = this, msg = host.querySelector("#bf-rs-msg");
      btn.disabled = true;
      msg.textContent = "Neustart wird angefordert …";
      api("/api/admin/restart", { method: "POST" })
        .then(function () { waitForServerAndReload(msg); })
        .catch(function (e) { btn.disabled = false; msg.innerHTML = '<span class="bad">' + esc(e.message) + "</span>"; });
    };
  }

  // Backend-Job (Server-Update) verfolgen, bis er fertig ist oder neu startet
  function followJob(job, msg) {
    if (!job || !job.id) return Promise.resolve();
    var id = job.id;
    function tick() {
      return api("/api/bgjobs/" + encodeURIComponent(id)).then(function (j) {
        msg.textContent = (j.detail || j.phase || "läuft") + (j.pct != null ? " (" + Math.round(j.pct) + " %)" : "");
        if (j.phase === "restart") return true;
        if (!j.running) {
          if (j.status === "error") throw new Error(j.error || "Update fehlgeschlagen");
          if (j.status === "cancelled") throw new Error("Update abgebrochen");
          return true;
        }
        return sleep(2000).then(tick);
      }, function (e) {
        // Server startet gerade neu -> das ist beim Update erwartet
        if (!e.status || e.status >= 500) return true;
        throw e;
      });
    }
    return tick();
  }

  function waitForServerAndReload(msg) {
    var started = Date.now();
    msg.textContent = "Backend startet neu – warte, bis es wieder erreichbar ist …";
    function poll() {
      return sleep(3000).then(function () {
        return fetch("/api/frontend/health", { cache: "no-store" }).then(function (r) {
          if (r.ok && Date.now() - started > 6000) { msg.textContent = "Wieder erreichbar – lade neu …"; hardReload(); return; }
          return poll();
        }, function () {
          if (Date.now() - started > 5 * 60000) { msg.textContent = "Der Server antwortet seit 5 Minuten nicht – bitte Logs prüfen."; return; }
          return poll();
        });
      });
    }
    poll();
  }

  // ------------------------------------------------------------ Laden
  function record(part, err) {
    BOOT.errors.push({ part: part.name + " (" + part.url + ")", message: String(err && err.message || err), stack: err && err.stack || "" });
    try { console.error("[BYTERA boot] " + part.url + " konnte nicht geladen werden:", err); } catch (e) {}
  }

  function boot() {
    injectStyles();
    var chain = Promise.resolve();
    var failedCritical = null;
    PARTS.forEach(function (part) {
      chain = chain.then(function () {
        if (failedCritical) return;                 // nach einem Totalausfall nichts weiter laden
        return import(part.url).catch(function (err) {
          record(part, err);
          if (part.critical) failedCritical = { part: part, err: err };
          else banner("⚠ Teil „" + esc(part.name) + "“ konnte nicht geladen werden: " + esc(describe(err)), "p" + part.name);
        });
      });
    });
    chain.then(function () {
      if (failedCritical) { emergency(failedCritical.part, failedCritical.err); return; }
      BOOT.ready = true;
    });
  }

  // Falls die Oberflaeche zwar laedt, aber beim Start haengen bleibt (z.B.
  // unbehandelter Fehler vor dem Anzeigen der Anmeldung): Nach 20 s ohne
  // sichtbaren Anmelde- oder Hauptbildschirm den Notfall-Zugang anbieten.
  function watchdog() {
    setTimeout(function () {
      if (document.getElementById("rmm-boot-fail")) return;
      var vis = function (id) { var x = document.getElementById(id); return x && !x.classList.contains("hidden") && x.style.display !== "none"; };
      if (vis("login-screen") || vis("app") || vis("change-pw-screen")) return;
      banner("⚠ Die Oberfläche startet nicht. <button data-a=\"emerg\" style=\"margin-left:6px\">Notfall-Modus öffnen</button>", "wd");
      var b = document.querySelector('#rmm-boot-banner-wd [data-a="emerg"]');
      if (b) b.onclick = function () { emergency({ name: "Oberfläche", url: "/js/app/app.js" }, new Error("Start hängt (kein Anmeldebildschirm nach 20 s). " + (BOOT.errors[0] ? BOOT.errors[0].message : ""))); };
    }, 20000);
  }

  // Unbehandelte Fehler sammeln (fuer die Details im Notfall-Modus)
  window.addEventListener("error", function (e) {
    if (BOOT.errors.length < 30) BOOT.errors.push({ part: "Laufzeit", message: String(e.message || e.error || "Fehler"), stack: e.error && e.error.stack || "" });
  });
  window.addEventListener("unhandledrejection", function (e) {
    var r = e.reason;
    if (BOOT.errors.length < 30) BOOT.errors.push({ part: "Laufzeit (Promise)", message: String(r && r.message || r), stack: r && r.stack || "" });
  });

  // Notfall-Modus jederzeit manuell erreichbar: /?notfall=1
  if (/[?&](notfall|rescue)=1/.test(location.search)) {
    var start = function () { emergency({ name: "Manuell", url: "-" }, new Error("Notfall-Modus manuell geöffnet (?notfall=1).")); };
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
    return;
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", function () { boot(); watchdog(); });
  else { boot(); watchdog(); }
})();
