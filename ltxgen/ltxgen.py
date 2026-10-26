"""ltxgen: agent-friendly wrapper around the official LTX-2.5 pipelines (Lightricks/LTX-2).

Stdlib only. Runs each generation in a fresh subprocess of the LTX-2 venv so GPU/RAM
is fully released between jobs. Every command prints a single JSON object on stdout.

Commands:
  generate   text/image -> video (+ audio)
  frame      extract a frame (e.g. last frame, for shot continuation via --image)
  sheet      contact sheet (grid of frames) so an agent can visually check a clip
  stitch     concatenate clips into one video
  probe      ffprobe summary of a video
  doctor     verify environment, model files and GPU
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("LTXGEN_ROOT") or Path(__file__).resolve().parent.parent)  # install root = parent of ltxgen/
REPO = ROOT / "LTX-2"
VENV_PY = REPO / ".venv" / "Scripts" / "python.exe"
MODELS = ROOT / "models" / "ltx-2.5"
OUTPUTS = ROOT / "outputs"
CONFIG = Path(__file__).with_name("config.json")

FILES = {
    "transformer_nvfp4": "diffusion_models/ltx-2.5-22b-distilled-transformer-nvfp4.safetensors",
    "transformer_bf16": "diffusion_models/ltx-2.5-22b-distilled-transformer-bf16.safetensors",
    "transformer_fp8": "diffusion_models/ltx-2.5-22b-distilled-transformer-fp8cast.safetensors",
    "text_encoder": "text_encoders/gemma4-12b-with-proj-ltx-2.5-bf16.safetensors",
    "video_vae": "vae/ltx-2.5-video-vae-bf16.safetensors",
    "video_vae_conv": "vae/ltx-2.5-video-vae-conv-bf16.safetensors",
    "audio_vae": "vae/ltx-2.5-audio-vae-bf16.safetensors",
    "duration_head": "model_patches/ltx-2.5-duration-head-bf16.safetensors",
    "spatial_upsampler": "latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors",
    "temporal_upsampler": "latent_upscale_models/ltx-2.5-latent-temporal-upscaler-x2-bf16-1.0.safetensors",
    "detailing_lora": "loras/ltx-2.5-22b-ic-lora-pixel-spatial-upscaler-x2-1.0.safetensors",
    "ingredients_lora": "ic-loras/ingredients/ltx-2.5-22b-ic-lora-ingredients-0.9.safetensors",
}

# Final output sizes (W, H). Two-stage pipelines run stage 1 at half size, so keep both
# dimensions divisible by 64.
ASPECTS = {
    "draft": {"16:9": (768, 448), "9:16": (448, 768), "1:1": (576, 576), "4:3": (704, 512), "3:4": (512, 704)},
    "standard": {"16:9": (1280, 704), "9:16": (704, 1280), "1:1": (960, 960), "4:3": (1088, 832), "3:4": (832, 1088)},
    "quality": {"16:9": (1536, 832), "9:16": (832, 1536), "1:1": (1152, 1152), "4:3": (1344, 1024), "3:4": (1024, 1344)},
}
PIPELINES = {"draft": "distilled", "standard": "distilled", "quality": "dfr_pipeline"}

# Machine-tuned defaults; overridden by config.json (written after benchmarking).
DEFAULT_CONFIG = {
    "transformer": "transformer_nvfp4",   # or transformer_bf16
    "quantization": "nvfp4-prequant",    # nvfp4-prequant | fp8-cast | none
    "offload": "disk",                   # none | cpu | disk
    "video_vae": "video_vae",            # video_vae (diffusion decoder) | video_vae_conv (faster)
    "fps": 24,
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG.exists():
        cfg.update(json.loads(CONFIG.read_text(encoding="utf-8")))
    return cfg


def out(obj: dict, code: int = 0) -> None:
    print(json.dumps(obj, indent=2, default=str))
    sys.exit(code)


def mpath(key: str) -> Path:
    return MODELS / FILES[key]


def slugify(text: str, n: int = 40) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return s[:n].strip("-") or "clip"


def seconds_to_frames(seconds: float, fps: float) -> int:
    """LTX needs num_frames = 8k + 1."""
    k = max(1, round(seconds * fps / 8))
    return 8 * k + 1


def ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        out({"ok": False, "error": "ffmpeg not found on PATH"}, 1)
    return exe


def probe(path: Path) -> dict:
    exe = shutil.which("ffprobe")
    if not exe or not path.exists():
        return {}
    r = subprocess.run(
        [exe, "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height,r_frame_rate,nb_frames",
         "-of", "json", str(path)], capture_output=True, text=True)
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        return {}
    info = {"duration_s": float(d.get("format", {}).get("duration", 0) or 0), "has_audio": False}
    for s in d.get("streams", []):
        if s.get("codec_type") == "video":
            num, den = (s.get("r_frame_rate") or "0/1").split("/")
            info.update(width=s.get("width"), height=s.get("height"),
                        fps=round(float(num) / float(den or 1), 3), frames=int(s.get("nb_frames") or 0))
        elif s.get("codec_type") == "audio":
            info["has_audio"] = True
    return info


def make_sheet(video: Path, dest: Path, cols: int = 4, rows: int = 2, width: int = 1600) -> Path:
    info = probe(video)
    n = cols * rows
    frames = max(info.get("frames") or 1, 1)
    step = max(frames // n, 1)
    vf = f"select='not(mod(n\\,{step}))',scale={width // cols}:-2,tile={cols}x{rows}"
    subprocess.run([ffmpeg(), "-y", "-v", "error", "-i", str(video), "-vf", vf, "-frames:v", "1", str(dest)],
                   check=True)
    return dest


# --------------------------------------------------------------------------- generate
def cmd_generate(a: argparse.Namespace) -> None:
    cfg = load_config()
    if a.offload:
        cfg["offload"] = a.offload
    if a.quantization:
        cfg["quantization"] = a.quantization
    if a.transformer:
        cfg["transformer"] = a.transformer
    if a.vae:
        cfg["video_vae"] = a.vae
    fps = a.fps or cfg["fps"]

    if a.width and a.height:
        w, h = a.width, a.height
    else:
        w, h = ASPECTS[a.preset][a.aspect]
    if w % 64 or h % 64:
        out({"ok": False, "error": f"width/height must be divisible by 64 (got {w}x{h})"}, 2)

    pipeline = PIPELINES[a.preset]
    if a.ingredients:
        # Reference-sheet consistency (cast.py). Ingredients was trained at 768x448 / 121 frames: stage 1 always
        # runs there; standard/quality then upscale x2 (1536x896) in a LoRA-free stage 2, draft stops at 768x448.
        pipeline = "ic_lora"
        portrait = a.aspect in ("9:16", "3:4")
        w, h = (896, 1536) if portrait else (1536, 896)
        # LoRA fusion can't stream NVFP4. Prefer the fp8 pre-cast copy (make_fp8_checkpoint.py): identical math,
        # half the bytes streamed per step.
        cfg["quantization"] = "fp8-cast"
        cfg["transformer"] = "transformer_fp8" if mpath("transformer_fp8").exists() else "transformer_bf16"
        if not a.frames and not a.seconds:
            a.frames = 121
    seed = a.seed if a.seed is not None else int(time.time()) % 2_147_483_647

    run_dir = Path(a.out_dir).resolve() if a.out_dir else OUTPUTS / f"{dt.datetime.now():%Y%m%d-%H%M%S}_{slugify(a.name or a.prompt)}"
    run_dir.mkdir(parents=True, exist_ok=True)
    video = run_dir / "video.mp4"

    if pipeline == "dfr_pipeline" and cfg["quantization"] == "nvfp4-prequant" and cfg["offload"] != "none":
        # DFR fuses the detailing LoRA into streamed blocks, which only bf16/fp8-cast support.
        cfg["quantization"], cfg["transformer"] = "fp8-cast", "transformer_bf16"
    tkey = cfg["transformer"]
    if cfg["quantization"] == "nvfp4-prequant":
        tkey = "transformer_nvfp4"
    elif tkey == "transformer_nvfp4" or (tkey == "transformer_fp8" and not mpath(tkey).exists()):
        tkey = "transformer_bf16"
    needed = [tkey, "text_encoder", cfg["video_vae"], "audio_vae", "spatial_upsampler"]
    if pipeline == "dfr_pipeline":
        needed.append("detailing_lora")
    if pipeline == "ic_lora":
        needed.append("ingredients_lora")
    if a.seconds is None and a.frames is None:
        needed.append("duration_head")
    missing = [str(mpath(k)) for k in needed if not mpath(k).exists()]
    if missing:
        out({"ok": False, "error": "missing model files", "missing": missing}, 3)

    cmd = [str(VENV_PY), str(Path(__file__).with_name("run_pipeline.py")), pipeline,
           "--transformer-path", str(mpath(tkey)),
           "--text-encoder-path", str(mpath("text_encoder")),
           "--video-vae-path", str(mpath(cfg["video_vae"])),
           "--audio-vae-path", str(mpath("audio_vae")),
           "--spatial-upsampler-path", str(mpath("spatial_upsampler")),
           "--prompt", (f"Reference sheet: {a.sheet_desc or 'the characters, props and location shown in the panels'}"
                        f"\n\nGenerated video: {a.prompt}") if a.ingredients else a.prompt,
           "--seed", str(seed),
           "--width", str(w), "--height", str(h),
           "--frame-rate", str(fps),
           "--output-path", str(video)]
    if pipeline == "dfr_pipeline":
        cmd += ["--detailing-lora", str(mpath("detailing_lora"))]
    if pipeline == "ic_lora":
        sheet = Path(a.ingredients)
        if not sheet.exists():
            out({"ok": False, "error": f"reference sheet not found: {sheet}"}, 2)
        n_frames = a.frames or seconds_to_frames(a.seconds, fps)
        ref = run_dir / "reference_sheet.mp4"
        subprocess.run([ffmpeg(), "-y", "-v", "error", "-loop", "1", "-framerate", str(fps), "-i", str(sheet),
                        "-frames:v", str(max(121, n_frames)), "-vf",
                        f"scale={w // 2}:{h // 2}:force_original_aspect_ratio=decrease,"
                        f"pad={w // 2}:{h // 2}:(ow-iw)/2:(oh-ih)/2:color=black,format=yuv420p",
                        "-c:v", "libx264", "-crf", "12", str(ref)], check=True)
        cmd += ["--lora", str(mpath("ingredients_lora")), str(a.ingredients_strength),
                "--video-conditioning", str(ref), "1.0"]
        if a.preset == "draft":
            cmd += ["--skip-stage-2"]
    if a.frames:
        cmd += ["--num-frames", str(a.frames)]
    elif a.seconds:
        cmd += ["--num-frames", str(seconds_to_frames(a.seconds, fps))]
    else:
        cmd += ["--duration-head-path", str(mpath("duration_head"))]
    if cfg["quantization"] and cfg["quantization"] != "none":
        cmd += ["--quantization", cfg["quantization"]]
    if cfg["offload"] and cfg["offload"] != "none":
        cmd += ["--offload", cfg["offload"]]
    for img in a.image or []:
        path, frame, strength, *binding = img + ["0", "1.0"][len(img) - 1:]
        if not Path(path).exists():
            out({"ok": False, "error": f"image not found: {path}"}, 2)
        cmd += ["--image", str(Path(path).resolve()), str(frame), str(strength), *binding]
    if a.enhance_prompt:
        cmd += ["--enhance-prompt"]
    for extra in a.extra or []:
        cmd += extra.split(" ", 1) if extra.startswith("--") and " " in extra else [extra]

    meta = {"prompt": a.prompt, "preset": a.preset, "pipeline": pipeline, "seed": seed, "width": w, "height": h,
            "fps": fps, "requested_frames": a.frames or (seconds_to_frames(a.seconds, fps) if a.seconds else "auto"),
            "images": a.image or [], "config": cfg, "command": cmd, "started": dt.datetime.now().isoformat()}
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    env = dict(os.environ)
    env.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    env["PYTHONIOENCODING"] = "utf-8"
    t0 = time.time()
    with open(run_dir / "log.txt", "w", encoding="utf-8") as log:
        rc = subprocess.run(cmd, cwd=str(REPO), env=env, stdout=log, stderr=subprocess.STDOUT).returncode
    elapsed = round(time.time() - t0, 1)

    meta["elapsed_s"] = elapsed
    meta["returncode"] = rc
    ok = rc == 0 and video.exists()
    result = {"ok": ok, "video": str(video) if ok else None, "dir": str(run_dir), "seed": seed,
              "elapsed_s": elapsed, "log": str(run_dir / "log.txt")}
    if ok:
        info = probe(video)
        meta["video_info"] = info
        result["video_info"] = info
        if not a.no_sheet:
            try:
                result["sheet"] = str(make_sheet(video, run_dir / "sheet.jpg"))
            except subprocess.CalledProcessError:
                pass
    else:
        tail = (run_dir / "log.txt").read_text(encoding="utf-8", errors="replace").splitlines()[-25:]
        result["error_tail"] = tail
        result["hint"] = hint_for("\n".join(tail))
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    out(result, 0 if ok else 1)


def hint_for(log: str) -> str | None:
    if "out of memory" in log.lower() or "OutOfMemory" in log:
        return "GPU OOM: use preset draft, fewer seconds, --offload disk, or --vae video_vae_conv."
    if "ltx_kernels" in log or "nvfp4" in log.lower():
        return "NVFP4 kernels unavailable: retry with --quantization fp8-cast --transformer transformer_bf16."
    if "paging file" in log.lower() or "MemoryError" in log:
        return "System RAM exhausted: use --offload disk."
    return None


# --------------------------------------------------------------------------- utilities
def cmd_frame(a: argparse.Namespace) -> None:
    src = Path(a.video)
    dest = Path(a.output) if a.output else src.with_name(f"{src.stem}_{a.which}.png")
    if a.which == "last":
        args = ["-sseof", "-0.1", "-i", str(src), "-update", "1", "-frames:v", "1"]
    elif a.which == "first":
        args = ["-i", str(src), "-frames:v", "1"]
    else:
        args = ["-ss", str(float(a.which)), "-i", str(src), "-frames:v", "1"]
    subprocess.run([ffmpeg(), "-y", "-v", "error", *args, str(dest)], check=True)
    out({"ok": True, "image": str(dest)})


def cmd_sheet(a: argparse.Namespace) -> None:
    src = Path(a.video)
    dest = Path(a.output) if a.output else src.with_name(f"{src.stem}_sheet.jpg")
    out({"ok": True, "sheet": str(make_sheet(src, dest, a.cols, a.rows))})


def intro_cut(clip: Path, window: float = 2.5, thr: float = 0.25) -> float:
    """Time of a hard cut inside the first `window` seconds (0 if none). LTX/Ingredients shots sometimes open with
    ~1 s of an unrelated composition and then cut to the real shot (F01 s16/s23, F02 s04)."""
    r = subprocess.run([ffmpeg(), "-hide_banner", "-t", str(window), "-i", str(clip), "-vf",
                        f"select='gt(scene,{thr})',showinfo", "-an", "-f", "null", "-"], capture_output=True, text=True)
    ts = [float(t) for t in re.findall(r"pts_time:([\d.]+)", r.stderr)]
    ts = [t for t in ts if 0.15 < t < window]
    return round(max(ts), 3) if ts else 0.0


def stitch(clips: list[Path], dest: Path, transition: str = "cut", fade_s: float = 0.5,
           size: tuple[int, int] | None = None, trim_intro: bool = False,
           starts: list[float] | None = None) -> dict:
    """Concatenate clips (normalized to `size` or the first clip's size/fps) with hard cuts or crossfades.
    trim_intro drops an unrelated opening composition that hard-cuts to the real shot in the first 1.6 s."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    infos = [probe(c) for c in clips]
    # explicit per-clip trims (from QA: only confirmed sheet-leak intros) beat blind cut detection, which also cut
    # away GOOD opening close-ups that hard-cut to a second angle (F11 s09/s12)
    starts = list(starts) if starts else [intro_cut(c) if trim_intro else 0.0 for c in clips]
    for i, st in zip(infos, starts):
        i["duration_s"] = (i.get("duration_s") or 0) - st
    n = len(clips)
    W, H, fps = infos[0].get("width"), infos[0].get("height"), infos[0].get("fps") or 24
    if size:
        W, H = size
    has_audio = all(i.get("has_audio") for i in infos)
    f = [f"[{i}:v]trim=start={starts[i]},setpts=PTS-STARTPTS,"
         f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{H},"
         f"setsar=1,fps={fps},format=yuv420p,settb=AVTB[v{i}]" for i in range(n)]
    if has_audio:
        f += [f"[{i}:a]atrim=start={starts[i]},asetpts=PTS-STARTPTS,aresample=48000,"
              f"aformat=channel_layouts=stereo[a{i}]" for i in range(n)]
    if transition == "fade" and n > 1:
        fade_s = min(fade_s, *(i["duration_s"] / 2 for i in infos))
        offset, prev_v, prev_a = 0.0, "[v0]", "[a0]"
        for i in range(1, n):
            offset += infos[i - 1]["duration_s"] - fade_s
            f.append(f"{prev_v}[v{i}]xfade=transition=fade:duration={fade_s}:offset={offset:.3f}[xv{i}]")
            prev_v = f"[xv{i}]"
            if has_audio:
                f.append(f"{prev_a}[a{i}]acrossfade=d={fade_s}[xa{i}]")
                prev_a = f"[xa{i}]"
        vout, aout = prev_v, prev_a
    else:
        if has_audio:
            f.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[cv][ca]")
            vout, aout = "[cv]", "[ca]"
        else:
            f.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[cv]")
            vout, aout = "[cv]", None
    if aout:  # generated audio peaks at 0 dBFS (clipping); normalize loudness for the whole film
        f.append(f"{aout}loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[an]")
        aout = "[an]"
    maps = ["-map", vout] + (["-map", aout, "-c:a", "aac", "-b:a", "192k"] if has_audio else [])
    inputs = sum((["-i", str(c)] for c in clips), [])
    subprocess.run([ffmpeg(), "-y", "-v", "error", *inputs, "-filter_complex", ";".join(f), *maps,
                    "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart", str(dest)], check=True)
    return {"ok": True, "video": str(dest), "video_info": probe(dest),
            "trimmed": {str(c.name if False else c): t for c, t in zip(clips, starts) if t}}


def cmd_stitch(a: argparse.Namespace) -> None:
    clips = [Path(c).resolve() for c in a.clips]
    for c in clips:
        if not c.exists():
            out({"ok": False, "error": f"missing clip {c}"}, 2)
    out(stitch(clips, Path(a.output).resolve(), a.transition, a.fade))


# --------------------------------------------------------------------------- storyboard
def cmd_storyboard(a: argparse.Namespace) -> None:
    """Render every shot of a storyboard JSON, then stitch them into final.mp4.

    Resumable: shots whose video already exists are skipped unless listed in --redo.
    """
    sb_path = Path(a.storyboard).resolve()
    sb = json.loads(sb_path.read_text(encoding="utf-8"))
    proj = Path(sb.get("output_dir") or OUTPUTS / slugify(sb.get("title", sb_path.stem)))
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "storyboard.json").write_text(json.dumps(sb, indent=2), encoding="utf-8")
    redo = set(a.redo or [])
    only = set(a.only or [])
    style = sb.get("style", "").strip()
    base_seed = sb.get("seed", 42)
    results, prev_video = [], None
    t0 = time.time()

    for i, shot in enumerate(sb["shots"]):
        sid = shot.get("id") or f"s{i + 1:02d}"
        sdir = proj / "shots" / sid
        video = sdir / "video.mp4"
        skip = (only and sid not in only) or (video.exists() and sid not in redo)
        if skip:
            results.append({"id": sid, "ok": video.exists(), "video": str(video) if video.exists() else None,
                            "skipped": True})
            prev_video = video if video.exists() else None
            continue
        prompt = shot["prompt"].strip() + (f" {style}" if style else "")
        args = [sys.executable, str(Path(__file__).resolve()), "generate", "--prompt", prompt,
                "--preset", shot.get("preset", sb.get("preset", "draft")),
                "--aspect", shot.get("aspect", sb.get("aspect", "16:9")),
                "--seed", str(shot.get("seed", base_seed + i)), "--out-dir", str(sdir)]
        if shot.get("seconds") or sb.get("seconds"):
            args += ["--seconds", str(shot.get("seconds") or sb.get("seconds"))]
        image = shot.get("image")
        if shot.get("continue_from_previous") and prev_video and prev_video.exists():
            image = str(sdir.parent / f"{sid}_start.png")
            subprocess.run([ffmpeg(), "-y", "-v", "error", "-sseof", "-0.1", "-i", str(prev_video), "-update", "1",
                            "-frames:v", "1", image], check=True)
        if image:
            args += ["--image", image, "0", str(shot.get("image_strength", 1.0))]
        if shot.get("end_image"):
            secs = shot.get("seconds") or sb.get("seconds") or 5
            if not (shot.get("seconds") or sb.get("seconds")):
                args += ["--seconds", str(secs)]
            last = seconds_to_frames(secs, sb.get("fps", 24)) - 1
            args += ["--image", shot["end_image"], str(last), "1.0", "guide"]
        print(f"[storyboard] {sid} ({i + 1}/{len(sb['shots'])}) rendering...", file=sys.stderr, flush=True)
        r = subprocess.run(args, capture_output=True, text=True)
        try:
            res = json.loads(r.stdout)
        except json.JSONDecodeError:
            res = {"ok": False, "error": (r.stderr or r.stdout)[-1500:]}
        res["id"] = sid
        results.append(res)
        prev_video = video if res.get("ok") else None
        if not res.get("ok") and not a.keep_going:
            break

    summary = {"title": sb.get("title"), "project_dir": str(proj), "elapsed_s": round(time.time() - t0, 1),
               "shots": [{k: r.get(k) for k in ("id", "ok", "video", "sheet", "seed", "elapsed_s", "skipped",
                                                "hint", "error")} for r in results]}
    good = [Path(r["video"]) for r in results if r.get("ok") and r.get("video")]
    if len(good) == len(sb["shots"]) and not a.no_stitch:
        final = proj / "final.mp4"
        summary["final"] = stitch(good, final, sb.get("transition", "cut"), sb.get("fade_seconds", 0.5))
        try:
            summary["final_sheet"] = str(make_sheet(final, proj / "final_sheet.jpg", cols=6, rows=3))
        except subprocess.CalledProcessError:
            pass
    else:
        summary["final"] = None
        summary["missing"] = [s.get("id") or f"s{i + 1:02d}" for i, s in enumerate(sb["shots"])
                              if not any(r["id"] == (s.get("id") or f"s{i + 1:02d}") and r.get("ok") for r in results)]
    (proj / "result.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    out({"ok": summary["final"] is not None or a.no_stitch, **summary},
        0 if summary["final"] is not None or a.no_stitch else 1)


