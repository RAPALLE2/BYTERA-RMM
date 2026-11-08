// init.js (Modul "url-scheme") - verkabelt das bytera://-Schema beim Start.
//   * Modul-API registrieren: getModuleApi("url-scheme").resolve/make/parse/copy
//     (die Core-Suchleiste und der Desktop-Client nutzen das).
//   * Klicks auf <a href="bytera://..."> im ganzen Dashboard abfangen.
//   * Beim Laden ?go=bytera://... bzw. #bytera://... einmal ausfuehren.
import { registerModuleApi } from "/js/app/modules.js";
import { resolveByteraUrl, parseByteraUrl, makeByteraUrl, copyByteraUrl, isByteraUrl } from "./urlscheme.js";

let linkHooked = false;

export function init() {
  registerModuleApi("url-scheme", {
    resolve: resolveByteraUrl,
    parse: parseByteraUrl,
    make: makeByteraUrl,
    copy: copyByteraUrl,
    isUrl: isByteraUrl,
  });

  // Klicks auf bytera://-Links abfangen (einmalig global).
  if (!linkHooked) {
    linkHooked = true;
    document.addEventListener("click", (ev) => {
      const a = ev.target?.closest?.("a[href^='bytera://']");
      if (!a) return;
      ev.preventDefault();
      resolveByteraUrl(a.getAttribute("href"));
    });
  }

  // Deep-Link beim Laden: ?go=bytera://...  oder  #bytera://...
  try {
    let target = null;
    const go = new URLSearchParams(location.search).get("go");
    if (isByteraUrl(go)) target = go;
    else {
      const hash = decodeURIComponent((location.hash || "").replace(/^#/, ""));
      if (isByteraUrl(hash)) target = hash;
    }
    if (target) {
      // Aus der Adresse entfernen, damit ein Reload es nicht erneut ausloest.
      try {
        const url = new URL(location.href);
        url.searchParams.delete("go");
        if (isByteraUrl(decodeURIComponent((url.hash || "").replace(/^#/, "")))) url.hash = "";
        history.replaceState(null, "", url.toString());
      } catch { /* egal */ }
      setTimeout(() => resolveByteraUrl(target), 400);
    }
  } catch { /* egal */ }
}
