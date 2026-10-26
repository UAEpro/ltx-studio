// Film workspace: header, 6-step pipeline, and one panel per step.
import { api, esc, icon, fmtTime, pct, toast, modal, confirmBox, $, $$, hydrateIcons, debounce, showJob, jobListeners } from "../app.js";

const STEPS = [
  { id: "story", name: "Story", icon: "edit" },
  { id: "cast", name: "Cast & places", icon: "users" },
  { id: "shots", name: "Shot list", icon: "clap" },
  { id: "render", name: "Render", icon: "zap" },
  { id: "review", name: "Review", icon: "eye" },
  { id: "final", name: "Final film", icon: "film" },
];
const LOOKS = [
  { name: "Cinematic warm", sw: "linear-gradient(90deg,#f5a524,#7c2d12)", style: "Warm, emotional cinematic drama, golden natural light, rich colours, soft shallow depth of field, gentle handheld camera, realistic 35mm film look." },
  { name: "Film noir", sw: "linear-gradient(90deg,#eee,#111)", style: "Black and white film noir, high-contrast chiaroscuro lighting, deep shadows, smoke in hard light beams, grainy 35mm black and white film look." },
  { name: "Documentary", sw: "linear-gradient(90deg,#6b8f71,#2f3e46)", style: "Naturalistic documentary look, available light, muted true-to-life colours, steady handheld camera, crisp detail, realistic." },
  { name: "Nordic cold", sw: "linear-gradient(90deg,#9ecbff,#1e3a5f)", style: "Quiet, cold Nordic drama, deep blue twilight, snow, warm amber window light against the cold, soft film grain, steady camera, realistic 35mm film look." },
  { name: "Vivid festival", sw: "linear-gradient(90deg,#ff4d6d,#ffb703)", style: "Warm, vivid saturated colours, candlelight and festive lights, soft film grain, intimate handheld camera, realistic 35mm film look." },
  { name: "Epic nature", sw: "linear-gradient(90deg,#2a9d8f,#e9c46a)", style: "Grand wildlife and nature cinematography, golden light, wide vistas, rich earthy colours, smooth aerial and steady telephoto shots, realistic." },
];
const LANGS = [["en", "English"], ["ar", "Arabic"], ["es", "Spanish"], ["fr", "French"], ["pt", "Portuguese"], ["it", "Italian"],
  ["de", "German"], ["tr", "Turkish"], ["ja", "Japanese"], ["hi", "Hindi"], ["no", "Norwegian"], ["ru", "Russian"], ["zh", "Mandarin"]];

