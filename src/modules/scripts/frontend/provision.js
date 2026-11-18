// provision.js (module "scripts") - client provisioning with presets
// ----------------------------------------------------------------
// Preset list on the left, an editor with all options on the right (built
// dynamically from the backend catalog). Presets are saved and rolled out to
// many clients (one backend job, live progress). Like an unattended.xml, but as
// an external script editable in the UI.
import { state } from "/js/app/state.js";
import { moduleRequest } from "/js/app/modules.js";
import { watchJobs } from "/js/app/bgjobs.js";
import { esc, uiConfirm } from "/js/app/utils.js";
import { icon } from "./icons.js";

const req = (p, o = {}) => moduleRequest("/api/scripts/provision" + p, o);
const toast = (m, t = "success", ms = 1900) => window.notify?.(m, t, ms);

export function renderProvision(body, win) {
  let CAT = null, DEF = null, presets = [], sel = null, cfg = null, selOs = "windows", dirty = false;

  body.innerHTML = `<div style="display:flex;height:100%;min-height:0;font-size:13px">
    <aside style="width:250px;flex:0 0 250px;border-right:1px solid var(--border);display:flex;flex-direction:column;min-height:0">
      <div style="padding:12px 12px 8px"><b style="font-size:15px;display:flex;align-items:center;gap:6px">${icon("kit", 16)} Provisioning</b>
        <div style="font-size:11.5px;color:var(--subtext);margin-top:2px">Presets for new clients</div></div>
      <div style="padding:0 10px 8px;display:flex;gap:6px">
        <button class="btn-primary" data-new-win style="flex:1;font-size:12px">+ Windows</button>
        <button class="btn-primary" data-new-lin style="flex:1;font-size:12px">+ Linux</button></div>
      <div data-list style="flex:1;overflow:auto;padding:0 8px 10px"></div></div>
    <main data-main style="flex:1;overflow:auto;min-height:0;position:relative"></main></div>`;
  const listEl = body.querySelector("[data-list]"), main = body.querySelector("[data-main]");

  const loadCat = async () => {
    const d = await req("/catalog"); CAT = d.catalog; DEF = d.defaults;
  };
  const loadPresets = async () => { presets = (await req("/presets")).presets; drawList(); };

  const drawList = () => {
    const grp = (os) => presets.filter((p) => p.os === os);
    const sec = (os, label, icon) => {
      const ps = grp(os);
      return `<div style="font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--subtext);padding:10px 8px 4px">${icon} ${label}</div>`
        + (ps.map((p) => `<a data-p="${esc(p.id)}" style="display:block;padding:8px 10px;border-radius:9px;cursor:pointer;${sel === p.id ? "background:color-mix(in srgb,var(--accent) 16%,transparent);font-weight:600" : ""}">
            <div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(p.name)}</div>
            ${p.description ? `<div style="font-size:11px;color:var(--subtext);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(p.description)}</div>` : ""}</a>`).join("")
          || `<div style="padding:4px 10px;color:var(--subtext);font-size:12px">—</div>`);
    };
    listEl.innerHTML = sec("windows", "Windows", icon("windows", 13)) + sec("linux", "Linux", icon("linux", 13));
    listEl.querySelectorAll("[data-p]").forEach((a) => a.addEventListener("click", () => open(a.dataset.p)));
  };

  const confirmLeave = async () => !dirty || await uiConfirm("Discard unsaved changes?");

  const open = async (pid) => {
    if (!(await confirmLeave())) return;
    const p = presets.find((x) => x.id === pid); if (!p) return;
    sel = pid; selOs = p.os; cfg = structuredClone(p.config); dirty = false;
    drawList(); drawEditor(p.name, p.description);
  };
  const newPreset = async (os) => {
    if (!(await confirmLeave())) return;
    sel = null; selOs = os; cfg = structuredClone(DEF[os]); dirty = false;
    drawList(); drawEditor("", "");
  };

  body.querySelector("[data-new-win]").addEventListener("click", () => newPreset("windows"));
  body.querySelector("[data-new-lin]").addEventListener("click", () => newPreset("linux"));

  // ---- small building blocks
  const setDirty = () => { dirty = true; };
  const sectionBox = (title, hint, inner) => `<section style="border:1px solid var(--border);border-radius:12px;margin:0 0 14px;overflow:hidden">
    <div style="padding:10px 14px;background:var(--panel-2,var(--panel));font-weight:700">${esc(title)}${hint ? `<span style="font-weight:400;color:var(--subtext);font-size:11.5px"> — ${esc(hint)}</span>` : ""}</div>
    <div style="padding:12px 14px">${inner}</div></section>`;
  const checks = (items, chosen, name, cols = 2) => `<div style="display:grid;grid-template-columns:repeat(${cols},1fr);gap:4px 14px">`
    + items.map((it) => `<label style="display:flex;gap:7px;align-items:flex-start;font-size:12.5px;padding:2px 0">
        <input type="checkbox" data-${name}="${esc(it.id)}" ${chosen.includes(it.id) ? "checked" : ""} style="margin-top:2px">
        <span>${esc(it.label)}</span></label>`).join("") + `</div>`;

  function wireChecks(scope, name, arr) {
    scope.querySelectorAll(`[data-${name}]`).forEach((c) => c.addEventListener("change", () => {
      const id = c.getAttribute(`data-${name}`);
      const i = arr.indexOf(id);
      if (c.checked && i < 0) arr.push(id); else if (!c.checked && i >= 0) arr.splice(i, 1);
      setDirty();
    }));
  }

  const drawEditor = (name, desc) => {
    main.innerHTML = `<div style="position:sticky;top:0;z-index:5;background:var(--panel);border-bottom:1px solid var(--border);padding:12px 18px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <span style="display:flex">${selOs === "windows" ? icon("windows", 18) : icon("linux", 18)}</span>
        <input data-name value="${esc(name)}" placeholder="Preset name" style="font-size:15px;font-weight:600;min-width:200px;flex:1">
        <input data-desc value="${esc(desc)}" placeholder="Description (optional)" style="flex:2;min-width:160px">
        <button class="btn" data-preview>${icon("eye", 14)} Preview</button>
        <button class="btn" data-download ${sel ? "" : "disabled"} title="${sel ? "Download script" : "Save first"}">${icon("download", 14)} Script</button>
        <button class="btn-primary" data-save>${icon("save", 14)} Save</button>
        <button class="btn" data-run ${sel ? "" : "disabled"} title="${sel ? "Roll out to clients" : "Save first"}">${icon("rocket", 14)} Roll out</button>
        ${sel ? `<button class="btn" data-del style="color:var(--danger)">${icon("trash", 14)}</button>` : ""}
      </div>
      <div data-form style="padding:16px 18px;max-width:1000px"></div>`;
    const form = main.querySelector("[data-form]");
    selOs === "windows" ? winForm(form) : linForm(form);

    main.querySelector("[data-name]").addEventListener("input", setDirty);
    main.querySelector("[data-desc]").addEventListener("input", setDirty);
    main.querySelector("[data-preview]").addEventListener("click", preview);
    main.querySelector("[data-save]").addEventListener("click", save);
    main.querySelector("[data-download]")?.addEventListener("click", () => {
      if (sel) window.open(`/api/scripts/provision/presets/${sel}/file`, "_blank");
    });
    main.querySelector("[data-run]")?.addEventListener("click", runDialog);
    main.querySelector("[data-del]")?.addEventListener("click", del);
  };

  // ============================================================ WINDOWS
  function winForm(form) {
    const w = CAT.windows, g = cfg.general;
    const appGroups = [...new Set(w.apps.map((a) => a.group))];
    form.innerHTML =
      sectionBox("General", "", `<div style="display:grid;grid-template-columns:150px 1fr;gap:8px 12px;align-items:center;max-width:620px">
        <label>Computer name</label><input data-g="computer_name" value="${esc(g.computer_name)}" placeholder="empty = unchanged · {SERIAL} = serial number">
        <label>Time zone</label><input data-g="timezone" value="${esc(g.timezone)}" placeholder="e.g. W. Europe Standard Time">
        <label>Reboot afterwards</label><select data-g="reboot">
          <option value="needed"${g.reboot === "needed" ? " selected" : ""}>only if needed</option>
          <option value="always"${g.reboot === "always" ? " selected" : ""}>always</option>
          <option value="never"${g.reboot === "never" ? " selected" : ""}>never</option></select>
        <label></label><label style="display:flex;gap:7px;font-size:12.5px"><input type="checkbox" data-g="restore_point"${g.restore_point ? " checked" : ""}> Create a restore point first</label></div>`)
      + sectionBox("Remove preinstalled apps (debloat)", "built-in & sponsored apps", checks(w.appx, cfg.debloat.appx, "appx")
        + `<div style="margin-top:10px;display:grid;grid-template-columns:150px 1fr;gap:8px 12px;align-items:start">
          <label style="padding-top:5px">Custom (Appx name)</label><textarea data-custom="appx_custom" rows="2" placeholder="e.g. Microsoft.Something – one per line" style="font-family:monospace">${esc((cfg.debloat.appx_custom || []).join("\n"))}</textarea></div>
          <label style="display:flex;gap:7px;font-size:12.5px;margin-top:8px"><input type="checkbox" data-debloat="onedrive"${cfg.debloat.onedrive ? " checked" : ""}> Uninstall OneDrive</label>`)
      + sectionBox("Privacy & UI", "registry tweaks for all users + the default profile", checks(w.tweaks, cfg.debloat.tweaks, "tweaks"))
      + sectionBox("Power & efficiency", "", `<div style="display:grid;grid-template-columns:150px 1fr;gap:8px 12px;align-items:center;max-width:620px;margin-bottom:10px">
        <label>Power plan</label><select data-eff="power_plan">
          <option value=""${cfg.efficiency.power_plan === "" ? " selected" : ""}>unchanged</option>
          <option value="balanced"${cfg.efficiency.power_plan === "balanced" ? " selected" : ""}>Balanced</option>
          <option value="high"${cfg.efficiency.power_plan === "high" ? " selected" : ""}>High performance</option>
          <option value="ultimate"${cfg.efficiency.power_plan === "ultimate" ? " selected" : ""}>Ultimate performance</option></select>
        <label>Turn off display (min, AC)</label><input type="number" min="0" max="600" data-effn="monitor_ac" value="${cfg.efficiency.monitor_ac ?? ""}" placeholder="0 = never">
        <label>Sleep (min, AC)</label><input type="number" min="0" max="600" data-effn="standby_ac" value="${cfg.efficiency.standby_ac ?? ""}" placeholder="0 = never"></div>`
        + checks(w.efficiency, cfg.efficiency.options, "effopt"))
      + sectionBox("Install standard apps", "winget / Chocolatey / Ninite",
        `<div style="display:flex;gap:16px;align-items:center;margin-bottom:10px;flex-wrap:wrap">
          <label style="display:flex;gap:6px;align-items:center">Source <select data-apps="engine">
            <option value="winget"${cfg.apps.engine === "winget" ? " selected" : ""}>winget (included in Windows)</option>
            <option value="choco"${cfg.apps.engine === "choco" ? " selected" : ""}>Chocolatey</option>
            <option value="ninite"${cfg.apps.engine === "ninite" ? " selected" : ""}>Ninite</option></select></label>
          <label style="display:flex;gap:6px;align-items:center;font-size:12.5px"><input type="checkbox" data-apps-upg${cfg.apps.upgrade_all ? " checked" : ""}> upgrade all programs at the end</label></div>`
        + appGroups.map((grp) => `<div style="font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--subtext);margin:8px 0 2px">${esc(grp)}</div>`
            + checks(w.apps.filter((a) => a.group === grp).map((a) => ({ id: a.id, label: a.label })), cfg.apps.ids, "app", 3)).join("")
        + `<div style="display:grid;grid-template-columns:150px 1fr;gap:8px 12px;margin-top:10px;align-items:start">
          <label style="padding-top:5px">Custom packages</label><textarea data-custom="apps_custom" rows="2" placeholder="winget ID / Choco package / Ninite slug – one per line" style="font-family:monospace">${esc((cfg.apps.custom || []).join("\n"))}</textarea></div>`)
      + sectionBox("Windows updates", "", `<label style="display:flex;gap:7px;font-size:12.5px"><input type="checkbox" data-upd="install"${cfg.updates.install ? " checked" : ""}> Check for and install updates</label>
        <label style="display:flex;gap:7px;font-size:12.5px;margin-top:4px"><input type="checkbox" data-upd="drivers"${cfg.updates.drivers ? " checked" : ""}> also driver updates</label>`)
      + sectionBox("Win11Debloat (Raphire)", "downloads debloat.raphi.re and applies it", `<label style="display:flex;gap:7px;font-size:12.5px;margin-bottom:8px"><input type="checkbox" data-w11d-en${cfg.w11d.enabled ? " checked" : ""}> Also run Win11Debloat</label>
        <div data-w11d style="${cfg.w11d.enabled ? "" : "opacity:.5;pointer-events:none"}">
          <div style="display:flex;gap:6px;align-items:center;margin-bottom:8px;font-size:12.5px">Apply to <select data-w11d-target>
            <option value="default_and_users"${cfg.w11d.target === "default_and_users" ? " selected" : ""}>default profile + existing users</option>
            <option value="default"${cfg.w11d.target === "default" ? " selected" : ""}>default profile only (new users)</option>
            <option value="users"${cfg.w11d.target === "users" ? " selected" : ""}>existing users only</option></select></div>
          ${checks(w.w11d, cfg.w11d.params, "w11d")}
          <div style="display:grid;grid-template-columns:150px 1fr;gap:8px 12px;margin-top:8px;align-items:start">
            <label style="padding-top:5px">Extra switches</label><textarea data-custom="w11d_extra" rows="1" placeholder="e.g. DisableBingSearch – without a dash, one per line" style="font-family:monospace">${esc((cfg.w11d.extra || []).join("\n"))}</textarea></div></div>`)
      + sectionBox("Optimizer (hellzerg)", "downloads the latest Optimizer.exe and applies a template silently", `<label style="display:flex;gap:7px;font-size:12.5px;margin-bottom:8px"><input type="checkbox" data-opt-en${cfg.optimizer.enabled ? " checked" : ""}> Run Optimizer with a template</label>
        <div data-opt style="${cfg.optimizer.enabled ? "" : "opacity:.5;pointer-events:none"}">
          <textarea data-opt-tpl rows="8" style="width:100%;font-family:monospace;font-size:12px" spellcheck="false">${esc(cfg.optimizer.template || "")}</textarea>
          <div style="font-size:11px;color:var(--subtext);margin-top:4px">JSON as in the Optimizer automation docs. WindowsVersion must match (10/11). Note: Optimizer is discontinued (successor: OptimizerNXT).</div></div>`)
      + sectionBox("Windows features", "optional components (DISM feature names)", `<div style="display:grid;grid-template-columns:150px 1fr;gap:8px 12px;align-items:start">
        <label style="padding-top:5px">Enable</label><textarea data-feat="enable" rows="2" placeholder="e.g. Microsoft-Windows-Subsystem-Linux – one per line" style="font-family:monospace">${esc((cfg.features.enable || []).join("\n"))}</textarea>
        <label style="padding-top:5px">Disable</label><textarea data-feat="disable" rows="2" placeholder="e.g. WindowsMediaPlayer – one per line" style="font-family:monospace">${esc((cfg.features.disable || []).join("\n"))}</textarea></div>`)
      + sectionBox("Custom commands", "PowerShell", `<label style="font-size:12px;color:var(--subtext)">Before</label>
        <textarea data-cust="pre" rows="3" style="width:100%;font-family:monospace;margin-bottom:8px">${esc(cfg.custom.pre || "")}</textarea>
        <label style="font-size:12px;color:var(--subtext)">After</label>
        <textarea data-cust="post" rows="3" style="width:100%;font-family:monospace">${esc(cfg.custom.post || "")}</textarea>`);
    wireWin(form);
  }

  function wireWin(form) {
    const on = (sattr, fn) => form.querySelectorAll(`[${sattr}]`).forEach((el) => el.addEventListener(el.type === "checkbox" || el.tagName === "SELECT" ? "change" : "input", () => { fn(el); setDirty(); }));
    on("data-g", (el) => { const k = el.dataset.g; cfg.general[k] = el.type === "checkbox" ? el.checked : el.value; });
    on("data-eff", (el) => { cfg.efficiency[el.dataset.eff] = el.value; });
    on("data-effn", (el) => { cfg.efficiency[el.dataset.effn] = el.value === "" ? null : parseInt(el.value, 10); });
    on("data-apps", (el) => { cfg.apps[el.dataset.apps] = el.value; });
    on("data-apps-upg", (el) => { cfg.apps.upgrade_all = el.checked; });
    on("data-debloat", (el) => { cfg.debloat[el.dataset.debloat] = el.checked; });
    on("data-upd", (el) => { cfg.updates[el.dataset.upd] = el.checked; });
    on("data-w11d-target", (el) => { cfg.w11d.target = el.value; });
    on("data-w11d-en", (el) => { cfg.w11d.enabled = el.checked; const b = form.querySelector("[data-w11d]"); b.style.opacity = el.checked ? "" : ".5"; b.style.pointerEvents = el.checked ? "" : "none"; });
    on("data-opt-en", (el) => { cfg.optimizer.enabled = el.checked; const b = form.querySelector("[data-opt]"); b.style.opacity = el.checked ? "" : ".5"; b.style.pointerEvents = el.checked ? "" : "none"; });
    on("data-opt-tpl", (el) => { cfg.optimizer.template = el.value; });
    const tx = (sel2, set) => form.querySelectorAll(sel2).forEach((el) => el.addEventListener("input", () => { set(el.value.split("\n").map((s) => s.trim()).filter(Boolean)); setDirty(); }));
    tx('[data-custom="appx_custom"]', (v) => cfg.debloat.appx_custom = v);
    tx('[data-custom="apps_custom"]', (v) => cfg.apps.custom = v);
    tx('[data-custom="w11d_extra"]', (v) => cfg.w11d.extra = v);
    form.querySelectorAll("[data-feat]").forEach((el) => el.addEventListener("input", () => { cfg.features[el.dataset.feat] = el.value.split("\n").map((s) => s.trim()).filter(Boolean); setDirty(); }));
    form.querySelectorAll("[data-cust]").forEach((el) => el.addEventListener("input", () => { cfg.custom[el.dataset.cust] = el.value; setDirty(); }));
    wireChecks(form, "appx", cfg.debloat.appx);
    wireChecks(form, "tweaks", cfg.debloat.tweaks);
    wireChecks(form, "effopt", cfg.efficiency.options);
    wireChecks(form, "app", cfg.apps.ids);
    wireChecks(form, "w11d", cfg.w11d.params);
  }

  // ============================================================ LINUX
  function linForm(form) {
    const l = CAT.linux, g = cfg.general;
    const pkgGroups = [...new Set(l.packages.map((p) => p.group))];
    form.innerHTML =
      sectionBox("General", "", `<div style="display:grid;grid-template-columns:150px 1fr;gap:8px 12px;align-items:center;max-width:620px">
        <label>Hostname</label><input data-g="hostname" value="${esc(g.hostname)}" placeholder="empty = unchanged · {SERIAL}">
        <label>Time zone</label><input data-g="timezone" value="${esc(g.timezone)}" placeholder="Europe/Berlin">
        <label>Reboot afterwards</label><select data-g="reboot">
          <option value="needed"${g.reboot === "needed" ? " selected" : ""}>only if needed</option>
          <option value="always"${g.reboot === "always" ? " selected" : ""}>always</option>
          <option value="never"${g.reboot === "never" ? " selected" : ""}>never</option></select></div>`)
      + sectionBox("Actions", "apt / dnf / zypper / pacman / apk are detected automatically", checks(l.options, cfg.options, "opt", 1))
      + sectionBox("Install packages", "names are translated per distribution",
        pkgGroups.map((grp) => `<div style="font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--subtext);margin:8px 0 2px">${esc(grp)}</div>`
          + checks(l.packages.filter((p) => p.group === grp).map((p) => ({ id: p.id, label: p.label })), cfg.packages, "pkg", 3)).join("")
        + `<div style="display:grid;grid-template-columns:150px 1fr;gap:8px 12px;margin-top:10px;align-items:start">
          <label style="padding-top:5px">Custom packages</label><textarea data-custom="packages_custom" rows="2" placeholder="exact package names – one per line" style="font-family:monospace">${esc((cfg.packages_custom || []).join("\n"))}</textarea></div>`)
      + sectionBox("Custom commands", "Bash", `<label style="font-size:12px;color:var(--subtext)">Before</label>
        <textarea data-cust="pre" rows="3" style="width:100%;font-family:monospace;margin-bottom:8px">${esc(cfg.custom.pre || "")}</textarea>
        <label style="font-size:12px;color:var(--subtext)">After</label>
        <textarea data-cust="post" rows="3" style="width:100%;font-family:monospace">${esc(cfg.custom.post || "")}</textarea>`);
    form.querySelectorAll("[data-g]").forEach((el) => el.addEventListener(el.tagName === "SELECT" ? "change" : "input", () => { cfg.general[el.dataset.g] = el.value; setDirty(); }));
    form.querySelector('[data-custom="packages_custom"]').addEventListener("input", (e) => { cfg.packages_custom = e.target.value.split("\n").map((s) => s.trim()).filter(Boolean); setDirty(); });
    form.querySelectorAll("[data-cust]").forEach((el) => el.addEventListener("input", () => { cfg.custom[el.dataset.cust] = el.value; setDirty(); }));
    wireChecks(form, "opt", cfg.options);
    wireChecks(form, "pkg", cfg.packages);
  }

  // ============================================================ Actions
  const body2 = () => ({ name: main.querySelector("[data-name]").value.trim(),
                         desc: main.querySelector("[data-desc]").value.trim() });

  async function save() {
    const { name, desc } = body2();
    if (!name) { toast("Please enter a name", "error"); return; }
    try {
      const saved = sel
        ? await req(`/presets/${sel}`, { method: "PUT", body: JSON.stringify({ name, os: selOs, description: desc, config: cfg }) })
        : await req("/presets", { method: "POST", body: JSON.stringify({ name, os: selOs, description: desc, config: cfg }) });
      sel = saved.id; cfg = saved.config; dirty = false;
      await loadPresets(); drawEditor(saved.name, saved.description); drawList();
      toast("Preset saved");
    } catch (e) { toast(e.message, "error", 6000); }
  }

  async function preview() {
    try {
      const { script } = await req("/render", { method: "POST", body: JSON.stringify({ os: selOs, config: cfg }) });
      overlay(`<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
          <b>Script preview (${selOs === "windows" ? "PowerShell" : "Bash"})</b>
          <button class="btn" data-copy>Copy</button></div>
        <pre style="background:var(--panel-2,#111);border:1px solid var(--border);border-radius:8px;padding:12px;overflow:auto;max-height:70vh;font-size:12px;white-space:pre-wrap">${esc(script)}</pre>`, 900,
        (ov) => ov.querySelector("[data-copy]").addEventListener("click", () => { navigator.clipboard?.writeText(script); toast("Copied"); }));
    } catch (e) { toast(e.message, "error", 6000); }
  }

  async function del() {
    if (!(await uiConfirm(`Delete preset "${body2().name}"?`))) return;
    try { await req(`/presets/${sel}`, { method: "DELETE" }); sel = null; cfg = null; dirty = false; await loadPresets(); main.innerHTML = emptyHint(); }
    catch (e) { toast(e.message, "error", 6000); }
  }

  function runDialog() {
    const clients = (state.clients || []).filter((c) => pvOs(c) === selOs);
    const online = clients.filter((c) => c.online);
    const ov = overlay(`<h3 style="margin:0 0 4px;display:flex;align-items:center;gap:6px">${icon("rocket", 16)} Roll out "${esc(body2().name)}"</h3>
      <div style="font-size:12px;color:var(--subtext);margin-bottom:10px">${selOs === "windows" ? "Windows" : "Linux"} clients only. ${online.length} online, ${clients.length - online.length} offline.</div>
      <div style="display:flex;gap:8px;margin-bottom:6px"><input data-q placeholder="Filter …" style="flex:1"><label style="display:flex;gap:5px;align-items:center;font-size:12.5px"><input type="checkbox" data-all> all</label></div>
      <div data-cl style="max-height:300px;overflow:auto;border:1px solid var(--border);border-radius:8px;padding:6px"></div>
      <label style="display:flex;gap:6px;align-items:center;font-size:12.5px;margin-top:8px"><input type="checkbox" data-queue checked> Queue offline clients (run when they next come online)</label>
      <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px"><button class="btn" data-x>Cancel</button><button class="btn-primary" data-go>Roll out</button></div>`, 560);
    const clEl = ov.querySelector("[data-cl]"), q = ov.querySelector("[data-q]");
    const paint = () => {
      const f = q.value.trim().toLowerCase();
      clEl.innerHTML = clients.filter((c) => !f || (c.hostname || "").toLowerCase().includes(f)).map((c) => `<label style="display:flex;gap:7px;align-items:center;padding:3px 2px;font-size:12.5px">
        <input type="checkbox" class="pv-c" value="${esc(c.id)}">
        <span style="width:7px;height:7px;border-radius:50%;background:${c.online ? "var(--online,#16a34a)" : "var(--subtext)"}"></span>
        ${esc(c.hostname || c.id)} <span style="color:var(--subtext)">${esc(c.platform || "")}${c.online ? "" : " · offline"}</span></label>`).join("")
        || `<div style="color:var(--subtext);padding:6px">No ${selOs} clients</div>`;
    };
    q.addEventListener("input", paint); paint();
    ov.querySelector("[data-all]").addEventListener("change", (e) => clEl.querySelectorAll(".pv-c").forEach((c) => { c.checked = e.target.checked; }));
    ov.querySelector("[data-go]").addEventListener("click", async () => {
      const ids = [...clEl.querySelectorAll(".pv-c:checked")].map((c) => c.value);
      if (!ids.length) { toast("No clients selected", "error"); return; }
      const queue = ov.querySelector("[data-queue]").checked;
      ov.remove();
      runProgress(ids, queue);
    });
  }

  function runProgress(ids, queue) {
    const b = {}; b[selOs === "windows" ? "preset_windows" : "preset_linux"] = sel;
    const ov = overlay(`<div style="display:flex;justify-content:space-between;align-items:center"><b style="display:flex;align-items:center;gap:6px">${icon("rocket", 15)} Rolling out …</b><button class="btn" data-x>Close</button></div>
      <div data-bar style="height:8px;background:var(--panel-2,#222);border-radius:5px;margin:10px 0;overflow:hidden"><div data-fill style="height:100%;width:0;background:var(--accent);transition:width .3s"></div></div>
      <div data-info style="font-size:12.5px;color:var(--subtext);margin-bottom:6px">started …</div>
      <pre data-log style="background:var(--panel-2,#111);border:1px solid var(--border);border-radius:8px;padding:10px;overflow:auto;height:300px;font-size:11.5px;margin:0"></pre>`, 760);
    const fill = ov.querySelector("[data-fill]"), info = ov.querySelector("[data-info]"), logEl = ov.querySelector("[data-log]");
    const onUpd = (job) => {
      if (!job) return;
      fill.style.width = `${Math.round(job.pct || 0)}%`;
      if (job.detail) info.textContent = job.detail;
      if (job.logs) { logEl.textContent = job.logs.join("\n"); logEl.scrollTop = logEl.scrollHeight; }
      if (job.status === "done") info.textContent = "Done. " + (job.detail || "");
      if (job.status === "error") info.textContent = "Error: " + (job.error || "");
    };
    moduleRequest("/api/scripts/provision/run", { method: "POST", body: JSON.stringify({ ...b, client_ids: ids, queue_offline: queue }), onJob: onUpd })
      .then((r) => { const ok = Object.values(r?.results || {}).filter((x) => x.ok).length; const q = (r?.queued || []).length; toast(`Done: ${ok} succeeded${q ? ", " + q + " queued" : ""}`, "success", 4500); info.textContent = "Done."; })
      .catch((e) => { toast(e.message, "error", 7000); info.textContent = "Error: " + e.message; });
  }

  // ---- Overlay helper
  function overlay(html, width = 620, after) {
    const ov = document.createElement("div");
    ov.style.cssText = "position:fixed;inset:0;z-index:9500;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;padding:14px";
    ov.innerHTML = `<div style="background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:16px 18px;width:${width}px;max-width:100%;max-height:calc(100dvh - 28px);overflow:auto;font-size:13px">${html}</div>`;
    const close = () => { ov.dispatchEvent(new Event("remove")); ov.remove(); };
    document.body.appendChild(ov);
    ov.addEventListener("click", (e) => { if (e.target === ov) close(); });
    ov.querySelector("[data-x]")?.addEventListener("click", close);
    after?.(ov);
    return ov;
  }
  const pvOs = (c) => (String(c.platform || "").toLowerCase().includes("win") ? "windows" : "linux");
  const emptyHint = () => `<div style="padding:40px;text-align:center;color:var(--subtext)">Pick a preset on the left or create a new one.</div>`;

  main.innerHTML = `<div style="padding:40px;text-align:center;color:var(--subtext)">Loading …</div>`;
  (async () => {
    try { await loadCat(); await loadPresets(); main.innerHTML = emptyHint(); }
    catch (e) { main.innerHTML = `<div style="padding:30px;color:var(--danger)">${esc(e.message)}</div>`; }
  })();
}
