// Settings: AI writer keys, Hugging Face token, local model, farm token.
import { api, esc, icon, toast, $, hydrateIcons } from "../app.js";

export async function settingsView(root) {
  let s;
  try { s = await api("/api/settings"); } catch (e) { root.innerHTML = `<div class="note bad">${esc(e.message)}</div>`; return {}; }
  root.innerHTML = `<div class="page-head"><div><div class="eyebrow">Settings</div><h1>Settings</h1><p>Keys are stored on this PC only (studio/studio.json).</p></div></div>
    <div class="setup-groups">
      <div class="card stack"><h3>${icon("sparkle", "sm")} Script writer</h3>
        <label class="field">Claude API key <small>${s.anthropic_api_key ? "set ✓ - paste a new one to replace" : "optional - best scripts"}</small><input id="ak" type="password" placeholder="${s.anthropic_api_key ? "••••••••" : "sk-ant-…"}"></label>
        <label class="field">Local model (Ollama) <small>${s.ollama_models.length ? `${s.ollama_models.length} installed` : "Ollama not running"}</small>
          <select id="lm">${[...new Set([s.local_model, ...s.ollama_models])].map((m) => `<option ${m === s.local_model ? "selected" : ""}>${esc(m)}</option>`).join("")}</select></label></div>
      <div class="card stack"><h3>${icon("download", "sm")} Downloads</h3>
        <label class="field">Hugging Face token <small>${s.hf_token ? "set ✓" : "needed for the gated character-consistency model"}</small><input id="hf" type="password" placeholder="${s.hf_token ? "••••••••" : "hf_…"}"></label>
        <p class="small muted" style="margin:0">Create one at huggingface.co → Settings → Access Tokens (read access), and accept the licence on the Lightricks/LTX-2.5-22b-IC-LoRA-Ingredients page.</p></div>
      <div class="card stack"><h3>${icon("cpu", "sm")} Render farm</h3>
        <label class="field">Worker token <small>other PCs use this to join</small><input type="text" readonly value="${esc(s.token)}" class="mono"></label>
        <div class="small muted">Studio port ${s.port} · <a href="/classic" style="text-decoration:underline">classic dashboard</a></div></div>
    </div>
    <div class="row mt"><button class="btn primary" id="save">${icon("check", "sm")} Save settings</button></div>`;
  hydrateIcons(root);
  $("#save").onclick = async () => {
    const body = { local_model: $("#lm").value };
    if ($("#ak").value.trim()) body.anthropic_api_key = $("#ak").value.trim();
    if ($("#hf").value.trim()) body.hf_token = $("#hf").value.trim();
    try { await api("/api/settings", { body }); toast("Saved", "ok"); location.reload(); } catch (e) { toast(e.message, "bad"); }
  };
  return {};
}
