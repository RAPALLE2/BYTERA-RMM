// manage.js (Modul "proxmox") - Erweiterte Verwaltung: VM/CT erstellen,
// migrieren, klonen, in Vorlage wandeln, Disk vergrößern, ISOs/Templates.
// Dialoge; Backend manage_routes.py. Von dashboard.js aus aufgerufen.
import { esc, uiConfirm, uiPrompt } from "/js/app/utils.js";
import { preq, fmtBytes } from "./common.js";

const toast = (m, t = "success", ms = 2200) => window.notify?.(m, t, ms);

function overlay(html, width = 560) {
  const ov = document.createElement("div");
  ov.style.cssText = "position:fixed;inset:0;z-index:9600;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;padding:14px";
  ov.innerHTML = `<div style="background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:16px 18px;width:${width}px;max-width:100%;max-height:calc(100dvh - 28px);overflow:auto;font-size:13px">${html}</div>`;
  document.body.appendChild(ov);
  ov.addEventListener("click", (e) => { if (e.target === ov) ov.remove(); });
  ov.querySelector("[data-x]")?.addEventListener("click", () => ov.remove());
  return ov;
}
const opt = (arr, cur) => arr.map((v) => `<option value="${esc(v.value ?? v)}" ${(v.value ?? v) === cur ? "selected" : ""}>${esc(v.label ?? v)}</option>`).join("");

async function nodeList(C) { try { return (await preq(`/${C}/node-names`)).nodes || []; } catch { return []; } }
async function storages(C, node, content) { try { return (await preq(`/${C}/nodes/${encodeURIComponent(node)}/storages?content=${content}`)).storages || []; } catch { return []; } }
async function bridges(C, node) { try { return (await preq(`/${C}/nodes/${encodeURIComponent(node)}/bridges`)).bridges || []; } catch { return []; } }
async function content(C, node, store, ct) { try { return (await preq(`/${C}/nodes/${encodeURIComponent(node)}/content?store=${encodeURIComponent(store)}&content=${ct}`)).content || []; } catch { return []; } }
const nodeName = (n) => (typeof n === "string" ? n : n.node || n.name);

