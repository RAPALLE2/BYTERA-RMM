// config.js
// ---------
// Since the frontend is served by the same Python backend that also runs the
// API, we can simply use "" here (empty string = same address as the web page
// itself). If the frontend ever runs from elsewhere, just enter a full URL
// here.
export const BACKEND_URL = "";

// ---------------------------------------------------------------------------
// Canonical address of this installation
// ---------------------------------------------------------------------------
// `location.host` is only the address through which the user HAPPENS to access
// it right now. For anything entered on ANOTHER machine - WebDAV paths, network
// drive commands, install lines - that is the wrong source: whoever opens the
// dashboard internally via IP would get an address that nobody reaches from
// outside.
//
// The backend knows the correct one (setting "Full URL" resp. domain/host +
// port, otherwise the request itself incl. reverse-proxy headers). It is
// fetched once here and remembered; until it arrives, location is the fallback
// so nothing stays empty.

let _publicBase = null;      // resolved value
let _publicBasePromise = null;

function _fromLocation() {
  const port = window.location.port
    || (window.location.protocol === "https:" ? "443" : "80");
  return {
    base_url: window.location.origin,
    scheme: window.location.protocol.replace(":", ""),
    host: window.location.hostname,
    port: Number(port),
    netloc: window.location.host,
    configured: false,
  };
}

/** Immediately available value - from location as long as nothing is loaded. */
export function publicBaseNow() {
  return _publicBase || _fromLocation();
}

/**
 * Load the canonical address (once per session). If the fetch fails - old
 * backend, no network - the address from location is used instead of throwing
 * an error: an inaccurate address is better than an empty one.
 */
export function loadPublicBase() {
  if (_publicBase) return Promise.resolve(_publicBase);
  if (_publicBasePromise) return _publicBasePromise;
  _publicBasePromise = fetch(`${BACKEND_URL}/api/public-base`, {
    headers: { Authorization: `Bearer ${localStorage.getItem("rmm_token") || ""}` },
  })
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
    .then((d) => {
      _publicBase = { ...(_fromLocation()), ...d };
      return _publicBase;
    })
    .catch(() => _fromLocation());
  return _publicBasePromise;
}
