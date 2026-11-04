// tempusb.js (Core) - Digitaler USB-Stick
// ---------------------------------------------------------------------------
// Bereiche je Benutzer/Gruppe anlegen, Dateien ablegen und an ein Gerät
// anhängen (Ordner-Sync, Standard C:\bytera bzw. /mnt/bytera). Der Benutzer am
// Gerät kann dort z.B. ein Skript ablegen und per Remote-Screen/Shell starten;
// was er ablegt, erscheint wieder hier.
import { api, getToken } from "/js/app/api.js";
import { esc, uiConfirm, uiPrompt } from "/js/app/utils.js";
import { state } from "/js/app/state.js";
import { t } from "/js/app/i18n.js";

const req = (path, opts = {}) =>
  fetch(path, { ...opts, headers: { Authorization: "Bearer " + getToken(), ...(opts.headers || {}) } })
    .then(async (r) => { if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.status); return r; });
const jreq = (path, opts) => req(path, opts).then((r) => r.json());

export async function renderTempUsb(body) {
  body.innerHTML = `<div style="display:flex;height:100%;min-height:0">
      <div style="width:240px;border-right:1px solid var(--border);display:flex;flex-direction:column">
        <div style="padding:8px;border-bottom:1px solid var(--border);display:flex;gap:6px">
          <b style="flex:1;align-self:center">${t("tu_areas")}</b>
          <button class="btn btn-sm btn-primary" id="tu-new">+</button>
        </div>
        <div id="tu-list" style="flex:1;overflow:auto"></div>
      </div>
      <div id="tu-main" style="flex:1;display:flex;flex-direction:column;min-width:0;padding:10px">
        <div style="color:var(--subtext)">${t("tu_pick")}</div>
      </div>
    </div>`;
  const listEl = body.querySelector("#tu-list");
  const mainEl = body.querySelector("#tu-main");
  let areas = [];
  let canManage = false;
  let current = null;

  async function load() {
    const r = await jreq("/api/temp-usb");
    areas = r.areas || [];
    canManage = !!r.can_manage;
    body.querySelector("#tu-new").style.display = canManage ? "" : "none";
    listEl.innerHTML = areas.length
      ? areas.map((a) => `<div class="tu-item" data-id="${a.id}" style="padding:8px 10px;cursor:pointer;border-bottom:1px solid var(--border)">
          💾 ${esc(a.label)}<div style="font-size:11px;color:var(--subtext)">${a.owner_type === "group" ? "👥" : "👤"} ${Object.keys(a.attached || {}).length ? "· " + Object.keys(a.attached).length + " ×" + t("tu_attached_short") : ""}</div></div>`).join("")
      : `<div style="padding:10px;color:var(--subtext)">${t("tu_none")}</div>`;
    listEl.querySelectorAll(".tu-item").forEach((el) =>
      el.addEventListener("click", () => open(el.dataset.id)));
  }

  async function open(id) {
    current = areas.find((a) => a.id === id);
    if (!current) return;
    await openFolder("");
  }

  async function openFolder(path) {
    const data = await jreq(`/api/temp-usb/${current.id}/files?path=${encodeURIComponent(path)}`);
    const up = path ? path.split("/").slice(0, -1).join("/") : null;
    mainEl.innerHTML = `
      <div style="display:flex;gap:6px;align-items:center;margin-bottom:8px">
        <b style="flex:1">💾 ${esc(current.label)} <span style="color:var(--subtext);font-weight:400">/${esc(path)}</span></b>
        <button class="btn btn-sm" id="tu-attach">🔌 ${t("tu_attach")}</button>
        ${canManage ? `<button class="btn btn-sm" id="tu-rename">✏️</button><button class="btn btn-sm btn-danger" id="tu-del">🗑️</button>` : ""}
      </div>
      <div style="display:flex;gap:6px;margin-bottom:8px">
        <input type="file" id="tu-files" multiple style="flex:1">
        <button class="btn btn-sm" id="tu-mkdir">📁+</button>
      </div>
      <div id="tu-entries" style="flex:1;overflow:auto;border:1px solid var(--border);border-radius:6px"></div>`;
    const entriesEl = mainEl.querySelector("#tu-entries");
    const rows = [];
    if (up !== null || path) rows.push(`<div class="tu-row" data-up="${esc(up || "")}" style="padding:7px 10px;cursor:pointer;border-bottom:1px solid var(--border)">📁 ..</div>`);
    for (const e of data.entries) {
      rows.push(`<div class="tu-row" data-${e.dir ? "dir" : "file"}="${esc((path ? path + "/" : "") + e.name)}" style="padding:7px 10px;cursor:pointer;border-bottom:1px solid var(--border);display:flex;gap:8px">
        <span style="flex:1">${e.dir ? "📁" : "📄"} ${esc(e.name)}</span>
        ${e.dir ? "" : `<span style="color:var(--subtext)">${fmtSize(e.size)}</span>`}
        ${canManage ? `<span class="tu-rm" data-rm="${esc((path ? path + "/" : "") + e.name)}" title="löschen" style="color:var(--danger)">✕</span>` : ""}</div>`);
    }
    entriesEl.innerHTML = rows.join("") || `<div style="padding:10px;color:var(--subtext)">${t("tu_empty")}</div>`;

    entriesEl.querySelectorAll("[data-up]").forEach((el) => el.addEventListener("click", () => openFolder(el.dataset.up)));
    entriesEl.querySelectorAll("[data-dir]").forEach((el) => el.addEventListener("click", () => openFolder(el.dataset.dir)));
    entriesEl.querySelectorAll("[data-file]").forEach((el) => el.addEventListener("click", (ev) => {
      if (ev.target.classList.contains("tu-rm")) return;
      window.open(`/api/temp-usb/${current.id}/download?path=${encodeURIComponent(el.dataset.file)}`, "_blank");
    }));
    entriesEl.querySelectorAll(".tu-rm").forEach((el) => el.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      if (!(await uiConfirm(t("tu_del_file") + " „" + el.dataset.rm.split("/").pop() + "“?"))) return;
      await req(`/api/temp-usb/${current.id}/delete`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: el.dataset.rm }) });
      openFolder(path);
    }));
    mainEl.querySelector("#tu-files").addEventListener("change", async (ev) => {
      const fd = new FormData();
      fd.append("path", path);
      for (const f of ev.target.files) fd.append("files", f);
      await req(`/api/temp-usb/${current.id}/upload`, { method: "POST", body: fd });
      openFolder(path);
    });
    mainEl.querySelector("#tu-mkdir").addEventListener("click", async () => {
      const n = await uiPrompt(t("tu_newfolder"));
      if (!n) return;
      await req(`/api/temp-usb/${current.id}/mkdir`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: (path ? path + "/" : "") + n }) });
      openFolder(path);
    });
    mainEl.querySelector("#tu-attach").addEventListener("click", attachDialog);
    mainEl.querySelector("#tu-del")?.addEventListener("click", async () => {
      if (!(await uiConfirm(t("tu_del_area") + " „" + current.label + "“?"))) return;
      await req(`/api/temp-usb/${current.id}`, { method: "DELETE" });
      current = null; mainEl.innerHTML = `<div style="color:var(--subtext)">${t("tu_pick")}</div>`; load();
    });
    mainEl.querySelector("#tu-rename")?.addEventListener("click", async () => {
      const n = await uiPrompt(t("tu_rename"), { value: current.label });
      if (!n) return;
      await req(`/api/temp-usb/${current.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ label: n }) });
      current.label = n; load(); openFolder(path);
    });
  }

  async function attachDialog() {
    const online = (state.clients || []).filter((c) => c.online);
    if (!online.length) { window.notify?.(t("tu_no_online"), "warning"); return; }
    const pick = await uiPrompt(t("tu_attach_which"), {
      description: online.map((c, i) => `${i + 1}: ${c.hostname}`).join("\n"),
      placeholder: "1",
    });
    const idx = parseInt(pick, 10) - 1;
    if (!(idx >= 0 && idx < online.length)) return;
    const path = await uiPrompt(t("tu_target"), { placeholder: "C:\\bytera / /mnt/bytera", value: "" });
    try {
      await req(`/api/temp-usb/${current.id}/attach`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ clientId: online[idx].id, mode: "folder", path: path || "" }),
      });
      window.notify?.(t("tu_attached") + " " + online[idx].hostname, "success");
      load();
    } catch (e) { window.notify?.(String(e.message || e), "error"); }
  }

  await createHook();
  async function createHook() {
    body.querySelector("#tu-new").addEventListener("click", async () => {
      const label = await uiPrompt(t("tu_new_label"), { placeholder: t("tu_new_ph") });
      if (!label) return;
      const dir = await jreq("/api/temp-usb/directory").catch(() => ({ users: [], groups: [] }));
      // einfache Auswahl: eigener Benutzer als Standard; Gruppen per Nummer
      let owner_type = "user", owner_id = state.user?.id || "";
      if (dir.groups?.length) {
        const g = await uiPrompt(t("tu_owner"), {
          description: t("tu_owner_me") + "\n" + dir.groups.map((x, i) => `${i + 1}: 👥 ${x.name}`).join("\n"),
          placeholder: t("tu_owner_me_ph"),
        });
        const gi = parseInt(g, 10) - 1;
        if (gi >= 0 && gi < dir.groups.length) { owner_type = "group"; owner_id = dir.groups[gi].id; }
      }
      await req("/api/temp-usb", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ label, owner_type, owner_id }) });
      load();
    });
  }

  // Status-Updates vom Agenten (angehängt/gelöst) -> Liste auffrischen
  try {
    const { dashboardSocket } = await import("/js/app/socket.js");
    const onStatus = () => load();
    dashboardSocket.on("temp-usb-status", onStatus);
  } catch { /* egal */ }

  await load();
}

function fmtSize(n) {
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  if (n < 1073741824) return (n / 1048576).toFixed(1) + " MB";
  return (n / 1073741824).toFixed(1) + " GB";
}
