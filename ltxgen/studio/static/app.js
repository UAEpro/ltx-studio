"use strict";
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
let CFG = {}, currentProject = null, pollTimer = null;

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const text = await r.text();
  let data; try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!r.ok) throw new Error((data && data.detail) || r.statusText);
  return data;
}
function toast(msg, ms = 3500) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), ms);
}
function fmtTime(s) { if (s == null) return ""; s = Math.round(s); return s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`; }

// ---------------- tabs
document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
function showTab(name) {
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".panel").forEach((p) => (p.hidden = p.id !== `tab-${name}`));
  try { localStorage.setItem("tab", name); } catch {}
  if (name === "projects" && !currentProject) loadProjects();
  if (name === "workers") loadWorkers();
}

// ---------------- create
function parseStoryboard() {
  const raw = $("#storyboard").value.trim();
  if (!raw) throw new Error("Paste a storyboard JSON first");
  const m = raw.match(/\{[\s\S]*\}/);
  const sb = JSON.parse(m ? m[0] : raw);
  if (!Array.isArray(sb.shots) || !sb.shots.length) throw new Error("Storyboard has no shots");
  if ($("#ov-preset").value) sb.preset = $("#ov-preset").value;
  if ($("#ov-aspect").value) sb.aspect = $("#ov-aspect").value;
  sb.shots.forEach((s, i) => { s.id = s.id || `s${String(i + 1).padStart(2, "0")}`; });
  return sb;
}
function preview() {
  let sb;
  try { sb = parseStoryboard(); } catch (e) { toast("Storyboard error: " + e.message); return null; }
  const total = sb.shots.reduce((a, s) => a + Number(s.seconds ?? sb.seconds ?? 5), 0);
  const chained = sb.shots.filter((s) => s.continue_from_previous).length;
  $("#pv-title").textContent = sb.title || "Untitled";
  $("#pv-summary").textContent = `${sb.shots.length} shots · ~${Math.round(total)} s · preset ${sb.preset || "draft"} · ${sb.aspect || "16:9"} · ` +
    `${sb.transition || "cut"} transitions · ${chained} chained (render sequentially), ${sb.shots.length - chained} independent (render in parallel)`;
  $("#pv-rows").innerHTML = sb.shots.map((s) => `<tr><td>${esc(s.id)}</td><td>${esc(s.seconds ?? sb.seconds ?? 5)}</td>
    <td>${s.continue_from_previous ? "↳ continues" : ""}</td><td>${esc(s.prompt)}</td>
    <td class="ref">${s.image ? `<img class="refimg" src="/api/uploads/${esc(String(s.image).split(/[\\/]/).pop())}">` : ""}
      <label class="button small">${s.image ? "Change" : "Picture"}<input type="file" accept="image/*" hidden data-shot="${esc(s.id)}"></label>
      ${s.image ? `<select data-strength="${esc(s.id)}">${[1.0, 0.8, 0.6, 0.4].map((v) => `<option ${Math.abs(v - (s.image_strength ?? 1)) < 0.01 ? "selected" : ""}>${v}</option>`).join("")}</select>` : ""}</td></tr>`).join("");
  document.querySelectorAll("#pv-rows input[type=file]").forEach((inp) => (inp.onchange = () => attachPreviewImage(inp.dataset.shot, inp.files[0])));
  document.querySelectorAll("#pv-rows select[data-strength]").forEach((sel) => (sel.onchange = () => setShotField(sel.dataset.strength, "image_strength", Number(sel.value))));
  $("#preview-card").hidden = false;
  return sb;
}
function setShotField(sid, key, value) {
  const raw = $("#storyboard").value; const m = raw.match(/\{[\s\S]*\}/);
  const sb = JSON.parse(m ? m[0] : raw);
  const shot = sb.shots.find((x, i) => (x.id || `s${String(i + 1).padStart(2, "0")}`) === sid);
  if (value === null) delete shot[key]; else shot[key] = value;
  $("#storyboard").value = JSON.stringify(sb, null, 2); preview();
}
async function attachPreviewImage(sid, file) {
  if (!file) return;
  const fd = new FormData(); fd.append("file", file);
  const r = await fetch("/api/uploads", { method: "POST", body: fd });
  if (!r.ok) return toast((await r.json()).detail || "upload failed");
  const { path } = await r.json();
  setShotField(sid, "image", path); toast(`${sid}: reference picture attached (start frame)`);
}
$("#btn-validate").onclick = preview;
$("#btn-example").onclick = async () => {
  const r = await fetch("/static/example.json"); $("#storyboard").value = await r.text(); preview();
};
$("#btn-copy-prompt").onclick = async () => {
  const text = CFG.scenario_prompt + ($("#scenario").value.trim() || "<write your scenario here>") +
    ($("#notes").value.trim() ? `\n\nExtra direction: ${$("#notes").value.trim()}` : "");
  try { await navigator.clipboard.writeText(text); toast("Prompt copied. Paste it into claude.ai, then paste Claude's JSON into step 2."); }
  catch { $("#storyboard").value = text; toast("Clipboard blocked; prompt placed in the storyboard box, copy it from there."); }
};
$("#btn-claude").onclick = async () => {
  const scenario = $("#scenario").value.trim();
  if (!scenario) return toast("Write a scenario first");
  const [engine, model] = $("#engine").value.split("|");
  const b = $("#btn-claude"); b.disabled = true; b.textContent = engine === "claude" ? "Claude is writing…" : "Gemma is writing (local)…";
  try {
    const r = await api("/api/scenario", { method: "POST", body: JSON.stringify({ scenario, notes: $("#notes").value, engine, model }) });
    $("#storyboard").value = JSON.stringify(r.storyboard, null, 2); preview();
  } catch (e) { toast(e.message, 6000); }
  finally { b.disabled = !$("#engine").value; b.textContent = "Write storyboard"; }
};
$("#btn-start").onclick = async () => {
  const sb = preview(); if (!sb) return;
  try {
    const r = await api("/api/projects", { method: "POST", body: JSON.stringify(sb) });
    toast("Rendering started"); showTab("projects"); openProject(r.project_id);
  } catch (e) { toast(e.message, 6000); }
};

// ---------------- projects
const COLORS = { done: "var(--ok)", claimed: "var(--run)", ready: "var(--line)", pending: "var(--line)", failed: "var(--err)", cancelled: "var(--warn)" };
function bar(counts, n) {
  return `<div class="bar">${["done", "claimed", "failed", "cancelled", "ready", "pending"].map((k) =>
    counts[k] ? `<i style="width:${(100 * counts[k]) / n}%;background:${COLORS[k]}"></i>` : "").join("")}</div>`;
}
async function loadProjects() {
  $("#project-view").hidden = true; $("#project-list").hidden = false; currentProject = null;
  const list = await api("/api/projects");
  $("#project-list").innerHTML = list.length ? list.map((p) => {
    const n = Object.values(p.counts).reduce((a, b) => a + b, 0) || 1;
    return `<div class="card pcard" data-id="${p.id}"><div class="row between"><b>${esc(p.title)}</b>
      <span class="badge s-${p.status === "done" ? "done" : p.status.includes("fail") ? "failed" : "claimed"}">${esc(p.status)}</span></div>
      ${bar(p.counts, n)}<div class="muted small">${p.counts.done || 0}/${n} shots · ${esc(p.preset)} · ${new Date(p.created * 1000).toLocaleString()}</div></div>`;
  }).join("") : `<p class="muted">No projects yet. Create one in “New film”.</p>`;
  document.querySelectorAll(".pcard").forEach((c) => (c.onclick = () => openProject(c.dataset.id)));
}
$("#btn-back").onclick = loadProjects;

async function openProject(id) {
  currentProject = id; $("#project-list").hidden = true; $("#project-view").hidden = false;
  $("#pj-shots").innerHTML = ""; await renderProject();
}
async function renderProject() {
  if (!currentProject) return;
  let p; try { p = await api(`/api/projects/${currentProject}`); } catch (e) { toast(e.message); return loadProjects(); }
  const done = p.shots.filter((s) => s.status === "done").length;
  $("#pj-title").textContent = p.title;
  $("#pj-meta").textContent = `${p.status}${p.error ? " – " + p.error : ""} · ${done}/${p.shots.length} shots done · preset ${p.storyboard.preset || "draft"} · ${p.storyboard.aspect || "16:9"}`;
  const fin = $("#pj-final");
  if (p.final) {
    fin.hidden = false;
    const v = $("#pj-final-video"), src = p.final + "?v=" + p.created + done;
    if (v.dataset.src !== src) { v.src = src; v.dataset.src = src; }
    $("#pj-final-dl").href = p.final;
    $("#pj-final-info").textContent = p.final_info ? `${p.final_info.width}×${p.final_info.height} · ${fmtTime(p.final_info.duration_s)}` : "";
  } else fin.hidden = true;
  const box = $("#pj-shots");
  p.shots.forEach((s) => {
    let card = box.querySelector(`[data-shot="${s.id}"]`);
    const key = `${s.status}|${s.version}|${s.video}|${s.progress}|${s.worker}`;
    if (card && card.dataset.key === key) return;
    const editing = card && card.contains(document.activeElement);
    if (editing) { card.querySelector(".st").innerHTML = statusLine(s); return; }
    const html = shotCard(s, p);
    if (!card) { card = document.createElement("div"); card.className = "card shot"; card.dataset.shot = s.id; box.appendChild(card); }
    card.innerHTML = html; card.dataset.key = key;
    card.querySelector(".redo").onclick = () => redo(s.id, card);
    card.querySelector(".reffile").onchange = (e) => uploadRef(s.id, card, e.target.files[0]);
    const clr = card.querySelector(".refclear");
    if (clr) clr.onclick = async () => {
      await api(`/api/projects/${currentProject}/shots/${s.id}/redo`, { method: "POST", body: JSON.stringify({ image: "" }) });
      toast(`${s.id}: picture removed, re-rendering`); card.dataset.key = ""; renderProject();
    };
  });
}
function statusLine(s) {
  const w = s.worker ? ` · ${esc(s.worker)}` : "";
  const prog = s.status === "claimed" && s.progress ? ` · ${esc(s.progress).slice(0, 80)}` : "";
  const t = s.elapsed_s ? ` · ${fmtTime(s.elapsed_s)}` : "";
  const dep = s.status === "pending" && s.dep ? ` · waiting for ${esc(s.dep)}` : "";
  return `<span class="badge s-${s.status}">${s.status === "claimed" ? "rendering" : esc(s.status)}</span>
    <span class="muted small"> v${s.version}${w}${t}${dep}${prog}</span>`;
}
function shotCard(s) {
  const media = s.video
    ? `<video controls preload="metadata" poster="${s.sheet || ""}" src="${s.video}"></video>`
    : `<div class="placeholder">${s.status === "claimed" ? "Rendering…" : s.status === "failed" ? "Failed" : "Queued"}</div>`;
  const err = s.error ? `<div class="err">${esc(typeof s.error === "object" ? [s.error.error, s.error.hint].filter(Boolean).join("\n") : s.error)}</div>` : "";
  return `<div class="row between"><b>${esc(s.id)}${s.dep ? ` <span class="muted small">↳ ${esc(s.dep)}</span>` : ""}</b>
      <span class="st">${statusLine(s)}</span></div>
    ${media}${err}
    <textarea rows="4">${esc(s.prompt)}</textarea>
    <div class="row ref">${s.ref_image ? `<img class="refimg" src="${s.ref_image}" title="reference picture">` : ""}
      <label class="button small">${s.ref_image ? "Change picture" : "Add reference picture"}<input class="reffile" type="file" accept="image/*" hidden></label>
      <label>Strength <select class="refstrength">${[1.0, 0.8, 0.6, 0.4].map((v) => `<option ${Math.abs(v - (s.ref_strength ?? 1)) < 0.01 ? "selected" : ""}>${v}</option>`).join("")}</select></label>
      ${s.ref_image ? `<button class="ghost small refclear">Remove</button>` : ""}</div>
    <div class="row"><label>Seed <input class="seed" type="number" value="${s.params.seed}"></label>
      <button class="ghost small" onclick="this.previousElementSibling.querySelector('input').value=Math.floor(Math.random()*1e6)">🎲</button>
      <button class="redo">Redo shot</button>
      ${s.sheet ? `<a class="button small" href="${s.sheet}" target="_blank">Frames</a>` : ""}</div>`;
}
async function uploadRef(sid, card, file) {
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file); fd.append("strength", card.querySelector(".refstrength").value); fd.append("role", "start");
  const r = await fetch(`/api/projects/${currentProject}/shots/${sid}/image`, { method: "POST", body: fd });
  if (!r.ok) return toast((await r.json()).detail || "upload failed");
  toast(`${sid}: picture attached, re-rendering from it`); card.dataset.key = ""; renderProject();
}
async function redo(sid, card) {
  const body = { prompt: card.querySelector("textarea").value, seed: Number(card.querySelector(".seed").value) };
  try { await api(`/api/projects/${currentProject}/shots/${sid}/redo`, { method: "POST", body: JSON.stringify(body) });
    toast(`${sid} queued again`); card.dataset.key = ""; renderProject(); } catch (e) { toast(e.message); }
}
$("#btn-stitch").onclick = async () => {
  try { await api(`/api/projects/${currentProject}/stitch`, { method: "POST" }); toast("Stitched"); renderProject(); }
  catch (e) { toast(e.message, 6000); }
};
for (const preset of ["standard", "quality"]) {
  $(`#btn-final-${preset}`).onclick = async () => {
    if (!confirm(`Render a new ${preset} version of every shot (same prompts and seeds)?`)) return;
    try { const r = await api(`/api/projects/${currentProject}/finalize`, { method: "POST", body: JSON.stringify({ preset }) });
      toast(`${preset} pass queued`); openProject(r.project_id); } catch (e) { toast(e.message, 6000); }
  };
}
$("#btn-delete").onclick = async () => {
  if (!confirm("Delete this project and all its videos?")) return;
  await api(`/api/projects/${currentProject}`, { method: "DELETE" }); loadProjects();
};