// ============================================================ VM / CT erstellen
export async function createDialog(C, type, after) {
  const nodes = await nodeList(C);
  if (!nodes.length) { toast("Keine Nodes gefunden", "error"); return; }
  const isVM = type === "qemu";
  const ov = overlay(`<h3 style="margin:0 0 12px">${isVM ? "＋ Neue VM" : "＋ Neuer Container"}</h3>
    <div style="display:grid;grid-template-columns:140px 1fr;gap:8px 10px;align-items:center">
      <label>Node</label><select data-f="node">${opt(nodes.map(nodeName), nodeName(nodes[0]))}</select>
      <label>VMID</label><input data-f="vmid" type="number" placeholder="leer = automatisch">
      <label>${isVM ? "Name" : "Hostname"}</label><input data-f="name" placeholder="z.B. ${isVM ? "web01" : "ct-web"}">
      <div data-body style="display:contents"></div>
    </div>
    <details style="margin-top:10px"><summary style="cursor:pointer;color:var(--subtext)">Erweitert (rohe Parameter)</summary>
      <textarea data-extra rows="2" placeholder='JSON, z.B. {"onboot":1}' style="width:100%;font-family:monospace;margin-top:6px"></textarea></details>
    <label style="display:flex;gap:6px;align-items:center;margin-top:10px;font-size:12.5px"><input type="checkbox" data-start> Nach dem Erstellen starten</label>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px"><button class="btn" data-x>Abbrechen</button><button class="btn-primary" data-ok>Erstellen</button></div>`, 600);
  const body = ov.querySelector("[data-body]");
  const nodeSel = ov.querySelector('[data-f="node"]');

  async function fillNode() {
    const node = nodeSel.value;
    body.innerHTML = `<label></label><div style="color:var(--subtext)">Lade Storage/Netz …</div>`;
    const br = await bridges(C, node);
    if (isVM) {
      const disk = await storages(C, node, "images");
      const isoStores = await storages(C, node, "iso");
      let isos = [];
      for (const s of isoStores) isos = isos.concat((await content(C, node, s.storage, "iso")).map((x) => x.volid));
      body.innerHTML = `
        <label>ISO (CD)</label><select data-f="iso"><option value="">— keine —</option>${opt(isos, "")}</select>
        <label>Disk-Storage</label><select data-f="storage">${opt(disk.map((s) => s.storage), disk[0]?.storage)}</select>
        <label>Disk-Größe (GB)</label><input data-f="disk_gb" type="number" value="32">
        <label>CPU-Kerne</label><input data-f="cores" type="number" value="2">
        <label>Sockets</label><input data-f="sockets" type="number" value="1">
        <label>RAM (MiB)</label><input data-f="memory" type="number" value="2048">
        <label>OS-Typ</label><select data-f="ostype">${opt([["l26","Linux"],["win11","Windows 11"],["win10","Windows 10"],["win2k22","Windows Server 2022"],["other","Andere"]].map((x)=>({value:x[0],label:x[1]})),"l26")}</select>
        <label>BIOS</label><select data-f="bios">${opt([["seabios","SeaBIOS"],["ovmf","OVMF (UEFI)"]].map((x)=>({value:x[0],label:x[1]})),"seabios")}</select>
        <label>Bridge</label><select data-f="bridge">${opt(br.map((b) => b.iface), br[0]?.iface || "vmbr0")}</select>
        <label>VLAN</label><input data-f="vlan" type="number" placeholder="optional">
        <label></label><label style="font-size:12.5px"><input type="checkbox" data-f="agent" checked> QEMU-Gast-Agent aktivieren</label>`;
    } else {
      const root = await storages(C, node, "rootdir");
      const tplStores = await storages(C, node, "vztmpl");
      let tpls = [];
      for (const s of tplStores) tpls = tpls.concat((await content(C, node, s.storage, "vztmpl")).map((x) => x.volid));
      body.innerHTML = `
        <label>CT-Template</label><select data-f="ostemplate">${tpls.length ? opt(tpls, tpls[0]) : `<option value="">— keins (erst herunterladen) —</option>`}</select>
        <label>rootfs-Storage</label><select data-f="storage">${opt(root.map((s) => s.storage), root[0]?.storage)}</select>
        <label>rootfs-Größe (GB)</label><input data-f="rootfs_gb" type="number" value="8">
        <label>CPU-Kerne</label><input data-f="cores" type="number" value="1">
        <label>RAM (MiB)</label><input data-f="memory" type="number" value="512">
        <label>Swap (MiB)</label><input data-f="swap" type="number" value="512">
        <label>Passwort</label><input data-f="password" type="password" placeholder="Root-Passwort">
        <label>SSH-Keys</label><textarea data-f="ssh_public_keys" rows="2" placeholder="optional, ein Key pro Zeile"></textarea>
        <label>Bridge</label><select data-f="bridge">${opt(br.map((b) => b.iface), br[0]?.iface || "vmbr0")}</select>
        <label>VLAN</label><input data-f="vlan" type="number" placeholder="optional">
        <label>IP</label><input data-f="ip" value="dhcp" placeholder="dhcp oder 192.168.1.50/24">
        <label>Gateway</label><input data-f="gw" placeholder="optional">
        <label></label><label style="font-size:12.5px"><input type="checkbox" data-f="unprivileged" checked> Unprivilegiert</label>
        <label></label><label style="font-size:12.5px"><input type="checkbox" data-f="nesting"> Nesting (Docker im CT)</label>`;
    }
  }
  nodeSel.addEventListener("change", fillNode);
  await fillNode();

  ov.querySelector("[data-ok]").addEventListener("click", async () => {
    const g = (k) => { const el = ov.querySelector(`[data-f="${k}"]`); if (!el) return undefined; return el.type === "checkbox" ? el.checked : (el.value === "" ? undefined : el.value); };
    let extra = {}; const raw = ov.querySelector("[data-extra]").value.trim();
    if (raw) { try { extra = JSON.parse(raw); } catch { toast("Erweitert: kein gültiges JSON", "error"); return; } }
    const num = (k) => g(k) === undefined ? undefined : Number(g(k));
    const payload = { node: nodeSel.value, vmid: num("vmid"), start: ov.querySelector("[data-start]").checked, extra };
    if (isVM) Object.assign(payload, { name: g("name") || "", iso: g("iso") || "", storage: g("storage"), disk_gb: num("disk_gb"),
      cores: num("cores"), sockets: num("sockets"), memory: num("memory"), ostype: g("ostype"), bios: g("bios"),
      bridge: g("bridge"), vlan: num("vlan"), agent: g("agent") });
    else Object.assign(payload, { hostname: g("name") || "", ostemplate: g("ostemplate") || "", storage: g("storage"),
      rootfs_gb: num("rootfs_gb"), cores: num("cores"), memory: num("memory"), swap: num("swap"), password: g("password") || "",
      ssh_public_keys: g("ssh_public_keys") || "", bridge: g("bridge"), vlan: num("vlan"), ip: g("ip") || "dhcp", gw: g("gw") || "",
      unprivileged: g("unprivileged"), nesting: g("nesting") });
    const btn = ov.querySelector("[data-ok]"); btn.disabled = true; btn.textContent = "⏳ erstellt …";
    try { await preq(`/${C}/create/${isVM ? "qemu" : "lxc"}`, { method: "POST", body: payload }); ov.remove(); toast(isVM ? "VM erstellt" : "Container erstellt"); after?.(); }
    catch (e) { toast(e.message, "error", 7000); btn.disabled = false; btn.textContent = "Erstellen"; }
  });
}

