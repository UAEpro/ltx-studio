// LTX Studio web app - shell: router, API, icons, toasts, modal, jobs dock. No build step (ES modules).
import { filmsView } from "./views/films.js";
import { filmView } from "./views/film.js";
import { setupView } from "./views/setup.js";
import { farmView } from "./views/farm.js";
import { settingsView } from "./views/settings.js";

// ---------------------------------------------------------------- API
export async function api(path, opts = {}) {
  const method = opts.method || (opts.body !== undefined ? "POST" : "GET");
  const r = await fetch(path, {
    method, headers: opts.body !== undefined ? { "Content-Type": "application/json" } : {},
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail || j); } catch {}
    throw new Error(msg || `HTTP ${r.status}`);
  }
  return r.status === 204 ? null : r.json();
}

// ---------------------------------------------------------------- helpers
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
export const fmtTime = (s) => {
  if (s == null || isNaN(s)) return "–";
  s = Math.round(s);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60), r = s % 60;
  if (m < 60) return `${m}:${String(r).padStart(2, "0")}`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
};
export const pct = (x) => (x == null ? "–" : `${Math.round(x * 100)}%`);
export function debounce(fn, ms = 600) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

const ICONS = {
  film: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M7 3v18M17 3v18M3 8h4M3 16h4M17 8h4M17 16h4M3 12h18"/>',
  cpu: '<rect x="5" y="5" width="14" height="14" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>',
  download: '<path d="M12 3v12M7 10l5 5 5-5M4 21h16"/>',
  sliders: '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>',
  play: '<path d="M7 4v16l13-8z"/>',
  check: '<path d="M4 12l5 5L20 6"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  refresh: '<path d="M20 11a8 8 0 0 0-14.9-3.5M4 4v4h4M4 13a8 8 0 0 0 14.9 3.5M20 20v-4h-4"/>',
  edit: '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M14 6l4 4"/>',
  scissors: '<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M20 4L8.5 15.5M14.5 14.5L20 20M8.5 8.5L12 12"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  trash: '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
  wand: '<path d="M15 4V2M15 10V8M11 6H9M21 6h-2M18 3l-1 1M18 9l-1-1M3 21l11-11 2 2L5 23z"/>',
  users: '<circle cx="9" cy="8" r="4"/><path d="M2 21c0-4 3-6 7-6s7 2 7 6M17 4a4 4 0 0 1 0 8M22 21c0-3-2-5-5-6"/>',
  clap: '<path d="M4 9h16v11H4zM4 9l2-5 14 2-1 3M9 4.5L8 9M14 5.2L13 9"/>',
  eye: '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  star: '<path d="M12 3l2.8 5.8 6.2.9-4.5 4.4 1 6.3L12 17.6 6.5 20.4l1-6.3L3 9.7l6.2-.9z"/>',
  alert: '<path d="M12 3l10 18H2zM12 10v5M12 18h.01"/>',
  back: '<path d="M15 5l-7 7 7 7"/>',
  image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="M21 17l-5-5-9 8"/>',
  layers: '<path d="M12 3l9 5-9 5-9-5zM3 13l9 5 9-5"/>',
  zap: '<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  sparkle: '<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8zM19 16l.8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8z"/>',
  up: '<path d="M12 19V5M6 11l6-6 6 6"/>',
  down: '<path d="M12 5v14M6 13l6 6 6-6"/>',
  map: '<path d="M9 4l-6 2v14l6-2 6 2 6-2V4l-6 2zM9 4v14M15 6v14"/>',
  box: '<path d="M21 8l-9-5-9 5 9 5zM3 8v8l9 5 9-5V8M12 13v8"/>',
  ext: '<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
  terminal: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9l3 3-3 3M12 15h5"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
};
export const icon = (name, cls = "") => `<i data-icon="${name}" class="${cls}"><svg viewBox="0 0 24 24">${ICONS[name] || ""}</svg></i>`;
export function hydrateIcons(root = document) {
  $$("i[data-icon]:not(:has(svg))", root).forEach((el) => { el.innerHTML = `<svg viewBox="0 0 24 24">${ICONS[el.dataset.icon] || ""}</svg>`; });
}

export function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = `${icon(kind === "bad" ? "alert" : kind === "ok" ? "check" : "info")}<div>${esc(msg)}</div>`;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "bad" ? 7000 : 4000);
}

export function modal(title, bodyHtml, { wide = true, onClose } = {}) {
  const root = $("#modal-root");
  root.innerHTML = `<div class="modal-bg"><div class="modal" style="${wide ? "" : "width:min(620px,100%)"}">
    <div class="mhead"><h2>${title}</h2><button class="btn ghost icon" data-close>${icon("x")}</button></div>
    <div class="mbody">${bodyHtml}</div></div></div>`;
  const close = () => { root.innerHTML = ""; document.removeEventListener("keydown", onKey); onClose && onClose(); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);
  $(".modal-bg", root).addEventListener("mousedown", (e) => { if (e.target.classList.contains("modal-bg")) close(); });
  $("[data-close]", root).onclick = close;
  return { el: $(".modal", root), close };
}

