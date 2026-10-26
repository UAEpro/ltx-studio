"""MCP server exposing LTX-2.5 video generation to agents.

Generation is slow (minutes), so it is job based: `generate_video` enqueues and returns a
job id; `wait_for_job` blocks up to a timeout; jobs run one at a time (single GPU).
All heavy lifting is delegated to ltxgen.py in a subprocess.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from queue import Queue

from mcp.server.fastmcp import FastMCP, Image

HERE = Path(__file__).parent
LTXGEN = HERE / "ltxgen.py"
JOBS_FILE = HERE / "jobs.json"

mcp = FastMCP("ltx-video")
_jobs: dict[str, dict] = {}
_queue: Queue[str] = Queue()
_lock = threading.Lock()
_current: dict[str, subprocess.Popen] = {}


def _save() -> None:
    with _lock:
        JOBS_FILE.write_text(json.dumps(_jobs, indent=2), encoding="utf-8")


def _run_ltxgen(args: list[str], timeout: float | None = None) -> dict:
    r = subprocess.run([sys.executable, str(LTXGEN), *args], capture_output=True, text=True, timeout=timeout)
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": (r.stderr or r.stdout)[-2000:]}


def _worker() -> None:
    while True:
        job_id = _queue.get()
        job = _jobs[job_id]
        if job["status"] == "cancelled":
            continue
        job.update(status="running", started=time.time())
        _save()
        proc = subprocess.Popen([sys.executable, str(LTXGEN), *job["args"]], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        _current[job_id] = proc
        stdout, stderr = proc.communicate()
        _current.pop(job_id, None)
        try:
            result = json.loads(stdout)
        except json.JSONDecodeError:
            result = {"ok": False, "error": (stderr or stdout)[-2000:]}
        if job["status"] != "cancelled":
            job.update(status="done" if result.get("ok") else "failed", result=result, finished=time.time())
        _save()


threading.Thread(target=_worker, daemon=True).start()


@mcp.tool()
def generate_video(
    prompt: str,
    preset: str = "standard",
    aspect: str = "16:9",
    seconds: float | None = 5.0,
    seed: int | None = None,
    image_path: str | None = None,
    image_frame: int = 0,
    image_strength: float = 1.0,
    end_image_path: str | None = None,
    enhance_prompt: bool = False,
    name: str | None = None,
) -> dict:
    """Queue an LTX-2.5 text/image-to-video generation (video + synchronized audio).

    prompt: detailed cinematic description - subject, action over time, camera move, lighting,
        style, and audio (dialogue in quotes, ambience, music). One flowing paragraph works best.
    preset: "draft" (768x448, fastest, for iteration), "standard" (1280x704), "quality" (DFR, 1536x832, slow).
    aspect: "16:9" | "9:16" | "1:1" | "4:3" | "3:4".
    seconds: clip length (max ~20). None = model predicts duration from the prompt.
    image_path: optional start image (image-to-video). image_frame/image_strength place it.
    end_image_path: optional image to guide the final frame (keyframe interpolation).
    Returns {job_id}. Then call wait_for_job(job_id).
    """
    args = ["generate", "--prompt", prompt, "--preset", preset, "--aspect", aspect]
    if seconds is not None:
        args += ["--seconds", str(seconds)]
    if seed is not None:
        args += ["--seed", str(seed)]
    if image_path:
        args += ["--image", image_path, str(image_frame), str(image_strength)]
    if end_image_path:
        fps = 24
        frames = 8 * max(1, round((seconds or 5) * fps / 8)) + 1
        args += ["--image", end_image_path, str(frames - 1), "1.0", "guide"]
    if enhance_prompt:
        args += ["--enhance-prompt"]
    if name:
        args += ["--name", name]
    job_id = uuid.uuid4().hex[:8]
    _jobs[job_id] = {"id": job_id, "status": "queued", "prompt": prompt, "preset": preset, "args": args,
                     "queued": time.time()}
    _save()
    _queue.put(job_id)
    return {"job_id": job_id, "position": _queue.qsize()}


@mcp.tool()
def wait_for_job(job_id: str, timeout_s: int = 540) -> dict:
    """Block until a job finishes or timeout_s elapses (call again if still running).
    On success returns video path, contact-sheet path, seed, timing and video info."""
    end = time.time() + timeout_s
    while time.time() < end:
        job = _jobs.get(job_id)
        if job is None:
            return {"error": f"unknown job {job_id}"}
        if job["status"] in ("done", "failed", "cancelled"):
            return job
        time.sleep(3)
    job = dict(_jobs[job_id])
    if job.get("started"):
        job["running_for_s"] = round(time.time() - job["started"])
    return job


@mcp.tool()
def list_jobs(limit: int = 20) -> list[dict]:
    """Most recent jobs with status and result summary."""
    jobs = sorted(_jobs.values(), key=lambda j: j["queued"], reverse=True)[:limit]
    return [{k: j.get(k) for k in ("id", "status", "prompt", "preset")} |
            {"video": (j.get("result") or {}).get("video")} for j in jobs]


@mcp.tool()
def cancel_job(job_id: str) -> dict:
    """Cancel a queued or running job."""
    job = _jobs.get(job_id)
    if not job:
        return {"error": "unknown job"}
    job["status"] = "cancelled"
    proc = _current.get(job_id)
    if proc:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    _save()
    return {"cancelled": job_id}


@mcp.tool()
def view_video(video_path: str, cols: int = 4, rows: int = 2) -> Image:
    """Return a contact-sheet image (grid of evenly spaced frames) so you can visually
    check a generated clip for prompt adherence, artifacts and continuity."""
    sheet = Path(video_path).with_name(Path(video_path).stem + f"_sheet_{cols}x{rows}.jpg")
    r = _run_ltxgen(["sheet", video_path, "--cols", str(cols), "--rows", str(rows), "--output", str(sheet)])
    if not r.get("ok"):
        raise RuntimeError(r.get("error"))
    return Image(path=r["sheet"])


@mcp.tool()
def extract_frame(video_path: str, which: str = "last", output_path: str | None = None) -> dict:
    """Extract a frame ("first", "last", or a time in seconds) as PNG. Use the last frame as
    image_path of the next generate_video call to continue a shot seamlessly."""
    args = ["frame", video_path, "--which", which]
    if output_path:
        args += ["--output", output_path]
    return _run_ltxgen(args)


@mcp.tool()
def stitch_videos(clip_paths: list[str], output_path: str, transition: str = "cut", fade_seconds: float = 0.5) -> dict:
    """Concatenate clips (with audio) into one video. transition: "cut" or "fade" (crossfade)."""
    return _run_ltxgen(["stitch", *clip_paths, "--output", output_path, "--transition", transition,
                        "--fade", str(fade_seconds)])


def _enqueue(args: list[str], label: str, preset: str) -> dict:
    job_id = uuid.uuid4().hex[:8]
    _jobs[job_id] = {"id": job_id, "status": "queued", "prompt": label, "preset": preset, "args": args,
                     "queued": time.time()}
    _save()
    _queue.put(job_id)
    return {"job_id": job_id, "position": _queue.qsize()}


@mcp.tool()
def render_storyboard(storyboard: dict, redo: list[str] | None = None, only: list[str] | None = None) -> dict:
    """Queue a multi-scene film: renders every shot, then stitches them into final.mp4 (with audio).

    storyboard = {
      "title": "Robot and the Flower",
      "preset": "draft",            # draft for review pass; re-run with "standard"/"quality" for finals
      "aspect": "16:9", "seed": 100, "seconds": 5,
      "style": "Shared style sentence appended to every prompt (look, palette, film stock, lens).",
      "transition": "cut" | "fade", "fade_seconds": 0.5,
      "shots": [
        {"id": "s01", "prompt": "...", "seconds": 6},
        {"id": "s02", "prompt": "...", "continue_from_previous": true},   # starts from s01's last frame
        {"id": "s03", "prompt": "...", "image": "D:/refs/hero.png", "seed": 7},
        {"id": "s04", "prompt": "...", "end_image": "D:/refs/end.png"}      # guide the final frame
      ]
    }
    Resumable: existing shots are skipped; pass redo=["s03"] to re-render specific shots, only=[...] to
    render a subset. Output in D:/video/outputs/<title-slug>/ (shots/<id>/video.mp4, final.mp4, result.json).
    Returns {job_id}; poll with wait_for_job (call repeatedly; a film takes a long time).
    """
    path = HERE / "storyboards"
    path.mkdir(exist_ok=True)
    file = path / f"{time.strftime('%Y%m%d-%H%M%S')}_{uuid.uuid4().hex[:4]}.json"
    file.write_text(json.dumps(storyboard, indent=2), encoding="utf-8")
    args = ["storyboard", str(file), "--keep-going"]
    if redo:
        args += ["--redo", *redo]
    if only:
        args += ["--only", *only]
    return _enqueue(args, f"storyboard: {storyboard.get('title', file.stem)}", storyboard.get("preset", "draft"))


@mcp.tool()
def probe_video(video_path: str) -> dict:
    """Duration, resolution, fps, frame count and audio presence of a video."""
    return _run_ltxgen(["probe", video_path])


@mcp.tool()
def doctor() -> dict:
    """Check the LTX-2.5 installation: model files, torch/CUDA, kernels, active config."""
    return _run_ltxgen(["doctor"], timeout=120)


if __name__ == "__main__":
    mcp.run()
