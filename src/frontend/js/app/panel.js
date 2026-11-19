// panel.js
// --------
// Renders the central main area, depending on the sidebar selection:
//   - Client   -> detail view (Status | overview with Metrics/Notes/Disk | Actions)
//   - Location -> aggregated overview of all clients of this location
//   - Tenant   -> aggregated overview, grouped by location
//
// The metrics view offers: gradient donuts (CPU/RAM/Disk), CPU cores/threads,
// RAM used/free, network graph with in/out toggle, labelled history
// charts with a hover tooltip and a time-range dropdown.

import { state, findClient } from "./state.js";
// URL-scheme module is loaded dynamically on click (robust if off/missing)
import { hasClientPerm, hasGlobalPerm, isAdmin } from "./state.js";
import { formatBytes, formatUptime, esc, gradientDonutSvg, CPU_GRADIENT, RAM_GRADIENT, DISK_GRADIENT, interactiveChart, uiConfirm, uiPrompt, uiChoice } from "./utils.js";
import { getHistoryRange, TIME_RANGES, seedHistory, hasSeeded } from "./metricshistory.js";
import { openWindow } from "./windowmanager.js";
import { isModuleOn, getClientActions, getClientWidgets, runActionHandlers } from "./modules.js";   // Optional module functions (Guacamole button, module buttons, widgets)
import { subjectPickerHtml, readSubjectPicker, initSubjectPicker } from "./subjectpicker.js";
import { api } from "./api.js";
import { renderDashboard } from "../feature/dashboard.js";
import { favStarHtml, favState, cycleFav } from "./sidebar.js";
import { renderClientLayout } from "./dashlayout.js";
import { hideFleetTip, showFleetTip, attachHoverTip, scaleToContainer } from "./fleetcharts.js";
import { renderMainByStyle } from "./styles.js";
import { clientIconSvg, batteryHtml } from "./deviceicons.js";

// Row hover (like dashboard/clientmetrics): highlight the row + tooltip
// with the label + the FULL value of exactly this row.
function bindRowHover(rowEl, tipFn) {
  rowEl.style.borderRadius = rowEl.style.borderRadius || "6px";
  rowEl.style.cursor = "default";
  rowEl.addEventListener("mouseenter", (e) => { rowEl.style.background = "var(--panel-2, #1b2740)"; showFleetTip(tipFn(), e.clientX, e.clientY); });
  rowEl.addEventListener("mousemove", (e) => showFleetTip(tipFn(), e.clientX, e.clientY));
  rowEl.addEventListener("mouseleave", () => { rowEl.style.background = ""; hideFleetTip(); });
}

// Latest shipped agent version (loaded once) for the "outdated" hint.
let _latestAgentVersion = null;
api.getAgentVersion().then((r) => { _latestAgentVersion = r && r.version; }).catch(() => {});

// Shows the reported agent version; if it differs from the latest, a
// clear "outdated" hint is shown (a common reason why
// update/uninstall from the dashboard does not take effect).
function agentVersionBadge(client) {
  const v = client.agent_version;
  if (!v) return `<span style="color:var(--subtext)">unknown</span>`;
  if (_latestAgentVersion && v !== _latestAgentVersion) {
    return `<span title="New: ${esc(_latestAgentVersion)}">${esc(v)} <span style="color:var(--warn)">⚠ outdated</span></span>`;
  }
  return `<span>${esc(v)}</span>`;
}
import { t, osIcon, osLabel } from "./i18n.js";

// State of the overview (remembered per view). The tab selection is now kept PER
// folder instance (multiple overview folders possible), default here.
let showNetIn = true;
let showNetOut = true;
let timeRangeIndex = 0;           // index in TIME_RANGES (default: 5 min)

const content = () => document.getElementById("main-content");

export function renderMainContent() {
  // View is switched/rebuilt: remove all floating hover tooltips
  // (otherwise they could linger past the client switch).
  try { hideFleetTip(); } catch {}
  const el = content();
  // A style (module "styles", e.g. Classic Hard) can draw the main area itself
  if (renderMainByStyle(el)) return;
  if (!state.selection || state.selection.type === "dashboard") {
    // Without the 'see_dashboard' permission the dashboard view deliberately stays a
    // a blank black page (no numbers, widgets or favorites).
    if (!isAdmin() && !hasGlobalPerm("see_dashboard")) {
      el.innerHTML = `<div style="position:absolute;inset:0;background:#000"></div>`;
      return;
    }
    renderDashboard(el);
    return;
  }
  if (state.selection.type === "client") renderClientView(el, state.selection.id);
  else if (state.selection.type === "location") renderAggregateView(el, "location", state.selection.id);
  else if (state.selection.type === "tenant") renderAggregateView(el, "tenant", state.selection.id);
}

// -----------------------------------------------------------------
// Status helpers
// -----------------------------------------------------------------

function statusInfo(client) {
  if (client.status_override === "maintenance") return { cls: "status-maintenance", label: t("status_maintenance") };
  if (client.online) return { cls: "status-online", label: t("status_online") };
  return { cls: "status-offline", label: t("status_offline") };
}

// -----------------------------------------------------------------
// CLIENT DETAIL VIEW
// -----------------------------------------------------------------

function renderClientView(el, clientId) {
  const client = findClient(clientId);
  if (!client) { el.innerHTML = `<div class="empty-state">${t("client_not_found")}</div>`; return; }

  const status = statusInfo(client);

  el.innerHTML = `
    <div class="client-view-head" style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <h2 style="margin:0;display:flex;align-items:center;gap:8px">
        <span class="status-dot-lg ${status.cls}"></span>${clientIconSvg(client, 24)}${esc(client.hostname)}
        ${favStarHtml("clients", client.id)}
      </h2>
      <span style="display:flex;align-items:center;gap:12px">
        ${batteryHtml(client, "full")}
        <span style="color:var(--subtext);font-size:13px">${osIcon(client.platform, client.release)} ${esc(osLabel(client.platform, client.release))} · ${esc(client.ip || "")}</span>
        <span id="dash-layout-toolbar"></span>
      </span>
    </div>
    <div id="dash-layout-host"></div>
    <div id="dash-module-widgets" style="display:flex;flex-wrap:wrap;gap:12px;margin-top:14px"></div>
  `;

  // Render the customizable layout (status/actions/overview folders).
  renderClientLayout(el.querySelector("#dash-layout-host"),
                     el.querySelector("#dash-layout-toolbar"), client);

  // Widgets of active modules (registerClientWidget), e.g. the location map.
  const wHost = el.querySelector("#dash-module-widgets");
  getClientWidgets().forEach((w) => {
    let ok = false;
    try { ok = w.match ? w.match(client) : true; } catch { ok = false; }
    if (!ok) return;
    const box = document.createElement("div");
    box.style.cssText = "flex:1 1 320px;min-width:300px";
    wHost.appendChild(box);
    try { w.render(box, client); } catch (e) { box.innerHTML = `<div style="color:var(--danger);font-size:12px">Widget error: ${esc(e.message)}</div>`; }
  });

  maybePromptDeviceType(client);
}

// -----------------------------------------------------------------
// Auto-detection of the device type: the agent reports when it runs in a VM or
// an LXC container. When the client panel is opened for the first time the
// user is asked whether the client should be kept as VM/LXC (with host choice) or
// still as physical. The answer is stored
// (device_type_ack); after that the question never appears again.
// -----------------------------------------------------------------
const _dtPromptShown = new Set();   // only once per session per client

