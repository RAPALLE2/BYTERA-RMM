# "Styles" module

Additional interfaces for BYTERA RMM. The **default "Futuristic"** (desktop with
windows, start menu, widgets) is built into the core and does not need this
module. Everything else comes from here:

| Style          | ID             | Folder                  | Structure                                                     |
|----------------|----------------|-------------------------|---------------------------------------------------------------|
| Classic        | `standard`     | `styles/classic/`       | flat, navigation on the left with the start menu apps, apps as tabs, dashboard stays |
| Classic Hard   | `classic_hard` | `styles/classic_hard/`  | like Proxmox: tree on the left, categories with a fixed view on the right, no widgets |

Each user picks their style in **Profile → Interface**. The admin sets the default
for new users under **Settings → General → Interface**. When the module is off,
everyone gets Futuristic – the stored choice is kept and takes effect again as
soon as the module is on.

---

## Structure

```
src/modules/styles/
├── module.json
├── README.md                ← this file
├── backend/
│   ├── module.py
│   ├── registry.py          ← reads frontend/styles/*/style.json
│   └── styles_routes.py     ← GET /api/styles
└── frontend/
    ├── styles.js            ← loads the user's style after login
    ├── engine/              ← layout engines that styles can use
    │   ├── classic.js       ← navigation on the left + tabs + status bar
    │   ├── classicview.js   ← Proxmox view (categories instead of widgets)
    │   └── layout.css
    └── styles/              ← THE STYLES LIVE HERE – one folder per style
        ├── classic/
        │   ├── style.json
        │   └── style.css
        └── classic_hard/
            ├── style.json
            └── style.css
```

---

## Add your own style

1. Create a folder: `src/modules/styles/frontend/styles/<my-style>/`
2. Put a `style.json` and a CSS file in it (example below).
3. Done – the style appears **immediately** in the profile for selection, without a restart.
   (Reload the browser once with Ctrl+F5 if needed.)

### `style.json`

```json
{
  "id": "night",
  "name": "Night blue",
  "description": "Dark blue, large font.",
  "order": 50,
  "bodyClass": "style-night",
  "layout": "none",
  "css": ["style.css"],
  "js": "style.js"
}
```

| Field         | Required | Meaning |
|---------------|----------|---------|
| `id`          | no       | Unique ID (lowercase letters, digits, `-`, `_`). Without it = folder name. `futuristic` is reserved. |
| `name`        | yes      | Name in the selection. |
| `description` | no       | Short description (appears after the name). |
| `order`       | no       | Order in the selection (smaller = higher up, default 100). |
| `bodyClass`   | no       | CSS class(es) for `<body>` while the style is active. Default: `style-<id>`. |
| `layout`      | no       | `none` = operation like Futuristic, only a different look (default) · `classic` = navigation on the left + tabs · `classic-hard` = like Proxmox. |
| `css`         | no       | List of CSS files, relative to the style folder. Other styles can be reused too, e.g. `"../classic/style.css"`. |
| `js`          | no       | Optional script (ES module) with `export async function apply(ctx)` – for anything CSS alone cannot do. |

In addition, while the style is active, `body[data-style="<id>"]` always applies – so
rules can be targeted even without a custom `bodyClass`.

### `style.css` – example

The easiest way is to override the color variables of the interface:

```css
body.style-night {
  --bg: #0b1220;
  --panel: #111a2e;
  --panel-2: #16223b;
  --border: #23314f;
  --text: #e6ecff;
  --subtext: #8ea0c8;
  font-size: 15px;
}
body.style-night .rmm-window { border-radius: 2px; }
```

Important variables: `--bg`, `--panel`, `--panel-2`, `--border`, `--text`,
`--subtext`, `--accent` / `--accent-rgb` (accent color from the profile),
`--online`, `--warn`, `--danger`. Light/dark: additionally `body.theme-light`.

### `style.js` – optional

```js
// ctx = { style, base, module, req }
//   base = URL of the style folder (for your own images/files)
//   req  = authenticated requests to the backend, e.g. await req("/api/clients")
export async function apply(ctx) {
  const logo = document.querySelector(".topbar-title");
  if (logo) logo.title = `Style: ${ctx.style.name}`;
}
```

For deeper changes the core provides hooks (`/js/app/styles.js`):

| Function                       | For what |
|--------------------------------|----------|
| `setMainContentRenderer(fn)`   | Draw the main area (dashboard/selection) yourself – `fn(el)`, return `false` = the core draws |
| `onMetrics(fn)`                | New live values of a client – `fn(clientId)` |
| `onWindowsChanged(fn)`         | The window list has changed |

```js
import { setMainContentRenderer } from "/js/app/styles.js";
export async function apply() {
  setMainContentRenderer((el) => { el.innerHTML = "<h1>My dashboard</h1>"; return true; });
}
```

---

## Safety / errors

* If a style fails to load (broken CSS path, error in the script), the interface
  automatically falls back to **Futuristic** and shows a message – it stays usable.
* A faulty `style.json` is skipped (note in the backend log), the remaining styles
  stay selectable.
* Styles are normal frontend files of the module and are only served while the
  module is active. Do not store credentials or similar in style files.
