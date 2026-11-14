// bgjobs.js (Core)
// ----------------
// Frontend-Seite der Hintergrund-Jobs (Backend: app/bgjobs.py).
//
// GRUNDREGEL: Aufgaben laufen im Backend. Eine App startet einen Job nur und
// ZEIGT dann an, was das Backend meldet. Neu laden, ein zweites Fenster oder
// ein anderer Benutzer sehen denselben Job - nichts haengt an dieser Seite.
//
//   // Aufgabe starten und (bequem wie frueher) auf das Ergebnis warten:
//   const res = await runJob("/api/clients/bulk-exec", { method: "POST", body: {...},
//                            onUpdate: (job) => status.textContent = job.detail });
//
//   // Beim Oeffnen einer App einen laufenden/letzten Job wieder anzeigen:
//   const stop = watchJobs({ kind: "patch.scan", key: clientId }, (job) => {...});
//   stop();      // beim Schliessen des Fensters
//
// Live: Socket-Event "bgjob:update" meldet nur {id, kind, key, rev}; die
// Details kommen per REST (dort werden die Rechte geprueft). Faellt der Socket
// aus, fragt ein langsamer Ersatz-Takt nach, solange ein Job laeuft.
// Alle Jobs zeigt ausserdem die Aufgaben-Anzeige in der Taskleiste (jobtray.js).

import { moduleRequest } from "./modules.js";
import { dashboardSocket } from "./socket.js";

export const fetchJob = (id) => moduleRequest(`/api/bgjobs/${encodeURIComponent(id)}`);

export async function listJobs({ kind, key, running = false, history = false, limit } = {}) {
  const q = new URLSearchParams();
  if (kind) q.set("kind", kind);
  if (key != null) q.set("key", key);
  if (running) q.set("running", "1");
  if (history) q.set("history", "1");
  if (limit) q.set("limit", String(limit));
  return (await moduleRequest(`/api/bgjobs?${q}`)).jobs || [];
}

export const cancelJob = (id) =>
  moduleRequest(`/api/bgjobs/${encodeURIComponent(id)}/cancel`, { method: "POST" });

/** Dauer eines Jobs lesbar ("12 s", "3 min", "1.2 h"). */
export function jobDuration(job) {
  const end = job.finished || Date.now();
  const sec = Math.max(0, Math.round((end - job.started) / 1000));
  if (sec < 90) return `${sec} s`;
  if (sec < 5400) return `${Math.round(sec / 60)} min`;
  return `${(sec / 3600).toFixed(1)} h`;
}

/** Kurzer Statustext eines Jobs ("läuft · 42 % · Details"). */
export function jobStatusText(job) {
  if (!job) return "";
  if (job.running) {
    return [job.cancel_requested ? "wird abgebrochen …" : "läuft",
      job.pct != null ? `${Math.round(job.pct)} %` : "", job.detail].filter(Boolean).join(" · ");
  }
  if (job.status === "error") return `Fehler: ${job.error || "?"}`;
  if (job.status === "cancelled") return `abgebrochen${job.cancelled_by ? " von " + job.cancelled_by : ""}`;
  return `fertig (${jobDuration(job)})`;
}

/** Wartet, bis der Job fertig ist; onUpdate(job) bei jeder Aenderung. */
export function waitJob(id, onUpdate) {
  return new Promise((resolve) => {
    let stop = null;
    stop = watchJobs({ id }, (job) => {
      if (!job) return;
      try { onUpdate?.(job); } catch (e) { console.error(e); }
      if (!job.running) { stop?.(); resolve(job); }
    });
  });
}

/** Fuer eigene fetch-Helfer: ?background=1 an aendernde Aufrufe haengen. */
export function withBackground(url, method = "GET") {
  if ((method || "GET").toUpperCase() === "GET" || /[?&]background=/.test(url)) return url;
  return url + (url.includes("?") ? "&" : "?") + "background=1";
}

/** Fuer eigene fetch-Helfer: Ist die Antwort ein Job, live auf das Ergebnis
 *  warten (Fehler -> Exception); sonst die Antwort unveraendert zurueck. */
