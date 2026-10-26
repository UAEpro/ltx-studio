# LTX Studio: distributed rendering on 5 PCs

One **studio server** (main PC) holds the projects, the shot queue and all files. Every PC with an
RTX 5080 (the main PC included) runs one **worker** that pulls a shot, renders it locally with
`ltxgen.py generate`, and uploads the result. Joined workers also **seed**: they serve the model and code
files to PCs that join later, torrent-style. Protocol: `studio/PROTOCOL.md` (v1 + v1.1).

```
                        browser (dashboard: projects, shots, Workers tab, "Download join file")
                                         |
                                         v
  +--------------------- MAIN PC (D:\video) -------------------------+
  |  studio server :7860  queue + storage + manifest + file seeding  |
  |  worker (RTX 5080)                                               |
  +------------------------------------------------------------------+
        ^  HTTP/JSON + X-LTX-Token: register / heartbeat / claim / complete / fail
        |  GET /api/dist/manifest, /api/dist/peers, /api/dist/file/<path> (Range)
  +-----+--------------+------------------+------------------+
  |                    |                  |                  |
 PC2 worker         PC3 worker         PC4 worker         PC5 worker
 + seeder :7861     + seeder :7861     + seeder :7861     (joining: fetches 64 MiB pieces
   <---------------- pieces flow between all peers ------->  from main + PC2..PC4 at once)
 Root: <drive>:\ltx-studio (LTX-2\, ltxgen\, models\ltx-2.5\, tools\, studio_worker\)
```

## Why per-shot parallelism (and not splitting the model)
- The model is 22B parameters. Splitting it across PCs (tensor or pipeline parallel) would move
  activations over the network at every layer of every denoising step. Gigabit Ethernet does about
  0.1 GB/s; the PCIe/VRAM links inside one PC do 30 to 900 GB/s. The LAN is roughly 100 times too slow,
  so a split render would be *slower* than one PC on its own.
- Shots are independent jobs, so each PC renders whole shots. That needs no fast interconnect and scales
  almost linearly.
- A shot with `continue_from_previous: true` starts from the previous shot's last frame, so it has to
  wait for that shot to finish. A chain like s01 -> s02 -> s03 renders one shot at a time, and
  independent shots (hard cuts) render in parallel. To get the most out of the farm, use hard cuts
  between scenes and save continuations for shots that really need them.

## Throughput expectations
- 5 PCs means about 5 shots rendering at once. A 12-shot draft film takes about 3 draft-render rounds
  instead of 12.
- One shot still takes as long as it does on a single PC: minutes for `draft`, 10 or more for `quality`.
  Adding PCs doesn't make a single shot faster.
- Joining is limited by the network. On gigabit, ~110 MB/s per sender, so about 25 min per 160 GB
  from one peer. With N seeders a joining PC can pull up to N times that, until its own link or disk is
  full. Several PCs joining at once share the load, because each one finished earlier becomes a seeder.

## Main PC (once)
1. The main install works: `python ltxgen\ltxgen.py doctor` shows the GPU and all model files.
2. Open the server port for the LAN. In an **Administrator** PowerShell (it prints every command and asks
   first):
   `powershell -ExecutionPolicy Bypass -File D:\video\ltxgen\studio\deploy\share_models_main_pc.ps1`
   This adds the firewall rule "LTX Studio server (TCP 7860)" for the **Private** profile only. It also
   creates read-only SMB shares, which only the manual method below needs. If your LAN is marked
   *Public*, it prints the command to switch it to Private.
3. Start the studio server (`studio\start_studio.ps1`). Wait for the manifest: the first start hashes
   all files (a few minutes for ~160 GB). Until then `/api/dist/manifest` answers 503, and join files
   wait and retry by themselves.
4. Run the main PC's own worker. It never seeds, because the server already serves every file:
   `start_worker.ps1 -Server http://localhost:7860 -Token <token>`.

## Add a PC: one click (primary method)
1. On the new PC, open `http://MAINPC:7860`, go to **Workers**, and click **Download join file**. You
   get `join-ltx-studio.bat` with the server address and token built in.
