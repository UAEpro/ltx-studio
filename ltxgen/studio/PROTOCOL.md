# LTX Studio – coordinator ⇄ worker protocol (v1)

One **studio server** (dashboard + job queue + storage) runs on the main PC. Every PC with a GPU
(including the main PC) runs one **worker** that pulls shot tasks, renders them locally with
`ltxgen.py generate`, and uploads the result. Parallelism is per shot (one shot = one task on one GPU);
the model itself is never split across machines.

- Transport: HTTP/JSON on the LAN. Default server URL `http://<main-pc>:7860`.
- Auth: every worker request sends header `X-LTX-Token: <token>` (token in `studio/studio.json` on the
  server, passed to the worker by `--token` / env `LTX_STUDIO_TOKEN`). Dashboard (browser) routes
  under `/api/projects*` are unauthenticated on the LAN in v1.
- Times are unix seconds (float). IDs are short hex strings.

## Task lifecycle
`pending` → (deps satisfied) `ready` → `claimed` (by a worker, lease 15 min, renewed by heartbeat) →
`done` | `failed`. A claimed task whose lease expires goes back to `ready` (worker presumed dead).
A shot with `continue_from_previous: true` depends on the previous shot: it stays `pending` until that
shot is `done`; the server then extracts the previous video's last frame and attaches it as the
start image. `redo` creates a fresh task for the shot (old outputs kept as previous versions).

## Worker endpoints (require token)

### POST /api/workers/register
Request: `{"worker_id": "pc2-5080", "hostname": "PC2", "gpu": "NVIDIA GeForce RTX 5080",
"vram_gb": 16, "presets": ["draft","standard","quality"], "version": "1"}`
`presets` = presets this worker can run (quality needs bf16 transformer + detailing LoRA).
Response: `{"ok": true, "poll_interval_s": 5}`

### POST /api/workers/{worker_id}/heartbeat
Request: `{"status": "idle"|"busy", "task_id": "ab12cd34"|null, "progress": "free text"|null}`
Response: `{"ok": true, "cancel": false}` – if `cancel` is true the worker must kill the current render,
and not upload it.
Workers send a heartbeat every ≤30 s (idle or busy). Workers silent for >90 s show as offline.

### POST /api/tasks/claim
Request: `{"worker_id": "pc2-5080"}`
Response 204 (no body) if nothing to do, else 200:
```json
{"task_id": "ab12cd34", "project_id": "9f3e21aa", "shot_id": "s03",
 "params": {"prompt": "...full prompt incl. style...", "preset": "draft", "aspect": "16:9",
            "seconds": 5, "seed": 102},
 "inputs": {"start_image": {"url": "/api/files/9f3e21aa/inputs/s03_start.png", "frame": 0, "strength": 1.0},
            "end_image": null}}
```
`inputs.*` are null or `{url, frame, strength[, binding]}`; `url` is relative to the server; `end_image`
has `"frame": <last frame index>` and `"binding": "guide"`. Download with the token header.
Only tasks whose preset is in the worker's `presets` are handed out.

### GET /api/files/{project_id}/{path}
Serves project files (inputs, shot outputs, final). Token OR same-origin browser access.

### POST /api/tasks/{task_id}/complete   (multipart/form-data)
Fields: `worker_id` (text), `result` (text: the JSON printed by `ltxgen.py generate`), files:
`video` (video.mp4, required), `sheet` (sheet.jpg, optional), `log` (log.txt, optional).
Response: `{"ok": true}`.

### POST /api/tasks/{task_id}/fail
Request: `{"worker_id": "...", "error": "...", "hint": "...", "log_tail": ["..."]}`
Response: `{"ok": true}`. Server marks task failed (no automatic retry in v1; user clicks Redo).