export async function resolveJob(data, onJob) {
  if (!(data && data.job === true && data.id)) return data;
  try { onJob?.(data); } catch (e) { console.error(e); }
  const job = data.running ? await waitJob(data.id, onJob) : await fetchJob(data.id);
  if (job.status === "error") throw new Error(job.error || "Fehler");
  if (job.status === "cancelled") throw new Error(`Abgebrochen${job.cancelled_by ? " von " + job.cancelled_by : ""}`);
  return job.result !== undefined ? job.result : (await fetchJob(job.id)).result;
}

/**
 * Aufgabe als Backend-Job starten und auf das Ergebnis warten.
 * path: Fach-Endpunkt (bekommt ?background=1); opts: {method, body, onUpdate}.
 * Gibt das Ergebnis zurueck wie frueher die direkte Antwort; Fehler -> Exception.
 * Der Job laeuft im Backend weiter, auch wenn diese Seite geschlossen wird.
 */
export async function runJob(path, { method = "POST", body, onUpdate } = {}) {
  const url = path + (path.includes("?") ? "&" : "?") + "background=1";
  const started = await moduleRequest(url, {
    method, body: body === undefined ? undefined : (typeof body === "string" ? body : JSON.stringify(body)),
  });
  if (!started || !started.job || !started.id) return started;   // Endpunkt ist (noch) kein Job
  try { onUpdate?.(started); } catch (e) { console.error(e); }
  const job = started.running ? await waitJob(started.id, onUpdate) : await fetchJob(started.id);
  if (job.status === "error") throw new Error(job.error || "Fehler");
  if (job.status === "cancelled") {
    const err = new Error(`Abgebrochen${job.cancelled_by ? " von " + job.cancelled_by : ""}`);
    err.cancelled = true;
    throw err;
  }
  if (job.result === undefined) return (await fetchJob(job.id)).result;
  return job.result;
}

/**
 * Beobachtet einen Job.
 *   {id}         genau diesen Job
 *   {kind, key}  den laufenden, sonst zuletzt beendeten Job (auch aus dem Verlauf)
 *   {kind}       den neuesten Job dieser Art (key egal)
 * onJob(job | null) bei jeder Aenderung. Rueckgabe: Funktion zum Abmelden.
 */
export function watchJobs({ id: fixedId, kind, key }, onJob) {
  let current = null;            // zuletzt gemeldeter Job
  let stopped = false;
  let inflight = null, again = false, pendingId = null;
  let timer = null;

  async function refresh(id) {
    if (stopped) return;
    if (inflight) { again = true; pendingId = id || pendingId; return; }
    inflight = (async () => {
      try {
        let job = null;
        const want = fixedId || id;
        if (want) job = await fetchJob(want);
        else {
          const jobs = await listJobs({ kind, key, history: true, limit: 5 });
          const pick = jobs.find((j) => j.running) || jobs[0];
          job = pick ? await fetchJob(pick.id) : null;
        }
        if (stopped) return;
        // Ein alter Lauf verdraengt keinen neueren, laufenden.
        if (job && current && job.id !== current.id && current.running
            && job.started < current.started) return;
        current = job;
        onJob(job);
      } catch { /* z.B. abgelaufen oder keine Rechte -> naechster Takt */ }
    })();
    await inflight;
    inflight = null;
    if (again) { const nid = pendingId || current?.id; again = false; pendingId = null; refresh(nid); return; }
    schedule();
  }

  // Ersatz-Takt nur solange etwas laeuft (falls der Socket hakt)
  function schedule() {
    clearTimeout(timer);
    if (!stopped && current?.running) timer = setTimeout(() => refresh(current.id), 4000);
  }

  const onEvent = (p) => {
    if (!p) return;
    if (fixedId) { if (p.id === fixedId) refresh(fixedId); return; }
    if (p.kind !== kind) return;
    if (key !== undefined && p.key !== key) return;
    refresh(p.id);
  };
  dashboardSocket.on("bgjob:update", onEvent);
  // Nach einem Verbindungsabbruch (z.B. Backend-Neustart) Stand neu holen
  const onReconnect = () => refresh(current?.id && !current.running ? null : current?.id);
  dashboardSocket.on("connect", onReconnect);
  refresh(fixedId || null);

  return () => {
    stopped = true;
    clearTimeout(timer);
    dashboardSocket.off("bgjob:update", onEvent);
    dashboardSocket.off("connect", onReconnect);
  };
}
