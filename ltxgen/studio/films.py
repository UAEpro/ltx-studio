"""Film workflow API for the LTX Studio web app.

A *film* is a folder films/<id>_<slug>/ with film.json (story, cast, locations, props, shots) - the same format the
command-line factory uses. The web app edits film.json and runs factory.py steps as background *jobs*:

  assets   prepare --assets-only   cast portraits + turnarounds, location/prop images (GPU)
  submit   prepare                 build storyboard (roles, opener, blocking, sheets) and queue it in the studio
  qa       qa [--no-speech]        leak / face / bars / speech checks
  autofix  autofix                 redo leaked / frozen / barred shots
  reshot   reshot <shot>           rebuild one edited shot and re-render it
  finish   finish                  copy final.mp4 + contact sheet into the film folder
  bench    bench                   benchmark row (studio/benchmark.json)

A monitor thread runs QA + autofix every 4 finished shots, finish + full QA when done.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import ltxgen  # noqa: E402

ROOT = ltxgen.ROOT
FILMS = ROOT / "films"
CAST = ROOT / "cast"
STUDIO = ROOT / "studio"
JOBS_DIR = STUDIO / "jobs"
FACTORY = HERE.parent / "factory.py"
PY = sys.executable
FILMS.mkdir(parents=True, exist_ok=True)
JOBS_DIR.mkdir(parents=True, exist_ok=True)

router = APIRouter()
STAGES = ("story", "cast", "shots", "render", "review", "final")
GPU_KINDS = {"assets"}  # casting renders share the GPU with the worker (never two pipelines at once)


# --------------------------------------------------------------------------- helpers
def _read(p: Path, default=None):
    try:
        return json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        return default


def _write(p: Path, data) -> None:
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def film_dir(fid: str) -> Path:
    d = FILMS / fid
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", fid) or not (d / "film.json").exists():
        raise HTTPException(404, f"no such film: {fid}")
    return d


def furl(fid: str, p: Path | str | None) -> str | None:
    if not p:
        return None
    p = Path(p)
    try:
        rel = p.resolve().relative_to((FILMS / fid).resolve())
        return f"/api/fs/films/{fid}/{rel.as_posix()}"
    except ValueError:
        pass
    try:
        rel = p.resolve().relative_to(CAST.resolve())
        return f"/api/fs/cast/{rel.as_posix()}"
    except ValueError:
        return None


def _studio():
    import server  # the running studio app (same process)
    return server


def _project(pid: str | None) -> dict | None:
    if not pid:
        return None
    try:
        return _studio().get_project(pid)
    except HTTPException:
        return None


def _bench_rows() -> dict:
    b = _read(STUDIO / "benchmark.json", [])
    rows = b if isinstance(b, list) else b.get("films", [])
    return {r.get("film"): r for r in rows if isinstance(r, dict)}


# --------------------------------------------------------------------------- jobs
_jobs: dict[str, dict] = {}
_jobs_lock = threading.RLock()
_gpu_queue: list[str] = []


def _job_cmd(kind: str, fdir: Path, args: list[str]) -> list[str]:
    base = [PY, str(FACTORY)]
    if kind == "assets":
        return base + ["prepare", str(fdir), "--assets-only"]
    if kind == "submit":
        return base + ["prepare", str(fdir)] + args
    if kind in ("qa", "autofix", "bench", "finish", "reshot", "recast"):
        return base + [kind, str(fdir)] + args
    raise HTTPException(400, f"unknown job kind {kind}")


def start_job(kind: str, fid: str | None, args: list[str] | None = None, cmd: list[str] | None = None,
              title: str | None = None) -> dict:
    jid = uuid.uuid4().hex[:8]
    fdir = FILMS / fid if fid else None
    job = {"id": jid, "kind": kind, "film": fid, "title": title or kind, "status": "queued", "created": time.time(),
           "started": None, "finished": None, "result": None, "error": None,
           "cmd": cmd or _job_cmd(kind, fdir, args or []), "log": str(JOBS_DIR / f"{jid}.log")}
    with _jobs_lock:
        _jobs[jid] = job
        if kind in GPU_KINDS:
            _gpu_queue.append(jid)
        else:
            threading.Thread(target=_run_job, args=(jid,), daemon=True).start()
    return _public(job)


def _run_job(jid: str) -> None:
    job = _jobs[jid]
    job.update(status="running", started=time.time())
    env = {**__import__("os").environ, "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1", "PYTHON_COLORS": "0", "TERM": "dumb"}
    try:
        with open(job["log"], "w", encoding="utf-8") as log:
            proc = subprocess.Popen(job["cmd"], stdout=log, stderr=subprocess.STDOUT, cwd=str(ROOT), env=env)
            job["pid"] = proc.pid
            rc = proc.wait()
        text = re.sub(r"\[[0-9;]*m", "", Path(job["log"]).read_text("utf-8", errors="replace"))
        last = next((ln for ln in reversed(text.strip().splitlines()) if ln.startswith("{")), None)
        job["result"] = json.loads(last) if last else None
        if rc != 0:
            job.update(status="failed", error=(text.strip().splitlines() or ["failed"])[-1][-400:])
        else:
            job["status"] = "done"
    except Exception as e:  # noqa: BLE001
        job.update(status="failed", error=str(e))
    job["finished"] = time.time()


def _gpu_runner() -> None:
    while True:
        jid = None
        with _jobs_lock:
            if _gpu_queue and not any(j["status"] == "running" and j["kind"] in GPU_KINDS for j in _jobs.values()):
                jid = _gpu_queue.pop(0)
        if jid:
            _run_job(jid)
        time.sleep(2)


def _public(job: dict) -> dict:
    return {k: v for k, v in job.items() if k not in ("cmd",)}


def _tail(path: str, n: int = 40) -> list[str]:
    try:
        lines = re.sub(r"\[[0-9;]*m", "", Path(path).read_text("utf-8", errors="replace")).splitlines()
    except OSError:
        return []
    return [ln for ln in lines if "WARN:" not in ln][-n:]


@router.get("/api/jobs")
def list_jobs(film: str | None = None) -> list[dict]:
    with _jobs_lock:
        jobs = [j for j in _jobs.values() if not film or j["film"] == film]
    return [_public(j) for j in sorted(jobs, key=lambda j: j["created"], reverse=True)[:30]]


@router.get("/api/jobs/{jid}")
def get_job(jid: str) -> dict:
    j = _jobs.get(jid)
    if not j:
        raise HTTPException(404, "no such job")
    return {**_public(j), "tail": _tail(j["log"])}


# --------------------------------------------------------------------------- film model
def _cast_info(fid: str, film: dict, state: dict) -> list[dict]:
    out = []
    for name, spec in film.get("cast", {}).items():
        key = state.get("cast", {}).get(name) or f"{film['id']}_{name}"
        d = CAST / key
        out.append({"name": name, "role": spec.get("role", ""), "desc": spec.get("desc", ""),
                    "animal": bool(spec.get("animal")), "pose": spec.get("pose", ""),
                    "ready": (d / "meta.json").exists(),
                    "panel": furl(fid, d / "panel.png") if (d / "panel.png").exists() else None,
                    "portrait": furl(fid, d / "source.png") if (d / "source.png").exists() else None})
    return out


def _asset_info(fid: str, fdir: Path, kind: str, items: dict) -> list[dict]:
    out = []
    for key, spec in items.items():
        img = next((fdir / "assets" / f"{kind}_{key}{e}" for e in (".png", ".jpg")
                    if (fdir / "assets" / f"{kind}_{key}{e}").exists()), None)
        out.append({"key": key, **{k: v for k, v in spec.items() if isinstance(v, (str, bool, int, float))},
                    "ready": img is not None, "image": furl(fid, img) if img else None})
    return out


def _thumb(pid: str, sid: str, version: int) -> str | None:
    vdir = STUDIO / "projects" / pid / "shots" / sid / f"v{version}"
    if not (vdir / "video.mp4").exists():
        return None
    return f"/api/thumb/{pid}/{sid}/{version}"


def film_summary(fdir: Path, bench: dict | None = None) -> dict:
    film = _read(fdir / "film.json", {})
    state = _read(fdir / "state.json", {})
    review = _read(fdir / "review.json", {})
    fid = fdir.name
    shots = film.get("shots", [])
    secs = sum(float(s.get("seconds", 6)) for s in shots)
    proj = None
    counts = {}
    if state.get("project_id"):
        import server
        row = server.q1("SELECT status FROM projects WHERE id=?", (state["project_id"],))
        if row:
            proj = row["status"]
            for r in server.q("SELECT t.status FROM shots s JOIN tasks t ON t.id=s.task_id WHERE s.project_id=?",
                              (state["project_id"],)):
                counts[r["status"]] = counts.get(r["status"], 0) + 1
    final = fdir / "final.mp4"
    has_poster = final.exists() or any((fdir / "assets").glob("loc_*.png")) or any((fdir / "assets").glob("casting_*.png"))
    stage = _stage(film, state, counts, final.exists())
    b = (bench or {}).get(film.get("id"))
    return {"id": fid, "code": film.get("id"), "title": film.get("title", fid), "logline": film.get("logline", ""),
            "shots": len(shots), "seconds": round(secs), "aspect": film.get("aspect", "16:9"),
            "stage": stage, "project_id": state.get("project_id"), "project_status": proj, "counts": counts,
            "final": furl(fid, final) if final.exists() else None,
            "poster": f"/api/poster/{fid}?t={int(final.stat().st_mtime) if final.exists() else 0}" if has_poster else None,
            "score": review.get("overall"), "modified": (fdir / "film.json").stat().st_mtime,
            "bench": {k: b.get(k) for k in ("duration_s", "redos", "face_consistency", "speech_cer_mean",
                                            "final_leaks", "render_min")} if b else None}


def _poster(fdir: Path) -> Path | None:
    """One clean frame for the gallery: from final.mp4 (at ~15%), else the first cast portrait / location."""
    out = fdir / "poster.jpg"
    final = fdir / "final.mp4"
    if final.exists():
        if not out.exists() or out.stat().st_mtime < final.stat().st_mtime:
            dur = (ltxgen.probe(final) or {}).get("duration_s") or 60
            subprocess.run([ltxgen.ffmpeg(), "-y", "-v", "error", "-ss", f"{dur * 0.15:.1f}", "-i", str(final),
                            "-frames:v", "1", "-vf", "scale=640:-2", str(out)], timeout=60)
        if out.exists():
            return out
    for cand in sorted((fdir / "assets").glob("loc_*.png")) + sorted((fdir / "assets").glob("casting_*.png")):
        return cand
    return None


def _stage(film: dict, state: dict, counts: dict, has_final: bool) -> str:
    if has_final:
        return "final"
    if counts:
        total = sum(counts.values())
        return "review" if counts.get("done", 0) == total else "render"
    if not film.get("shots"):
        return "story"
    cast_ok = all((CAST / f"{film.get('id')}_{n}" / "meta.json").exists() for n in film.get("cast", {}))
    return "shots" if cast_ok and state.get("cast") is not None else "cast"


@router.get("/api/films")
def list_films() -> list[dict]:
    bench = _bench_rows()
    out = [film_summary(d, bench) for d in FILMS.iterdir() if (d / "film.json").exists()]
    return sorted(out, key=lambda f: f["modified"], reverse=True)


@router.get("/api/films/{fid}")
def get_film(fid: str) -> dict:
    fdir = film_dir(fid)
    film = _read(fdir / "film.json", {})
    state = _read(fdir / "state.json", {})
    sb = _read(fdir / "storyboard.studio.json", {"shots": []})
    sb_by = {s["id"]: s for s in sb.get("shots", [])}
    qa = (_read(fdir / "qa.json", {}) or {}).get("shots", {})
    proj = _project(state.get("project_id"))
    pshots = {s["id"]: s for s in (proj or {}).get("shots", [])}
    accepted = state.get("accepted", {})
    lint = _lint(film)
    shots = []
    for i, sh in enumerate(film.get("shots", [])):
        ps = pshots.get(sh["id"])
        v = ps["version"] if ps else None
        q = qa.get(f"{sh['id']}@v{v}", {}) if v else {}
        shots.append({**sh, "index": i,
                      "studio_prompt": (ps or {}).get("prompt") or sb_by.get(sh["id"], {}).get("prompt"),
                      "has_sheet": bool(sb_by.get(sh["id"], {}).get("ingredients")),
                      "sheet_img": furl(fid, fdir / "sheets" / f"{sh['id']}.png")
                      if (fdir / "sheets" / f"{sh['id']}.png").exists() else None,
                      "status": (ps or {}).get("status"), "version": v, "versions": (ps or {}).get("versions", []),
                      "video": (ps or {}).get("video"), "contact": (ps or {}).get("sheet"),
                      "thumb": _thumb(state["project_id"], sh["id"], v) if ps and ps.get("video") else None,
                      "progress": (ps or {}).get("progress"), "worker": (ps or {}).get("worker"),
                      "elapsed_s": (ps or {}).get("elapsed_s"), "error": (ps or {}).get("error"),
                      "qa": {"flags": q.get("flags", []), "face": q.get("face_sim"), "leak_s": q.get("leak_seconds"),
                             "cer": q.get("speech_cer"), "heard": q.get("heard")} if q else None,
                      "accepted": accepted.get(sh["id"]) == v and v is not None,
                      "lint": [m for m in lint if m.startswith(sh["id"] + ":")]})
    summary = film_summary(fdir, _bench_rows())
    return {**summary, "film": film, "state": {k: state.get(k) for k in ("project_id", "submitted")},
            "cast": _cast_info(fid, film, state),
            "locations": _asset_info(fid, fdir, "loc", film.get("locations", {})),
            "props": _asset_info(fid, fdir, "prop", film.get("props", {})),
            "assets_review": furl(fid, fdir / "assets_review.jpg") if (fdir / "assets_review.jpg").exists() else None,
            "shots_detail": shots, "lint": [m for m in lint if not re.match(r"s\d+:", m)], "lint_all": lint,
            "review": _read(fdir / "review.json"), "final_sheet": furl(fid, fdir / "final_sheet.jpg")
            if (fdir / "final_sheet.jpg").exists() else None,
            "project": {k: (proj or {}).get(k) for k in ("status", "error", "final", "final_info")} if proj else None,
            "jobs": list_jobs(fid)[:8]}


def _lint(film: dict) -> list[str]:
    try:
        import factory
        return factory.lint(json.loads(json.dumps(film)))
    except Exception as e:  # noqa: BLE001
        return [f"lint unavailable: {e}"]


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:30] or "film"


@router.post("/api/films")
def create_film(body: dict = Body(...)) -> dict:
    title = (body.get("title") or "Untitled").strip()
    nums = [int(m.group(1)) for d in FILMS.iterdir() if (m := re.match(r"F(\d+)_", d.name))]
    code = f"F{max(nums, default=0) + 1:02d}"
    fid = f"{code}_{_slug(title)}"
    (FILMS / fid).mkdir(parents=True)
    film = {"id": code, "title": title, "logline": body.get("logline", ""), "aspect": body.get("aspect", "16:9"),
            "seed": int(body.get("seed") or (int(code[1:]) * 1000 + 10000)), "output_size": [1280, 704],
            "transition": "cut", "style": body.get("style", ""), "dialogue_lang": body.get("dialogue_lang", "en"),
            "length_s": body.get("length_s", 180), "cast": {}, "locations": {}, "props": {}, "shots": []}
    _write(FILMS / fid / "film.json", film)
    return {"id": fid}


@router.put("/api/films/{fid}")
def save_film(fid: str, film: dict = Body(...)) -> dict:
    fdir = film_dir(fid)
    old = _read(fdir / "film.json", {})
    film["id"] = old.get("id", film.get("id"))
    for i, sh in enumerate(film.get("shots", [])):
        sh.setdefault("id", f"s{i + 1:02d}")
    shutil.copyfile(fdir / "film.json", fdir / "film.json.bak")
    _write(fdir / "film.json", film)
    return {"ok": True, "lint": _lint(film)}


@router.delete("/api/films/{fid}")
def delete_film(fid: str) -> dict:
    fdir = film_dir(fid)
    trash = FILMS / "_trash"
    trash.mkdir(exist_ok=True)
    shutil.move(str(fdir), str(trash / f"{fid}_{int(time.time())}"))  # recoverable, never hard-deleted
    return {"ok": True}


@router.post("/api/films/{fid}/jobs/{kind}")
def film_job(fid: str, kind: str, body: dict = Body(default={})) -> dict:
    film_dir(fid)
    args = [str(a) for a in body.get("args", [])]
    if kind == "submit" and body.get("force"):
        args.append("--force")
    return start_job(kind, fid, args, title=body.get("title"))


# --------------------------------------------------------------------------- shot actions
def _state(fid: str) -> tuple[Path, dict]:
    fdir = film_dir(fid)
    return fdir, _read(fdir / "state.json", {})


@router.post("/api/films/{fid}/shots/{sid}/accept")
def accept_shot(fid: str, sid: str, body: dict = Body(default={})) -> dict:
    fdir, state = _state(fid)
    proj = _project(state.get("project_id"))
    v = next((s["version"] for s in (proj or {}).get("shots", []) if s["id"] == sid), None)
    acc = state.setdefault("accepted", {})
    if body.get("undo"):
        acc.pop(sid, None)
    else:
        acc[sid] = v
    _write(fdir / "state.json", state)
    return {"ok": True, "accepted": acc}


@router.post("/api/films/{fid}/shots/{sid}/redo")
def redo_shot(fid: str, sid: str, body: dict = Body(default={})) -> dict:
    """mode: new_seed | edited (film.json already saved) | text_only. Rebuilds the shot through factory reshot."""
    fdir, state = _state(fid)
    if not state.get("project_id"):
        raise HTTPException(409, "film is not rendering yet")
    film = _read(fdir / "film.json", {})
    sh = next((s for s in film["shots"] if s["id"] == sid), None)
    if not sh:
        raise HTTPException(404, "no such shot")
    args = [sid]
    if body.get("mode", "new_seed") in ("new_seed", "text_only"):
        proj = _project(state["project_id"])
        cur = next((s["params"]["seed"] for s in proj["shots"] if s["id"] == sid), sh.get("seed", 0))
        args += ["--seed", str(int(cur) + 100)]
    if body.get("mode") == "text_only":
        args.append("--no-sheet")
    state.get("accepted", {}).pop(sid, None)
    _write(fdir / "state.json", state)
    return start_job("reshot", fid, args, title=f"redo {sid}")


@router.post("/api/films/{fid}/shots/{sid}/version")
def use_version(fid: str, sid: str, body: dict = Body(...)) -> dict:
    _, state = _state(fid)
    return _studio().use_version(state["project_id"], sid, {"version": int(body["version"])})


@router.post("/api/films/{fid}/shots/{sid}/trim")
def trim_shot(fid: str, sid: str, body: dict = Body(...)) -> dict:
    """Cut the first N seconds of a shot (e.g. a short sheet flash) and re-stitch."""
    fdir, state = _state(fid)
    sec = max(0.0, min(5.0, float(body.get("seconds", 0))))
    film = _read(fdir / "film.json", {})
    for sh in film["shots"]:
        if sh["id"] == sid:
            if sec:
                sh["trim"] = sec
            else:
                sh.pop("trim", None)
    _write(fdir / "film.json", film)
    pid = state.get("project_id")
    if pid:
        tf = STUDIO / "projects" / pid / "trims.json"
        trims = _read(tf, {}) or {}
        if sec:
            trims[sid] = sec
        else:
            trims.pop(sid, None)
        _write(tf, trims)
    return {"ok": True, "trim": sec}


@router.post("/api/films/{fid}/restitch")
def restitch(fid: str) -> dict:
    _, state = _state(fid)
    res = _studio().stitch_project(state["project_id"])
    start_job("finish", fid, title="update final film")
    return {"ok": True, "duration_s": (res.get("video_info") or {}).get("duration_s")}


@router.post("/api/films/{fid}/review")
def save_review(fid: str, body: dict = Body(...)) -> dict:
    fdir = film_dir(fid)
    rv = _read(fdir / "review.json", {}) or {}
    rv.update({k: body[k] for k in ("overall", "summary", "issues_found") if k in body})
    _write(fdir / "review.json", rv)
    start_job("bench", fid, title="benchmark")
    return {"ok": True}


# --------------------------------------------------------------------------- files + thumbs
@router.get("/api/fs/films/{fid}/{path:path}", response_model=None)
def fs_film(fid: str, path: str) -> FileResponse:
    base = (FILMS / fid).resolve()
    t = (base / path).resolve()
    if base not in t.parents or not t.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(t)


@router.get("/api/fs/cast/{path:path}", response_model=None)
def fs_cast(path: str) -> FileResponse:
    base = CAST.resolve()
    t = (base / path).resolve()
    if base not in t.parents or not t.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(t)


@router.get("/api/poster/{fid}", response_model=None)
def poster(fid: str) -> FileResponse:
    p = _poster(film_dir(fid))
    if not p:
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "max-age=86400"})


@router.get("/api/thumb/{pid}/{sid}/{version}", response_model=None)
def thumb(pid: str, sid: str, version: int) -> FileResponse:
    vdir = (STUDIO / "projects" / pid / "shots" / sid / f"v{version}").resolve()
    if (STUDIO / "projects").resolve() not in vdir.parents:
        raise HTTPException(404)
    out = vdir / "thumb.jpg"
    if not out.exists():
        src = vdir / "video.mp4"
        if not src.exists():
            raise HTTPException(404)
        subprocess.run([ltxgen.ffmpeg(), "-y", "-v", "error", "-ss", "2.5", "-i", str(src), "-frames:v", "1",
                        "-vf", "scale=480:-2", str(out)], timeout=60)
    if not out.exists():
        raise HTTPException(404)
    return FileResponse(out)


# --------------------------------------------------------------------------- AI writer
def _writer_prompt(film: dict, brief: str) -> str:
    guide = (HERE / "film_prompt.md").read_text("utf-8")
    length = int(film.get("length_s") or 180)
    n = max(6, round(length / 7))
    return (guide + f"\n\n## Required length\nExactly {n} shots of about 7 seconds (total about {length} seconds). "
            f"Dialogue in {film.get('dialogue_lang', 'en')}.\n\n## The brief\n" + brief +
            "\n\n## Current film.json (keep id, fill/replace the rest)\n" +
            json.dumps({k: film.get(k) for k in ("id", "title", "logline", "aspect", "style", "dialogue_lang",
                                                 "length_s")}, ensure_ascii=False))


@router.post("/api/films/{fid}/write")
def write_film(fid: str, body: dict = Body(...)) -> dict:
    """Story brief -> full film.json (cast, locations, props, shots) by Claude or local Ollama."""
    fdir = film_dir(fid)
    film = _read(fdir / "film.json", {})
    brief = (body.get("brief") or film.get("logline") or "").strip()
    if not brief:
        raise HTTPException(400, "write a story first")
    server = _studio()
    prompt = _writer_prompt(film, brief)
    engine = body.get("engine") or ("claude" if server._claude_available() else "local")
    try:
        if engine == "claude":
            out = server._write_with_claude(prompt)
        else:
            model = body.get("model") or server.LOCAL_MODEL
            out = _ollama_json(prompt, model)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"{engine} writer failed: {e}") from e
    m = re.search(r"\{.*\}", out, re.S)
    try:
        new = json.loads(m.group(0)) if m else None
    except ValueError:
        new = None
    if not isinstance(new, dict) or not new.get("shots"):
        raise HTTPException(502, "the writer did not return a valid film (no shots)")
    new["id"] = film["id"]
    new["length_s"] = film.get("length_s", 180)
    new.setdefault("logline", brief)
    for k in ("seed", "output_size", "transition"):
        new.setdefault(k, film.get(k))
    shutil.copyfile(fdir / "film.json", fdir / "film.json.bak")
    _write(fdir / "film.json", new)
    return {"ok": True, "engine": engine, "shots": len(new["shots"]), "lint": _lint(new)}


def _ollama_json(prompt: str, model: str) -> str:
    import urllib.request
    server = _studio()
    req = urllib.request.Request(f"{server.OLLAMA_URL}/api/chat", method="POST",
                                 headers={"Content-Type": "application/json"},
                                 data=json.dumps({"model": model, "stream": False, "format": "json", "keep_alive": 0,
                                                  "think": False, "options": {"temperature": 0.7, "num_ctx": 32768},
                                                  "messages": [{"role": "user", "content": prompt}]}).encode())
    with urllib.request.urlopen(req, timeout=1800) as r:
        return json.load(r)["message"]["content"]


# --------------------------------------------------------------------------- monitor
_mon: dict[str, dict] = {}


def _monitor() -> None:
    while True:
        try:
            for d in FILMS.iterdir():
                st = _read(d / "state.json", {}) if (d / "film.json").exists() else None
                if not st or not st.get("project_id") or (d / ".no_monitor").exists():
                    continue
                proj = _project(st["project_id"])
                if not proj:
                    continue
                m = _mon.setdefault(d.name, {"qa_done": None, "finished": None})
                done = [s for s in proj["shots"] if s["status"] == "done"]
                key = ",".join(f"{s['id']}{s['version']}" for s in done)
                busy = any(j["film"] == d.name and j["status"] in ("queued", "running") for j in _jobs.values())
                if busy:
                    continue
                if proj["status"] == "rendering" and done and m["qa_done"] != key and \
                        (m["qa_done"] is None or len(done) - m.get("qa_n", 0) >= 4):
                    m.update(qa_done=key, qa_n=len(done))
                    _chain(d.name, [("qa", ["--no-speech"]), ("autofix", [])], "QA + autofix")
                elif proj["status"] == "done" and proj.get("final") and m["finished"] != key and \
                        (not (d / "final.mp4").exists() or
                         (d / "final.mp4").stat().st_mtime < Path(STUDIO / "projects" / st["project_id"] /
                                                                    "final.mp4").stat().st_mtime):
                    m["finished"] = key
                    # Whisper during another film's render can crash it -> speech QA only when the GPU is free
                    _chain(d.name, [("finish", []), ("qa", [] if not _gpu_busy() else ["--no-speech"])], "finish film")
        except Exception as e:  # noqa: BLE001
            print("[film monitor]", e, file=sys.stderr)
        time.sleep(20)


def _gpu_busy() -> bool:
    return bool(_studio().q1("SELECT id FROM tasks WHERE status='claimed'"))


def _chain(fid: str, steps: list[tuple[str, list[str]]], title: str) -> None:
    def run():
        for kind, args in steps:
            j = start_job(kind, fid, args, title=title)
            while _jobs[j["id"]]["status"] in ("queued", "running"):
                time.sleep(2)
    threading.Thread(target=run, daemon=True).start()


def start_background() -> None:
    threading.Thread(target=_gpu_runner, daemon=True).start()
    threading.Thread(target=_monitor, daemon=True).start()
