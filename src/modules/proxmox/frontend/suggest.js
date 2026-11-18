// suggest.js (module "proxmox") - suggestions: agents that run as VM/CT in Proxmox
// -----------------------------------------------------------------------------------
// Hint in the bottom right + dialog: accept one or all (mark as VM/CT and subordinate
// them to the HEAD) or dismiss. Backend: match_routes.py
//
// HEAD = parent agent. In a Proxmox cluster (pve1, pve2, ...) often only one agent
// has the Proxmox connection - but the guests should sit under the agent of their own node.
//   * "Head per Proxmox node" (top, general): applies to ALL guests of that node (existing too).
//   * Per-row choice: the FIRST change on a node sets the head for the whole node
//     (example: give a guest on pve2 to the agent pve2 -> all pve2 guests go to pve2, the
//     pve1 guests stay with pve1). After that, a per-row change is an exception for that guest only.
// "Do not ask again" permanently hides the current suggestions (per user, synced);
// they remain reachable in the Proxmox dashboard (tab "Mapping").
import { esc } from "/js/app/utils.js";
import { state } from "/js/app/state.js";
import { preq } from "./common.js";
import { icon } from "./icons.js";

const SEEN = "rmm_pve_suggest_seen";                       // "Later" (this session only)
const neverKey = () => `rmm_pve_never:${state.user?.username || ""}`;
const sid = (s) => `${s.client_id}:${s.host}:${s.vmid}`;

function neverSet() {
  try { return new Set(JSON.parse(localStorage.getItem(neverKey()) || "[]")); } catch { return new Set(); }
}
function addNever(list) {
  const n = neverSet();
  list.forEach((s) => n.add(sid(s)));
  localStorage.setItem(neverKey(), JSON.stringify([...n].slice(-2000)));
}

export function maybeShowBanner(suggestions, onChange, extra = {}) {
  const never = neverSet();
  const open = suggestions.filter((s) => !never.has(sid(s)));
  const ids = open.map(sid).sort().join(",");
  document.getElementById("pve-suggest-banner")?.remove();
  if (!open.length || sessionStorage.getItem(SEEN) === ids) return;
  const el = document.createElement("div");
  el.id = "pve-suggest-banner";
  el.style.cssText = "position:fixed;right:16px;bottom:64px;z-index:9300;background:var(--panel);border:1px solid var(--accent);" +
    "border-radius:10px;padding:14px 16px;width:360px;max-width:calc(100vw - 32px);box-shadow:0 8px 28px rgba(0,0,0,.45);font-size:13px";
  el.innerHTML = `<div style="display:flex;gap:10px;align-items:flex-start">
      <span style="display:flex">${icon("cluster", 20)}</span>
      <div style="flex:1"><b>${open.length} agent(s) run in Proxmox</b>
        <div style="color:var(--subtext);margin-top:2px">${esc(open.slice(0, 3).map((s) => s.client_name).join(", "))}${open.length > 3 ? " …" : ""}
        - sort as VM/CT and subordinate to the head (Proxmox node)?</div>
        <div style="display:flex;gap:6px;margin-top:10px;flex-wrap:wrap"><button class="btn btn-primary btn-sm" data-open>View</button>
          <button class="btn btn-sm" data-later>Later</button>
          <button class="btn btn-sm btn-ghost" data-never title="Stop showing these suggestions as a hint">Do not ask again</button></div></div></div>`;
  document.body.appendChild(el);
  el.querySelector("[data-later]").addEventListener("click", () => { sessionStorage.setItem(SEEN, ids); el.remove(); });
  el.querySelector("[data-never]").addEventListener("click", () => {
    addNever(open); el.remove();
    window.notify?.("Will not be asked again - reachable in the Proxmox dashboard under \"Mapping\".", "info", 6000);
  });
  el.querySelector("[data-open]").addEventListener("click", () => { el.remove(); openSuggestDialog(open, onChange, extra); });
}