// ============================================================ Gast-Verwaltung (migrieren/klonen/…)
export function guestMenu(C, g, after) {
  const base = `/${C}/guests/${encodeURIComponent(g.node)}/${g.type}/${g.vmid}`;
  const ov = overlay(`<h3 style="margin:0 0 12px">⚙ ${esc(g.name || g.vmid)} verwalten</h3>
    <div style="display:flex;flex-direction:column;gap:8px">
      <button class="btn" data-migrate>➡ Migrieren (Node wechseln)</button>
      <button class="btn" data-clone>⧉ Klonen</button>
      <button class="btn" data-resize>⤢ Disk vergrößern</button>
      ${g.template ? "" : `<button class="btn" data-template>📦 In Vorlage umwandeln</button>`}</div>`, 380);
  ov.querySelector("[data-migrate]").addEventListener("click", async () => { ov.remove(); await migrateDialog(C, g, after); });
  ov.querySelector("[data-clone]").addEventListener("click", async () => { ov.remove(); await cloneDialog(C, g, after); });
  ov.querySelector("[data-resize]").addEventListener("click", async () => { ov.remove(); await resizeDialog(C, g, base, after); });
  ov.querySelector("[data-template]")?.addEventListener("click", async () => {
    if (!(await uiConfirm(`„${g.name || g.vmid}“ dauerhaft in eine Vorlage umwandeln? Das kann nicht rückgängig gemacht werden.`, { okText: "Umwandeln", danger: true }))) return;
    try { await preq(`${base}/template`, { method: "POST" }); ov.remove?.(); toast("In Vorlage umgewandelt"); after?.(); } catch (e) { toast(e.message, "error"); }
  });
}

async function migrateDialog(C, g, after) {
  const nodes = (await nodeList(C)).map(nodeName).filter((n) => n !== g.node);
  if (!nodes.length) { toast("Kein anderer Node vorhanden", "error"); return; }
  const ov = overlay(`<h3 style="margin:0 0 12px">➡ ${esc(g.name || g.vmid)} migrieren</h3>
    <div style="display:grid;grid-template-columns:140px 1fr;gap:8px 10px;align-items:center">
      <label>Ziel-Node</label><select data-target>${opt(nodes, nodes[0])}</select>
      <label></label><label style="font-size:12.5px"><input type="checkbox" data-online checked> ${g.type === "qemu" ? "Live-Migration (läuft weiter)" : "Neustart-Migration"}</label>
      ${g.type === "qemu" ? `<label></label><label style="font-size:12.5px"><input type="checkbox" data-local checked> Lokale Disks mitnehmen</label>` : ""}</div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px"><button class="btn" data-x>Abbrechen</button><button class="btn-primary" data-ok>Migrieren</button></div>`, 440);
  ov.querySelector("[data-ok]").addEventListener("click", async () => {
    const b = ov.querySelector("[data-ok]"); b.disabled = true; b.textContent = "⏳ migriert …";
    try {
      await preq(`/${C}/guests/${encodeURIComponent(g.node)}/${g.type}/${g.vmid}/migrate`, { method: "POST",
        body: { target: ov.querySelector("[data-target]").value, online: ov.querySelector("[data-online]").checked,
                with_local_disks: ov.querySelector("[data-local]")?.checked ?? false } });
      ov.remove(); toast("Migration abgeschlossen"); after?.();
    } catch (e) { toast(e.message, "error", 7000); b.disabled = false; b.textContent = "Migrieren"; }
  });
}