export async function filmView(root, fid, startStep) {
  fid = decodeURIComponent(fid);
  let data = null, film = null, step = startStep, timer = null, saving = false, dirty = false, filter = "all";
  root.innerHTML = `<div id="fh"></div><div class="pipeline" id="pipe"></div><div id="panel"></div>`;

  const onJob = (j) => { if (j.film === fid) load(true); };
  jobListeners.add(onJob);

  async function load(refreshPanel = false) {
    try { data = await api(`/api/films/${encodeURIComponent(fid)}`); }
    catch (e) { root.innerHTML = `<div class="empty"><h3>Film not found</h3><p>${esc(e.message)}</p><a class="btn" href="#/films">Back to films</a></div>`; return; }
    if (!dirty || !film) film = structuredClone(data.film);
    if (!step) step = defaultStep();
    renderHead(); renderPipe();
    if (refreshPanel || ["render", "review", "final"].includes(step) || !$("#panel").children.length) renderPanel();
    schedule();
  }
  function schedule() {
    clearTimeout(timer);
    const active = data.stage === "render" || (data.jobs || []).some((j) => ["queued", "running"].includes(j.status));
    timer = setTimeout(() => load(false), active ? 4000 : 15000);
  }
  function defaultStep() {
    return { story: "story", cast: "cast", shots: "shots", render: "render", review: "review", final: "final" }[data.stage] || "story";
  }
  const jobRunning = (kind) => (data.jobs || []).find((j) => j.kind === kind && ["queued", "running"].includes(j.status));

  // ---------------------------------------------------------------- save
  const saveNow = async () => {
    saving = true; setSaved("Saving…");
    try {
      const r = await api(`/api/films/${encodeURIComponent(fid)}`, { method: "PUT", body: film });
      dirty = false; data.film = structuredClone(film); data.lint_all = r.lint; setSaved("Saved");
      renderPipe();
      if (step === "shots") $$(".shot-row").forEach((row) => updateRowLint(row));
    } catch (e) { setSaved("Not saved"); toast(e.message, "bad"); }
    saving = false;
  };
  const save = debounce(saveNow, 700);
  const touch = () => { dirty = true; setSaved("Editing…"); save(); };
  const setSaved = (t) => { const el = $("#saved"); if (el) el.textContent = t; };

  // ---------------------------------------------------------------- header
  function renderHead() {
    const total = (film.shots || []).reduce((a, s) => a + (+s.seconds || 6), 0);
    $("#fh").innerHTML = `<div class="film-head"><div>
      <a class="crumb" href="#/films">${icon("back", "sm")} All films</a>
      <h1><input id="title" value="${esc(film.title)}" aria-label="Film title"></h1>
      <div class="facts"><span class="chip">${esc(data.code)}</span><span class="chip">${esc(film.aspect || "16:9")}</span>
        <span class="chip">${icon("clap", "sm")} ${(film.shots || []).length} shots</span><span class="chip">${icon("clock", "sm")} ${fmtTime(total)}</span>
        ${film.dialogue_lang ? `<span class="chip">${esc((LANGS.find((l) => l[0] === film.dialogue_lang) || [0, film.dialogue_lang])[1])}</span>` : ""}
        ${data.score ? `<span class="chip on">★ ${data.score}/10</span>` : ""}<span class="chip ghost small muted" id="saved" style="background:transparent;border-color:transparent">${dirty ? "Editing…" : ""}</span></div>
      </div><div class="row">${nextAction()}</div></div>`;
    $("#title").oninput = (e) => { film.title = e.target.value; touch(); };
    const na = $("#next-act");
    if (na) na.onclick = () => go(na.dataset.step);
    hydrateIcons($("#fh"));
  }
  function nextAction() {
    const map = { story: ["Write the script", "story"], cast: ["Create the cast", "cast"], shots: ["Check & render", "shots"],
      render: ["Watch progress", "render"], review: ["Review shots", "review"], final: ["Watch the film", "final"] };
    const [label, s] = map[data.stage] || map.story;
    return s === step ? "" : `<button class="btn primary" id="next-act" data-step="${s}">${icon("play", "sm")} ${label}</button>`;
  }

  // ---------------------------------------------------------------- pipeline
  function stepState(id) {
    const shots = data.shots_detail || [];
    const counts = data.counts || {};
    const total = Object.values(counts).reduce((a, b) => a + b, 0);
    const done = counts.done || 0;
    const lint = (data.lint_all || []).length;
    switch (id) {
      case "story": return film.shots?.length ? ["done", `${film.shots.length} shots written`, 1] : ["attention", "Describe your story", 0];
      case "cast": {
        if (jobRunning("assets")) return ["running", "Casting on the GPU…", null];
        const items = [...data.cast, ...data.locations, ...data.props];
        if (!items.length) return [film.shots?.length ? "attention" : "", film.shots?.length ? "No cast yet" : "After the story", 0];
        const ready = items.filter((x) => x.ready).length;
        return ready === items.length ? ["done", `${data.cast.length} cast · ${data.locations.length} places`, 1]
          : ["attention", `${ready}/${items.length} ready`, ready / items.length];
      }
      case "shots":
        if (jobRunning("submit")) return ["running", "Submitting…", null];
        if (data.project_id) return ["done", "Sent to render", 1];
        if (!film.shots?.length) return ["", "After the story", 0];
        return lint ? ["attention", `${lint} warning${lint > 1 ? "s" : ""}`, 0] : ["attention", "Ready to render", 0];
      case "render":
        if (!total) return ["", "Not started", 0];
        return done === total ? ["done", `${total} shots rendered`, 1] : ["running", `${done}/${total} · ${counts.claimed ? "rendering" : "queued"}`, done / total];
      case "review": {
        if (!total) return ["", "After rendering", 0];
        const acc = shots.filter((s) => s.accepted).length;
        const flagged = shots.filter((s) => s.qa?.flags?.length && !s.accepted).length;
        return acc === shots.length ? ["done", "All shots approved", 1]
          : [done ? "attention" : "", `${acc}/${shots.length} approved${flagged ? ` · ${flagged} flagged` : ""}`, acc / shots.length];
      }
      case "final": return data.final ? ["done", fmtTime(data.bench?.duration_s || data.project?.final_info?.duration_s), 1] : ["", "After review", 0];
    }
  }
  function renderPipe() {
    $("#pipe").innerHTML = STEPS.map((s, i) => {
      const [cls, label, p] = stepState(s.id);
      return `<button class="step ${cls} ${s.id === step ? "active" : ""}" data-step="${s.id}">
        <div class="top"><span class="num">${cls === "done" ? icon("check", "sm") : i + 1}</span><span class="name">${s.name}</span></div>
        <div class="state">${esc(label)}</div><div class="bar ${cls === "done" ? "ok" : ""}"><span style="width:${p == null ? 35 : Math.round(p * 100)}%"></span></div></button>`;
    }).join("");
    $$(".step", $("#pipe")).forEach((b) => (b.onclick = () => go(b.dataset.step)));
    hydrateIcons($("#pipe"));
  }
  function go(s) {
    if (!s || s === step) return;
    step = s;
    history.replaceState(null, "", `#/film/${encodeURIComponent(fid)}/${s}`);
    renderPipe(); renderHead(); renderPanel();
  }

  // ---------------------------------------------------------------- panels
  function renderPanel() {
    const p = $("#panel");
    const y = window.scrollY;
    ({ story: storyPanel, cast: castPanel, shots: shotsPanel, render: renderPanelView, review: reviewPanel, final: finalPanel })[step](p);
    hydrateIcons(p);
    window.scrollTo(0, y);
  }

  async function runJob(kind, body = {}, title) {
    try {
      if (dirty) await saveNow();
      const j = await api(`/api/films/${encodeURIComponent(fid)}/jobs/${kind}`, { body: { ...body, title } });
      toast(`${title || kind} started`);
      data.jobs = [j, ...(data.jobs || [])];
      renderPipe(); renderPanel(); schedule();
      return j;
    } catch (e) { toast(e.message, "bad"); }
  }

  // ---- 1 story
  function storyPanel(p) {
    const lenS = film.length_s || 180;
    p.innerHTML = `<div class="panel-head"><div><h2>Story</h2><p>Write the idea in plain words. The AI writer turns it into characters, places and a full shot list following the rules that keep films consistent.</p></div></div>
    <div class="story-grid">
      <div class="card stack">
        <label class="field">The story <small>Who, where, what happens, how it ends. Real places and cultures work best.</small>
          <textarea class="brief" id="brief" placeholder="Who, where, what happens and how it ends…">${esc(film.logline || "")}</textarea></label>
        <div class="row between"><div class="row">
          <label class="field" style="min-width:200px">Writer<select id="engine"><option value="">Best available</option><option value="claude">Claude (API)</option><option value="local">Local model (Ollama)</option></select></label></div>
          <button class="btn primary lg" id="write">${icon("sparkle")} ${film.shots?.length ? "Rewrite the script" : "Write the script"}</button></div>
        ${film.shots?.length ? `<div class="note warn">${icon("alert", "sm")}<div>Rewriting replaces the current cast, places and ${film.shots.length} shots (a backup is kept).</div></div>` : ""}
        <details class="adv"><summary>Edit the raw film file (advanced)</summary><p class="small muted">Paste or tweak the complete film.json.</p>
          <textarea id="rawjson" rows="14" class="mono">${esc(JSON.stringify(film, null, 2))}</textarea>
          <div class="row end mt"><button class="btn sm" id="saveraw">Save film file</button></div></details>
      </div>
      <div class="stack">
        <div class="card stack"><div class="card-head" style="margin:0"><h3>Format</h3></div>
          <label class="field">Aspect<div class="seg" id="aspect">${["16:9", "9:16", "1:1"].map((a) => `<button data-v="${a}" class="${(film.aspect || "16:9") === a ? "on" : ""}">${a}</button>`).join("")}</div></label>
          <label class="field">Length · <b id="lenv">${fmtTime(lenS)}</b><input type="range" id="len" min="60" max="300" step="30" value="${lenS}"></label>
          <label class="field">Dialogue language<select id="lang">${LANGS.map(([c, n]) => `<option value="${c}" ${film.dialogue_lang === c ? "selected" : ""}>${n}</option>`).join("")}</select></label>
        </div>
        <div class="card stack"><div class="card-head" style="margin:0"><h3>Look</h3><span class="small muted">applies to every shot</span></div>
          <div class="preset-looks">${LOOKS.map((l, i) => `<button class="look ${film.style === l.style ? "on" : ""}" data-i="${i}"><div class="sw" style="background:${l.sw}"></div><b>${l.name}</b></button>`).join("")}</div>
          <label class="field">Style line<textarea id="style" rows="3">${esc(film.style || "")}</textarea></label></div>
      </div></div>`;
    $("#brief").oninput = (e) => { film.logline = e.target.value; touch(); };
    $("#style").oninput = (e) => { film.style = e.target.value; touch(); $$(".look").forEach((b) => b.classList.remove("on")); };
    $("#lang").onchange = (e) => { film.dialogue_lang = e.target.value; touch(); renderHead(); };
    $("#len").oninput = (e) => { film.length_s = +e.target.value; $("#lenv").textContent = fmtTime(film.length_s); touch(); };
    $("#aspect").onclick = (e) => { const b = e.target.closest("button"); if (!b) return; film.aspect = b.dataset.v; $$("#aspect button").forEach((x) => x.classList.toggle("on", x === b)); touch(); renderHead(); };
    $$(".look").forEach((b) => (b.onclick = () => { film.style = LOOKS[b.dataset.i].style; $("#style").value = film.style; $$(".look").forEach((x) => x.classList.toggle("on", x === b)); touch(); }));
    $("#saveraw").onclick = async () => {
      try { film = JSON.parse($("#rawjson").value); await saveNow(); toast("Film file saved", "ok"); load(true); } catch (e) { toast(`Invalid JSON: ${e.message}`, "bad"); }
    };
    $("#write").onclick = async () => {
      if (!film.logline?.trim()) return toast("Write the story first", "bad");
      if (film.shots?.length && !(await confirmBox("Rewrite the script?", "This replaces the cast, places and all shots. The current version is backed up as film.json.bak.", "Rewrite"))) return;
      const b = $("#write"); b.disabled = true; b.innerHTML = `<span class="spinner"></span> Writing… (about a minute)`;
      try {
        if (dirty) await saveNow();
        const r = await api(`/api/films/${encodeURIComponent(fid)}/write`, { body: { brief: film.logline, engine: $("#engine").value || undefined } });
        toast(`Script written by ${r.engine}: ${r.shots} shots`, "ok");
        dirty = false; film = null; await load(true); go("cast");
      } catch (e) { toast(e.message, "bad"); b.disabled = false; b.innerHTML = `${icon("sparkle")} Write the script`; hydrateIcons(b); }
    };
  }

  // ---- 2 cast
  function castPanel(p) {
    const job = jobRunning("assets");
    const byName = Object.fromEntries((data.cast || []).map((c) => [c.name, c]));
    const byLoc = Object.fromEntries((data.locations || []).map((c) => [c.key, c]));
    const byProp = Object.fromEntries((data.props || []).map((c) => [c.key, c]));
    const castLint = (data.lint_all || []).filter((m) => m.startsWith("cast "));
    p.innerHTML = `<div class="panel-head"><div><h2>Cast & places</h2><p>Each character gets a portrait and a turnaround; places and props get one image. These become the reference sheets that keep every shot consistent.</p></div>
      <div class="row">${data.assets_review ? `<a class="btn" href="${data.assets_review}" target="_blank">${icon("image", "sm")} Full review image</a>` : ""}
      <button class="btn primary" id="cast-go" ${job ? "disabled" : ""}>${job ? `<span class="spinner"></span> Casting… ${job.status === "queued" ? "(waiting for GPU)" : ""}` : `${icon("users", "sm")} Create cast & places`}</button></div></div>
      ${job ? `<div class="note" style="margin-bottom:14px">${icon("info", "sm")}<div>Rendering portraits and turnarounds - about 6 minutes per character, 2 per place. <a href="#" id="see-log" style="text-decoration:underline">See live log</a></div></div>` : ""}
      ${castLint.length ? `<div class="note warn" style="margin-bottom:14px">${icon("alert", "sm")}<div>${castLint.map(esc).join("<br>")}</div></div>` : ""}
      <div class="card-head"><h3>${icon("users", "sm")} Characters</h3><button class="btn sm" id="add-cast">${icon("plus", "sm")} Add character</button></div>
      <div class="cast-grid">${Object.entries(film.cast || {}).map(([n, c]) => personCard(n, c, byName[n])).join("") || `<div class="empty">No characters yet - write the script or add one.</div>`}</div>
      <div class="card-head mt-lg"><h3>${icon("map", "sm")} Places</h3><button class="btn sm" id="add-loc">${icon("plus", "sm")} Add place</button></div>
      <div class="cast-grid">${Object.entries(film.locations || {}).map(([k, l]) => placeCard("loc", k, l, byLoc[k])).join("") || `<div class="empty">No places yet.</div>`}</div>
      <div class="card-head mt-lg"><h3>${icon("box", "sm")} Props <span class="muted small">objects that must look identical in every shot</span></h3><button class="btn sm" id="add-prop">${icon("plus", "sm")} Add prop</button></div>
      <div class="cast-grid">${Object.entries(film.props || {}).map(([k, l]) => placeCard("prop", k, l, byProp[k])).join("") || `<div class="empty">No props.</div>`}</div>`;
    $("#cast-go").onclick = () => runJob("assets", {}, "create cast & places");
    if ($("#see-log")) $("#see-log").onclick = (e) => { e.preventDefault(); showJob(job.id); };
    $("#add-cast").onclick = () => { const n = uniqueKey("NEW", film.cast); film.cast[n] = { role: "the new character", desc: "" }; touch(); castPanel(p); hydrateIcons(p); };
    $("#add-loc").onclick = () => { const k = uniqueKey("PLACE", film.locations); film.locations[k] = { desc: "", prompt: "", in_sheet: false }; touch(); castPanel(p); hydrateIcons(p); };
    $("#add-prop").onclick = () => { film.props = film.props || {}; const k = uniqueKey("PROP", film.props); film.props[k] = { desc: "" }; touch(); castPanel(p); hydrateIcons(p); };
    bindCastEditors(p);
  }
  function uniqueKey(base, obj) { let k = base, i = 2; while (obj && obj[k]) k = base + i++; return k; }
  function personCard(n, c, info) {
    const img = info?.panel ? `background-image:url('${info.panel}')` : info?.portrait ? `background-image:url('${info.portrait}')` : "";
    return `<div class="card person" data-cast="${esc(n)}">
      <div><div class="ph ${info?.panel ? "panel-crop" : ""}" style="${img}">${img ? "" : "not cast yet"}
        <span class="state">${info?.ready ? '<span class="chip ok">cast</span>' : '<span class="chip">pending</span>'}</span></div></div>
      <div class="stack" style="gap:8px">
        <div class="row" style="gap:6px"><input class="name-input grow" data-k="name" value="${esc(n)}" title="Name used in shot prompts">
          <button class="btn icon sm ghost" data-recast title="Recast (new portrait)">${icon("refresh", "sm")}</button>
          <button class="btn icon sm ghost danger" data-del title="Remove">${icon("trash", "sm")}</button></div>
        <input data-k="role" value="${esc(c.role || "")}" placeholder="role, e.g. the fisherman">
        <textarea data-k="desc" rows="4" placeholder="age, face, hair, full outfit head to toe">${esc(c.desc || "")}</textarea>
        <label class="row small muted" style="gap:8px"><span class="toggle"><input type="checkbox" data-k="animal" ${c.animal ? "checked" : ""}><span></span></span> Animal (natural pose)</label>
      </div>
      ${info?.panel ? `<img class="panel-strip" src="${info.panel}" style="grid-column:1/-1" alt="turnaround">` : ""}</div>`;
  }
  function placeCard(kind, k, l, info) {
    return `<div class="card place" data-${kind}="${esc(k)}">
      <div class="img" style="${info?.image ? `background-image:url('${info.image}')` : ""}">${info?.image ? "" : "no image yet"}</div>
      <div class="row" style="gap:6px"><input class="name-input grow" data-k="key" value="${esc(k)}">
        <button class="btn icon sm ghost" data-recast title="New image">${icon("refresh", "sm")}</button>
        <button class="btn icon sm ghost danger" data-del title="Remove">${icon("trash", "sm")}</button></div>
      <input data-k="desc" value="${esc(l.desc || "")}" placeholder="short description">
      ${kind === "loc" ? `<textarea data-k="prompt" rows="3" placeholder="image prompt for an empty establishing still">${esc(l.prompt || "")}</textarea>`
        : `<input data-k="role" value="${esc(l.role || "")}" placeholder="how prompts call it, e.g. the red passport">`}</div>`;
  }
  function bindCastEditors(p) {
    $$("[data-cast]", p).forEach((card) => {
      const n = card.dataset.cast;
      $$("[data-k]", card).forEach((inp) => {
        inp.addEventListener(inp.type === "checkbox" ? "change" : "input", () => {
          const k = inp.dataset.k;
          if (k === "name") {
            const nn = inp.value.toUpperCase().replace(/[^A-Z0-9_]/g, "");
            if (!nn || nn === n || film.cast[nn]) return;
            film.cast = Object.fromEntries(Object.entries(film.cast).map(([a, b]) => [a === card.dataset.cast ? nn : a, b]));
            film.shots.forEach((s) => { s.prompt = s.prompt.replace(new RegExp(`\\b${card.dataset.cast}\\b`, "g"), nn); s.cast = (s.cast || []).map((c) => (c === card.dataset.cast ? nn : c)); });
            card.dataset.cast = nn;
          } else if (k === "animal") film.cast[card.dataset.cast].animal = inp.checked;
          else film.cast[card.dataset.cast][k] = inp.value;
          touch();
        });
      });
      $("[data-del]", card).onclick = async () => { if (!(await confirmBox("Remove character?", `Remove ${card.dataset.cast} from the film?`, "Remove"))) return; delete film.cast[card.dataset.cast]; touch(); castPanel(p); hydrateIcons(p); };
      $("[data-recast]", card).onclick = async () => { await runJob("recast", { args: [card.dataset.cast] }, `recast ${card.dataset.cast}`); toast("Now press Create cast & places to render the new portrait"); };
    });
    for (const kind of ["loc", "prop"]) {
      const coll = kind === "loc" ? "locations" : "props";
      $$(`[data-${kind}]`, p).forEach((card) => {
        $$("[data-k]", card).forEach((inp) => inp.addEventListener("input", () => {
          const key = card.dataset[kind], k = inp.dataset.k;
          if (k === "key") {
            const nk = inp.value.toUpperCase().replace(/[^A-Z0-9_]/g, "");
            if (!nk || nk === key || film[coll][nk]) return;
            film[coll] = Object.fromEntries(Object.entries(film[coll]).map(([a, b]) => [a === key ? nk : a, b]));
            film.shots.forEach((s) => { if (kind === "loc" && s.location === key) s.location = nk; if (kind === "prop") s.prompt = s.prompt.replace(new RegExp(`\\b${key}\\b`, "g"), nk); });
            card.dataset[kind] = nk;
          } else film[coll][key][k] = inp.value;
          touch();
        }));
        $("[data-del]", card).onclick = () => { delete film[coll][card.dataset[kind]]; touch(); castPanel(p); hydrateIcons(p); };
        $("[data-recast]", card).onclick = () => runJob("recast", { args: [`${kind}:${card.dataset[kind]}`] }, `new image ${card.dataset[kind]}`);
      });
    }
  }

  // ---- 3 shots
  function shotsPanel(p) {
    const total = (film.shots || []).reduce((a, s) => a + (+s.seconds || 6), 0);
    const general = (data.lint_all || []).filter((m) => !/^s\d+[a-z]?:/.test(m));
    const submitted = !!data.project_id;
    const job = jobRunning("submit");
    p.innerHTML = `<div class="panel-head"><div><h2>Shot list</h2><p>One action per shot, 5-8 seconds each. Write <b>NAMES</b> of characters and <b>PROP</b> keys in capitals - they become reference sheets automatically.</p></div>
      <div class="row"><span class="chip">${(film.shots || []).length} shots · ${fmtTime(total)}</span>
        ${submitted ? `<a class="btn" href="#/film/${encodeURIComponent(fid)}/render">${icon("zap", "sm")} See rendering</a>`
          : `<button class="btn primary" id="submit" ${job ? "disabled" : ""}>${job ? '<span class="spinner"></span> Sending…' : `${icon("play", "sm")} Start rendering`}</button>`}</div></div>
      ${submitted ? `<div class="note" style="margin-bottom:14px">${icon("info", "sm")}<div>This film is already rendering. Edits here are used when you redo a shot from <b>Review</b> (Edit &amp; redo).</div></div>` : ""}
      ${general.length ? `<div class="note warn" style="margin-bottom:14px">${icon("alert", "sm")}<div>${general.map(esc).join("<br>")}</div></div>` : ""}
      <div class="shot-list" id="shots">${(film.shots || []).map((s, i) => shotRow(s, i)).join("")}</div>
      <div class="row mt"><button class="btn" id="add-shot">${icon("plus", "sm")} Add shot</button></div>`;
    if ($("#submit")) $("#submit").onclick = async () => {
      if (dirty) await saveNow();
      const lint = data.lint_all || [];
      if (lint.length && !(await confirmBox("Render with warnings?", `${lint.length} warning(s) found. They usually cause bad shots:<ul style="text-align:left">${lint.slice(0, 6).map((m) => `<li>${esc(m)}</li>`).join("")}</ul>`, "Render anyway"))) return;
      const missing = [...data.cast, ...data.locations, ...data.props].filter((x) => !x.ready).length;
      if (missing && !(await confirmBox("Cast not ready", `${missing} character/place image(s) are missing. Rendering will create them first (takes longer).`, "Continue"))) return;
      runJob("submit", { force: lint.length > 0 }, "start rendering");
    };
    $("#add-shot").onclick = () => {
      const last = film.shots[film.shots.length - 1];
      film.shots.push({ id: nextShotId(), seconds: 7, cast: [], location: last?.location || Object.keys(film.locations || {})[0], prompt: "" });
      touch(); shotsPanel(p); hydrateIcons(p);
    };
    $$(".shot-row", p).forEach((row) => bindShotRow(row, p));
  }
  function nextShotId() { let n = film.shots.length + 1, id; do { id = `s${String(n++).padStart(2, "0")}`; } while (film.shots.some((s) => s.id === id)); return id; }
  function shotRow(s, i) {
    const lint = (data.lint_all || []).filter((m) => m.startsWith(s.id + ":"));
    return `<div class="shot-row ${lint.length ? "lint" : ""}" data-i="${i}">
      <div class="sid">${esc(s.id)}</div>
      <div class="side">
        <label class="small muted">Seconds<input type="number" min="3" max="10" step="1" data-k="seconds" value="${esc(s.seconds || 7)}"></label>
        <label class="small muted">Place<select data-k="location">${Object.keys(film.locations || {}).map((k) => `<option ${s.location === k ? "selected" : ""}>${esc(k)}</option>`).join("")}<option value="" ${!s.location ? "selected" : ""}>-</option></select></label>
        <div class="cast-toggle">${Object.keys(film.cast || {}).map((n) => `<button class="chip ${(s.cast || []).includes(n) ? "on" : ""}" data-castt="${esc(n)}">${esc(n)}</button>`).join("")}</div>
      </div>
      <div><textarea data-k="prompt" placeholder="What happens, camera, light. Audio: sounds, music, dialogue in quotes.">${esc(s.prompt || "")}</textarea>
        <input data-k="setting" value="${esc(s.setting || "")}" placeholder="Setting override (optional): e.g. the same kitchen at night" style="margin-top:6px;font-size:12.5px">
        <div class="lint-box">${lint.map((m) => `<div class="lint-line">${icon("alert", "sm")} ${esc(m.slice(s.id.length + 1))}</div>`).join("")}</div></div>
      <div class="tools"><button class="btn icon sm ghost" data-mv="-1" title="Move up">${icon("up", "sm")}</button>
        <button class="btn icon sm ghost" data-mv="1" title="Move down">${icon("down", "sm")}</button>
        <button class="btn icon sm ghost danger" data-del title="Delete">${icon("trash", "sm")}</button></div></div>`;
  }
  function bindShotRow(row, p) {
    const i = +row.dataset.i, s = film.shots[i];
    $$("[data-k]", row).forEach((inp) => inp.addEventListener(inp.tagName === "SELECT" ? "change" : "input", () => {
      const k = inp.dataset.k;
      if (k === "seconds") s.seconds = +inp.value;
      else if (k === "setting") { if (inp.value.trim()) s.setting = inp.value; else delete s.setting; }
      else s[k] = inp.value;
      touch();
    }));
    $$("[data-castt]", row).forEach((b) => (b.onclick = () => {
      s.cast = s.cast || [];
      const n = b.dataset.castt;
      s.cast = s.cast.includes(n) ? s.cast.filter((x) => x !== n) : [...s.cast, n];
      b.classList.toggle("on"); touch();
    }));
    $$("[data-mv]", row).forEach((b) => (b.onclick = () => {
      const j = i + +b.dataset.mv; if (j < 0 || j >= film.shots.length) return;
      [film.shots[i], film.shots[j]] = [film.shots[j], film.shots[i]]; touch(); shotsPanel(p); hydrateIcons(p);
    }));
    $("[data-del]", row).onclick = async () => { if (!(await confirmBox("Delete shot?", `Delete ${s.id}?`, "Delete"))) return; film.shots.splice(i, 1); touch(); shotsPanel(p); hydrateIcons(p); };
  }
  function updateRowLint(row) {
    const s = film.shots[+row.dataset.i]; if (!s) return;
    const lint = (data.lint_all || []).filter((m) => m.startsWith(s.id + ":"));
    row.classList.toggle("lint", lint.length > 0);
    $(".lint-box", row).innerHTML = lint.map((m) => `<div class="lint-line">${icon("alert", "sm")} ${esc(m.slice(s.id.length + 1))}</div>`).join("");
  }

  // ---- 4 render
  function renderPanelView(p) {
    const shots = data.shots_detail || [];
    if (!data.project_id) {
      p.innerHTML = `<div class="empty"><h3>Not rendering yet</h3><p>Finish the shot list and press Start rendering.</p><a class="btn primary" href="#/film/${encodeURIComponent(fid)}/shots">Go to shot list</a></div>`;
      return;
    }
    const done = shots.filter((s) => s.status === "done").length;
    const running = shots.filter((s) => s.status === "claimed");
    const times = shots.map((s) => s.elapsed_s).filter(Boolean);
    const avg = times.length ? times.reduce((a, b) => a + b, 0) / times.length : 270;
    const left = shots.length - done;
    const redos = shots.reduce((a, s) => a + Math.max(0, (s.versions?.length || 1) - 1), 0);
    const flagged = shots.filter((s) => s.qa?.flags?.length).length;
    const frac = shots.length ? done / shots.length : 0;
    const R = 82, C = 2 * Math.PI * R;
    p.innerHTML = `<div class="card render-hero">
        <div class="ring"><svg width="190" height="190"><circle cx="95" cy="95" r="${R}" stroke="rgba(255,255,255,.07)" stroke-width="12" fill="none"/>
          <circle cx="95" cy="95" r="${R}" stroke="url(#g)" stroke-width="12" fill="none" stroke-linecap="round" stroke-dasharray="${C}" stroke-dashoffset="${C * (1 - frac)}"/>
          <defs><linearGradient id="g"><stop offset="0" stop-color="#f5a524"/><stop offset="1" stop-color="#ff7a45"/></linearGradient></defs></svg>
          <div class="ring-label"><div><b>${Math.round(frac * 100)}%</b><span>${done} of ${shots.length} shots</span></div></div></div>
        <div class="stack"><div><h2>${left ? "Rendering your film" : "All shots rendered"}</h2>
          <p class="muted" style="margin:4px 0 0">${left ? `About <b>${fmtTime(avg * left)}</b> left at ${fmtTime(avg)} per shot. Quality checks run automatically every 4 shots and leaked shots are redone.` : "Head to Review to approve every shot."}</p></div>
          <div class="kpis"><div class="kpi"><div class="v">${done}/${shots.length}</div><div class="l">shots done</div></div>
            <div class="kpi"><div class="v">${running.length ? esc(running.map((s) => s.id).join(", ")) : "–"}</div><div class="l">rendering now</div></div>
            <div class="kpi"><div class="v">${redos}</div><div class="l">redos so far</div></div>
            <div class="kpi"><div class="v" style="color:${flagged ? "var(--warn)" : "inherit"}">${flagged}</div><div class="l">flagged by QA</div></div></div>
          ${running[0]?.progress ? `<div class="small muted">${esc(running[0].id)}: ${esc(running[0].progress)}</div>` : ""}
          ${!left ? `<div><a class="btn primary" href="#/film/${encodeURIComponent(fid)}/review">${icon("eye", "sm")} Review shots</a></div>` : ""}</div></div>
      <div class="card-head mt"><h3>Shots</h3><span class="small muted">click a finished shot to watch it</span></div>
      <div class="tiles">${shots.map((s) => `<div class="tile ${s.status === "done" ? "" : s.status === "claimed" ? "running" : "queued"}" data-sid="${esc(s.id)}" style="${s.thumb ? `background-image:url('${s.thumb}')` : ""}">
        ${s.qa?.flags?.length ? `<span class="flag chip bad">${esc(s.qa.flags[0].split(":")[0])}</span>` : ""}
        <div class="lbl"><span>${esc(s.id)}${s.version > 1 ? ` · v${s.version}` : ""}</span><span>${s.status === "done" ? icon("check", "sm") : s.status === "claimed" ? '<span class="spinner" style="width:12px;height:12px"></span>' : s.status === "failed" ? "failed" : ""}</span></div></div>`).join("")}</div>
      ${jobsCard()}`;
    $$(".tile", p).forEach((t) => (t.onclick = () => { const s = shots.find((x) => x.id === t.dataset.sid); if (s?.video) openShot(s.id); }));
    bindJobs(p);
  }
  function jobsCard() {
    const jobs = data.jobs || [];
    if (!jobs.length) return "";
    return `<div class="card mt"><div class="card-head"><h3>${icon("terminal", "sm")} Activity</h3></div>
      ${jobs.slice(0, 6).map((j) => `<div class="row between" style="padding:7px 0;border-top:1px solid var(--line)">
        <div class="row" style="gap:9px">${["queued", "running"].includes(j.status) ? '<span class="spinner"></span>' : j.status === "done" ? `<span style="color:var(--ok)">${icon("check", "sm")}</span>` : `<span style="color:var(--bad)">${icon("alert", "sm")}</span>`}
          <span>${esc(j.title)}</span>${j.error ? `<span class="small muted">${esc(j.error.slice(0, 90))}</span>` : ""}</div>
        <button class="btn sm ghost" data-joblog="${j.id}">log</button></div>`).join("")}</div>`;
  }
  function bindJobs(p) { $$("[data-joblog]", p).forEach((b) => (b.onclick = () => showJob(b.dataset.joblog))); }

  // ---- 5 review
  function reviewPanel(p) {
    const shots = (data.shots_detail || []).filter((s) => s.status);
    if (!shots.length) {
      p.innerHTML = `<div class="empty"><h3>Nothing to review yet</h3><p>Shots appear here as soon as they render.</p></div>`;
      return;
    }
    const flaggedN = shots.filter((s) => s.qa?.flags?.length && !s.accepted).length;
    const todo = shots.filter((s) => s.status === "done" && !s.accepted).length;
    const list = shots.filter((s) => filter === "all" || (filter === "todo" && s.status === "done" && !s.accepted)
      || (filter === "flagged" && s.qa?.flags?.length) || (filter === "accepted" && s.accepted));
    p.innerHTML = `<div class="panel-head"><div><h2>Review</h2><p>Watch every shot. Approve the good ones; for the rest, take a new attempt, render without the reference sheet, edit the prompt, or trim a bad start. Shortcut in the player: <span class="kbd">A</span> approve, <span class="kbd">R</span> new take, <span class="kbd">←</span><span class="kbd">→</span> next.</p></div>
      <div class="row"><div class="seg" id="filter">
        <button data-f="all" class="${filter === "all" ? "on" : ""}">All ${shots.length}</button>
        <button data-f="todo" class="${filter === "todo" ? "on" : ""}">To review ${todo}</button>
        <button data-f="flagged" class="${filter === "flagged" ? "on" : ""}">Flagged ${flaggedN}</button>
        <button data-f="accepted" class="${filter === "accepted" ? "on" : ""}">Approved ${shots.filter((s) => s.accepted).length}</button></div>
        <button class="btn" id="acc-clean">${icon("check", "sm")} Approve all unflagged</button></div></div>
      <div class="review-grid">${list.map(reviewCard).join("") || `<div class="empty">Nothing in this filter.</div>`}</div>`;
    $("#filter").onclick = (e) => { const b = e.target.closest("button"); if (!b) return; filter = b.dataset.f; reviewPanel(p); hydrateIcons(p); };
    $("#acc-clean").onclick = async () => {
      const ok = shots.filter((s) => s.status === "done" && !s.accepted && !s.qa?.flags?.length);
      for (const s of ok) await api(`/api/films/${encodeURIComponent(fid)}/shots/${s.id}/accept`, { body: {} });
      toast(`${ok.length} shots approved`, "ok"); load(true);
    };
    $$(".rcard", p).forEach((c) => {
      const sid = c.dataset.sid;
      const th = $(".thumb", c);
      th.onclick = () => openShot(sid);
      th.onmouseenter = () => { const s = shots.find((x) => x.id === sid); if (!s?.video || $("video", th)) return; th.insertAdjacentHTML("beforeend", `<video src="${s.video}" muted autoplay loop playsinline></video>`); };
      th.onmouseleave = () => { const v = $("video", th); if (v) v.remove(); };
      const acc = $("[data-acc]", c); if (acc) acc.onclick = () => accept(sid, c.classList.contains("accepted"));
      const rd = $("[data-redo]", c); if (rd) rd.onclick = () => redo(sid, "new_seed");
      $("[data-more]", c).onclick = () => openShot(sid);
    });
  }
  function reviewCard(s) {
    const flags = s.qa?.flags || [];
    const flagChips = flags.map((f) => `<span class="chip bad">${esc(f.replace("face:", "face ").replace(/F\d+_/, ""))}</span>`).join("");
    const st = s.status !== "done" ? `<span class="chip ${s.status === "claimed" ? "warn" : ""}">${s.status === "claimed" ? "rendering" : s.status}</span>` : "";
    return `<div class="rcard ${s.accepted ? "accepted" : ""} ${flags.length && !s.accepted ? "flagged" : ""}" data-sid="${esc(s.id)}">
      <div class="thumb" style="${s.thumb ? `background-image:url('${s.thumb}')` : ""}"><div class="corner"><span class="chip">${esc(s.id)}</span>${s.accepted ? `<span class="chip ok">${icon("check", "sm")} approved</span>` : ""}${st}${s.trim ? `<span class="chip info">trim ${s.trim}s</span>` : ""}</div>
        ${s.version ? `<span class="ver chip">v${s.version}${s.versions?.length > 1 ? ` of ${s.versions.length}` : ""}</span>` : ""}</div>
      <div class="body">${flagChips ? `<div class="chips">${flagChips}</div>` : ""}<p>${esc(s.prompt)}</p>
        <div class="acts">${s.status === "done" ? `<button class="btn sm ${s.accepted ? "" : "ok"}" data-acc>${icon(s.accepted ? "x" : "check", "sm")} ${s.accepted ? "Unapprove" : "Approve"}</button>
          <button class="btn sm" data-redo>${icon("refresh", "sm")} New take</button>` : ""}<button class="btn sm icon" data-more title="Open">${icon("edit", "sm")}</button></div></div></div>`;
  }
  async function accept(sid, undo = false) {
    try { await api(`/api/films/${encodeURIComponent(fid)}/shots/${sid}/accept`, { body: { undo } }); await load(true); } catch (e) { toast(e.message, "bad"); }
  }
  async function redo(sid, mode) {
    try {
      if (dirty) await saveNow();
      await api(`/api/films/${encodeURIComponent(fid)}/shots/${sid}/redo`, { body: { mode } });
      toast(`${sid}: ${mode === "text_only" ? "rendering without reference sheet" : mode === "edited" ? "rendering your edit" : "new take queued"}`, "ok");
      setTimeout(() => load(true), 1500);
    } catch (e) { toast(e.message, "bad"); }
  }

  function openShot(sid) {
    const shots = (data.shots_detail || []).filter((s) => s.status);
    let idx = shots.findIndex((s) => s.id === sid);
    const draw = () => {
      const s = shots[idx];
      const fs = film.shots.find((x) => x.id === s.id) || {};
      const flags = s.qa?.flags || [];
      m.el.querySelector(".mhead h2").textContent = `Shot ${s.id}${s.version ? ` · take ${s.version}` : ""}`;
      $(".mbody", m.el).innerHTML = `<div class="shot-modal"><div>
          ${s.video ? `<video src="${s.video}" controls autoplay loop playsinline></video>` : `<div class="empty">Not rendered yet (${esc(s.status || "")})</div>`}
          ${s.contact ? `<img class="contact" src="${s.contact}" alt="contact sheet" title="click to expand" onclick="this.classList.toggle('open')">` : ""}
          ${s.versions?.length > 1 ? `<div class="mt"><div class="eyebrow" style="margin-bottom:6px">Takes</div><div class="versions">${s.versions.map((v) => `<button class="chip ${v === s.version ? "on" : ""}" data-ver="${v}">take ${v}</button>`).join("")}</div></div>` : ""}
        </div><div class="stack">
          <div class="row">${s.status === "done" ? `<button class="btn ${s.accepted ? "" : "primary"}" id="m-acc">${icon(s.accepted ? "x" : "check", "sm")} ${s.accepted ? "Unapprove" : "Approve"} <span class="kbd">A</span></button>` : ""}
            <button class="btn" id="m-redo">${icon("refresh", "sm")} New take <span class="kbd">R</span></button></div>
          ${flags.length ? `<div class="note bad">${icon("alert", "sm")}<div><b>Quality check</b><ul>${flags.map((f) => `<li>${esc(explainFlag(f, s))}</li>`).join("")}</ul></div></div>`
            : s.qa ? `<div class="note ok">${icon("check", "sm")}<div>Quality check passed${s.qa.cer != null ? ` · dialogue ${pct(1 - s.qa.cer)} accurate` : ""}</div></div>` : ""}
          ${s.qa?.heard ? `<div class="small muted">Heard: “${esc(s.qa.heard)}”</div>` : ""}
          <label class="field">Prompt <small>edit and press “Redo with edits” - names in CAPS become roles + reference sheet</small>
            <textarea id="m-prompt" rows="7">${esc(fs.prompt || "")}</textarea></label>
          <label class="field">Setting override<input id="m-setting" value="${esc(fs.setting || "")}" placeholder="optional"></label>
          <div class="row"><button class="btn primary" id="m-edit">${icon("edit", "sm")} Redo with edits</button>
            <button class="btn" id="m-text" title="Fixes sheet leaks and copies of a person">${icon("layers", "sm")} Without reference sheet</button></div>
          <div class="row"><label class="field" style="width:150px">Trim start (sec)<input id="m-trim" type="number" min="0" max="5" step="0.5" value="${fs.trim || 0}"></label>
            <button class="btn" id="m-trim-go" style="margin-top:20px">${icon("scissors", "sm")} Apply trim</button></div>
          <details class="adv"><summary>What the model actually received</summary><p class="small mono" style="white-space:pre-wrap;margin:8px 0 0">${esc(s.studio_prompt || "")}</p>
            ${s.sheet_img ? `<img src="${s.sheet_img}" style="margin-top:8px;border-radius:8px" alt="reference sheet">` : ""}</details>
          <div class="row between small muted"><span>${idx + 1} / ${shots.length}</span><span><button class="btn sm ghost" id="m-prev">← prev</button><button class="btn sm ghost" id="m-next">next →</button></span></div>
        </div></div>`;
      hydrateIcons(m.el);
      const acc = $("#m-acc", m.el); if (acc) acc.onclick = async () => { await accept(s.id, s.accepted); s.accepted = !s.accepted; if (s.accepted && idx < shots.length - 1) idx++; draw(); };
      $("#m-redo", m.el).onclick = () => { redo(s.id, "new_seed"); };
      $("#m-text", m.el).onclick = () => { redo(s.id, "text_only"); };
      $("#m-edit", m.el).onclick = async () => {
        fs.prompt = $("#m-prompt", m.el).value;
        const st = $("#m-setting", m.el).value.trim(); if (st) fs.setting = st; else delete fs.setting;
        await saveNow(); redo(s.id, "edited");
      };
      $("#m-trim-go", m.el).onclick = async () => {
        const sec = +$("#m-trim", m.el).value || 0;
        try { await api(`/api/films/${encodeURIComponent(fid)}/shots/${s.id}/trim`, { body: { seconds: sec } }); fs.trim = sec; s.trim = sec;
          toast(sec ? `${s.id}: first ${sec}s will be cut when the film is stitched` : `${s.id}: trim removed`, "ok"); } catch (e) { toast(e.message, "bad"); }
      };
      $$("[data-ver]", m.el).forEach((b) => (b.onclick = async () => {
        try { await api(`/api/films/${encodeURIComponent(fid)}/shots/${s.id}/version`, { body: { version: +b.dataset.ver } }); toast(`${s.id}: using take ${b.dataset.ver}`, "ok"); await load(true); m.close(); } catch (e) { toast(e.message, "bad"); }
      }));
      $("#m-prev", m.el).onclick = () => { if (idx > 0) { idx--; draw(); } };
      $("#m-next", m.el).onclick = () => { if (idx < shots.length - 1) { idx++; draw(); } };
    };
    const keys = (e) => {
      if (["INPUT", "TEXTAREA"].includes(document.activeElement?.tagName)) return;
      if (e.key === "ArrowRight") $("#m-next", m.el)?.click();
      if (e.key === "ArrowLeft") $("#m-prev", m.el)?.click();
      if (e.key === "a" || e.key === "A") $("#m-acc", m.el)?.click();
      if (e.key === "r" || e.key === "R") $("#m-redo", m.el)?.click();
    };
    const m = modal("Shot", "", { onClose: () => { document.removeEventListener("keydown", keys); load(true); } });
    document.addEventListener("keydown", keys);
    draw();
  }
  function explainFlag(f, s) {
    if (f === "sheet_leak") return `The reference sheet shows up in the video (${s.qa?.leak_s ?? "?"}s). Try “Without reference sheet” or a new take.`;
    if (f === "intro_leak_trimmed") return "A short sheet flash at the start - it is cut automatically when the film is stitched.";
    if (f.startsWith("face:")) return `${f.slice(5).replace(/F\d+_/, "")}'s face doesn't match the cast photo well (may be fine if seen from the side or small).`;
    if (f === "bars") return "Black bars around the picture.";
    if (f === "frozen") return "Almost no movement.";
    if (f === "black") return "Mostly black frames.";
    if (f === "speech") return "The dialogue doesn't match the script.";
    return f;
  }

  // ---- 6 final
  function finalPanel(p) {
    const b = data.bench || {};
    const rv = data.review || {};
    if (!data.final) {
      const allDone = data.project?.status === "done";
      p.innerHTML = `<div class="empty"><h3>${allDone ? "Putting the film together…" : "The film isn't finished yet"}</h3>
        <p>${allDone ? "The shots are stitched automatically and copied here in a moment." : "It is assembled automatically when every shot has rendered."}</p>
        ${data.project_id ? `<button class="btn" id="restitch">${icon("refresh", "sm")} Stitch now</button>` : ""}</div>`;
      if ($("#restitch")) $("#restitch").onclick = restitch;
      return;
    }
    p.innerHTML = `<div class="final-wrap"><div class="stack">
        <div class="player"><video src="${data.final}?t=${Math.round(data.modified)}" controls playsinline poster="${data.poster || ""}"></video></div>
        ${data.final_sheet ? `<img src="${data.final_sheet}" style="border-radius:12px;border:1px solid var(--line)" alt="contact sheet">` : ""}</div>
      <div class="stack">
        <div class="card"><div class="card-head"><h3>Benchmark</h3><button class="btn sm ghost" id="bench">${icon("refresh", "sm")} Update</button></div>
          <div class="kpis"><div class="kpi"><div class="v">${fmtTime(b.duration_s)}</div><div class="l">length</div></div>
            <div class="kpi"><div class="v">${b.redos ?? "–"}</div><div class="l">redos</div></div>
            <div class="kpi"><div class="v">${pct(b.face_consistency)}</div><div class="l">face consistency</div></div>
            <div class="kpi"><div class="v">${b.speech_cer_mean != null ? pct(1 - b.speech_cer_mean) : "–"}</div><div class="l">dialogue accuracy</div></div>
            <div class="kpi"><div class="v">${b.final_leaks ?? "–"}</div><div class="l">sheet leaks</div></div>
            <div class="kpi"><div class="v">${b.render_min ? fmtTime(b.render_min * 60) : "–"}</div><div class="l">render time</div></div></div></div>
        <div class="card stack"><h3>Your verdict</h3>
          <div class="stars" id="stars">${Array.from({ length: 10 }, (_, i) => `<button data-v="${i + 1}" class="${(rv.overall || 0) > i ? "on" : ""}" title="${i + 1}/10"><svg viewBox="0 0 24 24"><path d="M12 3l2.8 5.8 6.2.9-4.5 4.4 1 6.3L12 17.6 6.5 20.4l1-6.3L3 9.7l6.2-.9z"/></svg></button>`).join("")}</div>
          <label class="field">Notes<textarea id="rv-sum" rows="4" placeholder="What worked, what didn't">${esc(rv.summary || "")}</textarea></label>
          <div class="row end"><button class="btn primary" id="rv-save">Save verdict</button></div></div>
        <div class="card stack"><h3>Film</h3>
          <div class="row"><a class="btn" href="${data.final}" download>${icon("download", "sm")} Download MP4</a>
            <button class="btn" id="restitch">${icon("refresh", "sm")} Re-stitch</button><button class="btn" id="fullqa">${icon("eye", "sm")} Full quality check</button></div></div>
        ${jobsCard()}</div></div>`;
    let score = rv.overall || 0;
    $$("#stars button").forEach((s) => (s.onclick = () => { score = +s.dataset.v; $$("#stars button").forEach((x) => x.classList.toggle("on", +x.dataset.v <= score)); }));
    $("#rv-save").onclick = async () => { try { await api(`/api/films/${encodeURIComponent(fid)}/review`, { body: { overall: score || null, summary: $("#rv-sum").value } }); toast("Verdict saved", "ok"); load(true); } catch (e) { toast(e.message, "bad"); } };
    $("#bench").onclick = () => runJob("bench", {}, "benchmark");
    $("#fullqa").onclick = () => runJob("qa", {}, "full quality check");
    $("#restitch").onclick = restitch;
    bindJobs(p);
  }
  async function restitch() {
    try { const r = await api(`/api/films/${encodeURIComponent(fid)}/restitch`, { body: {} }); toast(`Film re-stitched (${fmtTime(r.duration_s)})`, "ok"); load(true); } catch (e) { toast(e.message, "bad"); }
  }

  await load(true);
  return { film: fid, go, destroy() { clearTimeout(timer); jobListeners.delete(onJob); if (dirty) saveNow(); } };
}
