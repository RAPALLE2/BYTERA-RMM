// manage.js (module "proxmox") - advanced management: create VM/CT,
// migrate, clone, convert to template, grow disk, ISOs/templates.
// Dialogs; backend manage_routes.py. Called from dashboard.js.
import { esc, uiConfirm, uiPrompt } from "/js/app/utils.js";
import { preq, fmtBytes } from "./common.js";
import { icon } from "./icons.js";

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

// ============================================================ Create VM / CT
export async function createDialog(C, type, after) {
  const nodes = await nodeList(C);
  if (!nodes.length) { toast("No nodes found", "error"); return; }
  const isVM = type === "qemu";
  const ov = overlay(`<h3 style="margin:0 0 12px;display:flex;align-items:center;gap:6px">${icon("plus", 16)} ${isVM ? "New VM" : "New container"}</h3>
    <div style="display:grid;grid-template-columns:140px 1fr;gap:8px 10px;align-items:center">
      <label>Node</label><select data-f="node">${opt(nodes.map(nodeName), nodeName(nodes[0]))}</select>
      <label>VMID</label><input data-f="vmid" type="number" placeholder="empty = automatic">
      <label>${isVM ? "Name" : "Hostname"}</label><input data-f="name" placeholder="e.g. ${isVM ? "web01" : "ct-web"}">
      <div data-body style="display:contents"></div>
    </div>
    <details style="margin-top:10px"><summary style="cursor:pointer;color:var(--subtext)">Advanced (raw parameters)</summary>
      <textarea data-extra rows="2" placeholder='JSON, e.g. {"onboot":1}' style="width:100%;font-family:monospace;margin-top:6px"></textarea></details>
    <label style="display:flex;gap:6px;align-items:center;margin-top:10px;font-size:12.5px"><input type="checkbox" data-start> Start after creating</label>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px"><button class="btn" data-x>Cancel</button><button class="btn-primary" data-ok>Create</button></div>`, 600);
  const body = ov.querySelector("[data-body]");
  const nodeSel = ov.querySelector('[data-f="node"]');

  async function fillNode() {
    const node = nodeSel.value;
    body.innerHTML = `<label></label><div style="color:var(--subtext)">Loading storage/network …</div>`;
    const br = await bridges(C, node);
    if (isVM) {
      const disk = await storages(C, node, "images");
      const isoStores = await storages(C, node, "iso");
      let isos = [];
      for (const s of isoStores) isos = isos.concat((await content(C, node, s.storage, "iso")).map((x) => x.volid));
      body.innerHTML = `
        <label>ISO (CD)</label><select data-f="iso"><option value="">— none —</option>${opt(isos, "")}</select>
        <label>Disk storage</label><select data-f="storage">${opt(disk.map((s) => s.storage), disk[0]?.storage)}</select>
        <label>Disk size (GB)</label><input data-f="disk_gb" type="number" value="32">
        <label>CPU cores</label><input data-f="cores" type="number" value="2">
        <label>Sockets</label><input data-f="sockets" type="number" value="1">
        <label>RAM (MiB)</label><input data-f="memory" type="number" value="2048">
        <label>OS type</label><select data-f="ostype">${opt([["l26","Linux"],["win11","Windows 11"],["win10","Windows 10"],["win2k22","Windows Server 2022"],["other","Other"]].map((x)=>({value:x[0],label:x[1]})),"l26")}</select>
        <label>BIOS</label><select data-f="bios">${opt([["seabios","SeaBIOS"],["ovmf","OVMF (UEFI)"]].map((x)=>({value:x[0],label:x[1]})),"seabios")}</select>
        <label>Bridge</label><select data-f="bridge">${opt(br.map((b) => b.iface), br[0]?.iface || "vmbr0")}</select>
        <label>VLAN</label><input data-f="vlan" type="number" placeholder="optional">
        <label></label><label style="font-size:12.5px"><input type="checkbox" data-f="agent" checked> Enable QEMU guest agent</label>`;
    } else {
      const root = await storages(C, node, "rootdir");
      const tplStores = await storages(C, node, "vztmpl");
      let tpls = [];
      for (const s of tplStores) tpls = tpls.concat((await content(C, node, s.storage, "vztmpl")).map((x) => x.volid));
      body.innerHTML = `
        <label>CT template</label><select data-f="ostemplate">${tpls.length ? opt(tpls, tpls[0]) : `<option value="">— none (download first) —</option>`}</select>
        <label>rootfs storage</label><select data-f="storage">${opt(root.map((s) => s.storage), root[0]?.storage)}</select>
        <label>rootfs size (GB)</label><input data-f="rootfs_gb" type="number" value="8">
        <label>CPU cores</label><input data-f="cores" type="number" value="1">
        <label>RAM (MiB)</label><input data-f="memory" type="number" value="512">
        <label>Swap (MiB)</label><input data-f="swap" type="number" value="512">
        <label>Password</label><input data-f="password" type="password" placeholder="root password">
        <label>SSH keys</label><textarea data-f="ssh_public_keys" rows="2" placeholder="optional, one key per line"></textarea>
        <label>Bridge</label><select data-f="bridge">${opt(br.map((b) => b.iface), br[0]?.iface || "vmbr0")}</select>
        <label>VLAN</label><input data-f="vlan" type="number" placeholder="optional">
        <label>IP</label><input data-f="ip" value="dhcp" placeholder="dhcp or 192.168.1.50/24">
        <label>Gateway</label><input data-f="gw" placeholder="optional">
        <label></label><label style="font-size:12.5px"><input type="checkbox" data-f="unprivileged" checked> Unprivileged</label>
        <label></label><label style="font-size:12.5px"><input type="checkbox" data-f="nesting"> Nesting (Docker in the CT)</label>`;
    }
  }
  nodeSel.addEventListener("change", fillNode);
  await fillNode();

  ov.querySelector("[data-ok]").addEventListener("click", async () => {
    const g = (k) => { const el = ov.querySelector(`[data-f="${k}"]`); if (!el) return undefined; return el.type === "checkbox" ? el.checked : (el.value === "" ? undefined : el.value); };
    let extra = {}; const raw = ov.querySelector("[data-extra]").value.trim();
    if (raw) { try { extra = JSON.parse(raw); } catch { toast("Advanced: not valid JSON", "error"); return; } }
    const num = (k) => g(k) === undefined ? undefined : Number(g(k));
    const payload = { node: nodeSel.value, vmid: num("vmid"), start: ov.querySelector("[data-start]").checked, extra };
    if (isVM) Object.assign(payload, { name: g("name") || "", iso: g("iso") || "", storage: g("storage"), disk_gb: num("disk_gb"),
      cores: num("cores"), sockets: num("sockets"), memory: num("memory"), ostype: g("ostype"), bios: g("bios"),
      bridge: g("bridge"), vlan: num("vlan"), agent: g("agent") });
    else Object.assign(payload, { hostname: g("name") || "", ostemplate: g("ostemplate") || "", storage: g("storage"),
      rootfs_gb: num("rootfs_gb"), cores: num("cores"), memory: num("memory"), swap: num("swap"), password: g("password") || "",
      ssh_public_keys: g("ssh_public_keys") || "", bridge: g("bridge"), vlan: num("vlan"), ip: g("ip") || "dhcp", gw: g("gw") || "",
      unprivileged: g("unprivileged"), nesting: g("nesting") });
    const btn = ov.querySelector("[data-ok]"); btn.disabled = true; btn.textContent = "Creating …";
    try { await preq(`/${C}/create/${isVM ? "qemu" : "lxc"}`, { method: "POST", body: payload }); ov.remove(); toast(isVM ? "VM created" : "Container created"); after?.(); }
    catch (e) { toast(e.message, "error", 7000); btn.disabled = false; btn.textContent = "Create"; }
  });
}