async function cloneDialog(C, g, after) {
  const nodes = (await nodeList(C)).map(nodeName);
  const ov = overlay(`<h3 style="margin:0 0 12px">⧉ ${esc(g.name || g.vmid)} klonen</h3>
    <div style="display:grid;grid-template-columns:140px 1fr;gap:8px 10px;align-items:center">
      <label>Neue VMID</label><input data-newid type="number" placeholder="leer = automatisch">
      <label>Name</label><input data-name placeholder="Name des Klons">
      <label>Ziel-Node</label><select data-target>${opt(nodes, g.node)}</select>
      <label>Modus</label><select data-full>${opt([["1","Vollständiger Klon"],["0","Verknüpfter Klon (nur Vorlage)"]].map((x)=>({value:x[0],label:x[1]})),"1")}</select></div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px"><button class="btn" data-x>Abbrechen</button><button class="btn-primary" data-ok>Klonen</button></div>`, 440);
  ov.querySelector("[data-ok]").addEventListener("click", async () => {
    const b = ov.querySelector("[data-ok]"); b.disabled = true; b.textContent = "⏳ klont …";
    try {
      await preq(`/${C}/guests/${encodeURIComponent(g.node)}/${g.type}/${g.vmid}/clone`, { method: "POST",
        body: { newid: ov.querySelector("[data-newid]").value ? Number(ov.querySelector("[data-newid]").value) : null,
                name: ov.querySelector("[data-name]").value, full: ov.querySelector("[data-full]").value === "1",
                target: ov.querySelector("[data-target]").value } });
      ov.remove(); toast("Klon erstellt"); after?.();
    } catch (e) { toast(e.message, "error", 7000); b.disabled = false; b.textContent = "Klonen"; }
  });
}

async function resizeDialog(C, g, base, after) {
  let cfg = {};
  try { cfg = (await preq(base)).config || {}; } catch { /* egal */ }
  const disks = Object.keys(cfg).filter((k) => /^(scsi|sata|ide|virtio)\d+$/.test(k) && !/media=cdrom/.test(String(cfg[k])) || k === "rootfs" || /^mp\d+$/.test(k));
  const ov = overlay(`<h3 style="margin:0 0 12px">⤢ Disk vergrößern — ${esc(g.name || g.vmid)}</h3>
    <div style="display:grid;grid-template-columns:140px 1fr;gap:8px 10px;align-items:center">
      <label>Disk</label><select data-disk>${opt(disks.length ? disks : ["rootfs"], disks[0])}</select>
      <label>Vergrößern um</label><input data-size placeholder="z.B. +8G (nur Vergrößern möglich)"></div>
    <div style="font-size:11.5px;color:var(--subtext);margin-top:8px">Proxmox kann Disks nur vergrößern, nicht verkleinern.</div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px"><button class="btn" data-x>Abbrechen</button><button class="btn-primary" data-ok>Vergrößern</button></div>`, 440);
  ov.querySelector("[data-ok]").addEventListener("click", async () => {
    try { await preq(`${base}/resize`, { method: "PUT", body: { disk: ov.querySelector("[data-disk]").value, size: ov.querySelector("[data-size]").value.trim() } });
      ov.remove(); toast("Disk vergrößert"); after?.(); } catch (e) { toast(e.message, "error", 6000); }
  });
}

// ============================================================ Storage: ISOs & Templates
export function storageActions(C, node, store, storeContent, after) {
  // Gibt HTML-Knöpfe + eine wire()-Funktion zurück, die dashboard.js einhängt.
  const html = `<div style="display:flex;gap:6px;flex-wrap:wrap;margin:8px 0">
      ${/iso|vztmpl/.test(storeContent) ? `<button class="btn btn-sm" data-dl>⬇ Download per URL</button>` : ""}
      ${/vztmpl/.test(storeContent) ? `<button class="btn btn-sm" data-apl>📦 CT-Vorlagen (pveam)</button>` : ""}</div>`;
  const wire = (box) => {
    box.querySelector("[data-dl]")?.addEventListener("click", () => downloadDialog(C, node, store, storeContent, after));
    box.querySelector("[data-apl]")?.addEventListener("click", () => aplDialog(C, node, store, after));
  };
  return { html, wire };
}