2. Double-click it and accept the administrator prompt. Then wait. The window shows each step and
   ends with a big **SUCCESS** or **FAILED** line:

   | Step | What happens |
   |---|---|
   | checks | NVIDIA driver present (`nvidia-smi`). Install root = `<fixed drive with most free space>:\ltx-studio`, which needs 160 GB free; an existing `ltx-studio` folder is reused (resume). |
   | bootstrap | Downloads `tools\uv.exe` and `ltxgen\studio\join.py` from the server, then runs `uv run --python 3.13 join.py` (uv fetches its own Python). |
   | 1-3 download | Gets the manifest and peer list. Checks disk space. Downloads every file in 64 MiB pieces with 10 parallel connections, spread over the main PC and every joined PC (faster peers get more pieces). Each piece is sha256-verified; a bad piece is fetched again from another peer, and a peer that fails 3 times in a row is banned for 10 min. The peer list refreshes every 60 s. Progress line: `%`, GB, MB/s, ETA, active peers. |
   | 4 env (internet) | `uv sync --frozen --extra natten --python 3.13` in `LTX-2` (same lockfile and Python as the main PC), `triton-windows` + `ninja`, the `ltx_kernels` .pth, the all2all stub, and the prebuilt NVFP4 kernel (`nvfp4_cpp.cp313-win_amd64.pyd`). No CUDA toolkit or Visual Studio is needed. |
   | 5 verify | torch sees the GPU, the NVFP4 kernel loads, `ltxgen.py doctor` is clean, and at least `draft` can run. Otherwise the script stops and does not join. |
   | 6 join | Firewall rule "LTX Studio seeder (TCP 7861)" (Private only). Scheduled Task "LTX Studio Worker" at logon (current user, highest privileges, restart on failure), which runs `<root>\studio_worker\start_worker.cmd`. Starts it now and waits until the dashboard shows the PC online. |
3. **Resume and update.** Re-run the same file at any time. Finished pieces are recorded in
   `<root>\.join_state.json`, and only missing or changed pieces are downloaded again. Files that are
   already present but unrecorded (for example copied by hand) are hash-checked once instead of
   downloaded. Re-running after the main PC changed code or models updates this PC and restarts its
   worker.
4. Don't let the PC sleep (`powercfg /change standby-timeout-ac 0`). The worker blocks sleep while it
   runs, but a sleeping PC can't pick up new shots.

Disk layout on a joined PC: `tools\python` (uv-managed Python), `tools\uv-cache` (wheel cache,
~10 GB; delete it after joining to free space), `tools\ffmpeg.exe` (on the worker's PATH via
`start_worker.cmd`, not the machine PATH), `join.log`, and `studio_worker\worker.log`.

## Add a PC: manual method (fallback)
Use this when the one-click file can't be used, for example with no internet access for Python
wheels, or a different install root. Requirements: Windows 11, an NVIDIA driver, about 80 GB free
(about 125 GB with `-WithQuality`), and read access to `\\MAINPC\ltx-models`.
```
powershell -ExecutionPolicy Bypass -File \\MAINPC\ltxgen\studio\deploy\setup_worker_pc.ps1 `
    -MainPC MAINPC -Token <token> [-Root D:\video] [-WithQuality] [-ModelSource smb|hf]
