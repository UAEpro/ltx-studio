// Setup: check this PC and install tools, the LTX engine and the models from the browser.
import { api, esc, icon, toast, $, $$, hydrateIcons, showJob, jobListeners } from "../app.js";

const LEVEL = { core: "required", consistency: "consistent characters", quality: "optional" };

export async function setupView(root) {
  let timer;
  const onJob = () => load();
  jobListeners.add(onJob);

  async function load() {
    clearTimeout(timer);
    let st, jobs;
    try { [st, jobs] = await Promise.all([api("/api/setup"), api("/api/jobs")]); } catch (e) { root.innerHTML = `<div class="note bad">${esc(e.message)}</div>`; return; }
    const running = jobs.filter((j) => j.kind === "setup" && ["queued", "running"].includes(j.status));
    const missing = st.missing || [];
    root.innerHTML = `<div class="page-head"><div><div class="eyebrow">Setup</div><h1>Get this PC ready</h1>
      <p>Everything LTX Studio needs, checked live. Missing pieces install from here - tools through Windows Package Manager, the engine from GitHub, models from Hugging Face.</p></div></div>
      <div class="setup-hero"><div class="row" style="gap:16px">
        <div style="width:54px;height:54px;border-radius:16px;display:grid;place-items:center;background:${st.ready ? "var(--ok-soft)" : "var(--accent-soft)"};color:${st.ready ? "var(--ok)" : "var(--accent)"}">${icon(st.ready ? "check" : "download")}</div>
        <div><h2>${st.ready ? "Ready to make films" : `${missing.length} thing${missing.length === 1 ? "" : "s"} to install`}</h2>
        <p class="muted" style="margin:3px 0 0">${st.ready ? "The engine, models and tools are all in place." : esc(missing.slice(0, 5).join(" · ")) + (missing.length > 5 ? " …" : "")}</p></div></div>
        ${st.ready ? "" : `<button class="btn primary lg" id="all" ${running.length ? "disabled" : ""}>${running.length ? '<span class="spinner"></span> Installing…' : `${icon("download")} Install everything`}</button>`}</div>
      ${!st.hf_token ? `<div class="note warn" style="margin-bottom:16px">${icon("alert", "sm")}<div>The character-consistency model is gated on Hugging Face. Accept its licence on huggingface.co and paste a <b>Hugging Face token</b> in <a href="#/settings" style="text-decoration:underline">Settings</a> before installing it.</div></div>` : ""}
      ${running.length ? `<div class="card" style="margin-bottom:16px"><div class="card-head"><h3><span class="spinner" style="display:inline-block;vertical-align:-3px"></span> ${esc(running[0].title)}</h3><button class="btn sm" id="seelog">Full log</button></div><div class="log" id="livelog">…</div></div>` : ""}
      <div class="setup-groups">${st.groups.map((g) => `<div class="card"><div class="card-head"><h3>${esc(g.title)}</h3></div>
        ${g.items.map((it) => checkRow(it, running)).join("")}</div>`).join("")}</div>`;
    hydrateIcons(root);
    if ($("#all")) $("#all").onclick = async () => {
      try { const r = await api("/api/setup/install-all", { body: {} }); toast(`Installing ${r.steps.length} item(s) one after another`, "ok"); setTimeout(load, 1500); } catch (e) { toast(e.message, "bad"); }
    };
    $$("[data-install]", root).forEach((b) => (b.onclick = async () => {
      try { await api(`/api/setup/install/${b.dataset.install}`, { body: {} }); toast("Started", "ok"); setTimeout(load, 1000); } catch (e) { toast(e.message, "bad"); }
    }));
    if (running.length) {
      $("#seelog").onclick = () => showJob(running[0].id);
      try { const j = await api(`/api/jobs/${running[0].id}`); const el = $("#livelog"); el.textContent = (j.tail || []).slice(-14).join("\n") || "starting…"; el.scrollTop = el.scrollHeight; } catch {}
    }
    timer = setTimeout(load, running.length ? 3000 : 20000);
  }
  function checkRow(it, running) {
    const busy = running.some((j) => j.title.toLowerCase().includes((it.title || "").toLowerCase().split(" ")[0]));
    const optional = it.level === "quality";
    const ic = it.ok === true ? "ok" : it.ok === false ? (optional ? "opt" : "bad") : "unk";
    const prog = it.ok === false && it.progress > 0 && it.progress < 1;
    return `<div class="check"><span class="ic ${ic}">${icon(it.ok === true ? "check" : it.ok === false ? (optional ? "plus" : "x") : "info")}</span>
      <div><div class="t">${esc(it.title)}${it.level ? `<span class="level">${LEVEL[it.level] || it.level}</span>` : ""}${it.gb ? `<span class="level">${it.gb} GB</span>` : ""}</div>
        <div class="d">${esc(it.detail || it.why || "")}</div>${prog ? `<div class="bar"><span style="width:${Math.round(it.progress * 100)}%"></span></div>` : ""}</div>
      <div>${it.install && it.ok !== true ? `<button class="btn sm ${it.ok === false && !optional ? "primary" : ""}" data-install="${it.key}" ${busy ? "disabled" : ""}>${it.key === "doctor" ? "Run" : it.key === "worker" ? "Start" : "Install"}</button>` : ""}</div></div>`;
  }
  await load();
  return { destroy() { clearTimeout(timer); jobListeners.delete(onJob); } };
}