function maybePromptDeviceType(client) {
  const detected = client.detected_device_type;
  if (!detected || !["vm", "lxc"].includes(detected)) return;
  if (client.device_type_ack) return;
  if ((client.device_type || "physical") !== "physical") return;   // already set manually
  if (_dtPromptShown.has(client.id)) return;
  _dtPromptShown.add(client.id);

  const label = detected === "vm" ? "virtual machine (VM)" : "LXC container";
  // Possible hosts: physical clients (except the client itself).
  const hosts = state.clients.filter((c) =>
    c.id !== client.id && (c.device_type || "physical") === "physical");

  const overlay = document.createElement("div");
  overlay.style.cssText = "position:fixed;inset:0;z-index:9400;background:rgba(0,0,0,0.5);" +
    "display:flex;align-items:center;justify-content:center";
  overlay.innerHTML = `
    <div style="background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:22px;width:440px;max-width:92vw">
      <h3 style="margin:0 0 8px">${t("dt_detected", { label: esc(label) })}</h3>
      <p style="color:var(--subtext);font-size:13px;margin:0 0 12px">
        The agent on <b>${esc(client.hostname)}</b> detected that it runs in a
        ${t("dt_question", { label: esc(label) })}
        ${t("dt_hint")}
      </p>
      <div class="form-row">
        <label>${t("dt_parent")}</label>
        <select id="dt-host">
          <option value="">${t("pn_no_host")}</option>
          ${hosts.map((h) => `<option value="${esc(h.id)}">${esc(h.hostname)}</option>`).join("")}
        </select>
      </div>
      <div id="dt-error" class="form-error hidden"></div>
      <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px">
        <button class="btn" id="dt-keep">Keep as physical</button>
        <button class="btn-primary" id="dt-apply">${t("dt_apply", { kind: detected === "vm" ? "VM" : "LXC" })}</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  const finish = async (fields) => {
    try {
      const updated = await api.updateClient(client.id, { ...fields, device_type_ack: 1 });
      Object.assign(client, updated);   // update local state
      overlay.remove();
      renderMainContent();
      window.notify?.(
        fields.device_type === "physical"
          ? t("dt_keep_physical", { host: client.hostname })
          : `${client.hostname} is now classified as ${fields.device_type.toUpperCase()}.`,
        "success");
    } catch (e) {
      const err = overlay.querySelector("#dt-error");
      err.textContent = e.message; err.classList.remove("hidden");
    }
  };
  overlay.querySelector("#dt-keep").addEventListener("click", () =>
    finish({ device_type: "physical" }));
  overlay.querySelector("#dt-apply").addEventListener("click", () =>
    finish({ device_type: detected, parent_client_id: overlay.querySelector("#dt-host").value || null }));
}

// -----------------------------------------------------------------
// Reusable building blocks ("parts") of the client view. Each part
// renders into a given target element and can live both in the dashboard
// layout and in a detached window (apps/panelpart.js).
// -----------------------------------------------------------------

// --- STATUS part ---
// Layout: ACTION BUTTONS on the LEFT stacked, TEXT (status + info rows)
// on the RIGHT - fills the box well. 1x1-optimized + grows proportionally;
// each info row has its own hover (highlight + tooltip with the label and
// the FULL value of that row, e.g. the complete hostname/OS string).
export function renderStatusPart(target, client) {
  const status = statusInfo(client);
  const m = client.metrics;
  const rows = [
    { label: t("os"), raw: `${osLabel(client.platform, client.release)}`, html: `${osIcon(client.platform, client.release)} ${esc(osLabel(client.platform, client.release))}` },
    { label: t("ip"), raw: client.ip || "–" },
    { label: t("hostname"), raw: client.hostname || "–" },
    { label: t("arch"), raw: client.arch || "?" },
    { label: t("uptime"), raw: m ? formatUptime(m.uptime) : "–" },
    { label: "Agent", raw: client.agent_version || "–", html: agentVersionBadge(client) },
  ];
  target.innerHTML = "";
  // STRETCH instead of scaling: buttons left, text right - the two columns
  // always stretch across the WHOLE panel area (same layout,
  // full space), the font stays constantly readable.
  const holder = document.createElement("div");
  // min-height instead of height: if the panel is too small, the content grows and the
  // panel scrolls (instead of being cut off at the top / squashed).
  holder.style.cssText = "width:100%;min-height:100%;flex:1 0 auto;box-sizing:border-box;display:flex;align-items:center;gap:14px 20px;flex-wrap:wrap;min-width:0;align-content:center";
  const box = holder;
  box.innerHTML = `
    <div class="quick-actions" style="flex:none;width:180px;display:flex;flex-direction:column;gap:7px;margin-top:0;flex-wrap:nowrap;justify-content:center;align-self:stretch">
      ${hasClientPerm(client.id, "c_power") ? `
      <button class="btn" data-quick="reboot" ${client.online ? "" : "disabled"}>🔄 ${t("reboot")}</button>
      <button class="btn" data-quick="shutdown" ${client.online ? "" : "disabled"}>📴 ${t("shutdown")}</button>` : ""}
      ${hasClientPerm(client.id, "manage_agent") || hasClientPerm(client.id, "manage_nodes") ? `
      <button class="btn" data-quick="update" ${client.online ? "" : "disabled"}>⬆️ ${t("update_agent")}</button>` : ""}
      ${hasClientPerm(client.id, "manage_agent") ? `
      <button class="btn btn-danger" data-quick="uninstall" ${client.online ? "" : "disabled"} title="${t("uninstall_agent")}">🗑️ ${t("uninstall_agent")}</button>` : ""}
      ${hasClientPerm(client.id, "c_portforward") ? `
      <button class="btn" data-quick="forward" title="Make a service of this device reachable through the server – without VPN and without open ports at the customer">🔀 Port</button>` : ""}
      ${hasClientPerm(client.id, "manage_clients") ? `<button class="btn" data-quick="edit">✏️ ${t("edit")}</button>` : ""}
    </div>
    <div style="flex:1;min-width:200px;display:flex;flex-direction:column;justify-content:center">
      <div class="status-label" style="font-size:15px"><span class="status-dot-lg ${status.cls}"></span>${status.label}</div>
      <div class="status-info" style="margin-top:10px">
        ${rows.map((r, i) => `<div data-i="${i}" style="padding:2px 4px;font-size:14px"><span>${esc(r.label)}</span><b style="word-break:normal">${r.html || esc(r.raw)}</b></div>`).join("")}
      </div>
    </div>`;
  target.appendChild(holder);
  box.querySelectorAll(".status-info > div[data-i]").forEach((rowEl) => {
    const r = rows[+rowEl.dataset.i];
    bindRowHover(rowEl, () => `<b>${esc(r.label)}</b><br>${esc(r.raw)}`);
  });
  box.querySelectorAll("[data-quick]").forEach((btn) =>
    btn.addEventListener("click", () => {
      // Errors MUST become visible. Previously the returned promise was
      // never evaluated anywhere: if something failed it ended up
      // silently as an unhandled rejection in the console - the
      // button "simply did nothing", with no hint as to why.
      Promise.resolve(handleQuickAction(btn.dataset.quick, client))
        .catch((e) => {
          console.error("[quick-action]", btn.dataset.quick, e);
          window.notify?.(
            `Action "${btn.dataset.quick}" failed: ${e?.message || e}`,
            "error", 12000);
        });
    }));
}

// --- ACTIONS part ---
// 1x1-optimized (fixed natural width) + grows proportionally:
// the action buttons scale with the panel size.
export function renderActionsPart(target, client) {
  target.innerHTML = "";
  const holder = document.createElement("div");
  // NO more scaling down: with many module buttons everything kept getting
  // smaller. Now normal size; if it does not fit, the panel scrolls
  // (.dash-lp-body / .panelpart-body have overflow:auto). min-height instead
  // of height so centering does not cut anything off at the top on overflow.
  holder.className = "actions-holder";
  holder.style.cssText = "width:100%;min-height:100%;flex:1 0 auto;display:flex;align-items:center;justify-content:center;box-sizing:border-box;padding:2px 0";
  const box = document.createElement("div");
  box.style.cssText = "width:100%;max-width:260px;display:flex;flex-direction:column;gap:8px;margin:auto 0";
  box.innerHTML = `
    ${hasClientPerm(client.id, "c_explorer_view") ? `<button class="action-btn" data-action="explorer" ${client.online ? "" : "disabled"}>📁 ${t("file_explorer")}</button>` : ""}
    ${hasClientPerm(client.id, "c_terminal_console") ? `<button class="action-btn" data-action="terminal" ${client.online ? "" : "disabled"}>⌨️ ${t("terminal")}</button>` : ""}
    ${hasClientPerm(client.id, "c_screen_view") ? `<button class="action-btn" data-action="vnc" ${client.online ? "" : "disabled"}>🖥️ ${t("remote_screen")}${hasClientPerm(client.id, "c_screen") ? "" : " 👁️"}</button>` : ""}
    ${isModuleOn("guacamole") && hasClientPerm(client.id, "c_guacamole") ? `<button class="action-btn" data-action="guacamole">🕹️ Guacamole</button>` : ""}
    ${hasClientPerm(client.id, "c_taskmanager_view") ? `<button class="action-btn" data-action="taskmanager" ${client.online ? "" : "disabled"}>📋 ${t("task_manager")}</button>` : ""}
    <button class="action-btn" data-action="copylink" title="Copy a bytera:// link to this device">🔗 ${t("copy_link")}</button>
    ${(isModuleOn("temp-usb") && (isAdmin() || hasGlobalPerm("manage_temp_usb"))) ? `<button class="action-btn" data-action="tempusb" ${client.online ? "" : "disabled"} title="${t("tu_attach_dev_title")}">💾 ${t("tu_attach_dev")}</button>` : ""}`;
  // Buttons of active modules (registerClientAction in modules.js), e.g. "🖧 Proxmox"
  getClientActions().forEach((a, i) => {
    let label = null;
    try { label = a.label(client); } catch { label = null; }
    if (!label) return;
    const btn = document.createElement("button");
    btn.className = "action-btn";
    btn.textContent = label;
    btn.addEventListener("click", () => { try { a.onClick(client); } catch (e) { window.notify?.(e.message, "error"); } });
    box.appendChild(btn);
  });
  holder.appendChild(box);
  target.appendChild(holder);
  box.querySelectorAll("[data-action]").forEach((btn) =>
    btn.addEventListener("click", (ev) => {
      if (btn.dataset.action === "copylink") {
        import("/js/app/modules.js").then(({ getModuleApi }) => {
          const api = getModuleApi("url-scheme");
          if (api) api.copy(api.make("agent", client.id));
          else window.notify?.('The "URL scheme" module is not active.', "warning", 6000);
        }).catch(() => {});
        return;
      }
      if (btn.dataset.action === "tempusb") { window.openRmmApp?.("temp-usb"); return; }
      handleAction(btn.dataset.action, client, ev);
    }));
}

// --- WEBSITES-Part (Quick-Access) ---
// =================================================================
// WEBSITES WIDGET (complete)
// Contains EVERYTHING that used to live under "Edit client":
// create (name, URL, open-in, monitoring + mode + interval),
// edit, toggle open mode, monitoring on/off, favorite star,
// status indicator with tooltip and delete.
// =================================================================
const WS_NOTIFY_LABELS = { down: "on DOWN", up: "on UP", always: "always" };
const WS_INTERVALS = [
  [30, "30 seconds"], [60, "1 minute"], [300, "5 minutes"],
  [900, "15 minutes"], [1800, "30 minutes"], [3600, "1 hour"],
];
function wsIntervalLabel(sec) {
  if (!sec) return "–";
  if (sec >= 3600) return `${Math.round(sec / 3600)} h`;
  if (sec >= 60) return `${Math.round(sec / 60)} min`;
  return `${sec} s`;
}

// Form for creating (w = null) or editing (w = website object).
function wsFormHtml(w, client) {
  const v = w || { name: "", url: client.ip ? `http://${client.ip}:80/` : "",
                   open_mode: "external", monitor_enabled: 0,
                   monitor_notify: "down", monitor_interval_seconds: 300 };
  return `
    <div class="ws-form" style="border:1px solid var(--border);border-radius:8px;padding:8px;margin:6px 0;display:flex;flex-direction:column;gap:6px">
      <input class="ws-f-name" type="text" placeholder="Name (e.g. Proxmox Web UI)" value="${esc(v.name)}" />
      <input class="ws-f-url" type="text" placeholder="https://…" value="${esc(v.url)}" />
      <select class="ws-f-openmode">
        <option value="external" ${v.open_mode !== "internal" ? "selected" : ""}>${t("ws_open_external")}</option>
        <option value="internal" ${v.open_mode === "internal" ? "selected" : ""}>${t("ws_open_internal")}</option>
      </select>
      <label style="display:flex;gap:6px;align-items:center;font-size:12px">
        <input class="ws-f-mon" type="checkbox" ${v.monitor_enabled ? "checked" : ""} /> Enable uptime monitoring
      </label>
      <div class="ws-f-monopts" style="${v.monitor_enabled ? "" : "display:none"};display:flex;flex-direction:column;gap:6px">
        <select class="ws-f-notify">
          <option value="down" ${v.monitor_notify === "down" ? "selected" : ""}>Notify: when DOWN</option>
          <option value="up" ${v.monitor_notify === "up" ? "selected" : ""}>Notify: when UP</option>
          <option value="always" ${v.monitor_notify === "always" ? "selected" : ""}>Notify: always (every scan)</option>
        </select>
        <select class="ws-f-interval">
          ${WS_INTERVALS.map(([sec, lbl]) =>
            `<option value="${sec}" ${Number(v.monitor_interval_seconds) === sec ? "selected" : ""}>Scan every ${lbl}</option>`).join("")}
        </select>
        <div style="font-size:11px;color:var(--subtext)">
          ${t("ws_notify_hint")}
        </div>
      </div>
      <div class="ws-f-error form-error hidden"></div>
      <div style="display:flex;gap:6px">
        <button class="btn-primary ws-f-save" style="flex:1">${w ? t("save") : t("ws_add")}</button>
        <button class="btn ws-f-cancel">${t("cancel")}</button>
      </div>
    </div>`;
}