```
It is idempotent and works through these steps:
1. Installs missing tools with winget: Git, Python 3.12, uv, FFmpeg, CUDA 13.x, and VS 2022 Build Tools.
2. Clones LTX-2 at `e98f6d9` and runs `uv sync --extra natten`.
3. Copies `ltxgen`.
4. Builds the NVFP4 kernel locally.
5. Copies the models: robocopy from the SMB share by default, or HuggingFace with `-ModelSource hf`.
   For HuggingFace, run `hf auth login` with an account that accepted the gated licenses of
   Lightricks/LTX-2.5 and Lightricks/LTX-2.5-22b-IC-LoRA-Pixel-Spatial-Upscaler; the script sets
   `HF_HUB_DISABLE_XET=1`.
6. Runs `ltxgen.py doctor` and the self-tests.

Then start the worker with `deploy\start_worker.ps1`, or register it at logon with
`deploy\install_worker_task.ps1` (`-Uninstall` removes the task). Add `--seed` in `start_worker.ps1` /
`worker.py` if this PC should also seed.

## Verify
On `http://MAINPC:7860`, open the **Workers** tab. Every PC should be online with its GPU and presets.
Render a draft project: shots spread across the workers, and each busy worker shows live progress such
as `3m12s | Denoising: 62%| step 5/8`. Logs: `<root>\studio_worker\worker.log`. Per-task files:
`<root>\studio_worker\<task_id>\`, deleted after 2 days.

## Worker options (`worker.py`)
`--server URL --token T` (or env `LTX_STUDIO_SERVER` / `LTX_STUDIO_TOKEN`), `--root` (env `LTXGEN_ROOT`),
`--worker-id` (default `<hostname>-<gpu>`), `--presets auto|draft,standard,...`,
`--seed / --no-seed` (default: seed once every manifest file is present locally, never on the server's PC),
`--seed-port 7861`, `--once`, and `--dry-run`, which fakes renders with a tiny ffmpeg clip for protocol
tests. `--dry-run --once --presets selftest` checks connectivity and the token without claiming real work.

The seeder is read-only. It serves only manifest paths whose local size matches, needs the token, and
runs at background CPU/IO priority with at most 4 uploads at a time, so it doesn't slow down renders.

## Troubleshooting
| Symptom | Fix |
|---|---|
| Join: `cannot download ... from http://MAINPC:7860` | The server isn't running, or the main PC firewall blocks 7860 (run `share_models_main_pc.ps1`; the LAN must be **Private**). Test with `Test-NetConnection MAINPC -Port 7860`. |
| Join: `server is still building the manifest` | Normal after a server start (hashing). The script waits on its own. |
| Join: `server rejected the token` | Download a fresh join file. The token is in `D:\video\studio\studio.json`. |
| Join: `not enough disk space` | Free space on the biggest drive, or move an existing `ltx-studio` folder there. |
| Join: `GPU check failed` | Update the NVIDIA driver (torch cu132 needs a recent one) and re-run. |
| Join: `uv sync` failed | The Python env step needs internet access to pypi.org and download.pytorch.org. Re-run once online. |
| Join is slow / `peers 1 active` | Only the main PC is seeding. Other PCs seed only once they have every file of the *current* manifest, so a code change on the main PC pauses them until they re-run the join file. Check `Get-NetConnectionProfile` (Private) on the seeders. |
| `peer X banned` | That peer sent bad data or kept failing. Its pieces come from the others, and it's retried after 10 min. |
| Worker online but never gets shots | It advertises fewer presets (shown in the Workers tab) because model files are missing. Presets are re-checked every 5 min. |
| Shot failed with an `OOM` hint | Use `draft`, shorter shots (5 to 6 s), or `--vae video_vae_conv` in `ltxgen\config.json`. Close other GPU apps such as games, browsers with video, or Ollama. |
| `NVFP4 kernels unavailable` | The kernel's Python tag must match the venv (cp313). Re-run the join file. On the manual path, rebuild with VS Build Tools + CUDA 13.x. |
| Worker shows offline mid-render | Sleep or reboot. Disable sleep and pause Windows Update restarts. After 15 min without a heartbeat, the server gives the shot to another worker. |
| Cancel in the dashboard | The worker kills the render's process tree within one heartbeat (20 s) and uploads nothing. |

## Security notes
- This setup is for the LAN only. Never forward 7860 or 7861 on the router. Both firewall rules are
  limited to the **Private** profile.
- Every worker and seeder request carries the shared token (`X-LTX-Token`). Treat it like a password. It
  is embedded in the join file and stored in plain text in `studio_worker\start_worker.cmd` and
  `worker_settings.json` on each worker. Dashboard routes and `GET /join` are unauthenticated on the LAN
  in v1, so anyone on the LAN can download a join file that contains the token.
- Seeders are read-only and only serve files listed in the manifest. Path traversal is rejected.
- The join script needs admin rights only for the firewall rule and the scheduled task. It changes
  nothing else on the system: no machine PATH, no power settings.