// ============================================================ Guest management (migrate/clone/…)
export function guestMenu(C, g, after) {
  const base = `/${C}/guests/${encodeURIComponent(g.node)}/${g.type}/${g.vmid}`;
  const ov = overlay(`<h3 style="margin:0 0 12px;display:flex;align-items:center;gap:6px">${icon("gear", 16)} Manage ${esc(g.name || g.vmid)}</h3>
    <div style="display:flex;flex-direction:column;gap:8px">
      <button class="btn" data-migrate>${icon("migrate", 14)} Migrate (change node)</button>
      <button class="btn" data-clone>${icon("clone", 14)} Clone</button>
      <button class="btn" data-resize>${icon("resize", 14)} Grow disk</button>
      ${g.template ? "" : `<button class="btn" data-template>${icon("box", 14)} Convert to template</button>`}</div>`, 380);
  ov.querySelector("[data-migrate]").addEventListener("click", async () => { ov.remove(); await migrateDialog(C, g, after); });
  ov.querySelector("[data-clone]").addEventListener("click", async () => { ov.remove(); await cloneDialog(C, g, after); });
  ov.querySelector("[data-resize]").addEventListener("click", async () => { ov.remove(); await resizeDialog(C, g, base, after); });
  ov.querySelector("[data-template]")?.addEventListener("click", async () => {
    if (!(await uiConfirm(`Permanently convert "${g.name || g.vmid}" to a template? This cannot be undone.`, { okText: "Convert", danger: true }))) return;
    try { await preq(`${base}/template`, { method: "POST" }); ov.remove?.(); toast("Converted to template"); after?.(); } catch (e) { toast(e.message, "error"); }
  });
}

