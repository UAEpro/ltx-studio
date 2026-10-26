"""LTX Studio server: web dashboard + distributed shot queue for LTX-2.5 (see PROTOCOL.md).

Run:  D:\\video\\ltxgen\\.venv\\Scripts\\python.exe D:\\video\\ltxgen\\studio\\server.py [--host 0.0.0.0] [--port 7860]
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import ltxgen  # noqa: E402  (stdlib helpers: stitch, make_sheet, probe, seconds_to_frames, ffmpeg)

sys.modules.setdefault("server", sys.modules[__name__])  # films.py / setup.py import "server" -> this module, not a copy

DATA = ltxgen.ROOT / "studio"
PROJECTS = DATA / "projects"
DB_PATH = DATA / "studio.db"
CONFIG_PATH = DATA / "studio.json"
LEASE_S = 15 * 60
OFFLINE_S = 90
PRESETS = ("draft", "standard", "quality")
ASPECTS = tuple(ltxgen.ASPECTS["standard"])

DATA.mkdir(parents=True, exist_ok=True)
PROJECTS.mkdir(parents=True, exist_ok=True)
if not CONFIG_PATH.exists():
    CONFIG_PATH.write_text(json.dumps({"token": secrets.token_hex(16), "port": 7860}, indent=2), encoding="utf-8")
CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
if CONFIG.get("anthropic_api_key"):  # saved from the web app's Settings page
    import os as _os
    _os.environ.setdefault("ANTHROPIC_API_KEY", CONFIG["anthropic_api_key"])

_lock = threading.RLock()
_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.row_factory = sqlite3.Row
_db.executescript("""
CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, title TEXT, created REAL, storyboard TEXT,
  status TEXT, final TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS shots(project_id TEXT, shot_id TEXT, idx INT, prompt TEXT, params TEXT, dep TEXT,
  version INT, task_id TEXT, PRIMARY KEY(project_id, shot_id));
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, project_id TEXT, shot_id TEXT, version INT, status TEXT,
  worker_id TEXT, params TEXT, inputs TEXT, created REAL, claimed REAL, lease REAL, finished REAL,
  result TEXT, error TEXT, progress TEXT, cancel INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS workers(id TEXT PRIMARY KEY, info TEXT, last_seen REAL, status TEXT, task_id TEXT,
  progress TEXT, completed INT DEFAULT 0);
