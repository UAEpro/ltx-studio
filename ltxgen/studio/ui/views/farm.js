// Render farm: workers on this and other PCs, queue, add-a-PC.
import { api, esc, icon, fmtTime, toast, $, hydrateIcons } from "../app.js";

export async function farmView(root) {
  let timer;
  async function load() {
    clearTimeout(timer);
    let workers = [], queue = { counts: {} }, cfg = {};
    try { [workers, queue, cfg] = await Promise.all([api("/api/workers"), api("/api/queue"), api("/api/settings")]); } catch (e) { toast(e.message, "bad"); }
    const base = location.hostname === "localhost" || location.hostname === "127.0.0.1" ? (cfg.lan_url || location.origin) : location.origin;
    const c = queue.counts || {};
    root.innerHTML = `<div class="page-head"><div><div class="eyebrow">Render farm</div><h1>Workers</h1>
      <p>Every computer with a supported GPU can join and render shots in parallel. Shots are handed out one at a time; a PC that goes offline gives its shot back automatically.</p></div>
      <a class="btn primary lg" href="#addpc" onclick="document.getElementById('joincmd').scrollIntoView({behavior:'smooth'});return false">${icon("plus")} Add a computer</a></div>
      <div class="kpis" style="margin-bottom:18px">
        <div class="kpi"><div class="v">${workers.filter((w) => w.status !== "offline").length}</div><div class="l">online</div></div>
        <div class="kpi"><div class="v">${c.claimed || 0}</div><div class="l">rendering</div></div>
        <div class="kpi"><div class="v">${(c.ready || 0) + (c.pending || 0)}</div><div class="l">waiting</div></div>
        <div class="kpi"><div class="v">${c.done || 0}</div><div class="l">shots rendered</div></div></div>
      <div class="workers">${workers.map((w) => {
        const m = /(\d+)%/.exec(w.progress || "");
        return `<div class="card worker"><div class="head"><span class="dot ${w.status === "busy" ? "run" : w.status === "offline" ? "" : "ok"}"></span>
          <h3 class="grow">${esc(w.hostname || w.id)}</h3><span class="chip ${w.status === "busy" ? "warn" : w.status === "offline" ? "" : "ok"}">${esc(w.status)}</span></div>
          <div class="gpu mt">${esc(w.gpu || "GPU unknown")} · ${esc((w.presets || []).join(", "))}</div>
          ${w.status === "busy" ? `<div class="small mt">${esc((w.progress || "").slice(0, 90))}</div>${m ? `<div class="bar mt"><span style="width:${m[1]}%"></span></div>` : ""}` : ""}
          <div class="small muted mt">${w.completed} shots done · seen ${fmtTime(w.last_seen_s)} ago</div></div>`;
      }).join("") || `<div class="empty"><h3>No workers yet</h3><p>Start the worker on this PC from Setup, or add another PC.</p><a class="btn" href="#/setup">Go to Setup</a></div>`}</div>
      <div class="card mt-lg"><div class="card-head"><div><h3>Adding another computer</h3><p>Any PC with an NVIDIA GPU (Windows or Linux) or an Apple Silicon Mac with 64 GB+ memory can render. Both computers must be on the same network.</p></div></div>
        <div class="stack">
          <div><div class="eyebrow" style="margin-bottom:6px">Windows</div>
            <div class="row"><a class="btn" href="/join">${icon("download", "sm")} Download join file</a><span class="small muted">Double-click it on the new PC. It copies the engine and models from the PCs already in the farm.</span></div></div>
          <div><div class="eyebrow" style="margin-bottom:6px">Linux / macOS</div>
            <div class="row" style="flex-wrap:nowrap"><input class="mono" readonly id="joincmd" value="curl -fsSL ${base}/join.sh | bash">
              <button class="btn" id="copyjoin">Copy</button></div>
            <p class="small muted" style="margin:6px 0 0">Run it in a terminal on the new computer. It installs the engine, downloads the models from Hugging Face and joins this farm. For the character-consistency model, add your Hugging Face token: <span class="mono">… | HF_TOKEN=hf_xxx bash</span></p></div>
        </div></div>`;
    hydrateIcons(root);
    const cj = $("#copyjoin");
    if (cj) cj.onclick = () => { navigator.clipboard.writeText($("#joincmd").value); toast("Copied", "ok"); };
    timer = setTimeout(load, 5000);
  }
  await load();
  return { destroy() { clearTimeout(timer); } };
}