## Dashboard / project endpoints (browser)
- `POST /api/projects` body = storyboard JSON (format: `ltxgen/storyboard_example.json`) → `{"project_id"}`
- `GET /api/projects` → list; `GET /api/projects/{id}` → project + shots + task states + file URLs
- `POST /api/projects/{id}/shots/{shot_id}/redo` body `{"prompt"?: str, "seed"?: int}`
- `POST /api/projects/{id}/stitch` → builds `final.mp4` (all shots must be done)
- `POST /api/projects/{id}/finalize` body `{"preset": "standard"|"quality"}` → new project copy at
  higher preset with same seeds/prompts
- `GET /api/workers` → workers with status, current task, last seen, completed count
- `DELETE /api/projects/{id}` / `POST /api/tasks/{task_id}/cancel`

## Peer distribution + one-click join (v1.1)
Goal: a new PC runs ONE file (`join-ltx-studio.bat`, downloaded from the dashboard, server URL + token
embedded) and ends up as a working, auto-starting worker. Big files are fetched in 64 MiB pieces in
parallel from every peer that has them (main PC + already-joined workers), torrent-style but simple:
static piece hashes, HTTP Range requests, no tracker beyond the studio server.

Install root on a worker: `<drive>:\ltx-studio` (drive with most free space). Layout mirrors the main PC
root (`D:\video`): `LTX-2\`, `ltxgen\`, `models\ltx-2.5\...`, `tools\`. ltxgen uses `LTXGEN_ROOT`.

### GET /api/dist/manifest   (token)
503 `{"detail": "building manifest"}` while hashing; else
```json
{"version": "a1b2c3d4e5f6", "piece_size": 67108864,
 "files": [{"path": "models/ltx-2.5/vae/ltx-2.5-audio-vae-bf16.safetensors", "size": 361234567,
            "kind": "model", "pieces": ["<sha256 hex of piece 0>", "..."]}]}
```
`path` is relative to the install root, forward slashes. `kind`: `model` (models/ltx-2.5/**), `code`
(LTX-2/** without .venv/.git/__pycache__, ltxgen/** without .venv/storyboards/jobs.json/__pycache__),
`tool` (`tools/uv.exe`, `tools/ffmpeg.exe`, `tools/ffprobe.exe`, `tools/nvfp4_cpp.cp313-win_amd64.pyd`
— the prebuilt NVFP4 kernel, valid for the pinned torch; copy into the venv site-packages after `uv sync`).
Every piece is `piece_size` bytes except the last of each file. `version` changes whenever any file changes;
only complete files are listed (partial downloads on the main PC are excluded).

### GET /api/dist/peers   (token)
`[{"id": "main", "url": "http://192.168.1.10:7860/api/dist/file/"},
  {"id": "pc2-5080", "url": "http://192.168.1.12:7861/dist/file/"}]`
= the server itself + online workers that registered with `seed_url`. Workers whose `seed_version` differs
from the current manifest (code changed on main since they joined) are listed with
`"kinds": ["model", "tool"]`; joiners use them only for those kinds (a 404 means "doesn't have it").
A piece is fetched as `GET <url><path>` with header `Range: bytes=a-b` and `X-LTX-Token`.

### GET /api/dist/file/{path}   (token, Range supported) — server seeds every manifest file.
### Worker seeding
A joined worker runs a read-only HTTP seeder (default port 7861, Private-profile firewall rule added by
the join script) serving `GET /dist/file/{path}` with Range + token check, only for paths in the
manifest whose local size matches. Its register payload adds
`"seed_url": "http://<lan-ip>:7861/dist/file/", "seed_version": "<manifest version it fully has>"`
(`seed_version` null while incomplete).

### GET /join   (no token; LAN)  → `join-ltx-studio.bat` with `SERVER` and `TOKEN` embedded
The .bat self-elevates, downloads `tools/uv.exe` and `ltxgen/studio/join.py` from the server, and runs
`uv run --no-project --python 3.12 join.py --server ... --token ...`.

## Storage layout (server)
`D:\video\studio\projects\<project_id>\` → `storyboard.json`, `inputs/`, `shots/<shot_id>/v<N>/{video.mp4,
sheet.jpg,log.txt,result.json}`, `final.mp4`, `final_sheet.jpg`. State in `D:\video\studio\studio.db` (SQLite).
