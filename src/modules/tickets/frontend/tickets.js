// apps/tickets.js
// ---------------
// Ticket system: list + detail. Functions (depending on permission):
//   ticket_read    - view tickets (only admins/creators/assignees, the server filters)
//   ticket_create  - create new tickets
//   ticket_edit    - change title/description/priority/due date/clients
//   ticket_comment - comment + attach files/screenshots
//   ticket_assign  - assign users/groups
//   ticket_resolve - change status / mark as resolved
//   ticket_delete  - delete tickets
import { api } from "/js/app/api.js";
import { state, isAdmin, hasGlobalPerm } from "/js/app/state.js";
import { esc, uiConfirm } from "/js/app/utils.js";
import { subjectPickerHtml, readSubjectPicker, initSubjectPicker, splitGroups } from "/js/app/subjectpicker.js";
import { RMM_TARGET, RMM_LABEL, targetLabel } from "/js/app/tickettarget.js";
// t() under an alias: in this file "t" is already used as a local
// variable name (tenant/target/trigger/token etc.).
import { t as tr } from "/js/app/i18n.js";
import { condenseHints } from "/js/app/help.js";
import { getModuleApi, moduleRequest } from "/js/app/modules.js";
import { icon } from "./icons.js";

// --- Images in tickets (module "spaces") ---------------------------------
// An image is stored as [[space-img:<token>|<name>]] in the text. The file lives in
// the sender's space (folder "Ticket attachments"); it is loaded with authentication.
const IMG_RE = /\[\[space-img:([A-Za-z0-9_-]+)\|([^\]]*)\]\]/g;
const imgCache = new Map();
const spaces = () => getModuleApi("spaces");
const richText = (text) => esc(text).replace(IMG_RE, (_, tok, name) =>
  `<img data-sptok="${tok}" alt="${name}" title="${name}" style="display:block;max-width:320px;max-height:320px;border-radius:6px;margin:4px 0;cursor:zoom-in;background:rgba(0,0,0,.15);min-width:60px;min-height:40px">`);
async function loadImages(root) {
  const sp = spaces();
  for (const img of root.querySelectorAll("img[data-sptok]")) {
    const tok = img.dataset.sptok;
    img.addEventListener("click", () => sp?.openShareLink(tok));
    img.addEventListener("error", () => {
      if (!img.src) return;
      img.replaceWith(Object.assign(document.createElement("span"), { textContent: `[${img.alt || "image"} (cannot be displayed)]` }));
    }, { once: true });
    if (imgCache.has(tok)) { img.src = imgCache.get(tok); continue; }
    try {
      if (!sp) throw new Error("Spaces off");
      const u = await sp.blobUrl(`/api/spaces/s/${tok}/download?inline=1`);
      imgCache.set(tok, u); img.src = u;
    } catch {
      img.replaceWith(Object.assign(document.createElement("span"), { textContent: `[${img.alt || "image"} (not available)]` }));
    }
  }
}

// Colors are fixed, the labels are translated ONLY ON ACCESS.
// Important: these tables are evaluated when the module loads - at that point
// the language is not yet fixed. A direct tr(...) here (as it used to be for
// "resolved") would freeze the text to the language at load time and ignore a
// later language switch.
const STATUS_COLOR = {
  open: "#4da6ff", in_progress: "#ffd166", resolved: "#3ecf8e", closed: "#8892a6",
};
const STATUS_KEY = {
  open: "tk_s_open", in_progress: "tk_s_progress",
  resolved: "tk_s_resolved", closed: "tk_s_closed",
};
const PRIO_COLOR = {
  low: "#8892a6", normal: "#4da6ff", high: "#ffd166", critical: "#ff4d6d",
};
const PRIO_KEY = {
  low: "tk_p_low", normal: "tk_p_normal", high: "tk_p_high", critical: "tk_p_critical",
};
const statusOf = (k) => ({
  label: tr(STATUS_KEY[k] || STATUS_KEY.open),
  color: STATUS_COLOR[k] || STATUS_COLOR.open,
});
const prioOf = (k) => ({
  label: tr(PRIO_KEY[k] || PRIO_KEY.normal),
  color: PRIO_COLOR[k] || PRIO_COLOR.normal,
});