async function migrateDialog(C, g, after) {
  const nodes = (await nodeList(C)).map(nodeName).filter((n) => n !== g.node);
  if (!nodes.length) { toast("No other node available", "error"); return; }
  const ov = overlay(`<h3 style="margin:0 0 12px;display:flex;align-items:center;gap:6px">${icon("migrate", 16)} Migrate ${esc(g.name || g.vmid)}</h3>
    <div style="display:grid;grid-template-columns:140px 1fr;gap:8px 10px;align-items:center">
      <label>Target node</label><select data-target>${opt(nodes, nodes[0])}</select>
      <label></label><label style="font-size:12.5px"><input type="checkbox" data-online checked> ${g.type === "qemu" ? "Live migration (stays running)" : "Restart migration"}</label>
      ${g.type === "qemu" ? `<label></label><label style="font-size:12.5px"><input type="checkbox" data-local checked> Bring local disks</label>` : ""}</div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px"><button class="btn" data-x>Cancel</button><button class="btn-primary" data-ok>Migrate</button></div>`, 440);
  ov.querySelector("[data-ok]").addEventListener("click", async () => {
    const b = ov.querySelector("[data-ok]"); b.disabled = true; b.textContent = "Migrating …";
    try {
      await preq(`/${C}/guests/${encodeURIComponent(g.node)}/${g.type}/${g.vmid}/migrate`, { method: "POST",
        body: { target: ov.querySelector("[data-target]").value, online: ov.querySelector("[data-online]").checked,
                with_local_disks: ov.querySelector("[data-local]")?.checked ?? false } });
      ov.remove(); toast("Migration finished"); after?.();
    } catch (e) { toast(e.message, "error", 7000); b.disabled = false; b.textContent = "Migrate"; }
  });
}

