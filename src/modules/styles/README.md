# Modul „Styles“

Weitere Oberflächen für BYTERA RMM. Der **Standard „Futuristisch“** (Desktop mit
Fenstern, Startmenü, Widgets) steckt im Core und braucht dieses Modul nicht.
Alles andere kommt von hier:

| Style          | ID             | Ordner                  | Aufbau                                                        |
|----------------|----------------|-------------------------|---------------------------------------------------------------|
| Classic        | `standard`     | `styles/classic/`       | flach, Navigation links mit den Apps des Startmenüs, Apps als Tabs, Dashboard bleibt |
| Classic Hard   | `classic_hard` | `styles/classic_hard/`  | wie Proxmox: Baum links, rechts Kategorien mit fester Ansicht, keine Widgets |

Jeder Benutzer wählt seinen Style im **Profil → Oberfläche**. Den Standard für
neue Benutzer legt der Admin unter **Einstellungen → Allgemein → Oberfläche**
fest. Ist das Modul aus, gilt für alle Futuristisch – die gespeicherte Wahl
bleibt erhalten und greift wieder, sobald das Modul an ist.

---

## Aufbau

```
src/modules/styles/
├── module.json
├── README.md                ← diese Datei
├── backend/
│   ├── module.py
│   ├── registry.py          ← liest frontend/styles/*/style.json
│   └── styles_routes.py     ← GET /api/styles
└── frontend/
    ├── styles.js            ← lädt nach dem Login den Style des Benutzers
    ├── engine/              ← Layout-Engines, die Styles benutzen können
    │   ├── classic.js       ← Navigation links + Tabs + Statusleiste
    │   ├── classicview.js   ← Proxmox-Ansicht (Kategorien statt Widgets)
    │   └── layout.css
    └── styles/              ← HIER LIEGEN DIE STYLES – ein Ordner je Style
        ├── classic/
        │   ├── style.json
        │   └── style.css
        └── classic_hard/
            ├── style.json
            └── style.css
```

---

## Eigenen Style hinzufügen

1. Ordner anlegen: `src/modules/styles/frontend/styles/<mein-style>/`
2. Darin eine `style.json` und eine CSS-Datei ablegen (Beispiel unten).
3. Fertig – der Style erscheint **sofort** im Profil zur Auswahl, ohne Neustart.
   (Browser ggf. einmal mit Strg+F5 neu laden.)

### `style.json`

```json
{
  "id": "nacht",
  "name": "Nachtblau",
  "description": "Dunkles Blau, große Schrift.",
  "order": 50,
  "bodyClass": "style-nacht",
  "layout": "none",
  "css": ["style.css"],
  "js": "style.js"
}
```

| Feld          | Pflicht | Bedeutung |
|---------------|---------|-----------|
| `id`          | nein    | Eindeutige ID (Kleinbuchstaben, Ziffern, `-`, `_`). Ohne Angabe = Ordnername. `futuristic` ist reserviert. |
| `name`        | ja      | Name in der Auswahl. |
| `description` | nein    | Kurzbeschreibung (erscheint hinter dem Namen). |
| `order`       | nein    | Reihenfolge in der Auswahl (kleiner = weiter oben, Standard 100). |
| `bodyClass`   | nein    | CSS-Klasse(n) für `<body>`, solange der Style aktiv ist. Standard: `style-<id>`. |
| `layout`      | nein    | `none` = Bedienung wie Futuristisch, nur anderes Aussehen (Standard) · `classic` = Navigation links + Tabs · `classic-hard` = wie Proxmox. |
| `css`         | nein    | Liste von CSS-Dateien, relativ zum Style-Ordner. Auch fremde Styles lassen sich mitbenutzen, z. B. `"../classic/style.css"`. |
| `js`          | nein    | Optionales Script (ES-Modul) mit `export async function apply(ctx)` – für alles, was CSS allein nicht kann. |

Zusätzlich gilt während der Style aktiv ist immer `body[data-style="<id>"]` – damit
lassen sich Regeln auch ohne eigene `bodyClass` gezielt setzen.

### `style.css` – Beispiel

Am einfachsten überschreibt man die Farb-Variablen der Oberfläche:

```css
body.style-nacht {
  --bg: #0b1220;
  --panel: #111a2e;
  --panel-2: #16223b;
  --border: #23314f;
  --text: #e6ecff;
  --subtext: #8ea0c8;
  font-size: 15px;
}
body.style-nacht .rmm-window { border-radius: 2px; }
```

Wichtige Variablen: `--bg`, `--panel`, `--panel-2`, `--border`, `--text`,
`--subtext`, `--accent` / `--accent-rgb` (Akzentfarbe aus dem Profil),
`--online`, `--warn`, `--danger`. Hell/Dunkel: zusätzlich `body.theme-light`.

### `style.js` – optional

```js
// ctx = { style, base, module, req }
//   base = URL des Style-Ordners (für eigene Bilder/Dateien)
//   req  = authentifizierte Anfragen ans Backend, z. B. await req("/api/clients")
export async function apply(ctx) {
  const logo = document.querySelector(".topbar-title");
  if (logo) logo.title = `Style: ${ctx.style.name}`;
}
```

Für tiefere Eingriffe stellt der Core Andockstellen bereit
(`/js/app/styles.js`):

| Funktion                       | Wofür |
|--------------------------------|-------|
| `setMainContentRenderer(fn)`   | Hauptbereich (Dashboard/Auswahl) selbst zeichnen – `fn(el)`, Rückgabe `false` = Core zeichnet |
| `onMetrics(fn)`                | Neue Live-Werte eines Clients – `fn(clientId)` |
| `onWindowsChanged(fn)`         | Fensterliste hat sich geändert |

```js
import { setMainContentRenderer } from "/js/app/styles.js";
export async function apply() {
  setMainContentRenderer((el) => { el.innerHTML = "<h1>Mein Dashboard</h1>"; return true; });
}
```

---

## Sicherheit / Fehler

* Schlägt ein Style beim Laden fehl (kaputtes CSS-Pfad, Fehler im Script), fällt
  die Oberfläche automatisch auf **Futuristisch** zurück und zeigt eine Meldung –
  sie bleibt bedienbar.
* Eine fehlerhafte `style.json` wird übersprungen (Hinweis im Backend-Log),
  die übrigen Styles bleiben wählbar.
* Styles sind normale Frontend-Dateien des Moduls und werden nur ausgeliefert,
  solange das Modul aktiv ist. Keine Zugangsdaten o. Ä. in Style-Dateien ablegen.