// Wires up a form. onDone(fields) receives the entered values.
function bindWsForm(formEl, onDone, onCancel) {
  const monCb = formEl.querySelector(".ws-f-mon");
  const opts = formEl.querySelector(".ws-f-monopts");
  monCb.addEventListener("change", () => { opts.style.display = monCb.checked ? "flex" : "none"; });
  // Clicks in the form must neither drag nor expand the panel.
  formEl.addEventListener("mousedown", (e) => e.stopPropagation());
  formEl.addEventListener("click", (e) => e.stopPropagation());
  formEl.querySelector(".ws-f-cancel").addEventListener("click", onCancel);
  formEl.querySelector(".ws-f-save").addEventListener("click", async () => {
    const err = formEl.querySelector(".ws-f-error");
    err.classList.add("hidden");
    const name = formEl.querySelector(".ws-f-name").value.trim();
    const url = formEl.querySelector(".ws-f-url").value.trim();
    if (!name || !url) { err.textContent = t("u_name_und_url_erforderlich"); err.classList.remove("hidden"); return; }
    try {
      await onDone({
        name, url,
        open_mode: formEl.querySelector(".ws-f-openmode").value,
        monitor_enabled: monCb.checked,
        monitor_notify: formEl.querySelector(".ws-f-notify").value,
        monitor_interval_seconds: parseInt(formEl.querySelector(".ws-f-interval").value, 10),
      });
    } catch (e) { err.textContent = e.message; err.classList.remove("hidden"); }
  });
}

export function renderWebsitesPart(target, client) {
  // Re-render on favorite change (star click, also elsewhere) so that
  // favorites move to the top immediately. Bind the listener only ONCE per target.
  if (!target._wsFavListener) {
    target._wsFavListener = (e) => {
      if (!document.body.contains(target)) {
        window.removeEventListener("favorites-changed", target._wsFavListener);
        return;
      }
      if (e.detail?.kind === "websites") renderWebsitesPart(target, client);
    };
    window.addEventListener("favorites-changed", target._wsFavListener);
  }
  const mayEdit = isAdmin() || hasClientPerm(client.id, "c_websites_edit");
  const reload = () => renderWebsitesPart(target, client);

  target.innerHTML = `<div style="color:var(--subtext);font-size:12px">${t("loading")}</div>`;
  api.getClientWebsites(client.id).then((sites) => {
    target.innerHTML = "";
    if (!sites || !sites.length) {
      target.innerHTML = `<div style="color:var(--subtext);font-size:12px;margin-bottom:6px">${t("ws_none")}</div>`;
    } else {
      // Favorite hierarchy in the widget: golden stars (sidebar+dashboard)
      // at the very top, then accent 1 (sidebar), then accent 2 (dashboard),
      // and websites without a star at the bottom.
      const favRank = (w) => {
        const f = favState("websites", w.id);
        if (f.s && f.d) return 0;
        if (f.s) return 1;
        if (f.d) return 2;
        return 3;
      };
      sites = [...sites].sort((a, b) => favRank(a) - favRank(b));
      target.innerHTML = sites.map((w) => {
        const dotColor = !w.monitor_enabled ? "var(--subtext)"
          : w.last_status === "up" ? "var(--online, #3ecf8e)"
          : w.last_status === "down" ? "var(--danger, #ff4d6d)" : "var(--subtext)";
        const favMeta = { name: w.name, url: w.url, clientId: client.id, clientHostname: client.hostname, open_mode: w.open_mode };
        return `
          <div class="action-btn" data-ws="${esc(w.id)}" style="display:flex;align-items:center;gap:8px;padding-right:8px">
            <a href="${esc(w.url)}" data-ws-openlink="${esc(w.id)}"
               style="display:flex;align-items:center;gap:8px;text-decoration:none;flex:1;min-width:0;color:inherit;cursor:pointer">
              <span style="color:${dotColor}">●</span>
              <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(w.name)}${w.open_mode === "internal" ? " 🪟" : ""}</span>
            </a>
            ${favStarHtml("websites", w.id, favMeta)}
            ${mayEdit ? `
              <button class="btn btn-sm" data-ws-openmode="${esc(w.id)}"
                title="${t("ws_tip_mode", { mode: w.open_mode === "internal" ? t("ws_mode_internal") : t("ws_mode_external") })}"
>${w.open_mode === "internal" ? "🪟" : "🔗"}</button>
              <button class="btn btn-sm${w.monitor_enabled ? " active" : ""}" data-ws-mon="${esc(w.id)}"
                title="Monitoring ${w.monitor_enabled ? `on (every ${wsIntervalLabel(w.monitor_interval_seconds)}, ${WS_NOTIFY_LABELS[w.monitor_notify] || w.monitor_notify}) – click: off` : "off – click: on"}"
                style="${w.monitor_enabled ? "" : "opacity:.55"}">📡</button>
              <button class="btn btn-sm" data-ws-edit="${esc(w.id)}" title="Edit"
>✏️</button>
              <button class="btn btn-danger btn-sm" data-ws-del="${esc(w.id)}" data-ws-name="${esc(w.name)}"
                title="${t("ws_tip_delete")}"
>🗑</button>` : ""}
          </div>
          <div class="ws-editbox" data-ws-editbox="${esc(w.id)}" style="display:none"></div>`;
      }).join("");

      // Open by open_mode: 'internal' -> internal browser window,
      // otherwise a normal external tab.
      target.querySelectorAll("[data-ws-openlink]").forEach((a) =>
        a.addEventListener("click", (e) => {
          e.preventDefault();
          const s2 = sites.find((x) => x.id === a.dataset.wsOpenlink);
          if (s2) import("./modules.js").then((m) => m.openWebsiteEntry(s2));
        })
      );
      // BUGFIX: the favorite star in the website WIDGET did not react to
      // clicks. That is why the star is additionally wired DIRECTLY here.
      target.querySelectorAll(".fav-star[data-fav]").forEach((star) => {
        star.addEventListener("mousedown", (e) => { e.preventDefault(); e.stopPropagation(); });
        star.addEventListener("click", (e) => {
          e.preventDefault(); e.stopPropagation();
          const [kind, id] = star.dataset.fav.split(":");
          let meta;
          if (star.dataset.favMeta) { try { meta = JSON.parse(star.dataset.favMeta); } catch {} }
          star.classList.remove("fav-pop");
          void star.offsetWidth;
          star.classList.add("fav-pop");
          cycleFav(kind, id, meta);
        });
      });

      // Toggle open mode
      target.querySelectorAll("[data-ws-openmode]").forEach((b) =>
        b.addEventListener("click", async (e) => {
          e.preventDefault(); e.stopPropagation();
          const w = sites.find((x) => x.id === b.dataset.wsOpenmode);
          try {
            await api.updateClientWebsite(client.id, w.id,
              { open_mode: w.open_mode === "internal" ? "external" : "internal" });
            reload();
          } catch (err) { window.notify?.(t("ws_change_failed", { err: err.message }), "error"); }
        }));

      // Monitoring on/off
      target.querySelectorAll("[data-ws-mon]").forEach((b) =>
        b.addEventListener("click", async (e) => {
          e.preventDefault(); e.stopPropagation();
          const w = sites.find((x) => x.id === b.dataset.wsMon);
          try {
            await api.updateClientWebsite(client.id, w.id, { monitor_enabled: !w.monitor_enabled });
            reload();
          } catch (err) { window.notify?.(t("ws_change_failed", { err: err.message }), "error"); }
        }));

      // Edit (expand the form directly under the row)
      target.querySelectorAll("[data-ws-edit]").forEach((b) =>
        b.addEventListener("click", (e) => {
          e.preventDefault(); e.stopPropagation();
          const w = sites.find((x) => x.id === b.dataset.wsEdit);
          const box = target.querySelector(`[data-ws-editbox="${w.id}"]`);
          if (box.style.display !== "none") { box.style.display = "none"; box.innerHTML = ""; return; }
          box.style.display = "";
          box.innerHTML = wsFormHtml(w, client);
          bindWsForm(box.querySelector(".ws-form"),
            async (fields) => {
              await api.updateClientWebsite(client.id, w.id, fields);
              window.notify?.("Website saved", "success");
              reload();
            },
            () => { box.style.display = "none"; box.innerHTML = ""; });
        }));

      // Status tooltip per row
      target.querySelectorAll("[data-ws]").forEach((rowEl) => {
        const w = sites.find((x) => x.id === rowEl.dataset.ws);
        if (!w) return;
        const statusTxt = !w.monitor_enabled ? t("u_kein_monitoring")
          : w.last_status === "up" ? "reachable"
          : w.last_status === "down" ? t("ws_unreachable", { detail: w.last_error ? ": " + w.last_error : "" })
          : t("ws_unchecked");
        const color = !w.monitor_enabled ? "var(--subtext)"
          : w.last_status === "up" ? "var(--online, #3ecf8e)"
          : w.last_status === "down" ? "var(--danger, #ff4d6d)" : "var(--subtext)";
        const tipHtml = () => `<b>${esc(w.name)}</b><br><span style="color:var(--subtext)">${esc(w.url)}</span>`
          + `<br><span style="color:${color}">●</span> ${esc(statusTxt)}`
          + (w.monitor_enabled ? `<br><span style="color:var(--subtext)">Scan every ${wsIntervalLabel(w.monitor_interval_seconds)}, notify ${WS_NOTIFY_LABELS[w.monitor_notify] || w.monitor_notify}</span>` : "")
          + (w.last_checked ? `<br><span style="color:var(--subtext)">${t("ws_last_check", { date: new Date(w.last_checked).toLocaleString() })}</span>` : "");
        rowEl.addEventListener("mouseenter", (e) => showFleetTip(tipHtml(), e.clientX, e.clientY));
        rowEl.addEventListener("mousemove", (e) => showFleetTip(tipHtml(), e.clientX, e.clientY));
        rowEl.addEventListener("mouseleave", () => hideFleetTip());
      });

      // Delete
      target.querySelectorAll("[data-ws-del]").forEach((b) =>
        b.addEventListener("click", async (e) => {
          e.preventDefault(); e.stopPropagation();
          if (!(await uiConfirm(`Remove website "${b.dataset.wsName}"?`, {
            okText: "Remove", danger: true }))) return;
          try {
            await api.deleteClientWebsite(client.id, b.dataset.wsDel);
            window.notify?.("Website removed.", "success");
            reload();
          } catch (err) { window.notify?.(t("ws_delete_failed", { err: err.message }), "error"); }
        }));
    }

    // "+ Add website": expands the full form directly in the
    // widget (name, URL, open-in, monitoring + mode + interval).
    if (!mayEdit) return;
    const addBox = document.createElement("div");
    const addBtn = document.createElement("button");
    addBtn.className = "action-btn";
    addBtn.style.cssText = "width:100%;justify-content:center;color:var(--accent);margin-top:4px";
    addBtn.textContent = t("ws_add_btn");
    addBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      if (addBox.innerHTML) { addBox.innerHTML = ""; return; }
      addBox.innerHTML = wsFormHtml(null, client);
      bindWsForm(addBox.querySelector(".ws-form"),
        async (fields) => {
          await api.createClientWebsite(client.id, fields);
          window.notify?.(t("ws_linked", { name: fields.name }), "success");
          reload();
        },
        () => { addBox.innerHTML = ""; });
    });
    target.appendChild(addBtn);
    target.appendChild(addBox);
  }).catch(() => { target.innerHTML = `<div style="color:var(--subtext);font-size:12px">No websites.</div>`; });
}