""")


def q(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    with _lock:
        cur = _db.execute(sql, args)
        _db.commit()
        return cur.fetchall()


def q1(sql: str, args: tuple = ()) -> sqlite3.Row | None:
    rows = q(sql, args)
    return rows[0] if rows else None


def new_id() -> str:
    return uuid.uuid4().hex[:8]


def check_token(token: str | None) -> None:
    if token != CONFIG["token"]:
        raise HTTPException(401, "bad or missing X-LTX-Token")


def pdir(pid: str) -> Path:
    return PROJECTS / pid


def file_url(pid: str, path: Path) -> str:
    return f"/api/files/{pid}/{path.relative_to(pdir(pid)).as_posix()}"


# --------------------------------------------------------------------------- storyboard → tasks
def validate_storyboard(sb: dict) -> dict:
    if not isinstance(sb, dict) or not isinstance(sb.get("shots"), list) or not sb["shots"]:
        raise HTTPException(400, "storyboard must be an object with a non-empty 'shots' list")
    if sb.get("preset", "draft") not in PRESETS:
        raise HTTPException(400, f"preset must be one of {PRESETS}")
    if sb.get("aspect", "16:9") not in ASPECTS:
        raise HTTPException(400, f"aspect must be one of {ASPECTS}")
    seen = set()
    for i, shot in enumerate(sb["shots"]):
        if not isinstance(shot, dict) or not str(shot.get("prompt", "")).strip():
            raise HTTPException(400, f"shot #{i + 1} needs a non-empty 'prompt'")
        shot.setdefault("id", f"s{i + 1:02d}")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", str(shot["id"])) or shot["id"] in seen:
            raise HTTPException(400, f"shot id {shot['id']!r} invalid or duplicated")
        seen.add(shot["id"])
        secs = shot.get("seconds", sb.get("seconds"))
        if secs is not None and not (1 <= float(secs) <= 20):
            raise HTTPException(400, f"shot {shot['id']}: seconds must be 1-20")
        for key in ("image", "end_image"):
            if shot.get(key) and not Path(shot[key]).is_file():
                raise HTTPException(400, f"shot {shot['id']}: {key} not found on server: {shot[key]}")
        ing = shot.get("ingredients")
        if ing and not Path(ing.get("sheet", "")).is_file():
            raise HTTPException(400, f"shot {shot['id']}: ingredients sheet not found on server: {ing.get('sheet')}")
    return sb


def shot_params(sb: dict, shot: dict, i: int) -> dict:
    style = (shot.get("style", sb.get("style")) or "").strip()  # per-shot style override
    return {"prompt": shot["prompt"].strip() + (f" {style}" if style else ""),
            "preset": shot.get("preset", sb.get("preset", "draft")),
            "aspect": shot.get("aspect", sb.get("aspect", "16:9")),
            "seconds": shot.get("seconds", sb.get("seconds", 5)),
            "seed": int(shot.get("seed", sb.get("seed", 1000) + i))}


def static_inputs(pid: str, shot: dict, params: dict) -> dict:
    inputs: dict = {"start_image": None, "end_image": None}
    ing = shot.get("ingredients")  # {"sheet": path, "desc": "...", "strength": 1.0} -> Ingredients IC-LoRA
    if ing and ing.get("sheet"):
        src = Path(ing["sheet"])
        dest = pdir(pid) / "inputs" / f"{shot['id']}_sheet{src.suffix.lower() or '.png'}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        inputs["ingredients"] = {"url": file_url(pid, dest), "desc": ing.get("desc", ""),
                                 "strength": float(ing.get("strength", 1.0))}
    for key, frame in (("image", 0), ("end_image", None)):
        if not shot.get(key):
            continue
        src = Path(shot[key])
        dest = pdir(pid) / "inputs" / f"{shot['id']}_{key}{src.suffix.lower()}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        if key == "image":
            inputs["start_image"] = {"url": file_url(pid, dest), "frame": 0,
                                     "strength": float(shot.get("image_strength", 1.0))}
        else:
            last = ltxgen.seconds_to_frames(float(params["seconds"] or 5), 24) - 1
            inputs["end_image"] = {"url": file_url(pid, dest), "frame": last, "strength": 1.0, "binding": "guide"}
    return inputs


def create_task(pid: str, sid: str, version: int, params: dict, inputs: dict, has_dep: bool) -> str:
    tid = new_id()
    q("INSERT INTO tasks(id,project_id,shot_id,version,status,params,inputs,created) VALUES(?,?,?,?,?,?,?,?)",
      (tid, pid, sid, version, "pending" if has_dep else "ready", json.dumps(params), json.dumps(inputs), time.time()))
    q("UPDATE shots SET version=?, task_id=? WHERE project_id=? AND shot_id=?", (version, tid, pid, sid))
    return tid


def create_project(sb: dict) -> str:
    sb = validate_storyboard(sb)
    pid = new_id()
    (pdir(pid) / "inputs").mkdir(parents=True, exist_ok=True)
    (pdir(pid) / "storyboard.json").write_text(json.dumps(sb, indent=2), encoding="utf-8")
    q("INSERT INTO projects(id,title,created,storyboard,status) VALUES(?,?,?,?,?)",
      (pid, sb.get("title") or "Untitled", time.time(), json.dumps(sb), "rendering"))
    prev = None
    for i, shot in enumerate(sb["shots"]):
        params = shot_params(sb, shot, i)
        dep = prev if shot.get("continue_from_previous") and prev else ""
        q("INSERT INTO shots(project_id,shot_id,idx,prompt,params,dep,version) VALUES(?,?,?,?,?,?,0)",
          (pid, shot["id"], i, shot["prompt"], json.dumps(params), dep))
        create_task(pid, shot["id"], 1, params, static_inputs(pid, shot, params), bool(dep))
        prev = shot["id"]
    return pid


# --------------------------------------------------------------------------- scheduler
def _promote_pending() -> None:
    for t in q("SELECT * FROM tasks WHERE status='pending'"):
        shot = q1("SELECT * FROM shots WHERE project_id=? AND shot_id=?", (t["project_id"], t["shot_id"]))
        if not shot or shot["task_id"] != t["id"]:
            continue
        dep = q1("SELECT t.* FROM shots s JOIN tasks t ON t.id=s.task_id WHERE s.project_id=? AND s.shot_id=?",
                 (t["project_id"], shot["dep"]))
        if not dep or dep["status"] != "done":
            continue
        video = Path(json.loads(dep["result"])["video_path"])
        frame = pdir(t["project_id"]) / "inputs" / f"{t['shot_id']}_start_from_{shot['dep']}_v{dep['version']}.png"
        try:
            subprocess.run([ltxgen.ffmpeg(), "-y", "-v", "error", "-sseof", "-0.1", "-i", str(video), "-update", "1",
                            "-frames:v", "1", str(frame)], check=True, timeout=60)
        except Exception as e:  # noqa: BLE001
            q("UPDATE tasks SET status='failed', error=? WHERE id=?", (f"continuity frame extraction failed: {e}", t["id"]))
            continue
        inputs = json.loads(t["inputs"])
        inputs["start_image"] = {"url": file_url(t["project_id"], frame), "frame": 0, "strength": 1.0}
        q("UPDATE tasks SET status='ready', inputs=? WHERE id=?", (json.dumps(inputs), t["id"]))


def _expire_leases() -> None:
    q("UPDATE tasks SET status='ready', worker_id=NULL, progress='lease expired, requeued' "
      "WHERE status='claimed' AND lease < ?", (time.time(),))


def _auto_stitch() -> None:
    for p in q("SELECT id FROM projects WHERE status='rendering'"):
        states = [r["status"] for r in q("SELECT t.status FROM shots s JOIN tasks t ON t.id=s.task_id "
                                         "WHERE s.project_id=?", (p["id"],))]
        if states and all(s == "done" for s in states):
            try:
                stitch_project(p["id"])
            except Exception as e:  # noqa: BLE001
                q("UPDATE projects SET status='stitch_failed', error=? WHERE id=?", (str(e)[-500:], p["id"]))


def scheduler() -> None:
    while True:
        try:
            _expire_leases()
            _promote_pending()
            _auto_stitch()
        except Exception as e:  # noqa: BLE001
            print("[scheduler]", e, file=sys.stderr)
        time.sleep(3)


def stitch_project(pid: str) -> dict:
    p = q1("SELECT * FROM projects WHERE id=?", (pid,))
    if not p:
        raise HTTPException(404, "no such project")
    sb = json.loads(p["storyboard"])
    rows = q("SELECT s.shot_id, t.status, t.result FROM shots s JOIN tasks t ON t.id=s.task_id "
             "WHERE s.project_id=? ORDER BY s.idx", (pid,))
    missing = [r["shot_id"] for r in rows if r["status"] != "done"]
    if missing:
        raise HTTPException(409, f"shots not done yet: {missing}")
    q("UPDATE projects SET status='stitching' WHERE id=?", (pid,))
    clips = [Path(json.loads(r["result"])["video_path"]) for r in rows]
    final = pdir(pid) / "final.mp4"
    size = tuple(sb["output_size"]) if sb.get("output_size") else None
    trims_f = pdir(pid) / "trims.json"  # {shot_id: seconds} written by factory QA for confirmed intro leaks
    trims = json.loads(trims_f.read_text("utf-8")) if trims_f.exists() else {}
    starts = [float(trims.get(r["shot_id"], 0.0)) for r in rows]
    res = ltxgen.stitch(clips, final, sb.get("transition", "cut"), float(sb.get("fade_seconds", 0.5)), size,
                        starts=starts)
    try:
        ltxgen.make_sheet(final, pdir(pid) / "final_sheet.jpg", cols=6, rows=3)
    except subprocess.CalledProcessError:
        pass
    q("UPDATE projects SET status='done', final=?, error=NULL WHERE id=?", (json.dumps(res["video_info"]), pid))
    return res


# --------------------------------------------------------------------------- app
app = FastAPI(title="LTX Studio")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
app.mount("/ui", StaticFiles(directory=HERE / "ui"), name="ui")
import films  # noqa: E402  (film workflow: films, jobs, shot actions, monitor)
import setup_page as setup_mod  # noqa: E402  (setup page: checks + installers)
app.include_router(films.router)
app.include_router(setup_mod.router)


@app.middleware("http")
async def no_cache_ui(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/ui/"):  # no build step / hashed names: always revalidate after an update
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/", response_class=HTMLResponse, response_model=None)
def index() -> FileResponse:
    return FileResponse(HERE / "ui" / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/classic", response_class=HTMLResponse, response_model=None)
def classic() -> FileResponse:
    return FileResponse(HERE / "static" / "index.html")


# ---- worker endpoints
@app.post("/api/workers/register")
async def register(req: Request, x_ltx_token: str | None = Header(None)) -> dict:
    check_token(x_ltx_token)
    info = await req.json()
    wid = info["worker_id"]
    q("INSERT INTO workers(id,info,last_seen,status) VALUES(?,?,?,'idle') ON CONFLICT(id) DO UPDATE SET "
      "info=excluded.info, last_seen=excluded.last_seen, status='idle'", (wid, json.dumps(info), time.time()))
    return {"ok": True, "poll_interval_s": 5}


@app.post("/api/workers/{wid}/heartbeat")
async def heartbeat(wid: str, req: Request, x_ltx_token: str | None = Header(None)) -> dict:
    check_token(x_ltx_token)
    hb = await req.json()
    if not q1("SELECT id FROM workers WHERE id=?", (wid,)):
        raise HTTPException(404, "unknown worker")
    q("UPDATE workers SET last_seen=?, status=?, task_id=?, progress=? WHERE id=?",
      (time.time(), hb.get("status"), hb.get("task_id"), hb.get("progress"), wid))
    cancel = False
    if hb.get("task_id"):
        t = q1("SELECT * FROM tasks WHERE id=?", (hb["task_id"],))
        if t:
            cancel = bool(t["cancel"]) or t["worker_id"] != wid or t["status"] != "claimed"
            if not cancel:
                q("UPDATE tasks SET lease=?, progress=? WHERE id=?", (time.time() + LEASE_S, hb.get("progress"), t["id"]))
    return {"ok": True, "cancel": cancel}


@app.post("/api/tasks/claim", response_model=None)
async def claim(req: Request, x_ltx_token: str | None = Header(None)) -> Response | dict:
    check_token(x_ltx_token)
    wid = (await req.json())["worker_id"]
    w = q1("SELECT info FROM workers WHERE id=?", (wid,))
    if not w:
        raise HTTPException(404, "unknown worker")
    presets = json.loads(w["info"]).get("presets", [])
    with _lock:
        rows = q("SELECT t.* FROM tasks t JOIN projects p ON p.id=t.project_id JOIN shots s ON "
                 "s.project_id=t.project_id AND s.shot_id=t.shot_id WHERE t.status='ready' "
                 "ORDER BY p.created, s.idx")
        def can_run(r) -> bool:
            if json.loads(r["params"])["preset"] not in presets:
                return False
            return "ingredients" in presets or not json.loads(r["inputs"] or "{}").get("ingredients")
        task = next((r for r in rows if can_run(r)), None)
        if task is None:
            return Response(status_code=204)
        now = time.time()
        q("UPDATE tasks SET status='claimed', worker_id=?, claimed=?, lease=?, progress='claimed' WHERE id=?",
          (wid, now, now + LEASE_S, task["id"]))
    q("UPDATE workers SET status='busy', task_id=?, last_seen=? WHERE id=?", (task["id"], now, wid))
    return {"task_id": task["id"], "project_id": task["project_id"], "shot_id": task["shot_id"],
            "params": json.loads(task["params"]), "inputs": json.loads(task["inputs"])}


@app.post("/api/tasks/{tid}/complete")
async def complete(tid: str, worker_id: str = Form(...), result: str = Form("{}"), video: UploadFile = File(...),
                   sheet: UploadFile | None = File(None), log: UploadFile | None = File(None),
                   x_ltx_token: str | None = Header(None)) -> dict:
    check_token(x_ltx_token)
    t = q1("SELECT * FROM tasks WHERE id=?", (tid,))
    if not t:
        raise HTTPException(404, "unknown task")
    if t["status"] == "cancelled" or t["cancel"]:
        return {"ok": True, "ignored": "task was cancelled"}
    out = pdir(t["project_id"]) / "shots" / t["shot_id"] / f"v{t['version']}"
    out.mkdir(parents=True, exist_ok=True)
    for up, name in ((video, "video.mp4"), (sheet, "sheet.jpg"), (log, "log.txt")):
        if up is not None:
            with open(out / name, "wb") as f:
                shutil.copyfileobj(up.file, f)
    try:
        res = json.loads(result)
    except json.JSONDecodeError:
        res = {}
    info = ltxgen.probe(out / "video.mp4")
    if not info.get("duration_s"):
        q("UPDATE tasks SET status='failed', error='uploaded video is not playable', finished=? WHERE id=?",
          (time.time(), tid))
        raise HTTPException(400, "uploaded video is not playable")
    if not (out / "sheet.jpg").exists():
        try:
            ltxgen.make_sheet(out / "video.mp4", out / "sheet.jpg")
        except subprocess.CalledProcessError:
            pass
    res.update(video_path=str(out / "video.mp4"), worker_id=worker_id, video_info=info)
    (out / "result.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
    q("UPDATE tasks SET status='done', finished=?, result=?, progress='done' WHERE id=?",
      (time.time(), json.dumps(res), tid))
    q("UPDATE workers SET completed=completed+1, status='idle', task_id=NULL WHERE id=?", (worker_id,))
    return {"ok": True}


@app.post("/api/tasks/{tid}/fail")
async def fail(tid: str, req: Request, x_ltx_token: str | None = Header(None)) -> dict:
    check_token(x_ltx_token)
    body = await req.json()
    err = {"error": body.get("error"), "hint": body.get("hint"), "log_tail": body.get("log_tail")}
    q("UPDATE tasks SET status='failed', finished=?, error=? WHERE id=? AND status!='cancelled'",
      (time.time(), json.dumps(err), tid))
    q("UPDATE workers SET status='idle', task_id=NULL WHERE id=?", (body.get("worker_id"),))
    return {"ok": True}


@app.get("/api/files/{pid}/{path:path}", response_model=None)
def files(pid: str, path: str) -> FileResponse:
    base = pdir(pid).resolve()
    target = (base / path).resolve()
    if base not in target.parents or not target.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(target)


# ---- dashboard endpoints
@app.get("/api/config")
def config() -> dict:
    return {"presets": PRESETS, "aspects": ASPECTS, "token_file": str(CONFIG_PATH),
            "claude_available": _claude_available(), "local_models": _ollama_models(), "local_model": LOCAL_MODEL, "scenario_prompt": (HERE / "scenario_prompt.md").read_text("utf-8")}


@app.post("/api/projects")
async def post_project(req: Request) -> dict:
    return {"project_id": create_project(await req.json())}


@app.get("/api/projects")
def list_projects() -> list[dict]:
    out = []
    for p in q("SELECT * FROM projects ORDER BY created DESC"):
        counts: dict[str, int] = {}
        for r in q("SELECT t.status FROM shots s JOIN tasks t ON t.id=s.task_id WHERE s.project_id=?", (p["id"],)):
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        out.append({"id": p["id"], "title": p["title"], "created": p["created"], "status": p["status"],
                    "preset": json.loads(p["storyboard"]).get("preset", "draft"), "counts": counts,
                    "final": f"/api/files/{p['id']}/final.mp4" if p["status"] == "done" else None})
    return out


def _ref_url(pid: str, path: str | None, sid: str | None = None) -> str | None:
    if not path:
        return None
    pth = Path(path)
    copied = pdir(pid) / "inputs" / f"{sid}_image{pth.suffix.lower()}"
    if sid and copied.exists():
        return file_url(pid, copied)
    try:
        return file_url(pid, pth.resolve())
    except ValueError:
        return f"/api/uploads/{pth.name}" if pth.parent.resolve() == (DATA / "uploads").resolve() else None


@app.get("/api/projects/{pid}")
def get_project(pid: str) -> dict:
    p = q1("SELECT * FROM projects WHERE id=?", (pid,))
    if not p:
        raise HTTPException(404, "no such project")
    shots = []
    sb_shots = {x["id"]: x for x in json.loads(p["storyboard"])["shots"]}
    for s in q("SELECT * FROM shots WHERE project_id=? ORDER BY idx", (pid,)):
        t = q1("SELECT * FROM tasks WHERE id=?", (s["task_id"],))
        res = json.loads(t["result"]) if t and t["result"] else None
        vdir = pdir(pid) / "shots" / s["shot_id"] / f"v{s['version']}"
        shots.append({
            "id": s["shot_id"], "prompt": s["prompt"], "params": json.loads(s["params"]), "dep": s["dep"] or None,
            "version": s["version"], "task_id": s["task_id"], "status": t["status"] if t else None,
            "worker": t["worker_id"] if t else None, "progress": t["progress"] if t else None,
            "error": json.loads(t["error"]) if t and t["error"] and t["error"].startswith("{") else (t["error"] if t else None),
            "elapsed_s": round(t["finished"] - t["claimed"], 1) if t and t["finished"] and t["claimed"] else None,
            "video": file_url(pid, vdir / "video.mp4") if res else None,
            "ref_image": _ref_url(pid, sb_shots.get(s["shot_id"], {}).get("image"), s["shot_id"]),
            "ref_strength": sb_shots.get(s["shot_id"], {}).get("image_strength", 1.0),
            "sheet": file_url(pid, vdir / "sheet.jpg") if res and (vdir / "sheet.jpg").exists() else None,
            "versions": sorted(int(d.name[1:]) for d in (vdir.parent.glob("v*") if vdir.parent.exists() else [])
                               if (d / "video.mp4").exists()),
        })
    final = pdir(pid) / "final.mp4"
    return {"id": pid, "title": p["title"], "status": p["status"], "error": p["error"], "created": p["created"],
            "storyboard": json.loads(p["storyboard"]), "shots": shots,
            "final": file_url(pid, final) if p["status"] == "done" and final.exists() else None,
            "final_sheet": file_url(pid, pdir(pid) / "final_sheet.jpg") if (pdir(pid) / "final_sheet.jpg").exists() else None,
            "final_info": json.loads(p["final"]) if p["final"] else None}


@app.post("/api/projects/{pid}/shots/{sid}/redo")
async def redo(pid: str, sid: str, req: Request) -> dict:
    try:
        body = await req.json() if (await req.body()) else {}
    except ValueError as e:
        raise HTTPException(400, f"invalid JSON body: {e}") from e
    return _redo(pid, sid, body)


def _redo(pid: str, sid: str, body: dict) -> dict:
    s = q1("SELECT * FROM shots WHERE project_id=? AND shot_id=?", (pid, sid))
    if not s:
        raise HTTPException(404, "no such shot")
    p = q1("SELECT * FROM projects WHERE id=?", (pid,))
    sb = json.loads(p["storyboard"])
    params = json.loads(s["params"])
    shot = next(x for x in sb["shots"] if x["id"] == sid)
    if body.get("style") is not None:  # per-shot style override (F14: Earth scenes pulled into the ISS by the film style)
        shot["style"] = body["style"]
    if body.get("prompt") or body.get("style") is not None:
        shot["prompt"] = (body.get("prompt") or shot["prompt"]).strip()
        style = (shot.get("style", sb.get("style")) or "").strip()
        params["prompt"] = shot["prompt"] + (f" {style}" if style else "")
        q("UPDATE shots SET prompt=? WHERE project_id=? AND shot_id=?", (shot["prompt"], pid, sid))
    if body.get("seed") is not None:
        params["seed"] = shot["seed"] = int(body["seed"])
    if body.get("seconds"):
        params["seconds"] = shot["seconds"] = float(body["seconds"])
    for key in ("image", "end_image"):  # "" clears a reference image
        if key in body:
            if body[key]:
                shot[key] = body[key]
            else:
                shot.pop(key, None)
    if body.get("image_strength") is not None:
        shot["image_strength"] = float(body["image_strength"])
    if body.get("ingredients"):  # {"sheet", "desc", "strength"}: replace the reference sheet / its description
        shot["ingredients"] = {**shot.get("ingredients", {}), **body["ingredients"]}
    elif "ingredients" in body:  # null / {} -> plain shot without a reference sheet
        shot.pop("ingredients", None)
    q("UPDATE shots SET params=? WHERE project_id=? AND shot_id=?", (json.dumps(params), pid, sid))
    q("UPDATE projects SET storyboard=?, status='rendering', error=NULL WHERE id=?", (json.dumps(sb), pid))
    old = q1("SELECT * FROM tasks WHERE id=?", (s["task_id"],))
    if old and old["status"] in ("ready", "pending", "claimed"):
        q("UPDATE tasks SET status='cancelled', cancel=1 WHERE id=?", (old["id"],))
    tid = create_task(pid, sid, s["version"] + 1, params, static_inputs(pid, shot, params), bool(s["dep"]))
    return {"ok": True, "task_id": tid}


IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}


def _save_upload(up: UploadFile, dest_dir: Path, stem: str) -> Path:
    ext = Path(up.filename or "").suffix.lower()
    if ext not in IMAGE_EXT:
        raise HTTPException(400, f"image must be one of {sorted(IMAGE_EXT)}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{stem}_{new_id()}{ext}"
    with open(dest, "wb") as f:
        shutil.copyfileobj(up.file, f)
    return dest


@app.post("/api/uploads")
def upload_image(file: UploadFile = File(...)) -> dict:
    """Reference picture for a storyboard that is not created yet; returns a server path for shot.image."""
    dest = _save_upload(file, DATA / "uploads", "ref")
    return {"path": str(dest), "url": f"/api/uploads/{dest.name}"}


@app.get("/api/uploads/{name}", response_model=None)
def get_upload(name: str) -> FileResponse:
    f = (DATA / "uploads" / name).resolve()
    if f.parent != (DATA / "uploads").resolve() or not f.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(f)


@app.post("/api/projects/{pid}/shots/{sid}/image")
def shot_image(pid: str, sid: str, file: UploadFile = File(...), strength: float = Form(1.0),
               role: str = Form("start")) -> dict:
    """Attach a reference picture to a shot (start frame, or guide for the last frame) and re-render it."""
    if role not in ("start", "end"):
        raise HTTPException(400, "role must be start or end")
    if not 0.1 <= strength <= 1.0:
        raise HTTPException(400, "strength must be 0.1-1.0")
    dest = _save_upload(file, pdir(pid) / "inputs", f"{sid}_upload")
    body = {"image" if role == "start" else "end_image": str(dest)}
    if role == "start":
        body["image_strength"] = strength
    return _redo(pid, sid, body)


@app.post("/api/projects/{pid}/shots/{sid}/use_version")
def use_version(pid: str, sid: str, body: dict = Body(...)) -> dict:
    """Make an earlier finished version of a shot the current one (e.g. a redo came out worse)."""
    t = q1("SELECT id FROM tasks WHERE project_id=? AND shot_id=? AND version=? AND status='done'",
           (pid, sid, int(body.get("version", 0))))
    if not t:
        raise HTTPException(404, "no finished task for that version")
    q("UPDATE shots SET version=?, task_id=? WHERE project_id=? AND shot_id=?", (int(body["version"]), t["id"], pid, sid))
    q("UPDATE projects SET status='rendering' WHERE id=?", (pid,))  # auto-stitch again with this version
    return {"ok": True}


@app.post("/api/projects/{pid}/stitch")
def post_stitch(pid: str) -> dict:
    return stitch_project(pid)


@app.post("/api/projects/{pid}/finalize")
async def finalize(pid: str, req: Request) -> dict:
    preset = (await req.json()).get("preset", "standard")
    if preset not in PRESETS:
        raise HTTPException(400, "bad preset")
    p = q1("SELECT * FROM projects WHERE id=?", (pid,))
    if not p:
        raise HTTPException(404, "no such project")
    sb = json.loads(p["storyboard"])
    for s in q("SELECT * FROM shots WHERE project_id=?", (pid,)):
        shot = next(x for x in sb["shots"] if x["id"] == s["shot_id"])
        shot["seed"] = json.loads(s["params"])["seed"]
        shot.pop("preset", None)
    sb["preset"] = preset
    sb["title"] = re.sub(r" \[(draft|standard|quality)\]$", "", sb.get("title") or "Untitled") + f" [{preset}]"
    return {"project_id": create_project(sb)}


@app.delete("/api/projects/{pid}")
def delete_project(pid: str) -> dict:
    q("UPDATE tasks SET status='cancelled', cancel=1 WHERE project_id=? AND status IN ('ready','pending','claimed')", (pid,))
    q("DELETE FROM shots WHERE project_id=?", (pid,))
    q("DELETE FROM projects WHERE id=?", (pid,))
    shutil.rmtree(pdir(pid), ignore_errors=True)
    return {"ok": True}


@app.post("/api/tasks/{tid}/cancel")
def cancel_task(tid: str) -> dict:
    q("UPDATE tasks SET status='cancelled', cancel=1 WHERE id=? AND status IN ('ready','pending','claimed')", (tid,))
    return {"ok": True}


@app.get("/api/workers")
def workers() -> list[dict]:
    now = time.time()
    out = []
    for w in q("SELECT * FROM workers ORDER BY id"):
        info = json.loads(w["info"] or "{}")
        online = now - (w["last_seen"] or 0) < OFFLINE_S
        out.append({"id": w["id"], "hostname": info.get("hostname"), "gpu": info.get("gpu"),
                    "presets": info.get("presets"), "status": w["status"] if online else "offline",
                    "task_id": w["task_id"] if online else None, "progress": w["progress"] if online else None,
                    "last_seen_s": round(now - (w["last_seen"] or 0)), "completed": w["completed"]})
    return out


@app.get("/api/queue")
def queue() -> dict:
    counts = {r["status"]: r["n"] for r in q("SELECT status, COUNT(*) n FROM tasks GROUP BY status")}
    return {"counts": counts}


# ---- scenario → storyboard: local Gemma via Ollama (default) or Claude (optional)
OLLAMA_URL = CONFIG.get("ollama_url", "http://localhost:11434")
LOCAL_MODEL = CONFIG.get("local_model", "gemma4:12b")
STORYBOARD_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"}, "preset": {"type": "string", "enum": list(PRESETS)},
        "aspect": {"type": "string", "enum": list(ASPECTS)}, "seed": {"type": "integer"},
        "seconds": {"type": "number"}, "transition": {"type": "string", "enum": ["cut", "fade"]},
        "fade_seconds": {"type": "number"}, "style": {"type": "string"},
        "characters": {"type": "object", "additionalProperties": {"type": "string"}},
        "shots": {"type": "array", "minItems": 1, "items": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "seconds": {"type": "number"}, "prompt": {"type": "string"},
                           "continue_from_previous": {"type": "boolean"}},
            "required": ["id", "seconds", "prompt", "continue_from_previous"]}},
    },
    "required": ["title", "preset", "aspect", "seed", "seconds", "transition", "style", "characters", "shots"],
}


def _claude_available() -> bool:
    import os
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _ollama_models() -> list[str]:
    import urllib.request
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=2) as r:
            return [m["name"] for m in json.load(r).get("models", [])]
    except Exception:  # noqa: BLE001
        return []


def _local_gpu_busy() -> bool:
    """True if a worker on this PC is rendering right now (then Gemma runs on CPU to leave VRAM alone)."""
    import socket
    host = socket.gethostname().lower()
    for w in q("SELECT info, status, last_seen FROM workers"):
        info = json.loads(w["info"] or "{}")
        if (info.get("hostname") or "").lower() == host and w["status"] == "busy" and time.time() - w["last_seen"] < OFFLINE_S:
            return True
    return False


def _write_with_ollama(prompt: str, model: str) -> str:
    import urllib.request
    options = {"temperature": 0.7, "num_ctx": 16384}
    if _local_gpu_busy():
        options["num_gpu"] = 0  # CPU only: don't steal VRAM from the running video render
    req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", method="POST", headers={"Content-Type": "application/json"},
                                 data=json.dumps({"model": model, "stream": False, "format": STORYBOARD_SCHEMA,
                                                  "keep_alive": 0, "think": False, "options": options,
                                                  "messages": [{"role": "user", "content": prompt}]}).encode())
    with urllib.request.urlopen(req, timeout=1800) as r:
        return json.load(r)["message"]["content"]


def _write_with_claude(prompt: str) -> str:
    import anthropic
    client = anthropic.Anthropic()
    with client.beta.messages.stream(
        model="claude-opus-5-5",
        max_tokens=32000,
        output_config={"effort": "high"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason == "refusal":
        raise HTTPException(422, "Claude declined this scenario")
    return "".join(b.text for b in msg.content if b.type == "text")


@app.post("/api/scenario")
def scenario(body: dict = Body(...)) -> dict:
    text = (body.get("scenario") or "").strip()
    if not text:
        raise HTTPException(400, "scenario is empty")
    engine = body.get("engine", "local")
    prompt = (HERE / "scenario_prompt.md").read_text("utf-8") + text
    if body.get("notes"):
        prompt += f"\n\nExtra direction: {body['notes']}"
    try:
        if engine == "claude":
            out = _write_with_claude(prompt)
        else:
            model = body.get("model") or LOCAL_MODEL
            if model not in _ollama_models():
                raise HTTPException(503, f"local model {model} not available in Ollama ({OLLAMA_URL}); run: ollama pull {model}")
            out = _write_with_ollama(prompt, model)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"{engine} request failed: {e}") from e
    m = re.search(r"\{.*\}", out, re.S)
    try:
        sb = json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        sb = None
    if not sb or not sb.get("shots"):
        raise HTTPException(502, f"{engine} did not return a valid storyboard; try again")
    for i, shot in enumerate(sb["shots"]):
        shot["id"] = shot.get("id") or f"s{i + 1:02d}"
    return {"storyboard": sb, "engine": engine}


# --------------------------------------------------------------------------- peer distribution (PROTOCOL v1.1)
PIECE = 64 * 1024 * 1024
DIST_CACHE = DATA / "dist_cache.json"
_dist: dict = {"manifest": None, "paths": {}}
_CODE_SKIP = {".venv", ".git", "__pycache__", "storyboards", "build", "node_modules", ".pytest_cache", ".ruff_cache"}


def _lan_ip() -> str:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _dist_sources() -> dict[str, tuple[Path, str]]:
    """published path -> (local file, kind). Only complete files (HF partials live in .cache)."""
    root = ltxgen.ROOT
    out: dict[str, tuple[Path, str]] = {}
    for f in sorted((root / "models" / "ltx-2.5").rglob("*.safetensors")):
        if ".cache" not in f.parts:
            out[f.relative_to(root).as_posix()] = (f, "model")
    for top in ("LTX-2", "ltxgen"):
        for f in sorted((root / top).rglob("*")):
            rel = f.relative_to(root)
            if f.is_file() and not (set(rel.parts) & _CODE_SKIP) and f.suffix not in (".pyc",) \
                    and f.name not in ("jobs.json",):
                out[rel.as_posix()] = (f, "code")
    tools = {"uv.exe": shutil.which("uv"), "ffmpeg.exe": shutil.which("ffmpeg"), "ffprobe.exe": shutil.which("ffprobe")}
    for pyd in (root / "LTX-2" / ".venv" / "Lib" / "site-packages").glob("nvfp4_cpp*.pyd"):
        tools[pyd.name] = str(pyd)
    for name, src in tools.items():
        if src:
            out[f"tools/{name}"] = (Path(src), "tool")
    return out


def _hash_pieces(path: Path) -> list[str]:
    import hashlib
    pieces = []
    with open(path, "rb") as f:
        while chunk := f.read(PIECE):
            pieces.append(hashlib.sha256(chunk).hexdigest())
    return pieces or [hashlib.sha256(b"").hexdigest()]


def _env_info() -> dict:
    cfg = ltxgen.ROOT / "LTX-2" / ".venv" / "pyvenv.cfg"
    py = next((ln.split("=", 1)[1].strip() for ln in cfg.read_text().splitlines() if ln.startswith("version_info")),
              "3.13") if cfg.exists() else "3.13"
    return {"python": ".".join(py.split(".")[:2]), "torch": "2.13.0+cu132", "triton_windows": "3.8.0.post29",
            "ltx2_commit": "e98f6d9"}


def build_manifest() -> None:
    import hashlib
    cache = json.loads(DIST_CACHE.read_text("utf-8")) if DIST_CACHE.exists() else {}
    files, paths, changed = [], {}, False
    for rel, (src, kind) in _dist_sources().items():
        st = src.stat()
        c = cache.get(rel)
        if not c or c["size"] != st.st_size or c["mtime"] != st.st_mtime or c.get("src") != str(src):
            c = {"size": st.st_size, "mtime": st.st_mtime, "src": str(src), "pieces": _hash_pieces(src)}
            cache[rel] = c
            changed = True
        files.append({"path": rel, "size": c["size"], "kind": kind, "pieces": c["pieces"]})
        paths[rel] = src
    for rel in [r for r in cache if r not in paths]:
        del cache[rel]
        changed = True
    if changed:
        DIST_CACHE.write_text(json.dumps(cache), encoding="utf-8")
    digest = hashlib.sha256(json.dumps([(f["path"], f["size"], f["pieces"]) for f in files]).encode()).hexdigest()[:12]
    _dist["manifest"] = {"version": digest, "piece_size": PIECE, "env": _env_info(), "files": files}
    _dist["paths"] = paths


def manifest_loop() -> None:
    while True:
        try:
            build_manifest()
        except Exception as e:  # noqa: BLE001
            print("[manifest]", e, file=sys.stderr)
        time.sleep(120)


@app.get("/api/dist/manifest")
def dist_manifest(x_ltx_token: str | None = Header(None)) -> dict:
    check_token(x_ltx_token)
    if not _dist["manifest"]:
        raise HTTPException(503, "building manifest")
    return _dist["manifest"]


@app.get("/api/dist/status")
def dist_status() -> dict:
    m = _dist["manifest"]
    if not m:
        return {"ready": False}
    by_kind: dict[str, float] = {}
    for f in m["files"]:
        by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + f["size"]
    have = {f["path"] for f in m["files"]}
    return {"ready": True, "version": m["version"], "files": len(m["files"]),
            "gb": {k: round(v / 1e9, 1) for k, v in by_kind.items()},
            "missing_models": [k for k, rel in ltxgen.FILES.items() if f"models/ltx-2.5/{rel}" not in have]}


def _server_base(req: Request) -> str:
    host = req.headers.get("host", f"localhost:{CONFIG.get('port', 7860)}")
    name, _, port = host.partition(":")
    if name in ("localhost", "127.0.0.1", "::1", "[::1]"):
        host = f"{_lan_ip()}:{port or CONFIG.get('port', 7860)}"
    return f"http://{host}"


@app.get("/api/dist/peers")
def dist_peers(req: Request, x_ltx_token: str | None = Header(None)) -> list[dict]:
    check_token(x_ltx_token)
    peers = [{"id": "main", "url": f"{_server_base(req)}/api/dist/file/"}]
    version = (_dist["manifest"] or {}).get("version")
    now = time.time()
    for w in q("SELECT id, info, last_seen FROM workers"):
        info = json.loads(w["info"] or "{}")
        if not info.get("seed_url") or now - (w["last_seen"] or 0) >= OFFLINE_S:
            continue
        if version and info.get("seed_version") == version:
            peers.append({"id": w["id"], "url": info["seed_url"]})
        else:  # code changed on main since this worker joined: its models/tools are still identical
            peers.append({"id": w["id"], "url": info["seed_url"], "kinds": ["model", "tool"]})
    return peers


@app.get("/api/dist/file/{path:path}", response_model=None)
def dist_file(path: str, x_ltx_token: str | None = Header(None)) -> FileResponse:
    check_token(x_ltx_token)
    src = _dist["paths"].get(path)
    if src is None or not src.is_file():
        raise HTTPException(404, "not in manifest")
    return FileResponse(src, media_type="application/octet-stream")


@app.get("/join", response_model=None)
def join_file(req: Request) -> Response:
    tpl = HERE / "join_template.bat"
    if not tpl.exists():
        raise HTTPException(503, "join_template.bat not available yet")
    bat = tpl.read_text("utf-8").replace("__SERVER__", _server_base(req)).replace("__TOKEN__", CONFIG["token"])
    return Response(bat.replace("\r\n", "\n").replace("\n", "\r\n").encode("ascii", "replace"),
                    media_type="application/octet-stream",
                    headers={"Content-Disposition": 'attachment; filename="join-ltx-studio.bat"'})


@app.get("/join.sh", response_model=None)
def join_sh(req: Request) -> Response:
    """Linux/macOS render PC: `curl -fsSL http://MAINPC:7860/join.sh | bash` - clones LTX Studio, installs the engine
    and models (Hugging Face; set HF_TOKEN first for the gated consistency model) and starts a worker for this farm."""
    repo = CONFIG.get("repo_url", "https://github.com/UAEpro/ltx-studio")
    sh = f"""#!/usr/bin/env bash
set -euo pipefail
DIR="${{LTX_DIR:-$HOME/ltx-studio}}"
command -v git >/dev/null || {{ echo "git is required (Linux: sudo apt install git, macOS: xcode-select --install)"; exit 1; }}
[ -d "$DIR/.git" ] || git clone {repo} "$DIR"
git -C "$DIR" pull --ff-only || true
bash "$DIR/ltxgen/studio/install.sh" --worker "{_server_base(req)}" "{CONFIG['token']}"
"""
    return Response(sh.encode(), media_type="text/x-shellscript")


def main() -> None:
    import uvicorn
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=CONFIG.get("port", 7860))
    a = ap.parse_args()
    CONFIG["port"] = a.port  # runtime only (join links, worker start); not written back
    threading.Thread(target=scheduler, daemon=True).start()
    threading.Thread(target=manifest_loop, daemon=True).start()
    films.start_background()
    print(f"LTX Studio on http://localhost:{a.port}  (worker token in {CONFIG_PATH})")
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