export function openSuggestDialog(suggestions, onChange, extra = {}) {
  const ov = document.createElement("div");
  ov.style.cssText = "position:fixed;inset:0;z-index:9500;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;padding:16px";
  ov.innerHTML = `<div style="background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:16px 18px;width:760px;max-width:100%;
      max-height:90vh;display:flex;flex-direction:column;font-size:13px;box-sizing:border-box">
    <div style="display:flex;align-items:center;margin-bottom:6px"><b style="flex:1;font-size:15px;display:flex;align-items:center;gap:6px">${icon("cluster", 16)} Agents detected in Proxmox</b><button class="btn-tool" data-x>${icon("close", 15)}</button></div>
    <p style="color:var(--subtext);margin:0 0 10px">Per Proxmox, these agents run in a VM or container. Accepting marks them as
      <b>VM</b>/<b>CT</b> and subordinates them to the <b>head</b> - the agent of the Proxmox node the guest runs on.
      The agent panel then shows "Open in Proxmox".</p>
    <div data-heads></div>
    <div data-list style="flex:1;overflow:auto;min-height:80px"></div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px;flex-wrap:wrap">
      <button class="btn btn-ghost" data-never style="margin-right:auto">Do not ask again</button>
      <button class="btn" data-x2>Close</button>
      <button class="btn btn-primary" data-all>Accept all</button></div></div>`;
  document.body.appendChild(ov);
  let list = [...suggestions];
  let cands = extra.candidates || [];
  const override = {};                       // client_id -> head (exception for this guest only)
  const setNodes = new Set();                // "host|node" whose head has already been set here
  const nodeKey = (s) => `${s.host}|${s.node}`;
  const close = () => { ov.remove(); onChange?.(); };
  ov.querySelector("[data-x]").onclick = close;
  ov.querySelector("[data-x2]").onclick = close;
  ov.querySelector("[data-never]").onclick = () => {
    addNever(list);
    window.notify?.("Will not be asked again - reachable in the Proxmox dashboard under \"Mapping\".", "info", 6000);
    close();
  };

  const nameOf = (id) => (cands.find((c) => c.id === id) || (state.clients || []).find((c) => c.id === id) || {}).hostname || id;
  const opts = (sel, exclude) => {
    const items = cands.filter((c) => c.id !== exclude);
    if (sel && !items.some((c) => c.id === sel)) items.unshift({ id: sel, hostname: nameOf(sel) });
    return items.map((c) => `<option value="${esc(c.id)}" ${c.id === sel ? "selected" : ""}>${esc(c.hostname)}${c.connected ? " *" : ""}</option>`).join("");
  };
  const headOf = (s) => override[s.client_id] || s.head;

  async function reload() {
    try {
      const g = await preq("/guest-map");
      const ids = new Set(list.map((s) => s.client_id));
      list = (g.suggestions || []).filter((s) => ids.has(s.client_id));
      cands = g.candidates?.length ? g.candidates : cands;
    } catch (e) { window.notify?.(e.message, "error"); }
    draw();
  }

  async function setNodeHead(host, node, head) {
    try {
      const r = await preq("/heads", { method: "PUT", body: { host, node, head } });
      setNodes.add(`${host}|${node}`);
      window.notify?.(`Head for node ${node}: ${nameOf(head)}` + (r.moved ? ` (${r.moved} existing mapping(s) moved)` : ""), "success", 4000);
      // exceptions that now match the node head are no longer exceptions
      list.forEach((s) => { if (nodeKey(s) === `${host}|${node}` && override[s.client_id] === head) delete override[s.client_id]; });
    } catch (e) { window.notify?.(e.message, "error"); }
    await reload();
  }

  const draw = () => {
    // ---- general: head per Proxmox node
    const nodes = new Map();
    list.forEach((s) => { if (!nodes.has(nodeKey(s))) nodes.set(nodeKey(s), s); });
    ov.querySelector("[data-heads]").innerHTML = nodes.size ? `
      <div style="border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-bottom:10px">
        <div style="font-weight:600;margin-bottom:6px">Head per Proxmox node <span style="font-weight:400;color:var(--subtext)">(applies to all guests of the node)</span></div>
        ${[...nodes.values()].map((s) => `
          <div data-node="${esc(nodeKey(s))}" style="display:flex;gap:8px;align-items:center;padding:3px 0;flex-wrap:wrap">
            <span style="min-width:170px">Node <b>${esc(s.node)}</b> <span style="color:var(--subtext)">(${esc(s.host_name)})</span></span>
            <select data-nodehead style="flex:1;min-width:180px">${opts(s.head)}</select>
            <span style="font-size:11px;color:var(--subtext)">${s.head_source === "set" ? "set" : s.head_source === "hostname" ? "detected (hostname)" : "connected agent"}</span>
          </div>`).join("")}
      </div>` : "";
    ov.querySelectorAll("[data-node]").forEach((row) => {
      const [host, node] = row.dataset.node.split("|");
      row.querySelector("[data-nodehead]").onchange = (e) => setNodeHead(host, node, e.target.value);
    });

    // ---- per guest
    ov.querySelector("[data-list]").innerHTML = list.length ? list.map((s) => `
      <div data-c="${esc(s.client_id)}" style="display:flex;gap:10px;align-items:center;padding:8px 2px;border-bottom:1px solid var(--border);flex-wrap:wrap">
        <span style="display:flex">${s.type === "qemu" ? icon("server", 18) : icon("box", 18)}</span>
        <div style="flex:1;min-width:220px"><b>${esc(s.client_name)}</b>
          <div style="font-size:12px;color:var(--subtext)">${s.type === "qemu" ? "VM" : "CT"} ${s.vmid} "${esc(s.name)}" · node ${esc(s.node)} (${esc(s.host_name)}) · detected via ${esc(s.reason)}</div></div>
        <label style="display:flex;gap:6px;align-items:center;font-size:12px">Head
          <select data-head style="max-width:190px">${opts(headOf(s), s.client_id)}</select></label>
        ${override[s.client_id] ? `<span title="Exception for this guest only" style="font-size:11px;color:var(--warning,#d97706)">Exception</span>` : ""}
        <button class="btn btn-sm" data-ign>No</button><button class="btn btn-primary btn-sm" data-acc>Accept</button></div>`).join("")
      : `<div style="color:var(--subtext);padding:8px 0">All done.</div>`;
    ov.querySelectorAll("[data-c]").forEach((row) => {
      const cid = row.dataset.c;
      const s = list.find((x) => x.client_id === cid);
      row.querySelector("[data-acc]").onclick = () => act(cid, "accept");
      row.querySelector("[data-ign]").onclick = () => act(cid, "ignore");
      row.querySelector("[data-head]").onchange = (e) => {
        const v = e.target.value;
        if (!setNodes.has(nodeKey(s)) && s.head_source !== "set") {
          // first choice on this node -> applies to the whole node (all other guests there follow)
          setNodeHead(s.host, s.node, v);
        } else {
          if (v === s.head) delete override[cid]; else override[cid] = v;
          draw();
        }
      };
    });
    ov.querySelector("[data-all]").disabled = !list.length;
  };

  async function act(cid, what) {
    const s = list.find((x) => x.client_id === cid);
    try {
      const body = what === "accept" ? { head: headOf(s), remember: !override[cid] } : undefined;
      const r = await preq(`/suggestions/${encodeURIComponent(cid)}/${what}`, { method: "POST", body });
      if (what === "accept" && r.client) window.dispatchEvent(new CustomEvent("rmm-client-updated", { detail: r.client }));
      list = list.filter((x) => x.client_id !== cid); draw();
    } catch (e) { window.notify?.(e.message, "error"); }
  }
  ov.querySelector("[data-all]").onclick = async () => {
    for (const s of [...list]) await act(s.client_id, "accept");
    window.notify?.("Mappings accepted", "success");
  };
  draw();
}