// =================================================================
// CHILDREN WIDGET: VMs and LXC containers that run on THIS client
// (clients with parent_client_id === client.id). Per entry:
//   - status indicator (online/offline) + type icon (🖥️ VM / 📦 LXC)
//   - short info (CPU/RAM, if online)
//   - button "Open": selects the client in the sidebar
//   - button "Window": opens it as its own window (like the desktop drop)
// =================================================================
export function childClientsOf(clientId) {
  return (state.clients || []).filter((c) => c.parent_client_id === clientId);
}

export function renderChildrenPart(target, client) {
  // Follow live: when new metrics arrive for one of the guests (or the client
  // list changes), redraw the panel. Bind the listener only ONCE per target.
  if (!target._kidsListener) {
    target._kidsListener = (e) => {
      if (!document.body.contains(target)) {
        window.removeEventListener("metrics-updated", target._kidsListener);
        return;
      }
      const id = e?.detail?.id;
      if (!id || childClientsOf(client.id).some((c) => c.id === id)) {
        renderChildrenPart(target, client);
      }
    };
    window.addEventListener("metrics-updated", target._kidsListener);
  }
  target.innerHTML = "";
  const kids = childClientsOf(client.id)
    .sort((a, b) => (b.online - a.online) || String(a.hostname || "").localeCompare(String(b.hostname || "")));

  if (!kids.length) {
    target.innerHTML = `<div style="color:var(--subtext);font-size:12px">
      ${t("pn_no_kids")}<br>
      ${t("kids_hint")}</div>`;
    return;
  }

  const head = document.createElement("div");
  head.style.cssText = "font-size:11.5px;color:var(--subtext);margin-bottom:4px";
  const onlineCount = kids.filter((c) => c.online).length;
  head.textContent = `${kids.length} guest${kids.length === 1 ? "" : " systems"} · ${onlineCount} online`;
  target.appendChild(head);

  for (const kid of kids) {
    const dot = kid.online ? "var(--online, #3ecf8e)" : "var(--subtext)";
    const cpu = kid.metrics?.cpuLoad;
    const memPct = kid.metrics?.memTotal
      ? Math.round((kid.metrics.memUsed || 0) / kid.metrics.memTotal * 100) : null;
    const info = kid.online && (cpu != null || memPct != null)
      ? `${cpu != null ? `CPU ${Math.round(cpu)}%` : ""}${cpu != null && memPct != null ? " · " : ""}${memPct != null ? `RAM ${memPct}%` : ""}`
      : (kid.online ? "online" : "offline");

    const row = document.createElement("div");
    row.className = "action-btn";
    row.style.cssText = "display:flex;align-items:center;gap:8px;padding-right:8px";
    row.innerHTML = `
      <span style="color:${dot};flex:none">●</span>
      ${clientIconSvg(kid, 18)}
      <span style="flex:1;min-width:0;display:flex;flex-direction:column;line-height:1.2;text-align:left">
        <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(kid.hostname || kid.id)}</span>
        <span style="font-size:10.5px;color:var(--subtext)">${esc(info)}</span>
      </span>
      <button class="btn btn-sm" data-kid-open="${esc(kid.id)}" title="${t("kids_select")}"
>${t("kid_open")}</button>
      <button class="btn btn-sm" data-kid-win="${esc(kid.id)}" title="${t("kid_open_win")}"
>↗️</button>`;
    target.appendChild(row);
  }

  // "Open": select the client in the sidebar (main view switches).
  target.querySelectorAll("[data-kid-open]").forEach((b) =>
    b.addEventListener("click", (e) => {
      e.preventDefault(); e.stopPropagation();
      const id = b.dataset.kidOpen;
      import("./sidebar.js").then((m) => {
        try { m.selectClientExternal(id); }
        catch { state.selection = { type: "client", id }; }
      }).catch(() => { state.selection = { type: "client", id }; });
    }));

  // "↗️": open as its own window (same view as the desktop drop).
  target.querySelectorAll("[data-kid-win]").forEach((b) =>
    b.addEventListener("click", (e) => {
      e.preventDefault(); e.stopPropagation();
      const kid = findClient(b.dataset.kidWin);
      if (!kid) return;
      openWindow({
        singleton: true,
        key: `panelpart-${kid.id}-client-x`, appId: "panelpart",
        title: kid.hostname,
        props: { clientId: kid.id, part: "client" },
        clientColor: kid.color, w: 900, h: 620,
      });
    }));
}

// Whether a client has any linked websites at all (for auto-hide in the default layout).
export function clientHasWebsites(clientId) {
  return api.getClientWebsites(clientId).then((s) => !!(s && s.length)).catch(() => false);
}

