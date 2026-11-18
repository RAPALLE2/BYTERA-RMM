// common.js (module "proxmox") - shared helpers
import { getToken } from "/js/app/api.js";
import { withBackground, resolveJob } from "/js/app/bgjobs.js";

export async function preq(path, opts = {}) {
  const token = getToken() || "";
  const headers = { ...(token ? { Authorization: "Bearer " + token } : {}) };
  if (opts.body !== undefined && typeof opts.body !== "string") { opts = { ...opts, body: JSON.stringify(opts.body) }; }
  if (opts.body) headers["Content-Type"] = "application/json";
  const res = await fetch(withBackground("/api/proxmox" + path, opts.method), { ...opts, headers });   // backend job, see bgjobs.js
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch { /* ignore */ }
    const err = new Error(msg); err.status = res.status; throw err;
  }
  return resolveJob(await res.json());
}

export const fmtBytes = (b) => {
  if (!b) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return `${b.toFixed(b >= 10 || i === 0 ? 0 : 1)} ${u[i]}`;
};
export const fmtUptime = (s) => {
  if (!s) return "–";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d} d ${h} h` : h ? `${h} h ${m} min` : `${m} min`;
};
export const pct = (a, b) => (b ? Math.min(100, Math.round((a / b) * 100)) : 0);
export const bar = (p, color = "var(--accent)") =>
  `<div style="height:6px;background:var(--border);border-radius:3px;overflow:hidden">
     <div style="height:100%;width:${p}%;background:${p > 90 ? "var(--danger)" : p > 75 ? "var(--warn)" : color}"></div></div>`;
export const STATUS = {
  running: { c: "var(--online)", t: "running" }, stopped: { c: "var(--subtext)", t: "stopped" },
  paused: { c: "var(--warn)", t: "paused" }, suspended: { c: "var(--warn)", t: "paused" },
};
export const dot = (st) => `<span title="${(STATUS[st] || {}).t || st}" style="display:inline-block;width:8px;height:8px;border-radius:50%;
  background:${(STATUS[st] || {}).c || "var(--warn)"}"></span>`;