const fmtDate = (ms) => ms ? new Date(ms).toLocaleString(undefined,
  { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" }) : "–";
const fmtDay = (ms) => ms ? new Date(ms).toLocaleDateString(undefined) : "–";

// Visibility of comments - identical to the client notes.
const COMMENT_VIS = {
  all: { icon: icon("globe", 13), key: "tk_vis_all" },
  private: { icon: icon("lock", 13), key: "tk_vis_private" },
  custom: { icon: icon("users", 13), key: "tk_vis_custom" },
};

export function renderTickets(body, win) {
  // Turn the explanatory texts on this page into "?" symbols - once right after
  // drawing and once delayed for lazily loaded areas.
  setTimeout(() => condenseHints(body), 0);
  setTimeout(() => condenseHints(body), 400);

  const may = (p) => isAdmin() || hasGlobalPerm(p);
  let tickets = [];
  let subjects = { users: [], groups: [] };
  let ticketUsers = { users: [], groups: [] };   // choices for "specific users"
  // display name of a share ({type,id} or old plain user ID)
  const shareName = (item) => {
    const type = typeof item === "string" ? "user" : (item.type || "user");
    const id = typeof item === "string" ? item : item.id;
    if (type === "group") {
      const g = (ticketUsers.groups || []).find((x) => x.id === id);
      return g ? `${icon("users", 12)} ${g.name}` : `${icon("users", 12)} ?`;
    }
    const u = (ticketUsers.users || []).find((x) => x.id === id);
    return u ? u.username : id;
  };
  let currentId = null;
  let statusFilter = "active";   // 'active' | 'all' | a single status

  body.innerHTML = `
    <div style="display:flex;height:100%;background:var(--panel);position:relative">
      <div style="width:300px;min-width:300px;border-right:1px solid var(--border);display:flex;flex-direction:column">
        <div style="padding:8px 10px;border-bottom:1px solid var(--border);display:flex;gap:6px;align-items:center">
          <select id="tk-filter">
            <option value="active">${tr("tk_f_active")}</option>
            <option value="all">${tr("tk_f_all")}</option>
            <option value="open">${tr("tk_s_open")}</option>
            <option value="in_progress">${tr("tk_s_progress")}</option>
            <option value="resolved">${tr("tk_s_resolved")}</option>
            <option value="closed">${tr("tk_s_closed")}</option>
          </select>
          <span style="flex:1"></span>
          ${isAdmin() ? `<button class="btn" id="tk-portal" title="Set up the public ticket portal (/web/tickets)">${icon("globe", 15)}</button>` : ""}
          ${may("ticket_create") ? `<button class="btn-primary" id="tk-new">+ Ticket</button>` : ""}
        </div>
        <div id="tk-list" style="flex:1;overflow:auto"></div>
      </div>
      <div id="tk-detail" style="flex:1;overflow:auto;min-width:0"></div>
      <div id="tk-editor" style="display:none;position:absolute;inset:0;background:rgba(0,0,0,0.55);align-items:center;justify-content:center;z-index:5"></div>
    </div>
  `;
  const listEl = body.querySelector("#tk-list");
  const detailEl = body.querySelector("#tk-detail");
  const editorEl = body.querySelector("#tk-editor");

  body.querySelector("#tk-filter").addEventListener("change", (e) => {
    statusFilter = e.target.value; drawList();
  });
  body.querySelector("#tk-new")?.addEventListener("click", () => openEditor(null));

  // --- Ticket portal (admin): set up /web/tickets ---
  body.querySelector("#tk-portal")?.addEventListener("click", async () => {
    let cfg;
    try { cfg = await moduleRequest("/api/tickets/portal/admin-config"); }
    catch (e) { window.notify?.(e.message, "error"); return; }
    editorEl.style.display = "flex";
    editorEl.innerHTML = `
      <div style="background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:18px;width:460px;max-height:90%;overflow:auto">
        <h3 style="margin:0 0 10px;display:flex;align-items:center;gap:7px">${icon("globe", 18)} Ticket portal</h3>
        <p style="font-size:12px;color:var(--subtext);margin:0 0 10px">
          Public page at <code>${esc(window.location.origin)}/web/tickets</code> where
          (with or without login, depending on the setting) tickets can be created.
          When logged in, users also see their own and assigned tickets there and can
          edit them. New portal tickets go to the support group.
          ${cfg.webserver_on ? "" : `<b style="color:var(--warn)">The "Webserver" module is not active - the portal page becomes reachable only once it is enabled.</b>`}
        </p>
        <div class="form-row"><label><input type="checkbox" id="tp-on" ${cfg.enabled ? "checked" : ""}/> Enable portal</label></div>
        <div class="form-row"><label><input type="checkbox" id="tp-login" ${cfg.require_login ? "checked" : ""}/> Login required (RMM account)</label></div>
        <div class="form-row"><label>Title</label><input id="tp-title" value="${esc(cfg.title || "")}"/></div>
        <div class="form-row"><label>Welcome text</label><textarea id="tp-intro" rows="3" style="width:100%">${esc(cfg.intro || "")}</textarea></div>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
          <button class="btn" id="tp-cancel">${tr("cancel")}</button>
          <button class="btn-primary" id="tp-save">${tr("save")}</button>
        </div>
      </div>`;
    editorEl.querySelector("#tp-cancel").addEventListener("click", () => { editorEl.style.display = "none"; });
    editorEl.querySelector("#tp-save").addEventListener("click", async () => {
      try {
        await moduleRequest("/api/tickets/portal/admin-config", {
          method: "POST", noJob: true,
          body: JSON.stringify({
            enabled: editorEl.querySelector("#tp-on").checked,
            require_login: editorEl.querySelector("#tp-login").checked,
            title: editorEl.querySelector("#tp-title").value,
            intro: editorEl.querySelector("#tp-intro").value,
          }),
        });
        editorEl.style.display = "none";
        window.notify?.("Portal settings saved", "success");
      } catch (e) { window.notify?.(e.message, "error"); }
    });
  });

  const subjectLabel = (a) => {
    const list = a.subject_type === "user" ? subjects.users : subjects.groups;
    const hit = list.find((s) => s.id === a.subject_id);
    return (a.subject_type === "group" ? icon("users", 12) + " " : icon("user", 12) + " ") + (hit ? hit.label : a.subject_id);
  };
  // Knows both clients and the main RMM as a target.
  const clientLabel = (cid) => targetLabel(cid);

  // ---------------- list ----------------
  function drawList() {
    let rows = tickets;
    if (statusFilter === "active") rows = rows.filter((t) => t.status === "open" || t.status === "in_progress");
    else if (statusFilter !== "all") rows = rows.filter((t) => t.status === statusFilter);
    if (!rows.length) {
      listEl.innerHTML = `<div style="padding:14px;color:var(--subtext);font-size:12px">${tr("tk_none_in_view")}</div>`;
      return;
    }
    listEl.innerHTML = rows.map((t) => {
      const st = statusOf(t.status);
      const pr = prioOf(t.priority);
      const overdue = t.due_date && t.due_date < Date.now() && t.status !== "resolved" && t.status !== "closed";
      return `
      <div data-id="${esc(t.id)}" style="padding:9px 10px;border-bottom:1px solid var(--border);cursor:pointer;
           background:${t.id === currentId ? "var(--panel-2)" : "transparent"}">
        <div style="font-weight:600;font-size:13px;display:flex;gap:6px;align-items:center">
          <span style="width:8px;height:8px;border-radius:50%;background:${pr.color};flex:none" title="${tr("tk_priority")}: ${pr.label}"></span>
          <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(t.title)}</span>
        </div>
        <div style="display:flex;gap:8px;font-size:11px;color:var(--subtext);margin-top:2px;align-items:center">
          <span style="color:${st.color}">● ${st.label}</span>
          <span>${fmtDay(t.created_at)}</span>
          ${overdue ? `<span style="color:#ff4d6d">${icon("clock", 12)} ${tr("tk_overdue")}</span>` : t.due_date ? `<span>${icon("clock", 12)} ${fmtDay(t.due_date)}</span>` : ""}
          ${t.assignees?.length ? `<span>${icon("users", 12)} ${t.assignees.length}</span>` : ""}
        </div>
      </div>`;
    }).join("");
    listEl.querySelectorAll("[data-id]").forEach((el) =>
      el.addEventListener("click", () => selectTicket(el.dataset.id)));
  }

  async function selectTicket(id) {
    currentId = id;
    drawList();
    detailEl.innerHTML = `<div style="padding:16px;color:var(--subtext)">${tr("loading")}</div>`;
    try {
      const t = await api.ticket(id);
      drawDetail(t);
    } catch (e) {
      detailEl.innerHTML = `<div style="padding:16px;color:var(--danger)">${esc(e.message)}</div>`;
    }
  }

  // ---------------- detail ----------------
  function drawDetail(t) {
    const st = statusOf(t.status);
    const pr = prioOf(t.priority);
    detailEl.innerHTML = `
      <div style="padding:16px 18px">
        <div style="display:flex;gap:10px;align-items:flex-start;flex-wrap:wrap">
          <h2 style="margin:0;flex:1;min-width:200px">${esc(t.title)}</h2>
          ${may("ticket_edit") ? `<button class="btn" id="tkd-edit">${icon("edit", 14)} ${tr("edit")}</button>` : ""}
          ${may("ticket_delete") ? `<button class="btn btn-danger" id="tkd-del">${icon("trash", 14)} ${tr("delete")}</button>` : ""}
        </div>
        <div style="display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--subtext);margin:8px 0 12px">
          <span style="color:${st.color}">● ${st.label}</span>
          <span style="color:${pr.color}">${tr("tk_priority")}: ${pr.label}</span>
          <span>${tr("tk_created_by")} <b>${esc(t.created_by)}</b> ${tr("tk_on")} ${fmtDate(t.created_at)}</span>
          <span>${tr("tk_updated")}: ${fmtDate(t.updated_at)}</span>
          <span>${tr("tk_due")}: <b>${fmtDay(t.due_date)}</b></span>
          ${t.status === "resolved" ? `<span style="color:#3ecf8e">${tr("tk_resolved_by")} ${esc(t.resolved_by || "?")} ${tr("tk_on")} ${fmtDate(t.resolved_at)}</span>` : ""}
        </div>

        ${may("ticket_resolve") ? `
        <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px">
          ${t.status !== "in_progress" && t.status !== "resolved" && t.status !== "closed"
            ? `<button class="btn" data-status="in_progress">${tr("tk_s_progress")}</button>` : ""}
          ${t.status !== "resolved" && t.status !== "closed"
            ? `<button class="btn-primary" data-status="resolved">${icon("check", 14)} ${tr("tk_mark_resolved")}</button>` : ""}
          ${t.status === "resolved"
            ? `<button class="btn" data-status="closed">${tr("close")}</button>` : ""}
          ${(t.status === "resolved" || t.status === "closed")
            ? `<button class="btn" data-status="open">${tr("tk_reopen")}</button>` : ""}
        </div>` : ""}

        <h3 style="margin:0 0 6px">${tr("tk_description")}</h3>
        <div style="background:var(--panel-2);border:1px solid var(--border);border-radius:8px;padding:10px 12px;
             font-size:13px;white-space:pre-wrap;word-break:break-word">${richText(t.description || "") || `<i style='color:var(--subtext)'>${tr("tk_no_description")}</i>`}</div>

        <h3 style="margin:16px 0 6px">${tr("tk_assigned_to")}
          ${may("ticket_assign") ? `<button class="btn btn-sm" id="tkd-assign" style="margin-left:6px">${tr("tk_assign")}</button>` : ""}
        </h3>
        <div style="display:flex;gap:6px;flex-wrap:wrap;font-size:12px">
          ${t.assignees.length
            ? t.assignees.map((a) => `<span style="background:var(--panel-2);border:1px solid var(--border);border-radius:20px;padding:3px 10px">${esc(subjectLabel(a))}</span>`).join("")
            : `<span style="color:var(--subtext)">${tr("tk_nobody")}</span>`}
        </div>

        <h3 style="margin:16px 0 6px">${tr("tk_linked_clients")}</h3>
        <div style="display:flex;gap:6px;flex-wrap:wrap;font-size:12px">
          ${t.clients.length
            ? t.clients.map((cid) => `<span style="background:var(--panel-2);border:1px solid var(--border);border-radius:20px;padding:3px 10px">${esc(clientLabel(cid))}</span>`).join("")
            : `<span style="color:var(--subtext)">${tr("tk_no_clients")}</span>`}
        </div>

        <h3 style="margin:16px 0 6px">${tr("tk_attachments")}
          ${may("ticket_comment") ? `<button class="btn btn-sm" id="tkd-upload" style="margin-left:6px">${icon("paperclip", 13)} ${tr("tk_upload")}</button>
            <input type="file" id="tkd-file" hidden />` : ""}
        </h3>
        <div id="tkd-files" style="display:flex;gap:10px;flex-wrap:wrap"></div>

        <h3 style="margin:16px 0 6px">${tr("tk_comments")} (${t.comments.length})
          <button class="btn btn-sm" id="tkd-activity" style="margin-left:6px">${icon("clock", 13)} ${tr("tk_history")}</button>
        </h3>
        <div id="tkd-activity-box" style="display:none;margin-bottom:10px"></div>
        <div style="display:flex;flex-direction:column;gap:8px">
          ${t.comments.map((c) => {
            const v = COMMENT_VIS[c.visibility] || COMMENT_VIS.all;
            const shared = c.visibility === "custom" && (c.shared_with || []).length
              ? ` (${c.shared_with.map(shareName).map(esc).join(", ")})` : "";
            return `
            <div style="background:var(--panel-2);border:1px solid var(--border);border-radius:8px;padding:8px 12px;font-size:13px">
              <div style="font-size:11px;color:var(--subtext);margin-bottom:3px;display:flex;justify-content:space-between;gap:8px">
                <span><b>${esc(c.author)}</b> · ${fmtDate(c.created_at)}
                  · <span title="${tr("tk_visibility")}">${v.icon} ${esc(tr(v.key))}${shared}</span></span>
                ${c.can_edit ? `<button class="btn btn-danger btn-sm" data-cdel="${esc(c.id)}"
>${icon("trash", 13)}</button>` : ""}
              </div>
              <div style="white-space:pre-wrap;word-break:break-word">${c.hidden
                ? `<i style="color:var(--subtext)">${tr("tk_private_comment")}</i>`
                : richText(c.text)}</div>
            </div>`; }).join("") || `<span style="color:var(--subtext);font-size:12px">${tr("tk_no_comments")}</span>`}
        </div>
        ${may("ticket_comment") ? `
        <div style="display:flex;flex-direction:column;gap:6px;margin-top:10px">
          <div style="display:flex;gap:8px">
            <textarea id="tkd-comment" rows="2" placeholder="${tr("tk_write_comment")}"
              style="flex:1;resize:none"></textarea>
            ${spaces() ? `<button class="btn btn-icon" id="tkd-img" title="${tr("tk_attach_image")}" style="align-self:stretch;height:auto">${icon("image", 15)}</button>
            <input type="file" id="tkd-imgfile" accept="image/*" multiple style="display:none">` : ""}
            <button class="btn-primary" id="tkd-comment-send">${tr("tk_send")}</button>
          </div>
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            <select id="tkd-comment-vis" style="max-width:230px">
              ${Object.entries(COMMENT_VIS).map(([k, o]) =>
                `<option value="${k}">${tr(o.key)}</option>`).join("")}
            </select>
            <div id="tkd-comment-users" style="display:none;flex:1;min-width:220px">
              ${subjectPickerHtml(ticketUsers, [], { name: "tkd-subj" })}
            </div>
          </div>
        </div>` : ""}
      </div>
    `;

    // ---- Aktionen ----
    detailEl.querySelectorAll("[data-status]").forEach((b) =>
      b.addEventListener("click", async () => {
        try { drawDetail(await api.setTicketStatus(t.id, b.dataset.status)); await load(false); }
        catch (e) { window.notify?.(e.message, "error"); }
      }));
    detailEl.querySelector("#tkd-edit")?.addEventListener("click", () => openEditor(t));
    detailEl.querySelector("#tkd-del")?.addEventListener("click", async () => {
      if (!(await uiConfirm(tr("tk_delete_q", { title: t.title }), { danger: true }))) return;
      try {
        await api.deleteTicket(t.id);
        currentId = null; detailEl.innerHTML = "";
        await load(false);
      } catch (e) { window.notify?.(e.message, "error"); }
    });
    detailEl.querySelector("#tkd-assign")?.addEventListener("click", () => openAssign(t));
    // Comment visibility: show the user picker only for "specific users"
    const visSel = detailEl.querySelector("#tkd-comment-vis");
    const usersBox = detailEl.querySelector("#tkd-comment-users");
    visSel?.addEventListener("change", () => {
      usersBox.style.display = visSel.value === "custom" ? "" : "none";
    });
    initSubjectPicker(usersBox);
    // Images: upload and insert a marker into the comment field
    const commentTa = detailEl.querySelector("#tkd-comment");
    async function attachImages(files) {
      const sp = spaces();
      const imgs = [...files].filter((f) => f.type.startsWith("image/"));
      if (!sp || !imgs.length || !commentTa) return;
      for (const f of imgs) {
        try {
          const a = await sp.uploadAttachment(f, "tickets");
          const name = (a.name || "Bild").replace(/[\]|]/g, "_");
          commentTa.value = (commentTa.value ? commentTa.value.replace(/\s*$/, "\n") : "") + `[[space-img:${a.token}|${name}]]\n`;
        } catch (e) { window.notify?.(tr("tk_image_not_attached") + ": " + e.message, "error"); }
      }
      commentTa.focus();
    }
    const imgFile = detailEl.querySelector("#tkd-imgfile");
    detailEl.querySelector("#tkd-img")?.addEventListener("click", () => imgFile.click());
    imgFile?.addEventListener("change", () => { attachImages(imgFile.files); imgFile.value = ""; });
    commentTa?.addEventListener("paste", (e) => {
      const files = [...(e.clipboardData?.files || [])].filter((f) => f.type.startsWith("image/"));
      if (files.length && spaces()) { e.preventDefault(); attachImages(files); }
    });
    loadImages(detailEl);
    detailEl.querySelector("#tkd-comment-send")?.addEventListener("click", async () => {
      const ta = detailEl.querySelector("#tkd-comment");
      if (!ta.value.trim()) return;
      const visibility = visSel?.value || "all";
      const shared = readSubjectPicker(usersBox);
      if (visibility === "custom" && !shared.length) {
        window.notify?.(tr("note_pick_user"), "warn"); return;
      }
      try { drawDetail(await api.addTicketComment(t.id, ta.value, visibility, shared)); }
      catch (e) { window.notify?.(e.message, "error"); }
    });
    detailEl.querySelectorAll("[data-cdel]").forEach((b) =>
      b.addEventListener("click", async () => {
        if (!(await uiConfirm(tr("u_kommentar_loschen"), { okText: tr("delete"), danger: true }))) return;
        try { drawDetail(await api.deleteTicketComment(t.id, b.dataset.cdel)); }
        catch (e) { window.notify?.(e.message, "error"); }
      }));

    // Activity log of the ticket
    const actBtn = detailEl.querySelector("#tkd-activity");
    const actBox = detailEl.querySelector("#tkd-activity-box");
    actBtn?.addEventListener("click", async () => {
      const open = actBox.style.display === "none";
      actBox.style.display = open ? "" : "none";
      if (!open) return;
      actBox.innerHTML = `<div style="color:var(--subtext);font-size:12px">${tr("loading")}</div>`;
      try {
        const log = await api.getTicketActivity(t.id);
        actBox.innerHTML = log.length ? `
          <div style="border:1px solid var(--border);border-radius:8px;padding:6px;max-height:200px;overflow:auto">
            ${log.map((e) => `
              <div style="font-size:12px;padding:2px 0;display:flex;gap:8px">
                <span style="color:var(--subtext);flex:none">${fmtDate(e.created_at)}</span>
                <span style="flex:none"><b>${esc(e.actor_name)}</b></span>
                <span style="flex:1;min-width:0">${esc(e.label)}${e.details ? " – " + esc(e.details) : ""}</span>
              </div>`).join("")}
          </div>` : `<div style="color:var(--subtext);font-size:12px">${tr("tk_no_activity")}</div>`;
      } catch (e) {
        actBox.innerHTML = `<div style="color:var(--danger);font-size:12px">${esc(e.message)}</div>`;
      }
    });

    // ---- Upload ----
    const fileInput = detailEl.querySelector("#tkd-file");
    detailEl.querySelector("#tkd-upload")?.addEventListener("click", () => fileInput.click());
    fileInput?.addEventListener("change", async () => {
      const f = fileInput.files[0];
      if (!f) return;
      try { drawDetail(await api.uploadTicketFile(t.id, f)); }
      catch (e) { window.notify?.(`${tr("tk_upload_failed")}: ${e.message}`, "error"); }
    });

    // ---- Render attachments (images with preview, rest as a download tile) ----
    const filesEl = detailEl.querySelector("#tkd-files");
    if (!t.files.length) {
      filesEl.innerHTML = `<span style="color:var(--subtext);font-size:12px">${tr("tk_no_files")}</span>`;
    } else {
      for (const f of t.files) {
        const card = document.createElement("div");
        card.style.cssText = "background:var(--panel-2);border:1px solid var(--border);border-radius:8px;padding:8px;font-size:11px;max-width:160px";
        const isImg = (f.mime || "").startsWith("image/");
        card.innerHTML = `
          ${isImg ? `<div data-preview style="width:140px;height:90px;background:#0a1420;border-radius:6px;display:flex;align-items:center;justify-content:center;color:var(--subtext);cursor:pointer">${tr("loading")}</div>` : `<div style="text-align:center;padding:4px 0">${icon("file", 26)}</div>`}
          <div style="margin-top:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(f.filename)}">${esc(f.filename)}</div>
          <div style="color:var(--subtext)">${(f.size / 1024).toFixed(0)} KB · ${esc(f.uploaded_by)}</div>
          <div style="display:flex;gap:4px;margin-top:4px">
            <button class="btn btn-sm" data-dl>${icon("download", 14)}</button>
            ${may("ticket_edit") ? `<button class="btn btn-sm btn-danger" data-rm>${icon("trash", 14)}</button>` : ""}
          </div>`;
        filesEl.appendChild(card);
        // Bild-Vorschau mit Auth-Header (Blob-URL).
        if (isImg) {
          api.fetchTicketFile(t.id, f.id).then((url) => {
            const pv = card.querySelector("[data-preview]");
            if (pv) { pv.innerHTML = `<img src="${url}" style="max-width:140px;max-height:90px;border-radius:6px" />`;
              pv.addEventListener("click", () => window.open(url, "_blank")); }
          }).catch(() => {});
        }
        card.querySelector("[data-dl]").addEventListener("click", async () => {
          try {
            const url = await api.fetchTicketFile(t.id, f.id);
            const a = document.createElement("a");
            a.href = url; a.download = f.filename; a.click();
          } catch (e) { window.notify?.(e.message, "error"); }
        });
        card.querySelector("[data-rm]")?.addEventListener("click", async () => {
          if (!(await uiConfirm(tr("tk_del_file_q", { name: f.filename }), { danger: true }))) return;
          try { drawDetail(await api.deleteTicketFile(t.id, f.id)); }
          catch (e) { window.notify?.(e.message, "error"); }
        });
      }
    }
  }

  // ---------------- assign dialog ----------------
  function openAssign(t) {
    const isChecked = (type, id) => t.assignees.some((a) => a.subject_type === type && a.subject_id === id);
    // Unmanaged AD groups go into their own folder so the list stays tidy
    // when many directory groups have been imported.
    const { managed: managedGroups, unmanaged: unmanagedGroups } = splitGroups(subjects.groups || []);
    const groupRow = (arr) => arr.map((g) => `
      <label style="display:flex;gap:6px;align-items:center;cursor:pointer">
        <input type="checkbox" data-stype="group" data-sid="${esc(g.id)}"
          ${isChecked("group", g.id) ? "checked" : ""}/> ${esc(g.label || g.name)}</label>`).join("");
    editorEl.style.display = "flex";
    editorEl.innerHTML = `
      <div style="background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:18px;width:380px;max-height:90%;overflow:auto">
        <h3 style="margin:0 0 10px">${tr("tk_assign_title")}</h3>
        <div style="border:1px solid var(--border);border-radius:8px;padding:8px;max-height:280px;overflow:auto;font-size:12px">
          <div style="font-weight:700;margin-bottom:4px">${tr("tab_users")}</div>
          ${subjects.users.map((u) => `<label style="display:flex;gap:6px;align-items:center;cursor:pointer">
            <input type="checkbox" data-stype="user" data-sid="${esc(u.id)}" ${isChecked("user", u.id) ? "checked" : ""}/> ${esc(u.label)}</label>`).join("")}
          <div style="font-weight:700;margin:8px 0 4px">${tr("pm_groups")}</div>
          ${groupRow(managedGroups) || `<div style="color:var(--subtext)">${tr("set_no_groups")}</div>`}
          ${unmanagedGroups.length ? `
            <details style="margin-top:8px;border:1px dashed var(--border);border-radius:7px;padding:4px 7px"
                     ${unmanagedGroups.some((g) => isChecked("group", g.id)) ? "open" : ""}>
              <summary style="cursor:pointer;color:var(--subtext);list-style:none">
                ${icon("folder", 13)} ${tr("tk_unmanaged_groups")} (${unmanagedGroups.length})</summary>
              <div style="margin-top:4px">${groupRow(unmanagedGroups)}</div>
            </details>` : ""}
        </div>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px">
          <button class="btn" id="tka-cancel">${tr("cancel")}</button>
          <button class="btn-primary" id="tka-save">${tr("save")}</button>
        </div>
      </div>`;
    editorEl.querySelector("#tka-cancel").addEventListener("click", () => { editorEl.style.display = "none"; });
    editorEl.querySelector("#tka-save").addEventListener("click", async () => {
      const assignees = [...editorEl.querySelectorAll("input:checked")]
        .map((cb) => ({ subject_type: cb.dataset.stype, subject_id: cb.dataset.sid }));
      try {
        const updated = await api.setTicketAssignees(t.id, assignees);
        editorEl.style.display = "none";
        drawDetail(updated);
        await load(false);
      } catch (e) { window.notify?.(e.message, "error"); }
    });
  }

  // ---------------- create/edit dialog ----------------
  function openEditor(existing) {
    const isEdit = !!existing;
    const cliChecked = (cid) => existing?.clients?.includes(cid);
    editorEl.style.display = "flex";
    editorEl.innerHTML = `
      <div style="background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:18px;width:480px;max-height:92%;overflow:auto">
        <h3 style="margin:0 0 12px">${isEdit ? tr("tk_edit") : tr("tk_new")}</h3>
        <div class="form-row"><label>${tr("tk_title_label")}</label>
          <input id="tke-title" value="${esc(existing?.title || "")}" placeholder="${tr("tk_title_ph")}" /></div>
        <div class="form-row"><label>${tr("tk_description")}</label>
          <textarea id="tke-desc" rows="5" style="width:100%">${esc(existing?.description || "")}</textarea></div>
        <div class="form-row"><label>${tr("tk_priority")}</label>
          <select id="tke-prio">
            ${Object.keys(PRIO_KEY).map((k) =>
              `<option value="${k}" ${(existing?.priority ?? "normal") === k ? "selected" : ""}>${tr(PRIO_KEY[k])}</option>`).join("")}
          </select></div>
        <div class="form-row"><label>${tr("tk_due_on")}</label>
          <input id="tke-due" type="date" value="${existing?.due_date ? new Date(existing.due_date).toISOString().slice(0, 10) : ""}" /></div>
        <div class="form-row"><label>${tr("tk_concerns")}</label></div>
        <div style="border:1px solid var(--border);border-radius:8px;padding:8px;max-height:170px;overflow:auto;font-size:12px">
          <!-- The RMM itself as a target: for everything that is not tied to a
               single device (dashboard, permissions, server). -->
          <label style="display:flex;gap:6px;align-items:center;cursor:pointer">
            <input type="checkbox" data-cid="${esc(RMM_TARGET)}" ${cliChecked(RMM_TARGET) ? "checked" : ""}/>
            ${icon("building", 14)} ${esc(RMM_LABEL)}</label>
          <div style="font-weight:700;margin:8px 0 4px;color:var(--subtext)">${tr("tk_clients")}</div>
          ${state.clients.map((c) => `<label style="display:flex;gap:6px;align-items:center;cursor:pointer">
            <input type="checkbox" data-cid="${esc(c.id)}" ${cliChecked(c.id) ? "checked" : ""}/> ${icon("computer", 14)} ${esc(c.hostname)}</label>`).join("")
          || `<div style="color:var(--subtext)">${tr("tk_no_clients_visible")}</div>`}
        </div>
        ${may("ticket_assign") ? `
        <div class="form-row" style="margin-top:10px"><label>${tr("tk_assign_to")}</label></div>
        <div id="tke-assign" style="border:1px solid var(--border);border-radius:8px;padding:8px;max-height:150px;overflow:auto;font-size:12px">
          <div style="font-weight:700;margin-bottom:4px">${tr("tab_users")}</div>
          ${subjects.users.map((u) => `<label style="display:flex;gap:6px;align-items:center;cursor:pointer">
            <input type="checkbox" data-stype="user" data-sid="${esc(u.id)}"
              ${existing?.assignees?.some((a) => a.subject_type === "user" && a.subject_id === u.id) ? "checked" : ""}/> ${esc(u.label)}</label>`).join("")}
          <div style="font-weight:700;margin:8px 0 4px">${tr("pm_groups")}</div>
          ${(() => {
            // Unmanaged (AD) groups live in a collapsed folder -
            // otherwise the own groups get lost in a long AD list.
            const isOn = (g) => existing?.assignees?.some(
              (a) => a.subject_type === "group" && a.subject_id === g.id);
            const rows = (arr) => arr.map((g) => `
              <label style="display:flex;gap:6px;align-items:center;cursor:pointer">
                <input type="checkbox" data-stype="group" data-sid="${esc(g.id)}"
                  ${isOn(g) ? "checked" : ""}/> ${esc(g.label || g.name)}</label>`).join("");
            const { managed, unmanaged } = splitGroups(subjects.groups || []);
            let out = rows(managed) || `<div style="color:var(--subtext)">${tr("set_no_groups")}</div>`;
            if (unmanaged.length) {
              out += `
                <details style="margin-top:6px" ${unmanaged.some(isOn) ? "open" : ""}>
                  <summary style="cursor:pointer;color:var(--subtext)">
                    ${icon("folder", 13)} ${tr("tk_unmanaged_groups")} (${unmanaged.length})
                  </summary>
                  <div style="margin-top:4px">${rows(unmanaged)}</div>
                </details>`;
            }
            return out;
          })()}
        </div>` : ""}
        <div id="tke-error" class="hidden" style="color:var(--danger);font-size:12px;margin-top:8px"></div>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px">
          <button class="btn" id="tke-cancel">${tr("cancel")}</button>
          <button class="btn-primary" id="tke-save">${isEdit ? tr("save") : tr("tk_create")}</button>
        </div>
      </div>`;
    const q = (sel) => editorEl.querySelector(sel);
    q("#tke-cancel").addEventListener("click", () => { editorEl.style.display = "none"; });
    q("#tke-save").addEventListener("click", async () => {
      const due = q("#tke-due").value;
      const payload = {
        title: q("#tke-title").value.trim(),
        description: q("#tke-desc").value,
        priority: q("#tke-prio").value,
        due_date: due ? new Date(due + "T12:00:00").getTime() : null,
        clients: [...editorEl.querySelectorAll("[data-cid]:checked")].map((cb) => cb.dataset.cid),
        assignees: [...editorEl.querySelectorAll("#tke-assign input:checked")]
          .map((cb) => ({ subject_type: cb.dataset.stype, subject_id: cb.dataset.sid })),
      };
      try {
        const t = isEdit ? await api.updateTicket(existing.id, payload)
                         : await api.createTicket(payload);
        editorEl.style.display = "none";
        currentId = t.id;
        drawDetail(t);
        await load(false);
      } catch (e) {
        const err = q("#tke-error");
        err.textContent = e.message;
        err.classList.remove("hidden");
      }
    });
  }

  // ---------------- loading ----------------
  async function load(selectFirst = true) {
    try {
      const [rows, subj, tusers] = await Promise.all([
        api.tickets(),
        api.ticketSubjects().catch(() => ({ users: [], groups: [] })),
        api.getTicketUsers().catch(() => ({ users: [], groups: [] })),
      ]);
      tickets = rows;
      subjects = subj;
      ticketUsers = tusers;
      drawList();
      if (selectFirst && !currentId && tickets.length) selectTicket(tickets[0].id);
      if (!tickets.length) {
        detailEl.innerHTML = `<div style="padding:20px;color:var(--subtext);font-size:13px">
          ${tr("tk_none_visible")}</div>`;
      }
    } catch (e) {
      listEl.innerHTML = `<div style="padding:12px;color:var(--danger);font-size:12px">${esc(e.message)}</div>`;
    }
  }
  load();
}