// =================================================================
// NOTES with visibility + activity log
//   for everyone        -> anyone allowed to see the client
//   only me             -> the author only
//   for specific        -> author + selected users
// The author may edit/delete (admins may too, for cleanup, but do
// not see the content of other people's private notes).
// =================================================================
const NOTE_VIS = {
  all: { icon: "🌍", label: t("note_vis_all") },
  private: { icon: "🔒", label: t("note_vis_private") },
  custom: { icon: "👥", label: t("note_vis_custom") },
};

export function renderNotesPart(target, client) {
  const mayEdit = isAdmin() || hasClientPerm(client.id, "c_notes_edit");
  let showLog = false;
  let users = null;              // {users:[…], groups:[…]}
  let editingId = null;
  let addOpen = false;           // "Add note" collapsed by default

  target.innerHTML = `<div style="color:var(--subtext);font-size:12px">${t("loading")}</div>`;

  async function load() {
    let notes = [];
    try { notes = await api.getNotes(client.id); }
    catch (e) {
      target.innerHTML = `<div style="color:var(--danger);font-size:12px">${esc(e.message)}</div>`;
      return;
    }
    if (!users) { try { users = await api.getNotesUsers(client.id); } catch { users = { users: [], groups: [] }; } }
    draw(notes);
  }

  // Display name of a share ({type,id} or old plain user ID).
  function shareName(item) {
    const type = typeof item === "string" ? "user" : (item.type || "user");
    const id = typeof item === "string" ? item : item.id;
    if (type === "group") {
      const g = (users?.groups || []).find((x) => x.id === id);
      return g ? `👥 ${g.name}` : "👥 ?";
    }
    const u = (users?.users || []).find((x) => x.id === id);
    return u ? u.username : id;
  }

  // Form for a new note or for editing.
  function formHtml(note) {
    const v = note?.visibility || "all";
    return `
      <div class="note-form" style="border:1px solid var(--border);border-radius:8px;padding:8px;display:flex;flex-direction:column;gap:6px;margin-bottom:8px">
        <textarea class="nf-text" rows="3" placeholder="Write a note…"
          style="resize:vertical;min-height:56px">${esc(note?.text || "")}</textarea>
        <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
          <select class="nf-vis" style="flex:1;min-width:150px">
            ${Object.entries(NOTE_VIS).map(([k, o]) =>
              `<option value="${k}" ${v === k ? "selected" : ""}>${o.icon} Visible ${o.label}</option>`).join("")}
          </select>
          <label style="display:flex;gap:5px;align-items:center;font-size:12px;color:var(--subtext)">
            <input type="checkbox" class="nf-pin" ${note?.pinned ? "checked" : ""} /> pin
          </label>
        </div>
        <div class="nf-share" style="${v === "custom" ? "" : "display:none"}">
          ${subjectPickerHtml(users, note?.shared_with || [], { name: "nf-subj" })}
        </div>
        <div class="nf-error form-error hidden"></div>
        <div style="display:flex;gap:6px">
          <button class="btn-primary nf-save" style="flex:1">${note ? t("save") : "+ Create note"}</button>
          ${note ? `<button class="btn nf-cancel">Cancel</button>` : ""}
        </div>
      </div>`;
  }

  function bindForm(el, note) {
    const visSel = el.querySelector(".nf-vis");
    const shareBox = el.querySelector(".nf-share");
    visSel.addEventListener("change", () => {
      shareBox.style.display = visSel.value === "custom" ? "" : "none";
    });
    initSubjectPicker(shareBox);
    el.addEventListener("mousedown", (e) => e.stopPropagation());
    el.querySelector(".nf-cancel")?.addEventListener("click", () => {
      editingId = null; addOpen = false; load();
    });
    el.querySelector(".nf-save").addEventListener("click", async () => {
      const err = el.querySelector(".nf-error");
      err.classList.add("hidden");
      const text = el.querySelector(".nf-text").value.trim();
      if (!text) { err.textContent = "The note is empty"; err.classList.remove("hidden"); return; }
      const data = {
        text,
        visibility: visSel.value,
        pinned: el.querySelector(".nf-pin").checked,
        shared_with: readSubjectPicker(el.querySelector(".nf-share")),
      };
      if (data.visibility === "custom" && !data.shared_with.length) {
        err.textContent = t("note_pick_user");
        err.classList.remove("hidden"); return;
      }
      try {
        if (note) await api.updateNote(client.id, note.id, data);
        else { await api.createNote(client.id, data); addOpen = false; }
        editingId = null;
        load();
      } catch (e) { err.textContent = e.message; err.classList.remove("hidden"); }
    });
  }

  function draw(notes) {
    target.innerHTML = `
      <div style="display:flex;flex-direction:column;gap:6px;min-height:0">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
          <span style="font-size:11.5px;color:var(--subtext)">${notes.length} note${notes.length === 1 ? "" : "s"}</span>
          <button class="btn btn-sm" id="note-log">${t("note_history")}</button>
        </div>
        <div id="note-logbox" style="display:none"></div>
        <div id="note-add"></div>
        <div id="note-list" style="display:flex;flex-direction:column;gap:6px"></div>
      </div>`;

    // "Add note" is COLLAPSED by default - otherwise the form would
    // permanently take half the space in the widget. A click opens it;
    // the state is kept across redraws (addOpen).
    if (mayEdit && !editingId) {
      const box = target.querySelector("#note-add");
      const btn = document.createElement("button");
      btn.className = "action-btn";
      btn.style.cssText = "width:100%;justify-content:center;color:var(--accent)";
      btn.textContent = addOpen ? t("note_add_cancel") : t("note_add");
      btn.addEventListener("click", () => { addOpen = !addOpen; draw(notes); });
      box.appendChild(btn);
      if (addOpen) {
        const form = document.createElement("div");
        form.style.marginTop = "6px";
        form.innerHTML = formHtml(null);
        box.appendChild(form);
        bindForm(form.querySelector(".note-form"), null);
        form.querySelector(".nf-text")?.focus();
      }
    }

    const listEl = target.querySelector("#note-list");
    if (!notes.length) {
      listEl.innerHTML = `<div style="color:var(--subtext);font-size:12px">${t("pn_no_notes")}</div>`;
    } else {
      listEl.innerHTML = notes.map((n) => {
        if (editingId === n.id) return `<div class="note-edit" data-edit="${esc(n.id)}"></div>`;
        const vinfo = NOTE_VIS[n.visibility] || NOTE_VIS.all;
        const shared = n.visibility === "custom" && (n.shared_with || []).length
          ? ` (${n.shared_with.map(shareName).map(esc).join(", ")})` : "";
        const when = new Date(n.updated_at || n.created_at).toLocaleString("de-DE");
        return `
          <div class="panel" style="padding:7px 9px;${n.pinned ? "border-color:var(--accent)" : ""}">
            <div style="display:flex;justify-content:space-between;gap:8px;align-items:flex-start">
              <div style="font-size:11px;color:var(--subtext)">
                ${n.pinned ? "📌 " : ""}${vinfo.icon} ${esc(vinfo.label)}${shared}
                · ${esc(n.author_name || "?")} · ${when}
              </div>
              ${n.can_edit ? `<div style="display:flex;gap:4px;flex:none">
                ${n.hidden ? "" : `<button class="btn btn-sm" data-nedit="${esc(n.id)}">✏️</button>`}
                <button class="btn btn-danger btn-sm" data-ndel="${esc(n.id)}">🗑</button>
              </div>` : ""}
            </div>
            <div style="font-size:13px;white-space:pre-wrap;overflow-wrap:anywhere;margin-top:3px">
              ${n.hidden ? `<i style="color:var(--subtext)">${t("note_private_hidden")}</i>` : esc(n.text)}
            </div>
          </div>`;
      }).join("");

      // Insert the edit form in place of the note
      const editEl = listEl.querySelector("[data-edit]");
      if (editEl) {
        const note = notes.find((x) => x.id === editEl.dataset.edit);
        editEl.innerHTML = formHtml(note);
        bindForm(editEl.querySelector(".note-form"), note);
      }
      listEl.querySelectorAll("[data-nedit]").forEach((b) =>
        b.addEventListener("click", () => { editingId = b.dataset.nedit; draw(notes); }));
      listEl.querySelectorAll("[data-ndel]").forEach((b) =>
        b.addEventListener("click", async () => {
          if (!(await uiConfirm(t("note_delete_q"), { okText: t("delete"), danger: true }))) return;
          try { await api.deleteNote(client.id, b.dataset.ndel); load(); }
          catch (e) { window.notify?.(e.message, "error"); }
        }));
    }

    // Expand/collapse the activity log
    const logBtn = target.querySelector("#note-log");
    const logBox = target.querySelector("#note-logbox");
    logBox.style.display = showLog ? "" : "none";
    if (showLog) fillLog(logBox);
    logBtn.addEventListener("click", () => {
      showLog = !showLog;
      logBox.style.display = showLog ? "" : "none";
      if (showLog) fillLog(logBox);
    });
  }

  async function fillLog(box) {
    box.innerHTML = `<div style="color:var(--subtext);font-size:11.5px">Loading history…</div>`;
    try {
      const log = await api.getNotesActivity(client.id);
      box.innerHTML = log.length ? `
        <div style="border:1px solid var(--border);border-radius:8px;padding:6px;max-height:160px;overflow:auto">
          ${log.map((e) => `
            <div style="font-size:11.5px;padding:2px 0;display:flex;gap:6px">
              <span style="color:var(--subtext);flex:none">${new Date(e.created_at).toLocaleString("de-DE")}</span>
              <span style="flex:none"><b>${esc(e.actor_name)}</b></span>
              <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
                ${esc(e.label)}${e.details ? " – " + esc(e.details) : ""}</span>
            </div>`).join("")}
        </div>`
        : `<div style="color:var(--subtext);font-size:11.5px">${t("note_no_activity")}</div>`;
    } catch (e) {
      box.innerHTML = `<div style="color:var(--danger);font-size:11.5px">${esc(e.message)}</div>`;
    }
  }

  load();
}