// ---------------- workers
async function loadWorkers() {
  const [ws, qs] = await Promise.all([api("/api/workers"), api("/api/queue")]);
  const online = ws.filter((w) => w.status !== "offline").length;
  $("#worker-badge").textContent = `${online}/${ws.length}`;
  if ($("#tab-workers").hidden) return;
  $("#wk-rows").innerHTML = ws.length ? ws.map((w) => `<tr><td><b>${esc(w.id)}</b><div class="muted small">${esc(w.hostname)}</div></td>
    <td>${esc(w.gpu)}</td><td>${esc((w.presets || []).join(", "))}</td>
    <td class="s-${w.status === "offline" ? "offline" : w.status === "busy" ? "busy" : "online"}">${esc(w.status)}</td>
    <td class="small">${w.task_id ? esc(w.task_id) + "<div class='muted'>" + esc((w.progress || "").slice(0, 60)) + "</div>" : ""}</td>
    <td>${w.completed}</td><td class="muted">${fmtTime(w.last_seen_s)} ago</td></tr>`).join("")
    : `<tr><td colspan="7" class="muted">No workers registered yet.</td></tr>`;
  try {
    const d = await api("/api/dist/status");
    $("#dist-status").textContent = !d.ready ? "Preparing file list (hashing)…"
      : `${d.files} files ready to share · models ${d.gb.model || 0} GB` + (d.missing_models.length ? ` · still downloading here: ${d.missing_models.join(", ")}` : " · complete");
  } catch {}
  const c = qs.counts;
  $("#wk-queue").textContent = `Queue: ${c.ready || 0} ready · ${c.pending || 0} waiting on previous shot · ${c.claimed || 0} rendering · ${c.done || 0} done · ${c.failed || 0} failed`;
}

