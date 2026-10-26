// Render farm: workers on this and other PCs, queue, add-a-PC.
import { api, esc, icon, fmtTime, toast, $, hydrateIcons } from "../app.js";

export async function farmView(root) {
  let timer;
  async function load() {
    clearTimeout(timer);
    let workers = [], queue = { counts: {} };
    try { [workers, queue] = await Promise.all([api("/api/workers"), api("/api/queue")]); } catch (e) { toast(e.message, "bad"); }
    const c = queue.counts || {};
    root.innerHTML = `<div class="page-head"><div><div class="eyebrow">Render farm</div><h1>Workers</h1>
      <p>Every PC with an NVIDIA GPU can join and render shots in parallel. Shots are handed out one at a time; a PC that goes offline gives its shot back automatically.</p></div>
      <a class="btn primary lg" href="/join">${icon("plus")} Add a PC</a></div>
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
      <div class="card mt-lg"><div class="card-head"><div><h3>Adding another PC</h3><p>Download the join file on that PC and double-click it. It copies the engine and models from the PCs already in the farm and starts a worker.</p></div></div>
        <div class="row"><a class="btn" href="/join">${icon("download", "sm")} Download join file</a><span class="small muted">Both PCs must be on the same network.</span></div></div>`;
    hydrateIcons(root);
    timer = setTimeout(load, 5000);
  }
  await load();
  return { destroy() { clearTimeout(timer); } };
}