// --- OVERVIEW part (single sub-view: metrics|notes|disk) ---
// tab selects the sub-view to show. rerender() allows toggles/time range.
export function renderOverviewSub(target, client, tab, rerender) {
  // Load history on first display.
  if (!hasSeeded(client.id)) {
    seedHistory(client.id, []);
    api.getMetricsHistory(client.id).then((res) => {
      seedHistory(client.id, res.points || []);
      if (state.selection && state.selection.type === "client" && state.selection.id === client.id) rerender?.();
    }).catch(() => {});
  }
  target.innerHTML = "";
  const m = client.metrics;

  if (tab === "notes") return renderNotesPart(target, client);

  if (!m) { target.innerHTML = `<div style="color:var(--subtext);padding:20px 0">${t("no_live_data")}</div>`; return; }

  if (tab === "disk") {
    const disks = (m.disks && m.disks.length) ? m.disks
      : [{ device: "System", mountpoint: "", used: m.diskUsed, total: m.diskTotal }];
    const row = document.createElement("div");
    row.className = "metrics-row";
    row.style.flexWrap = "wrap";
    row.innerHTML = disks.map((d, i) => {
      const pct = d.total ? (d.used / d.total) * 100 : 0;
      const label = esc(d.device || d.mountpoint || "Disk");
      return `<div class="donut-wrap" data-disk="${i}" style="border-radius:10px;padding:4px">${gradientDonutSvg(pct, DISK_GRADIENT, label, `${formatBytes(d.used)} / ${formatBytes(d.total)}`)}</div>`;
    }).join("");
    target.appendChild(row);
    // Hover PER disk: highlight the donut + tooltip with the exact values
    // of EXACTLY this disk (device, used/total, free, percent).
    row.querySelectorAll("[data-disk]").forEach((el) => {
      const d = disks[+el.dataset.disk];
      const pct = d.total ? Math.round((d.used / d.total) * 100) : 0;
      bindRowHover(el, () => `<b>${esc(d.device || d.mountpoint || "Disk")}</b>${d.mountpoint && d.device ? ` <span style="color:var(--subtext)">(${esc(d.mountpoint)})</span>` : ""}<br>` +
        `Used: <b>${formatBytes(d.used)}</b> / ${formatBytes(d.total)} (${pct}%)<br>` +
        `<span style="color:var(--subtext)">Free: ${formatBytes((d.total || 0) - (d.used || 0))}</span>`);
    });
    return;
  }

  // metrics
  const cpuPct = m.cpuLoad ?? 0;
  const ramPct = m.memTotal ? (m.memUsed / m.memTotal) * 100 : 0;
  const swapTotal = m.swapTotal ?? 0, swapUsed = m.swapUsed ?? 0;
  const swapPct = swapTotal ? (swapUsed / swapTotal) * 100 : 0;
  const swapDonut = swapTotal > 0 ? `
    <div class="donut-wrap" data-donut="swap" style="border-radius:10px;padding:4px">
      ${gradientDonutSvg(swapPct, RAM_GRADIENT, "Swap", `${formatBytes(swapUsed)} / ${formatBytes(swapTotal)}`)}
      <div class="metric-sub">${t("free")}: ${formatBytes(swapTotal - swapUsed)}</div>
    </div>` : "";

  const topRow = document.createElement("div");
  topRow.className = "metrics-row";
  topRow.innerHTML = `
    <div class="donut-wrap" data-donut="cpu" style="border-radius:10px;padding:4px">
      ${gradientDonutSvg(cpuPct, CPU_GRADIENT, "CPU", "")}
      <div class="metric-sub">${m.cpuCores ?? "?"} ${t("cores")} · ${m.cpuThreads ?? "?"} ${t("threads")}</div>
    </div>
    <div class="donut-wrap" data-donut="ram" style="border-radius:10px;padding:4px">
      ${gradientDonutSvg(ramPct, RAM_GRADIENT, "RAM", `${formatBytes(m.memUsed)} / ${formatBytes(m.memTotal)}`)}
      <div class="metric-sub">${t("free")}: ${formatBytes(m.memAvailable ?? (m.memTotal - m.memUsed))}</div>
    </div>
    ${swapDonut}`;
  target.appendChild(topRow);

  // Hover PER donut: highlight + tooltip with the exact live values
  // of EXACTLY this metric (not just the panel name).
  {
    const donutTips = {
      cpu: () => `<b>CPU usage</b><br><b>${Math.round(cpuPct)}%</b>` +
        (m.cpuModel ? `<br><span style="color:var(--subtext)">${esc(m.cpuModel)}</span>` : "") +
        `<br><span style="color:var(--subtext)">${m.cpuCores ?? "?"} ${t("cores")} · ${m.cpuThreads ?? "?"} ${t("threads")}${m.cpuFreq ? ` · ${Math.round(m.cpuFreq)} MHz` : ""}</span>`,
      ram: () => `<b>RAM usage</b><br><b>${Math.round(ramPct)}%</b> — ${formatBytes(m.memUsed)} / ${formatBytes(m.memTotal)}` +
        `<br><span style="color:var(--subtext)">${t("free")}: ${formatBytes(m.memAvailable ?? (m.memTotal - m.memUsed))}</span>`,
      swap: () => `<b>Swap usage</b><br><b>${Math.round(swapPct)}%</b> — ${formatBytes(swapUsed)} / ${formatBytes(swapTotal)}` +
        `<br><span style="color:var(--subtext)">${t("free")}: ${formatBytes(swapTotal - swapUsed)}</span>`,
    };
    topRow.querySelectorAll("[data-donut]").forEach((el) => bindRowHover(el, donutTips[el.dataset.donut] || (() => "")));
  }

  const range = getHistoryRange(client.id, TIME_RANGES[timeRangeIndex].ms);
  const rangeBar = document.createElement("div");
  rangeBar.style.cssText = "display:flex;justify-content:flex-end;align-items:center;gap:8px;margin:14px 0 4px";
  rangeBar.innerHTML = `
    <span style="font-size:11px;color:var(--subtext)">${t("time_range")}:</span>
    <select class="ov-time-range">
      ${TIME_RANGES.map((r, i) => `<option value="${i}" ${i === timeRangeIndex ? "selected" : ""}>${r.label}</option>`).join("")}
    </select>`;
  target.appendChild(rangeBar);

  const chartsRow = document.createElement("div");
  chartsRow.style.cssText = "display:flex;gap:20px;flex-wrap:wrap";
  const cpuChartWrap = document.createElement("div");
  cpuChartWrap.className = "mini-chart";
  cpuChartWrap.innerHTML = `<div style="font-size:11px;color:var(--subtext);margin-bottom:4px">${t("cpu_history")} (%)</div>`;
  cpuChartWrap.appendChild(interactiveChart([{ label: "CPU", color: "#a97cff", values: range.cpu, timestamps: range.ts }], { unit: "%", yMax: 100 }));
  const ramChartWrap = document.createElement("div");
  ramChartWrap.className = "mini-chart";
  ramChartWrap.innerHTML = `<div style="font-size:11px;color:var(--subtext);margin-bottom:4px">${t("ram_history")} (%)</div>`;
  ramChartWrap.appendChild(interactiveChart([{ label: "RAM", color: "#c7d34a", values: range.ram, timestamps: range.ts }], { unit: "%", yMax: 100 }));
  const netChartWrap = document.createElement("div");
  netChartWrap.className = "mini-chart";
  const netHeader = document.createElement("div");
  netHeader.style.cssText = "display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;gap:6px;flex-wrap:wrap";
  netHeader.innerHTML = `
    <span style="font-size:11px;color:var(--subtext)">${t("network")}</span>
    <span style="font-size:11px">
      <label style="color:#3ecf8e;margin-right:8px;cursor:pointer"><input type="checkbox" class="ov-net-in" ${showNetIn ? "checked" : ""} /> ↓ ${formatBytes(m.netIn ?? 0)}/s</label>
      <label style="color:#f5a524;cursor:pointer"><input type="checkbox" class="ov-net-out" ${showNetOut ? "checked" : ""} /> ↑ ${formatBytes(m.netOut ?? 0)}/s</label>
    </span>`;
  netChartWrap.appendChild(netHeader);
  const netSeries = [];
  if (showNetIn) netSeries.push({ label: "↓ In", color: "#3ecf8e", values: range.netIn, timestamps: range.ts });
  if (showNetOut) netSeries.push({ label: "↑ Out", color: "#f5a524", values: range.netOut, timestamps: range.ts });
  if (netSeries.length) netChartWrap.appendChild(interactiveChart(netSeries, { formatValue: (v) => formatBytes(v) + "/s" }));
  chartsRow.appendChild(cpuChartWrap); chartsRow.appendChild(ramChartWrap); chartsRow.appendChild(netChartWrap);
  target.appendChild(chartsRow);

  const netIn = target.querySelector(".ov-net-in");
  const netOut = target.querySelector(".ov-net-out");
  const rangeSel = target.querySelector(".ov-time-range");
  if (netIn) netIn.addEventListener("change", () => { showNetIn = netIn.checked; rerender?.(); });
  if (netOut) netOut.addEventListener("change", () => { showNetOut = netOut.checked; rerender?.(); });
  if (rangeSel) rangeSel.addEventListener("change", () => { timeRangeIndex = parseInt(rangeSel.value); rerender?.(); });
}