// ---------------- init
(async function init() {
  CFG = await api("/api/config");
  const opts = [...CFG.local_models].sort((a, b) => (b === CFG.local_model) - (a === CFG.local_model) || (b.startsWith("gemma") - a.startsWith("gemma")))
    .map((m) => `<option value="local|${esc(m)}">Local · ${esc(m)}</option>`);
  if (CFG.claude_available) opts.push(`<option value="claude|">Claude Opus 5.5 (API)</option>`);
  $("#engine").innerHTML = opts.join("") || `<option value="">none available</option>`;
  $("#btn-claude").disabled = !opts.length;
  $("#claude-hint").textContent = opts.length
    ? "Local models run fully on this PC via Ollama (unloaded right after writing, so rendering keeps the GPU)."
    : `No writer found: install Ollama and run “ollama pull ${CFG.local_model}”, or set ANTHROPIC_API_KEY. You can also copy the prompt into claude.ai and paste the JSON into step 2.`;
  $("#wk-token-file").textContent = CFG.token_file;
  let tab = "create"; try { tab = localStorage.getItem("tab") || "create"; } catch {}
  showTab(tab);
  pollTimer = setInterval(() => {
    if (!$("#tab-projects").hidden) currentProject ? renderProject() : loadProjects();
    loadWorkers().catch(() => {});
  }, 3000);
})();