def cmd_probe(a: argparse.Namespace) -> None:
    out({"ok": True, "video": a.video, "video_info": probe(Path(a.video))})


def cmd_doctor(_: argparse.Namespace) -> None:
    files = {k: {"path": str(mpath(k)), "exists": mpath(k).exists(),
                 "gb": round(mpath(k).stat().st_size / 1e9, 2) if mpath(k).exists() else 0} for k in FILES}
    report = {"repo": REPO.exists(), "venv_python": VENV_PY.exists(), "ffmpeg": bool(shutil.which("ffmpeg")),
              "config": load_config(), "files": files}
    if VENV_PY.exists():
        r = subprocess.run([str(VENV_PY), "-c",
                            "import torch,json;print(json.dumps({'torch':torch.__version__,'cuda':torch.cuda.is_available(),"
                            "'gpu':torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,"
                            "'cc':torch.cuda.get_device_capability(0) if torch.cuda.is_available() else None}))"],
                           capture_output=True, text=True)
        try:
            report["torch"] = json.loads(r.stdout.strip().splitlines()[-1])
        except Exception:
            report["torch"] = {"error": r.stderr[-500:]}
        r = subprocess.run([str(VENV_PY), "-c", "import ltx_kernels; print('ok')"], capture_output=True, text=True)
        report["ltx_kernels"] = r.stdout.strip() == "ok"
    out({"ok": True, **report})