// Metadata for the individual sub-views of an overview folder.
export const OVERVIEW_SUBS = {
  metrics: () => t("tab_metrics"),
  notes: () => t("tab_notes"),
  disk: () => t("tab_disk"),
};

// -----------------------------------------------------------------
// Quick Actions & Actions
// -----------------------------------------------------------------

export async function handleQuickAction(action, client) {
  if (action === "forward") {
    // Port forwarding. The most convenient way to a service on the
    // device: the server makes it reachable on its own port and
    // passes everything through the existing agent connection. No VPN,
    // no driver, no open ports at the customer.
    let presets = [];
    try { presets = (await api.listForwards()).presets || []; } catch { }
    const service = await uiChoice(`Which service on ${client.hostname}?`,
      [...presets.map((p) => ({ label: `${p.name} (Port ${p.port})`, value: String(p.port) })),
       { label: "Other port …", value: "custom" }],
      { description: "The service is made reachable through the server. "
                     + "Nothing changes on the device." });
    if (!service) return;

    let port = parseInt(service, 10);
    if (service === "custom") {
      const input = await uiPrompt("Port on the device", {
        description: "The port number of the service, e.g. 8080.", value: "8080" });
      port = parseInt(input, 10);
      if (!port) return;
    }

    const duration = await uiChoice("How long?", [
      { label: "1 hour", value: "60" },
      { label: "4 hours", value: "240" },
      { label: "24 hours", value: "1440" },
      { label: "Until closed", value: "0" },
    ], { description: "After that the port is automatically closed again." });
    if (duration === null || duration === undefined) return;

    try {
      const rec = await api.createForward(client.id, port,
        { minutes: parseInt(duration, 10), label: client.hostname });
      const host = window.location.hostname;
      const preset = presets.find((p) => p.port === port);
      await uiConfirm(`✅ ${host}:${rec.listen_port}`, {
        description: `The service (port ${port} on ${client.hostname}) is now `
          + `reachable at ${host}:${rec.listen_port}.\n\n`
          + (preset ? preset.hint + "\n\n" : "")
          + `The forwarding runs through the server – nothing was installed `
          + `on the device and no port had to be opened.`,
        okText: "Understood" });
    } catch (e) {
      window.notify?.("Forwarding failed: " + e.message, "error", 12000);
    }
    return;
  }
  if (action === "edit") {
    openWindow({ key: `edit-${client.id}`, appId: "edit-client", title: `${t("edit")} — ${client.hostname}`, props: { clientId: client.id }, w: 480, h: 520 });
    return;
  }
  if (action === "reboot" || action === "shutdown") {
    const label = action === "reboot" ? t("reboot") : t("shutdown");
    if (!(await uiConfirm(`${client.hostname}: ${label}?`, { okText: label, danger: true }))) return;
    const isWin = (client.platform || "").toLowerCase().includes("windows");
    let cmd;
    if (action === "reboot") cmd = isWin ? "shutdown /r /t 0" : "sudo reboot";
    else cmd = isWin ? "shutdown /s /t 0" : "sudo shutdown -h now";
    try { await api.execOnClient(client.id, cmd); window.notify?.(t("command_sent"), "success"); }
    catch (e) { window.notify?.(t("error_prefix", { err: e.message }), "error"); }
    return;
  }
  if (action === "update") {
    // The update button offers two paths: the ordinary agent update and the
    // management of the NODE (a separate process on the client, see feature/node.js).
    // Only what the user is permitted for on THIS client is offered:
    // agent update -> "manage_agent", node -> "manage_nodes" (client permission).
    const opts = [];
    if (hasClientPerm(client.id, "manage_agent")) opts.push({ label: `⬆️ ${t("update_agent")}`, value: "agent" });
    if (hasClientPerm(client.id, "manage_nodes")) opts.push({ label: "🧱 Manage node …", value: "node" });
    if (!opts.length) return;
    const choice = opts.length === 1 ? opts[0].value : await uiChoice(`${client.hostname}`, opts, {
      description: "A node is a separate process alongside the agent: it checks Docker and runs "
        + "node modules (e.g. VPN, proxy, network drives). An ordinary client does not need one.",
    });
    if (!choice) return;
    if (choice === "node") {
      openWindow({ key: `node-${client.id}`, appId: "node", title: `Node \u2013 ${client.hostname}`,
                   props: { clientId: client.id, clientName: client.hostname }, w: 820, h: 680 });
      return;
    }

    if (!(await uiConfirm(t("agent_update_q", { host: client.hostname }), { description: t("agent_update_desc"), okText: t("agent_update_ok") }))) return;
    window.notify?.(t("pn_agent_updating", { host: client.hostname }), "info", 60000, { tag: "agent-update:" + client.id });
    const tag = { tag: "agent-update:" + client.id };
    // The update is a BACKEND JOB: the backend waits for the agent's response.
    // This page only displays - reloading aborts nothing, the state
    // is shown in the task tray.
    let lastPhase = "";
    try {
      const res = await api.updateAgent(client.id, (job) => {
        if (job.running && job.phase && job.phase !== lastPhase) {
          lastPhase = job.phase;
          window.notify?.(`${client.hostname}: ${job.detail || job.phase}`,
            job.phase === "launched-fallback" ? "warn" : "info", 60000, tag);
        }
      });
      window.notify?.(t("pn_agent_updated", { host: client.hostname })
        + (res?.version ? ` (${res.version})` : ""), "success", 8000, tag);
    } catch (e) {
      window.notify?.(t("pn_update_failed") + ": " + e.message
        + (/rollback|did not connect/i.test(e.message) ? " – the previous agent version keeps running." : ""),
        "error", 30000, tag);
    }
    return;
  }
  if (action === "uninstall") {
    if (!(await uiConfirm(t("agent_uninstall_q", { host: client.hostname }), {
      description: t("agent_uninstall_desc"),
      okText: t("agent_uninstall_ok"), danger: true }))) return;
    window.notify?.(t("agent_uninstall_run", { host: client.hostname }), "info", 60000, { tag: "agent-uninstall:" + client.id });
    try {
      const res = await api.uninstallAgent(client.id);
      if (res && res.removed) {
        // Removed from the dashboard. Clear the selection if this client was selected.
        if (state.selection && state.selection.type === "client" && state.selection.id === client.id) {
          state.selection = null;
        }
        window.notify?.(t("pn_agent_removed", { host: client.hostname }), "success", 8000, { tag: "agent-uninstall:" + client.id });
        renderMainContent();
      } else {
        window.notify?.(`Uninstall on ${client.hostname} completed.`, "success", 6000, { tag: "agent-uninstall:" + client.id });
      }
    } catch (e) {
      window.notify?.("Uninstall failed: " + e.message, "error", 12000, { tag: "agent-uninstall:" + client.id });
    }
  }
}

export function handleAction(action, client, ev, { forceWeb = false, params = null } = {}) {
  // Modules may take over (e.g. client app: open externally) - not with forceWeb
  if (!forceWeb && runActionHandlers(action, client, ev)) return;
  const colorProps = { clientColor: client.color };
  // params (e.g. from bytera://.../remotescreen/screen=2&fps=10) go into props,
  // so the respective app (e.g. remote support) can evaluate them.
  const props = { clientId: client.id, clientName: client.hostname, platform: client.platform, ...(params || {}) };
  if (action === "terminal") {
    // Stable key: clicking again brings the existing terminal to the front.
    // Holding Shift creates a second terminal (suffix --2).
    const openCount = state.windows.filter((w) => w.appId === "terminal" && w.props?.clientId === client.id).length;
    const title = openCount > 0
      ? `${t("terminal")} — ${client.hostname} (${openCount + 1})`
      : `${t("terminal")} — ${client.hostname}`;
    openWindow({ key: `terminal-${client.id}`, appId: "terminal", title, props, ...colorProps });
  }
  else if (action === "explorer") openWindow({ key: `explorer-${client.id}`, appId: "explorer", title: `${t("file_explorer")} — ${client.hostname}`, props, ...colorProps });
  else if (action === "vnc") openWindow({ key: `vnc-${client.id}`, appId: "vnc", title: `${t("remote_screen")} — ${client.hostname}`, props, ...colorProps, w: 800, h: 600 });
  else if (action === "guacamole") openWindow({
    key: `guac-${client.id}`, appId: "guacamole",
    title: `Guacamole — ${client.hostname}`,
    props: { clientId: client.id, clientName: client.hostname, host: client.ip, platform: client.platform },
    ...colorProps, w: 900, h: 640,
  });
  else if (action === "taskmanager") openWindow({ key: `task-${client.id}`, appId: "taskmanager", title: `${t("task_manager")} — ${client.hostname}`, props, ...colorProps });
}

// -----------------------------------------------------------------
// AGGREGIERTE ANSICHT (Tenant / Location)
// -----------------------------------------------------------------