async function cloneDialog(C, g, after) {
  const nodes = (await nodeList(C)).map(nodeName);
  const ov = overlay(`<h3 style="margin:0 0 12px;display:flex;align-items:center;gap:6px">${icon("clone", 16)} Clone ${esc(g.name || g.vmid)}</h3>
    <div style="display:grid;grid-template-columns:140px 1fr;gap:8px 10px;align-items:center">
      <label>New VMID</label><input data-newid type="number" placeholder="empty = automatic">
      <label>Name</label><input data-name placeholder="name of the clone">
      <label>Target node</label><select data-target>${opt(nodes, g.node)}</select>
      <label>Mode</label><select data-full>${opt([["1","Full clone"],["0","Linked clone (template only)"]].map((x)=>({value:x[0],label:x[1]})),"1")}</select></div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px"><button class="btn" data-x>Cancel</button><button class="btn-primary" data-ok>Clone</button></div>`, 440);
  ov.querySelector("[data-ok]").addEventListener("click", async () => {
    const b = ov.querySelector("[data-ok]"); b.disabled = true; b.textContent = "Cloning …";
    try {
      await preq(`/${C}/guests/${encodeURIComponent(g.node)}/${g.type}/${g.vmid}/clone`, { method: "POST",
        body: { newid: ov.querySelector("[data-newid]").value ? Number(ov.querySelector("[data-newid]").value) : null,
                name: ov.querySelector("[data-name]").value, full: ov.querySelector("[data-full]").value === "1",
                target: ov.querySelector("[data-target]").value } });
      ov.remove(); toast("Clone created"); after?.();
    } catch (e) { toast(e.message, "error", 7000); b.disabled = false; b.textContent = "Clone"; }
  });
}

async function resizeDialog(C, g, base, after) {
  let cfg = {};
  try { cfg = (await preq(base)).config || {}; } catch { /* egal */ }
  const disks = Object.keys(cfg).filter((k) => /^(scsi|sata|ide|virtio)\d+$/.test(k) && !/media=cdrom/.test(String(cfg[k])) || k === "rootfs" || /^mp\d+$/.test(k));
  const ov = overlay(`<h3 style="margin:0 0 12px;display:flex;align-items:center;gap:6px">${icon("resize", 16)} Grow disk — ${esc(g.name || g.vmid)}</h3>
    <div style="display:grid;grid-template-columns:140px 1fr;gap:8px 10px;align-items:center">
      <label>Disk</label><select data-disk>${opt(disks.length ? disks : ["rootfs"], disks[0])}</select>
      <label>Grow by</label><input data-size placeholder="e.g. +8G (growing only)"></div>
    <div style="font-size:11.5px;color:var(--subtext);margin-top:8px">Proxmox can only grow disks, not shrink them.</div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px"><button class="btn" data-x>Cancel</button><button class="btn-primary" data-ok>Grow</button></div>`, 440);
  ov.querySelector("[data-ok]").addEventListener("click", async () => {
    try { await preq(`${base}/resize`, { method: "PUT", body: { disk: ov.querySelector("[data-disk]").value, size: ov.querySelector("[data-size]").value.trim() } });
      ov.remove(); toast("Disk grown"); after?.(); } catch (e) { toast(e.message, "error", 6000); }
  });
}

// ============================================================ Storage: ISOs & templates
export function storageActions(C, node, store, storeContent, after) {
  // Returns HTML buttons + a wire() function that dashboard.js attaches.
  const html = `<div style="display:flex;gap:6px;flex-wrap:wrap;margin:8px 0">
      ${/iso|vztmpl/.test(storeContent) ? `<button class="btn btn-sm" data-dl>${icon("download", 13)} Download by URL</button>` : ""}
      ${/vztmpl/.test(storeContent) ? `<button class="btn btn-sm" data-apl>${icon("box", 13)} CT templates (pveam)</button>` : ""}</div>`;
  const wire = (box) => {
    box.querySelector("[data-dl]")?.addEventListener("click", () => downloadDialog(C, node, store, storeContent, after));
    box.querySelector("[data-apl]")?.addEventListener("click", () => aplDialog(C, node, store, after));
  };
  return { html, wire };
}

export async function deleteVolume(C, node, store, volid, after) {
  if (!(await uiConfirm(`Delete?`, { description: volid, okText: "Delete", danger: true }))) return;
  try { await preq(`/${C}/nodes/${encodeURIComponent(node)}/storage/${encodeURIComponent(store)}/delete`, { method: "POST", body: { volid } }); toast("Deleted"); after?.(); }
  catch (e) { toast(e.message, "error", 6000); }
}

