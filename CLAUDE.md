# LTX-2.5 video generation workspace

Local install of Lightricks **LTX-2.5** (22B audio+video model) for agentic video generation.
Hardware: RTX 5080 16 GB (Blackwell, sm_120), 31 GB RAM, NVMe. The model does not fit in VRAM,
so weights are streamed (offload) and the transformer runs in NVFP4.

## Layout
- `LTX-2/` – official repo (Lightricks/LTX-2), venv in `LTX-2/.venv` (uv). Don't edit.
- `models/ltx-2.5/` – weights (see `ltxgen/download_models.ps1`).
- `ltxgen/ltxgen.py` – stdlib CLI wrapper; every command prints one JSON object.
- `ltxgen/mcp_server.py` – MCP server `ltx-video` (registered in `.mcp.json`).
- `ltxgen/config.json` – machine-tuned defaults (quantization / offload / VAE).
- `outputs/<timestamp>_<slug>/` – `video.mp4`, `sheet.jpg` (contact sheet), `meta.json` (prompt, seed, command, timing), `log.txt`.

## How to generate
Prefer the MCP tools (`generate_video` → `wait_for_job` → `view_video`). CLI equivalent:
```
python ltxgen/ltxgen.py generate --prompt "..." --preset draft --seconds 5 --aspect 16:9
python ltxgen/ltxgen.py generate --prompt "..." --image start.png            # image-to-video
python ltxgen/ltxgen.py generate --prompt "..." --image a.png 0 1 --image b.png 120 1 guide  # keyframes
python ltxgen/ltxgen.py frame outputs/.../video.mp4 --which last              # continuation frame
python ltxgen/ltxgen.py sheet outputs/.../video.mp4                           # look at it
python ltxgen/ltxgen.py stitch a.mp4 b.mp4 --output outputs/final.mp4
python ltxgen/ltxgen.py doctor
```
Generation runs minutes; run CLI generations in the background. Only one generation at a time (one GPU).

Presets: `draft` 768x448 distilled (iterate here) · `standard` 1280x704 distilled · `quality` DFR 1536x832 (slow, final renders).
Constraints: frames = 8k+1 (wrapper converts `--seconds`), width/height divisible by 64, ≤ ~20 s per clip.
Omit `--seconds` to let the duration head pick the length from the prompt.

## LTX Studio (web app + multi-PC render farm)
- Start: `ltxgen/studio/start_studio.ps1` (or desktop shortcut) → http://localhost:7860. Fresh PC: `ltxgen/studio/install.ps1`
  then Setup page → "Install everything" (tools via winget, LTX-2 engine, models from HF, worker). Old UI: `/classic`.
- Web app = `ltxgen/studio/ui/` (no build step: app.js + views/*.js + app.css). Backend: `server.py` (queue, workers),
  `films.py` (films API, jobs, monitor that runs QA+autofix every 4 shots and finishes the film), `setup_page.py`.
- A film = `films/<Fxx_slug>/film.json` (same format as `factory.py` uses). Six steps in the UI: Story (AI writer:
  `film_prompt.md`, Claude or Ollama) → Cast & places (`factory.py prepare --assets-only`) → Shot list (lint) → Render
  (`prepare`) → Review (approve / new take / without sheet / edit & redo = `factory.py reshot` / trim) → Final.
- Workers: `ltxgen/studio/worker.py`; multi-PC: Render farm page → join file. Parallelism is per shot.
- Redoing a shot does not re-render later shots chained to it; redo those too if continuity matters.

## Scenario → multi-scene film (main use case)
When the user gives a scenario/story:
1. **Treatment**: restate the story in 3–5 sentences; decide total length, aspect, tone. Ask only if
   something essential is ambiguous (otherwise pick sensible defaults: 16:9, 30–60 s, 5–6 s per shot).
2. **Style bible**: one `style` sentence (look, lens, palette, grain) + a fixed visual description of each
   recurring character/location. Paste the character description verbatim into every shot it appears in.
3. **Shot list** → storyboard JSON (format: `ltxgen/storyboard_example.json`, docs in `render_storyboard`).
   One action per shot, vary shot sizes (wide/medium/close), give every shot sound/dialogue.
   Use `continue_from_previous: true` when a shot must flow directly from the previous one (same place,
   continuous action); hard cuts between scenes otherwise.
4. **Draft pass**: `render_storyboard` with `"preset": "draft"` (or CLI `ltxgen.py storyboard file.json`).
5. **Review**: `view_video` every shot; fix bad ones by editing prompt/seed and `redo=["s03"]`.
6. **Final pass**: set `"preset": "standard"` (or `"quality"`), new `output_dir`/title, render again with
   the same seeds; final film is `outputs/<title>/final.mp4` (+ `final_sheet.jpg`).
7. Report the final path, total duration, and a one-line summary per shot.

## Agentic workflow (single clips)
1. Plan shots (one clip = one shot, 3–10 s). Write each prompt (see below).
2. Render each shot as `draft` with a fixed seed. Inspect with `view_video` / the contact sheet; check prompt
   adherence, anatomy, text, and motion. Revise the prompt or seed and re-render as needed.
3. For continuity between shots, extract the last frame of shot N and pass it as `image_path` of shot N+1.
4. Re-render approved shots with `standard`/`quality` using the same seed and prompt.
5. `stitch_videos` the finals. Record decisions in `outputs/<project>/notes.md`.

## Prompt guide (LTX-2.x)
Single flowing paragraph, present tense, chronological, ~80–200 words:
start with the main action → specific movements/gestures → subject appearance → setting/background →
camera (angle, lens, movement: "slow dolly in", "handheld tracking shot") → lighting/colour/mood →
**audio** (ambience, SFX, music, and dialogue in quotes with who speaks and their tone).
Be literal and concrete; avoid negations and abstract adjectives. For image-to-video, describe the motion
and camera rather than re-describing the image.

## Troubleshooting
Results carry `hint` on failure. OOM → `draft`, shorter clip, `--vae video_vae_conv`. RAM exhausted →
`--offload disk`. NVFP4 kernel errors → `--quantization fp8-cast --transformer transformer_bf16`
(needs the 42 GB bf16 file: `download_models.ps1 -WithBf16`).
License: LTX-2.x Community License (free commercial use under $10M annual revenue).