def main() -> None:
    p = argparse.ArgumentParser(prog="ltxgen", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="text/image to video with audio")
    g.add_argument("--prompt", required=True)
    g.add_argument("--preset", choices=list(PIPELINES), default="standard")
    g.add_argument("--aspect", choices=list(ASPECTS["standard"]), default="16:9")
    g.add_argument("--width", type=int)
    g.add_argument("--height", type=int)
    g.add_argument("--seconds", type=float, help="clip length; omit (with --frames) for automatic duration")
    g.add_argument("--frames", type=int, help="exact frame count (8k+1)")
    g.add_argument("--fps", type=float)
    g.add_argument("--seed", type=int)
    g.add_argument("--image", nargs="+", action="append", metavar=("PATH", "FRAME STRENGTH"),
                   help="conditioning image: PATH [FRAME_IDX=0] [STRENGTH=1.0]; repeatable")
    g.add_argument("--enhance-prompt", action="store_true", help="let Gemma rewrite the prompt")
    g.add_argument("--name", help="short name for the output folder")
    g.add_argument("--out-dir")
    g.add_argument("--offload", choices=["none", "cpu", "disk"])
    g.add_argument("--quantization", choices=["nvfp4-prequant", "fp8-cast", "none"])
    g.add_argument("--transformer", choices=["transformer_nvfp4", "transformer_bf16"])
    g.add_argument("--vae", choices=["video_vae", "video_vae_conv"])
    g.add_argument("--no-sheet", action="store_true")
    g.add_argument("--ingredients", help="reference sheet PNG (cast.py sheet ...) to keep characters/props consistent")
    g.add_argument("--sheet-desc", help="text describing the sheet panels (cast.py prints it)")
    g.add_argument("--ingredients-strength", type=float, default=1.0)
    g.add_argument("--extra", action="append", help="raw extra flag passed to the pipeline, e.g. '--num-generated-keyframes 2'")
    g.set_defaults(fn=cmd_generate)

    f = sub.add_parser("frame", help="extract a frame (first|last|<seconds>)")
    f.add_argument("video")
    f.add_argument("--which", default="last")
    f.add_argument("--output")
    f.set_defaults(fn=cmd_frame)

    s = sub.add_parser("sheet", help="contact sheet of a video")
    s.add_argument("video")
    s.add_argument("--cols", type=int, default=4)
    s.add_argument("--rows", type=int, default=2)
    s.add_argument("--output")
    s.set_defaults(fn=cmd_sheet)

    st = sub.add_parser("stitch", help="concatenate clips")
    st.add_argument("clips", nargs="+")
    st.add_argument("--output", required=True)
    st.add_argument("--transition", choices=["cut", "fade"], default="cut")
    st.add_argument("--fade", type=float, default=0.5, help="crossfade seconds")
    st.set_defaults(fn=cmd_stitch)

    sb = sub.add_parser("storyboard", help="render all shots of a storyboard JSON and stitch final.mp4")
    sb.add_argument("storyboard", help="path to storyboard JSON (see ltxgen/storyboard_example.json)")
    sb.add_argument("--redo", nargs="+", help="shot ids to re-render even if they exist")
    sb.add_argument("--only", nargs="+", help="render only these shot ids")
    sb.add_argument("--keep-going", action="store_true", help="continue after a failed shot")
    sb.add_argument("--no-stitch", action="store_true")
    sb.set_defaults(fn=cmd_storyboard)

    pr = sub.add_parser("probe")
    pr.add_argument("video")
    pr.set_defaults(fn=cmd_probe)

    d = sub.add_parser("doctor", help="check installation")
    d.set_defaults(fn=cmd_doctor)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