function downloadDialog(C, node, store, storeContent, after) {
  const types = [];
  if (/iso/.test(storeContent)) types.push({ value: "iso", label: "ISO image" });
  if (/vztmpl/.test(storeContent)) types.push({ value: "vztmpl", label: "CT template" });
  const ov = overlay(`<h3 style="margin:0 0 12px;display:flex;align-items:center;gap:6px">${icon("download", 16)} Download by URL &rarr; ${esc(store)}</h3>
    <div style="display:grid;grid-template-columns:120px 1fr;gap:8px 10px;align-items:center">
      <label>Kind</label><select data-f="content">${opt(types, types[0]?.value)}</select>
      <label>URL</label><input data-f="url" placeholder="https://…/image.iso">
      <label>File name</label><input data-f="filename" placeholder="empty = from URL">
      <label>Checksum</label><input data-f="checksum" placeholder="optional (sha256)"></div>
    <div style="font-size:11.5px;color:var(--subtext);margin-top:8px">Proxmox downloads directly - even large ISOs, without going through the dashboard.</div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px"><button class="btn" data-x>Cancel</button><button class="btn-primary" data-ok>Download</button></div>`, 520);
  ov.querySelector("[data-ok]").addEventListener("click", async () => {
    const g = (k) => ov.querySelector(`[data-f="${k}"]`).value.trim();
    if (!g("url")) { toast("URL is missing", "error"); return; }
    const b = ov.querySelector("[data-ok]"); b.disabled = true; b.textContent = "Downloading …";
    try { await preq(`/${C}/nodes/${encodeURIComponent(node)}/storage/${encodeURIComponent(store)}/download-url`, { method: "POST",
        body: { content: g("content"), url: g("url"), filename: g("filename"), checksum: g("checksum") } });
      ov.remove(); toast("Download finished"); after?.();
    } catch (e) { toast(e.message, "error", 7000); b.disabled = false; b.textContent = "Download"; }
  });
}

async function aplDialog(C, node, store, after) {
  let tpls = [];
  try { tpls = (await preq(`/${C}/nodes/${encodeURIComponent(node)}/aplinfo`)).templates || []; } catch (e) { toast(e.message, "error"); return; }
  const ov = overlay(`<h3 style="margin:0 0 10px;display:flex;align-items:center;gap:6px">${icon("box", 16)} CT templates (pveam) &rarr; ${esc(store)}</h3>
    <input data-q placeholder="Filter …" style="width:100%;margin-bottom:8px">
    <div data-list style="max-height:50vh;overflow:auto"></div>`, 560);
  const list = ov.querySelector("[data-list]"), q = ov.querySelector("[data-q]");
  const draw = () => {
    const f = q.value.toLowerCase();
    list.innerHTML = tpls.filter((t) => !f || `${t.template} ${t.description}`.toLowerCase().includes(f)).map((t) => `
      <div style="display:flex;gap:8px;align-items:center;padding:6px 4px;border-bottom:1px solid var(--border)">
        <div style="flex:1"><div style="font-size:12.5px">${esc(t.template)}</div><div style="font-size:11px;color:var(--subtext)">${esc(t.os || "")} ${esc(t.version || "")} · ${esc(t.section || "")}</div></div>
        <button class="btn btn-sm" data-t="${esc(t.template)}">${icon("download", 14)}</button></div>`).join("") || `<div style="color:var(--subtext);padding:8px">No templates.</div>`;
    list.querySelectorAll("[data-t]").forEach((b) => b.addEventListener("click", async () => {
      b.disabled = true; b.innerHTML = icon("download", 14);
      try { await preq(`/${C}/nodes/${encodeURIComponent(node)}/aplinfo/download`, { method: "POST", body: { storage: store, template: b.dataset.t } }); toast("Template downloaded"); ov.remove(); after?.(); }
      catch (e) { toast(e.message, "error", 6000); b.disabled = false; b.innerHTML = icon("download", 14); }
    }));
  };
  q.addEventListener("input", draw); draw();
}