export async function confirmBox(title, text, ok = "Continue") {
  return new Promise((res) => {
    const m = modal(title, `<p class="muted" style="margin:0 0 18px">${text}</p>
      <div class="row end"><button class="btn" data-no>Cancel</button><button class="btn primary" data-yes>${esc(ok)}</button></div>`,
      { wide: false, onClose: () => res(false) });
    $("[data-no]", m.el).onclick = () => { m.close(); };
    $("[data-yes]", m.el).onclick = () => { res(true); m.close(); };
  });
}

// ---------------------------------------------------------------- jobs dock
const seenDone = new Map();
export const jobListeners = new Set();
async function pollJobs() {
  try {
    const jobs = await api("/api/jobs");
    const now = Date.now() / 1000;
    const show = jobs.filter((j) => ["queued", "running"].includes(j.status) || (j.finished && now - j.finished < 12));
    const dock = $("#jobs-dock");
    const details = await Promise.all(show.slice(0, 4).map((j) => (j.status === "running" ? api(`/api/jobs/${j.id}`).catch(() => j) : j)));
    dock.innerHTML = details.map((j) => {
      const st = j.status === "running" ? '<span class="spinner"></span>' : j.status === "queued" ? '<span class="dot"></span>'
        : j.status === "done" ? `<span style="color:var(--ok)">${icon("check", "sm")}</span>` : `<span style="color:var(--bad)">${icon("alert", "sm")}</span>`;
      const last = (j.tail || []).filter((l) => l.trim()).slice(-1)[0] || (j.error || (j.status === "queued" ? "waiting for the GPU…" : ""));
      return `<div class="job" data-job="${j.id}" style="cursor:pointer"><div class="row">${st}<span class="t">${esc(j.title)}${j.film ? ` · <span class="muted">${esc(j.film.split("_")[0])}</span>` : ""}</span>
        <span class="small muted">${j.started ? fmtTime((j.finished || now) - j.started) : ""}</span></div>
        ${last ? `<div class="last">${esc(last)}</div>` : ""}</div>`;
    }).join("");
    $$("[data-job]", dock).forEach((el) => (el.onclick = () => showJob(el.dataset.job)));
    for (const j of jobs) {
      if (j.finished && !seenDone.has(j.id)) {
        seenDone.set(j.id, true);
        if (now - j.finished < 15) {
          toast(`${j.title}: ${j.status === "done" ? "done" : "failed"}`, j.status === "done" ? "ok" : "bad");
          jobListeners.forEach((fn) => fn(j));
        }
      }
    }
  } catch {}
}

export async function showJob(id) {
  const m = modal("Job log", `<div class="log" style="max-height:60vh" id="joblog">loading…</div>`);
  const tick = async () => {
    try {
      const j = await api(`/api/jobs/${id}`);
      const el = $("#joblog", m.el);
      if (!el) return;
      el.textContent = `${j.title} - ${j.status}${j.error ? "\n" + j.error : ""}\n\n` + (j.tail || []).join("\n");
      el.scrollTop = el.scrollHeight;
      if (["queued", "running"].includes(j.status)) setTimeout(tick, 2000);
    } catch {}
  };
  tick();
}

async function pollRail() {
  try {
    const [workers, setup] = await Promise.all([api("/api/workers"), api("/api/setup").catch(() => null)]);
    const online = workers.filter((w) => w.status !== "offline");
    const busy = online.filter((w) => w.status === "busy");
    const navW = $("#nav-workers");
    navW.textContent = online.length ? `${online.length}` : "0";
    navW.className = online.length ? "ok" : "warn";
    const g = $("#gpu-status");
    if (!online.length) g.innerHTML = `<span class="dot warn" style="display:inline-block"></span> <b>No render worker</b><br><span>Start one in Setup</span>`;
    else {
      const w = busy[0] || online[0];
      const m = /(\d+)%/.exec(w.progress || "");
      g.innerHTML = `<div class="row" style="gap:7px"><span class="dot ${busy.length ? "run" : "ok"}"></span><b>${esc(w.hostname || w.id)}</b></div>
        <div style="margin-top:4px">${busy.length ? esc((w.progress || "rendering").slice(0, 60)) : "idle · ready"}</div>
        ${busy.length && m ? `<div class="bar"><span style="width:${m[1]}%"></span></div>` : ""}`;
    }
    const ns = $("#nav-setup");
    if (setup) { ns.textContent = setup.ready ? "" : "!"; ns.className = setup.ready ? "" : "warn"; }
  } catch {}
}

// ---------------------------------------------------------------- router
let current = null;
async function route() {
  const parts = (location.hash || "#/films").slice(2).split("/");
  const [page, ...rest] = parts;
  $$(".rail nav a").forEach((a) => a.classList.toggle("active", a.dataset.nav === (page === "film" ? "films" : page)));
  if (page === "film" && current && current.film === rest[0] && current.go) { current.go(rest[1]); return; }  // step switch, same film
  if (current && current.destroy) current.destroy();
  const view = $("#view");
  view.innerHTML = "";
  window.scrollTo(0, 0);
  const views = { films: filmsView, film: filmView, setup: setupView, farm: farmView, settings: settingsView };
  current = await (views[page] || filmsView)(view, ...rest);
  hydrateIcons(view);
}
window.addEventListener("hashchange", route);
hydrateIcons();
route();
pollJobs(); setInterval(pollJobs, 3000);
pollRail(); setInterval(pollRail, 8000);
