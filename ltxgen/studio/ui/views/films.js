// Films gallery + "New film" dialog.
import { api, esc, icon, fmtTime, modal, toast, $, hydrateIcons } from "../app.js";

const STAGE_LABEL = { story: "Story", cast: "Casting", shots: "Shot list", render: "Rendering", review: "Review", final: "Finished" };

export async function filmsView(root) {
  root.innerHTML = `<div class="page-head"><div><div class="eyebrow">Library</div><h1>Your films</h1>
    <p>Every film goes through the same six steps: story, cast, shots, render, review, final. Pick one up where you left it.</p></div>
    <button class="btn primary lg" id="new-film">${icon("plus")} New film</button></div>
    <div id="films" class="films-grid"><div class="empty"><div class="spinner" style="margin:0 auto"></div></div></div>`;
  $("#new-film").onclick = newFilm;
  let films = [];
  try { films = await api("/api/films"); } catch (e) { toast(e.message, "bad"); }
  const grid = $("#films");
  const cards = films.map((f) => {
    const sheet = false;
    const prog = f.stage === "render" && f.counts ? ` · ${(f.counts.done || 0)}/${Object.values(f.counts).reduce((a, b) => a + b, 0)}` : "";
    return `<a class="film-card" href="#/film/${encodeURIComponent(f.id)}">
      <div class="poster ${sheet ? "sheet" : ""}" style="${f.poster ? `background-image:url('${f.poster}')` : ""}">
        <span class="badge stage-badge ${f.stage}">${f.stage === "render" ? '<span class="dot run"></span>' : ""}${STAGE_LABEL[f.stage] || f.stage}${prog}</span>
        ${f.score ? `<span class="score">★ ${f.score}/10</span>` : ""}
        <span class="code">${esc(f.code || "")}</span>
        ${f.final ? `<div class="poster-play"><span><svg viewBox="0 0 24 24"><path d="M7 4v16l13-8z"/></svg></span></div>` : ""}
      </div>
      <div class="meta"><h3>${esc(f.title)}</h3><p>${esc(f.logline || "No story yet")}</p>
        <div class="stats"><span>${icon("clock", "sm")} <b>${fmtTime(f.bench?.duration_s || f.seconds)}</b></span>
          <span>${icon("clap", "sm")} <b>${f.shots}</b> shots</span>
          ${f.bench?.face_consistency != null ? `<span>${icon("users", "sm")} <b>${Math.round(f.bench.face_consistency * 100)}%</b></span>` : ""}</div></div></a>`;
  });
  grid.innerHTML = `<button class="film-card new-card" id="new-card"><div><div class="plus">${icon("plus")}</div>
      <h3>Start a new film</h3><p class="muted small" style="margin:4px 0 0">Describe a story, AI writes the shots</p></div></button>` + cards.join("");
  $("#new-card").onclick = newFilm;
  hydrateIcons(grid);
  return {};
}

export function newFilm() {
  const m = modal("New film", `<div class="stack">
    <label class="field">Title<input id="nf-title" type="text" placeholder="e.g. The Night Ferry" autofocus></label>
    <label class="field">What is it about? <small>A few sentences are enough - you can refine it in the Story step.</small>
      <textarea id="nf-log" rows="5" placeholder="On the last ferry across the Bosphorus, an old tea seller helps a Japanese backpacker find her lost passport..."></textarea></label>
    <div class="row"><label class="field grow">Aspect<div class="seg" id="nf-aspect">
      <button class="on" data-v="16:9">16:9 film</button><button data-v="9:16">9:16 vertical</button><button data-v="1:1">1:1</button></div></label>
      <label class="field grow">Length<div class="seg" id="nf-len"><button data-v="60">1 min</button><button data-v="120">2 min</button>
      <button class="on" data-v="180">3 min</button><button data-v="240">4 min</button></div></label></div>
    <div class="row end"><button class="btn" data-close2>Cancel</button><button class="btn primary" id="nf-go">${icon("sparkle")} Create film</button></div></div>`, { wide: false });
  for (const id of ["nf-aspect", "nf-len"]) {
    $(`#${id}`, m.el).onclick = (e) => { const b = e.target.closest("button"); if (!b) return; [...b.parentNode.children].forEach((x) => x.classList.toggle("on", x === b)); };
  }
  $("[data-close2]", m.el).onclick = m.close;
  $("#nf-go", m.el).onclick = async () => {
    const title = $("#nf-title", m.el).value.trim();
    if (!title) return toast("Give your film a title", "bad");
    try {
      const r = await api("/api/films", { body: { title, logline: $("#nf-log", m.el).value.trim(),
        aspect: $("#nf-aspect .on", m.el).dataset.v, length_s: +$("#nf-len .on", m.el).dataset.v } });
      m.close();
      location.hash = `#/film/${r.id}/story`;
    } catch (e) { toast(e.message, "bad"); }
  };
  hydrateIcons(m.el);
}
