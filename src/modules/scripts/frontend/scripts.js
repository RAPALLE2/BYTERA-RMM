// apps/scripts.js
// ---------------
// Manages saved scripts: name + command (multi-line possible, e.g.
// "apt update && apt upgrade -y") + target OS (windows/linux/any).
// A script can also be run directly on a client from here.

import { state } from "/js/app/state.js";
import { api } from "/js/app/api.js";
import { esc, uiConfirm } from "/js/app/utils.js";
import { t } from "/js/app/i18n.js";
import { icon } from "./icons.js";

export function renderScripts(body, win) {
  function draw() {
    body.innerHTML = `
      <div class="settings-section">
        <h3>New script</h3>
        <div class="form-row">
          <label>Name</label>
          <input type="text" id="sc-name" placeholder="e.g. update" />
        </div>
        <div class="form-row">
          <label>${t("sc_command")}</label>
          <textarea id="sc-cmd" style="min-height:70px;font-family:monospace" placeholder="apt update && apt upgrade -y"></textarea>
        </div>
        <div class="form-row">
          <label>Operating system</label>
          <select id="sc-os">
            <option value="any">All</option>
            <option value="linux">Linux</option>
            <option value="windows">Windows</option>
          </select>
        </div>
        <div class="form-row">
          <label>Folder (optional)</label>
          <input type="text" id="sc-folder" list="sc-folder-list" placeholder="e.g. Maintenance" />
          <datalist id="sc-folder-list"></datalist>
        </div>
        <div id="sc-error" class="form-error hidden"></div>
        <button class="btn-primary" id="sc-add" style="margin-top:4px">+ Save script</button>

        <h3 style="margin-top:24px">Saved scripts</h3>
        <div id="sc-list"></div>
      </div>
    `;

    body.querySelector("#sc-add").addEventListener("click", async () => {
      const name = body.querySelector("#sc-name").value.trim();
      const command = body.querySelector("#sc-cmd").value.trim();
      const os = body.querySelector("#sc-os").value;
      const folder = body.querySelector("#sc-folder").value.trim();
      const err = body.querySelector("#sc-error");
      err.classList.add("hidden");
      if (!name || !command) { err.textContent = t("u_name_und_befehl_erforderlich"); err.classList.remove("hidden"); return; }
      try {
        await api.createScript({ name, command, os, folder });
        draw();
      } catch (e) { err.textContent = e.message; err.classList.remove("hidden"); }
    });

    loadList();
  }

  async function loadList() {
    const listEl = body.querySelector("#sc-list");
    try {
      const scripts = await api.getScripts();
      if (!scripts.length) {
        listEl.innerHTML = `<div style="color:var(--subtext);font-size:13px">${t("sc_none")}</div>`;
        return;
      }
      // Online clients for the "run on..." selector
      const onlineClients = state.clients.filter((c) => c.online);
      const clientOptions = onlineClients.map((c) => `<option value="${c.id}">${esc(c.hostname)}</option>`).join("");

      // Collect existing folders (for the form datalist + move selector)
      const folders = [...new Set(scripts.map((s) => (s.folder || "").trim()).filter(Boolean))].sort((a, b) => a.localeCompare(b));
      const dl = body.querySelector("#sc-folder-list");
      if (dl) dl.innerHTML = folders.map((f) => `<option value="${esc(f)}"></option>`).join("");

      const renderScript = (s) => `
        <div class="panel" style="margin-bottom:10px">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
            <strong>${esc(s.name)}</strong>
            <span style="font-size:11px;color:var(--subtext);text-transform:uppercase">${esc(s.os)}</span>
          </div>
          <pre style="background:var(--panel-2);padding:8px;border-radius:6px;margin:8px 0;font-size:12px;white-space:pre-wrap;overflow-x:auto">${esc(s.command)}</pre>
          <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
            <select data-run-target="${s.id}">
              <option value="">${t("sc_pick_client")}</option>${clientOptions}
            </select>
            <button class="action-btn" data-run="${s.id}">${icon("play", 13)} ${t("src_run")}</button>
            <select data-move="${s.id}" title="Move to folder">
              <option value="">${(s.folder || "").trim() ? esc(s.folder) : "No folder"}</option>
              ${(s.folder || "").trim() ? `<option value="__root__">(No folder)</option>` : ""}
              ${folders.filter((f) => f !== (s.folder || "").trim()).map((f) => `<option value="${esc(f)}">${esc(f)}</option>`).join("")}
              <option value="__new__">+ ${t("exp_new_folder")}…</option>
            </select>
            <button class="btn btn-danger" data-del="${s.id}">${t("delete")}</button>
            <span data-result="${s.id}" style="font-size:12px;color:var(--subtext)"></span>
          </div>
        </div>`;

      // Show grouped by folder: first all folders (collapsible), then scripts
      // without a folder.
      const byFolder = new Map();
      for (const s of scripts) {
        const key = (s.folder || "").trim();
        if (!byFolder.has(key)) byFolder.set(key, []);
        byFolder.get(key).push(s);
      }
      let html = "";
      for (const f of folders) {
        html += `
          <details open style="margin-bottom:10px">
            <summary style="cursor:pointer;font-weight:600;padding:6px 4px;display:flex;align-items:center;gap:6px">${icon("folder", 14)} ${esc(f)} <span style="color:var(--subtext);font-weight:normal;font-size:12px">(${byFolder.get(f).length})</span></summary>
            <div style="margin:8px 0 0 14px;border-left:2px solid var(--border);padding-left:12px">
              ${byFolder.get(f).map(renderScript).join("")}
            </div>
          </details>`;
      }
      const rootScripts = byFolder.get("") || [];
      if (rootScripts.length) {
        html += folders.length
          ? `<div style="font-weight:600;padding:6px 4px;display:flex;align-items:center;gap:6px">${icon("file", 14)} No folder <span style="color:var(--subtext);font-weight:normal;font-size:12px">(${rootScripts.length})</span></div>`
          : "";
        html += rootScripts.map(renderScript).join("");
      }
      listEl.innerHTML = html;

      // Move to folder
      listEl.querySelectorAll("[data-move]").forEach((sel) =>
        sel.addEventListener("change", async () => {
          const s = scripts.find((x) => x.id === sel.dataset.move);
          if (!s || !sel.value) return;
          let folder = sel.value;
          if (folder === "__root__") folder = "";
          if (folder === "__new__") {
            const { uiPrompt } = await import("/js/app/utils.js");
            folder = await uiPrompt("New folder", { placeholder: "Folder name" });
            if (!folder || !folder.trim()) { sel.value = ""; return; }
            folder = folder.trim();
          }
          try {
            await api.updateScript(s.id, { name: s.name, command: s.command, os: s.os, folder });
            loadList();
          } catch (e) { sel.value = ""; console.warn(e); }
        })
      );

      // Run
      listEl.querySelectorAll("[data-run]").forEach((btn) =>
        btn.addEventListener("click", async () => {
          const scriptId = btn.dataset.run;
          const script = scripts.find((s) => s.id === scriptId);
          const targetSel = listEl.querySelector(`[data-run-target="${scriptId}"]`);
          const resultEl = listEl.querySelector(`[data-result="${scriptId}"]`);
          const clientId = targetSel.value;
          if (!clientId) { resultEl.textContent = t("u_bitte_client_wahlen"); return; }
          resultEl.textContent = t("u_lauft_3");
          try {
            const res = await api.execOnClient(clientId, script.command);
            const ok = res.code === 0;
            const out = [res.stdout, res.stderr].filter((x) => x && x.trim()).join("\n").trim();
            resultEl.innerHTML = `<span style="color:${ok ? "var(--accent)" : "var(--danger)"};display:inline-flex;align-items:center;gap:4px">${ok ? icon("check", 13) : icon("cross", 13)} Exit ${res.code}</span>`;
            // Show the client's output directly under the script.
            let pre = listEl.querySelector(`[data-output="${scriptId}"]`);
            if (!pre) {
              pre = document.createElement("pre");
              pre.dataset.output = scriptId;
              pre.style.cssText = "background:var(--panel-2);border:1px solid var(--border);padding:8px;" +
                "border-radius:6px;margin:8px 0 0;font-size:12px;white-space:pre-wrap;overflow:auto;max-height:240px";
              resultEl.closest(".panel")?.appendChild(pre);
            }
            pre.textContent = out || t("u_keine_ausgabe");
          } catch (e) {
            resultEl.innerHTML = `<span style="color:var(--danger)">${esc(e.message)}</span>`;
          }
        })
      );

      // Delete
      listEl.querySelectorAll("[data-del]").forEach((btn) =>
        btn.addEventListener("click", async () => {
          if (!(await uiConfirm(t("u_skript_loschen"), { okText: t("delete"), danger: true }))) return;
          await api.deleteScript(btn.dataset.del);
          loadList();
        })
      );
    } catch (e) {
      listEl.innerHTML = `<div style="color:var(--danger)">${esc(e.message)}</div>`;
    }
  }

  draw();
}