function clientCard(c) {
  const m = c.metrics;
  const status = statusInfo(c);
  const cpuPct = m ? m.cpuLoad ?? 0 : 0;
  const ramPct = m && m.memTotal ? (m.memUsed / m.memTotal) * 100 : 0;
  return `
    <div class="panel" style="cursor:pointer" data-open-client="${c.id}">
      <div style="display:flex;align-items:center;justify-content:space-between">
        <strong><span class="status-dot-lg ${status.cls}"></span>${esc(c.hostname)}</strong>
        <span style="font-size:11px;color:var(--subtext)">${osIcon(c.platform, c.release)} ${esc(c.ip || "")}</span>
      </div>
      <div style="margin-top:10px;font-size:12px;color:var(--subtext)">
        CPU ${Math.round(cpuPct)}% · RAM ${Math.round(ramPct)}%${m ? " · " + formatUptime(m.uptime) : ""}
      </div>
    </div>`;
}

function renderAggregateView(el, type, id) {
  let title, html = "";

  if (type === "tenant") {
    const tenant = state.hierarchy.tenants.find((x) => x.id === id);
    title = tenant ? tenant.name : "Tenant";
    // Group clients by location, with a heading per location
    const locations = state.hierarchy.locations.filter((l) => l.tenant_id === id);
    let totalOnline = 0, totalCount = 0;

    for (const loc of locations) {
      const clients = state.clients.filter((c) => c.location_id === loc.id && !c.parent_client_id);
      if (!clients.length) continue;
      totalCount += clients.length;
      totalOnline += clients.filter((c) => c.online).length;
      html += `
        <h3 style="margin:18px 0 8px;color:var(--subtext);font-size:13px;text-transform:uppercase">
          📍 ${esc(loc.name)} <span style="font-weight:400">(${clients.filter((c) => c.online).length}/${clients.length})</span>
        </h3>
        <div class="client-grid">${clients.map(clientCard).join("")}</div>`;
    }

    // Clients without a location (but in the tenant)
    const noLoc = state.clients.filter((c) => c.tenant_id === id && !c.location_id && !c.parent_client_id);
    if (noLoc.length) {
      totalCount += noLoc.length;
      totalOnline += noLoc.filter((c) => c.online).length;
      html += `<h3 style="margin:18px 0 8px;color:var(--subtext);font-size:13px;text-transform:uppercase">${t("no_location")}</h3>
               <div class="client-grid">${noLoc.map(clientCard).join("")}</div>`;
    }

    el.innerHTML = `
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">
        <h2 style="margin:0">${esc(title)}</h2>
        <span style="color:var(--subtext);font-size:13px">${totalOnline}/${totalCount} ${t("online")}</span>
      </div>
      ${html || `<div class="empty-state">${t("no_devices")}</div>`}`;

  } else {
    const location = state.hierarchy.locations.find((l) => l.id === id);
    title = location ? location.name : "Location";
    const clients = state.clients.filter((c) => c.location_id === id && !c.parent_client_id);
    const online = clients.filter((c) => c.online).length;
    el.innerHTML = `
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
        <h2 style="margin:0">📍 ${esc(title)}</h2>
        <span style="color:var(--subtext);font-size:13px">${online}/${clients.length} ${t("online")}</span>
      </div>
      <div class="client-grid">${clients.map(clientCard).join("") || `<div class="empty-state">${t("no_devices")}</div>`}</div>`;
  }

  el.querySelectorAll("[data-open-client]").forEach((card) =>
    card.addEventListener("click", () => {
      state.selection = { type: "client", id: card.dataset.openClient };
      renderMainContent();
      // Expand the sidebar path to the client so it is visible
      import("./sidebar.js").then((m) => m.revealClient(card.dataset.openClient));
    })
  );
}


// ==========================================================================
// PATCH PANEL: available software updates of a client
// --------------------------------------------------------------------------
// Scan, list of programs with a newer version available, install.
// The scan runs in the background (reverse proxies abort long requests),
// so the progress is polled instead of waiting for a response.
// ==========================================================================

const PATCH_LEVEL_COLORS = {
  security: "#ff4d6d", critical: "#f5a524", important: "#facc15",
  moderate: "#4da6ff", low: "#64748b", feature: "#a78bfa", other: "#7f93ad",
};

export function renderPatchesPart(bodyEl, client) {
  let pollTimer = null;
  let busy = false;

  const stop = () => { clearTimeout(pollTimer); pollTimer = null; };
  // When the panel is rebuilt, no timer may keep running.
  bodyEl.addEventListener("DOMNodeRemovedFromDocument", stop);

  async function load() {
    let data;
    try {
      data = await api.getClientPatches(client.id);
    } catch (e) {
      bodyEl.innerHTML = `<div style="color:var(--danger);font-size:12px;padding:6px">${esc(e.message)}</div>`;
      return;
    }
    const patches = data.patches || [];
    const online = data.client?.online;
    const sec = patches.filter((p) => p.level === "security").length;

    bodyEl.innerHTML = `
      <div style="display:flex;flex-direction:column;height:100%;gap:6px">
        <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
          <span style="font-size:18px;font-weight:600">${patches.length}</span>
          <span style="font-size:11px;color:var(--subtext)">${t("patch_open")}</span>
          ${sec ? `<span style="font-size:10px;padding:1px 6px;border-radius:99px;
            background:#ff4d6d22;color:#ff4d6d;border:1px solid #ff4d6d55">
            ${sec} ${t("patch_col_security")}</span>` : ""}
          <span style="flex:1"></span>
          <button class="btn btn-sm" data-scan 
            ${online ? "" : "disabled"} title="${esc(t("patch_scan_btn"))}">🔍</button>
          <button class="btn btn-sm" data-all 
            ${online && patches.length ? "" : "disabled"}>${t("patch_install_all")}</button>
        </div>
        <div data-status style="font-size:10px;color:var(--subtext);min-height:12px"></div>
        <div data-list style="flex:1;overflow:auto;display:flex;flex-direction:column;gap:3px">
          ${patches.length ? patches.map((p) => `
            <div style="display:flex;align-items:center;gap:6px;padding:3px 5px;
                 border:1px solid var(--border);border-radius:6px;background:var(--panel-2)">
              <span style="width:6px;height:6px;border-radius:99px;flex:none;
                background:${PATCH_LEVEL_COLORS[p.level] || PATCH_LEVEL_COLORS.other}"></span>
              <div style="flex:1;min-width:0">
                <div style="font-size:11px;overflow:hidden;text-overflow:ellipsis;
                     white-space:nowrap" title="${esc(p.name)}">${esc(p.name)}</div>
                <div style="font-size:9px;color:var(--subtext)">
                  ${esc(p.current_version || "—")} → ${esc(p.available_version || "?")}</div>
              </div>
              <button class="btn btn-sm" data-one="${esc(p.id)}" data-uid="${esc(p.uid)}"
                data-src="${esc(p.source)}" data-name="${esc(p.name)}"
                style="flex:none"
                ${online ? "" : "disabled"}>⬆</button>
            </div>`).join("")
            : `<div style="font-size:11px;color:var(--subtext);padding:4px">${
                 data.client?.patch_last_scan ? t("patch_none_open") : t("patch_never_scanned")}</div>`}
        </div>
      </div>`;

    const statusEl = bodyEl.querySelector("[data-status]");
    const setStatus = (txt) => { if (statusEl) statusEl.textContent = txt || ""; };

    bodyEl.querySelector("[data-scan]")?.addEventListener("click", async () => {
      if (busy) return;
      busy = true;
      setStatus(t("patch_scanning"));
      try {
        const r = await api.scanPatches(client.id);
        if (r.hint) window.notify?.(r.hint, "warn", 8000);
      } catch (e) {
        busy = false; setStatus(""); return window.notify?.(e.message, "error");
      }
      poll();
    });

    const install = async (items) => {
      if (busy || !items.length) return;
      busy = true;
      setStatus(t("patch_installing", { what: items.length }));
      try {
        const r = await api.applyPatches(client.id, items);
        window.notify?.(t("patch_result", {
          ok: (r.installed || []).length, failed: (r.failed || []).length }),
          (r.failed || []).length ? "warn" : "success");
      } catch (e) {
        window.notify?.(e.message, "error");
      }
      busy = false; setStatus(""); load();
    };

    bodyEl.querySelector("[data-all]")?.addEventListener("click", () =>
      install(patches.map((p) => ({ uid: p.uid, source: p.source, name: p.name }))));
    bodyEl.querySelectorAll("[data-one]").forEach((b) =>
      b.addEventListener("click", () => install([{
        uid: b.dataset.uid, source: b.dataset.src, name: b.dataset.name }])));

    function poll() {
      const started = Date.now();
      const tick = async () => {
        let job = null;
        try { job = await api.getPatchJob(client.id); } catch {}
        // Only stop when the backend KNOWS the job. An empty
        // response means "not registered yet", not "already finished".
        if (job && job.known && !job.running) {
          busy = false; setStatus("");
          if (job.error) window.notify?.(job.error, "error", 9000);
          return load();
        }
        if (job && job.running) {
          setStatus(job.detail ? `${job.phase} - ${job.detail}` : (job.phase || ""));
        }
        if (Date.now() - started > 1800000) { busy = false; setStatus(""); return load(); }
        pollTimer = setTimeout(tick, 2500);
      };
      pollTimer = setTimeout(tick, 600);
    }

    // If a job was already running when opening, follow it directly.
    api.getPatchJob(client.id).then((job) => {
      if (job && job.running) { busy = true; setStatus(t("patch_scanning")); poll(); }
    }).catch(() => {});
  }

  load();
}