export async function deleteVolume(C, node, store, volid, after) {
  if (!(await uiConfirm(`Löschen?`, { description: volid, okText: "Löschen", danger: true }))) return;
  try { await preq(`/${C}/nodes/${encodeURIComponent(node)}/storage/${encodeURIComponent(store)}/delete`, { method: "POST", body: { volid } }); toast("Gelöscht"); after?.(); }
  catch (e) { toast(e.message, "error", 6000); }
}

function downloadDialog(C, node, store, storeContent, after) {
  const types = [];
  if (/iso/.test(storeContent)) types.push({ value: "iso", label: "ISO-Abbild" });
  if (/vztmpl/.test(storeContent)) types.push({ value: "vztmpl", label: "CT-Template" });
  const ov = overlay(`<h3 style="margin:0 0 12px">⬇ Download per URL → ${esc(store)}</h3>
    <div style="display:grid;grid-template-columns:120px 1fr;gap:8px 10px;align-items:center">
      <label>Art</label><select data-f="content">${opt(types, types[0]?.value)}</select>
      <label>URL</label><input data-f="url" placeholder="https://…/image.iso">
      <label>Dateiname</label><input data-f="filename" placeholder="leer = aus URL">
      <label>Prüfsumme</label><input data-f="checksum" placeholder="optional (sha256)"></div>
    <div style="font-size:11.5px;color:var(--subtext);margin-top:8px">Proxmox lädt direkt herunter – auch große ISOs, ohne Umweg übers Dashboard.</div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px"><button class="btn" data-x>Abbrechen</button><button class="btn-primary" data-ok>Herunterladen</button></div>`, 520);
  ov.querySelector("[data-ok]").addEventListener("click", async () => {
    const g = (k) => ov.querySelector(`[data-f="${k}"]`).value.trim();
    if (!g("url")) { toast("URL fehlt", "error"); return; }
    const b = ov.querySelector("[data-ok]"); b.disabled = true; b.textContent = "⏳ lädt …";
    try { await preq(`/${C}/nodes/${encodeURIComponent(node)}/storage/${encodeURIComponent(store)}/download-url`, { method: "POST",
        body: { content: g("content"), url: g("url"), filename: g("filename"), checksum: g("checksum") } });
      ov.remove(); toast("Download abgeschlossen"); after?.();
    } catch (e) { toast(e.message, "error", 7000); b.disabled = false; b.textContent = "Herunterladen"; }
  });
}

async function aplDialog(C, node, store, after) {
  let tpls = [];
  try { tpls = (await preq(`/${C}/nodes/${encodeURIComponent(node)}/aplinfo`)).templates || []; } catch (e) { toast(e.message, "error"); return; }
  const ov = overlay(`<h3 style="margin:0 0 10px">📦 CT-Vorlagen (pveam) → ${esc(store)}</h3>
    <input data-q placeholder="Filtern …" style="width:100%;margin-bottom:8px">
    <div data-list style="max-height:50vh;overflow:auto"></div>`, 560);
  const list = ov.querySelector("[data-list]"), q = ov.querySelector("[data-q]");
  const draw = () => {
    const f = q.value.toLowerCase();
    list.innerHTML = tpls.filter((t) => !f || `${t.template} ${t.description}`.toLowerCase().includes(f)).map((t) => `
      <div style="display:flex;gap:8px;align-items:center;padding:6px 4px;border-bottom:1px solid var(--border)">
        <div style="flex:1"><div style="font-size:12.5px">${esc(t.template)}</div><div style="font-size:11px;color:var(--subtext)">${esc(t.os || "")} ${esc(t.version || "")} · ${esc(t.section || "")}</div></div>
        <button class="btn btn-sm" data-t="${esc(t.template)}">⬇</button></div>`).join("") || `<div style="color:var(--subtext);padding:8px">Keine Vorlagen.</div>`;
    list.querySelectorAll("[data-t]").forEach((b) => b.addEventListener("click", async () => {
      b.disabled = true; b.textContent = "⏳";
      try { await preq(`/${C}/nodes/${encodeURIComponent(node)}/aplinfo/download`, { method: "POST", body: { storage: store, template: b.dataset.t } }); toast("Vorlage geladen"); ov.remove(); after?.(); }
      catch (e) { toast(e.message, "error", 6000); b.disabled = false; b.textContent = "⬇"; }
    }));
  };
  q.addEventListener("input", draw); draw();
}
